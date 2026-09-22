"""Main window: menus, docks, tabs and the glue between the session and the widgets."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from PySide6.QtCore import QByteArray, QEvent, QSettings, Qt, QTimer, QUrl
from PySide6.QtGui import (
    QAction,
    QActionGroup,
    QCloseEvent,
    QDesktopServices,
    QDragEnterEvent,
    QDropEvent,
    QKeySequence,
)
from PySide6.QtWidgets import (
    QApplication,
    QDialog,
    QDockWidget,
    QFileDialog,
    QMainWindow,
    QMenu,
    QMessageBox,
    QSizePolicy,
    QTabWidget,
    QWidget,
)

from edb_explorer import __app_name__, __version__
from edb_explorer.core import EdbDatabase, Session
from edb_explorer.core.backends import KINDS
from edb_explorer.core.session import ESE_EXTENSIONS
from edb_explorer.core.sqlworkspace import SqlWorkspace
from edb_explorer.gui.icons import app_icon, icon, kind_icon
from edb_explorer.gui.shortcuts import ShortcutRegistry
from edb_explorer.gui.tasks import TaskManager, TaskPanel
from edb_explorer.gui.theme import THEMES, apply_theme, current_theme, theme_preference
from edb_explorer.gui.widgets.agents_tab import AgentsTab
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
from edb_explorer.gui.widgets.mailbox_tab import MailboxTab
from edb_explorer.gui.widgets.project_dialogs import PROJECT_FILTER, ExportProjectDialog, ImportProjectDialog
from edb_explorer.gui.widgets.query_tab import QueryTab, ResultsGrid, TimelineTab
from edb_explorer.gui.widgets.settings_dialog import (
    SettingsDialog,
    apply_cache_dir,
    cache_dir_setting,
    memory_rows_setting,
)
from edb_explorer.gui.widgets.shortcuts_dialog import ShortcutsDialog
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
    "SQL dumps (*.sql);;BSON dumps (*.bson);;Windows Event Log (*.evtx);;All files (*)"
)
MAX_RECENT = 12
_GUARDED_TASKS = frozenset({"Exporting", "Importing", "Extracting", "Generating", "Writing"})


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
        self.memory_rows = memory_rows_setting(self.settings)
        apply_cache_dir(cache_dir_setting(self.settings))
        self.tasks = TaskManager(self)
        self.shortcuts = ShortcutRegistry(self.settings, self)
        self._toolbar_tips: list[tuple[QAction, str, QAction]] = []
        self.workspace = SqlWorkspace()
        self._sql_tab: QueryTab | None = None
        self._timeline_tab: TimelineTab | None = None
        self._agents_tab: AgentsTab | None = None

        self._build_central()
        self._build_docks()
        self._build_menus()
        self._restore_state()
        self._update_recent_menu()
        self.tabs.currentChanged.connect(self._update_title)
        self._update_title()
        from edb_explorer import portable

        pdata = portable.data_dir()
        self.statusBar().showMessage(
            f"Portable mode - settings, keys and caches live in {pdata}"
            if pdata
            else "Open a database with File ▸ Open, or drop files here."
        )
        if self.settings.value("restore_session", True, type=bool) and self.last_session():
            QTimer.singleShot(0, self._maybe_reopen_last_session)

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
        self.tabs.tabBar().installEventFilter(self)  # middle-click closes a tab
        placeholder = WelcomePage()
        placeholder.open_files.connect(self.open_files_dialog)
        placeholder.scan_folder.connect(self.open_folder_dialog)
        placeholder.open_recent.connect(lambda p: self.open_files([p]))
        placeholder.clear_recent.connect(lambda: (self.settings.setValue("recent", []), self._update_recent_menu()))
        placeholder.import_project.connect(lambda: self.import_project_dialog())
        placeholder.reopen_session.connect(self.reopen_last_session)
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
        self.tree.mailboxes_activated.connect(self.show_mailboxes)
        self.tree.stats_requested.connect(lambda db_id, t: self._show_stats_for(db_id, t))
        self.tree.sql_requested.connect(lambda db_id: self.show_sql(db_id))
        self.tree.reveal_requested.connect(lambda p: self._reveal(Path(p)))
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
        a_open = self._act(
            file_menu, "&Open database(s)…", self.open_files_dialog, QKeySequence.StandardKey.Open, "open"
        )
        a_scan = self._act(file_menu, "Open &folder (scan)…", self.open_folder_dialog, "Ctrl+Shift+O", "scan")
        self.recent_menu = file_menu.addMenu("Open &recent")
        self.recent_menu.setIcon(icon("history"))
        self._act(file_menu, "Reopen &last session", self.reopen_last_session, "Ctrl+Shift+T", "history")
        file_menu.addSeparator()
        self._act(file_menu, "&Import project…", self.import_project_dialog, "Ctrl+Shift+I", "project")
        self._act(file_menu, "Export &project…", self.export_project_dialog, "Ctrl+Shift+P", "project")
        file_menu.addSeparator()
        a_extract = self._act(file_menu, "&Extract / export…", self.export_current, "Ctrl+E", "extract")
        self._act(file_menu, "Extract &selected rows…", self.extract_selection, "Ctrl+Shift+E", "rows")
        a_report = self._act(file_menu, "Generate &report…", self.show_report, "Ctrl+R", "report")
        file_menu.addSeparator()
        self._act(
            file_menu, "Close &tab", lambda: self._close_tab(self.tabs.currentIndex()), QKeySequence.StandardKey.Close
        )
        self._act(file_menu, "Close &database", self._close_current_database, "Ctrl+Shift+W")
        self._act(file_menu, "Close &all databases", self.close_all_databases)
        file_menu.addSeparator()
        self._act(file_menu, "&Quit", self.close, QKeySequence.StandardKey.Quit)

        view = mb.addMenu("&View")
        for dock in (self.dock_tree, self.dock_info, self.dock_inspector, self.dock_tasks):
            toggle = dock.toggleViewAction()
            view.addAction(toggle)
            self.shortcuts.register(toggle, "View", description=f"Show / hide the {toggle.text()} panel")
        view.addSeparator()
        self._act(view, "Collapse all databases", lambda: self.tree.collapse_all(), "Ctrl+Shift+-", "collapse")
        self._act(view, "Expand all databases", lambda: self.tree.expand_all(), "Ctrl+Shift+=", "expand")
        view.addSeparator()
        theme_menu = view.addMenu("&Theme")
        self._theme_group = QActionGroup(self)
        self._theme_actions: dict[str, QAction] = {}
        for pref, label, glyph in (
            ("light", "&Light", "sun"),
            ("dark", "&Dark", "moon"),
            ("system", "Follow &system", None),
        ):
            act = QAction(label, self)
            act.setCheckable(True)
            if glyph:
                act.setIcon(icon(glyph))
            act.triggered.connect(lambda _c=False, p=pref: self.set_theme(p))
            self._theme_group.addAction(act)
            theme_menu.addAction(act)
            self._theme_actions[pref] = act
            self.shortcuts.register(act, "View", f"view.theme_{pref}", description="Switch the colour theme")
        self.theme_toggle = self._act(view, "Toggle light / dark", self._toggle_theme, "Ctrl+Shift+D")
        self._act(view, "Reset &layout", self._reset_layout)

        analysis = mb.addMenu("&Analysis")
        a_sql = self._act(analysis, "&SQL console", self.show_sql, "Ctrl+Q", "sql")
        a_timeline = self._act(analysis, "&Timeline", self.show_timeline, "Ctrl+L", "timeline")
        self._act(analysis, "Column &statistics for current table…", self.show_stats, "Ctrl+I", "stats")
        a_mail = self._act(analysis, "Exchange &mailbox viewer", lambda: self.show_mailboxes(None), "Ctrl+M", "mailbox")
        analysis.addSeparator()
        a_agents = self._act(
            analysis, "&AI agents (Claude Code, Codex, Gemini…)", self.show_agents, "Ctrl+Shift+A", "agents"
        )
        self.views_menu = analysis.addMenu("Artifact &views")
        self.views_menu.aboutToShow.connect(self._fill_views_menu)

        tools = mb.addMenu("&Tools")
        a_search = self._act(tools, "&Find in database(s)…", self.show_search, "Ctrl+Shift+F", "search")
        self._act(tools, "Filter &rows in current table", self._focus_filter, QKeySequence.StandardKey.Find, "filter")
        self._act(tools, "&Timestamp decoder…", lambda: self.show_timestamp(None), "Ctrl+T", "clock")
        self._act(tools, "&Count records in all tables", lambda: self._count_all(self._current_db_id()))

        settings_menu = mb.addMenu("&Settings")
        self._act(settings_menu, "&Preferences…  (rows kept in memory, disk cache)", self.show_settings, "Ctrl+,")
        self._act(settings_menu, "&Keyboard shortcuts…", self.show_shortcuts, "Ctrl+Shift+K", "keyboard")

        help_menu = mb.addMenu("&Help")
        self._act(help_menu, "&Keyboard shortcuts", self.show_shortcuts, None, "keyboard", rebindable=False)
        self._act(help_menu, "&MCP server setup…", self._show_mcp_help)
        self._act(help_menu, "Check for &updates…", self.check_for_updates)
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
        tb.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextUnderIcon)
        for text, slot, glyph, tip, menu_action in (
            ("Open", self.open_files_dialog, "open", "Open database file(s)", a_open),
            ("Scan", self.open_folder_dialog, "scan", "Scan a folder or mounted image for databases", a_scan),
            ("Search", self.show_search, "search", "Find text across every table of the open databases", a_search),
            ("SQL", self.show_sql, "sql", "SQL console over any format", a_sql),
            ("Timeline", self.show_timeline, "timeline", "Timeline of every timestamp column", a_timeline),
            ("Mail", lambda: self.show_mailboxes(None), "mailbox", "Exchange mailbox viewer", a_mail),
            ("Agents", self.show_agents, "agents", "AI agents: Claude Code, Codex, Gemini…", a_agents),
            ("Extract", self.export_current, "extract", "Extract / export the current table or results", a_extract),
            ("Report", self.show_report, "report", "Generate a report", a_report),
        ):
            act = self._act(None, text, slot, None, glyph)
            self._toolbar_tips.append((act, tip, menu_action))
            tb.addAction(act)
        self._refresh_toolbar_tips()
        self.shortcuts.changed.connect(self._refresh_toolbar_tips)
        spacer = QWidget()
        spacer.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        tb.addWidget(spacer)
        self.theme_button = self._act(None, "Theme", self._toggle_theme, None)
        tb.addAction(self.theme_button)
        self._sync_theme_ui()

    def _act(
        self,
        menu: QMenu | None,
        text: str,
        slot: Any,
        shortcut: Any = None,
        glyph: str | None = None,
        rebindable: bool = True,
    ) -> QAction:
        act = QAction(text, self)
        if glyph:
            act.setIcon(icon(glyph))
        if shortcut:
            act.setShortcut(QKeySequence(shortcut) if isinstance(shortcut, str) else shortcut)
        act.triggered.connect(slot)
        if menu is not None:
            menu.addAction(act)
            if rebindable:
                # the registry applies the user's override (Settings ▸ Keyboard shortcuts) on top of the default
                self.shortcuts.register(act, menu.title())
        return act

    def _refresh_toolbar_tips(self) -> None:
        """Toolbar tooltips quote the menu action's *current* shortcut."""
        from edb_explorer.gui.shortcuts import key_text

        for act, tip, menu_action in self._toolbar_tips:
            key = key_text(menu_action.shortcut())
            act.setToolTip(f"{tip}  ({key})" if key else tip)

    def show_shortcuts(self) -> None:
        ShortcutsDialog(self.shortcuts, self).exec()

    # ------------------------------------------------------------------ #
    # Opening databases
    # ------------------------------------------------------------------ #
    def open_files_dialog(self) -> None:
        start = self.settings.value("last_dir", str(Path.home()))
        files, _ = QFileDialog.getOpenFileNames(self, "Open database(s)", start, FILE_FILTER)
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

    def _open_one(self, path: str, db_id: str | None = None, done: Any = None) -> None:
        """Open one file in the background; ``db_id`` pins the session id (project import), ``done`` runs after."""
        name = Path(path).name
        task = self.tasks.start(f"Opening {name}", detail=path)

        def job(progress: Any, should_stop: Any) -> tuple[list[EdbDatabase], dict[str, str]]:
            progress("detecting format")
            kind = self.session.detect(path)
            progress(f"parsing {kind or 'file'} catalog")
            if db_id:
                try:
                    return [self.session.open(path, db_id=db_id)], {}
                except Exception as exc:
                    return [], {path: f"{exc.__class__.__name__}: {exc}"}
            return self.session.open_many([path])

        worker = FunctionWorker(job, parent=self)
        worker.progress.connect(lambda m: task.progress(detail=str(m)))
        worker.result.connect(
            lambda r: (
                self._opened(r, auto_open=db_id is None),  # a project / session restore opens its own tabs
                task.finish(
                    f"{r[0][0].info.kind_name} · {r[0][0].profile.name} · {r[0][0].info.table_count} tables"
                    if r[0]
                    else next(iter(r[1].values()), "failed"),
                    failed=not r[0],
                ),
                done() if done else None,
            )
        )
        worker.failed.connect(
            lambda m: (self._opened(([], {path: m})), task.finish(m, failed=True), done() if done else None)
        )
        self._workers.append(worker)
        worker.start()

    # ------------------------------------------------------------------ #
    # Project files
    # ------------------------------------------------------------------ #
    def capture_workspace(self) -> dict[str, Any]:
        """Open tabs, current tab and dock layout - what a project restores on the other machine."""
        tabs: list[dict[str, Any]] = []
        for i in range(self.tabs.count()):
            w = self.tabs.widget(i)
            if isinstance(w, TableTab):
                tabs.append(
                    {
                        "kind": "table",
                        "db": w.db.id,
                        "table": w.table.name,
                        "filter": w.filter_edit.text(),
                        "regex": w.regex_btn.isChecked(),
                        "case": w.case_btn.isChecked(),
                        "hidden": sorted(w.model.column_names[c] for c in w.user_hidden),
                    }
                )
            elif isinstance(w, QueryTab):
                tabs.append({"kind": "sql", "text": w.editor.toPlainText()})
            elif isinstance(w, TimelineTab):
                tabs.append({"kind": "timeline"})
            elif isinstance(w, MailboxTab):
                tabs.append({"kind": "mailbox", "db": w.db.id})
        return {
            "tabs": tabs,
            "current": self.tabs.currentIndex(),
            "layout": bytes(self.saveState().toBase64()).decode("ascii"),
            "theme": theme_preference(QApplication.instance()),  # type: ignore[arg-type]
        }

    def export_project_dialog(self) -> None:
        dbs = self.session.databases()
        if not dbs:
            QMessageBox.information(self, "Export project", "Open the databases you want to share first.")
            return
        dlg = ExportProjectDialog(dbs, self.capture_workspace(), self.tasks, self)
        if dlg.exec() == QDialog.DialogCode.Accepted and dlg.result_path:
            self.statusBar().showMessage(f"Project written to {dlg.result_path}", 10000)

    def import_project_dialog(self, path: str | None = None) -> None:
        if not path:
            path, _ = QFileDialog.getOpenFileName(self, "Import project", str(Path.home()), PROJECT_FILTER)
        if not path:
            return
        dlg = ImportProjectDialog(path, self.tasks, self)
        if dlg.exec() != QDialog.DialogCode.Accepted or dlg.result is None:
            return
        self.open_project(dlg.located(), dlg.result.project.workspace)

    def open_project(self, located: list[tuple[str, str]], workspace: dict[str, Any]) -> None:
        """Open the located databases under their project ids, then restore the workspace tabs and layout."""
        pending = {db_id for db_id, _p in located}
        if not pending:
            return

        def one_done(db_id: str) -> None:
            pending.discard(db_id)
            if not pending:
                self.restore_workspace(workspace)

        for db_id, local in located:
            if db_id in self.session:
                pending.discard(db_id)
                continue
            self._open_one(local, db_id=db_id, done=lambda i=db_id: one_done(i))
        if not pending:
            self.restore_workspace(workspace)

    def restore_workspace(self, workspace: dict[str, Any]) -> None:
        opened_tabs = 0
        for spec in workspace.get("tabs") or []:
            kind = spec.get("kind")
            try:
                if kind == "table" and spec.get("db") in self.session:
                    tab = self.open_table(spec["db"], spec["table"])
                    if tab is not None:
                        opened_tabs += 1
                        if spec.get("filter"):
                            tab.regex_btn.setChecked(bool(spec.get("regex")))
                            tab.case_btn.setChecked(bool(spec.get("case")))
                            tab.filter_edit.setText(spec["filter"])
                        for name in spec.get("hidden") or []:
                            c = tab.model.column_position(name)
                            if c is not None:
                                tab.user_hidden.add(c)
                elif kind == "sql":
                    self.show_sql(sql=spec.get("text", ""))
                    opened_tabs += 1
                elif kind == "timeline":
                    self.show_timeline()
                    opened_tabs += 1
                elif kind == "mailbox" and spec.get("db") in self.session:
                    self.show_mailboxes(spec["db"])
                    opened_tabs += 1
            except Exception as exc:  # a tab that cannot be restored must not stop the others
                log.warning("Could not restore %s tab: %s", kind, exc)
        layout = workspace.get("layout")
        if layout:
            try:
                self.restoreState(QByteArray.fromBase64(QByteArray(layout.encode("ascii"))))
            except Exception:
                pass
        current = workspace.get("current")
        if isinstance(current, int) and 0 <= current < self.tabs.count():
            self.tabs.setCurrentIndex(current)
        self.statusBar().showMessage(
            f"Project opened: {len(self.session)} database(s), {opened_tabs} tab(s) restored", 10000
        )

    def _opened(self, result: tuple[list[EdbDatabase], dict[str, str]], auto_open: bool = True) -> None:
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
            if auto_open and self.tabs.count() == 1 and self.tabs.widget(0) is self._placeholder and len(opened) == 1:
                # first database: open its most useful view automatically
                from edb_explorer.core.exchange import is_exchange_database

                if is_exchange_database(opened[0]):
                    self.show_mailboxes(opened[0].id)
                else:
                    tables = opened[0].tables(include_system=False)
                    names = {t.name for t in tables}
                    # the profile lists its tables most-useful first (urls before meta, messages before handles)
                    preferred = next((n for n in opened[0].profile.table_names if n in names), None)
                    if preferred or tables:
                        self.open_table(opened[0].id, preferred or tables[0].name)
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
        tab = TableTab(db, info, self.memory_rows)
        tab.row_selected.connect(self._row_selected)
        tab.status.connect(lambda m: self.statusBar().showMessage(m))
        tab.interpret_requested.connect(self.show_timestamp)
        self._track_loader(tab)
        tab.extract_requested.connect(lambda scope, t=tab: self._export(t.db.id, t.table.name, t, scope))
        tab.loader.finished_ok.connect(lambda _n, d=db: self.tree.refresh_counts(d))  # type: ignore[union-attr]
        label = info.display_name if len(info.display_name) <= 32 else info.display_name[:30] + "…"
        idx = self.tabs.addTab(tab, kind_icon(db.kind), label)
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
        loader.store_grew.connect(lambda n, _c: task.progress(n, known or 0, f"{n:,} rows (disk cache)"))
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
        if w is self._agents_tab:
            self._agents_tab = None
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
            w = self.tabs.widget(index)
            menu.addAction("Close tab", lambda: self._close_tab(index))
            menu.addAction("Close other tabs", lambda: self._close_other_tabs(index))
            menu.addAction("Close tabs to the right", lambda: self._close_tabs_right(index))
            if isinstance(w, TableTab):
                menu.addSeparator()
                menu.addAction("Reload table", w.reload)
                menu.addAction("Column statistics…", lambda: self._show_stats_for(w.db.id, w.table.name))
                menu.addAction("Show file in folder", lambda: self._reveal(w.db.path))
        menu.addAction("Close all tabs", lambda: self._close_other_tabs(-1))
        menu.exec(self.tabs.mapToGlobal(pos))

    def _close_tabs_right(self, index: int) -> None:
        for i in reversed(range(index + 1, self.tabs.count())):
            self._close_tab(i)

    def eventFilter(self, obj: Any, event: Any) -> bool:
        if (
            obj is self.tabs.tabBar()
            and event.type() == QEvent.Type.MouseButtonRelease
            and event.button() == Qt.MouseButton.MiddleButton
        ):
            index = self.tabs.tabBar().tabAt(event.position().toPoint())
            if index >= 0:
                self._close_tab(index)
                return True
        return super().eventFilter(obj, event)

    def _update_title(self, *_args: Any) -> None:
        w = self.tabs.currentWidget()
        if isinstance(w, TableTab):
            context = f"{w.table.display_name} · {w.db.path.name}"
        elif isinstance(w, MailboxTab):
            context = f"Mailboxes · {w.db.path.name}"
        elif w is not None and w is not self._placeholder:
            context = self.tabs.tabText(self.tabs.currentIndex()).lstrip("▶ ")
        else:
            context = ""
        self.setWindowTitle(f"{context} - {__app_name__}" if context else __app_name__)

    def _reveal(self, path: Path) -> None:
        """Open the file manager on the folder that holds ``path``."""
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(path.parent if path.is_file() else path)))

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

    def show_mailboxes(self, db_id: str | None) -> None:
        from edb_explorer.core.exchange import is_exchange_database

        candidates = [d for d in self.session if is_exchange_database(d)]
        db = None
        if db_id:
            db = self.session.get(db_id)
        elif candidates:
            current = self._current_db_id()
            db = next((d for d in candidates if d.id == current), candidates[0])
        if db is None or not is_exchange_database(db):
            QMessageBox.information(
                self,
                "Mailbox viewer",
                "Open an Exchange mailbox database (.edb with Mailbox/Folder/Message tables) first.",
            )
            return
        for i in range(self.tabs.count()):
            w = self.tabs.widget(i)
            if isinstance(w, MailboxTab) and w.db is db:
                self.tabs.setCurrentIndex(i)
                return
        self._drop_placeholder()
        tab = MailboxTab(db, self.tasks, self)
        tab.status.connect(lambda m: self.statusBar().showMessage(m))
        idx = self.tabs.addTab(tab, f"✉ {db.path.name}")
        self.tabs.setTabToolTip(idx, f"Exchange mailbox viewer\n{db.path}")
        self.tabs.setCurrentIndex(idx)

    def show_agents(self) -> None:
        if self._agents_tab is None:
            self._drop_placeholder()
            self._agents_tab = AgentsTab(self.session, self)
            self._agents_tab.status.connect(lambda m: self.statusBar().showMessage(m))
            self.tabs.addTab(self._agents_tab, "AI agents")
        self.tabs.setCurrentWidget(self._agents_tab)

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

    def show_settings(self) -> None:
        dlg = SettingsDialog(self.settings, self)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            self.memory_rows = dlg.memory_rows
            self.statusBar().showMessage(
                f"Preferences saved: up to {self.memory_rows:,} rows per table kept in memory"
                + (f", disk cache in {dlg.cache_dir}" if dlg.cache_dir else ""),
                8000,
            )

    def set_theme(self, preference: str) -> None:
        """Switch to ``light``, ``dark`` or ``system`` and remember it."""
        if preference not in THEMES:
            preference = "dark"
        apply_theme(QApplication.instance(), preference)  # type: ignore[arg-type]
        self.settings.setValue("theme", preference)
        self._sync_theme_ui()

    def _toggle_theme(self) -> None:
        app = QApplication.instance()
        self.set_theme("light" if current_theme(app) == "dark" else "dark")  # type: ignore[arg-type]

    def _sync_theme_ui(self) -> None:
        app = QApplication.instance()
        shown, pref = current_theme(app), theme_preference(app)  # type: ignore[arg-type]
        for name, act in self._theme_actions.items():
            act.setChecked(name == pref)
        # the toggle shows what you will get when you click it
        glyph, other = ("sun", "light") if shown == "dark" else ("moon", "dark")
        for act in (self.theme_button, self.theme_toggle):
            act.setIcon(icon(glyph))
        self.theme_button.setText(other.capitalize())
        self.theme_button.setToolTip(
            f"Switch to the {other} theme  (Ctrl+Shift+D)"
            + ("  · currently following the system" if pref == "system" else "")
        )

    def _reset_layout(self) -> None:
        for d in (self.dock_tree, self.dock_info, self.dock_inspector):
            d.setFloating(False)
            d.show()
        self.addDockWidget(Qt.DockWidgetArea.LeftDockWidgetArea, self.dock_tree)
        self.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, self.dock_info)
        self.addDockWidget(Qt.DockWidgetArea.BottomDockWidgetArea, self.dock_inspector)
        self.resizeDocks([self.dock_tree, self.dock_info], [320, 340], Qt.Orientation.Horizontal)
        self.resizeDocks([self.dock_inspector], [260], Qt.Orientation.Vertical)

    def _confirm_quit_with_tasks(self, n: int) -> bool:
        answer = QMessageBox.question(
            self,
            "Quit",
            f"{n} export / import task(s) are still running and will be cancelled.\n\nQuit anyway?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        return answer == QMessageBox.StandardButton.Yes

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
        last = self.last_session()
        self._placeholder.set_last_session(
            ", ".join(Path(d["path"]).name for d in last["databases"][:4])
            + ("…" if last and len(last["databases"]) > 4 else "")
            if last
            else None
        )
        for n, p in enumerate(recent, start=1):
            path = Path(p)
            exists = path.exists()
            act = self.recent_menu.addAction(f"&{n}  {path.name}" + ("" if exists else "   (missing)"))
            act.setToolTip(p)
            act.setStatusTip(p)
            act.setEnabled(exists)
            act.triggered.connect(lambda _c=False, path=p: self.open_files([path]))
        if recent:
            self.recent_menu.addSeparator()
            if any(not Path(p).exists() for p in recent):
                self.recent_menu.addAction("Remove missing files", self._prune_recent)
            self.recent_menu.addAction(
                "Clear list", lambda: (self.settings.setValue("recent", []), self._update_recent_menu())
            )
        else:
            self.recent_menu.addAction("(empty)").setEnabled(False)

    def _prune_recent(self) -> None:
        self.settings.setValue("recent", [p for p in self._recent() if Path(p).exists()])
        self._update_recent_menu()

    # ------------------------------------------------------------------ #
    # Last session
    # ------------------------------------------------------------------ #
    def last_session(self) -> dict[str, Any] | None:
        raw = self.settings.value("last_session")
        if not raw:
            return None
        try:
            data = json.loads(str(raw))
        except ValueError:
            return None
        dbs = [d for d in data.get("databases", []) if Path(d.get("path", "")).exists()]
        return {"databases": dbs, "workspace": data.get("workspace") or {}} if dbs else None

    def _save_session(self) -> None:
        if not len(self.session):
            self.settings.remove("last_session")
            return
        data = {
            "databases": [{"id": db.id, "path": str(db.path)} for db in self.session],
            "workspace": self.capture_workspace(),
        }
        self.settings.setValue("last_session", json.dumps(data))

    def _maybe_reopen_last_session(self) -> None:
        if len(self.session):  # files were passed on the command line / dropped already
            return
        self.reopen_last_session()

    def reopen_last_session(self) -> None:
        last = self.last_session()
        if not last:
            self.statusBar().showMessage("No previous session to reopen", 5000)
            return
        self.statusBar().showMessage(f"Reopening last session: {len(last['databases'])} database(s)…", 8000)
        self.open_project([(d["id"], d["path"]) for d in last["databases"]], last["workspace"])

    # ------------------------------------------------------------------ #
    # Updates
    # ------------------------------------------------------------------ #
    def check_for_updates(self) -> None:
        from edb_explorer.core.updates import check_latest_release

        self.statusBar().showMessage("Checking GitHub for a newer release…")
        worker = FunctionWorker(lambda progress, should_stop: check_latest_release(__version__), parent=self)

        def show(result: Any) -> None:
            latest, url, newer = result
            if newer:
                answer = QMessageBox.question(
                    self,
                    "Update available",
                    f"EDB Explorer {latest} is available (you have {__version__}).\n\nOpen the release page?",
                    QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                )
                if answer == QMessageBox.StandardButton.Yes:
                    QDesktopServices.openUrl(QUrl(url))
            else:
                QMessageBox.information(self, "Up to date", f"EDB Explorer {__version__} is the latest release.")
            self.statusBar().clearMessage()

        worker.result.connect(show)
        worker.failed.connect(lambda m: QMessageBox.warning(self, "Check for updates", f"Could not check: {m}"))
        self._workers.append(worker)
        worker.start()

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
            elif p.is_file() and p.suffix.lower() == ".edbproj":
                self.import_project_dialog(str(p))
            elif p.is_file():
                paths.append(str(p))
        if paths:
            self.open_files(paths)

    def closeEvent(self, event: QCloseEvent) -> None:
        # only tasks whose result the user is waiting for (project export / import) are worth a prompt;
        # table loads, searches and queries are simply cancelled
        important = [t for t in self.tasks.running() if t.title.split(" ")[0] in _GUARDED_TASKS]
        if important and not self._confirm_quit_with_tasks(len(important)):
            event.ignore()
            return
        self._save_session()
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
