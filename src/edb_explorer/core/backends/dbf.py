"""dBase / FoxPro / Clipper .dbf backend via ``dbfread`` (memo files .dbt/.fpt picked up automatically)."""

from __future__ import annotations

import logging
from collections.abc import Iterator
from datetime import date, datetime, timezone
from typing import Any

from edb_explorer.core.backends.base import Backend, BackendInfo, BackendTable, sanitize_columns
from edb_explorer.core.exceptions import InvalidDatabaseError
from edb_explorer.core.models import ColumnInfo

log = logging.getLogger(__name__)

# First byte of a DBF header: known version signatures.
DBF_VERSIONS = {
    0x02: "FoxBASE",
    0x03: "dBase III/IV (no memo)",
    0x04: "dBase 7",
    0x05: "dBase 5",
    0x30: "Visual FoxPro",
    0x31: "Visual FoxPro (autoincrement)",
    0x32: "Visual FoxPro (varchar)",
    0x43: "dBase IV SQL table",
    0x63: "dBase IV SQL system",
    0x7B: "dBase IV (memo)",
    0x83: "dBase III+ (memo)",
    0x8B: "dBase IV (memo)",
    0x8E: "dBase IV SQL (memo)",
    0xCB: "dBase IV SQL system (memo)",
    0xE5: "Clipper SIX (SMT memo)",
    0xF5: "FoxPro 2.x (memo)",
    0xFB: "FoxBASE",
}
_TYPE_NAMES = {
    "C": "Character",
    "N": "Numeric",
    "F": "Float",
    "L": "Logical",
    "D": "Date",
    "M": "Memo",
    "T": "DateTime",
    "I": "Integer",
    "Y": "Currency",
    "B": "Double",
    "G": "General",
    "P": "Picture",
    "0": "Flags",
    "V": "Varchar",
    "Q": "Varbinary",
    "+": "Autoincrement",
    "@": "Timestamp",
}


def is_dbf_file(path: Any) -> bool:
    try:
        with open(path, "rb") as fh:
            head = fh.read(32)
    except OSError:
        return False
    if len(head) < 32 or head[0] not in DBF_VERSIONS:
        return False
    y, m, d = head[1], head[2], head[3]
    header_len = int.from_bytes(head[8:10], "little")
    record_len = int.from_bytes(head[10:12], "little")
    return 1 <= m <= 12 and 1 <= d <= 31 and y <= 200 and 32 < header_len < 65535 and 0 < record_len < 65535


class DbfBackend(Backend):
    kind = "dbf"
    kind_name = "dBase / FoxPro (DBF)"

    def __init__(self, path: Any) -> None:
        super().__init__(path)
        try:
            from dbfread import DBF
        except ImportError as exc:  # pragma: no cover
            raise InvalidDatabaseError("dbfread is not installed") from exc
        if not is_dbf_file(self.path):
            raise InvalidDatabaseError(f"{self.path.name}: not a DBF file")
        try:
            self._dbf = DBF(str(self.path), load=False, ignore_missing_memofile=True, char_decode_errors="replace")
        except Exception as exc:
            raise InvalidDatabaseError(f"{self.path.name}: cannot parse DBF ({exc})") from exc
        self._names = sanitize_columns([f.name for f in self._dbf.fields])
        self._table_name = self.path.stem

    def close(self) -> None:
        pass

    def tables(self) -> list[BackendTable]:
        columns = [
            ColumnInfo(
                i + 1,
                name,
                _TYPE_NAMES.get(f.type, f.type),
                0,
                "fixed",
                f.length,
                self._dbf.encoding,
                f.type in ("C", "M", "V"),
                f.type in ("G", "P", "Q"),
            )
            for i, (f, name) in enumerate(zip(self._dbf.fields, self._names, strict=False))
        ]
        return [
            BackendTable(
                name=self._table_name, columns=columns, record_count=len(self._dbf), extra={"deleted_available": True}
            ),
            BackendTable(
                name=f"{self._table_name} (deleted records)",
                columns=columns,
                extra={"description": "Records flagged as deleted (dBase soft delete)"},
            ),
        ]

    def iter_rows(self, table: str) -> Iterator[dict[str, Any]]:
        source = self._dbf.deleted if table.endswith("(deleted records)") else self._dbf
        for rec in source:
            yield {name: _norm(v) for name, v in zip(self._names, rec.values(), strict=False)}

    def count(self, table: str) -> int | None:
        return len(self._dbf) if not table.endswith("(deleted records)") else None

    def info(self) -> BackendInfo:
        with open(self.path, "rb") as fh:
            head = fh.read(32)
        version = DBF_VERSIONS.get(head[0], f"0x{head[0]:02x}")
        y, m, d = head[1], head[2], head[3]
        try:
            updated = datetime(1900 + y, m, d, tzinfo=timezone.utc)
        except ValueError:
            updated = None
        return BackendInfo(
            kind=self.kind,
            kind_name=self.kind_name,
            page_size=int.from_bytes(head[10:12], "little"),
            state=version,
            last_attach=updated,
            encoding=self._dbf.encoding,
            header={
                "version": version,
                "last_update": updated.date().isoformat() if updated else None,
                "record_count": len(self._dbf),
                "header_length": int.from_bytes(head[8:10], "little"),
                "record_length": int.from_bytes(head[10:12], "little"),
                "memo_file": str(getattr(self._dbf, "memofilename", "") or ""),
            },
        )


def _norm(v: Any) -> Any:
    if isinstance(v, datetime | date):
        return v.isoformat()
    return v
