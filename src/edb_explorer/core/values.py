"""Value decoding helpers.

ESE stores several column types in ways that need interpretation before they
are useful to an analyst:

* ``DateTime`` columns hold an 8-byte value that is *usually* an OLE Automation
  date (a ``double`` counting days since 1899-12-30) but some applications
  store a Windows FILETIME instead.  We decode the OLE form when it is
  plausible and otherwise keep the raw integer.
* Binary columns often contain UTF-16LE strings (SRUM, Windows Search), GUIDs,
  SIDs or FILETIMEs.  :func:`decode_bytes` tries the common cases.
* ``Currency`` / ``LongLong`` columns frequently carry FILETIME values.
  :func:`interpret_timestamp` exposes every plausible reading so a user (or an
  AI agent) can pick the right one.
"""

from __future__ import annotations

import base64
import binascii
import math
import re
import struct
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Literal

UTC = timezone.utc

BytesMode = Literal["smart", "hex", "base64", "raw"]

_FILETIME_EPOCH = datetime(1601, 1, 1, tzinfo=UTC)
_OLE_EPOCH = datetime(1899, 12, 30, tzinfo=UTC)
_UNIX_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
_MAC_EPOCH = datetime(1904, 1, 1, tzinfo=UTC)
_COCOA_EPOCH = datetime(2001, 1, 1, tzinfo=UTC)

# Plausibility window used when guessing timestamp encodings.
_MIN_PLAUSIBLE = datetime(1980, 1, 1, tzinfo=UTC)
_MAX_PLAUSIBLE = datetime(2100, 1, 1, tzinfo=UTC)

_PRINTABLE_RE = re.compile(r"^[\x20-\x7e\t\r\n]*$")


