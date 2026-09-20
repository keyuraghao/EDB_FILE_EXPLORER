"""Plain data models describing databases, tables, columns and query results.

Everything here is a frozen dataclass with a ``to_dict`` helper so it can be
serialised to JSON for the MCP server and CLI without any Qt or dissect types
leaking out of the core layer.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any

JsonValue = None | bool | int | float | str | list[Any] | dict[str, Any]


def _clean(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, dict):
        return {k: _clean(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [_clean(v) for v in value]
    return value


@dataclass(frozen=True, slots=True)
class ColumnInfo:
    """Schema information for a single table column."""

    identifier: int
    name: str
    type: str
    type_id: int
    storage: str  # fixed | variable | tagged
    size: int | None
    encoding: str | None
    is_text: bool
    is_binary: bool

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class IndexInfo:
    """Schema information for a table index."""

    name: str
    is_primary: bool
    is_unique: bool
    columns: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["columns"] = list(self.columns)
        return d


@dataclass(frozen=True, slots=True)
class TableInfo:
    """Schema information for a table."""

    name: str
    display_name: str
    root_page: int
    columns: tuple[ColumnInfo, ...]
    indexes: tuple[IndexInfo, ...]
    is_system: bool
    description: str | None = None
    record_count: int | None = None  # populated lazily - counting requires a full scan

    @property
    def column_names(self) -> list[str]:
        return [c.name for c in self.columns]

    def to_dict(self, include_columns: bool = True) -> dict[str, Any]:
        d: dict[str, Any] = {
            "name": self.name,
            "display_name": self.display_name,
            "description": self.description,
            "root_page": self.root_page,
            "is_system": self.is_system,
            "column_count": len(self.columns),
            "index_count": len(self.indexes),
            "record_count": self.record_count,
        }
        if include_columns:
            d["columns"] = [c.to_dict() for c in self.columns]
            d["indexes"] = [i.to_dict() for i in self.indexes]
        return d


@dataclass(frozen=True, slots=True)
class DatabaseInfo:
    """Metadata about an opened database file."""

    id: str
    path: str
    file_name: str
    size_bytes: int
    page_size: int
    format_version: int
    format_revision: int
    created_version: int
    created_revision: int
    state: str
    profile_id: str
    profile_name: str
    table_count: int
    kind: str = "ese"
    kind_name: str = "Microsoft ESE / JET Blue"
    created: datetime | None = None
    last_attach: datetime | None = None
    last_detach: datetime | None = None
    windows_version: str | None = None
    encoding: str | None = None
    sha256: str | None = None
    sidecars: list[str] = field(default_factory=list)
    header: dict[str, Any] = field(default_factory=dict)

    def to_dict(self, include_header: bool = False) -> dict[str, Any]:
        d = _clean(asdict(self))
        if not include_header:
            d.pop("header", None)
        return d


@dataclass(frozen=True, slots=True)
class RecordPage:
    """A page of decoded records from a table."""

    table: str
    columns: list[str]
    rows: list[dict[str, Any]]
    offset: int
    limit: int
    has_more: bool
    total: int | None = None
    scanned: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "table": self.table,
            "columns": self.columns,
            "offset": self.offset,
            "limit": self.limit,
            "returned": len(self.rows),
            "has_more": self.has_more,
            "total": self.total,
            "scanned": self.scanned,
            "rows": _clean(self.rows),
        }


@dataclass(frozen=True, slots=True)
class SearchHit:
    """A single search match."""

    database: str
    table: str
    row_index: int
    column: str
    value: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
