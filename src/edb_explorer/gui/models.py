"""Qt item models: an append-only record model, a type-aware sort/filter proxy and the database tree."""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any

from PySide6.QtCore import QAbstractTableModel, QModelIndex, QPersistentModelIndex, QSortFilterProxyModel, Qt
from PySide6.QtGui import QColor, QStandardItem, QStandardItemModel

from edb_explorer.core import ColumnInfo, EdbDatabase, TableInfo
from edb_explorer.core.values import decode_ese_datetime, display_value

RAW_ROLE = Qt.ItemDataRole.UserRole + 1
ROW_INDEX_ROLE = Qt.ItemDataRole.UserRole + 2

KIND_ROLE = Qt.ItemDataRole.UserRole + 10
DB_ID_ROLE = Qt.ItemDataRole.UserRole + 11
TABLE_ROLE = Qt.ItemDataRole.UserRole + 12
VIEW_ROLE = Qt.ItemDataRole.UserRole + 13

_NUMERIC_TYPES = frozenset(
    {
        "Bit",
        "UnsignedByte",
        "Short",
        "Long",
        "Currency",
        "IEEESingle",
        "IEEEDouble",
        "UnsignedLong",
        "LongLong",
        "UnsignedShort",
        "DateTime",
    }
)


class RecordTableModel(QAbstractTableModel):
    """Holds the decoded rows of one table.  Rows are appended in batches by :class:`RecordLoader`."""

    def __init__(self, columns: tuple[ColumnInfo, ...], parent: Any = None) -> None:
        super().__init__(parent)
        self.columns = list(columns)
        self.column_names = [c.name for c in self.columns]
        self.column_types = {c.name: c.type for c in self.columns}
        self._col_index = {c.name: i for i, c in enumerate(self.columns)}
        self._rows: list[dict[str, Any]] = []
        self._indices: list[int] = []
        self._display: list[dict[int, str]] = []
        self._index_to_row: dict[int, int] = {}
        self.seen_columns: set[int] = set()
        self._dim = QColor("#8a9099")

    # ---- Qt API ------------------------------------------------------- #
    def rowCount(self, parent: QModelIndex | QPersistentModelIndex = QModelIndex()) -> int:  # noqa: B008
        return 0 if parent.isValid() else len(self._rows)

    def columnCount(self, parent: QModelIndex | QPersistentModelIndex = QModelIndex()) -> int:  # noqa: B008
        return 0 if parent.isValid() else len(self.columns)

    def headerData(self, section: int, orientation: Qt.Orientation, role: int = Qt.ItemDataRole.DisplayRole) -> Any:
        if orientation == Qt.Orientation.Horizontal:
            if role == Qt.ItemDataRole.DisplayRole:
                return self.column_names[section]
            if role == Qt.ItemDataRole.ToolTipRole:
                c = self.columns[section]
                return f"{c.name}\nType: {c.type} ({c.storage})\nID: {c.identifier}" + (
                    f"\nEncoding: {c.encoding}" if c.encoding else ""
                )
        elif role == Qt.ItemDataRole.DisplayRole and section < len(self._indices):
            return str(self._indices[section])
        return None

    def data(self, index: QModelIndex | QPersistentModelIndex, role: int = Qt.ItemDataRole.DisplayRole) -> Any:
        if not index.isValid():
            return None
        r, c = index.row(), index.column()
        if role == Qt.ItemDataRole.DisplayRole:
            return self.display_text(r, c)
        if role == Qt.ItemDataRole.ToolTipRole:
            name = self.column_names[c]
            val = self._rows[r].get(name)
            if val is None:
                return None
            text = display_value(val, self.column_types.get(name), 2000)
            return text if len(text) > 40 else None
        if role == RAW_ROLE:
            return self._rows[r].get(self.column_names[c])
        if role == ROW_INDEX_ROLE:
            return self._indices[r]
        if role == Qt.ItemDataRole.TextAlignmentRole:
            if self.columns[c].type in _NUMERIC_TYPES and self.columns[c].type != "DateTime":
                return int(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            return int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        if role == Qt.ItemDataRole.ForegroundRole and self._rows[r].get(self.column_names[c]) is None:
            return self._dim
        return None

    # ---- helpers ------------------------------------------------------ #
    def display_text(self, r: int, c: int) -> str:
        cache = self._display[r]
        text = cache.get(c)
        if text is None:
            name = self.column_names[c]
            val = self._rows[r].get(name)
            text = "" if val is None else display_value(val, self.column_types.get(name), 300)
            cache[c] = text
        return text

    def append_rows(self, indices: list[int], rows: list[dict[str, Any]]) -> set[int]:
        """Append a batch; returns the set of column positions seen for the first time."""
        if not rows:
            return set()
        first = len(self._rows)
        new_cols: set[int] = set()
        for name_set in (row.keys() for row in rows):
            for name in name_set:
                ci = self._col_index.get(name)
                if ci is not None and ci not in self.seen_columns:
                    self.seen_columns.add(ci)
                    new_cols.add(ci)
        self.beginInsertRows(QModelIndex(), first, first + len(rows) - 1)
        self._rows.extend(rows)
        self._indices.extend(indices)
        self._display.extend({} for _ in rows)
        for offset, idx in enumerate(indices):
            self._index_to_row[idx] = first + offset
        self.endInsertRows()
        return new_cols

    def clear(self) -> None:
        self.beginResetModel()
        self._rows.clear()
        self._indices.clear()
        self._display.clear()
        self._index_to_row.clear()
        self.seen_columns.clear()
        self.endResetModel()

    def raw_row(self, r: int) -> dict[str, Any]:
        return self._rows[r]

    def row_index(self, r: int) -> int:
        return self._indices[r]

    def model_row_for_index(self, table_index: int) -> int | None:
        return self._index_to_row.get(table_index)

    def column_position(self, name: str) -> int | None:
        return self._col_index.get(name)


class RecordFilterProxy(QSortFilterProxyModel):
    """Substring / regex filter over displayed text with type-aware sorting."""

    def __init__(self, parent: Any = None) -> None:
        super().__init__(parent)
        self._text = ""
        self._regex: re.Pattern[str] | None = None
        self._case = False
        self._column_only: int | None = None
        self.setDynamicSortFilter(False)

    def set_filter(
        self, text: str, regex: bool = False, case_sensitive: bool = False, column: int | None = None
    ) -> None:
        self._text = text
        self._case = case_sensitive
        self._column_only = column
        self._regex = None
        if text and regex:
            try:
                self._regex = re.compile(text, 0 if case_sensitive else re.IGNORECASE)
            except re.error:
                self._regex = None
        self.invalidateFilter()

    def filterAcceptsRow(self, source_row: int, source_parent: QModelIndex | QPersistentModelIndex) -> bool:
        if not self._text:
            return True
        model: RecordTableModel = self.sourceModel()  # type: ignore[assignment]
        cols = [self._column_only] if self._column_only is not None else sorted(model.seen_columns)
        needle = self._text if self._case else self._text.lower()
        for c in cols:
            text = model.display_text(source_row, c)
            if not text:
                continue
            if self._regex is not None:
                if self._regex.search(text):
                    return True
            elif (needle in text) if self._case else (needle in text.lower()):
                return True
        return False

    def lessThan(self, left: QModelIndex | QPersistentModelIndex, right: QModelIndex | QPersistentModelIndex) -> bool:
        a = left.data(RAW_ROLE)
        b = right.data(RAW_ROLE)
        if a is None:
            return False
        if b is None:
            return True
        return _sort_key(a) < _sort_key(b)


def _sort_key(v: Any) -> tuple[int, Any]:
    if isinstance(v, bool):
        return (0, int(v))
    if isinstance(v, int | float):
        return (0, v)
    if isinstance(v, datetime):
        return (1, v.timestamp())
    if isinstance(v, bytes):
        return (2, v)
    if isinstance(v, list):
        return (3, str(v))
    return (4, str(v).casefold())


def display_datetime_sort_value(v: Any) -> Any:
    d = decode_ese_datetime(v) if isinstance(v, int) else v
    return d if isinstance(d, datetime) else v


class DatabaseTreeModel(QStandardItemModel):
    """Databases -> tables tree."""

    def __init__(self, parent: Any = None) -> None:
        super().__init__(parent)
        self.setHorizontalHeaderLabels(["Name", "Rows", "Cols"])
        self._db_items: dict[str, QStandardItem] = {}

    def add_database(self, db: EdbDatabase, icon: Any = None) -> QStandardItem:
        item = QStandardItem(db.path.name)
        item.setEditable(False)
        item.setToolTip(
            f"{db.path}\n{db.info.kind_name}\n{db.profile.name}\n{db.info.table_count} tables · {db.info.size_bytes:,} bytes"
        )
        item.setData("db", KIND_ROLE)
        item.setData(db.id, DB_ID_ROLE)
        if icon is not None:
            item.setIcon(icon)
        sub = QStandardItem("")
        sub.setEditable(False)
        cols = QStandardItem(str(db.info.table_count))
        cols.setEditable(False)
        cols.setToolTip(f"{db.info.table_count} tables")
        cols.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self.appendRow([item, sub, cols])
        try:
            from edb_explorer.core.exchange import is_exchange_database

            exchange = is_exchange_database(db)
        except Exception:
            exchange = False
        if exchange:
            from edb_explorer.gui.icons import kind_icon

            mb = QStandardItem("Mailboxes (Exchange viewer)")
            mb.setEditable(False)
            mb.setIcon(kind_icon("mailbox"))
            mb.setData("mailboxes", KIND_ROLE)
            mb.setData(db.id, DB_ID_ROLE)
            mb.setToolTip("Browse mailboxes, folders and messages like a mail client - double-click to open")
            mb.setForeground(QColor("#f6ad55"))
            item.appendRow([mb, QStandardItem(""), QStandardItem("")])
        if db.profile.views:
            analysis = QStandardItem("Analysis views")
            analysis.setEditable(False)
            analysis.setData("views", KIND_ROLE)
            analysis.setData(db.id, DB_ID_ROLE)
            analysis.setToolTip("Ready-made queries for this database type - double-click to run")
            analysis.setForeground(QColor("#6ea8fe"))
            for v in db.profile.views:
                vi = QStandardItem(v.name)
                vi.setEditable(False)
                vi.setData("view", KIND_ROLE)
                vi.setData(db.id, DB_ID_ROLE)
                vi.setData(v.id, VIEW_ROLE)
                vi.setToolTip(f"{v.description}\n\n{v.sql}")
                vi.setForeground(QColor("#6ea8fe"))
                analysis.appendRow([vi, QStandardItem(""), QStandardItem("")])
            item.appendRow([analysis, QStandardItem(""), QStandardItem("")])
        for t in db.tables():
            item.appendRow(self._table_row(db.id, t))
        self._db_items[db.id] = item
        return item

    def _table_row(self, db_id: str, t: TableInfo) -> list[QStandardItem]:
        name = QStandardItem(t.display_name)
        name.setEditable(False)
        name.setData("table", KIND_ROLE)
        name.setData(db_id, DB_ID_ROLE)
        name.setData(t.name, TABLE_ROLE)
        tip = t.name if t.display_name == t.name else f"{t.name}\n{t.display_name}"
        if t.description:
            tip += f"\n\n{t.description}"
        name.setToolTip(tip)
        if t.is_system:
            name.setForeground(QColor("#8a9099"))
        rows = QStandardItem("" if t.record_count is None else f"{t.record_count:,}")
        rows.setEditable(False)
        rows.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        cols = QStandardItem(str(len(t.columns)))
        cols.setEditable(False)
        cols.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        return [name, rows, cols]

    def remove_database(self, db_id: str) -> None:
        item = self._db_items.pop(db_id, None)
        if item is not None:
            self.removeRow(item.row())

    def update_counts(self, db: EdbDatabase) -> None:
        item = self._db_items.get(db.id)
        if item is None:
            return
        for r in range(item.rowCount()):
            name_item = item.child(r, 0)
            if name_item.data(KIND_ROLE) != "table":
                continue
            count = db.cached_count(name_item.data(TABLE_ROLE))
            if count is not None:
                item.child(r, 1).setText(f"{count:,}")

    def database_item(self, db_id: str) -> QStandardItem | None:
        return self._db_items.get(db_id)
