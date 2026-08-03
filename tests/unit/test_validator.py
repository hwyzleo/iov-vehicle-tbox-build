"""Unit tests for cross-reference validation."""

from __future__ import annotations

from pathlib import Path

import pytest

from tbox_build.errors import ValidationFailure
from tbox_build.manifest import Service, BuildConfig, RuntimeConfig, ServiceManifest
from tbox_build.validator import (
    validate_systemd_references,
    validate_health_smoke,
    validate_all,
)


def _make_service(
    sid: str,
    repository: str = ".",
    dependencies: list[str] | None = None,
    systemd_units: list[str] | None = None,
    after: list[str] | None = None,
    health_check: str | None = None,
    smoke_test: str | None = None,
) -> Service:
    return Service(
        id=sid,
        repository=repository,
        build=BuildConfig(
            preset="orin-release",
            targets=[sid],
            service_dependencies=dependencies or [],
        ),
        runtime=RuntimeConfig(
            systemd_units=systemd_units or [],
            after=after or [],
            health_check=health_check,
            smoke_test=smoke_test,
        ),
    )


class TestSystemdReferences:
    def test_valid_after_reference(self):
        svc_a = _make_service("svc-a", systemd_units=["svc-a.service"])
        svc_b = _make_service("svc-b", systemd_units=["svc-b.service"],
                              after=["svc-a.service"])
        manifest = ServiceManifest(services={"svc-a": svc_a, "svc-b": svc_b})
        validate_systemd_references(manifest)  # should not raise

    def test_external_target_allowed(self):
        svc_a = _make_service("svc-a", after=["network.target"])
        manifest = ServiceManifest(services={"svc-a": svc_a})
        validate_systemd_references(manifest)  # should not raise

    def test_unknown_unit_reference(self):
        svc_a = _make_service("svc-a", after=["nonexistent.service"])
        manifest = ServiceManifest(services={"svc-a": svc_a})
        with pytest.raises(ValidationFailure) as exc_info:
            validate_systemd_references(manifest)
        assert any("unknown systemd unit" in d for d in exc_info.value.details)

    def test_empty_after(self):
        svc_a = _make_service("svc-a", after=[])
        manifest = ServiceManifest(services={"svc-a": svc_a})
        validate_systemd_references(manifest)  # should not raise


class TestHealthSmoke:
    def test_existing_scripts_pass(self, project_root: Path):
        from tbox_build.manifest import Project
        project = Project(project_root)
        manifest = project.load_service_manifest()
        validate_health_smoke(manifest, project_root)  # should not raise

    def test_missing_health_check(self, tmp_path: Path):
        svc = _make_service("svc-a", health_check="nonexistent.sh")
        manifest = ServiceManifest(services={"svc-a": svc})
        with pytest.raises(ValidationFailure) as exc_info:
            validate_health_smoke(manifest, tmp_path)
        assert any("not found" in d for d in exc_info.value.details)

    def test_missing_smoke_test(self, tmp_path: Path):
        svc = _make_service("svc-a", smoke_test="nonexistent.sh")
        manifest = ServiceManifest(services={"svc-a": svc})
        with pytest.raises(ValidationFailure) as exc_info:
            validate_health_smoke(manifest, tmp_path)
        assert any("not found" in d for d in exc_info.value.details)

    def test_no_health_smoke_ok(self, tmp_path: Path):
        svc = _make_service("svc-a")
        manifest = ServiceManifest(services={"svc-a": svc})
        validate_health_smoke(manifest, tmp_path)  # should not raise

    def test_non_executable_script(self, tmp_path: Path):
        script = tmp_path / "script.sh"
        script.write_text("#!/bin/bash\necho hello\n")
        script.chmod(0o644)  # not executable
        svc = _make_service("svc-a", health_check="script.sh")
        manifest = ServiceManifest(services={"svc-a": svc})
        with pytest.raises(ValidationFailure) as exc_info:
            validate_health_smoke(manifest, tmp_path)
        assert any("not executable" in d for d in exc_info.value.details)


class TestValidateAll:
    def test_real_manifest_validates(self, project_root: Path):
        from tbox_build.manifest import Project
        project = Project(project_root)
        manifest = project.load_service_manifest()
        warnings = validate_all(manifest, project_root)
        assert isinstance(warnings, list)

    def test_validate_all_with_missing_dep(self, tmp_path: Path):
        svc = _make_service("svc-a", dependencies=["nonexistent"])
        manifest = ServiceManifest(services={"svc-a": svc})
        with pytest.raises(ValidationFailure, match="service_dependencies"):
            validate_all(manifest, tmp_path)

    def test_validate_all_with_cycle(self, tmp_path: Path):
        svc_a = _make_service("svc-a", dependencies=["svc-b"])
        svc_b = _make_service("svc-b", dependencies=["svc-a"])
        manifest = ServiceManifest(services={"svc-a": svc_a, "svc-b": svc_b})
        with pytest.raises(ValidationFailure, match="cycles"):
            validate_all(manifest, tmp_path)
