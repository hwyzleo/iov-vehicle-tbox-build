"""Verification for TBOX Build.

Provides package verification (checksum, manifest integrity, structure)
and staging verification (ELF check, artifact manifest, path conflicts).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any

from .elfcheck import check_staging, check_symbol_integrity_staging
from .errors import TboxBuildError
from .staging import StagingDir, sha256_file


@dataclass
class VerifyResult:
    """Result of a verification run."""

    checks: list[dict[str, Any]] = field(default_factory=list)
    status: str = "pending"  # pending, success, failed
    errors: list[str] = field(default_factory=list)

    def add_check(self, name: str, passed: bool, message: str = "") -> None:
        self.checks.append({
            "name": name,
            "status": "success" if passed else "failed",
            "message": message,
        })
        if not passed:
            self.status = "failed"
            self.errors.append(f"{name}: {message}")

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class Verifier:
    """Verifies packages and staging output."""

    #: On-target systemd unit trees that the payload may populate (service
    #: repositories install units via their own install rules).
    _UNIT_PAYLOAD_DIRS = (
        "usr/lib/systemd/system",
        "lib/systemd/system",
        "etc/systemd/system",
    )

    def __init__(self, staging: StagingDir):
        self.staging = staging

    # -- declared-unit presence gate (CR-006 §3.3) -----------------------

    def _load_service_manifest(self):
        """Load the service manifest; None when this staging has no project."""
        try:
            from .manifest import load_service_manifest
            return load_service_manifest(
                self.staging.project_root / "manifests" / "services.yaml"
            )
        except Exception:
            return None

    @staticmethod
    def _owner_services_from_manifest(manifest: dict[str, Any] | None) -> set[str]:
        """Service ids that actually produced artifacts in this build."""
        if not manifest:
            return set()
        return {
            str(a.get("owner_service", ""))
            for a in manifest.get("artifacts", [])
            if a.get("owner_service")
        }

    @staticmethod
    def _payload_unit_names(install_root: Path) -> set[str]:
        """Unit file basenames present in the staged payload."""
        names: set[str] = set()
        for rel in Verifier._UNIT_PAYLOAD_DIRS:
            d = install_root / rel
            if d.is_dir():
                names.update(p.name for p in d.glob("*.service"))
        return names

    def _missing_declared_units(
        self, payload_units: set[str], owner_services: set[str]
    ) -> tuple[list[tuple[str, str]], str | None]:
        """Declared ``runtime.systemd_units`` missing from the payload.

        Returns ``(missing, skip_reason)``. Only services that actually
        produced artifacts in this build (``owner_service`` in the artifact
        manifest) are checked: a unit declared by a service not in the
        package is not required. When ownership evidence is unavailable the
        gate reports a skip reason instead of guessing — a name-only
        registration must never stand in for the real unit file
        (TBOX-BUILD-DSN-CR-006 §3.3).
        """
        sm = self._load_service_manifest()
        if sm is None:
            return [], "service manifest unavailable; declared-unit gate skipped"
        if not owner_services:
            return [], (
                "artifact manifest unavailable; declared-unit gate skipped "
                "(no ownership evidence)"
            )
        missing: list[tuple[str, str]] = []
        for svc in sm:
            if svc.id not in owner_services:
                continue
            if not svc.runtime.systemd_units:
                continue
            for unit in svc.runtime.systemd_units:
                if unit not in payload_units:
                    missing.append((svc.id, unit))
        return missing, None

    def _add_declared_unit_check(
        self, result: VerifyResult, payload_units: set[str], owner_services: set[str]
    ) -> None:
        """Report the declared-unit gate into *result* (shared by staging/package)."""
        missing, skip = self._missing_declared_units(payload_units, owner_services)
        if skip:
            result.add_check(
                "systemd-units-declared", True,
                f"SKIPPED: {skip}",
            )
            return
        if missing:
            result.add_check(
                "systemd-units-declared", False,
                "declared systemd unit(s) not installed by the locked service "
                "version: " + "; ".join(
                    f"{svc} -> {unit}" for svc, unit in missing
                ) + " (CR-006 §3.3: missing unit is an explicit packaging "
                "failure; the owning repository must install it)",
            )
        else:
            result.add_check(
                "systemd-units-declared", True,
                "All declared systemd units installed by their locked versions",
            )

    def verify_staging(self) -> VerifyResult:
        """Verify the staging directory: ELF check + artifact manifest."""
        result = VerifyResult()

        # 1. Install-root exists
        if not self.staging.install_root.exists():
            result.add_check(
                "install-root-exists", False,
                f"Install-root not found: {self.staging.install_root}",
            )
            result.status = "failed"
            return result
        result.add_check("install-root-exists", True)

        # 2. ELF / pollution check
        try:
            elf_results = check_staging(self.staging.install_root)
            violations = sum(len(r.violations) for r in elf_results)
            warnings = sum(len(r.warnings) for r in elf_results)
            result.add_check(
                "elf-pollution-check",
                violations == 0,
                f"{len(elf_results)} files checked, "
                f"{violations} violation(s), {warnings} warning(s)",
            )
        except Exception as exc:
            result.add_check("elf-pollution-check", False, str(exc))

        # 3. Artifact manifest exists and is valid JSON
        manifest_path = self.staging.manifests_dir / "artifact-manifest.json"
        if not manifest_path.exists():
            result.add_check(
                "artifact-manifest", False,
                f"Artifact manifest not found: {manifest_path}",
            )
        else:
            try:
                with open(manifest_path, encoding="utf-8") as f:
                    manifest = json.load(f)
                artifact_count = len(manifest.get("artifacts", []))
                result.add_check(
                    "artifact-manifest", True,
                    f"{artifact_count} artifact(s) recorded",
                )
            except (json.JSONDecodeError, KeyError) as exc:
                result.add_check("artifact-manifest", False, str(exc))

        # 4. Check for path conflicts in manifest
        if manifest_path.exists():
            try:
                with open(manifest_path, encoding="utf-8") as f:
                    manifest = json.load(f)
                paths: dict[str, str] = {}
                conflicts = 0
                for artifact in manifest.get("artifacts", []):
                    path = artifact.get("path", "")
                    owner = artifact.get("owner_service", "")
                    if path in paths and paths[path] != owner:
                        conflicts += 1
                    else:
                        paths[path] = owner
                result.add_check(
                    "path-conflicts",
                    conflicts == 0,
                    f"{conflicts} conflict(s)" if conflicts else "No conflicts",
                )
            except Exception as exc:
                result.add_check("path-conflicts", False, str(exc))

        # 5. systemd ExecStart binaries are actually installed
        #    Catches unit/binary name mismatches (e.g. ExecStart=/usr/bin/tbox-prov
        #    while the installed daemon is /usr/bin/tbox_prov), which otherwise
        #    only surface at deploy time as a service that exits 127.
        missing = self._missing_execstart_binaries()
        result.add_check(
            "systemd-execstart",
            not missing,
            "All unit ExecStart binaries present"
            if not missing
            else "ExecStart target(s) not found in install-root: "
            + "; ".join(f"{unit} -> {path}" for unit, path in missing),
        )

        # 5b. Declared systemd units must be installed by the locked service
        #     version (CR-006 §3.3): a name-only registration never stands in
        #     for the real unit file. Ownership comes from the staging
        #     artifact manifest so only services in this build are required.
        payload_units = self._payload_unit_names(self.staging.install_root)
        owner_services: set[str] = set()
        staging_manifest = self.staging.manifests_dir / "artifact-manifest.json"
        if staging_manifest.is_file():
            try:
                with open(staging_manifest, encoding="utf-8") as f:
                    owner_services = self._owner_services_from_manifest(
                        json.load(f)
                    )
            except (OSError, json.JSONDecodeError):
                owner_services = set()
        self._add_declared_unit_check(result, payload_units, owner_services)

        # 6. ELF symbol-integrity gate (CR-004 D4, BUILD-REQ-048)
        #    Verifies that undefined dynamic symbols in every staged ELF are
        #    satisfiable by its transitive DT_NEEDED closure, resolved from the
        #    controlled roots in BUILD precedence: SDK staging -> TARGET
        #    dependency staging -> sysroot. Strong unresolved symbols and
        #    missing release dependencies fail; weak undefs are diagnostics.
        try:
            roots = self._symbol_roots()
            sym_results = check_symbol_integrity_staging(
                self.staging.install_root,
                roots=roots,
                missing_library_policy="error",
                exempt_consumers=self._link_exempt_consumers(),
            )
            sym_violations = sum(len(r.violations) for r in sym_results)
            sym_warnings = sum(len(r.warnings) for r in sym_results)
            result.add_check(
                "elf-symbol-integrity",
                sym_violations == 0,
                f"{len(sym_results)} ELF(s) checked, "
                f"{sym_violations} violation(s), {sym_warnings} warning(s) "
                f"(roots: {len(roots)})",
            )
            if sym_violations:
                for r in sym_results:
                    for v in r.violations:
                        result.errors.append(v)
        except Exception as exc:
            result.add_check("elf-symbol-integrity", False, str(exc))

        if result.status == "pending":
            result.status = "success"

        return result

    def _symbol_roots(self) -> list[Path]:
        """Controlled roots for symbol resolution (CR-004 §7.2).

        Precedence: the composed staging set (install-root) -> SDK staging
        dirs -> TARGET dependency staging -> sysroot, matching the CMake
        find-root order (SPEC §5.4).
        """
        from .manifest import Project
        project = Project(self.staging.project_root)
        roots: list[Path] = [self.staging.install_root]
        if self.staging.sdk_root.is_dir():
            roots.extend(sorted(p for p in self.staging.sdk_root.iterdir() if p.is_dir()))
        roots.append(self.staging.dep_staging)
        roots.append(project.sysroot_path)
        return roots

    def _link_exempt_consumers(self) -> frozenset[str]:
        """Basenames of consumers covered by reviewed D2/D4 exemptions.

        A CMake target ``tbox_prov`` maps to a staged artifact whose basename
        equals the target (``tbox_prov``) or its first dotted component
        (``tbox_prov.so``). Exempted consumers have their strong-unresolved
        findings downgraded to warnings (D4 staging corroboration, CR-004 §7.4).
        """
        from .manifest import Project
        project = Project(self.staging.project_root)
        exemptions = project.load_link_exemptions()
        if exemptions is None:
            return frozenset()
        names: set[str] = set()
        for ex in exemptions.exemptions:
            names.add(ex.target)
            names.add(ex.target.split(".")[0])
        return frozenset(names)

    def _missing_execstart_binaries(self) -> list[tuple[str, str]]:
        """Return (unit_name, exec_path) for TBOX ExecStart targets that are
        not present in the staging install-root.

        Only ExecStart binaries whose basename begins with ``tbox`` are
        checked; system tools (``/bin/sh``, ``/usr/bin/env`` ...) are provided
        by the base image and intentionally excluded to avoid false positives.
        """
        root = self.staging.install_root
        unit_dirs = [
            root / "usr" / "lib" / "systemd" / "system",
            root / "lib" / "systemd" / "system",
            root / "etc" / "systemd" / "system",
        ]
        missing: list[tuple[str, str]] = []
        for unit_dir in unit_dirs:
            if not unit_dir.is_dir():
                continue
            for unit in sorted(unit_dir.glob("*.service")):
                for raw in unit.read_text(encoding="utf-8", errors="replace").splitlines():
                    line = raw.strip()
                    if not line.startswith("ExecStart="):
                        continue
                    value = line[len("ExecStart="):].strip()
                    # Strip systemd special prefixes (@, -, +, !, :) then take
                    # the executable (first whitespace-separated token).
                    value = value.lstrip("@-+!:")
                    if not value:
                        continue
                    exec_path = value.split()[0]
                    base = Path(exec_path).name
                    if not base.startswith("tbox"):
                        continue
                    # Map an absolute on-target path to the staged install-root.
                    staged = root / exec_path.lstrip("/")
                    if not staged.exists():
                        missing.append((unit.name, exec_path))
        return missing

    def verify_package(self, package_path: Path) -> VerifyResult:
        """Verify a release package: checksum + structure."""
        result = VerifyResult()

        # 1. Package exists
        if not package_path.is_file():
            result.add_check("package-exists", False, f"Package not found: {package_path}")
            result.status = "failed"
            return result
        result.add_check("package-exists", True)

        # 2. Checksum file exists and matches
        checksum_file = package_path.with_suffix(".sha256")
        if not checksum_file.is_file():
            result.add_check("checksum-file", False, "Checksum file not found")
        else:
            with open(checksum_file) as f:
                expected = f.read().split()[0]
            actual = sha256_file(package_path)
            result.add_check(
                "checksum-match",
                expected == actual,
                f"expected={expected[:16]}..., actual={actual[:16]}..."
                if expected != actual else "Checksum matches",
            )

        # 3. Package contains expected directories + declared-unit gate
        import tarfile
        try:
            with tarfile.open(package_path, "r:gz") as tar:
                members = tar.getnames()
                has_install_root = any("install-root" in m for m in members)
                has_manifest = any("artifact-manifest.json" in m for m in members)
                has_package_info = any("package.json" in m for m in members)
                result.add_check(
                    "package-structure",
                    has_install_root and has_manifest and has_package_info,
                    f"install_root={has_install_root}, manifest={has_manifest}, "
                    f"package_info={has_package_info}",
                )

                # 4. Declared systemd units present in the package payload
                #     (CR-006 §3.3) — ownership from the in-package artifact
                #     manifest, unit names from the tar member list.
                payload_units = {
                    Path(m).name
                    for m in members
                    if m.endswith(".service")
                    and any(m.startswith("install-root/" + d)
                            for d in self._UNIT_PAYLOAD_DIRS)
                }
                owner_services: set[str] = set()
                manifest_member = next(
                    (m for m in members
                     if m.endswith("artifact-manifest.json")), None
                )
                if manifest_member:
                    extracted = tar.extractfile(manifest_member)
                    if extracted is not None:
                        try:
                            owner_services = self._owner_services_from_manifest(
                                json.loads(extracted.read().decode("utf-8"))
                            )
                        except (json.JSONDecodeError, UnicodeDecodeError):
                            owner_services = set()
                self._add_declared_unit_check(result, payload_units, owner_services)
        except Exception as exc:
            result.add_check("package-structure", False, str(exc))
            result.add_check("systemd-units-declared", False, str(exc))

        if result.status == "pending":
            result.status = "success"

        return result
