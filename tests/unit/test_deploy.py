"""Unit tests for the automated deploy pipeline (dry-run plan generation)."""

from __future__ import annotations

import hashlib
import tarfile
from pathlib import Path

from tbox_build.manifest import Project
from tbox_build.deploy import Deployer


def _make_package(tmp_path: Path) -> Path:
    """Create a minimal release package (install-root + checksum)."""
    payload = tmp_path / "install-root"
    (payload / "usr" / "bin").mkdir(parents=True)
    (payload / "usr" / "bin" / "tbox_someip").write_text("#!/bin/true\n")
    unit_dir = payload / "usr" / "lib" / "systemd" / "system"
    unit_dir.mkdir(parents=True)
    for unit in ("tbox-someip.service", "tbox-prov.service"):
        (unit_dir / unit).write_text("[Service]\nExecStart=/usr/bin/x\n")

    pkg = tmp_path / "pkg.tar.gz"
    with tarfile.open(pkg, "w:gz") as tar:
        for p in payload.rglob("*"):
            tar.add(p, arcname=str(p.relative_to(tmp_path)))
    digest = hashlib.sha256(pkg.read_bytes()).hexdigest()
    pkg.with_suffix(".sha256").write_text(f"{digest}  {pkg.name}\n")
    return pkg


def _deployer(project_root: Path, **kw) -> Deployer:
    return Deployer(Project(project_root), target_host="orin.local",
                    target_user="tbox", identity="/k/id", **kw)


class TestDeployPlan:
    def test_dry_run_is_safe_and_complete(self, tmp_path, project_root):
        pkg = _make_package(tmp_path)
        report = _deployer(project_root).deploy(pkg, execute=False)
        assert report.status == "success (dry-run)"
        names = [s.name for s in report.steps]
        for expected in ("pre-check", "upload", "backup", "install", "ldconfig",
                         "daemon-reload", "restart", "smoke", "cleanup"):
            assert expected in names, f"missing step {expected}"
        for s in report.steps:
            if s.name != "pre-check":
                assert s.status == "planned"

    def test_ssh_and_rsync_command_shape(self, tmp_path, project_root):
        pkg = _make_package(tmp_path)
        report = _deployer(project_root).deploy(pkg, execute=False)
        upload = next(s for s in report.steps if s.name == "upload")
        joined = "\n".join(upload.commands)
        assert "tbox@orin.local" in joined
        assert "-i /k/id" in joined
        assert "rsync -a" in joined

    def test_restart_units_dependency_order(self, tmp_path, project_root):
        pkg = _make_package(tmp_path)
        report = _deployer(project_root).deploy(pkg, execute=False)
        restart = next(s for s in report.steps if s.name == "restart")
        cmds = "\n".join(restart.commands)
        assert "tbox-someip.service" in cmds
        assert "tbox-prov.service" in cmds
        assert "systemctl enable --now" in cmds
        # prov must be restarted before someip (dependency order)
        assert cmds.index("tbox-prov.service") < cmds.index("tbox-someip.service")

    def test_no_sudo_option(self, tmp_path, project_root):
        pkg = _make_package(tmp_path)
        report = _deployer(project_root, use_sudo=False).deploy(pkg, execute=False)
        install = next(s for s in report.steps if s.name == "install")
        assert "sudo" not in "".join(install.commands)

    def test_execute_without_host_stays_dry_run(self, tmp_path, project_root):
        pkg = _make_package(tmp_path)
        d = Deployer(Project(project_root), target_host=None)
        report = d.deploy(pkg, execute=True)
        assert "dry-run" in report.status
        assert any("--execute requires --host" in e for e in report.errors)

    def test_pre_check_fails_on_missing_package(self, tmp_path, project_root):
        report = _deployer(project_root).deploy(tmp_path / "nope.tar.gz", execute=False)
        assert report.status == "failed"

    def test_pre_check_fails_on_checksum_mismatch(self, tmp_path, project_root):
        pkg = _make_package(tmp_path)
        pkg.with_suffix(".sha256").write_text("deadbeef  pkg.tar.gz\n")
        report = _deployer(project_root).deploy(pkg, execute=False)
        assert report.status == "failed"

    def test_rollback_dry_run_without_host(self, project_root):
        d = Deployer(Project(project_root), target_host=None)
        report = d.rollback("/var/tbox/backups/tbox-x.tar.gz")
        assert "dry-run" in report.status

    def test_compound_commands_wrapped_in_single_sudo(self, tmp_path, project_root):
        # restart chains 'enable && restart'; both must run under one sudo
        # (via 'sudo sh -c'), not just the first command.
        pkg = _make_package(tmp_path)
        report = _deployer(project_root).deploy(pkg, execute=False)
        restart = next(s for s in report.steps if s.name == "restart")
        for cmd in restart.commands:
            assert "sudo sh -c" in cmd
        backup = next(s for s in report.steps if s.name == "backup")
        assert "sudo sh -c" in backup.commands[0]

    def test_upload_staging_dir_not_sudo(self, tmp_path, project_root):
        # /tmp staging must be created as the login user so rsync can write it.
        pkg = _make_package(tmp_path)
        report = _deployer(project_root).deploy(pkg, execute=False)
        upload = next(s for s in report.steps if s.name == "upload")
        assert "mkdir -p" in upload.commands[0]
        assert "sudo" not in upload.commands[0]

    def test_ask_sudo_pass_renders_sudo_S(self, tmp_path, project_root):
        pkg = _make_package(tmp_path)
        report = _deployer(project_root, ask_sudo_pass=True).deploy(pkg, execute=False)
        install = next(s for s in report.steps if s.name == "install")
        assert "sudo -S -p" in install.commands[0]
