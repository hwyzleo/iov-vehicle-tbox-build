"""Unit tests for CR-002 schema: plural targets/components, {name, staging}
install components, dependency field split, legacy compat and exclusivity."""

from __future__ import annotations

from pathlib import Path

import pytest

from tbox_build.errors import SchemaValidationError
from tbox_build.schema import (
    validate_service_manifest,
    validate_dependency_lock,
    load_schema,
)


def _service(**build_overrides):
    build = {"preset": "orin-release"}
    if "target" not in build_overrides and "targets" not in build_overrides:
        build["targets"] = ["a"]
    build.update(build_overrides)
    return {"services": {"svc-a": {"repository": ".", "build": build}}}


class TestPluralTargets:
    def test_plural_targets_accepted(self, project_root: Path):
        validate_service_manifest(
            _service(targets=["t1", "t2"]), project_root
        )

    def test_singular_target_accepted(self, project_root: Path):
        validate_service_manifest(_service(target="a"), project_root)

    def test_target_and_targets_rejected(self, project_root: Path):
        with pytest.raises(SchemaValidationError):
            validate_service_manifest(
                _service(target="a", targets=["b"]), project_root
            )

    def test_neither_target_nor_targets_rejected(self, project_root: Path):
        with pytest.raises(SchemaValidationError):
            validate_service_manifest(
                {"services": {"svc-a": {"repository": ".", "build": {"preset": "x"}}}},
                project_root,
            )


class TestInstallComponents:
    def test_object_array_accepted(self, project_root: Path):
        validate_service_manifest(
            _service(
                install_components=[
                    {"name": "sdk", "staging": "sdk"},
                    {"name": "rt", "staging": "rootfs"},
                ]
            ),
            project_root,
        )

    def test_singular_install_component_accepted(self, project_root: Path):
        validate_service_manifest(_service(install_component="rt"), project_root)

    def test_singular_and_plural_rejected(self, project_root: Path):
        with pytest.raises(SchemaValidationError):
            validate_service_manifest(
                _service(install_component="rt", install_components=[{"name": "x", "staging": "rootfs"}]),
                project_root,
            )

    def test_invalid_staging_rejected(self, project_root: Path):
        with pytest.raises(SchemaValidationError):
            validate_service_manifest(
                _service(install_components=[{"name": "x", "staging": "wrong"}]),
                project_root,
            )

    def test_missing_staging_rejected(self, project_root: Path):
        with pytest.raises(SchemaValidationError):
            validate_service_manifest(
                _service(install_components=[{"name": "x"}]),
                project_root,
            )

    def test_missing_name_rejected(self, project_root: Path):
        with pytest.raises(SchemaValidationError):
            validate_service_manifest(
                _service(install_components=[{"staging": "sdk"}]),
                project_root,
            )


class TestDependencyFields:
    def test_explicit_dep_fields_accepted(self, project_root: Path):
        validate_service_manifest(
            _service(
                service_dependencies=["svc-b"],
                target_dependencies=["yaml-cpp"],
            ),
            project_root,
        )

    def test_legacy_dependencies_accepted(self, project_root: Path):
        validate_service_manifest(_service(dependencies=["svc-b"]), project_root)

    def test_legacy_and_service_deps_rejected(self, project_root: Path):
        with pytest.raises(SchemaValidationError):
            validate_service_manifest(
                _service(dependencies=["x"], service_dependencies=["y"]),
                project_root,
            )

    def test_legacy_and_target_deps_rejected(self, project_root: Path):
        with pytest.raises(SchemaValidationError):
            validate_service_manifest(
                _service(dependencies=["x"], target_dependencies=["y"]),
                project_root,
            )


class TestKindField:
    def test_library_kind_accepted(self, project_root: Path):
        data = {
            "services": {
                "fw": {
                    "source_dir": ".",
                    "kind": "library",
                    "build": {"preset": "x", "targets": ["fw"]},
                    "runtime": {"systemd_units": []},
                }
            }
        }
        validate_service_manifest(data, project_root)

    def test_daemon_kind_accepted(self, project_root: Path):
        validate_service_manifest(
            {"services": {"svc-a": {"repository": ".", "kind": "daemon", "build": {"preset": "x", "target": "a"}}}},
            project_root,
        )

    def test_invalid_kind_rejected(self, project_root: Path):
        with pytest.raises(SchemaValidationError):
            validate_service_manifest(
                {"services": {"svc-a": {"repository": ".", "kind": "binary", "build": {"preset": "x", "target": "a"}}}},
                project_root,
            )


class TestSourceLocation:
    def test_source_dir_only_accepted(self, project_root: Path):
        validate_service_manifest(
            {"services": {"svc-a": {"source_dir": ".", "build": {"preset": "x", "target": "a"}}}},
            project_root,
        )

    def test_neither_repository_nor_source_dir_rejected(self, project_root: Path):
        with pytest.raises(SchemaValidationError):
            validate_service_manifest(
                {"services": {"svc-a": {"build": {"preset": "x", "target": "a"}}}},
                project_root,
            )


class TestLockSchema:
    def test_valid_lock(self, project_root: Path):
        from tbox_build.manifest import load_yaml
        data = load_yaml(project_root / "dependencies" / "lock.yaml")
        validate_dependency_lock(data, project_root)

    def test_dict_form_required(self, project_root: Path):
        with pytest.raises(SchemaValidationError):
            validate_dependency_lock({"dependencies": []}, project_root)

    def test_invalid_boundary_rejected(self, project_root: Path):
        data = {
            "dependencies": {
                "foo": {
                    "version": "1.0",
                    "source": {"url": "x", "sha256": "y"},
                    "license": "MIT",
                    "boundary": "HOST",
                    "architecture": "aarch64",
                    "linkage": "static",
                }
            }
        }
        with pytest.raises(SchemaValidationError):
            validate_dependency_lock(data, project_root)
