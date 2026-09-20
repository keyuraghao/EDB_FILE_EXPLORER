"""Exchange support: ProP blob grammar, RTF, store model over a fake mailbox database, export, agents config."""

from __future__ import annotations

import json
import struct
import uuid
from pathlib import Path
from typing import Any

import pytest

from edb_explorer.core import Session
from edb_explorer.core.backends.ese import ESE_MAGIC
from edb_explorer.core.exchange import ExchangeStore, is_exchange_database, parse_property_blob, property_name
from edb_explorer.core.exchange.export import export_messages, message_to_eml, message_to_html, message_to_text
from edb_explorer.core.exchange.props import decode_value_for_display
from edb_explorer.core.exchange.rtf import decompress_rtf, rtf_to_text
from tests.conftest import FakeColumn, FakeIndex, FakeTable, _Header

FILETIME_2021 = 132565120200137766


# --------------------------------------------------------------------------- #
# ProP encoder (test helper) - mirrors the grammar documented in props.py
# --------------------------------------------------------------------------- #
def _lenprefix(base: int, n: int, ansi: bool = False) -> tuple[int, bytes]:
    if n == 0:
        return base | (0x04 if ansi else 0), b""
    if n < 256:
        return base | 1 | (0x04 if ansi else 0), bytes([n])
    return base | 2 | (0x04 if ansi else 0), struct.pack("<H", n)


def encode_property_blob(props: dict[int, Any], offpage: list[int] | None = None) -> bytes:
    tags = bytearray()
    values = bytearray()
    entries: list[tuple[int, int, bytes]] = []
    for pid, v in props.items():
        if isinstance(v, bool):
            entries.append((0x08, pid, bytes([0x09 if v else 0x08])))
        elif isinstance(v, int) and v > 0xFFFFFFFF:
            entries.append((0x38, pid, b"\x38" + struct.pack("<Q", v)))
        elif isinstance(v, int):
            entries.append((0x18, pid, b"\x1b" + struct.pack("<i", v)))
        elif isinstance(v, uuid.UUID):
            entries.append((0x40, pid, b"\x40" + v.bytes_le))
        elif isinstance(v, str):
            raw = v.encode("latin-1")
            m, ln = _lenprefix(0x48, len(raw), ansi=True)
            entries.append((0x48, pid, bytes([m]) + ln + raw))
        elif isinstance(v, bytes):
            m, ln = _lenprefix(0x50, len(v))
            entries.append((0x50, pid, bytes([m]) + ln + v))
        elif isinstance(v, list):
            body = bytearray([0x99, len(v)])
            for item in v:
                body += struct.pack("<i", item)
            entries.append((0x98, pid, bytes(body)))
    for pid in offpage or []:
        entries.append((0x4A, pid, b""))
    entries.sort(key=lambda e: e[1])
    for ty, pid, val in entries:
        tags += bytes([ty]) + struct.pack("<H", pid)
        values += val
    return b"ProP" + struct.pack("<HH", 0x0400, len(entries)) + bytes(tags) + bytes(values)


