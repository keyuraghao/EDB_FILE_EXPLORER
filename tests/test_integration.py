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
