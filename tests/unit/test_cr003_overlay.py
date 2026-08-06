"""Unit tests for the platform config overlay algorithm (BUILD CR-003 §6)."""

from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from tbox_build.manifest import Project, ServiceManifest
from tbox_build.orchestrator import BuildOrchestrator, BuildConfig, OverlayReport
from tbox_build.staging import sha256_file

_PLATFORM_YAML = """\
platform: orin
architecture: aarch64
rootfs:
  id: orin-r35.3.1
  repository_path: sysroots/orin-r35.3.1
toolchain:
  target_triple: aarch64-linux-gnu
  sysroot: orin-r35.3.1
"""
_SYSROOT_YAML = """\
sysroot:
  id: orin-r35.3.1
  digest: test
  import_status: verified
"""
_DEPLOYMENT_YAML = """\
version: 1
platforms:
  orin:
    files:
      - path: /etc/tbox/common.yaml
        owner: build
        category: release-managed
        deploy_policy: replace
      - path_glob: /etc/tbox/conf.d/*.yaml
        owner: service
        category: release-managed
        deploy_policy: replace
      - path_glob: /etc/tbox/credentials/**
        owner: provisioning
        category: device-managed
        deploy_policy: preserve
"""


def _make_project(tmp_path: Path) -> Project:
    manifests = tmp_path / "manifests"
    manifests.mkdir(parents=True, exist_ok=True)
    (manifests / "orin-platform.yaml").write_text(_PLATFORM_YAML)
    (manifests / "orin-r35.3.1.yaml").write_text(_SYSROOT_YAML)
    (manifests / "config-deployment.yaml").write_text(_DEPLOYMENT_YAML)
    return Project(tmp_path)


def _make_orchestrator(tmp_path: Path, dry_run: bool = False) -> BuildOrchestrator:
    project = _make_project(tmp_path)
    config = BuildConfig(platform="orin", profile="release", dry_run=dry_run)
    orch = BuildOrchestrator(project, config)
    orch.staging.prepare()
    return orch


def _write_overlay_files(root: Path, files: dict[str, str]) -> None:
    """Write files under configs/orin/rootfs/."""
    for rel, content in files.items():
        p = root / "configs" / "orin" / "rootfs" / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)


