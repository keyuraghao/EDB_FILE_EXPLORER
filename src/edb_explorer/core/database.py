"""Thread-safe, read-only wrapper around a single ESE database file."""

from __future__ import annotations

import hashlib
import logging
import os
import threading
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

from dissect.esedb import EseDB
from dissect.esedb.exceptions import Error as DissectError
from dissect.esedb.exceptions import InvalidDatabase

from edb_explorer.core.exceptions import InvalidDatabaseError, TableNotFoundError
from edb_explorer.core.models import ColumnInfo, DatabaseInfo, IndexInfo, RecordPage, TableInfo
from edb_explorer.core.profiles import GENERIC, Profile, detect_profile, is_system_table
from edb_explorer.core.values import BytesMode, logtime_to_datetime, normalize_value

log = logging.getLogger(__name__)

ESE_MAGIC = b"\xef\xcd\xab\x89"  # ulMagic 0x89ABCDEF little-endian, at file offset 4

_DB_STATES = {
    1: "just created",
    2: "dirty shutdown",
    3: "clean shutdown",
    4: "being converted",
    5: "force detach",
}

RowFilter = Callable[[dict[str, Any]], bool]


def is_ese_file(path: str | os.PathLike[str]) -> bool:
    """Cheap magic-number check used when scanning directories."""
    try:
        with open(path, "rb") as fh:
            head = fh.read(8)
    except OSError:
        return False
    return len(head) >= 8 and head[4:8] == ESE_MAGIC


def _column_info(col: Any) -> ColumnInfo:
    storage = "fixed" if col.is_fixed else "variable" if col.is_variable else "tagged"
    encoding = None
    try:
        enc = col.encoding
        encoding = enc.name if enc is not None and hasattr(enc, "name") else (str(enc) if enc else None)
    except Exception:
        encoding = None
    return ColumnInfo(
        identifier=int(col.identifier),
        name=str(col.name),
        type=col.type.name,
        type_id=int(col.type),
        storage=storage,
        size=col.size,
        encoding=encoding,
        is_text=bool(col.is_text),
        is_binary=bool(col.is_binary),
    )


def _index_info(idx: Any) -> IndexInfo:
    try:
        columns = tuple(c.name for c in idx.columns)
    except Exception:
        columns = ()
    try:
        unique = bool(idx.idb_flags & idx.idb_flags.__class__.Unique)
    except Exception:
        unique = False
    return IndexInfo(name=str(idx.name), is_primary=bool(idx.is_primary), is_unique=unique, columns=columns)


