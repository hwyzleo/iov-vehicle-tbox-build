# ci/build-in-docker.sh - Run TBOX build inside Docker
#
# Usage:
#   ./ci/build-in-docker.sh [--set tbox-orin-minimal] [--clean]
#
# Builds the Docker image (if needed) and runs the build inside.
# The project root and sysroot are mounted as volumes.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

IMAGE_NAME="tbox-build:0.1-alpha"
DOCKERFILE="$SCRIPT_DIR/Dockerfile"

# Build image if not present
if ! docker image inspect "$IMAGE_NAME" >/dev/null 2>&1; then
    echo "Building Docker image $IMAGE_NAME..."
    docker build -t "$IMAGE_NAME" -f "$DOCKERFILE" "$PROJECT_ROOT"
fi

# Run build inside container
echo "Running build in Docker container..."
docker run --rm \
    -v "$PROJECT_ROOT":/workspace \
    -v "$PROJECT_ROOT/sysroots":/workspace/sysroots:ro \
    -e TBOX_ROOT=/workspace \
    --platform linux/arm64 \
    "$IMAGE_NAME" \
    bash -c "cd /workspace && python3 -m tbox_build build $*"
