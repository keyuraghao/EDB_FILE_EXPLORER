"""Core, UI-agnostic parsing layer shared by the GUI, CLI and MCP server."""

from __future__ import annotations

from edb_explorer.core.database import EdbDatabase
from edb_explorer.core.exceptions import (
    DatabaseNotFoundError,
    EdbExplorerError,
    InvalidDatabaseError,
    PathNotAllowedError,
    TableNotFoundError,
)
from edb_explorer.core.models import (
    ColumnInfo,
    DatabaseInfo,
    IndexInfo,
    RecordPage,
    SearchHit,
    TableInfo,
)
from edb_explorer.core.session import Session

__all__ = [
    "ColumnInfo",
    "DatabaseInfo",
    "DatabaseNotFoundError",
    "EdbDatabase",
    "EdbExplorerError",
    "IndexInfo",
    "InvalidDatabaseError",
    "PathNotAllowedError",
    "RecordPage",
    "SearchHit",
    "Session",
    "TableInfo",
    "TableNotFoundError",
]
