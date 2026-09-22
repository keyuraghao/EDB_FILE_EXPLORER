"""Settings ▸ Preferences: the in-memory row budget (slider + field) and the disk cache location."""

from __future__ import annotations

import os
import shutil
import tempfile
from typing import Any

from PySide6.QtCore import QSettings, Qt
from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QSizePolicy,
    QSlider,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from edb_explorer.core import rowstore

#: Rows of a table kept as Python objects before the rest spills to the disk cache.
DEFAULT_MEMORY_ROWS = 250_000
MIN_MEMORY_ROWS = 10_000
MAX_MEMORY_ROWS = 5_000_000
#: Measured on Security.evtx / SRUM / ntds rows: ~2 KB of Python objects per row.
BYTES_PER_ROW = 2_048
_SLIDER_STEP = 10_000
_PRESETS = (50_000, 100_000, DEFAULT_MEMORY_ROWS, 500_000, 1_000_000, 2_000_000)

EXPLANATION = (
    "Every row of every table is always loaded, whatever the size of the file. This setting decides "
    "<b>how many rows of a table are kept in memory</b> as Python objects - the fastest way to browse, sort "
    "and filter.<br><br>"
    "When a table has more rows than this budget, everything loaded so far is moved into a temporary "
    "<b>SQLite cache on disk</b> and the remaining rows stream straight into it. The grid then reads windows of "
    "rows from the cache, and sorting and filtering run as SQL in the background - slower than in memory, but "
    "memory use stays flat no matter how many rows the table has. The status bar shows "
    "<i>(disk cache)</i> when a table is served this way.<br><br>"
    "Raise the budget if you have plenty of RAM and want big tables to stay in memory; lower it on a "
    "small machine or when opening many tables at once. Applies to tables opened from now on."
)


def memory_rows_setting(settings: QSettings) -> int:
    try:
        value = int(settings.value("memory_rows", DEFAULT_MEMORY_ROWS))
    except (TypeError, ValueError):
        value = DEFAULT_MEMORY_ROWS
    return max(MIN_MEMORY_ROWS, min(MAX_MEMORY_ROWS, value))


def cache_dir_setting(settings: QSettings) -> str:
    """Configured cache directory ('' = system temp / ``EDB_EXPLORER_CACHE_DIR``)."""
    return str(settings.value("cache_dir", "") or "")


def apply_cache_dir(path: str) -> None:
    rowstore.DEFAULT_CACHE_DIR = path or None


def _fmt_bytes(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:,.0f} {unit}" if unit in ("B", "KB") else f"{n:,.1f} {unit}"
        n /= 1024
    return f"{n:,.1f} TB"


