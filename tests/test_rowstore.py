"""The disk row store must give back exactly the rows it was given and filter/sort like the in-memory grid."""

from __future__ import annotations

import os
import re
import uuid
from datetime import datetime, timezone
from typing import Any

import pytest

from edb_explorer.core import ColumnInfo
from edb_explorer.core.rowstore import DiskRowStore, FilterSpec, RowStoreCancelledError, SortSpec, StoreRows
from edb_explorer.core.values import display_value


def _col(i: int, name: str, ctype: str) -> ColumnInfo:
    return ColumnInfo(i, name, ctype, 0, "fixed", None, None, ctype == "Text", ctype == "Binary")


COLUMNS = (
    _col(1, "n", "Long"),
    _col(2, "f", "IEEEDouble"),
    _col(3, "b", "Bit"),
    _col(4, "when", "DateTime"),
    _col(5, "hinted", "ts:unix"),
    _col(6, "s", "Text"),
    _col(7, "blob", "Binary"),
    _col(8, "multi", "LongBinary"),
    _col(9, "g", "GUID"),
    _col(10, "never_set", "Long"),
)
ROWS: list[dict[str, Any]] = [
    {"n": 1, "f": 1.5, "b": True, "when": 44197.5, "hinted": 1_600_000_000, "s": "plain", "blob": b"a\x00b\x00"},
    {"n": -(2**40), "f": float("nan"), "b": False, "when": 0, "s": "with\nnewline\r", "multi": [b"x", 1, "y"]},
    {"n": 3, "f": float("inf"), "s": "x" * 400, "blob": bytes(range(16)), "g": uuid.UUID(int=7)},
    {"n": 4, "s": "", "when": datetime(2020, 1, 1, tzinfo=timezone.utc), "blob": b""},
    {"n": 2**70, "s": "Zulu", "blob": bytearray(b"ba")},  # beyond int64, bytearray
    {"n": 5, "s": "alpha", "hinted": 1_700_000_000.25},
]


def _same(a: Any, b: Any) -> bool:
    if isinstance(a, float) and isinstance(b, float) and a != a and b != b:
        return True
    return a == b and type(a) is type(b)


@pytest.fixture
def store(tmp_path: Any) -> Any:
    st = DiskRowStore(COLUMNS, tmp_path)
    yield st
    st.close()


def test_round_trip_every_value_type(store: DiskRowStore, tmp_path: Any) -> None:
    assert store.append([10, 11, 12], ROWS[:3]) == {0, 1, 2, 3, 4, 5, 6, 7, 8}
    assert store.append([13, 14, 15], ROWS[3:]) == set()
    assert store.count == 6 and store.seen_sorted == tuple(range(9))
    got = store.fetch(None, 0, 6)
    assert [g[0] for g in got] == list(range(6)) and [g[1] for g in got] == list(range(10, 16))
    for (_pos, _idx, row), expected in zip(got, ROWS, strict=True):
        assert set(row) == set(expected)
        for k, v in expected.items():
            assert _same(row[k], v), (k, row[k], v)
    assert store.fetch(None, 4, 100)[0][2]["s"] == "Zulu"
    assert store.fetch(None, 3, 3) == []
    assert store.position_of_index(12) == 2 and store.position_of_index(99) is None
    assert store.view_row_for_position(None, 5) == 5 and store.view_row_for_position(None, 6) is None
    assert os.path.exists(store.path)
    path = store.path
    store.close()
    assert not os.path.exists(path) and store.closed


def test_filters_match_display_text_like_the_proxy(store: DiskRowStore) -> None:
    store.append(list(range(len(ROWS))), ROWS)

    def expected(spec: FilterSpec) -> list[int]:
        out = []
        needle = spec.text if spec.case_sensitive else spec.text.lower()
        rx = None
        if spec.regex:
            try:
                rx = re.compile(spec.text, 0 if spec.case_sensitive else re.IGNORECASE)
            except re.error:
                return list(range(len(ROWS)))  # RecordFilterProxy shows everything for a broken pattern
        for i, row in enumerate(ROWS):
            for c in COLUMNS:
                v = row.get(c.name)
                if v is None:
                    continue
                text = display_value(v, c.type, 300)
                if not text:
                    continue
                hit = rx.search(text) if rx else (needle in (text if spec.case_sensitive else text.lower()))
                if hit:
                    out.append(i)
                    break
        return out

    specs = [
        FilterSpec("PLAIN"),
        FilterSpec("PLAIN", case_sensitive=True),
        FilterSpec("plain", case_sensitive=True),
        FilterSpec(r"^-\d+$", regex=True),
        FilterSpec(r"^-\d+", regex=True),
        FilterSpec("newline"),
        FilterSpec("\\n"),  # newlines are shown escaped, so the two characters "\n" match row 1
        FilterSpec("%"),
        FilterSpec("_"),
        FilterSpec("2020-01-01"),
        FilterSpec("zulu"),
        FilterSpec("ZULU", case_sensitive=True),
        FilterSpec("00000000-0000-0000-0000-000000000007"),
        FilterSpec("[x"),
        FilterSpec("(unclosed", regex=True),  # broken pattern: everything, like the proxy
        FilterSpec("ünïcode"),
    ]
    for spec in specs:
        vid = store.build_view(spec, None)
        got = [pos for pos, _idx, _row in store.fetch(vid, 0, 100)]
        assert got == expected(spec), spec
        assert store.view_count(vid) == len(got)
        store.drop_view(vid)
    assert store._views == {}


