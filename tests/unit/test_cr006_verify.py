"""CR-006 packaging gate tests: declared units must be installed (CR-006 §3.3).

Covers BUILD-REQ-053/054 packaging enforcement: a service's declared
``runtime.systemd_units`` must actually be present in the staged payload /
release package produced by the locked service version. A name-only
registration must never stand in for the real unit file; missing units are
an explicit packaging failure.
"""

from __future__ import annotations

import json
import tarfile
from pathlib import Path

from tbox_build.staging import StagingDir
from tbox_build.verify import Verifier

MINIMAL_SERVICES = """\
services:
  time:
    kind: service
    source_dir: ../iov-vehicle-tbox-time
    build:
      preset: orin-release
      targets: [tbox_time]
      service_dependencies: []
      install_components: []
    runtime:
      systemd_units: [tbox-time.service]
      after: []
  sec:
    kind: service
    source_dir: ../iov-vehicle-tbox-sec
    build:
      preset: orin-release
      targets: [tbox_sec]
      service_dependencies: [time]
      install_components: []
    runtime:
      systemd_units: [tbox-sec.service]
      after: [tbox-time.service]
"""


def _make_project(tmp_path: Path) -> Path:
    (tmp_path / "manifests").mkdir()
    (tmp_path / "manifests" / "services.yaml").write_text(
        MINIMAL_SERVICES, encoding="utf-8"
    )
    return tmp_path


def _make_staging(project_root: Path) -> StagingDir:
    staging = StagingDir(project_root, "orin", "release")
    staging.prepare(clean=True)
    return staging


def _unit_dir(staging: StagingDir) -> Path:
    d = staging.install_root / "usr" / "lib" / "systemd" / "system"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _write_artifact_manifest(staging: StagingDir, owners: list[str]) -> None:
    staging.manifests_dir.mkdir(parents=True, exist_ok=True)
    artifacts = [
        {"path": f"install-root/usr/bin/{owner}", "owner_service": owner}
        for owner in owners
    ]
    (staging.manifests_dir / "artifact-manifest.json").write_text(
        json.dumps({"artifacts": artifacts}), encoding="utf-8"
    )


class TestStagingGate:
    def test_unit_present_passes(self, tmp_path: Path):
        project = _make_project(tmp_path)
        staging = _make_staging(project)
        (_unit_dir(staging) / "tbox-time.service").write_text("[Unit]\n")
        (_unit_dir(staging) / "tbox-sec.service").write_text("[Unit]\n")
        _write_artifact_manifest(staging, ["time", "sec"])

        result = Verifier(staging).verify_staging()
        checks = {c["name"]: c["status"] for c in result.checks}
        assert checks["systemd-units-declared"] == "success"

    def test_missing_declared_unit_fails(self, tmp_path: Path):
        project = _make_project(tmp_path)
        staging = _make_staging(project)
        # time's daemon artifact is present (owner_service evidence) but the
        # locked TIME version did NOT install tbox-time.service -> gate fails.
        (staging.install_root / "usr" / "bin").mkdir(parents=True, exist_ok=True)
        (staging.install_root / "usr" / "bin" / "tbox_time").write_text("elf")
        _write_artifact_manifest(staging, ["time"])

        result = Verifier(staging).verify_staging()
        checks = {c["name"]: c["status"] for c in result.checks}
        assert checks["systemd-units-declared"] == "failed"
        assert result.status == "failed"

    def test_service_not_in_package_not_required(self, tmp_path: Path):
        project = _make_project(tmp_path)
        staging = _make_staging(project)
        # Only SEC is in this package; SEC's own unit is present. TIME is not
        # part of the build, so its unit is not required.
        (_unit_dir(staging) / "tbox-sec.service").write_text("[Unit]\n")
        _write_artifact_manifest(staging, ["sec"])

        result = Verifier(staging).verify_staging()
        checks = {c["name"]: c["status"] for c in result.checks}
        assert checks["systemd-units-declared"] == "success"

    def test_no_artifact_manifest_skips_with_evidence(self, tmp_path: Path):
        project = _make_project(tmp_path)
        staging = _make_staging(project)
        result = Verifier(staging).verify_staging()
        check = next(c for c in result.checks if c["name"] == "systemd-units-declared")
        assert check["status"] == "success"
        assert "SKIPPED" in check["message"]
        assert "ownership evidence" in check["message"]


class TestPackageGate:
    def _write_package(
        self, tmp_path: Path, *, with_unit: bool, owners: list[str]
    ) -> Path:
        pkg = tmp_path / "pkg.tar.gz"
        with tarfile.open(pkg, "w:gz") as tar:
            if with_unit:
                data = b"[Unit]\n"
                info = tarfile.TarInfo(
                    "install-root/usr/lib/systemd/system/tbox-time.service"
                )
                info.size = len(data)
                tar.addfile(info, __import__("io").BytesIO(data))
            bin_data = b"elf"
            bin_info = tarfile.TarInfo("install-root/usr/bin/tbox_time")
            bin_info.size = len(bin_data)
            tar.addfile(bin_info, __import__("io").BytesIO(bin_data))
            manifest = json.dumps({
                "artifacts": [
                    {"path": "install-root/usr/bin/tbox_time",
                     "owner_service": owner}
                    for owner in owners
                ]
            }).encode("utf-8")
            m_info = tarfile.TarInfo("manifests/artifact-manifest.json")
            m_info.size = len(manifest)
            tar.addfile(m_info, __import__("io").BytesIO(manifest))
        return pkg

    def test_package_missing_unit_fails(self, tmp_path: Path):
        project = _make_project(tmp_path)
        staging = _make_staging(project)
        pkg = self._write_package(tmp_path, with_unit=False, owners=["time"])
        result = Verifier(staging).verify_package(pkg)
        checks = {c["name"]: c["status"] for c in result.checks}
        assert checks["systemd-units-declared"] == "failed"
        assert result.status == "failed"

    def test_package_with_unit_passes(self, tmp_path: Path):
        project = _make_project(tmp_path)
        staging = _make_staging(project)
        pkg = self._write_package(tmp_path, with_unit=True, owners=["time"])
        result = Verifier(staging).verify_package(pkg)
        checks = {c["name"]: c["status"] for c in result.checks}
        assert checks["systemd-units-declared"] == "success"
