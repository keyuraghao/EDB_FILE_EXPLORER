"""A session holds several open databases and hands out stable identifiers."""

from __future__ import annotations

import logging
import os
import re
import threading
from collections.abc import Iterator, Sequence
from pathlib import Path

from edb_explorer.core.backends import detect_kind
from edb_explorer.core.database import EdbDatabase
from edb_explorer.core.exceptions import DatabaseNotFoundError, PathNotAllowedError
from edb_explorer.core.models import DatabaseInfo

log = logging.getLogger(__name__)

# Extensions commonly used by supported databases.  Directory scans check file
# signatures so unusual names are still found.
ESE_EXTENSIONS = frozenset({".edb", ".dit", ".dat", ".db", ".mdb", ".vol", ".jtx"})
ALL_EXTENSIONS = ESE_EXTENSIONS | frozenset(
    {
        ".sqlite",
        ".sqlite3",
        ".sqlitedb",
        ".storedata",
        ".db3",
        ".s3db",
        ".accdb",
        ".dbf",
        ".bdb",
        ".sql",
        ".bson",
        ".ldb",
    }
)
_SKIP_SUFFIXES = (".log", ".chk", ".jrs", "-wal", "-shm", "-journal", ".dbt", ".fpt")

_SLUG_RE = re.compile(r"[^a-z0-9]+")


def _slugify(text: str) -> str:
    return _SLUG_RE.sub("-", text.lower()).strip("-") or "db"


class Session:
    """Registry of open :class:`EdbDatabase` objects.

    ``allowed_roots`` optionally restricts which paths can be opened - the MCP
    server uses this so an agent cannot read arbitrary files on the host.
    """

    def __init__(self, allowed_roots: Sequence[str | os.PathLike[str]] | None = None) -> None:
        self._dbs: dict[str, EdbDatabase] = {}
        self._lock = threading.RLock()
        self.allowed_roots: list[Path] = [Path(r).expanduser().resolve() for r in (allowed_roots or [])]

    # ------------------------------------------------------------------ #
    def _check_allowed(self, path: Path) -> None:
        if not self.allowed_roots:
            return
        for root in self.allowed_roots:
            try:
                path.relative_to(root)
                return
            except ValueError:
                continue
        raise PathNotAllowedError(f"{path} is outside the allowed directories: {[str(r) for r in self.allowed_roots]}")

    def _unique_id(self, path: Path) -> str:
        base = _slugify(path.stem)
        candidate = base
        n = 2
        while candidate in self._dbs:
            candidate = f"{base}-{n}"
            n += 1
        return candidate

    # ------------------------------------------------------------------ #
    def open(self, path: str | os.PathLike[str], db_id: str | None = None, kind: str | None = None) -> EdbDatabase:
        """Open a database (any supported format), or return the existing handle if it is already open.

        A file inside a LevelDB directory (``CURRENT``, ``*.ldb``, ``*.log``) opens the directory.
        """
        resolved = Path(path).expanduser().resolve()
        if resolved.is_file() and (kind == "leveldb" or (kind is None and detect_kind(resolved) == "leveldb")):
            resolved = resolved.parent
            kind = "leveldb"
        self._check_allowed(resolved)
        with self._lock:
            for db in self._dbs.values():
                if db.path == resolved:
                    return db
            if db_id and db_id in self._dbs:
                raise DatabaseNotFoundError(f"Identifier {db_id!r} is already in use")
            db = EdbDatabase(resolved, db_id or self._unique_id(resolved), kind)
            self._dbs[db.id] = db
            log.info("Opened %s as %s (%s, %d tables)", resolved, db.id, db.profile.name, db.info.table_count)
            return db

    def open_many(self, paths: Sequence[str | os.PathLike[str]]) -> tuple[list[EdbDatabase], dict[str, str]]:
        """Open several files; returns (opened, {path: error})."""
        opened: list[EdbDatabase] = []
        errors: dict[str, str] = {}
        for p in paths:
            try:
                opened.append(self.open(p))
            except Exception as exc:
                errors[str(p)] = str(exc)
        return opened, errors

    def close(self, db_id: str) -> None:
        with self._lock:
            db = self._dbs.pop(self.resolve_id(db_id), None)
        if db:
            db.close()

    def close_all(self) -> None:
        with self._lock:
            dbs = list(self._dbs.values())
            self._dbs.clear()
        for db in dbs:
            db.close()

    # ------------------------------------------------------------------ #
    def resolve_id(self, ref: str) -> str:
        """Accept an id, a file name, or a full path."""
        with self._lock:
            if ref in self._dbs:
                return ref
            lowered = ref.lower()
            for db_id, db in self._dbs.items():
                if lowered in (db_id.lower(), db.path.name.lower(), str(db.path).lower()):
                    return db_id
            if len(self._dbs) == 1 and ref in ("", "default", "*"):
                return next(iter(self._dbs))
        raise DatabaseNotFoundError(f"No open database matches {ref!r}. Open ones: {', '.join(self._dbs) or '(none)'}")

    def get(self, ref: str) -> EdbDatabase:
        return self._dbs[self.resolve_id(ref)]

    def __contains__(self, ref: str) -> bool:
        try:
            self.resolve_id(ref)
        except DatabaseNotFoundError:
            return False
        return True

    def __len__(self) -> int:
        return len(self._dbs)

    def __iter__(self) -> Iterator[EdbDatabase]:
        return iter(list(self._dbs.values()))

    def databases(self) -> list[EdbDatabase]:
        return list(self._dbs.values())

    def infos(self) -> list[DatabaseInfo]:
        return [db.info for db in self._dbs.values()]

    # ------------------------------------------------------------------ #
    def scan(
        self,
        directory: str | os.PathLike[str],
        recursive: bool = True,
        check_magic: bool = True,
        max_files: int = 10_000,
        kinds: set[str] | None = None,
    ) -> list[Path]:
        """Find supported database files (and LevelDB directories) under a directory.

        Returns paths sorted by name; LevelDB stores are returned as their directory.
        """
        root = Path(directory).expanduser().resolve()
        self._check_allowed(root)
        if not root.is_dir():
            raise FileNotFoundError(f"Not a directory: {root}")
        found: list[Path] = []
        seen_dirs: set[Path] = set()
        walker = root.rglob("*") if recursive else root.glob("*")
        for entry in walker:
            if len(found) >= max_files:
                break
            try:
                if entry.is_dir():
                    continue
                if not entry.is_file():
                    continue
                name = entry.name.lower()
                if name.endswith(_SKIP_SUFFIXES) and not (name.endswith(".log") and entry.parent not in seen_dirs):
                    continue
                if entry.parent in seen_dirs:
                    continue
                if check_magic:
                    kind = detect_kind(entry)
                    if kind is None or (kinds and kind not in kinds):
                        continue
                    if kind == "leveldb":
                        seen_dirs.add(entry.parent)
                        found.append(entry.parent)
                        continue
                    found.append(entry)
                elif entry.suffix.lower() in ALL_EXTENSIONS:
                    found.append(entry)
            except OSError:
                continue
        return sorted(set(found))

    def detect(self, path: str | os.PathLike[str]) -> str | None:
        """Backend kind for ``path`` or None."""
        return detect_kind(path)