class TestOverlayBasic:
    def test_no_platform_dir_empty_overlay(self, tmp_path):
        orch = _make_orchestrator(tmp_path)
        report = orch._stage_platform_config_overlay(ServiceManifest({}), [])
        assert report.entries == []
        assert report.status == "success"

    def test_overlay_stages_files(self, tmp_path):
        _write_overlay_files(tmp_path, {
            "etc/tbox/common.yaml": "common:\n  store:\n    root: /var/tbox\n",
            "etc/tbox/conf.d/mqtt.yaml": "mqtt:\n  broker_host: x\n",
        })
        orch = _make_orchestrator(tmp_path)
        report = orch._stage_platform_config_overlay(ServiceManifest({}), [])
        targets = {e.target_path for e in report.entries}
        assert "/etc/tbox/common.yaml" in targets
        assert "/etc/tbox/conf.d/mqtt.yaml" in targets
        assert report.status == "success"

    def test_overlay_overwrites_and_records_prior(self, tmp_path):
        _write_overlay_files(tmp_path, {
            "etc/tbox/conf.d/mqtt.yaml": "mqtt:\n  broker_host: new\n",
        })
        orch = _make_orchestrator(tmp_path)
        # Simulate a service-installed default template already in install-root
        conf_d = orch.staging.install_root / "etc" / "tbox" / "conf.d"
        conf_d.mkdir(parents=True)
        default = conf_d / "mqtt.yaml"
        default.write_text("mqtt:\n  broker_host: old\n")
        prior_sha = sha256_file(default)

        report = orch._stage_platform_config_overlay(ServiceManifest({}), [])
        entry = next(e for e in report.entries if e.rel_path == "etc/tbox/conf.d/mqtt.yaml")
        assert entry.overwrote is True
        assert entry.prior_sha256 == prior_sha
        assert entry.sha256 != prior_sha
        # Final content is the overlay, not the default
        assert "new" in default.read_text()

    def test_no_prior_file_not_overwrote(self, tmp_path):
        _write_overlay_files(tmp_path, {
            "etc/tbox/conf.d/mqtt.yaml": "mqtt:\n  broker_host: x\n",
        })
        orch = _make_orchestrator(tmp_path)
        report = orch._stage_platform_config_overlay(ServiceManifest({}), [])
        entry = next(e for e in report.entries if e.rel_path == "etc/tbox/conf.d/mqtt.yaml")
        assert entry.overwrote is False
        assert entry.prior_sha256 is None

    def test_atomic_write_no_tmp_left(self, tmp_path):
        _write_overlay_files(tmp_path, {
            "etc/tbox/common.yaml": "common:\n  store:\n    root: /var/tbox\n",
        })
        orch = _make_orchestrator(tmp_path)
        orch._stage_platform_config_overlay(ServiceManifest({}), [])
        tbox = orch.staging.install_root / "etc" / "tbox"
        assert (tbox / "common.yaml").is_file()
        assert not list(tbox.glob("*.tmp"))

    def test_permission_normalized_to_0644(self, tmp_path):
        # Source file has 0755; overlay must normalize to 0644.
        root = tmp_path / "configs" / "orin" / "rootfs" / "etc" / "tbox"
        root.mkdir(parents=True)
        f = root / "common.yaml"
        f.write_text("common:\n  store:\n    root: /var/tbox\n")
        os.chmod(f, 0o755)
        orch = _make_orchestrator(tmp_path)
        orch._stage_platform_config_overlay(ServiceManifest({}), [])
        staged = orch.staging.install_root / "etc" / "tbox" / "common.yaml"
        assert stat.S_IMODE(staged.stat().st_mode) == 0o644


class TestOverlayPathValidation:
    def test_symlink_rejected(self, tmp_path):
        root = tmp_path / "configs" / "orin" / "rootfs" / "etc" / "tbox" / "conf.d"
        root.mkdir(parents=True)
        target = tmp_path / "external.yaml"
        target.write_text("evil")
        (root / "mqtt.yaml").write_text("mqtt:\n  x: 1\n")
        os.symlink(target, root / "link.yaml")
        orch = _make_orchestrator(tmp_path)
        report = orch._stage_platform_config_overlay(ServiceManifest({}), [])
        assert report.status == "failed"
        assert any("symlink" in e for e in report.errors)
        # The symlink must not be staged
        assert not (orch.staging.install_root / "etc" / "tbox" / "conf.d" / "link.yaml").exists()

    def test_unauthorized_target_fails(self, tmp_path):
        _write_overlay_files(tmp_path, {
            "etc/tbox/credentials/ca.key": "secret",
        })
        orch = _make_orchestrator(tmp_path)
        report = orch._stage_platform_config_overlay(ServiceManifest({}), [])
        assert report.status == "failed"
        assert any("device-managed" in e or "not declared" in e for e in report.errors)
        assert "/etc/tbox/credentials/ca.key" in report.unauthorized

    def test_non_regular_file_rejected(self, tmp_path):
        # FIFO is a non-regular file.
        root = tmp_path / "configs" / "orin" / "rootfs" / "etc" / "tbox" / "conf.d"
        root.mkdir(parents=True)
        (root / "mqtt.yaml").write_text("mqtt:\n  x: 1\n")
        try:
            os.mkfifo(root / "fifo.yaml")
        except OSError:
            pytest.skip("cannot create fifo on this platform")
        orch = _make_orchestrator(tmp_path)
        report = orch._stage_platform_config_overlay(ServiceManifest({}), [])
        assert report.status == "failed"
        assert any("non-regular" in e for e in report.errors)

    def test_dotdot_in_path_rejected(self, tmp_path):
        # The _is_safe_overlay_path static method catches '..' in rel parts.
        from tbox_build.orchestrator import BuildOrchestrator as BO
        src = tmp_path / "configs" / "orin" / "rootfs" / ".." / "evil.yaml"
        rel = Path("..") / "evil.yaml"
        overlay_root = tmp_path / "configs" / "orin" / "rootfs"
        err = BO._is_safe_overlay_path(src, rel, overlay_root)
        assert err is not None
        assert ".." in err


