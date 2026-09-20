from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from edb_explorer.cli import app

runner = CliRunner()


def test_version() -> None:
    r = runner.invoke(app, ["--version"])
    assert r.exit_code == 0 and "EDB Explorer" in r.output


def test_info_tables_schema(fake_edb: Path) -> None:
    r = runner.invoke(app, ["info", str(fake_edb), "--json"])
    assert r.exit_code == 0, r.output
    assert json.loads(r.output)["profile_id"] == "srum"
    r = runner.invoke(app, ["tables", str(fake_edb), "--json", "--count"])
    assert r.exit_code == 0
    data = json.loads(r.output)
    assert {t["name"]: t["record_count"] for t in data}["SruDbIdMapTable"] == 4
    r = runner.invoke(app, ["schema", str(fake_edb), "SruDbIdMapTable", "--json"])
    assert json.loads(r.output)["columns"][0]["name"] == "IdType"


def test_dump_and_search(fake_edb: Path) -> None:
    r = runner.invoke(app, ["dump", str(fake_edb), "SruDbIdMapTable", "-f", "jsonl", "-g", "svchost"])
    assert r.exit_code == 0, r.output
    rows = [json.loads(line) for line in r.output.splitlines() if line.strip()]
    assert rows == [{"_row": 2, "IdType": 0, "IdIndex": 3, "IdBlob": "C:\\Windows\\svchost.exe"}]
    r = runner.invoke(app, ["dump", str(fake_edb), "Network Data Usage", "-n", "2", "-f", "csv", "-c", "AppId"])
    assert r.output.splitlines()[:3] == ["_row,AppId", "0,11", "1,12"]
    r = runner.invoke(app, ["search", str(fake_edb), "S-1-5", "--json"])
    assert json.loads(r.output)[0]["row_index"] == 1
    r = runner.invoke(app, ["dump", str(fake_edb), "missing"])
    assert r.exit_code == 1


def test_export_scan_timestamp(fake_edb: Path, tmp_path: Path) -> None:
    out = tmp_path / "exp"
    r = runner.invoke(app, ["export", str(fake_edb), "-o", str(out), "-f", "csv"])
    assert r.exit_code == 0, r.output
    assert (out / "SruDbIdMapTable.csv").exists()
    one = tmp_path / "one.jsonl"
    r = runner.invoke(app, ["export", str(fake_edb), "-o", str(one), "-t", "SruDbIdMapTable", "-f", "jsonl"])
    assert r.exit_code == 0 and one.read_text().count("\n") == 4
    r = runner.invoke(app, ["scan", str(tmp_path), "--json"])
    assert {"path": str(fake_edb), "kind": "ese"} in json.loads(r.output)
    r = runner.invoke(app, ["timestamp", "132565120200137766"])
    assert "2021-01-30" in r.output


def test_report_command(fake_edb: Path, tmp_path: Path) -> None:
    out = tmp_path / "rep.html"
    r = runner.invoke(app, ["report", str(fake_edb), "-o", str(out), "--no-hash", "--samples", "1", "--case", "C-9"])
    assert r.exit_code == 0, r.output
    assert out.exists() and "C-9" in out.read_text(encoding="utf-8")
    r = runner.invoke(app, ["report", str(fake_edb), "-o", str(tmp_path / "rep"), "-f", "txt", "--no-hash"])
    assert r.exit_code == 0 and (tmp_path / "rep.txt").exists()
    r = runner.invoke(app, ["export", str(fake_edb), "-o", str(tmp_path / "x.docx"), "-f", "docx"])
    assert r.exit_code == 1


def test_analysis_commands(fake_edb: Path, tmp_path: Path) -> None:
    r = runner.invoke(app, ["sql", str(fake_edb), "SELECT COUNT(*) AS n FROM SruDbIdMapTable", "-f", "json"])
    assert r.exit_code == 0, r.output
    assert json.loads(r.output) == [{"n": 4}]
    r = runner.invoke(app, ["views", str(fake_edb)])
    assert r.exit_code == 0 and "network_usage" in r.output
    r = runner.invoke(app, ["stats", str(fake_edb), "Network Data Usage", "--json"])
    assert r.exit_code == 0
    cols = {c["name"]: c for c in json.loads(r.output)["columns"]}
    assert cols["TimeStamp"]["timestamp_kind"] == "ese"
    out = tmp_path / "tl.jsonl"
    r = runner.invoke(app, ["timeline", str(fake_edb), "-o", str(out)])
    assert r.exit_code == 0 and out.read_text().count("\n") == 25
    r = runner.invoke(app, ["summary", str(fake_edb)])
    assert r.exit_code == 0 and json.loads(r.output)["profile"].startswith("System Resource")
    r = runner.invoke(app, ["formats"])
    assert r.exit_code == 0 and "leveldb" in r.output
