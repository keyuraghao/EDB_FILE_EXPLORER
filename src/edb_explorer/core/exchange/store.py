"""Mailbox / folder / message model over an Exchange 2013+ mailbox database."""

from __future__ import annotations

import logging
import re
import threading
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any

from edb_explorer.core.database import Database
from edb_explorer.core.exceptions import EdbExplorerError
from edb_explorer.core.exchange.props import (
    ATTACH_METHODS,
    RECIPIENT_TYPES,
    PropertyBlob,
    decode_value_for_display,
    parse_property_blob,
    property_name,
)
from edb_explorer.core.exchange.rtf import decompress_rtf, rtf_to_text
from edb_explorer.core.values import filetime_to_datetime

log = logging.getLogger(__name__)

# MAPI ids used for summaries
P_SUBJECT, P_NORMALIZED_SUBJECT, P_SUBJECT_PREFIX = 0x0037, 0x0E1D, 0x003D
P_SENDER_NAME, P_SENDER_EMAIL, P_SENDER_SMTP = 0x0C1A, 0x0C1F, 0x5D01
P_SENT_REP_NAME, P_SENT_REP_SMTP, P_SENT_REP_EMAIL = 0x0042, 0x5D02, 0x0065
P_DISPLAY_TO, P_DISPLAY_CC, P_DISPLAY_BCC = 0x0E04, 0x0E03, 0x0E02
P_INTERNET_MESSAGE_ID, P_TRANSPORT_HEADERS = 0x1035, 0x007D
P_BODY, P_HTML, P_RTF = 0x1000, 0x1013, 0x1009
P_CLIENT_SUBMIT_TIME, P_DELIVERY_TIME = 0x0039, 0x0E06
P_CONVERSATION_TOPIC = 0x0070
P_CREATOR_NAME, P_CREATOR_SMTP = 0x3FF8, 0x5D0A
P_RECIP_TYPE, P_RECIP_DISPLAY, P_EMAIL_ADDRESS, P_SMTP, P_RECIP_DISPLAY2 = 0x0C15, 0x3001, 0x3003, 0x39FE, 0x5FF6

SYSTEM_MAILBOX_RE = re.compile(
    r"^(HealthMailbox|SystemMailbox|Microsoft Exchange|In-Place Archive|DiscoverySearchMailbox"
    r"|FederatedEmail|Migration)",
    re.I,
)
_SPECIAL_FOLDERS = {
    1: "Root",
    2: "IPM Subtree",
    3: "Inbox",
    4: "Outbox",
    5: "Sent Items",
    6: "Deleted Items",
    7: "Calendar",
    8: "Contacts",
    9: "Drafts",
    10: "Journal",
    11: "Notes",
    12: "Tasks",
    13: "Junk E-mail",
    14: "Conversation Action Settings",
}


def is_exchange_database(db: Database) -> bool:
    names = set(db.table_names())
    return db.kind == "ese" and {"Mailbox", "Folder", "Message"} <= names


def _dt(v: Any) -> str | None:
    """Normalise a date column value (FILETIME int or ISO string) to ISO-8601."""
    if v is None or v == 0:
        return None
    if isinstance(v, str):
        return v
    if isinstance(v, int):
        d = filetime_to_datetime(v)
        return d.isoformat() if d else None
    if isinstance(v, datetime):
        return v.isoformat()
    return str(v)


def _hex(v: Any) -> str | None:
    if isinstance(v, bytes | bytearray | memoryview):
        return bytes(v).hex()
    if isinstance(v, str) and v.startswith("0x"):
        return v[2:]
    return None if v is None else str(v)


