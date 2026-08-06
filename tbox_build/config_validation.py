"""Configuration validation for TBOX Build (BUILD CR-003 §8).

Implements the BUILD-owned configuration checks that run after the platform
config overlay is staged into install-root and before packaging:

  * **common.yaml single source** (§4.1): the formal ``/etc/tbox/common.yaml``
    comes only from ``configs/<platform>/rootfs/etc/tbox/common.yaml``;
    ``conf.d/*.yaml`` must not inline a top-level ``common:`` key.
  * **Schema / parse validation** (§8.1): each final ``conf.d/<service>.yaml``
    is validated against the schema or controlled check command declared in
    the service metadata; incompatible fields, missing required fields, type
    errors and corrupted YAML fail the build before packaging.
  * **Secret material scanning** (§8.2): scans ``configs/<platform>/rootfs/**``,
    ``install-root/**`` and release packages for forbidden extensions, PEM
    private-key markers, YAML/JSON sensitive fields and provisioning-specific
    paths. Findings report only the rule id, file and field path -- never the
    matched value.
  * **File permission normalization** (§8.3): release-managed non-secret
    configuration files are normalized to 0644.

The framework-config deep common schema/merge semantics are owned by
framework (CR-003 §12); BUILD only enforces the single-source and structural
rules above.
"""

from __future__ import annotations

import json
import os
import re
import stat
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

import jsonschema
import yaml

from .errors import ConfigValidationError
from .manifest import Service, ServiceManifest


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: Version of the secret-scan rule set; recorded in the artifact manifest so
#: that a scan result is reproducible against a known rule set.
SECRET_RULESET_VERSION = "cr003-v1"

#: File extensions that must never appear in BUILD-owned config payloads.
FORBIDDEN_EXTENSIONS: frozenset[str] = frozenset({".key", ".p12", ".pfx"})

#: PEM private-key header markers (scanned as raw bytes).
PEM_PRIVATE_KEY_MARKERS: tuple[bytes, ...] = (
    b"BEGIN PRIVATE KEY",
    b"BEGIN RSA PRIVATE KEY",
    b"BEGIN EC PRIVATE KEY",
)

#: YAML/JSON field names (matched case-insensitively, recursively) whose
#: presence indicates a likely secret. A non-placeholder value is a finding.
SENSITIVE_FIELD_NAMES: frozenset[str] = frozenset({
    "private_key",
    "client_secret",
    "password",
    "token",
    "kek",
})

#: Path fragments (relative to a scan root, posix style) that identify
#: provisioning-owned locations; their presence in BUILD payloads is a finding.
PROVISIONING_PATH_FRAGMENTS: tuple[str, ...] = (
    "etc/tbox/credentials/",
    "etc/tbox/device.yaml",
)

#: File basenames reserved for provisioning secrets.
PROVISIONING_FILE_NAMES: frozenset[str] = frozenset({
    ".soft_kek",
    "device_identity",
})

#: Explicit test-placeholder values allowed in sensitive fields. A sensitive
#: field whose scalar value matches one of these (case-insensitive) is not a
#: finding. Real secrets must never be listed here.
PLACEHOLDER_ALLOWLIST: frozenset[str] = frozenset({
    "",
    "test-placeholder",
    "redacted",
    "changeme",
    "placeholder",
    "none",
    "null",
    "<<placeholder>>",
})

#: Default normalized mode for release-managed non-secret config files (§8.3).
DEFAULT_CONFIG_MODE = 0o644


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------


@dataclass
class SecretFinding:
    """A single secret-scan hit.

    ``value`` is intentionally never populated; findings report only the rule,
    file and field path so logs and manifests never leak matched secrets.
    """

    rule_id: str
    file: str
    field_path: str | None = None  # None for file-level rules (extension, PEM)
    reason: str = ""


@dataclass
class SecretScanReport:
    """Aggregate secret-scan result for a set of files."""

    ruleset_version: str = SECRET_RULESET_VERSION
    findings: list[SecretFinding] = field(default_factory=list)
    files_scanned: int = 0

    @property
    def passed(self) -> bool:
        return not self.findings


