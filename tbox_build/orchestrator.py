"""Build orchestration for TBOX Build.

Ties together manifest loading, validation, dependency-graph ordering,
CMake configure/build/install, staging, ELF checking and artifact
manifest generation into a single pipeline.

Usage::

    orch = BuildOrchestrator(project, platform="orin", profile="release")
    report = orch.build(set_id="tbox-orin-minimal")
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .artifact import ArtifactManifest, get_git_commit
from .elfcheck import check_staging, assert_clean, ElfCheckResult
from .errors import BuildFailure, TboxBuildError
from .graph import DependencyGraph
from .manifest import Project, Service, ServiceManifest, ReleaseSetManifest
from .schema import validate_service_manifest, validate_release_set_manifest
from .staging import StagingDir
from .validator import validate_all


# ---------------------------------------------------------------------------
# Build configuration and report
# ---------------------------------------------------------------------------


@dataclass
class BuildConfig:
    """Build configuration parameters."""

    platform: str = "orin"
    profile: str = "release"
    jobs: int = 1
    clean: bool = False
    dry_run: bool = False

    @property
    def build_type(self) -> str:
        return "Debug" if "debug" in self.profile.lower() else "Release"

    @property
    def is_orin(self) -> bool:
        return self.platform == "orin"

    @property
    def generator(self) -> str:
        return "Ninja" if self.is_orin else "Unix Makefiles"


@dataclass
class ServiceResult:
    """Build result for a single service."""

    id: str
    status: str = "pending"  # pending, success, failed, skipped
    steps: dict[str, str] = field(default_factory=dict)  # step -> status
    installed_files: list[str] = field(default_factory=list)
    duration_seconds: float = 0.0
    error: str | None = None


@dataclass
class BuildReport:
    """Machine-readable build report."""

    version: str = "0.1.0"
    platform: str = ""
    profile: str = ""
    release_set: str | None = None
    services_requested: list[str] = field(default_factory=list)
    start_time: str = ""
    end_time: str = ""
    duration_seconds: float = 0.0
    status: str = "pending"  # pending, success, failed
    service_results: list[ServiceResult] = field(default_factory=list)
    artifact_manifest_path: str | None = None
    elf_check_violations: int = 0
    elf_check_warnings: int = 0
    errors: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), indent=2)

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(self.to_json())


# ---------------------------------------------------------------------------
# Build orchestrator
# ---------------------------------------------------------------------------


class BuildOrchestrator:
    """Orchestrates the full TBOX build pipeline."""

    def __init__(self, project: Project, config: BuildConfig | None = None):
        self.project = project
        self.config = config or BuildConfig()
        self.staging = StagingDir(project.root, self.config.platform, self.config.profile)

    # -- manifest loading and validation ----------------------------------

    def load_and_validate(self) -> tuple[ServiceManifest, ReleaseSetManifest]:
        """Load all manifests and run pre-build validation."""
        # Load raw YAML for schema validation
        from .manifest import load_yaml

        svc_raw = load_yaml(self.project.service_manifest_path)
        rs_raw = load_yaml(self.project.release_set_manifest_path)

        validate_service_manifest(svc_raw, self.project.root)
        validate_release_set_manifest(rs_raw, self.project.root)

        service_manifest = self.project.load_service_manifest()
        release_set_manifest = self.project.load_release_set_manifest()

        # Cross-reference and filesystem validations
        validate_all(service_manifest, self.project.root)

        return service_manifest, release_set_manifest

    # -- CMake command builders -------------------------------------------

    def _configure_cmd(self, service: Service) -> list[str]:
        source_dir = self.project.root / service.effective_source_dir
        build_dir = self.staging.service_build_dir(service.id)
        build_dir.parent.mkdir(parents=True, exist_ok=True)

        cmd = [
            "cmake",
            "-S", str(source_dir),
            "-B", str(build_dir),
            "-G", self.config.generator,
            f"-DCMAKE_BUILD_TYPE={self.config.build_type}",
            "-DCMAKE_INSTALL_PREFIX=/usr",
            # GNUInstallDirs on some CMake versions does not expand sysconfdir
            # to /etc when prefix is /usr; set it explicitly.
            "-DCMAKE_INSTALL_SYSCONFDIR=/etc",
            "-DCMAKE_INSTALL_LOCALSTATEDIR=/var",
            f"-DTBOX_ROOT={self.project.root}",
        ]

        if self.config.is_orin:
            toolchain = self.project.cmake_dir / "toolchains" / "orin-aarch64.cmake"
            cmd.append(f"-DCMAKE_TOOLCHAIN_FILE={toolchain}")
            cmd.append(f"-DTBOX_SYSROOT={self.project.sysroot_path}")

        return cmd

    def _build_cmd(self, service: Service) -> list[str]:
        build_dir = self.staging.service_build_dir(service.id)
        cmd = [
            "cmake", "--build", str(build_dir),
            "--target", service.build.target,
        ]
        if self.config.generator == "Ninja":
            cmd.extend(["--", f"-j{self.config.jobs}"])
        elif self.config.generator == "Unix Makefiles":
            cmd.extend(["--", f"-j{self.config.jobs}"])
        return cmd

    def _install_cmd(self, service: Service) -> list[str]:
        build_dir = self.staging.service_build_dir(service.id)
        cmd = ["cmake", "--install", str(build_dir)]
        if service.build.install_component:
            cmd.extend(["--component", service.build.install_component])
        return cmd

    def _cmake_env(self) -> dict[str, str]:
        """Return environment variables required by CMake and the toolchain.

        TBOX_SYSROOT must be an env var (not just a cache var) so that
        CMake's try_compile sub-processes can still find the sysroot.
        """
        env: dict[str, str] = {
            "TBOX_ROOT": str(self.project.root),
        }
        if self.config.is_orin:
            env["TBOX_SYSROOT"] = str(self.project.sysroot_path)
        return env

    # -- subprocess execution ---------------------------------------------

    def _run(
        self,
        cmd: list[str],
        service_id: str,
        step: str,
        env: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess:
        """Run a command, capturing output to per-service log files."""
        log_file = self.staging.service_log_file(service_id, step)
        log_file.parent.mkdir(parents=True, exist_ok=True)

        full_env = os.environ.copy()
        if env:
            full_env.update(env)

        with open(log_file, "w", encoding="utf-8") as log:
            log.write(f"$ {' '.join(cmd)}\n")
            log.write(f"env: DESTDIR={full_env.get('DESTDIR', '(unset)')}\n")
            log.write("---\n")
            log.flush()
            result = subprocess.run(
                cmd,
                stdout=log,
                stderr=subprocess.STDOUT,
                env=full_env,
                cwd=str(self.project.root),
            )
        return result

    def _run_step(
        self,
        cmd: list[str],
        service: Service,
        step: str,
        env: dict[str, str] | None = None,
    ) -> str:
        """Run a step and return status string."""
        if self.config.dry_run:
            print(f"  [DRY-RUN] {step}: {' '.join(cmd)}")
            return "skipped"
        print(f"  {step}: {' '.join(cmd[:3])}...")
        result = self._run(cmd, service.id, step, env=env)
        if result.returncode != 0:
            return "failed"
        return "success"

    # -- per-service build ------------------------------------------------

    def _build_service(
        self,
        service: Service,
        service_manifest: ServiceManifest,
        git_commit: str,
    ) -> ServiceResult:
        """Configure, build and install a single service."""
        result = ServiceResult(id=service.id)
        start = time.time()
        print(f"\n[{service.id}]")

        # Clean build dir if requested
        if self.config.clean:
            build_dir = self.staging.service_build_dir(service.id)
            if build_dir.exists() and not self.config.dry_run:
                shutil.rmtree(build_dir)

        # 1. Configure
        status = self._run_step(
            self._configure_cmd(service), service, "configure",
            env=self._cmake_env(),
        )
        result.steps["configure"] = status
        if status == "failed":
            result.status = "failed"
            result.error = f"Configure failed, see {self.staging.service_log_file(service.id, 'configure')}"
            result.duration_seconds = time.time() - start
            return result

        # 2. Build
        status = self._run_step(
            self._build_cmd(service), service, "build",
            env=self._cmake_env(),
        )
        result.steps["build"] = status
        if status == "failed":
            result.status = "failed"
            result.error = f"Build failed, see {self.staging.service_log_file(service.id, 'build')}"
            result.duration_seconds = time.time() - start
            return result

        # 3. Install (with DESTDIR staging)
        before = self.staging.snapshot_paths()
        env = self._cmake_env()
        env["DESTDIR"] = str(self.staging.install_root)
        status = self._run_step(
            self._install_cmd(service), service, "install", env=env
        )
        result.steps["install"] = status
        if status == "failed":
            result.status = "failed"
            result.error = f"Install failed, see {self.staging.service_log_file(service.id, 'install')}"
            result.duration_seconds = time.time() - start
            return result

        # Track installed files
        if not self.config.dry_run:
            new_files = self.staging.new_files_after_install(before, service.id)
            result.installed_files = sorted(new_files)

        result.status = "success"
        result.duration_seconds = time.time() - start
        return result

    # -- full pipeline ----------------------------------------------------

    def build(
        self,
        set_id: str | None = None,
        service_id: str | None = None,
    ) -> BuildReport:
        """Run the full build pipeline.

        Either *set_id* (release set) or *service_id* (single service)
        must be provided.  If neither is given, all services are built.
        """
        report = BuildReport(
            platform=self.config.platform,
            profile=self.config.profile,
            release_set=set_id,
            start_time=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        )
        pipeline_start = time.time()

        try:
            # 1. Load and validate manifests
            print("=== Loading and validating manifests ===")
            service_manifest, release_set_manifest = self.load_and_validate()
            print(f"  Loaded {len(service_manifest)} service(s), "
                  f"{len(release_set_manifest)} release set(s)")

            # 2. Determine build order
            graph = DependencyGraph(service_manifest)
            if service_id:
                report.services_requested = [service_id]
                order = graph.build_order([service_id])
            elif set_id:
                rs = release_set_manifest.get(set_id)
                if rs is None:
                    raise BuildFailure(f"Release set '{set_id}' not found")
                report.services_requested = list(rs.services)
                order = graph.build_order(rs.services)
            else:
                report.services_requested = graph.all_service_ids()
                order = graph.build_order()
            print(f"  Build order: {' -> '.join(order)}")

            # 3. Prepare staging
            print("=== Preparing staging ===")
            self.staging.prepare(clean=self.config.clean)
            print(f"  Staging: {self.staging.root}")

            # 4. Build each service
            git_commit = get_git_commit(self.project.root)
            for sid in order:
                service = service_manifest.get(sid)
                if service is None:
                    raise BuildFailure(f"Service '{sid}' not found in manifest")
                result = self._build_service(service, service_manifest, git_commit)
                report.service_results.append(result)
                if result.status == "failed":
                    report.status = "failed"
                    report.errors.append(
                        f"Service '{sid}' failed: {result.error}"
                    )
                    # Stop on first failure
                    break

            # 5. ELF / pollution check (only if all builds succeeded)
            if report.status != "failed" and not self.config.dry_run:
                print("\n=== ELF / pollution check ===")
                elf_results = check_staging(self.staging.install_root)
                violations = sum(len(r.violations) for r in elf_results)
                warnings = sum(len(r.warnings) for r in elf_results)
                report.elf_check_violations = violations
                report.elf_check_warnings = warnings
                if violations > 0:
                    report.status = "failed"
                    for r in elf_results:
                        for v in r.violations:
                            report.errors.append(v)
                else:
                    print(f"  {len(elf_results)} file(s) checked, "
                          f"{violations} violation(s), {warnings} warning(s)")

            # 6. Artifact manifest (only if all checks passed)
            if report.status != "failed" and not self.config.dry_run:
                print("\n=== Generating artifact manifest ===")
                artifact_manifest = self._generate_artifact_manifest(
                    service_manifest, git_commit, report.service_results
                )
                manifest_path = self.staging.manifests_dir / "artifact-manifest.json"
                artifact_manifest.save(manifest_path)
                report.artifact_manifest_path = str(
                    manifest_path.relative_to(self.project.root)
                )
                print(f"  {len(artifact_manifest.entries)} artifact(s) -> {manifest_path}")

            if report.status != "failed":
                report.status = "success"

        except TboxBuildError as exc:
            report.status = "failed"
            report.errors.append(str(exc))
        except Exception as exc:
            report.status = "failed"
            report.errors.append(f"Unexpected error: {exc}")

        report.end_time = datetime.now(timezone.utc).isoformat(timespec="seconds")
        report.duration_seconds = time.time() - pipeline_start

        # Save report
        report_path = self.staging.manifests_dir / "build-report.json"
        if self.staging.manifests_dir.exists() or not self.config.dry_run:
            report.save(report_path)

        return report

    def _generate_artifact_manifest(
        self,
        service_manifest: ServiceManifest,
        git_commit: str,
        service_results: list[ServiceResult],
    ) -> ArtifactManifest:
        """Generate the artifact manifest from staging files."""
        platform_manifest = self.project.load_platform_manifest()
        sysroot_manifest = self.project.load_sysroot_manifest()

        manifest = ArtifactManifest(
            platform=self.config.platform,
            profile=self.config.profile,
            platform_manifest=platform_manifest,
            sysroot_manifest=sysroot_manifest,
        )

        # Build a map of path -> (service_id, target, version)
        file_owners: dict[str, tuple[str, str, str]] = {}
        for result in service_results:
            for path in result.installed_files:
                svc = service_manifest.get(result.id)
                if svc:
                    file_owners[path] = (
                        result.id,
                        svc.build.target,
                        "0.1.0",  # TODO: read from CMake project version
                    )

        for staged in self.staging.scan_files():
            owner_info = file_owners.get(staged.rel_path)
            if owner_info:
                svc_id, target, version = owner_info
            else:
                svc_id, target, version = "unknown", "unknown", "unknown"
            staged.owner_service = svc_id
            manifest.add_staged_file(
                staged,
                owner_service=svc_id,
                owner_target=target,
                version=version,
                git_commit=git_commit,
            )

        manifest.check_conflicts()
        return manifest
