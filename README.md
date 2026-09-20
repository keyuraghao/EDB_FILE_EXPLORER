<p align="center">
  <img src="src/edb_explorer/resources/icon_256.png" width="96" alt="EDB Explorer icon">
</p>

<h1 align="center">EDB Explorer</h1>

<p align="center">
  Cross-platform GUI, CLI and <a href="https://modelcontextprotocol.io">MCP server</a> for exploring Microsoft
  <b>ESE / JET Blue</b> databases - <code>ntds.dit</code>, <code>SRUDB.dat</code>, Exchange <code>.edb</code>,
  <code>WebCacheV01.dat</code>, <code>Windows.edb</code> and any other Extensible Storage Engine file - built for
  digital forensics and incident response.
</p>

<p align="center">
  <a href="https://github.com/keyuraghao/EDB_FILE_EXPLORER/actions/workflows/ci.yml"><img src="https://github.com/keyuraghao/EDB_FILE_EXPLORER/actions/workflows/ci.yml/badge.svg" alt="CI"></a>
  <a href="https://github.com/keyuraghao/EDB_FILE_EXPLORER/releases"><img src="https://img.shields.io/github/v/release/keyuraghao/EDB_FILE_EXPLORER?include_prereleases" alt="Release"></a>
  <img src="https://img.shields.io/badge/python-3.10%20%7C%203.11%20%7C%203.12%20%7C%203.13-blue" alt="Python">
  <img src="https://img.shields.io/badge/platform-Linux%20%7C%20Windows%20%7C%20macOS-lightgrey" alt="Platforms">
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-MIT-green" alt="MIT"></a>
</p>

<p align="center">
  <img src="docs/screenshots/main-dark.png" width="900" alt="EDB Explorer main window">
</p>

---

## Why

Windows stores a surprising amount of evidence in ESE databases: the Active Directory database,
SRUM's per-application network and CPU usage, Exchange mailboxes, browser history, the Windows
Search index, User Access Logging, Windows Update history… Existing tools are either Windows-only,
command-line-only, or built for a single database type.

