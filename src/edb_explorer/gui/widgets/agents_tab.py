"""AI agents tab: run Claude Code / Codex / Gemini / Copilot / Aider (or a shell) inside the app with the MCP server pre-configured."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

from PySide6.QtCore import QSettings, Qt, Signal
from PySide6.QtWidgets import (
    QApplication,
    QComboBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QTabWidget,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from edb_explorer.core import Session
from edb_explorer.core.agents import (
    AGENTS,
    AgentSpec,
    agent_status,
    configure_agent,
    find_executable,
    mcp_config_snippet,
)
from edb_explorer.gui.icons import std
from edb_explorer.gui.terminal import TerminalWidget, terminal_available


class AgentsTab(QWidget):
    status = Signal(str)

    def __init__(self, session: Session, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.session = session
        layout = QVBoxLayout(self)
        layout.setContentsMargins(6, 6, 6, 6)

        bar = QHBoxLayout()
        bar.addWidget(QLabel("Agent:"))
        self.agent = QComboBox()
        for a in AGENTS:
            self.agent.addItem(a.name, a.id)
        self.agent.addItem("Custom command…", "custom")
        self.agent.currentIndexChanged.connect(self._refresh_status)
        bar.addWidget(self.agent)
        self.custom_cmd = QLineEdit()
        self.custom_cmd.setPlaceholderText("custom command, e.g. ollama run llama3  /  opencode")
        self.custom_cmd.hide()
        bar.addWidget(self.custom_cmd, 1)
        self.state = QLabel("")
        self.state.setObjectName("dim")
        bar.addWidget(self.state, 1)
        self.configure_btn = QPushButton("Configure MCP")
        self.configure_btn.setToolTip(
            "Register this application's MCP server with the selected agent so it can query the open databases"
        )
        self.configure_btn.clicked.connect(self.configure)
        bar.addWidget(self.configure_btn)
        self.login_btn = QPushButton("Login")
        self.login_btn.clicked.connect(self.login)
        bar.addWidget(self.login_btn)
        self.start_btn = QPushButton("Start agent")
        self.start_btn.setIcon(std("SP_MediaPlay"))
        self.start_btn.clicked.connect(self.start_agent)
        bar.addWidget(self.start_btn)
        self.shell_btn = QPushButton("Shell")
        self.shell_btn.clicked.connect(
            lambda: self._open_terminal(
                [os.environ.get("SHELL", "cmd" if sys.platform == "win32" else "bash")], "shell"
            )
        )
        bar.addWidget(self.shell_btn)
        more = QToolButton()
        more.setText("More ▾")
        more.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        from PySide6.QtWidgets import QMenu

        menu = QMenu(self)
        menu.addAction("Copy MCP config JSON (for Cursor, VS Code, Windsurf, Claude Desktop…)", self.copy_config)
        menu.addAction("Open agent in the system terminal instead", self.open_system_terminal)
        menu.addAction("Show MCP server command", self.show_command)
        more.setMenu(menu)
        bar.addWidget(more)
        layout.addLayout(bar)

        self.hint = QLabel("")
        self.hint.setObjectName("dim")
        self.hint.setWordWrap(True)
        layout.addWidget(self.hint)

        self.tabs = QTabWidget()
        self.tabs.setTabsClosable(True)
        self.tabs.setDocumentMode(True)
        self.tabs.tabCloseRequested.connect(self._close_terminal)
        layout.addWidget(self.tabs, 1)
        ok, why = terminal_available()
        if not ok:
            self.hint.setText(f"Embedded terminal unavailable: {why}. Use 'More ▸ Open agent in the system terminal'.")
        self._refresh_status()

    # ------------------------------------------------------------------ #
    def _spec(self) -> AgentSpec | None:
        aid = self.agent.currentData()
        if aid == "custom":
            return None
        return next(a for a in AGENTS if a.id == aid)

    def _allowed_dirs(self) -> list[str]:
        dirs: list[str] = []
        for db in self.session:
            d = str(db.path if db.path.is_dir() else db.path.parent)
            if d not in dirs:
                dirs.append(d)
        return dirs

    def _refresh_status(self) -> None:
        spec = self._spec()
        self.custom_cmd.setVisible(spec is None)
        if spec is None:
            self.state.setText("")
            self.hint.setText(
                "Any interactive command can run here. Paste the MCP config (More ▸ Copy MCP config JSON) into that tool's settings."
            )
            self.configure_btn.setEnabled(False)
            self.login_btn.setEnabled(False)
            return
        st = agent_status(spec)
        if st["installed"]:
            self.state.setText(f"✔ {st['version'] or 'installed'}  ({st['path']})")
        else:
            self.state.setText("✘ not installed")
        self.configure_btn.setEnabled(bool(st["installed"]) and spec.supports_mcp)
        self.login_btn.setEnabled(bool(st["installed"]) and spec.login_args is not None)
        self.start_btn.setEnabled(bool(st["installed"]))
        hint = spec.notes
        if not st["installed"] and spec.install_hint:
            hint = f"Install: {spec.install_hint}"
        elif spec.login_hint:
            hint = f"{spec.login_hint}  {spec.notes}".strip()
        self.hint.setText(hint)

    # ------------------------------------------------------------------ #
    def configure(self) -> None:
        spec = self._spec()
        if spec is None:
            return
        try:
            msg = configure_agent(spec, self._allowed_dirs())
        except Exception as exc:
            msg = f"Failed: {exc}"
        self.status.emit(msg)
        QMessageBox.information(
            self,
            "Configure MCP",
            msg + "\n\nThe agent can now call tools like open_database, run_sql, timeline and generate_report.\n"
            "Restart the agent if it is already running.",
        )

    def login(self) -> None:
        spec = self._spec()
        if spec is None or spec.login_args is None:
            return
        exe = find_executable(spec.command)
        if exe:
            self._open_terminal([exe, *spec.login_args], f"{spec.name} login")

    def start_agent(self) -> None:
        spec = self._spec()
        if spec is None:
            cmd = self.custom_cmd.text().strip()
            if not cmd:
                QMessageBox.information(self, "Agent", "Enter a command first.")
                return
            self._open_terminal(cmd, cmd.split()[0])
            return
        exe = find_executable(spec.command)
        if not exe:
            QMessageBox.information(self, "Agent", f"{spec.name} is not installed.\n{spec.install_hint}")
            return
        self._open_terminal([exe, *spec.start_args], spec.name)

    def _open_terminal(self, argv: list[str] | str, title: str) -> None:
        ok, why = terminal_available()
        if not ok:
            QMessageBox.warning(self, "Terminal", why)
            return
        term = TerminalWidget()
        cwd = str(QSettings().value("last_dir", str(Path.home())))
        dirs = self._allowed_dirs()
        env = {"EDB_EXPLORER_OPEN_DATABASES": os.pathsep.join(str(db.path) for db in self.session)}
        idx = self.tabs.addTab(term, title)
        self.tabs.setCurrentIndex(idx)
        term.exited.connect(lambda code, t=term: self._on_exit(t, code))
        try:
            term.start(argv, cwd=dirs[0] if dirs else cwd, env=env)
        except Exception as exc:
            QMessageBox.warning(self, "Terminal", f"Could not start {argv}: {exc}")
            self.tabs.removeTab(idx)
            term.deleteLater()
            return
        term.setFocus()
        self.status.emit(f"Started {title}")

    def _on_exit(self, term: TerminalWidget, code: int) -> None:
        i = self.tabs.indexOf(term)
        if i >= 0:
            self.tabs.setTabText(i, self.tabs.tabText(i) + f" (exited {code})")

    def _close_terminal(self, index: int) -> None:
        w = self.tabs.widget(index)
        if isinstance(w, TerminalWidget):
            w.terminate()
        self.tabs.removeTab(index)
        if w is not None:
            w.deleteLater()

    def copy_config(self) -> None:
        text = json.dumps(mcp_config_snippet(self._allowed_dirs()), indent=2)
        QApplication.clipboard().setText(text)
        self.status.emit("MCP configuration copied to clipboard")
        QMessageBox.information(self, "MCP configuration", "Copied to clipboard:\n\n" + text)

    def show_command(self) -> None:
        from edb_explorer.core.agents import mcp_server_command

        QMessageBox.information(self, "MCP server command", " ".join(mcp_server_command(self._allowed_dirs())))

    def open_system_terminal(self) -> None:
        spec = self._spec()
        cmd = (
            [find_executable(spec.command) or spec.command, *spec.start_args]
            if spec
            else self.custom_cmd.text().split()
        )
        if not cmd or not cmd[0]:
            return
        try:
            if sys.platform == "win32":
                subprocess.Popen(["cmd", "/c", "start", "", *cmd])
            elif sys.platform == "darwin":
                script = " ".join(cmd).replace('"', '\\"')
                subprocess.Popen(["osascript", "-e", f'tell app "Terminal" to do script "{script}"'])
            else:
                for term in ("x-terminal-emulator", "gnome-terminal", "konsole", "xfce4-terminal", "xterm"):
                    if find_executable(term):
                        flag = ["--"] if term in ("gnome-terminal", "x-terminal-emulator") else ["-e"]
                        subprocess.Popen([term, *flag, *cmd])
                        break
                else:
                    raise OSError("no terminal emulator found")
        except OSError as exc:
            QMessageBox.warning(self, "Terminal", str(exc))

    def shutdown(self) -> None:
        for i in range(self.tabs.count()):
            w = self.tabs.widget(i)
            if isinstance(w, TerminalWidget):
                w.terminate()

    def sizeHint(self) -> Any:
        from PySide6.QtCore import QSize

        return QSize(900, 500)


__all__ = ["AgentsTab", "Qt"]
