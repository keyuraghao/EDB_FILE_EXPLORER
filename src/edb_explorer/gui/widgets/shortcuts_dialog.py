"""Settings ▸ Keyboard shortcuts: every action grouped by menu, rebindable, with conflict handling."""

from __future__ import annotations

from typing import Any

from PySide6.QtCore import QEvent, Qt
from PySide6.QtGui import QBrush, QColor, QFont, QKeySequence
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QDialog,
    QDialogButtonBox,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QKeySequenceEdit,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from edb_explorer.gui.shortcuts import ShortcutEntry, ShortcutRegistry, key_text

ID_ROLE = Qt.ItemDataRole.UserRole + 1

#: Shortcuts that belong to a widget rather than a menu action; shown for completeness, not editable.
BUILT_IN: tuple[tuple[str, str, str], ...] = (
    ("Table / results grid", "Copy selected rows as TSV", "Ctrl+C"),
    ("Table / results grid", "Filter rows (focus the filter box)", "Ctrl+F"),
    ("SQL console", "Run query", "Ctrl+Return"),
)


class ShortcutsDialog(QDialog):
    """Browse and rebind the application's keyboard shortcuts (changes apply immediately and persist)."""

    def __init__(self, registry: ShortcutRegistry, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.registry = registry
        self._current: str | None = None
        self.setWindowTitle("Keyboard shortcuts")
        self.resize(820, 640)
        layout = QVBoxLayout(self)
        layout.setSpacing(10)

        intro = QLabel(
            "Every menu action is listed with its shortcut. Select one, press the new key combination in the "
            "box below and click <b>Assign</b> (or press Enter). A key already used elsewhere is flagged - "
            "assigning it anyway moves it. Changes take effect immediately and are remembered."
        )
        intro.setWordWrap(True)
        intro.setTextFormat(Qt.TextFormat.RichText)
        layout.addWidget(intro)

        self.filter_edit = QLineEdit()
        self.filter_edit.setPlaceholderText("Filter actions or keys…")
        self.filter_edit.setClearButtonEnabled(True)
        self.filter_edit.textChanged.connect(self._apply_filter)
        layout.addWidget(self.filter_edit)

        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(["Action", "Shortcut", "Default"])
        self.tree.setRootIsDecorated(True)
        self.tree.setAlternatingRowColors(True)
        self.tree.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.tree.setUniformRowHeights(True)
        hh = self.tree.header()
        hh.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        hh.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        hh.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        self.tree.currentItemChanged.connect(lambda cur, _prev: self._select(cur))
        self.tree.itemDoubleClicked.connect(lambda _i, _c: self.key_edit.setFocus())
        layout.addWidget(self.tree, 1)

        editor = QGroupBox("Shortcut for the selected action")
        el = QVBoxLayout(editor)
        self.selected_label = QLabel("Select an action above.")
        self.selected_label.setObjectName("dim")
        el.addWidget(self.selected_label)
        row = QHBoxLayout()
        self.key_edit = QKeySequenceEdit()
        self.key_edit.setMaximumSequenceLength(1)
        self.key_edit.setClearButtonEnabled(True)
        self.key_edit.setToolTip("Click here and press the keys you want, e.g. Ctrl+Shift+P")
        self.key_edit.editingFinished.connect(self._preview_conflict)
        self.key_edit.keySequenceChanged.connect(lambda _s: self._preview_conflict())
        self.key_edit.installEventFilter(self)
        row.addWidget(self.key_edit, 1)
        self.assign_btn = QPushButton("Assign")
        self.assign_btn.setDefault(True)
        self.assign_btn.clicked.connect(self._assign)
        row.addWidget(self.assign_btn)
        self.clear_btn = QPushButton("Clear")
        self.clear_btn.setToolTip("Remove the shortcut from this action")
        self.clear_btn.clicked.connect(lambda: self._assign(QKeySequence()))
        row.addWidget(self.clear_btn)
        self.reset_btn = QPushButton("Reset to default")
        self.reset_btn.clicked.connect(self._reset_one)
        row.addWidget(self.reset_btn)
        el.addLayout(row)
        self.conflict_label = QLabel()
        self.conflict_label.setWordWrap(True)
        self.conflict_label.setObjectName("dim")
        el.addWidget(self.conflict_label)
        layout.addWidget(editor)

        buttons = QDialogButtonBox()
        self.reset_all_btn = buttons.addButton("Reset all to defaults", QDialogButtonBox.ButtonRole.ResetRole)
        self.reset_all_btn.clicked.connect(self._reset_all)
        copy_btn = buttons.addButton("Copy as text", QDialogButtonBox.ButtonRole.ActionRole)
        copy_btn.setToolTip("Copy a cheat-sheet of every action and its shortcut to the clipboard")
        copy_btn.clicked.connect(self._copy)
        close_btn = buttons.addButton(QDialogButtonBox.StandardButton.Close)
        close_btn.clicked.connect(self.accept)
        layout.addWidget(buttons)

        self._populate()
        self.registry.changed.connect(self._refresh)
        self._select(None)

    # ------------------------------------------------------------------ #
    def _populate(self) -> None:
        self.tree.clear()
        bold = QFont()
        bold.setBold(True)
        for cat in self.registry.categories():
            top = QTreeWidgetItem([cat, "", ""])
            top.setFont(0, bold)
            top.setFlags(top.flags() & ~Qt.ItemFlag.ItemIsSelectable)
            self.tree.addTopLevelItem(top)
            for e in self.registry.entries():
                if e.category != cat:
                    continue
                item = QTreeWidgetItem([e.label, key_text(e.current), key_text(e.default)])
                item.setData(0, ID_ROLE, e.id)
                if e.description:
                    item.setToolTip(0, e.description)
                top.addChild(item)
        fixed = QTreeWidgetItem(["Built-in (not rebindable)", "", ""])
        fixed.setFont(0, bold)
        fixed.setFlags(fixed.flags() & ~Qt.ItemFlag.ItemIsSelectable)
        self.tree.addTopLevelItem(fixed)
        for where, label, key in BUILT_IN:
            item = QTreeWidgetItem([f"{label}  -  {where}", key, key])
            item.setForeground(0, QBrush(QColor("#8a9099")))
            item.setForeground(1, QBrush(QColor("#8a9099")))
            item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsSelectable)
            fixed.addChild(item)
        self.tree.expandAll()
        self._refresh()

    def _refresh(self) -> None:
        """Update the key columns in place (after any rebinding)."""
        changed = QColor("#e0a458")
        for t in range(self.tree.topLevelItemCount()):
            top = self.tree.topLevelItem(t)
            for c in range(top.childCount()):
                item = top.child(c)
                eid = item.data(0, ID_ROLE)
                e = self.registry.get(eid) if eid else None
                if e is None:
                    continue
                item.setText(1, key_text(e.current))
                item.setText(2, key_text(e.default))
                item.setForeground(1, QBrush(changed) if not e.is_default else QBrush())
                item.setToolTip(
                    1, "" if e.is_default else f"Changed from the default ({key_text(e.default) or 'none'})"
                )
        if self._current:
            self._show_entry(self.registry.get(self._current))

    def _apply_filter(self, text: str) -> None:
        needle = text.strip().lower()
        for t in range(self.tree.topLevelItemCount()):
            top = self.tree.topLevelItem(t)
            visible = 0
            for c in range(top.childCount()):
                item = top.child(c)
                hit = not needle or needle in item.text(0).lower() or needle in item.text(1).lower()
                item.setHidden(not hit)
                visible += hit
            top.setHidden(visible == 0)

    # ------------------------------------------------------------------ #
    def _select(self, item: QTreeWidgetItem | None) -> None:
        eid = item.data(0, ID_ROLE) if item is not None else None
        self._current = eid
        entry = self.registry.get(eid) if eid else None
        self._show_entry(entry)

    def _show_entry(self, entry: ShortcutEntry | None) -> None:
        enabled = entry is not None
        for w in (self.key_edit, self.assign_btn, self.clear_btn, self.reset_btn):
            w.setEnabled(enabled)
        if entry is None:
            self.selected_label.setText("Select an action above.")
            self.key_edit.clear()
            self.conflict_label.clear()
            return
        self.selected_label.setText(
            f"<b>{entry.label}</b>  ({entry.category})  -  now: <b>{key_text(entry.current) or 'none'}</b>, "
            f"default: {key_text(entry.default) or 'none'}"
        )
        self.key_edit.setKeySequence(entry.current)
        self.reset_btn.setEnabled(not entry.is_default)
        self.clear_btn.setEnabled(not entry.current.isEmpty())
        self._preview_conflict()

    def _preview_conflict(self) -> None:
        if not self._current:
            return
        seq = self.key_edit.keySequence()
        taken = self.registry.conflicts(seq, exclude=self._current)
        if taken:
            names = ", ".join(f"{e.label} ({e.category})" for e in taken)
            self.conflict_label.setText(f"⚠ {key_text(seq)} is already used by: {names}. Assigning will take it over.")
        elif seq.isEmpty():
            self.conflict_label.setText("No key - Assign clears the shortcut.")
        else:
            self.conflict_label.setText(f"{key_text(seq)} is free.")

    def _assign(self, seq: QKeySequence | None = None) -> None:
        if not self._current:
            return
        if not isinstance(seq, QKeySequence):
            seq = self.key_edit.keySequence()
        entry = self.registry.get(self._current)
        if entry is None:
            return
        try:
            self.registry.assign(self._current, seq)
        except ValueError as taken:
            answer = QMessageBox.question(
                self,
                "Shortcut already in use",
                f"{key_text(seq)} is currently bound to {taken}.\n\nAssign it to “{entry.label}” instead?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            )
            if answer != QMessageBox.StandardButton.Yes:
                return
            self.registry.assign(self._current, seq, steal=True)
        self._refresh()

    def _reset_one(self) -> None:
        if not self._current:
            return
        entry = self.registry.get(self._current)
        if entry is not None:
            self._assign(entry.default)

    def _reset_all(self) -> None:
        answer = QMessageBox.question(
            self,
            "Reset all shortcuts",
            "Restore the default shortcut of every action?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if answer == QMessageBox.StandardButton.Yes:
            self.registry.reset_all()
            self._refresh()

    def _copy(self) -> None:
        QApplication.clipboard().setText(self.registry.as_text())
        self.conflict_label.setText("Copied the shortcut list to the clipboard.")

    def eventFilter(self, obj: Any, event: Any) -> bool:
        # Enter in the key box assigns instead of being recorded as the shortcut.
        if (
            obj is self.key_edit
            and event.type() == QEvent.Type.KeyPress
            and event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter)
            and not (event.modifiers() & ~Qt.KeyboardModifier.KeypadModifier)
        ):
            self._assign()
            return True
        return super().eventFilter(obj, event)
