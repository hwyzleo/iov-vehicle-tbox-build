"""Integration tests for CR-004 release-chain protection gates.

Covers the end-to-end wiring: validate_all integrates D1/D3/D2 rules on the
real project, and the ELF symbol-integrity gate (D4) runs over the real
staging install-root when it exists.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tbox_build.elfcheck import (
    check_symbol_integrity,
    check_symbol_integrity_staging,
)
from tbox_build.errors import ValidationFailure
from tbox_build.manifest import Project
from tbox_build.orchestrator import BuildOrchestrator, BuildConfig
from tbox_build.validator import validate_all


def _install_root() -> Path:
    return (
        Path(__file__).resolve().parent.parent.parent
        / "out" / "orin" / "release" / "install-root"
    )


def _sysroot() -> Path:
    return (
        Path(__file__).resolve().parent.parent.parent
        / "sysroots" / "orin-r35.3.1"
    )


class TestValidationIntegration:
    def test_validate_all_runs_cr004_gates(self, project_root: Path):
        """D1/D3/D2 gates run on the real project without failing it."""
        project = Project(project_root)
        manifest = project.load_service_manifest()
        warnings = validate_all(manifest, project_root)
        # The only pre-existing warning is the hello example config-path gap.
        assert not any("not registered" in w for w in warnings)
        assert not any("retirement" in w.lower() for w in warnings)

    def test_load_and_validate_orchestrator(self, project_root: Path):
        """The build orchestrator's manifest gate accepts the real project."""
        orch = BuildOrchestrator(Project(project_root), BuildConfig(dry_run=True))
        svc_manifest, rs_manifest = orch.load_and_validate()
        assert "diag" in svc_manifest.services

    def test_inventory_covers_registered_services(self, project_root: Path):
        project = Project(project_root)
        inventory = project.load_repository_inventory()
        assert inventory is not None
        registered = set(project.load_service_manifest().services)
        # every registered service has an inventory entry (authoritative scope)
        for svc_id in registered:
            assert svc_id in inventory.repositories, (
                f"registered service '{svc_id}' missing from repository-inventory.yaml"
            )

    def test_retirement_manifest_backward_compat(self, project_root: Path):
        """Existing service manifests stay valid; retirement fields optional."""
        project = Project(project_root)
        rm = project.load_retirement_manifest()
        assert rm is not None
        for platform, rp in rm.platforms.items():
            assert hasattr(rp, "retired_units") and hasattr(rp, "retired_paths")


@pytest.mark.skipif(
    not _install_root().is_dir() or not _sysroot().is_dir(),
    reason="staged install-root or sysroot not available",
)
class TestSymbolIntegrityIntegration:
    def test_staging_passes_symbol_integrity(self, project_root: Path):
        """The composed staging set must satisfy its own DT_NEEDED closure.

        Mirrors verify.py's elf-symbol-integrity check with the same
        controlled-root precedence (install-root -> sdk -> deps -> sysroot).
        """
        project = Project(project_root)
        staging_root = _install_root()
        sdk = staging_root.parent / "sdk"
        deps = staging_root.parent / "deps"
        roots = [staging_root]
        if sdk.is_dir():
            roots.extend(sorted(p for p in sdk.iterdir() if p.is_dir()))
        roots.append(deps)
        roots.append(project.sysroot_path)

        results = check_symbol_integrity_staging(
            staging_root, roots=roots, missing_library_policy="error"
        )
        violations = sum(len(r.violations) for r in results)
        assert violations == 0, "\n".join(
            v for r in results for v in r.violations[:3]
        )
        # strong checks ran on real ELFs; at least one consumer was examined
        assert len(results) >= 1

    def test_staged_diag_closure_complete(self, project_root: Path):
        """Regression: tbox_diag's closure must satisfy its strong symbols
        (the historical libhwyz.so/yaml-cpp failure mode, CR-004 §2)."""
        staging_root = _install_root()
        diag = staging_root / "usr" / "bin" / "tbox_diag"
        if not diag.is_file():
            pytest.skip("tbox_diag not staged in this build")
        project = Project(project_root)
        results = check_symbol_integrity(
            [diag],
            roots=[staging_root, project.sysroot_path],
            missing_library_policy="error",
        )
        assert results[0].violations == []
