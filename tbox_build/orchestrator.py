"""Build orchestration for TBOX Build.

Ties together manifest loading, validation, dependency-graph ordering,
TARGET dependency recipes, CMake configure/build/install (multi-target,
multi-component with SDK/rootfs staging), ELF checking and artifact
manifest generation into a single pipeline.

Usage::

    orch = BuildOrchestrator(project, platform="orin", profile="release")
    report = orch.build(set_id="tbox-framework-orin")
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
from .elfcheck import check_staging, ElfCheckResult
from .errors import BuildFailure, TboxBuildError
from .graph import DependencyGraph
from .manifest import (
    Project,
    Service,
    ServiceManifest,
    ReleaseSetManifest,
    InstallComponent,
)
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
    skip_recipes: bool = False

    @property
    def build_type(self) -> str:
        return "Debug" if "debug" in self.profile.lower() else "Release"

    @property
    def is_orin(self) -> bool:
        return self.platform == "orin"

    @property
    def generator(self) -> str:
        return "Ninja" if self.is_orin else "Unix Makefiles"

    @property
    def is_release(self) -> bool:
        return "release" in self.profile.lower()


@dataclass
class ComponentInstallResult:
    """Install result for a single install component."""

    name: str
    staging: str
    destdir: str
    status: str = "pending"
    installed_files: list[str] = field(default_factory=list)


@dataclass
class ServiceResult:
    """Build result for a single service."""

    id: str
    status: str = "pending"  # pending, success, failed, skipped
    steps: dict[str, str] = field(default_factory=dict)  # step -> status
    targets: list[str] = field(default_factory=list)
    components: list[ComponentInstallResult] = field(default_factory=list)
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
    target_dependencies: list[str] = field(default_factory=list)
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
        from .manifest import load_yaml

        svc_raw = load_yaml(self.project.service_manifest_path)
        rs_raw = load_yaml(self.project.release_set_manifest_path)
        lock_raw = load_yaml(self.project.dependency_lock_path)

        validate_service_manifest(svc_raw, self.project.root)
        validate_release_set_manifest(rs_raw, self.project.root)
        from .schema import validate_dependency_lock
        validate_dependency_lock(lock_raw, self.project.root)

        service_manifest = self.project.load_service_manifest()
        release_set_manifest = self.project.load_release_set_manifest()
        lock = self.project.load_dependency_lock()

        # Cross-reference and filesystem validations
        validate_all(service_manifest, self.project.root, lock)

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
        """Build all declared targets in one cmake --build invocation."""
        build_dir = self.staging.service_build_dir(service.id)
        cmd = [
            "cmake", "--build", str(build_dir),
            "--target", *service.build.targets,
        ]
        if self.config.generator == "Ninja":
            cmd.extend(["--", f"-j{self.config.jobs}"])
        elif self.config.generator == "Unix Makefiles":
            cmd.extend(["--", f"-j{self.config.jobs}"])
        return cmd

    def _install_cmds(self, service: Service) -> list[tuple[InstallComponent, list[str], Path]]:
        """Return [(component, cmd, destdir)] for each install component."""
        build_dir = self.staging.service_build_dir(service.id)
        result: list[tuple[InstallComponent, list[str], Path]] = []
        for component in service.build.install_components:
            destdir = self.staging.component_destdir(service.id, component.staging)
            cmd = [
                "cmake", "--install", str(build_dir),
                "--prefix", "/usr",
                "--component", component.name,
            ]
            result.append((component, cmd, destdir))
        return result

    def _cmake_env(self, service: Service | None = None) -> dict[str, str]:
        """Return environment variables required by CMake and the toolchain.

        TBOX_SYSROOT must be an env var (not just a cache var) so that
        CMake's try_compile sub-processes can still find the sysroot.
        TBOX_DEP_STAGING points at the TARGET dependency staging prefix.
        TBOX_SDK_STAGING points at an upstream service SDK staging prefix
        (empty/absent when building a leaf library like framework).
        """
        env: dict[str, str] = {
            "TBOX_ROOT": str(self.project.root),
        }
        if self.config.is_orin:
            env["TBOX_SYSROOT"] = str(self.project.sysroot_path)
            env["TBOX_DEP_STAGING"] = str(self.staging.dep_staging)
        # SDK staging: inject ALL upstream service dependency SDK directories
        # as a ':'-separated list (TBOX-MQTT-DSN-CR-011 §6.1). The toolchain
        # processes TBOX_SDK_STAGING_DIRS and prepends each <dir>/usr to
        # CMAKE_FIND_ROOT_PATH / CMAKE_PREFIX_PATH. We keep TBOX_SDK_STAGING
        # (first dep) for backward compatibility.
        if service is not None and service.build.service_dependencies:
            first_dep = service.build.service_dependencies[0]
            sdk = self.staging.sdk_dir(first_dep)
            env["TBOX_SDK_STAGING"] = str(sdk)
            sdk_dirs = [str(self.staging.sdk_dir(d)) for d in service.build.service_dependencies]
            # 下游优先：声明顺序为 SEC→PROV→framework（第一个 dep 是 framework，
            # 最后一个 dep 是 MQTT 的最下游上游），将列表反转以匹配设计顺序。
            sdk_dirs.reverse()
            env["TBOX_SDK_STAGING_DIRS"] = ":".join(sdk_dirs)
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

    # -- recipe pre-staging -----------------------------------------------

    def _prepare_target_dependencies(
        self, graph: DependencyGraph, service_ids: list[str]
    ) -> list[str]:
        """Prepare TARGET dependencies (recipes) before services configure.

        Returns the list of prepared dependency names. In dry-run mode the
        recipes are not executed; in release mode PENDING source checksums
        are rejected by the recipe executor.
        """
        target_deps = sorted(graph.target_dependency_set(service_ids))
        if not target_deps:
            return []

        lock = self.project.load_dependency_lock()
        print(f"=== Preparing TARGET dependencies: {target_deps} ===")

        from .dependency import RecipeExecutor

        executor = RecipeExecutor(
            project_root=self.project.root,
            staging=self.staging,
            lock=lock,
            config=self.config,
        )
        for name in target_deps:
            entry = lock.get(name)
            if entry is None:
                raise BuildFailure(
                    f"Target dependency '{name}' is not declared in lock.yaml"
                )
            if self.config.is_release and not entry.is_source_pinned:
                raise BuildFailure(
                    f"Target dependency '{name}' source SHA-256 is not pinned "
                    f"(marked PENDING); release builds require a filled checksum"
                )
            if self.config.dry_run or self.config.skip_recipes:
                print(f"  [DRY-RUN/SKIP] recipe {name}")
                continue
            print(f"  recipe: {name} ({entry.version})")
            executor.build(name)
        return target_deps

    # -- per-service build ------------------------------------------------

    def _build_service(
        self,
        service: Service,
        service_manifest: ServiceManifest,
        git_commit: str,
    ) -> ServiceResult:
        """Configure, build and install a single service."""
        result = ServiceResult(id=service.id, targets=list(service.build.targets))
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
            env=self._cmake_env(service),
        )
        result.steps["configure"] = status
        if status == "failed":
            result.status = "failed"
            result.error = f"Configure failed, see {self.staging.service_log_file(service.id, 'configure')}"
            result.duration_seconds = time.time() - start
            return result

        # 2. Build (all targets)
        status = self._run_step(
            self._build_cmd(service), service, "build",
            env=self._cmake_env(service),
        )
        result.steps["build"] = status
        if status == "failed":
            result.status = "failed"
            result.error = f"Build failed, see {self.staging.service_log_file(service.id, 'build')}"
            result.duration_seconds = time.time() - start
            return result

        # 3. Install each component (DESTDIR computed from staging class)
        base_env = self._cmake_env(service)
        all_installed: list[str] = []
        for component, cmd, destdir in self._install_cmds(service):
            comp_result = ComponentInstallResult(
                name=component.name,
                staging=component.staging,
                destdir=str(destdir),
            )
            destdir.mkdir(parents=True, exist_ok=True)
            before = self._snapshot_under(destdir)
            env = dict(base_env)
            env["DESTDIR"] = str(destdir)
            step_name = f"install:{component.name}"
            status = self._run_step(cmd, service, step_name, env=env)
            result.steps[step_name] = status
            comp_result.status = status
            if status == "failed":
                result.status = "failed"
                result.error = (
                    f"Install component '{component.name}' failed, see "
                    f"{self.staging.service_log_file(service.id, step_name)}"
                )
                result.components.append(comp_result)
                result.duration_seconds = time.time() - start
                return result
            if not self.config.dry_run:
                new_files = self._new_files_under(destdir, before)
                comp_result.installed_files = sorted(new_files)
                all_installed.extend(new_files)
            result.components.append(comp_result)

        result.installed_files = sorted(all_installed)
        result.status = "success"
        result.duration_seconds = time.time() - start
        return result

    def _snapshot_under(self, root: Path) -> set[str]:
        result: set[str] = set()
        if not root.exists():
            return result
        for path in root.rglob("*"):
            if path.is_file() or path.is_symlink():
                result.add(str(path.relative_to(root)))
        return result

    def _new_files_under(self, root: Path, before: set[str]) -> list[str]:
        after = self._snapshot_under(root)
        return sorted(after - before)

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
            lock = self.project.load_dependency_lock()
            graph = DependencyGraph(service_manifest, lock)
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

            # 4. Prepare TARGET dependencies (recipes) before any configure
            report.target_dependencies = self._prepare_target_dependencies(
                graph, report.services_requested
            )

            # 5. Build each service
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

            # 6. ELF / pollution check (only if all builds succeeded)
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

            # 7. Artifact manifest (only if all checks passed)
            if report.status != "failed" and not self.config.dry_run:
                print("\n=== Generating artifact manifest ===")
                artifact_manifest = self._generate_artifact_manifest(
                    service_manifest, lock, git_commit, report.service_results
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
        lock,
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
            dependency_lock=lock,
        )

        # Build a map of logical rel path -> (service_id, target, version, component, staging)
        file_owners: dict[str, tuple[str, str, str, str, str]] = {}
        for result in service_results:
            svc = service_manifest.get(result.id)
            if svc is None:
                continue
            target_str = ",".join(svc.build.targets)
            comp_by_file: dict[str, ComponentInstallResult] = {}
            for comp in result.components:
                for f in comp.installed_files:
                    comp_by_file[f] = comp
            for path in result.installed_files:
                comp = comp_by_file.get(path)
                comp_name = comp.name if comp else "unknown"
                comp_staging = comp.staging if comp else "rootfs"
                if comp_staging == "sdk":
                    # SDK components install into a per-service DESTDIR
                    # (sdk/<service_id>), so ``path`` is relative to that
                    # subdir. scan_files() walks the shared sdk_root (sdk/)
                    # and emits rel paths like "<service_id>/<path>"; include
                    # the service_id segment so the owner lookup key matches.
                    rel_key = f"sdk/{result.id}/{path}"
                else:
                    rel_key = f"rootfs/{path}"
                file_owners[rel_key] = (
                    result.id, target_str, "0.1.0", comp_name, comp_staging,
                )

        for staged in self.staging.scan_files():
            owner_info = file_owners.get(staged.rel_path)
            if owner_info:
                svc_id, target, version, component, staging = owner_info
            else:
                svc_id, target, version, component, staging = (
                    "unknown", "unknown", "unknown", "unknown", "unknown",
                )
            staged.owner_service = svc_id
            manifest.add_staged_file(
                staged,
                owner_service=svc_id,
                owner_target=target,
                version=version,
                git_commit=git_commit,
                install_component=component,
                staging=staging,
            )

        manifest.check_conflicts()
        return manifest
