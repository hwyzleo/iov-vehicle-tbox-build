"""Unit tests for CR-002 manifest model: InstallComponent, plural fields,
legacy dependency migration, framework entry and dependency lock parsing."""

from __future__ import annotations

from pathlib import Path

import pytest

from tbox_build.errors import ManifestError, SchemaValidationError
from tbox_build.manifest import (
    Project,
    load_service_manifest,
    load_dependency_lock,
    resolve_legacy_dependencies,
    InstallComponent,
    Service,
    BuildConfig,
    RuntimeConfig,
    ServiceManifest,
    DependencyLock,
)


class TestInstallComponentParsing:
    def test_object_components_parsed(self, tmp_path: Path):
        svc_yaml = tmp_path / "services.yaml"
        svc_yaml.write_text(
            "services:\n"
            "  fw:\n"
            "    source_dir: .\n"
            "    build:\n"
            "      preset: orin-release\n"
            "      targets: [fw]\n"
            "      install_components:\n"
            "        - name: fw-sdk\n"
            "          staging: sdk\n"
            "        - name: fw-runtime\n"
            "          staging: rootfs\n"
        )
        manifest = load_service_manifest(svc_yaml)
        svc = manifest.get("fw")
        assert svc.build.install_components == [
            InstallComponent(name="fw-sdk", staging="sdk"),
            InstallComponent(name="fw-runtime", staging="rootfs"),
        ]

    def test_singular_install_component_normalized(self, tmp_path: Path):
        svc_yaml = tmp_path / "services.yaml"
        svc_yaml.write_text(
            "services:\n"
            "  svc:\n"
            "    repository: .\n"
            "    build:\n"
            "      preset: x\n"
            "      target: a\n"
            "      install_component: a-runtime\n"
        )
        manifest = load_service_manifest(svc_yaml)
        assert manifest.get("svc").build.install_components == [
            InstallComponent(name="a-runtime", staging="rootfs")
        ]

    def test_singular_and_plural_install_rejected(self, tmp_path: Path):
        svc_yaml = tmp_path / "services.yaml"
        svc_yaml.write_text(
            "services:\n"
            "  svc:\n"
            "    repository: .\n"
            "    build:\n"
            "      preset: x\n"
            "      target: a\n"
            "      install_component: a-runtime\n"
            "      install_components:\n"
            "        - name: x\n"
            "          staging: rootfs\n"
        )
        with pytest.raises(SchemaValidationError):
            load_service_manifest(svc_yaml)

    def test_duplicate_target_rejected(self, tmp_path: Path):
        svc_yaml = tmp_path / "services.yaml"
        svc_yaml.write_text(
            "services:\n"
            "  svc:\n"
            "    repository: .\n"
            "    build:\n"
            "      preset: x\n"
            "      targets: [a, a]\n"
        )
        with pytest.raises(SchemaValidationError):
            load_service_manifest(svc_yaml)


