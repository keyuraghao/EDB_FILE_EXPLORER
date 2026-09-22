"""Headless checks of the Qt record model: rendering must equal display_value, index lookups must stay exact."""

from __future__ import annotations

import os
import uuid
from datetime import datetime, timezone
from typing import Any

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


def test_action_icons_are_distinct_and_non_empty(app: QApplication) -> None:
    from edb_explorer.gui.icons import ICON_NAMES, icon, render_glyph

    seen: dict[bytes, str] = {}
    for name in ICON_NAMES:
        pm = render_glyph(name, 24)
        img = pm.toImage()
        raw = bytes(img.constBits())
        assert any(raw), name  # something was painted
        assert raw not in seen, (name, seen.get(raw))  # no two actions share a glyph
        seen[raw] = name
        assert not icon(name).isNull() and icon(name) is icon(name)  # cached
    assert icon("no-such-glyph").isNull()


def test_theme_switching(app: QApplication) -> None:
    from edb_explorer.gui.theme import THEMES, apply_theme, current_theme, resolve_theme, theme_preference

    assert THEMES == ("light", "dark", "system")
    assert apply_theme(app, "light") == "light" and current_theme(app) == "light" and theme_preference(app) == "light"
    assert apply_theme(app, "dark") == "dark" and current_theme(app) == "dark"
    shown = apply_theme(app, "system")
    assert shown in ("light", "dark") and shown == resolve_theme(app, "system") and theme_preference(app) == "system"
    assert apply_theme(app, "bogus") == "dark" and theme_preference(app) == "dark"


# --------------------------------------------------------------------------- #
# Disk mode: tables bigger than the memory budget
# --------------------------------------------------------------------------- #
def test_model_disk_mode_matches_memory_mode(app: QApplication, tmp_path: Any) -> None:
    from edb_explorer.core.rowstore import DiskRowStore, SortSpec

    mem = RecordTableModel(COLUMNS)
    mem.append_rows(list(range(len(ROWS))), ROWS)
    store = DiskRowStore(COLUMNS, tmp_path)
    store.append([0, 1], ROWS[:2])
    model = RecordTableModel(COLUMNS)
    model.append_rows([0, 1], ROWS[:2])
    model.attach_store(store)
    assert model.disk and model.rowCount() == 2 and model.seen_columns == {0, 1, 2, 3, 4, 5, 6, 7}
    assert model.store_grew(4, set(store.append([2, 3], ROWS[2:]))) == {8}  # the GUID column appears in row 2
    assert model.rowCount() == 4 and model.seen_sorted == mem.seen_sorted and model.seen_columns == mem.seen_columns
    for r in range(4):
        assert model.row_index(r) == r and model.headerData(r, Qt.Orientation.Vertical) == str(r)
        for c in range(len(COLUMNS)):
            assert model.display_text(r, c) == mem.display_text(r, c), (r, c)
            assert model.data(model.index(r, c), Qt.ItemDataRole.DisplayRole) == mem.display_text(r, c)
            raw, want = model.data(model.index(r, c), RAW_ROLE), mem.data(mem.index(r, c), RAW_ROLE)
            assert raw == want or (raw != raw and want != want)
    assert model.raw_row(2)["g"] == ROWS[2]["g"] and model.model_row_for_index(3) == 3
    assert model.model_row_for_index(9) is None
    heard: list[tuple[int, bool]] = []
    model.sort_requested.connect(lambda c, d: heard.append((c, d)))
    model.sort(0, Qt.SortOrder.DescendingOrder)
    assert heard == [(0, True)]
    model.set_view(store.build_view(None, SortSpec(0, True)))
    assert [model.raw_row(r)["n"] for r in range(4)] == [4, 3, 1, -(2**40)]
    assert model.model_row_for_index(0) == 2 and [model.row_index(r) for r in range(4)] == [3, 2, 0, 1]
    rows = model.store_rows(["n"])
    assert len(rows) == 4 and [r["n"] for r in rows] == [4, 3, 1, -(2**40)]
    model.set_view(None)
    assert [model.raw_row(r)["n"] for r in range(4)] == [1, -(2**40), 3, 4] and store._views == {}
    model.clear()
    assert not model.disk and model.rowCount() == 0 and store.closed


