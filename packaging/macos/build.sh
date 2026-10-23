#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$ROOT"
PYTHON="${P011_BUILD_PYTHON:-$ROOT/build/recognition-preparation/venv-macos-cp312/bin/python}"
CONFIGURATION_ARGS=("$ROOT/packaging/check_release_configuration.py")
CHECK_CONFIGURATION=0
for arg in "$@"; do
  case "$arg" in
    --official) CONFIGURATION_ARGS+=(--official) ;;
    --check-configuration) CHECK_CONFIGURATION=1 ;;
    *) echo "Unknown build option: $arg" >&2; exit 2 ;;
  esac
done
"$PYTHON" "${CONFIGURATION_ARGS[@]}"
[[ "$CHECK_CONFIGURATION" == 0 ]] || exit 0
[[ "$(uname -s)" == Darwin && "$(uname -m)" == arm64 ]] || { echo "This recognition candidate is for macOS arm64" >&2; exit 1; }
RESOURCE_DIR="$ROOT/packaging/resources"
export P011_FFMPEG="${P011_FFMPEG:-$RESOURCE_DIR/bin/ffmpeg}"
export P011_FFPROBE="${P011_FFPROBE:-$RESOURCE_DIR/bin/ffprobe}"
WHISPER_DIR="${P011_WHISPER_DIR:-$ROOT/build/recognition-preparation/macos-whisper-cpu-build/bin}"
BUILD_DIST="${P011_BUILD_DIST:-$ROOT/dist/mac-webui}"
BROWSER_ROOT="${PLAYWRIGHT_BROWSERS_PATH:-$HOME/Library/Caches/ms-playwright}"
export PYTHONPATH="$ROOT/src"
[[ -f "$RESOURCE_DIR/THIRD_PARTY_NOTICES.txt" ]] || { echo "packaging/resources notices missing" >&2; exit 1; }
for tool in "$P011_FFMPEG" "$P011_FFPROBE"; do
  [[ -x "$tool" ]] || { echo "Missing executable media tool: $tool" >&2; exit 1; }
done
# Preserve the existing subtitle and text rendering features before replacing
# any build. A decodable FFmpeg binary can still lack the required filters.
if ! "$P011_FFMPEG" -hide_banner -filters 2>/dev/null | awk '$2=="subtitles" {subtitles=1} $2=="drawtext" {drawtext=1} END {exit !(subtitles && drawtext)}'; then
  echo "Selected FFmpeg lacks required subtitles/drawtext filters; choose the complete media tool." >&2
  exit 1
fi
# Analysis preserves each source basename. Give it real, verified canonical
# copies in a unique build directory outside its --clean workpath.
mkdir -p "$ROOT/build"
MEDIA_STAGE="$(mktemp -d "$ROOT/build/mac-media-tools.XXXXXX")"
"$PYTHON" "$ROOT/packaging/macos/media_bundle.py" --ffmpeg "$P011_FFMPEG" --ffprobe "$P011_FFPROBE" --output-root "$MEDIA_STAGE"
export P011_FFMPEG="$MEDIA_STAGE/ffmpeg"
export P011_FFPROBE="$MEDIA_STAGE/ffprobe"
"$PYTHON" -c 'import webview, WebKit, PyInstaller, sqlalchemy, alembic, pydantic, openpyxl, rapidocr, onnxruntime'
"$PYTHON" "$ROOT/packaging/recognition_bundle.py" prepare --platform macos_arm64 --source-root "$WHISPER_DIR" --output-root "$RESOURCE_DIR"
"$PYTHON" "$ROOT/packaging/macos/browser_bundle.py" preflight --browser-root "$BROWSER_ROOT"
"$PYTHON" -m PyInstaller --noconfirm --clean --distpath "$BUILD_DIST" --workpath "$ROOT/build/mac-webui" "$ROOT/packaging/macos/yijianjifa.spec"
APP_PATH="$BUILD_DIST/一剪即发.app"
"$PYTHON" "${CONFIGURATION_ARGS[@]}" --frozen-root "$APP_PATH/Contents/Frameworks"
"$PYTHON" "$ROOT/packaging/macos/browser_bundle.py" stage --browser-root "$BROWSER_ROOT" --app-root "$APP_PATH"
"$PYTHON" "$ROOT/packaging/recognition_bundle.py" finalize --platform macos_arm64 --output-root "$RESOURCE_DIR" --frozen-root "$APP_PATH/Contents/Frameworks"
# PyInstaller has already signed inner binaries. Seal the finalized resource
# inventory by signing only the outer development app; no commercial signature.
codesign --force --sign - "$APP_PATH"
codesign --verify --deep --strict "$APP_PATH"
"$PYTHON" "$ROOT/packaging/macos/browser_bundle.py" check --app-root "$APP_PATH"
# Verify the final relocated tools, including their dynamic-library paths.
# Copying a previously bundled executable can otherwise pass inventory checks
# while its dependencies remain absent from the new application.
MEDIA_FIXTURE="$ROOT/src/yijianjifa/webui/assets/capability-test.mp4"
"$APP_PATH/Contents/Frameworks/bin/ffprobe" -v error -show_entries format=duration -of default=noprint_wrappers=1:nokey=1 "$MEDIA_FIXTURE" >/dev/null
"$APP_PATH/Contents/Frameworks/bin/ffmpeg" -hide_banner -loglevel error -i "$MEDIA_FIXTURE" -f null -
echo "Built development app; signing, licenses and target-host acceptance are separate release gates."
