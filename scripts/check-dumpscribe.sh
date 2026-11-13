#!/usr/bin/env bash
# Check that vendor/dumpscribe.c compiles, without touching vendor/.
#
# With libopenobex installed this is a full build into a temporary
# directory, with the same flags as build-dumpscribe.sh. Without it, the
# source is syntax-checked against scripts/obex-stub (declarations only):
# that catches typos and type errors in an edit, not link or runtime
# problems, so say "syntax-checked only" when that is all that ran.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
SRC="$ROOT/vendor/dumpscribe.c"
LIBS=(glib-2.0 libusb-1.0 libarchive libxml-2.0)

if pkg-config --exists openobex "${LIBS[@]}" 2>/dev/null; then
    out="$(mktemp -d)"
    trap 'rm -rf "$out"' EXIT
    # shellcheck disable=SC2046  # pkg-config output is a list of flags
    gcc -Wall -o "$out/dumpscribe" "$SRC" \
        $(pkg-config --cflags --libs openobex "${LIBS[@]}")
    echo "dumpscribe: full build OK"
    exit 0
fi

if ! pkg-config --exists "${LIBS[@]}" 2>/dev/null; then
    echo "dumpscribe: cannot check; missing build libraries (${LIBS[*]})." >&2
    echo "Install them with: ./run setup" >&2
    exit 2
fi
# The stub declares functions without prototypes, which C23 rejects.
# shellcheck disable=SC2046  # pkg-config output is a list of flags
gcc -std=gnu17 -fsyntax-only -w -I"$ROOT/scripts/obex-stub" "$SRC" \
    $(pkg-config --cflags "${LIBS[@]}")
echo "dumpscribe: syntax OK (openobex not installed, so not a full build)"
