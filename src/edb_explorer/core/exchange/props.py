"""Parser for the Exchange 2013+ store property blob (``ProP`` format).

Layout (reverse-engineered, validated on thousands of blobs):

    "ProP" | version u16 | count u16
    count × [ type u8 | property id u16 ]          # tag table
    values in tag order, each starting with a marker byte

``type`` is the storage class of the property; bit 0x02 set means the value is
kept in another blob (``OffPagePropertyBlob`` / ``LargePropertyValueBlob``)
and has no value here.  The marker byte repeats the class in its high bits and
encodes the size in the low bits:

    0x08 bool         marker 0x08 = False, 0x09 = True
    0x18/0x20 int     low bits = byte count (0 = value 0, 3 = 4 bytes, 4 = 8 bytes)
    0x38 int64        marker 0x38 = 8 bytes (FILETIME / int64)
    0x40 GUID         16 bytes
    0x48 string       bit 0x04 = 8-bit text, low 2 bits = length-prefix size (0 = empty)
    0x50 binary       low 2 bits = length-prefix size
    0x98/0xB8/0xC8/0xD0 multi-valued int32 / int64 / string / binary: count prefix then items
"""

from __future__ import annotations

import struct
import uuid
from dataclasses import dataclass, field
from typing import Any

from edb_explorer.core.values import filetime_to_datetime

MAGIC = b"ProP"
_LEN_SIZES = {0: 0, 1: 1, 2: 2, 3: 4}
_INT_SIZES = {0: 0, 1: 1, 2: 2, 3: 4, 4: 8, 5: 1, 6: 2, 7: 4}
_INT64_SIZES = {0: 8, 1: 4, 2: 2, 3: 1, 4: 8, 5: 1, 6: 2, 7: 4}

