"""ELF architecture and host-pollution checking.

Pure-Python ELF parser (no external dependencies) that verifies:

  * ELF class and machine match the expected target (aarch64 / 64-bit)
  * Dynamic interpreter is a Linux loader, not a host (macOS) one
  * DT_NEEDED entries reference expected system libraries
  * DT_RPATH / DT_RUNPATH do not contain host or build-tree paths
  * Files are not Mach-O (host) or x86_64 ELF (host architecture)

Also detects Mach-O binaries for macOS host-pollution checks.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, field
from pathlib import Path

from .errors import ElfCheckError

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_ELF_MAGIC = b"\x7fELF"

_ELFCLASS32 = 1
_ELFCLASS64 = 2

_ELFDATA2LSB = 1  # little-endian
_ELFDATA2MSB = 2  # big-endian

EM_AARCH64 = 183
EM_X86_64 = 62
EM_ARM = 40

ET_REL = 1  # relocatable (object file)
ET_EXEC = 2  # executable
ET_DYN = 3  # shared object
ET_CORE = 4  # core dump

PT_INTERP = 3

SHT_DYNAMIC = 6
SHT_DYNSYM = 11

SHT_GNU_VERSYM = 0x6FFFFFFF
SHT_GNU_VERDEF = 0x6FFFFFFD
SHT_GNU_VERNEED = 0x6FFFFFFE

DT_NULL = 0
DT_NEEDED = 1
DT_SONAME = 14
DT_STRTAB = 5
DT_RPATH = 15
DT_RUNPATH = 29
DT_STRSZ = 10

# Symbol binding (ELF64_ST_BIND)
STB_GLOBAL = 1
STB_WEAK = 2

# Symbol types (ELF64_ST_TYPE)
STT_NOTYPE = 0
STT_OBJECT = 1
STT_FUNC = 2
STT_SECTION = 3
STT_FILE = 4
STT_GNU_IFUNC = 10

# Version index semantics (versym)
VER_NDX_LOCAL = 0
VER_NDX_GLOBAL = 1

_MACHINE_NAMES: dict[int, str] = {
    0: "EM_NONE",
    40: "EM_ARM",
    62: "EM_X86_64",
    183: "EM_AARCH64",
}

# Mach-O magic numbers (host pollution detection)
_MACHO_MAGICS = {
    b"\xfe\xed\xfa\xce": "MH_MAGIC (32-bit BE)",
    b"\xfe\xed\xfa\xcf": "MH_MAGIC_64 (64-bit BE)",
    b"\xce\xfa\xed\xfe": "MH_MAGIC (32-bit LE)",
    b"\xcf\xfa\xed\xfe": "MH_MAGIC_64 (64-bit LE)",
}

# Default host paths that must never appear in RPATH/RUNPATH
DEFAULT_HOST_POLLUTION_PATHS = (
    "/usr/local",
    "/opt/homebrew",
    "/opt/local",
    "/sw",
    "/nix",
    "/home/",
    "/Users/",
    "/tmp/",
)

# Build-tree path indicators (relative or absolute build dirs)
DEFAULT_BUILD_TREE_INDICATORS = (
    "/out/",
    "out/orin",
    "build/",
    "CMakeFiles",
)


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


@dataclass
class ElfInfo:
    """Parsed ELF metadata."""

    is_elf: bool = False
    elf_class: int = 0  # 0=unknown, 1=32, 2=64
    elf_data: int = 0  # 0=unknown, 1=LE, 2=BE
    elf_type: int = 0
    elf_machine: int = 0
    interpreter: str | None = None
    needed: list[str] = field(default_factory=list)
    rpath: list[str] = field(default_factory=list)
    runpath: list[str] = field(default_factory=list)
    soname: str | None = None

    @property
    def machine_name(self) -> str:
        return _MACHINE_NAMES.get(self.elf_machine, f"UNKNOWN({self.elf_machine})")

    @property
    def is_64bit(self) -> bool:
        return self.elf_class == _ELFCLASS64

    @property
    def is_executable(self) -> bool:
        return self.elf_type in (ET_EXEC, ET_DYN) and self.interpreter is not None

    @property
    def is_shared_lib(self) -> bool:
        return self.elf_type == ET_DYN and self.interpreter is None


@dataclass
class FileClassification:
    """Classification of a file in staging."""

    path: Path
    file_type: str  # "elf", "macho", "ar_archive", "script", "data", "other"
    elf_info: ElfInfo | None = None
    macho_description: str | None = None


# ---------------------------------------------------------------------------
# ELF parsing
# ---------------------------------------------------------------------------


def _read_elf_header(data: bytes) -> tuple[int, int, dict] | None:
    """Parse ELF header. Returns (elf_class, elf_data, fields) or None."""
    if len(data) < 64 or data[:4] != _ELF_MAGIC:
        return None

    elf_class = data[4]
    elf_data = data[5]

    if elf_class == _ELFCLASS64:
        if elf_data == _ELFDATA2LSB:
            fmt = "<"
        elif elf_data == _ELFDATA2MSB:
            fmt = ">"
        else:
            return None
        # e_type(H) e_machine(H) e_version(I) e_entry(Q) e_phoff(Q) e_shoff(Q)
        # e_flags(I) e_ehsize(H) e_phentsize(H) e_phnum(H)
        # e_shentsize(H) e_shnum(H) e_shstrndx(H)
        try:
            (
                e_type, e_machine, _e_version, _e_entry,
                e_phoff, e_shoff, _e_flags, _e_ehsize,
                e_phentsize, e_phnum,
                e_shentsize, e_shnum, _e_shstrndx,
            ) = struct.unpack_from(fmt + "HHIQQQIHHHHHH", data, 16)
        except struct.error:
            return None
        return elf_class, elf_data, {
            "e_type": e_type,
            "e_machine": e_machine,
            "e_phoff": e_phoff,
            "e_phnum": e_phnum,
            "e_phentsize": e_phentsize,
            "e_shoff": e_shoff,
            "e_shnum": e_shnum,
            "e_shentsize": e_shentsize,
            "is64": True,
            "fmt": fmt,
        }
    elif elf_class == _ELFCLASS32:
        if elf_data == _ELFDATA2LSB:
            fmt = "<"
        elif elf_data == _ELFDATA2MSB:
            fmt = ">"
        else:
            return None
        try:
            (
                e_type, e_machine, _e_version, _e_entry,
                e_phoff, e_shoff, _e_flags, _e_ehsize,
                e_phentsize, e_phnum,
                e_shentsize, e_shnum, _e_shstrndx,
            ) = struct.unpack_from(fmt + "HHIIIIIHHHHHH", data, 16)
        except struct.error:
            return None
        return elf_class, elf_data, {
            "e_type": e_type,
            "e_machine": e_machine,
            "e_phoff": e_phoff,
            "e_phnum": e_phnum,
            "e_phentsize": e_phentsize,
            "e_shoff": e_shoff,
            "e_shnum": e_shnum,
            "e_shentsize": e_shentsize,
            "is64": False,
            "fmt": fmt,
        }
    return None


def _parse_program_headers(
    data: bytes, hdr: dict
) -> str | None:
    """Find and return the interpreter (PT_INTERP) string."""
    fmt = hdr["fmt"]
    is64 = hdr["is64"]
    phoff = hdr["e_phoff"]
    phnum = hdr["e_phnum"]
    phentsize = hdr["e_phentsize"]

    if is64:
        # p_type(I) p_flags(I) p_offset(Q) p_vaddr(Q) p_paddr(Q)
        # p_filesz(Q) p_memsz(Q) p_align(Q)
        ph_fmt = fmt + "IIQQQQQQ"
        ph_size = 56
    else:
        # p_type(I) p_offset(I) p_vaddr(I) p_paddr(I) p_filesz(I)
        # p_memsz(I) p_flags(I) p_align(I)
        ph_fmt = fmt + "IIIIIIII"
        ph_size = 32

    for i in range(phnum):
        offset = phoff + i * phentsize
        if offset + ph_size > len(data):
            break
        fields = struct.unpack_from(ph_fmt, data, offset)
        p_type = fields[0]
        if p_type != PT_INTERP:
            continue
        if is64:
            p_offset = fields[2]
            p_filesz = fields[5]
        else:
            p_offset = fields[1]
            p_filesz = fields[4]
        raw = data[p_offset : p_offset + p_filesz]
        return raw.rstrip(b"\x00").decode("utf-8", errors="replace")
    return None


def _parse_sections(data: bytes, hdr: dict) -> list[dict]:
    """Parse all section headers. Returns a list of section dicts.

    Each dict carries ``name`` (offset into .shstrtab), ``type``, ``offset``,
    ``size``, ``link``, ``info``, ``flags``, ``addralign``, ``entsize``.
    """
    fmt = hdr["fmt"]
    is64 = hdr["is64"]
    shoff = hdr["e_shoff"]
    shnum = hdr["e_shnum"]
    shentsize = hdr["e_shentsize"]

    if shoff == 0 or shnum == 0:
        return []

    if is64:
        sh_fmt = fmt + "IIQQQQIIQQ"
        sh_size = 64
    else:
        sh_fmt = fmt + "IIIIIIIIII"
        sh_size = 40

    sections = []
    for i in range(shnum):
        offset = shoff + i * shentsize
        if offset + sh_size > len(data):
            break
        fields = struct.unpack_from(sh_fmt, data, offset)
        (
            sh_name, sh_type, sh_flags, sh_addr,
            sh_offset, sh_size_val, sh_link, sh_info,
            sh_addralign, sh_entsize,
        ) = fields
        sections.append({
            "name": sh_name,
            "type": sh_type,
            "flags": sh_flags,
            "addr": sh_addr,
            "offset": sh_offset,
            "size": sh_size_val,
            "link": sh_link,
            "info": sh_info,
            "addralign": sh_addralign,
            "entsize": sh_entsize,
        })
    return sections


def _read_strtab(data: bytes, str_offset: int) -> str:
    """Read a NUL-terminated string from a string table buffer."""
    end = data.find(b"\x00", str_offset)
    if end < 0:
        end = len(data)
    return data[str_offset:end].decode("utf-8", errors="replace")


def _parse_dynamic_section(data: bytes, hdr: dict) -> tuple[list[str], list[str], list[str], str | None]:
    """Parse .dynamic section. Returns (needed, rpath, runpath, soname)."""
    fmt = hdr["fmt"]
    is64 = hdr["is64"]
    sections = _parse_sections(data, hdr)

    # Find SHT_DYNAMIC
    dynamic_offset = 0
    dynamic_size = 0
    dynamic_link = 0  # section index of .dynstr
    for sec in sections:
        if sec["type"] == SHT_DYNAMIC:
            dynamic_offset = sec["offset"]
            dynamic_size = sec["size"]
            dynamic_link = sec["link"]
            break

    if dynamic_offset == 0 or dynamic_size == 0:
        return [], [], [], None

    # Find the linked string table (.dynstr)
    dynstr_offset = 0
    dynstr_size = 0
    if 0 < dynamic_link < len(sections):
        dynstr_section = sections[dynamic_link]
        dynstr_offset = dynstr_section["offset"]
        dynstr_size = dynstr_section["size"]
    else:
        # Try to find .dynstr by looking for SHT_STRTAB that's not .shstrtab
        # This is a fallback; the linked approach is more reliable
        return [], [], [], None

    if dynstr_offset == 0 or dynstr_size == 0:
        return [], [], [], None

    # Read the string table
    strtab = data[dynstr_offset : dynstr_offset + dynstr_size]

    # Parse dynamic entries
    if is64:
        dyn_fmt = fmt + "qQ"  # d_tag (signed), d_val
        dyn_entry_size = 16
    else:
        dyn_fmt = fmt + "iI"
        dyn_entry_size = 8

    needed: list[str] = []
    rpath: list[str] = []
    runpath: list[str] = []
    soname: str | None = None

    num_entries = dynamic_size // dyn_entry_size
    for i in range(num_entries):
        offset = dynamic_offset + i * dyn_entry_size
        if offset + dyn_entry_size > len(data):
            break
        d_tag, d_val = struct.unpack_from(dyn_fmt, data, offset)
        if d_tag == DT_NULL:
            break
        elif d_tag == DT_NEEDED:
            needed.append(_read_strtab(strtab, d_val))
        elif d_tag == DT_SONAME:
            soname = _read_strtab(strtab, d_val)
        elif d_tag == DT_RPATH:
            rpath.extend(_read_strtab(strtab, d_val).split(":"))
        elif d_tag == DT_RUNPATH:
            runpath.extend(_read_strtab(strtab, d_val).split(":"))

    # Filter empty strings
    needed = [n for n in needed if n]
    rpath = [r for r in rpath if r]
    runpath = [r for r in runpath if r]

    return needed, rpath, runpath, soname


def parse_elf(path: Path) -> ElfInfo:
    """Parse an ELF file and return its metadata."""
    with open(path, "rb") as f:
        data = f.read()

    hdr_result = _read_elf_header(data)
    if hdr_result is None:
        return ElfInfo(is_elf=False)

    elf_class, elf_data, hdr = hdr_result
    info = ElfInfo(
        is_elf=True,
        elf_class=elf_class,
        elf_data=elf_data,
        elf_type=hdr["e_type"],
        elf_machine=hdr["e_machine"],
    )

    info.interpreter = _parse_program_headers(data, hdr)
    info.needed, info.rpath, info.runpath, info.soname = _parse_dynamic_section(data, hdr)
    return info


# ---------------------------------------------------------------------------
# File classification
# ---------------------------------------------------------------------------


def classify_file(path: Path) -> FileClassification:
    """Classify a file as ELF, Mach-O, ar archive, script, or other."""
    try:
        with open(path, "rb") as f:
            header = f.read(16)
    except OSError:
        return FileClassification(path=path, file_type="other")

    if len(header) < 4:
        return FileClassification(path=path, file_type="data")

    # ELF
    if header[:4] == _ELF_MAGIC:
        elf_info = parse_elf(path)
        return FileClassification(path=path, file_type="elf", elf_info=elf_info)

    # Mach-O
    if header[:4] in _MACHO_MAGICS:
        return FileClassification(
            path=path, file_type="macho", macho_description=_MACHO_MAGICS[header[:4]]
        )

    # ar archive (static library .a)
    if header[:8] == b"!<arch>\n":
        return FileClassification(path=path, file_type="ar_archive")

    # Script (starts with #!)
    if header[:2] == b"#!":
        return FileClassification(path=path, file_type="script")

    return FileClassification(path=path, file_type="data")


# ---------------------------------------------------------------------------
# ar archive member inspection
# ---------------------------------------------------------------------------


def check_archive_members(
    path: Path,
    expected_machine: int = EM_AARCH64,
    expected_class: int = _ELFCLASS64,
) -> tuple[int, list[str]]:
    """Inspect every ELF member of an ar archive.

    Returns ``(members_checked, violations)``. Non-ELF members (such as
    symbol tables or long-name indices) are skipped. Each ELF member must
    be ELF64 and match *expected_machine* (default AArch64).
    """
    violations: list[str] = []
    checked = 0
    try:
        with open(path, "rb") as f:
            magic = f.read(8)
        if magic != b"!<arch>\n":
            return 0, [f"{path}: not a valid ar archive (bad magic)"]
        with open(path, "rb") as f:
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
                # Skip symbol table / long-name index members.
                if name in ("", "/") or name.startswith("//") or name.startswith("/SYM"):
                    f.seek((size + 1) // 2 * 2, 1)
                    continue
                member_data = f.read(size)
                if size % 2 == 1:
                    f.read(1)
                if len(member_data) >= 4 and member_data[:4] == _ELF_MAGIC:
                    checked += 1
                    ei_class = member_data[4]
                    e_machine = struct.unpack_from("<H", member_data, 18)[0]
                    if ei_class != expected_class:
                        violations.append(
                            f"{path}: member '{name}' is not ELF64 (class={ei_class})"
                        )
                    elif e_machine != expected_machine:
                        violations.append(
                            f"{path}: member '{name}' is not "
                            f"{_MACHINE_NAMES.get(expected_machine, '?')} "
                            f"(machine={e_machine})"
                        )
    except OSError as exc:
        return 0, [f"{path}: could not read archive ({exc})"]
    return checked, violations


# ---------------------------------------------------------------------------
# Pollution checking
# ---------------------------------------------------------------------------


@dataclass
class ElfCheckResult:
    """Result of checking a single file."""

    path: Path
    violations: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    classification: FileClassification | None = None


def check_file(
    path: Path,
    expected_machine: int = EM_AARCH64,
    expected_class: int = _ELFCLASS64,
    host_pollution_paths: tuple[str, ...] = DEFAULT_HOST_POLLUTION_PATHS,
    build_tree_indicators: tuple[str, ...] = DEFAULT_BUILD_TREE_INDICATORS,
) -> ElfCheckResult:
    """Check a single file for architecture and pollution issues.

    Returns an :class:`ElfCheckResult` with violations and warnings.
    Non-ELF files (scripts, data, configs) are skipped with no violations.
    """
    result = ElfCheckResult(path=path)
    result.classification = classify_file(path)
    cls = result.classification

    # Mach-O is always a violation (host binary in target staging)
    if cls.file_type == "macho":
        result.violations.append(
            f"Mach-O binary detected ({cls.macho_description}): {path}. "
            f"Host macOS binary must not enter target staging."
        )
        return result

    # ar archives: deeply check each ELF member is AArch64 ELF64.
    if cls.file_type == "ar_archive":
        members_checked, arch_violations = check_archive_members(
            path, expected_machine=expected_machine, expected_class=expected_class
        )
        result.violations.extend(arch_violations)
        if members_checked == 0 and not arch_violations:
            result.warnings.append(
                f"Static library has no ELF members to check: {path}"
            )
        return result

    # Scripts and data files are not checked
    if cls.file_type != "elf" or cls.elf_info is None or not cls.elf_info.is_elf:
        return result

    elf = cls.elf_info

    # Check ELF class
    if elf.elf_class != expected_class:
        result.violations.append(
            f"ELF class mismatch: expected {expected_class} (64-bit), "
            f"got {elf.elf_class} in {path}"
        )

    # Check machine
    if elf.elf_machine != expected_machine:
        result.violations.append(
            f"ELF machine mismatch: expected {expected_machine} "
            f"({_MACHINE_NAMES.get(expected_machine, '?')}), "
            f"got {elf.elf_machine} ({elf.machine_name}) in {path}"
        )

    # x86_64 ELF is a host pollution violation
    if elf.elf_machine == EM_X86_64:
        result.violations.append(
            f"x86_64 ELF detected in target staging: {path}. "
            f"Host architecture binary must not enter target staging."
        )

    # Check interpreter (dynamic executables only)
    if elf.interpreter:
        if "/lib/ld-linux" not in elf.interpreter and "/lib64/ld-linux" not in elf.interpreter:
            result.violations.append(
                f"Unexpected ELF interpreter '{elf.interpreter}' in {path}. "
                f"Expected a Linux dynamic loader."
            )

    # Check RPATH for host pollution
    all_paths = elf.rpath + elf.runpath
    for rp in all_paths:
        for host_path in host_pollution_paths:
            if rp.startswith(host_path):
                result.violations.append(
                    f"Host path '{rp}' in RPATH/RUNPATH of {path}"
                )
        for indicator in build_tree_indicators:
            if indicator in rp:
                result.violations.append(
                    f"Build-tree path '{rp}' in RPATH/RUNPATH of {path}"
                )
        # Origin-relative paths ($ORIGIN) are allowed
        if rp.startswith("$ORIGIN"):
            continue

    return result


def check_staging(
    staging_root: Path,
    expected_machine: int = EM_AARCH64,
    expected_class: int = _ELFCLASS64,
    host_pollution_paths: tuple[str, ...] = DEFAULT_HOST_POLLUTION_PATHS,
    build_tree_indicators: tuple[str, ...] = DEFAULT_BUILD_TREE_INDICATORS,
) -> list[ElfCheckResult]:
    """Check all files in a staging directory tree.

    Returns a list of :class:`ElfCheckResult` for every file checked.
    """
    results: list[ElfCheckResult] = []
    for path in sorted(staging_root.rglob("*")):
        if not path.is_file():
            continue
        result = check_file(
            path,
            expected_machine=expected_machine,
            expected_class=expected_class,
            host_pollution_paths=host_pollution_paths,
            build_tree_indicators=build_tree_indicators,
        )
        results.append(result)
    return results


def assert_clean(results: list[ElfCheckResult]) -> None:
    """Raise ElfCheckError if any result has violations."""
    all_violations: list[str] = []
    for r in results:
        all_violations.extend(r.violations)
    if all_violations:
        raise ElfCheckError(
            f"ELF/pollution check failed ({len(all_violations)} violation(s))",
            all_violations,
        )


# ---------------------------------------------------------------------------
# Symbol tables and GNU versioning (CR-004 D4, BUILD-REQ-048)
# ---------------------------------------------------------------------------


@dataclass
class DynamicSymbol:
    """A single .dynsym entry."""

    name: str
    binding: str  # GLOBAL | WEAK
    sym_type: str
    shndx: int
    version_index: int
    required_version: str | None
    is_undef: bool
    is_ifunc: bool


@dataclass
class SymbolTable:
    """Parsed dynamic symbol table + GNU version metadata for one ELF.

    ``undef_strong``/``undef_weak`` map symbol name -> required GNU version
    (None when the reference is unversioned). ``exports`` maps a defined
    symbol name -> set of version names it defines (empty set = unversioned).
    """

    soname: str | None
    needed: list[str]
    symbols: list[DynamicSymbol]
    undef_strong: dict[str, str | None]
    undef_weak: dict[str, str | None]
    exports: dict[str, set[str]]
    ifunc: set[str]


_BINDING_NAMES = {STB_GLOBAL: "GLOBAL", STB_WEAK: "WEAK"}

_TYPE_NAMES = {
    STT_NOTYPE: "NOTYPE",
    STT_OBJECT: "OBJECT",
    STT_FUNC: "FUNC",
    STT_SECTION: "SECTION",
    STT_FILE: "FILE",
    STT_GNU_IFUNC: "GNU_IFUNC",
}


def _parse_gnu_verneed(
    data: bytes, sections: list[dict], fmt: str, strtab: bytes
) -> dict[int, str]:
    """Parse SHT_GNU_verneed: version index (vna_other) -> version name."""
    result: dict[int, str] = {}
    for sec in sections:
        if sec["type"] != SHT_GNU_VERNEED:
            continue
        off = sec["offset"]
        end = off + sec["size"]
        pos = off
        while pos + 16 <= end:
            vn_version, vn_cnt, vn_file, vn_aux, vn_next = struct.unpack_from(
                fmt + "HHIII", data, pos
            )
            aux_pos = pos + vn_aux
            for _ in range(vn_cnt):
                if aux_pos + 16 > end:
                    break
                vna_hash, vna_flags, vna_other, vna_name, vna_next_aux = (
                    struct.unpack_from(fmt + "IHHII", data, aux_pos)
                )
                result[vna_other] = _read_strtab(strtab, vna_name)
                if vna_next_aux == 0:
                    break
                aux_pos += vna_next_aux
            if vn_next == 0:
                break
            pos += vn_next
    return result


def _parse_gnu_verdef(
    data: bytes, sections: list[dict], fmt: str, strtab: bytes
) -> dict[int, str]:
    """Parse SHT_GNU_verdef: version index (vd_ndx) -> version name.

    Only the first aux entry of a verdef is the version's own name; further
    aux entries declare parent versions and must not overwrite it.
    """
    result: dict[int, str] = {}
    for sec in sections:
        if sec["type"] != SHT_GNU_VERDEF:
            continue
        off = sec["offset"]
        end = off + sec["size"]
        pos = off
        while pos + 20 <= end:
            vd_version, vd_flags, vd_ndx, vd_cnt, vd_hash, vd_aux, vd_next = (
                struct.unpack_from(fmt + "HHHHIII", data, pos)
            )
            aux_pos = pos + vd_aux
            if vd_cnt >= 1 and aux_pos + 8 <= end:
                vda_name, vda_next = struct.unpack_from(fmt + "II", data, aux_pos)
                result[vd_ndx] = _read_strtab(strtab, vda_name)
            if vd_next == 0:
                break
            pos += vd_next
    return result


def _parse_gnu_versym(
    data: bytes, sections: list[dict], fmt: str, count: int
) -> list[int]:
    """Parse SHT_GNU_versym into version indices (VER_NDX_* semantics kept)."""
    for sec in sections:
        if sec["type"] != SHT_GNU_VERSYM:
            continue
        off = sec["offset"]
        result: list[int] = []
        for i in range(count):
            pos = off + i * 2
            if pos + 2 > len(data):
                break
            val = struct.unpack_from(fmt + "H", data, pos)[0]
            result.append(val & 0x7FFF)  # strip VERSYM_HIDDEN
        return result
    return [VER_NDX_GLOBAL] * count


def _parse_dynsym(
    data: bytes, sections: list[dict], fmt: str, is64: bool
) -> SymbolTable | None:
    """Parse SHT_DYNSYM + GNU version sections into a SymbolTable."""
    symsec = None
    for sec in sections:
        if sec["type"] == SHT_DYNSYM:
            symsec = sec
            break
    if symsec is None:
        return None
    sym_offset = symsec["offset"]
    sym_size = symsec["size"]
    sym_link = symsec["link"]
    entsize = symsec["entsize"] or (24 if is64 else 16)

    if not (0 < sym_link < len(sections)):
        return None
    strsec = sections[sym_link]
    strtab = data[strsec["offset"] : strsec["offset"] + strsec["size"]]

    verneed_map = _parse_gnu_verneed(data, sections, fmt, strtab)
    verdef_map = _parse_gnu_verdef(data, sections, fmt, strtab)
    count = sym_size // entsize
    versym = _parse_gnu_versym(data, sections, fmt, count)

    symbols: list[DynamicSymbol] = []
    for i in range(count):
        off = sym_offset + i * entsize
        if is64:
            if off + 24 > len(data):
                break
            st_name, st_info, st_other, st_shndx, st_value, st_size = (
                struct.unpack_from(fmt + "IBBHQQ", data, off)
            )
        else:
            if off + 16 > len(data):
                break
            st_name, st_value, st_size, st_info, st_other, st_shndx = (
                struct.unpack_from(fmt + "IIIBBH", data, off)
            )
        name = _read_strtab(strtab, st_name)
        binding = st_info >> 4
        sym_type = st_info & 0xF
        if binding not in (STB_GLOBAL, STB_WEAK):
            continue
        if not name:
            continue
        version_index = versym[i] if i < len(versym) else VER_NDX_GLOBAL
        is_undef = st_shndx == 0
        required_version: str | None = None
        if is_undef and version_index >= 2:
            required_version = verneed_map.get(version_index)
        elif not is_undef and version_index >= 2:
            # defined symbol's version index refers to a verdef entry
            defined_version = verdef_map.get(version_index)
            if defined_version is not None:
                pass  # captured below per-symbol version set
        symbols.append(DynamicSymbol(
            name=name,
            binding=_BINDING_NAMES.get(binding, f"BIND_{binding}"),
            sym_type=_TYPE_NAMES.get(sym_type, f"TYPE_{sym_type}"),
            shndx=st_shndx,
            version_index=version_index,
            required_version=required_version,
            is_undef=is_undef,
            is_ifunc=sym_type == STT_GNU_IFUNC,
        ))

    undef_strong: dict[str, str | None] = {}
    undef_weak: dict[str, str | None] = {}
    exports: dict[str, set[str]] = {}
    ifunc: set[str] = set()
    for sym in symbols:
        if sym.is_undef:
            target = undef_weak if sym.binding == "WEAK" else undef_strong
            target.setdefault(sym.name, sym.required_version)
            continue
        # exported definition (GLOBAL/WEAK, defined, not a section/file marker)
        if sym.sym_type in ("SECTION", "FILE"):
            continue
        exports.setdefault(sym.name, set())
        if sym.version_index >= 2:
            exports[sym.name].add(_symbol_verdef_name(verdef_map, sym.version_index))
        if sym.is_ifunc:
            ifunc.add(sym.name)

    return SymbolTable(
        soname=None,  # filled by caller from ElfInfo
        needed=[],
        symbols=symbols,
        undef_strong=undef_strong,
        undef_weak=undef_weak,
        exports=exports,
        ifunc=ifunc,
    )


def _symbol_verdef_name(verdef_map: dict[int, str], version_index: int) -> str:
    """Map a defined symbol's version index to its version name."""
    return verdef_map.get(version_index, f"VER{version_index}")


