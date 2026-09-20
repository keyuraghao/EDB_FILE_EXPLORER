# Contributing

Thanks for helping improve EDB Explorer. Bug reports, new database profiles and format
improvements are all welcome.

## Development setup

```bash
git clone https://github.com/keyuraghao/edb-explorer
cd edb-explorer
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
  core/        parser wrapper, session, value decoding, profiles, search, export, report  (no Qt)
  gui/         PySide6 application (main window, models, widgets, dialogs, workers)
  mcp/         Model Context Protocol server
  cli.py       Typer command-line interface
packaging/     PyInstaller spec, build scripts, Dockerfile
tests/         pytest suite with a fake ESE backend (tests/conftest.py)
```

Keep the `core` package free of Qt imports so the CLI and MCP server stay lightweight.

## Adding a database profile

Profiles live in `src/edb_explorer/core/profiles.py`. Add a `Profile` with the tables that
identify the database (`signature_tables`), friendly `table_names` and short
`table_descriptions`. Add a case to `tests/test_core.py::test_detect_profile`.

## Pull requests

- One logical change per PR, with tests.
- `ruff check`, `ruff format --check` and `pytest` must pass (CI runs them on Linux, Windows and macOS).
- Update `CHANGELOG.md` under *Unreleased*.
- Never commit evidence files. `.gitignore` blocks `*.edb`, `*.dit`, `*.dat` for that reason.

## Releasing

1. Bump `__version__` in `src/edb_explorer/__init__.py` and `version` in `pyproject.toml`.
2. Move the *Unreleased* notes to a new version heading in `CHANGELOG.md`.
3. Tag: `git tag v0.2.0 && git push --tags`. The release workflow builds the wheel, the Linux
   and Windows bundles and the Docker image, and attaches everything to the GitHub release.
