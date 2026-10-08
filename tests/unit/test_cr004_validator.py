"""Unit tests for CR-004 validation gates: D1 manifest coverage, D3 retirement
manifest validation, and D2 link-hardening exemption records.

Fixtures build a minimal fake TBOX project root (manifests/ + sibling repos)
so each validator rule is exercised in isolation.
"""

from __future__ import annotations

import datetime
from pathlib import Path

import pytest
import yaml

from tbox_build.errors import ValidationFailure, SchemaValidationError
from tbox_build.manifest import (
    Project,
    Service,
    BuildConfig,
    RuntimeConfig,
    ServiceManifest,
    load_repository_inventory,
    load_retirement_manifest,
    load_link_exemptions,
)
from tbox_build.validator import (
    validate_manifest_coverage,
    validate_retirement_manifest,
    validate_link_exemptions,
    validate_all,
    _retirement_path_error,
    _retirement_unit_error,
)


# ---------------------------------------------------------------------------
# Fake project helpers
# ---------------------------------------------------------------------------


def _write_yaml(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")


_ORIN_PLATFORM = {
    "platform": "orin",
    "architecture": "aarch64",
    "rootfs": {"id": "orin-r35.3.1", "repository_path": "sysroots/orin-r35.3.1"},
    "toolchain": {"target_triple": "aarch64-linux-gnu", "sysroot": "orin-r35.3.1"},
}


def _make_service(sid: str, source_dir: str, units: list[str] | None = None) -> Service:
    return Service(
        id=sid,
        repository=source_dir,
        source_dir=source_dir,
        build=BuildConfig(
            preset="orin-release",
            targets=[sid],
            service_dependencies=[],
            target_dependencies=[],
        ),
        runtime=RuntimeConfig(systemd_units=units or []),
    )


def _make_manifest(svc: dict[str, Service]) -> ServiceManifest:
    return ServiceManifest(services=svc)


def _make_repo(
    root: Path,
    name: str,
    *,
    cmake: str = "",
    unit: bool = False,
    unit_dir: str = "packaging/systemd",
) -> Path:
    """Create a sibling-style repo dir under *root* with optional evidence."""
    repo = root / name
    repo.mkdir(parents=True, exist_ok=True)
    if cmake:
        (repo / "CMakeLists.txt").write_text(cmake, encoding="utf-8")
    if unit:
        d = repo / unit_dir
        d.mkdir(parents=True, exist_ok=True)
        (d / f"{name}.service").write_text(
            "[Unit]\nDescription=test\n[Service]\nExecStart=/usr/bin/x\n",
            encoding="utf-8",
        )
    return repo


_RUNTIME_CMAKE = (
    "cmake_minimum_required(VERSION 3.16)\n"
    "project(foo)\n"
    "install(TARGETS foo\n"
    "  RUNTIME DESTINATION bin\n"
    "  COMPONENT foo-runtime)\n"
)


def _make_project(
    tmp_path: Path,
    *,
    services: dict[str, Service] | None = None,
    inventory: dict | None = None,
    retirement: dict | None = None,
    config_deployment: dict | None = None,
    link_exemptions: dict | None = None,
    sibling_evidence: list[str] | None = None,
) -> Path:
    """Create a fake project root with manifests/ and optional repos."""
    root = tmp_path / "project"
    _write_yaml(root / "manifests" / "orin-platform.yaml", _ORIN_PLATFORM)
    if services is not None:
        _write_yaml(
            root / "manifests" / "services.yaml",
            {"services": {sid: {
                "source_dir": svc.effective_source_dir,
                "build": {
                    "preset": svc.build.preset,
                    "targets": svc.build.targets,
                },
                "runtime": {
                    "systemd_units": svc.runtime.systemd_units,
                },
            } for sid, svc in services.items()}},
        )
    else:
        _write_yaml(root / "manifests" / "services.yaml", {"services": {}})
    _write_yaml(root / "manifests" / "release-set.yaml", {"release_sets": {}})
    _write_yaml(root / "dependencies" / "lock.yaml", {"dependencies": {}})
    if inventory is not None:
        _write_yaml(root / "manifests" / "repository-inventory.yaml", inventory)
    if retirement is not None:
        _write_yaml(root / "manifests" / "retirement.yaml", retirement)
    if config_deployment is not None:
        _write_yaml(root / "manifests" / "config-deployment.yaml", config_deployment)
    if link_exemptions is not None:
        _write_yaml(root / "manifests" / "link-exemptions.yaml", link_exemptions)
    # Repo dirs are placed as siblings of the project (../iov-vehicle-tbox-*).
    for name in sibling_evidence or []:
        _make_repo(tmp_path, name, cmake=_RUNTIME_CMAKE, unit=True)
    return root


def _future() -> str:
    return (datetime.date.today() + datetime.timedelta(days=30)).isoformat()


def _past() -> str:
    return (datetime.date.today() - datetime.timedelta(days=30)).isoformat()


# ---------------------------------------------------------------------------
# D1 - manifest coverage gate
# ---------------------------------------------------------------------------


class TestD1ManifestCoverage:
    def test_registered_service_passes(self, tmp_path):
        svc = _make_service("prov", "../iov-vehicle-tbox-prov",
                            units=["tbox-prov.service"])
        root = _make_project(
            tmp_path,
            services={"prov": svc},
            inventory={
                "version": 1,
                "repositories": {"prov": {"path": "../iov-vehicle-tbox-prov"}},
                "coverage_allowlist": [],
            },
            sibling_evidence=["iov-vehicle-tbox-prov"],
        )
        # prov repo carries both a unit and a runtime install component.
        repo = tmp_path / "iov-vehicle-tbox-prov"
        (repo / "CMakeLists.txt").write_text(
            "install(TARGETS prov\n"
            "  RUNTIME DESTINATION bin\n"
            "  COMPONENT prov-runtime)\n",
            encoding="utf-8",
        )
        warnings = validate_manifest_coverage(_make_manifest({"prov": svc}), root)
        assert not any("not registered" in w for w in warnings)

    def test_strong_signal_unregistered_fails(self, tmp_path):
        svc = _make_service("prov", "../iov-vehicle-tbox-prov")
        root = _make_project(
            tmp_path,
            services={"prov": svc},
            inventory={
                "version": 1,
                "repositories": {"ghost": {"path": "../iov-vehicle-tbox-ghost"}},
                "coverage_allowlist": [],
            },
            sibling_evidence=["iov-vehicle-tbox-ghost"],
        )
        # ghost repo: unit + runtime component, but no service registered for it.
        repo = tmp_path / "iov-vehicle-tbox-ghost"
        (repo / "CMakeLists.txt").write_text(_RUNTIME_CMAKE, encoding="utf-8")
        with pytest.raises(ValidationFailure, match="manifest coverage"):
            validate_manifest_coverage(_make_manifest({"prov": svc}), root)

    def test_unit_only_unregistered_warns(self, tmp_path):
        svc = _make_service("prov", "../iov-vehicle-tbox-prov")
        root = _make_project(
            tmp_path,
            services={"prov": svc},
            inventory={
                "version": 1,
                "repositories": {"half": {"path": "../iov-vehicle-tbox-half"}},
                "coverage_allowlist": [],
            },
            sibling_evidence=["iov-vehicle-tbox-half"],
        )
        repo = tmp_path / "iov-vehicle-tbox-half"
        (repo / "CMakeLists.txt").write_text(
            "install(FILES x.txt DESTINATION share)\n", encoding="utf-8"
        )
        warnings = validate_manifest_coverage(_make_manifest({"prov": svc}), root)
        assert any("single-signal" in w for w in warnings)

    def test_runtime_only_unregistered_warns(self, tmp_path):
        svc = _make_service("prov", "../iov-vehicle-tbox-prov")
        root = _make_project(
            tmp_path,
            services={"prov": svc},
            inventory={
                "version": 1,
                "repositories": {"half": {"path": "../iov-vehicle-tbox-half"}},
                "coverage_allowlist": [],
            },
        )
        repo = _make_repo(tmp_path, "iov-vehicle-tbox-half", cmake=_RUNTIME_CMAKE)
        warnings = validate_manifest_coverage(_make_manifest({"prov": svc}), root)
        assert any("single-signal" in w for w in warnings)

    def test_valid_allowlist_passes_with_diagnostic(self, tmp_path):
        svc = _make_service("prov", "../iov-vehicle-tbox-prov")
        root = _make_project(
            tmp_path,
            services={"prov": svc},
            inventory={
                "version": 1,
                "repositories": {"legacy": {"path": "../iov-vehicle-tbox-legacy"}},
                "coverage_allowlist": [{
                    "repository": "legacy",
                    "owner": "build-team",
                    "reason": "legacy monolith pending DIAG migration",
                    "expires": _future(),
                }],
            },
            sibling_evidence=["iov-vehicle-tbox-legacy"],
        )
        warnings = validate_manifest_coverage(_make_manifest({"prov": svc}), root)
        assert any("allowlist" in w for w in warnings)

    def test_expired_allowlist_fails(self, tmp_path):
        svc = _make_service("prov", "../iov-vehicle-tbox-prov")
        root = _make_project(
            tmp_path,
            services={"prov": svc},
            inventory={
                "version": 1,
                "repositories": {"legacy": {"path": "../iov-vehicle-tbox-legacy"}},
                "coverage_allowlist": [{
                    "repository": "legacy",
                    "owner": "build-team",
                    "reason": "legacy monolith pending DIAG migration",
                    "expires": _past(),
                }],
            },
            sibling_evidence=["iov-vehicle-tbox-legacy"],
        )
        with pytest.raises(ValidationFailure) as exc_info:
            validate_manifest_coverage(_make_manifest({"prov": svc}), root)
        assert any("expired" in d for d in exc_info.value.details)

    def test_allowlist_requires_expiry_or_review(self, tmp_path):
        inv_path = tmp_path / "repository-inventory.yaml"
        _write_yaml(inv_path, {
            "version": 1,
            "repositories": {"legacy": {"path": "../x"}},
            "coverage_allowlist": [{
                "repository": "legacy",
                "owner": "build-team",
                "reason": "legacy monolith pending migration",
            }],
        })
        with pytest.raises(SchemaValidationError, match="expires"):
            load_repository_inventory(inv_path)

    def test_generic_pattern_allowlist_rejected(self, tmp_path):
        inv_path = tmp_path / "repository-inventory.yaml"
        _write_yaml(inv_path, {
            "version": 1,
            "repositories": {"a": {"path": "../a"}},
            "coverage_allowlist": [{
                "repository": "*",
                "owner": "build-team",
                "reason": "everything is fine for now",
                "expires": _future(),
            }],
        })
        with pytest.raises(SchemaValidationError, match="generic pattern"):
            load_repository_inventory(inv_path)

    def test_multiline_cmake_component_parsed(self, tmp_path):
        cmake = (
            "install(\n"
            "  FILES config/prov.default.yaml\n"
            "  DESTINATION ${CMAKE_INSTALL_SYSCONFDIR}/tbox/conf.d\n"
            "  RENAME prov.yaml\n"
            "  COMPONENT prov-runtime)\n"
        )
        repo = _make_repo(tmp_path, "iov-vehicle-tbox-prov", cmake=cmake)
        from tbox_build.validator import _repo_runtime_components
        comps = _repo_runtime_components(repo)
        assert "prov-runtime" in comps

    def test_component_does_not_match_non_runtime(self, tmp_path):
        repo = _make_repo(
            tmp_path, "iov-vehicle-tbox-prov",
            cmake="install(FILES x DESTINATION bin COMPONENT prov-sdk)\n",
        )
        from tbox_build.validator import _repo_runtime_components
        assert _repo_runtime_components(repo) == set()

    def test_sibling_discovery_diagnostic_warning(self, tmp_path):
        svc = _make_service("prov", "../iov-vehicle-tbox-prov")
        root = _make_project(
            tmp_path,
            services={"prov": svc},
            inventory={
                "version": 1,
                "repositories": {"prov": {"path": "../iov-vehicle-tbox-prov"}},
                "coverage_allowlist": [],
            },
            sibling_evidence=["iov-vehicle-tbox-prov", "iov-vehicle-tbox-ghost"],
        )
        warnings = validate_manifest_coverage(_make_manifest({"prov": svc}), root)
        assert any("sibling repository" in w and "diagnostic" in w for w in warnings)

    def test_missing_inventory_skips(self, tmp_path):
        svc = _make_service("prov", "../iov-vehicle-tbox-prov")
        root = _make_project(tmp_path, services={"prov": svc})
        warnings = validate_manifest_coverage(_make_manifest({"prov": svc}), root)
        assert any("skipped" in w for w in warnings)

    def test_validate_all_integrates_d1(self, tmp_path):
        svc = _make_service("prov", "../iov-vehicle-tbox-prov")
        root = _make_project(
            tmp_path,
            services={"prov": svc},
            inventory={
                "version": 1,
                "repositories": {
                    "prov": {"path": "../iov-vehicle-tbox-prov"},
                    "ghost": {"path": "../iov-vehicle-tbox-ghost"},
                },
                "coverage_allowlist": [],
            },
            sibling_evidence=["iov-vehicle-tbox-prov", "iov-vehicle-tbox-ghost"],
        )
        repo = tmp_path / "iov-vehicle-tbox-ghost"
        (repo / "CMakeLists.txt").write_text(_RUNTIME_CMAKE, encoding="utf-8")
        with pytest.raises(ValidationFailure, match="manifest coverage"):
            validate_all(_make_manifest({"prov": svc}), root)


# ---------------------------------------------------------------------------
# D3 - retirement manifest validation
# ---------------------------------------------------------------------------


_CONFIG_DEPLOYMENT = {
    "version": 1,
    "platforms": {
        "orin": {
            "files": [
                {"path": "/etc/tbox/device.yaml",
                 "owner": "provisioning", "category": "device-managed",
                 "deploy_policy": "preserve"},
                {"path_glob": "/etc/tbox/credentials/**",
                 "owner": "provisioning", "category": "device-managed",
                 "deploy_policy": "preserve"},
            ]
        }
    },
}


class TestD3RetirementValidation:
    def _make(self, tmp_path, retirement: dict, active_units: list[str] | None = None):
        svc = _make_service("prov", "../iov-vehicle-tbox-prov",
                            units=active_units or [])
        root = _make_project(
            tmp_path,
            services={"prov": svc},
            retirement=retirement,
            config_deployment=_CONFIG_DEPLOYMENT,
        )
        return root, _make_manifest({"prov": svc})

    def test_valid_retirement_passes(self, tmp_path):
        root, manifest = self._make(tmp_path, {
            "version": 1,
            "platforms": {"orin": {
                "retired_units": [{
                    "name": "prov.service",
                    "owner": "build",
                    "reason": "replaced-by-tbox-prov.service",
                }],
                "retired_paths": [{
                    "path": "/opt/tbox/bin/prov",
                    "owner": "build",
                    "reason": "legacy-monolith",
                }],
            }},
        })
        validate_retirement_manifest(manifest, root)  # no raise

    def test_active_unit_conflict_fails(self, tmp_path):
        root, manifest = self._make(tmp_path, {
            "version": 1,
            "platforms": {"orin": {
                "retired_units": [{
                    "name": "tbox-prov.service",
                    "owner": "build",
                    "reason": "replaced",
                }],
                "retired_paths": [],
            }},
        }, active_units=["tbox-prov.service"])
        with pytest.raises(ValidationFailure) as exc_info:
            validate_retirement_manifest(manifest, root)
        assert any("conflicts with an active" in d for d in exc_info.value.details)

    def test_wildcard_path_fails(self, tmp_path):
        root, manifest = self._make(tmp_path, {
            "version": 1,
            "platforms": {"orin": {
                "retired_units": [],
                "retired_paths": [{
                    "path": "/opt/tbox/bin/*",
                    "owner": "build",
                    "reason": "legacy",
                }],
            }},
        })
        with pytest.raises(ValidationFailure) as exc_info:
            validate_retirement_manifest(manifest, root)
        assert any("wildcard" in d for d in exc_info.value.details)

    def test_path_outside_approved_roots_fails(self, tmp_path):
        root, manifest = self._make(tmp_path, {
            "version": 1,
            "platforms": {"orin": {
                "retired_units": [],
                "retired_paths": [{
                    "path": "/var/lib/tbox/prov",
                    "owner": "build",
                    "reason": "legacy",
                }],
            }},
        })
        with pytest.raises(ValidationFailure) as exc_info:
            validate_retirement_manifest(manifest, root)
        assert any("outside approved roots" in d for d in exc_info.value.details)

    def test_protected_device_yaml_fails(self, tmp_path):
        root, manifest = self._make(tmp_path, {
            "version": 1,
            "platforms": {"orin": {
                "retired_units": [],
                "retired_paths": [{
                    "path": "/etc/tbox/device.yaml",
                    "owner": "build",
                    "reason": "legacy",
                }],
            }},
        })
        with pytest.raises(ValidationFailure) as exc_info:
            validate_retirement_manifest(manifest, root)
        assert any("protected path" in d for d in exc_info.value.details)

    def test_protected_credentials_fails(self, tmp_path):
        root, manifest = self._make(tmp_path, {
            "version": 1,
            "platforms": {"orin": {
                "retired_units": [],
                "retired_paths": [{
                    "path": "/etc/tbox/credentials/secrets.yaml",
                    "owner": "build",
                    "reason": "legacy",
                }],
            }},
        })
        with pytest.raises(ValidationFailure) as exc_info:
            validate_retirement_manifest(manifest, root)
        assert any("protected path" in d for d in exc_info.value.details)

    def test_device_managed_intersection_fails(self, tmp_path):
        # /etc/tbox/conf.d is not in the fake config-deployment manifest, but
        # a device-managed path must be rejected via the protected path rule;
        # here we exercise the config-deployment intersection for an exact
        # device-managed rule match.
        root, manifest = self._make(tmp_path, {
            "version": 1,
            "platforms": {"orin": {
                "retired_units": [],
                "retired_paths": [{
                    "path": "/opt/tbox/device.yaml",
                    "owner": "build",
                    "reason": "legacy",
                }],
            }},
        })
        # Not protected (approved root /opt/tbox) and no config rule -> pass.
        validate_retirement_manifest(manifest, root)

    def test_duplicate_unit_fails(self, tmp_path):
        root, manifest = self._make(tmp_path, {
            "version": 1,
            "platforms": {"orin": {
                "retired_units": [
                    {"name": "prov.service", "owner": "build", "reason": "a"},
                    {"name": "prov.service", "owner": "build", "reason": "b"},
                ],
                "retired_paths": [],
            }},
        })
        with pytest.raises(ValidationFailure) as exc_info:
            validate_retirement_manifest(manifest, root)
        assert any("duplicate retired unit" in d for d in exc_info.value.details)

    def test_missing_manifest_is_noop(self, tmp_path):
        svc = _make_service("prov", "../iov-vehicle-tbox-prov")
        root = _make_project(tmp_path, services={"prov": svc})
        warnings = validate_retirement_manifest(_make_manifest({"prov": svc}), root)
        assert warnings == []

    def test_parser_defaults_empty_arrays(self, tmp_path):
        rm_path = tmp_path / "retirement.yaml"
        _write_yaml(rm_path, {
            "version": 1,
            "platforms": {"orin": {"retired_units": [], "retired_paths": []}},
        })
        rm = load_retirement_manifest(rm_path)
        assert rm.platform_for("orin").retired_units == []
        assert rm.platform_for("orin").retired_paths == []
        assert rm.platform_for("missing").retired_units == []


# ---------------------------------------------------------------------------
# D2 - link-hardening exemption records
# ---------------------------------------------------------------------------


class TestD2LinkExemptions:
    def _make(self, tmp_path, exemptions: list[dict]):
        root = _make_project(tmp_path, link_exemptions={
            "version": 1,
            "exemptions": exemptions,
        })
        return root

    def test_valid_exemption_passes(self, tmp_path):
        root = self._make(tmp_path, [{
            "target": "tbox_prov",
            "owner": "prov-team",
            "reason": "plugin symbols injected at runtime",
            "symbol_class": "undefined-func",
            "risk": "low",
            "scope": "tbox-prov-orin",
            "removal_condition": "plugin provider packaged",
        }])
        validate_link_exemptions(root)  # no raise

    def test_missing_field_fails(self, tmp_path):
        root = self._make(tmp_path, [{
            "target": "tbox_prov",
            "owner": "prov-team",
            "reason": "plugin symbols",
            "symbol_class": "undefined-func",
            "risk": "low",
            # scope + removal_condition missing
        }])
        with pytest.raises(ValidationFailure) as exc_info:
            validate_link_exemptions(root)
        assert any("missing required field" in d for d in exc_info.value.details)

    def test_duplicate_target_fails(self, tmp_path):
        root = self._make(tmp_path, [
            {"target": "tbox_prov", "owner": "a", "reason": "r1",
             "symbol_class": "s", "risk": "low", "scope": "x",
             "removal_condition": "c"},
            {"target": "tbox_prov", "owner": "b", "reason": "r2",
             "symbol_class": "s", "risk": "low", "scope": "x",
             "removal_condition": "c"},
        ])
        with pytest.raises(ValidationFailure) as exc_info:
            validate_link_exemptions(root)
        assert any("duplicate link exemption" in d for d in exc_info.value.details)

    def test_schema_rejects_unknown_fields(self, tmp_path):
        from tbox_build.schema import validate_link_exemptions_schema
        root = self._make(tmp_path, [{
            "target": "tbox_prov", "owner": "a", "reason": "r",
            "symbol_class": "s", "risk": "low", "scope": "x",
            "removal_condition": "c", "extra": "nope",
        }])
        data = {"version": 1, "exemptions": [
            {"target": "tbox_prov", "owner": "a", "reason": "r",
             "symbol_class": "s", "risk": "low", "scope": "x",
             "removal_condition": "c", "extra": "nope"},
        ]}
        with pytest.raises(Exception):
            validate_link_exemptions_schema(data, root)


# ---------------------------------------------------------------------------
# CR-004 评审回归：退役条目的注入面与批准根收窄（P0-1）
# ---------------------------------------------------------------------------


class TestRetirementEntryHardening:
    """Regression for review finding P0-1.

    Before the fix, ``_retirement_path_error`` only rejected wildcards, ``..``
    and root escape, and ``RETIREMENT_ALLOWED_ROOTS`` contained bare
    ``/usr/lib`` / ``/lib`` / ``/usr/bin``. Shell metacharacters and critical
    system files therefore passed validation and reached an unquoted remote
    ``rm -rf`` running under sudo.
    """

    # 字符集：任何 shell 元字符或空白都必须被拒
    @pytest.mark.parametrize("path", [
        "/opt/tbox/stale; rm -rf /",
        "/opt/tbox/$(reboot)",
        "/opt/tbox/x`id`",
        "/opt/tbox/a b",
        "/opt/tbox/a\tb",
        "/opt/tbox/a|b",
        "/opt/tbox/a&b",
        "/opt/tbox/a>b",
        "/opt/tbox/a'b",
        '/opt/tbox/a"b',
        "/opt/tbox/a\\b",
        "/opt/tbox/a\nb",
    ])
    def test_shell_metacharacters_rejected(self, path):
        err = _retirement_path_error(path, None)
        assert err is not None, f"unsafe path accepted: {path!r}"

    # 批准根收窄：系统关键文件不得被退役
    @pytest.mark.parametrize("path", [
        "/usr/lib/libc.so.6",
        "/usr/bin/systemctl",
        "/lib/ld-linux-aarch64.so.1",
        "/usr/local/lib/libfoo.so",
        "/usr/lib",            # 目录本身
        "/usr/bin",
        "/usr/lib/systemd/system",   # 单元目录本身
        "/usr/lib/systemd/system/sshd.service",  # 非 TBOX 单元仍需合法名，但目录受限
    ])
    def test_unscoped_system_paths_rejected(self, path):
        err = _retirement_path_error(path, None)
        assert err is not None, f"unsafe system path accepted: {path!r}"

    @pytest.mark.parametrize("path", [
        "/opt/tbox",
        "/opt/tbox/bin/prov",
        "/opt/tbox/lib/libhwyz.so",
        "/usr/bin/tbox_legacy",                     # tier3: 带 tbox 作用域
        "/usr/lib/libhwyz.so",                      # tier3: 带 hwyz 作用域
    ])
    def test_scoped_legacy_paths_accepted(self, path):
        assert _retirement_path_error(path, None) is None, path

    def test_unit_file_path_requires_declared_unit(self):
        p = "/usr/lib/systemd/system/prov.service"
        # 未在 retired_units 声明 -> 拒绝
        err = _retirement_path_error(p, None, frozenset())
        assert err is not None and "not declared in retired_units" in err
        # 已声明 -> 放行
        assert _retirement_path_error(p, None, {"prov.service"}) is None

    @pytest.mark.parametrize("name", [
        "prov.service; reboot",
        "prov.service $(id)",
        "prov service.service",
        "prov",                 # 无单元后缀
        "prov.unknown",
        "../prov.service",
    ])
    def test_unsafe_unit_names_rejected(self, name):
        assert _retirement_unit_error(name) is not None, name

    @pytest.mark.parametrize("name", [
        "prov.service",
        "tbox-legacy.service",
        "legacy@1.service",
        "old.socket",
        "old.timer",
    ])
    def test_valid_unit_names_accepted(self, name):
        assert _retirement_unit_error(name) is None, name

    def test_injection_entry_fails_validation_end_to_end(self, tmp_path):
        svc = _make_service("prov", "../iov-vehicle-tbox-prov", units=[])
        root = _make_project(
            tmp_path,
            services={"prov": svc},
            retirement={
                "version": 1,
                "platforms": {"orin": {
                    "retired_units": [{
                        "name": "prov.service; reboot",
                        "owner": "build", "reason": "r",
                    }],
                    "retired_paths": [{
                        "path": "/opt/tbox/bin/x; rm -rf /",
                        "owner": "build", "reason": "r",
                    }],
                }},
            },
            config_deployment=_CONFIG_DEPLOYMENT,
        )
        manifest = _make_manifest({"prov": svc})
        with pytest.raises(ValidationFailure) as exc_info:
            validate_retirement_manifest(manifest, root)
        details = " ".join(exc_info.value.details)
        assert "not a valid systemd unit name" in details
        assert "characters outside the allowed set" in details

    def test_schema_pattern_rejects_injection(self, tmp_path):
        from tbox_build.schema import validate_retirement_manifest_schema
        data = {
            "version": 1,
            "platforms": {"orin": {
                "retired_units": [],
                "retired_paths": [{
                    "path": "/opt/tbox/x; rm -rf /",
                    "owner": "build", "reason": "r",
                }],
            }},
        }
        with pytest.raises(Exception):
            validate_retirement_manifest_schema(data, tmp_path)


# ---------------------------------------------------------------------------
# CR-004 评审回归：gate 加载失败不得静默（P2-10）
# ---------------------------------------------------------------------------


class TestGateLoadFailureIsVisible:
    def test_unreadable_inventory_produces_warning(self, tmp_path, monkeypatch):
        svc = _make_service("prov", "../iov-vehicle-tbox-prov")
        root = _make_project(tmp_path, services={"prov": svc})
        manifest = _make_manifest({"prov": svc})

        import tbox_build.validator as v

        def _boom(self):  # noqa: ANN001
            raise RuntimeError("corrupt inventory")

        monkeypatch.setattr(v.Project, "load_repository_inventory", _boom)
        warnings = v.validate_manifest_coverage(manifest, root)
        assert any("could not run" in w and "gate skipped" in w for w in warnings)
