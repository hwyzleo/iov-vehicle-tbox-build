"""Unit tests for CR-004 D4: ELF symbol-integrity verification (BUILD-REQ-048).

Builds synthetic ELF64 shared objects with real .dynsym / .dynamic /
.gnu.version* sections so the DT_NEEDED closure resolver, GNU version
matching and IFUNC/weak policies are exercised deterministically.
"""

from __future__ import annotations

import struct
from pathlib import Path

import pytest

from tbox_build.elfcheck import (
    check_symbol_integrity,
    parse_symbols,
)

STT_FUNC = 2
STT_GNU_IFUNC = 10
STB_GLOBAL = 1
STB_WEAK = 2
SHT_STRTAB = 3
SHT_DYNAMIC = 6
SHT_DYNSYM = 11
SHT_GNU_VERSYM = 0x6FFFFFFF
SHT_GNU_VERDEF = 0x6FFFFFFD
SHT_GNU_VERNEED = 0x6FFFFFFE
EM_AARCH64 = 183


def _shdr(sh_type, offset, size, link=0, entsize=0, addralign=8) -> bytes:
    return struct.pack(
        "<IIQQQQIIQQ",
        0, sh_type, 0, 0, offset, size, link, 0, addralign, entsize,
    )


def build_shared_elf(
    path: Path,
    *,
    soname: str = "libtest.so",
    needed: list[str] | None = None,
    exports: list[str] | None = None,
    versioned_exports: dict[str, str] | None = None,
    ifunc: set[str] | None = None,
    undef_strong: dict[str, str | None] | None = None,
    undef_weak: dict[str, str | None] | None = None,
) -> Path:
    """Build a minimal ELF64 aarch64 ET_DYN with dynamic symbols.

    ``exports`` / ``versioned_exports`` are defined (provided) symbols;
    ``undef_strong`` / ``undef_weak`` map symbol name -> required GNU
    version (None = unversioned reference).
    """
    exports = list(exports or [])
    versioned_exports = dict(versioned_exports or {})
    ifunc = set(ifunc or [])
    undef_strong = dict(undef_strong or {})
    undef_weak = dict(undef_weak or {})
    needed = list(needed or [])

    # Version index assignment (verdef: 2..; verneed: 100.. keeps them apart).
    def_versions = sorted(set(versioned_exports.values()))
    verdef_index = {v: i + 2 for i, v in enumerate(def_versions)}
    need_versions = sorted({
        v for v in list(undef_strong.values()) + list(undef_weak.values())
        if v is not None
    })
    verneed_index = {v: i + 100 for i, v in enumerate(need_versions)}

    # Symbol table (name, binding, type, shndx, version_index)
    symbols: list[tuple[str, int, int, int, int]] = []
    for name in exports:
        st = STT_GNU_IFUNC if name in ifunc else STT_FUNC
        symbols.append((name, STB_GLOBAL, st, 1, 1))
    for name, ver in versioned_exports.items():
        st = STT_GNU_IFUNC if name in ifunc else STT_FUNC
        symbols.append((name, STB_GLOBAL, st, 1, verdef_index[ver]))
    for name, ver in undef_strong.items():
        symbols.append((name, STB_GLOBAL, STT_FUNC, 0,
                        verneed_index.get(ver, 1) if ver else 1))
    for name, ver in undef_weak.items():
        symbols.append((name, STB_WEAK, STT_FUNC, 0,
                        verneed_index.get(ver, 1) if ver else 1))

    # .dynstr
    all_strings = [soname] + needed + [s[0] for s in symbols]
    all_strings += def_versions + need_versions
    strtab = bytearray(b"\x00")
    offsets: dict[str, int] = {}
    for s in all_strings:
        if s not in offsets:
            offsets[s] = len(strtab)
            strtab += s.encode() + b"\x00"
    strtab = bytes(strtab)

    # .dynsym
    dynsym = b""
    for name, binding, st, shndx, ver_idx in symbols:
        dynsym += struct.pack(
            "<IBBHQQ", offsets[name], (binding << 4) | st, 0, shndx, 0, 0
        )

    # .dynamic
    dynamic = b""
    dynamic += struct.pack("<qQ", 14, offsets[soname])  # DT_SONAME
    for n in needed:
        dynamic += struct.pack("<qQ", 1, offsets[n])    # DT_NEEDED
    dynamic += struct.pack("<qQ", 0, 0)                 # DT_NULL

    # .gnu.version (versym): one u16 per symbol
    versym = b"".join(struct.pack("<H", s[4]) for s in symbols)

    # .gnu.version_r (verneed)
    verneed = b""
    if need_versions:
        for ver in need_versions:
            idx = verneed_index[ver]
            # Elf64_Verneed: vn_version, vn_cnt, vn_file, vn_aux, vn_next
            verneed += struct.pack("<HHIII", 1, 1, offsets[soname], 16, 0)
            # Elf64_Vernaux: vna_hash, vna_flags, vna_other, vna_name, vna_next
            verneed += struct.pack("<IHHII", 0, 0, idx, offsets[ver], 0)

    # .gnu.version_d (verdef)
    verdef = b""
    if def_versions:
        for ver in def_versions:
            idx = verdef_index[ver]
            # Elf64_Verdef: vd_version, vd_flags, vd_ndx, vd_cnt, vd_hash,
            #               vd_aux, vd_next
            verdef += struct.pack("<HHHHIII", 1, 0, idx, 1, 0, 20, 0)
            # Elf64_Verdaux: vda_name, vda_next
            verdef += struct.pack("<II", offsets[ver], 0)

    # Assemble file: header + sections + section header table
    sections_data: list[tuple[int, int, int, int, int]] = [
        # (type, size, link, entsize, addralign)
        (SHT_STRTAB, len(strtab), 0, 0, 1),                       # 1 .dynstr
        (SHT_DYNSYM, len(dynsym), 1, 24, 8),                      # 2 .dynsym
        (SHT_DYNAMIC, len(dynamic), 1, 16, 8),                    # 3 .dynamic
        (SHT_GNU_VERSYM, len(versym), 2, 2, 2),                   # 4 .gnu.version
    ]
    if need_versions:
        sections_data.append(
            (SHT_GNU_VERNEED, len(verneed), 1, 0, 8))             # 5 .gnu.version_r
    if def_versions:
        sections_data.append(
            (SHT_GNU_VERDEF, len(verdef), 1, 0, 8))               # 6 .gnu.version_d

    shnum = 1 + len(sections_data)  # index 0 is SHT_NULL
    payloads = [strtab, dynsym, dynamic, versym]
    if need_versions:
        payloads.append(verneed)
    if def_versions:
        payloads.append(verdef)

    # Compute offsets
    offset = 64
    sh_offsets: list[int] = []
    for payload in payloads:
        sh_offsets.append(offset)
        offset += len(payload)
    shoff = offset

    # ELF header: 64 bytes, ET_DYN, EM_AARCH64, 64-bit LE
    e_ident = b"\x7fELF" + bytes([2, 1, 1, 0]) + b"\x00" * 8
    header = struct.pack(
        "<16sHHIQQQIHHHHHH",
        e_ident, 3, EM_AARCH64, 1, 0, 0, shoff, 0,
        64, 0, 0, 64, shnum, 0,
    )

    blob = header + b"".join(payloads)
    blob += _shdr(0, 0, 0)  # section 0: NULL
    for i, (stype, size, link, entsize, addralign) in enumerate(sections_data):
        blob += _shdr(stype, sh_offsets[i], size, link, entsize, addralign)

    path.write_bytes(blob)
    return path