class TestOverlayStaleCleanup:
    def test_removed_platform_file_cleaned_on_incremental(self, tmp_path):
        _write_overlay_files(tmp_path, {
            "etc/tbox/conf.d/mqtt.yaml": "mqtt:\n  broker_host: x\n",
            "etc/tbox/conf.d/sec.yaml": "sec:\n  ipc: y\n",
        })
        orch = _make_orchestrator(tmp_path)
        # First overlay: stages both files, saves report.
        orch._stage_platform_config_overlay(ServiceManifest({}), [])
        assert (orch.staging.install_root / "etc" / "tbox" / "conf.d" / "sec.yaml").is_file()

        # Remove sec.yaml from the platform source.
        (tmp_path / "configs" / "orin" / "rootfs" / "etc" / "tbox" / "conf.d" / "sec.yaml").unlink()

        # Second overlay (incremental): must clean the stale sec.yaml.
        report = orch._stage_platform_config_overlay(ServiceManifest({}), [])
        assert "etc/tbox/conf.d/sec.yaml" in report.removed_stale
        assert not (orch.staging.install_root / "etc" / "tbox" / "conf.d" / "sec.yaml").exists()
        # mqtt.yaml still present.
        assert (orch.staging.install_root / "etc" / "tbox" / "conf.d" / "mqtt.yaml").is_file()

    def test_clean_incremental_consistency(self, tmp_path):
        _write_overlay_files(tmp_path, {
            "etc/tbox/common.yaml": "common:\n  store:\n    root: /var/tbox\n",
            "etc/tbox/conf.d/mqtt.yaml": "mqtt:\n  broker_host: x\n",
        })
        # Incremental build.
        orch1 = _make_orchestrator(tmp_path)
        r1 = orch1._stage_platform_config_overlay(ServiceManifest({}), [])
        sha1 = {e.rel_path: e.sha256 for e in r1.entries}

        # Clean build (fresh staging).
        import shutil
        shutil.rmtree(orch1.staging.root)
        orch2 = _make_orchestrator(tmp_path)
        r2 = orch2._stage_platform_config_overlay(ServiceManifest({}), [])
        sha2 = {e.rel_path: e.sha256 for e in r2.entries}

        assert sha1 == sha2


