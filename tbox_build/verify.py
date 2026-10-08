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

    def __init__(self, staging: StagingDir):
        self.staging = staging

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

        # 3. Package contains expected directories
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
        except Exception as exc:
            result.add_check("package-structure", False, str(exc))

        if result.status == "pending":
            result.status = "success"

        return result
