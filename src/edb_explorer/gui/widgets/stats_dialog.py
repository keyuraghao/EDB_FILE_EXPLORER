"""Column statistics dialog."""

from __future__ import annotations

from typing import Any

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QAbstractItemView,
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QLabel,
    QProgressBar,
    QPushButton,
    QSpinBox,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from edb_explorer.core import Database
from edb_explorer.core.analysis import ColumnStats, column_statistics
from edb_explorer.gui.tasks import TaskManager
from edb_explorer.gui.workers import FunctionWorker


class StatsDialog(QDialog):
    def __init__(self, db: Database, table: str, tasks: TaskManager, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.db = db
        self.table = table
        self.tasks = tasks
        self._worker: FunctionWorker | None = None
        self._stats: list[ColumnStats] = []
        self.setWindowTitle(f"Column statistics - {table}")
        self.resize(1000, 600)
        layout = QVBoxLayout(self)
        bar = QHBoxLayout()
        bar.addWidget(QLabel(f"{db.path.name}  ›  {table}"))
        bar.addStretch(1)
        bar.addWidget(QLabel("Rows to scan:"))
        self.max_rows = QSpinBox()
        self.max_rows.setRange(0, 50_000_000)
        self.max_rows.setSpecialValueText("all")
        self.max_rows.setValue(0)
        bar.addWidget(self.max_rows)
        self.run_btn = QPushButton("Compute")
        self.run_btn.clicked.connect(self.run)
        bar.addWidget(self.run_btn)
        layout.addLayout(bar)
        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(
            ["Column", "Type", "Non-null", "Nulls", "Distinct", "Min", "Max", "Timestamps", "Top values"]
        )
        self.tree.setAlternatingRowColors(True)
        self.tree.setRootIsDecorated(False)
        self.tree.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.tree.setSortingEnabled(True)
        layout.addWidget(self.tree, 1)
        self.progress = QProgressBar()
        self.progress.setRange(0, 0)
        self.progress.hide()
        layout.addWidget(self.progress)
        self.info = QLabel("")
        self.info.setObjectName("dim")
        layout.addWidget(self.info)
        btns = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        btns.rejected.connect(self.reject)
        copy = btns.addButton("Copy as TSV", QDialogButtonBox.ButtonRole.ActionRole)
        copy.clicked.connect(self._copy)
        layout.addWidget(btns)
        self.run()

    def run(self) -> None:
        if self._worker and self._worker.isRunning():
            return
        max_rows = self.max_rows.value() or None
        task = self.tasks.start(f"Statistics: {self.table}", cancel=lambda: self._worker and self._worker.cancel())

        def job(progress: Any, should_stop: Any) -> tuple[list[ColumnStats], int]:
            return column_statistics(
                self.db, self.table, max_rows=max_rows, progress=lambda m: (progress(m), not should_stop())[1]
            )

        self._worker = FunctionWorker(job, parent=self)
        self._worker.progress.connect(lambda m: (self.info.setText(str(m)), task.progress(detail=str(m))))
        self._worker.result.connect(lambda r: (self._done(r), task.finish(f"{r[1]:,} rows")))
        self._worker.failed.connect(
            lambda m: (self.info.setText(f"Error: {m}"), self.progress.hide(), task.finish(m, failed=True))
        )
        self.progress.show()
        self.run_btn.setEnabled(False)
        self._worker.start()

    def _done(self, result: tuple[list[ColumnStats], int]) -> None:
        stats, n = result
        self._stats = stats
        self.progress.hide()
        self.run_btn.setEnabled(True)
        self.tree.clear()
        for st in stats:
            d = st.to_dict()
            ts = (
                f"{d['timestamp_kind_name']}: {st.timestamp_min} → {st.timestamp_max}"
                if st.timestamp_kind and st.timestamp_min
                else (d["timestamp_kind_name"] or "")
            )
            top = ", ".join(f"{v} ({c:,})" for v, c in st.top_values[:5])
            item = QTreeWidgetItem(
                [
                    st.name,
                    st.type,
                    f"{st.non_null:,}",
                    f"{st.nulls:,}",
                    (f"{st.distinct:,}" if st.distinct_exact else f"≥{st.distinct:,}"),
                    d["min"] or "",
                    d["max"] or "",
                    ts,
                    top,
                ]
            )
            for col in (2, 3, 4):
                item.setTextAlignment(col, Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            item.setToolTip(8, "\n".join(f"{v}: {c:,}" for v, c in st.top_values))
            self.tree.addTopLevelItem(item)
        for i in range(self.tree.columnCount()):
            self.tree.resizeColumnToContents(i)
            self.tree.setColumnWidth(i, min(self.tree.columnWidth(i), 320))
        self.info.setText(
            f"{n:,} rows scanned · {len(stats)} columns · {sum(1 for s in stats if s.timestamp_kind)} timestamp column(s)"
        )

    def _copy(self) -> None:
        from PySide6.QtWidgets import QApplication

        lines = [
            "column\ttype\tnon_null\tnulls\tdistinct\tmin\tmax\ttimestamp_kind\ttimestamp_min\ttimestamp_max\ttop_values"
        ]
        for st in self._stats:
            d = st.to_dict()
            lines.append(
                "\t".join(
                    str(x)
                    for x in (
                        st.name,
                        st.type,
                        st.non_null,
                        st.nulls,
                        st.distinct,
                        d["min"],
                        d["max"],
                        st.timestamp_kind or "",
                        st.timestamp_min or "",
                        st.timestamp_max or "",
                        "; ".join(f"{v} ({c})" for v, c in st.top_values),
                    )
                )
            )
        QApplication.clipboard().setText("\n".join(lines))
        self.info.setText("Copied statistics to clipboard")

    def reject(self) -> None:
        if self._worker and self._worker.isRunning():
            self._worker.cancel()
            self._worker.wait(3000)
        super().reject()