@dataclass
class SchemaCheckResult:
    """Schema / parse validation result for a single service config."""

    service_id: str
    target_path: str
    schema_source: str | None = None
    status: str = "skipped"  # pass | fail | skipped
    errors: list[str] = field(default_factory=list)


@dataclass
class ConfigValidationReport:
    """Aggregate validation report for the whole overlay + staging pass."""

    common_ok: bool = True
    conf_d_ok: bool = True
    schema_checks: list[SchemaCheckResult] = field(default_factory=list)
    secret_scan: SecretScanReport = field(default_factory=SecretScanReport)
    permission_normalizations: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return (
            self.common_ok
            and self.conf_d_ok
            and self.secret_scan.passed
            and all(s.status != "fail" for s in self.schema_checks)
            and not self.errors
        )


# ---------------------------------------------------------------------------
# Secret scanning
# ---------------------------------------------------------------------------


def scan_secrets(paths: Iterable[Path]) -> SecretScanReport:
    """Scan an iterable of file paths for secret material (§8.2).

    Returns a :class:`SecretScanReport`. Findings never carry the matched
    value. Scans: forbidden extensions, PEM private-key markers, YAML/JSON
    sensitive fields (with placeholder allowlist) and provisioning-specific
    paths/filenames.
    """
    report = SecretScanReport()
    for path in paths:
        if not path.is_file() or path.is_symlink():
            # Skip symlinks and non-regular files; the overlay stager rejects
            # them earlier, but scan_secrets is also called on arbitrary trees.
            if path.is_symlink():
                report.files_scanned += 1
            continue
        report.files_scanned += 1
        _scan_one_file(path, report)
    return report


def _scan_one_file(path: Path, report: SecretScanReport) -> None:
    rel = str(path)
    suffix = path.suffix.lower()

    # Rule: forbidden extension
    if suffix in FORBIDDEN_EXTENSIONS:
        report.findings.append(SecretFinding(
            rule_id="SECRET-FORBIDDEN-EXT",
            file=rel,
            reason=f"forbidden extension '{suffix}' (allowed only in provisioning)",
        ))

    # Rule: provisioning-specific path / filename
    posix_rel = path.as_posix()
    if any(frag in posix_rel for frag in PROVISIONING_PATH_FRAGMENTS):
        report.findings.append(SecretFinding(
            rule_id="SECRET-PROVISIONING-PATH",
            file=rel,
            reason="file under a provisioning-owned path must not be packaged",
        ))
    if path.name in PROVISIONING_FILE_NAMES:
        report.findings.append(SecretFinding(
            rule_id="SECRET-PROVISIONING-FILE",
            file=rel,
            reason=f"provisioning-owned filename '{path.name}' must not be packaged",
        ))

    # Read raw bytes for PEM marker scan (works on any file type)
    try:
        raw = path.read_bytes()
    except OSError:
        return

    for marker in PEM_PRIVATE_KEY_MARKERS:
        if marker in raw:
            report.findings.append(SecretFinding(
                rule_id="SECRET-PEM-PRIVATE-KEY",
                file=rel,
                reason=f"PEM private-key marker '{marker.decode()}' detected",
            ))
            break  # one PEM finding per file is enough

    # Rule: YAML/JSON sensitive fields (only for parseable text configs)
    if suffix in (".yaml", ".yml", ".json"):
        _scan_sensitive_fields(path, suffix, report)


def _scan_sensitive_fields(path: Path, suffix: str, report: SecretScanReport) -> None:
    """Walk a YAML/JSON document and flag sensitive field names.

    A sensitive field with a scalar value outside the placeholder allowlist is
    a finding. Complex (dict/list) values in sensitive fields are always
    findings (a secret should never be a structured object).
    """
    try:
        if suffix == ".json":
            data = json.loads(path.read_text(encoding="utf-8"))
        else:
            data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (yaml.YAMLError, json.JSONDecodeError, UnicodeDecodeError, OSError):
        # Parse failure is a schema/parse concern (run_schema_check), not a
        # secret-scan finding. Corrupt files are caught there.
        return
    if data is None:
        return
    _walk_sensitive(data, [], path, report)


