# TBOX Build Architecture

> BUILD v0.1-alpha | CR: TBOX-BUILD-DSN-CR-001

## Overview

TBOX Build is a unified build orchestration system for TBOX services
targeting NVIDIA Orin (Linux aarch64).  It provides cross-compilation,
staging, artifact manifest generation, packaging, deployment and
verification through a single controlled toolchain and sysroot.

## Design Boundaries

1. **Platform Manifest** - Fixed target environment baseline
2. **Toolchain + Sysroot** - Cross-compilation and target system view
3. **Project Contract** - CMake integration rules for services
4. **Build Orchestrator** - Dependency-graph-based build ordering
5. **Staging + Package** - Rootfs mapping and release artifacts
6. **Deploy + Verify** - Device deployment and smoke testing

## Directory Structure

```
tbox-build/
├── manifests/           Platform, service, release-set manifests + schemas
├── cmake/               Toolchain file and CMake modules
├── presets/             CMakePresets.json (host/orin, debug/release)
├── dependencies/        Dependency lock file and recipes
├── scripts/             Shell entry points (build.sh, package.sh, ...)
├── ci/                  Dockerfile and CI pipeline
├── packaging/           Platform-level systemd assets
├── tbox_build/          Python orchestration engine
├── tests/               Unit and integration tests
├── docs/                Documentation
├── examples/minimal/    Built-in minimal aarch64 example
└── sysroots/            Orin r35.3.1 sysroot (imported, read-only)
```

## Orchestration Engine

The Python package `tbox_build/` provides:

| Module | Responsibility |
|--------|---------------|
| `manifest.py` | Load and parse YAML manifests into typed dataclasses |
| `schema.py` | JSON Schema validation for service/release-set manifests |
| `graph.py` | Dependency graph: topological sort, cycle/missing-dep detection |
| `validator.py` | Cross-reference validation: systemd units, health/smoke scripts |
| `elfcheck.py` | Pure-Python ELF parser: architecture, dynamic deps, RPATH, pollution |
| `staging.py` | Staging directory management and file ownership tracking |
| `artifact.py` | Artifact manifest generation with per-file metadata |
| `orchestrator.py` | Full build pipeline: configure -> build -> install -> check -> manifest |
| `packaging.py` | tar + manifest package creation |
| `deploy.py` | Deploy pipeline framework (pre-check -> upload -> backup -> install -> verify) |
| `verify.py` | Staging and package verification |
| `sysroot.py` | Sysroot import: symlink fixing, file manifest, architecture verification |

## Build Pipeline

```
Manifests → Schema Validation → Graph Validation → Topological Sort
    ↓
For each service (in dep order):
    CMake Configure → CMake Build → CMake Install (DESTDIR staging)
    ↓
ELF/Pollution Check → Artifact Manifest → Package
    ↓
Deploy (pre-check → upload → backup → install → reload → restart → smoke)
```

## Build Environment

- **Container**: Ubuntu 20.04.6 Focal, linux/arm64
- **Compiler**: GCC/G++ 9.4.x (native aarch64 in arm64 container)
- **Build tools**: CMake 3.16, Ninja 1.10, Make 4.2 (fallback)
- **Sysroot**: orin-r35.3.1 (imported from device snapshot, 20224 files)
- **CI Runner target**: native Linux arm64

## Phase 1 Pass Criteria

| Criteria | Status |
|----------|--------|
| Manifest parsing | ✅ Verified (98 unit/integration tests) |
| Dependency sorting | ✅ Verified (topological sort with cycle/missing detection) |
| configure/build/install | ✅ Verified (Docker container, aarch64 ELF output) |
| ELF/pollution check | ✅ Verified (0 violations, all EM_AARCH64) |
| Staging | ✅ Verified (6 files, no path conflicts) |
| Packaging | ✅ Verified (tar + artifact manifest + SHA-256) |
| Optional Orin run | Framework ready (deploy/verify scripts) |
