"""CR-005 ordering drop-in materialization and artifact recording.

Covers BUILD-REQ-049: deterministic generation, fixed path, ownership
conflict failure, empty-after no-op, and artifact manifest provenance.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tbox_build.errors import BuildFailure
from tbox_build.manifest import (
    BuildConfig,
    Project,
    RuntimeConfig,
    Service,
    ServiceManifest,
)
from tbox_build.ordering import (
    ORDERING_DROPIN_FILENAME,
    dropin_rel_path,
    materialize_systemd_ordering,
    render_ordering_dropin,
)
from tbox_build.staging import StagingDir


def _svc(sid: str, *, units=None, after=None) -> Service:
    return Service(
        id=sid,
        repository=sid,
        kind="service",
        build=BuildConfig(preset="orin-release", targets=[sid]),
        runtime=RuntimeConfig(systemd_units=units or [], after=after or []),
    )


class TestRender:
    def test_dedup_and_sort(self):
        assert render_ordering_dropin(
            ["b.service", "a.service", "b.service"]
        ) == "[Unit]\nAfter=a.service b.service\n"

    def test_single_trailing_newline(self):
        content = render_ordering_dropin(["a.service"])
        assert content.endswith("\n") and not content.endswith("\n\n")


class TestMaterialize:
    def test_writes_fixed_path_and_content(self, tmp_path: Path):
        manifest = ServiceManifest(services={
            "b": _svc("b", units=["b.service"], after=["c.service", "a.service"]),
        })
        records = materialize_systemd_ordering(tmp_path, manifest)
        assert len(records) == 1
        rec = records[0]
        assert rec.rel_path == dropin_rel_path("b.service")
        assert rec.target_path == "/" + dropin_rel_path("b.service")
        assert rec.after == ("a.service", "c.service")
        assert rec.mode == 0o644
        dst = tmp_path / rec.rel_path
        assert dst.read_text() == "[Unit]\nAfter=a.service c.service\n"
        assert (dst.stat().st_mode & 0o777) == 0o644
        assert rec.sha256

    def test_empty_after_produces_no_file(self, tmp_path: Path):
        manifest = ServiceManifest(services={
            "b": _svc("b", units=["b.service"], after=[]),
        })
        assert materialize_systemd_ordering(tmp_path, manifest) == []
        assert not (tmp_path / "etc" / "systemd" / "system").exists()

    def test_multiple_units_share_after(self, tmp_path: Path):
        manifest = ServiceManifest(services={
            "b": _svc("b", units=["b.service", "b-helper.service"],
                      after=["a.service"]),
        })
        records = materialize_systemd_ordering(tmp_path, manifest)
        assert {r.unit for r in records} == {"b.service", "b-helper.service"}
        for rec in records:
            assert (tmp_path / rec.rel_path).read_text() == \
                "[Unit]\nAfter=a.service\n"

    def test_existing_file_conflict_fails(self, tmp_path: Path):
        dst = tmp_path / dropin_rel_path("b.service")
        dst.parent.mkdir(parents=True)
        dst.write_text("# service-repo-owned file\n")
        manifest = ServiceManifest(services={
            "b": _svc("b", units=["b.service"], after=["a.service"]),
        })
        with pytest.raises(BuildFailure, match="already exists in staging"):
            materialize_systemd_ordering(tmp_path, manifest)

    def test_duplicate_path_across_services_fails(self, tmp_path: Path):
        manifest = ServiceManifest(services={
            "b": _svc("b", units=["shared.service"], after=["a.service"]),
            "c": _svc("c", units=["shared.service"], after=["a.service"]),
        })
        with pytest.raises(BuildFailure, match="claimed by both"):
            materialize_systemd_ordering(tmp_path, manifest)

    def test_invalid_unit_name_fails(self, tmp_path: Path):
        manifest = ServiceManifest(services={
            "b": _svc("b", units=["../../evil"], after=["a.service"]),
        })
        with pytest.raises(BuildFailure):
            materialize_systemd_ordering(tmp_path, manifest)

    def test_invalid_after_entry_fails(self, tmp_path: Path):
        manifest = ServiceManifest(services={
            "b": _svc("b", units=["b.service"], after=["bad name"]),
        })
        with pytest.raises(BuildFailure):
            materialize_systemd_ordering(tmp_path, manifest)


class TestArtifactRecording:
    def test_dropins_recorded_as_build_owned(self, tmp_path: Path, project_root: Path):
        from tbox_build.orchestrator import BuildOrchestrator, BuildConfig as OrchConfig

        manifest = ServiceManifest(services={
            "b": _svc("b", units=["b.service"], after=["a.service"]),
        })
        orch = BuildOrchestrator(Project(project_root), OrchConfig(dry_run=True))
        orch.staging = StagingDir(tmp_path, "orin", "release")
        orch.staging.prepare()
        orch._materialize_ordering(manifest)

        am = orch._generate_artifact_manifest(
            manifest, lock=None, git_commit="test", service_results=[]
        )
        owned = [e for e in am.entries if e.build_owned]
        assert len(owned) == 1
        entry = owned[0]
        assert entry.owner_service == "build"
        assert entry.install_component == "build-ordering"
        assert entry.source_service == "b"
        assert entry.after_ordering == ["a.service"]
        assert entry.mode == "0644"
        assert entry.path == "rootfs/" + dropin_rel_path("b.service")
        assert entry.sha256

    def test_missing_artifact_entry_fails(self, tmp_path: Path, project_root: Path):
        from tbox_build.orchestrator import BuildOrchestrator, BuildConfig as OrchConfig

        manifest = ServiceManifest(services={
            "b": _svc("b", units=["b.service"], after=["a.service"]),
        })
        orch = BuildOrchestrator(Project(project_root), OrchConfig(dry_run=True))
        orch.staging = StagingDir(tmp_path, "orin", "release")
        orch.staging.prepare()
        orch._materialize_ordering(manifest)
        # Remove the generated file to simulate a lost/unrecorded drop-in.
        (orch.staging.install_root / dropin_rel_path("b.service")).unlink()
        with pytest.raises(BuildFailure, match="missing from the artifact manifest"):
            orch._generate_artifact_manifest(
                manifest, lock=None, git_commit="test", service_results=[]
            )
