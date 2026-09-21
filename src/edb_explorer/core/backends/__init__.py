"""Format detection and backend registry."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from edb_explorer.core.backends.base import Backend, BackendInfo, BackendTable
from edb_explorer.core.exceptions import InvalidDatabaseError


@dataclass(frozen=True, slots=True)
class KindInfo:
    id: str
    name: str
    description: str
    extensions: tuple[str, ...]


KINDS: tuple[KindInfo, ...] = (
    KindInfo(
        "ese",
        "Microsoft ESE / JET Blue",
        "ntds.dit, SRUDB.dat, Exchange .edb, WebCacheV01.dat, Windows.edb, UAL",
        (".edb", ".dit", ".dat", ".mdb", ".jtx", ".vol"),
    ),
    KindInfo(
        "sqlite",
        "SQLite 3",
        "iOS/Android app databases, Chrome/Firefox/Safari, macOS knowledgeC, Windows ActivitiesCache, Signal, WhatsApp",
        (".db", ".sqlite", ".sqlite3", ".sqlitedb", ".storedata", ".db3", ".s3db"),
    ),
    KindInfo(
        "leveldb",
        "LevelDB (Chromium / Electron)",
        "Chrome/Edge IndexedDB & Local/Session Storage, Discord, Teams, Slack, VS Code",
        (".ldb", ".log"),
    ),
    KindInfo("access", "Microsoft Access (Jet/ACE)", ".mdb / .accdb application databases", (".mdb", ".accdb")),
    KindInfo("dbf", "dBase / FoxPro (DBF)", "Legacy business applications, GIS shapefile attribute tables", (".dbf",)),
    KindInfo(
        "bsddb", "Berkeley DB", "RPM databases, sendmail/postfix maps, older Firefox cert stores", (".db", ".bdb")
    ),
    KindInfo(
        "sqldump",
        "SQL dump (MySQL / PostgreSQL / SQLite)",
        "mysqldump, pg_dump and sqlite .dump text exports",
        (".sql",),
    ),
    KindInfo("bson", "BSON dump (mongodump)", "MongoDB collection dumps", (".bson",)),
    KindInfo(
        "evtx",
        "Windows Event Log (EVTX)",
        "Security.evtx, System.evtx, Application.evtx, Sysmon and other Windows event logs",
        (".evtx",),
    ),
)
KIND_NAMES = {k.id: k.name for k in KINDS}

_HEAD = 4096


def detect_kind(path: str | os.PathLike[str]) -> str | None:
    """Sniff the format of a file (or LevelDB directory) from its signature; None if unsupported."""
    p = Path(path)
    if p.is_dir():
        from edb_explorer.core.backends.leveldb import is_leveldb_dir

        return "leveldb" if is_leveldb_dir(p) else None
    try:
        with open(p, "rb") as fh:
            head = fh.read(_HEAD)
    except OSError:
        return None
    if len(head) < 16:
        return None
    from edb_explorer.core.backends.ese import ESE_MAGIC

    if head[4:8] == ESE_MAGIC:
        return "ese"
    from edb_explorer.core.backends.sqlite import SQLITE_MAGIC

    if head.startswith(SQLITE_MAGIC):
        return "sqlite"
    from edb_explorer.core.backends.evtx import EVTX_MAGIC

    if head.startswith(EVTX_MAGIC):
        return "evtx"
    from edb_explorer.core.backends.access import ACCESS_MAGICS

    if head[4:19] in ACCESS_MAGICS:
        return "access"
    from edb_explorer.core.backends.bsddb import bsd_kind

    if bsd_kind(head):
        return "bsddb"
    if p.suffix.lower() in (".ldb", ".sst") or (p.name == "CURRENT" or p.name.startswith("MANIFEST-")):
        from edb_explorer.core.backends.leveldb import is_leveldb_dir

        if is_leveldb_dir(p.parent):
            return "leveldb"
    if p.suffix.lower() == ".log":
        from edb_explorer.core.backends.leveldb import is_leveldb_dir

        if is_leveldb_dir(p.parent):
            return "leveldb"
    from edb_explorer.core.backends.dbf import is_dbf_file

    if p.suffix.lower() == ".dbf" and is_dbf_file(p):
        return "dbf"
    from edb_explorer.core.backends.bsondump import looks_like_bson

    if looks_like_bson(head, p.name):
        return "bson"
    from edb_explorer.core.backends.sqldump import looks_like_sql_dump

    if looks_like_sql_dump(head, p.name):
        return "sqldump"
    if is_dbf_file(p):  # DBF has no magic; accept when the header is fully plausible
        return "dbf"
    return None


def open_backend(path: str | os.PathLike[str], kind: str | None = None) -> Backend:
    """Instantiate the right backend for ``path`` (auto-detected unless ``kind`` is given)."""
    kind = kind or detect_kind(path)
    p = Path(path)
    if kind is None:
        raise InvalidDatabaseError(f"{p.name}: unrecognised database format")
    if kind == "ese":
        from edb_explorer.core.backends.ese import EseBackend

        return EseBackend(p)
    if kind == "sqlite":
        from edb_explorer.core.backends.sqlite import SqliteBackend

        return SqliteBackend(p)
    if kind == "leveldb":
        from edb_explorer.core.backends.leveldb import LevelDbBackend

        return LevelDbBackend(p)
    if kind == "access":
        from edb_explorer.core.backends.access import AccessBackend

        return AccessBackend(p)
    if kind == "dbf":
        from edb_explorer.core.backends.dbf import DbfBackend

        return DbfBackend(p)
    if kind == "bsddb":
        from edb_explorer.core.backends.bsddb import BsdDbBackend

        return BsdDbBackend(p)
    if kind == "sqldump":
        from edb_explorer.core.backends.sqldump import SqlDumpBackend

        return SqlDumpBackend(p)
    if kind == "bson":
        from edb_explorer.core.backends.bsondump import BsonDumpBackend

        return BsonDumpBackend(p)
    if kind == "evtx":
        from edb_explorer.core.backends.evtx import EvtxBackend

        return EvtxBackend(p)
    raise InvalidDatabaseError(f"Unknown backend kind {kind!r}")


__all__ = ["KINDS", "KIND_NAMES", "Backend", "BackendInfo", "BackendTable", "KindInfo", "detect_kind", "open_backend"]
