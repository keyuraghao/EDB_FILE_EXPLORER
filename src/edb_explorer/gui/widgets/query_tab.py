"""SQL console / artifact view / timeline results tab."""

from __future__ import annotations

import csv
import io
from datetime import datetime, timezone
from typing import Any

from PySide6.QtCore import QDate, QSettings, Qt, Signal
from PySide6.QtGui import QAction, QFont, QKeySequence
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QCheckBox,
    QComboBox,
    QDateEdit,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMenu,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QSpinBox,
    QSplitter,
    QTableView,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from edb_explorer.core import ColumnInfo, Session
from edb_explorer.core.analysis import build_timeline
from edb_explorer.core.sqlworkspace import QueryResult, SqlWorkspace
from edb_explorer.gui.icons import icon
from edb_explorer.gui.models import RAW_ROLE, RecordFilterProxy, RecordTableModel
from edb_explorer.gui.tasks import TaskManager
from edb_explorer.gui.workers import FunctionWorker


def _mono() -> QFont:
    f = QFont("Monospace")
    f.setStyleHint(QFont.StyleHint.Monospace)
    return f


def _columns(names: list[str]) -> tuple[ColumnInfo, ...]:
    return tuple(ColumnInfo(i + 1, n, "ANY", 0, "dynamic", None, None, False, False) for i, n in enumerate(names))


class ResultsGrid(QWidget):
    """A filterable grid over an in-memory result set (shared by SQL, views and timeline)."""

    row_selected = Signal(dict)  # raw row dict
    row_activated = Signal(dict)
    status = Signal(str)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.model = RecordTableModel((), self)
        self.proxy = RecordFilterProxy(self)
        self.proxy.setSourceModel(self.model)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        bar = QHBoxLayout()
        self.filter_edit = QLineEdit()
        self.filter_edit.setPlaceholderText("Filter results")
        self.filter_edit.setClearButtonEnabled(True)
        self.filter_edit.textChanged.connect(lambda t: (self.proxy.set_filter(t), self._status()))
        bar.addWidget(self.filter_edit, 1)
        self.count_label = QLabel("")
        self.count_label.setObjectName("dim")
        bar.addWidget(self.count_label)
        copy = QToolButton()
        copy.setText("Copy")
        copy.setToolTip("Copy selected rows as TSV")
        copy.clicked.connect(self.copy_selection)
        bar.addWidget(copy)
        layout.addLayout(bar)
        self.view = QTableView()
        self.view.setModel(self.proxy)
        self.view.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.view.setHorizontalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.view.setAlternatingRowColors(True)
        self.view.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.view.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.view.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.view.setWordWrap(False)
        self.view.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        self.view.horizontalHeader().setDefaultSectionSize(160)
        self.view.verticalHeader().setDefaultSectionSize(24)
        self.view.setSortingEnabled(True)
        self.view.horizontalHeader().setSortIndicator(-1, Qt.SortOrder.AscendingOrder)
        self.view.doubleClicked.connect(lambda idx: self.row_activated.emit(self._raw(idx)))
        self.view.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.view.customContextMenuRequested.connect(self._menu)
        layout.addWidget(self.view, 1)
        self.view.selectionModel().currentRowChanged.connect(
            lambda cur, _p: self.row_selected.emit(self._raw(cur)) if cur.isValid() else None
        )
        act = QAction(self)
        act.setShortcut(QKeySequence.StandardKey.Copy)
        act.setShortcutContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
        act.triggered.connect(self.copy_selection)
        self.addAction(act)

    def _raw(self, idx: Any) -> dict[str, Any]:
        src = self.proxy.mapToSource(idx)
        return self.model.raw_row(src.row()) if src.isValid() else {}

    def set_result(self, columns: list[str], rows: list[dict[str, Any]]) -> None:
        self.view.setSortingEnabled(False)
        self.model = RecordTableModel(_columns(columns), self)
        self.proxy.setSourceModel(self.model)
        self.model.append_rows(list(range(len(rows))), rows)
        self.view.setSortingEnabled(True)
        self.view.horizontalHeader().setSortIndicator(-1, Qt.SortOrder.AscendingOrder)
        for c in range(len(columns)):
            longest = len(columns[c])
            for r in range(min(40, len(rows))):
                longest = max(longest, min(48, len(self.model.display_text(r, c))))
            self.view.setColumnWidth(c, max(80, min(420, 8 * longest + 24)))
        self.view.selectionModel().currentRowChanged.connect(
            lambda cur, _p: self.row_selected.emit(self._raw(cur)) if cur.isValid() else None
        )
        self._status()

    def _status(self) -> None:
        shown = self.proxy.rowCount()
        total = self.model.rowCount()
        self.count_label.setText(f"{shown:,} of {total:,} rows" if shown != total else f"{total:,} rows")

    def rows(self) -> list[dict[str, Any]]:
        return [self._raw(self.proxy.index(r, 0)) for r in range(self.proxy.rowCount())]

    def selected_rows(self) -> list[dict[str, Any]]:
        sel = self.view.selectionModel()
        rows = sorted({i.row() for i in sel.selectedIndexes()}) if sel else []
        return [self._raw(self.proxy.index(r, 0)) for r in rows]

    def columns(self) -> list[str]:
        return list(self.model.column_names)

    def copy_selection(self) -> None:
        rows = self.selected_rows() or self.rows()
        buf = io.StringIO()
        w = csv.writer(buf, delimiter="\t", lineterminator="\n")
        cols = self.columns()
        w.writerow(cols)
        from edb_explorer.core.values import display_value

        for r in rows:
            w.writerow([display_value(r.get(c), None, 10_000) for c in cols])
        QApplication.clipboard().setText(buf.getvalue())
        self.status.emit(f"Copied {len(rows)} row(s)")

    def _menu(self, pos: Any) -> None:
        idx = self.view.indexAt(pos)
        menu = QMenu(self)
        if idx.isValid():
            text = idx.data(Qt.ItemDataRole.DisplayRole) or ""
            raw = idx.data(RAW_ROLE)
            from edb_explorer.core.values import display_value

            menu.addAction("Copy cell", lambda: QApplication.clipboard().setText(display_value(raw, None, 10_000_000)))
            if text:
                menu.addAction(f'Filter "{text[:30]}"', lambda: self.filter_edit.setText(text))
        menu.addAction("Copy rows as TSV", self.copy_selection)
        menu.exec(self.view.viewport().mapToGlobal(pos))


