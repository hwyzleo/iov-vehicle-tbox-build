"""Integration tests for CR-002: framework dry-run orchestration, SDK/rootfs
staging routing, recipe skip, and archive member ELF checks."""

from __future__ import annotations

import json
import struct
from pathlib import Path

import pytest

from tbox_build.manifest import Project
from tbox_build.orchestrator import (
    BuildOrchestrator,
    BuildConfig,
    ComponentInstallResult,
    ServiceResult,
)
from tbox_build.staging import StagingDir
from tbox_build.graph import DependencyGraph
from tbox_build.elfcheck import check_archive_members, check_staging, EM_AARCH64


class TestFrameworkDryRun:
    """Dry-run build plan for the framework-only release set."""

    def test_dry_run_framework_build_plan(self, project_root: Path):
        project = Project(project_root)
        config = BuildConfig(platform="orin", profile="debug", dry_run=True)
        orch = BuildOrchestrator(project, config)
        report = orch.build(set_id="tbox-framework-orin")

        assert report.status == "success"
        assert len(report.service_results) == 1
        sr = report.service_results[0]
        assert sr.id == "framework"
        assert sr.targets == [
            "framework-config",
            "framework-store",
            "framework-log",
            "framework-ipc",
            "framework-hash",
            "framework-application",
        ]
        # configure + build skipped; two component installs skipped
        assert sr.steps["configure"] == "skipped"
        assert sr.steps["build"] == "skipped"
        install_steps = [k for k in sr.steps if k.startswith("install:")]
        assert sorted(install_steps) == ["install:framework-runtime", "install:framework-sdk"]
        assert all(sr.steps[k] == "skipped" for k in install_steps)
        # two components recorded with correct staging routing
        assert len(sr.components) == 2
        comp_by_name = {c.name: c for c in sr.components}
        assert comp_by_name["framework-sdk"].staging == "sdk"
        assert comp_by_name["framework-runtime"].staging == "rootfs"

    def test_dry_run_records_target_dependencies(self, project_root: Path):
        project = Project(project_root)
        config = BuildConfig(platform="orin", profile="debug", dry_run=True)
        orch = BuildOrchestrator(project, config)
        report = orch.build(set_id="tbox-framework-orin")
        assert report.target_dependencies == ["yaml-cpp"]

    def test_dry_run_build_cmd_uses_all_targets(self, project_root: Path):
        project = Project(project_root)
        config = BuildConfig(platform="orin", profile="debug", dry_run=True)
        orch = BuildOrchestrator(project, config)
        from tbox_build.manifest import load_service_manifest
        manifest = load_service_manifest(project.service_manifest_path)
        fw = manifest.get("framework")
        cmd = orch._build_cmd(fw)
        # one cmake --build invocation with --target <all targets>
        assert "--target" in cmd
        idx = cmd.index("--target")
        targets_in_cmd = cmd[idx + 1: idx + 1 + len(fw.build.targets)]
        assert targets_in_cmd == fw.build.targets

    def test_dry_run_install_destdir_routing(self, project_root: Path):
        project = Project(project_root)
        config = BuildConfig(platform="orin", profile="debug", dry_run=True)
        orch = BuildOrchestrator(project, config)
        from tbox_build.manifest import load_service_manifest
        manifest = load_service_manifest(project.service_manifest_path)
        fw = manifest.get("framework")
        cmds = orch._install_cmds(fw)
        # sdk component -> sdk/framework, runtime component -> install-root
        for component, cmd, destdir in cmds:
            if component.staging == "sdk":
                assert destdir == orch.staging.sdk_dir("framework")
            elif component.staging == "rootfs":
                assert destdir == orch.staging.install_root
        assert len(cmds) == 2