class TestLegacyDependencyMigration:
    def test_service_id_migrated_to_service_deps(self, tmp_path: Path):
        svc_yaml = tmp_path / "services.yaml"
        svc_yaml.write_text(
            "services:\n"
            "  a:\n"
            "    repository: .\n"
            "    build:\n"
            "      preset: x\n"
            "      target: a\n"
            "      dependencies: [b]\n"
            "  b:\n"
            "    repository: .\n"
            "    build:\n"
            "      preset: x\n"
            "      target: b\n"
        )
        manifest = load_service_manifest(svc_yaml)
        resolve_legacy_dependencies(manifest, lock_names=set())
        assert manifest.get("a").build.service_dependencies == ["b"]
        assert not manifest.get("a").build.has_legacy_dependencies

    def test_lock_name_migrated_to_target_deps(self, tmp_path: Path):
        svc_yaml = tmp_path / "services.yaml"
        svc_yaml.write_text(
            "services:\n"
            "  fw:\n"
            "    source_dir: .\n"
            "    build:\n"
            "      preset: x\n"
            "      target: fw\n"
            "      dependencies: [yaml-cpp]\n"
        )
        manifest = load_service_manifest(svc_yaml)
        resolve_legacy_dependencies(manifest, lock_names={"yaml-cpp"})
        assert manifest.get("fw").build.target_dependencies == ["yaml-cpp"]
        assert manifest.get("fw").build.service_dependencies == []

    def test_ambiguous_legacy_rejected(self, tmp_path: Path):
        svc_yaml = tmp_path / "services.yaml"
        svc_yaml.write_text(
            "services:\n"
            "  shared:\n"
            "    repository: .\n"
            "    build:\n"
            "      preset: x\n"
            "      target: shared\n"
            "      dependencies: [shared]\n"
        )
        manifest = load_service_manifest(svc_yaml)
        with pytest.raises(SchemaValidationError, match="ambiguous"):
            resolve_legacy_dependencies(manifest, lock_names={"shared"})

    def test_unclassifiable_legacy_rejected(self, tmp_path: Path):
        svc_yaml = tmp_path / "services.yaml"
        svc_yaml.write_text(
            "services:\n"
            "  a:\n"
            "    repository: .\n"
            "    build:\n"
            "      preset: x\n"
            "      target: a\n"
            "      dependencies: [ghost]\n"
        )
        manifest = load_service_manifest(svc_yaml)
        with pytest.raises(SchemaValidationError, match="could not be classified"):
            resolve_legacy_dependencies(manifest, lock_names=set())


class TestFrameworkManifest:
    def test_framework_entry(self, project_root: Path):
        project = Project(project_root)
        manifest = project.load_service_manifest()
        fw = manifest.get("framework")
        assert fw is not None
        assert fw.kind == "library"
        assert fw.is_library
        assert fw.source_dir == "../iov-vehicle-tbox-framework"
        assert fw.build.targets == [
            "framework-config",
            "framework-store",
            "framework-log",
            "framework-ipc",
            "framework-hash",
            "framework-application",
        ]
        assert fw.build.install_components == [
            InstallComponent(name="framework-sdk", staging="sdk"),
            InstallComponent(name="framework-runtime", staging="rootfs"),
        ]
        assert fw.build.service_dependencies == []
        assert fw.build.target_dependencies == ["yaml-cpp"]
        assert fw.runtime.systemd_units == []

    def test_release_set_framework(self, project_root: Path):
        project = Project(project_root)
        rs = project.load_release_set_manifest()
        fw_set = rs.get("tbox-framework-orin")
        assert fw_set is not None
        assert fw_set.services == ["framework"]
        assert fw_set.platform == "orin"
        assert fw_set.profile == "release"


class TestDependencyLock:
    def test_load_lock(self, project_root: Path):
        project = Project(project_root)
        lock = project.load_dependency_lock()
        assert "yaml-cpp" in lock
        dep = lock.get("yaml-cpp")
        assert dep is not None
        assert dep.version == "0.8.0"
        assert dep.license == "BSD-3-Clause"
        assert dep.boundary == "TARGET"
        assert dep.architecture == "aarch64"
        assert dep.linkage == "static"
        assert dep.is_static
        assert dep.cmake_options["YAML_BUILD_SHARED_LIBS"] == "OFF"

    def test_pending_sha256_not_pinned(self, project_root: Path):
        project = Project(project_root)
        lock = project.load_dependency_lock()
        dep = lock.get("yaml-cpp")
        assert not dep.is_source_pinned

    def test_filled_sha256_pinned(self, tmp_path: Path):
        lock_yaml = tmp_path / "lock.yaml"
        lock_yaml.write_text(
            "dependencies:\n"
            "  foo:\n"
            "    version: '1.0'\n"
            "    source:\n"
            "      url: http://x\n"
            "      sha256: abc123def\n"
            "    license: MIT\n"
            "    boundary: TARGET\n"
            "    architecture: aarch64\n"
            "    linkage: static\n"
        )
        lock = load_dependency_lock(lock_yaml)
        assert lock.get("foo").is_source_pinned

    def test_list_form_rejected(self, tmp_path: Path):
        lock_yaml = tmp_path / "lock.yaml"
        lock_yaml.write_text("dependencies: []\n")
        with pytest.raises(ManifestError, match="mapping keyed by"):
            load_dependency_lock(lock_yaml)