# Well-known MAPI property identifiers (subset large enough for mail triage).
PROPERTY_NAMES: dict[int, str] = {
    0x0002: "PR_ALTERNATE_RECIPIENT_ALLOWED",
    0x0017: "PR_IMPORTANCE",
    0x001A: "PR_MESSAGE_CLASS",
    0x0023: "PR_ORIGINATOR_DELIVERY_REPORT_REQUESTED",
    0x0026: "PR_PRIORITY",
    0x0029: "PR_READ_RECEIPT_REQUESTED",
    0x002B: "PR_RECIPIENT_REASSIGNMENT_PROHIBITED",
    0x002E: "PR_ORIGINAL_SENSITIVITY",
    0x0030: "PR_REPLY_TIME",
    0x0031: "PR_REPORT_TAG",
    0x0032: "PR_REPORT_TIME",
    0x0036: "PR_SENSITIVITY",
    0x0037: "PR_SUBJECT",
    0x0039: "PR_CLIENT_SUBMIT_TIME",
    0x003A: "PR_REPORT_NAME",
    0x003B: "PR_SENT_REPRESENTING_SEARCH_KEY",
    0x003D: "PR_SUBJECT_PREFIX",
    0x003F: "PR_RECEIVED_BY_ENTRYID",
    0x0040: "PR_RECEIVED_BY_NAME",
    0x0041: "PR_SENT_REPRESENTING_ENTRYID",
    0x0042: "PR_SENT_REPRESENTING_NAME",
    0x0043: "PR_RCVD_REPRESENTING_ENTRYID",
    0x0044: "PR_RCVD_REPRESENTING_NAME",
    0x0047: "PR_MESSAGE_SUBMISSION_ID",
    0x004F: "PR_REPLY_RECIPIENT_ENTRIES",
    0x0050: "PR_REPLY_RECIPIENT_NAMES",
    0x0051: "PR_RECEIVED_BY_SEARCH_KEY",
    0x0052: "PR_RCVD_REPRESENTING_SEARCH_KEY",
    0x0057: "PR_MESSAGE_TO_ME",
    0x0058: "PR_MESSAGE_CC_ME",
    0x0059: "PR_MESSAGE_RECIP_ME",
    0x0064: "PR_SENT_REPRESENTING_ADDRTYPE",
    0x0065: "PR_SENT_REPRESENTING_EMAIL_ADDRESS",
    0x0070: "PR_CONVERSATION_TOPIC",
    0x0071: "PR_CONVERSATION_INDEX",
    0x0075: "PR_RECEIVED_BY_ADDRTYPE",
    0x0076: "PR_RECEIVED_BY_EMAIL_ADDRESS",
    0x0077: "PR_RCVD_REPRESENTING_ADDRTYPE",
    0x0078: "PR_RCVD_REPRESENTING_EMAIL_ADDRESS",
    0x007D: "PR_TRANSPORT_MESSAGE_HEADERS",
    0x007F: "PR_TNEF_CORRELATION_KEY",
    0x0C15: "PR_RECIPIENT_TYPE",
    0x0C17: "PR_REPLY_REQUESTED",
    0x0C19: "PR_SENDER_ENTRYID",
    0x0C1A: "PR_SENDER_NAME",
    0x0C1D: "PR_SENDER_SEARCH_KEY",
    0x0C1E: "PR_SENDER_ADDRTYPE",
    0x0C1F: "PR_SENDER_EMAIL_ADDRESS",
    0x0E01: "PR_DELETE_AFTER_SUBMIT",
    0x0E02: "PR_DISPLAY_BCC",
    0x0E03: "PR_DISPLAY_CC",
    0x0E04: "PR_DISPLAY_TO",
    0x0E06: "PR_MESSAGE_DELIVERY_TIME",
    0x0E07: "PR_MESSAGE_FLAGS",
    0x0E08: "PR_MESSAGE_SIZE",
    0x0E09: "PR_PARENT_ENTRYID",
    0x0E0F: "PR_RESPONSIBILITY",
    0x0E1B: "PR_HASATTACH",
    0x0E1D: "PR_NORMALIZED_SUBJECT",
    0x0E1F: "PR_RTF_IN_SYNC",
    0x0E20: "PR_ATTACH_SIZE",
    0x0E21: "PR_ATTACH_NUM",
    0x0E28: "PR_PRIMARY_SEND_ACCT",
    0x0E29: "PR_NEXT_SEND_ACCT",
    0x0E4B: "PR_SENDER_SID",
    0x0E4C: "PR_SENT_REPRESENTING_SID",
    0x0E4D: "PR_ORIGINAL_SENDER_SID",
    0x0E58: "PR_LAST_MODIFIER_SID",
    0x0E99: "PR_SENDER_ENTRYID_WORKSPACE",
    0x0E9A: "PR_SENT_REPRESENTING_ENTRYID_WORKSPACE",
    0x0F0A: "PR_SEARCH_ATTACHMENTS_OLK",
    0x0FF4: "PR_ACCESS",
    0x0FF7: "PR_ACCESS_LEVEL",
    0x0FF9: "PR_RECORD_KEY",
    0x0FFE: "PR_OBJECT_TYPE",
    0x0FFF: "PR_ENTRYID",
    0x1000: "PR_BODY",
    0x1009: "PR_RTF_COMPRESSED",
    0x1013: "PR_HTML",
    0x1015: "PR_BODY_CONTENT_ID",
    0x1016: "PR_NATIVE_BODY_INFO",
    0x1035: "PR_INTERNET_MESSAGE_ID",
    0x1039: "PR_INTERNET_REFERENCES",
    0x1042: "PR_IN_REPLY_TO_ID",
    0x1046: "PR_INTERNET_RETURN_PATH",
    0x1080: "PR_ICON_INDEX",
    0x1081: "PR_LAST_VERB_EXECUTED",
    0x1082: "PR_LAST_VERB_EXECUTION_TIME",
    0x1090: "PR_FLAG_STATUS",
    0x1095: "PR_FLAG_ICON",
    0x10F3: "PR_URL_COMP_NAME",
    0x10F4: "PR_ATTR_HIDDEN",
    0x10F5: "PR_ATTR_SYSTEM",
    0x10F6: "PR_ATTR_READONLY",
    0x3001: "PR_DISPLAY_NAME",
    0x3002: "PR_ADDRTYPE",
    0x3003: "PR_EMAIL_ADDRESS",
    0x3004: "PR_COMMENT",
    0x3007: "PR_CREATION_TIME",
    0x3008: "PR_LAST_MODIFICATION_TIME",
    0x300B: "PR_SEARCH_KEY",
    0x3010: "PR_TARGET_ENTRYID",
    0x3013: "PR_CONVERSATION_ID",
    0x3014: "PR_CONVERSATION_INDEX_TRACKING",
    0x3016: "PR_CONVERSATION_INDEX_TRACKING_LEGACY",
    0x3701: "PR_ATTACH_DATA_BIN",
    0x3702: "PR_ATTACH_ENCODING",
    0x3703: "PR_ATTACH_EXTENSION",
    0x3704: "PR_ATTACH_FILENAME",
    0x3705: "PR_ATTACH_METHOD",
    0x3707: "PR_ATTACH_LONG_FILENAME",
    0x3708: "PR_ATTACH_PATHNAME",
    0x370B: "PR_RENDERING_POSITION",
    0x370E: "PR_ATTACH_MIME_TAG",
    0x3712: "PR_ATTACH_CONTENT_ID",
    0x3714: "PR_ATTACH_FLAGS",
    0x3900: "PR_DISPLAY_TYPE",
    0x3905: "PR_DISPLAY_TYPE_EX",
    0x39FE: "PR_SMTP_ADDRESS",
    0x39FF: "PR_7BIT_DISPLAY_NAME",
    0x3A00: "PR_ACCOUNT",
    0x3A06: "PR_GIVEN_NAME",
    0x3A08: "PR_BUSINESS_TELEPHONE_NUMBER",
    0x3A11: "PR_SURNAME",
    0x3A17: "PR_TITLE",
    0x3A18: "PR_DEPARTMENT_NAME",
    0x3A20: "PR_TRANSMITABLE_DISPLAY_NAME",
    0x3A40: "PR_SEND_RICH_INFO",
    0x3FDE: "PR_INTERNET_CPID",
    0x3FDF: "PR_AUTO_RESPONSE_SUPPRESS",
    0x3FF1: "PR_MESSAGE_LOCALE_ID",
    0x3FF8: "PR_CREATOR_NAME",
    0x3FF9: "PR_CREATOR_ENTRYID",
    0x3FFA: "PR_LAST_MODIFIER_NAME",
    0x3FFB: "PR_LAST_MODIFIER_ENTRYID",
    0x3FFD: "PR_MESSAGE_CODEPAGE",
    0x401A: "PR_SENT_REPRESENTING_FLAGS",
    0x4022: "PR_RCVD_REPRESENTING_SMTP_ADDRESS",
    0x4023: "PR_SENDER_FLAGS",
    0x4029: "PR_READ_RECEIPT_ADDRTYPE",
    0x402A: "PR_READ_RECEIPT_EMAIL_ADDRESS",
    0x4030: "PR_SENDER_SIMPLE_DISP_NAME",
    0x4031: "PR_SENT_REPRESENTING_SIMPLE_DISP_NAME",
    0x4034: "PR_RECEIVED_BY_SIMPLE_DISP_NAME",
    0x4035: "PR_RCVD_REPRESENTING_SIMPLE_DISP_NAME",
    0x4038: "PR_LAST_MODIFIER_NAME_W",
    0x4059: "PR_CREATOR_FLAGS",
    0x405A: "PR_MODIFIER_FLAGS",
    0x5902: "PR_INETMAIL_OVERRIDE_FORMAT",
    0x5909: "PR_MSG_EDITOR_FORMAT",
    0x5D01: "PR_SENDER_SMTP_ADDRESS",
    0x5D02: "PR_SENT_REPRESENTING_SMTP_ADDRESS",
    0x5D07: "PR_RECEIVED_BY_SMTP_ADDRESS",
    0x5D08: "PR_RCVD_REPRESENTING_SMTP_ADDRESS_W",
    0x5D0A: "PR_CREATOR_SMTP_ADDRESS",
    0x5D0B: "PR_LAST_MODIFIER_SMTP_ADDRESS",
    0x5FDE: "PR_RECIPIENT_RESOURCESTATE",
    0x5FDF: "PR_RECIPIENT_ORDER",
    0x5FE5: "PR_RECIPIENT_PROPOSED",
    0x5FF6: "PR_RECIPIENT_DISPLAY_NAME",
    0x5FF7: "PR_RECIPIENT_ENTRYID",
    0x5FFD: "PR_RECIPIENT_FLAGS",
    0x5FFF: "PR_RECIPIENT_TRACKSTATUS",
    0x6001: "PR_NICKNAME",
    0x65C6: "PR_SECURE_SUBMIT_FLAGS",
    0x65E0: "PR_SOURCE_KEY",
    0x65E1: "PR_PARENT_SOURCE_KEY",
    0x65E2: "PR_CHANGE_KEY",
    0x65E3: "PR_PREDECESSOR_CHANGE_LIST",
    0x66CA: "PR_RECIPIENT_ADDRESS_TYPE",
    0x6740: "PR_SENT_MAIL_SVR_EID",
    0x6749: "PR_FOLDER_ID",
    0x674A: "PR_MID",
    0x6796: "PR_CI_SEARCH_ENABLED",
    0x3FFC: "PR_ORIGINAL_MESSAGE_CLASS",
    0x0E12: "PR_MESSAGE_RECIPIENTS",
    0x0E13: "PR_MESSAGE_ATTACHMENTS",
}
RECIPIENT_TYPES = {0: "originator", 1: "to", 2: "cc", 3: "bcc"}
ATTACH_METHODS = {
    0: "none",
    1: "by value",
    2: "by reference",
    3: "by reference resolve",
    4: "by reference only",
    5: "embedded message",
    6: "OLE",
    7: "web reference",
}