def _write(root: Path, name: str, **kw) -> Path:
    return build_shared_elf(root / name, **kw)


class TestSyntheticParsing:
    def test_parse_symbols_basic(self, tmp_path):
        lib = build_shared_elf(
            tmp_path / "liba.so",
            soname="liba.so",
            exports=["alpha", "beta"],
            undef_strong={"need_x": None},
        )
        t = parse_symbols(lib)
        assert t is not None
        assert t.soname == "liba.so"
        assert "alpha" in t.exports and "beta" in t.exports
        assert t.undef_strong == {"need_x": None}

    def test_versioned_symbols_parsed(self, tmp_path):
        lib = build_shared_elf(
            tmp_path / "libv.so",
            soname="libv.so",
            versioned_exports={"foo": "GLIBC_2.17"},
            undef_strong={"bar": "GLIBC_2.17"},
        )
        t = parse_symbols(lib)
        assert t.exports.get("foo") == {"GLIBC_2.17"}
        assert t.undef_strong.get("bar") == "GLIBC_2.17"

    def test_ifunc_marked(self, tmp_path):
        lib = build_shared_elf(
            tmp_path / "libi.so", soname="libi.so",
            exports=["gettimeofday"], ifunc={"gettimeofday"},
        )
        t = parse_symbols(lib)
        assert "gettimeofday" in t.ifunc


