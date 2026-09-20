from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("mcp")

from edb_explorer.core import Session
from edb_explorer.mcp.server import build_server


def _call(server: Any, name: str, **kwargs: Any) -> dict[str, Any]:
    result = asyncio.run(server.call_tool(name, kwargs))
    structured = getattr(result, "structured_content", None)
    if structured is not None:
        return structured
    if isinstance(result, tuple):
        return result[1]
    content = result.content if hasattr(result, "content") else result
    return json.loads(content[0].text)


@pytest.fixture
def server(fake_edb: Path) -> Any:
    return build_server(Session())


def test_tool_listing(server: Any) -> None:
    names = {t.name for t in asyncio.run(server.list_tools())}
    assert {"open_database", "list_tables", "query_table", "search", "get_record", "interpret_timestamp"} <= names


def test_open_and_query(server: Any, fake_edb: Path) -> None:
    info = _call(server, "open_database", path=str(fake_edb))
    assert info["id"] == "srudb" and info["profile_id"] == "srum"
    assert _call(server, "list_databases")["count"] == 1

    tables = _call(server, "list_tables", db="srudb")
    assert tables["count"] == 3 and tables["tables"][0]["display_name"] == "SRUM ID Map (apps / users)"
    assert _call(server, "list_tables", db="srudb", include_system=True)["count"] == 4
    assert _call(server, "list_tables", db="srudb", pattern="network")["count"] == 1

    schema = _call(server, "describe_table", db="srudb", table="SruDbIdMapTable", count=True)
    assert schema["record_count"] == 4 and schema["columns"][2]["type"] == "LongBinary"

    page = _call(server, "query_table", db="srudb", table="Network Data Usage", limit=5, offset=20)
    assert page["returned"] == 5 and page["has_more"] is False and page["total"] == 25

    filtered = _call(server, "query_table", db="srudb", table="SruDbIdMapTable", filter_text="svchost")
    assert filtered["returned"] == 1 and filtered["rows"][0]["_row"] == 2
    eq = _call(server, "query_table", db="srudb", table="SruDbIdMapTable", equals={"IdType": "3"})
    assert eq["returned"] == 1 and eq["rows"][0]["IdBlob"] == "S-1-5-18"

    rec = _call(server, "get_record", db="srudb", table="SruDbIdMapTable", row=1)
    assert rec["record"]["IdBlob"] == "S-1-5-18"
    assert "error" in _call(server, "get_record", db="srudb", table="SruDbIdMapTable", row=50)

    raw = _call(server, "get_record_raw", db="srudb", table="SruDbIdMapTable", row=1, column="IdBlob")
    assert raw["smart"] == "S-1-5-18" and raw["hex"] == "010100000000000512000000"

    hits = _call(server, "search", db="srudb", query="chrome")
    assert hits["count"] == 1 and hits["hits"][0]["row_index"] == 3

    ts = _call(server, "interpret_timestamp", value="132565120200137766")
    assert ts["best_guess"] == "filetime"

    assert _call(server, "count_records", db="srudb", table="Network Data Usage")["record_count"] == 25
    assert len(_call(server, "list_known_profiles")["profiles"]) >= 5


def test_errors_are_structured(server: Any, fake_edb: Path, not_edb: Path) -> None:
    assert _call(server, "open_database", path=str(not_edb))["error"] == "InvalidDatabaseError"
    assert _call(server, "list_tables", db="nope")["error"] == "DatabaseNotFoundError"
    _call(server, "open_database", path=str(fake_edb))
    assert _call(server, "describe_table", db="srudb", table="missing")["error"] == "TableNotFoundError"
    assert "error" in _call(server, "interpret_timestamp", value="not a number")


def test_export_and_close(server: Any, fake_edb: Path, tmp_path: Path) -> None:
    _call(server, "open_database", path=str(fake_edb), id="ev1")
    out = _call(server, "export_table_to_file", db="ev1", table="SruDbIdMapTable", output_path=str(tmp_path / "m.csv"))
    assert out["rows_written"] == 4 and Path(out["output_path"]).exists()
    out = _call(server, "export_database_to_directory", db="ev1", output_dir=str(tmp_path / "d"), format="jsonl")
    assert out["total_rows"] == 29
    scan = _call(server, "scan_directory", path=str(tmp_path))
    assert scan["count"] == 1
    assert _call(server, "close_database", db="ev1") == {"closed": "ev1"}
    assert _call(server, "list_databases")["count"] == 0


def test_allowlist_blocks_paths(fake_edb: Path, tmp_path: Path) -> None:
    srv = build_server(Session(allowed_roots=[tmp_path / "elsewhere"]))
    assert _call(srv, "open_database", path=str(fake_edb))["error"] == "PathNotAllowedError"


def test_generate_report_tool(server: Any, fake_edb: Path, tmp_path: Path) -> None:
    assert "error" in _call(server, "generate_report", output_path=str(tmp_path / "r.html"))
    _call(server, "open_database", path=str(fake_edb))
    out = _call(server, "generate_report", output_path=str(tmp_path / "r.html"), compute_hash=False, sample_rows=1)
    assert out["format"] == "html" and Path(out["output_path"]).exists()
    out = _call(server, "generate_report", output_path=str(tmp_path / "r"), format="md", compute_hash=False)
    assert out["output_path"].endswith(".md")
    xlsx = _call(
        server,
        "export_table_to_file",
        db="srudb",
        table="SruDbIdMapTable",
        output_path=str(tmp_path / "t.xlsx"),
        format="xlsx",
    )
    assert xlsx["rows_written"] == 4
