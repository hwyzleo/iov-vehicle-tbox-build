"""Pre-build manifest validation for TBOX Build.

Performs filesystem-dependent and cross-reference checks that the
schema validator and pure-graph analyser cannot cover:

  * systemd unit reference consistency (``after`` -> declared ``systemd_units``)
  * health/smoke script existence in service repositories
  * full manifest validation entry point
"""

from __future__ import annotations

import os
import stat
from pathlib import Path

from .errors import ValidationFailure
from .graph import DependencyGraph
from .manifest import ServiceManifest

# Well-known systemd targets that services may legitimately depend on
# without being declared by another TBOX service.
_SYSTEMD_EXTERNAL_TARGETS = frozenset({
    "basic.target",
    "default.target",
    "multi-user.target",
    "network.target",
    "network-online.target",
    "sockets.target",
    "sysinit.target",
    "systemd-journald.service",
})


def validate_systemd_references(manifest: ServiceManifest) -> None:
    """Ensure every ``after`` entry references a declared or well-known unit."""
    declared_units: set[str] = set()
    for svc in manifest:
        declared_units.update(svc.runtime.systemd_units)

    violations: list[str] = []
    for svc in manifest:
        for after_unit in svc.runtime.after:
            if after_unit in declared_units:
                continue
            if after_unit in _SYSTEMD_EXTERNAL_TARGETS:
                continue
            violations.append(
                f"Service '{svc.id}' references unknown systemd unit "
                f"'{after_unit}' in runtime.after"
            )
    if violations:
        raise ValidationFailure(
            f"systemd unit reference validation failed ({len(violations)} violation(s))",
            violations,
        )


def validate_health_smoke(manifest: ServiceManifest, project_root: Path) -> None:
    """Check that declared health_check / smoke_test scripts exist on disk."""
    violations: list[str] = []
    for svc in manifest:
        svc_root = project_root / svc.effective_source_dir
        for check_type, rel_path in (
            ("health_check", svc.runtime.health_check),
            ("smoke_test", svc.runtime.smoke_test),
        ):
            if rel_path is None:
                continue
            full_path = svc_root / rel_path
            if not full_path.is_file():
                violations.append(
                    f"Service '{svc.id}' {check_type} script not found: {full_path}"
                )
                continue
            st = full_path.stat()
            if not (st.st_mode & (stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)):
                violations.append(
                    f"Service '{svc.id}' {check_type} script not executable: {full_path}"
                )
    if violations:
        raise ValidationFailure(
            f"health/smoke script validation failed ({len(violations)} violation(s))",
            violations,
        )


def validate_all(manifest: ServiceManifest, project_root: Path) -> list[str]:
    """Run all pre-build validations.

    Raises :class:`ValidationFailure`, :class:`DependencyError` or
    :class:`CycleError` on failure.  Returns a list of non-fatal warning
    strings.
    """
    warnings: list[str] = []

    # 1. Dependency graph: cycles, missing deps
    graph = DependencyGraph(manifest)
    missing = graph.find_missing_dependencies()
    if missing:
        details = [f"{sid} depends on undeclared: {deps}" for sid, deps in missing.items()]
        raise ValidationFailure(
            f"Missing dependencies detected ({len(missing)} service(s))",
            details,
        )
    cycles = graph.detect_cycles()
    if cycles:
        raise ValidationFailure(
            f"Dependency cycles detected ({len(cycles)} cycle(s))",
            [f"{' -> '.join(c)}" for c in cycles],
        )

    # 2. systemd unit references
    validate_systemd_references(manifest)

    # 3. health/smoke script existence
    validate_health_smoke(manifest, project_root)

    return warnings
