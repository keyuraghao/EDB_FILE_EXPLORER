"""Left-hand navigator: open databases and their tables."""

from __future__ import annotations

from typing import Any

from PySide6.QtCore import QModelIndex, QPoint, QSortFilterProxyModel, Qt, Signal
from PySide6.QtWidgets import QHeaderView, QLineEdit, QMenu, QTreeView, QVBoxLayout, QWidget

from edb_explorer.core import EdbDatabase
from edb_explorer.gui.icons import app_icon
from edb_explorer.gui.models import DB_ID_ROLE, KIND_ROLE, TABLE_ROLE, DatabaseTreeModel


class DatabaseTree(QWidget):
    table_activated = Signal(str, str)
    database_selected = Signal(str)
    close_requested = Signal(str)
    export_requested = Signal(str)
    count_requested = Signal(str)
    export_table_requested = Signal(str, str)

    def __init__(self, parent: Any = None) -> None:
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)
        self.filter = QLineEdit()
        self.filter.setPlaceholderText("Filter tables")
        self.filter.setClearButtonEnabled(True)
        layout.addWidget(self.filter)

        self.model = DatabaseTreeModel(self)
        self.proxy = QSortFilterProxyModel(self)
        self.proxy.setSourceModel(self.model)
        self.proxy.setRecursiveFilteringEnabled(True)
        self.proxy.setFilterCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
        self.proxy.setFilterKeyColumn(0)
        self.filter.textChanged.connect(self.proxy.setFilterFixedString)

        self.view = QTreeView()
        self.view.setModel(self.proxy)
        self.view.setHeaderHidden(False)
        self.view.setAlternatingRowColors(True)
        self.view.setUniformRowHeights(True)
        self.view.setExpandsOnDoubleClick(False)
        self.view.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.view.customContextMenuRequested.connect(self._context_menu)
        self.view.activated.connect(self._activated)
        self.view.clicked.connect(self._clicked)
        hdr = self.view.header()
        hdr.setStretchLastSection(False)
        hdr.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        hdr.setSectionResizeMode(1, QHeaderView.ResizeMode.Fixed)
        hdr.setSectionResizeMode(2, QHeaderView.ResizeMode.Fixed)
        hdr.resizeSection(1, 76)
        hdr.resizeSection(2, 48)
        hdr.setMinimumSectionSize(40)
        layout.addWidget(self.view, 1)
        self._icon = app_icon(32)

    def add_database(self, db: EdbDatabase) -> None:
        item = self.model.add_database(db, self._icon)
        self.view.expand(self.proxy.mapFromSource(item.index()))
        self.view.setCurrentIndex(self.proxy.mapFromSource(item.index()))

    def remove_database(self, db_id: str) -> None:
        self.model.remove_database(db_id)

    def refresh_counts(self, db: EdbDatabase) -> None:
        self.model.update_counts(db)

    def _src(self, index: QModelIndex) -> QModelIndex:
        return self.proxy.mapToSource(index).siblingAtColumn(0)

    def _activated(self, index: QModelIndex) -> None:
        src = self._src(index)
        if src.data(KIND_ROLE) == "table":
            self.table_activated.emit(src.data(DB_ID_ROLE), src.data(TABLE_ROLE))
        else:
            self.view.setExpanded(index, not self.view.isExpanded(index))

    def _clicked(self, index: QModelIndex) -> None:
        src = self._src(index)
        if src.data(KIND_ROLE) in ("db", "table"):
            self.database_selected.emit(src.data(DB_ID_ROLE))

    def _context_menu(self, pos: QPoint) -> None:
        index = self.view.indexAt(pos)
        if not index.isValid():
            return
        src = self._src(index)
        db_id = src.data(DB_ID_ROLE)
        menu = QMenu(self)
        if src.data(KIND_ROLE) == "table":
            table = src.data(TABLE_ROLE)
            menu.addAction("Open table", lambda: self.table_activated.emit(db_id, table))
            menu.addAction("Export table…", lambda: self.export_table_requested.emit(db_id, table))
            menu.addSeparator()
        menu.addAction("Count records in all tables", lambda: self.count_requested.emit(db_id))
        menu.addAction("Export database…", lambda: self.export_requested.emit(db_id))
        menu.addSeparator()
        menu.addAction("Close database", lambda: self.close_requested.emit(db_id))
        menu.exec(self.view.viewport().mapToGlobal(pos))
