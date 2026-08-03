"""Schema validation for TBOX Build manifests.

Validates service and release-set manifests against JSON Schema
definitions stored as YAML in manifests/schemas/.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import jsonschema
import yaml

from .errors import SchemaValidationError

_SCHEMA_CACHE: dict[str, dict[str, Any]] = {}


def _schemas_dir(project_root: Path | None = None) -> Path:
    if project_root is not None:
        return project_root / "manifests" / "schemas"
    # Fall back to relative path from this module
    return Path(__file__).resolve().parent.parent / "manifests" / "schemas"


def load_schema(name: str, project_root: Path | None = None) -> dict[str, Any]:
    """Load a JSON Schema (stored as YAML) by name.

    Looks for ``manifests/schemas/<name>.schema.yaml``.
    """
    if name in _SCHEMA_CACHE:
        return _SCHEMA_CACHE[name]
    path = _schemas_dir(project_root) / f"{name}.schema.yaml"
    if not path.is_file():
        raise SchemaValidationError(f"Schema file not found: {path}")
    with open(path, encoding="utf-8") as f:
        schema = yaml.safe_load(f)
    _SCHEMA_CACHE[name] = schema
    return schema


def _format_errors(errors: list[jsonschema.ValidationError]) -> list[str]:
    return [f"{'.'.join(str(p) for p in e.absolute_path) or '(root)'}: {e.message}" for e in errors]


def validate_service_manifest(data: dict[str, Any], project_root: Path | None = None) -> None:
    """Validate a service manifest dict against the service schema.

    Raises SchemaValidationError on failure.
    """
    schema = load_schema("service", project_root)
    validator = jsonschema.Draft7Validator(schema)
    errors = sorted(validator.iter_errors(data), key=lambda e: list(e.absolute_path))
    if errors:
        raise SchemaValidationError(
            f"Service manifest schema validation failed ({len(errors)} error(s))",
            _format_errors(errors),
        )


def validate_release_set_manifest(
    data: dict[str, Any], project_root: Path | None = None
) -> None:
    """Validate a release-set manifest dict against the release-set schema.

    Raises SchemaValidationError on failure.
    """
    schema = load_schema("release-set", project_root)
    validator = jsonschema.Draft7Validator(schema)
    errors = sorted(validator.iter_errors(data), key=lambda e: list(e.absolute_path))
    if errors:
        raise SchemaValidationError(
            f"Release-set manifest schema validation failed ({len(errors)} error(s))",
            _format_errors(errors),
        )
