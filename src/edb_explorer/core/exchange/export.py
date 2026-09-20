"""Export Exchange messages as EML / HTML / TXT / JSON (with attachments)."""

from __future__ import annotations

import html
import json
import mimetypes
import os
import re
from collections.abc import Callable
from email import policy
from email.message import EmailMessage
from email.parser import HeaderParser
from email.utils import format_datetime, formataddr
from pathlib import Path
from typing import Any

from edb_explorer.core.exchange.store import ExchangeStore, MessageDetail, MessageSummary

MESSAGE_FORMATS = ("eml", "html", "txt", "json")
_SAFE = re.compile(r"[^A-Za-z0-9._ \-]+")
_SKIP_HEADERS = {"content-type", "content-transfer-encoding", "mime-version", "content-disposition", "content-id"}


def _addr(name: str, email_addr: str) -> str:
    if email_addr and "@" in email_addr:
        return formataddr((name, email_addr)) if name and name != email_addr else email_addr
    return name or email_addr


def _date(iso: str | None) -> str | None:
    if not iso:
        return None
    from datetime import datetime

    try:
        return format_datetime(datetime.fromisoformat(iso))
    except ValueError:
        return iso


def message_to_eml(store: ExchangeStore, detail: MessageDetail, include_attachments: bool = True) -> bytes:
    s = detail.summary
    msg = EmailMessage(policy=policy.SMTP)
    if detail.headers:
        parsed = HeaderParser(policy=policy.default).parsestr(detail.headers)
        for k, v in parsed.items():
            if k.lower() in _SKIP_HEADERS:
                continue
            try:
                msg[k] = str(v)
            except (ValueError, TypeError):
                continue
    if "From" not in msg and (s.sender_name or s.sender_email):
        msg["From"] = _addr(s.sender_name, s.sender_email)
    to = [_addr(r["name"], r["email"]) for r in detail.recipients if r["type"] == "to"]
    cc = [_addr(r["name"], r["email"]) for r in detail.recipients if r["type"] == "cc"]
    bcc = [_addr(r["name"], r["email"]) for r in detail.recipients if r["type"] == "bcc"]
    if "To" not in msg and (to or s.display_to):
        msg["To"] = ", ".join(to) if to else s.display_to
    if "Cc" not in msg and (cc or detail.display_cc):
        msg["Cc"] = ", ".join(cc) if cc else detail.display_cc
    if bcc and "Bcc" not in msg:
        msg["Bcc"] = ", ".join(bcc)
    if "Subject" not in msg:
        msg["Subject"] = s.subject
    if "Date" not in msg:
        d = _date(s.date_sent or s.date_received)
        if d:
            msg["Date"] = d
    if "Message-ID" not in msg and s.internet_message_id:
        msg["Message-ID"] = s.internet_message_id
    msg["X-EDB-Explorer-Mailbox"] = str(s.mailbox)
    msg["X-EDB-Explorer-DocumentId"] = str(s.document_id)
    text = detail.body_text or ""
    if detail.body_html:
        if text:
            msg.set_content(text)
            msg.add_alternative(detail.body_html, subtype="html")
        else:
            msg.set_content(detail.body_html, subtype="html")
    else:
        msg.set_content(text)
    if include_attachments:
        for att in detail.attachments:
            if not att.get("has_content"):
                continue
            try:
                name, data = store.attachment_content(s.mailbox, att["inid"])
            except Exception:
                continue
            ctype = mimetypes.guess_type(name)[0] or "application/octet-stream"
            maintype, subtype = ctype.split("/", 1)
            msg.add_attachment(data, maintype=maintype, subtype=subtype, filename=name)
    return msg.as_bytes()


