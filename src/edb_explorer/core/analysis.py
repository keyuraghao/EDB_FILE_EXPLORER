"""Analysis helpers: column statistics, timestamp detection, cross-database timeline, database summary."""

from __future__ import annotations

import re
import statistics
from collections import Counter
from collections.abc import Callable, Iterator
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any

from edb_explorer.core.database import Database
from edb_explorer.core.values import (
    TIMESTAMP_KINDS,
    decode_ese_datetime,
    decode_timestamp_kind,
    display_value,
    guess_timestamp_kind,
)

ProgressCallback = Callable[[str], bool]
_ISO_RE = re.compile(r"^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(:\d{2})?")
MAX_DISTINCT_TRACKED = 50_000


# --------------------------------------------------------------------------- #
# Timestamp column detection
# --------------------------------------------------------------------------- #
_TIME_WORDS = ("time", "date", "stamp", "created", "modified", "accessed", "expir", "visit", "_at", "_on")
_NOT_TIME_WORDS = ("timeline", "timeout", "runtime", "lifetime", "uptime", "datetaken_id", "update_count")


def _name_suggests_time(column: str) -> bool:
    """Only accept ambiguous integer encodings (unix, cocoa ...) when the column name hints at a time."""
    name = column.lower()
    if any(w in name for w in _NOT_TIME_WORDS):
        return False
    return any(w in name for w in _TIME_WORDS) or name in ("when", "start", "end", "first", "last", "ts")


def detect_timestamp_columns(db: Database, table: str, sample: int = 300) -> dict[str, str]:
    """Return {column: kind} for columns that look like timestamps.

    Sources, in priority order: profile hints (``ts:<kind>``), ESE ``DateTime``
    columns, a magnitude-based guess over a sample of numeric values, and
    ISO-8601-looking text.
    """
    real = db.resolve_table_name(table)
    types = db.column_types(real)
    kinds: dict[str, str] = {}
    for col, t in types.items():
        if t.startswith("ts:"):
            kinds[col] = t[3:]
        elif t == "DateTime":
            kinds[col] = "ese"
    samples: dict[str, list[Any]] = {c: [] for c in types if c not in kinds}
    if samples:
        for _, values in db.iter_records(real, stop=sample):
            for c, bucket in samples.items():
                v = values.get(c)
                if v is not None:
                    bucket.append(v)
        for c, vals in samples.items():
            if not vals:
                continue
            if all(isinstance(v, int | float) and not isinstance(v, bool) for v in vals):
                guess = guess_timestamp_kind(vals)
                if guess and (guess in ("filetime", "webkit") or _name_suggests_time(c)):
                    kinds[c] = guess
            elif all(isinstance(v, str) for v in vals) and sum(1 for v in vals if _ISO_RE.match(v)) >= max(
                1, int(0.9 * len(vals))
            ):
                kinds[c] = "iso"
    return kinds


def decode_any(value: Any, kind: str) -> datetime | None:
    """Decode a value of a known timestamp kind. ISO-8601 text is accepted for every kind because
    ``Database.iter_records`` already renders hinted columns as ISO strings."""
    if isinstance(value, str):
        try:
            dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    if kind == "iso":
        return None
    if kind == "ese":
        d = decode_ese_datetime(value) if isinstance(value, int | float) else None
        return d if isinstance(d, datetime) else None
    return decode_timestamp_kind(value, kind)


# --------------------------------------------------------------------------- #
# Column statistics
# --------------------------------------------------------------------------- #
@dataclass(slots=True)
class ColumnStats:
    name: str
    type: str
    rows: int = 0
    non_null: int = 0
    distinct: int = 0
    distinct_exact: bool = True
    kinds: dict[str, int] = field(default_factory=dict)  # python type counts: int/float/str/bytes/list
    min: Any = None
    max: Any = None
    mean: float | None = None
    min_length: int | None = None
    max_length: int | None = None
    top_values: list[tuple[str, int]] = field(default_factory=list)
    timestamp_kind: str | None = None
    timestamp_min: str | None = None
    timestamp_max: str | None = None

    @property
    def nulls(self) -> int:
        return self.rows - self.non_null

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["nulls"] = self.nulls
        d["null_ratio"] = round(self.nulls / self.rows, 4) if self.rows else None
        d["timestamp_kind_name"] = TIMESTAMP_KINDS.get(
            self.timestamp_kind or "", "ISO-8601 text" if self.timestamp_kind == "iso" else None
        )
        d["min"] = display_value(self.min, None, 200) if self.min is not None else None
        d["max"] = display_value(self.max, None, 200) if self.max is not None else None
        return d


