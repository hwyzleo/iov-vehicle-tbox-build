"""CR-006 Orin config overlay tests (BUILD-REQ-056, CR-006 §5).

The overlay is the production-shaped final TIME configuration:
* formal contract path under /etc/tbox/conf.d (covered by the conf.d
  replace policy, no new deploy exception);
* production profile only — no mock/fake backend;
* system-observe shape (TBOX-TIME-DSN-CR-002): TIME observes the system
  clock (read-only adjtimex + sd-bus timesync1) and distributes time
  facts; it does not write the clock, so the overlay must not carry
  write-specific keys (the config layer rejects them when present) and
  the legacy write-mode source adapters stay out of selection;
* socket path aligned with the SEC-side default contract.
"""

from __future__ import annotations

from pathlib import Path

import yaml

from tbox_build.manifest import load_config_deployment_manifest

OVERLAY = "configs/orin/rootfs/etc/tbox/conf.d/time.yaml"


class TestTimeOverlay:
    def test_overlay_exists_and_is_valid_yaml(self, project_root: Path):
        path = project_root / OVERLAY
        assert path.is_file()
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        assert data is not None
        assert "time" in data

    def test_production_profile_no_mock_no_fake(self, project_root: Path):
        data = yaml.safe_load((project_root / OVERLAY).read_text(encoding="utf-8"))
        time_cfg = data["time"]
        assert time_cfg["profile"] == "production"
        # CR-002: Orin runs the read-only observer, not the clock writer.
        assert time_cfg["backend"]["mode"] == "system-observe"
        assert "mock" not in str(time_cfg["backend"])
        assert time_cfg["backend"].get("mode") != "mock"

    def test_observe_mode_backend(self, project_root: Path):
        """Orin runs the read-only observer, not the clock writer (CR-002)."""
        data = yaml.safe_load((project_root / OVERLAY).read_text(encoding="utf-8"))
        backend = data["time"]["backend"]
        assert backend["mode"] == "system-observe"
        obs = backend["observer"]
        assert obs["driver"] == "timesyncd"
        assert 100 <= obs["poll_interval_ms"] <= 10000
        assert 100 <= obs["dbus_timeout_ms"] <= 5000

    def test_no_write_specific_keys(self, project_root: Path):
        """Write-mode keys are rejected by the config layer under
        system-observe, so the overlay must not carry them at all
        (CR-002 design 4.2: default values must not instantiate the
        writer / calibration logic)."""
        data = yaml.safe_load((project_root / OVERLAY).read_text(encoding="utf-8"))
        backend = data["time"]["backend"]
        core = data["time"]["core"]
        for key in ("allow_competing_writers", "competing_processes"):
            assert key not in backend, (
                f"backend.{key} only applies to backend.mode=linux and is "
                f"rejected under system-observe"
            )
        for key in ("step_threshold_ms", "slew_threshold_ms",
                    "alignment_tolerance_ms", "calibration_retry_limit"):
            assert key not in core, (
                f"core.{key} is write-specific and is rejected under "
                f"system-observe"
            )

    def test_error_bound_covers_device_and_fits_sec_gate(self, project_root: Path):
        """max_source_error_ms must cover the measured kernel maxerror while
        staying well under SEC's KL1 uncertainty gate.

        Measured on the Orin Nano (2026-11-05): adjtimex maxerror = 200.5 ms.
        SEC's kl1_trusted_upper_now rejects uncertainty > 1000 ms.
        """
        data = yaml.safe_load((project_root / OVERLAY).read_text(encoding="utf-8"))
        err = data["time"]["core"]["max_source_error_ms"]
        assert err > 201, "must cover the measured maxerror (200.5 ms)"
        assert err <= 1000, "must stay within SEC's KL1 uncertainty gate"

    def test_legacy_write_mode_sources_not_in_selection(self, project_root: Path):
        """Under system-observe the SystemObserverSource is the single source;
        the write-mode adapters (own NTP client / cellular / GNSS / RTC) do not
        participate. This is NOT the pre-CR-002 mistake of disabling every
        source while in write mode (which left zero candidates)."""
        data = yaml.safe_load((project_root / OVERLAY).read_text(encoding="utf-8"))
        sources = data["time"]["sources"]
        assert data["time"]["backend"]["mode"] == "system-observe"
        for name in ("ntp", "cellular", "gnss", "rtc"):
            assert sources[name]["enabled"] is False

    def test_single_writer_posture(self, project_root: Path):
        """Under system-observe the single-writer question is settled by role,
        not by detection: systemd-timesyncd is the legitimate and expected
        writer, and TIME never writes the clock. The competing-writer keys
        therefore must be absent (the config layer rejects them), and the
        observer must be the configured backend.
        """
        data = yaml.safe_load((project_root / OVERLAY).read_text(encoding="utf-8"))
        backend = data["time"]["backend"]
        assert backend["mode"] == "system-observe"
        assert "allow_competing_writers" not in backend
        assert "competing_processes" not in backend

    def test_socket_aligned_with_sec_contract(self, project_root: Path):
        data = yaml.safe_load((project_root / OVERLAY).read_text(encoding="utf-8"))
        assert data["time"]["ipc"]["socket_path"] == "/tmp/tbox-time.sock"

    def test_no_inline_common_section(self, project_root: Path):
        """common.yaml stays the single source; the overlay must not inline
        a top-level common: section (BUILD CR-003 §4.1)."""
        data = yaml.safe_load((project_root / OVERLAY).read_text(encoding="utf-8"))
        assert "common" not in data


class TestDeployPolicy:
    def test_confd_time_covered_by_replace_rule(self, project_root: Path):
        cdm = load_config_deployment_manifest(
            project_root / "manifests" / "config-deployment.yaml"
        )
        rule = cdm.match("orin", "/etc/tbox/conf.d/time.yaml")
        assert rule is not None
        assert rule.deploy_policy == "replace"
        # The formal contract path needs no new exception rule: the existing
        # conf.d/*.yaml glob already covers it.
        exact = cdm.match("orin", "/etc/tbox/time.yaml")
        assert exact is None  # legacy/inventory path has no dedicated rule
