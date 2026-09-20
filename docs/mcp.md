# MCP server reference

`edb-explorer mcp` exposes the EDB Explorer engine to any Model Context Protocol client. All
tools are read-only with respect to database files; the two `export_*` tools and
`generate_report` write to paths the agent names.

## Transports

| Transport | Command | Notes |
|---|---|---|
| stdio (default) | `edb-explorer mcp` | used by Claude Desktop, Claude Code, Cursor, Windsurf, … |
| Streamable HTTP | `edb-explorer mcp --transport streamable-http --host 127.0.0.1 --port 8765` | endpoint `http://host:port/mcp` |
| SSE (legacy) | `edb-explorer mcp --transport sse --port 8765` | endpoint `/sse` |

Pre-open files by listing them: `edb-explorer mcp SRUDB.dat ntds.dit`.

## Restricting file access

```
edb-explorer mcp --allow /cases/001/evidence --allow /mnt/images
EDB_EXPLORER_ALLOWED_PATHS=/cases/001/evidence:/mnt/images edb-explorer mcp   # ';' on Windows
```

`open_database` and `scan_directory` refuse paths outside the allow-list with a
`PathNotAllowedError` result.

## Tools

| Tool | Purpose | Key arguments |
|---|---|---|
| `open_database` | open a file, returns metadata and its `id` | `path`, `id?` |
| `scan_directory` | find ESE files by signature | `path`, `recursive`, `max_files` |
| `list_databases` | open databases | |
| `close_database` | release a file | `db` |
| `get_database_info` | header fields, timestamps, profile, optional SHA-256 | `db`, `include_header`, `compute_hash` |
| `list_tables` | tables with friendly names and column counts | `db`, `include_system`, `pattern` |
| `describe_table` | columns (JET type, storage, encoding) and indexes | `db`, `table`, `count` |
| `count_records` | exact record count | `db`, `table` |
| `query_table` | page through rows with filters | `db`, `table`, `limit` (≤1000), `offset`, `columns`, `filter_text`, `column_filters`, `equals`, `regex`, `include_nulls`, `bytes_mode`, `max_value_length` |
| `get_record` | one full record by `_row` | `db`, `table`, `row` |
| `get_record_raw` | hex dump and every decoding of one value | `db`, `table`, `row`, `column` |
| `search` | find text anywhere in a database | `db`, `query`, `tables`, `columns`, `regex`, `limit` |
| `export_table_to_file` | csv / xlsx / json / jsonl / txt / pdf | `db`, `table`, `output_path`, `format`, `columns`, `filter_text`, `limit` |
| `export_database_to_directory` | every table (xlsx → one workbook) | `db`, `output_dir`, `format`, `include_system`, `tables` |
| `generate_report` | html / pdf / docx / md / xlsx / txt / json | `output_path`, `db[]`, `format`, `title`, `case_id`, `analyst`, `notes`, `sample_rows`, `include_schema`, `count_records`, `compute_hash` |
| `interpret_timestamp` | FILETIME / OLE / Unix / WebKit readings | `value` |
| `list_known_profiles` | database types the tool recognises | |

Every table argument accepts the real name, a case-insensitive match, or the friendly display
name (`"Network Data Usage"` for `{973F5D5C-1D90-4944-BE8E-24B94231A174}`). `db` accepts the id,
the file name or the full path.

Errors are returned as `{"error": "<ExceptionName>", "message": "..."}` so the agent can recover.

## Resources and prompts

- `edb://databases` - open databases (JSON)
- `edb://{db}/tables` - table list (JSON)
- `edb://{db}/{table}/schema` - table schema (JSON)
- prompt `triage_database(path)` - guided first look at an unknown file

## Token hygiene

- `query_table` omits null columns and truncates values to `max_value_length` (default 512
  characters, marked with `… [+N chars]`); use `get_record` for full values.
- Binary values are decoded heuristically (`bytes_mode="smart"`): UTF-16 text, SID, GUID, else hex.
- `DateTime` columns are returned as ISO-8601 UTC when decodable.
