#!/usr/bin/env bash
# Installs a per-user desktop launcher for SoundCloud Library Manager.
# Usage: packaging/install.sh            (install)
#        packaging/install.sh --uninstall
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
APPS="${XDG_DATA_HOME:-$HOME/.local/share}/applications"
ICONS="${XDG_DATA_HOME:-$HOME/.local/share}/icons/hicolor/scalable/apps"
DESKTOP="$APPS/sclm.desktop"

if [[ "${1:-}" == "--uninstall" ]]; then
  rm -f "$DESKTOP" "$ICONS/sclm.svg"
  command -v update-desktop-database >/dev/null && update-desktop-database "$APPS" || true
  echo "Launcher removed (venv and library untouched)."; exit 0
fi

if [[ ! -x "$ROOT/.venv/bin/python" ]]; then
  echo "Creating virtualenv in $ROOT/.venv ..."
  python3 -m venv "$ROOT/.venv"
  "$ROOT/.venv/bin/pip" install -q --upgrade pip
fi
"$ROOT/.venv/bin/pip" install -q -r "$ROOT/requirements.txt"
"$ROOT/.venv/bin/pip" install -q -e "$ROOT"

mkdir -p "$APPS" "$ICONS"
install -m 644 "$ROOT/packaging/sclm.svg" "$ICONS/sclm.svg"
sed -e "s|@PYTHON@|$ROOT/.venv/bin/python|g" "$ROOT/packaging/sclm.desktop.in" > "$DESKTOP"
chmod 644 "$DESKTOP"
command -v desktop-file-validate >/dev/null && desktop-file-validate "$DESKTOP" || true
command -v update-desktop-database >/dev/null && update-desktop-database "$APPS" || true
echo "Installed launcher: $DESKTOP"
command -v ffmpeg >/dev/null || echo "Note: system ffmpeg not found; using the bundled imageio-ffmpeg binary. 'sudo apt install ffmpeg' is recommended."
if command -v ldconfig >/dev/null 2>&1 && ! ldconfig -p 2>/dev/null | grep -q 'libxcb-cursor\.so\.0'; then
  echo "Warning: libxcb-cursor.so.0 is missing; the Qt window system plugin cannot load without it."
  echo "  Run: sudo apt install libxcb-cursor0"
fi
