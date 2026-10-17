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

For Homebrew builds, use the same Homebrew libxml2 for Dia and libxslt. The
successful arm64 build used libxml2 2.15.4 with legacy APIs enabled by Homebrew.
Make the keg-only library visible before configuring Meson:

```sh
export PKG_CONFIG_PATH="$(brew --prefix libxml2)/lib/pkgconfig:$(brew --prefix libxslt)/lib/pkgconfig:$(brew --prefix gettext)/lib/pkgconfig${PKG_CONFIG_PATH:+:$PKG_CONFIG_PATH}"
```

Keep any local xpm-pixbuf prefix ahead of these paths. The workflow builds
xpm-pixbuf at commit `d290a0c846687b22d2a8c5aaec83a6689f30e1c3`. The packager
rejects conflicting libxml2 copies rather than selecting one arbitrarily.

The `--python` interpreter must import `gi` and `cairo` and match the
`python3-embed` pkg-config dependency used to build Dia. The packager copies that
interpreter's standard library and bindings, not arbitrary global site-packages.

## Build and package

For example, with dependencies already available through pkg-config:

```sh
export MACOSX_DEPLOYMENT_TARGET=15.0
meson setup build-macos --prefix="$PWD/build-macos/install" \
  -Ddoc=disabled -Dgtk_doc=false
meson compile -C build-macos -j 4
meson test -C build-macos --print-errorlogs
python3 installer/macosx/test_bundle.py
python3 installer/macosx/bundle.py --builddir build-macos \
  --python "$(command -v python3)" --minimum-macos 15.0 \
  --output dist/Dia.app
```

`15.0` matches the verified Apple Silicon build. Other deployment targets need
their own compatible dependency builds and validation.
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

A native, signed entry point invokes the resource launcher, which resolves
paths from its location and forwards command-line arguments. GdkPixbuf's loader
cache and GTK3's optional input-module cache are generated outside the signed
app for each launch and removed on exit. Fontconfig uses its normal user cache.
GTK introspection typelibs are rebuilt from their GIR sources with explicit
bundle-relative library references. Both linked and dynamically loaded libraries
resolve inside the bundle without DYLD search-path overrides. No unversioned
system Python, global GTK configuration or writable application directory is
required.

## Saved build adjustments

The reusable builder includes the corrections verified during the first native
Apple Silicon DMG build:

- Dependency copies are made writable before merging or relocation; source
  Homebrew files remain unchanged.
- Apple system resource directories are excluded from dependency discovery.
- Fontconfig's DTD-provided `its` and `xsi` namespace declarations are made
  explicit before parsing, and its paths and cache locations are relocated.
- Existing Mach-O signature load commands are retained during relocation to
  preserve arm64 `__LINKEDIT` layout. Final signing replaces invalidated signatures.

The workflow caches compiler outputs, reports the end of failing build logs,
and uploads `Dia.dmg` together with `Dia.dmg.sha256`. Manual runs accept a target
runner and a `package_only` option, which skips application tests and launch checks
while retaining packaging tests, relocation checks, signing verification, and
DMG verification. Pushes to `build/macos-arm64` use that Apple Silicon build-only
mode; normal branch CI retains the Intel/Apple Silicon matrix and application checks.

[The successful Apple Silicon run](https://github.com/ranthon3/dia-cleanup/actions/runs/39036535501)
verified the macOS 15+ arm64 build, bundle signatures, and disk image. It used
ad-hoc signing and did not launch Dia or perform Developer ID notarization.

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

## Apple Silicon builds for macOS 12.6

The `macos-monterey.yml` workflow builds an isolated arm64 runtime from source
with `MACOSX_DEPLOYMENT_TARGET=12.6`. It uses pinned GTK-OSX and JHBuild revisions;
`prepare_source_stack.py` copies their modulesets and validates SHA256 pins for
the selected source archives. Homebrew supplies host build tools, while
pkg-config searches only the isolated runtime prefix for library dependencies.
The upstream GTK-OSX bootstrap shell script is not executed.

`build_monterey.sh` runs within that JHBuild environment, builds the pinned
xpm-pixbuf revision and Dia, and packages `Dia-macOS-12.6-arm64.dmg` with a SHA256
file. Packaging rejects bundled binaries whose deployment version exceeds 12.6,
then verifies the app signature and disk image. This build-only workflow does
not launch Dia; testing the application on macOS 12.6 remains a separate step.

The workflow caches the source stack under a normalized absolute path. Its host
Autotools macro search includes Homebrew's `pkg.m4`, which libxml2's bootstrap
requires. CMake is used from Homebrew as a host tool rather than rebuilt as part
of the runtime stack.

## Signing, disk images and notarization

Without `--sign-identity`, the builder ad-hoc signs the bundle for local testing.
For distribution, supply a Developer ID Application identity available in your
Keychain and a numeric build number:

```sh
python3 installer/macosx/bundle.py --builddir build-macos \
  --python "$(command -v python3)" --minimum-macos 15.0 \
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
