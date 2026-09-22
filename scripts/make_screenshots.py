"""Regenerate docs/screenshots/*.png from synthetic evidence (see make_demo_data.py).

    python scripts/make_screenshots.py

Runs the real GUI offscreen. Everything shown comes from the generated demo folder under
<tempdir>/cases, with a neutral user / host name, so the images contain no case or machine data.
(The Exchange mailbox viewer has no screenshot on purpose: real mailbox content is case data.)
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

from PIL import Image, ImageFilter
from PySide6.QtCore import QPoint, QSettings, Qt
from PySide6.QtWidgets import QApplication, QWidget
from scripts.make_demo_data import main as make_demo

import edb_explorer.gui.main_window as mw
from edb_explorer.gui.theme import apply_theme
from edb_explorer.gui.widgets.table_tab import TableTab

OUT = ROOT / "docs" / "screenshots"
SIZE = (1520, 880)


def pump(app: QApplication, cond, timeout: float = 120) -> None:
    deadline = time.time() + timeout
    while not cond():
        app.processEvents()
        time.sleep(0.02)
        if time.time() > deadline:
            raise TimeoutError("GUI did not reach the expected state")


def settle(app: QApplication, n: int = 30) -> None:
    for _ in range(n):
        app.processEvents()
        time.sleep(0.01)


def loaded(win: mw.MainWindow) -> bool:
    return all(w._finished for w in (win.tabs.widget(i) for i in range(win.tabs.count())) if isinstance(w, TableTab))


def shot(win: QWidget, name: str, blur: list[tuple[int, int, int, int]] | None = None) -> None:
    path = OUT / name
    win.grab().save(str(path))
    if blur:
        img = Image.open(path)
        for x, y, w, h in blur:
            box = (max(x, 0), max(y, 0), min(x + w, img.width), min(y + h, img.height))
            img.paste(img.crop(box).filter(ImageFilter.GaussianBlur(14)), box)
        img.save(path)
    print("wrote", path.relative_to(ROOT))


def rect_of(widget: QWidget, window: QWidget) -> tuple[int, int, int, int]:
    p = widget.mapTo(window, QPoint(0, 0))
    return p.x(), p.y(), widget.width(), widget.height()


def new_window(app: QApplication, ini: Path, theme: str) -> mw.MainWindow:
    mw.QSettings = lambda *a, **k: QSettings(str(ini), QSettings.Format.IniFormat)
    QSettings(str(ini), QSettings.Format.IniFormat).setValue("restore_session", False)
    apply_theme(app, theme)
    win = mw.MainWindow()
    win.resize(*SIZE)
    win.show()
    settle(app)
    win.resizeDocks([win.dock_tree, win.dock_info], [300, 250], Qt.Orientation.Horizontal)
    settle(app)
    return win


def main() -> None:
    app = QApplication.instance() or QApplication([])
    # neutral identities and paths: nothing from this machine ends up in the images
    from edb_explorer.core import project
    from edb_explorer.gui.widgets import project_dialogs

    project.socket.gethostname = lambda: "ws-analyst"  # type: ignore[attr-defined]
    project._default_user = lambda: "analyst"
    project_dialogs.default_extract_dir = lambda: "/home/analyst/EDB Explorer projects"
    work = Path(tempfile.gettempdir()) / "cases"
    shutil.rmtree(work, ignore_errors=True)
    work.mkdir()
    demo = work / "Case-2025-0042"
    sys.argv = ["make_demo_data.py", str(demo)]
    make_demo()
    os.environ["EDB_EXPLORER_CONFIG_DIR"] = str(work / "cfg")
    ini = work / "settings.ini"
    OUT.mkdir(parents=True, exist_ok=True)
    files = [
        str(demo / "Windows" / "Security.evtx"),
        str(demo / "Browser" / "History"),
        str(demo / "iPhone" / "sms.db"),
    ]

    # ---- welcome page (dark) with a recent list --------------------------------------------- #
    QSettings(str(ini), QSettings.Format.IniFormat).setValue("recent", files)
    win = new_window(app, ini, "dark")
    shot(win, "welcome.png")

    # ---- main window, dark: Security.evtx table, folder-grouped tree, inspector ------------- #
    win.open_files(files)
    pump(app, lambda: len(win.session) == 3)
    settle(app)
    pump(app, lambda: loaded(win))
    win._close_other_tabs(-1)
    sec = next(d for d in win.session if d.path.suffix == ".evtx")
    tab = win.open_table(sec.id, "Security")
    pump(app, lambda: loaded(win))
    tab.filter_edit.setText("svc_backup")
    tab.apply_filter()
    settle(app)
    tab.view.selectRow(2)
    settle(app)
    win.tree.view.expandAll()
    settle(app)
    shot(win, "main-dark.png")

    # ---- analysis view (dark): failed logons ------------------------------------------------- #
    win.run_view(sec.id, "failed_logons")
    pump(app, lambda: "row" in win.tabs.currentWidget().info.text())
    settle(app, 60)
    shot(win, "analysis-view.png")

    # ---- timeline (dark) -------------------------------------------------------------------- #
    win.show_timeline()
    tl = win._timeline_tab
    tl.build()
    pump(app, lambda: tl.grid.model.rowCount() > 0 and not (tl._worker and tl._worker.isRunning()))
    settle(app, 60)
    shot(win, "timeline.png")

    # ---- search dialog (dark) --------------------------------------------------------------- #
    win.show_search()
    dlg = win._search_dialog
    dlg.query.setText("setup.exe")
    dlg.start()
    pump(app, lambda: dlg.go.isEnabled())
    settle(app)
    dlg.resize(980, 520)
    settle(app)
    shot(dlg, "search.png")
    dlg.close()

    # ---- project import dialog (dark) -------------------------------------------------------- #
    from edb_explorer.core.project import SigningIdentity, TrustStore, export_project
    from edb_explorer.gui.widgets.project_dialogs import ImportProjectDialog

    identity = SigningIdentity.load_or_create(name="Analyst")
    proj = work / "Case-2025-0042.edbproj"
    export_project(
        win.session.databases(),
        proj,
        name="Case 2025-0042",
        notes="Logons from 203.0.113.0/24 - see failed_logons",
        workspace=win.capture_workspace(),
        password="demo",
        identity=identity,
    )
    TrustStore().trust(identity.fingerprint, "Analyst (verified in person)", identity.public_b64)
    idlg = ImportProjectDialog(str(proj), win.tasks, win)
    idlg.show()
    settle(app)
    idlg.password.setText("demo")
    idlg._unlock()
    pump(app, lambda: idlg.result is not None)
    settle(app)
    shot(idlg, "project-import.png")
    idlg.close()

    # ---- keyboard shortcuts dialog (dark) --------------------------------------------------- #
    from edb_explorer.gui.widgets.shortcuts_dialog import ShortcutsDialog

    sdlg = ShortcutsDialog(win.shortcuts, win)
    sdlg.show()
    settle(app)
    shot(sdlg, "shortcuts.png")
    sdlg.close()
    win.close()
    settle(app)

    # ---- main window, light: Chromium History urls table ------------------------------------ #
    win = new_window(app, ini, "light")
    win.open_files(files)
    pump(app, lambda: len(win.session) == 3)
    settle(app)
    pump(app, lambda: loaded(win))
    win._close_other_tabs(-1)
    hist = next(d for d in win.session if d.path.name == "History")
    win.open_table(hist.id, "urls")
    pump(app, lambda: loaded(win))
    t2 = win.tabs.currentWidget()
    t2.view.sortByColumn(3, Qt.SortOrder.DescendingOrder)
    settle(app)
    t2.view.selectRow(0)
    win.tree.view.expandAll()
    settle(app, 60)
    shot(win, "main-light.png")
    win.close()
    settle(app)

    print("done")


if __name__ == "__main__":
    main()
