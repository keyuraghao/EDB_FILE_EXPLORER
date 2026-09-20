#!/usr/bin/env bash
# Build the Linux (or macOS) distributable: dist/edb-explorer-<version>-<os>-<arch>.tar.gz
set -euo pipefail
cd "$(dirname "$0")/.."

PY=${PYTHON:-python}
if [ -x .venv/bin/python ]; then PY=.venv/bin/python; fi

VERSION=$($PY -c "import sys; sys.path.insert(0,'src'); import edb_explorer; print(edb_explorer.__version__)")
OS=$(uname -s | tr '[:upper:]' '[:lower:]')
ARCH=$(uname -m)
NAME="edb-explorer-${VERSION}"

echo ">> Building ${NAME} for ${OS}/${ARCH}"
$PY -m PyInstaller packaging/edb-explorer.spec --noconfirm --clean --distpath dist --workpath build/pyinstaller

echo ">> Smoke test"
"dist/${NAME}/edb-explorer" --version
"dist/${NAME}/edb-explorer" timestamp 132565120200137766 >/dev/null

echo ">> Packaging"
cp README.md LICENSE CHANGELOG.md "dist/${NAME}/"
cp packaging/linux/EDB-Explorer.desktop packaging/linux/install.sh "dist/${NAME}/"
cp src/edb_explorer/resources/icon.png "dist/${NAME}/icon.png"
chmod +x "dist/${NAME}/EDB-Explorer" "dist/${NAME}/edb-explorer" "dist/${NAME}/install.sh"
tar -C dist -czf "dist/${NAME}-${OS}-${ARCH}.tar.gz" "${NAME}"
( cd dist && sha256sum "${NAME}-${OS}-${ARCH}.tar.gz" > "${NAME}-${OS}-${ARCH}.tar.gz.sha256" )
echo ">> Done: dist/${NAME}-${OS}-${ARCH}.tar.gz"
