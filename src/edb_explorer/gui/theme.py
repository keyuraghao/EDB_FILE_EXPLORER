"""Light / dark themes built on the Fusion style so they look identical on Linux and Windows."""

from __future__ import annotations

from PySide6.QtGui import QColor, QPalette
from PySide6.QtWidgets import QApplication

DARK = {
    "window": "#1f2226",
    "base": "#16181b",
    "alt": "#1b1e22",
    "text": "#e6e6e6",
    "dim": "#8a9099",
    "button": "#2a2e34",
    "highlight": "#3d7bd9",
    "highlight_text": "#ffffff",
    "border": "#3a3f46",
    "link": "#6ea8fe",
}

LIGHT = {
    "window": "#f3f4f6",
    "base": "#ffffff",
    "alt": "#f7f8fa",
    "text": "#1f2328",
    "dim": "#6e7781",
    "button": "#e9ecef",
    "highlight": "#2f6fd6",
    "highlight_text": "#ffffff",
    "border": "#d0d7de",
    "link": "#0969da",
}


def _palette(c: dict[str, str]) -> QPalette:
    p = QPalette()
    window, base, alt, text, dim = (
        QColor(c["window"]),
        QColor(c["base"]),
        QColor(c["alt"]),
        QColor(c["text"]),
        QColor(c["dim"]),
    )
    button, highlight, htext, link = (
        QColor(c["button"]),
        QColor(c["highlight"]),
        QColor(c["highlight_text"]),
        QColor(c["link"]),
    )
    p.setColor(QPalette.ColorRole.Window, window)
    p.setColor(QPalette.ColorRole.WindowText, text)
    p.setColor(QPalette.ColorRole.Base, base)
    p.setColor(QPalette.ColorRole.AlternateBase, alt)
    p.setColor(QPalette.ColorRole.ToolTipBase, base)
    p.setColor(QPalette.ColorRole.ToolTipText, text)
    p.setColor(QPalette.ColorRole.Text, text)
    p.setColor(QPalette.ColorRole.PlaceholderText, dim)
    p.setColor(QPalette.ColorRole.Button, button)
    p.setColor(QPalette.ColorRole.ButtonText, text)
    p.setColor(QPalette.ColorRole.BrightText, QColor("#ff5555"))
    p.setColor(QPalette.ColorRole.Link, link)
    p.setColor(QPalette.ColorRole.Highlight, highlight)
    p.setColor(QPalette.ColorRole.HighlightedText, htext)
    p.setColor(QPalette.ColorRole.Mid, QColor(c["border"]))
    for role in (QPalette.ColorRole.WindowText, QPalette.ColorRole.Text, QPalette.ColorRole.ButtonText):
        p.setColor(QPalette.ColorGroup.Disabled, role, dim)
    p.setColor(QPalette.ColorGroup.Disabled, QPalette.ColorRole.Highlight, QColor(c["border"]))
    return p


def _stylesheet(c: dict[str, str]) -> str:
    return f"""
    QToolTip {{ color: {c["text"]}; background: {c["base"]}; border: 1px solid {c["border"]}; padding: 4px; }}
    QHeaderView::section {{
        background: {c["button"]}; color: {c["text"]}; padding: 4px 6px;
        border: 0; border-right: 1px solid {c["border"]}; border-bottom: 1px solid {c["border"]};
    }}
    QTableView, QTreeView {{ gridline-color: {c["border"]}; selection-background-color: {c["highlight"]}; }}
    QTableView QTableCornerButton::section {{ background: {c["button"]}; border: 0; }}
    QLineEdit, QPlainTextEdit, QTextEdit, QComboBox, QSpinBox {{
        border: 1px solid {c["border"]}; border-radius: 4px; padding: 3px 6px; background: {c["base"]};
    }}
    QLineEdit:focus, QComboBox:focus {{ border-color: {c["highlight"]}; }}
    QTabBar::tab {{ padding: 6px 12px; border: 1px solid {c["border"]}; border-bottom: 0; margin-right: 1px;
                   border-top-left-radius: 4px; border-top-right-radius: 4px; background: {c["button"]}; }}
    QTabBar::tab:selected {{ background: {c["base"]}; }}
    QDockWidget::title {{ background: {c["button"]}; padding: 5px; }}
    QStatusBar {{ border-top: 1px solid {c["border"]}; }}
    QToolBar {{ border: 0; spacing: 4px; padding: 2px; }}
    QProgressBar {{ border: 1px solid {c["border"]}; border-radius: 3px; text-align: center; height: 12px; }}
    QProgressBar::chunk {{ background: {c["highlight"]}; }}
    QSplitter::handle {{ background: {c["border"]}; }}
    QLabel#dim {{ color: {c["dim"]}; }}
    QLabel#title {{ font-weight: 600; font-size: 13pt; }}
    QPlainTextEdit#mono {{ font-family: "JetBrains Mono", "Cascadia Mono", "Consolas", "DejaVu Sans Mono", monospace; }}
    """


def apply_theme(app: QApplication, name: str) -> None:
    colors = DARK if name == "dark" else LIGHT
    app.setStyle("Fusion")
    app.setPalette(_palette(colors))
    app.setStyleSheet(_stylesheet(colors))
    app.setProperty("edb_theme", name)


def current_theme(app: QApplication) -> str:
    return str(app.property("edb_theme") or "dark")
