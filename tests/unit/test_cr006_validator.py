"""CR-006 validator tests: TIME bootstrap-ordering contract.

Covers BUILD-REQ-054 regression assertions (TBOX-BUILD-DSN-CR-006 §3.1/§3.2):

* SEC's dependency on ``time`` must be expressed through ``runtime.after``
  (never hidden behind a waiver);
* ``time`` must not order after MQTT/TSP/SEC units (no time-sync bootstrap
  back-edges into the security/cloud path);
* the contract is a no-op when TIME is not part of the manifest (framework-only
  sets stay unchanged).
"""

from __future__ import annotations

import pytest

from tbox_build.errors import ValidationFailure
from tbox_build.manifest import (
    AfterWaiver,
    BuildConfig,
    RuntimeConfig,
    Service,
    ServiceManifest,
)
from tbox_build.validator import validate_time_ordering_contract


def _svc(
    sid: str,
    *,
    deps: list[str] | None = None,
    units: list[str] | None = None,
    after: list[str] | None = None,
    waivers: list[AfterWaiver] | None = None,
) -> Service:
    return Service(
        id=sid,
        repository=sid,
        kind="service",
        build=BuildConfig(
            preset="orin-release",
            targets=[sid],
            service_dependencies=deps or [],
        ),
        runtime=RuntimeConfig(
            systemd_units=units or [],
            after=after or [],
            after_waivers=waivers or [],
        ),
    )


def _time_manifest(
    sec_after=None,
    sec_waivers=None,
    time_after=None,
    extra_services=None,
) -> ServiceManifest:
    services = {
        "time": _svc("time", units=["tbox-time.service"], after=time_after or []),
        "sec": _svc(
            "sec",
            deps=["time"],
            units=["tbox-sec.service"],
            after=sec_after or [],
            waivers=sec_waivers or [],
        ),
    }
    services.update(extra_services or {})
    return ServiceManifest(services=services)


def _errors(manifest: ServiceManifest) -> list[str]:
    with pytest.raises(ValidationFailure) as exc:
        validate_time_ordering_contract(manifest)
    return exc.value.details


class TestSecMustOrderAfterTime:
    def test_after_includes_time_unit_passes(self):
        manifest = _time_manifest(sec_after=["tbox-time.service"])
        assert validate_time_ordering_contract(manifest) == []

    def test_missing_after_is_error(self):
        errors = _errors(_time_manifest(sec_after=[]))
        assert any("'time'" in e and "runtime.after" in e for e in errors)
        assert any("tbox-time.service" in e for e in errors)

    def test_after_other_unit_still_missing_time_is_error(self):
        errors = _errors(_time_manifest(sec_after=["tbox-prov.service"]))
        assert any("tbox-time.service" in e for e in errors)

    def test_waiver_for_time_is_error(self):
        waiver = AfterWaiver(
            dependency="time", unit="tbox-time.service",
            owner="sec", reason="TIME is optional",
        )
        errors = _errors(_time_manifest(sec_after=[], sec_waivers=[waiver]))
        assert any("must not waive ordering" in e for e in errors)


class TestTimeNoBootstrapBackEdges:
    def test_time_after_mqtt_is_rejected(self):
        manifest = _time_manifest(
            sec_after=["tbox-time.service"],
            time_after=["tbox-mqtt.service"],
            extra_services={
                "mqtt": _svc("mqtt", units=["tbox-mqtt.service"]),
            },
        )
        errors = _errors(manifest)
        assert any("must not order after 'tbox-mqtt.service'" in e for e in errors)

    def test_time_after_sec_is_rejected(self):
        manifest = _time_manifest(
            sec_after=["tbox-time.service"], time_after=["tbox-sec.service"]
        )
        errors = _errors(manifest)
        assert any("must not order after 'tbox-sec.service'" in e for e in errors)

    def test_time_after_tsp_is_rejected(self):
        manifest = _time_manifest(
            sec_after=["tbox-time.service"],
            time_after=["tbox-tsp.service"],
            extra_services={
                "tsp": _svc("tsp", units=["tbox-tsp.service"]),
            },
        )
        errors = _errors(manifest)
        assert any("must not order after 'tbox-tsp.service'" in e for e in errors)

    def test_time_after_framework_unit_is_unknown_unit_error(self):
        # A fabricated tbox-framework.service is rejected by the generic
        # unknown-unit check, not silently accepted.
        manifest = _time_manifest(
            sec_after=["tbox-time.service"],
            time_after=["tbox-framework.service"],
        )
        from tbox_build.validator import validate_systemd_references
        with pytest.raises(ValidationFailure) as exc:
            validate_systemd_references(manifest)
        assert any("unknown systemd unit" in e for e in exc.value.details)


class TestNoOpWithoutTime:
    def test_framework_only_manifest_returns_no_errors(self):
        manifest = ServiceManifest(services={
            "framework": _svc("framework", units=[]),
        })
        assert validate_time_ordering_contract(manifest) == []

    def test_manifest_without_sec_unaffected(self):
        manifest = ServiceManifest(services={
            "time": _svc("time", units=["tbox-time.service"]),
            "mqtt": _svc("mqtt", units=["tbox-mqtt.service"]),
        })
        assert validate_time_ordering_contract(manifest) == []


class TestRealManifest:
    def test_real_manifest_passes_contract(self, project_root):
        from tbox_build.manifest import load_service_manifest
        sm = load_service_manifest(project_root / "manifests" / "services.yaml")
        assert validate_time_ordering_contract(sm) == []
