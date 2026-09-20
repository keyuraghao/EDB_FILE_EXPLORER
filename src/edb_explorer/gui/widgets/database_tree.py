"""Left-hand navigator: open databases and their tables."""

from __future__ import annotations

from typing import Any

from PySide6.QtCore import QModelIndex, QPoint, QSortFilterProxyModel, Qt, Signal
from PySide6.QtWidgets import QHBoxLayout, QHeaderView, QLineEdit, QMenu, QToolButton, QTreeView, QVBoxLayout, QWidget

from edb_explorer.core import EdbDatabase
from edb_explorer.gui.icons import kind_icon, std
from edb_explorer.gui.models import DB_ID_ROLE, KIND_ROLE, TABLE_ROLE, VIEW_ROLE, DatabaseTreeModel


class DatabaseTree(QWidget):
    table_activated = Signal(str, str)
    database_selected = Signal(str)
    close_requested = Signal(str)
    export_requested = Signal(str)
    count_requested = Signal(str)
    export_table_requested = Signal(str, str)
    view_activated = Signal(str, str)  # db_id, view id
    mailboxes_activated = Signal(str)  # db_id (Exchange)
    stats_requested = Signal(str, str)
    sql_requested = Signal(str)

    def __init__(self, parent: Any = None) -> None:
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)
        top = QHBoxLayout()
        top.setSpacing(4)
        self.filter = QLineEdit()
        self.filter.setPlaceholderText("Filter tables")
        self.filter.setClearButtonEnabled(True)
        top.addWidget(self.filter, 1)
        self.collapse_btn = QToolButton()
        self.collapse_btn.setIcon(std("SP_TitleBarShadeButton"))
        self.collapse_btn.setToolTip("Collapse all databases")
        self.collapse_btn.setAutoRaise(True)
        self.collapse_btn.clicked.connect(self.collapse_all)
        top.addWidget(self.collapse_btn)
        self.expand_btn = QToolButton()
        self.expand_btn.setIcon(std("SP_TitleBarUnshadeButton"))
        self.expand_btn.setToolTip("Expand all databases")
        self.expand_btn.setAutoRaise(True)
        self.expand_btn.clicked.connect(self.expand_all)
        top.addWidget(self.expand_btn)
        layout.addLayout(top)

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

    def add_database(self, db: EdbDatabase) -> None:
        item = self.model.add_database(db, kind_icon(db.kind))
        self.view.expand(self.proxy.mapFromSource(item.index()))
        self.view.setCurrentIndex(self.proxy.mapFromSource(item.index()))

    def collapse_all(self) -> None:
        self.view.collapseAll()

    def expand_all(self) -> None:
        for r in range(self.proxy.rowCount()):
            self.view.expand(self.proxy.index(r, 0))

    def remove_database(self, db_id: str) -> None:
        self.model.remove_database(db_id)

    def refresh_counts(self, db: EdbDatabase) -> None:
        self.model.update_counts(db)

    def _src(self, index: QModelIndex) -> QModelIndex:
        return self.proxy.mapToSource(index).siblingAtColumn(0)

    def _activated(self, index: QModelIndex) -> None:
        src = self._src(index)
        kind = src.data(KIND_ROLE)
        if kind == "table":
            self.table_activated.emit(src.data(DB_ID_ROLE), src.data(TABLE_ROLE))
        elif kind == "view":
            self.view_activated.emit(src.data(DB_ID_ROLE), src.data(VIEW_ROLE))
        elif kind == "mailboxes":
            self.mailboxes_activated.emit(src.data(DB_ID_ROLE))
        else:
            self.view.setExpanded(index, not self.view.isExpanded(index))

    def _clicked(self, index: QModelIndex) -> None:
        src = self._src(index)
        if src.data(KIND_ROLE) in ("db", "table", "view", "views", "mailboxes"):
            self.database_selected.emit(src.data(DB_ID_ROLE))

    def _context_menu(self, pos: QPoint) -> None:
        index = self.view.indexAt(pos)
        if not index.isValid():
            return
        src = self._src(index)
        db_id = src.data(DB_ID_ROLE)
        menu = QMenu(self)
        kind = src.data(KIND_ROLE)
        if kind == "table":
            table = src.data(TABLE_ROLE)
            menu.addAction("Open table", lambda: self.table_activated.emit(db_id, table))
            menu.addAction("Column statistics…", lambda: self.stats_requested.emit(db_id, table))
            menu.addAction("Extract table…", lambda: self.export_table_requested.emit(db_id, table))
            menu.addSeparator()
        elif kind == "view":
            menu.addAction("Run view", lambda: self.view_activated.emit(db_id, src.data(VIEW_ROLE)))
            menu.addSeparator()
        elif kind == "mailboxes":
            menu.addAction("Open mailbox viewer", lambda: self.mailboxes_activated.emit(db_id))
            menu.addSeparator()
        menu.addAction("SQL console for this database", lambda: self.sql_requested.emit(db_id))
        menu.addAction("Count records in all tables", lambda: self.count_requested.emit(db_id))
        menu.addAction("Export database…", lambda: self.export_requested.emit(db_id))
        menu.addSeparator()
        menu.addAction("Close database", lambda: self.close_requested.emit(db_id))
        menu.exec(self.view.viewport().mapToGlobal(pos))
