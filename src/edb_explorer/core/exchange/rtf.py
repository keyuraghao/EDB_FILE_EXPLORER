"""Compressed RTF (LZFu, MS-OXRTFCP) decompression."""

from __future__ import annotations

import struct
import zlib

_INIT_DICT = (
    b"{\\rtf1\\ansi\\mac\\deff0\\deftab720{\\fonttbl;}{\\f0\\fnil \\froman \\fswiss \\fmodern \\fscript "
    b"\\fdecor MS Sans SerifSymbolArialTimes New RomanCourier{\\colortbl\\red0\\green0\\blue0\r\n\\par "
    b"\\pard\\plain\\f0\\fs20\\b\\i\\u\\tab\\tx"
)
_DICT_SIZE = 4096


def decompress_rtf(data: bytes) -> bytes:
    """Decompress a PR_RTF_COMPRESSED value. Returns raw RTF bytes (or the input if it isn't compressed RTF)."""
    if len(data) < 16:
        return data
    comp_size, raw_size, magic, _crc = struct.unpack_from("<IIII", data, 0)
    if magic == 0x414C454D:  # "MELA" - uncompressed
        return data[16 : 16 + raw_size]
    if magic != 0x75465A4C:  # "LZFu"
        return data
    src = data[16 : 4 + comp_size]
    if zlib.crc32(src) & 0xFFFFFFFF != _crc:
        pass  # keep going - forensic data may be damaged; output what we can
    buf = bytearray(_DICT_SIZE)
    buf[: len(_INIT_DICT)] = _INIT_DICT
    wp = len(_INIT_DICT)
    out = bytearray()
    pos = 0
    while pos < len(src) and len(out) < raw_size:
        flags = src[pos]
        pos += 1
        for bit in range(8):
            if pos >= len(src) or len(out) >= raw_size:
                break
            if flags & (1 << bit):
                if pos + 1 >= len(src):
                    break
                ref = (src[pos] << 8) | src[pos + 1]
                pos += 2
                offset, length = ref >> 4, (ref & 0x0F) + 2
                if offset == wp:
                    return bytes(out)
                for i in range(length):
                    c = buf[(offset + i) % _DICT_SIZE]
                    out.append(c)
                    buf[wp] = c
                    wp = (wp + 1) % _DICT_SIZE
            else:
                c = src[pos]
                pos += 1
                out.append(c)
                buf[wp] = c
                wp = (wp + 1) % _DICT_SIZE
    return bytes(out)


def rtf_to_text(rtf: bytes | str) -> str:
    """Very small RTF-to-text conversion for previews (control words stripped, groups flattened)."""
    text = rtf.decode("latin-1", "replace") if isinstance(rtf, bytes) else rtf
    out: list[str] = []
    i, n = 0, len(text)
    skip_depth = 0
    depth = 0
    while i < n:
        c = text[i]
        if c == "{":
            depth += 1
            i += 1
        elif c == "}":
            if skip_depth and depth == skip_depth:
                skip_depth = 0
            depth -= 1
            i += 1
        elif c == "\\":
            j = i + 1
            if j < n and text[j] in "\\{}":
                if not skip_depth:
                    out.append(text[j])
                i = j + 1
                continue
            if j < n and text[j] == "'":
                try:
                    if not skip_depth:
                        out.append(bytes([int(text[j + 1 : j + 3], 16)]).decode("cp1252", "replace"))
                except ValueError:
                    pass
                i = j + 3
                continue
            k = j
            while k < n and text[k].isalpha():
                k += 1
            word = text[j:k]
            m = k
            while m < n and (text[m].isdigit() or text[m] == "-"):
                m += 1
            param = text[k:m]
            if m < n and text[m] == " ":
                m += 1
            if (
                word
                in (
                    "fonttbl",
                    "colortbl",
                    "stylesheet",
                    "info",
                    "pict",
                    "object",
                    "htmltag",
                    "mhtmltag",
                    "themedata",
                    "datastore",
                    "generator",
                )
                and not skip_depth
            ):
                skip_depth = depth
            elif not skip_depth:
                if word in ("par", "line"):
                    out.append("\n")
                elif word == "tab":
                    out.append("\t")
                elif word == "u" and param:
                    try:
                        out.append(chr(int(param) & 0xFFFF))
                    except ValueError:
                        pass
                    if m < n:
                        m += 1  # skip the substitute character
            i = m
        else:
            if not skip_depth:
                out.append(c)
            i += 1
    return "".join(out).strip()
