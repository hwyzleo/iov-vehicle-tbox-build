"""CR-005 manifest parsing tests: runtime.after_waivers model (BUILD-REQ-051)."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from tbox_build.errors import SchemaValidationError
from tbox_build.manifest import load_service_manifest


def _write(path: Path, services: dict) -> Path:
    path.write_text(yaml.safe_dump({"services": services}), encoding="utf-8")
    return path


def _service(runtime: dict) -> dict:
    return {
        "repository": "svc",
        "kind": "service",
        "build": {"preset": "orin-release", "target": "svc"},
        "runtime": runtime,
    }


class TestAfterWaiverParsing:
    def test_parses_waiver(self, tmp_path: Path):
        path = _write(tmp_path / "services.yaml", {
            "tsp": _service({
                "systemd_units": ["tbox-tsp.service"],
                "after": [],
                "after_waivers": [{
                    "dependency": "mqtt",
                    "unit": "tbox-mqtt.service",
                    "owner": "tsp",
                    "reason": "DEGRADED local IPC",
                }],
            })
        })
        manifest = load_service_manifest(path)
        waivers = manifest.get("tsp").runtime.after_waivers
        assert len(waivers) == 1
        assert waivers[0].dependency == "mqtt"
        assert waivers[0].unit == "tbox-mqtt.service"
        assert waivers[0].owner == "tsp"
        assert waivers[0].reason == "DEGRADED local IPC"
        assert waivers[0].review_condition is None

    def test_optional_review_condition(self, tmp_path: Path):
        path = _write(tmp_path / "services.yaml", {
            "tsp": _service({
                "after_waivers": [{
                    "dependency": "mqtt",
                    "unit": "tbox-mqtt.service",
                    "owner": "tsp",
                    "reason": "r",
                    "review_condition": "after MQTT notify",
                }],
            })
        })
        waiver = load_service_manifest(path).get("tsp").runtime.after_waivers[0]
        assert waiver.review_condition == "after MQTT notify"

    def test_default_is_empty(self, tmp_path: Path):
        path = _write(tmp_path / "services.yaml", {
            "svc": _service({"systemd_units": ["svc.service"]})
        })
        assert load_service_manifest(path).get("svc").runtime.after_waivers == []

    def test_non_object_entry_rejected(self, tmp_path: Path):
        path = _write(tmp_path / "services.yaml", {
            "svc": _service({"after_waivers": ["not-an-object"]})
        })
        with pytest.raises(SchemaValidationError):
            load_service_manifest(path)

    def test_non_list_rejected(self, tmp_path: Path):
        path = _write(tmp_path / "services.yaml", {
            "svc": _service({"after_waivers": {"dependency": "x"}})
        })
        with pytest.raises(SchemaValidationError):
            load_service_manifest(path)
