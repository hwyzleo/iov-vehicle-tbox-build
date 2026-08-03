"""Unit tests for schema validation."""

from __future__ import annotations

from pathlib import Path

import pytest

from tbox_build.errors import SchemaValidationError
from tbox_build.schema import (
    validate_service_manifest,
    validate_release_set_manifest,
    load_schema,
)


class TestServiceSchema:
    def test_valid_service_manifest(self, project_root: Path):
        from tbox_build.manifest import load_yaml
        data = load_yaml(project_root / "manifests" / "services.yaml")
        validate_service_manifest(data, project_root)

    def test_missing_required_field_repository(self, project_root: Path):
        data = {
            "services": {
                "svc-a": {
                    "build": {"target": "a", "preset": "orin-release"},
                }
            }
        }
        with pytest.raises(SchemaValidationError):
            validate_service_manifest(data, project_root)

    def test_missing_required_field_build(self, project_root: Path):
        data = {
            "services": {
                "svc-a": {
                    "repository": ".",
                }
            }
        }
        with pytest.raises(SchemaValidationError):
            validate_service_manifest(data, project_root)

    def test_missing_required_field_target(self, project_root: Path):
        data = {
            "services": {
                "svc-a": {
                    "repository": ".",
                    "build": {"preset": "orin-release"},
                }
            }
        }
        with pytest.raises(SchemaValidationError):
            validate_service_manifest(data, project_root)

    def test_unknown_field_rejected(self, project_root: Path):
        data = {
            "services": {
                "svc-a": {
                    "repository": ".",
                    "build": {"target": "a", "preset": "orin-release"},
                    "unknown_field": "bad",
                }
            }
        }
        with pytest.raises(SchemaValidationError):
            validate_service_manifest(data, project_root)

    def test_empty_services_rejected(self, project_root: Path):
        data = {"services": {}}
        with pytest.raises(SchemaValidationError):
            validate_service_manifest(data, project_root)

    def test_invalid_service_id_uppercase(self, project_root: Path):
        data = {
            "services": {
                "ServiceA": {
                    "repository": ".",
                    "build": {"target": "a", "preset": "orin-release"},
                }
            }
        }
        with pytest.raises(SchemaValidationError):
            validate_service_manifest(data, project_root)

    def test_wrong_type_for_dependencies(self, project_root: Path):
        data = {
            "services": {
                "svc-a": {
                    "repository": ".",
                    "build": {
                        "target": "a",
                        "preset": "orin-release",
                        "dependencies": "not-a-list",
                    },
                }
            }
        }
        with pytest.raises(SchemaValidationError):
            validate_service_manifest(data, project_root)

    def test_optional_runtime_fields(self, project_root: Path):
        data = {
            "services": {
                "svc-a": {
                    "repository": ".",
                    "build": {"target": "a", "preset": "orin-release"},
                    "runtime": {
                        "systemd_units": ["svc-a.service"],
                        "after": ["network.target"],
                        "config_paths": ["/etc/tbox/svc-a"],
                    },
                }
            }
        }
        validate_service_manifest(data, project_root)


class TestReleaseSetSchema:
    def test_valid_release_set(self, project_root: Path):
        from tbox_build.manifest import load_yaml
        data = load_yaml(project_root / "manifests" / "release-set.yaml")
        validate_release_set_manifest(data, project_root)

    def test_missing_services(self, project_root: Path):
        data = {
            "release_sets": {
                "test-set": {
                    "description": "test",
                }
            }
        }
        with pytest.raises(SchemaValidationError):
            validate_release_set_manifest(data, project_root)

    def test_empty_services_list(self, project_root: Path):
        data = {
            "release_sets": {
                "test-set": {
                    "services": [],
                }
            }
        }
        with pytest.raises(SchemaValidationError):
            validate_release_set_manifest(data, project_root)

    def test_unknown_field_rejected(self, project_root: Path):
        data = {
            "release_sets": {
                "test-set": {
                    "services": ["svc-a"],
                    "bad_field": True,
                }
            }
        }
        with pytest.raises(SchemaValidationError):
            validate_release_set_manifest(data, project_root)


class TestSchemaLoading:
    def test_load_service_schema(self, project_root: Path):
        schema = load_schema("service", project_root)
        assert schema["title"] == "TBOX Build Service Manifest"
        assert "services" in schema["properties"]

    def test_load_release_set_schema(self, project_root: Path):
        schema = load_schema("release-set", project_root)
        assert schema["title"] == "TBOX Build Release Set Manifest"
        assert "release_sets" in schema["properties"]
