"""SQL dump backend: mysqldump / pg_dump / sqlite .dump text files become a queryable SQLite database.

The dump is parsed statement by statement (quotes, escapes, comments and
PostgreSQL ``COPY ... FROM stdin`` blocks are handled) and loaded into a
temporary SQLite file, which then behaves like any other database here.
"""

from __future__ import annotations

import logging
import os
import re
import sqlite3
import tempfile
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from edb_explorer.core.backends.base import Backend, BackendInfo, BackendTable, sanitize_columns
from edb_explorer.core.exceptions import InvalidDatabaseError
from edb_explorer.core.models import ColumnInfo, IndexInfo

log = logging.getLogger(__name__)

_DUMP_HINTS = (
    b"-- MySQL dump",
    b"-- MariaDB dump",
    b"-- PostgreSQL database dump",
    b"PRAGMA foreign_keys=OFF;",
    b"CREATE TABLE",
    b"INSERT INTO",
    b"COPY ",
)
_CONSTRAINT_WORDS = {
    "PRIMARY",
    "KEY",
    "INDEX",
    "UNIQUE",
    "CONSTRAINT",
    "FOREIGN",
    "CHECK",
    "FULLTEXT",
    "SPATIAL",
    "EXCLUDE",
    "LIKE",
}
_IDENT = r'(?:`[^`]+`|"[^"]+"|\[[^\]]+\]|[A-Za-z_][\w$]*)'
_CREATE_RE = re.compile(
    rf"CREATE\s+(?:TEMP(?:ORARY)?\s+)?TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?((?:{_IDENT}\.)?{_IDENT})\s*\(", re.I | re.S
)
_INSERT_RE = re.compile(
    rf"INSERT\s+(?:IGNORE\s+)?INTO\s+((?:{_IDENT}\.)?{_IDENT})\s*(\([^)]*\))?\s*VALUES\s*", re.I | re.S
)
_COPY_RE = re.compile(rf"COPY\s+((?:{_IDENT}\.)?{_IDENT})\s*(\([^)]*\))?\s+FROM\s+stdin", re.I)
_ESCAPES = {"n": "\n", "t": "\t", "r": "\r", "0": "\x00", "b": "\b", "Z": "\x1a"}


def looks_like_sql_dump(head: bytes, name: str = "") -> bool:
    if name.lower().endswith(".sql"):
        return True
    return any(h in head for h in _DUMP_HINTS[:4]) or (b"CREATE TABLE" in head and b"INSERT INTO" in head)


def _unquote(ident: str) -> str:
    ident = ident.strip()
    if "." in ident and not (ident[0] in '`"[' and ident[-1] in '`"]'):
        ident = ident.split(".")[-1]
    if ident[:1] in ("`", '"', "[") and len(ident) > 1:
        return ident[1:-1]
    return ident


# --------------------------------------------------------------------------- #
# Statement splitting
# --------------------------------------------------------------------------- #
_STATEMENT_SPECIAL = re.compile(r"""['"`;#/-]""")  # characters that can change state outside a string literal
_STRING_SPECIAL = {"'": re.compile(r"[\\']"), '"': re.compile(r'[\\"]'), "`": re.compile("`")}
_TOKEN_STOP = re.compile(r"[(),]")


