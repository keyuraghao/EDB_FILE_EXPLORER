"""Main window: menus, docks, tabs and the glue between the session and the widgets."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from PySide6.QtCore import QByteArray, QSettings, Qt, QTimer, QUrl
from PySide6.QtGui import QAction, QCloseEvent, QDesktopServices, QDragEnterEvent, QDropEvent, QKeySequence
from PySide6.QtWidgets import (
    QApplication,
    QDockWidget,
    QFileDialog,
    QMainWindow,
    QMenu,
    QMessageBox,
    QTabWidget,
    QWidget,
)

from edb_explorer import __app_name__, __version__
from edb_explorer.core import EdbDatabase, Session
from edb_explorer.core.backends import KINDS
from edb_explorer.core.session import ESE_EXTENSIONS
from edb_explorer.core.sqlworkspace import SqlWorkspace
from edb_explorer.gui.icons import app_icon, std
from edb_explorer.gui.tasks import TaskManager, TaskPanel
from edb_explorer.gui.theme import apply_theme, current_theme
from edb_explorer.gui.widgets.database_tree import DatabaseTree
from edb_explorer.gui.widgets.dialogs import (
    AboutDialog,
    ExportDialog,
    ReportDialog,
    ScanDialog,
    SearchDialog,
    TimestampDialog,
)
from edb_explorer.gui.widgets.info_panel import InfoPanel
from edb_explorer.gui.widgets.inspector import RecordInspector
from edb_explorer.gui.widgets.query_tab import QueryTab, ResultsGrid, TimelineTab
from edb_explorer.gui.widgets.stats_dialog import StatsDialog
from edb_explorer.gui.widgets.table_tab import TableTab
from edb_explorer.gui.widgets.welcome import WelcomePage
from edb_explorer.gui.workers import FunctionWorker

log = logging.getLogger(__name__)

_ALL_EXT = " ".join(sorted({f"*{e}" for k in KINDS for e in k.extensions}))
FILE_FILTER = (
    f"All supported databases ({_ALL_EXT});;"
    "ESE (*.edb *.dit *.dat *.jtx *.vol);;SQLite (*.db *.sqlite *.sqlite3 *.sqlitedb *.storedata);;"
    "LevelDB (CURRENT MANIFEST-* *.ldb *.log);;Access (*.mdb *.accdb);;dBase (*.dbf);;Berkeley DB (*.db *.bdb);;"
    "SQL dumps (*.sql);;BSON dumps (*.bson);;All files (*)"
)
MAX_RECENT = 12


class MainWindow(QMainWindow):
    def __init__(self, session: Session | None = None) -> None:
        super().__init__()
        self.session = session if session is not None else Session()
        self.settings = QSettings()
        self.setWindowTitle(__app_name__)
        self.setWindowIcon(app_icon())
        self.resize(1400, 860)
        self.setAcceptDrops(True)
        self._workers: list[FunctionWorker] = []
        self._search_dialog: SearchDialog | None = None
        self.max_rows = int(self.settings.value("max_rows", 1_000_000))
        self.tasks = TaskManager(self)
        self.workspace = SqlWorkspace()
        self._sql_tab: QueryTab | None = None
        self._timeline_tab: TimelineTab | None = None

        self._build_central()
        self._build_docks()
        self._build_menus()
        self._restore_state()
        self._update_recent_menu()
        self.statusBar().showMessage("Open a database with File ▸ Open, or drop files here.")

    # ------------------------------------------------------------------ #
    # UI construction
    # ------------------------------------------------------------------ #
    def _build_central(self) -> None:
        self.tabs = QTabWidget()
        self.tabs.setTabsClosable(True)
        self.tabs.setMovable(True)
        self.tabs.setDocumentMode(True)
        self.tabs.tabCloseRequested.connect(self._close_tab)
        self.tabs.currentChanged.connect(self._tab_changed)
        self.tabs.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.tabs.customContextMenuRequested.connect(self._tabs_menu)
        placeholder = WelcomePage()
        placeholder.open_files.connect(self.open_files_dialog)
        placeholder.scan_folder.connect(self.open_folder_dialog)
        placeholder.open_recent.connect(lambda p: self.open_files([p]))
        placeholder.clear_recent.connect(lambda: (self.settings.setValue("recent", []), self._update_recent_menu()))
        self._placeholder = placeholder
        self.setCentralWidget(self.tabs)
        self.tabs.addTab(placeholder, "Welcome")
        self.tabs.tabBar().setTabButton(0, self.tabs.tabBar().ButtonPosition.RightSide, None)

    def _build_docks(self) -> None:
        self.tree = DatabaseTree()
        self.tree.table_activated.connect(self.open_table)
        self.tree.database_selected.connect(self._show_db_info)
        self.tree.close_requested.connect(self.close_database)
        self.tree.export_requested.connect(lambda db_id: self._export(db_id, None))
        self.tree.export_table_requested.connect(lambda db_id, t: self._export(db_id, t))
        self.tree.count_requested.connect(self._count_all)
        self.tree.view_activated.connect(self.run_view)
        self.tree.stats_requested.connect(lambda db_id, t: self._show_stats_for(db_id, t))
        self.tree.sql_requested.connect(lambda db_id: self.show_sql(db_id))
        self.dock_tree = QDockWidget("Databases", self)
        self.dock_tree.setObjectName("dock_databases")
        self.dock_tree.setWidget(self.tree)
        self.addDockWidget(Qt.DockWidgetArea.LeftDockWidgetArea, self.dock_tree)

        self.info = InfoPanel()
        self.dock_info = QDockWidget("Properties", self)
        self.dock_info.setObjectName("dock_properties")
        self.dock_info.setWidget(self.info)
        self.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, self.dock_info)

        self.inspector = RecordInspector()
        self.dock_inspector = QDockWidget("Record inspector", self)
        self.dock_inspector.setObjectName("dock_inspector")
        self.dock_inspector.setWidget(self.inspector)
        self.addDockWidget(Qt.DockWidgetArea.BottomDockWidgetArea, self.dock_inspector)
        self.task_panel = TaskPanel(self.tasks)
        self.dock_tasks = QDockWidget("Tasks", self)
        self.dock_tasks.setObjectName("dock_tasks")
        self.dock_tasks.setWidget(self.task_panel)
        self.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, self.dock_tasks)
        self.tabifyDockWidget(self.dock_info, self.dock_tasks)
        self.dock_info.raise_()
        self.tasks.task_added.connect(lambda _t: self.dock_tasks.raise_())
        self.tasks.count_changed.connect(self._tasks_changed)
        self.resizeDocks([self.dock_tree, self.dock_info], [320, 340], Qt.Orientation.Horizontal)
        self.resizeDocks([self.dock_inspector], [260], Qt.Orientation.Vertical)

    def _tasks_changed(self, running: int) -> None:
        self.dock_tasks.setWindowTitle(f"Tasks ({running})" if running else "Tasks")

    def _build_menus(self) -> None:
        mb = self.menuBar()
        file_menu = mb.addMenu("&File")
        self._act(
            file_menu,
            "&Open database(s)…",
            self.open_files_dialog,
            QKeySequence.StandardKey.Open,
            "SP_DialogOpenButton",
        )
        self._act(file_menu, "Open &folder (scan)…", self.open_folder_dialog, "Ctrl+Shift+O", "SP_DirOpenIcon")
        self.recent_menu = file_menu.addMenu("Open &recent")
        file_menu.addSeparator()
        self._act(file_menu, "&Extract / export…", self.export_current, "Ctrl+E", "SP_DialogSaveButton")
        self._act(file_menu, "Extract &selected rows…", self.extract_selection, "Ctrl+Shift+E")
        self._act(file_menu, "Generate &report…", self.show_report, "Ctrl+R", "SP_FileDialogDetailedView")
        file_menu.addSeparator()
        self._act(
            file_menu, "Close &tab", lambda: self._close_tab(self.tabs.currentIndex()), QKeySequence.StandardKey.Close
        )
        self._act(file_menu, "Close &database", self._close_current_database, "Ctrl+Shift+W")
        self._act(file_menu, "Close &all databases", self.close_all_databases)
        file_menu.addSeparator()
        self._act(file_menu, "&Quit", self.close, QKeySequence.StandardKey.Quit)

        view = mb.addMenu("&View")
        view.addAction(self.dock_tree.toggleViewAction())
        view.addAction(self.dock_info.toggleViewAction())
        view.addAction(self.dock_inspector.toggleViewAction())
        view.addAction(self.dock_tasks.toggleViewAction())
        view.addSeparator()
        self._act(view, "Collapse all databases", lambda: self.tree.collapse_all(), "Ctrl+Shift+-")
        self._act(view, "Expand all databases", lambda: self.tree.expand_all(), "Ctrl+Shift+=")
        view.addSeparator()
        self.theme_action = self._act(view, "&Dark theme", self._toggle_theme, "Ctrl+Shift+D")
        self.theme_action.setCheckable(True)
        self.theme_action.setChecked(current_theme(QApplication.instance()) == "dark")  # type: ignore[arg-type]
        self._act(view, "Reset &layout", self._reset_layout)

        analysis = mb.addMenu("&Analysis")
        self._act(analysis, "&SQL console", self.show_sql, "Ctrl+Q", "SP_ComputerIcon")
        self._act(analysis, "&Timeline", self.show_timeline, "Ctrl+L", "SP_FileDialogListView")
        self._act(analysis, "Column &statistics for current table…", self.show_stats, "Ctrl+I")
        self.views_menu = analysis.addMenu("Artifact &views")
        self.views_menu.aboutToShow.connect(self._fill_views_menu)

        tools = mb.addMenu("&Tools")
        self._act(tools, "&Find in database(s)…", self.show_search, "Ctrl+Shift+F", "SP_FileDialogContentsView")
        self._act(tools, "Filter &rows in current table", self._focus_filter, QKeySequence.StandardKey.Find)
        self._act(tools, "&Timestamp decoder…", lambda: self.show_timestamp(None), "Ctrl+T")
        self._act(tools, "&Count records in all tables", lambda: self._count_all(self._current_db_id()))
        tools.addSeparator()
        self._act(tools, "Set &row limit…", self._set_row_limit)

        help_menu = mb.addMenu("&Help")
        self._act(help_menu, "&MCP server setup…", self._show_mcp_help)
        self._act(
            help_menu,
            "Project on &GitHub",
            lambda: QDesktopServices.openUrl(QUrl("https://github.com/keyuraghao/EDB_FILE_EXPLORER")),
        )
        help_menu.addSeparator()
        self._act(help_menu, "&About", lambda: AboutDialog(self).exec())

        tb = self.addToolBar("Main")
        tb.setObjectName("toolbar_main")
        tb.setMovable(False)
        tb.addAction(self._act(None, "Open", self.open_files_dialog, None, "SP_DialogOpenButton"))
        tb.addAction(self._act(None, "Scan folder", self.open_folder_dialog, None, "SP_DirOpenIcon"))
        tb.addAction(self._act(None, "Search", self.show_search, None, "SP_FileDialogContentsView"))
        tb.addAction(self._act(None, "SQL", self.show_sql, None, "SP_ComputerIcon"))
        tb.addAction(self._act(None, "Timeline", self.show_timeline, None, "SP_FileDialogListView"))
        tb.addAction(self._act(None, "Extract", self.export_current, None, "SP_DialogSaveButton"))
        tb.addAction(self._act(None, "Report", self.show_report, None, "SP_FileDialogDetailedView"))

    def _act(self, menu: QMenu | None, text: str, slot: Any, shortcut: Any = None, icon: str | None = None) -> QAction:
        act = QAction(text, self)
        if icon:
            act.setIcon(std(icon))
        if shortcut:
            act.setShortcut(QKeySequence(shortcut) if isinstance(shortcut, str) else shortcut)
        act.triggered.connect(slot)
        if menu is not None:
            menu.addAction(act)
        return act

    # ------------------------------------------------------------------ #
    # Opening databases
    # ------------------------------------------------------------------ #
    def open_files_dialog(self) -> None:
        start = self.settings.value("last_dir", str(Path.home()))
        files, _ = QFileDialog.getOpenFileNames(self, "Open ESE database(s)", start, FILE_FILTER)
        if files:
            self.settings.setValue("last_dir", str(Path(files[0]).parent))
            self.open_files(files)

    def open_folder_dialog(self) -> None:
        dlg = ScanDialog(self.session, self.settings.value("last_dir", str(Path.home())), self)
        if dlg.exec() and dlg.selected:
            self.settings.setValue("last_dir", dlg.dir_edit.text())
            self.open_files(dlg.selected)

    def open_files(self, paths: list[str]) -> None:
        paths = [p for p in paths if p]
        if not paths:
            return
        self.statusBar().showMessage(f"Opening {len(paths)} file(s)…")
        for path in paths:
            self._open_one(path)

    def _open_one(self, path: str) -> None:
        name = Path(path).name
        task = self.tasks.start(f"Opening {name}", detail=path)

        def job(progress: Any, should_stop: Any) -> tuple[list[EdbDatabase], dict[str, str]]:
            progress("detecting format")
            kind = self.session.detect(path)
            progress(f"parsing {kind or 'file'} catalog")
            return self.session.open_many([path])

        worker = FunctionWorker(job, parent=self)
        worker.progress.connect(lambda m: task.progress(detail=str(m)))
        worker.result.connect(
            lambda r: (
                self._opened(r),
                task.finish(
                    f"{r[0][0].info.kind_name} · {r[0][0].profile.name} · {r[0][0].info.table_count} tables"
                    if r[0]
                    else next(iter(r[1].values()), "failed"),
                    failed=not r[0],
                ),
            )
        )
        worker.failed.connect(lambda m: (self._opened(([], {path: m})), task.finish(m, failed=True)))
        self._workers.append(worker)
        worker.start()

    def _opened(self, result: tuple[list[EdbDatabase], dict[str, str]]) -> None:
        opened, errors = result
        for db in opened:
            if self.tree.model.database_item(db.id) is None:
                self.tree.add_database(db)
                self._add_recent(str(db.path))
            self._show_db_info(db.id)
        if opened:
            self.statusBar().showMessage(
                f"Opened {', '.join(d.path.name for d in opened)} · {len(self.session)} database(s) open", 8000
            )
            if self.tabs.count() == 1 and self.tabs.widget(0) is self._placeholder and len(opened) == 1:
                # first database: open its most interesting table automatically
                tables = opened[0].tables(include_system=False)
                if tables:
                    self.open_table(opened[0].id, tables[0].name)
        if errors:
            QMessageBox.warning(
                self, "Some files could not be opened", "\n\n".join(f"{p}\n{e}" for p, e in errors.items())
            )
        self._refresh_scopes()

    def _refresh_scopes(self) -> None:
        if self._search_dialog:
            self._search_dialog.refresh_scope()
        if self._sql_tab:
            self._sql_tab.refresh_databases()
        if self._timeline_tab:
            self._timeline_tab.refresh_databases()

    def close_database(self, db_id: str) -> None:
        for i in reversed(range(self.tabs.count())):
            w = self.tabs.widget(i)
            if isinstance(w, TableTab) and w.db.id == db_id:
                self._close_tab(i)
        self.tree.remove_database(db_id)
        if self.info.db is not None and self.info.db.id == db_id:
            self.info.show_database(None)
            self.inspector.clear()
        self.workspace.detach(db_id)
        self.session.close(db_id)
        self._refresh_scopes()
        self.statusBar().showMessage(f"Closed {db_id}", 4000)

    def _close_current_database(self) -> None:
        db_id = self._current_db_id()
        if db_id:
            self.close_database(db_id)

    def close_all_databases(self) -> None:
        for db in list(self.session):
            self.close_database(db.id)

    # ------------------------------------------------------------------ #
    # Tables / tabs
    # ------------------------------------------------------------------ #
    def open_table(
        self, db_id: str, table: str, select_row: int | None = None, column: str | None = None
    ) -> TableTab | None:
        try:
            db = self.session.get(db_id)
            info = db.table(table)
        except Exception as exc:
            QMessageBox.warning(self, "Cannot open table", str(exc))
            return None
        for i in range(self.tabs.count()):
            w = self.tabs.widget(i)
            if isinstance(w, TableTab) and w.db is db and w.table.name == info.name:
                self.tabs.setCurrentIndex(i)
                if select_row is not None:
                    self._select_when_loaded(w, select_row, column)
                return w
        if self.tabs.count() == 1 and self.tabs.widget(0) is self._placeholder:
            self.tabs.removeTab(0)
        tab = TableTab(db, info, self.max_rows)
        tab.row_selected.connect(self._row_selected)
        tab.status.connect(lambda m: self.statusBar().showMessage(m))
        tab.interpret_requested.connect(self.show_timestamp)
        self._track_loader(tab)
        tab.extract_requested.connect(lambda scope, t=tab: self._export(t.db.id, t.table.name, t, scope))
        tab.loader.finished_ok.connect(lambda _n, d=db: self.tree.refresh_counts(d))  # type: ignore[union-attr]
        label = info.display_name if len(info.display_name) <= 32 else info.display_name[:30] + "…"
        idx = self.tabs.addTab(tab, label)
        self.tabs.setTabToolTip(
            idx, f"{db.path.name}\n{info.name}" + (f"\n\n{info.description}" if info.description else "")
        )
        self.tabs.setCurrentIndex(idx)
        if select_row is not None:
            self._select_when_loaded(tab, select_row, column)
        return tab

    def _track_loader(self, tab: TableTab) -> None:
        loader = tab.loader
        if loader is None:
            return
        known = tab.db.cached_count(tab.table.name)
        task = self.tasks.start(f"Loading {tab.table.display_name}", cancel=tab.stop, detail=tab.db.path.name)
        loader.chunk_ready.connect(lambda _i, _r: task.progress(tab.loaded, known or 0, f"{tab.loaded:,} rows"))
        loader.finished_ok.connect(lambda n: task.finish(f"{n:,} rows"))
        loader.failed.connect(lambda m: task.finish(m, failed=True))

    def _select_when_loaded(self, tab: TableTab, row: int, column: str | None) -> None:
        if tab.select_row_index(row, column):
            return
        if tab.loader and tab.loader.isRunning():
            QTimer.singleShot(300, lambda: self._select_when_loaded(tab, row, column))

    def _close_tab(self, index: int) -> None:
        w = self.tabs.widget(index)
        if w is None or w is self._placeholder:
            return
        if hasattr(w, "shutdown"):
            w.shutdown()
        if w is self._sql_tab:
            self._sql_tab = None
        if w is self._timeline_tab:
            self._timeline_tab = None
        self.tabs.removeTab(index)
        w.deleteLater()
        if self.tabs.count() == 0:
            self._placeholder.set_recent(self._recent())
            self.tabs.addTab(self._placeholder, "Welcome")
            self.inspector.clear()

    def _tabs_menu(self, pos: Any) -> None:
        index = self.tabs.tabBar().tabAt(pos)
        menu = QMenu(self)
        if index >= 0 and self.tabs.widget(index) is not self._placeholder:
            menu.addAction("Close tab", lambda: self._close_tab(index))
            menu.addAction("Close other tabs", lambda: self._close_other_tabs(index))
        menu.addAction("Close all tabs", lambda: self._close_other_tabs(-1))
        menu.exec(self.tabs.mapToGlobal(pos))

    def _close_other_tabs(self, keep: int) -> None:
        keep_widget = self.tabs.widget(keep) if keep >= 0 else None
        for i in reversed(range(self.tabs.count())):
            if self.tabs.widget(i) is not keep_widget:
                self._close_tab(i)

    def _tab_changed(self, index: int) -> None:
        w = self.tabs.widget(index)
        if isinstance(w, TableTab):
            self._show_db_info(w.db.id)
            self.inspector.clear()
            idx = w.view.currentIndex()
            if idx.isValid():
                w._current_changed(idx, idx)

    def _current_tab(self) -> TableTab | None:
        w = self.tabs.currentWidget()
        return w if isinstance(w, TableTab) else None

    def _current_db_id(self) -> str | None:
        tab = self._current_tab()
        if tab:
            return tab.db.id
        if self.info.db is not None:
            return self.info.db.id
        dbs = list(self.session)
        return dbs[0].id if len(dbs) == 1 else None

    def _row_selected(self, db_id: str, table: str, row_index: int, row: dict[str, Any]) -> None:
        tab = self._current_tab()
        columns = tab.table.columns if tab else ()
        self.inspector.show_record(db_id, table, row_index, row, columns)

    def _show_db_info(self, db_id: str) -> None:
        try:
            self.info.show_database(self.session.get(db_id))
        except Exception:
            self.info.show_database(None)

    def _focus_filter(self) -> None:
        tab = self._current_tab()
        if tab:
            tab.focus_filter()

    # ------------------------------------------------------------------ #
    # Tools
    # ------------------------------------------------------------------ #
    def _drop_placeholder(self) -> None:
        if self.tabs.count() == 1 and self.tabs.widget(0) is self._placeholder:
            self.tabs.removeTab(0)

    def show_sql(self, db_id: str | None = None, sql: str = "", run: bool = False) -> QueryTab:
        if self._sql_tab is None:
            self._drop_placeholder()
            self._sql_tab = QueryTab(
                self.session, self.workspace, self.tasks, self, db_id=db_id or self._current_db_id()
            )
            self._sql_tab.status.connect(lambda m: self.statusBar().showMessage(m))
            self._sql_tab.extract_requested.connect(self._extract_grid)
            self._sql_tab.row_selected.connect(self._query_row_selected)
            self.tabs.addTab(self._sql_tab, "SQL console")
        elif db_id:
            self._sql_tab.refresh_databases(db_id)
        self.tabs.setCurrentWidget(self._sql_tab)
        if sql:
            self._sql_tab.set_sql(sql, run)
        return self._sql_tab

    def run_view(self, db_id: str, view_id: str) -> None:
        try:
            db = self.session.get(db_id)
            view = next(v for v in db.profile.views if v.id == view_id)
        except Exception as exc:
            QMessageBox.warning(self, "View", str(exc))
            return
        self._drop_placeholder()
        tab = QueryTab(
            self.session, self.workspace, self.tasks, self, initial_sql=view.sql, db_id=db_id, title=view.name
        )
        tab.status.connect(lambda m: self.statusBar().showMessage(m))
        tab.extract_requested.connect(self._extract_grid)
        tab.row_selected.connect(self._query_row_selected)
        idx = self.tabs.addTab(tab, f"▶ {view.name}")
        self.tabs.setTabToolTip(idx, f"{db.path.name}\n{view.description}")
        self.tabs.setCurrentIndex(idx)
        tab.run()

    def show_timeline(self) -> None:
        if self._timeline_tab is None:
            self._drop_placeholder()
            self._timeline_tab = TimelineTab(self.session, self.tasks, self)
            self._timeline_tab.status.connect(lambda m: self.statusBar().showMessage(m))
            self._timeline_tab.jump_requested.connect(lambda d, t, r: self.open_table(d, t, r))
            self._timeline_tab.extract_requested.connect(self._extract_grid)
            self.tabs.addTab(self._timeline_tab, "Timeline")
        self._timeline_tab.refresh_databases()
        self.tabs.setCurrentWidget(self._timeline_tab)

    def show_stats(self) -> None:
        tab = self._current_tab()
        if tab is None:
            QMessageBox.information(self, "Statistics", "Open a table first.")
            return
        self._show_stats_for(tab.db.id, tab.table.name)

    def _show_stats_for(self, db_id: str, table: str) -> None:
        try:
            db = self.session.get(db_id)
        except Exception as exc:
            QMessageBox.warning(self, "Statistics", str(exc))
            return
        dlg = StatsDialog(db, table, self.tasks, self)
        dlg.setModal(False)
        dlg.show()

    def _fill_views_menu(self) -> None:
        self.views_menu.clear()
        any_views = False
        for db in self.session:
            if not db.profile.views:
                continue
            any_views = True
            sub = self.views_menu.addMenu(f"{db.path.name}  -  {db.profile.name}")
            for v in db.profile.views:
                sub.addAction(v.name, lambda checked=False, d=db.id, vid=v.id: self.run_view(d, vid))
        if not any_views:
            self.views_menu.addAction("(no views for the open databases)").setEnabled(False)

    def _query_row_selected(self, db_id: str, title: str, row_index: int, row: dict[str, Any]) -> None:
        from edb_explorer.core import ColumnInfo

        cols = tuple(ColumnInfo(i + 1, k, "ANY", 0, "dynamic", None, None, False, False) for i, k in enumerate(row))
        self.inspector.show_record(db_id, title, row_index, row, cols)

    def _extract_grid(self, grid: ResultsGrid) -> None:
        db_id = self._current_db_id()
        try:
            db = self.session.get(db_id) if db_id else next(iter(self.session))
        except StopIteration:
            QMessageBox.information(self, "Extract", "Open a database first.")
            return
        ExportDialog(db, None, grid.rows(), grid.selected_rows(), grid.columns(), self, "results").exec()

    def show_search(self) -> None:
        if self._search_dialog is None:
            self._search_dialog = SearchDialog(self.session, self)
            self._search_dialog.hit_activated.connect(self._goto_hit)
        self._search_dialog.refresh_scope()
        self._search_dialog.show()
        self._search_dialog.raise_()
        self._search_dialog.query.setFocus()

    def _goto_hit(self, db_id: str, table: str, row_index: int, column: str) -> None:
        self.open_table(db_id, table, row_index, column)
        self.raise_()
        self.activateWindow()

    def show_timestamp(self, value: Any = None) -> None:
        TimestampDialog(value, self).show()

    def extract_selection(self) -> None:
        tab = self._current_tab()
        if tab and tab.selected_rows():
            self._export(tab.db.id, tab.table.name, tab, "selection")
        else:
            QMessageBox.information(self, "Extract", "Select one or more rows in a table first.")

    def show_report(self) -> None:
        if not len(self.session):
            QMessageBox.information(self, "Report", "Open a database first.")
            return
        ReportDialog(self.session, self._current_db_id(), self).exec()

    def export_current(self) -> None:
        tab = self._current_tab()
        if tab:
            self._export(tab.db.id, tab.table.name, tab)
        elif self._current_db_id():
            self._export(self._current_db_id() or "", None)
        else:
            QMessageBox.information(self, "Export", "Open a database first.")

    def _export(self, db_id: str, table: str | None, tab: TableTab | None = None, scope: str = "table") -> None:
        try:
            db = self.session.get(db_id)
        except Exception as exc:
            QMessageBox.warning(self, "Export", str(exc))
            return
        if tab is None and table:
            for i in range(self.tabs.count()):
                w = self.tabs.widget(i)
                if isinstance(w, TableTab) and w.db is db and w.table.name == table:
                    tab = w
                    break
        rows = tab.current_rows() if tab else None
        selected = tab.selected_rows() if tab else None
        cols = tab.visible_columns() if tab else None
        ExportDialog(db, table, rows, selected, cols, self, scope).exec()

    def _count_all(self, db_id: str | None) -> None:
        if not db_id:
            return
        db = self.session.get(db_id)
        self.statusBar().showMessage(f"Counting records in {db.path.name}…")

        def job(progress: Any, should_stop: Any) -> int:
            for t in db.tables():
                if should_stop():
                    break
                db.count_records(t.name)
                progress(t.name)
            return len(db.tables())

        worker = FunctionWorker(job, parent=self)
        worker.progress.connect(lambda name: self.statusBar().showMessage(f"Counted {name}"))
        worker.result.connect(
            lambda _n: (self.tree.refresh_counts(db), self.statusBar().showMessage("Record counts updated", 5000))
        )
        worker.failed.connect(lambda m: self.statusBar().showMessage(f"Count failed: {m}"))
        self._workers.append(worker)
        worker.start()

    def _set_row_limit(self) -> None:
        from PySide6.QtWidgets import QInputDialog

        value, ok = QInputDialog.getInt(
            self,
            "Row limit",
            "Maximum rows to load per table (protects memory on huge tables):",
            self.max_rows,
            1000,
            50_000_000,
            100_000,
        )
        if ok:
            self.max_rows = value
            self.settings.setValue("max_rows", value)

    def _toggle_theme(self, checked: bool) -> None:
        name = "dark" if checked else "light"
        apply_theme(QApplication.instance(), name)  # type: ignore[arg-type]
        self.settings.setValue("theme", name)

    def _reset_layout(self) -> None:
        for d in (self.dock_tree, self.dock_info, self.dock_inspector):
            d.setFloating(False)
            d.show()
        self.addDockWidget(Qt.DockWidgetArea.LeftDockWidgetArea, self.dock_tree)
        self.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, self.dock_info)
        self.addDockWidget(Qt.DockWidgetArea.BottomDockWidgetArea, self.dock_inspector)
        self.resizeDocks([self.dock_tree, self.dock_info], [320, 340], Qt.Orientation.Horizontal)
        self.resizeDocks([self.dock_inspector], [260], Qt.Orientation.Vertical)

    def _show_mcp_help(self) -> None:
        import shutil
        import sys

        exe = shutil.which("edb-explorer") or f"{sys.executable} -m edb_explorer"
        text = (
            "<p>Add this to your MCP client configuration (Claude Desktop / Claude Code / Cursor):</p>"
            '<pre>{\n  "mcpServers": {\n    "edb-explorer": {\n'
            f'      "command": "{exe}",\n      "args": ["mcp"]\n    }}\n  }}\n}}</pre>'
            "<p>Claude Code one-liner:<br><code>claude mcp add edb-explorer -- edb-explorer mcp</code></p>"
            "<p>Restrict which folders the agent may read with <code>--allow /path/to/evidence</code>.</p>"
        )
        QMessageBox.information(self, "MCP server setup", text)

    # ------------------------------------------------------------------ #
    # Recent files / settings
    # ------------------------------------------------------------------ #
    def _recent(self) -> list[str]:
        val = self.settings.value("recent", [])
        return [str(v) for v in (val if isinstance(val, list) else [val] if val else [])]

    def _add_recent(self, path: str) -> None:
        items = [p for p in self._recent() if p != path]
        items.insert(0, path)
        self.settings.setValue("recent", items[:MAX_RECENT])
        self._update_recent_menu()

    def _update_recent_menu(self) -> None:
        self.recent_menu.clear()
        recent = self._recent()
        self._placeholder.set_recent(recent)
        for p in recent:
            act = self.recent_menu.addAction(p)
            act.triggered.connect(lambda _c=False, path=p: self.open_files([path]))
        if recent:
            self.recent_menu.addSeparator()
            self.recent_menu.addAction(
                "Clear list", lambda: (self.settings.setValue("recent", []), self._update_recent_menu())
            )
        else:
            self.recent_menu.addAction("(empty)").setEnabled(False)

    def _restore_state(self) -> None:
        geo = self.settings.value("geometry")
        if isinstance(geo, QByteArray):
            self.restoreGeometry(geo)
        state = self.settings.value("window_state")
        if isinstance(state, QByteArray):
            self.restoreState(state)

    # ------------------------------------------------------------------ #
    # Drag & drop / close
    # ------------------------------------------------------------------ #
    def dragEnterEvent(self, event: QDragEnterEvent) -> None:
        if event.mimeData().hasUrls():
            event.acceptProposedAction()

    def dropEvent(self, event: QDropEvent) -> None:
        paths: list[str] = []
        for url in event.mimeData().urls():
            p = Path(url.toLocalFile())
            if p.is_dir():
                if self.session.detect(p) == "leveldb":
                    paths.append(str(p))
                else:
                    paths.extend(str(x) for x in self.session.scan(p))
            elif p.is_file():
                paths.append(str(p))
        if paths:
            self.open_files(paths)

    def closeEvent(self, event: QCloseEvent) -> None:
        self.settings.setValue("geometry", self.saveGeometry())
        self.settings.setValue("window_state", self.saveState())
        for i in range(self.tabs.count()):
            w = self.tabs.widget(i)
            if hasattr(w, "shutdown"):
                w.shutdown()
        self.tasks.cancel_all()
        for worker in self._workers:
            worker.cancel()
            worker.wait(2000)
        if self._search_dialog:
            self._search_dialog.close()
        self.session.close_all()
        self.workspace.close()
        super().closeEvent(event)


__all__ = ["ESE_EXTENSIONS", "MainWindow", "QWidget", "__version__"]
