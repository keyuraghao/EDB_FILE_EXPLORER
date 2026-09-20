"""Welcome page shown when no table is open: three big actions + recent files."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from edb_explorer import __app_name__, __version__
from edb_explorer.gui.icons import icon


class _BigButton(QPushButton):
    """A large clickable card: icon on top, bold title, dim subtitle."""

    def __init__(self, title: str, subtitle: str, glyph: str) -> None:
        super().__init__()
        self.setMinimumSize(240, 150)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setStyleSheet(
            "QPushButton { border: 1px solid palette(mid); border-radius: 12px; background: palette(button); padding: 12px; }"
            "QPushButton:hover { border: 2px solid #3d7bd9; background: palette(alternate-base); }"
            "QPushButton:pressed { background: palette(base); }"
            "QPushButton:disabled { color: palette(mid); }"
        )
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 14, 12, 14)
        layout.setSpacing(6)
        icon_label = QLabel()
        icon_label.setPixmap(icon(glyph).pixmap(40, 40))
        icon_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        title_label = QLabel(title)
        tf = QFont()
        tf.setPointSize(13)
        tf.setBold(True)
        title_label.setFont(tf)
        title_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        sub_label = QLabel(subtitle)
        sub_label.setObjectName("dim")
        sub_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        sub_label.setWordWrap(True)
        for lbl in (icon_label, title_label, sub_label):
            lbl.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
            layout.addWidget(lbl)


class WelcomePage(QWidget):
    open_files = Signal()
    open_recent = Signal(str)
    scan_folder = Signal()
    clear_recent = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(40, 30, 40, 30)
        outer.addStretch(1)
        title = QLabel(f"{__app_name__}")
        f = QFont()
        f.setPointSize(24)
        f.setBold(True)
        title.setFont(f)
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        outer.addWidget(title)
        sub = QLabel(
            f"v{__version__}  ·  ESE · SQLite · LevelDB · Access · DBF · Berkeley DB · SQL & BSON dumps\n"
            "Windows, macOS, iOS, Android, Linux and server databases - read-only, with SQL analysis and reports"
        )
        sub.setObjectName("dim")
        sub.setAlignment(Qt.AlignmentFlag.AlignCenter)
        outer.addWidget(sub)
        outer.addSpacing(24)

        row = QHBoxLayout()
        row.setSpacing(18)
        self.btn_open = _BigButton("Open files", "Import one or more database files", "open")
        self.btn_recent = _BigButton("Open recent", "Pick from recently opened files", "history")
        self.btn_scan = _BigButton("Scan folder", "Find every database in a folder or image", "scan")
        self.btn_open.clicked.connect(self.open_files)
        self.btn_recent.clicked.connect(self._toggle_recent)
        self.btn_scan.clicked.connect(self.scan_folder)
        for b in (self.btn_open, self.btn_recent, self.btn_scan):
            row.addWidget(b)
        outer.addLayout(row)
        outer.addSpacing(12)

        self.recent_box = QWidget()
        rl = QVBoxLayout(self.recent_box)
        rl.setContentsMargins(0, 0, 0, 0)
        head = QHBoxLayout()
        head.addWidget(QLabel("Recent files - double-click to open"))
        head.addStretch(1)
        clear = QPushButton("Clear list")
        clear.setFlat(True)
        clear.clicked.connect(self.clear_recent)
        head.addWidget(clear)
        rl.addLayout(head)
        self.recent_list = QListWidget()
        self.recent_list.setAlternatingRowColors(True)
        self.recent_list.itemActivated.connect(lambda it: self.open_recent.emit(it.data(Qt.ItemDataRole.UserRole)))
        self.recent_list.setMaximumHeight(220)
        rl.addWidget(self.recent_list)
        self.recent_box.hide()
        outer.addWidget(self.recent_box)

        hint = QLabel("Tip: you can also drag files or folders anywhere into this window.")
        hint.setObjectName("dim")
        hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        outer.addSpacing(8)
        outer.addWidget(hint)
        outer.addStretch(2)

    def set_recent(self, paths: list[str]) -> None:
        self.recent_list.clear()
        for p in paths:
            path = Path(p)
            item = QListWidgetItem(f"{path.name}    —    {path.parent}")
            item.setData(Qt.ItemDataRole.UserRole, p)
            item.setToolTip(p)
            if not path.exists():
                item.setForeground(Qt.GlobalColor.gray)
                item.setToolTip(p + "\n(no longer exists)")
            self.recent_list.addItem(item)
        self.btn_recent.setEnabled(bool(paths))
        if not paths:
            self.recent_box.hide()

    def _toggle_recent(self) -> None:
        self.recent_box.setVisible(not self.recent_box.isVisible())