def iter_statements(text_iter: Iterator[str]) -> Iterator[str]:
    """Yield SQL statements (``;``-terminated) and COPY blocks from a stream of text chunks.

    Handles ``'...'`` / ``"..."`` literals with backslash escapes, backtick identifiers, ``--`` and MySQL
    ``#`` line comments, ``/* */`` block comments and PostgreSQL ``COPY ... FROM stdin`` data
    terminated by a ``\\.`` line.  Scanning jumps between significant characters rather than stepping
    through every character, and a token split across two chunks (``\\'``, ``--``, ``/*``, ``*/``) is
    carried over so results do not depend on the read size.
    """
    buf: list[str] = []
    quote: str | None = None
    line_comment = block_comment = copy_mode = False
    copy_line = ""  # partial COPY data line carried across chunks
    carry = ""  # trailing character whose meaning depends on the next chunk
    for chunk in text_iter:
        if carry:
            chunk = carry + chunk
            carry = ""
        i, n = 0, len(chunk)
        while i < n:
            if copy_mode:
                j = chunk.find("\n", i)
                if j == -1:
                    buf.append(chunk[i:])
                    copy_line += chunk[i:]
                    i = n
                    break
                buf.append(chunk[i : j + 1])
                line, copy_line = copy_line + chunk[i:j], ""
                i = j + 1
                if line.rstrip("\r") == "\\.":
                    yield "".join(buf)
                    buf = []
                    copy_mode = False
            elif line_comment:
                j = chunk.find("\n", i)
                if j == -1:
                    i = n
                else:
                    i = j + 1  # the newline itself is dropped with the comment
                    line_comment = False
            elif block_comment:
                j = chunk.find("*/", i)
                if j == -1:
                    if chunk.endswith("*"):
                        carry = "*"
                    i = n
                else:
                    i = j + 2
                    block_comment = False
            elif quote:
                m = _STRING_SPECIAL[quote].search(chunk, i)
                if m is None:
                    buf.append(chunk[i:])
                    i = n
                    break
                j = m.start()
                if j > i:
                    buf.append(chunk[i:j])
                if chunk[j] == "\\":
                    if j + 1 < n:
                        buf.append(chunk[j : j + 2])
                        i = j + 2
                    else:
                        carry = "\\"
                        i = n
                else:
                    buf.append(quote)
                    quote = None
                    i = j + 1
            else:
                m = _STATEMENT_SPECIAL.search(chunk, i)
                if m is None:
                    buf.append(chunk[i:])
                    i = n
                    break
                j = m.start()
                if j > i:
                    buf.append(chunk[i:j])
                c = chunk[j]
                if c in "'\"`":
                    buf.append(c)
                    quote = c
                    i = j + 1
                elif c == ";":
                    stmt = "".join(buf).strip()
                    buf = []
                    i = j + 1
                    if stmt:
                        yield stmt
                        if _COPY_RE.match(stmt):
                            copy_mode = True
                            copy_line = ""
                elif c == "#":
                    line_comment = True
                    i = j + 1
                elif j + 1 >= n:  # "-" or "/" as the last character: needs the next chunk to decide
                    carry = c
                    i = n
                elif c == "-" and chunk[j + 1] == "-":
                    line_comment = True
                    i = j + 2
                elif c == "/" and chunk[j + 1] == "*":
                    block_comment = True
                    i = j + 2
                else:
                    buf.append(c)
                    i = j + 1
    if carry and carry != "*":
        buf.append(carry)  # a lone trailing "-", "/" or "\\" is literal text
    tail = "".join(buf).strip()
    if tail:
        yield tail


