"""CR-005 integration: real manifest ordering contract + drop-in materialization.

Validates the shipping manifests/services.yaml (including the reviewed
TSP -> MQTT waiver) and materializes drop-ins for the real TBOX service set
without compiling anything.
"""

from __future__ import annotations

from pathlib import Path

from tbox_build.manifest import Project, load_yaml
from tbox_build.orchestrator import BuildOrchestrator, BuildConfig
from tbox_build.ordering import dropin_rel_path
from tbox_build.schema import validate_service_manifest
from tbox_build.staging import StagingDir
from tbox_build.validator import validate_all


class TestRealManifestContract:
    def test_schema_accepts_shipping_manifest(self, project_root: Path):
        project = Project(project_root)
        validate_service_manifest(
            load_yaml(project.service_manifest_path), project_root
        )

    def test_validate_all_passes_and_records_tsp_waiver(self, project_root: Path):
        project = Project(project_root)
        manifest = project.load_service_manifest()
        warnings = validate_all(manifest, project.root)
        assert any(
            "tsp" in w and "waives after-ordering" in w and "mqtt" in w
            for w in warnings
        ), f"expected TSP->MQTT waiver diagnostic, got: {warnings}"

    def test_tsp_waiver_targets_mqtt_unit(self, project_root: Path):
        project = Project(project_root)
        tsp = project.load_service_manifest().get("tsp")
        # TSP orders after SEC (real runtime dependency, TBOX-TSP-DSN-CR-010
        # §3.2) while the MQTT dependency stays waived for DEGRADED operation.
        assert tsp.runtime.after == ["tbox-sec.service"]
        assert len(tsp.runtime.after_waivers) == 1
        waiver = tsp.runtime.after_waivers[0]
        assert waiver.dependency == "mqtt"
        assert waiver.unit == "tbox-mqtt.service"
        assert waiver.owner == "tsp"
        assert waiver.reason.strip()


class TestMaterializeRealServices:
    def _materialize(self, project_root: Path, tmp_path: Path):
        project = Project(project_root)
        manifest = project.load_service_manifest()
        orch = BuildOrchestrator(project, BuildConfig(dry_run=True))
        orch.staging = StagingDir(tmp_path, "orin", "release")
        orch.staging.prepare()
        records = orch._materialize_ordering(manifest)
        return orch, {r.source_service: r for r in records}

    def test_expected_dropin_set(self, project_root: Path, tmp_path: Path):
        _, by_service = self._materialize(project_root, tmp_path)
        # sec/diag/mqtt/someip/tsp declare ordering; framework and the hello
        # services have no ordering.
        assert set(by_service) == {"sec", "diag", "mqtt", "someip", "tsp"}

    def test_mqtt_dropin_is_deterministic_and_minimal(
        self, project_root: Path, tmp_path: Path
    ):
        orch, by_service = self._materialize(project_root, tmp_path)
        content = (orch.staging.install_root / by_service["mqtt"].rel_path).read_text()
        assert content == "[Unit]\nAfter=tbox-prov.service tbox-sec.service\n"
        assert "Requires=" not in content
        assert "Wants=" not in content

    def test_waived_tsp_dependency_absent_from_dropin(
        self, project_root: Path, tmp_path: Path
    ):
        """A waived dependency must never reach the generated After= line.

        TSP orders after SEC but waives MQTT, so its drop-in must list
        tbox-sec.service and must not mention tbox-mqtt.service.
        """
        orch, _ = self._materialize(project_root, tmp_path)
        dropin = orch.staging.install_root / dropin_rel_path("tbox-tsp.service")
        assert dropin.exists()
        content = dropin.read_text()
        assert content == "[Unit]\nAfter=tbox-sec.service\n"
        assert "tbox-mqtt.service" not in content

    def test_materialized_set_is_build_owned_in_artifact_manifest(
        self, project_root: Path, tmp_path: Path
    ):
        orch, by_service = self._materialize(project_root, tmp_path)
        manifest = orch.project.load_service_manifest()
        artifact_manifest = orch._generate_artifact_manifest(
            manifest, lock=None, git_commit="test", service_results=[]
        )
        owned = {e.path for e in artifact_manifest.entries if e.build_owned}
        for record in by_service.values():
            assert f"rootfs/{record.rel_path}" in owned
