"""Custom exception classes for TBOX Build."""

from __future__ import annotations


class TboxBuildError(Exception):
    """Base class for all TBOX Build errors."""


class ManifestError(TboxBuildError):
    """Manifest loading or parsing error."""


class SchemaValidationError(TboxBuildError):
    """Schema validation failed."""

    def __init__(self, message: str, errors: list | None = None):
        super().__init__(message)
        self.errors = errors or []


class DependencyError(TboxBuildError):
    """Dependency graph error (missing dependency, cross-layer reverse dep)."""


class CycleError(TboxBuildError):
    """Dependency cycle detected."""

    def __init__(self, message: str, cycles: list | None = None):
        super().__init__(message)
        self.cycles = cycles or []


class ValidationFailure(TboxBuildError):
    """Manifest validation failure (unique ID, systemd refs, health/smoke)."""

    def __init__(self, message: str, details: list | None = None):
        super().__init__(message)
        self.details = details or []


class PathConflictError(TboxBuildError):
    """Installation path conflict between services."""

    def __init__(self, message: str, conflicts: list | None = None):
        super().__init__(message)
        self.conflicts = conflicts or []


class ElfCheckError(TboxBuildError):
    """ELF architecture or host pollution check failure."""

    def __init__(self, message: str, violations: list | None = None):
        super().__init__(message)
        self.violations = violations or []


class BuildFailure(TboxBuildError):
    """Build step failure."""


class PackageError(TboxBuildError):
    """Packaging error."""


class ConfigValidationError(TboxBuildError):
    """Configuration validation failure (schema, secret, common source).

    Holds structured findings so callers can report without leaking
    matched secret values.
    """

    def __init__(self, message: str, details: list | None = None):
        super().__init__(message)
        self.details = details or []
