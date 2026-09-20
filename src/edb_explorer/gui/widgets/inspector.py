"""Record inspector: every column of the selected row, with hex and timestamp interpretations."""

from __future__ import annotations

import struct
import uuid
from datetime import datetime
from typing import Any

from PySide6.QtCore import Qt
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QPushButton,
    QSplitter,
    QTabWidget,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from edb_explorer.core import ColumnInfo
from edb_explorer.core.values import (
    decode_bytes,
    decode_ese_datetime,
    display_value,
    hexdump,
    interpret_timestamp,
    sid_from_bytes,
)


def _mono() -> QFont:
    f = QFont("Monospace")
    f.setStyleHint(QFont.StyleHint.Monospace)
    return f


class RecordInspector(QWidget):
    def __init__(self, parent: Any = None) -> None:
        super().__init__(parent)
        self._columns: dict[str, ColumnInfo] = {}
        self._row: dict[str, Any] = {}

        layout = QVBoxLayout(self)
        layout.setContentsMargins(6, 6, 6, 6)
        head = QHBoxLayout()
        self.title = QLabel("No record selected")
        self.title.setObjectName("dim")
        head.addWidget(self.title, 1)
        self.show_nulls = QCheckBox("Show empty columns")
        self.show_nulls.toggled.connect(self._rebuild)
        head.addWidget(self.show_nulls)
        self.filter = QLineEdit()
        self.filter.setPlaceholderText("Filter columns")
        self.filter.setClearButtonEnabled(True)
        self.filter.setMaximumWidth(200)
        self.filter.textChanged.connect(self._rebuild)
        head.addWidget(self.filter)
        layout.addLayout(head)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(["Column", "Type", "Value"])
        self.tree.setRootIsDecorated(False)
        self.tree.setAlternatingRowColors(True)
        self.tree.setColumnWidth(0, 200)
        self.tree.setColumnWidth(1, 90)
        self.tree.currentItemChanged.connect(self._show_detail)
        splitter.addWidget(self.tree)

        self.tabs = QTabWidget()
        self.value_edit = QPlainTextEdit()
        self.value_edit.setReadOnly(True)
        self.value_edit.setObjectName("mono")
        self.value_edit.setFont(_mono())
        self.hex_edit = QPlainTextEdit()
        self.hex_edit.setReadOnly(True)
        self.hex_edit.setObjectName("mono")
        self.hex_edit.setFont(_mono())
        self.hex_edit.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        self.interp_edit = QPlainTextEdit()
        self.interp_edit.setReadOnly(True)
        self.interp_edit.setObjectName("mono")
        self.interp_edit.setFont(_mono())
        self.tabs.addTab(self.value_edit, "Value")
        self.tabs.addTab(self.hex_edit, "Hex")
        self.tabs.addTab(self.interp_edit, "Interpretations")
        right = QWidget()
        rl = QVBoxLayout(right)
        rl.setContentsMargins(0, 0, 0, 0)
        rl.addWidget(self.tabs, 1)
        btns = QHBoxLayout()
        btns.addStretch(1)
        copy_val = QPushButton("Copy value")
        copy_val.clicked.connect(lambda: QApplication.clipboard().setText(self.value_edit.toPlainText()))
        copy_hex = QPushButton("Copy hex")
        copy_hex.clicked.connect(self._copy_hex)
        save = QPushButton("Extract value to file…")
        save.setToolTip("Save the raw bytes (binary columns) or the text of this value to a file")
        save.clicked.connect(self._save_value)
        btns.addWidget(copy_val)
        btns.addWidget(copy_hex)
        btns.addWidget(save)
        rl.addLayout(btns)
        splitter.addWidget(right)
        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 4)
        layout.addWidget(splitter, 1)
        self._current_bytes: bytes | None = None

    # ------------------------------------------------------------------ #
    def show_record(
        self, db_id: str, table: str, row_index: int, row: dict[str, Any], columns: tuple[ColumnInfo, ...]
    ) -> None:
        self._columns = {c.name: c for c in columns}
        self._row = row
        self.title.setText(
            f"{db_id}  ›  {table}  ›  row {row_index}   ({sum(1 for v in row.values() if v is not None)} values)"
        )
        self._rebuild()

    def clear(self) -> None:
        self._row = {}
        self._columns = {}
        self.title.setText("No record selected")
        self.tree.clear()
        self.value_edit.clear()
        self.hex_edit.clear()
        self.interp_edit.clear()

    def _rebuild(self) -> None:
        self.tree.clear()
        needle = self.filter.text().lower()
        show_nulls = self.show_nulls.isChecked()
        names = list(self._columns) if show_nulls else [n for n in self._columns if self._row.get(n) is not None]
        for name in names:
            if needle and needle not in name.lower():
                continue
            col = self._columns[name]
            val = self._row.get(name)
            item = QTreeWidgetItem([name, col.type, display_value(val, col.type, 300)])
            item.setData(0, Qt.ItemDataRole.UserRole, name)
            if val is None:
                item.setForeground(2, Qt.GlobalColor.gray)
            self.tree.addTopLevelItem(item)
        if self.tree.topLevelItemCount():
            self.tree.setCurrentItem(self.tree.topLevelItem(0))

    def _show_detail(self, item: QTreeWidgetItem | None, _prev: QTreeWidgetItem | None = None) -> None:
        self._current_bytes = None
        if item is None:
            return
        name = item.data(0, Qt.ItemDataRole.UserRole)
        col = self._columns.get(name)
        val = self._row.get(name)
        ctype = col.type if col else None
        self.value_edit.setPlainText(_full_text(val, ctype))
        if isinstance(val, bytes | bytearray | memoryview):
            data = bytes(val)
            self._current_bytes = data
            self.hex_edit.setPlainText(hexdump(data, max_bytes=65536))
            self.interp_edit.setPlainText(_byte_interpretations(data))
        elif isinstance(val, int | float) and not isinstance(val, bool):
            self.hex_edit.setPlainText(_int_hex(val))
            self.interp_edit.setPlainText(_number_interpretations(val, ctype))
        elif isinstance(val, list):
            self.hex_edit.setPlainText(
                "\n\n".join(hexdump(bytes(v), max_bytes=4096) if isinstance(v, bytes) else str(v) for v in val)
            )
            self.interp_edit.setPlainText(f"Multi-value column with {len(val)} value(s)")
        else:
            self.hex_edit.setPlainText(hexdump(str(val).encode("utf-8"), max_bytes=65536) if val is not None else "")
            self.interp_edit.setPlainText("")

    def _save_value(self) -> None:
        from PySide6.QtWidgets import QFileDialog

        item = self.tree.currentItem()
        name = item.data(0, Qt.ItemDataRole.UserRole) if item else "value"
        if self._current_bytes is not None:
            path, _ = QFileDialog.getSaveFileName(
                self, "Extract raw bytes", f"{name}.bin", "Binary (*.bin);;All files (*)"
            )
            if path:
                with open(path, "wb") as fh:
                    fh.write(self._current_bytes)
        else:
            path, _ = QFileDialog.getSaveFileName(self, "Extract value", f"{name}.txt", "Text (*.txt);;All files (*)")
            if path:
                with open(path, "w", encoding="utf-8") as fh:
                    fh.write(self.value_edit.toPlainText())

    def _copy_hex(self) -> None:
        if self._current_bytes is not None:
            QApplication.clipboard().setText(self._current_bytes.hex())
        else:
            QApplication.clipboard().setText(self.hex_edit.toPlainText())


