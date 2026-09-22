"""Icons: everything is painted with QPainter at runtime, so there are no image assets to ship.

* :func:`app_icon` - the database-cylinder application icon.
* :func:`kind_icon` - one coloured tile per database format (tree).
* :func:`icon` - the action icon set (toolbar, menus, buttons, welcome cards).  Every action has its own
  glyph and its own accent colour; the colours are mid-tone so the same pixmaps read well on the light
  and the dark theme.
* :func:`std` - Qt's stock pixmaps, kept for dialogs.
"""

from __future__ import annotations

from collections.abc import Callable

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QFont, QIcon, QLinearGradient, QPainter, QPainterPath, QPen, QPixmap
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


_KIND_STYLE = {
    "ese": ("#2b6cb0", "E"),
    "sqlite": ("#0e9f8f", "S"),
    "leveldb": ("#3f9142", "L"),
    "access": ("#b7332f", "A"),
    "dbf": ("#d97706", "D"),
    "bsddb": ("#6d28d9", "B"),
    "sqldump": ("#4b5563", "Q"),
    "bson": ("#1f7a3d", "M"),
    "evtx": ("#8a4b08", "W"),
    "mailbox": ("#c05621", "@"),
}
_kind_cache: dict[str, QIcon] = {}


def kind_icon(kind: str) -> QIcon:
    """A small coloured tile with a letter, one colour per database format."""
    if kind in _kind_cache:
        return _kind_cache[kind]
    color, letter = _KIND_STYLE.get(kind, ("#718096", "?"))
    icon = QIcon()
    for px in (16, 24, 32, 48):
        pm = QPixmap(px, px)
        pm.fill(Qt.GlobalColor.transparent)
        p = QPainter(pm)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(color))
        p.drawRoundedRect(QRectF(0.5, 0.5, px - 1, px - 1), px * 0.22, px * 0.22)
        p.setPen(QColor("#ffffff"))
        f = QFont("Sans", int(px * 0.55), QFont.Weight.Bold)
        p.setFont(f)
        p.drawText(QRectF(0, 0, px, px), Qt.AlignmentFlag.AlignCenter, letter)
        p.end()
        icon.addPixmap(pm)
    _kind_cache[kind] = icon
    return icon


def std(name: str) -> QIcon:
    """Fetch a QStyle standard icon by StandardPixmap name (e.g. 'SP_DialogOpenButton')."""
    style = QApplication.style()
    pixmap = getattr(QStyle.StandardPixmap, name)
    return style.standardIcon(pixmap)


# --------------------------------------------------------------------------- #
# Action icons.  Each glyph draws into a 24 x 24 unit canvas; the renderer scales it to every pixel size.
# --------------------------------------------------------------------------- #
_ICON_SIZES = (16, 20, 24, 32, 48, 64)
_WHITE = QColor("#ffffff")


def _pen(color: QColor | str, width: float = 1.9) -> QPen:
    pen = QPen(QColor(color), width)
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
    return pen


def _poly(*points: tuple[float, float], close: bool = True) -> QPainterPath:
    path = QPainterPath(QPointF(*points[0]))
    for x, y in points[1:]:
        path.lineTo(x, y)
    if close:
        path.closeSubpath()
    return path


def _folder(p: QPainter, fill: str, line: str) -> None:
    p.setPen(_pen(line))
    p.setBrush(QColor(fill))
    p.drawPath(_poly((3, 5.5), (9.5, 5.5), (11.5, 8), (21, 8), (21, 19.5), (3, 19.5)))
    p.drawLine(QPointF(3, 9.5), QPointF(21, 9.5))


def _magnifier(p: QPainter, color: str, cx: float, cy: float, r: float, halo: bool = False) -> None:
    if halo:
        p.setPen(_pen(_WHITE, 4.5))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawEllipse(QPointF(cx, cy), r, r)
        p.drawLine(QPointF(cx + r * 0.72, cy + r * 0.72), QPointF(cx + r * 1.7, cy + r * 1.7))
    p.setPen(_pen(color, 2.2))
    p.setBrush(QColor(255, 255, 255, 40))
    p.drawEllipse(QPointF(cx, cy), r, r)
    p.setPen(_pen(color, 2.8))
    p.drawLine(QPointF(cx + r * 0.72, cy + r * 0.72), QPointF(cx + r * 1.7, cy + r * 1.7))


