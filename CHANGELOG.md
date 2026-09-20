# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project adheres to
[Semantic Versioning](https://semver.org/).

## [Unreleased]

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