class TestFrameworkCmakeEnv:
    def test_dep_staging_injected(self, project_root: Path):
        project = Project(project_root)
        config = BuildConfig(platform="orin", profile="debug", dry_run=True)
        orch = BuildOrchestrator(project, config)
        from tbox_build.manifest import load_service_manifest
        manifest = load_service_manifest(project.service_manifest_path)
        fw = manifest.get("framework")
        env = orch._cmake_env(fw)
        assert "TBOX_DEP_STAGING" in env
        assert env["TBOX_DEP_STAGING"].endswith("deps")
        # framework is a leaf library: no SDK staging
        assert "TBOX_SDK_STAGING" not in env


class TestReleaseBuildRejectsPendingSha:
    def test_release_build_fails_on_pending_source(self, project_root: Path, monkeypatch):
        project = Project(project_root)
        # Force a PENDING lock to exercise the release guard, independent of
        # the real (now-pinned) lock.yaml value.
        from tbox_build.manifest import DependencyLock, DependencyEntry
        pending_entry = DependencyEntry(
            name="yaml-cpp", version="0.8.0", source_url="http://x",
            source_sha256="PENDING-FILL-BEFORE-RELEASE",
            license="BSD-3-Clause", boundary="TARGET",
            architecture="aarch64", linkage="static",
        )
        # nlohmann-json (added by CR-009) must be present in the patched
        # lock so validate_target_dependencies does not short-circuit on a
        # missing-entry error before the PENDING release guard runs.
        nlohmann_entry = DependencyEntry(
            name="nlohmann-json", version="3.11.2", source_url="http://x",
            source_sha256="d69f9deb6a75e2580465c6c4c5111b89c4dc2fa94e3a85fcd2ffcd9a143d9273",
            license="MIT", boundary="TARGET",
            architecture="aarch64", linkage="static",
        )
        monkeypatch.setattr(
            project, "load_dependency_lock",
            lambda: DependencyLock(dependencies={
                "yaml-cpp": pending_entry,
                "nlohmann-json": nlohmann_entry,
            }),
        )
        config = BuildConfig(platform="orin", profile="release", dry_run=True)
        orch = BuildOrchestrator(project, config)
        report = orch.build(set_id="tbox-framework-orin")
        # yaml-cpp sha256 is PENDING -> release build must fail
        assert report.status == "failed"
        assert any("PENDING" in e or "not pinned" in e for e in report.errors)


class TestArchiveMemberCheck:
    @staticmethod
    def _make_elf(machine: int) -> bytes:
        e_ident = b"\x7fELF" + bytes([2, 1, 1, 0]) + b"\x00" * 8
        header = e_ident
        header += struct.pack("<H", 1)
        header += struct.pack("<H", machine)
        header += struct.pack("<I", 1)
        header += b"\x00" * (64 - len(header))
        return header

    @staticmethod
    def _make_ar(members):
        out = b"!<arch>\n"
        for name, data in members:
            name_field = (name + "/")[:16].ljust(16)
            header = name_field.encode()
            header += b"0           "
            header += b"0     "
            header += b"0     "
            header += b"100644  "
            header += str(len(data)).encode().ljust(10)
            header += b"`\n"
            out += header + data
            if len(data) % 2 == 1:
                out += b"\n"
        return out

    def test_aarch64_archive_passes(self, tmp_path: Path):
        archive = tmp_path / "libyaml-cpp.a"
        archive.write_bytes(self._make_ar([("yaml.o", self._make_elf(EM_AARCH64))]))
        checked, violations = check_archive_members(archive)
        assert checked == 1
        assert violations == []

    def test_x86_archive_fails(self, tmp_path: Path):
        archive = tmp_path / "libbad.a"
        archive.write_bytes(self._make_ar([("bad.o", self._make_elf(62))]))
        checked, violations = check_archive_members(archive)
        assert checked == 1
        assert len(violations) == 1

    def test_staging_check_catches_x86_archive(self, tmp_path: Path):
        staging = tmp_path / "install-root" / "usr" / "lib"
        staging.mkdir(parents=True)
        (staging / "libbad.a").write_bytes(self._make_ar([("bad.o", self._make_elf(62))]))
        results = check_staging(tmp_path / "install-root")
        all_violations = [v for r in results for v in r.violations]
        assert any("not AArch64" in v or "62" in v for v in all_violations)


