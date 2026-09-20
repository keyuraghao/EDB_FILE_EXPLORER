"""LevelDB backend (Chromium IndexedDB / Local Storage / Session Storage, Electron apps, Discord, Teams, Slack ...).

A LevelDB *database* is a directory containing ``*.log`` write-ahead files and
``*.ldb`` / ``*.sst`` sorted tables.  This is a self-contained, read-only
parser for both file types (no C library); snappy/zstd blocks are decoded with
``cramjam``.  Deleted entries and superseded versions are kept because they
are frequently the interesting part in a forensic context.

References: https://github.com/google/leveldb/blob/main/doc/log_format.md and table_format.md
"""

from __future__ import annotations

import logging
import struct
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from edb_explorer.core.backends.base import Backend, BackendInfo, BackendTable
from edb_explorer.core.exceptions import InvalidDatabaseError
from edb_explorer.core.models import ColumnInfo
from edb_explorer.core.values import decode_bytes

log = logging.getLogger(__name__)

TABLE_MAGIC = b"\x57\xfb\x80\x8b\x24\x75\x47\xdb"
LOG_BLOCK_SIZE = 32768
_LOG_FULL, _LOG_FIRST, _LOG_MIDDLE, _LOG_LAST = 1, 2, 3, 4
_TYPE_DELETE, _TYPE_VALUE = 0, 1

_COLS = ("file", "sequence", "operation", "key", "value", "key_text", "value_text", "key_hex")


@dataclass(slots=True)
class LdbRecord:
    file: str
    sequence: int
    deleted: bool
    key: bytes
    value: bytes

    def as_dict(self) -> dict[str, Any]:
        return {
            "file": self.file,
            "sequence": self.sequence,
            "operation": "delete" if self.deleted else "put",
            "key": self.key,
            "value": self.value or None,
            "key_text": decode_key(self.key),
            "value_text": decode_value(self.value) if self.value else None,
            "key_hex": self.key.hex(),
        }


# --------------------------------------------------------------------------- #
# Low-level helpers
# --------------------------------------------------------------------------- #
def _varint(buf: bytes | memoryview, pos: int) -> tuple[int, int]:
    result = shift = 0
    while True:
        b = buf[pos]
        pos += 1
        result |= (b & 0x7F) << shift
        if not b & 0x80:
            return result, pos
        shift += 7
        if shift > 63:
            raise ValueError("varint too long")


def _decompress(data: bytes, ctype: int) -> bytes:
    if ctype == 0:
        return data
    import cramjam

    if ctype == 1:
        return bytes(cramjam.snappy.decompress_raw(data))
    if ctype == 2:
        return bytes(cramjam.zstd.decompress(data))
    raise ValueError(f"unknown block compression {ctype}")


def is_leveldb_dir(path: Path) -> bool:
    if not path.is_dir():
        return False
    names = {p.name for p in path.iterdir()} if path.exists() else set()
    return (
        "CURRENT" in names
        or any(n.startswith("MANIFEST-") for n in names)
        or any(n.endswith((".ldb", ".sst")) for n in names)
    )


def decode_key(key: bytes) -> str:
    """Best-effort textual form of a key (Chromium Local Storage: '_origin\\x00\\x01key')."""
    if key.startswith(b"_") and b"\x00" in key:
        origin, _, rest = key[1:].partition(b"\x00")
        if rest[:1] in (b"\x00", b"\x01"):
            rest = rest[1:]
        return f"{origin.decode('utf-8', 'replace')} :: {decode_bytes(rest) if rest else ''}"
    if key and all(32 <= b < 127 for b in key):
        return key.decode("ascii")
    return str(decode_bytes(key))


def decode_value(value: bytes) -> str:
    """Chromium Local Storage values are prefixed 0x00 (UTF-16LE) or 0x01 (Latin-1)."""
    if len(value) >= 2 and value[0] == 0 and len(value) % 2 == 1:
        try:
            return value[1:].decode("utf-16-le")
        except UnicodeDecodeError:
            pass
    if value[:1] == b"\x01":
        try:
            return value[1:].decode("latin-1")
        except UnicodeDecodeError:
            pass
    if value and all(32 <= b < 127 or b in (9, 10, 13) for b in value):
        return value.decode("ascii")
    return str(decode_bytes(value, max_length=4096))