# --------------------------------------------------------------------------- #
@dataclass(slots=True)
class MailboxInfo:
    number: int
    display_name: str
    owner_display_name: str | None
    guid: str | None
    message_count: int
    message_size: int
    deleted_count: int
    hidden_count: int
    last_logon: str | None
    last_logoff: str | None
    is_system: bool
    has_tables: bool
    deleted_on: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class FolderInfo:
    mailbox: int
    folder_id: str
    parent_id: str | None
    display_name: str
    message_count: int
    unread_count: int
    folder_count: int
    hidden_count: int
    container_class: str | None
    special_folder: str | None
    creation_time: str | None
    last_modification: str | None
    depth: int = 0
    path: str = ""
    children: list[FolderInfo] = field(default_factory=list)

    def to_dict(self, recursive: bool = False) -> dict[str, Any]:
        d = {k: getattr(self, k) for k in self.__slots__ if k != "children"}
        if recursive:
            d["children"] = [c.to_dict(True) for c in self.children]
        return d


@dataclass(slots=True)
class MessageSummary:
    mailbox: int
    folder_id: str
    document_id: int
    row: int
    subject: str
    sender_name: str
    sender_email: str
    display_to: str
    date_received: str | None
    date_sent: str | None
    size: int
    has_attachments: bool
    is_read: bool
    is_hidden: bool
    message_class: str
    importance: int
    internet_message_id: str | None
    conversation_topic: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class MessageDetail:
    summary: MessageSummary
    body_text: str | None
    body_html: str | None
    body_type: str
    headers: str | None
    recipients: list[dict[str, Any]]
    attachments: list[dict[str, Any]]
    properties: dict[str, Any]
    columns: dict[str, Any]
    display_cc: str = ""
    display_bcc: str = ""

    def to_dict(self, include_body: bool = True) -> dict[str, Any]:
        d: dict[str, Any] = {
            **self.summary.to_dict(),
            "display_cc": self.display_cc,
            "display_bcc": self.display_bcc,
            "body_type": self.body_type,
            "headers": self.headers,
            "recipients": self.recipients,
            "attachments": self.attachments,
            "properties": self.properties,
            "columns": self.columns,
        }
        if include_body:
            d["body_text"] = self.body_text
            d["body_html"] = self.body_html
        return d


