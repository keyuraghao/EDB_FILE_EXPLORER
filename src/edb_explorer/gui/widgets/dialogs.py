"""Dialogs: global search, export, directory scan, timestamp decoder, about."""

from __future__ import annotations

import os
from datetime import datetime
from pathlib import Path
from typing import Any

from PySide6.QtCore import QSettings, Qt, QUrl, Signal
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QRadioButton,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from edb_explorer import __app_name__, __version__
from edb_explorer.core import EdbDatabase, Session
from edb_explorer.core.export import EXPORT_FORMATS, FORMAT_LABELS, export_database, export_rows, export_table
from edb_explorer.core.report import REPORT_FORMAT_LABELS, REPORT_FORMATS, ReportOptions, generate_report
from edb_explorer.core.search import search_database
from edb_explorer.core.values import interpret_timestamp
from edb_explorer.gui.workers import FunctionWorker


# --------------------------------------------------------------------------- #
class SearchDialog(QDialog):
    """Search every table/column of one or all databases in a background thread."""

    hit_activated = Signal(str, str, int, str)  # db_id, table, row_index, column

    def __init__(self, session: Session, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.session = session
        self.setWindowTitle("Search databases")
        self.resize(900, 520)
        self.setModal(False)
        self._worker: FunctionWorker | None = None

        layout = QVBoxLayout(self)
        form = QHBoxLayout()
        self.query = QLineEdit()
        self.query.setPlaceholderText("Text to find in any column…")
        self.query.returnPressed.connect(self.start)
        form.addWidget(self.query, 1)
        self.scope = QComboBox()
        form.addWidget(self.scope)
        self.regex = QCheckBox("Regex")
        self.case = QCheckBox("Case")
        self.system = QCheckBox("MSys tables")
        form.addWidget(self.regex)
        form.addWidget(self.case)
        form.addWidget(self.system)
        self.limit = QSpinBox()
        self.limit.setRange(1, 100_000)
        self.limit.setValue(500)
        self.limit.setPrefix("max ")
        form.addWidget(self.limit)
        self.go = QPushButton("Search")
        self.go.clicked.connect(self.start)
        form.addWidget(self.go)
        self.stop_btn = QPushButton("Stop")
        self.stop_btn.setEnabled(False)
        self.stop_btn.clicked.connect(self.stop)
        form.addWidget(self.stop_btn)
        layout.addLayout(form)

        self.results = QTableWidget(0, 5)
        self.results.setHorizontalHeaderLabels(["Database", "Table", "Row", "Column", "Value"])
        self.results.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.results.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.results.horizontalHeader().setStretchLastSection(True)
        self.results.verticalHeader().setVisible(False)
        self.results.itemDoubleClicked.connect(self._activate)
        layout.addWidget(self.results, 1)

        foot = QHBoxLayout()
        self.status = QLabel("Double-click a hit to jump to the record.")
        self.status.setObjectName("dim")
        foot.addWidget(self.status, 1)
        self.progress = QProgressBar()
        self.progress.setRange(0, 0)
        self.progress.setMaximumWidth(160)
        self.progress.hide()
        foot.addWidget(self.progress)
        layout.addLayout(foot)
        self.refresh_scope()

    def refresh_scope(self) -> None:
        current = self.scope.currentData()
        self.scope.clear()
        self.scope.addItem("All open databases", "*")
        for db in self.session:
            self.scope.addItem(db.path.name, db.id)
        idx = self.scope.findData(current)
        if idx >= 0:
            self.scope.setCurrentIndex(idx)

    def start(self) -> None:
        text = self.query.text()
        if not text or (self._worker and self._worker.isRunning()):
            return
        self.results.setRowCount(0)
        scope = self.scope.currentData()
        dbs = list(self.session) if scope == "*" else [self.session.get(scope)]
        regex, case, system, limit = (
            self.regex.isChecked(),
            self.case.isChecked(),
            self.system.isChecked(),
            self.limit.value(),
        )

        def job(progress: Any, should_stop: Any) -> int:
            n = 0
            for db in dbs:
                for hit in search_database(db, text, None, None, regex, case, system, limit - n, should_stop):
                    progress(hit)
                    n += 1
                    if n >= limit:
                        return n
                if should_stop():
                    break
            return n

        self._worker = FunctionWorker(job, parent=self)
        self._worker.progress.connect(self._add_hit)
        self._worker.result.connect(self._done)
        self._worker.failed.connect(lambda m: self._done(-1, m))
        self.go.setEnabled(False)
        self.stop_btn.setEnabled(True)
        self.progress.show()
        self.status.setText("Searching…")
        self._worker.start()

    def stop(self) -> None:
        if self._worker:
            self._worker.cancel()

    def _add_hit(self, hit: Any) -> None:
        r = self.results.rowCount()
        self.results.insertRow(r)
        for c, text in enumerate((hit.database, hit.table, str(hit.row_index), hit.column, hit.value)):
            item = QTableWidgetItem(text)
            item.setData(Qt.ItemDataRole.UserRole, (hit.database, hit.table, hit.row_index, hit.column))
            self.results.setItem(r, c, item)
        if r % 50 == 0:
            self.status.setText(f"{r + 1} hits…")

    def _done(self, n: int, error: str | None = None) -> None:
        self.go.setEnabled(True)
        self.stop_btn.setEnabled(False)
        self.progress.hide()
        self.status.setText(f"Error: {error}" if error else f"{self.results.rowCount()} hit(s). Double-click to jump.")
        self.results.resizeColumnsToContents()

    def _activate(self, item: QTableWidgetItem) -> None:
        data = item.data(Qt.ItemDataRole.UserRole)
        if data:
            self.hit_activated.emit(*data)

    def closeEvent(self, event: Any) -> None:
        self.stop()
        super().closeEvent(event)


# --------------------------------------------------------------------------- #
class ExportDialog(QDialog):
    """Extract the selection, the displayed rows, the whole table or the whole database to any format."""

    def __init__(
        self,
        db: EdbDatabase,
        table: str | None = None,
        displayed_rows: list[dict[str, Any]] | None = None,
        selected_rows: list[dict[str, Any]] | None = None,
        visible_columns: list[str] | None = None,
        parent: QWidget | None = None,
        default_scope: str = "table",
    ) -> None:
        super().__init__(parent)
        self.db = db
        self.table = table
        self.displayed_rows = displayed_rows
        self.selected_rows = selected_rows
        self.visible_columns = visible_columns
        self._worker: FunctionWorker | None = None
        self.setWindowTitle("Extract / export")
        self.resize(600, 420)
        layout = QVBoxLayout(self)

        scope = QGroupBox("What to extract")
        sl = QVBoxLayout(scope)
        n_sel, n_disp, n_cols = len(selected_rows or []), len(displayed_rows or []), len(visible_columns or [])
        self.r_selected = QRadioButton(f"Selected rows - {n_sel} row(s), {n_cols} visible column(s)")
        self.r_table_view = QRadioButton(
            f"Rows as displayed (filtered/sorted) - {n_disp} row(s), {n_cols} visible column(s)"
        )
        self.r_table_all = QRadioButton(f"Whole table “{table}” - every row and column (streamed from disk)")
        self.r_db = QRadioButton(f"Whole database “{db.path.name}” - every table (xlsx: one workbook, sheet per table)")
        for r in (self.r_selected, self.r_table_view, self.r_table_all, self.r_db):
            sl.addWidget(r)
        results_mode = default_scope == "results"
        if results_mode:
            self.r_table_view.setText(f"Query / timeline results as displayed - {n_disp} row(s), {n_cols} column(s)")
            self.r_table_all.hide()
        self.r_selected.setEnabled(bool((table or results_mode) and n_sel))
        self.r_table_view.setEnabled(bool((table or results_mode) and displayed_rows is not None))
        self.r_table_all.setEnabled(bool(table))
        if default_scope == "selection" and n_sel:
            self.r_selected.setChecked(True)
        elif results_mode:
            (self.r_selected if n_sel else self.r_table_view).setChecked(True)
        elif table:
            self.r_table_all.setChecked(True)
        else:
            self.r_db.setChecked(True)
        self.include_system = QCheckBox("Include MSys* system tables (whole database only)")
        sl.addWidget(self.include_system)
        layout.addWidget(scope)

        form = QFormLayout()
        self.format = QComboBox()
        for f in EXPORT_FORMATS:
            self.format.addItem(f"{f}  -  {FORMAT_LABELS[f]}", f)
        form.addRow("Format", self.format)
        dest = QHBoxLayout()
        self.dest = QLineEdit()
        browse = QPushButton("Browse…")
        browse.clicked.connect(self._browse)
        dest.addWidget(self.dest, 1)
        dest.addWidget(browse)
        form.addRow("Destination", dest)
        self.open_after = QCheckBox("Open when finished")
        self.open_after.setChecked(True)
        form.addRow("", self.open_after)
        layout.addLayout(form)
        for r in (self.r_selected, self.r_table_all, self.r_table_view, self.r_db):
            r.toggled.connect(self._suggest_dest)
        self.format.currentIndexChanged.connect(self._suggest_dest)

        self.progress = QProgressBar()
        self.progress.setRange(0, 0)
        self.progress.hide()
        layout.addWidget(self.progress)
        self.status = QLabel("")
        self.status.setObjectName("dim")
        self.status.setWordWrap(True)
        layout.addWidget(self.status)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText("Extract")
        buttons.accepted.connect(self.run_export)
        buttons.rejected.connect(self.reject)
        self.buttons = buttons
        layout.addWidget(buttons)
        self._suggest_dest()

    def _fmt(self) -> str:
        return str(self.format.currentData())

    def _is_db(self) -> bool:
        return self.r_db.isChecked()

    def _suggest_dest(self) -> None:
        fmt = self._fmt()
        base = Path(str(QSettings().value("export_dir", str(Path.home() / "edb-explorer-exports"))))
        if self._is_db():
            self.dest.setText(str(base / self.db.path.stem))
        else:
            from edb_explorer.core.export import safe_filename

            suffix = "_selection" if self.r_selected.isChecked() else "_view" if self.r_table_view.isChecked() else ""
            self.dest.setText(str(base / f"{self.db.path.stem}_{safe_filename(self.table or 'results')}{suffix}.{fmt}"))

    def _browse(self) -> None:
        if self._is_db():
            d = QFileDialog.getExistingDirectory(self, "Choose output directory", self.dest.text())
            if d:
                self.dest.setText(d)
        else:
            fmt = self._fmt()
            f, _ = QFileDialog.getSaveFileName(
                self, "Save as", self.dest.text(), f"{fmt.upper()} (*.{fmt});;All files (*)"
            )
            if f:
                self.dest.setText(f)

    def run_export(self) -> None:
        dest = self.dest.text().strip()
        if not dest:
            self.status.setText("Choose a destination.")
            return
        fmt = self._fmt()
        db, table = self.db, self.table
        cols = self.visible_columns or []
        include_system = self.include_system.isChecked()
        types = db.column_types(table) if table else {}
        title = f"{db.path.name} - {table or 'results'}"
        QSettings().setValue("export_dir", str(Path(dest).parent))

        if self._is_db():

            def job(progress: Any, should_stop: Any) -> str:
                def cb(name: str, n: int) -> bool:
                    progress(f"{name}: {n:,} rows")
                    return not should_stop()

                res = export_database(db, dest, fmt, include_system, progress=cb)
                return f"Exported {len(res)} tables ({sum(res.values()):,} rows) to {dest}"

        elif self.r_selected.isChecked() or self.r_table_view.isChecked():
            rows = self.selected_rows if self.r_selected.isChecked() else self.displayed_rows

            def job(progress: Any, should_stop: Any) -> str:
                def cb(n: int) -> bool:
                    progress(f"{n:,} rows…")
                    return not should_stop()

                n = export_rows(rows or [], cols, dest, fmt, types, title, progress=cb)
                return f"Wrote {n:,} rows to {dest}"

        else:

            def job(progress: Any, should_stop: Any) -> str:
                def cb(n: int) -> bool:
                    progress(f"{n:,} rows…")
                    return not should_stop()

                n = export_table(db, table or "", dest, fmt, progress=cb)
                return f"Wrote {n:,} rows to {dest}"

        self._worker = FunctionWorker(job, parent=self)
        self._worker.progress.connect(lambda m: self.status.setText(str(m)))
        self._worker.result.connect(lambda m: self._done(m, dest))
        self._worker.failed.connect(lambda m: self._done(f"Failed: {m}", None))
        self.buttons.setEnabled(False)
        self.progress.show()
        self.status.setText("Extracting…")
        self._worker.start()

    def _done(self, message: str, path: str | None) -> None:
        self.progress.hide()
        self.status.setText(message)
        self.buttons.setEnabled(True)
        self.buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("Close")
        self.buttons.button(QDialogButtonBox.StandardButton.Ok).hide()
        if path and self.open_after.isChecked():
            QDesktopServices.openUrl(QUrl.fromLocalFile(path))

    def reject(self) -> None:
        if self._worker and self._worker.isRunning():
            self._worker.cancel()
            self._worker.wait(5000)
        super().reject()


# --------------------------------------------------------------------------- #
class ReportDialog(QDialog):
    """Generate an analyst report for one or more open databases."""

    def __init__(self, session: Session, preselect: str | None = None, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.session = session
        self._worker: FunctionWorker | None = None
        self.setWindowTitle("Generate report")
        self.resize(640, 560)
        layout = QVBoxLayout(self)

        box = QGroupBox("Databases to include")
        bl = QVBoxLayout(box)
        self.db_list = QListWidget()
        for db in session:
            item = QListWidgetItem(f"{db.path.name}   -   {db.profile.name}")
            item.setData(Qt.ItemDataRole.UserRole, db.id)
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(Qt.CheckState.Checked if preselect in (None, db.id) else Qt.CheckState.Unchecked)
            self.db_list.addItem(item)
        self.db_list.setMaximumHeight(120)
        bl.addWidget(self.db_list)
        layout.addWidget(box)

        form = QFormLayout()
        self.title = QLineEdit()
        self.title.setPlaceholderText("Optional - defaults to 'ESE database report - <files>'")
        self.case_id = QLineEdit()
        self.analyst = QLineEdit(str(QSettings().value("report_analyst", "")))
        self.notes = QPlainTextEdit()
        self.notes.setPlaceholderText("Optional notes included at the top of the report")
        self.notes.setMaximumHeight(70)
        form.addRow("Title", self.title)
        form.addRow("Case ID", self.case_id)
        form.addRow("Analyst", self.analyst)
        form.addRow("Notes", self.notes)
        self.include_schema = QCheckBox("Include full schema (columns and indexes) for every table")
        self.include_schema.setChecked(True)
        self.count = QCheckBox("Count records in every table (full scan)")
        self.count.setChecked(True)
        self.hash = QCheckBox("Compute SHA-256 of each file")
        self.hash.setChecked(True)
        self.system = QCheckBox("Include MSys* system tables")
        self.samples = QSpinBox()
        self.samples.setRange(0, 100)
        self.samples.setValue(5)
        form.addRow("", self.include_schema)
        form.addRow("", self.count)
        form.addRow("", self.hash)
        form.addRow("", self.system)
        form.addRow("Sample rows per table", self.samples)
        self.format = QComboBox()
        for f in REPORT_FORMATS:
            self.format.addItem(f"{f}  -  {REPORT_FORMAT_LABELS[f]}", f)
        form.addRow("Format", self.format)
        dest = QHBoxLayout()
        self.dest = QLineEdit()
        browse = QPushButton("Browse…")
        browse.clicked.connect(self._browse)
        dest.addWidget(self.dest, 1)
        dest.addWidget(browse)
        form.addRow("Destination", dest)
        self.open_after = QCheckBox("Open when finished")
        self.open_after.setChecked(True)
        form.addRow("", self.open_after)
        layout.addLayout(form)
        self.format.currentIndexChanged.connect(self._suggest_dest)

        self.progress = QProgressBar()
        self.progress.setRange(0, 0)
        self.progress.hide()
        layout.addWidget(self.progress)
        self.status = QLabel("")
        self.status.setObjectName("dim")
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        self.buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        self.buttons.button(QDialogButtonBox.StandardButton.Ok).setText("Generate")
        self.buttons.accepted.connect(self.run)
        self.buttons.rejected.connect(self.reject)
        layout.addWidget(self.buttons)
        self._suggest_dest()

    def _fmt(self) -> str:
        return str(self.format.currentData())

    def _suggest_dest(self) -> None:
        base = Path(str(QSettings().value("report_dir", str(Path.home() / "edb-explorer-reports"))))
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        self.dest.setText(str(base / f"edb_report_{stamp}.{self._fmt()}"))

    def _browse(self) -> None:
        fmt = self._fmt()
        f, _ = QFileDialog.getSaveFileName(
            self, "Save report as", self.dest.text(), f"{fmt.upper()} (*.{fmt});;All files (*)"
        )
        if f:
            self.dest.setText(f)

    def run(self) -> None:
        ids = [
            self.db_list.item(i).data(Qt.ItemDataRole.UserRole)
            for i in range(self.db_list.count())
            if self.db_list.item(i).checkState() == Qt.CheckState.Checked
        ]
        if not ids:
            self.status.setText("Select at least one database.")
            return
        dest = self.dest.text().strip()
        if not dest:
            self.status.setText("Choose a destination.")
            return
        dbs = [self.session.get(i) for i in ids]
        opts = ReportOptions(
            title=self.title.text().strip() or None,
            case_id=self.case_id.text().strip() or None,
            analyst=self.analyst.text().strip() or None,
            notes=self.notes.toPlainText().strip() or None,
            include_schema=self.include_schema.isChecked(),
            include_system=self.system.isChecked(),
            count_records=self.count.isChecked(),
            compute_hash=self.hash.isChecked(),
            sample_rows=self.samples.value(),
        )
        fmt = self._fmt()
        QSettings().setValue("report_analyst", self.analyst.text())
        QSettings().setValue("report_dir", str(Path(dest).parent))

        def job(progress: Any, should_stop: Any) -> str:
            def cb(msg: str) -> bool:
                progress(msg)
                return not should_stop()

            path = generate_report(dbs, dest, fmt, opts, cb)
            return str(path)

        self._worker = FunctionWorker(job, parent=self)
        self._worker.progress.connect(lambda m: self.status.setText(str(m)))
        self._worker.result.connect(self._done)
        self._worker.failed.connect(lambda m: self._done(None, f"Failed: {m}"))
        self.buttons.setEnabled(False)
        self.progress.show()
        self.status.setText("Generating…")
        self._worker.start()

    def _done(self, path: str | None, error: str | None = None) -> None:
        self.progress.hide()
        self.buttons.setEnabled(True)
        self.buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("Close")
        self.buttons.button(QDialogButtonBox.StandardButton.Ok).hide()
        if error:
            self.status.setText(error)
            return
        self.status.setText(f"Report written to {path}")
        if self.open_after.isChecked() and path:
            QDesktopServices.openUrl(QUrl.fromLocalFile(path))

    def reject(self) -> None:
        if self._worker and self._worker.isRunning():
            self._worker.cancel()
            self._worker.wait(5000)
        super().reject()


# --------------------------------------------------------------------------- #
class ScanDialog(QDialog):
    """Pick a directory, find every ESE file in it (by magic), choose which to open."""

    def __init__(self, session: Session, start_dir: str | None = None, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.session = session
        self.selected: list[str] = []
        self._worker: FunctionWorker | None = None
        self.setWindowTitle("Open folder - scan for ESE databases")
        self.resize(760, 460)
        layout = QVBoxLayout(self)
        row = QHBoxLayout()
        self.dir_edit = QLineEdit(start_dir or str(Path.home()))
        browse = QPushButton("Browse…")
        browse.clicked.connect(self._browse)
        self.scan_btn = QPushButton("Scan")
        self.scan_btn.clicked.connect(self.scan)
        row.addWidget(QLabel("Folder"))
        row.addWidget(self.dir_edit, 1)
        row.addWidget(browse)
        row.addWidget(self.scan_btn)
        layout.addLayout(row)
        self.recursive = QCheckBox("Include sub-folders")
        self.recursive.setChecked(True)
        layout.addWidget(self.recursive)
        self.list = QListWidget()
        self.list.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        layout.addWidget(self.list, 1)
        self.status = QLabel("Files are detected by their ESE header signature, not their extension.")
        self.status.setObjectName("dim")
        layout.addWidget(self.status)
        btns = QHBoxLayout()
        all_btn = QPushButton("Select all")
        all_btn.clicked.connect(lambda: self._check_all(True))
        none_btn = QPushButton("Select none")
        none_btn.clicked.connect(lambda: self._check_all(False))
        btns.addWidget(all_btn)
        btns.addWidget(none_btn)
        btns.addStretch(1)
        self.buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Open | QDialogButtonBox.StandardButton.Cancel)
        self.buttons.accepted.connect(self._accept)
        self.buttons.rejected.connect(self.reject)
        btns.addWidget(self.buttons)
        layout.addLayout(btns)

    def _browse(self) -> None:
        d = QFileDialog.getExistingDirectory(self, "Choose folder", self.dir_edit.text())
        if d:
            self.dir_edit.setText(d)
            self.scan()

    def scan(self) -> None:
        directory = self.dir_edit.text().strip()
        if not directory or not os.path.isdir(directory):
            self.status.setText("Not a directory.")
            return
        self.list.clear()
        self.scan_btn.setEnabled(False)
        self.status.setText("Scanning…")
        recursive = self.recursive.isChecked()
        self._worker = FunctionWorker(
            lambda progress, should_stop: self.session.scan(directory, recursive=recursive), parent=self
        )
        self._worker.result.connect(self._found)
        self._worker.failed.connect(lambda m: self._found([], m))
        self._worker.start()

    def _found(self, paths: list[Path], error: str | None = None) -> None:
        self.scan_btn.setEnabled(True)
        if error:
            self.status.setText(f"Error: {error}")
            return
        for p in paths:
            try:
                size = p.stat().st_size
            except OSError:
                size = 0
            item = QListWidgetItem(f"{p.name}   ({size:,} bytes)   —   {p}")
            item.setData(Qt.ItemDataRole.UserRole, str(p))
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(Qt.CheckState.Checked if size > 0 else Qt.CheckState.Unchecked)
            self.list.addItem(item)
        self.status.setText(f"{len(paths)} ESE database(s) found.")

    def _check_all(self, checked: bool) -> None:
        for i in range(self.list.count()):
            self.list.item(i).setCheckState(Qt.CheckState.Checked if checked else Qt.CheckState.Unchecked)

    def _accept(self) -> None:
        self.selected = [
            self.list.item(i).data(Qt.ItemDataRole.UserRole)
            for i in range(self.list.count())
            if self.list.item(i).checkState() == Qt.CheckState.Checked
        ]
        self.accept()


# --------------------------------------------------------------------------- #
class TimestampDialog(QDialog):
    """Decode an arbitrary number as FILETIME / OLE / Unix / WebKit…"""

    def __init__(self, value: Any = None, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Timestamp decoder")
        self.resize(560, 380)
        layout = QVBoxLayout(self)
        row = QHBoxLayout()
        self.edit = QLineEdit()
        self.edit.setPlaceholderText("Decimal, float, 0x-hex, or hex bytes (e.g. 132565120200137766)")
        self.edit.returnPressed.connect(self.decode)
        row.addWidget(self.edit, 1)
        go = QPushButton("Decode")
        go.clicked.connect(self.decode)
        row.addWidget(go)
        layout.addLayout(row)
        self.out = QPlainTextEdit()
        self.out.setReadOnly(True)
        self.out.setObjectName("mono")
        layout.addWidget(self.out, 1)
        if value is not None:
            self.edit.setText(value.hex() if isinstance(value, bytes) else str(value))
            self.decode()

    def decode(self) -> None:
        text = self.edit.text().strip()
        if not text:
            return
        try:
            if len(text) in (8, 16) and all(ch in "0123456789abcdefABCDEF" for ch in text) and not text.isdigit():
                info = interpret_timestamp(bytes.fromhex(text))
            else:
                info = interpret_timestamp(text)
        except (ValueError, OverflowError) as exc:
            self.out.setPlainText(f"Cannot decode: {exc}")
            return
        lines = [f"{k:<22} {v if v is not None else '-'}" for k, v in info.items()]
        self.out.setPlainText("\n".join(lines))


# --------------------------------------------------------------------------- #
class AboutDialog(QDialog):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle(f"About {__app_name__}")
        layout = QVBoxLayout(self)
        title = QLabel(f"<h2>{__app_name__} {__version__}</h2>")
        layout.addWidget(title)
        body = QLabel(
            "<p>A cross-platform explorer for Microsoft Extensible Storage Engine (ESE / JET Blue) databases: "
            "Active Directory <code>ntds.dit</code>, SRUM <code>SRUDB.dat</code>, Exchange <code>.edb</code>, "
            "<code>WebCacheV01.dat</code>, <code>Windows.edb</code> and more.</p>"
            "<p>Parsing is powered by <a href='https://github.com/fox-it/dissect.esedb'>dissect.esedb</a>; the UI by "
            "<a href='https://www.qt.io/qt-for-python'>PySide6</a>. The bundled MCP server exposes the same data to AI "
            "agents (<code>edb-explorer mcp</code>).</p>"
            "<p>All access is strictly read-only. MIT licensed.</p>"
        )
        body.setOpenExternalLinks(True)
        body.setWordWrap(True)
        layout.addWidget(body)
        btn = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        btn.rejected.connect(self.reject)
        btn.accepted.connect(self.accept)
        layout.addWidget(btn)
