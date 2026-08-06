"""Unit tests for config-deployment manifest matching and planner (CR-003 §7)."""

from __future__ import annotations

from pathlib import Path

from tbox_build.deploy import ConfigDeployPlanner
from tbox_build.manifest import (
    ConfigDeploymentManifest,
    ConfigDeployRule,
    load_config_deployment_manifest,
)

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
      - path: /etc/tbox/device.yaml
        owner: provisioning
        category: device-managed
        deploy_policy: preserve
      - path: /etc/tbox/local.yaml
        owner: provisioning
        category: device-managed
        deploy_policy: create_if_missing
"""


def _load_cdm(tmp_path: Path) -> ConfigDeploymentManifest:
    f = tmp_path / "config-deployment.yaml"
    f.write_text(_DEPLOYMENT_YAML)
    return load_config_deployment_manifest(f)


class TestConfigDeploymentMatching:
    def test_exact_path_match(self, tmp_path):
        cdm = _load_cdm(tmp_path)
        r = cdm.match("orin", "/etc/tbox/common.yaml")
        assert r is not None
        assert r.owner == "build"
        assert r.deploy_policy == "replace"

    def test_glob_match_conf_d(self, tmp_path):
        cdm = _load_cdm(tmp_path)
        r = cdm.match("orin", "/etc/tbox/conf.d/mqtt.yaml")
        assert r is not None
        assert r.category == "release-managed"

    def test_glob_match_credentials_recursive(self, tmp_path):
        cdm = _load_cdm(tmp_path)
        r = cdm.match("orin", "/etc/tbox/credentials/sub/ca.key")
        assert r is not None
        assert r.category == "device-managed"

    def test_exact_over_glob_priority(self, tmp_path):
        cdm = _load_cdm(tmp_path)
        # /etc/tbox/device.yaml is exact; /etc/tbox/credentials/** is glob.
        r = cdm.match("orin", "/etc/tbox/device.yaml")
        assert r.path == "/etc/tbox/device.yaml"

    def test_unmatched_returns_none(self, tmp_path):
        cdm = _load_cdm(tmp_path)
        assert cdm.match("orin", "/etc/tbox/unknown.yaml") is None

    def test_unknown_platform_returns_none(self, tmp_path):
        cdm = _load_cdm(tmp_path)
        assert cdm.match("x86", "/etc/tbox/common.yaml") is None


class TestConfigDeployPlanner:
    def _payload(self, tmp_path: Path) -> Path:
        root = tmp_path / "payload"
        tbox = root / "etc" / "tbox"
        (tbox / "conf.d").mkdir(parents=True)
        (tbox / "credentials").mkdir(parents=True)
        (tbox / "common.yaml").write_text("common:\n  store:\n    root: /var/tbox\n")
        (tbox / "conf.d" / "mqtt.yaml").write_text("mqtt:\n  broker_host: x\n")
        (tbox / "conf.d" / "sec.yaml").write_text("sec:\n  ipc: y\n")
        (tbox / "credentials" / "ca.crt").write_text("cert")
        (tbox / "device.yaml").write_text("vin: test\n")
        (tbox / "local.yaml").write_text("local: value\n")
        return root

    def test_replace_action(self, tmp_path):
        cdm = _load_cdm(tmp_path)
        planner = ConfigDeployPlanner(cdm, "orin")
        plan = planner.plan(self._payload(tmp_path))
        replaces = [a for a in plan.actions if a.action == "replace"]
        assert any(a.target_path == "/etc/tbox/common.yaml" for a in replaces)
        assert any(a.target_path == "/etc/tbox/conf.d/mqtt.yaml" for a in replaces)
        assert plan.replace_count == 3

    def test_preserve_action_device_managed(self, tmp_path):
        cdm = _load_cdm(tmp_path)
        planner = ConfigDeployPlanner(cdm, "orin")
        plan = planner.plan(self._payload(tmp_path))
        preserves = [a for a in plan.actions if a.target_path == "/etc/tbox/device.yaml"]
        assert len(preserves) == 1
        assert preserves[0].action == "preserve"

    def test_create_if_missing_when_device_lacks_file(self, tmp_path):
        cdm = _load_cdm(tmp_path)
        planner = ConfigDeployPlanner(cdm, "orin")
        plan = planner.plan(self._payload(tmp_path))
        creates = [a for a in plan.actions if a.action == "create_if_missing"]
        assert len(creates) == 1
        assert creates[0].target_path == "/etc/tbox/local.yaml"
        assert "will initialize" in creates[0].reason

    def test_create_if_missing_when_device_has_file(self, tmp_path):
        cdm = _load_cdm(tmp_path)
        payload = self._payload(tmp_path)
        # Simulate a device that already has local.yaml
        device = tmp_path / "device"
        (device / "etc" / "tbox").mkdir(parents=True)
        (device / "etc" / "tbox" / "local.yaml").write_text("existing")
        planner = ConfigDeployPlanner(cdm, "orin")
        plan = planner.plan(payload, device)
        creates = [a for a in plan.actions if a.action == "create_if_missing"]
        assert len(creates) == 1
        assert "already has file" in creates[0].reason

    def test_device_managed_in_payload_is_violation(self, tmp_path):
        cdm = _load_cdm(tmp_path)
        planner = ConfigDeployPlanner(cdm, "orin")
        plan = planner.plan(self._payload(tmp_path))
        # credentials/ca.crt and device.yaml are device-managed in the payload
        violations = [a for a in plan.actions if a.authority_violation]
        assert len(violations) == 2
        assert not plan.passed

    def test_unmatched_defaults_preserve(self, tmp_path):
        cdm = _load_cdm(tmp_path)
        root = tmp_path / "payload"
        tbox = root / "etc" / "tbox"
        tbox.mkdir(parents=True)
        (tbox / "unknown.yaml").write_text("x: 1\n")
        planner = ConfigDeployPlanner(cdm, "orin")
        plan = planner.plan(root)
        unmatched = [a for a in plan.actions if a.target_path == "/etc/tbox/unknown.yaml"]
        assert len(unmatched) == 1
        assert unmatched[0].action == "preserve"
        assert "unmatched" in unmatched[0].reason

    def test_replace_records_sha256(self, tmp_path):
        cdm = _load_cdm(tmp_path)
        planner = ConfigDeployPlanner(cdm, "orin")
        plan = planner.plan(self._payload(tmp_path))
        common = next(a for a in plan.actions if a.target_path == "/etc/tbox/common.yaml")
        assert common.payload_sha256 is not None
        assert len(common.payload_sha256) == 64

    def test_device_sha256_recorded(self, tmp_path):
        cdm = _load_cdm(tmp_path)
        payload = self._payload(tmp_path)
        device = tmp_path / "device"
        (device / "etc" / "tbox" / "conf.d").mkdir(parents=True)
        (device / "etc" / "tbox" / "conf.d" / "mqtt.yaml").write_text("old")
        planner = ConfigDeployPlanner(cdm, "orin")
        plan = planner.plan(payload, device)
        mqtt = next(a for a in plan.actions if a.target_path == "/etc/tbox/conf.d/mqtt.yaml")
        assert mqtt.device_sha256 is not None
        assert mqtt.payload_sha256 != mqtt.device_sha256

    def test_empty_payload(self, tmp_path):
        cdm = _load_cdm(tmp_path)
        root = tmp_path / "empty"
        root.mkdir()
        planner = ConfigDeployPlanner(cdm, "orin")
        plan = planner.plan(root)
        assert plan.actions == []
        assert plan.passed

    def test_summary(self, tmp_path):
        cdm = _load_cdm(tmp_path)
        planner = ConfigDeployPlanner(cdm, "orin")
        plan = planner.plan(self._payload(tmp_path))
        s = plan.summary()
        assert "replace" in s
        assert "preserve" in s
        assert "create_if_missing" in s
