"""Staging directory management for TBOX Build.

Manages the ``out/<platform>/<profile>/`` output tree:

    out/orin/release/
    ├── build/<service>/        per-service CMake build dirs
    ├── install-root/           merged DESTDIR install tree
    ├── manifests/              artifact manifest, build report
    ├── logs/                   per-service step logs
    └── packages/               release packages

Provides file ownership tracking (which service installed which file)
and path-conflict detection.
"""

from __future__ import annotations

import hashlib
import shutil
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class StagedFile:
    """A file in the staging install-root."""

    rel_path: str  # relative to install-root (e.g. "usr/bin/tbox-hello-cli")
    full_path: Path
    size: int
    sha256: str
    owner_service: str | None = None


def sha256_file(path: Path) -> str:
    """Compute SHA-256 hex digest of a file."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


class StagingDir:
    """Manages the staging output directory tree."""

    def __init__(self, project_root: Path, platform: str = "orin", profile: str = "release"):
        self.project_root = Path(project_root)
        self.platform = platform
        self.profile = profile
        self.root = self.project_root / "out" / platform / profile
        self.install_root = self.root / "install-root"
        self.build_dir = self.root / "build"
        self.manifests_dir = self.root / "manifests"
        self.logs_dir = self.root / "logs"
        self.packages_dir = self.root / "packages"

    def prepare(self, clean: bool = False) -> None:
        """Create the staging directory structure.

        If *clean* is True, remove existing build and install-root first.
        """
        if clean and self.root.exists():
            shutil.rmtree(self.root)
        for d in (
            self.root,
            self.install_root,
            self.build_dir,
            self.manifests_dir,
            self.logs_dir,
            self.packages_dir,
        ):
            d.mkdir(parents=True, exist_ok=True)

    def service_build_dir(self, service_id: str) -> Path:
        return self.build_dir / service_id

    def service_log_file(self, service_id: str, step: str) -> Path:
        return self.logs_dir / f"{service_id}-{step}.log"

    def snapshot_paths(self) -> set[str]:
        """Return a set of relative paths currently in install-root."""
        result: set[str] = set()
        if not self.install_root.exists():
            return result
        for path in self.install_root.rglob("*"):
            if path.is_file() and not path.is_symlink():
                result.add(str(path.relative_to(self.install_root)))
            elif path.is_symlink():
                result.add(str(path.relative_to(self.install_root)))
        return result

    def scan_files(self) -> list[StagedFile]:
        """Scan install-root and return all files with metadata."""
        files: list[StagedFile] = []
        if not self.install_root.exists():
            return files
        for path in sorted(self.install_root.rglob("*")):
            if not path.is_file() and not path.is_symlink():
                continue
            rel = str(path.relative_to(self.install_root))
            if path.is_symlink():
                files.append(StagedFile(
                    rel_path=rel, full_path=path, size=0, sha256=""
                ))
            else:
                stat = path.stat()
                files.append(StagedFile(
                    rel_path=rel,
                    full_path=path,
                    size=stat.st_size,
                    sha256=sha256_file(path),
                ))
        return files

    def new_files_after_install(
        self, before: set[str], service_id: str
    ) -> set[str]:
        """Return paths that appeared after install, attributed to *service_id*."""
        after = self.snapshot_paths()
        return after - before