class TestOverlayValidation:
    def test_overlay_fails_on_secret_in_platform_config(self, tmp_path):
        _write_overlay_files(tmp_path, {
            "etc/tbox/common.yaml": "common:\n  store:\n    root: /var/tbox\n",
            "etc/tbox/conf.d/sec.yaml": "sec:\n  password: real-secret-value\n",
        })
        orch = _make_orchestrator(tmp_path)
        report = orch._stage_platform_config_overlay(ServiceManifest({}), [])
        assert report.status == "failed"
        assert any("secret" in e.lower() for e in report.errors)
        # The secret value must not appear in the report.
        report_json = report.to_json()
        assert "real-secret-value" not in report_json

    def test_overlay_fails_on_conf_d_inline_common(self, tmp_path):
        _write_overlay_files(tmp_path, {
            "etc/tbox/common.yaml": "common:\n  store:\n    root: /var/tbox\n",
            "etc/tbox/conf.d/mqtt.yaml": "common:\n  store:\n    root: /var/lib/tbox\nmqtt:\n  x: 1\n",
        })
        orch = _make_orchestrator(tmp_path)
        report = orch._stage_platform_config_overlay(ServiceManifest({}), [])
        assert report.status == "failed"
        assert any("forbidden" in e for e in report.errors)

    def test_overlay_report_saved(self, tmp_path):
        _write_overlay_files(tmp_path, {
            "etc/tbox/common.yaml": "common:\n  store:\n    root: /var/tbox\n",
        })
        orch = _make_orchestrator(tmp_path)
        orch._stage_platform_config_overlay(ServiceManifest({}), [])
        report_path = orch.staging.manifests_dir / "platform-overlay-report.json"
        assert report_path.is_file()
        import json
        data = json.loads(report_path.read_text())
        assert data["platform"] == "orin"
        assert len(data["entries"]) == 1

    def test_dry_run_does_not_write_files(self, tmp_path):
        _write_overlay_files(tmp_path, {
            "etc/tbox/common.yaml": "common:\n  store:\n    root: /var/tbox\n",
        })
        orch = _make_orchestrator(tmp_path, dry_run=True)
        report = orch._stage_platform_config_overlay(ServiceManifest({}), [])
        assert len(report.entries) == 1
        assert report.entries[0].sha256 == "(dry-run)"
        assert not (orch.staging.install_root / "etc" / "tbox" / "common.yaml").exists()


class TestPlatformAssetStaging:
    """Tests for BUILD-owned platform asset staging (tbox.target, §8.3)."""

    def test_stage_platform_assets_copies_target(self, tmp_path):
        """tbox.target is staged into install-root/usr/lib/systemd/system/."""
        project = _make_project(tmp_path)
        pkg_dir = tmp_path / "packaging" / "systemd"
        pkg_dir.mkdir(parents=True)
        (pkg_dir / "tbox.target").write_text(
            "[Unit]\n"
            "Description=TBOX\n"
            "Wants=\n"
            "  tbox-prov.service\n"
            "  tbox-sec.service\n"
            "[Install]\n"
            "WantedBy=multi-user.target\n"
        )
        config = BuildConfig(platform="orin", profile="release")
        orch = BuildOrchestrator(project, config)
        orch.staging.prepare()

        sm = ServiceManifest({})  # empty; no built units
        staged = orch._stage_platform_assets(sm)

        assert len(staged) == 1
        assert staged[0] == "usr/lib/systemd/system/tbox.target"
        dst = orch.staging.install_root / "usr" / "lib" / "systemd" / "system" / "tbox.target"
        assert dst.is_file()
        # No built units -> Wants= should be empty
        content = dst.read_text()
        wants_line = next(l for l in content.splitlines() if l.startswith("Wants="))
        assert wants_line == "Wants="

    def test_filter_target_wants_keeps_built_units_only(self):
        """_filter_target_wants only keeps units in the built set."""
        content = (
            "[Unit]\n"
            "Description=TBOX\n"
            "Wants=\n"
            "  tbox-prov.service\n"
            "  tbox-sec.service\n"
            "  tbox-mqtt.service\n"
            "After=\n"
            "  tbox-prov.service\n"
            "  tbox-sec.service\n"
            "[Install]\n"
            "WantedBy=multi-user.target\n"
        )
        built = {"tbox-prov.service", "tbox-sec.service"}
        filtered = BuildOrchestrator._filter_target_wants(content, built)
        assert "tbox-prov.service" in filtered
        assert "tbox-sec.service" in filtered
        assert "tbox-mqtt.service" not in filtered

    def test_stage_platform_assets_no_dir(self, tmp_path):
        """No packaging/systemd/ dir -> no assets staged."""
        project = _make_project(tmp_path)
        config = BuildConfig(platform="orin", profile="release")
        orch = BuildOrchestrator(project, config)
        orch.staging.prepare()
        staged = orch._stage_platform_assets(ServiceManifest({}))
        assert staged == []
