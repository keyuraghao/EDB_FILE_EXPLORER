"""One tab = one table: filter bar, lazily loaded grid, context menu."""

from __future__ import annotations

import csv
import io
from collections.abc import Sequence
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
from edb_explorer.core.rowstore import DiskRowStore, FilterSpec, SortSpec
from edb_explorer.core.values import display_value
from edb_explorer.gui.icons import icon
from edb_explorer.gui.models import RAW_ROLE, RecordFilterProxy, RecordTableModel
from edb_explorer.gui.workers import RecordLoader, ViewBuilder


class TableTab(QWidget):
    """Every row of the table is loaded: the first ``memory_rows`` stay in memory (sorted/filtered by the
    proxy), bigger tables spill to a :class:`DiskRowStore` and the grid then reads windows from disk."""

    row_selected = Signal(str, str, int, dict)  # db_id, table, row_index, raw row
    status = Signal(str)
    interpret_requested = Signal(object)  # value to send to the timestamp helper
    extract_requested = Signal(str)  # scope: "selection" | "table"

    def __init__(self, db: EdbDatabase, table: TableInfo, memory_rows: int = 250_000, parent: Any = None) -> None:
        super().__init__(parent)
        self.db = db
        self.table = table
        self.memory_rows = memory_rows
        self.loader: RecordLoader | None = None
        self.loaded = 0
        self.user_hidden: set[int] = set()
        self._finished = False
        self._builder: ViewBuilder | None = None
        self._filter_column: int | None = None  # header menu "Filter in this column only"
        self._old_builders: list[ViewBuilder] = []  # cancelled builds still winding down
        self._sort: SortSpec | None = None
        self._view_stale = False  # rows arrived after the current disk view was built

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
        clear_act = QAction(self.filter_edit)
        clear_act.setShortcut(QKeySequence(Qt.Key.Key_Escape))
        clear_act.setShortcutContext(Qt.ShortcutContext.WidgetShortcut)
        clear_act.triggered.connect(lambda: (self.filter_edit.clear(), self.view.setFocus()))
        self.filter_edit.addAction(clear_act)
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

        self.column_scope = QToolButton()
        self.column_scope.setText("all columns")
        self.column_scope.setToolTip("Which column the filter searches (right-click a header to pick one)")
        self.column_scope.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        self.scope_menu = QMenu(self)
        self.column_scope.setMenu(self.scope_menu)
        self.scope_menu.aboutToShow.connect(self._populate_scope_menu)
        bar.addWidget(self.column_scope)

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
        self.stop_btn.setIcon(icon("stop"))
        self.stop_btn.setToolTip("Stop loading")
        self.stop_btn.clicked.connect(self.stop)
        bar.addWidget(self.stop_btn)

        self.reload_btn = QToolButton()
        self.reload_btn.setIcon(icon("reload"))
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
        self.view.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.view.setHorizontalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
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
        self.model.sort_requested.connect(self._sort_requested)
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
        goto = QAction(self)
        goto.setShortcut(QKeySequence("Ctrl+G"))
        goto.setShortcutContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
        goto.triggered.connect(self.goto_row)
        self.addAction(goto)
        reload = QAction(self)
        reload.setShortcut(QKeySequence(Qt.Key.Key_F5))
        reload.setShortcutContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
        reload.triggered.connect(self.reload)
        self.addAction(reload)

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
        self.loader = RecordLoader(self.db, self.table.name, self.memory_rows, self)
        self.loader.chunk_ready.connect(self._on_chunk)
        self.loader.spilled.connect(self._on_spilled)
        self.loader.store_grew.connect(self._on_store_grew)
        self.loader.finished_ok.connect(self._on_finished)
        self.loader.failed.connect(self._on_failed)
        self.loader.start()
        self._update_status("Loading…")

    def stop(self) -> None:
        if self.loader and self.loader.isRunning():
            self.loader.cancel()
            self._update_status("Stopping…")
        self._cancel_builder()

    def reload(self) -> None:
        self.stop()
        self._cancel_builder(wait=True)
        if self.loader:
            self.loader.wait(5000)
        self.view.setSortingEnabled(False)
        self._sort = None
        self._view_stale = False
        if self.model.disk:
            self.model.clear()  # also closes the store
            self.proxy.setSourceModel(self.model)
            self.view.setModel(self.proxy)
            self.view.selectionModel().currentRowChanged.connect(self._current_changed)
        else:
            self.model.clear()
        self.proxy.set_filter("")
        for c in range(self.model.columnCount()):
            self.view.setColumnHidden(c, True)
        self.start()

    def shutdown(self) -> None:
        self._cancel_builder(wait=True)
        if self.loader:
            self.loader.cancel()
            self.loader.wait(10000)
        if self.model.store is not None:
            self.model.store.close()

    def _on_chunk(self, indices: list[int], rows: list[dict[str, Any]]) -> None:
        new_cols = self.model.append_rows(indices, rows)
        self.loaded += len(rows)
        if new_cols:
            self._reveal_columns(new_cols)
        self._update_status("Loading…")

    def _on_spilled(self, store: DiskRowStore) -> None:
        """The table outgrew memory: the grid now reads from the disk store (the proxy steps aside)."""
        hh = self.view.horizontalHeader()
        widths = [self.view.columnWidth(c) for c in range(self.model.columnCount())]
        hidden = [self.view.isColumnHidden(c) for c in range(self.model.columnCount())]
        self.proxy.set_filter("")
        # Detach the proxy entirely: as a consumer it would re-sort/re-filter every row on each model
        # reset or insert (it keeps the sort column from the header's initial state), and that is
        # exactly the O(n) Python work disk mode exists to avoid.
        self.proxy.setSourceModel(None)
        self.model.attach_store(store)
        self.view.setModel(self.model)
        self.view.selectionModel().currentRowChanged.connect(self._current_changed)
        for c, (w, h) in enumerate(zip(widths, hidden, strict=True)):
            self.view.setColumnHidden(c, h)
            if not h:
                self.view.setColumnWidth(c, w)
        hh.setSortIndicator(-1, Qt.SortOrder.AscendingOrder)
        self.loaded = store.count
        # columns first populated by rows that reached the store before the model switched over
        self._reveal_columns({c for c in self.model.seen_sorted if hidden[c] and c not in self.user_hidden})
        if self.filter_edit.text():
            self._request_view()
        self._update_status("Loading…")

    def _on_store_grew(self, total: int, new_cols: list[int]) -> None:
        fresh = self.model.store_grew(total, set(new_cols))
        self.loaded = total
        if fresh:
            self._reveal_columns(fresh)
        if self.model.view_id is not None:
            self._view_stale = True
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
        if self.model.disk:
            self.loaded = self.model.store.count if self.model.store else total
            if self._view_stale or (self.filter_edit.text() and self.model.view_id is None):
                self._request_view()  # the disk view was built while rows were still arriving
        else:
            self.proxy.invalidate()
        self._update_status("Stopped" if cancelled else "Loaded")

    def _on_failed(self, message: str) -> None:
        self._finished = True
        self.progress.hide()
        self.stop_btn.setEnabled(False)
        self._update_status(f"Error: {message}")

    def _update_status(self, state: str) -> None:
        text = f"{state} · {self.loaded:,} rows loaded"
        if self.model.disk:
            text += " (disk cache)"
        if self.filter_edit.text():
            shown = self.model.rowCount() if self.model.disk else self.proxy.rowCount()
            text += f" · {shown:,} match filter"
            if self._view_stale:
                text += " (so far)"
        visible = sum(1 for c in range(self.model.columnCount()) if not self.view.isColumnHidden(c))
        text += f" · {visible}/{self.model.columnCount()} columns"
        self.status_label.setText(text)
        self.status.emit(text)

    # ------------------------------------------------------------------ #
    # Disk views (filter / sort once the table lives in the store)
    # ------------------------------------------------------------------ #
    def _request_view(self) -> None:
        """(Re)build the disk view for the current filter + sort in the background."""
        store = self.model.store
        if store is None:
            return
        self._cancel_builder()
        text = self.filter_edit.text()
        spec = (
            FilterSpec(text, self.regex_btn.isChecked(), self.case_btn.isChecked(), self._filter_column)
            if text
            else None
        )
        self._view_stale = False
        if spec is None and self._sort is None:
            self.model.set_view(None)
            self._update_status("Loaded" if self._finished else "Loading…")
            return
        self._builder = ViewBuilder(store, spec, self._sort, self)
        self._builder.done.connect(self._on_view_built)
        self._builder.failed.connect(lambda m: self._update_status(f"Filter failed: {m}"))
        self._builder.start()
        self.progress.setRange(0, 0)
        self.progress.show()
        self._update_status("Sorting…" if spec is None else "Filtering…")

    def _on_view_built(self, view_id: int) -> None:
        if self.sender() is not self._builder:  # superseded by a newer request
            if self.model.store is not None:
                self.model.store.drop_view(view_id)
            return
        self._builder = None
        self.model.set_view(view_id)
        if self._finished:
            self.progress.hide()
        self._update_status("Loaded" if self._finished else "Loading…")

    def _cancel_builder(self, wait: bool = False) -> None:
        builder, self._builder = self._builder, None
        if builder is not None:
            builder.cancel()
            self._old_builders.append(builder)
        if wait:
            for b in self._old_builders:
                b.wait(10000)
        self._old_builders = [b for b in self._old_builders if b.isRunning()]

    def _sort_requested(self, column: int, descending: bool) -> None:
        self._sort = SortSpec(column, descending) if column >= 0 else None
        self._request_view()

    def _source_row(self, view_row: int) -> int:
        """Model row behind a grid row (identity in disk mode, proxy mapping in memory mode)."""
        if self.model.disk:
            return view_row
        return self.proxy.mapToSource(self.proxy.index(view_row, 0)).row()

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
        if self.model.disk:
            self._request_view()
            return
        self.proxy.set_filter(
            self.filter_edit.text(), self.regex_btn.isChecked(), self.case_btn.isChecked(), self._filter_column
        )
        self._update_status("Loaded" if self._finished else "Loading…")

    def set_filter_column(self, column: int | None) -> None:
        """Restrict the filter box to one column (``None`` = every visible column)."""
        self._filter_column = column
        self.column_scope.setText(self.model.column_names[column] if column is not None else "all columns")
        self.apply_filter()

    def _populate_scope_menu(self) -> None:
        self.scope_menu.clear()
        act = self.scope_menu.addAction("All columns")
        act.setCheckable(True)
        act.setChecked(self._filter_column is None)
        act.triggered.connect(lambda: self.set_filter_column(None))
        self.scope_menu.addSeparator()
        for c in self.model.seen_sorted:
            a = self.scope_menu.addAction(self.model.column_names[c])
            a.setCheckable(True)
            a.setChecked(c == self._filter_column)
            a.triggered.connect(lambda _checked=False, col=c: self.set_filter_column(col))

    def goto_row(self) -> None:
        """Ctrl+G: jump to a row by its table index (the number in the row header)."""
        from PySide6.QtWidgets import QInputDialog

        last = self.model.rowCount()
        if not last:
            return
        current = self.view.currentIndex()
        start = self.model.row_index(self._source_row(current.row())) if current.isValid() else 0
        value, ok = QInputDialog.getInt(
            self, "Go to row", "Row index (as shown in the row header):", start, 0, 2**31 - 1
        )
        if ok and not self.select_row_index(value):
            self.status.emit(f"Row {value} is not loaded (or filtered out)")

    def focus_filter(self) -> None:
        self.filter_edit.setFocus()
        self.filter_edit.selectAll()

    def _current_changed(self, current: QModelIndex, _previous: QModelIndex) -> None:
        if not current.isValid():
            return
        r = self._source_row(current.row())
        self.row_selected.emit(self.db.id, self.table.name, self.model.row_index(r), self.model.raw_row(r))

    def select_row_index(self, table_index: int, column: str | None = None) -> bool:
        r = self.model.model_row_for_index(table_index)
        if r is None:
            return False
        c = self.model.column_position(column) if column else 0
        if c is None or self.view.isColumnHidden(c):
            c = next((i for i in range(self.model.columnCount()) if not self.view.isColumnHidden(i)), 0)
        idx = self.model.index(r, c) if self.model.disk else self.proxy.mapFromSource(self.model.index(r, c))
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
        for view_row in sorted({i.row() for i in sel.selectedIndexes()}):
            src = self._source_row(view_row)
            raw = self.model.raw_row(src)
            out.append({"_row": self.model.row_index(src), **{c: raw.get(c) for c in cols}})
        return out

    def current_rows(self) -> Sequence[dict[str, Any]]:
        """Rows currently displayed (after filtering/sorting); streamed lazily from disk in disk mode."""
        cols = self.visible_columns()
        if self.model.disk:
            return self.model.store_rows(cols)
        out = []
        for r in range(self.proxy.rowCount()):
            src = self._source_row(r)
            raw = self.model.raw_row(src)
            out.append({"_row": self.model.row_index(src), **{c: raw.get(c) for c in cols}})
        return out

    # ------------------------------------------------------------------ #
    # Copy / context menus
    # ------------------------------------------------------------------ #
    def copy_selection(self, fmt: str = "tsv") -> None:
        """Copy the selected rows (visible columns) as TSV, JSON or a Markdown table."""
        sel = self.view.selectionModel()
        if not sel or not sel.selectedIndexes():
            return
        rows = sorted({i.row() for i in sel.selectedIndexes()})
        cols = [c for c in range(self.model.columnCount()) if not self.view.isColumnHidden(c)]
        names = [self.model.column_names[c] for c in cols]
        grid = self.view.model()
        cells = [[grid.index(r, c).data(Qt.ItemDataRole.DisplayRole) or "" for c in cols] for r in rows]
        if fmt == "json":
            import json

            records = []
            for r in rows:
                raw = self.model.raw_row(self._source_row(r))
                records.append(
                    {
                        "_row": self.model.row_index(self._source_row(r)),
                        **{n: display_value(raw.get(n), self.model.column_types.get(n), 10_000_000) for n in names},
                    }
                )
            text = json.dumps(records, indent=2, ensure_ascii=False)
        elif fmt == "markdown":
            widths = [max(len(n), *(len(row[i]) for row in cells)) for i, n in enumerate(names)]

            def line(vals: list[str]) -> str:
                return (
                    "| " + " | ".join(v.replace("|", "\\|").ljust(w) for v, w in zip(vals, widths, strict=True)) + " |"
                )

            text = "\n".join(
                [line(names), "|" + "|".join("-" * (w + 2) for w in widths) + "|", *(line(row) for row in cells)]
            )
        else:
            buf = io.StringIO()
            writer = csv.writer(buf, delimiter="\t", lineterminator="\n")
            writer.writerow(names)
            writer.writerows(cells)
            text = buf.getvalue()
        QApplication.clipboard().setText(text)
        self.status.emit(f"Copied {len(rows)} row(s) as {fmt.upper() if fmt != 'markdown' else 'Markdown'}")

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
            menu.addAction("Copy row(s) as JSON", lambda: self.copy_selection("json"))
            menu.addAction("Copy row(s) as Markdown table", lambda: self.copy_selection("markdown"))
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
            menu.addAction(f"Filter in this column only  ({name})", lambda: self.set_filter_column(c))
            if self._filter_column is not None:
                menu.addAction("Filter in all columns", lambda: self.set_filter_column(None))
            menu.addAction("Sort ascending", lambda: self.view.sortByColumn(c, Qt.SortOrder.AscendingOrder))
            menu.addAction("Sort descending", lambda: self.view.sortByColumn(c, Qt.SortOrder.DescendingOrder))
            menu.addSeparator()
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
