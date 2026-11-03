#!/usr/bin/env bash
# Build a single-file AppImage of SoundCloud Library Manager (Linux x86_64).
#
#   packaging/build_appimage.sh            # -> dist/SoundCloud_Library_Manager-<ver>-x86_64.AppImage
#   SKIP_SELFTEST=1 packaging/build_appimage.sh
#
# Steps: venv (+ PyInstaller if missing) -> PyInstaller onedir from packaging/sclm.spec ->
# bundle checks (ldd, libxcb-cursor) -> AppDir -> pinned + SHA-256-verified appimagetool and
# AppImage runtime -> AppImage -> offline self-test of the frozen app.
# Idempotent: every run rebuilds build/pyinstaller and build/AppDir from scratch; downloaded
# tools in build/tools are reused when their checksum matches. Writes only to build/ and dist/.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

# --- pinned tools ---------------------------------------------------------------------------
APPIMAGETOOL_VERSION="1.9.1"
APPIMAGETOOL_URL="https://github.com/AppImage/appimagetool/releases/download/${APPIMAGETOOL_VERSION}/appimagetool-x86_64.AppImage"
APPIMAGETOOL_SHA256="ed4ce84f0d9caff66f50bcca6ff6f35aae54ce8135408b3fa33abfc3cb384eb0"
# Static type2 runtime (needs no libfuse2 at all; uses fusermount/fusermount3).
RUNTIME_VERSION="20251108"
RUNTIME_URL="https://github.com/AppImage/type2-runtime/releases/download/${RUNTIME_VERSION}/runtime-x86_64"
RUNTIME_SHA256="2fca8b443c92510f1483a883f60061ad09b46b978b2631c807cd873a47ec260d"

BUILD="$ROOT/build"
TOOLS="$BUILD/tools"
PYI_DIST="$BUILD/pyinstaller/dist"
PYI_WORK="$BUILD/pyinstaller/work"
APPDIR="$BUILD/AppDir"
DIST="$ROOT/dist"
VENV="$ROOT/.venv"
PY="$VENV/bin/python"

log() { printf '\n==> %s\n' "$*"; }
die() { printf 'build_appimage: ERROR: %s\n' "$*" >&2; exit 1; }

[[ "$(uname -m)" == "x86_64" ]] || die "x86_64 only (this machine: $(uname -m))"
START=$(date +%s)

VERSION="$(sed -n 's/^__version__ = "\(.*\)"$/\1/p' src/sclm/__init__.py)"
[[ -n "$VERSION" ]] || die "could not read __version__ from src/sclm/__init__.py"
OUT="$DIST/SoundCloud_Library_Manager-${VERSION}-x86_64.AppImage"
log "SoundCloud Library Manager $VERSION"

# --- 1. virtualenv --------------------------------------------------------------------------
if [[ ! -x "$PY" ]]; then
  log "Creating $VENV"
  python3 -m venv "$VENV"
  "$PY" -m pip install -q --upgrade pip
  # Runtime deps + beets (optional, bundled) + pytest + PyInstaller, all pinned.
  "$PY" -m pip install -q -r requirements-dev.txt
fi
if ! "$PY" -c "import PyInstaller" 2>/dev/null; then
  PYI_PIN="$(grep -iE '^pyinstaller==' requirements-dev.txt || echo 'pyinstaller==6.22.3')"
  log "Installing $PYI_PIN into $VENV"
  "$PY" -m pip install -q "$PYI_PIN"
fi
"$PY" -c "import PySide6.QtMultimedia, yt_dlp, mutagen, imageio_ffmpeg, certifi" \
  || die "runtime dependencies missing in $VENV (pip install -r requirements.txt)"
"$PY" -c "import beets" 2>/dev/null \
  || echo "note: beets is not installed in the venv; the AppImage will run without the optional beets step"

# --- 2. PyInstaller (onedir) ----------------------------------------------------------------
log "PyInstaller: packaging/sclm.spec"
rm -rf "$PYI_DIST" "$PYI_WORK"
"$PY" -m PyInstaller --noconfirm --clean --log-level WARN \
  --distpath "$PYI_DIST" --workpath "$PYI_WORK" packaging/sclm.spec
BUNDLE="$PYI_DIST/sclm"
[[ -x "$BUNDLE/sclm" ]] || die "PyInstaller produced no $BUNDLE/sclm"

# --- 3. bundle checks -----------------------------------------------------------------------
log "Checking shared-library resolution inside the bundle"
missing=0
while IFS= read -r -d '' so; do
  if ldd "$so" 2>/dev/null | grep -q 'not found'; then
    echo "unresolved dependency in ${so#$BUNDLE/}:"; ldd "$so" | grep 'not found'; missing=1
  fi
