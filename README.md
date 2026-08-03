# iov-vehicle-tbox-build

Unified build orchestration for TBOX services targeting NVIDIA Orin (Linux aarch64).

BUILD v0.1-alpha | CR: TBOX-BUILD-DSN-CR-001

## Quick Start

### Prerequisites

- Docker (linux/arm64 container support)
- Python 3.8+ with PyYAML and jsonschema
- CMake 3.16+ (for host builds)

### Build the minimal example

```bash
# 1. Import sysroot (fix symlinks, generate manifest, verify)
python3 -m tbox_build sysroot import

# 2. Validate manifests
python3 -m tbox_build validate

# 3. Build inside Docker (cross-compile to aarch64)
./ci/build-in-docker.sh --set tbox-orin-minimal

# 4. Package
python3 -m tbox_build package

# 5. Verify
python3 -m tbox_build verify
```

### Full CI pipeline

```bash
./ci/pipeline.sh
```

## Usage

```bash
# Validate manifests (schema, dependency graph, cross-references)
python3 -m tbox_build validate

# Build (configure -> compile -> install -> ELF check -> artifact manifest)
python3 -m tbox_build build --platform orin --profile release --set tbox-orin-minimal
python3 -m tbox_build build --service tbox-hello-cli
python3 -m tbox_build build --dry-run --set tbox-orin-minimal

# Package (tar + artifact manifest)
python3 -m tbox_build package --platform orin --profile release

# Deploy (framework, dry-run by default)
python3 -m tbox_build deploy <package.tar.gz> --dry-run

# Verify staging or package
python3 -m tbox_build verify
python3 -m tbox_build verify --package <package.tar.gz>

# ELF/pollution check
python3 -m tbox_build elfcheck

# Sysroot management
python3 -m tbox_build sysroot import
python3 -m tbox_build sysroot verify
python3 -m tbox_build sysroot fix-symlinks
```

## Architecture

See [docs/architecture.md](docs/architecture.md) for the full design.

## Testing

```bash
# Unit + integration tests
python3 -m pytest tests/ -v
```

## Project Structure

```
tbox-build/
├── manifests/         Platform manifest, service/release-set schemas
├── cmake/             Toolchain (orin-aarch64.cmake) and CMake modules
├── presets/           CMakePresets.json (host/orin, debug/release)
├── scripts/           Shell entry points
├── ci/                Dockerfile and CI pipeline
├── tbox_build/        Python orchestration engine
├── tests/             Unit and integration tests (pytest)
├── examples/minimal/  Built-in minimal aarch64 example
└── sysroots/          Orin r35.3.1 sysroot (imported, gitignored)
```
