"""Backend protocol: every supported file format implements this small interface.

The rest of the application (Database, Session, GUI, CLI, MCP, export, report,
analysis) only talks to :class:`Backend`, so adding a format means adding one
module here and registering its signature in ``detect.py``.
"""

from __future__ import annotations

import os
from abc import ABC, abstractmethod
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from edb_explorer.core.models import ColumnInfo, IndexInfo


@dataclass(slots=True)
class BackendTable:
    """Schema of one table as reported by a backend."""

    name: str
    columns: list[ColumnInfo]
    indexes: list[IndexInfo] = field(default_factory=list)
    root_page: int = 0
    is_system: bool = False
    record_count: int | None = None  # only when the format stores it cheaply
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class BackendInfo:
    """Format-level metadata (everything optional except kind)."""

    kind: str
    kind_name: str
    page_size: int = 0
    format_version: int = 0
    format_revision: int = 0
    created_version: int = 0
    created_revision: int = 0
    state: str = ""
    created: datetime | None = None
    last_attach: datetime | None = None
    last_detach: datetime | None = None
    windows_version: str | None = None
    encoding: str | None = None
    header: dict[str, Any] = field(default_factory=dict)
    sidecars: list[str] = field(default_factory=list)  # e.g. -wal, -journal, .ldb files


class Backend(ABC):
    """Read-only access to one database file (or directory, for LevelDB)."""

    kind: str = "unknown"
    kind_name: str = "Unknown"
    #: True when the backend can produce a native read-only sqlite3 connection (SQL console fast path).
    native_sql: bool = False

    def __init__(self, path: str | os.PathLike[str]) -> None:
        self.path = Path(path)

    # -- lifecycle --------------------------------------------------------- #
    @abstractmethod
    def close(self) -> None: ...

    # -- metadata ---------------------------------------------------------- #
    @abstractmethod
    def info(self) -> BackendInfo: ...

    @abstractmethod
    def tables(self) -> list[BackendTable]: ...

    # -- records ----------------------------------------------------------- #
    @abstractmethod
    def iter_rows(self, table: str) -> Iterator[dict[str, Any]]:
        """Yield raw row dicts in storage order. Missing/null columns may be omitted."""

    def count(self, table: str) -> int | None:
        """Cheap exact count if the format provides one, else None (caller walks the table)."""
        return None

    def sql_connection(self) -> Any | None:
        """A read-only ``sqlite3.Connection`` on the file itself, when ``native_sql`` is True."""
        return None

    def size_bytes(self) -> int:
        try:
            if self.path.is_dir():
                return sum(p.stat().st_size for p in self.path.rglob("*") if p.is_file())
            return self.path.stat().st_size
        except OSError:
            return 0


def sanitize_columns(names: list[str]) -> list[str]:
    """Make column names unique and non-empty (some formats allow duplicates/blank names)."""
    seen: dict[str, int] = {}
    out: list[str] = []
    for i, raw in enumerate(names):
        name = (raw or "").strip() or f"column_{i + 1}"
        if name in seen:
            seen[name] += 1
            name = f"{name}_{seen[name]}"
        else:
            seen[name] = 1
        out.append(name)
    return out
