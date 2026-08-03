"""TBOX Build CLI entry point.

Usage::

    python3 -m tbox_build <command> [options]

Commands:
    validate   Validate manifests (schema, graph, cross-references)
    build      Configure, build, install, ELF-check and generate manifest
    package    Create a release package from staging
    deploy     Deploy a package to a target device
    verify     Verify staging output or a release package
    elfcheck   Run ELF/pollution checks on staging
    sysroot    Sysroot management (import, verify, fix-symlinks)
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import __version__
from .manifest import Project
from .schema import validate_service_manifest, validate_release_set_manifest
from .manifest import load_yaml
from .validator import validate_all
from .graph import DependencyGraph
from .orchestrator import BuildOrchestrator, BuildConfig
from .packaging import Packager
from .deploy import Deployer
from .verify import Verifier
from .elfcheck import check_staging, assert_clean


def _add_common_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--project-root", "-r",
        default=None,
        help="Path to the tbox-build project root (default: current directory)",
    )


def _get_project(args: argparse.Namespace) -> Project:
    return Project(args.project_root)


def cmd_validate(args: argparse.Namespace) -> int:
    """Validate manifests."""
    project = _get_project(args)
    print("=== Validating manifests ===")

    # Schema validation
    svc_raw = load_yaml(project.service_manifest_path)
    rs_raw = load_yaml(project.release_set_manifest_path)

    try:
        validate_service_manifest(svc_raw, project.root)
        print(f"  [PASS] Service manifest schema validation")
    except Exception as exc:
        print(f"  [FAIL] Service manifest schema: {exc}")
        return 1

    try:
        validate_release_set_manifest(rs_raw, project.root)
        print(f"  [PASS] Release-set manifest schema validation")
    except Exception as exc:
        print(f"  [FAIL] Release-set manifest schema: {exc}")
        return 1

    # Load and cross-reference validation
    service_manifest = project.load_service_manifest()
    release_set_manifest = project.load_release_set_manifest()

    print(f"  Services: {list(service_manifest.services.keys())}")
    print(f"  Release sets: {list(release_set_manifest.release_sets.keys())}")

    try:
        validate_all(service_manifest, project.root)
        print(f"  [PASS] Cross-reference validation (deps, systemd, health/smoke)")
    except Exception as exc:
        print(f"  [FAIL] Cross-reference validation: {exc}")
        return 1

    # Topological sort
    graph = DependencyGraph(service_manifest)
    try:
        order = graph.build_order()
        print(f"  [PASS] Topological sort: {' -> '.join(order)}")
    except Exception as exc:
        print(f"  [FAIL] Topological sort: {exc}")
        return 1

    # Platform manifest
    pm = project.load_platform_manifest()
    print(f"  Platform: {pm.platform}, arch: {pm.architecture}, "
          f"sysroot: {pm.sysroot_id}")

    print("\nAll validations passed.")
    return 0


def cmd_build(args: argparse.Namespace) -> int:
    """Build services."""
    project = _get_project(args)
    config = BuildConfig(
        platform=args.platform,
        profile=args.profile,
        jobs=args.jobs,
        clean=args.clean,
        dry_run=args.dry_run,
    )
    orch = BuildOrchestrator(project, config)

    set_id = args.set
    service_id = args.service

    if not set_id and not service_id and not args.all:
        print("Error: must specify --set, --service, or --all")
        return 1

    report = orch.build(set_id=set_id, service_id=service_id)

    print(f"\n=== Build Report ===")
    print(f"Status: {report.status}")
    print(f"Duration: {report.duration_seconds:.1f}s")
    for sr in report.service_results:
        print(f"  [{sr.id}] {sr.status} ({sr.duration_seconds:.1f}s)")
        for step, status in sr.steps.items():
            print(f"    {step}: {status}")
        if sr.installed_files:
            print(f"    Installed: {len(sr.installed_files)} file(s)")
    if report.errors:
        print(f"\nErrors:")
        for err in report.errors:
            print(f"  - {err}")

    return 0 if report.status == "success" else 1


def cmd_package(args: argparse.Namespace) -> int:
    """Create a release package."""
    project = _get_project(args)
    from .staging import StagingDir
    from .artifact import get_git_commit

    staging = StagingDir(project.root, args.platform, args.profile)
    packager = Packager(staging)
    git_commit = get_git_commit(project.root)

    try:
        pkg_path = packager.create_package(
            name=args.name,
            version=args.version,
            git_commit=git_commit,
        )
        print(f"\nPackage: {pkg_path}")
        return 0
    except Exception as exc:
        print(f"Packaging failed: {exc}")
        return 1


def cmd_deploy(args: argparse.Namespace) -> int:
    """Deploy a package."""
    project = _get_project(args)
    deployer = Deployer(project, target_host=args.host, target_user=args.user)

    package_path = Path(args.package)
    report = deployer.deploy(package_path, dry_run=args.dry_run)

    print(f"Deploy status: {report.status}")
    for step in report.steps:
        print(f"  [{step.name}] {step.status}: {step.message}")
    if report.errors:
        print("Errors:")
        for err in report.errors:
            print(f"  - {err}")
    return 0 if "success" in report.status else 1


def cmd_verify(args: argparse.Namespace) -> int:
    """Verify staging or package."""
    project = _get_project(args)
    from .staging import StagingDir

    if args.package:
        staging = StagingDir(project.root, args.platform, args.profile)
        verifier = Verifier(staging)
        result = verifier.verify_package(Path(args.package))
    else:
        staging = StagingDir(project.root, args.platform, args.profile)
        verifier = Verifier(staging)
        result = verifier.verify_staging()

    print(f"Verify status: {result.status}")
    for check in result.checks:
        status_icon = "✓" if check["status"] == "success" else "✗"
        print(f"  {status_icon} [{check['name']}] {check['message']}")
    if result.errors:
        print("Errors:")
        for err in result.errors:
            print(f"  - {err}")
    return 0 if result.status == "success" else 1


def cmd_elfcheck(args: argparse.Namespace) -> int:
    """Run ELF/pollution checks on staging."""
    project = _get_project(args)
    from .staging import StagingDir

    staging = StagingDir(project.root, args.platform, args.profile)
    if not staging.install_root.exists():
        print(f"Staging install-root not found: {staging.install_root}")
        return 1

    results = check_staging(staging.install_root)
    total_violations = 0
    total_warnings = 0
    for r in results:
        if r.violations or r.warnings:
            print(f"\n{r.path}:")
            for v in r.violations:
                print(f"  VIOLATION: {v}")
                total_violations += 1
            for w in r.warnings:
                print(f"  WARNING: {w}")
                total_warnings += 1

    print(f"\n{len(results)} file(s) checked, "
          f"{total_violations} violation(s), {total_warnings} warning(s)")

    if total_violations > 0:
        return 1
    return 0


def cmd_sysroot(args: argparse.Namespace) -> int:
    """Sysroot management."""
    project = _get_project(args)
    from .sysroot import SysrootManager

    manager = SysrootManager(project)

    if args.action == "import":
        report = manager.import_sysroot(dry_run=args.dry_run)
    elif args.action == "verify":
        report = manager.verify_sysroot()
    elif args.action == "fix-symlinks":
        report = manager.fix_absolute_symlinks(dry_run=args.dry_run)
    elif args.action == "manifest":
        report = manager.generate_manifest()
    else:
        print(f"Unknown sysroot action: {args.action}")
        return 1

    print(json.dumps(report, indent=2))
    return 0 if report.get("status") != "failed" else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="tbox_build",
        description=f"TBOX Build Orchestrator v{__version__}",
    )
    _add_common_args(parser)
    subparsers = parser.add_subparsers(dest="command", required=True)

    # validate
    p_validate = subparsers.add_parser("validate", help="Validate manifests")
    _add_common_args(p_validate)
    p_validate.set_defaults(func=cmd_validate)

    # build
    p_build = subparsers.add_parser("build", help="Build services")
    _add_common_args(p_build)
    p_build.add_argument("--platform", default="orin", help="Target platform (default: orin)")
    p_build.add_argument("--profile", default="release", help="Build profile (debug/release)")
    p_build.add_argument("--set", default=None, help="Release set ID")
    p_build.add_argument("--service", default=None, help="Single service ID")
    p_build.add_argument("--all", action="store_true", help="Build all services")
    p_build.add_argument("--jobs", "-j", type=int, default=1, help="Parallel jobs")
    p_build.add_argument("--clean", action="store_true", help="Clean build")
    p_build.add_argument("--dry-run", action="store_true", help="Print commands without executing")
    p_build.set_defaults(func=cmd_build)

    # package
    p_package = subparsers.add_parser("package", help="Create a release package")
    _add_common_args(p_package)
    p_package.add_argument("--platform", default="orin")
    p_package.add_argument("--profile", default="release")
    p_package.add_argument("--name", default="tbox-orin")
    p_package.add_argument("--version", default="0.1.0-alpha")
    p_package.set_defaults(func=cmd_package)

    # deploy
    p_deploy = subparsers.add_parser("deploy", help="Deploy a package")
    _add_common_args(p_deploy)
    p_deploy.add_argument("package", help="Path to the package file")
    p_deploy.add_argument("--host", default=None, help="Target device host")
    p_deploy.add_argument("--user", default="tbox", help="Target SSH user")
    p_deploy.add_argument("--dry-run", action="store_true", help="Dry-run mode")
    p_deploy.set_defaults(func=cmd_deploy)

    # verify
    p_verify = subparsers.add_parser("verify", help="Verify staging or package")
    _add_common_args(p_verify)
    p_verify.add_argument("--platform", default="orin")
    p_verify.add_argument("--profile", default="release")
    p_verify.add_argument("--package", default=None, help="Package path to verify")
    p_verify.set_defaults(func=cmd_verify)

    # elfcheck
    p_elfcheck = subparsers.add_parser("elfcheck", help="Run ELF/pollution checks")
    _add_common_args(p_elfcheck)
    p_elfcheck.add_argument("--platform", default="orin")
    p_elfcheck.add_argument("--profile", default="release")
    p_elfcheck.set_defaults(func=cmd_elfcheck)

    # sysroot
    p_sysroot = subparsers.add_parser("sysroot", help="Sysroot management")
    _add_common_args(p_sysroot)
    p_sysroot.add_argument("action", choices=["import", "verify", "fix-symlinks", "manifest"])
    p_sysroot.add_argument("--dry-run", action="store_true")
    p_sysroot.set_defaults(func=cmd_sysroot)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
