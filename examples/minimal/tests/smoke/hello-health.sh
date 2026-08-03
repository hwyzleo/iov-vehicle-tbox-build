#!/bin/bash
# Health check for tbox-hello service.
# Exit 0 = healthy, non-zero = unhealthy.

set -euo pipefail

# Check if the tbox-hello-cli binary exists
if [ ! -x /usr/bin/tbox-hello-cli ]; then
    echo "ERROR: tbox-hello-cli binary not found"
    exit 1
fi

# Run the binary and check output
OUTPUT=$(/usr/bin/tbox-hello-cli 2>&1) || true
if echo "$OUTPUT" | grep -q "TBOX-Hello"; then
    echo "OK: tbox-hello-cli responds"
    exit 0
fi

echo "ERROR: tbox-hello-cli did not produce expected output"
exit 1