def _walk_sensitive(node: Any, trail: list[str], path: Path, report: SecretScanReport) -> None:
    if isinstance(node, dict):
        for key, value in node.items():
            key_str = str(key)
            child_trail = trail + [key_str]
            if key_str.lower() in SENSITIVE_FIELD_NAMES:
                _evaluate_sensitive_field(key_str, value, child_trail, path, report)
                # Do not descend into a sensitive field's structured value;
                # the field itself is the finding.
                continue
            _walk_sensitive(value, child_trail, path, report)
    elif isinstance(node, list):
        for idx, item in enumerate(node):
            _walk_sensitive(item, trail + [f"[{idx}]"], path, report)


def _evaluate_sensitive_field(
    key: str, value: Any, trail: list[str], path: Path, report: SecretScanReport
) -> None:
    field_path = ".".join(trail)
    if isinstance(value, (dict, list)):
        report.findings.append(SecretFinding(
            rule_id="SECRET-SENSITIVE-FIELD",
            file=str(path),
            field_path=field_path,
            reason=f"sensitive field '{key}' has a complex value",
        ))
        return
    scalar = str(value).strip().lower()
    if scalar in PLACEHOLDER_ALLOWLIST:
        return  # explicit test placeholder; not a real secret
    report.findings.append(SecretFinding(
        rule_id="SECRET-SENSITIVE-FIELD",
        file=str(path),
        field_path=field_path,
        reason=f"sensitive field '{key}' has a non-placeholder value",
    ))


# ---------------------------------------------------------------------------
# common.yaml single-source + conf.d inline-common checks
# ---------------------------------------------------------------------------


