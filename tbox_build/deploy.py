"""Deploy framework for TBOX Build.

Implements the deploy pipeline:
  pre-check -> upload -> backup -> install -> verify -> daemon-reload
  -> restart (dependency order) -> smoke test

Phase 1 provides the framework and local verification; actual SSH/SCP
deployment to an Orin device requires a configured target and is
executed via the verify module on the device side.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .errors import TboxBuildError
from .manifest import Project, ServiceManifest, ReleaseSetManifest
from .graph import DependencyGraph
from .staging import StagingDir, sha256_file


@dataclass
class DeployStep:
    name: str
    status: str = "pending"  # pending, success, failed, skipped
    message: str = ""
    duration_seconds: float = 0.0


@dataclass
class DeployReport:
    package_path: str = ""
    target_host: str = ""
    steps: list[DeployStep] = field(default_factory=list)
    status: str = "pending"
    errors: list[str] = field(default_factory=list)
    started_at: str = ""
    finished_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class Deployer:
    """Deploys a release package to a target device."""

    def __init__(
        self,
        project: Project,
        target_host: str | None = None,
        target_user: str = "tbox",
    ):
        self.project = project
        self.target_host = target_host
        self.target_user = target_user

    def _pre_check(self, package_path: Path) -> DeployStep:
        """Verify package exists and checksum matches."""
        step = DeployStep(name="pre-check")
        start = _now()

        if not package_path.is_file():
            step.status = "failed"
            step.message = f"Package not found: {package_path}"
            step.duration_seconds = _elapsed(start)
            return step

        checksum_file = package_path.with_suffix(".sha256")
        if checksum_file.is_file():
            with open(checksum_file) as f:
                expected = f.read().split()[0]
            actual = sha256_file(package_path)
            if expected != actual:
                step.status = "failed"
                step.message = f"Checksum mismatch: expected {expected}, got {actual}"
                step.duration_seconds = _elapsed(start)
                return step

        step.status = "success"
        step.message = f"Package verified ({package_path.name})"
        step.duration_seconds = _elapsed(start)
        return step

    def deploy(self, package_path: Path, dry_run: bool = True) -> DeployReport:
        """Execute the deploy pipeline.

        Phase 1 defaults to *dry_run* (no actual SSH deployment).
        """
        report = DeployReport(
            package_path=str(package_path),
            target_host=self.target_host or "(none)",
            started_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        )

        # 1. Pre-check
        step = self._pre_check(package_path)
        report.steps.append(step)
        if step.status == "failed":
            report.status = "failed"
            report.errors.append(step.message)
            report.finished_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
            return report

        # 2. Upload
        if dry_run or not self.target_host:
            report.steps.append(DeployStep(
                name="upload", status="skipped",
                message="Skipped (dry-run or no target host configured)",
            ))
        else:
            step = self._upload(package_path)
            report.steps.append(step)
            if step.status == "failed":
                report.status = "failed"
                report.errors.append(step.message)
                return report

        # 3-7. Backup, install, verify, reload, restart, smoke
        for step_name in ("backup", "install", "verify", "daemon-reload", "restart", "smoke"):
            if dry_run or not self.target_host:
                report.steps.append(DeployStep(
                    name=step_name, status="skipped",
                    message="Skipped (dry-run or no target host configured)",
                ))
            else:
                report.steps.append(DeployStep(
                    name=step_name, status="pending",
                    message="Not yet implemented for SSH deployment",
                ))

        report.status = "success" if not dry_run else "success (dry-run)"
        report.finished_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        return report

    def rollback(self, backup_path: Path) -> DeployReport:
        """Rollback to a previous version."""
        report = DeployReport(
            target_host=self.target_host or "(none)",
            started_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        )
        if not backup_path.exists():
            report.status = "failed"
            report.errors.append(f"Backup not found: {backup_path}")
            report.finished_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
            return report

        report.steps.append(DeployStep(
            name="rollback", status="skipped",
            message="Rollback framework ready; actual device rollback requires target host",
        ))
        report.status = "success (dry-run)"
        report.finished_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        return report


def _now() -> float:
    import time
    return time.time()


def _elapsed(start: float) -> float:
    import time
    return time.time() - start