def property_name(pid: int, named: dict[int, str] | None = None) -> str:
    if named and pid in named:
        return named[pid]
    name = PROPERTY_NAMES.get(pid)
    return name if name else f"0x{pid:04X}"


@dataclass(slots=True)
class PropertyBlob:
    """Decoded ``ProP`` blob: {property id: value} plus which ids live in another blob."""

    version: int = 0
    values: dict[int, Any] = field(default_factory=dict)
    offpage: list[int] = field(default_factory=list)
    truncated_at: int | None = None  # property id where parsing stopped, if the grammar failed

    def get(self, pid: int, default: Any = None) -> Any:
        return self.values.get(pid, default)

    def merge(self, other: PropertyBlob | None) -> PropertyBlob:
        if other:
            for k, v in other.values.items():
                self.values.setdefault(k, v)
        return self

    def as_named(self, named: dict[int, str] | None = None) -> dict[str, Any]:
        return {property_name(k, named): v for k, v in sorted(self.values.items())}


def _take(b: bytes, pos: int, n: int) -> tuple[bytes, int]:
    if pos + n > len(b):
        raise ValueError("truncated blob")
    return b[pos : pos + n], pos + n


def _read_len(b: bytes, pos: int, code: int) -> tuple[int, int]:
    size = _LEN_SIZES[code & 3]
    if size == 0:
        return 0, pos
    raw, pos = _take(b, pos, size)
    return int.from_bytes(raw, "little"), pos


