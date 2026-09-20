"""Database properties panel."""

from __future__ import annotations

from typing import Any

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QHBoxLayout, QLabel, QPushButton, QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget

from edb_explorer.core import EdbDatabase
from edb_explorer.gui.workers import FunctionWorker


class InfoPanel(QWidget):
    def __init__(self, parent: Any = None) -> None:
        super().__init__(parent)
        self.db: EdbDatabase | None = None
        self._worker: FunctionWorker | None = None
        layout = QVBoxLayout(self)
        layout.setContentsMargins(6, 6, 6, 6)
        self.title = QLabel("No database selected")
        self.title.setObjectName("title")
        self.title.setWordWrap(True)
        layout.addWidget(self.title)
        self.subtitle = QLabel("")
        self.subtitle.setObjectName("dim")
        self.subtitle.setWordWrap(True)
        layout.addWidget(self.subtitle)
        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(["Property", "Value"])
        self.tree.setAlternatingRowColors(True)
        self.tree.setColumnWidth(0, 170)
        layout.addWidget(self.tree, 1)
        row = QHBoxLayout()
        self.hash_btn = QPushButton("Compute SHA-256")
        self.hash_btn.clicked.connect(self._hash)
        row.addWidget(self.hash_btn)
        row.addStretch(1)
        layout.addLayout(row)

    def show_database(self, db: EdbDatabase | None) -> None:
        self.db = db
        self.tree.clear()
        if db is None:
            self.title.setText("No database selected")
            self.subtitle.setText("")
            return
        info = db.info
        self.title.setText(db.path.name)
        self.subtitle.setText(f"{db.profile.name}\n{db.profile.description}")
        general = QTreeWidgetItem(["General"])
        for k, v in (
            ("Identifier", info.id),
            ("Path", info.path),
            ("Size", f"{info.size_bytes:,} bytes"),
            ("Tables", str(info.table_count)),
            ("Page size", f"{info.page_size:,} bytes"),
            ("Format", f"0x{info.format_version:X} rev {info.format_revision}"),
            ("Created by format", f"0x{info.created_version:X} rev {info.created_revision}"),
            ("Shutdown state", info.state),
            ("Windows version", info.windows_version or "-"),
            ("SHA-256", info.sha256 or "(not computed)"),
        ):
            general.addChild(QTreeWidgetItem([k, v]))
        times = QTreeWidgetItem(["Timestamps (UTC)"])
        for k, v in (
            ("Created", info.created),
            ("Last attach", info.last_attach),
            ("Last detach", info.last_detach),
        ):
            times.addChild(QTreeWidgetItem([k, v.isoformat() if v else "-"]))
        header = QTreeWidgetItem(["Raw header"])
        for k, v in info.header.items():
            header.addChild(QTreeWidgetItem([k, str(v)]))
        for item in (general, times, header):
            self.tree.addTopLevelItem(item)
            item.setExpanded(item is not header)
        self.hash_btn.setEnabled(info.sha256 is None)
        for i in range(self.tree.topLevelItemCount()):
            self.tree.topLevelItem(i).setFlags(self.tree.topLevelItem(i).flags() | Qt.ItemFlag.ItemIsSelectable)

    def _hash(self) -> None:
        if self.db is None:
            return
        db = self.db
        self.hash_btn.setEnabled(False)
        self.hash_btn.setText("Hashing…")
        self._worker = FunctionWorker(lambda progress, should_stop: db.compute_sha256(), parent=self)
        self._worker.result.connect(lambda _h: self._hashed(db))
        self._worker.failed.connect(lambda msg: self.hash_btn.setText(f"Failed: {msg}"))
        self._worker.start()

    def _hashed(self, db: EdbDatabase) -> None:
        self.hash_btn.setText("Compute SHA-256")
        if self.db is db:
            self.show_database(db)
