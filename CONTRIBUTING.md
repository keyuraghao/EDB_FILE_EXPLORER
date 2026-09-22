# Contributing

Thanks for helping improve EDB Explorer. Bug reports, new database profiles and format
improvements are all welcome.

## Development setup

```bash
git clone https://github.com/keyuraghao/EDB_FILE_EXPLORER
cd EDB_FILE_EXPLORER
uv sync --all-extras          # creates .venv with GUI, MCP and dev dependencies
uv run pre-commit install     # optional: lint on commit
```

Run the pieces:

```bash
uv run edb-explorer gui                     # desktop app
uv run edb-explorer mcp                     # MCP server on stdio
uv run edb-explorer info some.edb           # CLI
uv run pytest                               # unit tests (no evidence files needed)
EDB_EXPLORER_TEST_DB=/path/to/SRUDB.dat uv run pytest -m integration
uv run ruff check src tests && uv run ruff format src tests
```

## Project layout

```
src/edb_explorer/
  core/        parser wrapper, session, value decoding, profiles, search, export, report,
               disk row store, project files  (no Qt)
  gui/         PySide6 application (main window, models, widgets, dialogs, workers, shortcuts)
  mcp/         Model Context Protocol server
  portable.py  portable mode (state next to the executable)
  cli.py       Typer command-line interface
packaging/     PyInstaller spec, build scripts (installer, portable, .deb, .dmg), Dockerfile
scripts/       docs generator, synthetic demo data, screenshot generator
tests/         pytest suite with a fake ESE backend and synthetic SQLite / EVTX fixtures
```

Keep the `core` package free of Qt imports so the CLI and MCP server stay lightweight.

## Adding a database profile

ESE profiles live in `src/edb_explorer/core/profiles.py`, every other application in
`profiles_apps.py`. Add a `Profile` with the tables that identify the database
(`signature_tables`), friendly `table_names`, `column_hints` for timestamp columns and
artifact `views`. Add a case to `tests/test_core.py::test_detect_profile` and regenerate
`docs/formats.md` with `python scripts/gen_profiles_doc.py`.

## Pull requests

- One logical change per PR, with tests.
- `ruff check`, `ruff format --check`, `mypy src/edb_explorer/core` and `pytest` must pass (CI runs
  them on Linux, Windows and macOS with Python 3.10-3.13). Keep tests platform-neutral: compare
  paths as `Path` objects, keys through `QKeySequence`, and never assert on wrapped console output.
- Update `CHANGELOG.md` under *Unreleased*.
- Never commit evidence files or screenshots of them. `.gitignore` blocks `*.edb`, `*.dit`, `*.dat`;
  screenshots are generated from synthetic data by `scripts/make_screenshots.py`.

## Releasing

1. Bump `__version__` in `src/edb_explorer/__init__.py` and `version` in `pyproject.toml`
   (`uv sync` refreshes `uv.lock`).
2. Move the *Unreleased* notes to a new version heading in `CHANGELOG.md` (the release notes are
   extracted from that section).
3. Push the commit and **wait for the CI run to be green on every platform**.
4. Only then tag: `git tag -a vX.Y.Z -m "..." && git push origin vX.Y.Z`. The release workflow
   builds the wheel, the Windows installer + portable zip, the Linux tarballs + `.deb`, the macOS
   `.dmg` + portable zip and the Docker image, and attaches everything (with `.sha256` files) to
   the GitHub release.
