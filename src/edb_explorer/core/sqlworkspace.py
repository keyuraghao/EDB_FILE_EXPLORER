"""SQL workspace: run read-only SQL across any open database, whatever its format.

Each database is attached to one in-process SQLite connection under a schema
named after its id.  Tables are *materialised* on demand into a temporary
file (values decoded the same way the GUI shows them - timestamps become ISO
strings, blobs become text where possible), so ``SELECT ... FROM urls`` works
for a Chrome History file and ``SELECT ... FROM "SruDbIdMapTable"`` for an
ESE file alike, and tables from different databases can be joined.
"""

from __future__ import annotations

import json
import logging
import os
import re
import sqlite3
import tempfile
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from edb_explorer.core.database import Database
from edb_explorer.core.exceptions import EdbExplorerError, TableNotFoundError
from edb_explorer.core.profiles import ArtifactView
from edb_explorer.core.values import normalize_value

log = logging.getLogger(__name__)

_PLACEHOLDER = re.compile(r"\{t:(\{[^}]*\}|[^{}]+)\}")
_SCHEMA_SAFE = re.compile(r"[^A-Za-z0-9_]")
ProgressCallback = Callable[[str, int], bool]


class SqlError(EdbExplorerError):
    """A query failed."""


@dataclass(slots=True)
class QueryResult:
    columns: list[str]
    rows: list[tuple[Any, ...]]
    truncated: bool
    elapsed: float
    sql: str
    row_count_hint: int | None = None

    def to_dict(self, max_value_length: int | None = None) -> dict[str, Any]:
        def clip(v: Any) -> Any:
            if isinstance(v, bytes):
                v = "0x" + v.hex()
            if max_value_length and isinstance(v, str) and len(v) > max_value_length:
                return v[:max_value_length] + f"… [+{len(v) - max_value_length} chars]"
            return v

        return {
            "columns": self.columns,
            "returned": len(self.rows),
            "truncated": self.truncated,
            "elapsed_seconds": round(self.elapsed, 3),
            "rows": [dict(zip(self.columns, (clip(v) for v in r), strict=False)) for r in self.rows],
        }


@dataclass(slots=True)
class _Attached:
    db: Database
    schema: str
    path: str
    tables: set[str] = field(default_factory=set)


