"""Windows Event Log (``.evtx``) backend.

An EVTX file is a flat stream of binary-XML event records (Security.evtx,
System.evtx, Sysmon `Operational` ...).  Each record shares a fixed ``System``
block (record id, timestamp, EventID, provider, channel, computer ...) and a
provider-specific ``EventData`` / ``UserData`` payload whose fields differ per
EventID.

We present one *table per channel* (a normal single-log file therefore has one
table named after its channel, e.g. ``Security``).  Every row carries the
``System`` columns plus the payload fields flattened one level deep, so
``TargetUserName``, ``LogonType``, ``IpAddress`` and friends become ordinary,
sortable, searchable columns.  The union of payload fields seen in a channel is
computed by a single pass over the file when the catalogue is first read
(mirroring the BSON backend), which also yields an exact record count for free.

Parsing uses the ``evtx`` Rust extension - it is ~30x faster than a pure-Python
BinXML reader (a 134 MB / 181k-record Security.evtx parses in ~1.5 s) and copes
with the template/substitution and chunk-recovery edge cases robustly.
"""

from __future__ import annotations

import json
import logging
import struct
from collections.abc import Iterator
from datetime import datetime
from typing import Any

from edb_explorer.core.backends.base import Backend, BackendInfo, BackendTable, sanitize_columns
from edb_explorer.core.exceptions import InvalidDatabaseError
from edb_explorer.core.models import ColumnInfo

log = logging.getLogger(__name__)

EVTX_MAGIC = b"ElfFile\x00"
# EVTX file header, from offset 0: magic, first/last chunk numbers, next record id, header size, minor/major
# version, header block size, chunk count.  ``flags`` (dirty/full) lives separately at offset 120.
_HEADER = struct.Struct("<8sQQQIHHHH")

#: The stable ``System`` columns, in display order, with the JET-ish type we report for each.
_SYSTEM_COLUMNS: tuple[tuple[str, str], ...] = (
    ("EventRecordID", "LongLong"),
    ("TimeCreated", "DateTime"),
    ("EventID", "Long"),
    ("Level", "Long"),
    ("LevelName", "Text"),
    ("Provider", "Text"),
    ("Channel", "Text"),
    ("Computer", "Text"),
    ("Task", "Long"),
    ("Opcode", "Long"),
    ("Keywords", "Text"),
    ("Version", "Long"),
    ("Correlation_ActivityID", "Text"),
    ("Correlation_RelatedActivityID", "Text"),
    ("Execution_ProcessID", "Long"),
    ("Execution_ThreadID", "Long"),
    ("Security_UserID", "Text"),
    ("ProviderGuid", "Text"),
)
_SYSTEM_NAMES = tuple(name for name, _ in _SYSTEM_COLUMNS)

_LEVELS = {0: "Information", 1: "Critical", 2: "Error", 3: "Warning", 4: "Information", 5: "Verbose"}


def looks_like_evtx(head: bytes) -> bool:
    return head[:8] == EVTX_MAGIC


def _scalar(value: Any) -> Any:
    """Reduce one payload value to a JSON-safe scalar (descending into XML ``#text`` wrappers)."""
    if value is None or isinstance(value, bool | int | float | str):
        return value
    if isinstance(value, dict):
        keys = set(value)
        if "#text" in keys and keys <= {"#text", "#attributes"}:
            return _scalar(value["#text"])  # unwrap <Elem attr=..>text</Elem>; the text may itself be a list
        if keys == {"#attributes"}:  # attribute-only element (e.g. an empty <Data>) - keep the attributes
            return json.dumps(value["#attributes"], ensure_ascii=False, default=str)
        return json.dumps(value, ensure_ascii=False, default=str)
    if isinstance(value, list):
        return json.dumps(value, ensure_ascii=False, default=str)
    return str(value)