class SettingsDialog(QDialog):
    """Edit and persist the preferences; ``memory_rows`` / ``cache_dir`` hold the accepted values."""

    def __init__(self, settings: QSettings, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.settings = settings
        self.memory_rows = memory_rows_setting(settings)
        self.cache_dir = cache_dir_setting(settings)
        self.setWindowTitle("Preferences")
        self.setMinimumWidth(760)
        layout = QVBoxLayout(self)
        layout.setSpacing(12)

        # ---- memory budget ------------------------------------------------ #
        box = QGroupBox("Rows kept in memory per table")
        bl = QVBoxLayout(box)
        bl.setSpacing(8)
        why = QLabel(EXPLANATION)
        why.setWordWrap(True)
        why.setTextFormat(Qt.TextFormat.RichText)
        why.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Minimum)
        bl.addWidget(why)

        row = QHBoxLayout()
        self.slider = QSlider(Qt.Orientation.Horizontal)
        self.slider.setRange(MIN_MEMORY_ROWS // _SLIDER_STEP, MAX_MEMORY_ROWS // _SLIDER_STEP)
        self.slider.setPageStep(10)
        self.slider.setTickPosition(QSlider.TickPosition.TicksBelow)
        self.slider.setTickInterval(50)
        self.slider.setToolTip("Drag to change the budget (steps of 10,000 rows)")
        row.addWidget(self.slider, 1)
        self.spin = QSpinBox()
        self.spin.setRange(MIN_MEMORY_ROWS, MAX_MEMORY_ROWS)
        self.spin.setSingleStep(_SLIDER_STEP)
        self.spin.setGroupSeparatorShown(True)
        self.spin.setSuffix(" rows")
        self.spin.setMinimumWidth(150)
        self.spin.setToolTip(f"Exact value, {MIN_MEMORY_ROWS:,} - {MAX_MEMORY_ROWS:,} rows")
        row.addWidget(self.spin)
        bl.addLayout(row)

        scale = QHBoxLayout()
        lo = QLabel(f"{MIN_MEMORY_ROWS:,}")
        lo.setObjectName("dim")
        hi = QLabel(f"{MAX_MEMORY_ROWS:,}")
        hi.setObjectName("dim")
        scale.addWidget(lo)
        scale.addStretch(1)
        scale.addWidget(hi)
        bl.addLayout(scale)

        presets = QHBoxLayout()
        presets.addWidget(QLabel("Presets:"))
        for value in _PRESETS:
            label = f"{value // 1000}k" if value < 1_000_000 else f"{value // 1_000_000}M"
            if value == DEFAULT_MEMORY_ROWS:
                label += " (default)"
            btn = QPushButton(label)
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            btn.setMinimumWidth(btn.sizeHint().width())
            btn.setToolTip(f"Keep up to {value:,} rows per table in memory (~{_fmt_bytes(value * BYTES_PER_ROW)})")
            btn.clicked.connect(lambda _c=False, v=value: self.set_memory_rows(v))
            presets.addWidget(btn)
        presets.addStretch(1)
        self.reset_btn = QPushButton("Restore default")
        self.reset_btn.setMinimumWidth(self.reset_btn.sizeHint().width())
        self.reset_btn.clicked.connect(lambda: self.set_memory_rows(DEFAULT_MEMORY_ROWS))
        presets.addWidget(self.reset_btn)
        bl.addLayout(presets)

        self.estimate = QLabel()
        self.estimate.setWordWrap(True)
        self.estimate.setObjectName("dim")
        bl.addWidget(self.estimate)
        layout.addWidget(box)

        # ---- cache directory ---------------------------------------------- #
        cbox = QGroupBox("Disk cache location")
        cl = QVBoxLayout(cbox)
        cl.setSpacing(8)
        note = QLabel(
            "Where the temporary SQLite caches of tables that exceed the budget are written. They are deleted "
            "when the table tab or the application closes. Expect roughly the size of the source table "
            "(the row text is stored once more for filtering), so pick a drive with room to spare. "
            "Leave empty for the system temporary directory (or <code>EDB_EXPLORER_CACHE_DIR</code>)."
        )
        note.setWordWrap(True)
        note.setTextFormat(Qt.TextFormat.RichText)
        note.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Minimum)
        cl.addWidget(note)
        crow = QHBoxLayout()
        self.cache_edit = QLineEdit(self.cache_dir)
        self.cache_edit.setPlaceholderText(os.environ.get("EDB_EXPLORER_CACHE_DIR") or tempfile.gettempdir())
        self.cache_edit.setClearButtonEnabled(True)
        self.cache_edit.textChanged.connect(self._update_cache_info)
        crow.addWidget(self.cache_edit, 1)
        browse = QPushButton("Browse…")
        browse.clicked.connect(self._browse)
        crow.addWidget(browse)
        cl.addLayout(crow)
        self.cache_info = QLabel()
        self.cache_info.setObjectName("dim")
        self.cache_info.setWordWrap(True)
        cl.addWidget(self.cache_info)
        layout.addWidget(cbox)
        layout.addStretch(1)

        start = QGroupBox("Startup")
        sl = QVBoxLayout(start)
        self.restore_session = QCheckBox("Reopen the databases and tabs of the last session when the app starts")
        self.restore_session.setChecked(settings.value("restore_session", True, type=bool))
        self.restore_session.setToolTip(
            "Files given on the command line or dropped on the window win over the last session"
        )
        sl.addWidget(self.restore_session)
        layout.addWidget(start)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel,
            Qt.Orientation.Horizontal,
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        self.slider.valueChanged.connect(lambda v: self.set_memory_rows(v * _SLIDER_STEP))
        self.spin.valueChanged.connect(self.set_memory_rows)
        self.set_memory_rows(self.memory_rows)
        self._update_cache_info()
        self.adjustSize()

    # ------------------------------------------------------------------ #
    def set_memory_rows(self, value: int) -> None:
        value = max(MIN_MEMORY_ROWS, min(MAX_MEMORY_ROWS, int(value)))
        self.memory_rows = value
        for w in (self.slider, self.spin):
            w.blockSignals(True)
        self.slider.setValue(round(value / _SLIDER_STEP))
        self.spin.setValue(value)
        for w in (self.slider, self.spin):
            w.blockSignals(False)
        self.reset_btn.setEnabled(value != DEFAULT_MEMORY_ROWS)
        self.estimate.setText(
            f"Tables up to <b>{value:,} rows</b> stay in memory, using about <b>{_fmt_bytes(value * BYTES_PER_ROW)}</b> "
            f"of RAM each (~{BYTES_PER_ROW // 1024} KB per row). Bigger tables continue in the disk cache."
        )

    def _browse(self) -> None:
        start = self.cache_edit.text() or tempfile.gettempdir()
        path = QFileDialog.getExistingDirectory(self, "Choose the disk cache directory", start)
        if path:
            self.cache_edit.setText(path)

    def _update_cache_info(self) -> None:
        path = self.cache_edit.text().strip() or os.environ.get("EDB_EXPLORER_CACHE_DIR") or tempfile.gettempdir()
        try:
            usage = shutil.disk_usage(path)
        except OSError as exc:
            self.cache_info.setText(f"⚠ {path}: {exc.strerror or exc}")
            return
        self.cache_info.setText(
            f"{path} - {_fmt_bytes(usage.free)} free of {_fmt_bytes(usage.total)}"
            + ("" if os.access(path, os.W_OK) else "  ⚠ not writable")
        )

    def accept(self) -> None:
        self.cache_dir = self.cache_edit.text().strip()
        if self.cache_dir and not os.path.isdir(self.cache_dir):
            self.cache_info.setText(f"⚠ {self.cache_dir} is not a directory")
            return
        self.settings.setValue("memory_rows", self.memory_rows)
        self.settings.setValue("cache_dir", self.cache_dir)
        self.settings.setValue("restore_session", self.restore_session.isChecked())
        apply_cache_dir(self.cache_dir)
        super().accept()

    def values(self) -> dict[str, Any]:
        return {"memory_rows": self.memory_rows, "cache_dir": self.cache_dir}
