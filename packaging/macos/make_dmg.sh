#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
DIST="${P011_BUILD_DIST:-$ROOT/dist/mac-webui}"
APP="$DIST/一剪即发.app"
OUT="${P011_DMG_OUTPUT:-$DIST/YiJianJiFa-Mac-Development.dmg}"
[[ -d "$APP" ]] || { echo "Build the WebView app first: packaging/macos/build.sh" >&2; exit 1; }
[[ ! -e "$OUT" ]] || { echo "Output already exists; choose another P011_DMG_OUTPUT: $OUT" >&2; exit 1; }
STAGE="$(mktemp -d "${TMPDIR:-/tmp}/p011-dmg.XXXXXX")"
trap 'rm -rf "$STAGE"' EXIT
ditto "$APP" "$STAGE/一剪即发.app"
ln -s /Applications "$STAGE/Applications"
hdiutil create -volname "一剪即发开发版" -srcfolder "$STAGE" -format UDZO "$OUT"
echo "Created development DMG: $OUT"
