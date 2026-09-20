"""Microsoft Access backend (.mdb Jet 3/4, .accdb ACE) via the pure-Python ``access-parser``."""

from __future__ import annotations

import logging
from collections.abc import Iterator
from typing import Any

from edb_explorer.core.backends.base import Backend, BackendInfo, BackendTable, sanitize_columns
from edb_explorer.core.exceptions import InvalidDatabaseError
from edb_explorer.core.models import ColumnInfo

log = logging.getLogger(__name__)

ACCESS_MAGICS = (b"Standard Jet DB", b"Standard ACE DB")  # at file offset 4


class AccessBackend(Backend):
    kind = "access"
    kind_name = "Microsoft Access (Jet/ACE)"

    def __init__(self, path: Any) -> None:
        super().__init__(path)
        try:
            from access_parser import AccessParser
        except ImportError as exc:  # pragma: no cover
            raise InvalidDatabaseError("access-parser is not installed") from exc
        try:
            self._db = AccessParser(str(self.path))
        except Exception as exc:
            raise InvalidDatabaseError(f"{self.path.name}: cannot parse Access database ({exc})") from exc
        self._tables: list[BackendTable] | None = None
        self._cache: dict[str, dict[str, list[Any]]] = {}

    def close(self) -> None:
        self._cache.clear()

    def _table_data(self, name: str) -> dict[str, list[Any]]:
        if name not in self._cache:
            try:
                data = self._db.parse_table(name) or {}
            except Exception as exc:
                log.warning("Failed to parse Access table %s: %s", name, exc)
                data = {}
            self._cache[name] = data
        return self._cache[name]

    def tables(self) -> list[BackendTable]:
        if self._tables is None:
            out: list[BackendTable] = []
            catalog = getattr(self._db, "catalog", {}) or {}
            for name in sorted(catalog):
                data = self._table_data(name)
                names = sanitize_columns(list(data.keys()))
                columns = [
                    ColumnInfo(i + 1, n, _guess_type(data[orig]), 0, "fixed", None, None, False, False)
                    for i, (orig, n) in enumerate(zip(data.keys(), names, strict=False))
                ]
                count = max((len(v) for v in data.values()), default=0)
                out.append(
                    BackendTable(
                        name=name,
                        columns=columns,
                        is_system=name.startswith("MSys") or name.startswith("f_"),
                        record_count=count,
                        extra={"_names": names},
                    )
                )
            self._tables = out
        return self._tables

    def iter_rows(self, table: str) -> Iterator[dict[str, Any]]:
        info = next((t for t in self.tables() if t.name == table), None)
        data = self._table_data(table)
        names = info.extra["_names"] if info else list(data.keys())
        cols = list(data.values())
        n = max((len(v) for v in cols), default=0)
        for i in range(n):
            yield {name: (col[i] if i < len(col) else None) for name, col in zip(names, cols, strict=False)}

    def count(self, table: str) -> int | None:
        data = self._table_data(table)
        return max((len(v) for v in data.values()), default=0)

    def info(self) -> BackendInfo:
        version = "unknown"
        try:
            with open(self.path, "rb") as fh:
                head = fh.read(64)
            version = "ACE (.accdb)" if b"ACE" in head[4:20] else "Jet (.mdb)"
            jet = head[20] if len(head) > 20 else 0
            version += f" v{jet}"
        except OSError:
            pass
        return BackendInfo(
            kind=self.kind, kind_name=self.kind_name, page_size=4096, state=version, header={"engine": version}
        )


def _guess_type(values: list[Any]) -> str:
    for v in values:
        if v is None:
            continue
        if isinstance(v, bool):
            return "Bit"
        if isinstance(v, int):
            return "Long"
        if isinstance(v, float):
            return "Double"
        if isinstance(v, bytes | bytearray):
            return "Binary"
        return "Text"
    return "Text"
