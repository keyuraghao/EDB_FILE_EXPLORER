"""Backend tests on synthetic files: SQLite, LevelDB, DBF, BSON, SQL dump, detection."""

from __future__ import annotations

import sqlite3
import struct
from pathlib import Path

import pytest

from edb_explorer.core import Database, InvalidDatabaseError, Session
from edb_explorer.core.backends import detect_kind, open_backend
from edb_explorer.core.backends.leveldb import LOG_BLOCK_SIZE, TABLE_MAGIC, decode_key, decode_value

WEBKIT = 13256512020013776  # 2021-01-30 20:27:00 UTC


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #
@pytest.fixture
def chrome_history(tmp_path: Path) -> Path:
    p = tmp_path / "History"
    c = sqlite3.connect(p)
    c.executescript(
        """
        CREATE TABLE urls(id INTEGER PRIMARY KEY, url LONGVARCHAR, title LONGVARCHAR, visit_count INTEGER DEFAULT 0,
                          typed_count INTEGER DEFAULT 0, last_visit_time INTEGER NOT NULL, hidden INTEGER DEFAULT 0);
        CREATE TABLE visits(id INTEGER PRIMARY KEY, url INTEGER NOT NULL, visit_time INTEGER NOT NULL, from_visit INTEGER,
                            transition INTEGER, segment_id INTEGER, visit_duration INTEGER);
        CREATE TABLE downloads(id INTEGER PRIMARY KEY, guid VARCHAR, current_path LONGVARCHAR, target_path LONGVARCHAR,
                               start_time INTEGER, received_bytes INTEGER, total_bytes INTEGER, state INTEGER, danger_type INTEGER,
                               end_time INTEGER, tab_url VARCHAR, referrer VARCHAR, mime_type VARCHAR, last_access_time INTEGER);
        CREATE TABLE keyword_search_terms(keyword_id INTEGER, url_id INTEGER, term LONGVARCHAR, normalized_term LONGVARCHAR);
        CREATE INDEX urls_url_index ON urls(url);
        """
    )
    for i in range(1, 21):
        c.execute(
            "INSERT INTO urls VALUES(?,?,?,?,?,?,0)",
            (i, f"https://site{i % 3}.example/p{i}", f"Page {i}", i, 0, WEBKIT + i * 60_000_000),
        )
        c.execute("INSERT INTO visits VALUES(?,?,?,?,?,?,?)", (i, i, WEBKIT + i * 60_000_000, 0, 805306368, 0, 5))
    c.execute(
        "INSERT INTO downloads VALUES(1,'g','/tmp/a.exe','/home/u/a.exe',?,10,10,1,0,?,'https://evil.example/a.exe','', 'application/x-msdownload',?)",
        (WEBKIT, WEBKIT + 1, WEBKIT + 1),
    )
    c.execute("INSERT INTO keyword_search_terms VALUES(1,2,'secret plans','secret plans')")
    c.execute("INSERT INTO visits VALUES(99,1,?,0,0,0,X'DEADBEEF')", (WEBKIT,))
    c.commit()
    c.close()
    return p


def _varint(n: int) -> bytes:
    out = bytearray()
    while True:
        b = n & 0x7F
        n >>= 7
        if n:
            out.append(b | 0x80)
        else:
            out.append(b)
            return bytes(out)