done < <(find "$BUNDLE/_internal" -type f -name '*.so*' -print0)
[[ $missing -eq 0 ]] || die "unresolved shared-library dependencies (see above)"
QXCB="$BUNDLE/_internal/PySide6/Qt/plugins/platforms/libqxcb.so"
XCB_CURSOR_LINE="$(ldd "$QXCB" | grep 'libxcb-cursor.so.0' || true)"
echo "libqxcb.so: $XCB_CURSOR_LINE"
[[ "$XCB_CURSOR_LINE" == *"$BUNDLE/_internal/"* ]] || die "libxcb-cursor.so.0 does not resolve inside the bundle"

# --- 4. AppDir ------------------------------------------------------------------------------
log "Assembling $APPDIR"
rm -rf "$APPDIR"
mkdir -p "$APPDIR/usr/lib" "$APPDIR/usr/bin" "$APPDIR/usr/share/applications" \
  "$APPDIR/usr/share/icons/hicolor/scalable/apps" "$APPDIR/usr/share/icons/hicolor/256x256/apps"
cp -a "$BUNDLE" "$APPDIR/usr/lib/sclm"
ln -s ../lib/sclm/sclm "$APPDIR/usr/bin/sclm"
install -m 755 packaging/appimage/AppRun "$APPDIR/AppRun"
install -m 644 packaging/appimage/sclm.desktop "$APPDIR/sclm.desktop"
install -m 644 packaging/appimage/sclm.desktop "$APPDIR/usr/share/applications/sclm.desktop"
install -m 644 packaging/sclm.svg "$APPDIR/sclm.svg"
install -m 644 packaging/sclm.svg "$APPDIR/usr/share/icons/hicolor/scalable/apps/sclm.svg"
QT_QPA_PLATFORM=offscreen "$PY" packaging/render_icon.py packaging/sclm.svg \
  "$APPDIR/usr/share/icons/hicolor/256x256/apps/sclm.png" 256
cp "$APPDIR/usr/share/icons/hicolor/256x256/apps/sclm.png" "$APPDIR/sclm.png"
ln -s sclm.png "$APPDIR/.DirIcon"
if command -v desktop-file-validate >/dev/null; then
  desktop-file-validate "$APPDIR/sclm.desktop" && echo "desktop-file-validate: OK"
else
  echo "note: desktop-file-validate not installed; skipped"
fi

# --- 5. pinned tools ------------------------------------------------------------------------
fetch() {  # fetch <url> <sha256> <dest>
  local url="$1" sum="$2" dest="$3"
  if [[ -f "$dest" ]] && echo "$sum  $dest" | sha256sum -c --status; then
    echo "cached: ${dest#$ROOT/}"; return
  fi
  rm -f "$dest" "$dest.part"
  echo "downloading $url"
  curl -fL --retry 3 --proto '=https' -o "$dest.part" "$url"
  echo "$sum  $dest.part" | sha256sum -c --status \
    || { rm -f "$dest.part"; die "SHA-256 mismatch for $url"; }
  mv "$dest.part" "$dest"
}
mkdir -p "$TOOLS"
fetch "$APPIMAGETOOL_URL" "$APPIMAGETOOL_SHA256" "$TOOLS/appimagetool-${APPIMAGETOOL_VERSION}-x86_64.AppImage"
fetch "$RUNTIME_URL" "$RUNTIME_SHA256" "$TOOLS/runtime-x86_64-${RUNTIME_VERSION}"
chmod 755 "$TOOLS/appimagetool-${APPIMAGETOOL_VERSION}-x86_64.AppImage"

# --- 6. AppImage ----------------------------------------------------------------------------
log "appimagetool -> ${OUT#$ROOT/}"
mkdir -p "$DIST"
rm -f "$OUT"
# Extract-and-run: works even where FUSE is unavailable in the build shell.
APPIMAGE_EXTRACT_AND_RUN=1 ARCH=x86_64 VERSION="$VERSION" \
  "$TOOLS/appimagetool-${APPIMAGETOOL_VERSION}-x86_64.AppImage" --no-appstream \
  --runtime-file "$TOOLS/runtime-x86_64-${RUNTIME_VERSION}" --comp zstd \
  "$APPDIR" "$OUT"
chmod 755 "$OUT"

# --- 7. offline self-test of the frozen app (no window, temp dirs only) ----------------------
if [[ "${SKIP_SELFTEST:-0}" != "1" ]]; then
  log "Self-test (onedir bundle, offline)"
  "$BUNDLE/sclm" --self-test
fi

log "Done in $(( $(date +%s) - START ))s"
echo "AppImage: $OUT ($(du -h "$OUT" | cut -f1))"
echo "AppDir:   $APPDIR ($(du -sh "$APPDIR" | cut -f1))"