EDB Explorer opens **any** ESE file, on **Linux, Windows and macOS**, with a pure-Python parser
([`dissect.esedb`](https://github.com/fox-it/dissect.esedb)) - no `esent.dll`, no compilation - and
exposes the same read-only engine three ways:

| Interface | Command | For |
|---|---|---|
| **Desktop GUI** | `edb-explorer gui` | analysts browsing many databases side by side |
| **CLI** | `edb-explorer info / tables / dump / search / export / report …` | scripting and quick triage |
| **MCP server** | `edb-explorer mcp` | AI agents (Claude Desktop, Claude Code, Cursor, any MCP client) |

Everything is **strictly read-only** - the tool never writes to a database file.

## Features

**GUI**
- Open many databases at once (`Ctrl+O`), drag-and-drop files or folders, or scan a directory for
  ESE files by signature (`Ctrl+Shift+O`) - regardless of extension.
- Tables stream in the background in growing batches; filter (substring / regex / case), sort
  numerically or by date, hide empty columns automatically (NTDS `datatable` has 3,500 of them),
  choose columns, copy rows as TSV.
- **Record inspector** with every column of the selected row, a hex dump, and one-click
  interpretations: UTF-16 / UTF-8 / Latin-1 text, SID, GUID (LE/BE), FILETIME, OLE date,
  Unix s/ms/µs, WebKit and Mac timestamps.
- **Known-database profiles** give friendly names and descriptions to cryptic tables
  (SRUM `{973F5D5C-…}` → *Network Data Usage*, NTDS `datatable` → *AD objects*, …).
- Cross-database **search** with jump-to-record, timestamp decoder (`Ctrl+T`), SHA-256 hashing,
  raw file-header view, recent files, dark and light themes.

**Extract** (`Ctrl+E`, or right-click → *Extract selected rows…*)
- Selected rows, displayed (filtered/sorted) rows, a whole table or a whole database.
- Formats: **CSV, XLSX, JSON, JSON Lines, TXT, PDF**. A whole-database XLSX export produces one
  workbook with a sheet per table. Individual binary values can be saved to disk from the inspector.

**Reports** (`Ctrl+R`)
- Analyst report for one or more databases: file metadata, SHA-256, format and shutdown state,
  timestamps, Windows build, detected type, table inventory with record counts, per-table schema
  and sample rows, plus case ID / analyst / notes.
- Formats: **HTML, PDF, DOCX, Markdown, XLSX, TXT, JSON**.

**Value decoding** (shared by all interfaces)
- `DateTime` columns → ISO-8601 UTC (OLE Automation date or FILETIME, whichever is plausible).
- Binary blobs → UTF-16 strings, SIDs (`S-1-5-21-…`), GUIDs or hex, automatically.
- Multi-valued and long-value (separated/compressed) columns handled by the parser.

## Supported databases

| File | Application | Profile |
|---|---|---|
| `ntds.dit` | Active Directory | objects, links, security descriptors |
| `SRUDB.dat` | System Resource Usage Monitor | network, application, energy, connectivity, push notifications |
| `*.edb` (Mailbox Database …) | Exchange Server mailbox store | mailboxes, folders, messages |
| `WebCacheV01.dat` | Internet Explorer / legacy Edge | history, cache, cookies, downloads |
| `Windows.edb` | Windows Search index | property store, gatherer |
| `Current.mdb` | User Access Logging | clients, roles, DNS |
| `DataStore.edb` | Windows Update | history, updates |
| `spartan.edb`, `catdb`, `store.vol` … | Edge, CryptoAPI, Windows Mail | |
| anything else | generic ESE | full schema, raw values |

## Install

### Binaries (no Python needed)

Download the archive for your OS from the [releases page](https://github.com/keyuraghao/EDB_FILE_EXPLORER/releases),
extract it, and run `EDB-Explorer` (GUI) or `edb-explorer` (CLI + MCP).

### From PyPI / source

```bash
pip install "edb-explorer[all]"          # GUI + MCP
pip install "edb-explorer[mcp]"          # headless: CLI + MCP server only (no Qt)

# or from a checkout
git clone https://github.com/keyuraghao/EDB_FILE_EXPLORER && cd edb-explorer
uv sync --all-extras && uv run edb-explorer gui
```

Requires Python 3.10+. On minimal Linux servers the GUI needs the usual Qt runtime libraries
(`libegl1 libgl1 libxkbcommon0 libdbus-1-3 libfontconfig1`).

## Usage

### GUI

```bash
edb-explorer gui                                   # empty window
edb-explorer gui SRUDB.dat ntds.dit "Mailbox Database.edb"
```

| Shortcut | Action |
|---|---|
| `Ctrl+O` / `Ctrl+Shift+O` | open files / scan a folder |
| `Ctrl+F` / `Ctrl+Shift+F` | filter rows in the current table / search every table |
| `Ctrl+E` / `Ctrl+Shift+E` | extract table or view / extract selected rows |
| `Ctrl+R` | generate report |
| `Ctrl+T` | timestamp decoder |
| `Ctrl+W` / `Ctrl+Shift+W` | close tab / close database |

### CLI

```bash
edb-explorer info SRUDB.dat --hash                     # metadata, profile, SHA-256
edb-explorer tables ntds.dit --count                   # table inventory with record counts
edb-explorer schema SRUDB.dat "Network Data Usage"     # columns and indexes (friendly names work)
edb-explorer dump SRUDB.dat SruDbIdMapTable -n 20 -g svchost -f jsonl
edb-explorer search WebCacheV01.dat "login.microsoftonline.com" --regex
edb-explorer export ntds.dit -o out/ -f xlsx           # whole database -> out/ntds.xlsx
edb-explorer export SRUDB.dat -t "Network Data Usage" -o net.pdf -f pdf
edb-explorer report SRUDB.dat ntds.dit -o case.docx --case CASE-2026-001 --analyst "K. Aghao"
edb-explorer scan /mnt/evidence                        # find ESE files by signature
edb-explorer timestamp 132565120200137766              # FILETIME? OLE? Unix? all readings
```

Every command has `--json` output where it makes sense and `--help`.

### MCP server (AI agents)

```bash
edb-explorer mcp                                  # stdio (default) - what Claude Desktop / Claude Code / Cursor use
edb-explorer mcp --allow /cases/001/evidence      # restrict what the agent may open (recommended)
edb-explorer mcp --transport streamable-http --port 8765
edb-explorer mcp SRUDB.dat ntds.dit               # pre-open databases at startup
```

Claude Code:

```bash
claude mcp add edb-explorer -- edb-explorer mcp --allow /cases/001/evidence
```

Claude Desktop / Cursor (`claude_desktop_config.json`, `.cursor/mcp.json`):

```json
{
  "mcpServers": {
    "edb-explorer": {
      "command": "edb-explorer",
      "args": ["mcp", "--allow", "/cases/001/evidence"]
    }
  }
}
```

Docker (headless, Streamable HTTP on `:8765`, evidence mounted read-only):

```bash
docker run --rm -p 8765:8765 -v /cases/001/evidence:/evidence:ro ghcr.io/keyuraghao/EDB_FILE_EXPLORER
```

**Tools exposed** - `open_database`, `scan_directory`, `list_databases`, `close_database`,
`get_database_info`, `list_tables`, `describe_table`, `count_records`, `query_table`
(paging + filters), `get_record`, `get_record_raw` (hex + every decoding), `search`,
`export_table_to_file`, `export_database_to_directory`, `generate_report`,
`interpret_timestamp`, `list_known_profiles`; resources `edb://databases`, `edb://{db}/tables`,
`edb://{db}/{table}/schema`; prompt `triage_database`. See [docs/mcp.md](docs/mcp.md).

An agent conversation typically looks like:

> *"Open /evidence/SRUDB.dat, find which applications sent the most data on 2021-01-30 and write
> a report."* → `open_database` → `list_tables` → `query_table("Network Data Usage", filter_text="2021-01-30")`
> → `query_table("SRUM ID Map", equals={"IdIndex": "491"})` → `generate_report(...)`

## Architecture

```
src/edb_explorer/
├── core/            UI-agnostic engine (no Qt)
│   ├── database.py  EdbDatabase: thread-safe read-only wrapper around dissect.esedb
│   ├── session.py   Session: many open databases, stable ids, path allow-list, directory scan
│   ├── values.py    timestamp / SID / GUID / UTF-16 decoding, hexdump, interpret_timestamp
│   ├── profiles.py  known-database detection and friendly table names
│   ├── search.py    FilterSpec (substring / regex / equals) and cross-table search
│   ├── formats.py   row writers: csv, xlsx, json, jsonl, txt, pdf
│   ├── export.py    streaming table / database / selection export
│   └── report.py    report model + html / pdf / docx / md / xlsx / txt / json renderers
├── gui/             PySide6 application (models, workers, widgets, dialogs, themes)
├── mcp/             MCP server built on the core
└── cli.py           Typer CLI
```

The core has a small surface (`Session`, `EdbDatabase`, `fetch()`, `iter_records()`, `search_database()`,
`export_*()`, `generate_report()`) so it is easy to embed in other tooling:

```python
from edb_explorer.core import Session
from edb_explorer.core.search import FilterSpec

with Session().open("SRUDB.dat") as db:
    print(db.info.profile_name, db.info.state)
    flt = FilterSpec(text="chrome").compile(db.column_types("SruDbIdMapTable"))
    for row in db.fetch("SruDbIdMapTable", row_filter=flt, limit=20).rows:
        print(row["_row"], row["IdBlob"])
```

## Development

```bash
uv sync --all-extras
uv run pytest                                     # 50+ unit tests on a fake ESE backend, no evidence needed
EDB_EXPLORER_TEST_DB=/path/to/any.edb uv run pytest -m integration
uv run ruff check src tests && uv run ruff format src tests && uv run mypy src/edb_explorer/core
./packaging/build.sh                              # Linux/macOS bundle   (Windows: packaging\build.ps1)
```

See [CONTRIBUTING.md](CONTRIBUTING.md) for the layout, how to add a database profile and the
release process, and [SECURITY.md](SECURITY.md) for MCP hardening notes.

## Acknowledgements

- [dissect.esedb](https://github.com/fox-it/dissect.esedb) by Fox-IT - the ESE parser doing the heavy lifting.
- [libesedb](https://github.com/libyal/libesedb) documentation by Joachim Metz for the format reference.
- Built with [PySide6](https://www.qt.io/qt-for-python), [Typer](https://typer.tiangolo.com),
  [MCP Python SDK](https://github.com/modelcontextprotocol/python-sdk), openpyxl, reportlab and python-docx.

## License

MIT - see [LICENSE](LICENSE).
