#!/usr/bin/env bash
# Registers EDB Explorer in the application menu (double-click / launcher) and on PATH.
# Run from the extracted folder:  ./install.sh        (uninstall with: ./install.sh --uninstall)
set -euo pipefail
APPDIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DATA="${XDG_DATA_HOME:-$HOME/.local/share}"
APPS="$DATA/applications"
ICONS="$DATA/icons/hicolor/256x256/apps"
BIN="$HOME/.local/bin"
DESKTOP="$APPS/edb-explorer.desktop"

if [ "${1:-}" = "--uninstall" ]; then
  rm -f "$DESKTOP" "$ICONS/edb-explorer.png" "$BIN/edb-explorer"
  command -v update-desktop-database >/dev/null 2>&1 && update-desktop-database "$APPS" 2>/dev/null || true
  echo "EDB Explorer removed from the application menu."
  exit 0
fi

chmod +x "$APPDIR/EDB-Explorer" "$APPDIR/edb-explorer"
mkdir -p "$APPS" "$ICONS" "$BIN"
sed "s#@APPDIR@#$APPDIR#g" "$APPDIR/EDB-Explorer.desktop" > "$DESKTOP"
chmod +x "$DESKTOP"
cp "$APPDIR/icon.png" "$ICONS/edb-explorer.png"
ln -sf "$APPDIR/edb-explorer" "$BIN/edb-explorer"
command -v update-desktop-database >/dev/null 2>&1 && update-desktop-database "$APPS" 2>/dev/null || true
command -v gtk-update-icon-cache >/dev/null 2>&1 && gtk-update-icon-cache -q "$DATA/icons/hicolor" 2>/dev/null || true

echo "Installed: 'EDB Explorer' is now in your application menu."
echo "Command line: $BIN/edb-explorer   (make sure ~/.local/bin is on your PATH)"
echo "You can also double-click $APPDIR/EDB-Explorer directly."
