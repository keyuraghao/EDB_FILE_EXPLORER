"""ESE / JET Blue backend (ntds.dit, SRUDB.dat, Exchange .edb, WebCacheV01.dat ...)."""

from __future__ import annotations

import logging
from collections.abc import Iterator
from typing import Any

from dissect.esedb import EseDB
from dissect.esedb.exceptions import Error as DissectError
from dissect.esedb.exceptions import InvalidDatabase

from edb_explorer.core.backends.base import Backend, BackendInfo, BackendTable
from edb_explorer.core.exceptions import InvalidDatabaseError
from edb_explorer.core.models import ColumnInfo, IndexInfo
from edb_explorer.core.profiles import is_system_table
from edb_explorer.core.values import logtime_to_datetime

log = logging.getLogger(__name__)

ESE_MAGIC = b"\xef\xcd\xab\x89"  # ulMagic 0x89ABCDEF little-endian, at file offset 4

_DB_STATES = {
    1: "just created",
    2: "dirty shutdown",
    3: "clean shutdown",
    4: "being converted",
    5: "force detach",
}

_HEADER_INTS = (
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
_HEADER_TIMES = ("logtimeAttach", "logtimeDetach", "logtimeConsistent", "logtimeRepair", "logtimeGenMaxCreate")


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


class EseBackend(Backend):
    kind = "ese"
    kind_name = "Microsoft ESE / JET Blue"

    def __init__(self, path: Any) -> None:
        super().__init__(path)
        self._fh = open(self.path, "rb")  # noqa: SIM115 - closed in close()
        try:
            self._db = EseDB(self._fh)
        except (InvalidDatabase, DissectError, ValueError, EOFError) as exc:
            self._fh.close()
            raise InvalidDatabaseError(f"{self.path.name}: not a valid ESE database ({exc})") from exc
        except Exception as exc:
            self._fh.close()
            raise InvalidDatabaseError(f"{self.path.name}: failed to parse ({exc})") from exc
        self._raw: dict[str, Any] = {}
        self._tables: list[BackendTable] | None = None

    def close(self) -> None:
        try:
            self._fh.close()
        finally:
            self._raw.clear()

    # ------------------------------------------------------------------ #
    def tables(self) -> list[BackendTable]:
        if self._tables is None:
            out: list[BackendTable] = []
            for raw in self._db.tables():
                try:
                    columns = [_column_info(c) for c in raw.columns]
                    indexes = [_index_info(i) for i in raw.indexes]
                except Exception as exc:
                    log.warning("Failed to read schema for table %s: %s", raw.name, exc)
                    columns, indexes = [], []
                self._raw[raw.name] = raw
                out.append(
                    BackendTable(
                        name=raw.name,
                        columns=columns,
                        indexes=indexes,
                        root_page=int(raw.root_page),
                        is_system=is_system_table(raw.name),
                    )
                )
            self._tables = out
        return self._tables

    def iter_rows(self, table: str) -> Iterator[dict[str, Any]]:
        self.tables()
        raw_table = self._raw[table]
        iterator = raw_table.records()
        errors = 0
        while True:
            try:
                record = next(iterator)
            except StopIteration:
                return
            except Exception as exc:  # corrupt page - keep going
                errors += 1
                if errors <= 5:
                    log.warning("Skipping unreadable record in %s: %s", table, exc)
                continue
            try:
                yield record.as_dict()
            except Exception as exc:
                yield {"!ERROR!": str(exc)}

    # ------------------------------------------------------------------ #
    def _header_dict(self) -> dict[str, Any]:
        h = self._db.header
        out: dict[str, Any] = {}
        for name in _HEADER_INTS:
            try:
                out[name] = int(getattr(h, name))
            except Exception:
                continue
        for name in _HEADER_TIMES:
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

    def info(self) -> BackendInfo:
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
        return BackendInfo(
            kind=self.kind,
            kind_name=self.kind_name,
            page_size=int(self._db.page_size),
            format_version=int(self._db.version),
            format_revision=int(self._db.format_major),
            created_version=header.get("ulCreateVersion", 0),
            created_revision=header.get("ulCreateUpdate", 0),
            state=_DB_STATES.get(header.get("dbstate", 0), f"unknown ({header.get('dbstate')})"),
            created=created,
            last_attach=logtime("logtimeAttach"),
            last_detach=logtime("logtimeDetach"),
            windows_version=win,
            header=header,
        )
