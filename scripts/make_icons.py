"""Render the application icon to PNG / ICO files used by PyInstaller and the README.

Run:  python scripts/make_icons.py
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from PySide6.QtCore import QSize  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from edb_explorer.gui.icons import app_icon  # noqa: E402


def main() -> None:
    app = QApplication([])  # noqa: F841 - needed for QPixmap
    out = Path(__file__).resolve().parents[1] / "src" / "edb_explorer" / "resources"
    out.mkdir(parents=True, exist_ok=True)
    icon = app_icon(512)
    icon.pixmap(QSize(512, 512)).save(str(out / "icon.png"), "PNG")
    icon.pixmap(QSize(256, 256)).save(str(out / "icon_256.png"), "PNG")
    if not icon.pixmap(QSize(256, 256)).save(str(out / "icon.ico"), "ICO"):
        # Fallback for Qt builds without the ICO writer: assemble a multi-size ICO by hand.
        _write_ico([icon.pixmap(QSize(s, s)) for s in (16, 32, 48, 64, 128, 256)], out / "icon.ico")
    print(f"wrote icons to {out}")


def _write_ico(pixmaps: list, path: Path) -> None:
    import struct
    from io import BytesIO

    from PySide6.QtCore import QBuffer, QIODevice

    images = []
    for pm in pixmaps:
        buf = QBuffer()
        buf.open(QIODevice.OpenModeFlag.WriteOnly)
        pm.save(buf, "PNG")
        images.append((pm.width(), bytes(buf.data())))
    header = struct.pack("<HHH", 0, 1, len(images))
    offset = 6 + 16 * len(images)
    entries, blobs = BytesIO(), BytesIO()
    for size, data in images:
        entries.write(struct.pack("<BBBBHHII", size % 256, size % 256, 0, 0, 1, 32, len(data), offset))
        blobs.write(data)
        offset += len(data)
    path.write_bytes(header + entries.getvalue() + blobs.getvalue())


if __name__ == "__main__":
    main()
