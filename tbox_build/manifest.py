"""Manifest loading and data model for TBOX Build.

Loads and parses platform manifest, sysroot manifest, service manifest,
release-set manifest and dependency lock from YAML files into typed
dataclass objects.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from .errors import ManifestError, SchemaValidationError


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


@dataclass
class InstallComponent:
    """A single install component declaration.

    ``name`` is the CMake install component name; ``staging`` is the
    BUILD-managed destination classification (``sdk`` or ``rootfs``).
    The orchestrator computes the physical DESTDIR from ``staging``;
    the component name never encodes the destination.
    """

    name: str
    staging: str  # "sdk" | "rootfs"


@dataclass
class BuildConfig:
    """Build configuration for a single service.

    Canonical (plural) fields only:

      * ``targets`` - CMake build targets, executed in declared order.
      * ``install_components`` - {name, staging} component objects.
      * ``service_dependencies`` - service ids forming the build topology.
      * ``target_dependencies`` - lock dependency names triggering recipes.

    Legacy single-value fields (``target``, ``install_component``) and the
    heterogeneous ``dependencies`` list are accepted by the parser and
    normalised into the canonical fields. ``_legacy_dependencies`` holds
    unresolved legacy ``dependencies`` entries until
    :func:`resolve_legacy_dependencies` classifies them with lock context.
    """

    preset: str
    targets: list[str] = field(default_factory=list)
    install_components: list[InstallComponent] = field(default_factory=list)
    service_dependencies: list[str] = field(default_factory=list)
    target_dependencies: list[str] = field(default_factory=list)
    cmake_cache_variables: dict[str, str] = field(default_factory=dict)
    _legacy_dependencies: list[str] = field(default_factory=list)

    @property
    def has_legacy_dependencies(self) -> bool:
        return bool(self._legacy_dependencies)


@dataclass
class ConfigValidation:
    """Service-declared configuration validation entry (CR-003 §5).

    Either ``schema`` (a JSON Schema path relative to the service source
    root) or ``check_command`` (a controlled command) is declared. The
    ``target_path`` is the on-device path the service reads; it must be a
    member of ``runtime.config_paths``. ``default_source`` is the service
    default template path (relative to the service source root).
    """

    target_path: str
    schema: str | None = None
    default_source: str | None = None
    check_command: str | None = None


@dataclass
class RuntimeConfig:
    """Runtime configuration for a single service."""

    systemd_units: list[str] = field(default_factory=list)
    after: list[str] = field(default_factory=list)
    health_check: str | None = None
    smoke_test: str | None = None
    config_paths: list[str] = field(default_factory=list)
    persistent_paths: list[str] = field(default_factory=list)
    required_devices: list[str] = field(default_factory=list)
    capabilities: list[str] = field(default_factory=list)
    config_validation: ConfigValidation | None = None


@dataclass
class Service:
    """A single TBOX service entry."""

    id: str
    repository: str
    build: BuildConfig
    runtime: RuntimeConfig = field(default_factory=RuntimeConfig)
    revision: str | None = None
    source_dir: str | None = None
    description: str | None = None
    kind: str = "daemon"

    @property
    def effective_source_dir(self) -> str:
        """Source directory relative to project root."""
        return self.source_dir or self.repository

    @property
    def is_library(self) -> bool:
        return self.kind == "library"


@dataclass
class ServiceManifest:
    """Collection of all declared services."""

    services: dict[str, Service]

    def get(self, service_id: str) -> Service | None:
        return self.services.get(service_id)

    def __len__(self) -> int:
        return len(self.services)

    def __contains__(self, service_id: str) -> bool:
        return service_id in self.services

    def __iter__(self):
        return iter(self.services.values())


@dataclass
class ConfigDeployRule:
    """A single deployment-policy rule from config-deployment.yaml (CR-003 §7.1).

    Exactly one of ``path`` (exact) or ``path_glob`` is set. Exact paths take
    priority over globs when matching.
    """

    owner: str
    category: str  # release-managed | device-managed
    deploy_policy: str  # replace | preserve | create_if_missing
    path: str | None = None
    path_glob: str | None = None


@dataclass
class ConfigDeploymentManifest:
    """Versioned configuration deployment policy manifest (CR-003 §7)."""

    version: int
    platforms: dict[str, list[ConfigDeployRule]]

    def rules_for(self, platform: str) -> list[ConfigDeployRule]:
        return self.platforms.get(platform, [])

    def match(self, platform: str, target_path: str) -> ConfigDeployRule | None:
        """Return the highest-priority matching rule for *target_path*.

        Exact ``path`` rules take priority over ``path_glob`` rules. If two
        rules of the same priority match with different policies, that is a
        conflict handled by the caller (this method returns the first match
        within a priority tier for determinism).
        """
        import re as _re
        exact: list[ConfigDeployRule] = []
        globs: list[ConfigDeployRule] = []
        for rule in self.rules_for(platform):
            if rule.path is not None:
                if rule.path == target_path:
                    exact.append(rule)
            elif rule.path_glob is not None:
                if _glob_match(rule.path_glob, target_path):
                    globs.append(rule)
        if exact:
            return exact[0]
        if globs:
            return globs[0]
        return None


@dataclass
class ReleaseSet:
    """A single release set."""

    id: str
    services: list[str]
    description: str | None = None
    platform: str | None = None
    profile: str | None = None


@dataclass
class ReleaseSetManifest:
    """Collection of all declared release sets."""

    release_sets: dict[str, ReleaseSet]

    def get(self, set_id: str) -> ReleaseSet | None:
        return self.release_sets.get(set_id)

    def __len__(self) -> int:
        return len(self.release_sets)

    def __iter__(self):
        return iter(self.release_sets.values())


@dataclass
class PlatformManifest:
    """Platform baseline manifest (orin-platform.yaml)."""

    data: dict[str, Any]

    @property
    def platform(self) -> str:
        return self.data.get("platform", "")

    @property
    def architecture(self) -> str:
        return self.data.get("architecture", "")

    @property
    def rootfs_id(self) -> str:
        return self.data.get("rootfs", {}).get("id", "")

    @property
    def sysroot_path(self) -> str:
        return self.data.get("rootfs", {}).get("repository_path", "")

    @property
    def target_triple(self) -> str:
        return self.data.get("toolchain", {}).get("target_triple", "")

    @property
    def cross_cc(self) -> str:
        return self.data.get("toolchain", {}).get("cross_cc", "")

    @property
    def cross_cxx(self) -> str:
        return self.data.get("toolchain", {}).get("cross_cxx", "")

    @property
    def container_image(self) -> str:
        rbe = self.data.get("reproducible_build_environment", {})
        return rbe.get("image", "")

    @property
    def container_digest(self) -> str:
        rbe = self.data.get("reproducible_build_environment", {})
        return rbe.get("image_manifest_digest", "")

    @property
    def container_os(self) -> str:
        rbe = self.data.get("reproducible_build_environment", {})
        return rbe.get("container_os", "")

    @property
    def sysroot_id(self) -> str:
        return self.data.get("toolchain", {}).get("sysroot", "")


@dataclass
class SysrootManifest:
    """Sysroot manifest (orin-r35.3.1.yaml)."""

    data: dict[str, Any]

    @property
    def id(self) -> str:
        return self.data.get("sysroot", {}).get("id", "")

    @property
    def repository_path(self) -> str:
        return self.data.get("sysroot", {}).get("repository_path", "")

    @property
    def digest(self) -> str:
        return self.data.get("sysroot", {}).get("digest", "")

    @property
    def file_manifest(self) -> str:
        return self.data.get("sysroot", {}).get("file_manifest", "")

    @property
    def import_status(self) -> str:
        return self.data.get("sysroot", {}).get("import_status", "pending")


# ---------------------------------------------------------------------------
# Dependency lock model
# ---------------------------------------------------------------------------


@dataclass
class DependencyPatch:
    """A patch applied to a dependency source tree."""

    file: str
    sha256: str


@dataclass
class DependencyEntry:
    """A single locked TARGET dependency."""

    name: str
    version: str
    source_url: str
    source_sha256: str
    license: str
    boundary: str  # TARGET
    architecture: str  # aarch64
    linkage: str  # static | shared
    patches: list[DependencyPatch] = field(default_factory=list)
    cmake_options: dict[str, str] = field(default_factory=dict)

    @property
    def is_source_pinned(self) -> bool:
        """True when the source SHA-256 has been filled (not a placeholder)."""
        sha = self.source_sha256.strip()
        if not sha:
            return False
        return not sha.upper().startswith("PENDING")

    @property
    def is_static(self) -> bool:
        return self.linkage == "static"


@dataclass
class DependencyLock:
    """Collection of all locked dependencies."""

    dependencies: dict[str, DependencyEntry]
    cache: dict[str, Any] = field(default_factory=dict)

    def get(self, name: str) -> DependencyEntry | None:
        return self.dependencies.get(name)

    def __contains__(self, name: str) -> bool:
        return name in self.dependencies

    def dependency_names(self) -> set[str]:
        return set(self.dependencies)

    def __len__(self) -> int:
        return len(self.dependencies)

    def __iter__(self):
        return iter(self.dependencies.values())


# ---------------------------------------------------------------------------
# Loading functions
# ---------------------------------------------------------------------------


def _glob_match(pattern: str, path: str) -> bool:
    """Match a glob pattern against an absolute posix path.

    ``**`` matches any sequence including ``/``; ``*`` matches any sequence
    except ``/``; ``?`` matches a single non-``/`` character.
    """
    import re as _re
    i = 0
    parts: list[str] = []
    while i < len(pattern):
        c = pattern[i]
        if c == "*":
            if i + 1 < len(pattern) and pattern[i + 1] == "*":
                parts.append(".*")
                i += 2
            else:
                parts.append("[^/]*")
                i += 1
        elif c == "?":
            parts.append("[^/]")
            i += 1
        else:
            parts.append(_re.escape(c))
            i += 1
    return _re.match("^" + "".join(parts) + "$", path) is not None


def load_yaml(path: Path) -> dict[str, Any]:
    """Load a YAML file and return its contents as a dict."""
    try:
        with open(path, encoding="utf-8") as f:
            data = yaml.safe_load(f)
    except FileNotFoundError:
        raise ManifestError(f"Manifest file not found: {path}")
    except yaml.YAMLError as exc:
        raise ManifestError(f"YAML parse error in {path}: {exc}")
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise ManifestError(f"Expected YAML mapping in {path}, got {type(data).__name__}")
    return data


def _parse_install_component(item: Any) -> InstallComponent:
    """Parse a single install component (object form)."""
    if not isinstance(item, dict):
        raise SchemaValidationError(
            "install_components entry must be an object with 'name' and 'staging'"
        )
    name = item.get("name")
    staging = item.get("staging")
    if not name or not isinstance(name, str):
        raise SchemaValidationError(
            "install_components entry requires a non-empty 'name' string"
        )
    if staging not in ("sdk", "rootfs"):
        raise SchemaValidationError(
            f"install_components '{name}' has invalid staging '{staging}'; "
            f"must be one of: sdk, rootfs"
        )
    return InstallComponent(name=name, staging=staging)


def _parse_build_config(data: dict[str, Any]) -> BuildConfig:
    """Parse a build config, normalising legacy single-value fields.

    Raises SchemaValidationError on mutual-exclusivity or uniqueness
    violations.
    """
    preset = data.get("preset")
    if not preset or not isinstance(preset, str):
        raise SchemaValidationError("build.preset is required and must be a non-empty string")

    has_targets = "targets" in data
    has_target = "target" in data
    if has_targets and has_target:
        raise SchemaValidationError(
            "build.target (singular) and build.targets (plural) must not be "
            "declared together"
        )
    if has_targets:
        raw_targets = data["targets"]
        if not isinstance(raw_targets, list) or not raw_targets:
            raise SchemaValidationError("build.targets must be a non-empty array")
        targets = [str(t) for t in raw_targets]
        _ensure_unique(targets, "build.targets")
    elif has_target:
        targets = [str(data["target"])]
    else:
        raise SchemaValidationError(
            "build requires either build.target or build.targets"
        )

    has_ics = "install_components" in data
    has_ic = "install_component" in data
    if has_ics and has_ic:
        raise SchemaValidationError(
            "build.install_component (singular) and build.install_components "
            "(plural) must not be declared together"
        )
    if has_ics:
        raw_ics = data["install_components"]
        if not isinstance(raw_ics, list):
            raise SchemaValidationError("build.install_components must be an array")
        install_components = [_parse_install_component(item) for item in raw_ics]
        _ensure_unique([c.name for c in install_components], "install_components name")
    elif has_ic:
        install_components = [
            InstallComponent(name=str(data["install_component"]), staging="rootfs")
        ]
    else:
        install_components = []

    service_deps = [str(d) for d in data.get("service_dependencies", [])]
    _ensure_unique(service_deps, "service_dependencies")
    target_deps = [str(d) for d in data.get("target_dependencies", [])]
    _ensure_unique(target_deps, "target_dependencies")

    cmake_cache_vars = {
        str(k): str(v) for k, v in data.get("cmake_cache_variables", {}).items()
    }

    legacy_deps: list[str] = []
    if "dependencies" in data:
        if service_deps or target_deps:
            raise SchemaValidationError(
                "legacy build.dependencies must not coexist with "
                "service_dependencies or target_dependencies"
            )
        legacy_deps = [str(d) for d in data["dependencies"]]
        _ensure_unique(legacy_deps, "dependencies")

    return BuildConfig(
        preset=preset,
        targets=targets,
        install_components=install_components,
        service_dependencies=service_deps,
        target_dependencies=target_deps,
        cmake_cache_variables=cmake_cache_vars,
        _legacy_dependencies=legacy_deps,
    )


def _ensure_unique(items: list[str], label: str) -> None:
    seen: set[str] = set()
    for item in items:
        if not item:
            raise SchemaValidationError(f"{label} contains an empty element")
        if item in seen:
            raise SchemaValidationError(f"{label} contains duplicate entry: {item}")
        seen.add(item)


def _parse_config_validation(data: dict[str, Any]) -> ConfigValidation | None:
    """Parse runtime.config_validation (CR-003 §5).

    Requires ``target_path``; at least one of ``schema`` or ``check_command``
    is expected (the latter may be absent if the schema is the only check).
    """
    if not isinstance(data, dict) or not data:
        return None
    target_path = data.get("target_path")
    if not target_path or not isinstance(target_path, str):
        raise SchemaValidationError(
            "config_validation.target_path is required and must be a non-empty string"
        )
    schema = data.get("schema")
    check_command = data.get("check_command")
    default_source = data.get("default_source")
    if schema is not None and not isinstance(schema, str):
        raise SchemaValidationError("config_validation.schema must be a string path")
    if check_command is not None and not isinstance(check_command, str):
        raise SchemaValidationError("config_validation.check_command must be a string")
    if default_source is not None and not isinstance(default_source, str):
        raise SchemaValidationError("config_validation.default_source must be a string")
    if schema is None and check_command is None:
        # CR-003 §5 prefers a schema or check_command, but service-owned
        # schemas are not yet created (§12). Accept target_path +
        # default_source alone; run_schema_check skips gracefully until a
        # schema/check_command is declared.
        pass
    return ConfigValidation(
        target_path=str(target_path),
        schema=str(schema) if schema else None,
        default_source=str(default_source) if default_source else None,
        check_command=str(check_command) if check_command else None,
    )


def _parse_runtime_config(data: dict[str, Any]) -> RuntimeConfig:
    return RuntimeConfig(
        systemd_units=list(data.get("systemd_units", [])),
        after=list(data.get("after", [])),
        health_check=data.get("health_check"),
        smoke_test=data.get("smoke_test"),
        config_paths=list(data.get("config_paths", [])),
        persistent_paths=list(data.get("persistent_paths", [])),
        required_devices=list(data.get("required_devices", [])),
        capabilities=list(data.get("capabilities", [])),
        config_validation=_parse_config_validation(data.get("config_validation", {})),
    )


def _parse_service(service_id: str, data: dict[str, Any]) -> Service:
    repository = data.get("repository")
    source_dir = data.get("source_dir")
    if not repository and not source_dir:
        raise SchemaValidationError(
            f"Service '{service_id}' must declare either 'repository' or 'source_dir'"
        )
    return Service(
        id=service_id,
        repository=repository or source_dir,
        build=_parse_build_config(data["build"]),
        runtime=_parse_runtime_config(data.get("runtime", {})),
        revision=data.get("revision"),
        source_dir=source_dir,
        description=data.get("description"),
        kind=data.get("kind", "daemon"),
    )


def load_service_manifest(path: Path) -> ServiceManifest:
    """Load and parse a service manifest YAML file.

    Legacy ``dependencies`` entries are left in ``_legacy_dependencies``;
    call :func:`resolve_legacy_dependencies` (or use
    :meth:`Project.load_service_manifest`) to classify them with lock
    context.
    """
    data = load_yaml(path)
    raw_services = data.get("services", {})
    services: dict[str, Service] = {}
    for sid, svc_data in raw_services.items():
        services[sid] = _parse_service(sid, svc_data)
    return ServiceManifest(services=services)


def resolve_legacy_dependencies(
    manifest: ServiceManifest, lock_names: set[str]
) -> ServiceManifest:
    """Classify legacy ``dependencies`` entries using lock context.

    For each service with unresolved legacy dependencies, every element is
    classified as:

      * service dependency - if it matches a declared service id;
      * target dependency - if it matches a lock dependency name;
      * error - if it matches both (ambiguous) or neither (unclassifiable).

    Mutates the manifest in place and returns it.
    """
    for svc in manifest:
        if not svc.build._legacy_dependencies:
            continue
        for dep in svc.build._legacy_dependencies:
            in_services = dep in manifest.services
            in_lock = dep in lock_names
            if in_services and in_lock:
                raise SchemaValidationError(
                    f"Service '{svc.id}' legacy dependency '{dep}' is ambiguous: "
                    f"matches both a service id and a lock dependency"
                )
            elif in_services:
                svc.build.service_dependencies.append(dep)
            elif in_lock:
                svc.build.target_dependencies.append(dep)
            else:
                raise SchemaValidationError(
                    f"Service '{svc.id}' legacy dependency '{dep}' could not be "
                    f"classified as a service or target dependency"
                )
        svc.build._legacy_dependencies = []
    return manifest


def load_release_set_manifest(path: Path) -> ReleaseSetManifest:
    """Load and parse a release-set manifest YAML file."""
    data = load_yaml(path)
    raw_sets = data.get("release_sets", {})
    release_sets: dict[str, ReleaseSet] = {}
    for set_id, set_data in raw_sets.items():
        release_sets[set_id] = ReleaseSet(
            id=set_id,
            services=list(set_data["services"]),
            description=set_data.get("description"),
            platform=set_data.get("platform"),
            profile=set_data.get("profile"),
        )
    return ReleaseSetManifest(release_sets=release_sets)


def load_platform_manifest(path: Path) -> PlatformManifest:
    """Load a platform manifest YAML file."""
    return PlatformManifest(data=load_yaml(path))


def load_sysroot_manifest(path: Path) -> SysrootManifest:
    """Load a sysroot manifest YAML file."""
    return SysrootManifest(data=load_yaml(path))


def _parse_dependency_entry(name: str, data: dict[str, Any]) -> DependencyEntry:
    source = data.get("source", {}) or {}
    patches_raw = data.get("patches", []) or []
    patches = [
        DependencyPatch(
            file=str(p.get("file", "")),
            sha256=str(p.get("sha256", "")),
        )
        for p in patches_raw
    ]
    cmake = data.get("cmake", {}) or {}
    options = cmake.get("options", {}) or {}
    return DependencyEntry(
        name=name,
        version=str(data.get("version", "")),
        source_url=str(source.get("url", "")),
        source_sha256=str(source.get("sha256", "")),
        license=str(data.get("license", "")),
        boundary=str(data.get("boundary", "")),
        architecture=str(data.get("architecture", "")),
        linkage=str(data.get("linkage", "")),
        patches=patches,
        cmake_options={str(k): str(v) for k, v in options.items()},
    )


def load_dependency_lock(path: Path) -> DependencyLock:
    """Load and parse a dependency lock YAML file."""
    data = load_yaml(path)
    raw_deps = data.get("dependencies")
    if raw_deps is None:
        raw_deps = {}
    if isinstance(raw_deps, list):
        raise ManifestError(
            "dependencies/lock.yaml 'dependencies' must be a mapping keyed by "
            "dependency name (CR-002 dict form), not a list"
        )
    deps: dict[str, DependencyEntry] = {}
    for name, entry_data in raw_deps.items():
        deps[name] = _parse_dependency_entry(name, entry_data)
    cache = data.get("cache", {}) or {}
    return DependencyLock(dependencies=deps, cache=cache)


def load_config_deployment_manifest(path: Path) -> ConfigDeploymentManifest:
    """Load and parse the config-deployment policy manifest (CR-003 §7)."""
    data = load_yaml(path)
    version = int(data.get("version", 1))
    platforms: dict[str, list[ConfigDeployRule]] = {}
    for plat, pdata in (data.get("platforms", {}) or {}).items():
        rules: list[ConfigDeployRule] = []
        for entry in (pdata.get("files", []) or []):
            path_val = entry.get("path")
            glob_val = entry.get("path_glob")
            if not path_val and not glob_val:
                raise SchemaValidationError(
                    "config-deployment rule must declare 'path' or 'path_glob'"
                )
            policy = entry.get("deploy_policy", "")
            if policy not in ("replace", "preserve", "create_if_missing"):
                raise SchemaValidationError(
                    f"config-deployment rule has invalid deploy_policy '{policy}'"
                )
            category = entry.get("category", "")
            if category not in ("release-managed", "device-managed"):
                raise SchemaValidationError(
                    f"config-deployment rule has invalid category '{category}'"
                )
            rules.append(ConfigDeployRule(
                owner=entry.get("owner", ""),
                category=category,
                deploy_policy=policy,
                path=path_val,
                path_glob=glob_val,
            ))
        platforms[plat] = rules
    return ConfigDeploymentManifest(version=version, platforms=platforms)


# ---------------------------------------------------------------------------
# Project layout helper
# ---------------------------------------------------------------------------


class Project:
    """Provides paths to project manifests and directories."""

    def __init__(self, root: Path | str | None = None):
        if root is None:
            root = Path.cwd()
        self.root = Path(root).resolve()
        self._validate_root()

    def _validate_root(self) -> None:
        platform_yaml = self.root / "manifests" / "orin-platform.yaml"
        if not platform_yaml.is_file():
            raise ManifestError(
                f"Not a TBOX Build project root: {self.root} "
                f"(missing manifests/orin-platform.yaml)"
            )

    @property
    def manifests_dir(self) -> Path:
        return self.root / "manifests"

    @property
    def schemas_dir(self) -> Path:
        return self.manifests_dir / "schemas"

    @property
    def platform_manifest_path(self) -> Path:
        return self.manifests_dir / "orin-platform.yaml"

    @property
    def sysroot_manifest_name(self) -> str:
        """Determine sysroot manifest filename from platform manifest."""
        pm = self.load_platform_manifest()
        sid = pm.sysroot_id or pm.rootfs_id
        return f"{sid}.yaml"

    @property
    def sysroot_manifest_path(self) -> Path:
        return self.manifests_dir / self.sysroot_manifest_name

    @property
    def service_manifest_path(self) -> Path:
        return self.manifests_dir / "services.yaml"

    @property
    def release_set_manifest_path(self) -> Path:
        return self.manifests_dir / "release-set.yaml"

    @property
    def config_deployment_manifest_path(self) -> Path:
        return self.manifests_dir / "config-deployment.yaml"

    @property
    def dependencies_dir(self) -> Path:
        return self.root / "dependencies"

    @property
    def dependency_lock_path(self) -> Path:
        return self.dependencies_dir / "lock.yaml"

    @property
    def recipes_dir(self) -> Path:
        return self.dependencies_dir / "recipes"

    @property
    def cmake_dir(self) -> Path:
        return self.root / "cmake"

    @property
    def presets_path(self) -> Path:
        return self.root / "presets" / "CMakePresets.json"

    @property
    def sysroot_path(self) -> Path:
        return self.root / "sysroots" / "orin-r35.3.1"

    @property
    def out_dir(self) -> Path:
        return self.root / "out"

    def load_platform_manifest(self) -> PlatformManifest:
        return load_platform_manifest(self.platform_manifest_path)

    def load_sysroot_manifest(self) -> SysrootManifest:
        path = self.sysroot_manifest_path
        if not path.is_file():
            raise ManifestError(f"Sysroot manifest not found: {path}")
        return load_sysroot_manifest(path)

    def load_dependency_lock(self) -> DependencyLock:
        """Load the dependency lock, returning an empty lock if absent."""
        path = self.dependency_lock_path
        if not path.is_file():
            return DependencyLock(dependencies={})
        return load_dependency_lock(path)

    def load_service_manifest(self) -> ServiceManifest:
        """Load the service manifest and resolve legacy dependencies."""
        manifest = load_service_manifest(self.service_manifest_path)
        lock = self.load_dependency_lock()
        resolve_legacy_dependencies(manifest, lock.dependency_names())
        return manifest

    def load_release_set_manifest(self) -> ReleaseSetManifest:
        return load_release_set_manifest(self.release_set_manifest_path)

    def load_config_deployment_manifest(self) -> ConfigDeploymentManifest:
        """Load the config-deployment manifest; returns empty if absent."""
        path = self.config_deployment_manifest_path
        if not path.is_file():
            return ConfigDeploymentManifest(version=1, platforms={})
        return load_config_deployment_manifest(path)

    def staging_dir(self, platform: str = "orin", profile: str = "release") -> Path:
        return self.out_dir / platform / profile
