"""CR-005 schema tests: strict runtime.after / runtime.after_waivers rules.

Covers BUILD-REQ-051 (structured waiver) and BUILD-REQ-050 (unique after).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tbox_build.errors import SchemaValidationError
from tbox_build.schema import validate_service_manifest


def _manifest(runtime: dict) -> dict:
    return {
        "services": {
            "svc": {
                "repository": "svc",
                "kind": "service",
                "build": {"preset": "orin-release", "target": "svc"},
                "runtime": runtime,
            }
        }
    }


VALID_WAIVER = {
    "dependency": "mqtt",
    "unit": "tbox-mqtt.service",
    "owner": "tsp",
    "reason": "TSP supports DEGRADED local IPC when MQTT is unavailable",
}


class TestAfter:
    def test_valid_after(self, project_root: Path):
        validate_service_manifest(
            _manifest({"systemd_units": ["a.service"], "after": ["b.service"]}),
            project_root,
        )

    def test_duplicate_after_rejected(self, project_root: Path):
        with pytest.raises(SchemaValidationError):
            validate_service_manifest(
                _manifest({"after": ["b.service", "b.service"]}), project_root
            )


class TestWaivers:
    def test_valid_waiver(self, project_root: Path):
        validate_service_manifest(
            _manifest({
                "systemd_units": ["a.service"],
                "after": [],
                "after_waivers": [VALID_WAIVER],
            }),
            project_root,
        )

    def test_optional_review_condition(self, project_root: Path):
        waiver = dict(VALID_WAIVER, review_condition="revisit when MQTT gains readiness")
        validate_service_manifest(
            _manifest({"after_waivers": [waiver]}), project_root
        )

    def test_missing_reason_rejected(self, project_root: Path):
        waiver = dict(VALID_WAIVER)
        del waiver["reason"]
        with pytest.raises(SchemaValidationError):
            validate_service_manifest(
                _manifest({"after_waivers": [waiver]}), project_root
            )

    def test_empty_reason_rejected(self, project_root: Path):
        waiver = dict(VALID_WAIVER, reason="")
        with pytest.raises(SchemaValidationError):
            validate_service_manifest(
                _manifest({"after_waivers": [waiver]}), project_root
            )

    def test_unknown_field_rejected(self, project_root: Path):
        waiver = dict(VALID_WAIVER, extra="nope")
        with pytest.raises(SchemaValidationError):
            validate_service_manifest(
                _manifest({"after_waivers": [waiver]}), project_root
            )

    def test_duplicate_waiver_rejected(self, project_root: Path):
        with pytest.raises(SchemaValidationError):
            validate_service_manifest(
                _manifest({"after_waivers": [VALID_WAIVER, VALID_WAIVER]}),
                project_root,
            )
