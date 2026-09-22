"""Disk-backed row store so a table grid can hold *every* record of a table, however large.

The GUI keeps the first rows of a table in memory (fast to sort and filter); once a table outgrows that
budget the rows spill into a temporary SQLite file managed here and the grid reads windows of rows back
on demand.  Memory then stays flat no matter how many records the table has.

Layout of the SQLite file:

* ``rows(pos INTEGER PRIMARY KEY, idx, x, t, c<N>...)`` - one typed column ``c<N>`` per source column
  (``N`` = column position) added lazily the first time that column holds a value, so a 3,000-column
  Exchange schema with 200 populated columns costs 200 columns.  ``idx`` is the backend row index,
  ``t`` the display text of the whole row (for substring/regex filters) and ``x`` a pickled dict of the
  values SQLite cannot store natively (datetime, list, bool, big int, NaN, UUID ...); those keep a
  sortable rendition in their typed column and the exact object in ``x``.
* ``v<K>(rowid INTEGER PRIMARY KEY, pos)`` - a *view*: the positions matching a filter and/or sort, in
  display order.  Any window of a view is one ``rowid BETWEEN`` lookup, so scrolling a hundred-million
  row result stays O(log n).

Two connections are used: one for writes (appends and view builds, serialised by a lock) and one for
reads, so the GUI thread can keep fetching pages while a long filter runs (WAL mode).
"""

from __future__ import annotations

import functools
import logging
import math
import os
import pickle
import re
import shutil
import sqlite3
import tempfile
import threading
import weakref
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from edb_explorer.core.models import ColumnInfo
from edb_explorer.core.values import display_value

log = logging.getLogger(__name__)

__all__ = [
    "CELL_TEXT_LENGTH",
    "DEFAULT_CACHE_DIR",
    "DiskRowStore",
    "FilterSpec",
    "RowStoreCancelledError",
    "SortSpec",
    "StoreRows",
]

#: Cells are rendered (and filtered) at this length, exactly like the in-memory grid.
CELL_TEXT_LENGTH = 300
#: Where caches go when a store is created without an explicit directory (the GUI sets it from its
#: preferences); ``EDB_EXPLORER_CACHE_DIR`` and finally the system temp directory are the fallbacks.
DEFAULT_CACHE_DIR: str | None = None
_SEP = "\x1f"
_I64_MIN, _I64_MAX = -(2**63), 2**63 - 1
_PICKLE = pickle.HIGHEST_PROTOCOL
_ESCAPE_LIKE = re.compile(r"([%_\\])")


class RowStoreCancelledError(Exception):
    """A view build was cancelled through its ``should_stop`` callback."""


@dataclass(frozen=True, slots=True)
class FilterSpec:
    text: str
    regex: bool = False
    case_sensitive: bool = False


@dataclass(frozen=True, slots=True)
class SortSpec:
    column: int  # column position
    descending: bool = False


def _split(value: Any, ctype: str | None) -> tuple[Any, Any, str]:
    """Return ``(typed, exotic, text)`` for one cell.

    ``typed`` is what goes into the sortable column, ``exotic`` the original object when SQLite cannot hold
    it (``_MISSING`` otherwise) and ``text`` its display string.  Subclasses of the builtin scalar types
    (dissect's cstruct ints, for instance) are stored as their builtin base.
    """
    t = type(value)
    if t is str or isinstance(value, str):
        if t is not str:
            value = str(value)
        if len(value) <= CELL_TEXT_LENGTH and "\r" not in value and "\n" not in value:
            return value, _MISSING, value
        return value, _MISSING, display_value(value, ctype, CELL_TEXT_LENGTH)
    if t is bool or isinstance(value, bool):
        return int(value), bool(value), str(bool(value))
    if t is int or isinstance(value, int):
        if t is not int:
            value = int(value)
        text = _number_text(value, ctype)
        if _I64_MIN <= value <= _I64_MAX:
            return value, _MISSING, text
        return float(value), value, text
    if t is float or isinstance(value, float):
        if t is not float:
            value = float(value)
        text = _number_text(value, ctype)
        if math.isnan(value):  # would become NULL
            return None, value, text
        return value, _MISSING, text
    if t is bytes or isinstance(value, bytes):
        if t is not bytes:
            value = bytes(value)
        return value, _MISSING, display_value(value, ctype, CELL_TEXT_LENGTH)
    if isinstance(value, datetime):
        return value.isoformat(), value, display_value(value, ctype, CELL_TEXT_LENGTH)
    if isinstance(value, bytearray | memoryview):
        raw = bytes(value)
        return raw, (value if t is bytearray else raw), display_value(raw, ctype, CELL_TEXT_LENGTH)
    text = display_value(value, ctype, CELL_TEXT_LENGTH)
    if isinstance(value, list | tuple):
        value = _plain(value)
    return text, value, text