@pytest.fixture
def leveldb_dir(tmp_path: Path) -> Path:
    d = tmp_path / "Local Storage" / "leveldb"
    d.mkdir(parents=True)
    (d / "CURRENT").write_text("MANIFEST-000002\n")
    batch = struct.pack("<QI", 5, 3)
    batch += b"\x01" + _varint(2) + b"k1" + _varint(2) + b"v1"
    batch += b"\x00" + _varint(2) + b"k2"
    key = b"_https://example.com\x00\x01name"
    val = b"\x00" + "Keyur".encode("utf-16-le")
    batch += b"\x01" + _varint(len(key)) + key + _varint(len(val)) + val
    # one FULL record plus one FIRST/LAST split record spanning a block boundary
    rec = struct.pack("<IHB", 0, len(batch), 1) + batch
    big_val = b"x" * (LOG_BLOCK_SIZE + 100)
    batch2 = struct.pack("<QI", 9, 1) + b"\x01" + _varint(3) + b"big" + _varint(len(big_val)) + big_val
    pad = LOG_BLOCK_SIZE - len(rec) - 7
    first = struct.pack("<IHB", 0, pad, 2) + batch2[:pad]
    last = struct.pack("<IHB", 0, len(batch2) - pad, 4) + batch2[pad:]
    (d / "000003.log").write_bytes(rec + first + last)

    def entry(key: bytes, value: bytes) -> bytes:
        return _varint(0) + _varint(len(key)) + _varint(len(value)) + key + value

    import cramjam

    ikey1 = b"k1" + struct.pack("<Q", (1 << 8) | 1)
    ikey2 = b"k9" + struct.pack("<Q", (2 << 8) | 1)
    block = entry(ikey1, b"old") + entry(ikey2, b"z") + struct.pack("<I", 0) + struct.pack("<I", 1)
    comp = bytes(cramjam.snappy.compress_raw(block))
    data = comp + b"\x01" + b"\0\0\0\0"
    handle = _varint(0) + _varint(len(comp))
    index = entry(ikey2, handle) + struct.pack("<I", 0) + struct.pack("<I", 1)
    ioff = len(data)
    data += index + b"\x00" + b"\0\0\0\0"
    moff = len(data)
    meta = struct.pack("<I", 0) + struct.pack("<I", 1)
    data += meta + b"\x00" + b"\0\0\0\0"
    footer = _varint(moff) + _varint(len(meta)) + _varint(ioff) + _varint(len(index))
    (d / "000001.ldb").write_bytes(data + footer.ljust(40, b"\0") + TABLE_MAGIC)
    return d


@pytest.fixture
def dbf_file(tmp_path: Path) -> Path:
    # dBase III header + 2 fields + 2 records (one flagged deleted)
    fields = [(b"NAME", b"C", 10), (b"AGE", b"N", 3)]
    header_len = 32 + 32 * len(fields) + 1
    record_len = 1 + sum(f[2] for f in fields)
    head = bytearray(32)
    head[0] = 0x03
    head[1:4] = bytes([121, 2, 3])
    head[4:8] = (2).to_bytes(4, "little")
    head[8:10] = header_len.to_bytes(2, "little")
    head[10:12] = record_len.to_bytes(2, "little")
    body = bytearray()
    for name, ftype, length in fields:
        fd = bytearray(32)
        fd[0 : len(name)] = name
        fd[11] = ftype[0]
        fd[16] = length
        body += fd
    body += b"\r"
    recs = b" " + b"Alice     " + b" 30" + b"*" + b"Bob       " + b" 41" + b"\x1a"
    p = tmp_path / "people.dbf"
    p.write_bytes(bytes(head) + bytes(body) + recs)
    return p


@pytest.fixture
def bson_file(tmp_path: Path) -> Path:
    import bson

    p = tmp_path / "users.bson"
    p.write_bytes(bson.dumps({"_id": 1, "name": "a", "tags": ["x", "y"]}) + bson.dumps({"_id": 2, "extra": 3.5}))
    return p


@pytest.fixture
def sql_dump(tmp_path: Path) -> Path:
    p = tmp_path / "site.sql"
    p.write_text(
        """-- MySQL dump 10.13
/*!40101 SET NAMES utf8 */;
CREATE TABLE `wp_users` (
  `ID` bigint(20) unsigned NOT NULL AUTO_INCREMENT,
  `user_login` varchar(60) NOT NULL DEFAULT '',
  `user_email` varchar(100) NOT NULL DEFAULT '',
  `user_registered` datetime NOT NULL,
  PRIMARY KEY (`ID`),
  KEY `user_login_key` (`user_login`)
) ENGINE=InnoDB;
INSERT INTO `wp_users` VALUES (1,'admin','a@b.c','2021-01-01 10:00:00'),(2,'O\\'Brien','o@b.c','2021-02-02 11:00:00');
CREATE TABLE `wp_posts` (`ID` bigint, `post_date` datetime, `post_title` text, `post_status` varchar(20), `post_type` varchar(20), `post_author` bigint, `guid` varchar(255));
INSERT INTO `wp_posts` (`ID`,`post_date`,`post_title`) VALUES (1,'2021-03-03 12:00:00','Hello; world');
CREATE TABLE `wp_options` (`option_id` bigint, `option_name` varchar(191), `option_value` longtext);
CREATE TABLE `wp_comments` (`comment_ID` bigint, `comment_date` datetime, `comment_author` text, `comment_author_email` text, `comment_author_IP` text, `comment_approved` text, `comment_content` text);
COPY public.events (id, what) FROM stdin;
1\tlogin
2\t\\N
\\.
""",
        encoding="utf-8",
    )
    return p


