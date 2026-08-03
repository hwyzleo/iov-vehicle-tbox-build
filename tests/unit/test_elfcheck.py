"""Unit tests for ELF parsing and pollution checking."""

from __future__ import annotations

from pathlib import Path

import pytest

from tbox_build.elfcheck import (
    ElfInfo,
    FileClassification,
    parse_elf,
    classify_file,
    check_file,
    check_staging,
    assert_clean,
    EM_AARCH64,
    EM_X86_64,
    _ELFCLASS64,
    _ELFCLASS32,
    ElfCheckError,
)


class TestElfParsing:
    def test_parse_aarch64_elf(self, minimal_aarch64_elf: Path):
        info = parse_elf(minimal_aarch64_elf)
        assert info.is_elf
        assert info.elf_class == _ELFCLASS64
        assert info.elf_machine == EM_AARCH64
        assert info.machine_name == "EM_AARCH64"
        assert info.is_64bit

    def test_parse_x86_64_elf(self, minimal_x86_64_elf: Path):
        info = parse_elf(minimal_x86_64_elf)
        assert info.is_elf
        assert info.elf_class == _ELFCLASS64
        assert info.elf_machine == EM_X86_64
        assert info.machine_name == "EM_X86_64"

    def test_parse_non_elf(self, tmp_path: Path):
        path = tmp_path / "not_elf"
        path.write_bytes(b"not an elf file at all")
        info = parse_elf(path)
        assert not info.is_elf

    def test_parse_empty_file(self, tmp_path: Path):
        path = tmp_path / "empty"
        path.write_bytes(b"")
        info = parse_elf(path)
        assert not info.is_elf

    @pytest.mark.skipif(
        not Path(__file__).resolve().parent.parent.parent.joinpath(
            "sysroots", "orin-r35.3.1", "lib", "aarch64-linux-gnu", "libc.so.6"
        ).exists(),
        reason="sysroot not available",
    )
    def test_parse_real_aarch64_libc(self, project_root: Path):
        libc = project_root / "sysroots" / "orin-r35.3.1" / "lib" / "aarch64-linux-gnu" / "libc.so.6"
        if libc.is_symlink():
            libc = libc.resolve()
        info = parse_elf(libc)
        assert info.is_elf
        assert info.elf_machine == EM_AARCH64
        assert info.elf_class == _ELFCLASS64


class TestFileClassification:
    def test_classify_elf(self, minimal_aarch64_elf: Path):
        cls = classify_file(minimal_aarch64_elf)
        assert cls.file_type == "elf"
        assert cls.elf_info is not None
        assert cls.elf_info.is_elf

    def test_classify_macho(self, macho_binary: Path):
        cls = classify_file(macho_binary)
        assert cls.file_type == "macho"
        assert cls.macho_description is not None

    def test_classify_ar_archive(self, ar_archive: Path):
        cls = classify_file(ar_archive)
        assert cls.file_type == "ar_archive"

    def test_classify_script(self, script_file: Path):
        cls = classify_file(script_file)
        assert cls.file_type == "script"

    def test_classify_data(self, tmp_path: Path):
        path = tmp_path / "data.bin"
        path.write_bytes(b"\x00\x01\x02\x03")
        cls = classify_file(path)
        assert cls.file_type == "data"


class TestCheckFile:
    def test_aarch64_elf_passes(self, minimal_aarch64_elf: Path):
        result = check_file(minimal_aarch64_elf)
        assert len(result.violations) == 0

    def test_x86_64_elf_fails(self, minimal_x86_64_elf: Path):
        result = check_file(minimal_x86_64_elf)
        assert len(result.violations) > 0
        assert any("x86_64" in v or "machine mismatch" in v for v in result.violations)

    def test_macho_fails(self, macho_binary: Path):
        result = check_file(macho_binary)
        assert len(result.violations) > 0
        assert any("Mach-O" in v for v in result.violations)

    def test_script_no_violations(self, script_file: Path):
        result = check_file(script_file)
        assert len(result.violations) == 0

    def test_ar_archive_warning(self, ar_archive: Path):
        result = check_file(ar_archive)
        assert len(result.violations) == 0
        assert len(result.warnings) > 0

    def test_data_file_no_violations(self, tmp_path: Path):
        path = tmp_path / "config.conf"
        path.write_text("key=value\n")
        result = check_file(path)
        assert len(result.violations) == 0


class TestCheckStaging:
    def test_clean_staging_passes(self, tmp_path: Path, minimal_aarch64_elf: Path, script_file: Path):
        staging = tmp_path / "staging"
        staging.mkdir()
        (staging / "usr").mkdir()
        (staging / "usr" / "bin").mkdir()
        (staging / "usr" / "bin" / "test_app").write_bytes(minimal_aarch64_elf.read_bytes())
        (staging / "etc").mkdir()
        (staging / "etc" / "test.conf").write_text("key=value\n")

        results = check_staging(staging)
        violations = sum(len(r.violations) for r in results)
        assert violations == 0

    def test_polluted_staging_fails(self, tmp_path: Path, minimal_x86_64_elf: Path, macho_binary: Path):
        staging = tmp_path / "staging"
        staging.mkdir()
        (staging / "usr").mkdir()
        (staging / "usr" / "bin").mkdir()
        (staging / "usr" / "bin" / "x86_app").write_bytes(minimal_x86_64_elf.read_bytes())
        (staging / "usr" / "lib").mkdir()
        (staging / "usr" / "lib" / "host.dylib").write_bytes(macho_binary.read_bytes())

        results = check_staging(staging)
        violations = sum(len(r.violations) for r in results)
        assert violations >= 2

    def test_assert_clean_raises(self, tmp_path: Path, minimal_x86_64_elf: Path):
        staging = tmp_path / "staging"
        staging.mkdir()
        (staging / "bad_app").write_bytes(minimal_x86_64_elf.read_bytes())

        results = check_staging(staging)
        with pytest.raises(ElfCheckError):
            assert_clean(results)

    def test_assert_clean_passes(self, tmp_path: Path, minimal_aarch64_elf: Path):
        staging = tmp_path / "staging"
        staging.mkdir()
        (staging / "good_app").write_bytes(minimal_aarch64_elf.read_bytes())

        results = check_staging(staging)
        assert_clean(results)  # should not raise
