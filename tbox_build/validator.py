"""Pre-build manifest validation for TBOX Build.

Performs filesystem-dependent and cross-reference checks that the
schema validator and pure-graph analyser cannot cover:

  * service_dependencies: self-dependency, missing service, cycles;
  * target_dependencies: must hit a lock entry;
  * kind: library services must not declare runtime units / health /
    smoke / after ordering;
  * source_dir: resolved path exists, contains a CMake project and stays
    within the approved workspace;
  * systemd unit reference consistency (``after`` -> declared units);
  * health/smoke script existence in service repositories;
  * full manifest validation entry point.
"""

from __future__ import annotations

import os
import re
import stat
from pathlib import Path

from .errors import ValidationFailure
from .graph import DependencyGraph
from .manifest import ServiceManifest, Project, DependencyLock

# Well-known systemd targets that services may legitimately depend on
# without being declared by another TBOX service.
_SYSTEMD_EXTERNAL_TARGETS = frozenset({
    "basic.target",
    "default.target",
    "multi-user.target",
    "network.target",
    "network-online.target",
    "sockets.target",
    "sysinit.target",
    "systemd-journald.service",
})


def validate_service_dependencies(manifest: ServiceManifest) -> None:
    """Check service_dependencies: no self-dep, all hit declared services."""
    violations: list[str] = []
    for svc in manifest:
        for dep in svc.build.service_dependencies:
            if dep == svc.id:
                violations.append(
                    f"Service '{svc.id}' must not depend on itself in "
                    f"service_dependencies"
                )
            elif dep not in manifest:
                violations.append(
                    f"Service '{svc.id}' has missing service dependency "
                    f"'{dep}' (not declared in services)"
                )
    if violations:
        raise ValidationFailure(
            f"service_dependencies validation failed ({len(violations)} violation(s))",
            violations,
        )


def validate_target_dependencies(
    manifest: ServiceManifest, lock: DependencyLock
) -> None:
    """Check target_dependencies: each must hit a lock entry; must not be a service id."""
    lock_names = lock.dependency_names()
    service_ids = set(manifest.services)
    violations: list[str] = []
    for svc in manifest:
        for dep in svc.build.target_dependencies:
            if dep in service_ids:
                violations.append(
                    f"Service '{svc.id}' target_dependencies entry '{dep}' is a "
                    f"service id; use service_dependencies for service ordering"
                )
            elif dep not in lock_names:
                violations.append(
                    f"Service '{svc.id}' has missing target dependency '{dep}' "
                    f"(not declared in dependencies/lock.yaml)"
                )
    if violations:
        raise ValidationFailure(
            f"target_dependencies validation failed ({len(violations)} violation(s))",
            violations,
        )


def validate_library_kind(manifest: ServiceManifest) -> None:
    """kind: library services must not declare daemon/runtime artefacts."""
    violations: list[str] = []
    for svc in manifest:
        if not svc.is_library:
            continue
        if svc.runtime.systemd_units:
            violations.append(
                f"Service '{svc.id}' is kind: library but declares "
                f"runtime.systemd_units (libraries have no daemon units)"
            )
        if svc.runtime.after:
            violations.append(
                f"Service '{svc.id}' is kind: library but declares "
                f"runtime.after ordering (libraries have no daemon ordering)"
            )
        if svc.runtime.health_check:
            violations.append(
                f"Service '{svc.id}' is kind: library but declares "
                f"runtime.health_check (libraries have no daemon health)"
            )
        if svc.runtime.smoke_test:
            violations.append(
                f"Service '{svc.id}' is kind: library but declares "
                f"runtime.smoke_test (libraries have no daemon smoke test)"
            )
    if violations:
        raise ValidationFailure(
            f"library kind validation failed ({len(violations)} violation(s))",
            violations,
        )


