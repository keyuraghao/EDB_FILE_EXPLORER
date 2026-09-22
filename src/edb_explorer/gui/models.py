"""Qt item models: an append-only record model, a type-aware sort/filter proxy and the database tree."""

from __future__ import annotations

import re
from array import array
from bisect import bisect_left
from collections import OrderedDict
from datetime import datetime
from typing import Any

from PySide6.QtCore import QAbstractTableModel, QModelIndex, QPersistentModelIndex, QSortFilterProxyModel, Qt, Signal
from PySide6.QtGui import QColor, QStandardItem, QStandardItemModel

from edb_explorer.core import ColumnInfo, EdbDatabase, TableInfo
from edb_explorer.core.rowstore import DiskRowStore, StoreRows
from edb_explorer.core.values import decode_ese_datetime, display_value

RAW_ROLE = Qt.ItemDataRole.UserRole + 1
ROW_INDEX_ROLE = Qt.ItemDataRole.UserRole + 2

KIND_ROLE = Qt.ItemDataRole.UserRole + 10
DB_ID_ROLE = Qt.ItemDataRole.UserRole + 11
TABLE_ROLE = Qt.ItemDataRole.UserRole + 12
VIEW_ROLE = Qt.ItemDataRole.UserRole + 13
FOLDER_ROLE = Qt.ItemDataRole.UserRole + 14

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
    """Holds the decoded rows of one table.  Rows are appended in batches by :class:`RecordLoader`.

    Rows stay as the sparse dicts the loader produces (wide ESE tables have thousands of columns of which a
    handful are set per row).  Display strings are only cached for values that are expensive to render
    (blobs, timestamps, lists, long text); numbers and plain strings are rendered on the fly.

    Two backings: rows live in memory until the loader spills the table into a :class:`DiskRowStore`
    (``attach_store``); from then on the model serves windows of rows from an LRU page cache over that
    store and sorting/filtering happen on disk through *views* (``set_view``), so a table of any size
    costs a bounded amount of memory.
    """

    PAGE = 256
    CACHED_PAGES = 256  # ~65k rows

    #: Disk mode only: the view wants a sort (column, descending) - the tab builds it in the background.
    sort_requested = Signal(int, bool)

    def __init__(self, columns: tuple[ColumnInfo, ...], parent: Any = None) -> None:
        super().__init__(parent)
        self.store: DiskRowStore | None = None
        self.view_id: int | None = None
        self._view_count = 0
        self._pages: OrderedDict[int, list[list[Any]]] = OrderedDict()  # page -> [pos, idx, row, display cache]
        self.columns = list(columns)
        self.column_names = [c.name for c in self.columns]
        self.column_types = {c.name: c.type for c in self.columns}
        self._col_index = {c.name: i for i, c in enumerate(self.columns)}
        # numbers in these columns display as str(value): no decoding, so nothing worth caching
        self._plain_number = [c.type != "DateTime" and not c.type.startswith("ts:") for c in self.columns]
        self._rows: list[dict[str, Any]] = []
        self._indices = array("q")
        self._display: list[dict[int, str] | None] = []
        # Row indices arrive in increasing order (storage order), so lookups can bisect; a dict is only
        # built if a caller ever appends out-of-order indices.
        self._index_to_row: dict[int, int] | None = None
        self.seen_columns: set[int] = set()
        self.seen_sorted: tuple[int, ...] = ()
        self._dim = QColor("#8a9099")

    # ---- Qt API ------------------------------------------------------- #
    def rowCount(self, parent: QModelIndex | QPersistentModelIndex = QModelIndex()) -> int:  # noqa: B008
        if parent.isValid():
            return 0
        return self._view_count if self.store is not None else len(self._rows)

    @property
    def disk(self) -> bool:
        """True once the rows live in a :class:`DiskRowStore` instead of memory."""
        return self.store is not None

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
        elif role == Qt.ItemDataRole.DisplayRole and section < self.rowCount():
            return str(self.row_index(section))
        return None

    def data(self, index: QModelIndex | QPersistentModelIndex, role: int = Qt.ItemDataRole.DisplayRole) -> Any:
        if not index.isValid():
            return None
        r, c = index.row(), index.column()
        if role == Qt.ItemDataRole.DisplayRole:
            return self.display_text(r, c)
        if role == Qt.ItemDataRole.ToolTipRole:
            name = self.column_names[c]
            val = self.raw_row(r).get(name)
            if val is None:
                return None
            text = display_value(val, self.column_types.get(name), 2000)
            return text if len(text) > 40 else None
        if role == RAW_ROLE:
            return self.raw_row(r).get(self.column_names[c])
        if role == ROW_INDEX_ROLE:
            return self.row_index(r)
        if role == Qt.ItemDataRole.TextAlignmentRole:
            if self.columns[c].type in _NUMERIC_TYPES and self.columns[c].type != "DateTime":
                return int(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            return int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        if role == Qt.ItemDataRole.ForegroundRole and self.raw_row(r).get(self.column_names[c]) is None:
            return self._dim
        return None

    def sort(self, column: int, order: Qt.SortOrder = Qt.SortOrder.AscendingOrder) -> None:
        # Only reached in disk mode (the view talks to the proxy while rows are in memory).
        if self.store is not None:
            self.sort_requested.emit(column, order == Qt.SortOrder.DescendingOrder)

    # ---- helpers ------------------------------------------------------ #
    def display_text(self, r: int, c: int) -> str:
        if self.store is not None:
            entry = self._entry(r)
            cache = entry[3]
            row = entry[2]
        else:
            cache = self._display[r]
            row = self._rows[r]
        if cache is not None:
            text = cache.get(c)
            if text is not None:
                return text
        name = self.column_names[c]
        val = row.get(name)
        if val is None:
            return ""
        if type(val) is str:
            if len(val) <= 300 and "\r" not in val and "\n" not in val:
                return val  # exactly what display_value would return
        elif self._plain_number[c] and isinstance(val, int | float):
            return str(val)
        text = display_value(val, self.column_types.get(name), 300)
        if cache is None:
            cache = {}
            if self.store is not None:
                entry[3] = cache
            else:
                self._display[r] = cache
        cache[c] = text
        return text

    # ---- disk mode ---------------------------------------------------- #
    def _entry(self, r: int) -> list[Any]:
        """``[pos, idx, row, display-cache]`` of view row ``r`` (fetches and caches its page)."""
        page = r // self.PAGE
        entries = self._pages.get(page)
        if entries is None:
            assert self.store is not None
            start = page * self.PAGE
            fetched = self.store.fetch(self.view_id, start, min(start + self.PAGE, self._view_count))
            entries = [[pos, idx, row, None] for pos, idx, row in fetched]
            self._pages[page] = entries
            if len(self._pages) > self.CACHED_PAGES:
                self._pages.popitem(last=False)
        else:
            self._pages.move_to_end(page)
        i = r - page * self.PAGE
        if i < len(entries):
            return entries[i]
        return [r, r, {}, None]  # a page shorter than announced (should not happen): render blank

    def attach_store(self, store: DiskRowStore) -> None:
        """Switch to disk mode: ``store`` already holds every row shown so far, in the same order."""
        self.beginResetModel()
        self._rows.clear()
        del self._indices[:]
        self._display.clear()
        self._index_to_row = None
        self.store = store
        self.view_id = None
        self._view_count = store.count
        self._pages.clear()
        self.seen_sorted = store.seen_sorted
        self.seen_columns = set(self.seen_sorted)
        self.endResetModel()

    def store_grew(self, total: int, new_cols: set[int]) -> set[int]:
        """The loader appended rows to the store (now ``total``); returns the columns seen for the first time."""
        assert self.store is not None
        fresh = set(new_cols) - self.seen_columns
        if fresh:
            self.seen_columns |= fresh
            self.seen_sorted = tuple(sorted(self.seen_columns))
        if self.view_id is None and total > self._view_count:
            first = self._view_count
            self.beginInsertRows(QModelIndex(), first, total - 1)
            self._view_count = total
            # the last page may have been fetched while partial
            self._pages.pop(first // self.PAGE, None)
            self.endInsertRows()
        return fresh

    def set_view(self, view_id: int | None) -> None:
        """Show the rows of a store view (``None`` = storage order); the previous view is dropped."""
        assert self.store is not None
        old = self.view_id
        self.beginResetModel()
        self.view_id = view_id
        self._view_count = self.store.view_count(view_id)
        self._pages.clear()
        self.endResetModel()
        if old is not None and old != view_id:
            self.store.drop_view(old)

    def store_rows(self, columns: list[str] | None = None) -> StoreRows:
        assert self.store is not None
        return self.store.rows(self.view_id, columns)

    def append_rows(self, indices: list[int], rows: list[dict[str, Any]]) -> set[int]:
        """Append a batch; returns the set of column positions seen for the first time."""
        if not rows:
            return set()
        first = len(self._rows)
        new_cols: set[int] = set()
        col_index = self._col_index
        seen = self.seen_columns
        for row in rows:
            for name in row:
                ci = col_index.get(name)
                if ci is not None and ci not in seen:
                    seen.add(ci)
                    new_cols.add(ci)
        if new_cols:
            self.seen_sorted = tuple(sorted(seen))
        if self._index_to_row is None:
            last = self._indices[-1] if self._indices else -1
            for idx in indices:
                if idx <= last:
                    self._index_to_row = {i: pos for pos, i in enumerate(self._indices)}
                    break
                last = idx
        self.beginInsertRows(QModelIndex(), first, first + len(rows) - 1)
        self._rows.extend(rows)
        self._indices.extend(indices)
        self._display.extend([None] * len(rows))
        if self._index_to_row is not None:
            for offset, idx in enumerate(indices):
                self._index_to_row[idx] = first + offset
        self.endInsertRows()
        return new_cols

    def clear(self) -> None:
        self.beginResetModel()
        self._rows.clear()
        del self._indices[:]
        self._display.clear()
        self._index_to_row = None
        self.seen_columns.clear()
        self.seen_sorted = ()
        self._pages.clear()
        self._view_count = 0
        self.view_id = None
        store, self.store = self.store, None
        self.endResetModel()
        if store is not None:
            store.close()

    def raw_row(self, r: int) -> dict[str, Any]:
        return self._entry(r)[2] if self.store is not None else self._rows[r]

    def row_index(self, r: int) -> int:
        return self._entry(r)[1] if self.store is not None else self._indices[r]

    def model_row_for_index(self, table_index: int) -> int | None:
        if self.store is not None:
            pos = self.store.position_of_index(table_index)
            return None if pos is None else self.store.view_row_for_position(self.view_id, pos)
        if self._index_to_row is not None:
            return self._index_to_row.get(table_index)
        i = bisect_left(self._indices, table_index)
        return i if i < len(self._indices) and self._indices[i] == table_index else None

    def column_position(self, name: str) -> int | None:
        return self._col_index.get(name)


class RecordFilterProxy(QSortFilterProxyModel):
    """Substring / regex filter over displayed text with type-aware sorting."""

    def __init__(self, parent: Any = None) -> None:
        super().__init__(parent)
        self._text = ""
        self._needle = ""
        self._regex: re.Pattern[str] | None = None
        self._case = False
        self._column_only: int | None = None
        self.setDynamicSortFilter(False)

    def set_filter(
        self, text: str, regex: bool = False, case_sensitive: bool = False, column: int | None = None
    ) -> None:
        self._text = text
        self._needle = text if case_sensitive else text.lower()
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
        cols = (self._column_only,) if self._column_only is not None else model.seen_sorted
        needle, regex, case, display = self._needle, self._regex, self._case, model.display_text
        for c in cols:
            text = display(source_row, c)
            if not text:
                continue
            if regex is not None:
                if regex.search(text):
                    return True
            elif (needle in text) if case else (needle in text.lower()):
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
        self._folder_items: dict[str, QStandardItem] = {}  # parent directory -> folder row

    def folder_item(self, directory: str) -> QStandardItem:
        """The top-level row for ``directory`` (created on first use); databases are grouped under it."""
        item = self._folder_items.get(directory)
        if item is not None:
            return item
        from edb_explorer.gui.icons import icon as glyph

        item = QStandardItem(self._folder_label(directory))
        item.setEditable(False)
        item.setToolTip(directory)
        item.setData("folder", KIND_ROLE)
        item.setData(directory, FOLDER_ROLE)
        item.setIcon(glyph("folder"))
        item.setForeground(QColor("#8a9099"))
        sub, cnt = QStandardItem(""), QStandardItem("")
        sub.setEditable(False)
        cnt.setEditable(False)
        self.appendRow([item, sub, cnt])
        self._folder_items[directory] = item
        # two folders with the same name are told apart by their parent
        for other_dir, other in self._folder_items.items():
            if other is not item and other.text() == item.text():
                other.setText(self._folder_label(other_dir, 2))
                item.setText(self._folder_label(directory, 2))
        return item

    @staticmethod
    def _folder_label(directory: str, parts: int = 1) -> str:
        from pathlib import PurePath

        tail = PurePath(directory).parts[-parts:]
        return "/".join(tail) if tail else directory

    def add_database(self, db: EdbDatabase, icon: Any = None) -> QStandardItem:
        folder = self.folder_item(str(db.path.parent))
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
        folder.appendRow([item, sub, cols])
        self._refresh_folder_count(folder)
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

    def _refresh_folder_count(self, folder: QStandardItem) -> None:
        n = folder.rowCount()
        cell = self.item(folder.row(), 2)
        if cell is not None:
            cell.setText(str(n))
            cell.setToolTip(f"{n} database{'s' if n != 1 else ''} in this folder")
            cell.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)

    def remove_database(self, db_id: str) -> None:
        item = self._db_items.pop(db_id, None)
        if item is None:
            return
        folder = item.parent()
        if folder is None:
            self.removeRow(item.row())
            return
        folder.removeRow(item.row())
        if folder.rowCount() == 0:
            self._folder_items.pop(folder.data(FOLDER_ROLE), None)
            self.removeRow(folder.row())
        else:
            self._refresh_folder_count(folder)

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