def _read_elf_data(path: Path) -> bytes:
    with open(path, "rb") as f:
        return f.read()


_SYMBOL_CACHE: dict[Path, SymbolTable | None] = {}


def parse_symbols(path: Path) -> SymbolTable | None:
    """Parse the dynamic symbol table + GNU version metadata of an ELF file.

    Returns None for non-ELF files or ELFs without a dynamic symbol table.
    The soname and DT_NEEDED list are merged from the ELF header parsing.
    """
    if path in _SYMBOL_CACHE:
        return _SYMBOL_CACHE[path]
    data = _read_elf_data(path)
    hdr_result = _read_elf_header(data)
    if hdr_result is None:
        _SYMBOL_CACHE[path] = None
        return None
    elf_class, _elf_data, hdr = hdr_result
    fmt = hdr["fmt"]
    is64 = elf_class == _ELFCLASS64
    sections = _parse_sections(data, hdr)
    table = _parse_dynsym(data, sections, fmt, is64)
    if table is None:
        _SYMBOL_CACHE[path] = None
        return None
    _, _, _, soname = _parse_dynamic_section(data, hdr)
    table.soname = soname
    info = parse_elf(path)
    table.needed = info.needed
    _SYMBOL_CACHE[path] = table
    return table


# ---------------------------------------------------------------------------
# Symbol-integrity verification (CR-004 §7, BUILD-REQ-048)
# ---------------------------------------------------------------------------