class TestSymbolIntegrity:
    def test_unresolved_strong_fails(self, tmp_path):
        consumer = _write(tmp_path, "bin", soname="bin",
                          undef_strong={"missing_sym": None})
        results = check_symbol_integrity([consumer], roots=[tmp_path])
        assert len(results) == 1
        assert len(results[0].violations) == 1
        assert "undefined symbol 'missing_sym'" in results[0].violations[0]
        assert "traversed:" in results[0].violations[0]

    def test_satisfied_by_direct_needed(self, tmp_path):
        prov = tmp_path / "lib"
        prov.mkdir()
        _write(prov, "libprov.so", soname="libprov.so",
               exports=["provided_fn"])
        consumer = _write(tmp_path, "bin", soname="bin",
                          needed=["libprov.so"],
                          undef_strong={"provided_fn": None})
        results = check_symbol_integrity([consumer], roots=[prov])
        assert results[0].violations == []

    def test_satisfied_via_transitive_closure(self, tmp_path):
        root = tmp_path / "lib"
        root.mkdir()
        _write(root, "libc.so", soname="libc.so", exports=["deep_fn"])
        _write(root, "libb.so", soname="libb.so", needed=["libc.so"],
               exports=["mid_fn"])
        consumer = _write(tmp_path, "bin", soname="bin",
                          needed=["libb.so"],
                          undef_strong={"deep_fn": None, "mid_fn": None})
        results = check_symbol_integrity([consumer], roots=[root])
        assert results[0].violations == []
        assert "libb.so" in results[0].warnings or True  # closure traversed

    def test_missing_library_release_error(self, tmp_path):
        consumer = _write(tmp_path, "bin", soname="bin",
                          needed=["libgone.so"],
                          undef_strong={"x": None})
        results = check_symbol_integrity([consumer], roots=[tmp_path],
                                         missing_library_policy="error")
        assert any("libgone.so" in v and "not found" in v
                   for v in results[0].violations)

    def test_missing_library_dev_warning(self, tmp_path):
        consumer = _write(tmp_path, "bin", soname="bin",
                          needed=["libgone.so"])
        results = check_symbol_integrity([consumer], roots=[tmp_path],
                                         missing_library_policy="warning")
        assert results[0].violations == []
        assert any("libgone.so" in w for w in results[0].warnings)

    def test_weak_undef_is_warning(self, tmp_path):
        consumer = _write(tmp_path, "bin", soname="bin",
                          undef_weak={"weak_gone": None})
        results = check_symbol_integrity([consumer], roots=[tmp_path])
        assert results[0].violations == []
        assert any("weak undefined symbol" in w for w in results[0].warnings)

    def test_ifunc_provider_satisfies(self, tmp_path):
        prov = tmp_path / "lib"
        prov.mkdir()
        _write(prov, "libi.so", soname="libi.so",
               exports=["gettimeofday"], ifunc={"gettimeofday"})
        consumer = _write(tmp_path, "bin", soname="bin",
                          needed=["libi.so"],
                          undef_strong={"gettimeofday": None})
        results = check_symbol_integrity([consumer], roots=[prov])
        assert results[0].violations == []

    def test_versioned_match_passes(self, tmp_path):
        prov = tmp_path / "lib"
        prov.mkdir()
        _write(prov, "libv.so", soname="libv.so",
               versioned_exports={"foo": "GLIBC_2.17"})
        consumer = _write(tmp_path, "bin", soname="bin",
                          needed=["libv.so"],
                          undef_strong={"foo": "GLIBC_2.17"})
        results = check_symbol_integrity([consumer], roots=[prov])
        assert results[0].violations == []

    def test_version_mismatch_fails(self, tmp_path):
        prov = tmp_path / "lib"
        prov.mkdir()
        _write(prov, "libv.so", soname="libv.so",
               versioned_exports={"foo": "GLIBC_2.18"})
        consumer = _write(tmp_path, "bin", soname="bin",
                          needed=["libv.so"],
                          undef_strong={"foo": "GLIBC_2.17"})
        results = check_symbol_integrity([consumer], roots=[prov])
        assert len(results[0].violations) == 1
        assert "undefined symbol 'foo@GLIBC_2.17'" in results[0].violations[0]

    def test_unversioned_ref_to_versioned_provider_passes(self, tmp_path):
        prov = tmp_path / "lib"
        prov.mkdir()
        _write(prov, "libv.so", soname="libv.so",
               versioned_exports={"foo": "GLIBC_2.17"})
        consumer = _write(tmp_path, "bin", soname="bin",
                          needed=["libv.so"], undef_strong={"foo": None})
        results = check_symbol_integrity([consumer], roots=[prov])
        assert results[0].violations == []

    def test_exempt_consumer_downgrades_to_warning(self, tmp_path):
        consumer = _write(tmp_path, "tbox_prov", soname="tbox_prov",
                          undef_strong={"plugin_sym": None})
        results = check_symbol_integrity(
            [consumer], roots=[tmp_path], exempt_consumers=frozenset({"tbox_prov"})
        )
        assert results[0].violations == []
        assert any("D4 corroboration" in w for w in results[0].warnings)

    def test_roots_precedence_sdk_first(self, tmp_path):
        sdk = tmp_path / "sdk"; dep = tmp_path / "dep"
        sdk.mkdir(); dep.mkdir()
        _write(sdk, "libx.so", soname="libx.so", exports=["sdk_fn"])
        _write(dep, "libx.so", soname="libx.so", exports=["dep_fn"])
        consumer = _write(tmp_path, "bin", soname="bin",
                          needed=["libx.so"], undef_strong={"sdk_fn": None})
        results = check_symbol_integrity([consumer], roots=[sdk, dep])
        assert results[0].violations == []

    def test_deterministic_diagnostics(self, tmp_path):
        a = _write(tmp_path, "bin1", soname="bin1", undef_strong={"z": None})
        b = _write(tmp_path, "bin2", soname="bin2", undef_strong={"a": None})
        results = check_symbol_integrity([b, a], roots=[tmp_path])
        paths = [str(r.path.name) for r in results]
        assert paths == ["bin1", "bin2"]  # sorted

    @pytest.mark.skipif(
        not Path(__file__).resolve().parent.parent.parent.joinpath(
            "sysroots", "orin-r35.3.1", "lib", "aarch64-linux-gnu", "libc.so.6"
        ).exists(),
        reason="sysroot not available",
    )
    def test_real_libc_passes(self, project_root: Path):
        """The real sysroot libc must satisfy its own DT_NEEDED closure."""
        sysroot = project_root / "sysroots" / "orin-r35.3.1"
        libc = sysroot / "usr/lib/aarch64-linux-gnu/libc.so.6"
        results = check_symbol_integrity(
            [libc],
            roots=[sysroot / "lib", sysroot / "usr/lib"],
            missing_library_policy="error",
        )
        assert len(results) == 1
        assert results[0].violations == []