def _plain(value: Any) -> Any:
    """Recursively replace scalar subclasses (cstruct ints, ...) by their builtin base so the value pickles."""
    t = type(value)
    if t in (int, float, str, bytes, bool, type(None)):
        return value
    if isinstance(value, bool):
        return bool(value)
    if isinstance(value, int):
        return int(value)
    if isinstance(value, float):
        return float(value)
    if isinstance(value, str):
        return str(value)
    if isinstance(value, bytes):
        return bytes(value)
    if isinstance(value, list):
        return [_plain(v) for v in value]
    if isinstance(value, tuple):
        return tuple(_plain(v) for v in value)
    if isinstance(value, dict):
        return {_plain(k): _plain(v) for k, v in value.items()}
    return value


_MISSING = object()


def _number_text(value: int | float, ctype: str | None) -> str:
    if ctype != "DateTime" and not (ctype or "").startswith("ts:"):
        return str(value)
    return display_value(value, ctype)


class StoreRows(Sequence[dict[str, Any]]):
    """A lazily fetched, read-only sequence of the rows of one view (feeds exports without loading them all)."""

    def __init__(self, store: DiskRowStore, view: int | None, columns: list[str] | None, page: int = 2000) -> None:
        self._store = store
        self._view = view
        self._columns = columns
        self._page = page
        self._len = store.view_count(view)

    def __len__(self) -> int:
        return self._len

    def _project(self, idx: int, row: dict[str, Any]) -> dict[str, Any]:
        if self._columns is None:
            return {"_row": idx, **row}
        return {"_row": idx, **{c: row.get(c) for c in self._columns}}

    def __getitem__(self, i: Any) -> Any:
        if isinstance(i, slice):
            return [self[j] for j in range(*i.indices(self._len))]
        if i < 0:
            i += self._len
        if not 0 <= i < self._len:
            raise IndexError(i)
        _pos, idx, row = self._store.fetch(self._view, i, i + 1)[0]
        return self._project(idx, row)

    def __iter__(self) -> Iterator[dict[str, Any]]:
        for start in range(0, self._len, self._page):
            for _pos, idx, row in self._store.fetch(self._view, start, min(start + self._page, self._len)):
                yield self._project(idx, row)


