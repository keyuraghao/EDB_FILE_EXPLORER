# MCP server reference

`edb-explorer mcp` exposes the EDB Explorer engine to any Model Context Protocol client. Every
supported format (ESE, SQLite, LevelDB, Access, DBF, Berkeley DB, SQL and BSON dumps) is available
through the same tools. All tools are read-only with respect to database files; the `export_*`
tools, `generate_report` and `timeline(output_path=…)` write to paths the agent names.

A typical agent session: `open_database` → `database_summary` → `list_views` / `run_view` →
`run_sql` for follow-up questions → `timeline` → `generate_report`.

## Transports

| Transport | Command | Notes |
|---|---|---|
| stdio (default) | `edb-explorer mcp` | used by Claude Desktop, Claude Code, Cursor, Windsurf, … |
| Streamable HTTP | `edb-explorer mcp --transport streamable-http --host 127.0.0.1 --port 8765` | endpoint `http://host:port/mcp` |
| SSE (legacy) | `edb-explorer mcp --transport sse --port 8765` | endpoint `/sse` |

Pre-open files by listing them: `edb-explorer mcp SRUDB.dat ntds.dit`.

## Registering with an agent from the app or CLI

The GUI's **AI agents** tab (or `edb-explorer agents --configure claude|codex|gemini|copilot --allow DIR`)
writes the server into the agent's own configuration:

| Agent | What is written |
|---|---|
| Claude Code | `claude mcp add --scope user edb-explorer -- <edb-explorer> mcp --allow …` |
| OpenAI Codex CLI | `[mcp_servers.edb-explorer]` in `~/.codex/config.toml` |
| Gemini CLI | `mcpServers.edb-explorer` in `~/.gemini/settings.json` |
| GitHub Copilot CLI | `mcpServers.edb-explorer` in `~/.copilot/mcp-config.json` |
| anything else | copy the JSON from *More ▸ Copy MCP config JSON* |

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
| `scan_directory` | find every supported database by signature | `path`, `recursive`, `max_files` |
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
| `interpret_timestamp` | FILETIME / OLE / Unix / WebKit / Cocoa readings | `value` |
| `list_known_profiles` | application profiles (platform, signature tables, views) | |
| `list_formats` | file formats that can be opened | |
| `database_summary` | detected app/platform, tables with timestamps, sampled date range, views - **call this first** | `db`, `count_records` |
| `run_sql` | read-only SQL over any format; other databases attached as `"id"."table"` | `db`, `sql`, `limit` (≤1000), `max_value_length` |
| `list_views` / `run_view` | profile artifact views (Chrome history, iOS messages, SRUM usage …) | `db`, `view`, `limit` |
| `column_statistics` | nulls, distinct, min/max/mean, top values, timestamp kind + range per column | `db`, `table`, `columns`, `max_rows`, `top` |
| `detect_timestamps` | which columns are timestamps and their encoding | `db`, `table` |
| `timeline` | events from every timestamp column across databases, sorted | `db[]`, `tables`, `start`, `end`, `limit`, `output_path` |
| `exchange_mailboxes` | mailboxes of an Exchange database | `db`, `include_system` |
| `exchange_folders` | folder tree with counts | `db`, `mailbox` |
| `exchange_messages` | message list (subject, sender, recipients, dates) | `db`, `mailbox`, `folder_id` / `folder_name`, `text`, `limit`, `offset` |
| `exchange_message` | headers, body, recipients, attachments, MAPI properties | `db`, `mailbox`, `document_id` |
| `exchange_export` | eml / html / txt / json files per message | `db`, `output_dir`, `mailbox`, `folder_id`, `document_ids`, `format` |
| `exchange_save_attachment` | write one attachment to disk | `db`, `mailbox`, `inid`, `output_path` |

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
