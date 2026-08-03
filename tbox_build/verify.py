"""Verification for TBOX Build.

Provides package verification (checksum, manifest integrity, structure)
and staging verification (ELF check, artifact manifest, path conflicts).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any

from .elfcheck import check_staging, assert_clean, ElfCheckResult
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

        if result.status == "pending":
            result.status = "success"

        return result

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
