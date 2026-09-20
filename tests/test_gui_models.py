"""Headless checks of the Qt record model: rendering must equal display_value, index lookups must stay exact."""

from __future__ import annotations

import os
import uuid
from datetime import datetime, timezone

import pytest

pytest.importorskip("PySide6")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication

from edb_explorer.core import ColumnInfo
from edb_explorer.core.values import display_value
from edb_explorer.gui.models import RAW_ROLE, RecordFilterProxy, RecordTableModel


@pytest.fixture(scope="module")
def app() -> QApplication:
    return QApplication.instance() or QApplication([])


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
ROWS = [
    {"n": 1, "f": 1.5, "b": True, "when": 44197.5, "hinted": 1_600_000_000, "s": "plain", "blob": b"a\x00b\x00"},
    {"n": -(2**40), "f": float("nan"), "b": False, "when": 0, "s": "with\nnewline\r", "multi": [b"x", 1, "y"]},
    {"n": 3, "f": float("inf"), "s": "x" * 400, "blob": bytes(range(16)), "g": uuid.UUID(int=7)},
    {"n": 4, "s": "", "when": datetime(2020, 1, 1, tzinfo=timezone.utc), "blob": b""},
]


def test_display_text_matches_display_value(app: QApplication) -> None:
    model = RecordTableModel(COLUMNS)
    model.append_rows(list(range(len(ROWS))), ROWS)
    for r, row in enumerate(ROWS):
        for c, col in enumerate(COLUMNS):
            expected = "" if row.get(col.name) is None else display_value(row[col.name], col.type, 300)
            assert model.display_text(r, c) == expected, (r, col.name)
            assert model.display_text(r, c) == expected  # second call may come from the cache
            assert model.data(model.index(r, c), Qt.ItemDataRole.DisplayRole) == expected
            raw = model.data(model.index(r, c), RAW_ROLE)
            assert raw == row.get(col.name) or (raw != raw and row[col.name] != row[col.name])  # NaN-safe
    assert model.seen_columns == {0, 1, 2, 3, 4, 5, 6, 7, 8}
    assert model.seen_sorted == tuple(range(9))
    assert model.raw_row(1) is ROWS[1]


def test_index_lookup_sorted_and_unsorted(app: QApplication) -> None:
    model = RecordTableModel(COLUMNS)
    model.append_rows([5, 9, 20], ROWS[:3])
    model.append_rows([21], ROWS[3:])
    assert [model.row_index(i) for i in range(4)] == [5, 9, 20, 21]
    assert [model.model_row_for_index(i) for i in (5, 9, 20, 21, 0, 6, 22)] == [0, 1, 2, 3, None, None, None]
    assert model.headerData(2, Qt.Orientation.Vertical) == "20"
    # out-of-order indices switch to the exact dict lookup (last one wins for duplicates, as before)
    model.append_rows([3, 9], ROWS[:2])
    assert model.model_row_for_index(3) == 4
    assert model.model_row_for_index(9) == 5
    assert model.model_row_for_index(21) == 3
    model.clear()
    assert model.rowCount() == 0 and model.model_row_for_index(5) is None and model.seen_sorted == ()


def test_filter_proxy(app: QApplication) -> None:
    model = RecordTableModel(COLUMNS)
    model.append_rows(list(range(len(ROWS))), ROWS)
    proxy = RecordFilterProxy()
    proxy.setSourceModel(model)
    proxy.set_filter("PLAIN")
    assert proxy.rowCount() == 1
    proxy.set_filter("PLAIN", case_sensitive=True)
    assert proxy.rowCount() == 0
    proxy.set_filter(r"^-\d+$", regex=True)
    assert proxy.rowCount() == 1 and proxy.index(0, 0).data(RAW_ROLE) == -(2**40)
    proxy.set_filter(
        "x", column=5
    )  # column-only: the 400 x's row and "xxx" in row 0? no - only rows whose s contains x
    assert proxy.rowCount() == 1
    proxy.set_filter("")
    assert proxy.rowCount() == 4