def column_statistics(
    db: Database,
    table: str,
    columns: list[str] | None = None,
    max_rows: int | None = None,
    top: int = 5,
    progress: ProgressCallback | None = None,
) -> tuple[list[ColumnStats], int]:
    """Profile every column of a table in a single pass. Returns (stats, rows_scanned)."""
    real = db.resolve_table_name(table)
    types = db.column_types(real)
    names = [c for c in (columns or db.table(real).column_names) if c in types]
    ts_kinds = detect_timestamp_columns(db, real)
    stats = {c: ColumnStats(name=c, type=types[c]) for c in names}
    counters: dict[str, Counter[Any]] = {c: Counter() for c in names}
    overflow: set[str] = set()
    sums: dict[str, float] = dict.fromkeys(names, 0.0)
    nums: dict[str, int] = dict.fromkeys(names, 0)
    ts_min: dict[str, datetime] = {}
    ts_max: dict[str, datetime] = {}
    n = 0
    for _, values in db.iter_records(real, stop=max_rows, include_nulls=False):
        n += 1
        for c in names:
            v = values.get(c)
            st = stats[c]
            if v is None:
                continue
            st.non_null += 1
            kind = type(v).__name__
            st.kinds[kind] = st.kinds.get(kind, 0) + 1
            key: Any = v
            if isinstance(v, bytes | bytearray | memoryview):
                key = bytes(v)
            elif isinstance(v, list | dict):
                key = display_value(v, None, 500)
            if c not in overflow:
                counters[c][key] += 1
                if len(counters[c]) > MAX_DISTINCT_TRACKED:
                    overflow.add(c)
                    st.distinct_exact = False
            if isinstance(v, bool):
                pass
            elif isinstance(v, int | float):
                nums[c] += 1
                sums[c] += float(v)
                if st.min is None or v < st.min:
                    st.min = v
                if st.max is None or v > st.max:
                    st.max = v
            elif isinstance(v, str | bytes):
                ln = len(v)
                st.min_length = ln if st.min_length is None else min(st.min_length, ln)
                st.max_length = ln if st.max_length is None else max(st.max_length, ln)
                if isinstance(v, str):
                    if st.min is None or (isinstance(st.min, str) and v < st.min):
                        st.min = v
                    if st.max is None or (isinstance(st.max, str) and v > st.max):
                        st.max = v
            kind_ts = ts_kinds.get(c)
            if kind_ts:
                dt = decode_any(v, kind_ts)
                if dt is not None:
                    if c not in ts_min or dt < ts_min[c]:
                        ts_min[c] = dt
                    if c not in ts_max or dt > ts_max[c]:
                        ts_max[c] = dt
        if progress and n % 5000 == 0 and not progress(f"{real}: {n:,} rows"):
            break
    out: list[ColumnStats] = []
    for c in names:
        st = stats[c]
        st.rows = n
        st.distinct = len(counters[c]) if c not in overflow else MAX_DISTINCT_TRACKED
        if nums[c]:
            st.mean = sums[c] / nums[c]
        st.top_values = [(display_value(k, types.get(c), 120), cnt) for k, cnt in counters[c].most_common(top)]
        st.timestamp_kind = ts_kinds.get(c)
        if c in ts_min:
            st.timestamp_min = ts_min[c].isoformat()
            st.timestamp_max = ts_max[c].isoformat()
        out.append(st)
    return out, n


# --------------------------------------------------------------------------- #
# Timeline
# --------------------------------------------------------------------------- #
@dataclass(slots=True)
class TimelineEvent:
    timestamp: str
    database: str
    table: str
    row: int
    column: str
    kind: str
    summary: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _summary(values: dict[str, Any], types: dict[str, str], skip: set[str], limit: int = 160) -> str:
    parts: list[str] = []
    for k, v in values.items():
        if k in skip or v is None:
            continue
        text = display_value(v, types.get(k), 60)
        if not text or text.startswith("0x"):
            continue
        parts.append(f"{k}={text}")
        if sum(len(p) for p in parts) > limit:
            break
    return "; ".join(parts)[:limit]


