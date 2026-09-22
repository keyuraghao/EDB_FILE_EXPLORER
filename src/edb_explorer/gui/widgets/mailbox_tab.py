"""Exchange mailbox viewer: mailboxes/folders tree, message list and a preview pane (mail-client layout)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from PySide6.QtCore import QSettings, Qt, Signal
from PySide6.QtGui import QFont, QPalette
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QCheckBox,
    QFileDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMenu,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QTextBrowser,
    QToolButton,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from edb_explorer.core import Database
from edb_explorer.core.exchange import ExchangeStore, MessageDetail, MessageSummary
from edb_explorer.core.exchange.export import (
    MESSAGE_FORMATS,
    export_messages,
    message_to_eml,
    message_to_html,
    message_to_text,
    summaries_to_rows,
)
from edb_explorer.core.export import EXPORT_FORMATS, export_rows
from edb_explorer.gui.icons import icon, kind_icon, std
from edb_explorer.gui.tasks import TaskManager
from edb_explorer.gui.widgets.query_tab import ResultsGrid
from edb_explorer.gui.workers import FunctionWorker

MAILBOX_ROLE = Qt.ItemDataRole.UserRole + 1
FOLDER_ROLE = Qt.ItemDataRole.UserRole + 2
LIST_COLUMNS = ["From", "Subject", "To", "Received", "Sent", "Size", "Attachments", "Read", "Class", "Id"]


def _mono() -> QFont:
    f = QFont("Monospace")
    f.setStyleHint(QFont.StyleHint.Monospace)
    return f


def _row(m: MessageSummary) -> dict[str, Any]:
    sender = (
        f"{m.sender_name} <{m.sender_email}>" if m.sender_email and m.sender_name else (m.sender_email or m.sender_name)
    )
    return {
        "From": sender,
        "Subject": m.subject,
        "To": m.display_to,
        "Received": m.date_received or "",
        "Sent": m.date_sent or "",
        "Size": m.size,
        "Attachments": "📎" if m.has_attachments else "",
        "Read": "" if m.is_read else "●",
        "Class": m.message_class,
        "Id": m.document_id,
        "_row": m.document_id,
        "_mailbox": m.mailbox,
        "_folder": m.folder_id,
    }


class MailboxTab(QWidget):
    status = Signal(str)

    def __init__(self, db: Database, tasks: TaskManager, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.db = db
        self.tasks = tasks
        self.store = ExchangeStore(db)
        self._worker: FunctionWorker | None = None
        self._detail_worker: FunctionWorker | None = None
        self._current: list[MessageSummary] = []
        self._detail: MessageDetail | None = None
        self._by_id: dict[tuple[int, int], MessageSummary] = {}

        layout = QVBoxLayout(self)
        layout.setContentsMargins(6, 6, 6, 6)
        bar = QHBoxLayout()
        self.search = QLineEdit()
        self.search.setPlaceholderText("Search subject / sender / recipients in the selected folder or mailbox")
        self.search.setClearButtonEnabled(True)
        self.search.returnPressed.connect(self._reload_messages)
        bar.addWidget(self.search, 1)
        self.show_system = QCheckBox("System mailboxes")
        self.show_system.toggled.connect(self._build_tree)
        bar.addWidget(self.show_system)
        self.show_hidden = QCheckBox("Hidden items")
        self.show_hidden.toggled.connect(self._reload_messages)
        bar.addWidget(self.show_hidden)
        self.whole_mailbox = QCheckBox("Whole mailbox")
        self.whole_mailbox.setToolTip("List messages of every folder of the selected mailbox")
        self.whole_mailbox.toggled.connect(self._reload_messages)
        bar.addWidget(self.whole_mailbox)
        self.export_btn = QToolButton()
        self.export_btn.setText("Export ▾")
        self.export_btn.setIcon(icon("extract"))
        self.export_btn.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.export_btn.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        self.export_menu = QMenu(self)
        self.export_btn.setMenu(self.export_menu)
        self.export_menu.aboutToShow.connect(self._fill_export_menu)
        bar.addWidget(self.export_btn)
        layout.addLayout(bar)

        outer = QSplitter(Qt.Orientation.Horizontal)
        self.tree = QTreeWidget()
        self.tree.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.tree.setTextElideMode(Qt.TextElideMode.ElideNone)
        self.tree.setHeaderLabels(["Mailbox / folder", "Items"])
        self.tree.header().setStretchLastSection(False)
        self.tree.header().setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        self.tree.setHorizontalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.tree.header().setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        self.tree.setAlternatingRowColors(True)
        self.tree.setIndentation(14)
        self.tree.setMinimumWidth(240)
        self.tree.currentItemChanged.connect(lambda cur, _p: self._folder_selected(cur))
        self.tree.itemExpanded.connect(self._expand_mailbox)
        self.tree.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.tree.customContextMenuRequested.connect(self._tree_menu)
        outer.addWidget(self.tree)

        right = QSplitter(Qt.Orientation.Vertical)
        self.grid = ResultsGrid()
        self.grid.view.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.grid.row_selected.connect(self._message_selected)
        self.grid.status.connect(self.status)
        self.grid.filter_edit.setPlaceholderText("Filter listed messages")
        right.addWidget(self.grid)

        preview = QWidget()
        pl = QVBoxLayout(preview)
        pl.setContentsMargins(0, 0, 0, 0)
        self.header = QLabel("Select a message to preview it.")
        self.header.setObjectName("dim")
        self.header.setWordWrap(True)
        self.header.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        pl.addWidget(self.header)
        self.tabs = QTabWidget()
        self.body_view = QTextBrowser()
        # e-mail HTML is authored for a white page (inline colours, black text): render it on "paper" in
        # both themes instead of letting the dark palette turn it into black-on-dark
        self.body_view.setObjectName("mailBody")
        self.body_view.document().setDefaultStyleSheet("a { color: #0969da; }")
        self.body_view.setOpenExternalLinks(False)
        self.body_view.setOpenLinks(False)
        self.text_view = QPlainTextEdit()
        self.text_view.setReadOnly(True)
        self.text_view.setFont(_mono())
        self.headers_view = QPlainTextEdit()
        self.headers_view.setReadOnly(True)
        self.headers_view.setFont(_mono())
        self.recipients = QTableWidget(0, 4)
        self.recipients.setHorizontalHeaderLabels(["Type", "Name", "Address", "Address type"])
        self.recipients.horizontalHeader().setStretchLastSection(True)
        self.recipients.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.attachments = QTableWidget(0, 5)
        self.attachments.setHorizontalHeaderLabels(["Name", "Size", "Method", "Created", "Inid"])
        self.attachments.horizontalHeader().setStretchLastSection(True)
        self.attachments.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.attachments.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        att_box = QWidget()
        al = QVBoxLayout(att_box)
        al.setContentsMargins(0, 0, 0, 0)
        al.addWidget(self.attachments, 1)
        ab = QHBoxLayout()
        ab.addStretch(1)
        save_att = QPushButton("Save selected attachment…")
        save_att.clicked.connect(self._save_attachment)
        save_all = QPushButton("Save all…")
        save_all.clicked.connect(self._save_all_attachments)
        ab.addWidget(save_att)
        ab.addWidget(save_all)
        al.addLayout(ab)
        self.props = QTreeWidget()
        self.props.setHeaderLabels(["Property", "Value"])
        self.props.setRootIsDecorated(False)
        self.props.setAlternatingRowColors(True)
        self.props.setColumnWidth(0, 300)
        self.tabs.addTab(self.body_view, "Message")
        self.tabs.addTab(self.text_view, "Plain text")
        self.tabs.addTab(self.headers_view, "Internet headers")
        self.tabs.addTab(self.recipients, "Recipients")
        self.tabs.addTab(att_box, "Attachments")
        self.tabs.addTab(self.props, "Properties")
        pl.addWidget(self.tabs, 1)
        right.addWidget(preview)
        right.setStretchFactor(0, 3)
        right.setStretchFactor(1, 4)
        outer.addWidget(right)
        outer.setStretchFactor(0, 1)
        outer.setStretchFactor(1, 2)
        outer.setSizes([420, 800])
        right.setSizes([320, 420])
        layout.addWidget(outer, 1)

        foot = QHBoxLayout()
        self.info = QLabel("")
        self.info.setObjectName("dim")
        foot.addWidget(self.info, 1)
        self.progress = QProgressBar()
        self.progress.setRange(0, 0)
        self.progress.setMaximumWidth(160)
        self.progress.hide()
        foot.addWidget(self.progress)
        layout.addLayout(foot)
        self._build_tree()

    # ------------------------------------------------------------------ #
    # Tree
    # ------------------------------------------------------------------ #
    def _build_tree(self) -> None:
        self.tree.clear()
        boxes = self.store.mailboxes(include_system=self.show_system.isChecked())
        for mb in boxes:
            item = QTreeWidgetItem([mb.display_name, f"{mb.message_count:,}"])
            item.setIcon(0, kind_icon("mailbox"))
            item.setData(0, MAILBOX_ROLE, mb.number)
            item.setToolTip(
                0,
                f"Mailbox #{mb.number}\n{mb.message_size:,} bytes · {mb.deleted_count} deleted · last logon {mb.last_logon or '-'}"
                + ("" if mb.has_tables else "\n(no message tables in this database)"),
            )
            if not mb.has_tables:
                item.setForeground(0, Qt.GlobalColor.gray)
            item.setChildIndicatorPolicy(
                QTreeWidgetItem.ChildIndicatorPolicy.ShowIndicator
                if mb.has_tables
                else QTreeWidgetItem.ChildIndicatorPolicy.DontShowIndicator
            )
            self.tree.addTopLevelItem(item)
        self.info.setText(f"{len(boxes)} mailbox(es) - expand one to see its folders")

    def _expand_mailbox(self, item: QTreeWidgetItem) -> None:
        # Only top-level mailbox items lazily load their folder tree (folder items carry FOLDER_ROLE).
        if item.childCount() or item.data(0, MAILBOX_ROLE) is None or item.data(0, FOLDER_ROLE) is not None:
            return
        number = int(item.data(0, MAILBOX_ROLE))
        folders = self.store.folders(number)
        by_id: dict[str, QTreeWidgetItem] = {}
        for f in folders:
            label = f.display_name
            fi = QTreeWidgetItem([label, f"{f.message_count:,}" if f.message_count else ""])
            fi.setIcon(0, std("SP_DirIcon"))
            fi.setData(0, MAILBOX_ROLE, number)
            fi.setData(0, FOLDER_ROLE, f.folder_id)
            fi.setToolTip(
                0,
                f"{f.path}\n{f.message_count} items · {f.unread_count} unread · {f.container_class or ''}\ncreated {f.creation_time or '-'}",
            )
            if f.special_folder or f.display_name in (
                "Inbox",
                "Sent Items",
                "Deleted Items",
                "Drafts",
                "Outbox",
                "Junk Email",
                "Junk E-mail",
            ):
                fi.setIcon(0, std("SP_DirHomeIcon" if f.display_name == "Inbox" else "SP_DirIcon"))
            parent = by_id.get(f.parent_id or "")
            (parent or item).addChild(fi)
            by_id[f.folder_id] = fi
            if f.display_name in ("Top of Information Store", "IPM_SUBTREE") or f.special_folder == "IPM Subtree":
                fi.setExpanded(True)
        # expand the "Top of Information Store" branch by default
        for fid, fi in by_id.items():
            folder = self.store.folder(number, fid)
            if folder and folder.display_name.lower().startswith("top of information"):
                fi.setExpanded(True)

    def _folder_selected(self, item: QTreeWidgetItem | None) -> None:
        if item is None:
            return
        self._reload_messages()

    def _tree_menu(self, pos: Any) -> None:
        item = self.tree.itemAt(pos)
        if item is None:
            return
        menu = QMenu(self)
        menu.addAction("Export this folder / mailbox as EML…", lambda: self._export_scope("eml", item))
        menu.addAction("Export this folder / mailbox as HTML…", lambda: self._export_scope("html", item))
        menu.addAction("Export this folder / mailbox as TXT…", lambda: self._export_scope("txt", item))
        menu.addAction("Export this folder / mailbox as JSON…", lambda: self._export_scope("json", item))
        menu.exec(self.tree.viewport().mapToGlobal(pos))

    # ------------------------------------------------------------------ #
    # Messages
    # ------------------------------------------------------------------ #
    def _scope(self) -> tuple[int | None, str | None]:
        item = self.tree.currentItem()
        if item is None:
            return None, None
        mailbox = item.data(0, MAILBOX_ROLE)
        folder = item.data(0, FOLDER_ROLE)
        if self.whole_mailbox.isChecked():
            folder = None
        return (int(mailbox) if mailbox is not None else None), folder

    def _reload_messages(self) -> None:
        mailbox, folder = self._scope()
        if mailbox is None:
            return
        if self._worker and self._worker.isRunning():
            self._worker.cancel()
        text = self.search.text().strip() or None
        hidden = self.show_hidden.isChecked()
        task = self.tasks.start(f"Listing messages of mailbox {mailbox}", detail=self.db.path.name)

        def job(progress: Any, should_stop: Any) -> list[MessageSummary]:
            return self.store.messages(mailbox, folder, include_hidden=hidden, text=text)

        self._worker = FunctionWorker(job, parent=self)
        self._worker.result.connect(lambda msgs: (self._show_messages(msgs), task.finish(f"{len(msgs):,} messages")))
        self._worker.failed.connect(
            lambda m: (self.info.setText(f"Error: {m}"), self.progress.hide(), task.finish(m, failed=True))
        )
        self.progress.show()
        self._worker.start()

    def _show_messages(self, msgs: list[MessageSummary]) -> None:
        self.progress.hide()
        self._current = msgs
        self._by_id = {(m.mailbox, m.document_id): m for m in msgs}
        self.grid.set_result(LIST_COLUMNS, [_row(m) for m in msgs])
        self.grid.view.setColumnWidth(1, 360)
        _mailbox, folder = self._scope()
        where = "whole mailbox" if folder is None else "folder"
        self.info.setText(
            f"{len(msgs):,} message(s) in {where} · {sum(1 for m in msgs if m.has_attachments)} with attachments"
        )
        self.status.emit(self.info.text())

    def _message_selected(self, row: dict[str, Any]) -> None:
        key = (row.get("_mailbox"), row.get("_row"))
        summary = self._by_id.get(key)  # type: ignore[arg-type]
        if summary is None:
            return
        if self._detail_worker and self._detail_worker.isRunning():
            self._detail_worker.cancel()
        self.header.setText(f"<i>Loading message {summary.document_id}…</i>")

        def job(progress: Any, should_stop: Any) -> MessageDetail:
            return self.store.message(summary.mailbox, summary.document_id)

        self._detail_worker = FunctionWorker(job, parent=self)
        self._detail_worker.result.connect(self._show_detail)
        self._detail_worker.failed.connect(
            lambda m: self.header.setText(f"<span style='color:#c0392b'>Error: {m}</span>")
        )
        self._detail_worker.start()

    def _show_detail(self, d: MessageDetail) -> None:
        self._detail = d
        s = d.summary
        import html as _h

        fg = QApplication.palette().color(QPalette.ColorRole.WindowText).name()  # the label itself is styled "dim"

        def row(k: str, v: str) -> str:
            return (
                f"<tr><td style='color:#8a9099;padding-right:10px'><b>{k}</b></td>"
                f"<td style='color:{fg}'>{_h.escape(v)}</td></tr>"
                if v
                else ""
            )

        sender = f"{s.sender_name} <{s.sender_email}>" if s.sender_email else s.sender_name
        atts = ", ".join(str(a["name"]) for a in d.attachments)
        self.header.setText(
            "<table>"
            + row("From", sender)
            + row("To", s.display_to)
            + row("Cc", d.display_cc)
            + row("Bcc", d.display_bcc)
            + row("Subject", s.subject)
            + row("Received", s.date_received or "")
            + row("Sent", s.date_sent or "")
            + row("Attachments", atts)
            + row("Class", s.message_class)
            + "</table>"
        )
        if d.body_html:
            self.body_view.setHtml(d.body_html)
        else:
            self.body_view.setPlainText(d.body_text or "(no body)")
        from edb_explorer.core.exchange.export import _strip_html

        self.text_view.setPlainText(d.body_text or (_strip_html(d.body_html) if d.body_html else ""))
        self.headers_view.setPlainText(d.headers or "(no internet headers stored for this message)")
        self.recipients.setRowCount(0)
        for r in d.recipients:
            i = self.recipients.rowCount()
            self.recipients.insertRow(i)
            for c, v in enumerate((r["type"], r["name"], r["email"], r["address_type"])):
                self.recipients.setItem(i, c, QTableWidgetItem(str(v)))
        self.attachments.setRowCount(0)
        for a in d.attachments:
            i = self.attachments.rowCount()
            self.attachments.insertRow(i)
            for c, v in enumerate((a["name"], f"{a['size']:,}", a["method"], a["created"] or "", a["inid"])):
                self.attachments.setItem(i, c, QTableWidgetItem(str(v)))
        self.tabs.setTabText(4, f"Attachments ({len(d.attachments)})" if d.attachments else "Attachments")
        self.tabs.setTabText(3, f"Recipients ({len(d.recipients)})" if d.recipients else "Recipients")
        self.props.clear()
        for k, v in d.columns.items():
            self.props.addTopLevelItem(QTreeWidgetItem([f"column: {k}", str(v)]))
        for k, v in d.properties.items():
            self.props.addTopLevelItem(QTreeWidgetItem([k, str(v)[:2000]]))

    # ------------------------------------------------------------------ #
    # Export
    # ------------------------------------------------------------------ #
    def _fill_export_menu(self) -> None:
        self.export_menu.clear()
        sel = self.export_menu.addMenu("Selected messages")
        for fmt in MESSAGE_FORMATS:
            sel.addAction(fmt.upper(), lambda f=fmt: self._export_selected(f))
        lst = self.export_menu.addMenu("Message list (as displayed)")
        for fmt in EXPORT_FORMATS:
            lst.addAction(fmt, lambda f=fmt: self._export_list(f))
        scope = self.export_menu.addMenu("Current folder / mailbox")
        for fmt in MESSAGE_FORMATS:
            scope.addAction(fmt.upper(), lambda f=fmt: self._export_scope(f, None))
        self.export_menu.addSeparator()
        self.export_menu.addAction("Copy message as EML to clipboard", self._copy_eml)

    def _selected_summaries(self) -> list[MessageSummary]:
        rows = self.grid.selected_rows()
        out = []
        for r in rows:
            m = self._by_id.get((r.get("_mailbox"), r.get("_row")))  # type: ignore[arg-type]
            if m:
                out.append(m)
        return out

    def _export_selected(self, fmt: str) -> None:
        msgs = self._selected_summaries()
        if not msgs:
            QMessageBox.information(self, "Export", "Select one or more messages first.")
            return
        self._export_messages(msgs, fmt)

    def _export_scope(self, fmt: str, item: QTreeWidgetItem | None) -> None:
        if item is not None:
            self.tree.setCurrentItem(item)
        mailbox, folder = self._scope()
        if mailbox is None:
            return
        if item is not None and item.data(0, FOLDER_ROLE) is None:
            folder = None
        msgs = self.store.messages(mailbox, folder, include_hidden=self.show_hidden.isChecked())
        self._export_messages(msgs, fmt)

    def _export_messages(self, msgs: list[MessageSummary], fmt: str) -> None:
        start = str(QSettings().value("export_dir", str(Path.home() / "edb-explorer-exports")))
        out = QFileDialog.getExistingDirectory(self, f"Export {len(msgs)} message(s) as {fmt.upper()} into…", start)
        if not out:
            return
        QSettings().setValue("export_dir", out)
        task = self.tasks.start(
            f"Exporting {len(msgs)} messages ({fmt})", cancel=lambda: self._worker and self._worker.cancel(), detail=out
        )

        def job(progress: Any, should_stop: Any) -> int:
            paths = export_messages(
                self.store,
                msgs,
                out,
                fmt,
                attachments=True,
                by_folder=True,
                progress=lambda i, p: (progress(f"{i}/{len(msgs)}"), not should_stop())[1],
            )
            return len(paths)

        self._worker = FunctionWorker(job, parent=self)
        self._worker.progress.connect(lambda m: task.progress(detail=str(m)))
        self._worker.result.connect(
            lambda n: (
                task.finish(f"{n} files written to {out}"),
                self.status.emit(f"Exported {n} message(s) to {out}"),
            )
        )
        self._worker.failed.connect(lambda m: task.finish(m, failed=True))
        self._worker.start()

    def _export_list(self, fmt: str) -> None:
        rows = summaries_to_rows(self._current)
        if not rows:
            return
        start = str(QSettings().value("export_dir", str(Path.home() / "edb-explorer-exports")))
        path, _ = QFileDialog.getSaveFileName(
            self, "Save message list", f"{start}/messages.{fmt}", f"{fmt.upper()} (*.{fmt})"
        )
        if not path:
            return
        n = export_rows(rows, list(rows[0].keys()), path, fmt, title=f"{self.db.path.name} - messages")
        self.status.emit(f"Wrote {n} rows to {path}")

    def _copy_eml(self) -> None:
        if self._detail:
            QApplication.clipboard().setText(message_to_eml(self.store, self._detail).decode("utf-8", "replace"))
            self.status.emit("EML copied to clipboard")

    def _save_attachment(self) -> None:
        if not self._detail or not self._detail.attachments:
            return
        rows = {i.row() for i in self.attachments.selectedIndexes()} or {0}
        for r in sorted(rows):
            att = self._detail.attachments[r]
            try:
                name, data = self.store.attachment_content(self._detail.summary.mailbox, att["inid"])
            except Exception as exc:
                QMessageBox.warning(self, "Attachment", str(exc))
                return
            path, _ = QFileDialog.getSaveFileName(self, "Save attachment", name)
            if path:
                Path(path).write_bytes(data)
                self.status.emit(f"Saved {path} ({len(data):,} bytes)")

    def _save_all_attachments(self) -> None:
        if not self._detail or not self._detail.attachments:
            return
        out = QFileDialog.getExistingDirectory(self, "Save attachments into…")
        if not out:
            return
        n = 0
        for att in self._detail.attachments:
            try:
                name, data = self.store.attachment_content(self._detail.summary.mailbox, att["inid"])
                (Path(out) / name).write_bytes(data)
                n += 1
            except Exception:
                continue
        self.status.emit(f"Saved {n} attachment(s) to {out}")

    def shutdown(self) -> None:
        for w in (self._worker, self._detail_worker):
            if w and w.isRunning():
                w.cancel()
                w.wait(3000)


__all__ = ["MailboxTab", "message_to_html", "message_to_text"]
