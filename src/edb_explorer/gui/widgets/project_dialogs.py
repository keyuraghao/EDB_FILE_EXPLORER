"""File ▸ Export / Import project: share the open databases + workspace as a verified ``.edbproj`` file."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import Any

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QSizePolicy,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from edb_explorer.core import EdbDatabase
from edb_explorer.core.project import (
    EXTENSION,
    ImportResult,
    ProjectHeader,
    SigningIdentity,
    TrustStore,
    export_project,
    import_project,
    inspect_project,
)
from edb_explorer.gui.tasks import TaskManager
from edb_explorer.gui.workers import FunctionWorker

PROJECT_FILTER = f"EDB Explorer project (*{EXTENSION});;All files (*)"


def _plain_error(message: str) -> str:
    """Drop the ``ExceptionClass: `` prefix the worker adds; users only need the sentence."""
    head, sep, rest = message.partition(": ")
    return rest if sep and head.isidentifier() else message


def _fmt_size(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:,.0f} {unit}" if unit == "B" else f"{n:,.1f} {unit}"
        n /= 1024
    return f"{n:,.1f} TB"


# --------------------------------------------------------------------------- #
class ExportProjectDialog(QDialog):
    """Choose what goes into the project file and how it is protected; the export runs as a task."""

    def __init__(
        self, databases: list[EdbDatabase], workspace: dict[str, Any], tasks: TaskManager, parent: QWidget | None = None
    ) -> None:
        super().__init__(parent)
        self.databases = databases
        self.workspace = workspace
        self.tasks = tasks
        self._worker: FunctionWorker | None = None
        self.result_path: Path | None = None
        self.setWindowTitle("Export project")
        self.resize(720, 640)
        layout = QVBoxLayout(self)
        layout.setSpacing(10)

        intro = QLabel(
            "A project file carries the list of open databases with the <b>SHA-256 of every file</b>, the open "
            "tabs and layout, and your notes, so a colleague can open the very same workspace on another "
            "computer. Every part of the file is covered by a <b>hash map</b> that is verified on import and "
            "<b>signed with your Ed25519 key</b>; a password additionally <b>encrypts</b> the contents."
        )
        intro.setWordWrap(True)
        intro.setTextFormat(Qt.TextFormat.RichText)
        layout.addWidget(intro)

        form = QFormLayout()
        self.name_edit = QLineEdit(databases[0].path.parent.name if databases else "project")
        form.addRow("Project name", self.name_edit)
        self.notes_edit = QPlainTextEdit()
        self.notes_edit.setPlaceholderText("Notes for the recipient (case number, what to look at, …)")
        self.notes_edit.setMaximumHeight(70)
        form.addRow("Notes", self.notes_edit)
        dest_row = QHBoxLayout()
        self.dest_edit = QLineEdit(str(Path.home() / (self.name_edit.text() + EXTENSION)))
        dest_row.addWidget(self.dest_edit, 1)
        browse = QPushButton("Browse…")
        browse.clicked.connect(self._browse)
        dest_row.addWidget(browse)
        form.addRow("Save as", dest_row)
        layout.addLayout(form)

        total = sum(db.path.stat().st_size for db in databases if db.path.is_file())
        self.table = QTableWidget(len(databases), 3)
        self.table.setHorizontalHeaderLabels(["Database", "Format", "Size"])
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.verticalHeader().setVisible(False)
        for r, db in enumerate(databases):
            self.table.setItem(r, 0, QTableWidgetItem(str(db.path)))
            self.table.setItem(r, 1, QTableWidgetItem(db.info.kind_name))
            self.table.setItem(r, 2, QTableWidgetItem(_fmt_size(db.path.stat().st_size if db.path.is_file() else 0)))
        self.table.setMaximumHeight(160)
        layout.addWidget(self.table)

        protect = QGroupBox("Protection")
        pl = QFormLayout(protect)
        self.embed = QCheckBox(f"Embed the evidence files - self-contained project ({_fmt_size(total)} more)")
        self.embed.setToolTip(
            "Copy the database files into the project so the recipient does not need their own copy. "
            "Without this, the project only records their names, paths and SHA-256 and the recipient "
            "points the import at the folder holding the files."
        )
        pl.addRow(self.embed)
        self.encrypt = QCheckBox("Encrypt with a password (AES-256-GCM, key derived with scrypt)")
        self.encrypt.toggled.connect(self._toggle_password)
        pl.addRow(self.encrypt)
        self.password = QLineEdit()
        self.password.setEchoMode(QLineEdit.EchoMode.Password)
        self.password.setEnabled(False)
        pl.addRow("Password", self.password)
        self.password2 = QLineEdit()
        self.password2.setEchoMode(QLineEdit.EchoMode.Password)
        self.password2.setEnabled(False)
        pl.addRow("Confirm", self.password2)
        self.sign = QCheckBox("Sign with my key (Ed25519)")
        self.sign.setChecked(True)
        pl.addRow(self.sign)
        self.signer_label = QLabel()
        self.signer_label.setObjectName("dim")
        self.signer_label.setWordWrap(True)
        try:
            ident = SigningIdentity.load_or_create()
            self.signer_label.setText(
                f"Signer: {ident.name} on {ident.host} - fingerprint <b>{ident.fingerprint}</b><br>"
                "Give the recipient this fingerprint through another channel so they can trust your signature."
            )
        except Exception as exc:  # no writable config dir, missing backend ...
            self.sign.setChecked(False)
            self.sign.setEnabled(False)
            self.signer_label.setText(f"Signing unavailable: {exc}")
        pl.addRow(self.signer_label)
        layout.addWidget(protect)

        self.status = QLabel()
        self.status.setObjectName("dim")
        layout.addWidget(self.status)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Cancel)
        self.export_btn = buttons.addButton("Export", QDialogButtonBox.ButtonRole.AcceptRole)
        buttons.accepted.connect(self._export)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self.name_edit.textChanged.connect(self._sync_dest)

    def _sync_dest(self, name: str) -> None:
        current = Path(self.dest_edit.text())
        self.dest_edit.setText(str(current.parent / ((name.strip() or "project") + EXTENSION)))

    def _toggle_password(self, on: bool) -> None:
        self.password.setEnabled(on)
        self.password2.setEnabled(on)
        if on:
            self.password.setFocus()

    def _browse(self) -> None:
        path, _ = QFileDialog.getSaveFileName(self, "Save project as", self.dest_edit.text(), PROJECT_FILTER)
        if path:
            self.dest_edit.setText(path)

    def _export(self) -> None:
        dest = self.dest_edit.text().strip()
        if not dest:
            self.status.setText("Choose where to save the project.")
            return
        password = None
        if self.encrypt.isChecked():
            if not self.password.text():
                self.status.setText("Enter a password, or untick encryption.")
                return
            if self.password.text() != self.password2.text():
                self.status.setText("The two passwords differ.")
                return
            password = self.password.text()
        identity = SigningIdentity.load_or_create() if self.sign.isChecked() else None
        name, notes = self.name_edit.text().strip(), self.notes_edit.toPlainText().strip()
        embed = self.embed.isChecked()
        self.export_btn.setEnabled(False)

        def job(progress: Any, should_stop: Any) -> Path:
            def cb(msg: str, done: int, total: int) -> bool:
                progress((msg, done, total))
                return not should_stop()

            export_project(
                self.databases,
                dest,
                name=name,
                notes=notes,
                workspace=self.workspace,
                password=password,
                embed=embed,
                identity=identity,
                progress=cb,
            )
            out = Path(dest)
            return out if out.suffix.lower() == EXTENSION else out.with_suffix(out.suffix + EXTENSION)

        self._worker = FunctionWorker(job, parent=self)
        task = self.tasks.start(f"Exporting project {name}", cancel=self._worker.cancel, detail=dest)
        self._worker.progress.connect(lambda p: (task.progress(p[1], p[2], p[0]), self.status.setText(p[0])))
        self._worker.result.connect(lambda out: (task.finish(str(out)), self._done(out)))
        self._worker.failed.connect(lambda m: (task.finish(m, failed=True), self._failed(m)))
        self._worker.start()

    def _done(self, out: Path) -> None:
        self.result_path = out
        self.accept()

    def _failed(self, message: str) -> None:
        self.export_btn.setEnabled(True)
        self.status.setText(f"Export failed: {_plain_error(message)}")


# --------------------------------------------------------------------------- #
class ImportProjectDialog(QDialog):
    """Verify a project (hash map + signature), unlock it, locate or extract its evidence, then open it."""

    def __init__(self, path: str, tasks: TaskManager, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.path = path
        self.tasks = tasks
        self.trust = TrustStore()
        self.header: ProjectHeader | None = None
        self.result: ImportResult | None = None
        self._worker: FunctionWorker | None = None
        self.setWindowTitle("Import project")
        self.resize(820, 680)
        layout = QVBoxLayout(self)
        layout.setSpacing(10)

        self.file_label = QLabel(f"<b>{path}</b>")
        self.file_label.setWordWrap(True)
        layout.addWidget(self.file_label)

        checks = QGroupBox("Verification")
        cl = QFormLayout(checks)
        self.integrity_label = QLabel("…")
        self.integrity_label.setWordWrap(True)
        cl.addRow("Integrity", self.integrity_label)
        sig_row = QHBoxLayout()
        self.signature_label = QLabel("…")
        self.signature_label.setWordWrap(True)
        sig_row.addWidget(self.signature_label, 1)
        self.trust_btn = QPushButton("Trust this signer")
        self.trust_btn.setToolTip("Remember this public-key fingerprint as a known colleague")
        self.trust_btn.clicked.connect(self._trust)
        self.trust_btn.hide()
        sig_row.addWidget(self.trust_btn)
        cl.addRow("Signature", sig_row)
        self.confidentiality_label = QLabel("…")
        cl.addRow("Confidentiality", self.confidentiality_label)
        pw_row = QHBoxLayout()
        self.password = QLineEdit()
        self.password.setEchoMode(QLineEdit.EchoMode.Password)
        self.password.setPlaceholderText("Password to decrypt the project")
        self.password.returnPressed.connect(self._unlock)
        pw_row.addWidget(self.password, 1)
        self.unlock_btn = QPushButton("Unlock")
        self.unlock_btn.clicked.connect(self._unlock)
        pw_row.addWidget(self.unlock_btn)
        self.pw_widget = QWidget()
        self.pw_widget.setLayout(pw_row)
        cl.addRow("", self.pw_widget)
        layout.addWidget(checks)

        self.project_label = QLabel()
        self.project_label.setWordWrap(True)
        self.project_label.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Minimum)
        layout.addWidget(self.project_label)

        self.table = QTableWidget(0, 5)
        self.table.setHorizontalHeaderLabels(["Database", "Format", "Size", "Local file", "Verified"])
        hh = self.table.horizontalHeader()
        hh.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        hh.setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        hh.setSectionResizeMode(4, QHeaderView.ResizeMode.ResizeToContents)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.verticalHeader().setVisible(False)
        layout.addWidget(self.table, 1)

        locate = QGroupBox("Evidence files")
        ll = QFormLayout(locate)
        search_row = QHBoxLayout()
        self.search_edit = QLineEdit()
        self.search_edit.setPlaceholderText("Folder on this computer holding the evidence (searched recursively)")
        search_row.addWidget(self.search_edit, 1)
        pick = QPushButton("Locate in folder…")
        pick.clicked.connect(self._pick_search)
        search_row.addWidget(pick)
        ll.addRow("Search in", search_row)
        extract_row = QHBoxLayout()
        self.extract_edit = QLineEdit(default_extract_dir())
        extract_row.addWidget(self.extract_edit, 1)
        pick2 = QPushButton("Browse…")
        pick2.clicked.connect(self._pick_extract)
        extract_row.addWidget(pick2)
        ll.addRow("Extract embedded files to", extract_row)
        self.embedded_note = QLabel()
        self.embedded_note.setObjectName("dim")
        ll.addRow(self.embedded_note)
        layout.addWidget(locate)

        self.status = QLabel()
        self.status.setObjectName("dim")
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Cancel)
        self.open_btn = buttons.addButton("Open project", QDialogButtonBox.ButtonRole.AcceptRole)
        self.open_btn.setEnabled(False)
        buttons.accepted.connect(self._open)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        self._inspect()

    # ------------------------------------------------------------------ #
    def _inspect(self) -> None:
        try:
            self.header = inspect_project(self.path, self.trust)
        except Exception as exc:
            self.integrity_label.setText(f"<span style='color:#c0392b'>Cannot read project: {exc}</span>")
            self.signature_label.setText("-")
            self.confidentiality_label.setText("-")
            self.pw_widget.hide()
            return
        h = self.header
        if h.integrity_ok:
            self.integrity_label.setText(
                "<span style='color:#2f855a'>OK - every member matches the signed hash map</span>"
            )
        else:
            self.integrity_label.setText(
                "<span style='color:#c0392b'>FAILED - the file was altered:</span><br>"
                + "<br>".join(h.integrity_errors)
            )
        self._show_signature()
        self.confidentiality_label.setText(
            "Encrypted (AES-256-GCM) - enter the password to unlock" if h.encrypted else "Not encrypted"
        )
        self.pw_widget.setVisible(h.encrypted)
        if not h.encrypted:
            self._resolve()
        else:
            self.password.setFocus()

    def _show_signature(self) -> None:
        assert self.header is not None
        sig = self.header.signature
        colour = (
            "#2f855a"
            if sig.valid and sig.trusted
            else "#b7791f"
            if sig.valid
            else "#c0392b"
            if sig.present
            else "#b7791f"
        )
        text = f"<span style='color:{colour}'>{sig.summary}</span>"
        if sig.present and sig.valid:
            text += f"<br>Fingerprint <b>{sig.fingerprint}</b> - compare it with the sender before trusting."
        self.signature_label.setText(text)
        self.trust_btn.setVisible(bool(sig.present and sig.valid and not sig.trusted))

    def _trust(self) -> None:
        assert self.header is not None
        sig = self.header.signature
        name = sig.signer.get("name") or sig.fingerprint
        self.trust.trust(sig.fingerprint, name, sig.public_key)
        sig.trusted, sig.trusted_as = True, name
        self._show_signature()

    def _unlock(self) -> None:
        if not self.password.text():
            self.status.setText("Enter the password first.")
            return
        self._resolve()

    def _pick_search(self) -> None:
        folder = QFileDialog.getExistingDirectory(self, "Folder holding the evidence files", self.search_edit.text())
        if folder:
            self.search_edit.setText(folder)
            self._resolve()

    def _pick_extract(self) -> None:
        folder = QFileDialog.getExistingDirectory(self, "Extract embedded files to", self.extract_edit.text())
        if folder:
            self.extract_edit.setText(folder)

    def _resolve(self) -> None:
        """Run import_project in the background: decrypt, extract embedded files, locate the rest."""
        if self.header is None:
            return
        if not self.header.integrity_ok:
            answer = QMessageBox.warning(
                self,
                "Integrity check failed",
                "This project does not match its hash map - its contents were changed after it was made.\n\n"
                "Continue anyway? The databases it points to will still be verified by SHA-256.",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            )
            if answer != QMessageBox.StandardButton.Yes:
                return
        password = self.password.text() or None
        search = [self.search_edit.text()] if self.search_edit.text().strip() else []
        extract_to = self.extract_edit.text().strip() or tempfile.gettempdir()
        self.open_btn.setEnabled(False)
        self.status.setText("Verifying and locating evidence…")

        def job(progress: Any, should_stop: Any) -> ImportResult:
            def cb(msg: str, done: int, total: int) -> bool:
                progress((msg, done, total))
                return not should_stop()

            return import_project(
                self.path,
                password=password,
                search_roots=search,
                extract_to=extract_to,
                trust=self.trust,
                require_integrity=False,
                progress=cb,
            )

        self._worker = FunctionWorker(job, parent=self)
        task = self.tasks.start(
            f"Importing project {Path(self.path).name}", cancel=self._worker.cancel, detail=self.path
        )
        self._worker.progress.connect(lambda p: (task.progress(p[1], p[2], p[0]), self.status.setText(p[0])))
        self._worker.result.connect(lambda r: (task.finish("verified"), self._resolved(r)))
        self._worker.failed.connect(lambda m: (task.finish(m, failed=True), self.status.setText(_plain_error(m))))
        self._worker.start()

    def _resolved(self, res: ImportResult) -> None:
        self.result = res
        p = res.project
        self.project_label.setText(
            f"<b>{p.name}</b> - created {p.created} by {p.author.get('user', '?')}@{p.author.get('host', '?')} "
            f"with EDB Explorer {p.app_version}" + (f"<br><i>{p.notes}</i>" if p.notes else "")
        )
        self.table.setRowCount(len(p.databases))
        missing = 0
        for r, db in enumerate(p.databases):
            local = res.evidence.get(db.id)
            ok = res.verified.get(db.id)
            self.table.setItem(r, 0, QTableWidgetItem(db.relative or db.name))
            self.table.setItem(r, 1, QTableWidgetItem(db.kind_name or db.kind))
            self.table.setItem(r, 2, QTableWidgetItem(_fmt_size(db.size)))
            self.table.setItem(r, 3, QTableWidgetItem(local or "not found - use “Locate in folder…”"))
            self.table.setItem(r, 4, QTableWidgetItem("SHA-256 matches" if ok else "no" if local else ""))
            if not local:
                missing += 1
                for c in range(5):
                    item = self.table.item(r, c)
                    if item:
                        item.setForeground(Qt.GlobalColor.red)
        embedded = [db for db in p.databases if db.embedded]
        self.embedded_note.setText(
            f"{len(embedded)} embedded file(s) extracted to {Path(res.extracted[0]).parent if res.extracted else self.extract_edit.text()}"
            if embedded
            else "This project does not embed the evidence files; they are looked up by path, name and SHA-256."
        )
        found = len(p.databases) - missing
        self.status.setText(
            f"{found}/{len(p.databases)} database(s) located and verified."
            + (" Point “Search in” at the folder holding the missing files." if missing else "")
        )
        self.open_btn.setEnabled(found > 0)

    def _open(self) -> None:
        if self.result is None:
            return
        self.accept()

    def located(self) -> list[tuple[str, str]]:
        """``(db id, local path)`` for every database that was found."""
        if self.result is None:
            return []
        return [(db_id, path) for db_id, path in self.result.evidence.items() if path]


def default_extract_dir() -> str:
    return str(Path.home() / "EDB Explorer projects") if os.path.isdir(Path.home()) else tempfile.gettempdir()
