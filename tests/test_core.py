from __future__ import annotations

import json
from pathlib import Path

import pytest

from edb_explorer.core import (
    DatabaseNotFoundError,
    EdbDatabase,
    InvalidDatabaseError,
    PathNotAllowedError,
    Session,
    TableNotFoundError,
)
from edb_explorer.core.database import is_ese_file
from edb_explorer.core.export import export_database, export_table
from edb_explorer.core.profiles import detect_profile, is_system_table
from edb_explorer.core.search import FilterSpec, search_database


# ---- profiles ----------------------------------------------------------- #
def test_detect_profile() -> None:
    assert detect_profile(["datatable", "link_table", "sd_table"]).id == "ntds"
    assert detect_profile(["SruDbIdMapTable", "SruDbCheckpointTable"]).id == "srum"
    assert detect_profile(["Mailbox", "Folder", "Message"]).id == "exchange"
    assert detect_profile(["Containers", "LeakFiles"]).id == "webcache"
    assert detect_profile(["Foo", "Bar"]).id == "generic"
    p = detect_profile(["SruDbIdMapTable"], "SRUDB.dat")
    assert p.id == "srum"
    assert p.display_name("{973F5D5C-1D90-4944-BE8E-24B94231A174}") == "Network Data Usage"
    assert is_system_table("MSysObjects") and not is_system_table("datatable")


# ---- database ------------------------------------------------------------ #
def test_open_invalid_file(not_edb: Path) -> None:
    assert not is_ese_file(not_edb)
    with pytest.raises(InvalidDatabaseError):
        EdbDatabase(not_edb)
    with pytest.raises(InvalidDatabaseError):
        EdbDatabase(not_edb.parent / "missing.edb")


def test_database_info_and_schema(fake_edb: Path) -> None:
    assert is_ese_file(fake_edb)
    with EdbDatabase(fake_edb) as db:
        info = db.info
        assert info.profile_id == "srum"
        assert info.page_size == 4096
        assert info.state == "clean shutdown"
        assert info.created is not None and info.created.year == 2020
        assert info.windows_version == "10.0 build 19042 SP0"
        assert info.table_count == 4
        assert info.to_dict()["created"].startswith("2020-11-19")
        names = db.table_names(include_system=False)
        assert "MSysObjects" not in names and "SruDbIdMapTable" in names
        t = db.table("srum id map (apps / users)")  # display name, case-insensitive
        assert t.name == "SruDbIdMapTable"
        assert [c.name for c in t.columns] == ["IdType", "IdIndex", "IdBlob"]
        assert t.columns[2].storage == "tagged" and t.columns[2].is_binary
        assert t.indexes[0].is_primary and t.indexes[0].is_unique
        with pytest.raises(TableNotFoundError):
            db.table("nope")
        assert db.compute_sha256() == db.compute_sha256()
        assert db.info.sha256 is not None
    assert db.closed


def test_fetch_pagination_and_counts(fake_edb: Path) -> None:
    with EdbDatabase(fake_edb) as db:
        page = db.fetch("Network Data Usage", offset=0, limit=10)
        assert len(page.rows) == 10 and page.has_more and page.total is None
        assert page.rows[0]["_row"] == 0 and page.rows[0]["TimeStamp"].startswith("2021-01-24")
        page = db.fetch("Network Data Usage", offset=20, limit=10)
        assert len(page.rows) == 5 and not page.has_more and page.total == 25
        assert db.cached_count("Network Data Usage") == 25
        assert db.count_records("SruDbIdMapTable") == 4
        assert db.table("SruDbIdMapTable").record_count == 4
        assert db.fetch("SruDbCheckpointTable").rows == []


def test_value_decoding_and_columns(fake_edb: Path) -> None:
    with EdbDatabase(fake_edb) as db:
        rows = db.fetch("SruDbIdMapTable", limit=10).rows
        assert rows[1]["IdBlob"] == "S-1-5-18"
        assert rows[2]["IdBlob"] == "C:\\Windows\\svchost.exe"
        assert "IdBlob" not in rows[0]  # nulls dropped by default
        full = db.fetch("SruDbIdMapTable", limit=1, include_nulls=True).rows[0]
        assert full["IdBlob"] is None
        hexed = db.fetch("SruDbIdMapTable", limit=2, bytes_mode="hex").rows[1]["IdBlob"]
        assert hexed == "010100000000000512000000"
        sub = db.fetch("Network Data Usage", limit=1, columns=["AppId", "UserId"]).rows[0]
        assert set(sub) == {"_row", "AppId", "UserId"}
        with pytest.raises(TableNotFoundError):
            db.fetch("Network Data Usage", columns=["Nope"])
        rec = db.get_record("SruDbIdMapTable", 2)
        assert rec and rec["_row"] == 2
        assert db.get_record("SruDbIdMapTable", 99) is None
        raw = db.get_raw_record("SruDbIdMapTable", 1)
        assert raw and isinstance(raw["IdBlob"], bytes)


