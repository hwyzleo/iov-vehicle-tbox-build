"""Unit tests for CR-002 graph: service vs target dependency separation,
target_dependency_set aggregation and release-set closure."""

from __future__ import annotations

import pytest

from tbox_build.graph import DependencyGraph
from tbox_build.manifest import (
    Service,
    BuildConfig,
    InstallComponent,
    RuntimeConfig,
    ServiceManifest,
    DependencyLock,
    DependencyEntry,
)


def _svc(sid, service_deps=None, target_deps=None, repository="."):
    return Service(
        id=sid,
        repository=repository,
        kind="daemon",
        build=BuildConfig(
            preset="orin-release",
            targets=[sid],
            service_dependencies=service_deps or [],
            target_dependencies=target_deps or [],
        ),
        runtime=RuntimeConfig(),
    )


def _lock(*names):
    deps = {}
    for n in names:
        deps[n] = DependencyEntry(
            name=n, version="1.0", source_url="u", source_sha256="abc",
            license="MIT", boundary="TARGET", architecture="aarch64",
            linkage="static",
        )
    return DependencyLock(dependencies=deps)


class TestTargetDependencySet:
    def test_target_deps_not_in_topology(self):
        manifest = ServiceManifest(services={
            "fw": _svc("fw", target_deps=["yaml-cpp"]),
        })
        graph = DependencyGraph(manifest, _lock("yaml-cpp"))
        order = graph.topological_sort()
        assert order == ["fw"]
        assert graph.target_dependency_set(["fw"]) == {"yaml-cpp"}

    def test_mixed_deps_closure(self):
        manifest = ServiceManifest(services={
            "fw": _svc("fw", target_deps=["yaml-cpp"]),
            "prov": _svc("prov", service_deps=["fw"], target_deps=[]),
        })
        graph = DependencyGraph(manifest, _lock("yaml-cpp"))
        closure = graph.closure(["prov"])
        assert closure == {"fw", "prov"}
        assert graph.target_dependency_set(["prov"]) == {"yaml-cpp"}

    def test_target_deps_aggregated_deduplicated(self):
        manifest = ServiceManifest(services={
            "fw": _svc("fw", target_deps=["yaml-cpp", "spdlog"]),
            "prov": _svc("prov", service_deps=["fw"], target_deps=["yaml-cpp"]),
        })
        graph = DependencyGraph(manifest, _lock("yaml-cpp", "spdlog"))
        assert graph.target_dependency_set(["prov"]) == {"yaml-cpp", "spdlog"}

    def test_missing_target_dep_detected(self):
        manifest = ServiceManifest(services={
            "fw": _svc("fw", target_deps=["ghost"]),
        })
        graph = DependencyGraph(manifest, _lock())
        missing = graph.find_missing_target_dependencies()
        assert "fw" in missing
        assert "ghost" in missing["fw"]

    def test_no_missing_target_dep_when_lock_present(self):
        manifest = ServiceManifest(services={
            "fw": _svc("fw", target_deps=["yaml-cpp"]),
        })
        graph = DependencyGraph(manifest, _lock("yaml-cpp"))
        assert graph.find_missing_target_dependencies() == {}


class TestFrameworkClosure:
    def test_framework_release_set_closure(self, project_root):
        from tbox_build.manifest import Project
        project = Project(project_root)
        manifest = project.load_service_manifest()
        lock = project.load_dependency_lock()
        graph = DependencyGraph(manifest, lock)
        closure = graph.closure(["framework"])
        assert closure == {"framework"}
        assert graph.target_dependency_set(["framework"]) == {"yaml-cpp"}

    def test_framework_build_order(self, project_root):
        from tbox_build.manifest import Project
        project = Project(project_root)
        manifest = project.load_service_manifest()
        lock = project.load_dependency_lock()
        graph = DependencyGraph(manifest, lock)
        order = graph.build_order(["framework"])
        assert order == ["framework"]