def test_prop_blob_roundtrip() -> None:
    guid = uuid.uuid4()
    props = {
        0x0037: "Re: Hello",
        0x0C1A: "Alice",
        0x5D01: "alice@example.com",
        0x0E1B: True,
        0x0E07: 1,
        0x0039: FILETIME_2021,
        0x300B: b"\x01\x02\x03",
        0x0071: b"",
        0x812B: guid,
        0x8155: [1, 2, 3],
    }
    blob = encode_property_blob(props, offpage=[0x007D, 0x4022])
    pb = parse_property_blob(blob)
    assert pb.truncated_at is None
    assert pb.values[0x0037] == "Re: Hello" and pb.values[0x5D01] == "alice@example.com"
    assert pb.values[0x0E1B] is True and pb.values[0x0E07] == 1 and pb.values[0x0039] == FILETIME_2021
    assert pb.values[0x300B] == b"\x01\x02\x03" and pb.values[0x0071] == b"" and pb.values[0x812B] == guid
    assert pb.values[0x8155] == [1, 2, 3]
    assert sorted(pb.offpage) == [0x007D, 0x4022]
    named = pb.as_named({0x8155: "custom-int-list"})
    assert named["PR_SUBJECT"] == "Re: Hello" and named["custom-int-list"] == [1, 2, 3]
    assert property_name(0x1035) == "PR_INTERNET_MESSAGE_ID" and property_name(0x9999) == "0x9999"
    assert decode_value_for_display(0x0039, FILETIME_2021).startswith("2021-01-30")
    assert parse_property_blob(b"garbage").values == {} and parse_property_blob(None).values == {}
    truncated = parse_property_blob(blob[:-6])
    assert truncated.truncated_at is not None and truncated.values[0x0037] == "Re: Hello"


def test_rtf() -> None:
    raw = b"{\\rtf1\\ansi\\deff0{\\fonttbl{\\f0 Arial;}}\\pard Hello\\par \\b World \\b0\\'e9!}"
    body = struct.pack("<IIII", len(raw) + 12, len(raw), 0x414C454D, 0) + raw
    assert decompress_rtf(body) == raw
    assert rtf_to_text(raw) == "Hello\nWorld é!"
    assert decompress_rtf(b"short") == b"short"


# --------------------------------------------------------------------------- #
# Fake Exchange database
# --------------------------------------------------------------------------- #
FID_ROOT = bytes.fromhex("01" * 26)
FID_TOP = bytes.fromhex("02" * 26)
FID_INBOX = bytes.fromhex("03" * 26)
FID_SENT = bytes.fromhex("04" * 26)


def _msg(
    doc: int,
    folder: bytes,
    subject: str,
    sender: str,
    smtp: str,
    received: int,
    body: bytes | None = None,
    body_type: int | None = None,
    attachments: bool = False,
    hidden: bool = False,
) -> dict[str, Any]:
    row: dict[str, Any] = {
        "MessageDocumentId": doc,
        "FolderId": folder,
        "DateReceived": received,
        "DateSent": received - 10_000_000,
        "Size": 1000 + doc,
        "IsRead": doc % 2 == 0,
        "IsHidden": hidden,
        "HasAttachments": attachments,
        "MessageClass": "IPM.Note",
        "DisplayTo": "Bob <bob@example.com>",
        "CodePage": 1252,
        "Importance": 1,
        "PropertyBlob": encode_property_blob(
            {0x0037: subject, 0x0C1A: sender, 0x5D01: smtp, 0x1035: f"<{doc}@example.com>", 0x0E03: "cc@example.com"},
            offpage=[0x007D],
        ),
        "OffPagePropertyBlob": encode_property_blob(
            {0x007D: f"Received: from mx\r\nSubject: {subject}\r\nX-Test: {doc}\r\n"}
        ),
        "RecipientList": b"\xd1\x01\x52"
        + struct.pack("<H", 0)
        + encode_property_blob({0x0C15: 1, 0x3001: "Bob", 0x39FE: "bob@example.com", 0x3002: "SMTP"}),
    }
    if body is not None:
        row["NativeBody"] = body
        row["BodyType"] = body_type
    if attachments:
        row["SubobjectsBlob"] = b"\x18\x19\x01\x18\x18\x21\x07"  # version 0, count 1, (index 0, inid 7)
    return row


