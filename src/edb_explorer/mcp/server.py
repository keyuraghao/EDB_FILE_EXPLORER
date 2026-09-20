"""MCP server for EDB Explorer.

Run with ``edb-explorer mcp`` (stdio, the default for Claude Desktop / Claude
Code / Cursor) or ``edb-explorer mcp --transport streamable-http --port 8765``.

Every tool is read-only: the server never writes to a database file.  Use
``--allow`` (or ``EDB_EXPLORER_ALLOWED_PATHS``) to restrict which directories
an agent may open.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path
from typing import Any

from edb_explorer import __app_name__, __version__
from edb_explorer.core import EdbExplorerError, Session
from edb_explorer.core.export import ExportFormat, export_database, export_table
from edb_explorer.core.profiles import PROFILES
from edb_explorer.core.report import REPORT_FORMATS, ReportOptions, generate_report
from edb_explorer.core.search import FilterSpec, search_database
from edb_explorer.core.values import BytesMode, decode_bytes, hexdump, interpret_timestamp

try:  # mcp >= 2.0
    from mcp.server.mcpserver import MCPServer as _Server
except ImportError:  # pragma: no cover - mcp 1.x
    from mcp.server.fastmcp import FastMCP as _Server  # type: ignore[no-redef,attr-defined]

try:
    from mcp_types import ToolAnnotations
except ImportError:  # pragma: no cover - mcp 1.x
    from mcp.types import ToolAnnotations  # type: ignore[no-redef,assignment,unused-ignore]

log = logging.getLogger(__name__)

MAX_LIMIT = 1000
DEFAULT_LIMIT = 50
DEFAULT_MAX_VALUE_LENGTH = 512

READ_ONLY = ToolAnnotations(read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=False)
WRITES_FILES = ToolAnnotations(
    read_only_hint=False, destructive_hint=False, idempotent_hint=True, open_world_hint=False
)

INSTRUCTIONS = f"""\
{__app_name__} {__version__} - read-only access to Microsoft ESE (Extensible Storage Engine) databases:
Active Directory ntds.dit, SRUM SRUDB.dat, Exchange .edb, WebCacheV01.dat, Windows.edb, UAL .mdb and any other
JET Blue database.

Typical workflow:
  1. `open_database(path)` (or `scan_directory` first to find files) -> note the returned `id`.
  2. `list_tables(db)` to see tables with friendly names, then `describe_table(db, table)` for columns.
  3. `query_table(db, table, limit=..., offset=..., filter_text=..., columns=[...])` to page through rows.
     Rows only include non-null columns to save tokens; pass include_nulls=true to see every column.
  4. `search(db, query)` to find a string anywhere in the database.
  5. `interpret_timestamp(value)` when you meet an unknown 64-bit number that may be a FILETIME / OLE date.
  6. `export_table_to_file` / `export_database_to_directory` extract data (csv, xlsx, json, jsonl, txt, pdf) and
     `generate_report` writes an analyst report (html, pdf, docx, md, xlsx, txt, json).