def iter_timeline(
    db: Database,
    tables: list[str] | None = None,
    start: datetime | None = None,
    end: datetime | None = None,
    max_rows_per_table: int | None = 250_000,
    include_system: bool = False,
    should_stop: Callable[[], bool] | None = None,
    progress: ProgressCallback | None = None,
) -> Iterator[TimelineEvent]:
    """Yield one event per (row, timestamp column) for every table that has timestamp columns."""
    names = [db.resolve_table_name(t) for t in tables] if tables else db.table_names(include_system)
    for name in names:
        if should_stop and should_stop():
            return
        kinds = detect_timestamp_columns(db, name)
        if not kinds:
            continue
        if progress and not progress(f"{db.id}: {name}"):
            return
        types = db.column_types(name)
        skip = set(kinds)
        for index, values in db.iter_records(name, stop=max_rows_per_table, include_nulls=False):
            if should_stop and should_stop():
                return
            summary: str | None = None
            for col, kind in kinds.items():
                v = values.get(col)
                if v is None:
                    continue
                dt = decode_any(v, kind)
                if dt is None or (start and dt < start) or (end and dt > end):
                    continue
                if summary is None:
                    summary = _summary(values, types, skip)
                yield TimelineEvent(dt.isoformat(), db.id, name, index, col, kind, summary)


def build_timeline(
    databases: list[Database],
    tables: list[str] | None = None,
    start: datetime | None = None,
    end: datetime | None = None,
    limit: int = 100_000,
    max_rows_per_table: int | None = 250_000,
    include_system: bool = False,
    should_stop: Callable[[], bool] | None = None,
    progress: ProgressCallback | None = None,
) -> tuple[list[TimelineEvent], bool]:
    """Collect and sort events across databases. Returns (events, truncated)."""
    events: list[TimelineEvent] = []
    truncated = False
    for db in databases:
        for ev in iter_timeline(db, tables, start, end, max_rows_per_table, include_system, should_stop, progress):
            events.append(ev)
            if len(events) >= limit:
                truncated = True
                break
        if truncated:
            break
    events.sort(key=lambda e: e.timestamp)
    return events, truncated


# --------------------------------------------------------------------------- #
# Summary
# --------------------------------------------------------------------------- #
def database_summary(db: Database, sample_rows: int = 300, count: bool = False) -> dict[str, Any]:
    """Quick overview: tables, timestamp columns and (sampled) date range per table."""
    tables_out: list[dict[str, Any]] = []
    overall_min: datetime | None = None
    overall_max: datetime | None = None
    for t in db.tables(include_system=False):
        kinds = detect_timestamp_columns(db, t.name, sample=sample_rows)
        entry: dict[str, Any] = {
            "name": t.name,
            "display_name": t.display_name,
            "columns": len(t.columns),
            "record_count": db.count_records(t.name) if count else t.record_count,
            "timestamp_columns": kinds,
        }
        if kinds:
            lo: datetime | None = None
            hi: datetime | None = None
            for _, values in db.iter_records(t.name, stop=sample_rows):
                for col, kind in kinds.items():
                    dt = decode_any(values.get(col), kind)
                    if dt is None:
                        continue
                    lo = dt if lo is None or dt < lo else lo
                    hi = dt if hi is None or dt > hi else hi
            if lo:
                entry["sampled_range"] = [lo.isoformat(), hi.isoformat() if hi else None]
                overall_min = lo if overall_min is None or lo < overall_min else overall_min
                overall_max = hi if overall_max is None or (hi and hi > overall_max) else overall_max
        tables_out.append(entry)
    return {
        "database": db.id,
        "kind": db.info.kind_name,
        "profile": db.profile.name,
        "platform": db.profile.platform or None,
        "tables": len(tables_out),
        "tables_with_timestamps": sum(1 for t in tables_out if t["timestamp_columns"]),
        "sampled_time_range": [
            overall_min.isoformat() if overall_min else None,
            overall_max.isoformat() if overall_max else None,
        ],
        "views": [v.name for v in db.profile.views],
        "table_details": tables_out,
    }


def numeric_summary(values: list[float]) -> dict[str, float]:
    if not values:
        return {}
    out = {"min": min(values), "max": max(values), "mean": statistics.fmean(values)}
    if len(values) > 1:
        out["stdev"] = statistics.pstdev(values)
    return out