def _g_open(p: QPainter) -> None:
    _folder(p, "#f0b64a", "#b07a12")


def _g_folder(p: QPainter) -> None:
    _folder(p, "#b8bec7", "#6e7781")


def _g_scan(p: QPainter) -> None:
    _folder(p, "#f0b64a", "#b07a12")
    _magnifier(p, "#2f6fd6", 14.5, 14, 3.6, halo=True)


def _g_search(p: QPainter) -> None:
    _magnifier(p, "#2f6fd6", 10, 10, 6.2)


def _g_sql(p: QPainter) -> None:
    p.setPen(_pen("#0b7a6e", 1.6))
    p.setBrush(QColor("#12a394"))
    p.drawRoundedRect(QRectF(2.5, 4, 19, 16), 2.5, 2.5)
    p.setPen(_pen(_WHITE, 2.0))
    p.drawPolyline([QPointF(6.5, 9), QPointF(10, 12), QPointF(6.5, 15)])
    p.drawLine(QPointF(12, 15.5), QPointF(17.5, 15.5))


def _g_timeline(p: QPainter) -> None:
    p.setPen(_pen("#7c5cd6", 2.0))
    p.drawLine(QPointF(3, 13), QPointF(21, 13))
    for x, up in ((6.5, True), (12, False), (17.5, True)):
        p.setPen(_pen("#7c5cd6", 1.6))
        p.drawLine(QPointF(x, 13), QPointF(x, 6.5 if up else 19.5))
        p.setPen(_pen("#7c5cd6", 1.6))
        p.setBrush(_WHITE)
        p.drawEllipse(QPointF(x, 13), 2.4, 2.4)


def _sparkle(p: QPainter, cx: float, cy: float, r: float, color: str) -> None:
    k = r * 0.28
    path = _poly(
        (cx, cy - r),
        (cx + k, cy - k),
        (cx + r, cy),
        (cx + k, cy + k),
        (cx, cy + r),
        (cx - k, cy + k),
        (cx - r, cy),
        (cx - k, cy - k),
    )
    p.setPen(_pen(color, 1.2))
    p.setBrush(QColor(color))
    p.drawPath(path)


def _g_agents(p: QPainter) -> None:
    _sparkle(p, 10, 13, 8.5, "#d6549a")
    _sparkle(p, 18.5, 5.5, 3.6, "#f0a1cf")


def _g_extract(p: QPainter) -> None:
    p.setPen(_pen("#3f9142", 2.0))
    p.setBrush(Qt.BrushStyle.NoBrush)
    p.drawPolyline([QPointF(4, 14), QPointF(4, 20), QPointF(20, 20), QPointF(20, 14)])
    p.setPen(_pen("#3f9142", 2.3))
    p.drawLine(QPointF(12, 3.5), QPointF(12, 15))
    p.drawPolyline([QPointF(7.5, 10.5), QPointF(12, 15), QPointF(16.5, 10.5)])


def _g_report(p: QPainter) -> None:
    p.setPen(_pen("#5a6b82", 1.7))
    p.setBrush(QColor("#f5f7fa"))
    p.drawPath(_poly((5.5, 3), (15, 3), (19.5, 7.5), (19.5, 21), (5.5, 21)))
    p.setBrush(QColor("#d4dbe5"))
    p.drawPath(_poly((15, 3), (15, 7.5), (19.5, 7.5)))
    p.setPen(_pen("#5a6b82", 1.6))
    for y, x2 in ((10.5, 16), (13.5, 16), (16.5, 12.5)):
        p.drawLine(QPointF(8.5, y), QPointF(x2, y))


def _g_sun(p: QPainter) -> None:
    p.setPen(_pen("#e0a020", 2.0))
    p.setBrush(QColor("#f6c443"))
    p.drawEllipse(QPointF(12, 12), 4.3, 4.3)
    p.setPen(_pen("#f6c443", 2.0))
    for i in range(8):
        p.save()
        p.translate(12, 12)
        p.rotate(i * 45)
        p.drawLine(QPointF(0, -7), QPointF(0, -10))
        p.restore()


