#!/usr/bin/env bash
# scripts/package.sh - TBOX Build packaging entry point
#
# Creates a release package from the staging install-root.
# Usage:
#   ./scripts/package.sh --platform orin --profile release
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

export TBOX_ROOT="$PROJECT_ROOT"
cd "$PROJECT_ROOT"

exec python3 -m tbox_build package "$@"
