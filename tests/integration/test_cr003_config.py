"""Integration tests for BUILD CR-003: config layering, overlay, deploy plan.

Covers the end-to-end overlay pipeline (manifest -> staging -> validation ->
artifact manifest) and the policy-aware deploy plan, plus a regression check
that the existing framework release-set still builds (dry-run).
"""

from __future__ import annotations

import hashlib
import json
import tarfile
from pathlib import Path

import pytest

from tbox_build.deploy import Deployer
from tbox_build.manifest import Project
from tbox_build.orchestrator import BuildOrchestrator, BuildConfig
from tbox_build.manifest import ServiceManifest

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


def _make_package_with_configs(tmp_path: Path) -> Path:
    """Create a release package containing config files under install-root."""
    payload = tmp_path / "install-root"
    tbox = payload / "etc" / "tbox"
    (tbox / "conf.d").mkdir(parents=True)
    (tbox / "common.yaml").write_text("common:\n  store:\n    root: /var/tbox\n")
    (tbox / "conf.d" / "mqtt.yaml").write_text("mqtt:\n  broker_host: x\n")
    (payload / "usr" / "bin").mkdir(parents=True)
    (payload / "usr" / "bin" / "tbox_mqtt").write_text("#!/bin/true\n")

    pkg = tmp_path / "pkg.tar.gz"
    with tarfile.open(pkg, "w:gz") as tar:
        for p in payload.rglob("*"):
            tar.add(p, arcname=str(p.relative_to(tmp_path)))
    digest = hashlib.sha256(pkg.read_bytes()).hexdigest()
    pkg.with_suffix(".sha256").write_text(f"{digest}  {pkg.name}\n")
    return pkg


class TestCR003OverlayPipeline:
    def test_end_to_end_overlay_validation_and_manifest(self, tmp_path):
        # Set up platform config overlay files.
        root = tmp_path / "configs" / "orin" / "rootfs" / "etc" / "tbox"
        (root / "conf.d").mkdir(parents=True)
        (root / "common.yaml").write_text("common:\n  store:\n    root: /var/tbox\n")
        (root / "conf.d" / "mqtt.yaml").write_text("mqtt:\n  broker_host: orin-gw\n")

        project = _make_project(tmp_path)
        orch = BuildOrchestrator(project, BuildConfig(platform="orin", profile="release"))
        orch.staging.prepare()

        # Simulate a service default already installed.
        conf_d = orch.staging.install_root / "etc" / "tbox" / "conf.d"
        conf_d.mkdir(parents=True)
        (conf_d / "mqtt.yaml").write_text("mqtt:\n  broker_host: localhost\n")

        report = orch._stage_platform_config_overlay(ServiceManifest({}), [])
        assert report.status == "success"
        assert len(report.entries) == 2

        # The overlay overwrote mqtt.yaml with the platform value.
        mqtt_entry = next(e for e in report.entries
                          if e.rel_path == "etc/tbox/conf.d/mqtt.yaml")
        assert mqtt_entry.overwrote is True
        assert mqtt_entry.prior_sha256 is not None
        final = orch.staging.install_root / "etc" / "tbox" / "conf.d" / "mqtt.yaml"
        assert "orin-gw" in final.read_text()

        # common.yaml has no prior (newly staged).
        common_entry = next(e for e in report.entries
                            if e.rel_path == "etc/tbox/common.yaml")
        assert common_entry.overwrote is False

        # Validation passed (common single-source, no inline common, no secrets).
        assert report.validation_summary["common_ok"] is True
        assert report.validation_summary["conf_d_ok"] is True
        assert report.validation_summary["secret_findings"] == 0

    def test_overlay_report_persisted_with_sha256(self, tmp_path):
        root = tmp_path / "configs" / "orin" / "rootfs" / "etc" / "tbox"
        root.mkdir(parents=True)
        (root / "common.yaml").write_text("common:\n  store:\n    root: /var/tbox\n")

        project = _make_project(tmp_path)
        orch = BuildOrchestrator(project, BuildConfig(platform="orin", profile="release"))
        orch.staging.prepare()
        orch._stage_platform_config_overlay(ServiceManifest({}), [])

        report_path = orch.staging.manifests_dir / "platform-overlay-report.json"
        data = json.loads(report_path.read_text())
        assert data["status"] == "success"
        assert len(data["entries"]) == 1
        assert len(data["entries"][0]["sha256"]) == 64
        assert data["entries"][0]["deploy_policy"] == "replace"

    def test_overlay_blocks_device_managed_secret(self, tmp_path):
        root = tmp_path / "configs" / "orin" / "rootfs" / "etc" / "tbox" / "credentials"
        root.mkdir(parents=True)
        (root / "device.key").write_text("-----BEGIN RSA PRIVATE KEY-----\nMIIB\n-----END RSA PRIVATE KEY-----\n")

        project = _make_project(tmp_path)
        orch = BuildOrchestrator(project, BuildConfig(platform="orin", profile="release"))
        orch.staging.prepare()
        report = orch._stage_platform_config_overlay(ServiceManifest({}), [])
        assert report.status == "failed"
        # The credentials path is unauthorized AND a secret.
        assert "/etc/tbox/credentials/device.key" in report.unauthorized


