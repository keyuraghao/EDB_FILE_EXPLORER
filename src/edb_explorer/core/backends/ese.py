"""ESE / JET Blue backend (ntds.dit, SRUDB.dat, Exchange .edb, WebCacheV01.dat ...)."""

from __future__ import annotations

import functools
import hashlib
import logging
from collections.abc import Callable, Iterator
from struct import Struct
from typing import Any

import dissect.esedb.record as _dissect_record
from dissect.esedb import EseDB, compression
from dissect.esedb.c_esedb import COLUMN_TYPE_MAP, TAGFLD_HEADER
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
# Fixed-width numeric columns: skip cstruct's generic bytes -> BytesIO -> unpack path
# --------------------------------------------------------------------------- #
def _fast_numeric_parser(ctype: Any) -> Callable[[bytes], Any] | None:
    """Direct ``Struct.unpack_from`` for a cstruct packed type; same result type, same error on bad input."""
    packchar = getattr(ctype, "packchar", None)
    endian = getattr(getattr(ctype, "cs", None), "endian", None)
    if not isinstance(ctype, type) or not packchar or not endian or getattr(ctype, "size", None) is None:
        return None
    unpack_from = Struct(f"{endian}{packchar}").unpack_from
    new = ctype.__new__

    def parse(buf: Any) -> Any:
        try:
            return new(ctype, unpack_from(buf)[0])
        except Exception:
            return ctype(buf)  # short / odd buffers: let dissect raise its own error

    parse.__name__ = f"fast_{ctype.__name__}"
    return parse


def _install_fast_numeric_parsers() -> None:
    for key, ct in list(COLUMN_TYPE_MAP.items()):
        if getattr(ct.parse, "__name__", "").startswith("fast_"):
            continue
        fast = _fast_numeric_parser(ct.parse)
        if fast is not None:
            COLUMN_TYPE_MAP[key] = ct._replace(parse=fast)


_install_fast_numeric_parsers()


# --------------------------------------------------------------------------- #
# Record decoding fast paths
#
# dissect.esedb is written for clarity, not throughput: every record builds two
# functools.lru_cache wrappers, every tagged value does four enum.Flag ``&``
# operations, and as_dict() goes through get() -> _get_fixed()/_get_variable()
# for every column.  The replacements below do exactly the same work with the
# same results and the same errors, just with less ceremony.  Each one is only
# installed when the dissect code it replaces is the code it was validated
# against (fingerprint of the referenced names), so a dissect upgrade that
# changes those internals silently falls back to dissect's own implementation.
# --------------------------------------------------------------------------- #
def _fingerprint(fn: Any) -> str:
    code = fn.__code__
    return hashlib.sha1(repr((code.co_argcount, code.co_names, code.co_varnames)).encode()).hexdigest()[:12]


_VALIDATED = {
    "RecordData.get": "4d37981752ba",
    "RecordData.as_dict": "77d5108f61a8",
    "RecordData._parse_value": "3a9e9e6c773a",
    "RecordData._get_fixed": "104b8d62bd03",
    "RecordData._get_variable": "8c2d1756d1b3",
}


def _matches(*names: str) -> bool:
    cls_name = "RecordData"
    for name in names:
        fn = getattr(RecordData, name.removeprefix(cls_name + "."))
        if _fingerprint(fn) != _VALIDATED.get(name):
            log.debug("dissect.esedb %s changed - keeping the stock implementation", name)
            return False
    return True


def _memo(maxsize: int) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """Drop-in for the per-record ``lru_cache(4096)`` in ``RecordData.__init__`` (creating an lru_cache costs more
    than a whole small record).  Positional-only dict cache, bounded by ``maxsize``."""

    def decorator(fn: Callable[..., Any]) -> Callable[..., Any]:
        cache: dict[tuple[Any, ...], Any] = {}

        def wrapper(*args: Any, **kwargs: Any) -> Any:
            key = args if not kwargs else (args, tuple(sorted(kwargs.items())))
            try:
                return cache[key]
            except KeyError:
                if len(cache) >= maxsize:
                    cache.clear()
                cache[key] = result = fn(*args, **kwargs)
                return result

        wrapper.__wrapped__ = fn  # type: ignore[attr-defined]
        wrapper.cache_clear = cache.clear  # type: ignore[attr-defined]
        return wrapper

    return decorator


