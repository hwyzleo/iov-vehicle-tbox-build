"""Unit tests for manifest loading and parsing."""

from __future__ import annotations

from pathlib import Path

import pytest

from tbox_build.errors import ManifestError
from tbox_build.manifest import (
    load_yaml,
    load_service_manifest,
    load_release_set_manifest,
    load_platform_manifest,
    Project,
)


class TestLoadYaml:
    def test_load_valid_yaml(self, project_root: Path):
        path = project_root / "manifests" / "services.yaml"
        data = load_yaml(path)
        assert "services" in data
        assert "tbox-hello-lib" in data["services"]
        assert "tbox-hello-cli" in data["services"]

    def test_load_missing_file(self, tmp_path: Path):
        with pytest.raises(ManifestError, match="not found"):
            load_yaml(tmp_path / "nonexistent.yaml")

    def test_load_non_dict(self, tmp_path: Path):
        path = tmp_path / "list.yaml"
        path.write_text("- item1\n- item2\n")
        with pytest.raises(ManifestError, match="Expected YAML mapping"):
            load_yaml(path)

    def test_load_empty_file(self, tmp_path: Path):
        path = tmp_path / "empty.yaml"
        path.write_text("")
        data = load_yaml(path)
        assert data == {}


class TestServiceManifest:
    def test_load_service_manifest(self, project_root: Path):
        manifest = load_service_manifest(project_root / "manifests" / "services.yaml")
        # CR-012: sec 服务加入后共 5 个服务
        assert len(manifest) == 5
        assert "tbox-hello-lib" in manifest
        assert "tbox-hello-cli" in manifest
        assert "framework" in manifest
        assert "prov" in manifest

    def test_service_fields(self, project_root: Path):
        manifest = load_service_manifest(project_root / "manifests" / "services.yaml")
        svc = manifest.get("tbox-hello-lib")
        assert svc is not None
        assert svc.repository == "examples/minimal"
        assert svc.build.targets == ["tbox-hello-lib"]
        assert svc.build.preset == "orin-release"
        assert svc.build.service_dependencies == []
        assert svc.build.target_dependencies == []
        assert len(svc.build.install_components) == 1
        assert svc.build.install_components[0].name == "tbox-hello-lib-runtime"
        assert svc.build.install_components[0].staging == "rootfs"

    def test_service_with_dependency(self, project_root: Path):
        manifest = load_service_manifest(project_root / "manifests" / "services.yaml")
        svc = manifest.get("tbox-hello-cli")
        assert svc is not None
        assert svc.build.service_dependencies == ["tbox-hello-lib"]

    def test_runtime_config(self, project_root: Path):
        manifest = load_service_manifest(project_root / "manifests" / "services.yaml")
        svc = manifest.get("tbox-hello-cli")
        assert svc is not None
        assert "tbox-hello.service" in svc.runtime.systemd_units
        assert svc.runtime.health_check == "tests/smoke/hello-health.sh"
        assert "/etc/tbox/hello" in svc.runtime.config_paths

    def test_effective_source_dir(self, project_root: Path):
        manifest = load_service_manifest(project_root / "manifests" / "services.yaml")
        svc = manifest.get("tbox-hello-lib")
        assert svc.effective_source_dir == "examples/minimal"

    def test_iter_services(self, project_root: Path):
        manifest = load_service_manifest(project_root / "manifests" / "services.yaml")
        ids = [s.id for s in manifest]
        assert "tbox-hello-lib" in ids
        assert "tbox-hello-cli" in ids


class TestReleaseSetManifest:
    def test_load_release_set(self, project_root: Path):
        manifest = load_release_set_manifest(
            project_root / "manifests" / "release-set.yaml"
        )
        rs = manifest.get("tbox-orin-minimal")
        assert rs is not None
        assert "tbox-hello-lib" in rs.services
        assert "tbox-hello-cli" in rs.services
        assert rs.platform == "orin"
        assert rs.profile == "release"


class TestPlatformManifest:
    def test_load_platform(self, project_root: Path):
        pm = load_platform_manifest(project_root / "manifests" / "orin-platform.yaml")
        assert pm.platform == "orin"
        assert pm.architecture == "aarch64"
        assert pm.rootfs_id == "orin-r35.3.1"
        assert pm.target_triple == "aarch64-linux-gnu"
        assert pm.cross_cc == "gcc-9"
        assert pm.sysroot_id == "orin-r35.3.1"


class TestProject:
    def test_project_paths(self, project_root: Path):
        project = Project(project_root)
        assert project.manifests_dir == project_root / "manifests"
        assert project.cmake_dir == project_root / "cmake"
        assert project.presets_path == project_root / "presets" / "CMakePresets.json"
        assert project.sysroot_path == project_root / "sysroots" / "orin-r35.3.1"

    def test_project_load_all(self, project_root: Path):
        project = Project(project_root)
        sm = project.load_service_manifest()
        # CR-012: sec 服务加入后共 5 个服务
        assert len(sm) == 5
        pm = project.load_platform_manifest()
        assert pm.platform == "orin"

    def test_project_invalid_root(self, tmp_path: Path):
        with pytest.raises(ManifestError, match="Not a TBOX Build project root"):
            Project(tmp_path)