def _flatten_payload(event: dict[str, Any]) -> dict[str, Any]:
    """Named ``EventData`` / ``UserData`` fields, flattened one level into ``{name: scalar}``."""
    out: dict[str, Any] = {}
    ed = event.get("EventData")
    if isinstance(ed, dict):
        for k, v in ed.items():
            if k == "#attributes":
                continue
            out[k] = _scalar(v)
    elif ed is not None:
        out["EventData"] = _scalar(ed)
    ud = event.get("UserData")
    if isinstance(ud, dict):
        for wrapper, inner in ud.items():
            if wrapper == "#attributes":
                continue
            if isinstance(inner, dict):
                for k, v in inner.items():
                    if k == "#attributes":
                        continue
                    out.setdefault(k, _scalar(v))
            else:
                out.setdefault(wrapper, _scalar(inner))
    return out


def _parse_time(raw: Any) -> Any:
    """Return a normalised ISO-8601 string (like the DBF/BSON backends) so timeline and stats decode it uniformly."""
    if isinstance(raw, str):
        try:
            return datetime.fromisoformat(raw.replace("Z", "+00:00")).isoformat()
        except ValueError:
            return raw
    if isinstance(raw, datetime):
        return raw.isoformat()
    return raw


def _system_row(event: dict[str, Any], record_id: Any, timestamp: Any) -> dict[str, Any]:
    system = event.get("System") or {}

    def attr(node: Any, key: str) -> Any:
        if isinstance(node, dict):
            return (node.get("#attributes") or {}).get(key)
        return None

    provider = system.get("Provider") or {}
    execution = system.get("Execution") or {}
    correlation = system.get("Correlation") or {}
    level = system.get("Level")
    time_created = attr(system.get("TimeCreated"), "SystemTime")
    row: dict[str, Any] = {
        "EventRecordID": system.get("EventRecordID", record_id),
        "TimeCreated": _parse_time(time_created if time_created is not None else timestamp),
        "EventID": _event_id(system.get("EventID")),
        "Level": level,
        "LevelName": _LEVELS.get(level) if isinstance(level, int) else None,
        "Provider": attr(provider, "Name"),
        "Channel": system.get("Channel"),
        "Computer": system.get("Computer"),
        "Task": system.get("Task"),
        "Opcode": system.get("Opcode"),
        "Keywords": system.get("Keywords"),
        "Version": system.get("Version"),
        "Correlation_ActivityID": attr(correlation, "ActivityID"),
        "Correlation_RelatedActivityID": attr(correlation, "RelatedActivityID"),
        "Execution_ProcessID": attr(execution, "ProcessID"),
        "Execution_ThreadID": attr(execution, "ThreadID"),
        "Security_UserID": attr(system.get("Security"), "UserID"),
        "ProviderGuid": attr(provider, "Guid"),
    }
    return row


def _event_id(node: Any) -> Any:
    # <EventID Qualifiers="..">4624</EventID> is rendered as {"#text": 4624, "#attributes": {...}}.
    if isinstance(node, dict):
        return node.get("#text")
    return node