def validate_source_dirs(manifest: ServiceManifest, project_root: Path) -> None:
    """Check source_dir resolves within the workspace and has a CMake project.

    The approved workspace boundary is the parent of the BUILD project root,
    which permits sibling repositories such as ``../iov-vehicle-tbox-framework``.
    """
    workspace_root = project_root.parent.resolve()
    violations: list[str] = []
    for svc in manifest:
        source_dir = project_root / svc.effective_source_dir
        try:
            resolved = source_dir.resolve()
        except OSError:
            violations.append(
                f"Service '{svc.id}' source_dir '{svc.effective_source_dir}' "
                f"could not be resolved"
            )
            continue
        # Stay within approved workspace (no escaping via ../../..)
        try:
            resolved.relative_to(workspace_root)
        except ValueError:
            violations.append(
                f"Service '{svc.id}' source_dir '{svc.effective_source_dir}' "
                f"resolves outside the approved workspace ({workspace_root})"
            )
            continue
        if not resolved.is_dir():
            violations.append(
                f"Service '{svc.id}' source_dir '{svc.effective_source_dir}' "
                f"does not exist (resolved: {resolved})"
            )
            continue
        if not (resolved / "CMakeLists.txt").is_file():
            violations.append(
                f"Service '{svc.id}' source_dir '{svc.effective_source_dir}' "
                f"has no CMakeLists.txt (not a CMake project)"
            )
    if violations:
        raise ValidationFailure(
            f"source_dir validation failed ({len(violations)} violation(s))",
            violations,
        )


def validate_systemd_references(manifest: ServiceManifest) -> None:
    """Ensure every ``after`` entry references a declared or well-known unit."""
    declared_units: set[str] = set()
    for svc in manifest:
        declared_units.update(svc.runtime.systemd_units)

    violations: list[str] = []
    for svc in manifest:
        for after_unit in svc.runtime.after:
            if after_unit in declared_units:
                continue
            if after_unit in _SYSTEMD_EXTERNAL_TARGETS:
                continue
            violations.append(
                f"Service '{svc.id}' references unknown systemd unit "
                f"'{after_unit}' in runtime.after"
            )
    if violations:
        raise ValidationFailure(
            f"systemd unit reference validation failed ({len(violations)} violation(s))",
            violations,
        )


def validate_health_smoke(manifest: ServiceManifest, project_root: Path) -> None:
    """Check that declared health_check / smoke_test scripts exist on disk."""
    violations: list[str] = []
    for svc in manifest:
        svc_root = project_root / svc.effective_source_dir
        for check_type, rel_path in (
            ("health_check", svc.runtime.health_check),
            ("smoke_test", svc.runtime.smoke_test),
        ):
            if rel_path is None:
                continue
            full_path = svc_root / rel_path
            if not full_path.is_file():
                violations.append(
                    f"Service '{svc.id}' {check_type} script not found: {full_path}"
                )
                continue
            st = full_path.stat()
            if not (st.st_mode & (stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)):
                violations.append(
                    f"Service '{svc.id}' {check_type} script not executable: {full_path}"
                )
    if violations:
        raise ValidationFailure(
            f"health/smoke script validation failed ({len(violations)} violation(s))",
            violations,
        )


def validate_config_validation(manifest: ServiceManifest) -> None:
    """Cross-reference config_validation against config_paths (CR-003 §5).

    Ensures ``config_validation.target_path`` is a member of the service's
    declared ``runtime.config_paths``. Schema/default_source path escape is
    checked at schema-check time (relative to the service source root).
    """
    violations: list[str] = []
    for svc in manifest:
        cv = svc.runtime.config_validation
        if cv is None:
            continue
        if cv.target_path not in svc.runtime.config_paths:
            violations.append(
                f"Service '{svc.id}' config_validation.target_path "
                f"'{cv.target_path}' must be a member of runtime.config_paths "
                f"{svc.runtime.config_paths}"
            )
    if violations:
        raise ValidationFailure(
            f"config_validation cross-reference failed ({len(violations)} violation(s))",
            violations,
        )


