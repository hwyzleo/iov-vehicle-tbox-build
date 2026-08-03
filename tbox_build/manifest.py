"""Manifest loading and data model for TBOX Build.

Loads and parses platform manifest, sysroot manifest, service manifest
and release-set manifest from YAML files into typed dataclass objects.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from .errors import ManifestError


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


@dataclass
class BuildConfig:
    """Build configuration for a single service."""

    target: str
    preset: str
    dependencies: list[str] = field(default_factory=list)
    install_component: str | None = None


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

    @property
    def effective_source_dir(self) -> str:
        """Source directory relative to project root."""
        return self.source_dir or self.repository


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
# Loading functions
# ---------------------------------------------------------------------------


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


def _parse_build_config(data: dict[str, Any]) -> BuildConfig:
    return BuildConfig(
        target=data["target"],
        preset=data["preset"],
        dependencies=list(data.get("dependencies", [])),
        install_component=data.get("install_component"),
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
    )


def _parse_service(service_id: str, data: dict[str, Any]) -> Service:
    return Service(
        id=service_id,
        repository=data["repository"],
        build=_parse_build_config(data["build"]),
        runtime=_parse_runtime_config(data.get("runtime", {})),
        revision=data.get("revision"),
        source_dir=data.get("source_dir"),
        description=data.get("description"),
    )


def load_service_manifest(path: Path) -> ServiceManifest:
    """Load and parse a service manifest YAML file."""
    data = load_yaml(path)
    raw_services = data.get("services", {})
    services: dict[str, Service] = {}
    for sid, svc_data in raw_services.items():
        services[sid] = _parse_service(sid, svc_data)
    return ServiceManifest(services=services)


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

    def load_service_manifest(self) -> ServiceManifest:
        return load_service_manifest(self.service_manifest_path)

    def load_release_set_manifest(self) -> ReleaseSetManifest:
        return load_release_set_manifest(self.release_set_manifest_path)

    def staging_dir(self, platform: str = "orin", profile: str = "release") -> Path:
        return self.out_dir / platform / profile
