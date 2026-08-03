#!/usr/bin/env bash
# scripts/build.sh - TBOX Build entry point
#
# Usage:
#   ./scripts/build.sh --platform orin --profile release --set tbox-orin-minimal
#   ./scripts/build.sh --service tbox-hello-cli
#   ./scripts/build.sh --dry-run --set tbox-orin-minimal
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

export TBOX_ROOT="$PROJECT_ROOT"
cd "$PROJECT_ROOT"

exec python3 -m tbox_build build "$@"