def validate_config_deployment_coverage(
    manifest: ServiceManifest, project_root: Path
) -> list[str]:
    """Check config-deployment.yaml covers all service config_paths (CR-003 §7).

    Every ``runtime.config_paths`` entry under ``/etc/tbox/**`` SHOULD be
    matched by a rule in ``manifests/config-deployment.yaml``. Unmatched
    paths default to ``preserve`` (§7.1), which may silently prevent
    release-managed configs from being replaced on the device. This check
    returns warnings (not errors) for unmatched paths so the operator is
    alerted to potential deploy-policy gaps.
    """
    warnings: list[str] = []
    try:
        project = Project(project_root)
        cdm = project.load_config_deployment_manifest()
    except Exception:
        # Not a valid project root or no config-deployment manifest; skip.
        return warnings

    for svc in manifest:
        for cp in svc.runtime.config_paths:
            if not cp.startswith("/etc/tbox/"):
                continue
            rule = cdm.match("orin", cp)
            if rule is None:
                warnings.append(
                    f"Service '{svc.id}' config_path '{cp}' is not matched "
                    f"by any rule in config-deployment.yaml (defaults to "
                    f"preserve; add an explicit rule if it should be replaced)"
                )
    return warnings


# ---------------------------------------------------------------------------
# D1 - Manifest coverage gate (CR-004 §4, BUILD-REQ-045)
# ---------------------------------------------------------------------------


def _cmake_tokenize(text: str) -> list[str]:
    """Tokenize a CMake argument list conservatively.

    Splits on whitespace and ``(),`` separators, keeps quoted strings as
    single tokens, and strips ``#`` comments. This is intentionally simple:
    it only needs to be robust enough to find ``COMPONENT <name>`` tokens
    inside ``install(...)`` bodies, including multiline calls.
    """
    tokens: list[str] = []
    i = 0
    n = len(text)
    while i < n:
        c = text[i]
        if c == "#":
            while i < n and text[i] != "\n":
                i += 1
            continue
        if c.isspace() or c in "(),":
            i += 1
            continue
        if c == '"':
            j = i + 1
            while j < n and text[j] != '"':
                j += 1
            tokens.append(text[i + 1 : j])
            i = j + 1
            continue
        j = i
        while j < n and not (text[j].isspace() or text[j] in '(),"#'):
            j += 1
        tokens.append(text[i:j])
        i = j
    return tokens


def _extract_cmake_install_components(cmake_text: str) -> set[str]:
    """Extract COMPONENT names from every ``install(...)`` call in CMake text.

    Uses a balanced-paren scanner followed by conservative tokenization, so
    multiline ``install(FILES ... COMPONENT <svc>-runtime)`` declarations are
    handled correctly (a raw single-line regex is insufficient, CR-004 §4.3).
    """
    components: set[str] = set()
    i = 0
    n = len(cmake_text)
    while i < n:
        c = cmake_text[i]
        if c == "#":
            while i < n and cmake_text[i] != "\n":
                i += 1
            continue
        if c.isalpha() or c == "_":
            j = i
            while j < n and (cmake_text[j].isalnum() or cmake_text[j] == "_"):
                j += 1
            word = cmake_text[i:j]
            if word == "install":
                k = j
                while k < n and (cmake_text[k].isspace() or cmake_text[k] == "("):
                    if cmake_text[k] == "(":
                        break
                    k += 1
                if k < n and cmake_text[k] == "(":
                    depth = 0
                    start = k
                    while k < n:
                        if cmake_text[k] == "(":
                            depth += 1
                        elif cmake_text[k] == ")":
                            depth -= 1
                            if depth == 0:
                                break
                        k += 1
                    body = cmake_text[start + 1 : k]
                    tokens = _cmake_tokenize(body)
                    for idx, tok in enumerate(tokens):
                        if tok == "COMPONENT" and idx + 1 < len(tokens):
                            components.add(tokens[idx + 1])
            i = j
        else:
            i += 1
    return components


