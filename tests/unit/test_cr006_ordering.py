"""CR-006 ordering drop-in tests (BUILD-REQ-054, reusing CR-005 mechanism).

Verifies that the SEC unit's BUILD-owned drop-in carries the PROV + TIME
ordering (After= only, no Requires/BindsTo/PartOf), and that TIME itself
declares no ordering (framework-library dependency only) so no drop-in is
materialized for it.
"""

from __future__ import annotations

from pathlib import Path

from tbox_build.manifest import (
    BuildConfig,
    RuntimeConfig,
    Service,
    ServiceManifest,
)
from tbox_build.ordering import (
    ORDERING_DROPIN_FILENAME,
    dropin_rel_path,
    materialize_systemd_ordering,
)


def _svc(sid: str, *, units=None, after=None) -> Service:
    return Service(
        id=sid,
        repository=sid,
        kind="service",
        build=BuildConfig(preset="orin-release", targets=[sid]),
        runtime=RuntimeConfig(systemd_units=units or [], after=after or []),
    )


class TestSecDropIn:
    def test_sec_dropin_orders_after_prov_and_time(self, tmp_path: Path):
        manifest = ServiceManifest(services={
            "time": _svc("time", units=["tbox-time.service"], after=[]),
            "sec": _svc(
                "sec",
                units=["tbox-sec.service"],
                after=["tbox-prov.service", "tbox-time.service"],
            ),
        })
        records = materialize_systemd_ordering(tmp_path, manifest)
        # Only SEC has a non-empty after -> exactly one drop-in.
        assert len(records) == 1
        rec = records[0]
        assert rec.rel_path == dropin_rel_path("tbox-sec.service")
        assert rec.source_service == "sec"
        dst = tmp_path / rec.rel_path
        assert dst.read_text() == (
            "[Unit]\nAfter=tbox-prov.service tbox-time.service\n"
        )

    def test_dropin_never_contains_requires(self, tmp_path: Path):
        manifest = ServiceManifest(services={
            "sec": _svc(
                "sec",
                units=["tbox-sec.service"],
                after=["tbox-time.service"],
            ),
        })
        materialize_systemd_ordering(tmp_path, manifest)
        content = (
            tmp_path / dropin_rel_path("tbox-sec.service")
        ).read_text()
        assert "Requires=" not in content
        assert "BindsTo=" not in content
        assert "PartOf=" not in content
        assert "Wants=" not in content

    def test_time_empty_after_produces_no_dropin(self, tmp_path: Path):
        manifest = ServiceManifest(services={
            "time": _svc("time", units=["tbox-time.service"], after=[]),
        })
        records = materialize_systemd_ordering(tmp_path, manifest)
        assert records == []
        assert not (tmp_path / dropin_rel_path("tbox-time.service")).exists()

    def test_real_manifest_sec_dropin(self, tmp_path: Path, project_root: Path):
        """The real manifests must materialize SEC ordering including TIME."""
        from tbox_build.manifest import load_service_manifest
        sm = load_service_manifest(project_root / "manifests" / "services.yaml")
        records = materialize_systemd_ordering(tmp_path, sm)
        sec_rec = next(r for r in records if r.unit == "tbox-sec.service")
        assert sec_rec.after == ("tbox-prov.service", "tbox-time.service")
        # TIME declares no ordering -> no drop-in of its own.
        assert not any(r.source_service == "time" for r in records)
