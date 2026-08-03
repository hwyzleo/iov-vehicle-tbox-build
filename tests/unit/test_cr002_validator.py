"""Unit tests for CR-002 validator: library kind rules, target_dependencies
lock hits, source_dir existence/workspace boundary."""

from __future__ import annotations

from pathlib import Path

import pytest

from tbox_build.errors import ValidationFailure
from tbox_build.manifest import (
    Service,
    BuildConfig,
    RuntimeConfig,
    ServiceManifest,
    DependencyLock,
    DependencyEntry,
)
from tbox_build.validator import (
    validate_service_dependencies,
    validate_target_dependencies,
    validate_library_kind,
    validate_source_dirs,
    validate_all,
)


def _svc(sid, kind="daemon", service_deps=None, target_deps=None,
         systemd_units=None, after=None, health=None, smoke=None,
         repository="."):
    return Service(
        id=sid,
        repository=repository,
        kind=kind,
        build=BuildConfig(
            preset="orin-release",
            targets=[sid],
            service_dependencies=service_deps or [],
            target_dependencies=target_deps or [],
        ),
        runtime=RuntimeConfig(
            systemd_units=systemd_units or [],
            after=after or [],
            health_check=health,
            smoke_test=smoke,
        ),
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


class TestServiceDependencies:
    def test_self_dependency_rejected(self):
        manifest = ServiceManifest(services={"a": _svc("a", service_deps=["a"])})
        with pytest.raises(ValidationFailure) as exc_info:
            validate_service_dependencies(manifest)
        assert any("depend on itself" in d for d in exc_info.value.details)

    def test_missing_service_dep_rejected(self):
        manifest = ServiceManifest(services={"a": _svc("a", service_deps=["ghost"])})
        with pytest.raises(ValidationFailure) as exc_info:
            validate_service_dependencies(manifest)
        assert any("missing service dependency" in d for d in exc_info.value.details)


class TestTargetDependencies:
    def test_missing_target_dep_rejected(self):
        manifest = ServiceManifest(services={"fw": _svc("fw", target_deps=["ghost"])})
        with pytest.raises(ValidationFailure) as exc_info:
            validate_target_dependencies(manifest, _lock())
        assert any("missing target dependency" in d for d in exc_info.value.details)

    def test_service_id_in_target_deps_rejected(self):
        manifest = ServiceManifest(services={
            "a": _svc("a"),
            "b": _svc("b", target_deps=["a"]),
        })
        with pytest.raises(ValidationFailure) as exc_info:
            validate_target_dependencies(manifest, _lock())
        assert any("is a service id" in d for d in exc_info.value.details)

    def test_valid_target_dep_accepted(self):
        manifest = ServiceManifest(services={"fw": _svc("fw", target_deps=["yaml-cpp"])})
        validate_target_dependencies(manifest, _lock("yaml-cpp"))  # no raise


class TestLibraryKind:
    def test_library_with_units_rejected(self):
        manifest = ServiceManifest(services={
            "fw": _svc("fw", kind="library", systemd_units=["fw.service"]),
        })
        with pytest.raises(ValidationFailure) as exc_info:
            validate_library_kind(manifest)
        assert any("systemd_units" in d for d in exc_info.value.details)

    def test_library_with_health_rejected(self):
        manifest = ServiceManifest(services={
            "fw": _svc("fw", kind="library", health="x.sh"),
        })
        with pytest.raises(ValidationFailure) as exc_info:
            validate_library_kind(manifest)
        assert any("health_check" in d for d in exc_info.value.details)

    def test_library_with_smoke_rejected(self):
        manifest = ServiceManifest(services={
            "fw": _svc("fw", kind="library", smoke="x.sh"),
        })
        with pytest.raises(ValidationFailure) as exc_info:
            validate_library_kind(manifest)
        assert any("smoke_test" in d for d in exc_info.value.details)

    def test_library_with_after_rejected(self):
        manifest = ServiceManifest(services={
            "fw": _svc("fw", kind="library", after=["net.target"]),
        })
        with pytest.raises(ValidationFailure) as exc_info:
            validate_library_kind(manifest)
        assert any("after ordering" in d for d in exc_info.value.details)

    def test_library_empty_runtime_accepted(self):
        manifest = ServiceManifest(services={"fw": _svc("fw", kind="library")})
        validate_library_kind(manifest)  # no raise


class TestSourceDirs:
    def test_existing_cmake_project_accepted(self, tmp_path: Path):
        (tmp_path / "proj").mkdir()
        (tmp_path / "proj" / "CMakeLists.txt").write_text("cmake_minimum_required(VERSION 3.10)\n")
        manifest = ServiceManifest(services={
            "svc": Service(
                id="svc", repository="proj", kind="daemon",
                build=BuildConfig(preset="x", targets=["svc"]),
                runtime=RuntimeConfig(),
            )
        })
        validate_source_dirs(manifest, tmp_path)

    def test_missing_dir_rejected(self, tmp_path: Path):
        manifest = ServiceManifest(services={
            "svc": Service(
                id="svc", repository="ghost", kind="daemon",
                build=BuildConfig(preset="x", targets=["svc"]),
                runtime=RuntimeConfig(),
            )
        })
        with pytest.raises(ValidationFailure) as exc_info:
            validate_source_dirs(manifest, tmp_path)
        assert any("does not exist" in d for d in exc_info.value.details)

    def test_missing_cmakelists_rejected(self, tmp_path: Path):
        (tmp_path / "proj").mkdir()
        manifest = ServiceManifest(services={
            "svc": Service(
                id="svc", repository="proj", kind="daemon",
                build=BuildConfig(preset="x", targets=["svc"]),
                runtime=RuntimeConfig(),
            )
        })
        with pytest.raises(ValidationFailure) as exc_info:
            validate_source_dirs(manifest, tmp_path)
        assert any("CMakeLists.txt" in d for d in exc_info.value.details)

    def test_workspace_escape_rejected(self, tmp_path: Path):
        manifest = ServiceManifest(services={
            "svc": Service(
                id="svc", repository="../../../../etc", kind="daemon",
                build=BuildConfig(preset="x", targets=["svc"]),
                runtime=RuntimeConfig(),
            )
        })
        with pytest.raises(ValidationFailure) as exc_info:
            validate_source_dirs(manifest, tmp_path)
        assert any("outside the approved workspace" in d for d in exc_info.value.details)


class TestValidateAllFramework:
    def test_framework_validates(self, project_root: Path):
        from tbox_build.manifest import Project
        project = Project(project_root)
        manifest = project.load_service_manifest()
        lock = project.load_dependency_lock()
        validate_all(manifest, project_root, lock)  # no raise