def _find_unit_evidence(repo_root: Path) -> list[Path]:
    """Find systemd unit evidence: packaging/systemd/*.service or systemd/*.service."""
    units: list[Path] = []
    for pattern in ("packaging/systemd/*.service", "systemd/*.service"):
        units.extend(sorted(repo_root.glob(pattern)))
    return units


def _repo_runtime_components(repo_root: Path) -> set[str]:
    """Return install COMPONENT names ending in ``-runtime`` declared by a repo."""
    cmake = repo_root / "CMakeLists.txt"
    if not cmake.is_file():
        return set()
    try:
        text = cmake.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return set()
    return {c for c in _extract_cmake_install_components(text) if c.endswith("-runtime")}


def validate_manifest_coverage(
    manifest: ServiceManifest, project_root: Path
) -> list[str]:
    """D1: detect services omitted from BUILD metadata before publication.

    Compares the explicitly declared repository inventory with
    ``manifests/services.yaml`` registration. For each declared repository:

    * unit evidence (packaging/systemd/*.service or systemd/*.service) and
      runtime-install evidence (``install(... COMPONENT <svc>-runtime)``)
      both present but not registered/allowlisted -> **error**;
    * only one signal present but not registered -> **warning**;
    * registered service or valid allowlist entry -> pass;
    * expired allowlist entry -> **error**.

    Sibling ``../iov-vehicle-tbox-*`` discovery is diagnostic-only: an
    undeclared candidate with unit/install evidence emits a warning and must
    not silently expand the release contract (CR-004 §4.2).

    Returns a list of non-fatal warnings; raises ValidationFailure on errors.
    """
    warnings: list[str] = []
    try:
        project = Project(project_root)
        inventory = project.load_repository_inventory()
    except Exception as exc:
        # 不静默：gate 因加载失败而未执行时必须留痕，否则一个畸形清单
        # 就能无声地关掉覆盖率检查（CR-004 评审 P2-10）。
        warnings.append(
            f"D1 manifest coverage check could not run "
            f"({type(exc).__name__}: {exc}); gate skipped"
        )
        return warnings
    if inventory is None or not inventory.repositories:
        warnings.append(
            "no repository-inventory.yaml; D1 manifest coverage check skipped"
        )
        return warnings

    registered_dirs: set[Path] = set()
    for svc in manifest:
        try:
            registered_dirs.add((project_root / svc.effective_source_dir).resolve())
        except OSError:
            pass

    declared_dirs: set[Path] = set()
    errors: list[str] = []
    for repo in inventory:
        repo_root = project_root / repo.path
        try:
            resolved = repo_root.resolve()
        except OSError:
            errors.append(
                f"repository '{repo.id}' path '{repo.path}' could not be resolved"
            )
            continue
        declared_dirs.add(resolved)
        if not resolved.is_dir():
            errors.append(
                f"repository '{repo.id}' declared path does not exist: {resolved}"
            )
            continue

        units = _find_unit_evidence(resolved)
        runtime_components = _repo_runtime_components(resolved)
        has_unit = bool(units)
        has_runtime = bool(runtime_components)

        if resolved in registered_dirs:
            continue  # registered service: pass

        allowlist_entry = inventory.find_allowlist(repo.id)
        if allowlist_entry is not None:
            if allowlist_entry.is_expired():
                errors.append(
                    f"repository '{repo.id}' coverage allowlist entry has expired "
                    f"(owner={allowlist_entry.owner}, "
                    f"review_condition={allowlist_entry.review_condition}); "
                    f"renew or register the repository"
                )
            else:
                warnings.append(
                    f"repository '{repo.id}' covered by coverage allowlist "
                    f"(owner={allowlist_entry.owner}, reason={allowlist_entry.reason!r}, "
                    f"review={allowlist_entry.review_condition})"
                )
            continue

        if has_unit and has_runtime:
            errors.append(
                f"repository '{repo.id}' ships systemd unit(s) "
                f"{[u.name for u in units]} and runtime install component(s) "
                f"{sorted(runtime_components)} but is neither registered in "
                f"services.yaml nor covered by an allowlist entry"
            )
        elif has_unit or has_runtime:
            warnings.append(
                f"repository '{repo.id}' has "
                + ("systemd unit evidence" if has_unit else "runtime-install evidence")
                + " but is not registered in services.yaml; single-signal "
                "partial evidence (register the service or add an allowlist "
                "entry)"
            )

    # Sibling discovery: diagnostic only, never expands the release contract.
    parent = project_root.parent
    for candidate in sorted(parent.glob("iov-vehicle-tbox-*")):
        if not candidate.is_dir():
            continue
        try:
            resolved = candidate.resolve()
        except OSError:
            continue
        if resolved in registered_dirs or resolved in declared_dirs:
            continue
        units = _find_unit_evidence(resolved)
        runtime_components = _repo_runtime_components(resolved)
        if units or runtime_components:
            warnings.append(
                f"sibling repository '{candidate.name}' has "
                + ("systemd unit evidence" if units else "install-component evidence")
                + " but is not declared in repository-inventory.yaml "
                "(diagnostic only; not part of the release contract)"
            )

    if errors:
        raise ValidationFailure(
            f"manifest coverage validation failed ({len(errors)} violation(s))",
            errors,
        )
    return warnings


