"""Left-hand navigator: open databases and their tables."""

from __future__ import annotations

from typing import Any

from PySide6.QtCore import (
    QEvent,
    QModelIndex,
    QPersistentModelIndex,
    QPoint,
    QRect,
    QSortFilterProxyModel,
    Qt,
    QTimer,
    Signal,
)
from PySide6.QtGui import QFont, QPainter, QPalette
from PySide6.QtWidgets import (
    QAbstractItemView,
    QHBoxLayout,
    QHeaderView,
    QLineEdit,
    QMenu,
    QStyle,
    QToolButton,
    QTreeView,
    QVBoxLayout,
    QWidget,
)

from edb_explorer.core import EdbDatabase
from edb_explorer.gui.icons import icon, kind_icon
from edb_explorer.gui.models import DB_ID_ROLE, KIND_ROLE, TABLE_ROLE, VIEW_ROLE, DatabaseTreeModel


class StickyHeader(QWidget):
    """Keeps the ancestors of the topmost visible row (folder, database) pinned at the top of a tree view
    while their children scroll underneath, like an IDE's sticky scroll; click a pinned row to jump to it."""

    def __init__(self, view: QTreeView) -> None:
        super().__init__(view.viewport())
        self.view = view
        self._rows: list[QPersistentModelIndex] = []
        self._row_h = 24
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, False)
        self.hide()
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(0)
        self._timer.timeout.connect(self._refresh)
        view.verticalScrollBar().valueChanged.connect(self.schedule)
        view.expanded.connect(lambda _i: self.schedule())
        view.collapsed.connect(lambda _i: self.schedule())
        view.viewport().installEventFilter(self)
        model = view.model()
        if model is not None:
            for sig in (model.rowsInserted, model.rowsRemoved, model.layoutChanged, model.modelReset):
                sig.connect(lambda *_a: self.schedule())

    def schedule(self) -> None:
        self._timer.start()

    def eventFilter(self, obj: Any, event: Any) -> bool:
        if obj is self.view.viewport() and event.type() == QEvent.Type.Resize:
            self.schedule()
        return False

    def _refresh(self) -> None:
        view = self.view
        top = view.indexAt(QPoint(4, 1))
        chain: list[QModelIndex] = []
        parent = top.parent() if top.isValid() else QModelIndex()
        while parent.isValid():
            chain.append(parent)
            parent = parent.parent()
        chain.reverse()  # outermost ancestor first
        self._row_h = max(view.rowHeight(top) if top.isValid() else 0, view.rowHeight(chain[0]) if chain else 0, 20)
        pinned: list[QPersistentModelIndex] = []
        for depth, idx in enumerate(chain):
            # pin an ancestor once its own row would be hidden behind the rows pinned above it
            if view.visualRect(idx).top() < depth * self._row_h:
                pinned.append(QPersistentModelIndex(idx))
            else:
                break
        self._rows = pinned
        height = len(pinned) * self._row_h
        if not pinned:
            self.hide()
            return
        self.setGeometry(0, 0, view.viewport().width(), height)
        self.show()
        self.raise_()
        self.update()

    def paintEvent(self, _event: Any) -> None:
        painter = QPainter(self)
        pal = self.palette()
        bg = pal.color(QPalette.ColorRole.Button)
        border = pal.color(QPalette.ColorRole.Mid)
        indent = self.view.indentation()
        icon_px = self.view.iconSize().width()
        if icon_px <= 0:
            icon_px = self.view.style().pixelMetric(QStyle.PixelMetric.PM_SmallIconSize)
        font = QFont(self.font())
        font.setBold(True)
        painter.setFont(font)
        for i, pidx in enumerate(self._rows):
            idx = QModelIndex(pidx)
            rect = QRect(0, i * self._row_h, self.width(), self._row_h)
            painter.fillRect(rect, bg)
            painter.setPen(border)
            painter.drawLine(rect.bottomLeft(), rect.bottomRight())
            depth = 0
            parent = idx.parent()
            while parent.isValid():
                depth += 1
                parent = parent.parent()
            x = 6 + depth * indent + indent // 2
            decoration = idx.data(Qt.ItemDataRole.DecorationRole)
            if decoration is not None and not decoration.isNull():
                decoration.paint(painter, QRect(x, rect.top() + (self._row_h - icon_px) // 2, icon_px, icon_px))
                x += icon_px + 6
            fg = idx.data(Qt.ItemDataRole.ForegroundRole)
            painter.setPen(fg.color() if fg is not None else pal.color(QPalette.ColorRole.Text))
            painter.drawText(
                QRect(x, rect.top(), self.width() - x - 6, self._row_h),
                int(Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft),
                str(idx.data(Qt.ItemDataRole.DisplayRole) or ""),
            )
        painter.end()

    def mousePressEvent(self, event: Any) -> None:
        i = int(event.position().y()) // self._row_h
        if 0 <= i < len(self._rows):
            idx = QModelIndex(self._rows[i])
            self.view.scrollTo(idx, QAbstractItemView.ScrollHint.PositionAtTop)
            self.view.setCurrentIndex(idx)
        event.accept()

    def pinned_labels(self) -> list[str]:
        return [str(QModelIndex(r).data(Qt.ItemDataRole.DisplayRole) or "") for r in self._rows]


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
        self.collapse_btn.setIcon(icon("collapse"))
        self.collapse_btn.setToolTip("Collapse all databases")
        self.collapse_btn.setAutoRaise(True)
        self.collapse_btn.clicked.connect(self.collapse_all)
        top.addWidget(self.collapse_btn)
        self.expand_btn = QToolButton()
        self.expand_btn.setIcon(icon("expand"))
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
        # smooth wheel / drag scrolling instead of jumping a row at a time
        self.view.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.view.setHorizontalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.view.verticalScrollBar().setSingleStep(12)
        # full names: never elide, size the column to the longest entry and scroll sideways if needed
        self.view.setTextElideMode(Qt.TextElideMode.ElideNone)
        self.view.setWordWrap(False)
        self.view.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.view.customContextMenuRequested.connect(self._context_menu)
        self.view.activated.connect(self._activated)
        self.view.clicked.connect(self._clicked)
        hdr = self.view.header()
        hdr.setStretchLastSection(False)
        hdr.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        hdr.setSectionResizeMode(1, QHeaderView.ResizeMode.Fixed)
        hdr.setSectionResizeMode(2, QHeaderView.ResizeMode.Fixed)
        hdr.resizeSection(1, 76)
        hdr.resizeSection(2, 48)
        hdr.setMinimumSectionSize(40)
        layout.addWidget(self.view, 1)
        self.sticky = StickyHeader(self.view)

    def add_database(self, db: EdbDatabase) -> None:
        item = self.model.add_database(db, kind_icon(db.kind))
        index = self.proxy.mapFromSource(item.index())
        self.view.expand(index.parent())  # the folder row
        self.view.expand(index)
        self.view.setCurrentIndex(index)

    def collapse_all(self) -> None:
        """Collapse every database (folders stay open so the files remain listed)."""
        for f in range(self.proxy.rowCount()):
            folder = self.proxy.index(f, 0)
            for r in range(self.proxy.rowCount(folder)):
                self.view.collapse(self.proxy.index(r, 0, folder))
            self.view.expand(folder)

    def expand_all(self) -> None:
        for f in range(self.proxy.rowCount()):
            folder = self.proxy.index(f, 0)
            self.view.expand(folder)
            for r in range(self.proxy.rowCount(folder)):
                self.view.expand(self.proxy.index(r, 0, folder))

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
        if kind == "folder":
            ids = [src.child(r, 0).data(DB_ID_ROLE) for r in range(self.model.rowCount(src))]
            menu.addAction("Expand all databases in this folder", lambda: self._expand_children(index, True))
            menu.addAction("Collapse all databases in this folder", lambda: self._expand_children(index, False))
            menu.addSeparator()
            menu.addAction(
                f"Close the {len(ids)} database(s) in this folder", lambda: [self.close_requested.emit(i) for i in ids]
            )
            menu.exec(self.view.viewport().mapToGlobal(pos))
            return
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

    def _expand_children(self, folder: QModelIndex, expanded: bool) -> None:
        self.view.expand(folder)
        for r in range(self.proxy.rowCount(folder)):
            self.view.setExpanded(self.proxy.index(r, 0, folder), expanded)
