"""SQLite backend - phones, browsers, macOS, Windows apps, Linux desktops.

Records are read with ``dissect.database``'s pure-Python parser so the evidence
file is never opened by the SQLite library itself (no locks, no -shm files, WAL
sidecars honoured).  The SQL console gets a native read-only ``immutable``
connection when there is no WAL to merge; otherwise tables are materialised.
"""

from __future__ import annotations

import logging
import re
import sqlite3
from collections.abc import Iterator
from datetime import datetime, timezone
from typing import Any

from dissect.database.sqlite3 import SQLite3
from dissect.database.sqlite3.exception import InvalidDatabase
from dissect.database.sqlite3.util import parse_table_columns_constraints

from edb_explorer.core.backends.base import Backend, BackendInfo, BackendTable, sanitize_columns
from edb_explorer.core.exceptions import InvalidDatabaseError
from edb_explorer.core.models import ColumnInfo, IndexInfo

log = logging.getLogger(__name__)

SQLITE_MAGIC = b"SQLite format 3\x00"
_TYPE_ID = {"INTEGER": 1, "REAL": 2, "TEXT": 3, "BLOB": 4, "NUMERIC": 5}
_INDEX_COLS = re.compile(r"\((.*)\)", re.S)


def _affinity(decl: str) -> str:
    d = (decl or "").upper()
    if "INT" in d:
        return "INTEGER"
    if any(k in d for k in ("CHAR", "CLOB", "TEXT")):
        return "TEXT"
    if "BLOB" in d or not d:
        return "BLOB" if "BLOB" in d else "ANY"
    if any(k in d for k in ("REAL", "FLOA", "DOUB")):
        return "REAL"
    return "NUMERIC"