# ---------------------------------------------------------------------------
# D3 - Retirement manifest validation (CR-004 §6.2, BUILD-REQ-047)
# ---------------------------------------------------------------------------


# Approved roots for retired paths, in two tiers (CR-004 评审 P0-1 收窄)。
#
# TIER 1 —— 遗留单体前缀，整棵树可退役：
#   /opt/tbox 不由任何现行 install 规则产出，其下内容全部属于历史遗留。
# TIER 2 —— systemd 单元目录，basename 必须是合法单元文件名。
# TIER 3 —— 系统二进制/库目录，basename 必须带 tbox/hwyz 作用域标识。
#   直接放开 /usr/lib、/lib、/usr/bin 等于允许 rm -rf /usr/lib/libc.so.6；
#   前缀匹配无法表达 docstring 所称的 "explicitly named"，故改为按 basename 约束。
RETIREMENT_TIER1_ROOTS = ("/opt/tbox",)

RETIREMENT_TIER2_UNIT_DIRS = (
    "/usr/lib/systemd/system",
    "/lib/systemd/system",
    "/etc/systemd/system",
)

RETIREMENT_TIER3_SCOPED_DIRS = (
    "/usr/bin",
    "/usr/sbin",
    "/usr/lib",
    "/usr/local/bin",
    "/usr/local/lib",
    "/lib",
)

# TIER 3 basename 必须命中其一，确保只能退役 TBOX 自己的历史产物。
RETIREMENT_TIER3_BASENAME_RE = re.compile(r"(tbox|hwyz)", re.IGNORECASE)

# 合法 systemd 单元名（与 retirement.schema.yaml 的 pattern 一致）。
RETIREMENT_UNIT_NAME_RE = re.compile(
    r"^[A-Za-z0-9@._-]+\.(service|socket|timer|target|path|mount|slice)$"
)

# 退役路径字符集：绝对路径，分段仅允许 [A-Za-z0-9._@+-]。
# 禁止空白与全部 shell 元字符，使路径进入远端 shell 后不可能被重新解析。
RETIREMENT_PATH_RE = re.compile(r"^/[A-Za-z0-9._@+-]+(/[A-Za-z0-9._@+-]+)*$")

# 向后兼容：旧常量名保留为两层根的并集，仅供外部只读引用。
RETIREMENT_ALLOWED_ROOTS = (
    RETIREMENT_TIER1_ROOTS + RETIREMENT_TIER2_UNIT_DIRS + RETIREMENT_TIER3_SCOPED_DIRS
)

# Device-managed / preserve protected paths that retirement must never touch.
RETIREMENT_PROTECTED_PATHS = (
    "/etc/tbox/device.yaml",
    "/etc/tbox/credentials",
)