# --------------------------------------------------------------------------- #
# Detection
# --------------------------------------------------------------------------- #
def test_detect_kinds(
    chrome_history: Path,
    leveldb_dir: Path,
    dbf_file: Path,
    bson_file: Path,
    sql_dump: Path,
    fake_edb: Path,
    not_edb: Path,
) -> None:
    assert detect_kind(chrome_history) == "sqlite"
    assert detect_kind(leveldb_dir) == "leveldb"
    assert detect_kind(leveldb_dir / "000003.log") == "leveldb"
    assert detect_kind(leveldb_dir / "CURRENT") == "leveldb"
    assert detect_kind(dbf_file) == "dbf"
    assert detect_kind(bson_file) == "bson"
    assert detect_kind(sql_dump) == "sqldump"
    assert detect_kind(fake_edb) == "ese"
    assert detect_kind(not_edb) is None
    with pytest.raises(InvalidDatabaseError):
        open_backend(not_edb)


def test_scan_finds_everything(
    chrome_history: Path, leveldb_dir: Path, dbf_file: Path, bson_file: Path, sql_dump: Path, tmp_path: Path
) -> None:
    found = Session().scan(tmp_path)
    names = {p.name for p in found}
    assert {"History", "leveldb", "people.dbf", "users.bson", "site.sql"} <= names
    assert "000003.log" not in names and "CURRENT" not in names


# --------------------------------------------------------------------------- #
# SQLite + profiles
# --------------------------------------------------------------------------- #
def test_sqlite_profile_hints_and_views(chrome_history: Path) -> None:
    with Database(chrome_history) as db:
        assert db.kind == "sqlite" and db.profile.id == "chromium_history"
        assert db.info.kind_name == "SQLite 3" and db.info.page_size == 4096
        t = db.table("urls")
        assert [c.type for c in t.columns][:2] == ["INTEGER", "TEXT"]
        assert t.indexes[0].is_primary and t.indexes[1].name == "urls_url_index"
        assert db.column_types("visits")["visit_time"] == "ts:webkit"
        rows = db.fetch("visits", limit=2).rows
        assert rows[0]["visit_time"].startswith("2021-01-30T20:28")
        raw = db.get_raw_record("visits", 20)
        assert raw and raw["visit_duration"] == b"\xde\xad\xbe\xef"
        assert db.count_records("urls") == 20
        assert db.native_sql_connection() is not None
        assert [v.id for v in db.profile.views] == ["history", "downloads", "searches", "top_sites"]


def test_sqlite_wal_sidecar_is_reported(tmp_path: Path) -> None:
    p = tmp_path / "wal.db"
    c = sqlite3.connect(p)
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("CREATE TABLE t(a INTEGER, b TEXT)")
    c.execute("INSERT INTO t VALUES (1, 'in wal')")
    c.commit()
    assert (tmp_path / "wal.db-wal").exists()
    with Database(p) as db:
        assert "wal.db-wal" in db.info.sidecars
        assert db.fetch("t").rows[0]["b"] == "in wal"
        assert db.native_sql_connection() is None  # WAL -> materialise instead of immutable connection
    c.close()


# --------------------------------------------------------------------------- #
# LevelDB
# --------------------------------------------------------------------------- #
def test_leveldb_records(leveldb_dir: Path) -> None:
    with Database(leveldb_dir) as db:
        assert db.kind == "leveldb" and db.profile.id == "chromium_localstorage"
        all_rows = db.fetch("all_records", limit=50, bytes_mode="raw").rows
        ops = [(r["sequence"], r["operation"], r["key_text"]) for r in all_rows]
        assert ops[:2] == [(1, "put", "k1"), (2, "put", "k9")]
        assert (6, "delete", "k2") in ops
        assert any(r["key_text"] == "https://example.com :: name" and r["value_text"] == "Keyur" for r in all_rows)
        big = next(r for r in all_rows if r["key_text"] == "big")
        assert len(big["value"]) == LOG_BLOCK_SIZE + 100  # split record reassembled
        live = {r["key_text"]: r["value_text"] for r in db.fetch("live", limit=50).rows}
        assert live["k1"] == "v1" and "k2" not in live and live["k9"] == "z"
        assert db.count_records("all_records") == 6
        assert {r["file"] for r in db.fetch("files").rows} >= {"CURRENT", "000003.log", "000001.ldb"}