class SqliteBackend(Backend):
    kind = "sqlite"
    kind_name = "SQLite 3"
    native_sql = True

    def __init__(self, path: Any) -> None:
        super().__init__(path)
        self._fh = open(self.path, "rb")  # noqa: SIM115
        try:
            self._db = SQLite3(self._fh)
        except (InvalidDatabase, ValueError, EOFError) as exc:
            self._fh.close()
            raise InvalidDatabaseError(f"{self.path.name}: not a valid SQLite database ({exc})") from exc
        except Exception as exc:
            self._fh.close()
            raise InvalidDatabaseError(f"{self.path.name}: failed to parse ({exc})") from exc
        self._raw: dict[str, Any] = {}
        self._tables: list[BackendTable] | None = None
        self._wal_path = self.path.with_name(self.path.name + "-wal")
        self._journal_path = self.path.with_name(self.path.name + "-journal")

    def close(self) -> None:
        try:
            self._db.close()
        except Exception:
            pass
        try:
            self._fh.close()
        finally:
            self._raw.clear()

    @property
    def has_wal(self) -> bool:
        return self._db.wal is not None

    # ------------------------------------------------------------------ #
    def tables(self) -> list[BackendTable]:
        if self._tables is None:
            index_map: dict[str, list[IndexInfo]] = {}
            try:
                for idx in self._db.indices():
                    cols: tuple[str, ...] = ()
                    m = _INDEX_COLS.search(idx.sql or "")
                    if m:
                        cols = tuple(c.strip().strip('"`[]').split(" ")[0] for c in m.group(1).split(","))
                    unique = bool(re.search(r"CREATE\s+UNIQUE", idx.sql or "", re.I))
                    index_map.setdefault(idx.table_name, []).append(
                        IndexInfo(name=idx.name, is_primary=False, is_unique=unique, columns=cols)
                    )
            except Exception as exc:
                log.debug("index enumeration failed: %s", exc)
            out: list[BackendTable] = []
            for raw in self._db.tables():
                names = sanitize_columns([c.name for c in raw.columns])
                try:
                    _, declared, _ = parse_table_columns_constraints(raw.sql)
                    decls = dict(declared)
                except Exception:
                    decls = {}
                columns = []
                for i, (col, name) in enumerate(zip(raw.columns, names, strict=False)):
                    decl = str(decls.get(col.name, "") or "")
                    aff = _affinity(decl.split(" ", maxsplit=1)[0] if decl else "")
                    columns.append(
                        ColumnInfo(
                            identifier=i + 1,
                            name=name,
                            type=aff,
                            type_id=_TYPE_ID.get(aff, 0),
                            storage="dynamic",
                            size=None,
                            encoding=self._db.encoding,
                            is_text=aff == "TEXT",
                            is_binary=aff == "BLOB",
                        )
                    )
                pk = getattr(raw, "primary_key", None)
                indexes = list(index_map.get(raw.name, []))
                if pk:
                    indexes.insert(0, IndexInfo(name="PRIMARY KEY", is_primary=True, is_unique=True, columns=(pk,)))
                self._raw[raw.name] = (raw, names)
                out.append(
                    BackendTable(
                        name=raw.name,
                        columns=columns,
                        indexes=indexes,
                        root_page=int(raw.page),
                        is_system=raw.name.startswith("sqlite_"),
                        extra={"sql": raw.sql},
                    )
                )
            self._tables = out
        return self._tables

    def iter_rows(self, table: str) -> Iterator[dict[str, Any]]:
        self.tables()
        raw, names = self._raw[table]
        errors = 0
        iterator = raw.rows()
        while True:
            try:
                row = next(iterator)
            except StopIteration:
                return
            except Exception as exc:
                errors += 1
                if errors <= 5:
                    log.warning("Skipping unreadable row in %s: %s", table, exc)
                if errors > 1000:
                    return
                continue
            items = [v for _, v in row]  # Row.__iter__ yields (declared name, value) in column order
            yield {n: (items[i] if i < len(items) else None) for i, n in enumerate(names)}

    # ------------------------------------------------------------------ #
    def sql_connection(self) -> sqlite3.Connection | None:
        if self.has_wal:
            return None  # immutable connections ignore the WAL; materialise instead
        try:
            uri = f"{self.path.resolve().as_uri()}?mode=ro&immutable=1"
            conn = sqlite3.connect(uri, uri=True, check_same_thread=False)
            conn.execute("PRAGMA query_only = 1")
            return conn
        except sqlite3.Error as exc:
            log.debug("native sqlite connection failed: %s", exc)
            return None

    def info(self) -> BackendInfo:
        h = self._db.header
        header: dict[str, Any] = {}
        for name in (
            "page_size",
            "write_version",
            "read_version",
            "reserved_size",
            "file_change_counter",
            "page_count",
            "first_freelist_trunk_page",
            "freelist_pages",
            "schema_cookie",
            "schema_format",
            "default_cache_size",
            "largest_root_page",
            "text_encoding",
            "user_version",
            "incremental_vacuum",
            "application_id",
            "version_valid_for",
            "sqlite_version",
        ):
            try:
                header[name] = int(getattr(h, name))
            except Exception:
                continue
        sidecars = [p.name for p in (self._wal_path, self._journal_path) if p.exists()]
        ver = header.get("sqlite_version", 0)
        ver_text = f"{ver // 1_000_000}.{(ver // 1000) % 1000}.{ver % 1000}" if ver else ""
        state = (
            "WAL present (merged)"
            if self.has_wal
            else "rollback journal present"
            if self._journal_path.exists()
            else "clean"
        )
        try:
            mtime = datetime.fromtimestamp(self.path.stat().st_mtime, tz=timezone.utc)
        except OSError:
            mtime = None
        return BackendInfo(
            kind=self.kind,
            kind_name=self.kind_name,
            page_size=int(self._db.page_size),
            format_version=header.get("schema_format", 0),
            format_revision=header.get("user_version", 0),
            state=state + (f" · written by SQLite {ver_text}" if ver_text else ""),
            last_attach=mtime,
            encoding=self._db.encoding,
            header=header,
            sidecars=sidecars,
        )