Binary values are decoded heuristically (UTF-16 text, SIDs, GUIDs, else 0x-hex).  `DateTime` columns are
converted to ISO-8601 UTC when they hold an OLE date or FILETIME.  Values longer than `max_value_length`
are truncated with a `… [+N chars]` marker; use `get_record` with a larger limit to read them in full.
"""


def _clamp_limit(limit: int) -> int:
    return max(1, min(int(limit), MAX_LIMIT))


def build_server(session: Session | None = None) -> Any:
    """Create the MCP server bound to ``session`` (a fresh one if omitted)."""
    session = session if session is not None else Session()
    server: Any = _Server(name="edb-explorer", instructions=INSTRUCTIONS, version=__version__)

    def _err(exc: Exception) -> dict[str, Any]:
        return {"error": exc.__class__.__name__, "message": str(exc)}

    # ------------------------------------------------------------------ #
    # Databases
    # ------------------------------------------------------------------ #
    @server.tool(annotations=READ_ONLY)
    def open_database(path: str, id: str | None = None) -> dict[str, Any]:
        """Open an ESE database file (.edb, .dit, .dat, .mdb ...) read-only and return its metadata.

        Args:
            path: Absolute or ~-relative path to the database file.
            id: Optional short identifier to refer to this database in later calls (defaults to the file stem).
        """
        try:
            db = session.open(path, id)
        except (EdbExplorerError, OSError) as exc:
            return _err(exc)
        return db.info.to_dict()

    @server.tool(annotations=READ_ONLY)
    def scan_directory(path: str, recursive: bool = True, max_files: int = 500) -> dict[str, Any]:
        """Find ESE database files under a directory by checking the file magic (not just the extension).

        Args:
            path: Directory to scan.
            recursive: Descend into sub-directories.
            max_files: Stop after this many matches.
        """
        try:
            found = session.scan(path, recursive=recursive, max_files=max_files)
        except (EdbExplorerError, OSError) as exc:
            return _err(exc)
        return {
            "directory": str(Path(path).expanduser().resolve()),
            "count": len(found),
            "files": [{"path": str(p), "size_bytes": p.stat().st_size, "name": p.name} for p in found],
        }

    @server.tool(annotations=READ_ONLY)
    def list_databases() -> dict[str, Any]:
        """List the databases currently open in this session."""
        return {"count": len(session), "databases": [db.info.to_dict() for db in session]}

    @server.tool(annotations=READ_ONLY)
    def close_database(db: str) -> dict[str, Any]:
        """Close an open database and release its file handle.

        Args:
            db: Database id, file name or path as returned by open_database / list_databases.
        """
        try:
            session.close(db)
        except EdbExplorerError as exc:
            return _err(exc)
        return {"closed": db}

    @server.tool(annotations=READ_ONLY)
    def get_database_info(db: str, include_header: bool = True, compute_hash: bool = False) -> dict[str, Any]:
        """Return metadata for an open database: format, page size, shutdown state, timestamps, profile.

        Args:
            db: Database id, file name or path.
            include_header: Include every decoded field of the ESE file header.
            compute_hash: Also compute the SHA-256 of the file (reads the whole file; may take a while).
        """
        try:
            database = session.get(db)
            if compute_hash:
                database.compute_sha256()
        except EdbExplorerError as exc:
            return _err(exc)
        info = database.info.to_dict(include_header=include_header)
        info["profile_description"] = database.profile.description
        return info

    # ------------------------------------------------------------------ #
    # Schema
    # ------------------------------------------------------------------ #
    @server.tool(annotations=READ_ONLY)
    def list_tables(db: str, include_system: bool = False, pattern: str | None = None) -> dict[str, Any]:
        """List tables in a database with friendly names (e.g. SRUM GUID tables) and column counts.

        Record counts are only present for tables that have already been fully scanned; call
        count_records for an exact figure.

        Args:
            db: Database id, file name or path.
            include_system: Include the MSys* catalog tables.
            pattern: Case-insensitive substring to filter table names / display names.
        """
        try:
            database = session.get(db)
        except EdbExplorerError as exc:
            return _err(exc)
        tables = database.tables(include_system)
        if pattern:
            low = pattern.lower()
            tables = [t for t in tables if low in t.name.lower() or low in t.display_name.lower()]
        return {
            "database": database.id,
            "profile": database.profile.name,
            "count": len(tables),
            "tables": [t.to_dict(include_columns=False) for t in tables],
        }

    @server.tool(annotations=READ_ONLY)
    def describe_table(db: str, table: str, count: bool = False) -> dict[str, Any]:
        """Return the schema of a table: columns (name, JET type, storage class, encoding) and indexes.

        Args:
            db: Database id, file name or path.
            table: Table name (exact, case-insensitive, or friendly display name).
            count: Also count the records (full table scan).
        """
        try:
            database = session.get(db)
            if count:
                database.count_records(table)
            info = database.table(table)
        except EdbExplorerError as exc:
            return _err(exc)
        return {"database": database.id, **info.to_dict()}

    @server.tool(annotations=READ_ONLY)
    def count_records(db: str, table: str) -> dict[str, Any]:
        """Count the records in a table (walks the whole B-tree; cached afterwards)."""
        try:
            database = session.get(db)
            n = database.count_records(table)
        except EdbExplorerError as exc:
            return _err(exc)
        return {"database": database.id, "table": database.resolve_table_name(table), "record_count": n}

    # ------------------------------------------------------------------ #
    # Records
    # ------------------------------------------------------------------ #
    @server.tool(annotations=READ_ONLY)
    def query_table(
        db: str,
        table: str,
        limit: int = DEFAULT_LIMIT,
        offset: int = 0,
        columns: list[str] | None = None,
        filter_text: str | None = None,
        column_filters: dict[str, str] | None = None,
        equals: dict[str, str] | None = None,
        regex: bool = False,
        include_nulls: bool = False,
        bytes_mode: BytesMode = "smart",
        max_value_length: int = DEFAULT_MAX_VALUE_LENGTH,
    ) -> dict[str, Any]:
        """Page through the records of a table with optional filtering.

        Each row carries a `_row` index (its position in the table) that can be passed to get_record.
        When a filter is given, offset/limit apply to the *matching* rows.

        Args:
            db: Database id, file name or path.
            table: Table name or display name.
            limit: Rows to return (1-1000).
            offset: Rows to skip.
            columns: Only return these columns (default: all non-null columns).
            filter_text: Case-insensitive substring that must appear in some column of the row.
            column_filters: {column: substring} - every listed column must contain its substring.
            equals: {column: value} - exact match on the displayed value.
            regex: Treat filter_text / column_filters as regular expressions.
            include_nulls: Emit every column even when null.
            bytes_mode: How to render binary values: smart (text/SID/GUID/hex), hex, or base64.
            max_value_length: Truncate long strings to this many characters.
        """
        try:
            database = session.get(db)
            spec = FilterSpec(text=filter_text, columns=column_filters or {}, equals=equals or {}, regex=regex)
            row_filter = spec.compile(database.column_types(table))
            page = database.fetch(
                table,
                offset=max(0, offset),
                limit=_clamp_limit(limit),
                columns=columns,
                row_filter=row_filter,
                bytes_mode=bytes_mode,
                max_length=max_value_length,
                include_nulls=include_nulls,
            )
        except (EdbExplorerError, ValueError) as exc:
            return _err(exc)
        return {"database": database.id, **page.to_dict()}

    @server.tool(annotations=READ_ONLY)
    def get_record(
        db: str,
        table: str,
        row: int,
        include_nulls: bool = False,
        bytes_mode: BytesMode = "smart",
        max_value_length: int | None = None,
    ) -> dict[str, Any]:
        """Fetch a single record by its `_row` index with full (untruncated) values.

        Args:
            db: Database id, file name or path.
            table: Table name or display name.
            row: Zero-based row index as returned in `_row`.
            include_nulls: Emit every column even when null.
            bytes_mode: smart | hex | base64.
            max_value_length: Optional truncation length (default: no truncation).
        """
        try:
            database = session.get(db)
            record = database.get_record(table, row, bytes_mode, max_value_length, include_nulls)
        except EdbExplorerError as exc:
            return _err(exc)
        if record is None:
            return {"error": "NotFound", "message": f"Row {row} does not exist in {table}"}
        return {"database": database.id, "table": database.resolve_table_name(table), "record": record}

    @server.tool(annotations=READ_ONLY)
    def get_record_raw(db: str, table: str, row: int, column: str, max_bytes: int = 4096) -> dict[str, Any]:
        """Return one binary/text value as hex, a hexdump and every decoding attempt (UTF-16, SID, GUID...).

        Args:
            db: Database id, file name or path.
            table: Table name or display name.
            row: Zero-based row index.
            column: Column to inspect.
            max_bytes: Cap on bytes returned in the hexdump.
        """
        try:
            database = session.get(db)
            record = database.get_raw_record(table, row)
        except EdbExplorerError as exc:
            return _err(exc)
        if record is None or column not in record:
            return {"error": "NotFound", "message": f"Row {row} / column {column!r} not found"}
        value = record[column]
        out: dict[str, Any] = {"table": database.resolve_table_name(table), "row": row, "column": column}
        if isinstance(value, bytes | bytearray | memoryview):
            data = bytes(value)
            out.update(
                length=len(data),
                hex=data[:max_bytes].hex(),
                hexdump=hexdump(data, max_bytes=max_bytes),
                smart=decode_bytes(data, "smart"),
                utf16le=_try(lambda: data.decode("utf-16-le", "replace").rstrip("\x00")),
                utf8=_try(lambda: data.decode("utf-8", "replace").rstrip("\x00")),
                latin1=_try(lambda: data.decode("latin-1")),
            )
            if len(data) in (4, 8):
                out["as_timestamp"] = interpret_timestamp(data)
        else:
            out["value"] = value if isinstance(value, int | float | str | list | type(None)) else str(value)
            if isinstance(value, int):
                out["as_timestamp"] = interpret_timestamp(value)
        return out

    # ------------------------------------------------------------------ #
    # Search / export / helpers
    # ------------------------------------------------------------------ #
    @server.tool(annotations=READ_ONLY)
    def search(
        db: str,
        query: str,
        tables: list[str] | None = None,
        columns: list[str] | None = None,
        regex: bool = False,
        case_sensitive: bool = False,
        include_system: bool = False,
        limit: int = 100,
    ) -> dict[str, Any]:
        """Search for a string (or regex) across all tables and columns of a database.

        Returns the table, row index, column and matching value for each hit so you can follow up
        with get_record.  Large databases may take a while; narrow with `tables`.

        Args:
            db: Database id, file name or path.
            query: Substring or regular expression.
            tables: Restrict the search to these tables.
            columns: Restrict the search to these column names.
            regex: Interpret query as a regular expression.
            case_sensitive: Match case exactly.
            include_system: Also search MSys* tables.
            limit: Maximum hits to return (1-1000).
        """
        try:
            database = session.get(db)
            hits = list(
                search_database(
                    database,
                    query,
                    tables,
                    columns,
                    regex,
                    case_sensitive,
                    include_system,
                    _clamp_limit(limit),
                )
            )
        except (EdbExplorerError, ValueError) as exc:
            return _err(exc)
        return {
            "database": database.id,
            "query": query,
            "count": len(hits),
            "hits": [h.to_dict() for h in hits],
        }

    @server.tool(annotations=WRITES_FILES)
    def export_table_to_file(
        db: str,
        table: str,
        output_path: str,
        format: ExportFormat = "csv",
        columns: list[str] | None = None,
        filter_text: str | None = None,
        limit: int | None = None,
    ) -> dict[str, Any]:
        """Export a table to CSV, JSON or JSON Lines on disk.

        Args:
            db: Database id, file name or path.
            table: Table name or display name.
            output_path: Destination file path (parent directories are created).
            format: csv | json | jsonl.
            columns: Only export these columns.
            filter_text: Only export rows containing this substring.
            limit: Maximum rows to write.
        """
        try:
            database = session.get(db)
            row_filter = FilterSpec(text=filter_text).compile(database.column_types(table))
            n = export_table(database, table, output_path, format, columns, row_filter, limit=limit)
        except (EdbExplorerError, OSError) as exc:
            return _err(exc)
        return {
            "table": database.resolve_table_name(table),
            "output_path": str(Path(output_path).resolve()),
            "format": format,
            "rows_written": n,
        }

    @server.tool(annotations=WRITES_FILES)
    def export_database_to_directory(
        db: str,
        output_dir: str,
        format: ExportFormat = "csv",
        include_system: bool = False,
        tables: list[str] | None = None,
    ) -> dict[str, Any]:
        """Extract every table (or a chosen subset) of a database into a directory, one file per table.

        With format=xlsx the whole database becomes a single workbook with one sheet per table.
        Formats: csv | xlsx | json | jsonl | txt | pdf.
        """
        try:
            database = session.get(db)
            results = export_database(database, output_dir, format, include_system, tables)
        except (EdbExplorerError, OSError) as exc:
            return _err(exc)
        return {
            "database": database.id,
            "output_dir": str(Path(output_dir).resolve()),
            "format": format,
            "tables": results,
            "total_rows": sum(results.values()),
        }

    @server.tool(name="generate_report", annotations=WRITES_FILES)
    def generate_report_tool(
        output_path: str,
        db: list[str] | None = None,
        format: str | None = None,
        title: str | None = None,
        case_id: str | None = None,
        analyst: str | None = None,
        notes: str | None = None,
        sample_rows: int = 5,
        include_schema: bool = True,
        count_records: bool = True,
        compute_hash: bool = True,
        include_system: bool = False,
        tables: list[str] | None = None,
    ) -> dict[str, Any]:
        """Write an analyst report describing one or more open databases.

        The report covers file metadata (size, SHA-256, format, shutdown state, timestamps, Windows build),
        the detected application profile, a table inventory with record counts, optional per-table schema,
        and sample rows.  Formats: html | pdf | docx | md | xlsx | txt | json.

        Args:
            output_path: Destination file; the format is inferred from its extension unless `format` is given.
            db: Database ids to include (default: every open database).
            format: html | pdf | docx | md | xlsx | txt | json.
            title: Report title.
            case_id: Case identifier printed in the header.
            analyst: Analyst name printed in the header.
            notes: Free-text notes for the report header.
            sample_rows: Sample rows per table (0 disables).
            include_schema: Include column/index listings for each table.
            count_records: Count records in every table (full scan of each table).
            compute_hash: Compute SHA-256 of each file.
            include_system: Include MSys* tables.
            tables: Restrict the report to these tables.
        """
        try:
            dbs = [session.get(d) for d in db] if db else list(session)
            if not dbs:
                return {"error": "NoDatabases", "message": "Open a database first."}
            opts = ReportOptions(
                title=title,
                case_id=case_id,
                analyst=analyst,
                notes=notes,
                include_schema=include_schema,
                include_system=include_system,
                count_records=count_records,
                compute_hash=compute_hash,
                sample_rows=max(0, sample_rows),
                tables=tables,
            )
            path = generate_report(dbs, output_path, format, opts)
        except (EdbExplorerError, OSError, ValueError) as exc:
            return _err(exc)
        return {
            "output_path": str(path),
            "format": path.suffix.lstrip("."),
            "databases": [d.id for d in dbs],
            "available_formats": list(REPORT_FORMATS),
        }

    @server.tool(name="interpret_timestamp", annotations=READ_ONLY)
    def interpret_timestamp_tool(value: str) -> dict[str, Any]:
        """Decode an unknown numeric timestamp every plausible way (FILETIME, OLE date, Unix s/ms/us, WebKit...).

        Args:
            value: Decimal integer, float, or 0x-prefixed hex string.
        """
        try:
            return interpret_timestamp(value)
        except (ValueError, OverflowError) as exc:
            return _err(exc)

    @server.tool(annotations=READ_ONLY)
    def list_known_profiles() -> dict[str, Any]:
        """List the database types EDB Explorer recognises and how it identifies them."""
        return {
            "profiles": [
                {
                    "id": p.id,
                    "name": p.name,
                    "description": p.description,
                    "signature_tables": list(p.signature_tables),
                    "file_hints": list(p.file_hints),
                }
                for p in PROFILES
            ]
        }

    # ------------------------------------------------------------------ #
    # Resources
    # ------------------------------------------------------------------ #
    @server.resource("edb://databases", mime_type="application/json")
    def databases_resource() -> str:
        """Open databases as JSON."""
        import json

        return json.dumps([d.info.to_dict() for d in session], indent=2)

    @server.resource("edb://{db}/tables", mime_type="application/json")
    def tables_resource(db: str) -> str:
        """Tables of a database as JSON."""
        import json

        return json.dumps([t.to_dict(include_columns=False) for t in session.get(db).tables()], indent=2)

    @server.resource("edb://{db}/{table}/schema", mime_type="application/json")
    def schema_resource(db: str, table: str) -> str:
        """Schema of a table as JSON."""
        import json

        return json.dumps(session.get(db).table(table).to_dict(), indent=2)

    @server.prompt()
    def triage_database(path: str) -> str:
        """Guided forensic triage of an ESE database."""
        return (
            f"Open the ESE database at {path} with open_database, identify what application produced it, list its "
            "tables, and summarise the most forensically relevant tables (users, timestamps, paths, network activity). "
            "Sample a few rows from each with query_table, decode any timestamps you are unsure about with "
            "interpret_timestamp, and finish with a concise analyst-style summary of what the database contains."
        )

    server._edb_session = session  # handy for tests / embedding
    return server


def _try(fn: Any) -> Any:
    try:
        return fn()
    except Exception as exc:
        return f"<{exc.__class__.__name__}>"


def run_server(
    transport: str = "stdio",
    host: str = "127.0.0.1",
    port: int = 8765,
    allowed_paths: list[str] | None = None,
    preload: list[str] | None = None,
) -> None:
    logging.basicConfig(level=logging.INFO, stream=sys.stderr, format="%(levelname)s %(name)s: %(message)s")
    session = Session(allowed_roots=list(allowed_paths) if allowed_paths else None)
    if preload:
        opened, errors = session.open_many(list(preload))
        for db in opened:
            log.info("Preloaded %s as %s", db.path, db.id)
        for path, err in errors.items():
            log.error("Could not open %s: %s", path, err)
    server = build_server(session)
    try:
        if transport == "stdio":
            server.run("stdio")
        elif transport == "sse":
            server.run("sse", host=host, port=port)
        else:
            server.run("streamable-http", host=host, port=port)
    finally:
        session.close_all()


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="edb-explorer-mcp", description="EDB Explorer MCP server")
    parser.add_argument("--transport", choices=["stdio", "sse", "streamable-http"], default="stdio")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument(
        "--allow",
        action="append",
        default=None,
        metavar="DIR",
        help="Only permit opening files under DIR (repeatable). "
        "Also honours EDB_EXPLORER_ALLOWED_PATHS (os.pathsep-separated).",
    )
    parser.add_argument("files", nargs="*", help="Database files to open at startup")
    args = parser.parse_args(argv)
    allowed = list(args.allow or [])
    env = os.environ.get("EDB_EXPLORER_ALLOWED_PATHS")
    if env:
        allowed.extend(p for p in env.split(os.pathsep) if p)
    run_server(args.transport, args.host, args.port, allowed or None, args.files)


if __name__ == "__main__":
    main()
