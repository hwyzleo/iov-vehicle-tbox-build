#!/usr/bin/env bash
# scripts/deploy.sh - TBOX Build deployment entry point
#
# Deploys a release package to a target device.
# Usage:
#   ./scripts/deploy.sh <package.tar.gz> --host orin.local
#   ./scripts/deploy.sh <package.tar.gz> --dry-run
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

export TBOX_ROOT="$PROJECT_ROOT"
cd "$PROJECT_ROOT"

exec python3 -m tbox_build deploy "$@"