def test_sort_views_and_windows(store: DiskRowStore) -> None:
    store.append(list(range(len(ROWS))), ROWS)
    asc = store.build_view(None, SortSpec(0, False))
    order = [row["n"] for _p, _i, row in store.fetch(asc, 0, 10)]
    assert order == [-(2**40), 1, 3, 4, 5, 2**70]
    desc = store.build_view(None, SortSpec(0, True))
    assert [row["n"] for _p, _i, row in store.fetch(desc, 0, 10)] == order[::-1]
    # NULLs last ascending, first descending; strings compare case-insensitively
    by_s = store.build_view(None, SortSpec(5, False))
    assert [row.get("s") for _p, _i, row in store.fetch(by_s, 0, 10)] == [
        "",
        "alpha",
        "plain",
        "with\nnewline\r",
        "x" * 400,
        "Zulu",
    ]
    by_g = store.build_view(None, SortSpec(8, False))
    assert store.fetch(by_g, 0, 1)[0][2]["n"] == 3  # the only GUID sorts first ascending
    by_g_desc = store.build_view(None, SortSpec(8, True))
    assert [row["n"] for _p, _i, row in store.fetch(by_g_desc, 0, 10)][-1] == 3
    # never-populated column: natural order
    nat = store.build_view(None, SortSpec(9, False))
    assert [p for p, _i, _r in store.fetch(nat, 0, 10)] == list(range(6))
    # filter + sort, windowed
    both = store.build_view(FilterSpec("a"), SortSpec(0, True))
    rows = store.fetch(both, 1, 3)
    # rows 0, 1 ("False"), 2 (GUID hex) and 5 match; n descending = 5, 3, 1, -2**40
    assert store.view_count(both) == 4 and [r["n"] for _p, _i, r in rows] == [3, 1]
    assert store.view_row_for_position(both, 0) == 2 and store.view_row_for_position(both, 4) is None


def test_lazy_columns_wide_schema(tmp_path: Any) -> None:
    cols = tuple(_col(i, f"c{i}", "Long") for i in range(3000))  # more than SQLite's column limit
    st = DiskRowStore(cols, tmp_path)
    try:
        st.append([0, 1], [{"c7": 1, "c2999": 2}, {"c7": 3}])
        assert st.seen_sorted == (7, 2999)
        assert [r for _p, _i, r in st.fetch(None, 0, 2)] == [{"c7": 1, "c2999": 2}, {"c7": 3}]
        st.append([2], [{"c1500": 9}])
        assert st.fetch(None, 2, 3)[0][2] == {"c1500": 9}
    finally:
        st.close()


def test_store_rows_sequence_and_cancel(store: DiskRowStore) -> None:
    store.append(list(range(len(ROWS))), ROWS)
    seq = store.rows(None, ["n", "s"])
    assert isinstance(seq, StoreRows) and len(seq) == 6
    assert seq[0] == {"_row": 0, "n": 1, "s": "plain"} and seq[-1]["n"] == 5
    assert [r["_row"] for r in seq] == list(range(6)) and len(seq[1:3]) == 2
    with pytest.raises(IndexError):
        seq[6]
    full = store.rows(store.build_view(FilterSpec("alpha"), None))
    assert list(full) == [{"_row": 5, "n": 5, "s": "alpha", "hinted": 1_700_000_000.25}]
    before = dict(store._views)
    with pytest.raises(RowStoreCancelledError):
        store.build_view(None, SortSpec(0, False), should_stop=lambda: True)
    polls = iter([False, False, True, True, True, True])
    store.PROGRESS_STEPS = 1
    with pytest.raises(RowStoreCancelledError):
        store.build_view(None, SortSpec(0, False), should_stop=lambda: next(polls, True))
    assert store._views == before  # cancelled views are not registered (and their tables are gone)
    assert [r[0] for r in store._r.execute("SELECT name FROM sqlite_master WHERE name LIKE 'v%'")] == [
        f"v{k}" for k in before
    ]


def test_scalar_subclasses_round_trip_as_builtins(tmp_path: Any) -> None:
    """dissect hands back cstruct int subclasses (also inside multi-value lists); they must survive the store."""

    class Int32(int):
        pass  # not picklable by reference from a function scope, like types.int32

    class Blob(bytes):
        pass

    st = DiskRowStore(COLUMNS, tmp_path)
    try:
        st.append([0], [{"n": Int32(7), "blob": Blob(b"ab"), "multi": [Int32(1), Int32(2)], "s": "x"}])
        _pos, _idx, row = st.fetch(None, 0, 1)[0]
        assert row == {"n": 7, "blob": b"ab", "multi": [1, 2], "s": "x"}
        assert type(row["n"]) is int and type(row["blob"]) is bytes and all(type(v) is int for v in row["multi"])
        assert st.view_count(st.build_view(FilterSpec("[1, 2]"), None)) == 1
        assert not st._unpicklable_logged
    finally:
        st.close()


def test_column_scoped_filter(store: DiskRowStore) -> None:
    store.append(list(range(len(ROWS))), ROWS)
    # "1" appears in many columns, but only rows 0 (n=1) match when the filter is restricted to column n
    assert store.view_count(store.build_view(FilterSpec("1"), None)) >= 3
    only_n = store.build_view(FilterSpec("1", column=0), None)
    assert [p for p, _i, _r in store.fetch(only_n, 0, 10)] == [0, 1, 4]  # 1, -1099511627776, 2**70 all contain "1"
    assert store.view_count(store.build_view(FilterSpec("^1$", regex=True, column=0), None)) == 1
    assert store.view_count(store.build_view(FilterSpec("ZULU", column=5), None)) == 1  # case-insensitive
    assert store.view_count(store.build_view(FilterSpec("ZULU", case_sensitive=True, column=5), None)) == 0
    # DateTime column is matched on its decoded display text, like the grid shows it
    assert store.view_count(store.build_view(FilterSpec("2021-01-01", column=3), None)) == 1
    assert store.view_count(store.build_view(FilterSpec("x", column=9), None)) == 0  # never-populated column
