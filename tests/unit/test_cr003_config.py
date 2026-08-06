"""Unit tests for configuration validation (BUILD CR-003 §8).

Covers: secret material scanning, common.yaml single-source enforcement,
conf.d inline-common prohibition, schema/parse validation and permission
normalization.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from tbox_build.config_validation import (
    ConfigValidator,
    SecretScanReport,
    check_conf_d_no_inline_common,
    normalize_config_mode,
    run_schema_check,
    scan_secrets,
    validate_common_yaml,
)
from tbox_build.manifest import (
    BuildConfig,
    ConfigValidation,
    Project,
    RuntimeConfig,
    Service,
    ServiceManifest,
)

# A minimal JSON Schema for a service config used by schema-check tests.
_SCHEMA_YAML = """\
$schema: "http://json-schema.org/draft-07/schema#"
type: object
required:
  - myservice
properties:
  myservice:
    type: object
    required:
      - endpoint
    properties:
      endpoint:
        type: string
      port:
        type: integer
"""


def _make_project(tmp_path: Path) -> Project:
    """Create a minimal TBOX Build project root in *tmp_path*."""
    manifests = tmp_path / "manifests"
    manifests.mkdir()
    (manifests / "orin-platform.yaml").write_text(
        "platform: orin\narchitecture: aarch64\n")
    (manifests / "orin-r35.3.1.yaml").write_text(
        "sysroot:\n  id: orin-r35.3.1\n  digest: test\n  import_status: verified\n")
    return Project(tmp_path)


# ---------------------------------------------------------------------------
# Secret scanning
# ---------------------------------------------------------------------------


class TestSecretScan:
    def test_forbidden_extension_key(self, tmp_path):
        f = tmp_path / "ca.key"
        f.write_text("not a real key")
        report = scan_secrets([f])
        assert any(r.rule_id == "SECRET-FORBIDDEN-EXT" for r in report.findings)

    def test_forbidden_extension_p12_and_pfx(self, tmp_path):
        for ext in (".p12", ".pfx"):
            f = tmp_path / f"cert{ext}"
            f.write_text("data")
            report = scan_secrets([f])
            assert any(r.rule_id == "SECRET-FORBIDDEN-EXT" for r in report.findings)

    def test_pem_private_key_marker(self, tmp_path):
        f = tmp_path / "id.pem"
        f.write_text("-----BEGIN RSA PRIVATE KEY-----\nMIIB...\n-----END RSA PRIVATE KEY-----\n")
        report = scan_secrets([f])
        assert any(r.rule_id == "SECRET-PEM-PRIVATE-KEY" for r in report.findings)

    def test_pem_ec_private_key_marker(self, tmp_path):
        f = tmp_path / "ec.pem"
        f.write_text("-----BEGIN EC PRIVATE KEY-----\nMHQ...\n-----END EC PRIVATE KEY-----\n")
        report = scan_secrets([f])
        assert any(r.rule_id == "SECRET-PEM-PRIVATE-KEY" for r in report.findings)

    def test_sensitive_field_password(self, tmp_path):
        f = tmp_path / "cfg.yaml"
        f.write_text("mqtt:\n  password: s3cr3t\n")
        report = scan_secrets([f])
        hits = [r for r in report.findings if r.rule_id == "SECRET-SENSITIVE-FIELD"]
        assert len(hits) == 1
        assert hits[0].field_path == "mqtt.password"

    def test_sensitive_field_placeholder_allowed(self, tmp_path):
        f = tmp_path / "cfg.yaml"
        f.write_text('mqtt:\n  password: "test-placeholder"\n')
        report = scan_secrets([f])
        assert not [r for r in report.findings if r.rule_id == "SECRET-SENSITIVE-FIELD"]

    def test_sensitive_field_empty_allowed(self, tmp_path):
        f = tmp_path / "cfg.yaml"
        f.write_text("sec:\n  token: \"\"\n")
        report = scan_secrets([f])
        assert not report.findings

    def test_sensitive_field_in_json(self, tmp_path):
        f = tmp_path / "cfg.json"
        f.write_text('{"client_secret": "real-secret-value"}')
        report = scan_secrets([f])
        assert any(r.rule_id == "SECRET-SENSITIVE-FIELD" and r.field_path == "client_secret"
                    for r in report.findings)

    def test_provisioning_path_credentials(self, tmp_path):
        f = tmp_path / "etc" / "tbox" / "credentials" / "ca.crt"
        f.parent.mkdir(parents=True)
        f.write_text("cert")
        report = scan_secrets([f])
        assert any(r.rule_id == "SECRET-PROVISIONING-PATH" for r in report.findings)

    def test_provisioning_filename_soft_kek(self, tmp_path):
        f = tmp_path / ".soft_kek"
        f.write_text("data")
        report = scan_secrets([f])
        assert any(r.rule_id == "SECRET-PROVISIONING-FILE" for r in report.findings)

    def test_clean_config_no_findings(self, tmp_path):
        f = tmp_path / "mqtt.yaml"
        f.write_text("mqtt:\n  broker_host: example.com\n  broker_port: 8883\n")
        report = scan_secrets([f])
        assert report.passed
        assert report.findings == []

    def test_finding_does_not_leak_value(self, tmp_path):
        secret = "super-secret-value-12345"
        f = tmp_path / "cfg.yaml"
        f.write_text(f"mqtt:\n  password: {secret}\n")
        report = scan_secrets([f])
        # The finding must not contain the matched value anywhere.
        for finding in report.findings:
            assert secret not in finding.reason
            assert secret not in finding.file
            assert secret not in (finding.field_path or "")

    def test_skips_symlinks(self, tmp_path):
        target = tmp_path / "real.txt"
        target.write_text("data")
        link = tmp_path / "link.key"
        os.symlink(target, link)
        report = scan_secrets([link])
        # symlink counted but not scanned for content
        assert report.files_scanned == 1


# ---------------------------------------------------------------------------
# common.yaml single-source + conf.d inline-common
# ---------------------------------------------------------------------------


class TestCommonValidation:
    def test_common_yaml_valid(self, tmp_path):
        f = tmp_path / "common.yaml"
        f.write_text("common:\n  store:\n    root: /var/tbox\n")
        assert validate_common_yaml(f) == []

    def test_common_yaml_missing(self, tmp_path):
        errors = validate_common_yaml(tmp_path / "common.yaml")
        assert len(errors) == 1
        assert "not found" in errors[0]

    def test_common_yaml_no_common_key(self, tmp_path):
        f = tmp_path / "common.yaml"
        f.write_text("other: value\n")
        errors = validate_common_yaml(f)
        assert errors and "common" in errors[0]

    def test_common_yaml_corrupt(self, tmp_path):
        f = tmp_path / "common.yaml"
        f.write_text("common: [unclosed\n")
        errors = validate_common_yaml(f)
        assert errors and "parse error" in errors[0]

    def test_conf_d_no_inline_common_ok(self, tmp_path):
        (tmp_path / "prov.yaml").write_text("prov:\n  endpoint: x\n")
        (tmp_path / "sec.yaml").write_text("sec:\n  ipc: y\n")
        assert check_conf_d_no_inline_common(tmp_path) == []

    def test_conf_d_inline_common_fails(self, tmp_path):
        (tmp_path / "mqtt.yaml").write_text("common:\n  store:\n    root: /var/tbox\nmqtt:\n  x: 1\n")
        errors = check_conf_d_no_inline_common(tmp_path)
        assert len(errors) == 1
        assert "forbidden" in errors[0]

    def test_conf_d_corrupt_fails(self, tmp_path):
        (tmp_path / "bad.yaml").write_text("mqtt: [unclosed\n")
        errors = check_conf_d_no_inline_common(tmp_path)
        assert errors and "parse error" in errors[0]


# ---------------------------------------------------------------------------
# Schema / parse validation
# ---------------------------------------------------------------------------


def _make_service(tmp_path: Path, schema: bool = True, check_command: str | None = None) -> Service:
    svc_dir = tmp_path / "svc"
    svc_dir.mkdir()
    if schema:
        schema_dir = svc_dir / "config" / "schema"
        schema_dir.mkdir(parents=True)
        (schema_dir / "myservice.schema.yaml").write_text(_SCHEMA_YAML)
    cv = ConfigValidation(
        target_path="/etc/tbox/conf.d/myservice.yaml",
        schema="config/schema/myservice.schema.yaml" if schema else None,
        check_command=check_command,
    )
    return Service(
        id="myservice",
        repository="svc",
        build=BuildConfig(preset="orin-release"),
        runtime=RuntimeConfig(
            config_paths=["/etc/tbox/conf.d/myservice.yaml"],
            config_validation=cv,
        ),
    )


class TestSchemaCheck:
    def test_schema_pass(self, tmp_path):
        svc = _make_service(tmp_path)
        install_root = tmp_path / "install-root"
        conf_d = install_root / "etc" / "tbox" / "conf.d"
        conf_d.mkdir(parents=True)
        (conf_d / "myservice.yaml").write_text(
            "myservice:\n  endpoint: tcp://localhost:1234\n  port: 1234\n")
        result = run_schema_check(svc, install_root, tmp_path)
        assert result.status == "pass"

    def test_schema_fail_missing_required(self, tmp_path):
        svc = _make_service(tmp_path)
        install_root = tmp_path / "install-root"
        conf_d = install_root / "etc" / "tbox" / "conf.d"
        conf_d.mkdir(parents=True)
        (conf_d / "myservice.yaml").write_text("myservice:\n  port: 1234\n")
        result = run_schema_check(svc, install_root, tmp_path)
        assert result.status == "fail"
        assert any("endpoint" in e for e in result.errors)

    def test_schema_fail_type_error(self, tmp_path):
        svc = _make_service(tmp_path)
        install_root = tmp_path / "install-root"
        conf_d = install_root / "etc" / "tbox" / "conf.d"
        conf_d.mkdir(parents=True)
        (conf_d / "myservice.yaml").write_text(
            "myservice:\n  endpoint: x\n  port: not-an-int\n")
        result = run_schema_check(svc, install_root, tmp_path)
        assert result.status == "fail"
        assert any("port" in e for e in result.errors)

    def test_corrupt_yaml_fails(self, tmp_path):
        svc = _make_service(tmp_path)
        install_root = tmp_path / "install-root"
        conf_d = install_root / "etc" / "tbox" / "conf.d"
        conf_d.mkdir(parents=True)
        (conf_d / "myservice.yaml").write_text("myservice: [unclosed\n")
        result = run_schema_check(svc, install_root, tmp_path)
        assert result.status == "fail"
        assert any("parse error" in e for e in result.errors)

    def test_schema_file_missing_skips(self, tmp_path):
        svc = _make_service(tmp_path, schema=False)
        svc.runtime.config_validation.schema = "config/schema/missing.schema.yaml"
        install_root = tmp_path / "install-root"
        conf_d = install_root / "etc" / "tbox" / "conf.d"
        conf_d.mkdir(parents=True)
        (conf_d / "myservice.yaml").write_text("myservice:\n  endpoint: x\n")
        result = run_schema_check(svc, install_root, tmp_path)
        assert result.status == "skipped"

    def test_target_not_staged_skips(self, tmp_path):
        svc = _make_service(tmp_path)
        install_root = tmp_path / "install-root"
        install_root.mkdir()
        result = run_schema_check(svc, install_root, tmp_path)
        assert result.status == "skipped"
        assert any("not staged" in e for e in result.errors)

    def test_schema_path_escape_fails(self, tmp_path):
        svc = _make_service(tmp_path)
        svc.runtime.config_validation.schema = "../../../etc/passwd"
        install_root = tmp_path / "install-root"
        conf_d = install_root / "etc" / "tbox" / "conf.d"
        conf_d.mkdir(parents=True)
        (conf_d / "myservice.yaml").write_text("myservice:\n  endpoint: x\n")
        result = run_schema_check(svc, install_root, tmp_path)
        assert result.status == "fail"
        assert any("escapes" in e for e in result.errors)

    def test_check_command_pass(self, tmp_path):
        svc = _make_service(tmp_path, schema=False,
                            check_command="test -f \"$TBOX_CONFIG_FILE\"")
        install_root = tmp_path / "install-root"
        conf_d = install_root / "etc" / "tbox" / "conf.d"
        conf_d.mkdir(parents=True)
        (conf_d / "myservice.yaml").write_text("myservice:\n  endpoint: x\n")
        result = run_schema_check(svc, install_root, tmp_path)
        assert result.status == "pass"

    def test_check_command_fail(self, tmp_path):
        svc = _make_service(tmp_path, schema=False, check_command="false")
        install_root = tmp_path / "install-root"
        conf_d = install_root / "etc" / "tbox" / "conf.d"
        conf_d.mkdir(parents=True)
        (conf_d / "myservice.yaml").write_text("myservice:\n  endpoint: x\n")
        result = run_schema_check(svc, install_root, tmp_path)
        assert result.status == "fail"


# ---------------------------------------------------------------------------
# Permission normalization
# ---------------------------------------------------------------------------


class TestPermissionNormalization:
    def test_normalize_to_0644(self, tmp_path):
        f = tmp_path / "cfg.yaml"
        f.write_text("x: 1\n")
        os.chmod(f, 0o755)
        assert normalize_config_mode(f) is True
        assert stat.S_IMODE(f.stat().st_mode) == 0o644

    def test_already_0644(self, tmp_path):
        f = tmp_path / "cfg.yaml"
        f.write_text("x: 1\n")
        os.chmod(f, 0o644)
        assert normalize_config_mode(f) is False

    def test_skip_symlink(self, tmp_path):
        target = tmp_path / "real.yaml"
        target.write_text("x: 1\n")
        link = tmp_path / "link.yaml"
        os.symlink(target, link)
        assert normalize_config_mode(link) is False


# ---------------------------------------------------------------------------
# ConfigValidator coordinator
# ---------------------------------------------------------------------------


class TestConfigValidator:
    def test_clean_config_passes(self, tmp_path):
        project = _make_project(tmp_path)
        install_root = tmp_path / "out" / "orin" / "release" / "install-root"
        tbox = install_root / "etc" / "tbox"
        (tbox / "conf.d").mkdir(parents=True)
        (tbox / "common.yaml").write_text(
            "common:\n  store:\n    root: /var/tbox\n")
        (tbox / "conf.d" / "mqtt.yaml").write_text("mqtt:\n  broker_host: x\n")
        # No services with config_validation -> no schema checks
        sm = ServiceManifest(services={})
        validator = ConfigValidator(project.root, install_root, "orin", sm)
        report = validator.validate()
        assert report.passed
        assert report.common_ok
        assert report.conf_d_ok
        assert report.secret_scan.passed

    def test_conf_d_inline_common_fails_validation(self, tmp_path):
        project = _make_project(tmp_path)
        install_root = tmp_path / "install-root"
        tbox = install_root / "etc" / "tbox"
        (tbox / "conf.d").mkdir(parents=True)
        (tbox / "common.yaml").write_text("common:\n  store:\n    root: /var/tbox\n")
        (tbox / "conf.d" / "bad.yaml").write_text("common:\n  x: 1\n")
        sm = ServiceManifest(services={})
        validator = ConfigValidator(project.root, install_root, "orin", sm)
        report = validator.validate()
        assert not report.conf_d_ok
        assert not report.passed

    def test_secret_in_install_root_fails(self, tmp_path):
        project = _make_project(tmp_path)
        install_root = tmp_path / "install-root"
        tbox = install_root / "etc" / "tbox"
        (tbox / "conf.d").mkdir(parents=True)
        (tbox / "common.yaml").write_text("common:\n  store:\n    root: /var/tbox\n")
        (tbox / "conf.d" / "sec.yaml").write_text("sec:\n  password: real-secret\n")
        sm = ServiceManifest(services={})
        validator = ConfigValidator(project.root, install_root, "orin", sm)
        report = validator.validate()
        assert not report.passed
        assert not report.secret_scan.passed