class TestCR003DeployPlan:
    def test_dry_run_deploy_includes_config_plan(self, tmp_path, project_root):
        pkg = _make_package_with_configs(tmp_path)
        deployer = Deployer(
            Project(project_root), target_host="orin.local",
            target_user="tbox", identity="/k/id",
        )
        report = deployer.deploy(pkg, execute=False)
        assert report.status == "success (dry-run)"
        names = [s.name for s in report.steps]
        assert "config-plan" in names
        assert "config-install" in names
        assert "install" in names

        # The config plan is recorded in the report.
        assert report.config_plan is not None
        assert report.config_plan["replace_count"] >= 2  # common + mqtt
        assert report.config_plan["violations"] == 0

    def test_install_excludes_tbox_config(self, tmp_path, project_root):
        pkg = _make_package_with_configs(tmp_path)
        deployer = Deployer(
            Project(project_root), target_host="orin.local",
            target_user="tbox", identity="/k/id",
        )
        report = deployer.deploy(pkg, execute=False)
        install = next(s for s in report.steps if s.name == "install")
        assert "--exclude=etc/tbox/**" in install.commands[0]

    def test_config_install_has_replace_commands(self, tmp_path, project_root):
        pkg = _make_package_with_configs(tmp_path)
        deployer = Deployer(
            Project(project_root), target_host="orin.local",
            target_user="tbox", identity="/k/id",
        )
        report = deployer.deploy(pkg, execute=False)
        config_install = next(s for s in report.steps if s.name == "config-install")
        joined = "\n".join(config_install.commands)
        assert "install -m 644" in joined
        assert "/etc/tbox/common.yaml" in joined
        assert "/etc/tbox/conf.d/mqtt.yaml" in joined

    def test_authority_violation_fails_deploy(self, tmp_path, project_root):
        """A package containing a device-managed file fails the deploy plan."""
        payload = tmp_path / "install-root"
        tbox = payload / "etc" / "tbox" / "credentials"
        tbox.mkdir(parents=True)
        (tbox / "device.key").write_text("secret")
        pkg = tmp_path / "pkg.tar.gz"
        with tarfile.open(pkg, "w:gz") as tar:
            for p in payload.rglob("*"):
                tar.add(p, arcname=str(p.relative_to(tmp_path)))
        digest = hashlib.sha256(pkg.read_bytes()).hexdigest()
        pkg.with_suffix(".sha256").write_text(f"{digest}  {pkg.name}\n")

        deployer = Deployer(
            Project(project_root), target_host="orin.local",
            target_user="tbox", identity="/k/id",
        )
        report = deployer.deploy(pkg, execute=False)
        assert report.status == "failed"
        assert any("device-managed" in e for e in report.errors)