def quote_ident(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _cell(value: Any, ctype: str | None) -> Any:
    if value is None or isinstance(value, bool | int | float | str):
        if isinstance(value, bool):
            return int(value)
        if isinstance(value, int | float) and (ctype == "DateTime" or (ctype or "").startswith("ts:")):
            return normalize_value(value, ctype)
        return value
    if isinstance(value, bytes | bytearray | memoryview):
        decoded = normalize_value(bytes(value), ctype, "smart")
        return decoded if isinstance(decoded, str) and not decoded.startswith("0x") else bytes(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, list | dict | tuple):
        return json.dumps(normalize_value(value, ctype), default=str, ensure_ascii=False)
    return str(value)


class SqlWorkspace:
    """One SQLite connection with every opened database attached as its own schema."""

    def __init__(self) -> None:
        self._dir = tempfile.mkdtemp(prefix="edb-sql-")
        self._conn = sqlite3.connect(":memory:", check_same_thread=False)
        self._conn.execute("PRAGMA temp_store = MEMORY")
        self._lock = threading.RLock()
        self._attached: dict[str, _Attached] = {}

    # ------------------------------------------------------------------ #
    def close(self) -> None:
        with self._lock:
            try:
                self._conn.close()
            finally:
                for a in self._attached.values():
                    Path(a.path).unlink(missing_ok=True)
                self._attached.clear()
                try:
                    os.rmdir(self._dir)
                except OSError:
                    pass

    def schema_for(self, db: Database) -> str:
        return self.attach(db).schema

    def attach(self, db: Database) -> _Attached:
        with self._lock:
            att = self._attached.get(db.id)
            if att is not None:
                return att
            schema = _SCHEMA_SAFE.sub("_", db.id) or "db"
            if schema[0].isdigit():
                schema = "db_" + schema
            base, n = schema, 2
            while any(a.schema == schema for a in self._attached.values()):
                schema = f"{base}_{n}"
                n += 1
            path = os.path.join(self._dir, f"{schema}.sqlite")
            try:
                self._conn.execute(f"ATTACH DATABASE ? AS {quote_ident(schema)}", (path,))
            except sqlite3.Error as exc:
                raise SqlError(
                    f"Cannot attach {db.id}: {exc} (SQLite allows a limited number of attached databases)"
                ) from exc
            att = _Attached(db=db, schema=schema, path=path)
            self._attached[db.id] = att
            return att

    def detach(self, db_id: str) -> None:
        with self._lock:
            att = self._attached.pop(db_id, None)
            if att is None:
                return
            try:
                self._conn.execute(f"DETACH DATABASE {quote_ident(att.schema)}")
            except sqlite3.Error:
                pass
            Path(att.path).unlink(missing_ok=True)

    def attached(self) -> dict[str, str]:
        """{db_id: schema}"""
        return {k: v.schema for k, v in self._attached.items()}

    # ------------------------------------------------------------------ #
    def qualified(self, db: Database, table: str) -> str:
        real = db.resolve_table_name(table)
        return f"{quote_ident(self.schema_for(db))}.{quote_ident(real)}"

    def ensure_table(
        self, db: Database, table: str, progress: ProgressCallback | None = None, max_rows: int | None = None
    ) -> str:
        """Materialise ``table`` (if not already) and return its qualified SQL name."""
        real = db.resolve_table_name(table)
        att = self.attach(db)
        if real in att.tables:
            return self.qualified(db, real)
        info = db.table(real)
        cols = info.column_names
        types = db.column_types(real)
        qname = self.qualified(db, real)
        col_defs = ", ".join(quote_ident(c) for c in cols)
        with self._lock:
            self._conn.execute(f"DROP TABLE IF EXISTS {qname}")
            self._conn.execute(f"CREATE TABLE {qname} ({col_defs})")
            placeholders = ", ".join("?" for _ in cols)
            insert = f"INSERT INTO {qname} VALUES ({placeholders})"
            batch: list[tuple[Any, ...]] = []
            col_types = [(c, types.get(c)) for c in cols]
            n = 0
            for n, (_, values) in enumerate(db.iter_records(real, include_nulls=False), start=1):
                get = values.get
                batch.append(tuple(None if (v := get(c)) is None else _cell(v, t) for c, t in col_types))
                if len(batch) >= 2000:
                    self._conn.executemany(insert, batch)
                    batch.clear()
                    if progress and not progress(real, n):
                        break
                if max_rows and n >= max_rows:
                    break
            if batch:
                self._conn.executemany(insert, batch)
            self._conn.commit()
            att.tables.add(real)
        return qname

    def ensure_database(
        self, db: Database, include_system: bool = False, progress: ProgressCallback | None = None
    ) -> list[str]:
        return [self.ensure_table(db, t.name, progress) for t in db.tables(include_system)]

    def materialized(self, db: Database) -> set[str]:
        att = self._attached.get(db.id)
        return set(att.tables) if att else set()

    # ------------------------------------------------------------------ #
    def resolve_placeholders(self, db: Database, sql: str) -> str:
        """Replace ``{t:Table}`` with the qualified materialised name (materialising as needed)."""

        def repl(m: re.Match[str]) -> str:
            name = m.group(1)
            try:
                return self.ensure_table(db, name)
            except TableNotFoundError as exc:
                raise SqlError(f"View needs table {name!r} which this database does not have") from exc

        return _PLACEHOLDER.sub(repl, sql)

    def auto_materialize(self, db: Database, sql: str) -> None:
        """Materialise every table of ``db`` whose name appears in ``sql`` (unquoted or quoted)."""
        low = sql.lower()
        for t in db.tables(include_system=True):
            n = t.name.lower()
            if n in low or quote_ident(t.name).lower() in low:
                self.ensure_table(db, t.name)

    def query(self, sql: str, limit: int | None = 1000, params: tuple[Any, ...] | None = None) -> QueryResult:
        """Execute read-only SQL; results are capped at ``limit`` rows (None = unlimited)."""
        started = time.perf_counter()
        with self._lock:
            self._conn.execute("PRAGMA query_only = 1")
            try:
                cur = self._conn.execute(sql, params or ())
                columns = [d[0] for d in cur.description] if cur.description else []
                if limit is None:
                    rows = cur.fetchall()
                    truncated = False
                else:
                    rows = cur.fetchmany(limit + 1)
                    truncated = len(rows) > limit
                    rows = rows[:limit]
            except sqlite3.Error as exc:
                raise SqlError(str(exc)) from exc
            finally:
                self._conn.execute("PRAGMA query_only = 0")
        return QueryResult(
            columns=columns, rows=rows, truncated=truncated, elapsed=time.perf_counter() - started, sql=sql
        )

    def query_database(self, db: Database, sql: str, limit: int | None = 1000) -> QueryResult:
        """Run SQL written against ``db``'s real table names (auto-materialises referenced tables)."""
        self.attach(db)
        sql = self.resolve_placeholders(db, sql)
        self.auto_materialize(db, sql)
        return self.query(sql, limit)

    def run_view(self, db: Database, view: ArtifactView | str, limit: int | None = 1000) -> QueryResult:
        if isinstance(view, str):
            found = next((v for v in db.profile.views if view in (v.id, v.name)), None)
            if found is None:
                raise SqlError(f"No view {view!r} for profile {db.profile.name}")
            view = found
        sql = self.resolve_placeholders(db, view.sql)
        return self.query(sql, limit)

    def list_views(self, db: Database) -> list[dict[str, str]]:
        return [{"id": v.id, "name": v.name, "description": v.description, "sql": v.sql} for v in db.profile.views]
