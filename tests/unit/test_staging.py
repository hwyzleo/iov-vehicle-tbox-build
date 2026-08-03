"""Unit tests for staging directory management."""

from __future__ import annotations

from pathlib import Path

import pytest

from tbox_build.staging import StagingDir, sha256_file


class TestStagingDir:
    def test_prepare_creates_dirs(self, tmp_path: Path):
        staging = StagingDir(tmp_path, "orin", "release")
        staging.prepare()
        assert staging.root.exists()
        assert staging.install_root.exists()
        assert staging.build_dir.exists()
        assert staging.manifests_dir.exists()
        assert staging.logs_dir.exists()
        assert staging.packages_dir.exists()

    def test_prepare_clean_removes_existing(self, tmp_path: Path):
        staging = StagingDir(tmp_path, "orin", "release")
        staging.prepare()
        old_file = staging.install_root / "old.txt"
        old_file.write_text("old")
        assert old_file.exists()

        staging.prepare(clean=True)
        assert not old_file.exists()
        assert staging.install_root.exists()

    def test_service_build_dir(self, tmp_path: Path):
        staging = StagingDir(tmp_path, "orin", "release")
        staging.prepare()
        build_dir = staging.service_build_dir("svc-a")
        assert build_dir == staging.build_dir / "svc-a"

    def test_service_log_file(self, tmp_path: Path):
        staging = StagingDir(tmp_path, "orin", "release")
        staging.prepare()
        log_file = staging.service_log_file("svc-a", "configure")
        assert log_file == staging.logs_dir / "svc-a-configure.log"

    def test_snapshot_empty(self, tmp_path: Path):
        staging = StagingDir(tmp_path, "orin", "release")
        staging.prepare()
        assert staging.snapshot_paths() == set()

    def test_snapshot_after_files(self, tmp_path: Path):
        staging = StagingDir(tmp_path, "orin", "release")
        staging.prepare()
        (staging.install_root / "usr").mkdir(parents=True)
        (staging.install_root / "usr" / "bin").mkdir()
        (staging.install_root / "usr" / "bin" / "app").write_text("binary")
        (staging.install_root / "etc").mkdir()
        (staging.install_root / "etc" / "config").write_text("config")

        snapshot = staging.snapshot_paths()
        assert "usr/bin/app" in snapshot
        assert "etc/config" in snapshot

    def test_new_files_after_install(self, tmp_path: Path):
        staging = StagingDir(tmp_path, "orin", "release")
        staging.prepare()
        before = staging.snapshot_paths()
        assert before == set()

        (staging.install_root / "usr").mkdir(parents=True)
        (staging.install_root / "usr" / "lib").mkdir()
        (staging.install_root / "usr" / "lib" / "libfoo.so").write_text("lib")

        new_files = staging.new_files_after_install(before, "svc-a")
        assert "usr/lib/libfoo.so" in new_files

    def test_scan_files(self, tmp_path: Path):
        staging = StagingDir(tmp_path, "orin", "release")
        staging.prepare()
        (staging.install_root / "usr").mkdir(parents=True)
        (staging.install_root / "usr" / "bin").mkdir()
        app_path = staging.install_root / "usr" / "bin" / "app"
        app_path.write_text("binary content")

        files = staging.scan_files()
        assert len(files) == 1
        assert files[0].rel_path == "rootfs/usr/bin/app"
        assert files[0].size == len("binary content")
        assert files[0].sha256 == sha256_file(app_path)

    def test_sha256_file(self, tmp_path: Path):
        path = tmp_path / "test.txt"
        path.write_text("hello world")
        digest = sha256_file(path)
        assert len(digest) == 64  # SHA-256 hex
        assert all(c in "0123456789abcdef" for c in digest)