def _g_moon(p: QPainter) -> None:
    outer = QPainterPath()
    outer.addEllipse(QPointF(12, 12), 8.5, 8.5)
    bite = QPainterPath()
    bite.addEllipse(QPointF(15.5, 9.5), 7, 7)
    p.setPen(_pen("#6b82e0", 1.4))
    p.setBrush(QColor("#9db0ff"))
    p.drawPath(outer.subtracted(bite))


def _g_mailbox(p: QPainter) -> None:
    p.setPen(_pen("#b5511b", 1.8))
    p.setBrush(QColor("#ffd8b8"))
    p.drawRoundedRect(QRectF(3, 5.5, 18, 13), 2, 2)
    p.setBrush(Qt.BrushStyle.NoBrush)
    p.drawPolyline([QPointF(3.5, 6.5), QPointF(12, 13), QPointF(20.5, 6.5)])


def _g_stats(p: QPainter) -> None:
    p.setPen(Qt.PenStyle.NoPen)
    for x, h, c in ((4.5, 8, "#8b9cf0"), (10, 15, "#5b6cd6"), (15.5, 11, "#8b9cf0")):
        p.setBrush(QColor(c))
        p.drawRoundedRect(QRectF(x, 20 - h, 4.2, h), 1, 1)
    p.setPen(_pen("#5b6cd6", 1.6))
    p.drawLine(QPointF(3, 20.5), QPointF(21, 20.5))


def _clock_face(p: QPainter, color: str, fill: str) -> None:
    p.setPen(_pen(color, 1.9))
    p.setBrush(QColor(fill))
    p.drawEllipse(QPointF(12, 12), 8.5, 8.5)
    p.setPen(_pen(color, 2.0))
    p.drawPolyline([QPointF(12, 7), QPointF(12, 12.3), QPointF(15.5, 14.5)])


def _g_clock(p: QPainter) -> None:
    _clock_face(p, "#c53030", "#fff0f0")


def _g_history(p: QPainter) -> None:
    p.setPen(_pen("#4a6fa5", 2.0))
    p.setBrush(Qt.BrushStyle.NoBrush)
    p.drawArc(QRectF(3.5, 3.5, 17, 17), 40 * 16, 290 * 16)
    p.drawPolyline([QPointF(3.2, 7.5), QPointF(3.8, 12.2), QPointF(8.3, 11.2)])
    p.drawPolyline([QPointF(12, 7.5), QPointF(12, 12.3), QPointF(15.3, 14.3)])


def _g_filter(p: QPainter) -> None:
    p.setPen(_pen("#4a6fa5", 1.7))
    p.setBrush(QColor("#c9d8ee"))
    p.drawPath(_poly((3.5, 4.5), (20.5, 4.5), (14, 12.5), (14, 19), (10, 21), (10, 12.5)))


def _g_keyboard(p: QPainter) -> None:
    p.setPen(_pen("#4a6fa5", 1.6))
    p.setBrush(QColor("#e4ebf5"))
    p.drawRoundedRect(QRectF(2.5, 6.5, 19, 11), 2, 2)
    p.setPen(_pen("#4a6fa5", 1.4))
    for y in (9.5, 12.0):
        for x in (5.0, 8.0, 11.0, 14.0, 17.0):
            p.drawPoint(QPointF(x, y))
    p.drawLine(QPointF(7, 14.8), QPointF(17, 14.8))


def _g_play(p: QPainter) -> None:
    p.setPen(_pen("#2f7a33", 1.4))
    p.setBrush(QColor("#3f9142"))
    p.drawPath(_poly((7, 4.5), (19.5, 12), (7, 19.5)))


def _g_stop(p: QPainter) -> None:
    p.setPen(_pen("#a12727", 1.4))
    p.setBrush(QColor("#c53030"))
    p.drawRoundedRect(QRectF(5.5, 5.5, 13, 13), 2, 2)


