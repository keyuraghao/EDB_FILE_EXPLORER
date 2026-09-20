"""ESE / JET Blue backend (ntds.dit, SRUDB.dat, Exchange .edb, WebCacheV01.dat ...)."""

from __future__ import annotations

import logging
from collections.abc import Iterator
from typing import Any

from dissect.esedb import EseDB
from dissect.esedb.exceptions import Error as DissectError
from dissect.esedb.exceptions import InvalidDatabase
from dissect.esedb.record import RecordData
from dissect.esedb.table import Column as DissectColumn

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


# --------------------------------------------------------------------------- #
# Template tables (Exchange 2013+ derives every per-mailbox table from a template)
# --------------------------------------------------------------------------- #
_orig_get_tagged = RecordData._get_tagged


def _get_tagged_with_derived(self: RecordData, column: Any) -> tuple[Any, Any]:
    """Tagged values of template-derived columns carry the ``fDerived`` flag; look them up accordingly."""
    if getattr(column, "_edb_derived", False):
        idx = self._find_tag_field_idx(column.identifier, True)
        if idx is None:
            return None, column.default
        tag_field = self._get_tag_field(idx)
        data_start = tag_field.offset + (1 if tag_field.has_extended_info else 0)
        data_end = self._get_tag_field(idx + 1).offset if idx + 1 < self._tagged_data_count else len(self.data)
        if tag_field.is_null:
            return tag_field, None
        base = self._tagged_data_start
        return tag_field, self.data[base + data_start : base + data_end]
    return _orig_get_tagged(self, column)


if getattr(RecordData._get_tagged, "__name__", "") != "_get_tagged_with_derived":
    RecordData._get_tagged = _get_tagged_with_derived


def _inherit_template(table: Any, tables_by_name: dict[str, Any]) -> str | None:
    """Copy the columns/indexes of ``table``'s template table into it (dissect leaves derived tables empty)."""
    record = getattr(table, "record", None)
    if record is None:
        return None
    try:
        template_name = record.get("TemplateTable")
    except Exception:
        return None
    if not template_name or table.columns:
        return template_name or None
    template = tables_by_name.get(template_name)
    if template is None:
        return template_name
    if not template.columns and getattr(template, "record", None) is not None:
        _inherit_template(template, tables_by_name)
    for col in template.columns:
        copy = DissectColumn(col.identifier, col.name, col.type, col.record)
        copy._edb_derived = True
        table._add_column(copy)
    if not table.indexes:
        table.indexes = list(template.indexes)
    if getattr(table, "_long_value_record", None) is None:
        table._long_value_record = getattr(template, "_long_value_record", None)
    return template_name


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
        self.templates: dict[str, str] = {}

    def close(self) -> None:
        try:
            self._fh.close()
        finally:
            self._raw.clear()

    # ------------------------------------------------------------------ #
    def tables(self) -> list[BackendTable]:
        if self._tables is None:
            out: list[BackendTable] = []
            raw_tables = list(self._db.tables())
            by_name = {t.name: t for t in raw_tables}
            templates: dict[str, str] = {}
            for raw in raw_tables:
                try:
                    tmpl = _inherit_template(raw, by_name)
                except Exception as exc:
                    log.debug("template inheritance failed for %s: %s", raw.name, exc)
                    tmpl = None
                if tmpl:
                    templates[raw.name] = tmpl
            self.templates = templates
            for raw in raw_tables:
                try:
                    columns = [_column_info(c) for c in raw.columns]
                    indexes = [_index_info(i) for i in raw.indexes]
                except Exception as exc:
                    log.warning("Failed to read schema for table %s: %s", raw.name, exc)
                    columns, indexes = [], []
                self._raw[raw.name] = raw
                extra = {"template": templates[raw.name]} if raw.name in templates else {}
                out.append(
                    BackendTable(
                        name=raw.name,
                        columns=columns,
                        indexes=indexes,
                        root_page=int(raw.root_page),
                        is_system=is_system_table(raw.name),
                        extra=extra,
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
