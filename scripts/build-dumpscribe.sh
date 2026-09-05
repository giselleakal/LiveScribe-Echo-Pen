#!/usr/bin/env bash
# Build the dumpscribe pen client from vendored source.
# Requires (Debian/Ubuntu):
#   build-essential pkg-config libglib2.0-dev libusb-1.0-0-dev
#   libopenobex2-dev libarchive-dev libxml2-dev libbluetooth-dev
set -euo pipefail
cd "$(dirname "$0")/../vendor"
gcc -Wall -o dumpscribe dumpscribe.c \
    $(pkg-config --cflags --libs glib-2.0 openobex libusb-1.0 libarchive libxml-2.0)
echo "built vendor/dumpscribe"