"""Thread-safe, read-only wrapper around one database file of any supported format."""

from __future__ import annotations

import hashlib
import logging
import os
import threading
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

from edb_explorer.core.backends import Backend, detect_kind, open_backend
from edb_explorer.core.backends.ese import ESE_MAGIC
from edb_explorer.core.exceptions import InvalidDatabaseError, TableNotFoundError
from edb_explorer.core.models import DatabaseInfo, RecordPage, TableInfo
from edb_explorer.core.profiles import GENERIC, Profile, detect_profile
from edb_explorer.core.values import BytesMode, normalize_value

log = logging.getLogger(__name__)

RowFilter = Callable[[dict[str, Any]], bool]

__all__ = ["ESE_MAGIC", "Database", "EdbDatabase", "RowFilter", "is_ese_file", "is_supported_file"]


def is_ese_file(path: str | os.PathLike[str]) -> bool:
    """Cheap magic-number check for ESE files."""
    try:
        with open(path, "rb") as fh:
            head = fh.read(8)
    except OSError:
        return False
    return len(head) >= 8 and head[4:8] == ESE_MAGIC


def is_supported_file(path: str | os.PathLike[str]) -> bool:
    return detect_kind(path) is not None


class Database:
    """A single opened database (ESE, SQLite, LevelDB, Access, DBF, Berkeley DB, SQL dump, BSON ...).

    All access to the backend is serialised through a re-entrant lock, held per
    record rather than per table so the GUI can interleave reads from several
    threads while a large table is loading.
    """

    def __init__(self, path: str | os.PathLike[str], db_id: str | None = None, kind: str | None = None) -> None:
        self.path = Path(path).expanduser().resolve()
        if not self.path.exists():
            raise InvalidDatabaseError(f"File not found: {self.path}")
        self.id = db_id or self.path.stem
        self._lock = threading.RLock()
        self.backend: Backend = open_backend(self.path, kind)
        self.kind = self.backend.kind
        self._tables: dict[str, TableInfo] = {}
        self._counts: dict[str, int] = {}
        self._sha256: str | None = None
        self._closed = False
        self.profile: Profile = GENERIC
        self._load_catalog()
        self.info = self._build_info()

    # ------------------------------------------------------------------ #
    # Lifecycle
    # ------------------------------------------------------------------ #
    def close(self) -> None:
        with self._lock:
            if not self._closed:
                self._closed = True
                try:
                    self.backend.close()
                except Exception as exc:
                    log.debug("close failed: %s", exc)

    @property
    def closed(self) -> bool:
        return self._closed

    def __enter__(self) -> Database:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def __repr__(self) -> str:
        return f"<Database id={self.id!r} kind={self.kind} path={str(self.path)!r} tables={len(self._tables)}>"

    # ------------------------------------------------------------------ #
    # Catalog
    # ------------------------------------------------------------------ #
    def _load_catalog(self) -> None:
        with self._lock:
            backend_tables = self.backend.tables()
        names = [t.name for t in backend_tables]
        self.profile = detect_profile(names, self.path.name, self.kind)
        for bt in backend_tables:
            if bt.record_count is not None:
                self._counts[bt.name] = bt.record_count
            self._tables[bt.name] = TableInfo(
                name=bt.name,
                display_name=self.profile.display_name(bt.name),
                root_page=bt.root_page,
                columns=tuple(bt.columns),
                indexes=tuple(bt.indexes),
                is_system=bt.is_system,
                description=self.profile.describe(bt.name) or bt.extra.get("description"),
                record_count=bt.record_count,
            )

    def _build_info(self) -> DatabaseInfo:
        bi = self.backend.info()
        return DatabaseInfo(
            id=self.id,
            path=str(self.path),
            file_name=self.path.name,
            size_bytes=self.backend.size_bytes(),
            page_size=bi.page_size,
            format_version=bi.format_version,
            format_revision=bi.format_revision,
            created_version=bi.created_version,
            created_revision=bi.created_revision,
            state=bi.state,
            profile_id=self.profile.id,
            profile_name=self.profile.name,
            table_count=len(self._tables),
            kind=bi.kind,
            kind_name=bi.kind_name,
            created=bi.created,
            last_attach=bi.last_attach,
            last_detach=bi.last_detach,
            windows_version=bi.windows_version,
            encoding=bi.encoding,
            sha256=self._sha256,
            sidecars=list(bi.sidecars),
            header=bi.header,
        )

    def compute_sha256(self, chunk_size: int = 4 * 1024 * 1024) -> str:
        """Hash the file on disk (cached). For directory databases (LevelDB) every file is hashed in name order."""
        if self._sha256 is None:
            digest = hashlib.sha256()
            files = sorted(p for p in self.path.rglob("*") if p.is_file()) if self.path.is_dir() else [self.path]
            for f in files:
                with open(f, "rb") as fh:
                    for chunk in iter(lambda: fh.read(chunk_size), b""):
                        digest.update(chunk)
            self._sha256 = digest.hexdigest()
            self.info = self._build_info()
        return self._sha256

    # ------------------------------------------------------------------ #
    # Tables
    # ------------------------------------------------------------------ #
    def tables(self, include_system: bool = True) -> list[TableInfo]:
        infos = [self._with_count(t) for t in self._tables.values()]
        if not include_system:
            infos = [t for t in infos if not t.is_system]
        return infos

    def table_names(self, include_system: bool = True) -> list[str]:
        return [t.name for t in self.tables(include_system)]

    def resolve_table_name(self, name: str) -> str:
        """Accept exact, case-insensitive or display names."""
        if name in self._tables:
            return name
        lowered = name.lower()
        for real, info in self._tables.items():
            if real.lower() == lowered or info.display_name.lower() == lowered:
                return real
        raise TableNotFoundError(f"Table {name!r} not found in {self.path.name}")

    def table(self, name: str) -> TableInfo:
        return self._with_count(self._tables[self.resolve_table_name(name)])

    def _with_count(self, info: TableInfo) -> TableInfo:
        count = self._counts.get(info.name)
        if count is None or info.record_count == count:
            return info
        return TableInfo(
            name=info.name,
            display_name=info.display_name,
            root_page=info.root_page,
            columns=info.columns,
            indexes=info.indexes,
            is_system=info.is_system,
            description=info.description,
            record_count=count,
        )

    def column_types(self, name: str) -> dict[str, str]:
        """Column -> storage type, with profile timestamp hints applied as ``ts:<kind>`` pseudo-types."""
        real = self.resolve_table_name(name)
        types = {c.name: c.type for c in self._tables[real].columns}
        for col, kind in self.profile.timestamp_hints(real).items():
            if col in types:
                types[col] = f"ts:{kind}"
        return types

    # ------------------------------------------------------------------ #
    # Records
    # ------------------------------------------------------------------ #
    def count_records(self, name: str) -> int:
        """Count records (backend fast path when available, else a full walk; cached)."""
        real = self.resolve_table_name(name)
        if real not in self._counts:
            with self._lock:
                fast = self.backend.count(real)
            if fast is not None:
                self._counts[real] = fast
            else:
                n = 0
                for _ in self._iter_raw(real):
                    n += 1
                self._counts[real] = n
        return self._counts[real]

    def _iter_raw(self, real: str) -> Iterator[tuple[int, dict[str, Any]]]:
        """Backend rows with their zero-based index; the lock is held per record, not per table."""
        lock = self._lock
        with lock:
            iterator = self.backend.iter_rows(real)
        index = 0
        while True:
            with lock:
                if self._closed:
                    return
                try:
                    values = next(iterator)
                except StopIteration:
                    return
            yield index, values
            index += 1

    def cached_count(self, name: str) -> int | None:
        return self._counts.get(self.resolve_table_name(name))

    def iter_records(
        self,
        name: str,
        columns: list[str] | None = None,
        start: int = 0,
        stop: int | None = None,
        raw: bool = False,
        row_filter: RowFilter | None = None,
        bytes_mode: BytesMode = "raw",
        max_length: int | None = None,
        include_nulls: bool = False,
    ) -> Iterator[tuple[int, dict[str, Any]]]:
        """Yield ``(row_index, values)`` in storage order.

        ``row_index`` is the zero-based position of the record and is stable
        for a given file.  ``start``/``stop`` are applied *after* filtering when
        ``row_filter`` is given, so they page through matches.
        """
        real = self.resolve_table_name(name)
        types = self.column_types(real)
        wanted = list(columns) if columns else None
        if wanted:
            missing = [c for c in wanted if c not in types]
            if missing:
                raise TableNotFoundError(f"Unknown column(s) in {real}: {', '.join(missing)}")
        # Decide once per call what each row needs; the per-row loop below only does work that can vary by row.
        needs_norm = bytes_mode != "raw" or max_length is not None or any(t.startswith("ts:") for t in types.values())
        # Without a filter, rows before ``start`` are skipped by position and never need decoding.
        skip_undecoded = row_filter is None
        # One pass for decode + null-strip when nothing has to see the un-stripped row (a decoded value can
        # itself become None, e.g. a zero DateTime, so the strip still happens after decoding).
        fused = needs_norm and not include_nulls and row_filter is None
        types_get = types.get
        matched = 0
        for index, values in self._iter_raw(real):
            if skip_undecoded and matched < start:
                matched += 1
                continue
            if wanted:
                values = {c: values.get(c) for c in wanted}
            if fused:
                values = {
                    k: nv
                    for k, v in values.items()
                    if v is not None and (nv := normalize_value(v, types_get(k), bytes_mode, max_length)) is not None
                }
            elif needs_norm:
                values = {
                    k: (normalize_value(v, types_get(k), bytes_mode, max_length) if v is not None else None)
                    for k, v in values.items()
                }
            if row_filter is not None:
                if not row_filter(values):
                    continue
                if matched < start:
                    matched += 1
                    continue
            if include_nulls and not wanted:
                values = {c: values.get(c) for c in types}
            elif not include_nulls and not fused:
                values = {k: v for k, v in values.items() if v is not None}
            matched += 1
            yield index, values
            if stop is not None and matched >= stop:
                break

    def fetch(
        self,
        name: str,
        offset: int = 0,
        limit: int = 100,
        columns: list[str] | None = None,
        row_filter: RowFilter | None = None,
        bytes_mode: BytesMode = "smart",
        max_length: int | None = 512,
        include_nulls: bool = False,
    ) -> RecordPage:
        """Fetch one page of decoded records (JSON-safe)."""
        real = self.resolve_table_name(name)
        rows: list[dict[str, Any]] = []
        last_index = -1
        for index, values in self.iter_records(
            real,
            columns=columns,
            start=offset,
            stop=offset + limit + 1,
            row_filter=row_filter,
            bytes_mode=bytes_mode,
            max_length=max_length,
            include_nulls=include_nulls,
        ):
            last_index = index
            rows.append({"_row": index, **values})
        has_more = len(rows) > limit
        rows = rows[:limit]
        total = None if row_filter else self._counts.get(real)
        if row_filter is None and not has_more:
            total = offset + len(rows)
            self._counts.setdefault(real, total)
        cols = list(columns) if columns else self.table(real).column_names
        return RecordPage(
            table=real,
            columns=cols,
            rows=rows,
            offset=offset,
            limit=limit,
            has_more=has_more,
            total=total,
            scanned=last_index + 1,
        )

    def get_record(
        self,
        name: str,
        row_index: int,
        bytes_mode: BytesMode = "smart",
        max_length: int | None = None,
        include_nulls: bool = False,
    ) -> dict[str, Any] | None:
        for index, values in self.iter_records(
            name,
            start=row_index,
            stop=row_index + 1,
            bytes_mode=bytes_mode,
            max_length=max_length,
            include_nulls=include_nulls,
        ):
            return {"_row": index, **values}
        return None

    def get_raw_record(self, name: str, row_index: int) -> dict[str, Any] | None:
        """Undecoded values (bytes stay bytes) - used by the GUI inspector."""
        for _, values in self.iter_records(name, start=row_index, stop=row_index + 1, include_nulls=True):
            return values
        return None

    # ------------------------------------------------------------------ #
    # SQL
    # ------------------------------------------------------------------ #
    def native_sql_connection(self) -> Any | None:
        """Read-only sqlite3 connection straight onto the file when the format allows it."""
        if not self.backend.native_sql:
            return None
        with self._lock:
            return self.backend.sql_connection()


EdbDatabase = Database
