"""One tab = one table: filter bar, lazily loaded grid, context menu."""

from __future__ import annotations

import csv
import io
from typing import Any

from PySide6.QtCore import QModelIndex, QPoint, Qt, QTimer, Signal
from PySide6.QtGui import QAction, QKeySequence
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QCheckBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMenu,
    QProgressBar,
    QTableView,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from edb_explorer.core import EdbDatabase, TableInfo
from edb_explorer.core.values import display_value
from edb_explorer.gui.icons import std
from edb_explorer.gui.models import RAW_ROLE, RecordFilterProxy, RecordTableModel
from edb_explorer.gui.workers import RecordLoader


class TableTab(QWidget):
    row_selected = Signal(str, str, int, dict)  # db_id, table, row_index, raw row
    status = Signal(str)
    interpret_requested = Signal(object)  # value to send to the timestamp helper
    extract_requested = Signal(str)  # scope: "selection" | "table"

    def __init__(self, db: EdbDatabase, table: TableInfo, max_rows: int = 1_000_000, parent: Any = None) -> None:
        super().__init__(parent)
        self.db = db
        self.table = table
        self.max_rows = max_rows
        self.loader: RecordLoader | None = None
        self.loaded = 0
        self.user_hidden: set[int] = set()
        self._finished = False

        self.model = RecordTableModel(table.columns, self)
        self.proxy = RecordFilterProxy(self)
        self.proxy.setSourceModel(self.model)

        self._build_ui()
        self.start()

    # ------------------------------------------------------------------ #
    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(6, 6, 6, 6)
        layout.setSpacing(4)

        bar = QHBoxLayout()
        bar.setSpacing(6)
        self.filter_edit = QLineEdit()
        self.filter_edit.setPlaceholderText("Filter rows (substring, or regex with .* toggle)  —  Ctrl+F")
        self.filter_edit.setClearButtonEnabled(True)
        self._debounce = QTimer(self)
        self._debounce.setSingleShot(True)
        self._debounce.setInterval(250)
        self._debounce.timeout.connect(self.apply_filter)
        self.filter_edit.textChanged.connect(lambda _t: self._debounce.start())
        bar.addWidget(self.filter_edit, 1)

        self.regex_btn = QToolButton()
        self.regex_btn.setText(".*")
        self.regex_btn.setCheckable(True)
        self.regex_btn.setToolTip("Treat filter as a regular expression")
        self.regex_btn.toggled.connect(self.apply_filter)
        bar.addWidget(self.regex_btn)

        self.case_btn = QToolButton()
        self.case_btn.setText("Aa")
        self.case_btn.setCheckable(True)
        self.case_btn.setToolTip("Case sensitive")
        self.case_btn.toggled.connect(self.apply_filter)
        bar.addWidget(self.case_btn)

        self.hide_empty = QCheckBox("Hide empty columns")
        self.hide_empty.setChecked(True)
        self.hide_empty.setToolTip("Only show columns that contain at least one value in the loaded rows")
        self.hide_empty.toggled.connect(self._apply_column_visibility)
        bar.addWidget(self.hide_empty)

        self.columns_btn = QToolButton()
        self.columns_btn.setText("Columns")
        self.columns_btn.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        self.columns_btn.setToolTip("Choose visible columns")
        self.columns_menu = QMenu(self)
        self.columns_btn.setMenu(self.columns_menu)
        self.columns_menu.aboutToShow.connect(self._populate_columns_menu)
        bar.addWidget(self.columns_btn)

        self.stop_btn = QToolButton()
        self.stop_btn.setIcon(std("SP_BrowserStop"))
        self.stop_btn.setToolTip("Stop loading")
        self.stop_btn.clicked.connect(self.stop)
        bar.addWidget(self.stop_btn)

        self.reload_btn = QToolButton()
        self.reload_btn.setIcon(std("SP_BrowserReload"))
        self.reload_btn.setToolTip("Reload table")
        self.reload_btn.clicked.connect(self.reload)
        bar.addWidget(self.reload_btn)
        layout.addLayout(bar)

        self.view = QTableView()
        self.view.setModel(self.proxy)
        self.view.setSortingEnabled(True)
        self.view.setAlternatingRowColors(True)
        self.view.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.view.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.view.setWordWrap(False)
        self.view.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.view.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.view.customContextMenuRequested.connect(self._context_menu)
        hh = self.view.horizontalHeader()
        hh.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        hh.setDefaultSectionSize(150)
        hh.setStretchLastSection(False)
        hh.setSectionsMovable(True)
        hh.setSortIndicatorShown(True)
        hh.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        hh.customContextMenuRequested.connect(self._header_menu)
        vh = self.view.verticalHeader()
        vh.setSectionResizeMode(QHeaderView.ResizeMode.Fixed)
        vh.setDefaultSectionSize(24)
        vh.setMinimumWidth(48)
        self.view.selectionModel().currentRowChanged.connect(self._current_changed)
        self.view.setSortingEnabled(False)  # enabled once loading finishes (sorting mid-load is wasteful)
        layout.addWidget(self.view, 1)

        foot = QHBoxLayout()
        self.status_label = QLabel()
        self.status_label.setObjectName("dim")
        foot.addWidget(self.status_label, 1)
        self.progress = QProgressBar()
        self.progress.setRange(0, 0)
        self.progress.setMaximumWidth(160)
        self.progress.setTextVisible(False)
        foot.addWidget(self.progress)
        layout.addLayout(foot)

        copy = QAction(self)
        copy.setShortcut(QKeySequence.StandardKey.Copy)
        copy.setShortcutContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
        copy.triggered.connect(self.copy_selection)
        self.addAction(copy)

        # start with every column hidden; they are revealed as data arrives
        for c in range(self.model.columnCount()):
            self.view.setColumnHidden(c, True)

    # ------------------------------------------------------------------ #
    # Loading
    # ------------------------------------------------------------------ #
    def start(self) -> None:
        self._finished = False
        self.loaded = 0
        self.progress.setRange(0, 0)
        self.progress.show()
        self.stop_btn.setEnabled(True)
        self.loader = RecordLoader(self.db, self.table.name, self.max_rows, self)
        self.loader.chunk_ready.connect(self._on_chunk)
        self.loader.finished_ok.connect(self._on_finished)
        self.loader.failed.connect(self._on_failed)
        self.loader.start()
        self._update_status("Loading…")

    def stop(self) -> None:
        if self.loader and self.loader.isRunning():
            self.loader.cancel()
            self._update_status("Stopping…")

    def reload(self) -> None:
        self.stop()
        if self.loader:
            self.loader.wait(5000)
        self.view.setSortingEnabled(False)
        self.model.clear()
        for c in range(self.model.columnCount()):
            self.view.setColumnHidden(c, True)
        self.start()

    def shutdown(self) -> None:
        if self.loader:
            self.loader.cancel()
            self.loader.wait(10000)

    def _on_chunk(self, indices: list[int], rows: list[dict[str, Any]]) -> None:
        new_cols = self.model.append_rows(indices, rows)
        self.loaded += len(rows)
        if new_cols:
            self._reveal_columns(new_cols)
        self._update_status("Loading…")

    def _on_finished(self, total: int) -> None:
        self._finished = True
        self.progress.hide()
        self.stop_btn.setEnabled(False)
        cancelled = bool(self.loader and self.loader.cancelled)
        # QHeaderView defaults to a *descending* indicator on section 0, which
        # setSortingEnabled(True) would apply immediately - keep natural order.
        self.view.horizontalHeader().setSortIndicator(-1, Qt.SortOrder.AscendingOrder)
        self.view.setSortingEnabled(True)
        self.proxy.invalidate()
        self._update_status("Stopped" if cancelled else "Loaded" if total < self.max_rows else "Row limit reached")

    def _on_failed(self, message: str) -> None:
        self._finished = True
        self.progress.hide()
        self.stop_btn.setEnabled(False)
        self._update_status(f"Error: {message}")

    def _update_status(self, state: str) -> None:
        shown = self.proxy.rowCount()
        text = f"{state} · {self.loaded:,} rows loaded"
        if self.filter_edit.text():
            text += f" · {shown:,} match filter"
        visible = sum(1 for c in range(self.model.columnCount()) if not self.view.isColumnHidden(c))
        text += f" · {visible}/{self.model.columnCount()} columns"
        self.status_label.setText(text)
        self.status.emit(text)

    # ------------------------------------------------------------------ #
    # Columns
    # ------------------------------------------------------------------ #
    def _reveal_columns(self, cols: set[int]) -> None:
        if not self.hide_empty.isChecked():
            return
        for c in cols:
            if c not in self.user_hidden:
                self.view.setColumnHidden(c, False)
                self._autosize(c)

    def _autosize(self, c: int) -> None:
        """Size a column from its header and a sample of the first rows (capped so blobs don't explode)."""
        name = self.model.column_names[c]
        longest = len(name)
        for r in range(min(50, self.model.rowCount())):
            longest = max(longest, min(48, len(self.model.display_text(r, c))))
        self.view.setColumnWidth(c, max(80, min(420, 8 * longest + 24)))

    def _apply_column_visibility(self) -> None:
        hide_empty = self.hide_empty.isChecked()
        for c in range(self.model.columnCount()):
            hidden = c in self.user_hidden or (hide_empty and c not in self.model.seen_columns)
            if self.view.isColumnHidden(c) != hidden:
                self.view.setColumnHidden(c, hidden)
                if not hidden:
                    self._autosize(c)
        self._update_status("Loaded" if self._finished else "Loading…")

    def _populate_columns_menu(self) -> None:
        self.columns_menu.clear()
        show_all = self.columns_menu.addAction("Show all columns")
        show_all.triggered.connect(self._show_all_columns)
        self.columns_menu.addSeparator()
        cols = range(self.model.columnCount())
        if self.model.columnCount() > 60:
            hint = self.columns_menu.addAction(f"{self.model.columnCount()} columns - showing non-empty only")
            hint.setEnabled(False)
            cols = sorted(self.model.seen_columns)
        for c in cols:
            act = self.columns_menu.addAction(self.model.column_names[c])
            act.setCheckable(True)
            act.setChecked(not self.view.isColumnHidden(c))
            act.toggled.connect(lambda checked, col=c: self._toggle_column(col, checked))

    def _toggle_column(self, c: int, visible: bool) -> None:
        if visible:
            self.user_hidden.discard(c)
        else:
            self.user_hidden.add(c)
        self._apply_column_visibility()

    def _show_all_columns(self) -> None:
        self.user_hidden.clear()
        self.hide_empty.setChecked(False)
        self._apply_column_visibility()

    def visible_columns(self) -> list[str]:
        hh = self.view.horizontalHeader()
        order = sorted(range(self.model.columnCount()), key=hh.visualIndex)
        return [self.model.column_names[c] for c in order if not self.view.isColumnHidden(c)]

    # ------------------------------------------------------------------ #
    # Filtering / selection
    # ------------------------------------------------------------------ #
    def apply_filter(self) -> None:
        self.proxy.set_filter(self.filter_edit.text(), self.regex_btn.isChecked(), self.case_btn.isChecked())
        self._update_status("Loaded" if self._finished else "Loading…")

    def focus_filter(self) -> None:
        self.filter_edit.setFocus()
        self.filter_edit.selectAll()

    def _current_changed(self, current: QModelIndex, _previous: QModelIndex) -> None:
        if not current.isValid():
            return
        src = self.proxy.mapToSource(current)
        r = src.row()
        self.row_selected.emit(self.db.id, self.table.name, self.model.row_index(r), self.model.raw_row(r))

    def select_row_index(self, table_index: int, column: str | None = None) -> bool:
        r = self.model.model_row_for_index(table_index)
        if r is None:
            return False
        c = self.model.column_position(column) if column else 0
        if c is None or self.view.isColumnHidden(c):
            c = next((i for i in range(self.model.columnCount()) if not self.view.isColumnHidden(i)), 0)
        idx = self.proxy.mapFromSource(self.model.index(r, c))
        if not idx.isValid():
            return False
        self.view.setCurrentIndex(idx)
        self.view.scrollTo(idx, QAbstractItemView.ScrollHint.PositionAtCenter)
        return True

    def selected_rows(self) -> list[dict[str, Any]]:
        """Rows currently selected in the grid (visible columns only)."""
        sel = self.view.selectionModel()
        if not sel:
            return []
        cols = self.visible_columns()
        out = []
        for proxy_row in sorted({i.row() for i in sel.selectedIndexes()}):
            src = self.proxy.mapToSource(self.proxy.index(proxy_row, 0)).row()
            raw = self.model.raw_row(src)
            out.append({"_row": self.model.row_index(src), **{c: raw.get(c) for c in cols}})
        return out

    def current_rows(self) -> list[dict[str, Any]]:
        """Rows currently displayed (after filtering/sorting), decoded to display strings."""
        cols = self.visible_columns()
        out = []
        for r in range(self.proxy.rowCount()):
            src = self.proxy.mapToSource(self.proxy.index(r, 0)).row()
            raw = self.model.raw_row(src)
            out.append({"_row": self.model.row_index(src), **{c: raw.get(c) for c in cols}})
        return out

    # ------------------------------------------------------------------ #
    # Copy / context menus
    # ------------------------------------------------------------------ #
    def copy_selection(self) -> None:
        sel = self.view.selectionModel()
        if not sel or not sel.selectedIndexes():
            return
        rows = sorted({i.row() for i in sel.selectedIndexes()})
        cols = [c for c in range(self.model.columnCount()) if not self.view.isColumnHidden(c)]
        buf = io.StringIO()
        writer = csv.writer(buf, delimiter="\t", lineterminator="\n")
        writer.writerow([self.model.column_names[c] for c in cols])
        for r in rows:
            writer.writerow([self.proxy.index(r, c).data(Qt.ItemDataRole.DisplayRole) or "" for c in cols])
        QApplication.clipboard().setText(buf.getvalue())
        self.status.emit(f"Copied {len(rows)} row(s) to clipboard")

    def _context_menu(self, pos: QPoint) -> None:
        idx = self.view.indexAt(pos)
        menu = QMenu(self)
        if idx.isValid():
            raw = idx.data(RAW_ROLE)
            col = self.model.column_names[idx.column()]
            text = idx.data(Qt.ItemDataRole.DisplayRole) or ""
            menu.addAction(
                "Copy cell",
                lambda: QApplication.clipboard().setText(
                    display_value(raw, self.model.column_types.get(col), 10_000_000)
                ),
            )
            menu.addAction("Copy row(s) as TSV", self.copy_selection)
            menu.addAction(f"Copy column name  ({col})", lambda: QApplication.clipboard().setText(col))
            menu.addSeparator()
            menu.addAction(
                "Extract selected rows…  (xlsx / pdf / txt / json / csv)",
                lambda: self.extract_requested.emit("selection"),
            )
            menu.addAction("Extract whole table…", lambda: self.extract_requested.emit("table"))
            menu.addSeparator()
            if text:
                menu.addAction(f'Filter rows containing "{text[:30]}"', lambda: self.filter_edit.setText(text))
            if isinstance(raw, int | float | bytes) and not isinstance(raw, bool):
                menu.addAction("Interpret as timestamp…", lambda: self.interpret_requested.emit(raw))
            menu.addSeparator()
            menu.addAction(f"Hide column  ({col})", lambda: self._toggle_column(idx.column(), False))
        menu.addAction("Show all columns", self._show_all_columns)
        menu.exec(self.view.viewport().mapToGlobal(pos))

    def _header_menu(self, pos: QPoint) -> None:
        hh = self.view.horizontalHeader()
        c = hh.logicalIndexAt(pos)
        menu = QMenu(self)
        if c >= 0:
            name = self.model.column_names[c]
            menu.addAction(f"Hide column  ({name})", lambda: self._toggle_column(c, False))
            menu.addAction("Resize to contents", lambda: self.view.resizeColumnToContents(c))
            menu.addAction("Copy column name", lambda: QApplication.clipboard().setText(name))
            menu.addSeparator()
        menu.addAction("Resize all visible columns to contents", self._resize_all)
        menu.addAction("Show all columns", self._show_all_columns)
        menu.exec(hh.mapToGlobal(pos))

    def _resize_all(self) -> None:
        for c in range(self.model.columnCount()):
            if not self.view.isColumnHidden(c):
                self.view.resizeColumnToContents(c)
                self.view.setColumnWidth(c, min(self.view.columnWidth(c), 500))
