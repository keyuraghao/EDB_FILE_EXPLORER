"""Berkeley DB backend (btree / hash / recno) via dissect.database.

Seen as RPM databases, sendmail/postfix maps and legacy Firefox cert8/key3 stores.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from typing import Any

from edb_explorer.core.backends.base import Backend, BackendInfo, BackendTable
from edb_explorer.core.exceptions import InvalidDatabaseError
from edb_explorer.core.models import ColumnInfo
from edb_explorer.core.values import decode_bytes

log = logging.getLogger(__name__)

# magic at offset 12 (little- or big-endian)
BSD_MAGICS = {0x00053162: "btree", 0x00061561: "hash", 0x00042253: "queue", 0x00040988: "heap"}


def bsd_kind(head: bytes) -> str | None:
    if len(head) < 16:
        return None
    for endian in ("little", "big"):
        magic = int.from_bytes(head[12:16], endian)
        if magic in BSD_MAGICS:
            return BSD_MAGICS[magic]
    return None


class BsdDbBackend(Backend):
    kind = "bsddb"
    kind_name = "Berkeley DB"

    def __init__(self, path: Any) -> None:
        super().__init__(path)
        try:
            from dissect.database.bsd import DB
        except ImportError as exc:  # pragma: no cover
            raise InvalidDatabaseError("dissect.database is not installed") from exc
        self._fh = open(self.path, "rb")  # noqa: SIM115
        try:
            self._db = DB(self._fh)
        except Exception as exc:
            self._fh.close()
            raise InvalidDatabaseError(f"{self.path.name}: cannot parse Berkeley DB ({exc})") from exc

    def close(self) -> None:
        self._fh.close()

    def tables(self) -> list[BackendTable]:
        cols = [
            ColumnInfo(1, "key", "BLOB", 0, "dynamic", None, None, False, True),
            ColumnInfo(2, "value", "BLOB", 0, "dynamic", None, None, False, True),
            ColumnInfo(3, "key_text", "TEXT", 0, "dynamic", None, None, True, False),
            ColumnInfo(4, "value_text", "TEXT", 0, "dynamic", None, None, True, False),
        ]
        return [BackendTable(name="records", columns=cols)]

    def iter_rows(self, table: str) -> Iterator[dict[str, Any]]:
        for key, value in self._db.records():
            k = key if isinstance(key, bytes) else str(key).encode()
            yield {
                "key": k,
                "value": value,
                "key_text": str(decode_bytes(k)),
                "value_text": str(decode_bytes(value, max_length=4096)),
            }

    def info(self) -> BackendInfo:
        with open(self.path, "rb") as fh:
            head = fh.read(64)
        kind = bsd_kind(head) or "unknown"
        try:
            version = int.from_bytes(head[16:20], "little")
            pagesize = int.from_bytes(head[20:24], "little")
        except Exception:
            version, pagesize = 0, 0
        return BackendInfo(
            kind=self.kind,
            kind_name=self.kind_name,
            page_size=pagesize,
            format_version=version,
            state=f"{kind} access method",
            header={"access_method": kind, "version": version, "pagesize": pagesize},
        )
