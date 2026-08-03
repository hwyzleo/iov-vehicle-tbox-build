# ci/build-in-docker.sh - Run TBOX build inside Docker (linux/arm64)
#
# WHAT IT DOES
#   Builds the arm64 build image (if missing), then runs the cross-compile
#   inside the container. The workspace PARENT is mounted at /workspace so the
#   service source repos referenced by manifests (e.g. ../iov-vehicle-tbox-prov)
#   are visible; TBOX_ROOT points at this project inside the container.
#
# ARGUMENT PASSTHROUGH
#   This script does NOT parse build options itself. Every argument you pass is
#   forwarded verbatim to the Python orchestrator:
#
#       ./ci/build-in-docker.sh <ARGS...>
#            └─────────────► python3 -m tbox_build build <ARGS...>
#
#   So the accepted flags are defined by `tbox_build build` (see
#   tbox_build/__main__.py), not by this script. The main ones:
#
#     --set <id>       Build a release set (services defined in
#                      manifests/release-set.yaml). Available sets:
#                        - tbox-orin-minimal   (hello-lib + hello-cli, self-test)
#                        - tbox-framework-orin (framework only)
#                        - tbox-prov-orin      (framework + prov)
#     --service <id>   Build a single service (deps built automatically).
#                        e.g. framework, prov
#     --all            Build all services
#     --platform <p>   Target platform (default: orin)
#     --profile <p>    debug | release (default: release)
#     -j, --jobs <n>   Parallel jobs (default: 1)
#     --clean          Clean build (discard cached CMake config/output)
#     --dry-run        Print the cmake commands without executing
#
# EXAMPLES
#   ./ci/build-in-docker.sh --set tbox-orin-minimal      # self-verification set
#   ./ci/build-in-docker.sh --set tbox-prov-orin         # framework + prov
#   ./ci/build-in-docker.sh --service prov               # prov (+ framework dep)
#   ./ci/build-in-docker.sh --service prov --clean       # force a clean rebuild
#   ./ci/build-in-docker.sh --set tbox-prov-orin --dry-run
#
# See docs/architecture.md and manifests/*.yaml for the full model.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

IMAGE_NAME="tbox-build:0.1-alpha"
DOCKERFILE="$SCRIPT_DIR/Dockerfile"

# The service manifests reference sibling repositories via source_dir
# (e.g. ../iov-vehicle-tbox-framework, ../iov-vehicle-tbox-prov). The approved
# workspace boundary is the PARENT of the BUILD project root, so mount that
# parent into the container to make sibling repos visible at the expected path.
WORKSPACE_PARENT="$(cd "$PROJECT_ROOT/.." && pwd)"
BUILD_SUBDIR="$(basename "$PROJECT_ROOT")"
CONTAINER_TBOX_ROOT="/workspace/${BUILD_SUBDIR}"

# Build image if not present
if ! docker image inspect "$IMAGE_NAME" >/dev/null 2>&1; then
    echo "Building Docker image $IMAGE_NAME..."
    docker build -t "$IMAGE_NAME" -f "$DOCKERFILE" "$PROJECT_ROOT"
fi

# Run build inside container
echo "Running build in Docker container..."
docker run --rm \
    -v "$WORKSPACE_PARENT":/workspace \
    -e TBOX_ROOT="$CONTAINER_TBOX_ROOT" \
    --platform linux/arm64 \
    "$IMAGE_NAME" \
    bash -c "cd '$CONTAINER_TBOX_ROOT' && python3 -m tbox_build build $*"
