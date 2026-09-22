"""Keyboard shortcut registry: every menu action has a stable id, a default key and a user override.

Overrides live in ``QSettings`` under ``shortcuts/<id>`` (portable key text; an empty string means "no
shortcut").  The registry applies them to the actions when they are registered and keeps them applied when
the user rebinds through the *Keyboard shortcuts* dialog, so menus, tooltips and the actions themselves
always agree.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from PySide6.QtCore import QObject, QSettings, Signal
from PySide6.QtGui import QAction, QKeySequence

_PLAIN = re.compile(r"&(?=.)")
_SLUG = re.compile(r"[^a-z0-9]+")


def plain_text(text: str) -> str:
    """Menu text without the accelerator ampersand and trailing ellipsis / annotations."""
    text = _PLAIN.sub("", text)
    text = text.split("  (")[0]
    return text.rstrip("…. ").strip()


def slug(text: str) -> str:
    return _SLUG.sub("_", plain_text(text).lower()).strip("_")


def key_text(seq: QKeySequence | None) -> str:
    return "" if seq is None or seq.isEmpty() else seq.toString(QKeySequence.SequenceFormat.NativeText)


@dataclass(slots=True)
class ShortcutEntry:
    id: str
    category: str
    label: str
    action: QAction
    default: QKeySequence
    description: str = ""

    @property
    def current(self) -> QKeySequence:
        return self.action.shortcut()

    @property
    def is_default(self) -> bool:
        return self.current.toString() == self.default.toString()


class ShortcutRegistry(QObject):
    """All rebindable actions of the main window."""

    changed = Signal()  # emitted after any rebinding (menus/tooltips refresh)

    def __init__(self, settings: QSettings, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.settings = settings
        self._entries: dict[str, ShortcutEntry] = {}
        self._order: list[str] = []

    # ------------------------------------------------------------------ #
    def register(
        self,
        action: QAction,
        category: str,
        entry_id: str | None = None,
        default: QKeySequence | str | int | None = None,
        description: str = "",
    ) -> ShortcutEntry:
        """Register ``action`` (its current shortcut becomes the default unless ``default`` is given) and apply
        the stored override, if any."""
        if default is None:
            default_seq = action.shortcut()
        elif isinstance(default, QKeySequence):
            default_seq = default
        elif isinstance(default, str):
            default_seq = QKeySequence(default)
        else:
            default_seq = QKeySequence(default)  # QKeySequence.StandardKey
            action.setShortcut(default_seq)
        cat = plain_text(category)
        eid = entry_id or f"{slug(cat)}.{slug(action.text())}"
        base, n = eid, 2
        while eid in self._entries:
            eid = f"{base}_{n}"
            n += 1
        entry = ShortcutEntry(eid, cat, plain_text(action.text()), action, QKeySequence(default_seq), description)
        self._entries[eid] = entry
        self._order.append(eid)
        stored = self.settings.value(f"shortcuts/{eid}")
        if stored is not None:
            action.setShortcut(QKeySequence(str(stored), QKeySequence.SequenceFormat.PortableText))
        elif not default_seq.isEmpty():
            action.setShortcut(default_seq)
        return entry

    def entries(self) -> list[ShortcutEntry]:
        return [self._entries[i] for i in self._order]

    def get(self, entry_id: str) -> ShortcutEntry | None:
        return self._entries.get(entry_id)

    def categories(self) -> list[str]:
        seen: list[str] = []
        for e in self.entries():
            if e.category not in seen:
                seen.append(e.category)
        return seen

    def key_for(self, entry_id: str) -> str:
        e = self._entries.get(entry_id)
        return key_text(e.current) if e else ""

    # ------------------------------------------------------------------ #
    def conflicts(self, seq: QKeySequence, exclude: str | None = None) -> list[ShortcutEntry]:
        if seq.isEmpty():
            return []
        wanted = seq.toString()
        return [e for e in self.entries() if e.id != exclude and e.current.toString() == wanted]

    def assign(self, entry_id: str, seq: QKeySequence | None, steal: bool = False) -> list[ShortcutEntry]:
        """Bind ``seq`` (``None``/empty clears) to ``entry_id``; returns the entries it was taken from.

        Without ``steal`` a conflicting binding raises :class:`ValueError` so the caller can ask the user.
        """
        entry = self._entries[entry_id]
        seq = QKeySequence() if seq is None else QKeySequence(seq)
        taken = self.conflicts(seq, exclude=entry_id)
        if taken and not steal:
            raise ValueError(", ".join(f"{e.label} ({e.category})" for e in taken))
        for other in taken:
            other.action.setShortcut(QKeySequence())
            self._persist(other)
        entry.action.setShortcut(seq)
        self._persist(entry)
        self.changed.emit()
        return taken

    def reset(self, entry_id: str, steal: bool = False) -> list[ShortcutEntry]:
        entry = self._entries[entry_id]
        return self.assign(entry_id, entry.default, steal=steal)

    def reset_all(self) -> None:
        for e in self.entries():
            e.action.setShortcut(e.default)
            self.settings.remove(f"shortcuts/{e.id}")
        self.changed.emit()

    def _persist(self, entry: ShortcutEntry) -> None:
        key = f"shortcuts/{entry.id}"
        if entry.is_default:
            self.settings.remove(key)
        else:
            self.settings.setValue(key, entry.current.toString(QKeySequence.SequenceFormat.PortableText))

    # ------------------------------------------------------------------ #
    def as_text(self) -> str:
        """Cheat-sheet: one line per action, grouped by menu."""
        lines: list[str] = []
        for cat in self.categories():
            lines.append(cat)
            for e in self.entries():
                if e.category == cat:
                    lines.append(f"  {e.label:<44} {key_text(e.current) or '-'}")
            lines.append("")
        return "\n".join(lines).rstrip() + "\n"

    def overrides(self) -> dict[str, Any]:
        return {e.id: key_text(e.current) for e in self.entries() if not e.is_default}
