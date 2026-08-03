"""Unit tests for dependency graph analysis."""

from __future__ import annotations

import pytest

from tbox_build.errors import CycleError, DependencyError
from tbox_build.graph import DependencyGraph
from tbox_build.manifest import Service, BuildConfig, RuntimeConfig, ServiceManifest


def _make_service(
    sid: str,
    dependencies: list[str] | None = None,
    repository: str = ".",
) -> Service:
    return Service(
        id=sid,
        repository=repository,
        build=BuildConfig(
            target=sid,
            preset="orin-release",
            dependencies=dependencies or [],
        ),
        runtime=RuntimeConfig(),
    )


def _make_manifest(services: list[Service]) -> ServiceManifest:
    return ServiceManifest(services={s.id: s for s in services})


class TestTopologicalSort:
    def test_simple_chain(self):
        svc_a = _make_service("a")
        svc_b = _make_service("b", dependencies=["a"])
        svc_c = _make_service("c", dependencies=["b"])
        graph = DependencyGraph(_make_manifest([svc_a, svc_b, svc_c]))
        order = graph.topological_sort()
        assert order.index("a") < order.index("b") < order.index("c")

    def test_diamond(self):
        svc_a = _make_service("a")
        svc_b = _make_service("b", dependencies=["a"])
        svc_c = _make_service("c", dependencies=["a"])
        svc_d = _make_service("d", dependencies=["b", "c"])
        graph = DependencyGraph(_make_manifest([svc_a, svc_b, svc_c, svc_d]))
        order = graph.topological_sort()
        assert order.index("a") < order.index("b")
        assert order.index("a") < order.index("c")
        assert order.index("b") < order.index("d")
        assert order.index("c") < order.index("d")

    def test_no_dependencies(self):
        svc_a = _make_service("a")
        svc_b = _make_service("b")
        graph = DependencyGraph(_make_manifest([svc_a, svc_b]))
        order = graph.topological_sort()
        assert set(order) == {"a", "b"}

    def test_single_service_closure(self):
        svc_a = _make_service("a")
        svc_b = _make_service("b", dependencies=["a"])
        svc_c = _make_service("c", dependencies=["b"])
        svc_d = _make_service("d")  # independent
        graph = DependencyGraph(_make_manifest([svc_a, svc_b, svc_c, svc_d]))
        order = graph.topological_sort(["c"])
        # Should include c, b, a (closure) but not d
        assert set(order) == {"a", "b", "c"}
        assert "d" not in order

    def test_all_services_in_result(self):
        svc_a = _make_service("a")
        svc_b = _make_service("b", dependencies=["a"])
        graph = DependencyGraph(_make_manifest([svc_a, svc_b]))
        order = graph.topological_sort()
        assert len(order) == 2


class TestCycleDetection:
    def test_simple_cycle(self):
        svc_a = _make_service("a", dependencies=["b"])
        svc_b = _make_service("b", dependencies=["a"])
        graph = DependencyGraph(_make_manifest([svc_a, svc_b]))
        cycles = graph.detect_cycles()
        assert len(cycles) >= 1

    def test_self_cycle(self):
        svc_a = _make_service("a", dependencies=["a"])
        graph = DependencyGraph(_make_manifest([svc_a]))
        cycles = graph.detect_cycles()
        assert len(cycles) >= 1

    def test_no_cycle(self):
        svc_a = _make_service("a")
        svc_b = _make_service("b", dependencies=["a"])
        graph = DependencyGraph(_make_manifest([svc_a, svc_b]))
        cycles = graph.detect_cycles()
        assert len(cycles) == 0

    def test_topological_sort_raises_on_cycle(self):
        svc_a = _make_service("a", dependencies=["b"])
        svc_b = _make_service("b", dependencies=["a"])
        graph = DependencyGraph(_make_manifest([svc_a, svc_b]))
        with pytest.raises(CycleError):
            graph.topological_sort()

    def test_validate_raises_on_cycle(self):
        svc_a = _make_service("a", dependencies=["b"])
        svc_b = _make_service("b", dependencies=["a"])
        graph = DependencyGraph(_make_manifest([svc_a, svc_b]))
        with pytest.raises(CycleError):
            graph.validate()


class TestMissingDependencies:
    def test_find_missing(self):
        svc_a = _make_service("a", dependencies=["nonexistent"])
        graph = DependencyGraph(_make_manifest([svc_a]))
        missing = graph.find_missing_dependencies()
        assert "a" in missing
        assert "nonexistent" in missing["a"]

    def test_no_missing(self):
        svc_a = _make_service("a")
        svc_b = _make_service("b", dependencies=["a"])
        graph = DependencyGraph(_make_manifest([svc_a, svc_b]))
        missing = graph.find_missing_dependencies()
        assert len(missing) == 0

    def test_validate_raises_on_missing(self):
        svc_a = _make_service("a", dependencies=["nonexistent"])
        graph = DependencyGraph(_make_manifest([svc_a]))
        with pytest.raises(DependencyError):
            graph.validate()


class TestClosure:
    def test_closure_includes_self(self):
        svc_a = _make_service("a")
        graph = DependencyGraph(_make_manifest([svc_a]))
        closure = graph.closure(["a"])
        assert "a" in closure

    def test_closure_includes_transitive_deps(self):
        svc_a = _make_service("a")
        svc_b = _make_service("b", dependencies=["a"])
        svc_c = _make_service("c", dependencies=["b"])
        graph = DependencyGraph(_make_manifest([svc_a, svc_b, svc_c]))
        closure = graph.closure(["c"])
        assert closure == {"a", "b", "c"}


class TestBuildOrder:
    def test_build_order_validates_and_sorts(self):
        svc_a = _make_service("a")
        svc_b = _make_service("b", dependencies=["a"])
        graph = DependencyGraph(_make_manifest([svc_a, svc_b]))
        order = graph.build_order()
        assert order.index("a") < order.index("b")

    def test_build_order_with_subset(self):
        svc_a = _make_service("a")
        svc_b = _make_service("b", dependencies=["a"])
        svc_c = _make_service("c")
        graph = DependencyGraph(_make_manifest([svc_a, svc_b, svc_c]))
        order = graph.build_order(["b"])
        assert set(order) == {"a", "b"}
        assert "c" not in order


class TestRealManifest:
    """Tests using the actual project manifests."""

    def test_real_manifest_topological_sort(self, project_root):
        from tbox_build.manifest import Project
        project = Project(project_root)
        manifest = project.load_service_manifest()
        graph = DependencyGraph(manifest)
        order = graph.build_order()
        assert "tbox-hello-lib" in order
        assert "tbox-hello-cli" in order
        assert order.index("tbox-hello-lib") < order.index("tbox-hello-cli")

    def test_real_manifest_no_cycles(self, project_root):
        from tbox_build.manifest import Project
        project = Project(project_root)
        manifest = project.load_service_manifest()
        graph = DependencyGraph(manifest)
        assert len(graph.detect_cycles()) == 0

    def test_real_manifest_no_missing_deps(self, project_root):
        from tbox_build.manifest import Project
        project = Project(project_root)
        manifest = project.load_service_manifest()
        graph = DependencyGraph(manifest)
        assert len(graph.find_missing_dependencies()) == 0