class TestArtifactManifestOwnership:
    """Non-dry-run artifact manifest ownership attribution.

    Regression test for the sdk-staging owner lookup miss: sdk artifacts must
    be attributed to their owner service and install component instead of
    falling back to "unknown". Exercises the real snapshot -> manifest
    pipeline (no CMake) by creating staged files and deriving
    ``installed_files`` via the orchestrator's own snapshot helpers so the
    path basis matches production exactly.
    """

    @staticmethod
    def _write(destdir: Path, rel: str, content: bytes = b"data\n") -> None:
        p = destdir / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(content)

    def test_sdk_and_rootfs_artifacts_attributed(self, project_root: Path, tmp_path: Path):
        project = Project(project_root)
        config = BuildConfig(platform="orin", profile="debug", dry_run=False)
        orch = BuildOrchestrator(project, config)
        # Redirect staging to tmp so the real out/ tree is untouched.
        orch.staging = StagingDir(tmp_path, config.platform, config.profile)
        orch.staging.prepare()

        # Simulate a real framework install into the per-service sdk DESTDIR
        # (sdk/framework) and the shared rootfs DESTDIR (install-root).
        sdk_destdir = orch.staging.component_destdir("framework", "sdk")
        rootfs_destdir = orch.staging.component_destdir("framework", "rootfs")

        sdk_rels = [
            "usr/include/tbox-framework/application.h",
            "usr/lib/cmake/framework/framework-config.cmake",
        ]
        rootfs_rels = ["usr/lib/libframework-runtime.so"]

        # Snapshot the (empty) DESTDIRs before install, exactly as
        # _build_service does, so installed_files carry the real path basis.
        sdk_before = orch._snapshot_under(sdk_destdir)
        rootfs_before = orch._snapshot_under(rootfs_destdir)

        for rel in sdk_rels:
            self._write(sdk_destdir, rel)
        for rel in rootfs_rels:
            self._write(rootfs_destdir, rel)

        sdk_installed = orch._new_files_under(sdk_destdir, sdk_before)
        rootfs_installed = orch._new_files_under(rootfs_destdir, rootfs_before)

        sdk_comp = ComponentInstallResult(
            name="framework-sdk", staging="sdk",
            destdir=str(sdk_destdir), status="success",
            installed_files=sdk_installed,
        )
        rt_comp = ComponentInstallResult(
            name="framework-runtime", staging="rootfs",
            destdir=str(rootfs_destdir), status="success",
            installed_files=rootfs_installed,
        )
        sr = ServiceResult(
            id="framework", status="success",
            targets=["framework-application"],
            components=[sdk_comp, rt_comp],
            installed_files=sorted(sdk_installed + rootfs_installed),
        )

        service_manifest = project.load_service_manifest()
        lock = project.load_dependency_lock()
        manifest = orch._generate_artifact_manifest(
            service_manifest, lock, git_commit="deadbeef",
            service_results=[sr],
        )

        by_path = {e.path: e for e in manifest.entries}

        # --- sdk artifacts: the bug made these "unknown" ---
        for rel in sdk_rels:
            entry = by_path[f"sdk/framework/{rel}"]
            assert entry.path.startswith("sdk/framework/")
            assert entry.owner_service == "framework"
            assert entry.install_component == "framework-sdk"
            assert entry.staging == "sdk"

        # --- rootfs artifacts: unaffected by the bug; guard regressions ---
        for rel in rootfs_rels:
            entry = by_path[f"rootfs/{rel}"]
            assert entry.owner_service == "framework"
            assert entry.install_component == "framework-runtime"
            assert entry.staging == "rootfs"

        # No artifact may be unattributed.
        assert all(e.owner_service != "unknown" for e in manifest.entries)
        assert all(e.install_component != "unknown" for e in manifest.entries)
        assert all(e.staging != "unknown" for e in manifest.entries)