# --------------------------------------------------------------------------- #
# Parsers
# --------------------------------------------------------------------------- #
def iter_log_file(path: Path) -> Iterator[LdbRecord]:
    """Parse a LevelDB write-ahead log (.log) into records."""
    data = path.read_bytes()
    pos = 0
    pending = bytearray()
    while pos + 7 <= len(data):
        block_left = LOG_BLOCK_SIZE - (pos % LOG_BLOCK_SIZE)
        if block_left < 7:  # trailer padding
            pos += block_left
            continue
        length, rtype = struct.unpack_from("<HB", data, pos + 4)
        pos += 7
        payload = data[pos : pos + length]
        pos += length
        if rtype == 0 and length == 0:
            continue  # zero-filled pre-allocated space
        if rtype == _LOG_FULL:
            yield from _parse_batch(bytes(payload), path.name)
        elif rtype == _LOG_FIRST:
            pending = bytearray(payload)
        elif rtype == _LOG_MIDDLE:
            pending += payload
        elif rtype == _LOG_LAST:
            pending += payload
            yield from _parse_batch(bytes(pending), path.name)
            pending = bytearray()


def _parse_batch(batch: bytes, source: str) -> Iterator[LdbRecord]:
    if len(batch) < 12:
        return
    seq, count = struct.unpack_from("<QI", batch, 0)
    pos = 12
    try:
        for i in range(count):
            rtype = batch[pos]
            pos += 1
            klen, pos = _varint(batch, pos)
            key = batch[pos : pos + klen]
            pos += klen
            value = b""
            if rtype == _TYPE_VALUE:
                vlen, pos = _varint(batch, pos)
                value = batch[pos : pos + vlen]
                pos += vlen
            yield LdbRecord(source, seq + i, rtype == _TYPE_DELETE, bytes(key), bytes(value))
    except (IndexError, ValueError) as exc:
        log.debug("truncated write batch in %s: %s", source, exc)


def _read_block(data: bytes, offset: int, size: int) -> bytes:
    ctype = data[offset + size]
    return _decompress(data[offset : offset + size], ctype)


def _iter_block_entries(block: bytes) -> Iterator[tuple[bytes, bytes]]:
    if len(block) < 4:
        return
    num_restarts = struct.unpack_from("<I", block, len(block) - 4)[0]
    end = len(block) - 4 - 4 * num_restarts
    pos = 0
    key = b""
    while pos < end:
        shared, pos = _varint(block, pos)
        non_shared, pos = _varint(block, pos)
        vlen, pos = _varint(block, pos)
        key = key[:shared] + block[pos : pos + non_shared]
        pos += non_shared
        value = block[pos : pos + vlen]
        pos += vlen
        yield key, value


def iter_table_file(path: Path) -> Iterator[LdbRecord]:
    """Parse a LevelDB sorted table (.ldb/.sst) into records."""
    data = path.read_bytes()
    if len(data) < 48 or data[-8:] != TABLE_MAGIC:
        raise ValueError(f"{path.name}: not a LevelDB table")
    footer = data[-48:]
    pos = 0
    _meta_off, pos = _varint(footer, pos)
    _meta_size, pos = _varint(footer, pos)
    index_off, pos = _varint(footer, pos)
    index_size, pos = _varint(footer, pos)
    index_block = _read_block(data, index_off, index_size)
    for _last_key, handle in _iter_block_entries(index_block):
        off, p = _varint(handle, 0)
        size, _ = _varint(handle, p)
        try:
            block = _read_block(data, off, size)
        except Exception as exc:
            log.debug("bad data block in %s: %s", path.name, exc)
            continue
        for ikey, value in _iter_block_entries(block):
            if len(ikey) < 8:
                continue
            tag = struct.unpack_from("<Q", ikey, len(ikey) - 8)[0]
            seq, rtype = tag >> 8, tag & 0xFF
            yield LdbRecord(path.name, seq, rtype == _TYPE_DELETE, ikey[:-8], value)


