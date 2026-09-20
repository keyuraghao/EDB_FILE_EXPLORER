from __future__ import annotations

import struct
from datetime import datetime, timezone

from edb_explorer.core.values import (
    decode_bytes,
    decode_ese_datetime,
    display_value,
    filetime_to_datetime,
    hexdump,
    interpret_timestamp,
    looks_like_utf16le,
    normalize_value,
    ole_to_datetime,
    sid_from_bytes,
)


def test_filetime() -> None:
    dt = filetime_to_datetime(132565120200137766)
    assert dt == datetime(2021, 1, 30, 20, 27, 0, 13776, tzinfo=timezone.utc)
    assert filetime_to_datetime(0) is None


def test_ole_date() -> None:
    assert ole_to_datetime(44220.5) == datetime(2021, 1, 24, 12, 0, tzinfo=timezone.utc)
    assert ole_to_datetime(0) is None


def test_decode_ese_datetime_ole_bits() -> None:
    bits = struct.unpack("<q", struct.pack("<d", 44220.25))[0]
    assert decode_ese_datetime(bits) == datetime(2021, 1, 24, 6, 0, tzinfo=timezone.utc)


def test_decode_ese_datetime_filetime_fallback() -> None:
    assert decode_ese_datetime(132565120200137766).year == 2021
    assert decode_ese_datetime(0) is None
    assert decode_ese_datetime(12345) == 12345  # neither reading plausible -> raw


def test_interpret_timestamp_variants() -> None:
    info = interpret_timestamp("132565120200137766")
    assert info["filetime"].startswith("2021-01-30")
    assert info["best_guess"] == "filetime"
    assert interpret_timestamp("0x1D6FE2E6A8C2E80")["filetime"].startswith("2021-02-08")
    assert interpret_timestamp(1611500000)["unix_seconds"].startswith("2021-01-24")
    assert interpret_timestamp(44220.5)["ole_automation"].startswith("2021-01-24")
    eight = struct.pack("<Q", 132565120200137766)
    assert interpret_timestamp(eight)["filetime"].startswith("2021-01-30")


def test_sid() -> None:
    assert sid_from_bytes(bytes.fromhex("010100000000000512000000")) == "S-1-5-18"
    rid = bytes.fromhex("010500000000000515000000") + (1).to_bytes(4, "little") * 3 + (500).to_bytes(4, "little")
    assert sid_from_bytes(rid) == "S-1-5-21-1-1-1-500"
    assert sid_from_bytes(b"\x01\x01\x00") is None


def test_smart_bytes() -> None:
    assert looks_like_utf16le("hello".encode("utf-16-le"))
    assert decode_bytes("C:\\x".encode("utf-16-le")) == "C:\\x"
    assert decode_bytes(b"plain ascii text") == "plain ascii text"
    assert decode_bytes(bytes.fromhex("010100000000000512000000")) == "S-1-5-18"
    guid = decode_bytes(bytes(range(16)))
    assert guid.startswith("{") and guid.endswith("}") and len(guid) == 38
    assert decode_bytes(b"\xff\xfe\x00\x01\x02") == "0xfffe000102"
    assert decode_bytes(b"\x01\x02", "hex") == "0102"
    assert decode_bytes(b"\x01\x02", "base64") == "AQI="
    assert decode_bytes(b"\x01\x02", "raw") == b"\x01\x02"
    assert decode_bytes(b"", "smart") == ""


def test_truncation_marker() -> None:
    out = decode_bytes(b"A" * 100, "hex", max_length=10)
    assert out.startswith("41414141") and "[+190 chars]" in out


def test_normalize_and_display() -> None:
    assert normalize_value(None) is None
    assert normalize_value(True) is True
    assert normalize_value(b"\x01", "Binary", "hex") == "01"
    assert normalize_value([b"ab", b"cd"], "Binary", "hex") == ["6162", "6364"]
    assert normalize_value(float("inf")) == "inf"
    dt = normalize_value(struct.unpack("<q", struct.pack("<d", 44220.0))[0], "DateTime")
    assert dt == "2021-01-24T00:00:00+00:00"
    assert display_value("a\nb") == "a\\nb"
    assert display_value([1, "x"]) == "[1, x]"
    assert display_value(None) == ""


def test_hexdump() -> None:
    text = hexdump(b"hello world\x00\x01", max_bytes=8)
    assert text.splitlines()[0].startswith("00000000  68 65 6c 6c 6f 20 77 6f")
    assert "more bytes" in text


def _display_generic(value: object, column_type: str | None, max_length: int) -> str:
    """display_value without its fast paths - the reference the fast paths must reproduce."""
    norm = normalize_value(value, column_type, "smart", max_length)
    if norm is None:
        return ""
    if isinstance(norm, list):
        return "[" + ", ".join(_display_generic(v, None, max_length) for v in norm) + "]"
    return str(norm).replace("\r", "\\r").replace("\n", "\\n")


def test_display_value_fast_paths_match_generic() -> None:
    class Int32(int):  # stands in for dissect's cstruct int subclasses
        pass

    values: list[object] = [
        "plain",
        "",
        "x" * 500,
        "line\nbreak\r",
        "exactly" + "!" * 193,
        1,
        Int32(-7),
        True,
        0,
        44197.5,
        1_600_000_000,
        float("nan"),
        float("inf"),
        2**70,
        b"a\x00b\x00",
        [1, "a", b"\x01"],
        None,
    ]

    def outcome(fn: object, *args: object) -> object:
        try:
            return fn(*args)  # type: ignore[operator]
        except Exception as exc:  # e.g. a DateTime that does not fit in 64 bits raises on both routes
            return ("raised", type(exc), str(exc))

    for value in values:
        for ctype in (None, "Long", "DateTime", "ts:unix", "ts:ole", "Text"):
            for max_length in (5, 200, 100_000):
                fast = outcome(display_value, value, ctype, max_length)
                generic = outcome(_display_generic, value, ctype, max_length)
                assert fast == generic, (value, ctype, max_length)


def test_looks_like_utf16le_odd_byte_check() -> None:
    from edb_explorer.core.values import looks_like_utf16le

    assert looks_like_utf16le("hello".encode("utf-16-le"))
    assert not looks_like_utf16le(b"h\x00e\x01")  # a non-NUL high byte anywhere disqualifies it
    assert not looks_like_utf16le(b"h\x00e")  # odd length
    assert not looks_like_utf16le(b"\x00\x00")  # decodes to nothing printable