def _read_string(b: bytes, pos: int, marker: int) -> tuple[str, int]:
    n, pos = _read_len(b, pos, marker)
    raw, pos = _take(b, pos, n)
    if marker & 0x04:
        return raw.decode("latin-1"), pos
    return raw.decode("utf-16-le", "replace"), pos


def parse_property_blob(data: bytes | None) -> PropertyBlob:
    """Decode a ProP blob; never raises - partial results are returned with ``truncated_at`` set."""
    out = PropertyBlob()
    if not data or len(data) < 8 or data[:4] != MAGIC:
        return out
    b = bytes(data)
    out.version, count = struct.unpack_from("<HH", b, 4)
    pos = 8
    tags: list[tuple[int, int]] = []
    for _ in range(count):
        if pos + 3 > len(b):
            break
        tags.append((b[pos], struct.unpack_from("<H", b, pos + 1)[0]))
        pos += 3
    for ty, pid in tags:
        if ty & 0x02:
            out.offpage.append(pid)
            continue
        try:
            if pos >= len(b):
                raise ValueError("truncated")
            m = b[pos]
            pos += 1
            base, lo = m & 0xF8, m & 0x07
            if base == 0x08:
                out.values[pid] = bool(lo)
            elif base in (0x18, 0x20):
                raw, pos = _take(b, pos, _INT_SIZES[lo])
                out.values[pid] = int.from_bytes(raw, "little", signed=True) if raw else 0
            elif base == 0x38:
                raw, pos = _take(b, pos, _INT64_SIZES[lo])
                out.values[pid] = int.from_bytes(raw, "little")
            elif base == 0x40:
                raw, pos = _take(b, pos, 16)
                out.values[pid] = uuid.UUID(bytes_le=raw)
            elif base == 0x48:
                out.values[pid], pos = _read_string(b, pos, m)
            elif base == 0x50:
                n, pos = _read_len(b, pos, m)
                out.values[pid], pos = _take(b, pos, n)
            elif base in (0x98, 0xB8, 0xC8, 0xD0, 0x70, 0x88):
                cnt, pos = _read_len(b, pos, m)
                items: list[Any] = []
                for _ in range(cnt):
                    if base == 0x98:
                        raw, pos = _take(b, pos, 4)
                        items.append(int.from_bytes(raw, "little", signed=True))
                    elif base == 0xB8:
                        raw, pos = _take(b, pos, 8)
                        items.append(int.from_bytes(raw, "little"))
                    elif base in (0x70, 0x88):
                        raw, pos = _take(b, pos, 2)
                        items.append(int.from_bytes(raw, "little", signed=True))
                    else:
                        m2 = b[pos]
                        pos += 1
                        if base == 0xD0:
                            n, pos = _read_len(b, pos, m2)
                            raw, pos = _take(b, pos, n)
                            items.append(raw)
                        else:
                            s, pos = _read_string(b, pos, m2)
                            items.append(s)
                out.values[pid] = items
            else:
                raise ValueError(f"unknown marker {m:#04x}")
        except (ValueError, KeyError, IndexError, struct.error):
            out.truncated_at = pid
            break
    return out


def decode_value_for_display(pid: int, value: Any) -> Any:
    """Make a property value JSON/GUI friendly (FILETIMEs to ISO, bytes to hex)."""
    if isinstance(value, uuid.UUID):
        return f"{{{value}}}"
    if isinstance(value, int) and not isinstance(value, bool) and value > 10**17:
        dt = filetime_to_datetime(value)
        if dt is not None and 1990 <= dt.year <= 2100:
            return dt.isoformat()
    if isinstance(value, bytes):
        from edb_explorer.core.values import decode_bytes

        return decode_bytes(value, "smart", max_length=2000)
    if isinstance(value, list):
        return [decode_value_for_display(pid, v) for v in value]
    return value
