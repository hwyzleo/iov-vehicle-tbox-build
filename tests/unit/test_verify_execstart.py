"""Regression tests for the systemd ExecStart verification guard.

Covers the deploy failure where tbox-prov.service pointed
ExecStart=/usr/bin/tbox-prov (hyphen) while the installed daemon was
/usr/bin/tbox_prov (underscore), causing the unit to exit 127 on the device.
"""

from __future__ import annotations

from pathlib import Path

from tbox_build.staging import StagingDir
from tbox_build.verify import Verifier


def _unit_dir(staging: StagingDir) -> Path:
    d = staging.install_root / "usr" / "lib" / "systemd" / "system"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _bin_dir(staging: StagingDir) -> Path:
    d = staging.install_root / "usr" / "bin"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _make_staging(tmp_path: Path) -> StagingDir:
    staging = StagingDir(tmp_path, "orin", "release")
    staging.prepare()
    return staging


def _write_unit(staging: StagingDir, name: str, exec_start: str) -> None:
    (_unit_dir(staging) / name).write_text(
        "[Unit]\nDescription=t\n[Service]\n"
        f"ExecStart={exec_start}\n[Install]\nWantedBy=multi-user.target\n"
    )


class TestExecStartGuard:
    def test_detects_binary_name_mismatch(self, tmp_path: Path):
        staging = _make_staging(tmp_path)
        (_bin_dir(staging) / "tbox_prov").write_text("elf")  # installed name
        _write_unit(staging, "tbox-prov.service", "/usr/bin/tbox-prov")  # wrong

        missing = Verifier(staging)._missing_execstart_binaries()
        assert missing == [("tbox-prov.service", "/usr/bin/tbox-prov")]

        result = Verifier(staging).verify_staging()
        checks = {c["name"]: c["status"] for c in result.checks}
        assert checks["systemd-execstart"] == "failed"
        assert result.status == "failed"

    def test_passes_when_binary_present(self, tmp_path: Path):
        staging = _make_staging(tmp_path)
        (_bin_dir(staging) / "tbox_prov").write_text("elf")
        _write_unit(staging, "tbox-prov.service", "/usr/bin/tbox_prov")

        assert Verifier(staging)._missing_execstart_binaries() == []

    def test_ignores_system_binaries(self, tmp_path: Path):
        """Non-tbox ExecStart targets (base-image tools) are not flagged."""
        staging = _make_staging(tmp_path)
        _write_unit(staging, "helper.service", "/bin/sh -c 'echo hi'")
        assert Verifier(staging)._missing_execstart_binaries() == []

    def test_strips_systemd_special_prefixes(self, tmp_path: Path):
        staging = _make_staging(tmp_path)
        (_bin_dir(staging) / "tbox_prov").write_text("elf")
        # leading '-' (ignore-failure) plus args must still resolve the binary
        _write_unit(staging, "tbox-prov.service", "-/usr/bin/tbox_prov --flag x")
        assert Verifier(staging)._missing_execstart_binaries() == []
