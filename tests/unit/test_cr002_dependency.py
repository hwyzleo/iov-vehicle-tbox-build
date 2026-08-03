"""Unit tests for CR-002 dependency recipe executor: cache key, SHA-256
verification, PENDING guard, archive member architecture check."""

from __future__ import annotations

import json
import struct
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from tbox_build.errors import BuildFailure
from tbox_build.manifest import DependencyEntry, DependencyLock
from tbox_build.dependency import RecipeExecutor
from tbox_build.staging import StagingDir
from tbox_build.orchestrator import BuildConfig
from tbox_build.elfcheck import check_archive_members, EM_AARCH64


def _entry(name="yaml-cpp", sha256="abc123", pinned=True):
    return DependencyEntry(
        name=name,
        version="0.8.0",
        source_url="http://x/y.tar.gz",
        source_sha256=sha256 if pinned else "PENDING-FILL-BEFORE-RELEASE",
        license="BSD-3-Clause",
        boundary="TARGET",
        architecture="aarch64",
        linkage="static",
        cmake_options={"YAML_BUILD_SHARED_LIBS": "OFF"},
    )


def _executor(tmp_path, entry, is_release=False):
    project_root = tmp_path
    (project_root / "dependencies" / "cache").mkdir(parents=True, exist_ok=True)
    lock = DependencyLock(dependencies={entry.name: entry})
    staging = MagicMock()
    staging.dep_staging = project_root / "out" / "orin" / "release" / "deps"
    staging.dep_staging_usr.return_value = project_root / "out" / "orin" / "release" / "deps" / "usr"
    staging.logs_dir = project_root / "out" / "orin" / "release" / "logs"
    config = MagicMock()
    config.is_release = is_release
    config.jobs = 1
    return RecipeExecutor(project_root, staging, lock, config)


class TestCacheKey:
    def test_cache_key_deterministic(self, tmp_path):
        entry = _entry()
        ex = _executor(tmp_path, entry)
        key1 = ex._cache_key(entry)
        key2 = ex._cache_key(entry)
        assert key1 == key2
        assert len(key1) == 64

    def test_cache_key_changes_with_version(self, tmp_path):
        ex = _executor(tmp_path, _entry())
        e1 = _entry(sha256="aaa")
        e2 = _entry(sha256="bbb")
        assert ex._cache_key(e1) != ex._cache_key(e2)


class TestSourceVerification:
    def test_pending_sha256_rejected(self, tmp_path):
        entry = _entry(pinned=False)
        ex = _executor(tmp_path, entry)
        with pytest.raises(BuildFailure, match="not pinned"):
            ex._verify_source(entry, tmp_path / "fake.tar.gz")

    def test_sha256_mismatch_rejected(self, tmp_path):
        entry = _entry(sha256="deadbeef")
        ex = _executor(tmp_path, entry)
        archive = tmp_path / "src.tar.gz"
        archive.write_bytes(b"not the right content")
        with pytest.raises(BuildFailure, match="SHA-256 mismatch"):
            ex._verify_source(entry, archive)

    def test_sha256_match_accepted(self, tmp_path):
        import hashlib
        content = b"correct source content"
        entry = _entry(sha256=hashlib.sha256(content).hexdigest())
        ex = _executor(tmp_path, entry)
        archive = tmp_path / "src.tar.gz"
        archive.write_bytes(content)
        ex._verify_source(entry, archive)  # no raise


class TestSourceCache:
    def test_missing_source_release_rejected(self, tmp_path):
        entry = _entry()
        ex = _executor(tmp_path, entry, is_release=True)
        with pytest.raises(BuildFailure, match="not found in cache"):
            ex._locate_source(entry)

    def test_missing_source_dev_rejected(self, tmp_path):
        entry = _entry()
        ex = _executor(tmp_path, entry, is_release=False)
        with pytest.raises(BuildFailure, match="not found in cache"):
            ex._locate_source(entry)

    def test_cached_source_located(self, tmp_path):
        entry = _entry()
        ex = _executor(tmp_path, entry)
        cache = tmp_path / "dependencies" / "cache" / "yaml-cpp"
        cache.mkdir(parents=True)
        archive = cache / "yaml-cpp-0.8.0.tar.gz"
        archive.write_bytes(b"x")
        found = ex._locate_source(entry)
        assert found == archive