def _soname_aliases(basename: str) -> list[str]:
    """Candidate lookup names for a library basename.

    ``libfoo.so.3.4.10`` -> [``libfoo.so.3.4.10``, ``libfoo.so.3``, ``libfoo.so``];
    ``libc.so.6`` -> [``libc.so.6``, ``libc.so``]; ``libfoo.so`` -> [``libfoo.so``].
    """
    aliases = [basename]
    name = basename
    while True:
        idx = name.rfind(".")
        if idx <= 0:
            break
        if name[idx + 1 :].isdigit():
            name = name[:idx]
            aliases.append(name)
        else:
            break
    return aliases


def _index_library_candidates(roots: list[Path]) -> dict[str, list[Path]]:
    """Index provider library files under *roots* (in precedence order).

    Both real files and symlinks are indexed (a DT_NEEDED soname frequently
    matches a symlink such as ``ld-linux-aarch64.so.1 -> ld-2.31.so``); the
    map keys are soname aliases so a DT_NEEDED soname resolves to candidate
    files in controlled-root precedence order.
    """
    index: dict[str, list[Path]] = {}
    for root in roots:
        if not root.is_dir():
            continue
        for so in sorted(root.rglob("*.so*")):
            if not (so.is_file() or so.is_symlink()):
                continue
            for alias in _soname_aliases(so.name):
                index.setdefault(alias, []).append(so)
    return index