class FakeExchangeEseDB:
    def __init__(self, fh: Any, impacket_compat: bool = False) -> None:
        head = fh.read(8)
        if head[4:8] != ESE_MAGIC:
            from dissect.esedb.exceptions import InvalidDatabase

            raise InvalidDatabase("bad magic")
        self.header = _Header()
        self.page_size = 32768
        self.version = 0x620
        self.format_major = 20
        mailbox_cols = [
            FakeColumn(1, "MailboxNumber", "Long"),
            FakeColumn(2, "MessageCount", "Long"),
            FakeColumn(3, "MessageSize", "LongLong"),
            FakeColumn(4, "LastLogonTime", "LongLong"),
            FakeColumn(128, "DisplayName", "Text", "variable"),
            FakeColumn(129, "MailboxGuid", "GUID"),
            FakeColumn(130, "MessageDeletedCount", "Long"),
        ]
        folder_cols = [
            FakeColumn(1, "FolderId", "Binary"),
            FakeColumn(2, "ParentFolderId", "Binary"),
            FakeColumn(3, "DisplayName", "Text", "variable"),
            FakeColumn(4, "MessageCount", "Long"),
            FakeColumn(5, "UnreadMessageCount", "Long"),
            FakeColumn(6, "SpecialFolderNumber", "Long"),
            FakeColumn(7, "ContainerClass", "Text", "variable"),
            FakeColumn(8, "CreationTime", "LongLong"),
        ]
        message_cols = [
            FakeColumn(i + 1, n, "Long")
            for i, n in enumerate(
                [
                    "MessageDocumentId",
                    "FolderId",
                    "DateReceived",
                    "DateSent",
                    "Size",
                    "IsRead",
                    "IsHidden",
                    "HasAttachments",
                    "MessageClass",
                    "DisplayTo",
                    "CodePage",
                    "Importance",
                    "PropertyBlob",
                    "OffPagePropertyBlob",
                    "RecipientList",
                    "NativeBody",
                    "BodyType",
                    "SubobjectsBlob",
                ]
            )
        ]
        attach_cols = [
            FakeColumn(1, "Inid", "Long"),
            FakeColumn(2, "AttachmentMethod", "Long"),
            FakeColumn(3, "Size", "Long"),
            FakeColumn(4, "CreationTime", "LongLong"),
            FakeColumn(128, "Name", "Text", "variable"),
            FakeColumn(129, "ContentId", "Text", "variable"),
            FakeColumn(256, "Content", "LongBinary", "tagged"),
        ]
        folders = [
            {
                "FolderId": FID_ROOT,
                "ParentFolderId": b"\x00" * 26,
                "DisplayName": "Root",
                "MessageCount": 0,
                "UnreadMessageCount": 0,
                "SpecialFolderNumber": 1,
            },
            {
                "FolderId": FID_TOP,
                "ParentFolderId": FID_ROOT,
                "DisplayName": "Top of Information Store",
                "MessageCount": 0,
                "UnreadMessageCount": 0,
            },
            {
                "FolderId": FID_INBOX,
                "ParentFolderId": FID_TOP,
                "DisplayName": "Inbox",
                "MessageCount": 3,
                "UnreadMessageCount": 1,
                "SpecialFolderNumber": 3,
                "ContainerClass": "IPF.Note",
                "CreationTime": FILETIME_2021,
            },
            {
                "FolderId": FID_SENT,
                "ParentFolderId": FID_TOP,
                "DisplayName": "Sent Items",
                "MessageCount": 1,
                "UnreadMessageCount": 0,
                "SpecialFolderNumber": 5,
            },
        ]
        html = b"<html><body><b>Hello</b> there</body></html>"
        messages = [
            _msg(
                1,
                FID_INBOX,
                "Invoice attached",
                "Mallory",
                "mallory@evil.example",
                FILETIME_2021 + 3_000_000_000,
                "Plain text body".encode("utf-16-le"),
                1,
                attachments=True,
            ),
            _msg(2, FID_INBOX, "Re: lunch", "Alice", "alice@example.com", FILETIME_2021 + 2_000_000_000, html, 3),
            _msg(3, FID_INBOX, "hidden thing", "System", "sys@example.com", FILETIME_2021 + 1_000_000_000, hidden=True),
            _msg(4, FID_SENT, "Sent one", "Me", "me@example.com", FILETIME_2021),
        ]
        attachments = [
            {
                "Inid": 7,
                "AttachmentMethod": 1,
                "Size": 12,
                "CreationTime": FILETIME_2021,
                "Name": "invoice.zip",
                "ContentId": ".zip",
                "Content": b"PK\x03\x04test1234",
            }
        ]
        self._tables = [
            FakeTable("MSysObjects", 4, [FakeColumn(1, "ObjidTable", "Long")], []),
            FakeTable(
                "Mailbox",
                10,
                mailbox_cols,
                [
                    {
                        "MailboxNumber": 1,
                        "DisplayName": "Jane Doe",
                        "MessageCount": 4,
                        "MessageSize": 4000,
                        "LastLogonTime": FILETIME_2021,
                        "MailboxGuid": "g1",
                        "MessageDeletedCount": 0,
                    },
                    {
                        "MailboxNumber": 2,
                        "DisplayName": "HealthMailbox-abc",
                        "MessageCount": 9,
                        "MessageSize": 90,
                        "MessageDeletedCount": 0,
                    },
                ],
                [FakeIndex("MailboxNumber", True, True)],
            ),
            FakeTable("Folder", 11, folder_cols, []),
            FakeTable("Message", 12, message_cols, []),
            FakeTable("Attachment", 13, attach_cols, []),
            FakeTable("Folder_1", 20, folder_cols, folders),
            FakeTable("Message_1", 21, message_cols, messages),
            FakeTable("Attachment_1", 22, attach_cols, attachments),
            FakeTable(
                "ExtendedPropertyNameMapping_1",
                23,
                [
                    FakeColumn(1, "PropNumber", "Long"),
                    FakeColumn(128, "PropName", "Text", "variable"),
                    FakeColumn(129, "PropGuid", "Text", "variable"),
                    FakeColumn(2, "PropDispId", "Long"),
                ],
                [{"PropNumber": 0x8155, "PropName": "x-custom", "PropGuid": "guid-1"}],
            ),
        ]

    def tables(self) -> list[FakeTable]:
        return self._tables


