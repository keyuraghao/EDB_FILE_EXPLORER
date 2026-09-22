# Packaging

| File | Purpose |
|---|---|
| `edb-explorer.spec` | PyInstaller spec producing `EDB-Explorer` (GUI) and `edb-explorer` (CLI + MCP) in one folder |
| `build.sh` | Linux: `edb-explorer-<ver>-linux-<arch>.tar.gz` (install.sh / uninstall.sh), `EDB-Explorer-<ver>-linux-<arch>-portable.tar.gz`, `edb-explorer_<ver>_<arch>.deb` (via `linux/build-deb.sh`). macOS: `EDB-Explorer-<ver>-macos-<arch>.dmg` (+ `macos/Uninstall EDB Explorer.command`) and `-portable.zip` |
| `build.ps1` | Windows: `EDB-Explorer-<ver>-setup.exe` (Inno Setup, `installer.iss`; the uninstaller can purge user data) and `EDB-Explorer-<ver>-windows-x64-portable.zip` (`windows/Uninstall.cmd`, `portable.txt`) |
| `portable.txt` | Present only in portable builds: `edb_explorer/portable.py` then keeps settings, keys and caches in `./data` |
| `Dockerfile` | Headless MCP server image (Streamable HTTP on :8765, evidence mounted read-only) |

The GitHub Actions release workflow (`.github/workflows/release.yml`) runs both build scripts on a
tag push (`v*`) and attaches the archives, checksums and the Python wheel to the release.
