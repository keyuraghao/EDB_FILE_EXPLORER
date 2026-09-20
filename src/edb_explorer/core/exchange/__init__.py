"""Exchange mailbox database support: property blobs, mailboxes, folders, messages, attachments, export."""

from __future__ import annotations

from edb_explorer.core.exchange.props import PropertyBlob, parse_property_blob, property_name
from edb_explorer.core.exchange.store import (
    ExchangeStore,
    FolderInfo,
    MailboxInfo,
    MessageDetail,
    MessageSummary,
    is_exchange_database,
)

__all__ = [
    "ExchangeStore",
    "FolderInfo",
    "MailboxInfo",
    "MessageDetail",
    "MessageSummary",
    "PropertyBlob",
    "is_exchange_database",
    "parse_property_blob",
    "property_name",
]