class DiskRowStore:
    """Append-only row store in a temporary SQLite file (see module docstring)."""

    #: SQLite VM steps between ``should_stop`` polls while a view is built.
    PROGRESS_STEPS = 20_000

    def __init__(self, columns: Sequence[ColumnInfo], directory: str | os.PathLike[str] | None = None) -> None:
        self.columns = list(columns)
        self.column_names = [c.name for c in self.columns]
        self._types = [c.type for c in self.columns]
        self._name_to_pos = {c.name: i for i, c in enumerate(self.columns)}
        base = directory or DEFAULT_CACHE_DIR or os.environ.get("EDB_EXPLORER_CACHE_DIR") or None
        self._dir = tempfile.mkdtemp(prefix="edb-rows-", dir=base)
        self.path = os.path.join(self._dir, "rows.sqlite")
        self._wlock = threading.RLock()
        self._rlock = threading.RLock()
        self._w = sqlite3.connect(self.path, isolation_level=None, check_same_thread=False)
        self._w.execute("PRAGMA journal_mode = WAL")
        self._w.execute("PRAGMA synchronous = OFF")
        self._w.execute("PRAGMA temp_store = FILE")
        self._w.execute("PRAGMA cache_size = -65536")
        self._w.execute("CREATE TABLE rows (pos INTEGER PRIMARY KEY, idx INTEGER, x BLOB, t TEXT)")
        self._r = sqlite3.connect(self.path, isolation_level=None, check_same_thread=False)
        self._r.execute("PRAGMA query_only = 1")
        self._r.execute("PRAGMA cache_size = -32768")
        for conn in (self._w, self._r):
            conn.create_function("edb_re", 2, _regexp, deterministic=True)
            conn.create_function("edb_icontains", 2, _icontains, deterministic=True)
        self.count = 0  # committed rows (what readers can see)
        self.seen: set[int] = set()  # column positions that have ever held a value
        self._seen_sorted: tuple[int, ...] = ()
        self._views: dict[int, int] = {}  # view id -> row count
        self._next_view = 1
        self._closed = False
        self._unpicklable_logged = False
        self._finalizer = weakref.finalize(self, _cleanup, self._w, self._r, self._dir)

    # ------------------------------------------------------------------ #
    # Lifecycle
    # ------------------------------------------------------------------ #
    def close(self) -> None:
        self._closed = True
        self._finalizer()

    @property
    def closed(self) -> bool:
        return self._closed

    @property
    def seen_sorted(self) -> tuple[int, ...]:
        """Column positions holding at least one value (a snapshot; safe to read while the writer appends)."""
        return self._seen_sorted

    def size_bytes(self) -> int:
        total = 0
        for name in os.listdir(self._dir) if os.path.isdir(self._dir) else ():
            try:
                total += os.path.getsize(os.path.join(self._dir, name))
            except OSError:
                pass
        return total

    # ------------------------------------------------------------------ #
    # Writing
    # ------------------------------------------------------------------ #
    def append(self, indices: Sequence[int], rows: Sequence[dict[str, Any]]) -> set[int]:
        """Append a batch (positions continue from ``count``); returns the column positions seen for the first time."""
        if not rows:
            return set()
        name_to_pos, types = self._name_to_pos, self._types
        seen = self.seen
        new_cols: set[int] = set()
        records: list[tuple[int, int, bytes | None, str, dict[int, Any]]] = []
        pos = self.count
        for idx, row in zip(indices, rows, strict=True):
            typed: dict[int, Any] = {}
            exotic: dict[str, Any] = {}
            texts: list[str] = []
            for name, value in row.items():
                if value is None:
                    continue
                ci = name_to_pos.get(name)
                if ci is None:
                    continue
                tv, ex, text = _split(value, types[ci])
                typed[ci] = tv
                texts.append(text)
                if ex is not _MISSING:
                    exotic[name] = ex
                if ci not in seen:
                    seen.add(ci)
                    new_cols.add(ci)
            blob = self._pickle(exotic) if exotic else None
            records.append((pos, idx, blob, _SEP.join(texts), typed))
            pos += 1
        with self._wlock:
            self._w.execute("BEGIN")
            try:
                for ci in sorted(new_cols):
                    self._w.execute(f"ALTER TABLE rows ADD COLUMN c{ci}")
                # rows in one batch rarely share the exact same key set; group by key set so each INSERT
                # statement binds only the columns its rows actually carry (wide sparse tables stay cheap)
                groups: dict[tuple[int, ...], list[tuple[Any, ...]]] = {}
                for p, idx, blob, text, typed in records:
                    key = tuple(sorted(typed))
                    groups.setdefault(key, []).append((p, idx, blob, text, *(typed[c] for c in key)))
                for key, values in groups.items():
                    cols = "pos, idx, x, t" + "".join(f", c{c}" for c in key)
                    marks = ", ".join("?" for _ in range(4 + len(key)))
                    self._w.executemany(f"INSERT INTO rows ({cols}) VALUES ({marks})", values)
                self._w.execute("COMMIT")
            except BaseException:
                self._w.execute("ROLLBACK")
                raise
            self.count = pos
            if new_cols:
                self._seen_sorted = tuple(sorted(seen))
        return new_cols

    def _pickle(self, exotic: dict[str, Any]) -> bytes:
        try:
            return pickle.dumps(exotic, _PICKLE)
        except Exception:
            if not self._unpicklable_logged:
                self._unpicklable_logged = True
                log.warning("Row store: some values cannot be pickled and are kept as text", exc_info=True)
            safe = {}
            for k, v in exotic.items():
                for candidate in (v, _plain(v), display_value(v, None, None)):  # type: ignore[arg-type]
                    try:
                        pickle.dumps(candidate, _PICKLE)
                    except Exception:
                        continue
                    safe[k] = candidate
                    break
            return pickle.dumps(safe, _PICKLE)

    # ------------------------------------------------------------------ #
    # Views (filter / sort)
    # ------------------------------------------------------------------ #
    def build_view(
        self,
        filter_spec: FilterSpec | None,
        sort_spec: SortSpec | None,
        should_stop: Callable[[], bool] | None = None,
    ) -> int:
        """Materialise a filtered and/or sorted position list; returns its view id (``view_count`` gives its size)."""
        where, params = self._where(filter_spec)
        order = ""
        if sort_spec is not None and sort_spec.column in self.seen:
            col = f"c{sort_spec.column}"
            direction = "DESC NULLS FIRST" if sort_spec.descending else "ASC NULLS LAST"
            order = f" ORDER BY {col} COLLATE NOCASE {direction}, pos"
        with self._wlock:
            vid = self._next_view
            self._next_view += 1
            if should_stop is not None:
                if should_stop():
                    raise RowStoreCancelledError
                self._w.set_progress_handler(lambda: 1 if should_stop() else 0, self.PROGRESS_STEPS)
            try:
                self._w.execute(f"CREATE TABLE v{vid} (rowid INTEGER PRIMARY KEY, pos INTEGER)")
                self._w.execute(f"INSERT INTO v{vid} (pos) SELECT pos FROM rows{where}{order}", params)
                n = self._w.execute(f"SELECT count(*) FROM v{vid}").fetchone()[0]
            except sqlite3.OperationalError as exc:
                self._w.set_progress_handler(None, 0)  # or the DROP would be interrupted too
                self._w.execute(f"DROP TABLE IF EXISTS v{vid}")
                if "interrupted" in str(exc):
                    raise RowStoreCancelledError from exc
                raise
            finally:
                self._w.set_progress_handler(None, 0)
            self._views[vid] = n
        return vid

    def _where(self, spec: FilterSpec | None) -> tuple[str, tuple[Any, ...]]:
        if spec is None or not spec.text:
            return "", ()
        if spec.regex:
            try:
                re.compile(spec.text)
            except re.error:
                return "", ()  # the in-memory proxy also shows everything for a broken pattern
            return " WHERE edb_re(?, t)", (("" if spec.case_sensitive else "(?i)") + spec.text,)
        if spec.case_sensitive:
            return " WHERE instr(t, ?) > 0", (spec.text,)
        if spec.text.isascii():
            return " WHERE t LIKE ? ESCAPE '\\'", ("%" + _ESCAPE_LIKE.sub(r"\\\1", spec.text) + "%",)
        return " WHERE edb_icontains(?, t)", (spec.text.lower(),)

    def drop_view(self, vid: int | None) -> None:
        if vid is None or vid not in self._views:
            return
        with self._wlock:
            self._views.pop(vid, None)
            try:
                self._w.execute(f"DROP TABLE IF EXISTS v{vid}")
            except sqlite3.Error:
                pass

    def view_count(self, vid: int | None) -> int:
        return self.count if vid is None else self._views.get(vid, 0)

    # ------------------------------------------------------------------ #
    # Reading
    # ------------------------------------------------------------------ #
    def fetch(self, vid: int | None, start: int, stop: int) -> list[tuple[int, int, dict[str, Any]]]:
        """Rows ``start <= view_row < stop`` of a view (``None`` = storage order) as ``(pos, idx, row)``."""
        if stop <= start:
            return []
        cols = self._seen_sorted
        names = [self.column_names[c] for c in cols]
        select = "r.pos, r.idx, r.x" + "".join(f", r.c{c}" for c in cols)
        if vid is None:
            sql = f"SELECT {select} FROM rows r WHERE r.pos >= ? AND r.pos < ? ORDER BY r.pos"
            params: tuple[int, int] = (start, stop)
        else:
            sql = (
                f"SELECT {select} FROM v{vid} v JOIN rows r ON r.pos = v.pos "
                "WHERE v.rowid > ? AND v.rowid <= ? ORDER BY v.rowid"
            )
            params = (start, stop)
        with self._rlock:
            recs = self._r.execute(sql, params).fetchall()
        out = []
        loads = pickle.loads
        for rec in recs:
            row = {n: v for n, v in zip(names, rec[3:], strict=True) if v is not None}
            if rec[2] is not None:
                row.update(loads(rec[2]))
            out.append((rec[0], rec[1], row))
        return out

    def position_of_index(self, idx: int) -> int | None:
        """Storage position of the row with backend index ``idx`` (they normally coincide)."""
        with self._rlock:
            hit = self._r.execute("SELECT idx FROM rows WHERE pos = ?", (idx,)).fetchone()
            if hit is not None and hit[0] == idx:
                return idx
            hit = self._r.execute("SELECT pos FROM rows WHERE idx = ? LIMIT 1", (idx,)).fetchone()
        return None if hit is None else int(hit[0])

    def view_row_for_position(self, vid: int | None, pos: int) -> int | None:
        if vid is None:
            return pos if 0 <= pos < self.count else None
        with self._rlock:
            hit = self._r.execute(f"SELECT rowid FROM v{vid} WHERE pos = ? LIMIT 1", (pos,)).fetchone()
        return None if hit is None else int(hit[0]) - 1

    def rows(self, vid: int | None = None, columns: list[str] | None = None) -> StoreRows:
        return StoreRows(self, vid, columns)


@functools.lru_cache(maxsize=32)
def _compile(pattern: str) -> re.Pattern[str] | None:
    try:
        return re.compile(pattern)
    except re.error:
        return None


def _regexp(pattern: str, text: str | None) -> int:
    """Regex filter: each cell is matched on its own, like the in-memory proxy (so ``^``/``$`` anchor a cell)."""
    if text is None:
        return 0
    rx = _compile(pattern)
    if rx is None:
        return 0
    search = rx.search
    return int(any(search(cell) for cell in text.split(_SEP)))


def _icontains(needle: str, text: str | None) -> int:
    return 0 if text is None else int(needle in text.lower())


def _cleanup(w: sqlite3.Connection, r: sqlite3.Connection, directory: str) -> None:
    for conn in (r, w):
        try:
            conn.close()
        except Exception:
            pass
    shutil.rmtree(directory, ignore_errors=True)
