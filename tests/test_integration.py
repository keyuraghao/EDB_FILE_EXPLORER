"""Integration tests against a real ESE file.  Set EDB_EXPLORER_TEST_DB to enable."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from edb_explorer.core import EdbDatabase, Session
from edb_explorer.core.search import search_database

DB = os.environ.get("EDB_EXPLORER_TEST_DB")
pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not DB or not Path(DB).is_file(), reason="EDB_EXPLORER_TEST_DB not set"),
]


def test_real_open_and_iterate() -> None:
    with EdbDatabase(DB) as db:  # type: ignore[arg-type]
        assert db.info.page_size in (2048, 4096, 8192, 16384, 32768)
        assert db.info.table_count > 0
        tables = db.tables(include_system=False)
        assert tables
        first = tables[0]
        page = db.fetch(first.name, limit=5)
        assert page.columns == first.column_names
        for row in page.rows:
            assert "_row" in row
        n = db.count_records(first.name)
        assert n == page.total or page.has_more


def test_real_search_is_bounded() -> None:
    with Session().open(DB) as db:  # type: ignore[arg-type]
        hits = list(search_database(db, "e", limit=5))
        assert len(hits) <= 5


def test_real_fast_decoding_matches_stock_dissect() -> None:
    """Every record decoded through our fast paths must equal dissect's stock decoding (values, types and errors)."""
    from dissect.esedb.record import RecordData

    from edb_explorer.core.backends import ese

    if not ese.FAST_PATHS["as_dict"]:
        pytest.skip("fast paths not active for this dissect version")

    def outcome(record: object) -> object:
        try:
            return record.as_dict()  # type: ignore[attr-defined]
        except Exception as exc:  # a record too short to have a header raises in both implementations
            return ("raised", type(exc), str(exc))

    with EdbDatabase(DB) as db:  # type: ignore[arg-type]
        backend = db.backend
        assert isinstance(backend, ese.EseBackend)
        for table in backend.tables():
            raw_table = backend._raw[table.name]
            for n, record in enumerate(raw_table.records()):
                if n >= 200:
                    break
                fast = outcome(record)
                RecordData.as_dict, RecordData._parse_value = ese._orig_as_dict, ese._orig_parse_value
                try:
                    stock = outcome(record)
                finally:
                    RecordData.as_dict, RecordData._parse_value = ese._as_dict_fast, ese._parse_value_fast
                if not isinstance(fast, dict):
                    assert fast == stock, table.name
                    continue
                assert isinstance(stock, dict) and list(fast) == list(stock), table.name
                for key in fast:
                    assert fast[key] == stock[key], (table.name, key)
                    assert type(fast[key]) is type(stock[key]), (table.name, key)
