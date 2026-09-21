"""Minimal EVTX writer for tests: every record carries an inline template definition + substitution values."""

from __future__ import annotations

import struct
import uuid
import zlib
from datetime import datetime, timezone
from typing import Any

CHUNK_SIZE = 0x10000
HEADER_BLOCK = 4096
_EPOCH = datetime(1601, 1, 1, tzinfo=timezone.utc)

# BXML value types
T_NULL, T_STRING, T_UINT8, T_UINT16, T_UINT32, T_UINT64, T_GUID, T_FILETIME, T_SID, T_HEX64 = (
    0x00, 0x01, 0x04, 0x06, 0x08, 0x0A, 0x0F, 0x11, 0x13, 0x15,
)


def filetime(dt: datetime) -> int:
    return int((dt - _EPOCH).total_seconds() * 10_000_000)


def _name_hash(s: str) -> int:
    h = 0
    for ch in s:
        h = (h * 65599 + ord(ch)) & 0xFFFF
    return h


class _Bxml:
    """Builds the BinXML byte stream of one template definition; tracks absolute chunk offsets for inline names."""

    def __init__(self, base: int) -> None:
        self.base = base  # chunk offset where this stream starts
        self.buf = bytearray()

    @property
    def pos(self) -> int:
        return self.base + len(self.buf)

    def name(self, s: str) -> None:
        # offset field == position right after it -> the name is inline
        self.buf += struct.pack("<I", self.pos + 4)
        enc = s.encode("utf-16-le")
        self.buf += struct.pack("<IHH", 0, _name_hash(s), len(s)) + enc + b"\x00\x00"

    def sub(self, idx: int, vtype: int, optional: bool = True) -> None:
        self.buf += struct.pack("<BHB", 0x0E if optional else 0x0D, idx, vtype)

    def text(self, s: str) -> None:
        self.buf += b"\x05\x01" + struct.pack("<H", len(s)) + s.encode("utf-16-le")

    def element(self, name: str, attrs: list[tuple[str, Any]] | None = None, children: list[Any] | None = None) -> None:
        """attrs / children values: str -> literal text, ("sub", idx, type) -> substitution, dict -> nested element."""
        attrs = attrs or []
        children = children or []
        self.buf += bytes([0x41 if attrs else 0x01]) + struct.pack("<H", 0xFFFF)
        size_at = len(self.buf)
        self.buf += b"\0\0\0\0"
        self.name(name)
        if attrs:
            attr_size_at = len(self.buf)
            self.buf += b"\0\0\0\0"
            for i, (an, av) in enumerate(attrs):
                self.buf += bytes([0x46 if i < len(attrs) - 1 else 0x06])
                self.name(an)
                self._value(av)
            struct.pack_into("<I", self.buf, attr_size_at, len(self.buf) - attr_size_at - 4)
        if children:
            self.buf += b"\x02"
            for ch in children:
                if isinstance(ch, dict):
                    self.element(ch["name"], ch.get("attrs"), ch.get("children"))
                else:
                    self._value(ch)
            self.buf += b"\x04"
        else:
            self.buf += b"\x03"
        struct.pack_into("<I", self.buf, size_at, len(self.buf) - size_at - 4)

    def _value(self, v: Any) -> None:
        if isinstance(v, tuple) and v[0] == "sub":
            self.sub(v[1], v[2])
        else:
            self.text(str(v))


def _encode_value(vtype: int, v: Any) -> bytes:
    if v is None or vtype == T_NULL:
        return b""
    if vtype == T_STRING:
        return str(v).encode("utf-16-le")
    if vtype == T_UINT8:
        return struct.pack("<B", v)
    if vtype == T_UINT16:
        return struct.pack("<H", v)
    if vtype == T_UINT32:
        return struct.pack("<I", v)
    if vtype in (T_UINT64, T_HEX64):
        return struct.pack("<Q", v)
    if vtype == T_FILETIME:
        return struct.pack("<Q", filetime(v) if isinstance(v, datetime) else v)
    if vtype == T_GUID:
        return uuid.UUID(v).bytes_le
    if vtype == T_SID:
        parts = [int(x) for x in v.split("-")[1:]]
        rev, auth, subs = parts[0], parts[1], parts[2:]
        return struct.pack("<BB", rev, len(subs)) + auth.to_bytes(6, "big") + b"".join(struct.pack("<I", s) for s in subs)
    raise ValueError(vtype)