def _retirement_unit_error(name: str) -> str | None:
    """Return an error string for an unsafe retired unit name, else None.

    单元名会直接进入远端 ``systemctl`` 命令，必须在此拦住 shell 元字符
    （CR-004 评审 P0-1：schema 原先只有 minLength，`prov.service; reboot`
    可一路到达设备）。
    """
    if not RETIREMENT_UNIT_NAME_RE.match(name):
        return (
            f"retired unit name '{name}' is not a valid systemd unit name "
            f"(expected [A-Za-z0-9@._-]+ with a .service/.socket/.timer/"
            f".target/.path/.mount/.slice suffix; shell metacharacters and "
            f"whitespace are forbidden)"
        )
    return None


def _retirement_path_error(
    path: str, cdm: Any, retired_units: frozenset[str] | set[str] = frozenset()
) -> str | None:
    """Return an error string for an unsafe retired path, else None.

    *retired_units* is the set of unit names declared in ``retired_units`` for
    the same platform. A path under a systemd unit directory is only approved
    when its basename is one of them, so removing an unrelated system unit
    file (e.g. ``sshd.service``) cannot be declared.
    """
    if not path.startswith("/"):
        return f"retired path must be absolute: '{path}'"
    if any(ch in path for ch in "*?[]"):
        return f"retired path uses a wildcard (forbidden): '{path}'"
    # 字符集校验先于一切语义判断：路径会被插入远端 shell 命令，含空白或
    # shell 元字符时既会被分词，也可能被重新解析成额外命令。
    if not RETIREMENT_PATH_RE.match(path):
        return (
            f"retired path '{path}' contains characters outside the allowed "
            f"set (segments must match [A-Za-z0-9._@+-]; whitespace and shell "
            f"metacharacters such as ; $ ` & | ( ) < > quotes and backslash "
            f"are forbidden)"
        )
    parts = path.split("/")
    if ".." in parts:
        return f"retired path uses '..' traversal: '{path}'"
    # Normalize: reject repeated slashes / trailing slash forms that could
    # alias a protected path.
    normalized = "/" + "/".join(p for p in parts if p)
    for protected in RETIREMENT_PROTECTED_PATHS:
        if normalized == protected or normalized.startswith(protected + "/"):
            return f"retired path intersects protected path '{protected}': '{path}'"

    basename = normalized.rsplit("/", 1)[-1]

    def _under(root: str) -> bool:
        return normalized == root or normalized.startswith(root + "/")

    approved = False
    if any(_under(root) for root in RETIREMENT_TIER1_ROOTS):
        # TIER 1: 遗留前缀整棵树可退役。
        approved = True
    elif any(_under(d) for d in RETIREMENT_TIER2_UNIT_DIRS):
        # TIER 2: 单元目录下只能退役具名单元文件，不能退役目录本身，
        # 且该单元必须已在同平台 retired_units 中声明 —— 否则就能借
        # 路径退役删掉任意系统单元文件（如 sshd.service）。
        if any(normalized == d for d in RETIREMENT_TIER2_UNIT_DIRS):
            return (
                f"retired path '{path}' is a systemd unit directory itself; "
                f"declare individual unit files instead"
            )
        if not RETIREMENT_UNIT_NAME_RE.match(basename):
            return (
                f"retired path '{path}' is under a systemd unit directory but "
                f"'{basename}' is not a valid unit file name"
            )
        if basename not in set(retired_units):
            return (
                f"retired path '{path}' removes unit file '{basename}' which is "
                f"not declared in retired_units for this platform; declare the "
                f"unit first so the retirement is explicit and auditable"
            )
        approved = True
    elif any(_under(d) for d in RETIREMENT_TIER3_SCOPED_DIRS):
        # TIER 3: 系统二进制/库目录，只允许 TBOX 自己的历史产物。
        if any(normalized == d for d in RETIREMENT_TIER3_SCOPED_DIRS):
            return (
                f"retired path '{path}' is a system directory itself; only "
                f"explicitly named TBOX artifacts may be retired"
            )
        if not RETIREMENT_TIER3_BASENAME_RE.search(basename):
            return (
                f"retired path '{path}' is under system directory and its "
                f"basename '{basename}' carries no TBOX scope marker "
                f"(tbox/hwyz); retiring unrelated system files is forbidden"
            )
        approved = True

    if not approved:
        return (
            f"retired path '{path}' is outside approved roots "
            f"(tier1: {', '.join(RETIREMENT_TIER1_ROOTS)}; "
            f"tier2: named unit files under {', '.join(RETIREMENT_TIER2_UNIT_DIRS)}; "
            f"tier3: tbox/hwyz-scoped basenames under "
            f"{', '.join(RETIREMENT_TIER3_SCOPED_DIRS)})"
        )
    # Intersections with device-managed or preserve config policy.
    if cdm is not None:
        rule = cdm.match("orin", normalized)
        if rule is not None and (
            rule.category == "device-managed" or rule.deploy_policy == "preserve"
        ):
            return (
                f"retired path '{path}' intersects config-deployment policy "
                f"({rule.category}/{rule.deploy_policy}, owner={rule.owner}); "
                f"device-managed/preserve paths must not be retired"
            )
    return None


