# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project adheres to
[Semantic Versioning](https://semver.org/).

## [Unreleased]

## [0.6.0] - 2026-09-21

### Changed
- **Tables of any size load completely.** The grid used to stop at a *row limit* (1,000,000 by default) and
  silently show nothing past it. Loading now keeps the first rows of a table in memory (default 250,000) and spills bigger tables into a temporary SQLite cache (`core/rowstore.py`); the grid
  then reads windows of rows from disk, sorting and filtering run as SQL views built in the background, and the
  inspector, copy, *Extract rows as displayed* and *go to row* all work on the full row set. Values round-trip
  exactly (verified row-by-row against the in-memory grid on ESE, EVTX and SRUM evidence, including
  multi-valued and blob columns); the cache is deleted when the tab or the application closes and honours
  `EDB_EXPLORER_CACHE_DIR`.

### Added
- **Settings ▸ Preferences…** (`Ctrl+,`): explains the in-memory row budget and sets it with a slider synced to an
  exact numeric field (10,000 - 5,000,000 rows, presets, live RAM estimate at ~2 KB/row, *Restore default* =
  250,000), plus the disk cache directory with its free space. Replaces the plain *Tools ▸ Set row limit…* prompt.
- **Settings ▸ Keyboard shortcuts…** (`Ctrl+Shift+K`, also in Help): every menu action - including panel toggles
  and theme choices - listed by menu with its current and default key. Rebind by pressing the new combination
  (conflicts are flagged and can be taken over), clear, reset one or all, filter the list, copy it as a
  cheat-sheet. Bindings persist in the settings (`gui/shortcuts.py` registry) and menus / toolbar tooltips
  follow the current keys.

## [0.5.0] - 2026-09-21

### Added
- **Windows Event Log (`.evtx`) support**: Security, System, Application, Sysmon and any other event log opens
  as a native format (detected by its `ElfFile` signature). Records are presented as one **table per channel**,
  with the `System` fields (record id, `TimeCreated`, EventID, level, provider, channel, computer, process/thread,
  user SID …) as stable leading columns and every `EventData` / `UserData` field flattened into its own sortable,
  searchable column. `TimeCreated` feeds the timeline and statistics like any other timestamp; the header exposes
  the format version, chunk count, next record id and the dirty/full flags. Profiles for the **Security**,
  **System**, **Application** and **Sysmon** channels add ready-made analysis views (event-ID summary, successful
  and failed logons, special-privilege assignments, process creation). Parsing uses the `evtx` Rust extension, so
  a 134 MB / 181k-event `Security.evtx` opens in a few seconds.

### Changed
- README screenshots regenerated from a neutral evidence tree (no user names, home directories or case
  identifiers); the embedded-agent screenshot was dropped.

## [0.4.0] - 2026-09-20

### Added
- **Theme switcher**: View ▸ Theme offers *Light*, *Dark* and *Follow system* (tracks OS colour-scheme changes
  while running); a sun / moon toggle at the right end of the toolbar (`Ctrl+Shift+D`) flips between the two.
- **Purpose-specific icons**: a painted icon set (no image assets) with a distinct glyph and colour per action -
  Open, Scan, Search, SQL, Timeline, Mail, Agents, Extract, Report, plus theme, statistics, timestamp decoder,
  filter, run / stop / reload / cancel and the welcome cards - replacing the stock Qt pixmaps that were shared
  between unrelated actions. Toolbar buttons now show labels under their icons; the Exchange mailbox viewer has
  a toolbar button.

## [0.3.1] - 2026-09-20

### Changed
- **ESE decoding is ~2× faster** (record walks, search, export, counts, timeline, statistics) with
  byte-identical results: fixed-width numeric columns are unpacked with `struct` instead of cstruct's
  generic stream path, the per-record `lru_cache` wrappers dissect builds are replaced by a plain dict
  memo, and `as_dict` / `_parse_value` fast paths avoid enum arithmetic and per-column dispatch. Every
  fast path is guarded by a fingerprint of the dissect code it replaces and silently falls back to the
  stock implementation on a dissect upgrade (`ese.FAST_PATHS` reports what is active; the integration
  suite compares fast and stock decoding record by record).
- `Database.iter_records` no longer re-evaluates per-row constants (on a 3,000-column `ntds.dit` this alone
  was half the walk time), skips decoding of rows before `start` when there is no filter, and decodes and
  null-strips in one pass. Counting uses the raw walk.
- Column statistics, timestamp detection, export writers and the SQL materialiser iterate the values a
  sparse row actually has instead of every schema column.
- `display_value` / `normalize_value` short-circuit plain strings and numbers; the UTF-16 heuristic and the
  LevelDB key/value text checks run in C instead of per-byte Python loops; the LevelDB `live` view is
  computed once per database.
- GUI record grid: display strings are only cached for values that are expensive to render (blobs,
  timestamps, lists, long text) - a filtered 10k-row SRUM table now costs ~0.3 KB/row of cache instead of
  ~1.9 KB/row - row indices live in a compact array with bisect lookups instead of a dict, and the filter
  proxy no longer re-sorts the seen-column set for every row.
- SQL dump statement splitting and value tokenising scan between significant characters instead of one
  Python step per character (~3.5× faster on INSERT-heavy dumps).

#### Measurements
Wall-clock on real evidence, same machine, outputs hashed and identical before/after.

| Operation | SRUDB.dat (22k rows) | ntds.dit (18k rows, 3,496-col `datatable`) |
|---|---|---|
| Walk all tables (raw) | 1.46s → 0.84s (1.7×) | 5.05s → 2.40s (2.1×) |
| Walk all tables (decoded) | 1.22s → 0.61s (2.0×) | 3.78s → 2.83s (1.3×) |
| Search, 500 hits | 1.34s → 0.71s (1.9×) | 5.37s → 2.47s (2.2×) |
| Column statistics, 6 tables | 0.11s → 0.07s (1.6×) | 7.35s → 3.36s (2.2×) |
| Timeline | 1.56s → 0.78s (2.0×) | 0.23s → 0.08s (2.7×) |
| Database summary | 0.12s → 0.06s (2.1×) | 0.52s → 0.06s (8.2×) |
| Materialise 8 tables to SQL + query | 1.06s → 0.70s (1.5×) | 0.71s → 0.59s (1.2×) |
| CSV export, 8 tables | 0.84s → 0.42s (2.0×) | 12.21s → 4.89s (2.5×) |
| Count all tables | 1.01s → 0.40s (2.5×) | 5.25s → 2.56s (2.1×) |

Exchange `.edb` open: 0.67s → 0.34s. GUI grid: display cache after filtering a 9.6k-row SRUM table
1,895 → 266 B/row; row-index lookups ~100 MB → 8 MB per million rows; filtering the ntds `datatable`
(831 populated columns) 1.91s → 1.14s. SQL dump (25 MB, 300k rows): statement splitting 13 → 45 MB/s,
full load 4.57s → 2.37s, and all 300,000 rows load (previously 299,800).

### Fixed
- SQL dumps that start with consecutive `--` comment lines (every `mysqldump` file) failed to open with
  `pop from empty list`; a line starting with a single `-` after a comment line was swallowed; `/*/` was
  treated as a complete comment.
- SQL dump statements whose `\'` escape, `--`, `/*` or `*/` straddled a 1 MiB read boundary were mis-split
  and their rows silently dropped; splitting is now independent of the read size. `#` comments follow the
  MySQL rule (anywhere outside a literal), and a CRLF `\.` COPY terminator is recognised.

## [0.3.0] - 2026-09-20

### Added
- **Exchange mailbox viewer** (mail-client layout): mailboxes → folder tree → message list → preview with
  Message / Plain text / Internet headers / Recipients / Attachments / Properties tabs. Messages are decoded
  from the Exchange 2013+ store (`ProP` property blobs, `NativeBody` text/HTML/RTF, recipient blobs,
  attachments via `SubobjectsBlob`), including template-derived per-mailbox tables. Export selected
  messages, a folder or a mailbox as **EML** (with attachments), **HTML**, **TXT** or **JSON**, save
  attachments, export the message list to any tabular format. CLI `mailboxes` / `mail`; MCP
  `exchange_mailboxes`, `exchange_folders`, `exchange_messages`, `exchange_message`, `exchange_export`,
  `exchange_save_attachment`.
- ESE **template tables** are now resolved (dissect.esedb leaves them empty), so every derived table in an
  Exchange database (`Message_N`, `Folder_N`, `Attachment_N`, …) shows its columns and values.
- **AI agents tab**: an embedded terminal (PTY + VT100 emulation) that runs Claude Code, OpenAI Codex CLI,
  Gemini CLI, GitHub Copilot CLI, Aider, a shell or any custom command inside the app, with one-click
  **Configure MCP** (registers this tool's MCP server with the agent, scoped to the open evidence folders),
  **Login** and **Start**. `More ▸ Copy MCP config JSON` for Cursor / VS Code / Windsurf / Claude Desktop.
  CLI `agents [--configure claude|codex|gemini|copilot]`.
- Distinct icons per database format in the database tree (ESE, SQLite, LevelDB, Access, DBF, Berkeley DB,
  SQL dump, BSON) and for mailboxes.
- Compressed RTF (LZFu) decoder and a MAPI property-name table.

### Fixed
- Launching the GUI from the console executable or a bare `edb-explorer` no longer keeps a console window /
  shell prompt busy (Windows: the console is released; Linux/macOS: the process detaches).

## [0.2.0] - 2026-09-20

### Added
- **New formats**: SQLite 3 (WAL-aware, pure-Python parser - no locks on evidence), LevelDB directories
  (Chromium / Electron Local Storage, Session Storage, IndexedDB - own reader incl. deleted and superseded
  records), Microsoft Access `.mdb`/`.accdb`, dBase/FoxPro `.dbf` (soft-deleted records exposed), Berkeley DB,
  SQL dumps (`mysqldump`, `pg_dump` incl. `COPY`, `sqlite .dump`) and `mongodump` `.bson`. Detection is by
  file signature; `scan` finds all of them.
- **52 application profiles** (mobile, desktop, browser, server) with per-column timestamp decoding
  (WebKit, Cocoa, Unix s/ms/µs/ns, FILETIME, OLE) and **artifact views** - ready-made SQL such as Chrome
  browsing history, iOS messages with handles, Android call log, SRUM network usage by application,
  knowledgeC app usage, macOS quarantine downloads.
- **SQL console** over any format (tables materialised into SQLite with decoded values; every open database
  attached as a schema for cross-database joins), in the GUI (`Ctrl+Q`), CLI (`sql`, `views`) and MCP
  (`run_sql`, `list_views`, `run_view`).
- **Timeline** across databases from every detected timestamp column (GUI `Ctrl+L`, CLI `timeline`, MCP
  `timeline`), with date filtering, jump-to-record and extraction.
- **Column statistics** with automatic timestamp-encoding detection (GUI `Ctrl+I`, CLI `stats`, MCP
  `column_statistics`, `detect_timestamps`) and a `database_summary` overview.
- GUI: welcome screen with **Open files / Open recent / Scan folder** cards and a recent-files list; a
  **Tasks** panel with a progress bar per file being opened / table being loaded so other files stay usable;
  **collapse / expand all** buttons in the database tree; *Analysis views* node per database; results grids
  with filtering, copy and multi-format extraction.
- CLI `formats`, `summary`; MCP `list_formats`; `docs/formats.md` generated from the profile registry.

### Changed
- `EdbDatabase` is now an alias of the format-agnostic `Database`; `DatabaseInfo` gained `kind`, `kind_name`,
  `encoding` and `sidecars`.

## [0.1.1] - 2026-09-20

### Added
- Windows installer (`EDB-Explorer-<version>-setup.exe`): Start Menu and desktop shortcuts, optional PATH entry
  and `.edb`/`.dit` file association. The portable zip is still published.
- Linux bundle ships `EDB-Explorer.desktop` and `install.sh` to register the app in the application menu.

### Changed
- Running `edb-explorer` with no command (or double-clicking the executable) now opens the GUI instead of
  printing help.

## [0.1.0] - 2026-09-20

### Added
- PySide6 desktop GUI: open many ESE databases at once, lazy-loading table grids with filtering,
  sorting and column management, a record inspector with hex view and timestamp/SID/GUID
  interpretations, cross-database search, drag-and-drop, folder scanning by file signature,
  dark and light themes.
- Known-database profiles with friendly table names for NTDS.dit, SRUM, Exchange, WebCache,
  Windows Search, User Access Logging, Windows Update and more.
- Extract selected rows, displayed rows, whole tables or whole databases to CSV, XLSX, JSON,
  JSON Lines, TXT and PDF (XLSX exports of a database produce one workbook with a sheet per table).
- Analyst reports in HTML, PDF, DOCX, Markdown, XLSX, TXT and JSON with file metadata, SHA-256,
  table inventory, record counts, schema and sample rows.
- CLI (`edb-explorer info|tables|schema|dump|search|export|report|scan|timestamp|gui|mcp`).
- MCP server (`edb-explorer mcp`) over stdio, SSE or Streamable HTTP with 17 read-only tools,
  resources and a triage prompt, plus a path allow-list for evidence directories.
- PyInstaller packaging for Linux and Windows, Dockerfile for a headless MCP server, GitHub
  Actions CI and release workflows.