class EdbDatabase:
    """A single opened ESE database.

    All access to the underlying ``dissect`` objects is serialised through a
    re-entrant lock so the GUI can read from worker threads while the MCP
    server or main thread inspects schema.
    """

    def __init__(self, path: str | os.PathLike[str], db_id: str | None = None) -> None:
        self.path = Path(path).expanduser().resolve()
        if not self.path.is_file():
            raise InvalidDatabaseError(f"File not found: {self.path}")
        self.id = db_id or self.path.stem
        self._lock = threading.RLock()
        self._fh = open(self.path, "rb")  # noqa: SIM115 - lifetime managed by close()
        try:
            self._db = EseDB(self._fh)
        except (InvalidDatabase, DissectError, ValueError, EOFError) as exc:
            self._fh.close()
            raise InvalidDatabaseError(f"{self.path.name}: not a valid ESE database ({exc})") from exc
        except Exception as exc:
            self._fh.close()
            raise InvalidDatabaseError(f"{self.path.name}: failed to parse ({exc})") from exc

        self._raw_tables: dict[str, Any] = {}
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
                    self._fh.close()
                finally:
                    self._raw_tables.clear()

    @property
    def closed(self) -> bool:
        return self._closed

    def __enter__(self) -> EdbDatabase:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def __repr__(self) -> str:
        return f"<EdbDatabase id={self.id!r} path={str(self.path)!r} tables={len(self._tables)}>"

    # ------------------------------------------------------------------ #
    # Catalog
    # ------------------------------------------------------------------ #
    def _load_catalog(self) -> None:
        with self._lock:
            raw_tables = list(self._db.tables())
        self.profile = detect_profile([t.name for t in raw_tables], self.path.name)
        for raw in raw_tables:
            try:
                columns = tuple(_column_info(c) for c in raw.columns)
                indexes = tuple(_index_info(i) for i in raw.indexes)
            except Exception as exc:
                log.warning("Failed to read schema for table %s: %s", raw.name, exc)
                columns, indexes = (), ()
            info = TableInfo(
                name=raw.name,
                display_name=self.profile.display_name(raw.name),
                root_page=int(raw.root_page),
                columns=columns,
                indexes=indexes,
                is_system=is_system_table(raw.name),
                description=self.profile.describe(raw.name),
            )
            self._raw_tables[raw.name] = raw
            self._tables[raw.name] = info

    def _header_dict(self) -> dict[str, Any]:
        h = self._db.header
        out: dict[str, Any] = {}
        fields = (
            "ulChecksum",
            "ulVersion",
            "ulDaeUpdateMajor",
            "ulDaeUpdateMinor",
            "cbPageSize",
            "dbstate",
            "dbid",
            "objidLast",
            "dwMajorVersion",
            "dwMinorVersion",
            "dwBuildNumber",
            "lSPNumber",
            "ulCreateVersion",
            "ulCreateUpdate",
            "lGenMinRequired",
            "lGenMaxRequired",
            "lGenMaxCommitted",
            "ulRepairCount",
            "ulBadChecksum",
            "ulECCFixSuccess",
            "ulECCFixFail",
            "ulIncrementalReseedCount",
            "ulPagePatchCount",
        )
        for name in fields:
            try:
                out[name] = int(getattr(h, name))
            except Exception:
                continue
        for name in (
            "logtimeAttach",
            "logtimeDetach",
            "logtimeConsistent",
            "logtimeRepair",
            "logtimeGenMaxCreate",
        ):
            try:
                dt = logtime_to_datetime(getattr(h, name))
                out[name] = dt.isoformat() if dt else None
            except Exception:
                continue
        try:
            out["signDb.ulRandom"] = int(h.signDb.ulRandom)
            dt = logtime_to_datetime(h.signDb.logtimeCreate)
            out["signDb.logtimeCreate"] = dt.isoformat() if dt else None
            out["signDb.szComputerName"] = bytes(h.signDb.szComputerName).rstrip(b"\x00").decode("latin-1")
        except Exception:
            pass
        return out

    def _build_info(self) -> DatabaseInfo:
        h = self._db.header
        header = self._header_dict()

        def logtime(attr: str) -> Any:
            try:
                return logtime_to_datetime(getattr(h, attr))
            except Exception:
                return None

        created = None
        try:
            created = logtime_to_datetime(h.signDb.logtimeCreate)
        except Exception:
            pass
        win = None
        try:
            if int(h.dwMajorVersion) or int(h.dwBuildNumber):
                win = (
                    f"{int(h.dwMajorVersion)}.{int(h.dwMinorVersion)} build {int(h.dwBuildNumber)} SP{int(h.lSPNumber)}"
                )
        except Exception:
            pass
        return DatabaseInfo(
            id=self.id,
            path=str(self.path),
            file_name=self.path.name,
            size_bytes=self.path.stat().st_size,
            page_size=int(self._db.page_size),
            format_version=int(self._db.version),
            format_revision=int(self._db.format_major),
            created_version=header.get("ulCreateVersion", 0),
            created_revision=header.get("ulCreateUpdate", 0),
            state=_DB_STATES.get(header.get("dbstate", 0), f"unknown ({header.get('dbstate')})"),
            profile_id=self.profile.id,
            profile_name=self.profile.name,
            table_count=len(self._tables),
            created=created,
            last_attach=logtime("logtimeAttach"),
            last_detach=logtime("logtimeDetach"),
            windows_version=win,
            sha256=self._sha256,
            header=header,
        )

    def compute_sha256(self, chunk_size: int = 4 * 1024 * 1024) -> str:
        """Hash the file on disk (cached).  Useful for evidence integrity notes."""
        if self._sha256 is None:
            digest = hashlib.sha256()
            with open(self.path, "rb") as fh:
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
        return {c.name: c.type for c in self.table(name).columns}

    # ------------------------------------------------------------------ #
    # Records
    # ------------------------------------------------------------------ #
    def count_records(self, name: str) -> int:
        """Count records by walking the table B-tree (cached after the first call)."""
        real = self.resolve_table_name(name)
        if real not in self._counts:
            n = 0
            with self._lock:
                for _ in self._raw_tables[real].records():
                    n += 1
            self._counts[real] = n
        return self._counts[real]

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
        """Yield ``(row_index, values)`` for records in table order.

        ``row_index`` is the zero-based position of the record in the B-tree
        leaf order and is stable for a given file.  ``start``/``stop`` are
        applied *after* filtering when ``row_filter`` is given, so they page
        through matches.
        """
        real = self.resolve_table_name(name)
        raw_table = self._raw_tables[real]
        types = self.column_types(real)
        wanted = list(columns) if columns else None
        if wanted:
            missing = [c for c in wanted if c not in types]
            if missing:
                raise TableNotFoundError(f"Unknown column(s) in {real}: {', '.join(missing)}")
        matched = 0
        errors = 0
        with self._lock:
            iterator = raw_table.records()
        index = -1
        while True:
            # Hold the lock per record (not per table) so other threads can
            # interleave reads while a large table is being loaded.
            with self._lock:
                if self._closed:
                    return
                try:
                    record = next(iterator)
                except StopIteration:
                    break
                except Exception as exc:
                    errors += 1
                    if errors <= 5:
                        log.warning("Skipping unreadable record in %s: %s", real, exc)
                    continue
                index += 1
                try:
                    values = record.as_dict(raw=raw)
                except Exception as exc:
                    values = {"!ERROR!": str(exc)}
            if wanted:
                values = {c: values.get(c) for c in wanted}
            if bytes_mode != "raw" or max_length is not None:
                values = {k: normalize_value(v, types.get(k), bytes_mode, max_length) for k, v in values.items()}
            if row_filter is not None and not row_filter(values):
                continue
            if include_nulls and not wanted:
                values = {c: values.get(c) for c in types}
            elif not include_nulls:
                values = {k: v for k, v in values.items() if v is not None}
            if matched < start:
                matched += 1
                continue
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
        # Ask for one extra to learn whether more rows exist.
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
            # We reached the end: the count is now known for free.
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
