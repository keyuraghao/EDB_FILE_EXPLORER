# Packaging

| File | Purpose |
|---|---|
| `edb-explorer.spec` | PyInstaller spec producing `EDB-Explorer` (GUI) and `edb-explorer` (CLI + MCP) in one folder |
| `build.sh` | Linux / macOS build → `dist/edb-explorer-<ver>-<os>-<arch>.tar.gz` |
| `build.ps1` | Windows build → `dist\edb-explorer-<ver>-windows-x64.zip` |
| `Dockerfile` | Headless MCP server image (Streamable HTTP on :8765, evidence mounted read-only) |

The GitHub Actions release workflow (`.github/workflows/release.yml`) runs both build scripts on a
tag push (`v*`) and attaches the archives, checksums and the Python wheel to the release.
