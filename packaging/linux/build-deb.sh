#!/usr/bin/env bash
# Build a Debian package from the PyInstaller output: dist/edb-explorer_<version>_<arch>.deb
# Installs to /opt/edb-explorer with a menu entry and /usr/bin/edb-explorer; remove with `apt remove edb-explorer`.
set -euo pipefail
NAME="$1"          # dist/edb-explorer-<version>
VERSION="$2"
ARCH=$(dpkg --print-architecture 2>/dev/null || echo amd64)
PKG="build/deb/edb-explorer"
rm -rf "$PKG"
mkdir -p "$PKG/DEBIAN" "$PKG/opt/edb-explorer" "$PKG/usr/bin" "$PKG/usr/share/applications" \
         "$PKG/usr/share/icons/hicolor/256x256/apps" "$PKG/usr/share/doc/edb-explorer"
cp -a "$NAME"/. "$PKG/opt/edb-explorer/"
rm -f "$PKG/opt/edb-explorer/install.sh" "$PKG/opt/edb-explorer/uninstall.sh" "$PKG/opt/edb-explorer/EDB-Explorer.desktop"
ln -s /opt/edb-explorer/edb-explorer "$PKG/usr/bin/edb-explorer"
ln -s /opt/edb-explorer/EDB-Explorer "$PKG/usr/bin/EDB-Explorer"
sed "s#@APPDIR@#/opt/edb-explorer#g" packaging/linux/EDB-Explorer.desktop > "$PKG/usr/share/applications/edb-explorer.desktop"
cp src/edb_explorer/resources/icon.png "$PKG/usr/share/icons/hicolor/256x256/apps/edb-explorer.png"
cp README.md CHANGELOG.md LICENSE "$PKG/usr/share/doc/edb-explorer/"
SIZE=$(du -sk "$PKG/opt" | cut -f1)
cat > "$PKG/DEBIAN/control" <<CONTROL
Package: edb-explorer
Version: $VERSION
Section: utils
Priority: optional
Architecture: $ARCH
Installed-Size: $SIZE
Maintainer: Keyur Aghao <kaghao@andrew.cmu.edu>
Depends: libegl1, libgl1, libxkbcommon0, libdbus-1-3, libfontconfig1, libxcb-cursor0 | libxcb1
Homepage: https://github.com/keyuraghao/EDB_FILE_EXPLORER
Description: Forensic explorer for ESE, SQLite, LevelDB, Access, DBF and Windows Event Log files
 Read-only GUI, CLI and MCP server for ntds.dit, SRUM, Exchange, WebCache, browser and
 mobile databases, with SQL analysis, timelines, reports and signed project files.
CONTROL
cat > "$PKG/DEBIAN/postinst" <<'POST'
#!/bin/sh
set -e
command -v update-desktop-database >/dev/null 2>&1 && update-desktop-database -q /usr/share/applications || true
command -v gtk-update-icon-cache >/dev/null 2>&1 && gtk-update-icon-cache -q /usr/share/icons/hicolor || true
exit 0
POST
cat > "$PKG/DEBIAN/postrm" <<'POST'
#!/bin/sh
set -e
command -v update-desktop-database >/dev/null 2>&1 && update-desktop-database -q /usr/share/applications || true
exit 0
POST
chmod 755 "$PKG/DEBIAN/postinst" "$PKG/DEBIAN/postrm"
OUT="dist/edb-explorer_${VERSION}_${ARCH}.deb"
dpkg-deb --build --root-owner-group "$PKG" "$OUT"
( cd dist && sha256sum "$(basename "$OUT")" > "$(basename "$OUT").sha256" )
echo ">> Done: $OUT"