@pytest.fixture
def exchange_edb(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr("edb_explorer.core.backends.ese.EseDB", FakeExchangeEseDB)
    p = tmp_path / "Mailbox Database.edb"
    p.write_bytes(b"\x00\x00\x00\x00" + ESE_MAGIC + b"\x00" * 4088)
    return p


def test_store_model(exchange_edb: Path, tmp_path: Path) -> None:
    db = Session().open(exchange_edb)
    assert is_exchange_database(db) and db.profile.id == "exchange"
    store = ExchangeStore(db)
    boxes = store.mailboxes()
    assert [b.number for b in boxes] == [1, 2] and boxes[1].is_system and not boxes[0].is_system
    assert store.mailboxes(include_system=False)[0].display_name == "Jane Doe"
    assert boxes[0].has_tables and not boxes[1].has_tables and boxes[0].last_logon.startswith("2021-01-30")

    folders = store.folders(1)
    assert [f.display_name for f in folders] == ["Root", "Top of Information Store", "Inbox", "Sent Items"]
    inbox = folders[2]
    assert inbox.depth == 2 and inbox.path == "Root/Top of Information Store/Inbox" and inbox.special_folder == "Inbox"

    msgs = store.messages(1, inbox.folder_id)
    assert [m.subject for m in msgs] == ["Invoice attached", "Re: lunch"]  # newest first, hidden excluded
    assert store.messages(1, inbox.folder_id, include_hidden=True)[2].subject == "hidden thing"
    assert store.messages(1, text="lunch")[0].sender_email == "alice@example.com"
    assert len(store.messages(1)) == 3 and store.message_count(1) == 4
    first = msgs[0]
    assert (
        first.has_attachments
        and first.internet_message_id == "<1@example.com>"
        and first.date_received.startswith("2021-01-30")
    )

    d = store.message(1, 1)
    assert d.body_type == "text" and d.body_text == "Plain text body" and d.body_html is None
    assert d.headers.startswith("Received: from mx") and d.display_cc == "cc@example.com"
    assert d.recipients == [{"type": "to", "name": "Bob", "email": "bob@example.com", "address_type": "SMTP"}]
    assert (
        d.attachments[0]["name"] == "invoice.zip"
        and d.attachments[0]["method"] == "by value"
        and d.attachments[0]["inid"] == 7
    )
    assert d.properties["PR_SUBJECT"] == "Invoice attached" and d.properties["PR_TRANSPORT_MESSAGE_HEADERS"]
    name, data = store.attachment_content(1, 7)
    assert name == "invoice.zip" and data.startswith(b"PK")

    d2 = store.message(1, 2)
    assert d2.body_type == "html" and "<b>Hello</b>" in d2.body_html and d2.attachments == []
    assert store.named_properties(1)[0x8155].startswith("x-custom")
    with pytest.raises(Exception, match="not found"):
        store.message(1, 99)

    # export
    eml = message_to_eml(store, d)
    assert b"Subject: Invoice attached" in eml and b"X-Test: 1" in eml and b'filename="invoice.zip"' in eml
    assert b"Plain text body" in eml
    html = message_to_html(d2)
    assert "<b>Hello</b>" in html and "alice@example.com" in html
    text = message_to_text(d2)
    assert "Hello there" in text
    paths = export_messages(store, msgs, tmp_path / "out", "eml")
    assert len(paths) == 2 and all(p.suffix == ".eml" for p in paths) and "Inbox" in str(paths[0])
    assert (paths[0].parent / "1_Invoice attached_att7_invoice.zip").exists() is False  # eml embeds attachments
    paths = export_messages(store, msgs[:1], tmp_path / "out2", "json", by_folder=False)
    assert json.loads(paths[0].read_text())["subject"] == "Invoice attached"
    assert (tmp_path / "out2" / "1_Invoice attached_att7_invoice.zip").exists()
    assert store.summary()["user_mailboxes"] == 1


def test_not_exchange(fake_edb: Path) -> None:
    db = Session().open(fake_edb)
    assert not is_exchange_database(db)
    with pytest.raises(Exception, match="not an Exchange"):
        ExchangeStore(db)


# --------------------------------------------------------------------------- #
# Agents
# --------------------------------------------------------------------------- #
def test_agent_config_writers(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from edb_explorer.core import agents

    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(agents.os.path, "expanduser", lambda p: p.replace("~", str(tmp_path)))
    gemini = agents.agent_by_id("gemini")
    msg = agents.configure_agent(gemini, ["/evidence"])
    cfg = json.loads((tmp_path / ".gemini" / "settings.json").read_text())
    assert "settings.json" in msg and cfg["mcpServers"]["edb-explorer"]["args"][-2:] == ["--allow", "/evidence"]
    (tmp_path / ".gemini" / "settings.json").write_text("{not json")
    agents.configure_agent(gemini, None)
    assert (tmp_path / ".gemini" / "settings.json.bak").exists()
    codex = agents.agent_by_id("codex")
    agents.configure_agent(codex, ["/a"])
    agents.configure_agent(codex, ["/b"])  # second call replaces the block
    toml = (tmp_path / ".codex" / "config.toml").read_text()
    assert toml.count("[mcp_servers.edb-explorer]") == 1 and '"/b"' in toml and '"/a"' not in toml
    assert "no MCP client" in agents.configure_agent(agents.agent_by_id("aider"))
    snippet = agents.mcp_config_snippet()
    assert "edb-explorer" in snippet["mcpServers"] and snippet["mcpServers"]["edb-explorer"]["args"][-1] == "mcp"
    st = agents.agent_status(agents.agent_by_id("shell"))
    assert st["installed"] is True
