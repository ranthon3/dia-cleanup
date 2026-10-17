#!/bin/sh
# Create a noninteractive drag-and-drop image, optionally notarizing it.
set -eu
usage() {
    echo "Usage: $0 Dia.app output.dmg [notarytool-keychain-profile]" >&2
    exit 2
}
[ "$#" -ge 2 ] && [ "$#" -le 3 ] || usage
[ "$(uname -s)" = Darwin ] || { echo 'DMG creation requires macOS' >&2; exit 1; }
APP=$(CDPATH= cd -- "$(dirname -- "$1")" && pwd)/$(basename -- "$1")
[ -d "$APP/Contents" ] || usage
[ ! -e "$2" ] || { echo "Output already exists: $2" >&2; exit 1; }
[ -d "$(dirname -- "$2")" ] || usage
OUTPUT=$(CDPATH= cd -- "$(dirname -- "$2")" && pwd)/$(basename -- "$2")
STAGE=$(mktemp -d "${TMPDIR:-/tmp}/dia-dmg.XXXXXXXX")
trap 'rm -rf "$STAGE"' EXIT
trap 'exit 129' HUP
trap 'exit 130' INT
trap 'exit 143' TERM
codesign --verify --deep --strict "$APP"
ditto "$APP" "$STAGE/$(basename -- "$APP")"
ln -s /Applications "$STAGE/Applications"
hdiutil create -srcfolder "$STAGE" -volname Dia -format UDZO "$OUTPUT"
hdiutil verify "$OUTPUT"
if [ "$#" -eq 3 ]; then
    xcrun notarytool submit "$OUTPUT" --keychain-profile "$3" --wait --output-format json > "$STAGE/notary-result.json"
    python3 -c 'import json, sys; result=json.load(open(sys.argv[1])); print(result); sys.exit(0 if result.get("status") == "Accepted" else 1)' "$STAGE/notary-result.json"
    xcrun stapler staple "$OUTPUT"
    xcrun stapler validate "$OUTPUT"
fi