# --------------------------------------------------------------------------- #
class LevelDbBackend(Backend):
    kind = "leveldb"
    kind_name = "LevelDB (Chromium / Electron)"

    def __init__(self, path: Any) -> None:
        super().__init__(path)
        if self.path.is_file():
            self.path = self.path.parent
        if not is_leveldb_dir(self.path):
            raise InvalidDatabaseError(f"{self.path}: not a LevelDB directory (no CURRENT/MANIFEST/.ldb files)")
        self._files = sorted(
            [p for p in self.path.iterdir() if p.is_file() and p.suffix in (".log", ".ldb", ".sst")],
            key=lambda p: (p.suffix != ".ldb", p.name),
        )
        self._records: list[LdbRecord] | None = None

    def close(self) -> None:
        self._records = None

    def _load(self) -> list[LdbRecord]:
        if self._records is None:
            out: list[LdbRecord] = []
            for f in self._files:
                try:
                    if f.suffix == ".log":
                        out.extend(iter_log_file(f))
                    else:
                        out.extend(iter_table_file(f))
                except Exception as exc:
                    log.warning("Skipping %s: %s", f.name, exc)
            out.sort(key=lambda r: r.sequence)
            self._records = out
        return self._records

    def _columns(self) -> list[ColumnInfo]:
        types = {"sequence": "INTEGER", "key": "BLOB", "value": "BLOB"}
        return [
            ColumnInfo(
                identifier=i + 1,
                name=c,
                type=types.get(c, "TEXT"),
                type_id=0,
                storage="dynamic",
                size=None,
                encoding=None,
                is_text=types.get(c, "TEXT") == "TEXT",
                is_binary=types.get(c) == "BLOB",
            )
            for i, c in enumerate(_COLS)
        ]

    def tables(self) -> list[BackendTable]:
        return [
            BackendTable(
                name="live",
                columns=self._columns(),
                extra={"description": "Latest value per key (deleted keys removed)"},
            ),
            BackendTable(
                name="all_records",
                columns=self._columns(),
                extra={
                    "description": "Every put/delete from every .log/.ldb file, incl. superseded and deleted versions"
                },
            ),
            BackendTable(
                name="files",
                columns=[
                    ColumnInfo(1, "file", "TEXT", 0, "dynamic", None, None, True, False),
                    ColumnInfo(2, "size_bytes", "INTEGER", 0, "dynamic", None, None, False, False),
                    ColumnInfo(3, "kind", "TEXT", 0, "dynamic", None, None, True, False),
                ],
            ),
        ]

    def iter_rows(self, table: str) -> Iterator[dict[str, Any]]:
        if table == "files":
            for f in sorted(self.path.iterdir()):
                if f.is_file():
                    yield {
                        "file": f.name,
                        "size_bytes": f.stat().st_size,
                        "kind": {".log": "write-ahead log", ".ldb": "sorted table", ".sst": "sorted table"}.get(
                            f.suffix, "metadata"
                        ),
                    }
            return
        records = self._load()
        if table == "all_records":
            for r in records:
                yield r.as_dict()
            return
        latest: dict[bytes, LdbRecord] = {}
        for r in records:  # sorted by sequence -> last wins
            latest[r.key] = r
        for key in sorted(latest):
            r = latest[key]
            if not r.deleted:
                yield r.as_dict()

    def count(self, table: str) -> int | None:
        if table == "all_records":
            return len(self._load())
        return None

    def info(self) -> BackendInfo:
        current = None
        try:
            current = (self.path / "CURRENT").read_text().strip()
        except OSError:
            pass
        return BackendInfo(
            kind=self.kind,
            kind_name=self.kind_name,
            state=f"manifest {current}" if current else "no CURRENT file",
            header={
                "directory": str(self.path),
                "log_files": sum(1 for f in self._files if f.suffix == ".log"),
                "table_files": sum(1 for f in self._files if f.suffix != ".log"),
                "current": current,
            },
            sidecars=[f.name for f in self._files],
        )
