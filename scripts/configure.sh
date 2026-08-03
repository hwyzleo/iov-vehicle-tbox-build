#!/usr/bin/env bash
# scripts/configure.sh - TBOX Build configure-only entry point
#
# Validates manifests and prints the build plan without building.
# Usage:
#   ./scripts/configure.sh
#   ./scripts/configure.sh --set tbox-orin-minimal
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

export TBOX_ROOT="$PROJECT_ROOT"
cd "$PROJECT_ROOT"

# First validate, then show build plan with dry-run
python3 -m tbox_build validate "$@" || true
echo ""
exec python3 -m tbox_build build --dry-run "$@"