def _g_reload(p: QPainter) -> None:
    p.setPen(_pen("#2f6fd6", 2.2))
    p.setBrush(Qt.BrushStyle.NoBrush)
    p.drawArc(QRectF(4, 4, 16, 16), 20 * 16, 300 * 16)
    p.setBrush(QColor("#2f6fd6"))
    p.setPen(_pen("#2f6fd6", 1.0))
    p.drawPath(_poly((17.5, 3.5), (21.5, 8.5), (15.5, 9.5)))


def _g_cancel(p: QPainter) -> None:
    p.setPen(_pen("#8a9099", 1.9))
    p.setBrush(Qt.BrushStyle.NoBrush)
    p.drawEllipse(QPointF(12, 12), 8.5, 8.5)
    p.setPen(_pen("#c53030", 2.2))
    p.drawLine(QPointF(8.5, 8.5), QPointF(15.5, 15.5))
    p.drawLine(QPointF(15.5, 8.5), QPointF(8.5, 15.5))


def _g_close(p: QPainter) -> None:
    p.setPen(_pen("#8a9099", 2.4))
    p.drawLine(QPointF(6.5, 6.5), QPointF(17.5, 17.5))
    p.drawLine(QPointF(17.5, 6.5), QPointF(6.5, 17.5))


def _g_collapse(p: QPainter) -> None:
    p.setPen(_pen("#8a9099", 2.2))
    p.drawPolyline([QPointF(6, 10), QPointF(12, 4.5), QPointF(18, 10)])
    p.drawPolyline([QPointF(6, 19), QPointF(12, 13.5), QPointF(18, 19)])


def _g_expand(p: QPainter) -> None:
    p.setPen(_pen("#8a9099", 2.2))
    p.drawPolyline([QPointF(6, 4.5), QPointF(12, 10), QPointF(18, 4.5)])
    p.drawPolyline([QPointF(6, 13.5), QPointF(12, 19), QPointF(18, 13.5)])


def _g_rows(p: QPainter) -> None:
    p.setPen(_pen("#5a6b82", 1.6))
    p.setBrush(QColor("#f5f7fa"))
    p.drawRoundedRect(QRectF(3, 4, 18, 16), 1.5, 1.5)
    for y in (9.5, 14.5):
        p.drawLine(QPointF(3, y), QPointF(21, y))
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(QColor("#3f9142"))
    p.drawRect(QRectF(3.8, 10.3, 16.4, 3.4))


_GLYPHS: dict[str, Callable[[QPainter], None]] = {
    "open": _g_open,
    "scan": _g_scan,
    "folder": _g_folder,
    "search": _g_search,
    "sql": _g_sql,
    "timeline": _g_timeline,
    "agents": _g_agents,
    "extract": _g_extract,
    "rows": _g_rows,
    "report": _g_report,
    "sun": _g_sun,
    "moon": _g_moon,
    "mailbox": _g_mailbox,
    "stats": _g_stats,
    "clock": _g_clock,
    "history": _g_history,
    "filter": _g_filter,
    "keyboard": _g_keyboard,
    "play": _g_play,
    "stop": _g_stop,
    "reload": _g_reload,
    "cancel": _g_cancel,
    "close": _g_close,
    "collapse": _g_collapse,
    "expand": _g_expand,
}
ICON_NAMES: tuple[str, ...] = tuple(_GLYPHS)
_icon_cache: dict[str, QIcon] = {}


def render_glyph(name: str, px: int) -> QPixmap:
    """Paint one action glyph at ``px`` pixels."""
    pm = QPixmap(px, px)
    pm.fill(Qt.GlobalColor.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.scale(px / 24.0, px / 24.0)
    _GLYPHS[name](p)
    p.end()
    return pm


def icon(name: str) -> QIcon:
    """An action icon by name (see ``ICON_NAMES``); unknown names give an empty icon."""
    cached = _icon_cache.get(name)
    if cached is not None:
        return cached
    result = QIcon()
    if name in _GLYPHS:
        for px in _ICON_SIZES:
            result.addPixmap(render_glyph(name, px))
    _icon_cache[name] = result
    return result