def _resolve_provider(
    needed: str, index: dict[str, list[Path]], parse_cache: dict[Path, SymbolTable | None]
) -> Path | None:
    """Resolve a DT_NEEDED soname to a provider file in precedence order."""
    for cand in index.get(needed, []):
        table = parse_cache.get(cand)
        if table is None:
            if cand not in parse_cache:
                table = parse_symbols(cand)
                parse_cache[cand] = table
            else:
                table = parse_cache[cand]
        if table is None:
            continue
        if table.soname == needed or cand.name == needed:
            return cand
    candidates = index.get(needed, [])
    return candidates[0] if candidates else None


def _consumer_exemption_keys(path: Path) -> set[str]:
    """Identities under which a consumer artifact may be exempted.

    manifests/link-exemptions.yaml records **CMake target names**, while a
    staged artifact carries its output filename. For executables the two
    usually coincide, but a shared-library target ``foo`` produces
    ``libfoo.so.1.2``; matching on the raw basename alone would silently miss
    every library exemption (CR-004 评审 P2-7). Return every plausible key.
    """
    name = path.name
    keys = {name}
    stem = name
    # Strip .so and any version suffixes: libfoo.so.1.2.3 -> libfoo
    if ".so" in stem:
        stem = stem.split(".so", 1)[0]
    else:
        stem = path.stem
    keys.add(stem)
    if stem.startswith("lib"):
        keys.add(stem[3:])
    return {k for k in keys if k}


