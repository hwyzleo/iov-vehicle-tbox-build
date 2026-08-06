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
import stat
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
    ConfigDeploymentManifest,
)
from .schema import validate_service_manifest, validate_release_set_manifest
from .staging import StagingDir, sha256_file
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
# Platform config overlay report (CR-003 §6/§9)
# ---------------------------------------------------------------------------


@dataclass
class OverlayFileEntry:
    """A single file staged by the platform config overlay."""

    source: str  # configs/<platform>/rootfs/<rel> (relative to project root)
    target_path: str  # logical on-device path, e.g. /etc/tbox/common.yaml
    rel_path: str  # relative to install-root, e.g. etc/tbox/common.yaml
    mode: int
    sha256: str
    overwrote: bool  # whether a service default was overridden
    prior_sha256: str | None = None
    prior_owner: str | None = None
    deploy_policy: str = "replace"
    category: str = "release-managed"


@dataclass
class OverlayReport:
    """Platform config overlay staging report (CR-003 §6 step 10)."""

    platform: str
    entries: list[OverlayFileEntry] = field(default_factory=list)
    removed_stale: list[str] = field(default_factory=list)
    unauthorized: list[str] = field(default_factory=list)
    status: str = "success"  # success | failed
    errors: list[str] = field(default_factory=list)
    validation_summary: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "platform": self.platform,
            "status": self.status,
            "entries": [asdict(e) for e in self.entries],
            "removed_stale": self.removed_stale,
            "unauthorized": self.unauthorized,
            "errors": self.errors,
            "validation": self.validation_summary,
        }

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
        self._overlay_report: OverlayReport | None = None

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

        # Per-service CMake cache variables (TBOX-SOMEIP-DSN-CR-006 §11.3):
        # BUILD 提供受控构建开关（e.g. USE_REAL_IPC=ON / USE_MOCK_SOMEIP=OFF），
        # 与 toolchain/sysroot 注入同等对待。
        for key, value in service.build.cmake_cache_variables.items():
            cmd.append(f"-D{key}={value}")

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
            # pkg-config reroot for staged TARGET deps (CR-006: someip consumes
            # vsomeip3 / CommonAPI / CommonAPI-SomeIP via pkg_check_modules).
            # Their installed .pc files carry prefix=/usr (DESTDIR install), so
            # includedir/libdir resolve to the absolute /usr/... which does not
            # exist on the build host. PKG_CONFIG_SYSROOT_DIR makes pkg-config
            # prepend the dep-staging root to those absolute paths, so
            # PkgConfig::COMMONAPI etc. point at <dep_staging>/usr/... . All
            # someip pkg-config modules (and their Requires) live in the dep
            # staging, so a single sysroot dir is correct here; CMake 3.16 does
            # not derive this from CMAKE_SYSROOT automatically.
            env["PKG_CONFIG_SYSROOT_DIR"] = str(self.staging.dep_staging)
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

    def _stage_runtime_shared_deps(self, lock: Any) -> list[str]:
        """Copy shared-linkage TARGET dependency runtime libs into install-root.

        Static deps (yaml-cpp, curl, mosquitto, nlohmann) are linked into the
        service binaries and need not be shipped. Shared deps (vsomeip,
        CommonAPI, CommonAPI-SomeIP) are loaded at runtime and must be present
        on the device, so their installed ``.so*`` files are copied from the
        dependency staging (``deps/usr/lib``) into the rootfs payload
        (``install-root/usr/lib``, on the default loader search path). Returns
        the list of staged relative paths.
        """
        dep_usr = self.staging.dep_staging_usr()
        install_usr = self.staging.install_root / "usr"
        staged: list[str] = []
        for name, entry in getattr(lock, "dependencies", {}).items():
            if getattr(entry, "linkage", None) != "shared":
                continue
            marker = self.project.root / "dependencies" / "cache" / name / ".built"
            installed: list[str] = []
            if marker.is_file():
                try:
                    installed = json.loads(marker.read_text()).get("installed_files", [])
                except (OSError, json.JSONDecodeError):
                    installed = []
            for rel in installed:
                # runtime shared objects only: lib/*.so* excluding cmake/pkgconfig
                if not rel.startswith("lib/"):
                    continue
                if rel.startswith("lib/cmake/") or rel.startswith("lib/pkgconfig/"):
                    continue
                base = rel.rsplit("/", 1)[-1]
                if ".so" not in base:
                    continue
                src = dep_usr / rel
                if not (src.exists() or src.is_symlink()):
                    continue
                dst = install_usr / rel
                dst.parent.mkdir(parents=True, exist_ok=True)
                if dst.exists() or dst.is_symlink():
                    dst.unlink()
                if src.is_symlink():
                    # preserve the symlink (e.g. libvsomeip3.so -> .so.3.4.10)
                    os.symlink(os.readlink(src), dst)
                else:
                    shutil.copy2(src, dst)
                staged.append("usr/" + rel)
        if staged:
            libs = sorted({s.rsplit('/', 1)[-1] for s in staged})
            print(f"  Staged {len(staged)} shared runtime lib file(s) into "
                  f"install-root: {', '.join(libs)}")
        return staged

    # -- platform device config overlay -----------------------------------

    # -- platform device config overlay (CR-003 §6) ---------------------

    _OVERLAY_REPORT_NAME = "platform-overlay-report.json"

    def _overlay_report_path(self) -> Path:
        return self.staging.manifests_dir / self._OVERLAY_REPORT_NAME

    @staticmethod
    def _is_safe_overlay_path(src: Path, rel: Path, overlay_root: Path) -> str | None:
        """Return an error string if *src* is unsafe, else None (§6 step 2).

        Rejects symlinks, absolute paths, ``..`` traversal, device/socket/fifo
        files and symlink escape outside the overlay root.
        """
        rel_str = rel.as_posix()
        if rel_str.startswith("/"):
            return f"absolute path in overlay: {src}"
        if ".." in rel.parts:
            return f"'..' traversal in overlay: {src}"
        if src.is_symlink():
            return f"symlink in overlay (forbidden): {src}"
        try:
            mode = src.lstat().st_mode
        except OSError as exc:
            return f"cannot stat overlay file: {src} ({exc})"
        if not stat.S_ISREG(mode):
            return f"non-regular file in overlay: {src}"
        # Symlink-escape guard: resolved real path must stay under overlay root.
        try:
            src.resolve().relative_to(overlay_root.resolve())
        except ValueError:
            return f"overlay path escapes root: {src}"
        return None

    def _load_previous_overlay_report(self) -> dict[str, Any] | None:
        """Load the previous build's overlay report (for stale cleanup)."""
        path = self._overlay_report_path()
        if not path.is_file():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None

    def _clean_stale_overlay(
        self, prev: dict[str, Any], overlay_root: Path, report: OverlayReport
    ) -> None:
        """Remove files placed by the previous overlay but absent now (§6 step 3)."""
        current_sources: set[str] = set()
        if overlay_root.is_dir():
            for src in overlay_root.rglob("*"):
                if src.is_file() and not src.is_symlink():
                    current_sources.add(src.relative_to(overlay_root).as_posix())
        for entry in prev.get("entries", []):
            rel_path = entry.get("rel_path", "")
            if not rel_path:
                continue
            if rel_path in current_sources:
                continue  # still present; will be re-staged
            stale = self.staging.install_root / rel_path
            if stale.is_file() or stale.is_symlink():
                stale.unlink()
                report.removed_stale.append(rel_path)

    def _find_prior_owner(
        self, dst: Path, service_results: list[ServiceResult]
    ) -> str | None:
        """Best-effort owner lookup for a pre-overlay install-root file."""
        try:
            rel = dst.relative_to(self.staging.install_root)
        except ValueError:
            return None
        rel_str = rel.as_posix()
        for result in service_results:
            if rel_str in result.installed_files:
                return result.id
        return None

    def _stage_platform_config_overlay(
        self, service_manifest: ServiceManifest,
        service_results: list[ServiceResult],
    ) -> OverlayReport:
        """Stage the platform config overlay into install-root (CR-003 §6).

        Implements the 10-step overlay algorithm: normalize/validate the
        platform root, build a safe file manifest, clean stale overlay state,
        authorize each target via config-deployment.yaml, record prior
        SHA-256/owner, write atomically with permission normalization, then
        run schema/secret/common validation and emit an overlay report.
        """
        report = OverlayReport(platform=self.config.platform)
        overlay_root = (
            self.project.root / "configs" / self.config.platform / "rootfs"
        )

        # Step 1: normalize; absent root -> empty overlay (nothing to stage)
        if not overlay_root.is_dir():
            self._overlay_report = report
            return report

        # Step 3: clean stale files from the previous overlay (incremental)
        prev = self._load_previous_overlay_report()
        if prev is not None:
            self._clean_stale_overlay(prev, overlay_root, report)

        cdm = self.project.load_config_deployment_manifest()

        # Steps 2, 4-8: validate, authorize, record, stage each file
        for src in sorted(overlay_root.rglob("*")):
            # Skip real directories (structure); everything else goes through
            # path validation, which rejects symlinks and non-regular files.
            if src.is_dir() and not src.is_symlink():
                continue
            rel = src.relative_to(overlay_root)

            # Step 2: reject unsafe paths
            err = self._is_safe_overlay_path(src, rel, overlay_root)
            if err is not None:
                report.status = "failed"
                report.errors.append(err)
                continue

            rel_str = rel.as_posix()
            target_logical = "/" + rel_str

            # Step 5: authorization -- only release-managed targets allowed
            rule = cdm.match(self.config.platform, target_logical)
            if rule is None:
                report.unauthorized.append(target_logical)
                report.status = "failed"
                report.errors.append(
                    f"overlay target not declared in config-deployment.yaml: "
                    f"{target_logical}"
                )
                continue
            if rule.category != "release-managed":
                report.unauthorized.append(target_logical)
                report.status = "failed"
                report.errors.append(
                    f"overlay target is {rule.category} (must be "
                    f"release-managed): {target_logical}"
                )
                continue

            dst = self.staging.install_root / rel

            # Step 6: record prior file (service default being overridden)
            prior_sha: str | None = None
            prior_owner: str | None = None
            overwrote = False
            if dst.is_file() and not dst.is_symlink():
                prior_sha = sha256_file(dst)
                prior_owner = self._find_prior_owner(dst, service_results)
                overwrote = True

            if self.config.dry_run:
                report.entries.append(OverlayFileEntry(
                    source=str(src.relative_to(self.project.root)),
                    target_path=target_logical,
                    rel_path=rel_str,
                    mode=0o644,
                    sha256="(dry-run)",
                    overwrote=overwrote,
                    prior_sha256=prior_sha,
                    prior_owner=prior_owner,
                    deploy_policy=rule.deploy_policy,
                    category=rule.category,
                ))
                continue

            # Step 7: temp file + permission normalization + atomic rename
            dst.parent.mkdir(parents=True, exist_ok=True)
            tmp = dst.with_name(dst.name + ".tmp")
            shutil.copy2(src, tmp)
            os.chmod(tmp, 0o644)
            if dst.exists() or dst.is_symlink():
                dst.unlink()
            os.rename(tmp, dst)

            # Step 8: record final entry
            report.entries.append(OverlayFileEntry(
                source=str(src.relative_to(self.project.root)),
                target_path=target_logical,
                rel_path=rel_str,
                mode=0o644,
                sha256=sha256_file(dst),
                overwrote=overwrote,
                prior_sha256=prior_sha,
                prior_owner=prior_owner,
                deploy_policy=rule.deploy_policy,
                category=rule.category,
            ))

        # Step 9: schema / secret / common validation + permission normalize
        if not self.config.dry_run:
            from .config_validation import ConfigValidator
            validator = ConfigValidator(
                self.project.root,
                self.staging.install_root,
                self.config.platform,
                service_manifest,
            )
            val_report = validator.validate()
            validator.normalize_permissions(val_report)
            report.validation_summary = {
                "common_ok": val_report.common_ok,
                "conf_d_ok": val_report.conf_d_ok,
                "secret_scan_ruleset": val_report.secret_scan.ruleset_version,
                "secret_findings": len(val_report.secret_scan.findings),
                "schema_checks": [
                    {"service": s.service_id, "status": s.status}
                    for s in val_report.schema_checks
                ],
                "permission_normalizations": val_report.permission_normalizations,
            }
            if not val_report.passed:
                report.status = "failed"
                report.errors.extend(val_report.errors)
                for f in val_report.secret_scan.findings:
                    report.errors.append(
                        f"secret {f.rule_id}: {f.file}"
                        + (f" field={f.field_path}" if f.field_path else "")
                    )
                for sc in val_report.schema_checks:
                    if sc.status == "fail":
                        report.errors.append(
                            f"schema check failed for '{sc.service_id}' "
                            f"({sc.target_path}): {'; '.join(sc.errors)}"
                        )

        # Step 10: save overlay report
        if not self.config.dry_run:
            report.save(self._overlay_report_path())

        staged = [e.target_path for e in report.entries]
        if staged:
            print(f"  Staged {len(staged)} {self.config.platform} platform "
                  f"config overlay file(s): {', '.join(staged)}")
        if report.removed_stale:
            print(f"  Removed {len(report.removed_stale)} stale overlay "
                  f"file(s): {', '.join(report.removed_stale)}")

        self._overlay_report = report
        return report

    # -- BUILD-owned platform assets (§8.3) -----------------------------

    _PLATFORM_ASSETS_DIR = "packaging/systemd"
    _PLATFORM_UNIT_DIR_REL = "usr/lib/systemd/system"

    def _stage_platform_assets(
        self, service_manifest: ServiceManifest
    ) -> list[str]:
        """Stage BUILD-owned platform aggregation assets into install-root.

        Copies release-set level assets that BUILD owns (e.g. ``tbox.target``)
        from ``packaging/systemd/`` into the rootfs staging so they are
        included in the release package. These assets express release-set
        composition only and do not change single-service runtime semantics
        (SPEC §8.3).

        Only units whose ``Wants=`` references are present in the built
        service set are staged; this keeps the target consistent with the
        actual release-set contents.

        Returns the list of staged relative paths.
        """
        assets_dir = self.project.root / self._PLATFORM_ASSETS_DIR
        if not assets_dir.is_dir():
            return []

        # Determine which service units were actually built in this run.
        built_units: set[str] = set()
        for svc in service_manifest:
            built_units.update(svc.runtime.systemd_units)

        staged: list[str] = []
        for src in sorted(assets_dir.iterdir()):
            if not src.is_file() or src.is_symlink():
                continue
            # Only stage .target units for now (BUILD-owned aggregation).
            # .service units are owned by individual services.
            if src.suffix != ".target":
                continue
            # Filter the unit's Wants= to only include built service units.
            content = src.read_text(encoding="utf-8")
            filtered = self._filter_target_wants(content, built_units)
            dst_dir = self.staging.install_root / self._PLATFORM_UNIT_DIR_REL
            dst_dir.mkdir(parents=True, exist_ok=True)
            dst = dst_dir / src.name
            dst.write_text(filtered, encoding="utf-8")
            os.chmod(dst, 0o644)
            rel = f"{self._PLATFORM_UNIT_DIR_REL}/{src.name}"
            staged.append(rel)
            print(f"  Staged platform asset: {rel}")
        return staged

    @staticmethod
    def _filter_target_wants(content: str, built_units: set[str]) -> str:
        """Filter a .target unit's Wants=/After= to built service units only.

        Keeps only the unit names that are present in *built_units* so the
        staged target reflects the actual release-set contents. Preserves all
        other lines (Description, Documentation, [Install], etc.).
        """
        lines = content.splitlines()
        result: list[str] = []
        skip_continuation = False
        for line in lines:
            stripped = line.strip()
            # Handle multi-line values (continuation lines after Wants=/After=)
            if skip_continuation:
                unit = stripped.rstrip()
                if unit in built_units:
                    result.append(line)
                # Stop skipping when we hit a non-indented line or empty line
                if not line.startswith(" ") and not line.startswith("\t"):
                    skip_continuation = False
                    if stripped and not stripped.startswith("#"):
                        result.append(line)
                continue
            if stripped.startswith("Wants=") or stripped.startswith("After="):
                # Check if value is on the same line or on continuation lines
                key, _, value = stripped.partition("=")
                inline_units = [u.strip() for u in value.split() if u.strip()]
                kept = [u for u in inline_units if u in built_units]
                if kept:
                    result.append(f"{key}=" + " ".join(kept))
                else:
                    result.append(f"{key}=")
                # If no inline units, the value is on continuation lines
                if not inline_units:
                    skip_continuation = True
            else:
                result.append(line)
        return "\n".join(result) + ("\n" if content.endswith("\n") else "")

    def _prepare_target_dependencies(
        self, graph: DependencyGraph, service_ids: list[str]
    ) -> list[str]:
        """Prepare TARGET dependencies (recipes) before services configure.

        Returns the list of prepared dependency names. In dry-run mode the
        recipes are not executed; in release mode PENDING source checksums
        are rejected by the recipe executor.

        Dependencies are prepared in a build-safe order (declared order within
        service topological order), not alphabetically, so TARGET deps that
        depend on other TARGET deps at configure time (e.g. commonapi-someip
        needs commonapi-core and vsomeip staged first) build correctly.
        """
        target_deps = graph.target_dependency_order(service_ids)
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
                # 6.5 Stage shared-linkage TARGET dependency runtime libraries
                # into install-root so the package is self-contained on the
                # device (the daemons resolve e.g. libvsomeip3.so / libCommonAPI*
                # at runtime; these live in the dep staging, not the device).
                self._stage_runtime_shared_deps(lock)

                # 6.6 Stage platform-specific device config overlay
                # (configs/<platform>/rootfs/**) into install-root, overriding
                # the generic per-service config templates installed above
                # (CR-003 §6). This is how Orin-specific device configs (e.g.
                # SEC mqtt TLS profile, MQTT broker host, common.yaml) get
                # packaged and deployed to the correct on-device paths
                # (/etc/tbox/...). Whole-file overlay; no YAML deep merge.
                overlay_report = self._stage_platform_config_overlay(
                    service_manifest, report.service_results
                )
                if overlay_report.status == "failed":
                    report.status = "failed"
                    report.errors.extend(overlay_report.errors)
                    # Skip artifact manifest; fall through to report save.
                else:
                    # 6.7 Stage BUILD-owned platform aggregation assets
                    # (e.g. tbox.target) into install-root so the package
                    # includes the release-set level systemd target (§8.3).
                    # These assets express release-set composition only;
                    # they do not change single-service runtime semantics.
                    self._stage_platform_assets(service_manifest)

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
                    rel_key = f"sdk/{result.id}/{path}"
                else:
                    rel_key = f"rootfs/{path}"
                file_owners[rel_key] = (
                    result.id, target_str, "0.1.0", comp_name, comp_staging,
                )

        # Overlay provenance map: rootfs/<rel_path> -> OverlayFileEntry (CR-003 §9)
        overlay_by_rel: dict[str, Any] = {}
        if self._overlay_report is not None:
            for entry in self._overlay_report.entries:
                overlay_by_rel[f"rootfs/{entry.rel_path}"] = entry
        # Schema-check status by service -> target_path
        schema_status: dict[str, str] = {}
        secret_ok = True
        if self._overlay_report is not None:
            for sc in self._overlay_report.validation_summary.get("schema_checks", []):
                schema_status[sc.get("service", "")] = sc.get("status", "skipped")
            secret_ok = self._overlay_report.validation_summary.get("secret_findings", 0) == 0

        for staged in self.staging.scan_files():
            owner_info = file_owners.get(staged.rel_path)
            if owner_info:
                svc_id, target, version, component, staging = owner_info
            else:
                svc_id, target, version, component, staging = (
                    "unknown", "unknown", "unknown", "unknown", "unknown",
                )
            staged.owner_service = svc_id
            ov = overlay_by_rel.get(staged.rel_path)
            kw: dict[str, Any] = {}
            if ov is not None:
                kw.update(
                    config_overlay_source=ov.source,
                    config_prior_sha256=ov.prior_sha256,
                    config_deploy_policy=ov.deploy_policy,
                    config_category=ov.category,
                    config_overlaid=ov.overwrote,
                    config_schema_check=schema_status.get(svc_id) if svc_id != "unknown" else None,
                    config_secret_scan="pass" if secret_ok else "fail",
                )
            manifest.add_staged_file(
                staged,
                owner_service=svc_id,
                owner_target=target,
                version=version,
                git_commit=git_commit,
                install_component=component,
                staging=staging,
                **kw,
            )

        manifest.check_conflicts()
        return manifest