class QueryTab(QWidget):
    """SQL console over the workspace (all open databases attached as schemas)."""

    status = Signal(str)
    row_selected = Signal(str, str, int, dict)  # db_id, table, row_index, raw row (for the inspector)
    extract_requested = Signal(object)  # ResultsGrid

    def __init__(
        self,
        session: Session,
        workspace: SqlWorkspace,
        tasks: TaskManager,
        parent: QWidget | None = None,
        initial_sql: str = "",
        db_id: str | None = None,
        title: str = "SQL",
    ) -> None:
        super().__init__(parent)
        self.session = session
        self.workspace = workspace
        self.tasks = tasks
        self.title = title
        self._worker: FunctionWorker | None = None
        layout = QVBoxLayout(self)
        layout.setContentsMargins(6, 6, 6, 6)
        bar = QHBoxLayout()
        bar.addWidget(QLabel("Database:"))
        self.db_combo = QComboBox()
        self.db_combo.setToolTip(
            'Tables of this database can be referenced by their plain name; others as "schema"."table"'
        )
        bar.addWidget(self.db_combo)
        self.limit = QSpinBox()
        self.limit.setRange(1, 5_000_000)
        self.limit.setValue(10_000)
        self.limit.setPrefix("limit ")
        bar.addWidget(self.limit)
        self.run_btn = QPushButton("Run  (Ctrl+Enter)")
        self.run_btn.setIcon(icon("play"))
        self.run_btn.clicked.connect(self.run)
        bar.addWidget(self.run_btn)
        self.stop_btn = QPushButton("Stop")
        self.stop_btn.setEnabled(False)
        self.stop_btn.clicked.connect(self.stop)
        bar.addWidget(self.stop_btn)
        self.tables_btn = QToolButton()
        self.tables_btn.setText("Tables ▾")
        self.tables_btn.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        self.tables_menu = QMenu(self)
        self.tables_btn.setMenu(self.tables_menu)
        self.tables_menu.aboutToShow.connect(self._fill_tables_menu)
        bar.addWidget(self.tables_btn)
        self.extract_btn = QPushButton("Extract results…")
        self.extract_btn.setIcon(icon("extract"))
        self.extract_btn.clicked.connect(lambda: self.extract_requested.emit(self.grid))
        bar.addWidget(self.extract_btn)
        self.history_btn = QToolButton()
        self.history_btn.setText("History ▾")
        self.history_btn.setToolTip("Queries you ran before (kept across sessions)")
        self.history_btn.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        self.history_menu = QMenu(self)
        self.history_btn.setMenu(self.history_menu)
        self.history_menu.aboutToShow.connect(self._fill_history_menu)
        bar.addWidget(self.history_btn)
        bar.addStretch(1)
        layout.addLayout(bar)

        split = QSplitter(Qt.Orientation.Vertical)
        self.editor = QPlainTextEdit()
        self.editor.setFont(_mono())
        self.editor.setObjectName("mono")
        self.editor.setPlaceholderText(
            'SELECT * FROM "TableName" LIMIT 100\n-- tables from other open databases: "schema"."table"  (see Tables ▾)'
        )
        self.editor.setPlainText(initial_sql)
        self.editor.setMaximumHeight(220)
        split.addWidget(self.editor)
        self.grid = ResultsGrid()
        self.grid.row_selected.connect(self._row_selected)
        self.grid.status.connect(self.status)
        split.addWidget(self.grid)
        split.setStretchFactor(1, 1)
        layout.addWidget(split, 1)
        foot = QHBoxLayout()
        self.info = QLabel("")
        self.info.setObjectName("dim")
        foot.addWidget(self.info, 1)
        self.progress = QProgressBar()
        self.progress.setRange(0, 0)
        self.progress.setMaximumWidth(160)
        self.progress.hide()
        foot.addWidget(self.progress)
        layout.addLayout(foot)
        run_act = QAction(self)
        run_act.setShortcut(QKeySequence("Ctrl+Return"))
        run_act.setShortcutContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
        run_act.triggered.connect(self.run)
        self.addAction(run_act)
        self.refresh_databases(db_id)

    def refresh_databases(self, select: str | None = None) -> None:
        current = select or self.db_combo.currentData()
        self.db_combo.clear()
        for db in self.session:
            self.db_combo.addItem(f"{db.path.name}  ({db.info.kind_name})", db.id)
        i = self.db_combo.findData(current)
        if i >= 0:
            self.db_combo.setCurrentIndex(i)

    def _fill_tables_menu(self) -> None:
        self.tables_menu.clear()
        for db in self.session:
            schema = self.workspace.schema_for(db)
            sub = self.tables_menu.addMenu(f"{db.path.name}   [{schema}]")
            for t in db.tables(include_system=False)[:200]:
                qn = f'"{schema}"."{t.name}"'
                sub.addAction(t.display_name, lambda q=qn: self.editor.insertPlainText(q))

    def run(self) -> None:
        sql = self.editor.textCursor().selectedText().replace(" ", "\n").strip() or self.editor.toPlainText().strip()
        if not sql or (self._worker and self._worker.isRunning()):
            return
        db_id = self.db_combo.currentData()
        db = self.session.get(db_id) if db_id else None
        limit = self.limit.value()
        task = self.tasks.start(f"SQL query ({self.title})", cancel=self.stop, detail=sql[:80])

        def job(progress: Any, should_stop: Any) -> QueryResult:
            def cb(name: str, n: int) -> bool:
                progress(f"materialising {name}: {n:,} rows")
                return not should_stop()

            if db is not None:
                self.workspace.attach(db)
                resolved = self.workspace.resolve_placeholders(db, sql)
                low = resolved.lower()
                for t in db.tables(include_system=True):
                    if t.name.lower() in low:
                        self.workspace.ensure_table(db, t.name, cb)
                return self.workspace.query(resolved, limit)
            return self.workspace.query(sql, limit)

        self._worker = FunctionWorker(job, parent=self)
        self._worker.progress.connect(lambda m: (self.info.setText(str(m)), task.progress(detail=str(m))))
        self._worker.result.connect(lambda r: (self._done(r), task.finish(f"{len(r.rows):,} rows")))
        self._worker.failed.connect(lambda m: (self._failed(m), task.finish(m, failed=True)))
        self.run_btn.setEnabled(False)
        self.stop_btn.setEnabled(True)
        self.progress.show()
        self.info.setText("Running…")
        self._worker.start()

    def stop(self) -> None:
        if self._worker:
            self._worker.cancel()

    # ---- history -------------------------------------------------------- #
    HISTORY_SIZE = 30

    def _history(self) -> list[str]:
        val = QSettings().value("sql_history", [])
        items = val if isinstance(val, list) else [val] if val else []
        return [str(x) for x in items]

    def _remember(self, sql: str) -> None:
        sql = sql.strip()
        if not sql:
            return
        items = [q for q in self._history() if q != sql]
        items.insert(0, sql)
        QSettings().setValue("sql_history", items[: self.HISTORY_SIZE])

    def _fill_history_menu(self) -> None:
        self.history_menu.clear()
        items = self._history()
        if not items:
            self.history_menu.addAction("(no queries yet)").setEnabled(False)
            return
        for q in items:
            label = " ".join(q.split())
            act = self.history_menu.addAction(label if len(label) <= 90 else label[:88] + "…")
            act.setToolTip(q)
            act.triggered.connect(lambda _c=False, sql=q: self.editor.setPlainText(sql))
        self.history_menu.addSeparator()
        self.history_menu.addAction("Clear history", lambda: QSettings().setValue("sql_history", []))

    def _done(self, result: QueryResult) -> None:
        self.run_btn.setEnabled(True)
        self.stop_btn.setEnabled(False)
        self.progress.hide()
        self._remember(result.sql if self.title == "SQL" else "")
        rows = [dict(zip(result.columns, r, strict=False)) for r in result.rows]
        self.grid.set_result(result.columns, rows)
        note = " (truncated - raise the limit to see more)" if result.truncated else ""
        self.info.setText(f"{len(rows):,} row(s) in {result.elapsed:.2f}s{note}")
        self.status.emit(self.info.text())

    def _failed(self, message: str) -> None:
        self.run_btn.setEnabled(True)
        self.stop_btn.setEnabled(False)
        self.progress.hide()
        self.info.setText(f"Error: {message}")
        self.status.emit(f"SQL error: {message}")

    def _row_selected(self, row: dict[str, Any]) -> None:
        db_id = self.db_combo.currentData() or ""
        self.row_selected.emit(
            db_id, self.title, int(row.get("_row", -1)) if isinstance(row.get("_row"), int) else -1, row
        )

    def set_sql(self, sql: str, run: bool = True) -> None:
        self.editor.setPlainText(sql)
        if run:
            self.run()

    def shutdown(self) -> None:
        self.stop()
        if self._worker:
            self._worker.wait(3000)


