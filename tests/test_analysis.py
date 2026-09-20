from __future__ import annotations

import json
from pathlib import Path

import pytest

from edb_explorer.core import Database, Session
from edb_explorer.core.analysis import (
    build_timeline,
    column_statistics,
    database_summary,
    detect_timestamp_columns,
)
from edb_explorer.core.sqlworkspace import SqlError, SqlWorkspace
from edb_explorer.core.values import decode_timestamp_kind, guess_timestamp_kind
from tests.test_backends import chrome_history  # noqa: F401 - fixture re-export


def test_guess_kinds() -> None:
    assert guess_timestamp_kind([13256512020013776, 13256512020013777, 13256512020013778]) == "webkit"
    assert guess_timestamp_kind([1611500000, 1611500001, 1611500002]) == "unix"
    assert guess_timestamp_kind([1611500000123, 1611500001123, 1611500002123]) == "unix_ms"
    assert guess_timestamp_kind([132565120200137766] * 3) == "filetime"
    assert guess_timestamp_kind([600000000, 600000100, 600000200]) == "cocoa"
    assert guess_timestamp_kind([1, 2, 3]) is None
    assert decode_timestamp_kind(600000000000000000, "cocoa_ns").year == 2020
    assert decode_timestamp_kind(0, "unix") is None


def test_detect_timestamps_and_stats(chrome_history: Path) -> None:  # noqa: F811
    with Database(chrome_history) as db:
        assert detect_timestamp_columns(db, "visits") == {"visit_time": "webkit"}
        assert detect_timestamp_columns(db, "keyword_search_terms") == {}
        stats, n = column_statistics(db, "urls")
        assert n == 20
        by = {s.name: s for s in stats}
        assert by["url"].distinct == 20 and by["url"].nulls == 0 and by["url"].max_length > 10
        assert by["last_visit_time"].timestamp_kind == "webkit"
        assert by["last_visit_time"].timestamp_min.startswith("2021-01-30")
        assert by["visit_count"].mean == 10.5 and by["visit_count"].to_dict()["min"] == "1"
        assert by["hidden"].top_values == [("0", 20)]


def test_timeline_and_summary(chrome_history: Path, fake_edb: Path) -> None:  # noqa: F811
    s = Session()
    a = s.open(chrome_history)
    b = s.open(fake_edb)
    events, truncated = build_timeline([a, b])
    assert not truncated
    assert any(e.database == "history" and e.table == "visits" for e in events)
    assert any(e.database == "srudb" and e.kind == "ese" for e in events)
    assert events == sorted(events, key=lambda e: e.timestamp)
    limited, truncated = build_timeline([a], limit=5)
    assert len(limited) == 5 and truncated
    summary = database_summary(a)
    assert summary["profile"].startswith("Chromium") and summary["tables_with_timestamps"] == 3
    assert summary["sampled_time_range"][0].startswith("2021-01-30")
    json.dumps(summary)


def test_sql_workspace(chrome_history: Path, fake_edb: Path) -> None:  # noqa: F811
    s = Session()
    a = s.open(chrome_history)
    b = s.open(fake_edb)
    ws = SqlWorkspace()
    try:
        res = ws.query_database(a, "SELECT COUNT(*) AS n FROM urls")
        assert res.rows == [(20,)] and res.columns == ["n"]
        res = ws.run_view(a, "searches")
        assert res.rows[0][1] == "secret plans"
        res = ws.run_view(a, "Downloads")
        assert res.columns[0] == "start_time" and res.rows[0][0].startswith("2021-01-30")
        # cross-database join between a SQLite file and an ESE file
        res = ws.query_database(b, 'SELECT (SELECT COUNT(*) FROM "history"."urls") + COUNT(*) FROM "SruDbIdMapTable"')
        assert res.rows == [(24,)]
        # decoded values inside materialised tables
        res = ws.query_database(b, "SELECT IdBlob FROM SruDbIdMapTable WHERE IdIndex = 2")
        assert res.rows == [("S-1-5-18",)]
        res = ws.query(f"SELECT * FROM {ws.ensure_table(a, 'visits')} LIMIT 3", limit=2)
        assert len(res.rows) == 2 and res.truncated
        with pytest.raises(SqlError):
            ws.query("DELETE FROM history.urls")
        with pytest.raises(SqlError):
            ws.run_view(a, "nope")
        assert ws.attached() == {"history": "history", "srudb": "srudb"}
        ws.detach("history")
        assert "history" not in ws.attached()
    finally:
        ws.close()
