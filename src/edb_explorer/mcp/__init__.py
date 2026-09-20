"""Model Context Protocol server exposing EDB Explorer to AI agents."""

from __future__ import annotations

__all__ = ["build_server", "main"]

from edb_explorer.mcp.server import build_server, main
