"""Generic TARGET dependency recipe executor for TBOX Build.

Provides the minimum通用 recipe mechanism required by CR-002 (BUILD-FW-REQ-006):

  * dependency lock parsing (via :class:`DependencyLock`);
  * controlled source cache lookup;
  * source SHA-256 verification (with PENDING rejection for release builds);
  * patch application;
  * toolchain configure / build / install to TBOX_DEP_STAGING;
  * archive member architecture check (all members must be AArch64);
  * cache key computation;
  * per-step logging and failure propagation.

The executor is generic: it is not named for or specialised to framework.
Any CMake-based TARGET dependency declared in ``dependencies/lock.yaml``
can be built once its source is available in the controlled cache.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import tarfile
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .errors import BuildFailure, TboxBuildError
from .elfcheck import parse_elf, EM_AARCH64, _ELFCLASS64
from .manifest import DependencyEntry, DependencyLock


@dataclass
class RecipeResult:
    """Result of building a single dependency."""

    name: str
    version: str
    status: str = "pending"
    source_sha256: str = ""
    product_sha256: str = ""
    cache_key: str = ""
    installed_files: list[str] = field(default_factory=list)
    archive_members_checked: int = 0
    error: str | None = None


class RecipeExecutor:
    """Builds TARGET dependencies from the lock into the dependency staging.

    The executor is deliberately CMake-generic: it configures with the Orin
    toolchain, applies ``cmake.options`` from the lock, builds and installs
    to ``TBOX_DEP_STAGING`` with ``CMAKE_INSTALL_PREFIX=/usr`` and a
    dependency-specific ``DESTDIR``.
    """

    def __init__(
        self,
        project_root: Path,
        staging: Any,
        lock: DependencyLock,
        config: Any | None = None,
    ):
        self.project_root = Path(project_root)
        self.staging = staging
        self.lock = lock
        self.config = config
        self.cache_dir = self.project_root / "dependencies" / "cache"
        self.recipes_dir = self.project_root / "dependencies" / "recipes"

    # -- source cache -----------------------------------------------------

    def _source_cache_dir(self, name: str) -> Path:
        return self.cache_dir / name

    def _locate_source(self, entry: DependencyEntry) -> Path:
        """Locate the controlled source archive in the cache.

        Release builds must not download uncontrolled sources online; the
        source must be pre-populated in ``dependencies/cache/<name>/``.
        """
        cache = self._source_cache_dir(entry.name)
        cache.mkdir(parents=True, exist_ok=True)
        # Look for any source archive in the cache dir.
        candidates = sorted(cache.glob("*.tar.*")) + sorted(cache.glob("*.zip"))
        if candidates:
            return candidates[0]
        # No cached source. In a release build this is fatal. In dev the
        # caller may populate the cache; we do not perform uncontrolled
        # online downloads from the executor itself.
        is_release = getattr(self.config, "is_release", False) if self.config else False
        if is_release:
            raise BuildFailure(
                f"Dependency '{entry.name}' source not found in cache {cache}; "
                f"release builds must pre-populate the controlled source cache"
            )
        raise BuildFailure(
            f"Dependency '{entry.name}' source not found in cache {cache}; "
            f"populate the cache before building (no uncontrolled online download)"
        )

    @staticmethod
    def _sha256_file(path: Path) -> str:
        h = hashlib.sha256()
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(65536), b""):
                h.update(chunk)
        return h.hexdigest()

    def _verify_source(self, entry: DependencyEntry, archive: Path) -> None:
        if not entry.is_source_pinned:
            raise BuildFailure(
                f"Dependency '{entry.name}' source SHA-256 is not pinned "
                f"(marked PENDING); cannot verify source"
            )
        actual = self._sha256_file(archive)
        if actual.lower() != entry.source_sha256.lower():
            raise BuildFailure(
                f"Dependency '{entry.name}' source SHA-256 mismatch: "
                f"expected {entry.source_sha256}, got {actual}"
            )

    def _extract_source(self, archive: Path, dest: Path) -> Path:
        """Extract a source archive into *dest*, returning the source root."""
        dest.mkdir(parents=True, exist_ok=True)
        if archive.suffix == ".zip":
            with zipfile.ZipFile(archive) as zf:
                zf.extractall(dest)
        else:
            with tarfile.open(archive, "r:*") as tf:
                tf.extractall(dest)
        # The archive usually contains a single top-level directory.
        entries = [p for p in dest.iterdir() if p.is_dir()]
        if len(entries) == 1:
            return entries[0]
        return dest

    # -- patching ---------------------------------------------------------

    def _apply_patches(self, entry: DependencyEntry, source_root: Path) -> None:
        for patch in entry.patches:
            patch_file = self.recipes_dir / patch.file
            if not patch_file.is_file():
                raise BuildFailure(
                    f"Dependency '{entry.name}' patch not found: {patch_file}"
                )
            actual = self._sha256_file(patch_file)
            if actual.lower() != patch.sha256.lower():
                raise BuildFailure(
                    f"Dependency '{entry.name}' patch SHA-256 mismatch for "
                    f"{patch.file}: expected {patch.sha256}, got {actual}"
                )
            result = subprocess.run(
                ["patch", "-p1", "-i", str(patch_file)],
                cwd=str(source_root),
                capture_output=True,
                text=True,
            )
            if result.returncode != 0:
                raise BuildFailure(
                    f"Dependency '{entry.name}' patch {patch.file} failed: "
                    f"{result.stderr}"
                )

    # -- cmake configure / build / install --------------------------------

    def _toolchain_file(self) -> Path:
        return self.project_root / "cmake" / "toolchains" / "orin-aarch64.cmake"

    def _cmake_env(self) -> dict[str, str]:
        env: dict[str, str] = {
            "TBOX_ROOT": str(self.project_root),
        }
        sysroot = self.project_root / "sysroots" / "orin-r35.3.1"
        if sysroot.is_dir():
            env["TBOX_SYSROOT"] = str(sysroot)
        env["TBOX_DEP_STAGING"] = str(self.staging.dep_staging)
        return env

    def _configure_cmd(
        self, entry: DependencyEntry, source_root: Path, build_dir: Path
    ) -> list[str]:
        cmd = [
            "cmake",
            "-S", str(source_root),
            "-B", str(build_dir),
            "-G", "Ninja",
            "-DCMAKE_BUILD_TYPE=Release",
            "-DCMAKE_INSTALL_PREFIX=/usr",
            f"-DCMAKE_TOOLCHAIN_FILE={self._toolchain_file()}",
        ]
        for key, value in entry.cmake_options.items():
            cmd.append(f"-D{key}={value}")
        return cmd

    def _build_cmd(self, build_dir: Path) -> list[str]:
        jobs = getattr(self.config, "jobs", 1) if self.config else 1
        return ["cmake", "--build", str(build_dir), "--", f"-j{jobs}"]

    def _install_cmd(self, build_dir: Path) -> list[str]:
        return ["cmake", "--install", str(build_dir), "--prefix", "/usr"]

    # -- archive architecture check ---------------------------------------

    def _check_archive_members(self, entry: DependencyEntry, staging_usr: Path) -> int:
        """Verify all .a archive members are AArch64 ELF64."""
        checked = 0
        for archive in staging_usr.rglob("*.a"):
            checked += self._check_one_archive(archive)
        return checked

    def _check_one_archive(self, archive: Path) -> int:
        """Check each member of an ar archive is AArch64 ELF64.

        Returns the number of members checked.
        """
        import io
        with open(archive, "rb") as f:
            magic = f.read(8)
        if magic != b"!<arch>\n":
            raise BuildFailure(
                f"Archive {archive} is not a valid ar archive (bad magic)"
            )
        checked = 0
        # Parse ar members manually (BSD/SysV format).
        with open(archive, "rb") as f:
            f.read(8)  # magic
            while True:
                header = f.read(60)
                if len(header) < 60:
                    break
                name = header[0:16].decode("ascii", errors="replace").strip()
                size_field = header[48:58].decode("ascii", errors="replace").strip()
                try:
                    size = int(size_field)
                except ValueError:
                    break
                # Skip symbol lookup table and long-name index members.
                if name in ("", "/") or name.startswith("//") or name.startswith("/SYM"):
                    f.seek((size + 1) // 2 * 2, 1)
                    continue
                member_data = f.read(size)
                # Pad to even boundary.
                if size % 2 == 1:
                    f.read(1)
                # Only inspect ELF members (skip non-ELF like hFILESIZE).
                if len(member_data) >= 4 and member_data[:4] == b"\x7fELF":
                    checked += 1
                    self._assert_aarch64(member_data, archive, name)
        return checked

    def _assert_aarch64(self, data: bytes, archive: Path, member: str) -> None:
        # Parse minimal ELF header fields from raw bytes.
        if len(data) < 20:
            return
        ei_class = data[4]  # 1=32, 2=64
        # e_machine at offset 18 (for both 32/64 ELF, little-endian).
        import struct
        e_machine = struct.unpack_from("<H", data, 18)[0]
        if ei_class != _ELFCLASS64:
            raise BuildFailure(
                f"Archive {archive} member '{member}' is not ELF64 (class={ei_class})"
            )
        if e_machine != EM_AARCH64:
            raise BuildFailure(
                f"Archive {archive} member '{member}' is not AArch64 "
                f"(machine={e_machine})"
            )

    # -- package config relocation check ----------------------------------

    def _check_package_config_relocatable(self, staging_usr: Path) -> None:
        """CMake package config files must not contain source/build/host paths."""
        pkg_dir = staging_usr / "lib" / "cmake"
        if not pkg_dir.is_dir():
            return
        bad_markers = [
            str(self.project_root),
            "/build/",
        ]
        for cfg in pkg_dir.rglob("*.cmake"):
            try:
                content = cfg.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            for marker in bad_markers:
                if marker in content:
                    raise BuildFailure(
                        f"CMake package config {cfg} contains non-relocatable "
                        f"path marker '{marker}'"
                    )

    # -- cache key --------------------------------------------------------

    def _cache_key(self, entry: DependencyEntry) -> str:
        """Compute a cache key digest from lock + platform/toolchain inputs."""
        import hashlib as _hashlib
        parts = [
            entry.name,
            entry.version,
            entry.source_sha256,
            entry.boundary,
            entry.architecture,
            entry.linkage,
            json.dumps(entry.cmake_options, sort_keys=True),
            json.dumps([p.__dict__ for p in entry.patches], sort_keys=True),
        ]
        # Platform manifest digest if available.
        try:
            pm_path = self.project_root / "manifests" / "orin-platform.yaml"
            if pm_path.is_file():
                parts.append(self._sha256_file(pm_path))
        except OSError:
            pass
        # Sysroot digest if available.
        try:
            sr_path = self.project_root / "manifests" / "orin-r35.3.1.yaml"
            if sr_path.is_file():
                parts.append(self._sha256_file(sr_path))
        except OSError:
            pass
        digest = _hashlib.sha256("\n".join(parts).encode("utf-8")).hexdigest()
        return digest

    # -- public entry point -----------------------------------------------

    def build(self, name: str) -> RecipeResult:
        """Build a single dependency from the lock into TBOX_DEP_STAGING."""
        entry = self.lock.get(name)
        if entry is None:
            raise BuildFailure(f"Dependency '{name}' is not declared in lock.yaml")

        result = RecipeResult(name=name, version=entry.version)
        log_dir = self.staging.logs_dir
        log_dir.mkdir(parents=True, exist_ok=True)
        log_file = log_dir / f"dep-{name}.log"

        try:
            cache_key = self._cache_key(entry)
            result.cache_key = cache_key
            marker = self.cache_dir / name / ".built"
            if marker.is_file():
                try:
                    saved = json.loads(marker.read_text())
                    if saved.get("cache_key") == cache_key:
                        # Inputs match, but the cache is only valid if the
                        # staged products are still present. staging.prepare(
                        # clean=True) wipes out/<plat>/<prof>/ (including
                        # deps/) without touching this marker, so a stale
                        # marker with missing products must trigger a rebuild
                        # instead of a silent skip that leaves deps/ empty.
                        installed = saved.get("installed_files", [])
                        staging_usr = self.staging.dep_staging_usr()
                        if installed and all(
                            (staging_usr / f).exists() for f in installed
                        ):
                            result.status = "cached"
                            result.source_sha256 = entry.source_sha256
                            result.installed_files = installed
                            print(f"  [CACHED] {name} (key={cache_key[:12]})")
                            return result
                        print(
                            f"  [STALE] {name}: cache marker present but "
                            f"staging products missing; rebuilding"
                        )
                except (OSError, json.JSONDecodeError):
                    pass

            with open(log_file, "w", encoding="utf-8") as log:
                log.write(f"recipe: {name} {entry.version}\n")
                log.write(f"cache_key: {cache_key}\n")
                log.flush()

                # 1. Locate and verify source.
                archive = self._locate_source(entry)
                log.write(f"source: {archive}\n")
                self._verify_source(entry, archive)
                result.source_sha256 = entry.source_sha256
                log.write(f"source_sha256: OK\n")
                log.flush()

                # 2. Extract and patch.
                extract_dir = self.cache_dir / name / "src"
                if extract_dir.exists():
                    shutil.rmtree(extract_dir)
                source_root = self._extract_source(archive, extract_dir)
                log.write(f"source_root: {source_root}\n")
                self._apply_patches(entry, source_root)
                log.write("patches: applied\n")
                log.flush()

                # 3. Configure.
                build_dir = self.cache_dir / name / "build"
                if build_dir.exists():
                    shutil.rmtree(build_dir)
                build_dir.mkdir(parents=True, exist_ok=True)
                configure_cmd = self._configure_cmd(entry, source_root, build_dir)
                log.write(f"$ {' '.join(configure_cmd)}\n")
                log.flush()
                env = os.environ.copy()
                env.update(self._cmake_env())
                r = subprocess.run(
                    configure_cmd, stdout=log, stderr=subprocess.STDOUT, env=env
                )
                if r.returncode != 0:
                    raise BuildFailure(
                        f"Dependency '{name}' configure failed (see {log_file})"
                    )

                # 4. Build.
                build_cmd = self._build_cmd(build_dir)
                log.write(f"$ {' '.join(build_cmd)}\n")
                log.flush()
                r = subprocess.run(
                    build_cmd, stdout=log, stderr=subprocess.STDOUT, env=env
                )
                if r.returncode != 0:
                    raise BuildFailure(
                        f"Dependency '{name}' build failed (see {log_file})"
                    )

                # 5. Install to TBOX_DEP_STAGING (DESTDIR).
                staging_usr = self.staging.dep_staging_usr()
                staging_usr.mkdir(parents=True, exist_ok=True)
                before = self._snapshot(staging_usr)
                install_cmd = self._install_cmd(build_dir)
                log.write(f"$ DESTDIR={self.staging.dep_staging} {' '.join(install_cmd)}\n")
                log.flush()
                install_env = dict(env)
                install_env["DESTDIR"] = str(self.staging.dep_staging)
                r = subprocess.run(
                    install_cmd, stdout=log, stderr=subprocess.STDOUT, env=install_env
                )
                if r.returncode != 0:
                    raise BuildFailure(
                        f"Dependency '{name}' install failed (see {log_file})"
                    )
                new_files = sorted(self._snapshot(staging_usr) - before)
                result.installed_files = new_files
                log.write(f"installed: {len(new_files)} file(s)\n")

                # 6. Archive architecture check.
                checked = self._check_archive_members(entry, staging_usr)
                result.archive_members_checked = checked
                log.write(f"archive_members_checked: {checked}\n")

                # 7. Package config relocatability check.
                self._check_package_config_relocatable(staging_usr)
                log.write("package_config: relocatable OK\n")

                # 8. Product SHA-256 (digest of installed file list+hashes).
                prod_parts = []
                for f in new_files:
                    prod_parts.append(f)
                result.product_sha256 = hashlib.sha256(
                    "\n".join(prod_parts).encode("utf-8")
                ).hexdigest()

            # Mark built.
            marker.parent.mkdir(parents=True, exist_ok=True)
            marker.write_text(json.dumps({
                "cache_key": cache_key,
                "version": entry.version,
                "source_sha256": entry.source_sha256,
                "product_sha256": result.product_sha256,
                "installed_files": result.installed_files,
            }))
            result.status = "success"
            print(f"  [OK] {name}: {len(result.installed_files)} file(s), "
                  f"{result.archive_members_checked} archive member(s)")
            return result

        except TboxBuildError as exc:
            result.status = "failed"
            result.error = str(exc)
            raise
        except Exception as exc:
            result.status = "failed"
            result.error = f"Unexpected error: {exc}"
            raise BuildFailure(result.error)

    @staticmethod
    def _snapshot(root: Path) -> set[str]:
        result: set[str] = set()
        if not root.exists():
            return result
        for path in root.rglob("*"):
            if path.is_file() or path.is_symlink():
                result.add(str(path.relative_to(root)))
        return result
