"""CR-006 release-set closure tests (BUILD-REQ-054).

Every release set that includes a TIME-consuming SEC must pull ``time`` in
through the dependency closure; framework-only and non-TIME-consumer sets
must stay unchanged; the standalone ``tbox-time-orin`` set is preserved.
"""

from __future__ import annotations

from pathlib import Path

from tbox_build.graph import DependencyGraph
from tbox_build.manifest import (
    Project,
    load_service_manifest,
    load_release_set_manifest,
)

SEC_CONTAINING_SETS = {
    "tbox-sec-orin",
    "tbox-diag-orin",
    "tbox-mqtt-orin",
    "tbox-tsp-orin",
    "tbox-someip-orin",
}

NON_TIME_SETS = {
    "tbox-orin-minimal",
    "tbox-framework-orin",
    "tbox-prov-orin",
}


class TestReleaseSetClosure:
    def _load(self, project_root: Path):
        sm = load_service_manifest(project_root / "manifests" / "services.yaml")
        rsm = load_release_set_manifest(project_root / "manifests" / "release-set.yaml")
        graph = DependencyGraph(sm)
        return sm, rsm, graph

    def test_sec_containing_sets_include_time_via_closure(self, project_root: Path):
        sm, rsm, graph = self._load(project_root)
        for set_id in SEC_CONTAINING_SETS:
            rs = rsm.get(set_id)
            assert rs is not None, set_id
            closure = graph.closure(rs.services)
            assert "time" in closure, (
                f"release set {set_id} (contains SEC) must include time via "
                f"dependency closure"
            )

    def test_framework_only_and_non_time_sets_not_expanded(self, project_root: Path):
        sm, rsm, graph = self._load(project_root)
        for set_id in NON_TIME_SETS:
            rs = rsm.get(set_id)
            assert rs is not None, set_id
            closure = graph.closure(rs.services)
            assert "time" not in closure, (
                f"release set {set_id} must not mechanically grow to include time"
            )

    def test_standalone_time_set_preserved(self, project_root: Path):
        sm, rsm, graph = self._load(project_root)
        rs = rsm.get("tbox-time-orin")
        assert rs is not None
        closure = graph.closure(rs.services)
        assert closure == {"framework", "time"}

    def test_build_order_places_time_before_sec(self, project_root: Path):
        sm, rsm, graph = self._load(project_root)
        order = graph.build_order(["sec"])
        assert order.index("time") < order.index("sec")
        # SEC must not depend on TIME transitively through the cloud path.
        assert "mqtt" not in graph.closure(["time"])
