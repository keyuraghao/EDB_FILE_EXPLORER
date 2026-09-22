#!/usr/bin/env bash
# Build the Linux or macOS distributables from the PyInstaller output.
#   Linux:  dist/edb-explorer-<v>-linux-<arch>.tar.gz           (install.sh / uninstall.sh, user-level)
#           dist/EDB-Explorer-<v>-linux-<arch>-portable.tar.gz  (portable.txt marker: state lives in ./data)
#           dist/edb-explorer_<v>_<arch>.deb                    (system install, `apt remove edb-explorer`)
#   macOS:  dist/EDB-Explorer-<v>-macos-<arch>.dmg               (drag to Applications + "Uninstall EDB Explorer.command")
#           dist/EDB-Explorer-<v>-macos-<arch>-portable.zip
set -euo pipefail
cd "$(dirname "$0")/.."

PY=${PYTHON:-python}
if [ -x .venv/bin/python ]; then PY=.venv/bin/python; fi

VERSION=$($PY -c "import sys; sys.path.insert(0,'src'); import edb_explorer; print(edb_explorer.__version__)")
OS=$(uname -s | tr '[:upper:]' '[:lower:]')
[ "$OS" = "darwin" ] && OS=macos
ARCH=$(uname -m)
NAME="edb-explorer-${VERSION}"
MARKER_TEXT="EDB Explorer portable build ${VERSION} - user data is kept in the data folder next to this file."

if [ "$OS" = "macos" ] && [ ! -f src/edb_explorer/resources/icon.icns ] && command -v iconutil >/dev/null; then
  echo ">> Building icon.icns"
  ICONSET=build/icon.iconset
  rm -rf "$ICONSET" && mkdir -p "$ICONSET"
  for s in 16 32 64 128 256 512; do
    sips -z $s $s src/edb_explorer/resources/icon.png --out "$ICONSET/icon_${s}x${s}.png" >/dev/null
    sips -z $((s*2)) $((s*2)) src/edb_explorer/resources/icon.png --out "$ICONSET/icon_${s}x${s}@2x.png" >/dev/null
  done
  iconutil -c icns "$ICONSET" -o src/edb_explorer/resources/icon.icns
fi

echo ">> Building ${NAME} for ${OS}/${ARCH}"
$PY -m PyInstaller packaging/edb-explorer.spec --noconfirm --clean --distpath dist --workpath build/pyinstaller

echo ">> Smoke test"
"dist/${NAME}/edb-explorer" --version
"dist/${NAME}/edb-explorer" timestamp 132565120200137766 >/dev/null

sha() { ( cd dist && shasum -a 256 "$1" > "$1.sha256" 2>/dev/null || sha256sum "$1" > "$1.sha256" ); }

if [ "$OS" = "macos" ]; then
  echo ">> Packaging macOS"
  APP="dist/EDB Explorer.app"
  [ -d "$APP" ] || { echo "PyInstaller did not produce $APP"; exit 1; }
  # a copy of the CLI next to the bundle so `edb-explorer` can be symlinked onto PATH
  STAGE="build/dmg"
  rm -rf "$STAGE" && mkdir -p "$STAGE"
  cp -R "$APP" "$STAGE/"
  ln -s /Applications "$STAGE/Applications"
  cp "packaging/macos/Uninstall EDB Explorer.command" "$STAGE/"
  cp README.md LICENSE CHANGELOG.md "$STAGE/"
  cat > "$STAGE/README-macOS.txt" <<TXT
EDB Explorer ${VERSION} for macOS
* Drag "EDB Explorer.app" to Applications.  The build is not notarised: on first launch right-click the app,
  choose Open, and confirm - or run:  xattr -dr com.apple.quarantine "/Applications/EDB Explorer.app"
* Command line / MCP server:  ln -s "/Applications/EDB Explorer.app/Contents/MacOS/edb-explorer" /usr/local/bin/edb-explorer
* Uninstall: double-click "Uninstall EDB Explorer.command" (removes the app and, optionally, settings, keys and caches).
TXT
  DMG="dist/EDB-Explorer-${VERSION}-macos-${ARCH}.dmg"
  rm -f "$DMG"
  hdiutil create -volname "EDB Explorer ${VERSION}" -srcfolder "$STAGE" -ov -format UDZO "$DMG"
  sha "$(basename "$DMG")"
  PORT="build/EDB-Explorer-${VERSION}-macos-${ARCH}-portable"
  rm -rf "$PORT" && mkdir -p "$PORT"
  cp -R "$APP" "$PORT/"
  echo "$MARKER_TEXT" > "$PORT/portable.txt"
  cp packaging/linux/README-portable.txt "$PORT/README-portable.txt"
  ( cd build && zip -qry "../dist/$(basename "$PORT").zip" "$(basename "$PORT")" )
  sha "$(basename "$PORT").zip"
  echo ">> Done: $DMG and dist/$(basename "$PORT").zip"
  exit 0
fi

echo ">> Packaging Linux"
cp README.md LICENSE CHANGELOG.md "dist/${NAME}/"
cp packaging/linux/EDB-Explorer.desktop packaging/linux/install.sh packaging/linux/uninstall.sh "dist/${NAME}/"
cp src/edb_explorer/resources/icon.png "dist/${NAME}/icon.png"
chmod +x "dist/${NAME}/EDB-Explorer" "dist/${NAME}/edb-explorer" "dist/${NAME}/install.sh" "dist/${NAME}/uninstall.sh"
tar -C dist -czf "dist/${NAME}-${OS}-${ARCH}.tar.gz" "${NAME}"
sha "${NAME}-${OS}-${ARCH}.tar.gz"

PORT="EDB-Explorer-${VERSION}-linux-${ARCH}-portable"
rm -rf "dist/${PORT}" && cp -a "dist/${NAME}" "dist/${PORT}"
rm -f "dist/${PORT}/install.sh" "dist/${PORT}/uninstall.sh" "dist/${PORT}/EDB-Explorer.desktop"
echo "$MARKER_TEXT" > "dist/${PORT}/portable.txt"
cp packaging/linux/README-portable.txt "dist/${PORT}/"
tar -C dist -czf "dist/${PORT}.tar.gz" "${PORT}"
sha "${PORT}.tar.gz"

if command -v dpkg-deb >/dev/null; then
  packaging/linux/build-deb.sh "dist/${NAME}" "${VERSION}"
else
  echo ">> dpkg-deb not found - skipping the .deb"
fi
echo ">> Done: dist/${NAME}-${OS}-${ARCH}.tar.gz, dist/${PORT}.tar.gz"