def message_to_html(detail: MessageDetail) -> str:
    s = detail.summary
    e = html.escape
    rows = [
        ("From", _addr(s.sender_name, s.sender_email)),
        ("To", s.display_to),
        ("Cc", detail.display_cc),
        ("Bcc", detail.display_bcc),
        ("Subject", s.subject),
        ("Sent", s.date_sent or ""),
        ("Received", s.date_received or ""),
        ("Message-ID", s.internet_message_id or ""),
        ("Class", s.message_class),
        ("Size", f"{s.size:,} bytes"),
        ("Mailbox / DocumentId", f"{s.mailbox} / {s.document_id}"),
    ]
    head = "".join(f"<tr><th>{e(k)}</th><td>{e(str(v))}</td></tr>" for k, v in rows if v)
    body = detail.body_html or f"<pre>{e(detail.body_text or '')}</pre>"
    atts = "".join(
        f"<li>{e(str(a['name']))} ({a['size']:,} bytes, {e(str(a['method']))})</li>" for a in detail.attachments
    )
    return (
        "<!DOCTYPE html><html><head><meta charset='utf-8'><title>" + e(s.subject) + "</title>"
        "<style>body{font-family:sans-serif;margin:1.5rem}table.h{border-collapse:collapse;margin-bottom:1rem}"
        "table.h th{text-align:left;padding:2px 10px 2px 0;color:#555}table.h td{padding:2px 0}.body{border-top:1px solid #ccc;padding-top:1rem}</style>"
        f"</head><body><table class='h'>{head}</table>"
        + (f"<p><b>Attachments:</b><ul>{atts}</ul></p>" if atts else "")
        + f"<div class='body'>{body}</div></body></html>"
    )


def message_to_text(detail: MessageDetail) -> str:
    s = detail.summary
    lines = [f"From: {_addr(s.sender_name, s.sender_email)}", f"To: {s.display_to}"]
    if detail.display_cc:
        lines.append(f"Cc: {detail.display_cc}")
    lines += [f"Subject: {s.subject}", f"Sent: {s.date_sent or ''}", f"Received: {s.date_received or ''}"]
    if s.internet_message_id:
        lines.append(f"Message-ID: {s.internet_message_id}")
    if detail.attachments:
        lines.append("Attachments: " + ", ".join(f"{a['name']} ({a['size']:,} bytes)" for a in detail.attachments))
    lines += ["", detail.body_text or _strip_html(detail.body_html or "")]
    return "\n".join(lines)


def _strip_html(text: str) -> str:
    text = re.sub(r"(?is)<(script|style).*?</\1>", "", text)
    text = re.sub(r"(?i)<br\s*/?>|</p>|</div>|</tr>", "\n", text)
    text = re.sub(r"<[^>]+>", "", text)
    return html.unescape(text).strip()


def message_to_json(detail: MessageDetail) -> str:
    return json.dumps(detail.to_dict(), indent=2, ensure_ascii=False, default=str)


def safe_name(text: str, limit: int = 80) -> str:
    return (_SAFE.sub("_", text).strip() or "message")[:limit]


def export_messages(
    store: ExchangeStore,
    messages: list[MessageSummary],
    output_dir: str | os.PathLike[str],
    fmt: str = "eml",
    attachments: bool = True,
    by_folder: bool = True,
    progress: Callable[[int, str], bool] | None = None,
) -> list[Path]:
    """Write each message to ``output_dir`` (optionally under its folder path). Returns written paths."""
    if fmt not in MESSAGE_FORMATS:
        raise ValueError(f"format must be one of {', '.join(MESSAGE_FORMATS)}")
    out = Path(output_dir)
    written: list[Path] = []
    for i, s in enumerate(messages, start=1):
        detail = store.message(s.mailbox, s.document_id)
        target_dir = out
        if by_folder:
            folder = store.folder(s.mailbox, s.folder_id)
            box = store.mailbox(s.mailbox).display_name
            target_dir = (
                out
                / safe_name(box, 60)
                / Path(*[safe_name(p, 60) for p in (folder.path.split("/") if folder else ["unknown"])])
            )
        target_dir.mkdir(parents=True, exist_ok=True)
        stem = f"{s.document_id}_{safe_name(s.subject or 'no subject')}"
        path = target_dir / f"{stem}.{fmt}"
        if fmt == "eml":
            path.write_bytes(message_to_eml(store, detail, attachments))
        elif fmt == "html":
            path.write_text(message_to_html(detail), encoding="utf-8")
        elif fmt == "txt":
            path.write_text(message_to_text(detail), encoding="utf-8")
        else:
            path.write_text(message_to_json(detail), encoding="utf-8")
        written.append(path)
        if attachments and fmt != "eml":
            for att in detail.attachments:
                if att.get("has_content"):
                    try:
                        name, data = store.attachment_content(s.mailbox, att["inid"])
                        (target_dir / f"{stem}_att{att['inid']}_{safe_name(name)}").write_bytes(data)
                    except Exception:
                        continue
        if progress and not progress(i, str(path)):
            break
    return written


def summaries_to_rows(messages: list[MessageSummary]) -> list[dict[str, Any]]:
    return [m.to_dict() for m in messages]