# --------------------------------------------------------------------------- #
# Value tokenising
# --------------------------------------------------------------------------- #
def parse_values(text: str) -> list[list[Any]]:
    """Parse the ``(...),(...)`` part of an INSERT statement."""
    rows: list[list[Any]] = []
    i, n = 0, len(text)
    while i < n:
        while i < n and text[i] in " \t\r\n,":
            i += 1
        if i >= n:
            break
        if text[i] != "(":
            raise ValueError(f"unexpected {text[i]!r} in VALUES")
        i += 1
        row: list[Any] = []
        while True:
            while i < n and text[i] in " \t\r\n":
                i += 1
            if i >= n:
                raise ValueError("unterminated VALUES tuple")
            c = text[i]
            if c in ("'", '"'):
                i += 1
                out: list[str] = []
                scan = _STRING_SPECIAL[c].search
                while i < n:
                    m = scan(text, i)
                    if m is None:  # unterminated literal: take the rest, the caller reports the error
                        out.append(text[i:])
                        i = n
                        break
                    j = m.start()
                    if j > i:
                        out.append(text[i:j])
                    if text[j] == "\\" and j + 1 < n:
                        nxt = text[j + 1]
                        out.append(_ESCAPES.get(nxt, nxt))
                        i = j + 2
                    elif text[j] == "\\":
                        out.append("\\")
                        i = j + 1
                    elif j + 1 < n and text[j + 1] == c:  # doubled quote
                        out.append(c)
                        i = j + 2
                    else:
                        i = j + 1
                        break
                row.append("".join(out))
            elif text.startswith(("X'", "x'"), i):
                j = text.index("'", i + 2)
                row.append(bytes.fromhex(text[i + 2 : j]))
                i = j + 1
            else:
                j = i
                depth = 0
                while True:  # up to the next top-level "," or ")" (function calls such as POINT(1 2) may nest)
                    m = _TOKEN_STOP.search(text, j)
                    if m is None:
                        j = n
                        break
                    j = m.start()
                    ch = text[j]
                    if ch == "(":
                        depth += 1
                    elif depth:
                        depth -= ch == ")"
                    else:
                        break
                    j += 1
                tok = text[i:j].strip()
                i = j
                row.append(_literal(tok))
            while i < n and text[i] in " \t\r\n":
                i += 1
            if i < n and text[i] == ",":
                i += 1
                continue
            if i < n and text[i] == ")":
                i += 1
                break
            raise ValueError(f"unexpected token near {text[i : i + 20]!r}")
        rows.append(row)
    return rows


def _literal(tok: str) -> Any:
    up = tok.upper()
    if up in ("NULL", "DEFAULT"):
        return None
    if up == "TRUE":
        return 1
    if up == "FALSE":
        return 0
    if up.startswith("0X"):
        try:
            return bytes.fromhex(tok[2:])
        except ValueError:
            return tok
    try:
        return int(tok)
    except ValueError:
        pass
    try:
        return float(tok)
    except ValueError:
        return tok


def _split_top_level(body: str) -> list[str]:
    parts: list[str] = []
    depth = 0
    quote: str | None = None
    cur: list[str] = []
    for c in body:
        if quote:
            cur.append(c)
            if c == quote:
                quote = None
            continue
        if c in ("'", '"', "`"):
            quote = c
        elif c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
        if c == "," and depth == 0:
            parts.append("".join(cur))
            cur = []
        else:
            cur.append(c)
    if "".join(cur).strip():
        parts.append("".join(cur))
    return parts


def _create_columns(stmt: str) -> tuple[str, list[tuple[str, str]], list[str]] | None:
    m = _CREATE_RE.match(stmt)
    if not m:
        return None
    name = _unquote(m.group(1))
    body = stmt[m.end() :]
    depth = 1
    end = 0
    quote: str | None = None
    for i, c in enumerate(body):
        if quote:
            if c == quote:
                quote = None
            continue
        if c in ("'", '"', "`"):
            quote = c
        elif c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
            if depth == 0:
                end = i
                break
    cols: list[tuple[str, str]] = []
    keys: list[str] = []
    for part in _split_top_level(body[:end]):
        part = part.strip()
        if not part:
            continue
        first = re.match(rf"({_IDENT})\s*(.*)", part, re.S)
        if not first:
            continue
        ident = first.group(1)
        if ident.upper() in _CONSTRAINT_WORDS and ident[0] not in '`"[':
            keys.append(part.split("(")[0].strip())
            continue
        typ = (first.group(2).split()[0] if first.group(2).strip() else "").strip(",")
        cols.append((_unquote(ident), typ))
    return name, cols, keys


def _affinity(typ: str) -> str:
    t = typ.upper()
    if "INT" in t or t in ("SERIAL", "BIGSERIAL", "SMALLSERIAL", "YEAR", "BOOL", "BOOLEAN"):
        return "INTEGER"
    if any(k in t for k in ("CHAR", "TEXT", "CLOB", "ENUM", "SET", "JSON", "UUID", "DATE", "TIME", "XML", "INET")):
        return "TEXT"
    if any(k in t for k in ("BLOB", "BINARY", "BYTEA", "IMAGE")):
        return "BLOB"
    if any(k in t for k in ("REAL", "FLOA", "DOUB", "DEC", "NUM", "MONEY")):
        return "REAL"
    return ""