class EvtxBackend(Backend):
    kind = "evtx"
    kind_name = "Windows Event Log (EVTX)"

    def __init__(self, path: Any) -> None:
        super().__init__(path)
        try:
            from evtx import PyEvtxParser  # noqa: F401
        except ImportError as exc:  # pragma: no cover
            raise InvalidDatabaseError("evtx is not installed (pip install evtx)") from exc
        try:
            with open(self.path, "rb") as fh:
                head = fh.read(4096)
        except OSError as exc:
            raise InvalidDatabaseError(f"{self.path.name}: {exc}") from exc
        if not looks_like_evtx(head):
            raise InvalidDatabaseError(f"{self.path.name}: not an EVTX file")
        self._header = head
        #: channel -> {"columns": [payload col names], "count": int}
        self._channels: dict[str, dict[str, Any]] | None = None
        self._malformed = 0
        # Fail fast on a truncated / unreadable file.
        try:
            next(self._iter_events(), None)
        except Exception as exc:
            raise InvalidDatabaseError(f"{self.path.name}: cannot parse EVTX ({exc})") from exc

    def close(self) -> None:
        pass

    # -- parsing ----------------------------------------------------------- #
    def _iter_events(self) -> Iterator[tuple[Any, Any, dict[str, Any]]]:
        """Yield ``(record_id, timestamp, Event dict)`` for every record, skipping unparseable ones."""
        from evtx import PyEvtxParser

        parser = PyEvtxParser(str(self.path))
        for rec in parser.records_json():
            if rec is None:
                continue
            data = rec.get("data")
            if not data:
                continue
            try:
                event = json.loads(data).get("Event", {})
            except (ValueError, AttributeError):
                self._malformed += 1
                continue
            yield rec.get("event_record_id"), rec.get("timestamp"), event

    def _scan(self) -> None:
        if self._channels is not None:
            return
        channels: dict[str, dict[str, Any]] = {}
        for record_id, timestamp, event in self._iter_events():
            row = _system_row(event, record_id, timestamp)
            channel = row.get("Channel") or self.path.stem
            payload = _flatten_payload(event)
            chan = channels.get(channel)
            if chan is None:
                chan = channels[channel] = {"columns": [], "seen": set(), "count": 0}
            chan["count"] += 1
            seen: set[str] = chan["seen"]
            for name in payload:
                if name not in seen and name not in _SYSTEM_NAMES:
                    seen.add(name)
                    chan["columns"].append(name)
        self._channels = channels

    # -- schema ------------------------------------------------------------ #
    def tables(self) -> list[BackendTable]:
        self._scan()
        assert self._channels is not None
        tables: list[BackendTable] = []
        multichannel = len(self._channels) > 1
        for channel, meta in self._channels.items():
            names = sanitize_columns(list(_SYSTEM_NAMES) + list(meta["columns"]))
            columns: list[ColumnInfo] = []
            sys_types = dict(_SYSTEM_COLUMNS)
            for i, name in enumerate(names):
                base = (list(_SYSTEM_NAMES) + list(meta["columns"]))[i]
                is_sys = i < len(_SYSTEM_NAMES)
                ctype = sys_types[base] if is_sys else "Text"
                columns.append(
                    ColumnInfo(
                        i + 1, name, ctype, 0, "fixed" if is_sys else "tagged", None, "utf-8", ctype == "Text", False
                    )
                )
            extra = {"channel": channel}
            if multichannel:
                extra["description"] = f"Events on the {channel} channel"
            tables.append(BackendTable(name=channel, columns=columns, record_count=meta["count"], extra=extra))
        return tables

    def iter_rows(self, table: str) -> Iterator[dict[str, Any]]:
        self._scan()
        assert self._channels is not None
        single = len(self._channels) == 1
        for record_id, timestamp, event in self._iter_events():
            row = _system_row(event, record_id, timestamp)
            channel = row.get("Channel") or self.path.stem
            if not single and channel != table:
                continue
            for name, value in _flatten_payload(event).items():
                if name not in _SYSTEM_NAMES:
                    row[name] = value
            yield row

    def count(self, table: str) -> int | None:
        self._scan()
        assert self._channels is not None
        if len(self._channels) == 1:
            return next(iter(self._channels.values()))["count"]
        meta = self._channels.get(table)
        return meta["count"] if meta else None

    # -- metadata ---------------------------------------------------------- #
    def info(self) -> BackendInfo:
        self._scan()
        assert self._channels is not None
        _magic, _first, _last, next_record_id, _hsize, minor, major, _block, num_chunks = _HEADER.unpack(
            self._header[: _HEADER.size]
        )
        flags = struct.unpack_from("<I", self._header, 120)[0]
        states = []
        if flags & 0x1:
            states.append("dirty")
        if flags & 0x2:
            states.append("full")
        total = sum(m["count"] for m in self._channels.values())
        state = f"{total:,} events"
        if states:
            state += " (" + ", ".join(states) + ")"
        header: dict[str, Any] = {
            "format_version": f"{major}.{minor}",
            "chunk_count": num_chunks,
            "next_record_id": next_record_id,
            "is_dirty": bool(flags & 0x1),
            "is_full": bool(flags & 0x2),
            "channels": {c: m["count"] for c, m in self._channels.items()},
            "events": total,
        }
        if self._malformed:
            header["unparsed_records"] = self._malformed
        return BackendInfo(
            kind=self.kind,
            kind_name=self.kind_name,
            page_size=0x10000,
            format_version=major,
            format_revision=minor,
            state=state,
            encoding="utf-8",
            windows_version="Windows",
            header=header,
        )