def test_leveldb_from_file_inside(leveldb_dir: Path) -> None:
    db = Session().open(leveldb_dir / "000003.log")
    assert db.path == leveldb_dir.resolve()
    assert decode_key(b"plain") == "plain" and decode_value(b"\x01abc") == "abc"


# --------------------------------------------------------------------------- #
# DBF / BSON / SQL dump
# --------------------------------------------------------------------------- #
def test_dbf(dbf_file: Path) -> None:
    with Database(dbf_file) as db:
        assert db.kind == "dbf"
        tables = db.table_names()
        assert tables == ["people", "people (deleted records)"]
        rows = db.fetch("people").rows
        assert rows == [{"_row": 0, "NAME": "Alice", "AGE": 30}]
        deleted = db.fetch("people (deleted records)").rows
        assert deleted[0]["NAME"] == "Bob"
        assert db.info.header["version"].startswith("dBase III")


def test_bson(bson_file: Path) -> None:
    with Database(bson_file) as db:
        assert db.kind == "bson" and db.profile.id == "mongo_dump"
        assert db.table("users").column_names == ["_id", "name", "tags", "extra"]
        rows = db.fetch("users").rows
        assert rows[0]["tags"] == '["x", "y"]' and rows[1]["extra"] == 3.5
        assert db.count_records("users") == 2


def test_sql_dump(sql_dump: Path) -> None:
    with Database(sql_dump) as db:
        assert db.kind == "sqldump" and db.profile.id == "wordpress"
        users = db.fetch("wp_users").rows
        assert users[1]["user_login"] == "O'Brien"
        assert db.table("wp_users").columns[0].type == "INTEGER"
        posts = db.fetch("wp_posts").rows
        assert posts[0]["post_title"] == "Hello; world" and "post_status" not in posts[0]
        assert db.fetch("events").rows == [{"_row": 0, "id": "1", "what": "login"}, {"_row": 1, "id": "2"}]
        assert db.native_sql_connection() is not None


# --------------------------------------------------------------------------- #
# ESE decoding fast paths (must stay active and must stay equivalent to dissect)
# --------------------------------------------------------------------------- #
def test_ese_fast_paths_are_installed() -> None:
    """If dissect.esedb changes the internals we replace, the guards fall back to the stock code; make that visible."""
    from edb_explorer.core.backends import ese

    assert ese.FAST_PATHS == {"memo": True, "parse_value": True, "as_dict": True}


def test_ese_fast_numeric_parser_matches_cstruct() -> None:
    from dissect.esedb.c_esedb import COLUMN_TYPE_MAP, c_esedb

    from edb_explorer.core.backends.ese import _fast_numeric_parser

    samples = {
        c_esedb.uint8: b"\xfe",
        c_esedb.int16: b"\xff\x7f",
        c_esedb.int32: b"\x00\x00\x00\x80",
        c_esedb.uint32: b"\xff\xff\xff\xff",
        c_esedb.int64: b"\x01\x00\x00\x00\x00\x00\x00\x80",
        c_esedb.float: struct.pack("<f", 1.5),
        c_esedb.double: struct.pack("<d", -2.25),
    }
    for ctype, raw in samples.items():
        fast = _fast_numeric_parser(ctype)
        assert fast is not None
        for buf in (raw, raw + b"\xaa\xbb", memoryview(raw)):  # trailing bytes are ignored, like cstruct
            expected = ctype(buf)  # the stock metaclass path: bytes -> BytesIO -> unpack
            got = fast(buf)
            assert got == expected and type(got) is type(expected)
        with pytest.raises(EOFError):  # short input: dissect's own error
            fast(raw[:-1])
    # Every fixed-width numeric JET type now uses the fast parser; everything else is untouched.
    fast_names = {
        ct.parse.__name__ for ct in COLUMN_TYPE_MAP.values() if getattr(ct.parse, "__name__", "").startswith("fast_")
    }
    assert fast_names == {
        "fast_uint8",
        "fast_int16",
        "fast_int32",
        "fast_int64",
        "fast_float",
        "fast_double",
        "fast_uint32",
        "fast_uint16",
    }
    assert COLUMN_TYPE_MAP[10].parse.__name__ == "decode_text"  # Text


