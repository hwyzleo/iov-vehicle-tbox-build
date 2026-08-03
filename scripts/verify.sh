#!/usr/bin/env bash
# scripts/verify.sh - TBOX Build verification entry point
#
# Verifies staging output or a release package.
# Usage:
#   ./scripts/verify.sh
#   ./scripts/verify.sh --package out/orin/release/packages/tbox-orin-*.tar.gz
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

export TBOX_ROOT="$PROJECT_ROOT"
cd "$PROJECT_ROOT"

exec python3 -m tbox_build verify "$@"
