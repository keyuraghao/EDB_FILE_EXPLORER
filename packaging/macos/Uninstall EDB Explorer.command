#!/bin/bash
# Uninstall EDB Explorer on macOS: removes the app bundle and, if you want, your settings, keys and caches.
set -euo pipefail
echo "EDB Explorer - uninstall"
echo
APP="/Applications/EDB Explorer.app"
if [ -d "$APP" ]; then
  read -r -p "Remove $APP? [y/N] " a
  if [[ "${a,,}" == "y" ]]; then rm -rf "$APP"; echo "Application removed."; fi
else
  echo "No $APP found (a portable copy is removed by deleting its folder)."
fi
read -r -p "Also remove your settings, project signing key, trusted signers and caches? [y/N] " purge
if [[ "${purge,,}" == "y" ]]; then
  rm -rf "$HOME/Library/Preferences/local.edb-explorer."*.plist "$HOME/Library/Preferences/EDB Explorer" \
         "$HOME/.config/edb-explorer" "${TMPDIR:-/tmp}"/edb-rows-* "${TMPDIR:-/tmp}"/edb-sql-* 2>/dev/null || true
  defaults delete "local.edb-explorer.EDB Explorer" >/dev/null 2>&1 || true
  echo "User data removed."
fi
echo "Done."
