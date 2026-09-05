#!/usr/bin/env bash
# End-to-end sync: download everything from the pen, then render pages.
#
# Prereqs:
#   1. Pen physically connected to Windows with a DATA cable (powered on).
#   2. Run scripts/windows-connect.ps1 in an admin PowerShell so the pen
#      is attached into WSL. Verify with:  lsusb  -> 1cfb:1032
#   3. This script handles: build (if needed) -> download -> convert.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"

if ! lsusb | grep -qi '1cfb'; then
    echo "ERROR: Livescribe pen is not attached to WSL." >&2
    echo "Run scripts/windows-connect.ps1 (admin PowerShell) first, then check 'lsusb'." >&2
    exit 1
fi

# --- 1. build the pen client if needed -------------------------------------
if [ ! -x "$ROOT/vendor/dumpscribe" ]; then
    echo "Building dumpscribe..."
    "$ROOT/scripts/build-dumpscribe.sh"
fi

# --- 2. download everything from the pen ------------------------------------
echo "Syncing pen (this runs as root to access the USB device)..."
sudo "$ROOT/vendor/dumpscribe" "$ROOT/data"
sudo chown -R "$(id -u):$(id -g)" "$ROOT/data"

# --- 3. render pages + find audio -------------------------------------------
echo "Converting to web output..."
python3 "$ROOT/src/convert.py"

echo "Done. Preview with: python3 $ROOT/src/serve.py"