class TimelineTab(QWidget):
    """Cross-database timeline built from every detected timestamp column."""

    status = Signal(str)
    jump_requested = Signal(str, str, int)  # db_id, table, row
    extract_requested = Signal(object)

    def __init__(self, session: Session, tasks: TaskManager, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.session = session
        self.tasks = tasks
        self._worker: FunctionWorker | None = None
        layout = QVBoxLayout(self)
        layout.setContentsMargins(6, 6, 6, 6)
        bar = QHBoxLayout()
        bar.addWidget(QLabel("Databases:"))
        self.scope = QComboBox()
        bar.addWidget(self.scope)
        self.use_range = QCheckBox("Between")
        bar.addWidget(self.use_range)
        self.start = QDateEdit(QDate.currentDate().addYears(-2))
        self.start.setCalendarPopup(True)
        self.end = QDateEdit(QDate.currentDate().addDays(1))
        self.end.setCalendarPopup(True)
        bar.addWidget(self.start)
        bar.addWidget(QLabel("and"))
        bar.addWidget(self.end)
        self.limit = QSpinBox()
        self.limit.setRange(1000, 5_000_000)
        self.limit.setValue(200_000)
        self.limit.setPrefix("max events ")
        bar.addWidget(self.limit)
        self.system = QCheckBox("System tables")
        bar.addWidget(self.system)
        self.build_btn = QPushButton("Build timeline")
        self.build_btn.setIcon(icon("timeline"))
        self.build_btn.clicked.connect(self.build)
        bar.addWidget(self.build_btn)
        self.stop_btn = QPushButton("Stop")
        self.stop_btn.setEnabled(False)
        self.stop_btn.clicked.connect(self.stop)
        bar.addWidget(self.stop_btn)
        self.extract_btn = QPushButton("Extract…")
        self.extract_btn.clicked.connect(lambda: self.extract_requested.emit(self.grid))
        bar.addWidget(self.extract_btn)
        bar.addStretch(1)
        layout.addLayout(bar)
        self.grid = ResultsGrid()
        self.grid.row_activated.connect(self._activated)
        self.grid.status.connect(self.status)
        layout.addWidget(self.grid, 1)
        foot = QHBoxLayout()
        self.info = QLabel(
            "Every row of every table that has a timestamp column becomes an event. Double-click to jump to the record."
        )
        self.info.setObjectName("dim")
        foot.addWidget(self.info, 1)
        self.progress = QProgressBar()
        self.progress.setRange(0, 0)
        self.progress.setMaximumWidth(160)
        self.progress.hide()
        foot.addWidget(self.progress)
        layout.addLayout(foot)
        self.refresh_databases()

    def refresh_databases(self) -> None:
        current = self.scope.currentData()
        self.scope.clear()
        self.scope.addItem("All open databases", "*")
        for db in self.session:
            self.scope.addItem(db.path.name, db.id)
        i = self.scope.findData(current)
        if i >= 0:
            self.scope.setCurrentIndex(i)

    def build(self) -> None:
        if self._worker and self._worker.isRunning():
            return
        scope = self.scope.currentData()
        dbs = list(self.session) if scope == "*" else [self.session.get(scope)]
        if not dbs:
            self.info.setText("Open a database first.")
            return
        start = end = None
        if self.use_range.isChecked():
            start = datetime.combine(self.start.date().toPython(), datetime.min.time(), tzinfo=timezone.utc)
            end = datetime.combine(self.end.date().toPython(), datetime.max.time(), tzinfo=timezone.utc)
        limit, system = self.limit.value(), self.system.isChecked()
        task = self.tasks.start("Building timeline", cancel=self.stop, detail=", ".join(d.path.name for d in dbs)[:80])

        def job(progress: Any, should_stop: Any) -> tuple[list[Any], bool]:
            return build_timeline(
                dbs,
                None,
                start,
                end,
                limit,
                include_system=system,
                should_stop=should_stop,
                progress=lambda m: (progress(m), not should_stop())[1],
            )

        self._worker = FunctionWorker(job, parent=self)
        self._worker.progress.connect(lambda m: (self.info.setText(f"Scanning {m}"), task.progress(detail=str(m))))
        self._worker.result.connect(lambda r: (self._done(r), task.finish(f"{len(r[0]):,} events")))
        self._worker.failed.connect(lambda m: (self._failed(m), task.finish(m, failed=True)))
        self.build_btn.setEnabled(False)
        self.stop_btn.setEnabled(True)
        self.progress.show()
        self._worker.start()

    def stop(self) -> None:
        if self._worker:
            self._worker.cancel()

    def _done(self, result: tuple[list[Any], bool]) -> None:
        events, truncated = result
        self.build_btn.setEnabled(True)
        self.stop_btn.setEnabled(False)
        self.progress.hide()
        cols = ["timestamp", "database", "table", "row", "column", "kind", "summary"]
        self.grid.set_result(cols, [e.to_dict() for e in events])
        self.info.setText(
            f"{len(events):,} events"
            + (" (limit reached)" if truncated else "")
            + " - double-click to jump to the record"
        )
        self.status.emit(self.info.text())

    def _failed(self, message: str) -> None:
        self.build_btn.setEnabled(True)
        self.stop_btn.setEnabled(False)
        self.progress.hide()
        self.info.setText(f"Error: {message}")

    def _activated(self, row: dict[str, Any]) -> None:
        if row.get("database") and row.get("table") is not None:
            self.jump_requested.emit(str(row["database"]), str(row["table"]), int(row.get("row", 0)))

    def shutdown(self) -> None:
        self.stop()
        if self._worker:
            self._worker.wait(3000)
