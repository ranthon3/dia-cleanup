#!/bin/sh
# Compatibility entry point; see README.md for the Meson-based interface.
set -eu
SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
exec python3 "$SCRIPT_DIR/bundle.py" "$@"