def test_ese_memo_caches_per_arguments() -> None:
    from edb_explorer.core.backends.ese import _memo

    calls: list[tuple] = []

    @_memo(2)
    def f(*args, **kwargs):
        calls.append((args, tuple(kwargs.items())))
        return len(calls)

    assert f(1) == 1 and f(1) == 1 and f(1, True) == 2 and f(1, is_derived=True) == 3
    assert f(1) == 4  # bounded: cache was cleared once it reached maxsize
    assert f.__wrapped__ is not None


# --------------------------------------------------------------------------- #
# SQL dump statement splitting
# --------------------------------------------------------------------------- #
MYSQLDUMP = (
    "-- MySQL dump 10.13  Distrib 8.0.36\n"
    "--\n"
    "-- Host: localhost    Database: shop\n"
    "-- ------------------------------------------------------\n"
    "/*!40101 SET NAMES utf8mb4 */;\n"
    "# a MySQL hash comment; with a semicolon\n"
    "CREATE TABLE `t` (`a` int, `b` text);\n"
    "/*/ not closed by its own opener */ INSERT INTO `t` VALUES (1,'x'),(2,'O\\'Brien -- not a comment'),"
    '(3,\'multi\\nline; still "one" value\'),(4,"it\'s"),(5,NULL);\n'
    "COPY public.ev (id, what) FROM stdin;\n1\tlogin\n2\t\\N\n\\.\n"
    "INSERT INTO t VALUES (6,'tail')"
)


def _split(text: str, size: int) -> list[str]:
    from edb_explorer.core.backends.sqldump import iter_statements

    return list(iter_statements(text[k : k + size] for k in range(0, len(text), size)))


def test_sql_dump_statements_are_independent_of_read_size() -> None:
    ref = _split(MYSQLDUMP, len(MYSQLDUMP))
    assert ref[:2] == [
        "CREATE TABLE `t` (`a` int, `b` text)",
        "INSERT INTO `t` VALUES (1,'x'),(2,'O\\'Brien -- not a comment'),(3,'multi\\nline; still \"one\" value'),(4,\"it's\"),(5,NULL)",
    ]
    assert (
        ref[2].startswith("COPY public.ev")
        and ref[3] == "\n1\tlogin\n2\t\\N\n\\.\n"
        and ref[4] == "INSERT INTO t VALUES (6,'tail')"
    )
    for size in (1, 2, 3, 5, 8, 13, 64):  # every boundary position, incl. inside \' , -- , /* , */ and \.
        assert _split(MYSQLDUMP, size) == ref, size


def test_sql_dump_real_mysqldump_header_loads(tmp_path: Path) -> None:
    p = tmp_path / "shop.sql"
    p.write_text(MYSQLDUMP, encoding="utf-8")
    with Database(p) as db:
        page = db.fetch("t", limit=10)
        assert [r.get("b") for r in page.rows] == [
            "x",
            "O'Brien -- not a comment",
            'multi\nline; still "one" value',
            "it's",
            None,
            "tail",
        ]
        assert db.count_records("ev") == 2


def test_sql_dump_parse_values_edge_cases() -> None:
    from edb_explorer.core.backends.sqldump import parse_values

    assert parse_values("(1,'a''b',\"c\\\"d\",X'4142',0x43,TRUE,NULL,-1.5e3,'esc\\t\\n\\\\')") == [
        [1, "a'b", 'c"d', b"AB", b"C", 1, None, -1500.0, "esc\t\n\\"]
    ]
    assert parse_values(" (1, 'x') , (2,'y')\n") == [[1, "x"], [2, "y"]]
    with pytest.raises(ValueError):
        parse_values("(1,'unterminated")
    with pytest.raises(ValueError):
        parse_values("1,2)")
