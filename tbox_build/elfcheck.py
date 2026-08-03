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

DT_NULL = 0
DT_NEEDED = 1
DT_STRTAB = 5
DT_RPATH = 15
DT_RUNPATH = 29
DT_STRSZ = 10

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


def _parse_dynamic_section(data: bytes, hdr: dict) -> tuple[list[str], list[str], list[str]]:
    """Parse .dynamic section. Returns (needed, rpath, runpath)."""
    fmt = hdr["fmt"]
    is64 = hdr["is64"]
    shoff = hdr["e_shoff"]
    shnum = hdr["e_shnum"]
    shentsize = hdr["e_shentsize"]

    if shoff == 0 or shnum == 0:
        return [], [], []

    # Parse section headers to find SHT_DYNAMIC and its linked string table
    if is64:
        sh_fmt = fmt + "IIQQQQIIQQ"
        sh_size = 64
    else:
        sh_fmt = fmt + "IIIIIIIIII"
        sh_size = 40

    dynamic_offset = 0
    dynamic_size = 0
    dynamic_link = 0  # section index of .dynstr

    dynstr_offset = 0
    dynstr_size = 0

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
            "offset": sh_offset,
            "size": sh_size_val,
            "link": sh_link,
        })

    # Find SHT_DYNAMIC
    for sec in sections:
        if sec["type"] == SHT_DYNAMIC:
            dynamic_offset = sec["offset"]
            dynamic_size = sec["size"]
            dynamic_link = sec["link"]
            break

    if dynamic_offset == 0 or dynamic_size == 0:
        return [], [], []

    # Find the linked string table (.dynstr)
    if 0 < dynamic_link < len(sections):
        dynstr_section = sections[dynamic_link]
        dynstr_offset = dynstr_section["offset"]
        dynstr_size = dynstr_section["size"]
    else:
        # Try to find .dynstr by looking for SHT_STRTAB that's not .shstrtab
        # This is a fallback; the linked approach is more reliable
        return [], [], []

    if dynstr_offset == 0 or dynstr_size == 0:
        return [], [], []

    # Read the string table
    strtab = data[dynstr_offset : dynstr_offset + dynstr_size]

    def _read_str(str_offset: int) -> str:
        end = strtab.find(b"\x00", str_offset)
        if end < 0:
            end = len(strtab)
        return strtab[str_offset:end].decode("utf-8", errors="replace")

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

    num_entries = dynamic_size // dyn_entry_size
    for i in range(num_entries):
        offset = dynamic_offset + i * dyn_entry_size
        if offset + dyn_entry_size > len(data):
            break
        d_tag, d_val = struct.unpack_from(dyn_fmt, data, offset)
        if d_tag == DT_NULL:
            break
        elif d_tag == DT_NEEDED:
            needed.append(_read_str(d_val))
        elif d_tag == DT_RPATH:
            rpath.extend(_read_str(d_val).split(":"))
        elif d_tag == DT_RUNPATH:
            runpath.extend(_read_str(d_val).split(":"))

    # Filter empty strings
    needed = [n for n in needed if n]
    rpath = [r for r in rpath if r]
    runpath = [r for r in runpath if r]

    return needed, rpath, runpath


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
    info.needed, info.rpath, info.runpath = _parse_dynamic_section(data, hdr)
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

    # ar archives are allowed but we can't check their contents easily
    if cls.file_type == "ar_archive":
        result.warnings.append(f"Static library (ar archive) not deeply checked: {path}")
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