_orig_parse_value = RecordData._parse_value
_orig_as_dict = RecordData.as_dict
_FLAG_MULTI = int(TAGFLD_HEADER.MultiValues)
_FLAG_SEPARATED = int(TAGFLD_HEADER.Separated)
_FLAG_COMPRESSED = int(TAGFLD_HEADER.Compressed)


def _parse_value_fast(
    self: RecordData, column: Any, value: Any, tag_field: Any = None, errors: str | None = "backslashreplace"
) -> Any:
    """``RecordData._parse_value`` with integer flag tests instead of enum.Flag arithmetic."""
    if self.esedb.impacket_compat:
        return _orig_parse_value(self, column, value, tag_field, errors)
    ctype = column.ctype
    parse_func = ctype.parse
    if column.is_text:
        parse_func = functools.partial(ctype.parse, encoding=column.encoding, errors=errors)
    multi = False
    if tag_field is not None:
        flags = int(tag_field.flags)
        if flags & _FLAG_MULTI:
            value = self._parse_multivalue(value, tag_field)
            multi = True
        elif flags & _FLAG_SEPARATED:
            value = self.table.get_long_value(bytes(value))
        elif flags & _FLAG_COMPRESSED:
            value = compression.decompress(value)
    if parse_func is None:
        parse_func = _dissect_record.noop
    return list(map(parse_func, value)) if multi else parse_func(value)


def _as_dict_fast(self: RecordData, raw: bool = False, errors: str | None = "backslashreplace") -> dict[str, Any]:
    """``RecordData.as_dict`` with the fixed/variable column paths inlined (tagged columns still go via get())."""
    if raw or self.header is None or self.esedb.impacket_compat:
        return _orig_as_dict(self, raw, errors)
    obj: dict[str, Any] = {}
    col_map = self.table._column_id_map
    data = self.data
    parse = self._parse_value
    bitmap = self._fixed_null_bitmap
    for cid in range(1, self._last_fixed_id + 1):
        column = col_map[cid]
        try:
            byte, bit = divmod(cid - 1, 8)
            if bitmap[byte] & (1 << bit):
                obj[column.name] = None
            else:
                offset = 4 + column.offset
                obj[column.name] = parse(column, data[offset : offset + column.size], None, errors)
        except Exception as e:
            obj[column.name] = f"!ERROR! {e}"
    offsets = self._variable_offsets
    var_start = self._variable_data_start
    for i, cid in enumerate(range(128, self._last_variable_id + 1)):
        column = col_map[cid]
        try:
            end = offsets[i]
            if end & 0x8000:
                obj[column.name] = None
            else:
                start = 0 if i == 0 else offsets[i - 1] & 0x7FFF
                obj[column.name] = parse(column, data[var_start + start : var_start + end], None, errors)
        except Exception as e:
            obj[column.name] = f"!ERROR! {e}"
    get = self.get
    for idx in range(self._tagged_data_count):
        column = col_map[self._get_tag_field(idx).identifier]
        try:
            obj[column.name] = get(column, raw, errors)
        except Exception as e:
            obj[column.name] = f"!ERROR! {e}"
    return obj


def _install_record_fast_paths() -> dict[str, bool]:
    # Fingerprints are taken before anything is patched.
    parse_value_ok = _matches("RecordData._parse_value")
    as_dict_ok = parse_value_ok and _matches(
        "RecordData.get", "RecordData.as_dict", "RecordData._get_fixed", "RecordData._get_variable"
    )
    if getattr(_dissect_record, "lru_cache", None) is functools.lru_cache:
        _dissect_record.lru_cache = _memo
    if parse_value_ok:
        RecordData._parse_value = _parse_value_fast
    if as_dict_ok:
        RecordData.as_dict = _as_dict_fast
    return {
        "memo": _dissect_record.lru_cache is _memo,
        "parse_value": RecordData._parse_value is _parse_value_fast,
        "as_dict": RecordData.as_dict is _as_dict_fast,
    }


FAST_PATHS = _install_record_fast_paths()


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
