# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project adheres to
[Semantic Versioning](https://semver.org/).

## [Unreleased]

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