def build_record(record_id: int, when: datetime, system: dict[str, Any], data: dict[str, Any], *,
                 channel: str, provider: str, provider_guid: str, chunk_offset: int,
                 user_data: bool = False, unnamed: list[str] | None = None) -> bytes:
    """One EVTX record. ``chunk_offset`` is where this record will start inside its chunk."""
    subs: list[tuple[int, Any]] = []  # (type, value) in substitution index order

    def S(vtype: int, value: Any) -> tuple[str, int, int]:
        subs.append((vtype, value))
        return ("sub", len(subs) - 1, vtype)

    # data starts at record + 24 (header) ; fragment header 4 + template token 1+1+4+4 = 10 -> definition at +38
    definition_at = chunk_offset + 24 + 4 + 10
    frag_base = definition_at + 24  # after next_template(4)+guid(16)+data_size(4)
    b = _Bxml(frag_base + 4)  # fragment header of the definition occupies 4 bytes first
    sysid = system
    sys_children: list[Any] = [
        {"name": "Provider", "attrs": [("Name", S(T_STRING, provider)), ("Guid", S(T_GUID, provider_guid))]},
        {"name": "EventID", "attrs": [("Qualifiers", S(T_NULL, None))], "children": [S(T_UINT16, sysid["EventID"])]},
        {"name": "Version", "children": [S(T_UINT8, sysid.get("Version", 0))]},
        {"name": "Level", "children": [S(T_UINT8, sysid.get("Level", 0))]},
        {"name": "Task", "children": [S(T_UINT16, sysid.get("Task", 0))]},
        {"name": "Opcode", "children": [S(T_UINT8, sysid.get("Opcode", 0))]},
        {"name": "Keywords", "children": [S(T_HEX64, sysid.get("Keywords", 0x8020000000000000))]},
        {"name": "TimeCreated", "attrs": [("SystemTime", S(T_FILETIME, when))]},
        {"name": "EventRecordID", "children": [S(T_UINT64, record_id)]},
        {"name": "Correlation", "attrs": [("ActivityID", S(T_NULL, None))]},
        {"name": "Execution", "attrs": [("ProcessID", S(T_UINT32, sysid.get("ProcessID", 592))),
                                        ("ThreadID", S(T_UINT32, sysid.get("ThreadID", 1000)))]},
        {"name": "Channel", "children": [S(T_STRING, channel)]},
        {"name": "Computer", "children": [S(T_STRING, sysid.get("Computer", "DC01.corp.local"))]},
        {"name": "Security", "attrs": [("UserID", S(T_SID, sysid["UserID"]) if sysid.get("UserID") else S(T_NULL, None))]},
    ]
    data_children: list[Any] = []
    for k, v in data.items():
        if isinstance(v, int) and not isinstance(v, bool):
            t = T_UINT32
        elif isinstance(v, datetime):
            t = T_FILETIME
        else:
            t = T_STRING if v is not None else T_NULL
        data_children.append({"name": "Data", "attrs": [("Name", k)], "children": [S(t, v)]})
    for v in unnamed or []:
        data_children.append({"name": "Data", "children": [S(T_STRING, v)]})
    if user_data:
        payload = {"name": "UserData", "children": [{"name": "EventXML", "attrs": [("xmlns", "Event_NS")],
                   "children": [{"name": k, "children": [S(T_STRING if not isinstance(v, int) else T_UINT32, v)]}
                                for k, v in data.items()]}]}
    else:
        payload = {"name": "EventData", "children": data_children}
    b.element("Event", [("xmlns", "http://schemas.microsoft.com/win/2004/08/events/event")],
              [{"name": "System", "children": sys_children}, payload])
    fragment = b"\x0f\x01\x01\x00" + bytes(b.buf) + b"\x00"
    definition = struct.pack("<I", 0) + uuid.uuid4().bytes_le + struct.pack("<I", len(fragment)) + fragment
    tpl_token = b"\x0c\x01" + struct.pack("<II", 0x1234, definition_at)
    desc = struct.pack("<I", len(subs))
    vals = b""
    for vtype, value in subs:
        enc = _encode_value(vtype, value)
        desc += struct.pack("<HBB", len(enc), vtype, 0)
        vals += enc
    body = b"\x0f\x01\x01\x00" + tpl_token + definition + desc + vals + b"\x00"
    size = 24 + len(body) + 4
    return struct.pack("<IIQQ", 0x2A2A, size, record_id, filetime(when)) + body + struct.pack("<I", size)


def build_chunk(records: list[dict[str, Any]], first_nr: int) -> bytes:
    """records: kwargs for build_record (minus chunk_offset)."""
    data = bytearray()
    ids: list[int] = []
    last_off = 512
    for r in records:
        off = 512 + len(data)
        last_off = off
        data += build_record(chunk_offset=off, **r)
        ids.append(r["record_id"])
    free = 512 + len(data)
    hdr = bytearray(512)
    struct.pack_into("<8sQQQQIIII", hdr, 0, b"ElfChnk\x00", first_nr, first_nr + len(ids) - 1, ids[0], ids[-1],
                     128, last_off, free, zlib.crc32(bytes(data)))
    struct.pack_into("<I", hdr, 120, 0)  # flags
    struct.pack_into("<I", hdr, 124, zlib.crc32(bytes(hdr[:120]) + bytes(hdr[128:512])))
    chunk = bytes(hdr) + bytes(data)
    return chunk + b"\x00" * (CHUNK_SIZE - len(chunk))


def build_file(chunks: list[bytes], *, first_chunk: int = 0, last_chunk: int | None = None,
               next_record_id: int = 1, dirty: bool = False, trailing_empty: int = 0) -> bytes:
    hdr = bytearray(HEADER_BLOCK)
    struct.pack_into("<8sQQQIHHHH", hdr, 0, b"ElfFile\x00", first_chunk,
                     len(chunks) - 1 if last_chunk is None else last_chunk, next_record_id, 128, 1, 3, HEADER_BLOCK,
                     len(chunks))
    struct.pack_into("<I", hdr, 120, 1 if dirty else 0)
    struct.pack_into("<I", hdr, 124, zlib.crc32(bytes(hdr[:120])))
    return bytes(hdr) + b"".join(chunks) + b"\x00" * (CHUNK_SIZE * trailing_empty)
