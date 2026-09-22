"""GUI entry point."""

from __future__ import annotations

import logging
import sys

from edb_explorer import __app_name__, __version__


def run(files: list[str] | None = None) -> int:
    """Create the QApplication, show the main window and run the event loop."""
    from PySide6.QtCore import QCoreApplication, Qt
    from PySide6.QtWidgets import QApplication

    from edb_explorer.gui.icons import app_icon
    from edb_explorer.gui.main_window import MainWindow
    from edb_explorer.gui.theme import apply_theme

    logging.basicConfig(level=logging.INFO, stream=sys.stderr, format="%(levelname)s %(name)s: %(message)s")
    from edb_explorer import portable

    data = portable.activate()  # portable builds keep settings / keys / caches next to the executable
    if data:
        logging.getLogger(__name__).info("Portable mode: user data in %s", data)
    QCoreApplication.setOrganizationName("EDB Explorer")
    QCoreApplication.setOrganizationDomain("edb-explorer.local")
    QCoreApplication.setApplicationName(__app_name__)
    QCoreApplication.setApplicationVersion(__version__)
    if hasattr(Qt, "HighDpiScaleFactorRoundingPolicy"):
        QApplication.setHighDpiScaleFactorRoundingPolicy(Qt.HighDpiScaleFactorRoundingPolicy.PassThrough)

    app = QApplication.instance() or QApplication(sys.argv[:1])
    app.setWindowIcon(app_icon())
    from PySide6.QtCore import QSettings

    apply_theme(app, str(QSettings().value("theme", "dark")))
    window = MainWindow()
    window.show()
    if files:
        window.open_files(list(files))
    return app.exec()


def main() -> None:
    """Console-script entry point (``edb-explorer-gui``)."""
    sys.exit(run(sys.argv[1:]))


if __name__ == "__main__":
    main()
