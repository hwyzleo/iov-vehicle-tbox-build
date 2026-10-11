"""CR-005 validator tests: after/waiver contract and reverse completeness.

Covers BUILD-REQ-050 (dependency <-> after completeness) and BUILD-REQ-051
(structured waiver). The key regression this CR fixes: a declared
``service_dependencies`` entry with no ordering edge must never pass silently,
and an "unused" drop-in ordering must be rejected at build time.
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
from tbox_build.validator import validate_systemd_references


def _svc(
    sid: str,
    *,
    kind: str = "service",
    deps: list[str] | None = None,
    units: list[str] | None = None,
    after: list[str] | None = None,
    waivers: list[AfterWaiver] | None = None,
) -> Service:
    return Service(
        id=sid,
        repository=sid,
        kind=kind,
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


def _waiver(dep: str, unit: str, owner: str = "o", reason: str = "r") -> AfterWaiver:
    return AfterWaiver(dependency=dep, unit=unit, owner=owner, reason=reason)


def _errors(manifest: ServiceManifest) -> list[str]:
    with pytest.raises(ValidationFailure) as exc:
        validate_systemd_references(manifest)
    return exc.value.details


class TestForwardReferences:
    def test_after_to_declared_unit_passes(self):
        m = ServiceManifest(services={
            "a": _svc("a", units=["a.service"]),
            "b": _svc("b", units=["b.service"], after=["a.service"]),
        })
        assert validate_systemd_references(m) == []

    def test_external_target_allowed(self):
        m = ServiceManifest(services={"a": _svc("a", after=["network.target"])})
        validate_systemd_references(m)

    def test_unknown_unit_rejected(self):
        m = ServiceManifest(services={"a": _svc("a", after=["ghost.service"])})
        assert any("unknown systemd unit" in e for e in _errors(m))

    def test_duplicate_after_rejected(self):
        m = ServiceManifest(services={
            "a": _svc("a", units=["a.service"]),
            "b": _svc("b", units=["b.service"], after=["a.service", "a.service"]),
        })
        assert any("duplicate unit" in e for e in _errors(m))

    def test_self_dependency_rejected(self):
        m = ServiceManifest(services={
            "a": _svc("a", units=["a.service"], after=["a.service"]),
        })
        assert any("self-dependency" in e for e in _errors(m))

    def test_duplicate_unit_ownership_rejected(self):
        m = ServiceManifest(services={
            "a": _svc("a", units=["shared.service"]),
            "b": _svc("b", units=["shared.service"]),
        })
        assert any("ambiguous unit-to-service mapping" in e for e in _errors(m))


class TestReverseCompleteness:
    def test_dependency_covered_by_after(self):
        m = ServiceManifest(services={
            "a": _svc("a", units=["a.service"]),
            "b": _svc("b", units=["b.service"], deps=["a"], after=["a.service"]),
        })
        assert validate_systemd_references(m) == []

    def test_dependency_covered_by_waiver_records_warning(self):
        m = ServiceManifest(services={
            "a": _svc("a", units=["a.service"]),
            "b": _svc("b", units=["b.service"], deps=["a"],
                      waivers=[_waiver("a", "a.service", reason="degraded")]),
        })
        warnings = validate_systemd_references(m)
        assert any("waives after-ordering" in w and "degraded" in w for w in warnings)

    def test_dependency_without_after_or_waiver_fails(self):
        m = ServiceManifest(services={
            "a": _svc("a", units=["a.service"]),
            "b": _svc("b", units=["b.service"], deps=["a"]),
        })
        assert any("neither in runtime.after nor covered" in e for e in _errors(m))

    def test_library_dependency_excluded(self):
        m = ServiceManifest(services={
            "fw": _svc("fw", kind="library", units=[]),
            "b": _svc("b", units=["b.service"], deps=["fw"]),
        })
        assert validate_systemd_references(m) == []

    def test_daemon_dependency_is_not_a_bypass(self):
        # A dependency whose kind is the legacy default must not escape the
        # completeness rule: any non-library dependency with a runtime unit is
        # subject to it (review feedback on TBOX-BUILD-DSN-CR-005).
        m = ServiceManifest(services={
            "a": _svc("a", kind="daemon", units=["a.service"]),
            "b": _svc("b", units=["b.service"], deps=["a"]),
        })
        assert any("neither in runtime.after nor covered" in e for e in _errors(m))

    def test_zero_unit_dependency_ambiguous(self):
        m = ServiceManifest(services={
            "a": _svc("a", kind="daemon", units=[]),
            "b": _svc("b", units=["b.service"], deps=["a"]),
        })
        assert any("declares no runtime systemd unit" in e for e in _errors(m))

    def test_multi_unit_dependency_ambiguous(self):
        m = ServiceManifest(services={
            "a": _svc("a", units=["a.service", "a-helper.service"]),
            "b": _svc("b", units=["b.service"], deps=["a"]),
        })
        assert any("multiple runtime systemd units" in e for e in _errors(m))


class TestWaiverContract:
    def test_orphan_waiver_rejected(self):
        m = ServiceManifest(services={
            "b": _svc("b", units=["b.service"], waivers=[_waiver("a", "a.service")]),
        })
        assert any("orphan waiver" in e for e in _errors(m))

    def test_blank_reason_rejected(self):
        m = ServiceManifest(services={
            "a": _svc("a", units=["a.service"]),
            "b": _svc("b", units=["b.service"], deps=["a"],
                      waivers=[_waiver("a", "a.service", reason="   ")]),
        })
        assert any("non-blank owner and reason" in e for e in _errors(m))

    def test_blank_owner_rejected(self):
        m = ServiceManifest(services={
            "a": _svc("a", units=["a.service"]),
            "b": _svc("b", units=["b.service"], deps=["a"],
                      waivers=[_waiver("a", "a.service", owner="")]),
        })
        assert any("non-blank owner and reason" in e for e in _errors(m))

    def test_duplicate_waiver_rejected(self):
        m = ServiceManifest(services={
            "a": _svc("a", units=["a.service"]),
            "b": _svc("b", units=["b.service"], deps=["a"],
                      waivers=[_waiver("a", "a.service"), _waiver("a", "a.service")]),
        })
        assert any("duplicate after_waiver" in e for e in _errors(m))

    def test_after_and_waiver_conflict_rejected(self):
        m = ServiceManifest(services={
            "a": _svc("a", units=["a.service"]),
            "b": _svc("b", units=["b.service"], deps=["a"], after=["a.service"],
                      waivers=[_waiver("a", "a.service")]),
        })
        assert any("both runtime.after and after_waivers" in e for e in _errors(m))

    def test_waiver_unit_mismatch_rejected(self):
        m = ServiceManifest(services={
            "a": _svc("a", units=["a.service"]),
            "b": _svc("b", units=["b.service"], deps=["a"],
                      waivers=[_waiver("a", "wrong.service")]),
        })
        assert any("does not match dependency" in e for e in _errors(m))

    def test_waiver_for_library_dependency_rejected(self):
        m = ServiceManifest(services={
            "fw": _svc("fw", kind="library"),
            "b": _svc("b", units=["b.service"], deps=["fw"],
                      waivers=[_waiver("fw", "fw.service")]),
        })
        assert any("library dependency" in e for e in _errors(m))


class TestOrderingCycle:
    def test_cycle_rejected(self):
        m = ServiceManifest(services={
            "a": _svc("a", units=["a.service"], deps=["b"], after=["b.service"]),
            "b": _svc("b", units=["b.service"], deps=["a"], after=["a.service"]),
        })
        assert any("ordering cycle" in e for e in _errors(m))

    def test_waiver_does_not_enter_graph(self):
        # a -> b ordering, b waives a: no cycle, no edge from waiver.
        m = ServiceManifest(services={
            "a": _svc("a", units=["a.service"]),
            "b": _svc("b", units=["b.service"], deps=["a"], after=["a.service"]),
            "c": _svc("c", units=["c.service"], deps=["b"], after=[],
                      waivers=[_waiver("b", "b.service")]),
        })
        warnings = validate_systemd_references(m)
        assert any("waives after-ordering" in w for w in warnings)