class TestArchiveMemberCheck:
    @staticmethod
    def _make_elf(machine: int) -> bytes:
        """Build a minimal 64-bit LE ELF header with the given e_machine."""
        e_ident = b"\x7fELF" + bytes([2, 1, 1, 0]) + b"\x00" * 8
        header = e_ident
        header += struct.pack("<H", 1)          # e_type = ET_REL
        header += struct.pack("<H", machine)    # e_machine
        header += struct.pack("<I", 1)          # e_version
        header += b"\x00" * (64 - len(header))  # pad to 64 bytes
        return header

    @staticmethod
    def _make_ar(members: list[tuple[str, bytes]]) -> bytes:
        """Build an ar archive from (name, data) member pairs."""
        out = b"!<arch>\n"
        for name, data in members:
            name_field = (name + "/")[:16].ljust(16)
            header = name_field.encode()
            header += b"0           "     # mtime
            header += b"0     "            # uid
            header += b"0     "            # gid
            header += b"100644  "          # mode
            header += str(len(data)).encode().ljust(10)  # size
            header += b"`\n"               # end marker
            out += header + data
            if len(data) % 2 == 1:
                out += b"\n"
        return out

    def test_aarch64_archive_accepted(self, tmp_path):
        archive = tmp_path / "libfoo.a"
        archive.write_bytes(self._make_ar([("foo.o", self._make_elf(EM_AARCH64))]))
        checked, violations = check_archive_members(archive)
        assert checked == 1
        assert violations == []

    def test_x86_member_rejected(self, tmp_path):
        archive = tmp_path / "libfoo.a"
        archive.write_bytes(self._make_ar([("foo.o", self._make_elf(62))]))
        checked, violations = check_archive_members(archive)
        assert checked == 1
        assert any("not AArch64" in v or "62" in v for v in violations)

    def test_mixed_archive_rejected(self, tmp_path):
        archive = tmp_path / "libfoo.a"
        archive.write_bytes(self._make_ar([
            ("good.o", self._make_elf(EM_AARCH64)),
            ("bad.o", self._make_elf(62)),
        ]))
        checked, violations = check_archive_members(archive)
        assert checked == 2
        assert len(violations) >= 1


class TestRecipeCacheInvalidation:
    """Recipe cache must be invalidated when staged products are missing.

    Regression: ``staging.prepare(clean=True)`` wipes out/<plat>/<prof>/
    (including deps/) but leaves ``dependencies/cache/<name>/.built``, so the
    recipe wrongly reported CACHED and skipped rebuilding, leaving deps/ empty
    and breaking downstream find_package().
    """

    @staticmethod
    def _real_executor(tmp_path: Path, entry: DependencyEntry):
        """Executor backed by a real StagingDir (real logs_dir / dep_staging)."""
        (tmp_path / "dependencies" / "cache").mkdir(parents=True, exist_ok=True)
        lock = DependencyLock(dependencies={entry.name: entry})
        staging = StagingDir(tmp_path, "orin", "debug")
        staging.prepare()
        config = BuildConfig(platform="orin", profile="debug", dry_run=False)
        return RecipeExecutor(tmp_path, staging, lock, config)

    @staticmethod
    def _write_marker(
        executor: RecipeExecutor,
        name: str,
        cache_key: str,
        installed_files: list[str],
        *,
        legacy: bool = False,
    ):
        marker = executor.cache_dir / name / ".built"
        marker.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "cache_key": cache_key,
            "version": "0.8.0",
            "source_sha256": "abc123",
            "product_sha256": "fake",
        }
        if not legacy:
            data["installed_files"] = installed_files
        marker.write_text(json.dumps(data))

    def test_cached_when_products_present(self, tmp_path: Path):
        entry = _entry()
        ex = self._real_executor(tmp_path, entry)
        cache_key = ex._cache_key(entry)
        rel = "lib/libyaml-cpp.a"
        # Create the staged product the marker claims was installed.
        staging_usr = ex.staging.dep_staging_usr()
        (staging_usr / rel).parent.mkdir(parents=True, exist_ok=True)
        (staging_usr / rel).write_bytes(b"data")
        self._write_marker(ex, "yaml-cpp", cache_key, [rel])

        result = ex.build("yaml-cpp")

        assert result.status == "cached"
        assert result.installed_files == [rel]
        assert result.source_sha256 == entry.source_sha256

    def test_rebuilds_when_products_missing(self, tmp_path: Path):
        """A stale marker with missing products must NOT short-circuit to
        CACHED. With no source archive available, the fall-through rebuild
        surfaces as a BuildFailure from _locate_source -- proving the cache
        was not trusted. (Buggy code returns "cached" and never raises.)"""
        entry = _entry()
        ex = self._real_executor(tmp_path, entry)
        cache_key = ex._cache_key(entry)
        rel = "lib/libyaml-cpp.a"
        # Marker claims built, but NO product on disk (simulates --clean).
        self._write_marker(ex, "yaml-cpp", cache_key, [rel])

        with pytest.raises(BuildFailure, match="not found in cache"):
            ex.build("yaml-cpp")

    def test_legacy_marker_without_installed_files_is_stale(self, tmp_path: Path):
        """Backward compat: a pre-fix marker lacking installed_files cannot
        be validated, so it is treated as stale and rebuilt (one-time)."""
        entry = _entry()
        ex = self._real_executor(tmp_path, entry)
        cache_key = ex._cache_key(entry)
        rel = "lib/libyaml-cpp.a"
        # Product present, but marker is legacy format (no installed_files).
        staging_usr = ex.staging.dep_staging_usr()
        (staging_usr / rel).parent.mkdir(parents=True, exist_ok=True)
        (staging_usr / rel).write_bytes(b"data")
        self._write_marker(ex, "yaml-cpp", cache_key, [rel], legacy=True)

        with pytest.raises(BuildFailure, match="not found in cache"):
            ex.build("yaml-cpp")