class TestCR003Regression:
    def test_framework_release_set_dry_run_no_regression(self, project_root):
        """The framework-only release set still builds (dry-run) with CR-003."""
        project = Project(project_root)
        orch = BuildOrchestrator(
            project, BuildConfig(platform="orin", profile="release", dry_run=True),
        )
        report = orch.build(set_id="tbox-framework-orin")
        assert report.status == "success"
        # framework is a library; no config overlay expected, but pipeline runs.
        assert len(report.service_results) == 1
        assert report.service_results[0].id == "framework"

    def test_existing_unit_tests_pass(self, project_root):
        """The real services.yaml with config_validation validates cleanly."""
        project = Project(project_root)
        sm = project.load_service_manifest()
        from tbox_build.validator import validate_all
        # Should not raise.
        validate_all(sm, project.root)
        # Services with config_validation have target_path in config_paths.
        for svc in sm:
            cv = svc.runtime.config_validation
            if cv:
                assert cv.target_path in svc.runtime.config_paths

    def test_real_config_deployment_manifest_loads(self, project_root):
        """The real manifests/config-deployment.yaml loads and matches."""
        project = Project(project_root)
        cdm = project.load_config_deployment_manifest()
        assert cdm.version == 1
        r = cdm.match("orin", "/etc/tbox/common.yaml")
        assert r is not None and r.category == "release-managed"
        r = cdm.match("orin", "/etc/tbox/credentials/x")
        assert r is not None and r.category == "device-managed"

    def test_real_platform_configs_have_no_inline_common(self, project_root):
        """The migrated configs/orin/rootfs/ conf.d files have no common:."""
        conf_d = project_root / "configs" / "orin" / "rootfs" / "etc" / "tbox" / "conf.d"
        assert conf_d.is_dir()
        from tbox_build.config_validation import check_conf_d_no_inline_common
        errors = check_conf_d_no_inline_common(conf_d)
        assert errors == [], f"conf.d has inline common: {errors}"

    def test_real_common_yaml_is_valid(self, project_root):
        """The real configs/orin/rootfs/etc/tbox/common.yaml is valid."""
        common = project_root / "configs" / "orin" / "rootfs" / "etc" / "tbox" / "common.yaml"
        from tbox_build.config_validation import validate_common_yaml
        assert validate_common_yaml(common) == []

    def test_config_deployment_covers_vsomeip_json(self, project_root):
        """vsomeip.json (strong-coupling exception) has a replace policy (§8.6.7).

        Without an explicit /etc/tbox/someip/** rule, vsomeip.json would
        default to preserve, silently preventing the release-managed SOME/IP
        runtime config from being updated on the device.
        """
        project = Project(project_root)
        cdm = project.load_config_deployment_manifest()
        rule = cdm.match("orin", "/etc/tbox/someip/vsomeip.json")
        assert rule is not None, \
            "/etc/tbox/someip/** must be declared in config-deployment.yaml"
        assert rule.category == "release-managed"
        assert rule.deploy_policy == "replace"

    def test_config_deployment_covers_all_service_config_paths(
        self, project_root
    ):
        """Every real service config_path under /etc/tbox/ is matched."""
        project = Project(project_root)
        cdm = project.load_config_deployment_manifest()
        sm = project.load_service_manifest()
        unmatched: list[str] = []
        for svc in sm:
            for cp in svc.runtime.config_paths:
                if not cp.startswith("/etc/tbox/"):
                    continue
                if cdm.match("orin", cp) is None:
                    unmatched.append(f"{svc.id}: {cp}")
        # tbox-hello-cli uses /etc/tbox/hello (minimal example, not a real
        # release-managed config); all real services must be covered.
        real_unmatched = [u for u in unmatched if not u.startswith("tbox-hello")]
        assert real_unmatched == [], \
            f"config_paths not covered by config-deployment.yaml: {real_unmatched}"

    def test_someip_config_validation_declared(self, project_root):
        """SOMEIP service declares config_validation for schema checking."""
        project = Project(project_root)
        sm = project.load_service_manifest()
        someip = sm.get("someip")
        assert someip is not None
        cv = someip.runtime.config_validation
        assert cv is not None, "someip must declare config_validation"
        assert cv.target_path == "/etc/tbox/conf.d/someip.yaml"
        assert cv.target_path in someip.runtime.config_paths

    def test_tbox_target_lists_all_services(self, project_root):
        """tbox.target aggregates all TBOX daemon services (§8.3)."""
        target = project_root / "packaging" / "systemd" / "tbox.target"
        assert target.is_file()
        content = target.read_text()
        for unit in ["tbox-prov.service", "tbox-sec.service",
                      "tbox-mqtt.service", "tbox-tsp.service",
                      "tbox-someip.service"]:
            assert unit in content, f"{unit} missing from tbox.target"
        # Must not change single-service semantics (Wants, not Requires)
        directive_lines = [
            l for l in content.splitlines()
            if l.strip() and not l.strip().startswith("#")
        ]
        assert not any(l.startswith("Requires=") for l in directive_lines), \
            "tbox.target must use Wants= (not Requires=) to avoid changing " \
            "single-service runtime semantics"
