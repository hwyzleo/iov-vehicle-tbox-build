"""Artifact manifest generation for TBOX Build.

Generates a JSON artifact manifest that records every file in the
staging install-root with:

  * Owner service and CMake target
  * Version and git commit
  * Build profile
  * Platform / toolchain / sysroot digest summary
  * SHA-256 checksum
  * ELF architecture, interpreter, dynamic dependencies, RPATH (if applicable)

Also detects installation path conflicts (two services claiming the same
path with different content).
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .elfcheck import ElfInfo, classify_file, EM_AARCH64, _ELFCLASS64, parse_elf, _MACHINE_NAMES
from .errors import PathConflictError
from .manifest import PlatformManifest, SysrootManifest
from .staging import StagedFile, StagingDir


@dataclass
class ArtifactEntry:
    """A single file entry in the artifact manifest."""

    path: str
    owner_service: str
    owner_target: str
    version: str
    git_commit: str
    profile: str
    sha256: str
    size: int
    file_type: str
    elf_info: dict[str, Any] | None = None


class ArtifactManifest:
    """Builds and serialises the artifact manifest."""

    def __init__(
        self,
        platform: str,
        profile: str,
        platform_manifest: PlatformManifest,
        sysroot_manifest: SysrootManifest,
    ):
        self.platform = platform
        self.profile = profile
        self.platform_manifest = platform_manifest
        self.sysroot_manifest = sysroot_manifest
        self.entries: list[ArtifactEntry] = []
        self.generated_at = datetime.now(timezone.utc).isoformat(timespec="seconds")

    def add_entry(self, entry: ArtifactEntry) -> None:
        self.entries.append(entry)

    def _elf_info_dict(self, elf: ElfInfo) -> dict[str, Any]:
        return {
            "class": "ELFCLASS64" if elf.is_64bit else "ELFCLASS32",
            "machine": elf.machine_name,
            "type": elf.elf_type,
            "interpreter": elf.interpreter,
            "needed": elf.needed,
            "rpath": elf.rpath,
            "runpath": elf.runpath,
        }

    def add_staged_file(
        self,
        staged: StagedFile,
        owner_service: str,
        owner_target: str,
        version: str,
        git_commit: str,
    ) -> None:
        """Add a staged file to the manifest, classifying and inspecting it."""
        cls = classify_file(staged.full_path)
        elf_info: dict[str, Any] | None = None
        if cls.file_type == "elf" and cls.elf_info is not None and cls.elf_info.is_elf:
            elf_info = self._elf_info_dict(cls.elf_info)

        entry = ArtifactEntry(
            path=staged.rel_path,
            owner_service=owner_service,
            owner_target=owner_target,
            version=version,
            git_commit=git_commit,
            profile=self.profile,
            sha256=staged.sha256,
            size=staged.size,
            file_type=cls.file_type,
            elf_info=elf_info,
        )
        self.add_entry(entry)

    def check_conflicts(self) -> None:
        """Raise PathConflictError if the same path has different owners."""
        path_owners: dict[str, str] = {}
        conflicts: list[str] = []
        for entry in self.entries:
            if entry.path in path_owners:
                if path_owners[entry.path] != entry.owner_service:
                    conflicts.append(
                        f"Path '{entry.path}' owned by both "
                        f"'{path_owners[entry.path]}' and '{entry.owner_service}'"
                    )
            else:
                path_owners[entry.path] = entry.owner_service
        if conflicts:
            raise PathConflictError(
                f"Installation path conflicts detected ({len(conflicts)} conflict(s))",
                conflicts,
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": "0.1.0",
            "platform": self.platform,
            "profile": self.profile,
            "generated_at": self.generated_at,
            "platform_manifest": {
                "platform": self.platform_manifest.platform,
                "architecture": self.platform_manifest.architecture,
                "rootfs_id": self.platform_manifest.rootfs_id,
                "sysroot_id": self.platform_manifest.sysroot_id,
                "target_triple": self.platform_manifest.target_triple,
            },
            "sysroot": {
                "id": self.sysroot_manifest.id,
                "digest": self.sysroot_manifest.digest,
                "import_status": self.sysroot_manifest.import_status,
            },
            "artifact_count": len(self.entries),
            "artifacts": [asdict(e) for e in self.entries],
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), indent=2, sort_keys=False)

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(self.to_json())


def get_git_commit(project_root: Path) -> str:
    """Get the current git commit hash of the project."""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=str(project_root),
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode == 0:
            return result.stdout.strip()
    except (subprocess.SubprocessError, OSError):
        pass
    return "unknown"