# --------------------------------------------------------------------------- #
class ExchangeStore:
    """Read-only view of the mailboxes inside an Exchange mailbox database."""

    def __init__(self, db: Database) -> None:
        if not is_exchange_database(db):
            raise EdbExplorerError(f"{db.path.name} is not an Exchange mailbox database")
        self.db = db
        self._lock = threading.RLock()
        self._names = set(db.table_names())
        self._mailboxes: list[MailboxInfo] | None = None
        self._folders: dict[int, list[FolderInfo]] = {}
        self._messages: dict[int, list[MessageSummary]] = {}
        self._named: dict[int, dict[int, str]] = {}
        self._doc_rows: dict[int, dict[int, int]] = {}

    # -- helpers -------------------------------------------------------- #
    def _table(self, base: str, mailbox: int) -> str | None:
        name = f"{base}_{mailbox}"
        return name if name in self._names else None

    def _rows(self, table: str, **kw: Any) -> Any:
        return self.db.iter_records(table, bytes_mode="raw", include_nulls=False, **kw)

    def named_properties(self, mailbox: int) -> dict[int, str]:
        if mailbox not in self._named:
            out: dict[int, str] = {}
            t = self._table("ExtendedPropertyNameMapping", mailbox)
            if t:
                for _, r in self._rows(t):
                    num = r.get("PropNumber")
                    if not isinstance(num, int):
                        continue
                    name = r.get("PropName")
                    disp = r.get("PropDispId")
                    guid = r.get("PropGuid")
                    label = (
                        name
                        if isinstance(name, str) and name
                        else (f"id 0x{disp:04X}" if isinstance(disp, int) else f"named 0x{num:04X}")
                    )
                    out[num] = f"{label} [{guid}]" if guid else str(label)
            self._named[mailbox] = out
        return self._named[mailbox]

    # -- mailboxes ------------------------------------------------------ #
    def mailboxes(self, include_system: bool = True) -> list[MailboxInfo]:
        if self._mailboxes is None:
            out: list[MailboxInfo] = []
            for _, r in self._rows("Mailbox"):
                num = r.get("MailboxNumber")
                if not isinstance(num, int):
                    continue
                name = (
                    r.get("DisplayName")
                    or r.get("MailboxOwnerDisplayName")
                    or r.get("SimpleDisplayNameWide")
                    or f"Mailbox {num}"
                )
                guid = r.get("MailboxGuid")
                out.append(
                    MailboxInfo(
                        number=num,
                        display_name=str(name),
                        owner_display_name=r.get("MailboxOwnerDisplayName"),
                        guid=str(guid) if guid is not None else None,
                        message_count=int(r.get("MessageCount") or 0),
                        message_size=int(r.get("MessageSize") or 0),
                        deleted_count=int(r.get("MessageDeletedCount") or 0),
                        hidden_count=int(r.get("HiddenMessageCount") or 0),
                        last_logon=_dt(r.get("LastLogonTime")),
                        last_logoff=_dt(r.get("LastLogoffTime")),
                        is_system=bool(SYSTEM_MAILBOX_RE.match(str(name))),
                        has_tables=self._table("Message", num) is not None,
                        deleted_on=_dt(r.get("DeletedOn")),
                    )
                )
            out.sort(key=lambda m: (m.is_system, m.display_name.lower()))
            self._mailboxes = out
        return [m for m in self._mailboxes if include_system or not m.is_system]

    def mailbox(self, number: int) -> MailboxInfo:
        for m in self.mailboxes():
            if m.number == number:
                return m
        raise EdbExplorerError(f"Mailbox {number} not found")

    # -- folders -------------------------------------------------------- #
    def folders(self, mailbox: int) -> list[FolderInfo]:
        """All folders of a mailbox in tree (depth-first) order, with ``depth`` and ``path`` filled in."""
        if mailbox in self._folders:
            return self._folders[mailbox]
        table = self._table("Folder", mailbox)
        infos: dict[str, FolderInfo] = {}
        if table:
            for _, r in self._rows(table):
                fid = _hex(r.get("FolderId"))
                if not fid:
                    continue
                special = r.get("SpecialFolderNumber")
                infos[fid] = FolderInfo(
                    mailbox=mailbox,
                    folder_id=fid,
                    parent_id=_hex(r.get("ParentFolderId")),
                    display_name=str(
                        r.get("DisplayName") or _SPECIAL_FOLDERS.get(special or 0) or f"Folder {fid[-8:]}"
                    ),
                    message_count=int(r.get("MessageCount") or 0),
                    unread_count=int(r.get("UnreadMessageCount") or 0),
                    folder_count=int(r.get("FolderCount") or 0),
                    hidden_count=int(r.get("HiddenItemCount") or 0),
                    container_class=r.get("ContainerClass"),
                    special_folder=_SPECIAL_FOLDERS.get(special) if isinstance(special, int) else None,
                    creation_time=_dt(r.get("CreationTime")),
                    last_modification=_dt(r.get("LastModificationTime")),
                )
        roots: list[FolderInfo] = []
        for f in infos.values():
            parent = infos.get(f.parent_id or "")
            if parent is None or parent is f:
                roots.append(f)
            else:
                parent.children.append(f)
        ordered: list[FolderInfo] = []

        def walk(node: FolderInfo, depth: int, path: str) -> None:
            node.depth = depth
            node.path = f"{path}/{node.display_name}" if path else node.display_name
            ordered.append(node)
            for child in sorted(node.children, key=lambda c: c.display_name.lower()):
                walk(child, depth + 1, node.path)

        for root in sorted(roots, key=lambda c: c.display_name.lower()):
            walk(root, 0, "")
        self._folders[mailbox] = ordered
        return ordered

    def folder(self, mailbox: int, folder_id: str) -> FolderInfo | None:
        return next((f for f in self.folders(mailbox) if f.folder_id == folder_id), None)

    # -- messages ------------------------------------------------------- #
    def _summaries(self, mailbox: int) -> list[MessageSummary]:
        if mailbox in self._messages:
            return self._messages[mailbox]
        table = self._table("Message", mailbox)
        out: list[MessageSummary] = []
        rows_index: dict[int, int] = {}
        if table:
            for idx, r in self._rows(table):
                doc = r.get("MessageDocumentId")
                if not isinstance(doc, int):
                    continue
                props = parse_property_blob(r.get("PropertyBlob")).merge(
                    parse_property_blob(r.get("OffPagePropertyBlob"))
                )
                prefix = str(props.get(P_SUBJECT_PREFIX) or r.get("SubjectPrefix") or "")
                subject = props.get(P_SUBJECT)
                if subject is None:
                    normalized = props.get(P_NORMALIZED_SUBJECT)
                    if normalized is not None:
                        subject = prefix + str(normalized)
                    else:
                        subject = props.get(P_CONVERSATION_TOPIC) or prefix
                sender_name = props.get(P_SENDER_NAME) or props.get(P_SENT_REP_NAME) or props.get(P_CREATOR_NAME) or ""
                sender_email = (
                    props.get(P_SENDER_SMTP)
                    or props.get(P_SENT_REP_SMTP)
                    or props.get(P_CREATOR_SMTP)
                    or props.get(P_SENDER_EMAIL)
                    or props.get(P_SENT_REP_EMAIL)
                    or ""
                )
                rows_index[doc] = idx
                out.append(
                    MessageSummary(
                        mailbox=mailbox,
                        folder_id=_hex(r.get("FolderId")) or "",
                        document_id=doc,
                        row=idx,
                        subject=str(subject),
                        sender_name=str(sender_name),
                        sender_email=str(sender_email),
                        display_to=str(r.get("DisplayTo") or props.get(P_DISPLAY_TO) or ""),
                        date_received=_dt(r.get("DateReceived")) or _dt(props.get(P_DELIVERY_TIME)),
                        date_sent=_dt(r.get("DateSent")) or _dt(props.get(P_CLIENT_SUBMIT_TIME)),
                        size=int(r.get("Size") or 0),
                        has_attachments=bool(r.get("HasAttachments")),
                        is_read=bool(r.get("IsRead")),
                        is_hidden=bool(r.get("IsHidden")),
                        message_class=str(r.get("MessageClass") or ""),
                        importance=int(r.get("Importance") or 1),
                        internet_message_id=props.get(P_INTERNET_MESSAGE_ID),
                        conversation_topic=props.get(P_CONVERSATION_TOPIC),
                    )
                )
        self._messages[mailbox] = out
        self._doc_rows[mailbox] = rows_index
        return out

    def messages(
        self,
        mailbox: int,
        folder_id: str | None = None,
        include_hidden: bool = False,
        text: str | None = None,
        limit: int | None = None,
        offset: int = 0,
    ) -> list[MessageSummary]:
        items = self._summaries(mailbox)
        if folder_id:
            items = [m for m in items if m.folder_id == folder_id]
        if not include_hidden:
            items = [m for m in items if not m.is_hidden]
        if text:
            q = text.lower()
            items = [
                m
                for m in items
                if q in m.subject.lower()
                or q in m.sender_name.lower()
                or q in m.sender_email.lower()
                or q in m.display_to.lower()
            ]
        items = sorted(items, key=lambda m: m.date_received or m.date_sent or "", reverse=True)
        if offset:
            items = items[offset:]
        if limit is not None:
            items = items[:limit]
        return items

    def message_count(self, mailbox: int, folder_id: str | None = None) -> int:
        return len(self.messages(mailbox, folder_id, include_hidden=True))

    def message(self, mailbox: int, document_id: int) -> MessageDetail:
        self._summaries(mailbox)
        row_idx = self._doc_rows.get(mailbox, {}).get(document_id)
        summary = next((m for m in self._messages.get(mailbox, []) if m.document_id == document_id), None)
        table = self._table("Message", mailbox)
        if row_idx is None or summary is None or table is None:
            raise EdbExplorerError(f"Message {document_id} not found in mailbox {mailbox}")
        r = next((row for _, row in self._rows(table, start=row_idx, stop=row_idx + 1)), None)
        if r is None:
            raise EdbExplorerError(f"Message {document_id} could not be read")
        props = (
            parse_property_blob(r.get("PropertyBlob"))
            .merge(parse_property_blob(r.get("OffPagePropertyBlob")))
            .merge(parse_property_blob(r.get("LargePropertyValueBlob")))
        )
        named = self.named_properties(mailbox)
        body_text, body_html, body_type = self._decode_body(r, props)
        headers = props.get(P_TRANSPORT_HEADERS)
        recipients = self._recipients(r.get("RecipientList"))
        attachments = self._attachments(mailbox, r.get("SubobjectsBlob"), bool(r.get("HasAttachments")))
        properties = {property_name(k, named): decode_value_for_display(k, v) for k, v in sorted(props.values.items())}
        columns = {
            k: decode_value_for_display(0, v)
            for k, v in r.items()
            if k
            not in (
                "PropertyBlob",
                "OffPagePropertyBlob",
                "LargePropertyValueBlob",
                "NativeBody",
                "RecipientList",
                "SubobjectsBlob",
                "BigFunnelPOI",
                "BigFunnelPartialPOI",
            )
        }
        return MessageDetail(
            summary=summary,
            body_text=body_text,
            body_html=body_html,
            body_type=body_type,
            headers=str(headers) if headers else None,
            recipients=recipients,
            attachments=attachments,
            properties=properties,
            columns=columns,
            display_cc=str(props.get(P_DISPLAY_CC) or ""),
            display_bcc=str(props.get(P_DISPLAY_BCC) or ""),
        )

    def _decode_body(self, r: dict[str, Any], props: PropertyBlob) -> tuple[str | None, str | None, str]:
        native = r.get("NativeBody")
        btype = r.get("BodyType")
        codepage = r.get("CodePage") or 1252
        enc = f"cp{codepage}" if isinstance(codepage, int) and codepage not in (1200, 65001) else "utf-8"
        text: str | None = None
        html: str | None = None
        kind = "none"
        if isinstance(native, bytes | bytearray | memoryview) and native:
            data = bytes(native)
            if btype == 1 or (btype is None and len(data) % 2 == 0 and data[1:2] == b"\x00"):
                kind = "text"
                try:
                    text = data.decode("utf-16-le")
                except UnicodeDecodeError:
                    text = data.decode(enc, "replace")
            elif btype == 3 or data.lstrip()[:1] == b"<":
                kind = "html"
                for candidate in ("utf-8", enc, "latin-1"):
                    try:
                        html = data.decode(candidate)
                        break
                    except (UnicodeDecodeError, LookupError):
                        continue
            elif btype == 2 or data[4:8] in (b"LZFu", b"MELA"):
                kind = "rtf"
                rtf = decompress_rtf(data)
                text = rtf_to_text(rtf)
            else:
                kind = "unknown"
                text = data.decode(enc, "replace")
        if text is None and isinstance(props.get(P_BODY), str):
            text, kind = props.get(P_BODY), kind if kind != "none" else "text"
        if html is None and isinstance(props.get(P_HTML), bytes | str):
            h = props.get(P_HTML)
            html = h if isinstance(h, str) else h.decode(enc, "replace")
            kind = "html" if kind == "none" else kind
        if text is None and isinstance(props.get(P_RTF), bytes):
            text, kind = rtf_to_text(decompress_rtf(props.get(P_RTF))), "rtf"
        return text, html, kind

    def _recipients(self, blob: Any) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        if not isinstance(blob, bytes | bytearray | memoryview):
            return out
        data = bytes(blob)
        pos = 0
        while True:
            i = data.find(b"ProP", pos)
            if i < 0:
                break
            pb = parse_property_blob(data[i:])
            pos = i + 4
            if not pb.values:
                continue
            rtype = pb.get(P_RECIP_TYPE)
            out.append(
                {
                    "type": RECIPIENT_TYPES.get(rtype, str(rtype)) if rtype is not None else "to",
                    "name": pb.get(P_RECIP_DISPLAY) or pb.get(P_RECIP_DISPLAY2) or "",
                    "email": pb.get(P_SMTP) or pb.get(P_EMAIL_ADDRESS) or "",
                    "address_type": pb.get(0x3002) or "",
                }
            )
        return out

    def _attachments(self, mailbox: int, subobjects: Any, has_attachments: bool) -> list[dict[str, Any]]:
        table = self._table("Attachment", mailbox)
        if table is None or not has_attachments:
            return []
        inids = _subobject_inids(subobjects)
        out: list[dict[str, Any]] = []
        for _, r in self._rows(table):
            inid = r.get("Inid")
            if inids and inid not in inids:
                continue
            method = r.get("AttachmentMethod")
            out.append(
                {
                    "inid": inid,
                    "name": r.get("Name") or r.get("ContentType") or f"attachment-{inid}",
                    "content_type": r.get("ContentId") or "",
                    "size": int(r.get("Size") or (len(r["Content"]) if isinstance(r.get("Content"), bytes) else 0)),
                    "method": ATTACH_METHODS.get(method, str(method)),
                    "created": _dt(r.get("CreationTime")),
                    "embedded_message": bool(r.get("IsEmbeddedMessage")),
                    "has_content": isinstance(r.get("Content"), bytes | bytearray | memoryview),
                }
            )
        return out

    def attachment_content(self, mailbox: int, inid: int) -> tuple[str, bytes]:
        table = self._table("Attachment", mailbox)
        if table is None:
            raise EdbExplorerError(f"Mailbox {mailbox} has no attachment table")
        for _, r in self._rows(table):
            if r.get("Inid") == inid:
                content = r.get("Content")
                if not isinstance(content, bytes | bytearray | memoryview):
                    raise EdbExplorerError(f"Attachment {inid} has no inline content")
                return str(r.get("Name") or f"attachment-{inid}"), bytes(content)
        raise EdbExplorerError(f"Attachment {inid} not found in mailbox {mailbox}")

    # -- overview ------------------------------------------------------- #
    def summary(self) -> dict[str, Any]:
        boxes = self.mailboxes()
        return {
            "database": self.db.id,
            "mailboxes": len(boxes),
            "user_mailboxes": sum(1 for m in boxes if not m.is_system),
            "messages": sum(m.message_count for m in boxes),
            "mailbox_list": [m.to_dict() for m in boxes],
        }


def _subobject_inids(blob: Any) -> set[int]:
    """SubobjectsBlob is a compact int sequence: version, count, then (index, inid) pairs."""
    if not isinstance(blob, bytes | bytearray | memoryview):
        return set()
    data = bytes(blob)
    ints: list[int] = []
    pos = 0
    sizes = {0: 0, 1: 1, 2: 2, 3: 4, 4: 8}
    while pos < len(data):
        m = data[pos]
        pos += 1
        n = sizes.get(m & 7, 0)
        ints.append(int.from_bytes(data[pos : pos + n], "little") if n else 0)
        pos += n
    if len(ints) < 2:
        return set()
    count = ints[1]
    tail = ints[-2 * count :] if count and len(ints) >= 2 * count else ints[2:]
    return {tail[i] for i in range(1, len(tail), 2)} if count else set(ints[2:])
