# Building a macOS application

The macOS packager consumes a configured native Meson build. It stages
`meson install` in a temporary directory; it does not install into the system.
The output is a self-contained `Dia.app`, followed optionally by a DMG.
The old Automake, GTK2 theme, Pango module and Finder-layout workflows are retired.

## Prerequisites

Use macOS with Xcode command-line tools and Python 3.9 or newer for the packaging
script. Build Dia and all dependencies for the same architecture and deployment
target. Separate arm64 and x86_64 packages are recommended; a universal package
requires every bundled native dependency to contain both architectures.

Install GTK3 (with the native macOS backend), GLib, Graphene, libxml2 >=2.14,
xpm-pixbuf, gettext, GObject Introspection, Python3, PyGObject and Pycairo, plus
Meson, Ninja, pkg-config, appstream and libxml2's command-line utilities. Optional
PDF import additionally needs compatible Poppler development headers. See
BUILDING.md and the macOS CI workflow for a concrete build recipe.

The `--python` interpreter must import `gi` and `cairo` and match the
`python3-embed` pkg-config dependency used to build Dia. The packager copies that
interpreter's standard library and bindings, not arbitrary global site-packages.

## Build and package

For example, with dependencies already available through pkg-config:

```sh
export MACOSX_DEPLOYMENT_TARGET=14.0
meson setup build-macos --prefix="$PWD/build-macos/install" \
  -Ddoc=disabled -Dgtk_doc=false
meson compile -C build-macos -j 4
meson test -C build-macos --print-errorlogs
python3 installer/macosx/test_bundle.py
python3 installer/macosx/bundle.py --builddir build-macos \
  --python "$(command -v python3)" --minimum-macos 14.0 \
  --output dist/Dia.app
```

`14.0` is an example, not a claim of support for every dependency on that OS.
Choose a deployment target that your dependencies support. The builder rejects
binaries requiring a newer macOS version or missing the application's CPU
architectures. Set the target when building dependencies too; changing plist
metadata cannot lower their deployment targets.

`osx-app.sh` forwards the same options to `bundle.py`. Old `-l`, `-b`, `-py` and
`-p` options are no longer supported. Output paths must not exist; move or remove
only your own previous artifacts before rebuilding.

The packager recursively copies non-system Mach-O dependencies and rewrites
linked references relative to each binary. Unresolved libraries, conflicting
library basenames, external symlinks, and leftover build-machine references fail
packaging. The bundle includes GTK3 loaders, GI typelibs, schemas, MIME data,
icons, translations, font configuration and the Python3 runtime. It doesn't
bundle Apple system libraries or GTK2 themes. Python plugins installed by users
may require additional third-party packages, which are not included automatically.

A native, signed entry point invokes the resource launcher, which resolves paths from its location and forwards command-line
arguments. GdkPixbuf's loader cache and GTK3's optional input-module cache are generated outside the signed app for each
launch and removed on exit. Fontconfig uses its normal user cache. GTK introspection typelibs are rebuilt from their GIR sources with explicit
bundle-relative library references. Both linked and dynamically loaded libraries
resolve inside the bundle without DYLD search-path overrides. No unversioned system
Python, global GTK configuration or writable application directory is required.

## Validate the artifact

Test the **packaged** application, not only the build tree. Move it to a path
containing spaces, then export a sample from the samples working directory:

```sh
mv dist/Dia.app 'dist/Dia Test.app'
cd samples
'../dist/Dia Test.app/Contents/MacOS/dia' render-test.dia \
  --export=/tmp/dia-render-test.svg
cd ..
codesign --verify --deep --strict 'dist/Dia Test.app'
open 'dist/Dia Test.app'
```

Confirm SVG output, fonts, shapes, translations, Python console, opening a
`.dia` document from Finder, and normal shutdown. Repeat on a clean machine
without the build dependencies installed. The CI export check covers relocation
and basic application behavior; it does not replace manual Finder/GUI testing.
GTK's native macOS file-open event handling must also be checked independently
of command-line argument forwarding.

## Signing, disk images and notarization

Without `--sign-identity`, the builder ad-hoc signs the bundle for local testing.
For distribution, supply a Developer ID Application identity available in your
Keychain and a numeric build number:

```sh
python3 installer/macosx/bundle.py --builddir build-macos \
  --python "$(command -v python3)" --minimum-macos 14.0 \
  --bundle-version 12 --sign-identity 'Developer ID Application: Your Name (TEAMID)' \
  --output dist/Dia.app
sh installer/macosx/osx-dmg.sh dist/Dia.app dist/Dia.dmg
```

Libraries and executables are signed inside-out with hardened runtime and
secure timestamps for Developer ID builds. No entitlements disabling library
validation are added. All bundled native plugins must be signed consistently.

For notarization, first store credentials securely using Apple's
`xcrun notarytool store-credentials`. Pass the Keychain profile name, never a
password or API key, as the third argument:

```sh
sh installer/macosx/osx-dmg.sh dist/Dia.app dist/Dia.dmg dia-notary-profile
```

The script verifies the application signature, creates and verifies a compressed
DMG with an Applications shortcut, waits for notarization, then staples and
validates the ticket. It does not mount images, disable verification, manipulate
Finder windows or overwrite existing output. Validate Gatekeeper acceptance on a
clean machine before release. Apple credentials and signing identities are not
required for unsigned CI artifacts (which use ad-hoc signatures).