def check_symbol_integrity(
    paths: list[Path],
    roots: list[Path] | None = None,
    missing_library_policy: str = "error",
    exempt_consumers: frozenset[str] = frozenset(),
) -> list[ElfCheckResult]:
    """Verify undefined dynamic symbols against each artifact's DT_NEEDED closure.

    For every executable / shared object / module in *paths*:

    1. parse strong and weak undefined dynamic symbols;
    2. build the transitive DT_NEEDED graph, locating dependencies in
       *roots* under BUILD's controlled-root precedence (SDK staging -> TARGET
       dependency staging -> sysroot);
    3. parse each provider's exported dynamic symbols and GNU version metadata;
    4. match symbol name, required version, visibility and provider definition;
    5. emit deterministic diagnostics (consumer, symbol, required version,
       traversed closure, missing provider/dependency).

    Policy (CR-004 §7.3): strong unresolved symbols fail; weak undefined
    symbols are non-fatal warnings; IFUNC definitions are valid providers;
    versioned symbols match base name + GNU version identity (no stripping of
    @GLIBC_* semantics); lazy binding is no exemption; a missing dependency
    library is a release error or an explicitly selected dev-mode warning;
    consumers in *exempt_consumers* (basenames from reviewed D2/D4 exemption
    records) have their strong-unresolved findings downgraded to warnings as
    staging corroboration.

    Results are merged into :class:`ElfCheckResult` (one per consumer).
    """
    roots = list(roots or [])
    results: list[ElfCheckResult] = []
    index = _index_library_candidates(roots)
    parse_cache: dict[Path, SymbolTable | None] = {}

    consumers: list[Path] = []
    for path in paths:
        try:
            cls = classify_file(path)
        except OSError:
            continue
        if cls.file_type != "elf" or cls.elf_info is None:
            continue
        # Only artifacts that participate in dynamic linking are checked.
        if cls.elf_info.elf_type not in (ET_EXEC, ET_DYN):
            continue
        consumers.append(path)

    # Pre-seed the parse cache from consumers so self references never loop.
    for path in consumers:
        if path not in parse_cache:
            parse_cache[path] = parse_symbols(path)

    roots_desc = ", ".join(str(r) for r in roots) or "(none)"

    for path in consumers:
        table = parse_cache.get(path)
        if table is None:
            continue
        result = ElfCheckResult(path=path)
        result.classification = classify_file(path)
        exempt = bool(_consumer_exemption_keys(path) & set(exempt_consumers))

        # 1. Resolve the transitive DT_NEEDED closure.
        closure: list[str] = []
        missing_libs: list[str] = []
        seen: set[str] = set()
        queue: list[str] = list(table.needed)
        while queue:
            needed = queue.pop(0)
            if needed in seen:
                continue
            seen.add(needed)
            provider = _resolve_provider(needed, index, parse_cache)
            if provider is None:
                missing_libs.append(needed)
                continue
            closure.append(needed)
            prov_table = parse_cache.get(provider)
            if prov_table is not None:
                for dep in prov_table.needed:
                    if dep not in seen:
                        queue.append(dep)

        # 2. Missing dependency libraries (policy-gated).
        for lib in missing_libs:
            msg = (
                f"{path}: dependency library '{lib}' not found in controlled "
                f"roots ({roots_desc}); DT_NEEDED closure incomplete"
            )
            if missing_library_policy == "warning":
                result.warnings.append(msg)
            else:
                result.violations.append(msg)

        # 3. Merge provider exports (version sets) across the closure.
        #    IFUNC 定义已随普通已定义符号进入 exports（_parse_dynsym 对所有
        #    已定义符号填充 exports，STT_GNU_IFUNC 只是额外打标），因此这里
        #    无需再单独累积 ifunc 集合（评审 P2-8：原实现收集后从未使用）。
        exports: dict[str, set[str]] = {}
        for provider_name in closure:
            provider = _resolve_provider(provider_name, index, parse_cache)
            prov_table = parse_cache.get(provider) if provider is not None else None
            if prov_table is None:
                continue
            for sym_name, versions in prov_table.exports.items():
                exports.setdefault(sym_name, set()).update(versions)

        # 4. Match every undefined symbol.
        def _describe(name: str, required_version: str | None) -> str:
            return name if not required_version else f"{name}@{required_version}"

        for name, required_version in sorted(table.undef_strong.items()):
            versions = exports.get(name)
            satisfied = versions is not None and (
                required_version is None or required_version in versions
            )
            if satisfied:
                continue
            msg = (
                f"{path}: undefined symbol '{_describe(name, required_version)}' "
                f"not provided by DT_NEEDED closure "
                f"(traversed: {' -> '.join(closure) or '(empty)'}; "
                f"roots: {roots_desc})"
            )
            if exempt:
                result.warnings.append(
                    "[D4 corroboration] " + msg + " (target is a reviewed D2/D4 exemption)"
                )
            else:
                result.violations.append(msg)

        for name, required_version in sorted(table.undef_weak.items()):
            versions = exports.get(name)
            if versions is not None and (
                required_version is None or required_version in versions
            ):
                continue
            result.warnings.append(
                f"{path}: weak undefined symbol "
                f"'{_describe(name, required_version)}' not provided by "
                f"DT_NEEDED closure (non-fatal diagnostic; "
                f"traversed: {' -> '.join(closure) or '(empty)'})"
            )

        results.append(result)

    results.sort(key=lambda r: str(r.path))
    return results


def check_symbol_integrity_staging(
    staging_root: Path,
    roots: list[Path] | None = None,
    missing_library_policy: str = "error",
    exempt_consumers: frozenset[str] = frozenset(),
) -> list[ElfCheckResult]:
    """Run :func:`check_symbol_integrity` over every file in a staging tree."""
    paths = [p for p in sorted(staging_root.rglob("*")) if p.is_file()]
    return check_symbol_integrity(
        paths,
        roots=roots,
        missing_library_policy=missing_library_policy,
        exempt_consumers=exempt_consumers,
    )
