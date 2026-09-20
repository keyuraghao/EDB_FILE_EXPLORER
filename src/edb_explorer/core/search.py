"""Row filtering and cross-table text search."""

from __future__ import annotations

import re
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from typing import Any

from edb_explorer.core.database import EdbDatabase, RowFilter
from edb_explorer.core.models import SearchHit
from edb_explorer.core.values import display_value


@dataclass(slots=True)
class FilterSpec:
    """Declarative row filter.

    * ``text`` - case-insensitive substring (or regex) matched against every
      column's displayed value.
    * ``columns`` - per-column substring matches, all of which must hold.
    * ``equals`` - per-column exact matches on the displayed value.
    """

    text: str | None = None
    columns: dict[str, str] = field(default_factory=dict)
    equals: dict[str, Any] = field(default_factory=dict)
    regex: bool = False
    case_sensitive: bool = False

    def is_empty(self) -> bool:
        return not (self.text or self.columns or self.equals)

    def compile(self, column_types: dict[str, str] | None = None) -> RowFilter | None:
        if self.is_empty():
            return None
        types = column_types or {}
        flags = 0 if self.case_sensitive else re.IGNORECASE

        def matcher(pattern: str) -> Callable[[str], bool]:
            if self.regex:
                rx = re.compile(pattern, flags)
                return lambda s: rx.search(s) is not None
            if self.case_sensitive:
                return lambda s: pattern in s
            low = pattern.lower()
            return lambda s: low in s.lower()

        global_match = matcher(self.text) if self.text else None
        col_matchers = {c: matcher(p) for c, p in self.columns.items()}
        equals = {c: str(v) for c, v in self.equals.items()}

        def _display(col: str, value: Any) -> str:
            return display_value(value, types.get(col), max_length=100_000)

        def row_filter(row: dict[str, Any]) -> bool:
            for col, expected in equals.items():
                if _display(col, row.get(col)) != expected:
                    return False
            for col, m in col_matchers.items():
                if not m(_display(col, row.get(col))):
                    return False
            if global_match is not None:
                return any(global_match(_display(k, v)) for k, v in row.items() if v is not None)
            return True

        return row_filter


def search_database(
    db: EdbDatabase,
    query: str,
    tables: list[str] | None = None,
    columns: list[str] | None = None,
    regex: bool = False,
    case_sensitive: bool = False,
    include_system: bool = False,
    limit: int = 200,
    should_stop: Callable[[], bool] | None = None,
) -> Iterator[SearchHit]:
    """Yield matches of ``query`` across tables.  Stops after ``limit`` hits."""
    if regex:
        rx = re.compile(query, 0 if case_sensitive else re.IGNORECASE)
        match: Callable[[str], bool] = lambda s: rx.search(s) is not None  # noqa: E731
    elif case_sensitive:
        match = lambda s: query in s  # noqa: E731
    else:
        q = query.lower()
        match = lambda s: q in s.lower()  # noqa: E731

    names = [db.resolve_table_name(t) for t in tables] if tables else db.table_names(include_system)
    found = 0
    for table_name in names:
        if should_stop and should_stop():
            return
        types = db.column_types(table_name)
        wanted = [c for c in (columns or []) if c in types] or None
        if columns and not wanted:
            continue
        for index, values in db.iter_records(table_name, columns=wanted):
            if should_stop and should_stop():
                return
            for col, val in values.items():
                if val is None:
                    continue
                text = display_value(val, types.get(col), max_length=100_000)
                if match(text):
                    yield SearchHit(db.id, table_name, index, col, text[:300])
                    found += 1
                    if found >= limit:
                        return
