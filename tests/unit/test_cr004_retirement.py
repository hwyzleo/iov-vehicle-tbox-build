"""Unit tests for CR-004 D3: deployment retirement (BUILD-REQ-047).

Covers RetirePlanner parsing, the retire step placement/commands in the
dry-run deploy plan, backup integration (unit fragment + retired paths),
and idempotence guards.
"""

from __future__ import annotations

import hashlib
import tarfile
from pathlib import Path

import yaml

from tbox_build.manifest import (
    Project,
    load_retirement_manifest,
)
from tbox_build.deploy import Deployer, RetirePlanner


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write_yaml(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")


_ORIN_PLATFORM = {
    "platform": "orin",
    "architecture": "aarch64",
    "rootfs": {"id": "orin-r35.3.1", "repository_path": "sysroots/orin-r35.3.1"},
    "toolchain": {"target_triple": "aarch64-linux-gnu", "sysroot": "orin-r35.3.1"},
}


def _make_project(tmp_path: Path, retirement: dict) -> Path:
    """Minimal project root with orin-platform + retirement manifests."""
    root = tmp_path / "project"
    _write_yaml(root / "manifests" / "orin-platform.yaml", _ORIN_PLATFORM)
    _write_yaml(root / "manifests" / "services.yaml", {"services": {}})
    _write_yaml(root / "manifests" / "release-set.yaml", {"release_sets": {}})
    _write_yaml(root / "manifests" / "retirement.yaml", retirement)
    return root


def _make_package(tmp_path: Path) -> Path:
    """Minimal release package with an install-root and checksum."""
    payload = tmp_path / "install-root"
    (payload / "usr" / "bin").mkdir(parents=True)
    (payload / "usr" / "bin" / "tbox_prov").write_text("#!/bin/true\n")
    pkg = tmp_path / "pkg.tar.gz"
    with tarfile.open(pkg, "w:gz") as tar:
        for p in payload.rglob("*"):
            tar.add(p, arcname=str(p.relative_to(tmp_path)))
    digest = hashlib.sha256(pkg.read_bytes()).hexdigest()
    pkg.with_suffix(".sha256").write_text(f"{digest}  {pkg.name}\n")
    return pkg


RETIREMENT_WITH_ENTRIES = {
    "version": 1,
    "platforms": {
        "orin": {
            "retired_units": [{
                "name": "prov.service",
                "owner": "build",
                "reason": "replaced-by-tbox-prov.service",
            }],
            "retired_paths": [{
                "path": "/opt/tbox/bin/prov",
                "owner": "build",
                "reason": "legacy-monolith",
            }],
        }
    },
}


class TestRetirePlanner:
    def test_parses_units_and_paths(self, tmp_path):
        rm_path = tmp_path / "retirement.yaml"
        _write_yaml(rm_path, RETIREMENT_WITH_ENTRIES)
        rm = load_retirement_manifest(rm_path)
        plan = RetirePlanner(rm, "orin").plan()
        assert [u.unit for u in plan.units] == ["prov.service"]
        assert [p.path for p in plan.paths] == ["/opt/tbox/bin/prov"]
        assert plan.summary() == "1 unit(s), 1 path(s)"

    def test_empty_platform_noop(self, tmp_path):
        rm_path = tmp_path / "retirement.yaml"
        _write_yaml(rm_path, {
            "version": 1,
            "platforms": {"orin": {"retired_units": [], "retired_paths": []}},
        })
        plan = RetirePlanner(load_retirement_manifest(rm_path), "orin").plan()
        assert plan.empty


class TestDeployRetireStep:
    def _report(self, tmp_path, retirement: dict):
        root = _make_project(tmp_path, retirement)
        pkg = _make_package(tmp_path)
        d = Deployer(Project(root), target_host="orin.local", target_user="tbox")
        return d, d.deploy(pkg, execute=False)

    def test_retire_step_placed_after_backup_before_install(self, tmp_path):
        d, report = self._report(tmp_path, RETIREMENT_WITH_ENTRIES)
        names = [s.name for s in report.steps]
        assert "retire" in names
        assert names.index("backup") < names.index("retire") < names.index("install")
        # full CR-004 transaction order
        expected = ["pre-check", "upload", "backup", "config-plan", "retire",
                    "install", "ldconfig", "daemon-reload", "restart", "smoke",
                    "cleanup"]
        assert [n for n in expected if n in names] == expected

    def test_retire_commands_disable_unit_and_remove_path(self, tmp_path):
        d, report = self._report(tmp_path, RETIREMENT_WITH_ENTRIES)
        retire = next(s for s in report.steps if s.name == "retire")
        joined = "\n".join(retire.commands)
        assert "systemctl disable --now prov.service" in joined
        assert "rm -rf -- /opt/tbox/bin/prov" in joined
        assert "systemctl is-enabled prov.service" in joined
        assert "systemctl is-active prov.service" in joined
        assert "prov.service (owner=build" in joined  # record owner/reason

    def test_backup_includes_retired_paths_and_unit_fragment(self, tmp_path):
        d, report = self._report(tmp_path, RETIREMENT_WITH_ENTRIES)
        backup = next(s for s in report.steps if s.name == "backup")
        joined = backup.commands[0]
        assert "FragmentPath --value prov.service" in joined
        assert "test -e /opt/tbox/bin/prov && echo /opt/tbox/bin/prov" in joined

    def test_idempotence_guards(self, tmp_path):
        """Missing targets are no-ops, but real failures must still surface.

        CR-004 评审 P0-2: the original implementation expressed idempotence with
        a trailing ``|| true``, which forces rc 0 and makes the retire step
        (and therefore its rollback path) permanently unreachable. Idempotence
        must come from an existence guard instead.
        """
        d, report = self._report(tmp_path, RETIREMENT_WITH_ENTRIES)
        retire = next(s for s in report.steps if s.name == "retire")
        cmds = retire.commands
        joined = "\n".join(cmds)
        # Idempotence via existence guards, not via rc suppression.
        assert "if [ -e /opt/tbox/bin/prov ]; then rm -rf -- /opt/tbox/bin/prov; fi" in joined
        assert "if systemctl cat prov.service >/dev/null 2>&1; then" in joined
        # The destructive commands must NOT swallow their exit status.
        destructive = [
            c for c in cmds
            if ("rm -rf" in c or "systemctl disable" in c) and not c.startswith("#")
        ]
        assert destructive, "expected disable/remove commands in the retire step"
        for cmd in destructive:
            assert "|| true" not in cmd, f"rc suppressed in destructive command: {cmd}"

    def test_retire_entries_are_shell_quoted(self, tmp_path):
        """CR-004 评审 P0-1: unit/path must be quoted on the remote side.

        The schema + validator reject shell metacharacters outright, so this
        exercises the defence-in-depth layer: even if a hostile entry reached
        the deployer, the remote shell must not be able to re-parse it into
        extra commands.
        """
        d, report = self._report(tmp_path, {
            "version": 1,
            "platforms": {"orin": {
                "retired_units": [
                    {"name": "prov.service; reboot", "owner": "o", "reason": "r"}],
                "retired_paths": [
                    {"path": "/opt/tbox/bin/x; rm -rf /", "owner": "o", "reason": "r"}],
            }},
        })
        retire = next(s for s in report.steps if s.name == "retire")
        cmds = [c for c in retire.commands if not c.startswith("#")]
        joined = "\n".join(cmds)
        # The payload survives only as a single quoted word.
        assert "'prov.service; reboot'" in joined
        assert "'/opt/tbox/bin/x; rm -rf /'" in joined
        # ...and never as a bare command the remote shell would execute.
        assert "; rm -rf / " not in joined.replace("'/opt/tbox/bin/x; rm -rf /'", "")
        assert "; reboot" not in joined.replace("'prov.service; reboot'", "")

    def test_empty_retirement_is_noop_step(self, tmp_path):
        d, report = self._report(tmp_path, {
            "version": 1,
            "platforms": {"orin": {"retired_units": [], "retired_paths": []}},
        })
        retire = next(s for s in report.steps if s.name == "retire")
        assert retire.commands == []
        assert "no retirements declared" in retire.message

    def test_dry_run_status_kept(self, tmp_path):
        d, report = self._report(tmp_path, RETIREMENT_WITH_ENTRIES)
        assert report.status == "success (dry-run)"
        retire = next(s for s in report.steps if s.name == "retire")
        assert retire.status == "planned"
