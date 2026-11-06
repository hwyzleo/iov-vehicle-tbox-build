"""CR-006 manifest registration tests: TIME service entry + SEC ordering.

Covers BUILD-REQ-053: the ``time`` service is registered with its real
contract (daemon/client targets, SDK/runtime components, unit contract,
config paths, capabilities) and SEC carries the TIME build dependency plus
the actual ``runtime.after`` ordering edge (BUILD-REQ-054).
"""

from __future__ import annotations

from pathlib import Path

from tbox_build.manifest import load_service_manifest


class TestTimeRegistration:
    def test_time_entry_registered(self, project_root: Path):
        sm = load_service_manifest(project_root / "manifests" / "services.yaml")
        time_svc = sm.get("time")
        assert time_svc is not None
        assert time_svc.kind == "service"
        assert time_svc.source_dir == "../iov-vehicle-tbox-time"

    def test_real_build_targets_and_components(self, project_root: Path):
        """Daemon/client targets and SDK/runtime components match the TIME
        repo's install/export contract (TBOX-TIME-DSN-CR-001 @ 327e3cb)."""
        sm = load_service_manifest(project_root / "manifests" / "services.yaml")
        time_svc = sm.get("time")
        assert set(time_svc.build.targets) == {"tbox_time", "tbox-time-lib"}
        components = {
            (c.name, c.staging) for c in time_svc.build.install_components
        }
        assert ("time-sdk", "sdk") in components
        assert ("time-runtime", "rootfs") in components
        # TIME only depends on the framework library; no invented cloud/security
        # deps for the sake of time distribution (CR-006 §2.1).
        assert time_svc.build.service_dependencies == ["framework"]
        assert "time" not in time_svc.build.service_dependencies

    def test_unit_contract_and_config_paths(self, project_root: Path):
        sm = load_service_manifest(project_root / "manifests" / "services.yaml")
        time_svc = sm.get("time")
        # Unit contract name; the unit file itself must be installed by the
        # locked TIME version (packaging gate enforces presence, CR-006 §3.3).
        assert time_svc.runtime.systemd_units == ["tbox-time.service"]
        # Formal config contract under conf.d; no startup ordering of its own
        # (TIME only depends on the framework library).
        assert "/etc/tbox/conf.d/time.yaml" in time_svc.runtime.config_paths
        assert time_svc.runtime.after == []
        # CR-002: TIME observes the system clock read-only (adjtimex + sd-bus)
        # and never writes it, so no capability is granted at all.
        assert time_svc.runtime.capabilities == []

    def test_sec_keeps_prov_and_adds_time_ordering(self, project_root: Path):
        sm = load_service_manifest(project_root / "manifests" / "services.yaml")
        sec = sm.get("sec")
        assert "time" in sec.build.service_dependencies
        assert "prov" in sec.build.service_dependencies
        # CR-006 §3.2: SEC after 保留 PROV 并增加 TIME 实际 unit.
        assert "tbox-prov.service" in sec.runtime.after
        assert "tbox-time.service" in sec.runtime.after
        # No waiver may substitute the TIME ordering edge.
        assert not any(w.dependency == "time" for w in sec.runtime.after_waivers)