def load_dump(source: Path, target: sqlite3.Connection, progress: Any = None) -> dict[str, int]:
    """Load ``source`` dump into the ``target`` sqlite connection; returns {table: rows}."""
    counts: dict[str, int] = {}
    schemas: dict[str, list[str]] = {}
    pending_copy: tuple[str, list[str] | None] | None = None
    conn = target
    conn.execute("PRAGMA journal_mode=OFF")
    conn.execute("PRAGMA synchronous=OFF")

    def ensure_table(name: str, cols: list[str]) -> None:
        if name in schemas:
            return
        cols = sanitize_columns(cols)
        schemas[name] = cols
        conn.execute(f'CREATE TABLE IF NOT EXISTS "{name}" ({", ".join(f"[{c}]" for c in cols)})')

    def text_chunks() -> Iterator[str]:
        with open(source, encoding="utf-8", errors="replace", newline="") as fh:
            while True:
                chunk = fh.read(1 << 20)
                if not chunk:
                    return
                yield chunk

    for stmt in iter_statements(text_chunks()):
        try:
            if pending_copy is not None:
                name, cols = pending_copy
                pending_copy = None
                rows = []
                for line in stmt.splitlines():
                    if line == "\\." or not line:
                        continue
                    rows.append([_copy_field(f) for f in line.split("\t")])
                if rows:
                    ensure_table(name, cols or [f"column_{i + 1}" for i in range(len(rows[0]))])
                    _insert(conn, name, schemas[name], cols, rows)
                    counts[name] = counts.get(name, 0) + len(rows)
                continue
            created = _create_columns(stmt)
            if created:
                name, col_defs, _keys = created
                ensure_table(name, [c for c, _ in col_defs])
                typed = ", ".join(
                    f"[{c}] {_affinity(t)}".strip()
                    for c, t in zip(schemas[name], (t for _, t in col_defs), strict=False)
                )
                conn.execute(f'DROP TABLE IF EXISTS "{name}"')
                conn.execute(f'CREATE TABLE "{name}" ({typed})')
                counts.setdefault(name, 0)
                continue
            m = _INSERT_RE.match(stmt)
            if m:
                name = _unquote(m.group(1))
                cols = [_unquote(c) for c in m.group(2).strip("() ").split(",")] if m.group(2) else None
                rows = parse_values(stmt[m.end() :])
                if not rows:
                    continue
                ensure_table(name, cols or [f"column_{i + 1}" for i in range(len(rows[0]))])
                _insert(conn, name, schemas[name], cols, rows)
                counts[name] = counts.get(name, 0) + len(rows)
                if progress and not progress(name, counts[name]):
                    break
                continue
            m = _COPY_RE.match(stmt)
            if m:
                name = _unquote(m.group(1))
                cols = [_unquote(c) for c in m.group(2).strip("() ").split(",")] if m.group(2) else None
                pending_copy = (name, cols)
        except (sqlite3.Error, ValueError) as exc:
            log.debug("skipping statement (%s): %.80s", exc, stmt)
    conn.commit()
    return counts


def _copy_field(f: str) -> Any:
    if f == "\\N":
        return None
    return re.sub(r"\\(.)", lambda m: _ESCAPES.get(m.group(1), m.group(1)), f)


def _insert(
    conn: sqlite3.Connection, name: str, schema: list[str], cols: list[str] | None, rows: list[list[Any]]
) -> None:
    width = max(len(r) for r in rows)
    if cols is None and width > len(schema):
        for i in range(len(schema), width):
            col = f"column_{i + 1}"
            conn.execute(f'ALTER TABLE "{name}" ADD COLUMN [{col}]')
            schema.append(col)
    target_cols = sanitize_columns(cols) if cols else schema[:width]
    for c in target_cols:
        if c not in schema:
            conn.execute(f'ALTER TABLE "{name}" ADD COLUMN [{c}]')
            schema.append(c)
    placeholders = ", ".join("?" for _ in target_cols)
    col_sql = ", ".join(f"[{c}]" for c in target_cols)
    fixed = [tuple(_cell(v) for v in (r + [None] * (len(target_cols) - len(r)))[: len(target_cols)]) for r in rows]
    conn.executemany(f'INSERT INTO "{name}" ({col_sql}) VALUES ({placeholders})', fixed)


