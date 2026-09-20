"""Icons: a procedurally painted application icon plus themed standard icons."""

from __future__ import annotations

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QFont, QIcon, QLinearGradient, QPainter, QPen, QPixmap
from PySide6.QtWidgets import QApplication, QStyle


def app_icon(size: int = 256) -> QIcon:
    """A database-cylinder glyph with an 'EDB' badge, rendered at several sizes."""
    icon = QIcon()
    for px in (16, 24, 32, 48, 64, 128, size):
        icon.addPixmap(_render(px))
    return icon


def _render(px: int) -> QPixmap:
    pm = QPixmap(px, px)
    pm.fill(Qt.GlobalColor.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    s = px / 256.0

    # rounded background tile
    grad = QLinearGradient(0, 0, px, px)
    grad.setColorAt(0.0, QColor("#2b6cb0"))
    grad.setColorAt(1.0, QColor("#1a365d"))
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(grad)
    p.drawRoundedRect(QRectF(0, 0, px, px), 48 * s, 48 * s)

    # cylinder
    body = QColor("#e2e8f0")
    shade = QColor("#a0aec0")
    p.setBrush(body)
    p.setPen(QPen(QColor("#1a202c"), max(1.0, 6 * s)))
    x, w = 56 * s, 144 * s
    top, bottom, eh = 60 * s, 176 * s, 28 * s
    p.drawRect(QRectF(x, top, w, bottom - top))
    p.setBrush(shade)
    p.drawEllipse(QRectF(x, bottom - eh / 2, w, eh))
    for y in (top + 38 * s, top + 76 * s):
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawArc(QRectF(x, y - eh / 2, w, eh), 180 * 16, 180 * 16)
    p.setBrush(QColor("#f7fafc"))
    p.drawEllipse(QRectF(x, top - eh / 2, w, eh))
    # hide the vertical rect edges crossing the top ellipse by redrawing sides
    p.setPen(QPen(QColor("#1a202c"), max(1.0, 6 * s)))
    p.drawLine(QPointF(x, top), QPointF(x, bottom))
    p.drawLine(QPointF(x + w, top), QPointF(x + w, bottom))

    if px >= 32:
        p.setPen(QColor("#1a202c"))
        f = QFont("Sans", int(max(6, 44 * s)), QFont.Weight.Black)
        p.setFont(f)
        p.drawText(QRectF(x, top + 58 * s, w, 60 * s), Qt.AlignmentFlag.AlignCenter, "EDB")
    p.end()
    return pm


def std(name: str) -> QIcon:
    """Fetch a QStyle standard icon by StandardPixmap name (e.g. 'SP_DialogOpenButton')."""
    style = QApplication.style()
    pixmap = getattr(QStyle.StandardPixmap, name)
    return style.standardIcon(pixmap)