def validate_retirement_manifest(
    manifest: ServiceManifest, project_root: Path
) -> list[str]:
    """D3: validate the retirement manifest (CR-004 §6.2, BUILD-REQ-047).

    Rejects active-unit conflicts (retired unit is also a declared active
    service unit), duplicate entries, wildcards/root escape, paths outside
    approved roots, and intersections with device-managed or preserve
    configuration policy (including /etc/tbox/device.yaml and
    /etc/tbox/credentials/**).

    Returns warnings; raises ValidationFailure on errors.
    """
    warnings: list[str] = []
    try:
        project = Project(project_root)
        rm = project.load_retirement_manifest()
    except Exception as exc:
        # 不静默：退役清单直接驱动设备上的 disable/rm，加载失败必须可见
        # （CR-004 评审 P2-10）。
        warnings.append(
            f"D3 retirement manifest check could not run "
            f"({type(exc).__name__}: {exc}); gate skipped"
        )
        return warnings
    if rm is None:
        return warnings

    active_units: set[str] = set()
    for svc in manifest:
        active_units.update(svc.runtime.systemd_units)

    cdm = project.load_config_deployment_manifest()
    errors: list[str] = []
    for platform, rp in rm.platforms.items():
        seen_units: set[str] = set()
        for unit in rp.retired_units:
            if not unit.name or not unit.owner or not unit.reason:
                errors.append(
                    f"retirement[{platform}] retired_unit requires name, owner "
                    f"and reason: {unit}"
                )
                continue
            if unit.name in seen_units:
                errors.append(
                    f"retirement[{platform}] duplicate retired unit: '{unit.name}'"
                )
            seen_units.add(unit.name)
            unit_err = _retirement_unit_error(unit.name)
            if unit_err is not None:
                errors.append(f"retirement[{platform}] {unit_err}")
            if unit.name in active_units:
                errors.append(
                    f"retirement[{platform}] retired unit '{unit.name}' conflicts "
                    f"with an active service systemd unit"
                )

        seen_paths: set[str] = set()
        for rpath in rp.retired_paths:
            if not rpath.path or not rpath.owner or not rpath.reason:
                errors.append(
                    f"retirement[{platform}] retired_path requires path, owner "
                    f"and reason: {rpath}"
                )
                continue
            if rpath.path in seen_paths:
                errors.append(
                    f"retirement[{platform}] duplicate retired path: '{rpath.path}'"
                )
            seen_paths.add(rpath.path)
            err = _retirement_path_error(rpath.path, cdm, seen_units)
            if err is not None:
                errors.append(f"retirement[{platform}] {err}")

    if errors:
        raise ValidationFailure(
            f"retirement manifest validation failed ({len(errors)} violation(s))",
            errors,
        )
    return warnings