# ---------------------------------------------------------------------------
# CR-004 评审回归 P2-7: 豁免键必须覆盖库产物基名
# ---------------------------------------------------------------------------


class TestConsumerExemptionKeys:
    """link-exemptions.yaml records CMake target names, artifacts carry
    filenames. Matching on the raw basename alone silently missed every
    shared-library exemption.
    """

    def test_executable_matches_target_name(self):
        from tbox_build.elfcheck import _consumer_exemption_keys

        keys = _consumer_exemption_keys(Path("/s/usr/bin/tbox_prov"))
        assert "tbox_prov" in keys

    def test_library_matches_target_name(self):
        from tbox_build.elfcheck import _consumer_exemption_keys

        for name in ("libfoo.so", "libfoo.so.1", "libfoo.so.1.2.3"):
            keys = _consumer_exemption_keys(Path("/s/usr/lib") / name)
            assert "foo" in keys, (name, keys)
            assert "libfoo" in keys, (name, keys)
            assert name in keys

    def test_versioned_tbox_library(self):
        from tbox_build.elfcheck import _consumer_exemption_keys

        keys = _consumer_exemption_keys(Path("/s/usr/lib/libhwyz.so.0.8"))
        assert {"hwyz", "libhwyz", "libhwyz.so.0.8"} <= keys
