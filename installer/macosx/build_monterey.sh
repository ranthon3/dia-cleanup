#!/usr/bin/env bash
# Run inside the isolated GTK-OSX environment configured for macOS 12.6.
set -euo pipefail
: "${DIA_MONTEREY_ROOT:?Missing isolated source-stack root}"
PREFIX="$DIA_MONTEREY_ROOT/prefix"
export PKG_CONFIG_LIBDIR="$PREFIX/lib/pkgconfig:$PREFIX/share/pkgconfig"
export PKG_CONFIG_PATH="$PKG_CONFIG_LIBDIR"
export MACOSX_DEPLOYMENT_TARGET=12.6
meson setup "$RUNNER_TEMP/xpm-build" "$RUNNER_TEMP/xpm-pixbuf" --prefix="$PREFIX" --libdir=lib
meson compile -C "$RUNNER_TEMP/xpm-build" -j 4
meson install -C "$RUNNER_TEMP/xpm-build"
meson setup build-monterey --prefix="$RUNNER_TEMP/dia-monterey-install" --libdir=lib -Ddoc=disabled -Dgtk_doc=false -Dtests=false --wrap-mode=nofallback
python3 installer/macosx/test_bundle.py
meson compile -C build-monterey -j 4
python3 installer/macosx/bundle.py --builddir build-monterey --python "$PREFIX/bin/python3" --minimum-macos 12.6 --output dist/Dia.app
codesign --verify --deep --strict dist/Dia.app
sh installer/macosx/osx-dmg.sh dist/Dia.app dist/Dia-macOS-12.6-arm64.dmg
shasum -a 256 dist/Dia-macOS-12.6-arm64.dmg > dist/Dia-macOS-12.6-arm64.dmg.sha256