def validate_common_yaml(common_path: Path) -> list[str]:
    """Validate the formal common.yaml (§4.1).

    Returns a list of error strings (empty = OK). Checks that the file is
    parseable YAML and has a top-level ``common`` mapping.
    """
    errors: list[str] = []
    if not common_path.is_file():
        errors.append(f"formal common.yaml not found: {common_path}")
        return errors
    try:
        data = yaml.safe_load(common_path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        errors.append(f"common.yaml parse error: {exc}")
        return errors
    if not isinstance(data, dict) or "common" not in data:
        errors.append("common.yaml must have a top-level 'common' mapping")
    elif not isinstance(data["common"], dict):
        errors.append("common.yaml top-level 'common' must be a mapping")
    return errors


def check_conf_d_no_inline_common(conf_d_dir: Path) -> list[str]:
    """Ensure no conf.d/*.yaml inlines a top-level ``common:`` key (§4.1).

    Returns a list of error strings (empty = OK).
    """
    errors: list[str] = []
    if not conf_d_dir.is_dir():
        return errors
    for yaml_file in sorted(conf_d_dir.glob("*.yaml")) + sorted(conf_d_dir.glob("*.yml")):
        try:
            data = yaml.safe_load(yaml_file.read_text(encoding="utf-8"))
        except yaml.YAMLError as exc:
            errors.append(f"{yaml_file}: parse error: {exc}")
            continue
        if isinstance(data, dict) and "common" in data:
            errors.append(
                f"{yaml_file}: top-level 'common:' is forbidden in conf.d "
                f"(common config must come from /etc/tbox/common.yaml)"
            )
    return errors


# ---------------------------------------------------------------------------
# Schema / parse validation
# ---------------------------------------------------------------------------


def run_schema_check(
    service: Service,
    install_root: Path,
    project_root: Path,
) -> SchemaCheckResult:
    """Validate a service's final staged config against its declared schema (§8.1).

    Resolves ``runtime.config_validation`` from the service metadata:

      * ``schema`` -- a JSON Schema (YAML/JSON) path relative to the service
        source root; the final staged config is validated against it.
      * ``check_command`` -- a controlled command (no network, no device write,
        no secret read, with timeout) run against the staged target file.
      * If neither is available or the target file is absent (service not
        built / no config installed), the check is skipped.

    The result never carries config field values -- only error messages from
    the validator.
    """
    cv = service.runtime.config_validation
    target_path = cv.target_path if cv else None
    result = SchemaCheckResult(
        service_id=service.id,
        target_path=target_path or "",
    )
    if cv is None or not target_path:
        result.status = "skipped"
        result.errors.append("no config_validation declared")
        return result

    # Resolve the final staged file (install-root + target_path, strip leading /)
    rel = target_path.lstrip("/")
    staged_file = install_root / rel
    if not staged_file.is_file():
        result.status = "skipped"
        result.errors.append(f"target file not staged: {target_path}")
        return result

    # Parse the final config (catches corruption)
    try:
        if staged_file.suffix == ".json":
            config_data = json.loads(staged_file.read_text(encoding="utf-8"))
        else:
            config_data = yaml.safe_load(staged_file.read_text(encoding="utf-8"))
    except (yaml.YAMLError, json.JSONDecodeError, UnicodeDecodeError, OSError) as exc:
        result.status = "fail"
        result.errors.append(f"config parse error: {exc}")
        return result

    # Controlled check command (no network, no device write, no secret, timeout)
    if cv.check_command:
        result.schema_source = cv.check_command
        return _run_check_command(service, cv.check_command, staged_file, project_root, result)

    # JSON Schema validation
    if cv.schema:
        result.schema_source = cv.schema
        return _run_json_schema_check(service, cv.schema, config_data, project_root, result)

    result.status = "skipped"
    result.errors.append("no schema or check_command declared")
    return result


def _run_json_schema_check(
    service: Service,
    schema_rel: str,
    config_data: Any,
    project_root: Path,
    result: SchemaCheckResult,
) -> SchemaCheckResult:
    src_root = project_root / service.effective_source_dir
    schema_path = (src_root / schema_rel).resolve()
    # Guard against path escape (§5: schema relative to service source root)
    try:
        schema_path.relative_to(src_root.resolve())
    except ValueError:
        result.status = "fail"
        result.errors.append(
            f"schema path '{schema_rel}' escapes service source root"
        )
        return result
    if not schema_path.is_file():
        result.status = "skipped"
        result.errors.append(f"schema file not found: {schema_path}")
        return result
    try:
        schema = yaml.safe_load(schema_path.read_text(encoding="utf-8"))
    except (yaml.YAMLError, OSError) as exc:
        result.status = "fail"
        result.errors.append(f"schema parse error: {exc}")
        return result
    try:
        validator = jsonschema.Draft7Validator(schema)
        errors = sorted(validator.iter_errors(config_data), key=lambda e: list(e.absolute_path))
    except jsonschema.SchemaError as exc:
        result.status = "fail"
        result.errors.append(f"schema itself is invalid: {exc.message}")
        return result
    if errors:
        result.status = "fail"
        for e in errors:
            loc = ".".join(str(p) for p in e.absolute_path) or "(root)"
            result.errors.append(f"{loc}: {e.message}")
        return result
    result.status = "pass"
    return result


def _run_check_command(
    service: Service,
    command: str,
    staged_file: Path,
    project_root: Path,
    result: SchemaCheckResult,
) -> SchemaCheckResult:
    """Run a controlled check command (§5: no network, no device write, no secret).

    The command is run with a stripped environment, a hard timeout and the
    staged file path injected via the ``TBOX_CONFIG_FILE`` env var. A non-zero
    exit or timeout is a validation failure.
    """
    env = {
        "TBOX_CONFIG_FILE": str(staged_file),
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
    }
    try:
        proc = subprocess.run(
            command,
            shell=True,
            capture_output=True,
            text=True,
            env=env,
            cwd=str(project_root),
            timeout=30,
        )
    except subprocess.TimeoutExpired:
        result.status = "fail"
        result.errors.append(f"check command timed out (>30s): {command}")
        return result
    except OSError as exc:
        result.status = "fail"
        result.errors.append(f"check command failed to start: {exc}")
        return result
    if proc.returncode != 0:
        result.status = "fail"
        # stderr may contain diagnostics; keep it but never echo config values
        diag = (proc.stderr or proc.stdout or "").strip()[:500]
        result.errors.append(f"check command exited {proc.returncode}: {diag}")
        return result
    result.status = "pass"
    return result


# ---------------------------------------------------------------------------
# File permission normalization (§8.3)
# ---------------------------------------------------------------------------


def normalize_config_mode(path: Path, mode: int = DEFAULT_CONFIG_MODE) -> bool:
    """Normalize a config file's mode to *mode* (default 0644).

    Returns True if the mode was changed. Symlinks and non-regular files are
    left untouched (the overlay stager rejects them earlier).
    """
    if not path.is_file() or path.is_symlink():
        return False
    st = path.stat()
    target = (st.st_mode & ~0o777) | mode
    if st.st_mode != target:
        path.chmod(target)
        return True
    return False


# ---------------------------------------------------------------------------
# Coordinator
# ---------------------------------------------------------------------------


class ConfigValidator:
    """Orchestrates all BUILD-owned config checks for a build pass.

    Run after the platform overlay is staged into *install_root* and before
    packaging. Raises :class:`ConfigValidationError` on failure.
    """

    def __init__(
        self,
        project_root: Path,
        install_root: Path,
        platform: str,
        service_manifest: ServiceManifest,
    ):
        self.project_root = Path(project_root)
        self.install_root = Path(install_root)
        self.platform = platform
        self.service_manifest = service_manifest
        self.overlay_root = self.project_root / "configs" / platform / "rootfs"

    def validate(self) -> ConfigValidationReport:
        """Run all checks and return an aggregate report."""
        report = ConfigValidationReport()

        # 1. common.yaml single source
        common_path = self.install_root / "etc" / "tbox" / "common.yaml"
        common_errors = validate_common_yaml(common_path)
        report.common_ok = not common_errors
        report.errors.extend(common_errors)

        # 2. conf.d no inline common
        conf_d_dir = self.install_root / "etc" / "tbox" / "conf.d"
        conf_errors = check_conf_d_no_inline_common(conf_d_dir)
        report.conf_d_ok = not conf_errors
        report.errors.extend(conf_errors)

        # 3. secret scan: overlay source + final install-root
        scan_paths: list[Path] = []
        if self.overlay_root.is_dir():
            scan_paths.extend(p for p in self.overlay_root.rglob("*") if p.is_file())
        if self.install_root.is_dir():
            scan_paths.extend(
                p for p in self.install_root.rglob("*")
                if p.is_file() and "etc/tbox" in p.as_posix()
            )
        report.secret_scan = scan_secrets(scan_paths)

        # 4. schema checks per service (only services with config_validation)
        for svc in self.service_manifest:
            if svc.runtime.config_validation is not None:
                report.schema_checks.append(
                    run_schema_check(svc, self.install_root, self.project_root)
                )

        return report

    def normalize_permissions(self, report: ConfigValidationReport) -> None:
        """Normalize modes for release-managed non-secret config files (§8.3).

        Records changed paths in ``report.permission_normalizations``.
        """
        conf_d_dir = self.install_root / "etc" / "tbox" / "conf.d"
        common_path = self.install_root / "etc" / "tbox" / "common.yaml"
        targets: list[Path] = [common_path]
        if conf_d_dir.is_dir():
            targets.extend(
                p for p in conf_d_dir.iterdir() if p.is_file() and p.suffix in (".yaml", ".yml")
            )
        for path in targets:
            if normalize_config_mode(path):
                report.permission_normalizations.append(
                    f"{path.relative_to(self.install_root)} -> 0o{DEFAULT_CONFIG_MODE:o}"
                )
