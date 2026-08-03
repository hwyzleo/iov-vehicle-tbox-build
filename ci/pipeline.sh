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
#   ./ci/pipeline.sh                    # full pipeline
#   ./ci/pipeline.sh --build-only       # build without package/deploy
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
IMAGE_NAME="tbox-build:0.1-alpha"
PLATFORM="${TBOX_PLATFORM:-orin}"
PROFILE="${TBOX_PROFILE:-release}"
SET="${TBOX_SET:-tbox-orin-minimal}"

cd "$PROJECT_ROOT"
export TBOX_ROOT="$PROJECT_ROOT"

echo "================================================"
echo " TBOX Build CI Pipeline"
echo " Platform: $PLATFORM, Profile: $PROFILE, Set: $SET"
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
echo ""
echo "[3-6/7] Build, install, ELF check, package (in Docker)"
docker run --rm \
    -v "$PROJECT_ROOT":/workspace \
    -e TBOX_ROOT=/workspace \
    --platform linux/arm64 \
    "$IMAGE_NAME" \
    bash -c "cd /workspace && \
        python3 -m tbox_build build --set $SET --profile $PROFILE --jobs 4 && \
        python3 -m tbox_build package --platform $PLATFORM --profile $PROFILE"

# Step 7: Verify package
echo ""
echo "[7/7] Verify package"
PKG=$(ls -t out/$PLATFORM/$PROFILE/packages/*.tar.gz 2>/dev/null | head -1)
if [ -z "$PKG" ]; then
    echo "ERROR: No package found"
    exit 1
fi
python3 -m tbox_build verify --platform $PLATFORM --profile $PROFILE --package "$PKG"

echo ""
echo "================================================"
echo " CI Pipeline: SUCCESS"
echo " Package: $PKG"
echo "================================================"
