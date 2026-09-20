"""BSON dump backend (mongodump ``*.bson`` collection files)."""

from __future__ import annotations

import json
import logging
import struct
from collections.abc import Iterator
from datetime import datetime
from typing import Any

from edb_explorer.core.backends.base import Backend, BackendInfo, BackendTable, sanitize_columns
from edb_explorer.core.exceptions import InvalidDatabaseError
from edb_explorer.core.models import ColumnInfo

log = logging.getLogger(__name__)


def looks_like_bson(head: bytes, name: str = "") -> bool:
    if not name.lower().endswith(".bson") or len(head) < 5:
        return False
    length = struct.unpack_from("<i", head, 0)[0]
    return 5 <= length <= 16 * 1024 * 1024


def _flatten(v: Any) -> Any:
    if isinstance(v, dict | list | tuple):
        return json.dumps(v, default=str, ensure_ascii=False)
    if isinstance(v, datetime):
        return v.isoformat()
    if isinstance(v, bytes | bytearray | int | float | str) or v is None:
        return v
    return str(v)


class BsonDumpBackend(Backend):
    kind = "bson"
    kind_name = "BSON dump (mongodump)"

    def __init__(self, path: Any) -> None:
        super().__init__(path)
        try:
            import bson  # noqa: F401
        except ImportError as exc:  # pragma: no cover
            raise InvalidDatabaseError("bson is not installed") from exc
        self._table = self.path.stem
        self._columns: list[str] | None = None
        self._count: int | None = None
        try:
            next(self._iter_docs(), None)
        except Exception as exc:
            raise InvalidDatabaseError(f"{self.path.name}: cannot parse BSON ({exc})") from exc

    def close(self) -> None:
        pass

    def _iter_docs(self) -> Iterator[dict[str, Any]]:
        import bson

        with open(self.path, "rb") as fh:
            while True:
                head = fh.read(4)
                if len(head) < 4:
                    return
                length = struct.unpack("<i", head)[0]
                if length < 5:
                    return
                body = head + fh.read(length - 4)
                if len(body) < length:
                    return
                try:
                    _end, doc = bson.decode_document(body, 0)
                except Exception as exc:
                    log.debug("bad BSON document: %s", exc)
                    continue
                yield doc

    def _scan(self) -> None:
        if self._columns is None:
            cols: list[str] = []
            n = 0
            for doc in self._iter_docs():
                n += 1
                for k in doc:
                    if k not in cols:
                        cols.append(k)
            self._columns = sanitize_columns(cols)
            self._count = n

    def tables(self) -> list[BackendTable]:
        self._scan()
        assert self._columns is not None
        cols = [
            ColumnInfo(i + 1, c, "ANY", 0, "dynamic", None, "utf-8", False, False) for i, c in enumerate(self._columns)
        ]
        return [BackendTable(name=self._table, columns=cols, record_count=self._count)]

    def iter_rows(self, table: str) -> Iterator[dict[str, Any]]:
        for doc in self._iter_docs():
            yield {k: _flatten(v) for k, v in doc.items()}

    def count(self, table: str) -> int | None:
        self._scan()
        return self._count

    def info(self) -> BackendInfo:
        self._scan()
        return BackendInfo(
            kind=self.kind,
            kind_name=self.kind_name,
            state=f"{self._count:,} documents",
            header={"collection": self._table, "documents": self._count, "fields": len(self._columns or [])},
        )
