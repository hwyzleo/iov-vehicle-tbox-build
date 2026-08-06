#!/usr/bin/env bash
# ci/pipeline.sh - TBOX Build CI pipeline
#
# Executes the full build verification pipeline:
#   1. Lint (manifest/schema validation)
#   2. Configure (cross-compile configure)
#   3. Build (compile + link)
#   4. Install (staging)
#   5. ELF/pollution check
#   6. Package + manifest
#   7. Verify package
#
# Usage:
#   ./ci/pipeline.sh [OPTIONS]
#
# OPTIONS (all optional; CLI flags override the matching TBOX_* env vars):
#   --set <id>         Release set to build/package (default: tbox-orin-minimal).
#                      Available sets are defined in manifests/release-set.yaml:
#                        - tbox-orin-minimal   (hello-lib + hello-cli, self-test)
#                        - tbox-framework-orin (framework only)
#                        - tbox-prov-orin      (framework + prov)
#                        - tbox-sec-orin       (framework + prov + sec)
#                        - tbox-mqtt-orin      (framework + prov + sec + mqtt)
#                        - tbox-tsp-orin       (framework + prov + sec + mqtt + tsp)
#                        - tbox-someip-orin    (full TBOX stack: + someip)
#   --service <id>     Build/package a single service instead of a set
#                      (mutually exclusive with --set; deps built automatically).
#   --platform <p>     Target platform (default: orin).
#   --profile <p>      debug | release (default: release).
#   -j, --jobs <n>     Parallel build jobs (default: 4).
#   --build-only       Run build + package but skip the final verify step.
#   -h, --help         Show this help and exit.
#
# ENV VAR DEFAULTS (used when the matching flag is not passed):
#   TBOX_PLATFORM, TBOX_PROFILE, TBOX_SET
#
# EXAMPLES:
#   ./ci/pipeline.sh                              # minimal set, full pipeline
#   ./ci/pipeline.sh --set tbox-prov-orin         # framework + prov
#   ./ci/pipeline.sh --service prov --jobs 8      # single service, 8 jobs
#   TBOX_SET=tbox-prov-orin ./ci/pipeline.sh      # still works via env var
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
IMAGE_NAME="tbox-build:0.1-alpha"

# Defaults (overridable by env vars, then by CLI flags below)
PLATFORM="${TBOX_PLATFORM:-orin}"
PROFILE="${TBOX_PROFILE:-release}"
SET="${TBOX_SET:-tbox-orin-minimal}"
SERVICE=""
JOBS=4
BUILD_ONLY=0

usage() {
    sed -n '2,37p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
}

while [ $# -gt 0 ]; do
    case "$1" in
        --set)
            [ $# -ge 2 ] || { echo "ERROR: --set requires an argument" >&2; exit 2; }
            SET="$2"; SERVICE=""; shift 2 ;;
        --set=*)
            SET="${1#*=}"; SERVICE=""; shift ;;
        --service)
            [ $# -ge 2 ] || { echo "ERROR: --service requires an argument" >&2; exit 2; }
            SERVICE="$2"; SET=""; shift 2 ;;
        --service=*)
            SERVICE="${1#*=}"; SET=""; shift ;;
        --platform)
            [ $# -ge 2 ] || { echo "ERROR: --platform requires an argument" >&2; exit 2; }
            PLATFORM="$2"; shift 2 ;;
        --platform=*)
            PLATFORM="${1#*=}"; shift ;;
        --profile)
            [ $# -ge 2 ] || { echo "ERROR: --profile requires an argument" >&2; exit 2; }
            PROFILE="$2"; shift 2 ;;
        --profile=*)
            PROFILE="${1#*=}"; shift ;;
        -j|--jobs)
            [ $# -ge 2 ] || { echo "ERROR: $1 requires an argument" >&2; exit 2; }
            JOBS="$2"; shift 2 ;;
        --jobs=*)
            JOBS="${1#*=}"; shift ;;
        --build-only)
            BUILD_ONLY=1; shift ;;
        -h|--help)
            usage; exit 0 ;;
        *)
            echo "ERROR: unknown argument: $1" >&2
            echo "Run '$0 --help' for usage." >&2
            exit 2 ;;
    esac
done

# Resolve the build scope: --service takes precedence when set, else --set.
if [ -n "$SERVICE" ]; then
    SCOPE_ARGS=(--service "$SERVICE")
    SCOPE_DESC="service=$SERVICE"
else
    SCOPE_ARGS=(--set "$SET")
    SCOPE_DESC="set=$SET"
fi

cd "$PROJECT_ROOT"
export TBOX_ROOT="$PROJECT_ROOT"

echo "================================================"
echo " TBOX Build CI Pipeline"
echo " Platform: $PLATFORM, Profile: $PROFILE, $SCOPE_DESC, Jobs: $JOBS"
echo "================================================"

# Step 1: Build Docker image if needed
echo ""
echo "[1/7] Build container image"
if ! docker image inspect "$IMAGE_NAME" >/dev/null 2>&1; then
    echo "  Building $IMAGE_NAME..."
    docker build -t "$IMAGE_NAME" -f ci/Dockerfile .
else
    echo "  Image $IMAGE_NAME already exists, skipping build"
fi

# Step 2: Validate manifests (host-side, no Docker needed)
echo ""
echo "[2/7] Lint - manifest/schema validation"
python3 -m tbox_build validate

# Steps 3-6: Build, install, ELF check, package (inside Docker)
#
# Service manifests reference sibling repositories via source_dir
# (e.g. ../iov-vehicle-tbox-framework, ../iov-vehicle-tbox-someip). The approved
# workspace boundary is the PARENT of the BUILD project root, so mount that
# parent into the container to make sibling repos visible at the expected path,
# and point TBOX_ROOT at this project inside the container.
WORKSPACE_PARENT="$(cd "$PROJECT_ROOT/.." && pwd)"
BUILD_SUBDIR="$(basename "$PROJECT_ROOT")"
CONTAINER_TBOX_ROOT="/workspace/${BUILD_SUBDIR}"

echo ""
echo "[3-6/7] Build, install, ELF check, package (in Docker)"
docker run --rm \
    -v "$WORKSPACE_PARENT":/workspace \
    -e TBOX_ROOT="$CONTAINER_TBOX_ROOT" \
    --platform linux/arm64 \
    "$IMAGE_NAME" \
    bash -c "cd '$CONTAINER_TBOX_ROOT' && \
        python3 -m tbox_build build ${SCOPE_ARGS[*]} --profile $PROFILE --jobs $JOBS && \
        python3 -m tbox_build package --platform $PLATFORM --profile $PROFILE"

# Step 7: Verify package
PKG=$(ls -t out/$PLATFORM/$PROFILE/packages/*.tar.gz 2>/dev/null | head -1)
if [ -z "$PKG" ]; then
    echo "ERROR: No package found"
    exit 1
fi

if [ "$BUILD_ONLY" -eq 1 ]; then
    echo ""
    echo "[7/7] Verify package - SKIPPED (--build-only)"
else
    echo ""
    echo "[7/7] Verify package"
    python3 -m tbox_build verify --platform $PLATFORM --profile $PROFILE --package "$PKG"
fi

echo ""
echo "================================================"
echo " CI Pipeline: SUCCESS"
echo " Package: $PKG"
echo "================================================"
