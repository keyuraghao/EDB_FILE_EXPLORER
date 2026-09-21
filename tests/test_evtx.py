"""EVTX backend tests on synthetic event logs (see ``tests/evtx_writer.py`` for the minimal writer)."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from edb_explorer.core import Database
from edb_explorer.core.analysis import build_timeline
from edb_explorer.core.backends import detect_kind
from edb_explorer.core.sqlworkspace import SqlWorkspace

from .evtx_writer import build_chunk, build_file

pytest.importorskip("evtx")

SEC_GUID = "54849625-5478-4994-A5BA-3E3B0328C30D"
T0 = datetime(2024, 3, 1, 8, 0, 0, tzinfo=timezone.utc)


def _security(record_id: int, minute: int, event_id: int, **data: object) -> dict[str, object]:
    return {
        "record_id": record_id,
        "when": T0.replace(minute=minute),
        "channel": "Security",
        "provider": "Microsoft-Windows-Security-Auditing",
        "provider_guid": SEC_GUID,
        "system": {"EventID": event_id, "Task": 12544, "UserID": None},
        "data": data,
    }


@pytest.fixture
def security_evtx(tmp_path: Path) -> Path:
    recs = [
        _security(
            100,
            0,
            4624,
            TargetUserName="alice",
            TargetDomainName="CORP",
            LogonType=10,
            IpAddress="10.0.0.5",
            WorkstationName="WS1",
            SubjectUserName="SYSTEM",
            LogonProcessName="User32",
            AuthenticationPackageName="Negotiate",
        ),
        _security(
            101,
            1,
            4624,
            TargetUserName="bob",
            TargetDomainName="CORP",
            LogonType=3,
            IpAddress="10.0.0.9",
            WorkstationName="WS2",
            SubjectUserName="SYSTEM",
            LogonProcessName="Kerberos",
            AuthenticationPackageName="Kerberos",
        ),
        _security(
            102,
            2,
            4625,
            TargetUserName="eve",
            TargetDomainName="CORP",
            LogonType=3,
            IpAddress="10.0.66.66",
            WorkstationName="EVIL",
            Status="0xc000006d",
            SubStatus="0xc0000064",
        ),
        _security(
            103,
            3,
            4672,
            SubjectUserName="admin",
            SubjectDomainName="CORP",
            SubjectLogonId="0x3e7",
            PrivilegeList="SeDebugPrivilege",
        ),
    ]
    p = tmp_path / "Security.evtx"
    p.write_bytes(build_file([build_chunk(recs, 0)], next_record_id=104))
    return p


def test_detect_evtx(security_evtx: Path) -> None:
    assert detect_kind(security_evtx) == "evtx"


def test_security_schema_and_rows(security_evtx: Path) -> None:
    with Database(security_evtx) as db:
        assert db.kind == "evtx" and db.profile.id == "evtx_security"
        assert db.table_names() == ["Security"]
        cols = db.table("Security").column_names
        # System columns come first, in a stable order, then the union of EventData fields.
        assert cols[:5] == ["EventRecordID", "TimeCreated", "EventID", "Level", "LevelName"]
        assert {"TargetUserName", "LogonType", "IpAddress", "PrivilegeList", "SubStatus"} <= set(cols)
        assert db.count_records("Security") == 4
        assert db.column_types("Security")["TimeCreated"] == "DateTime"

        rows = db.fetch("Security", limit=10).rows
        first = rows[0]
        assert first["EventRecordID"] == 100 and first["EventID"] == 4624
        assert first["TimeCreated"] == "2024-03-01T08:00:00+00:00"
        assert first["Provider"] == "Microsoft-Windows-Security-Auditing"
        assert first["TargetUserName"] == "alice" and first["LogonType"] == 10
        assert first["LevelName"] == "Information"
        # A field absent from one event is simply omitted (nulls stripped), not an error.
        assert "PrivilegeList" not in first
        assert rows[3]["PrivilegeList"] == "SeDebugPrivilege"


def test_security_info_header(security_evtx: Path) -> None:
    with Database(security_evtx) as db:
        info = db.info
        assert info.kind == "evtx" and info.page_size == 0x10000
        assert info.header["format_version"] == "3.1"
        assert info.header["channels"] == {"Security": 4}
        assert info.header["next_record_id"] == 104
        assert info.state == "4 events"


def test_security_views(security_evtx: Path) -> None:
    with Database(security_evtx) as db:
        assert [v.id for v in db.profile.views][:3] == ["event_summary", "logons", "failed_logons"]
        ws = SqlWorkspace()
        try:
            summary = {r[0]: r[1] for r in ws.run_view(db, "event_summary").rows}
            assert summary == {4624: 2, 4625: 1, 4672: 1}
            logons = ws.run_view(db, "logons").rows
            assert {r[1] for r in logons} == {"alice", "bob"}
            failed = ws.run_view(db, "failed_logons").rows
            assert len(failed) == 1 and failed[0][1] == "eve"
            priv = ws.run_view(db, "special_privileges").rows
            assert priv[0][1] == "admin" and priv[0][4] == "SeDebugPrivilege"
        finally:
            ws.close()


def test_timeline_uses_timecreated(security_evtx: Path) -> None:
    with Database(security_evtx) as db:
        events, truncated = build_timeline([db], limit=100)
        assert not truncated and len(events) == 4
        assert events[0].column == "TimeCreated"
        assert [e.timestamp for e in events] == sorted(e.timestamp for e in events)


def test_unnamed_and_userdata_payloads(tmp_path: Path) -> None:
    recs = [
        # Unnamed <Data> elements (Application error style) collect into a JSON list in a "Data" column.
        {
            "record_id": 200,
            "when": T0,
            "channel": "Application",
            "provider": "Application Error",
            "provider_guid": "00000000-0000-0000-0000-000000000000",
            "system": {"EventID": 1000, "Level": 2},
            "data": {},
            "unnamed": ["notepad.exe", "10.0.19041.1", "c0000005"],
        },
        # UserData (RDP style) is flattened one level into named columns.
        {
            "record_id": 201,
            "when": T0.replace(minute=5),
            "channel": "Microsoft-Windows-TerminalServices-LocalSessionManager/Operational",
            "provider": "Microsoft-Windows-TerminalServices-LocalSessionManager",
            "provider_guid": "5D896912-022D-40AA-A3A8-4FA5515C76D7",
            "system": {"EventID": 21, "Level": 4, "UserID": "S-1-5-18"},
            "data": {"User": "CORP\\bob", "SessionID": 3, "Address": "10.0.0.9"},
            "user_data": True,
        },
    ]
    p = tmp_path / "mixed.evtx"
    p.write_bytes(build_file([build_chunk(recs, 0)]))
    with Database(p) as db:
        # Two channels -> two tables, each named after its channel.
        assert set(db.table_names()) == {
            "Application",
            "Microsoft-Windows-TerminalServices-LocalSessionManager/Operational",
        }
        assert db.count_records("Application") == 1
        app = db.fetch("Application", limit=1).rows[0]
        assert app["Data"] == '["notepad.exe", "10.0.19041.1", "c0000005"]'
        rdp_table = "Microsoft-Windows-TerminalServices-LocalSessionManager/Operational"
        rdp = db.fetch(rdp_table, limit=1).rows[0]
        assert rdp["User"] == "CORP\\bob" and rdp["SessionID"] == 3 and rdp["Address"] == "10.0.0.9"
        assert rdp["Security_UserID"] == "S-1-5-18"


def test_not_evtx_is_rejected(tmp_path: Path) -> None:
    p = tmp_path / "bogus.evtx"
    p.write_bytes(b"NotAnEvtxFile" + b"\x00" * 4096)
    assert detect_kind(p) is None