def _full_text(val: Any, ctype: str | None) -> str:
    if val is None:
        return ""
    if isinstance(val, bytes | bytearray | memoryview):
        smart = decode_bytes(bytes(val), "smart")
        return str(smart)
    if isinstance(val, list):
        return "\n".join(_full_text(v, ctype) for v in val)
    if ctype == "DateTime" and isinstance(val, int | float):
        d = decode_ese_datetime(val)
        return f"{d.isoformat()}   (raw {val})" if isinstance(d, datetime) else str(val)
    return str(val)


def _int_hex(val: int | float) -> str:
    if isinstance(val, float):
        return f"double: {val!r}\nbits (LE): {struct.pack('<d', val).hex()}"
    try:
        signed = struct.pack("<q", val).hex() if -(2**63) <= val < 2**63 else "n/a"
    except struct.error:
        signed = "n/a"
    return f"decimal: {val}\nhex: {val:#x}\nint64 LE bytes: {signed}"


def _number_interpretations(val: int | float, ctype: str | None) -> str:
    lines = []
    if ctype == "DateTime":
        d = decode_ese_datetime(val)
        lines.append(f"ESE DateTime -> {d.isoformat() if isinstance(d, datetime) else 'not decodable'}")
        lines.append("")
    info = interpret_timestamp(val)
    lines.append("Timestamp readings (plausible 1980-2100 only):")
    for k, v in info.items():
        if k in ("raw", "best_guess"):
            continue
        lines.append(f"  {k:<20} {v or '-'}")
    lines.append(f"\nbest guess: {info.get('best_guess') or '-'}")
    return "\n".join(lines)


def _byte_interpretations(data: bytes) -> str:
    lines = [f"length: {len(data)} bytes", ""]
    smart = decode_bytes(data, "smart")
    lines.append(f"smart:     {str(smart)[:500]}")
    for enc in ("utf-16-le", "utf-8", "latin-1"):
        try:
            text = data.decode(enc, "replace").rstrip("\x00")
            lines.append(f"{enc:<10} {text[:300]!r}")
        except Exception:
            pass
    sid = sid_from_bytes(data)
    if sid:
        lines.append(f"SID:       {sid}")
    if len(data) == 16:
        lines.append(f"GUID (LE): {{{uuid.UUID(bytes_le=data)}}}")
        lines.append(f"GUID (BE): {{{uuid.UUID(bytes=data)}}}")
    if len(data) in (4, 8):
        le = int.from_bytes(data, "little")
        be = int.from_bytes(data, "big")
        lines.append(f"int LE:    {le}")
        lines.append(f"int BE:    {be}")
        if len(data) == 8:
            lines.append(f"double LE: {struct.unpack('<d', data)[0]!r}")
        lines.append("")
        lines.append("Timestamp readings (little-endian):")
        for k, v in interpret_timestamp(data).items():
            if k not in ("raw", "best_guess") and v:
                lines.append(f"  {k:<20} {v}")
    return "\n".join(lines)
