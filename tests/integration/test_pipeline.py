"""Integration tests for TBOX Build pipeline.

These tests run on macOS without Docker and verify:
  * Manifest parsing, schema validation and dependency ordering
  * Dry-run build plan generation
  * ELF checking with real sysroot files
  * Full validation flow end-to-end
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tbox_build.manifest import Project
from tbox_build.schema import validate_service_manifest, validate_release_set_manifest
from tbox_build.validator import validate_all
from tbox_build.graph import DependencyGraph
from tbox_build.orchestrator import BuildOrchestrator, BuildConfig
from tbox_build.elfcheck import check_staging, EM_AARCH64, _ELFCLASS64
from tbox_build.staging import StagingDir


class TestEndToEndValidation:
    """End-to-end manifest validation flow."""

    def test_full_validation_pipeline(self, project_root: Path):
        project = Project(project_root)

        # 1. Schema validation
        from tbox_build.manifest import load_yaml
        svc_data = load_yaml(project.service_manifest_path)
        rs_data = load_yaml(project.release_set_manifest_path)
        validate_service_manifest(svc_data, project_root)
        validate_release_set_manifest(rs_data, project_root)

        # 2. Load manifests
        service_manifest = project.load_service_manifest()
        release_set_manifest = project.load_release_set_manifest()

        # 3. Cross-reference validation
        validate_all(service_manifest, project_root)

        # 4. Dependency graph
        graph = DependencyGraph(service_manifest)
        order = graph.build_order()
        # CR-012: sec 服务加入后共 5 个服务；CR-006: someip 加入后共 8 个
        assert len(order) == 8
        assert order.index("tbox-hello-lib") < order.index("tbox-hello-cli")
        assert "framework" in order
        assert "prov" in order
        assert order.index("prov") < order.index("sec")
        # CR-006: someip 依赖 framework/tsp/prov，拓扑序在 tsp 之后
        assert "someip" in order
        assert order.index("tsp") < order.index("someip")

        # 5. Release set closure
        rs = release_set_manifest.get("tbox-orin-minimal")
        assert rs is not None
        closure = graph.closure(rs.services)
        assert closure == {"tbox-hello-lib", "tbox-hello-cli"}


class TestDryRunBuild:
    """Dry-run build plan generation (no actual compilation)."""

    def test_dry_run_build_plan(self, project_root: Path):
        project = Project(project_root)
        config = BuildConfig(
            platform="orin",
            profile="release",
            dry_run=True,
        )
        orch = BuildOrchestrator(project, config)
        report = orch.build(set_id="tbox-orin-minimal")

        # In dry-run, steps are "skipped" but order is determined
        assert report.status == "success"
        assert len(report.service_results) == 2
        assert report.service_results[0].id == "tbox-hello-lib"
        assert report.service_results[1].id == "tbox-hello-cli"
        for sr in report.service_results:
            assert sr.steps.get("configure") == "skipped"
            assert sr.steps.get("build") == "skipped"
            install_steps = [v for k, v in sr.steps.items() if k.startswith("install:")]
            assert install_steps and all(s == "skipped" for s in install_steps)

    def test_dry_run_single_service(self, project_root: Path):
        project = Project(project_root)
        config = BuildConfig(platform="orin", profile="release", dry_run=True)
        orch = BuildOrchestrator(project, config)
        report = orch.build(service_id="tbox-hello-cli")

        # Should include both the dependency and the service
        assert report.status == "success"
        assert len(report.service_results) == 2
        ids = [sr.id for sr in report.service_results]
        assert "tbox-hello-lib" in ids
        assert "tbox-hello-cli" in ids

    def test_dry_run_build_report_saved(self, project_root: Path):
        project = Project(project_root)
        config = BuildConfig(platform="orin", profile="release", dry_run=True)
        orch = BuildOrchestrator(project, config)
        report = orch.build(set_id="tbox-orin-minimal")

        # Build report should be saved
        report_path = orch.staging.manifests_dir / "build-report.json"
        assert report_path.exists()
        with open(report_path) as f:
            saved = json.load(f)
        assert saved["status"] == "success"
        assert saved["release_set"] == "tbox-orin-minimal"


class TestDryRunPackage:
    """Dry-run packaging (staging exists from dry-run build)."""

    def test_build_report_structure(self, project_root: Path):
        project = Project(project_root)
        config = BuildConfig(platform="orin", profile="release", dry_run=True)
        orch = BuildOrchestrator(project, config)
        report = orch.build(set_id="tbox-orin-minimal")

        assert report.platform == "orin"
        assert report.profile == "release"
        assert report.duration_seconds >= 0
        assert len(report.service_results) == 2
        for sr in report.service_results:
            assert sr.status == "success"


class TestSysrootElfCheck:
    """ELF checking with real sysroot files (if available)."""

    def test_sysroot_libc_is_aarch64(self, project_root: Path, has_sysroot: bool):
        if not has_sysroot:
            pytest.skip("sysroot not available")

        from tbox_build.elfcheck import parse_elf
        libc = project_root / "sysroots" / "orin-r35.3.1" / "lib" / "aarch64-linux-gnu" / "libc.so.6"
        if libc.is_symlink():
            libc = libc.resolve()
        if not libc.exists():
            pytest.skip("libc.so.6 not found in sysroot")

        info = parse_elf(libc)
        assert info.is_elf
        assert info.elf_machine == EM_AARCH64
        assert info.elf_class == _ELFCLASS64

    def test_sysroot_libstdcpp_is_aarch64(self, project_root: Path, has_sysroot: bool):
        if not has_sysroot:
            pytest.skip("sysroot not available")

        from tbox_build.elfcheck import parse_elf
        lib = project_root / "sysroots" / "orin-r35.3.1" / "lib" / "aarch64-linux-gnu" / "libstdc++.so.6"
        if lib.is_symlink():
            lib = lib.resolve()
        if not lib.exists():
            pytest.skip("libstdc++.so.6 not found in sysroot")

        info = parse_elf(lib)
        assert info.is_elf
        assert info.elf_machine == EM_AARCH64