def _cell(v: Any) -> Any:
    if isinstance(v, bytes | bytearray | int | float | str) or v is None:
        return v
    return str(v)


class SqlDumpBackend(Backend):
    kind = "sqldump"
    kind_name = "SQL dump (MySQL / PostgreSQL / SQLite)"
    native_sql = True

    def __init__(self, path: Any) -> None:
        super().__init__(path)
        fd, self._tmp_name = tempfile.mkstemp(prefix="edb-sqldump-", suffix=".sqlite")
        os.close(fd)
        self._conn = sqlite3.connect(self._tmp_name, check_same_thread=False)
        try:
            self._counts = load_dump(self.path, self._conn)
        except Exception as exc:
            self.close()
            raise InvalidDatabaseError(f"{self.path.name}: cannot parse SQL dump ({exc})") from exc
        if not self._counts:
            self.close()
            raise InvalidDatabaseError(f"{self.path.name}: no CREATE TABLE / INSERT / COPY statements found")
        self._conn.execute("PRAGMA query_only = 1")
        self._tables: list[BackendTable] | None = None

    def close(self) -> None:
        try:
            self._conn.close()
        finally:
            Path(self._tmp_name).unlink(missing_ok=True)

    def tables(self) -> list[BackendTable]:
        if self._tables is None:
            out = []
            for (name,) in self._conn.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"):
                cols = []
                for cid, cname, ctype, _nn, _dflt, _pk in self._conn.execute(f'PRAGMA table_info("{name}")'):
                    aff = ctype or "ANY"
                    cols.append(
                        ColumnInfo(cid + 1, cname, aff, 0, "dynamic", None, "utf-8", aff == "TEXT", aff == "BLOB")
                    )
                idx = [
                    IndexInfo(name=r[1], is_primary=False, is_unique=bool(r[2]), columns=())
                    for r in self._conn.execute(f'PRAGMA index_list("{name}")')
                ]
                out.append(BackendTable(name=name, columns=cols, indexes=idx, record_count=self._counts.get(name)))
            self._tables = out
        return self._tables

    def iter_rows(self, table: str) -> Iterator[dict[str, Any]]:
        cur = self._conn.execute(f'SELECT * FROM "{table}"')
        names = [d[0] for d in cur.description]
        for row in cur:
            yield dict(zip(names, row, strict=False))

    def count(self, table: str) -> int | None:
        return self._conn.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]

    def sql_connection(self) -> sqlite3.Connection | None:
        conn = sqlite3.connect(f"file:{self._tmp_name}?mode=ro", uri=True, check_same_thread=False)
        conn.execute("PRAGMA query_only = 1")
        return conn

    def info(self) -> BackendInfo:
        with open(self.path, "rb") as fh:
            head = fh.read(4096)
        dialect = (
            "MySQL/MariaDB"
            if b"MySQL" in head or b"MariaDB" in head or b"`" in head
            else "PostgreSQL"
            if b"PostgreSQL" in head or b"COPY " in head
            else "SQLite"
            if b"PRAGMA" in head
            else "generic SQL"
        )
        first = head.decode("utf-8", "replace").splitlines()[:6]
        return BackendInfo(
            kind=self.kind,
            kind_name=self.kind_name,
            state=f"{dialect} dump, {sum(self._counts.values()):,} rows loaded",
            header={
                "dialect": dialect,
                "tables": len(self._counts),
                "rows": sum(self._counts.values()),
                "banner": [ln for ln in first if ln.strip()][:4],
            },
        )