# ---------------------------------------------------------------------------
# D2 - Link-hardening exemption records (CR-004 §5.4, BUILD-REQ-046)
# ---------------------------------------------------------------------------


def validate_link_exemptions(project_root: Path) -> list[str]:
    """D2: validate structured link-hardening exemption records.

    Every record must carry target, owner, reason, symbol_class, risk, scope
    and removal_condition; duplicate targets are rejected. Repository-wide
    silent overrides are forbidden (only named targets may be exempted).

    Returns warnings; raises ValidationFailure on errors.
    """
    warnings: list[str] = []
    try:
        project = Project(project_root)
        exemptions = project.load_link_exemptions()
    except Exception as exc:
        # 不静默：豁免记录是 D2 的治理凭据，加载失败必须可见
        # （CR-004 评审 P2-10）。
        warnings.append(
            f"D2 link-exemption check could not run "
            f"({type(exc).__name__}: {exc}); gate skipped"
        )
        return warnings
    if exemptions is None:
        return warnings

    errors: list[str] = []
    seen: set[str] = set()
    for ex in exemptions.exemptions:
        missing = [
            field
            for field in (
                "target", "owner", "reason", "symbol_class",
                "risk", "scope", "removal_condition",
            )
            if not getattr(ex, field)
        ]
        if missing:
            errors.append(
                f"link exemption for '{ex.target or '(unnamed)'}' is missing "
                f"required field(s): {', '.join(missing)}"
            )
        if ex.target in seen:
            errors.append(f"duplicate link exemption target: '{ex.target}'")
        seen.add(ex.target)

    if errors:
        raise ValidationFailure(
            f"link exemption validation failed ({len(errors)} violation(s))",
            errors,
        )
    return warnings


def validate_all(
    manifest: ServiceManifest, project_root: Path, lock: DependencyLock | None = None
) -> list[str]:
    """Run all pre-build validations.

    Raises :class:`ValidationFailure`, :class:`DependencyError` or
    :class:`CycleError` on failure.  Returns a list of non-fatal warning
    strings.
    """
    warnings: list[str] = []

    if lock is None:
        try:
            project = Project(project_root)
            lock = project.load_dependency_lock()
        except Exception:
            # Not a valid project root (e.g. tmp_path in unit tests); treat
            # as an empty lock so pure-graph validations still run.
            from .manifest import DependencyLock
            lock = DependencyLock(dependencies={})

    # 1. service_dependencies: self-dep, missing
    validate_service_dependencies(manifest)

    # 2. target_dependencies: must hit lock, must not be service id
    validate_target_dependencies(manifest, lock)

    # 3. Dependency graph: cycles (topology uses service_dependencies only)
    graph = DependencyGraph(manifest, lock)
    cycles = graph.detect_cycles()
    if cycles:
        raise ValidationFailure(
            f"Dependency cycles detected ({len(cycles)} cycle(s))",
            [f"{' -> '.join(c)}" for c in cycles],
        )

    # 4. kind: library runtime rules
    validate_library_kind(manifest)

    # 5. source_dir existence / workspace boundary / CMake project
    validate_source_dirs(manifest, project_root)

    # 6. systemd unit references
    validate_systemd_references(manifest)

    # 7. health/smoke script existence
    validate_health_smoke(manifest, project_root)

    # 8. config_validation.target_path in config_paths (CR-003)
    validate_config_validation(manifest)

    # 9. config-deployment coverage of service config_paths (CR-003 §7)
    warnings.extend(validate_config_deployment_coverage(manifest, project_root))

    # 10. D1 manifest coverage gate (CR-004 §4, BUILD-REQ-045)
    warnings.extend(validate_manifest_coverage(manifest, project_root))

    # 11. D3 retirement manifest (CR-004 §6.2, BUILD-REQ-047)
    warnings.extend(validate_retirement_manifest(manifest, project_root))

    # 12. D2 link-hardening exemption records (CR-004 §5.4, BUILD-REQ-046)
    warnings.extend(validate_link_exemptions(project_root))

    return warnings