def _pump(app: QApplication, done: Any, timeout: float = 20.0) -> None:
    import time

    deadline = time.time() + timeout
    while not done() and time.time() < deadline:
        app.processEvents()
        time.sleep(0.005)
    assert done(), "timed out"


def test_table_tab_loads_every_row_by_spilling_to_disk(
    app: QApplication, fake_edb: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from edb_explorer.core import Session
    from edb_explorer.gui.widgets.table_tab import TableTab
    from tests.conftest import FakeColumn, FakeEseDB, FakeTable

    big = [{"Id": i, "Name": f"item-{i:05d}", "Odd": bool(i % 2)} for i in range(1_200)]

    class BigFakeEseDB(FakeEseDB):
        def __init__(self, fh: Any, impacket_compat: bool = False) -> None:
            super().__init__(fh, impacket_compat)
            cols = [FakeColumn(1, "Id", "Long"), FakeColumn(2, "Name", "Text"), FakeColumn(3, "Odd", "Bit")]
            self._tables.append(FakeTable("Big", 99, cols, big))

    monkeypatch.setattr("edb_explorer.core.backends.ese.EseDB", BigFakeEseDB)
    session = Session()
    db = session.open(fake_edb)
    try:
        tab = TableTab(db, db.table("Big"), memory_rows=500)
        loader = tab.loader
        assert loader is not None
        _pump(app, lambda: tab._finished)
        assert tab.model.disk and tab.model.rowCount() == 1_200 and tab.loaded == 1_200
        assert tab.view.model() is tab.model and "disk cache" in tab.status_label.text()
        assert [tab.model.display_text(r, 1) for r in (0, 499, 500, 1_199)] == [
            "item-00000",
            "item-00499",
            "item-00500",
            "item-01199",
        ]
        assert not any(tab.view.isColumnHidden(c) for c in range(3))
        # filter runs on disk in the background
        tab.filter_edit.setText("item-011")
        tab.apply_filter()
        _pump(app, lambda: tab._builder is None and tab.model.view_id is not None)
        assert tab.model.rowCount() == 100 and tab.model.raw_row(0)["Id"] == 1_100
        assert "100 match filter" in tab.status_label.text()
        # sort through the header, like a click
        tab.view.sortByColumn(0, Qt.SortOrder.DescendingOrder)
        _pump(app, lambda: tab._builder is None and tab.model.raw_row(0)["Id"] == 1_199)
        assert [tab.model.raw_row(r)["Id"] for r in range(3)] == [1_199, 1_198, 1_197]
        assert len(tab.current_rows()) == 100 and tab.current_rows()[0]["Name"] == "item-01199"
        assert tab.select_row_index(1_150) and tab.selected_rows()[0]["Id"] == 1_150
        tab.filter_edit.setText("")
        tab.apply_filter()
        _pump(app, lambda: tab._builder is None and tab.model.rowCount() == 1_200)
        assert tab.model.raw_row(0)["Id"] == 1_199  # still sorted descending
        store = tab.model.store
        assert store is not None
        tab.shutdown()
        assert store.closed
    finally:
        session.close_all()


def test_settings_dialog_budget_slider_and_field_stay_in_sync(app: QApplication, tmp_path: Any) -> None:
    from PySide6.QtCore import QSettings

    from edb_explorer.core import rowstore
    from edb_explorer.gui.widgets.settings_dialog import (
        DEFAULT_MEMORY_ROWS,
        MAX_MEMORY_ROWS,
        MIN_MEMORY_ROWS,
        SettingsDialog,
        memory_rows_setting,
    )

    settings = QSettings(str(tmp_path / "prefs.ini"), QSettings.Format.IniFormat)
    assert memory_rows_setting(settings) == DEFAULT_MEMORY_ROWS
    settings.setValue("memory_rows", "garbage")
    assert memory_rows_setting(settings) == DEFAULT_MEMORY_ROWS
    settings.setValue("memory_rows", 10**12)
    assert memory_rows_setting(settings) == MAX_MEMORY_ROWS
    settings.remove("memory_rows")

    dlg = SettingsDialog(settings)
    assert dlg.memory_rows == DEFAULT_MEMORY_ROWS and not dlg.reset_btn.isEnabled()
    dlg.slider.setValue(100)  # slider steps are 10,000 rows
    assert dlg.spin.value() == 1_000_000 and dlg.memory_rows == 1_000_000 and dlg.reset_btn.isEnabled()
    dlg.spin.setValue(123_456)
    assert dlg.slider.value() == 12 and dlg.memory_rows == 123_456 and "123,456 rows" in dlg.estimate.text()
    dlg.spin.setValue(0)
    assert dlg.memory_rows == MIN_MEMORY_ROWS
    dlg.reset_btn.click()
    assert dlg.memory_rows == DEFAULT_MEMORY_ROWS and dlg.spin.value() == DEFAULT_MEMORY_ROWS
    dlg.set_memory_rows(750_000)
    dlg.cache_edit.setText(str(tmp_path / "missing"))
    dlg.accept()
    assert dlg.result() != int(dlg.DialogCode.Accepted) and "not a directory" in dlg.cache_info.text()
    dlg.cache_edit.setText(str(tmp_path))
    dlg.accept()
    assert dlg.result() == int(dlg.DialogCode.Accepted)
    assert memory_rows_setting(settings) == 750_000 and settings.value("cache_dir") == str(tmp_path)
    assert str(tmp_path) == rowstore.DEFAULT_CACHE_DIR
    try:
        store = rowstore.DiskRowStore(COLUMNS)
        assert store.path.startswith(str(tmp_path))
        store.close()
    finally:
        rowstore.DEFAULT_CACHE_DIR = None


def test_shortcut_registry_overrides_conflicts_and_persistence(app: QApplication, tmp_path: Any) -> None:
    from PySide6.QtCore import QSettings
    from PySide6.QtGui import QAction, QKeySequence

    from edb_explorer.gui.shortcuts import ShortcutRegistry, key_text, plain_text, slug

    assert plain_text("&Open database(s)…") == "Open database(s)" and slug("&Open database(s)…") == "open_database_s"
    assert plain_text("&Preferences…  (rows kept in memory)") == "Preferences"
    settings = QSettings(str(tmp_path / "sc.ini"), QSettings.Format.IniFormat)
    settings.setValue("shortcuts/analysis.timeline", "Ctrl+Shift+T")  # a saved user override
    settings.setValue("shortcuts/file.quit", "")  # a saved "no shortcut"

    reg = ShortcutRegistry(settings)
    a_open = QAction("&Open database(s)…")
    a_open.setShortcut(QKeySequence("Ctrl+O"))
    e_open = reg.register(a_open, "&File")
    a_tl = QAction("&Timeline")
    a_tl.setShortcut(QKeySequence("Ctrl+L"))
    e_tl = reg.register(a_tl, "&Analysis")
    a_quit = QAction("&Quit")
    a_quit.setShortcut(QKeySequence("Ctrl+Shift+X"))
    e_quit = reg.register(a_quit, "&File")
    a_plain = QAction("Reset &layout")
    e_plain = reg.register(a_plain, "&View")

    assert (e_open.id, e_tl.id, e_quit.id, e_plain.id) == (
        "file.open_database_s",
        "analysis.timeline",
        "file.quit",
        "view.reset_layout",
    )
    assert reg.categories() == ["File", "Analysis", "View"]
    assert key_text(a_tl.shortcut()) == "Ctrl+Shift+T" and not e_tl.is_default and e_tl.default.toString() == "Ctrl+L"
    assert a_quit.shortcut().isEmpty() and not e_quit.is_default  # the empty override cleared the default
    assert e_open.is_default and key_text(a_open.shortcut()) == "Ctrl+O"

    heard: list[int] = []
    reg.changed.connect(lambda: heard.append(1))
    with pytest.raises(ValueError, match="Open database"):
        reg.assign(e_plain.id, QKeySequence("Ctrl+O"))
    assert not heard and key_text(a_plain.shortcut()) == ""
    taken = reg.assign(e_plain.id, QKeySequence("Ctrl+O"), steal=True)
    assert (
        [t.id for t in taken] == [e_open.id]
        and a_open.shortcut().isEmpty()
        and key_text(a_plain.shortcut()) == "Ctrl+O"
    )
    assert (
        settings.value("shortcuts/view.reset_layout") == "Ctrl+O"
        and settings.value("shortcuts/file.open_database_s") == ""
    )
    assert reg.conflicts(QKeySequence("Ctrl+O")) == [e_plain] and reg.conflicts(QKeySequence()) == []
    reg.reset(e_tl.id)
    assert e_tl.is_default and settings.value("shortcuts/analysis.timeline") is None
    assert reg.overrides() == {"file.open_database_s": "", "file.quit": "", "view.reset_layout": "Ctrl+O"}
    text = reg.as_text()
    assert "File\n  Open database(s)" in text and "Reset layout" in text and "Ctrl+O" in text
    reg.reset_all()
    assert all(e.is_default for e in reg.entries()) and reg.overrides() == {}
    assert len(heard) == 3  # steal, reset, reset_all
    assert settings.value("shortcuts/file.quit") is None and key_text(a_quit.shortcut()) == "Ctrl+Shift+X"


def test_shortcuts_dialog_assign_clear_reset(app: QApplication, tmp_path: Any) -> None:
    from PySide6.QtCore import QSettings
    from PySide6.QtGui import QAction, QKeySequence

    from edb_explorer.gui.shortcuts import ShortcutRegistry
    from edb_explorer.gui.widgets.shortcuts_dialog import ID_ROLE, ShortcutsDialog

    reg = ShortcutRegistry(QSettings(str(tmp_path / "sc.ini"), QSettings.Format.IniFormat))
    a, b = QAction("&Alpha"), QAction("&Beta")
    a.setShortcut(QKeySequence("Ctrl+1"))
    ea, eb = reg.register(a, "&Tools"), reg.register(b, "&Tools")
    dlg = ShortcutsDialog(reg)
    rows = {}
    for t in range(dlg.tree.topLevelItemCount()):
        top = dlg.tree.topLevelItem(t)
        for c in range(top.childCount()):
            rows[top.child(c).text(0)] = top.child(c)
    assert rows["Alpha"].data(0, ID_ROLE) == ea.id and rows["Alpha"].text(1) == "Ctrl+1" and rows["Beta"].text(1) == ""
    assert "Copy selected rows" in "".join(k for k in rows)  # built-ins are listed too
    assert not dlg.assign_btn.isEnabled()
    dlg.tree.setCurrentItem(rows["Beta"])
    assert dlg.assign_btn.isEnabled() and "Beta" in dlg.selected_label.text()
    dlg.key_edit.setKeySequence(QKeySequence("Ctrl+1"))
    dlg._preview_conflict()
    assert "already used by: Alpha" in dlg.conflict_label.text()
    dlg.key_edit.setKeySequence(QKeySequence("Ctrl+2"))
    dlg._assign()
    assert b.shortcut().toString() == "Ctrl+2" and rows["Beta"].text(1) == "Ctrl+2" and not eb.is_default
    dlg.tree.setCurrentItem(rows["Alpha"])
    dlg.clear_btn.click()
    assert a.shortcut().isEmpty() and rows["Alpha"].text(1) == "" and dlg.reset_btn.isEnabled()
    dlg.reset_btn.click()
    assert a.shortcut().toString() == "Ctrl+1" and not dlg.reset_btn.isEnabled()
    dlg.filter_edit.setText("beta")
    assert rows["Alpha"].isHidden() and not rows["Beta"].isHidden()
    dlg.filter_edit.setText("ctrl+2")
    assert not rows["Beta"].isHidden() and rows["Alpha"].isHidden()
