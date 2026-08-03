#!/bin/bash
# Smoke test for tbox-hello service IPC/config.
# Exit 0 = pass, non-zero = fail.

set -euo pipefail

# Check config file exists
if [ ! -f /etc/tbox/hello/hello.conf ]; then
    echo "FAIL: config file /etc/tbox/hello/hello.conf not found"
    exit 1
fi

# Run the CLI and verify output
OUTPUT=$(/usr/bin/tbox-hello-cli 2>&1)
if echo "$OUTPUT" | grep -q "TBOX-Hello, Orin!"; then
    echo "PASS: tbox-hello-cli produces expected greeting"
    exit 0
fi

echo "FAIL: unexpected output: $OUTPUT"
exit 1