# --------------------------------------------------------------------------- #
# Timestamps
# --------------------------------------------------------------------------- #
def filetime_to_datetime(value: int) -> datetime | None:
    """Convert a Windows FILETIME (100ns ticks since 1601-01-01) to UTC datetime."""
    if value <= 0:
        return None
    try:
        return _FILETIME_EPOCH + timedelta(microseconds=value // 10)
    except (OverflowError, ValueError):
        return None


def ole_to_datetime(value: float) -> datetime | None:
    """Convert an OLE Automation date (days since 1899-12-30) to UTC datetime."""
    if not math.isfinite(value) or value == 0:
        return None
    try:
        # OLE dates: fractional part is time of day; negative dates count backwards
        # but the fraction is still positive.
        days = math.floor(value) if value >= 0 else math.ceil(value)
        frac = abs(value - days)
        return _OLE_EPOCH + timedelta(days=days) + timedelta(days=frac)
    except (OverflowError, ValueError):
        return None


def cocoa_to_datetime(value: float, unit: str = "s") -> datetime | None:
    """Apple Cocoa / Core Data time: seconds (or ns) since 2001-01-01 UTC."""
    if value == 0:
        return None
    divisor = {"s": 1, "ms": 1_000, "us": 1_000_000, "ns": 1_000_000_000}[unit]
    try:
        return _COCOA_EPOCH + timedelta(seconds=value / divisor)
    except (OverflowError, ValueError):
        return None


#: Timestamp encodings that profiles can attach to columns (``ts:<kind>`` pseudo-types).
TIMESTAMP_KINDS = {
    "filetime": "Windows FILETIME (100ns since 1601)",
    "ole": "OLE Automation date (days since 1899-12-30)",
    "ese": "ESE DateTime (OLE or FILETIME)",
    "unix": "Unix seconds",
    "unix_ms": "Unix milliseconds",
    "unix_us": "Unix microseconds (PRTime)",
    "unix_ns": "Unix nanoseconds",
    "webkit": "WebKit/Chrome microseconds since 1601",
    "cocoa": "Cocoa seconds since 2001",
    "cocoa_ns": "Cocoa nanoseconds since 2001",
    "mac_hfs": "HFS+ seconds since 1904",
}


def decode_timestamp_kind(value: Any, kind: str) -> datetime | None:
    """Decode ``value`` using a known encoding; None when not decodable/implausible."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, str):
        try:
            value = float(value) if "." in value else int(value)
        except ValueError:
            return None
    if not isinstance(value, int | float) or value == 0:
        return None
    dt: datetime | None
    if kind == "filetime":
        dt = filetime_to_datetime(int(value))
    elif kind == "ole":
        dt = ole_to_datetime(float(value))
    elif kind == "ese":
        d = decode_ese_datetime(value)
        dt = d if isinstance(d, datetime) else None
    elif kind == "unix":
        dt = unix_to_datetime(value, "s")
    elif kind == "unix_ms":
        dt = unix_to_datetime(value, "ms")
    elif kind in ("unix_us", "prtime"):
        dt = unix_to_datetime(value, "us")
    elif kind == "unix_ns":
        dt = unix_to_datetime(value, "ns")
    elif kind == "webkit":
        dt = _safe_add(_FILETIME_EPOCH, microseconds=value)
    elif kind == "cocoa":
        # Some Apple stores switched from seconds to nanoseconds (iOS 11 sms.db); pick the plausible one.
        dt = cocoa_to_datetime(value, "s")
        if not _plausible(dt):
            dt = cocoa_to_datetime(value, "ns")
    elif kind == "cocoa_ns":
        dt = cocoa_to_datetime(value, "ns")
        if not _plausible(dt):
            dt = cocoa_to_datetime(value, "s")
    elif kind == "mac_hfs":
        dt = _safe_add(_MAC_EPOCH, seconds=value)
    else:
        return None
    return dt if _plausible(dt) else None


#: Value ranges (roughly 1995 .. 2100) used when *guessing* an encoding from raw numbers.
_KIND_RANGES: tuple[tuple[str, float, float], ...] = (
    ("filetime", 1.24e17, 1.58e17),
    ("webkit", 1.24e16, 1.58e16),
    ("unix_ns", 8.0e17, 4.1e18),
    ("cocoa_ns", 3.0e16, 3.1e18),
    ("unix_us", 8.0e14, 4.1e15),
    ("unix_ms", 8.0e11, 4.1e12),
    ("unix", 8.0e8, 4.1e9),
    ("cocoa", 3.0e7, 3.1e9),
    ("ole", 34700.0, 73000.0),
)


def guess_timestamp_kind(values: list[Any]) -> str | None:
    """Guess which encoding a column of numbers uses (>= 90% of samples must fall in the kind's range)."""
    nums = [v for v in values if isinstance(v, int | float) and not isinstance(v, bool) and v]
    if len(nums) < 3:
        return None
    best, best_hits = None, 0
    for kind, lo, hi in _KIND_RANGES:
        if kind == "ole" and not any(isinstance(v, float) for v in nums):
            continue  # integer columns are never OLE dates
        hits = sum(1 for v in nums if lo <= v <= hi)
        if hits > best_hits:
            best, best_hits = kind, hits
    if best and best_hits >= max(3, int(0.9 * len(nums))):
        return best
    return None


def unix_to_datetime(value: float, unit: str = "s") -> datetime | None:
    divisor = {"s": 1, "ms": 1_000, "us": 1_000_000, "ns": 1_000_000_000}[unit]
    try:
        return _UNIX_EPOCH + timedelta(seconds=value / divisor)
    except (OverflowError, ValueError):
        return None


def _safe_add(base: datetime, **delta: float) -> datetime | None:
    try:
        return base + timedelta(**delta)
    except (OverflowError, ValueError):
        return None


def _plausible(dt: datetime | None) -> bool:
    return dt is not None and _MIN_PLAUSIBLE <= dt <= _MAX_PLAUSIBLE


def int64_bits_to_double(value: int) -> float:
    """Reinterpret the bit pattern of a signed/unsigned 64-bit integer as a double."""
    return struct.unpack("<d", struct.pack("<q", value if value < 2**63 else value - 2**64))[0]


def decode_ese_datetime(value: int | float | None) -> datetime | int | float | None:
    """Best-effort decode of a JET ``DateTime`` column.

    ``dissect.esedb`` returns the raw 8 bytes as an int64.  Most databases
    store an OLE date there; if reinterpreting the bits gives a plausible
    date we return that, otherwise we try FILETIME, otherwise the raw value.
    """
    if value is None or value == 0:
        return None
    if isinstance(value, float):
        dt = ole_to_datetime(value)
        return dt if _plausible(dt) else value
    dt = ole_to_datetime(int64_bits_to_double(value))
    if _plausible(dt):
        return dt
    dt = filetime_to_datetime(value)
    if _plausible(dt):
        return dt
    return value


def interpret_timestamp(value: int | float | str | bytes) -> dict[str, Any]:
    """Return every plausible interpretation of a numeric timestamp.

    Useful for analysts and AI agents looking at an unknown 64-bit value.
    Each key maps to an ISO-8601 string or ``None`` if that reading is
    outside the plausible 1980-2100 window.
    """
    raw: int | float
    if isinstance(value, bytes):
        if len(value) == 8:
            raw = struct.unpack("<Q", value)[0]
        elif len(value) == 4:
            raw = struct.unpack("<I", value)[0]
        else:
            raise ValueError("bytes must be 4 or 8 bytes long")
    elif isinstance(value, str):
        text = value.strip()
        raw = int(text, 16) if text.lower().startswith("0x") else (float(text) if "." in text else int(text))
    else:
        raw = value

    def iso(dt: datetime | None) -> str | None:
        return dt.isoformat() if dt is not None and _plausible(dt) else None

    out: dict[str, Any] = {"raw": raw}
    if isinstance(raw, int):
        out["filetime"] = iso(filetime_to_datetime(raw))
        out["ole_automation_bits"] = iso(ole_to_datetime(int64_bits_to_double(raw))) if raw > 0 else None
        out["unix_seconds"] = iso(unix_to_datetime(raw, "s"))
        out["unix_milliseconds"] = iso(unix_to_datetime(raw, "ms"))
        out["unix_microseconds"] = iso(unix_to_datetime(raw, "us"))
        out["unix_nanoseconds"] = iso(unix_to_datetime(raw, "ns"))
        out["webkit_chrome"] = iso(_safe_add(_FILETIME_EPOCH, microseconds=raw)) if raw > 0 else None
        out["cocoa_seconds"] = iso(cocoa_to_datetime(raw, "s")) if raw > 0 else None
        out["cocoa_nanoseconds"] = iso(cocoa_to_datetime(raw, "ns")) if raw > 0 else None
        out["mac_hfs"] = iso(_safe_add(_MAC_EPOCH, seconds=raw)) if 0 < raw < 2**32 else None
    else:
        out["ole_automation"] = iso(ole_to_datetime(raw))
        out["unix_seconds"] = iso(unix_to_datetime(raw, "s"))
        out["cocoa_seconds"] = iso(cocoa_to_datetime(raw, "s")) if raw > 0 else None
    out["best_guess"] = next((k for k, v in out.items() if k != "raw" and v), None)
    return out


def logtime_to_datetime(logtime: Any) -> datetime | None:
    """Convert a dissect ``LOGTIME`` structure from the file header into a datetime."""
    try:
        year = int(logtime.bYear)
        if year == 0:
            return None
        ms = int(logtime.bMillisecondsLow) | (int(logtime.bMillisecondsHigh) << 7)
        return datetime(
            1900 + year,
            int(logtime.bMonth),
            int(logtime.bDay),
            int(logtime.bHours),
            int(logtime.bMinutes),
            int(logtime.bSeconds),
            min(ms, 999) * 1000,
            tzinfo=UTC,
        )
    except (AttributeError, ValueError, TypeError):
        return None


# --------------------------------------------------------------------------- #
# Binary data
# --------------------------------------------------------------------------- #
def looks_like_utf16le(data: bytes) -> bool:
    """Heuristic: even length, every odd byte is NUL and the text is mostly printable."""
    if len(data) < 2 or len(data) % 2:
        return False
    sample = data[:512]
    if any(sample[i] for i in range(1, len(sample), 2)):
        return False
    try:
        text = sample.decode("utf-16-le")
    except UnicodeDecodeError:
        return False
    text = text.rstrip("\x00")
    return bool(text) and _PRINTABLE_RE.match(text) is not None


def looks_like_utf8(data: bytes) -> bool:
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return False
    text = text.rstrip("\x00")
    return bool(text) and _PRINTABLE_RE.match(text) is not None


def sid_from_bytes(data: bytes) -> str | None:
    """Decode a binary Windows SID (``S-1-5-21-...``)."""
    if len(data) < 8 or data[0] != 1:
        return None
    sub_count = data[1]
    if len(data) != 8 + 4 * sub_count:
        return None
    authority = int.from_bytes(data[2:8], "big")
    subs = struct.unpack_from(f"<{sub_count}I", data, 8)
    return "S-1-" + "-".join(str(x) for x in (authority, *subs))


def hexdump(data: bytes, width: int = 16, max_bytes: int | None = None) -> str:
    """Classic offset / hex / ASCII dump."""
    lines: list[str] = []
    chunk = data if max_bytes is None else data[:max_bytes]
    for offset in range(0, len(chunk), width):
        row = chunk[offset : offset + width]
        hex_part = " ".join(f"{b:02x}" for b in row).ljust(width * 3 - 1)
        ascii_part = "".join(chr(b) if 32 <= b < 127 else "." for b in row)
        lines.append(f"{offset:08x}  {hex_part}  {ascii_part}")
    if max_bytes is not None and len(data) > max_bytes:
        lines.append(f"... ({len(data) - max_bytes} more bytes)")
    return "\n".join(lines)


def decode_bytes(data: bytes, mode: BytesMode = "smart", max_length: int | None = None) -> Any:
    """Turn a bytes value into something JSON-friendly and human-readable."""
    if mode == "raw":
        return data
    if mode == "base64":
        return base64.b64encode(data).decode("ascii")
    if mode == "hex":
        text = binascii.hexlify(data).decode("ascii")
        return _truncate(text, max_length)
    # smart
    if not data:
        return ""
    if looks_like_utf16le(data):
        return _truncate(data.decode("utf-16-le").rstrip("\x00"), max_length)
    if len(data) >= 4 and looks_like_utf8(data):
        return _truncate(data.decode("utf-8").rstrip("\x00"), max_length)
    sid = sid_from_bytes(data)
    if sid:
        return sid
    if len(data) == 16:
        return f"{{{uuid.UUID(bytes_le=data)}}}"
    return _truncate("0x" + binascii.hexlify(data).decode("ascii"), max_length)


def _truncate(text: str, max_length: int | None) -> str:
    if max_length is not None and len(text) > max_length:
        return text[:max_length] + f"… [+{len(text) - max_length} chars]"
    return text


# --------------------------------------------------------------------------- #
# Generic value normalisation
# --------------------------------------------------------------------------- #
def normalize_value(
    value: Any,
    column_type: str | None = None,
    bytes_mode: BytesMode = "smart",
    max_length: int | None = None,
) -> Any:
    """Convert a value returned by dissect into a JSON-serialisable Python value.

    ``column_type`` is the JET column type name (``DateTime``, ``Binary`` ...)
    so that ``DateTime`` columns can be converted to real datetimes.
    """
    if value is None:
        return None
    if isinstance(value, list):
        return [normalize_value(v, column_type, bytes_mode, max_length) for v in value]
    if isinstance(value, bool | int | float):
        if column_type == "DateTime":
            decoded = decode_ese_datetime(value)
            return decoded.isoformat() if isinstance(decoded, datetime) else decoded
        if column_type and column_type.startswith("ts:"):
            dt = decode_timestamp_kind(value, column_type[3:])
            return dt.isoformat() if dt else value
        if isinstance(value, float) and not math.isfinite(value):
            return str(value)
        return value
    if isinstance(value, bytes | bytearray | memoryview):
        return decode_bytes(bytes(value), bytes_mode, max_length)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, uuid.UUID):
        return f"{{{value}}}"
    if isinstance(value, str):
        return _truncate(value, max_length)
    return _truncate(str(value), max_length)


def display_value(value: Any, column_type: str | None = None, max_length: int = 200) -> str:
    """Single-line string for table cells."""
    norm = normalize_value(value, column_type, "smart", max_length)
    if norm is None:
        return ""
    if isinstance(norm, list):
        return "[" + ", ".join(display_value(v, None, max_length) for v in norm) + "]"
    text = str(norm)
    return text.replace("\r", "\\r").replace("\n", "\\n")
