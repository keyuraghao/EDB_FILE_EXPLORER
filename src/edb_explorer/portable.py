"""Portable mode: keep every bit of user state next to the executable.

A file named ``portable.txt`` beside the frozen executable (or beside ``EDB Explorer.app`` on macOS) switches
the application to portable mode: settings, the signing key / trust store and the disk cache all live under
``<that folder>/data/``, nothing is written to the registry, ``%APPDATA%``, ``~/.config`` or the system temp
directory, and deleting the folder removes every trace.  The installed variants never see the marker.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

MARKER = "portable.txt"

__all__ = ["MARKER", "activate", "data_dir", "is_frozen", "portable_root"]


def is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def portable_root() -> Path | None:
    """The folder holding ``portable.txt`` when running a portable build, else ``None``."""
    override = os.environ.get("EDB_EXPLORER_PORTABLE_ROOT")
    if override:
        return Path(override)
    if not is_frozen():
        return None
    exe_dir = Path(sys.executable).resolve().parent
    candidates = [exe_dir]
    if sys.platform == "darwin" and exe_dir.name == "MacOS" and exe_dir.parent.name == "Contents":
        candidates.append(exe_dir.parent.parent.parent)  # beside "EDB Explorer.app"
    for c in candidates:
        if (c / MARKER).is_file():
            return c
    return None


def data_dir() -> Path | None:
    root = portable_root()
    return None if root is None else root / "data"


def activate() -> Path | None:
    """Point settings, config and cache at ``<root>/data`` when portable; returns the data directory."""
    data = data_dir()
    if data is None:
        return None
    for sub in ("settings", "config", "cache"):
        (data / sub).mkdir(parents=True, exist_ok=True)
    os.environ.setdefault("EDB_EXPLORER_CONFIG_DIR", str(data / "config"))
    os.environ.setdefault("EDB_EXPLORER_CACHE_DIR", str(data / "cache"))
    try:
        from PySide6.QtCore import QSettings

        # must run before the first QSettings object exists; the app only uses the no-argument constructor,
        # which follows the default format - the native path is redirected too for good measure
        QSettings.setDefaultFormat(QSettings.Format.IniFormat)
        for fmt in (QSettings.Format.IniFormat, QSettings.Format.NativeFormat):
            QSettings.setPath(fmt, QSettings.Scope.UserScope, str(data / "settings"))
    except ImportError:  # CLI-only use
        pass
    return data
