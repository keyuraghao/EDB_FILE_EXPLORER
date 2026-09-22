#!/usr/bin/env bash
# Uninstall EDB Explorer that was set up with ./install.sh (user-level, from an extracted tarball).
# Removes the menu entry, icon and ~/.local/bin link; optionally your settings, keys and caches; optionally
# the extracted folder itself.   Portable builds: just delete the folder (everything lives in ./data).
set -euo pipefail
APPDIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
"$APPDIR/install.sh" --uninstall
echo
read -r -p "Also remove your settings, project signing key, trusted signers and caches? [y/N] " purge
if [[ "${purge,,}" == "y" ]]; then
  rm -rf "${XDG_CONFIG_HOME:-$HOME/.config}/EDB Explorer" "${XDG_CONFIG_HOME:-$HOME/.config}/edb-explorer" \
         "${XDG_CACHE_HOME:-$HOME/.cache}/edb-explorer" "${TMPDIR:-/tmp}"/edb-rows-* "${TMPDIR:-/tmp}"/edb-sql-* 2>/dev/null || true
  echo "User data removed."
fi
read -r -p "Delete the application folder $APPDIR as well? [y/N] " wipe
if [[ "${wipe,,}" == "y" ]]; then
  cd /
  rm -rf "$APPDIR"
  echo "EDB Explorer has been removed."
else
  echo "Application files left in $APPDIR."
fi