def test_filters(fake_edb: Path) -> None:
    with EdbDatabase(fake_edb) as db:
        types = db.column_types("SruDbIdMapTable")
        f = FilterSpec(text="svchost").compile(types)
        assert [r["_row"] for r in db.fetch("SruDbIdMapTable", row_filter=f).rows] == [2]
        f = FilterSpec(text=r"^S-1-5-\d+$", regex=True).compile(types)
        assert [r["_row"] for r in db.fetch("SruDbIdMapTable", row_filter=f).rows] == [1]
        f = FilterSpec(equals={"IdType": 3}).compile(types)
        assert len(db.fetch("SruDbIdMapTable", row_filter=f).rows) == 1
        f = FilterSpec(columns={"IdBlob": "EXE"}).compile(types)
        assert len(db.fetch("SruDbIdMapTable", row_filter=f).rows) == 2
        f = FilterSpec(columns={"IdBlob": "EXE"}, case_sensitive=True).compile(types)
        assert len(db.fetch("SruDbIdMapTable", row_filter=f).rows) == 0
        assert FilterSpec().compile(types) is None
        # paging through matches
        f = FilterSpec(text="exe").compile(types)
        page = db.fetch("SruDbIdMapTable", offset=1, limit=5, row_filter=f)
        assert [r["_row"] for r in page.rows] == [3] and not page.has_more


# ---- session ------------------------------------------------------------ #
def test_session_ids_and_lookup(fake_edb: Path, tmp_path: Path) -> None:
    s = Session()
    db = s.open(fake_edb)
    assert db.id == "srudb"
    assert s.open(fake_edb) is db  # same file -> same handle
    assert s.get("SRUDB.dat") is db and s.get(str(fake_edb)) is db and s.get("srudb") is db
    assert "srudb" in s and len(s) == 1
    copy = tmp_path / "sub" / "SRUDB.dat"
    copy.parent.mkdir()
    copy.write_bytes(fake_edb.read_bytes())
    db2 = s.open(copy)
    assert db2.id == "srudb-2"
    with pytest.raises(DatabaseNotFoundError):
        s.get("missing")
    s.close("srudb-2")
    assert len(s) == 1 and db2.closed
    s.close_all()
    assert len(s) == 0


def test_session_allowlist(fake_edb: Path, tmp_path: Path) -> None:
    s = Session(allowed_roots=[tmp_path / "elsewhere"])
    with pytest.raises(PathNotAllowedError):
        s.open(fake_edb)
    s = Session(allowed_roots=[tmp_path])
    assert s.open(fake_edb).id == "srudb"


def test_session_scan(fake_edb: Path, not_edb: Path, tmp_path: Path) -> None:
    (tmp_path / "nested").mkdir()
    (tmp_path / "nested" / "weird.bin").write_bytes(fake_edb.read_bytes())
    found = Session().scan(tmp_path)
    assert {p.name for p in found} == {"SRUDB.dat", "weird.bin"}
    assert [p.name for p in Session().scan(tmp_path, recursive=False)] == ["SRUDB.dat"]
    with pytest.raises(FileNotFoundError):
        Session().scan(tmp_path / "nope")


# ---- search ------------------------------------------------------------- #
def test_search(session: Session) -> None:
    db = session.get("srudb")
    hits = list(search_database(db, "svchost"))
    assert len(hits) == 1 and hits[0].table == "SruDbIdMapTable" and hits[0].column == "IdBlob"
    assert len(list(search_database(db, "S-1-5", regex=False))) == 1
    assert len(list(search_database(db, "2021-01-24", limit=3))) == 3
    assert list(search_database(db, "MSysObjects")) == []
    assert len(list(search_database(db, "MSysObjects", include_system=True))) == 1
    assert list(search_database(db, "exe", columns=["IdIndex"])) == []
    stopped = list(search_database(db, "2021", should_stop=lambda: True))
    assert stopped == []


# ---- export ------------------------------------------------------------- #
def test_export_formats(session: Session, tmp_path: Path) -> None:
    db = session.get("srudb")
    csv_path = tmp_path / "out" / "map.csv"
    assert export_table(db, "SruDbIdMapTable", csv_path, "csv") == 4
    lines = csv_path.read_text(encoding="utf-8").splitlines()
    assert lines[0] == "_row,IdType,IdIndex,IdBlob" and "S-1-5-18" in lines[2]

    json_path = tmp_path / "map.json"
    assert export_table(db, "SruDbIdMapTable", json_path, "json", limit=2) == 2
    data = json.loads(json_path.read_text(encoding="utf-8"))
    assert len(data) == 2 and data[1]["IdBlob"] == "S-1-5-18" and "IdBlob" not in data[0]

    jsonl_path = tmp_path / "net.jsonl"
    assert export_table(db, "Network Data Usage", jsonl_path, "jsonl", columns=["AppId"]) == 25
    first = json.loads(jsonl_path.read_text(encoding="utf-8").splitlines()[0])
    assert first == {"_row": 0, "AppId": 11}

    results = export_database(db, tmp_path / "all", "csv")
    assert set(results) == {"SruDbIdMapTable", "SruDbCheckpointTable", "{973F5D5C-1D90-4944-BE8E-24B94231A174}"}
    assert (tmp_path / "all" / "{973F5D5C-1D90-4944-BE8E-24B94231A174}.csv").exists()
    results = export_database(db, tmp_path / "sys", "jsonl", include_system=True, tables=["MSysObjects"])
    assert results == {"MSysObjects": 1}


def test_export_progress_cancel(session: Session, tmp_path: Path) -> None:
    db = session.get("srudb")
    calls: list[int] = []

    def progress(n: int) -> bool:
        calls.append(n)
        return False

    # progress fires every 500 rows; with 25 rows it never fires, export completes
    assert export_table(db, "Network Data Usage", tmp_path / "x.csv", progress=progress) == 25
    assert calls == []
