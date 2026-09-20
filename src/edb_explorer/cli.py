"""Command-line interface: ``edb-explorer <command>``."""

from __future__ import annotations

import json
import logging
import sys
from pathlib import Path
from typing import Annotated, Any

import typer
from rich.console import Console
from rich.table import Table as RichTable

from edb_explorer import __app_name__, __version__
from edb_explorer.core import EdbExplorerError, Session
from edb_explorer.core.analysis import build_timeline, column_statistics, database_summary
from edb_explorer.core.backends import KINDS
from edb_explorer.core.export import EXPORT_FORMATS, export_database, export_rows, export_table
from edb_explorer.core.report import REPORT_FORMATS, ReportOptions, generate_report
from edb_explorer.core.search import FilterSpec, search_database
from edb_explorer.core.sqlworkspace import SqlError, SqlWorkspace
from edb_explorer.core.values import display_value, interpret_timestamp

app = typer.Typer(
    name="edb-explorer",
    help=(
        f"{__app_name__} - explore Microsoft ESE databases (.edb/.dit/.dat) from a GUI, CLI or MCP server.\n\n"
        "Run without a command (or double-click the executable) to open the desktop GUI."
    ),
    invoke_without_command=True,
    rich_markup_mode="rich",
    context_settings={"help_option_names": ["-h", "--help"]},
)
console = Console()
err_console = Console(stderr=True)


def _version_callback(value: bool) -> None:
    if value:
        console.print(f"{__app_name__} {__version__}")
        raise typer.Exit()


@app.callback()
def _root(
    ctx: typer.Context,
    version: Annotated[
        bool, typer.Option("--version", "-V", callback=_version_callback, is_eager=True, help="Show version.")
    ] = False,
    verbose: Annotated[bool, typer.Option("--verbose", "-v", help="Verbose logging to stderr.")] = False,
) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.WARNING,
        stream=sys.stderr,
        format="%(levelname)s %(name)s: %(message)s",
    )
    if ctx.invoked_subcommand is None:
        # No command: behave like a desktop application (double-click / bare `edb-explorer`).
        _launch_gui([])


def _launch_gui(files: list[str]) -> None:
    try:
        from edb_explorer.gui.app import run
    except ImportError as exc:  # pragma: no cover
        _fail(RuntimeError(f"GUI dependencies missing ({exc}). Install with: pip install 'edb-explorer[gui]'"))
    raise typer.Exit(code=run(files))


def _fail(exc: Exception) -> None:
    err_console.print(f"[red]error:[/red] {exc}")
    raise typer.Exit(code=1)


def _open(path: Path) -> Any:
    try:
        return Session().open(path)
    except (EdbExplorerError, OSError) as exc:
        _fail(exc)


# --------------------------------------------------------------------------- #
@app.command()
def gui(
    files: Annotated[list[Path] | None, typer.Argument(help="Database files to open at startup.")] = None,
) -> None:
    """Launch the desktop GUI (also what a bare `edb-explorer` does)."""
    _launch_gui([str(f) for f in files or []])


@app.command()
def mcp(
    files: Annotated[list[Path] | None, typer.Argument(help="Database files to open at startup.")] = None,
    transport: Annotated[str, typer.Option(help="stdio | sse | streamable-http")] = "stdio",
    host: Annotated[str, typer.Option(help="Bind host for HTTP transports.")] = "127.0.0.1",
    port: Annotated[int, typer.Option(help="Bind port for HTTP transports.")] = 8765,
    allow: Annotated[
        list[Path] | None,
        typer.Option("--allow", help="Only permit opening files under this directory (repeatable)."),
    ] = None,
) -> None:
    """Run the MCP server so AI agents (Claude, Cursor, ...) can query databases."""
    try:
        from edb_explorer.mcp.server import run_server
    except ImportError as exc:  # pragma: no cover
        _fail(RuntimeError(f"MCP dependencies missing ({exc}). Install with: pip install 'edb-explorer[mcp]'"))
    run_server(transport, host, port, [str(p) for p in allow] if allow else None, [str(f) for f in files or []])


@app.command()
def info(
    file: Annotated[Path, typer.Argument(exists=True, help="Database file or LevelDB directory.")],
    as_json: Annotated[bool, typer.Option("--json", help="Output JSON.")] = False,
    header: Annotated[bool, typer.Option(help="Include all raw header fields.")] = False,
    hash: Annotated[bool, typer.Option("--hash", help="Compute SHA-256.")] = False,
) -> None:
    """Show database metadata (format, page size, state, timestamps, detected profile)."""
    db = _open(file)
    if hash:
        db.compute_sha256()
    data = db.info.to_dict(include_header=header)
    data["profile_description"] = db.profile.description
    if as_json:
        console.print_json(json.dumps(data, default=str))
        return
    t = RichTable(title=str(db.path), show_header=False, box=None)
    for k, v in data.items():
        if k == "header":
            continue
        t.add_row(f"[bold]{k}[/bold]", "" if v is None else str(v))
    console.print(t)
    if header:
        h = RichTable(title="Header", show_header=False, box=None)
        for k, v in data["header"].items():
            h.add_row(k, str(v))
        console.print(h)


@app.command()
def tables(
    file: Annotated[Path, typer.Argument(exists=True)],
    system: Annotated[bool, typer.Option("--system", help="Include MSys* tables.")] = False,
    count: Annotated[bool, typer.Option("--count", help="Count records (full scan).")] = False,
    as_json: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """List tables."""
    db = _open(file)
    infos = db.tables(system)
    if count:
        for t in infos:
            db.count_records(t.name)
        infos = db.tables(system)
    if as_json:
        console.print_json(json.dumps([t.to_dict(include_columns=False) for t in infos]))
        return
    rt = RichTable(title=f"{db.path.name} - {db.profile.name}")
    rt.add_column("Table")
    rt.add_column("Display name")
    rt.add_column("Columns", justify="right")
    rt.add_column("Indexes", justify="right")
    if count:
        rt.add_column("Records", justify="right")
    for t in infos:
        row = [
            t.name,
            t.display_name if t.display_name != t.name else "",
            str(len(t.columns)),
            str(len(t.indexes)),
        ]
        if count:
            row.append(str(t.record_count))
        rt.add_row(*row)
    console.print(rt)


@app.command()
def schema(
    file: Annotated[Path, typer.Argument(exists=True)],
    table: Annotated[str, typer.Argument(help="Table name.")],
    as_json: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Show the columns and indexes of a table."""
    db = _open(file)
    try:
        t = db.table(table)
    except EdbExplorerError as exc:
        _fail(exc)
    if as_json:
        console.print_json(json.dumps(t.to_dict()))
        return
    rt = RichTable(title=f"{t.name} ({t.display_name})" if t.display_name != t.name else t.name)
    for col in ("ID", "Column", "Type", "Storage", "Size", "Encoding"):
        rt.add_column(col)
    for c in t.columns:
        rt.add_row(str(c.identifier), c.name, c.type, c.storage, str(c.size or ""), c.encoding or "")
    console.print(rt)
    if t.indexes:
        it = RichTable(title="Indexes")
        it.add_column("Index")
        it.add_column("Primary")
        it.add_column("Unique")
        it.add_column("Columns")
        for i in t.indexes:
            it.add_row(i.name, "yes" if i.is_primary else "", "yes" if i.is_unique else "", ", ".join(i.columns))
        console.print(it)


@app.command()
def dump(
    file: Annotated[Path, typer.Argument(exists=True)],
    table: Annotated[str, typer.Argument(help="Table name.")],
    limit: Annotated[int, typer.Option("--limit", "-n", help="Max rows (0 = all).")] = 50,
    offset: Annotated[int, typer.Option(help="Rows to skip.")] = 0,
    columns: Annotated[list[str] | None, typer.Option("--column", "-c", help="Columns to show (repeatable).")] = None,
    grep: Annotated[str | None, typer.Option("--grep", "-g", help="Only rows containing this text.")] = None,
    fmt: Annotated[str, typer.Option("--format", "-f", help="table | json | jsonl | csv")] = "table",
    nulls: Annotated[bool, typer.Option("--nulls", help="Include null columns in JSON output.")] = False,
) -> None:
    """Print records from a table."""
    db = _open(file)
    try:
        row_filter = FilterSpec(text=grep).compile(db.column_types(table))
        types = db.column_types(table)
        cols = columns or db.table(table).column_names
        it = db.iter_records(
            table,
            columns=columns,
            start=offset,
            stop=None if limit == 0 else offset + limit,
            row_filter=row_filter,
            bytes_mode="raw" if fmt in ("table", "csv") else "smart",
            include_nulls=nulls,
        )
        if fmt == "table":
            rt = RichTable(title=table, show_lines=False)
            rt.add_column("#", justify="right")
            for c in cols:
                rt.add_column(c, overflow="fold", max_width=60)
            for index, values in it:
                rt.add_row(str(index), *(display_value(values.get(c), types.get(c), 200) for c in cols))
            console.print(rt)
        elif fmt == "csv":
            import csv

            w = csv.writer(sys.stdout)
            w.writerow(["_row", *cols])
            for index, values in it:
                w.writerow([index, *(display_value(values.get(c), types.get(c), 1_000_000) for c in cols)])
        elif fmt == "jsonl":
            for index, values in it:
                sys.stdout.write(json.dumps({"_row": index, **values}, default=str, ensure_ascii=False) + "\n")
        elif fmt == "json":
            rows = [{"_row": i, **v} for i, v in it]
            sys.stdout.write(json.dumps(rows, default=str, ensure_ascii=False, indent=2) + "\n")
        else:
            _fail(ValueError(f"unknown format {fmt!r}"))
    except EdbExplorerError as exc:
        _fail(exc)


@app.command()
def export(
    file: Annotated[Path, typer.Argument(exists=True)],
    out: Annotated[Path, typer.Option("--out", "-o", help="Output file (single table) or directory.")],
    table: Annotated[list[str] | None, typer.Option("--table", "-t", help="Table(s) to export (default: all).")] = None,
    fmt: Annotated[str, typer.Option("--format", "-f", help=" | ".join(EXPORT_FORMATS))] = "csv",
    system: Annotated[bool, typer.Option("--system", help="Include MSys* tables.")] = False,
) -> None:
    """Extract one table to a file, or every table to a directory (xlsx: one workbook, a sheet per table)."""
    if fmt not in EXPORT_FORMATS:
        _fail(ValueError(f"unknown format {fmt!r}; choose from {', '.join(EXPORT_FORMATS)}"))
    db = _open(file)
    try:
        if table and len(table) == 1 and (out.suffix or not out.is_dir()):
            n = export_table(db, table[0], out, fmt)
            console.print(f"Wrote {n} rows to {out}")
            return
        results = export_database(db, out, fmt, system, table)
        for name, n in results.items():
            console.print(f"{name}: {n} rows")
        console.print(f"[green]Exported {len(results)} tables ({sum(results.values())} rows) to {out}[/green]")
    except EdbExplorerError as exc:
        _fail(exc)


@app.command()
def report(
    files: Annotated[
        list[Path], typer.Argument(exists=True, help="One or more database files (or LevelDB directories).")
    ],
    out: Annotated[
        Path, typer.Option("--out", "-o", help="Output file; format inferred from extension unless --format.")
    ],
    fmt: Annotated[str | None, typer.Option("--format", "-f", help=" | ".join(REPORT_FORMATS))] = None,
    title: Annotated[str | None, typer.Option(help="Report title.")] = None,
    case_id: Annotated[str | None, typer.Option("--case", help="Case identifier.")] = None,
    analyst: Annotated[str | None, typer.Option(help="Analyst name.")] = None,
    notes: Annotated[str | None, typer.Option(help="Free-text notes for the report header.")] = None,
    samples: Annotated[int, typer.Option(help="Sample rows per table (0 = none).")] = 5,
    no_schema: Annotated[bool, typer.Option("--no-schema", help="Omit per-table column listings.")] = False,
    no_count: Annotated[bool, typer.Option("--no-count", help="Skip record counting (faster).")] = False,
    no_hash: Annotated[bool, typer.Option("--no-hash", help="Skip SHA-256 hashing.")] = False,
    system: Annotated[bool, typer.Option("--system", help="Include MSys* tables.")] = False,
    table: Annotated[list[str] | None, typer.Option("--table", "-t", help="Restrict to these tables.")] = None,
) -> None:
    """Generate an analyst report (html, pdf, docx, md, xlsx, txt or json) for one or more databases."""
    session = Session()
    opened, errors = session.open_many(files)
    for path, err in errors.items():
        err_console.print(f"[red]skipped[/red] {path}: {err}")
    if not opened:
        _fail(RuntimeError("no database could be opened"))
    opts = ReportOptions(
        title=title,
        case_id=case_id,
        analyst=analyst,
        notes=notes,
        include_schema=not no_schema,
        include_system=system,
        count_records=not no_count,
        compute_hash=not no_hash,
        sample_rows=samples,
        tables=table,
    )
    try:
        with console.status("Generating report…") as status:

            def _tick(message: str) -> bool:
                status.update(message)
                return True

            report_path = generate_report(opened, out, fmt, opts, _tick)
    except EdbExplorerError as exc:
        _fail(exc)
    console.print(f"[green]Report written to {report_path}[/green]")


@app.command()
def search(
    file: Annotated[Path, typer.Argument(exists=True)],
    query: Annotated[str, typer.Argument(help="Substring (or regex with --regex).")],
    table: Annotated[list[str] | None, typer.Option("--table", "-t")] = None,
    regex: Annotated[bool, typer.Option("--regex", "-r")] = False,
    case_sensitive: Annotated[bool, typer.Option("--case-sensitive", "-s")] = False,
    system: Annotated[bool, typer.Option("--system")] = False,
    limit: Annotated[int, typer.Option("--limit", "-n")] = 100,
    as_json: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Search for text across every table and column."""
    db = _open(file)
    try:
        hits = list(search_database(db, query, table, None, regex, case_sensitive, system, limit))
    except (EdbExplorerError, ValueError) as exc:
        _fail(exc)
    if as_json:
        console.print_json(json.dumps([h.to_dict() for h in hits]))
        return
    rt = RichTable(title=f"{len(hits)} hit(s) for {query!r}")
    for c in ("Table", "Row", "Column", "Value"):
        rt.add_column(c, overflow="fold")
    for h in hits:
        rt.add_row(h.table, str(h.row_index), h.column, h.value)
    console.print(rt)


@app.command()
def scan(
    directory: Annotated[Path, typer.Argument(exists=True, file_okay=False)],
    no_recursive: Annotated[bool, typer.Option("--no-recursive")] = False,
    as_json: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Find supported databases under a directory (by file signature, regardless of extension)."""
    session = Session()
    found = session.scan(directory, recursive=not no_recursive)
    if as_json:
        console.print_json(json.dumps([{"path": str(p), "kind": session.detect(p)} for p in found]))
        return
    for p in found:
        size = sum(f.stat().st_size for f in p.rglob("*") if f.is_file()) if p.is_dir() else p.stat().st_size
        console.print(f"{size:>14,}  {session.detect(p) or '?':<8} {p}")
    console.print(f"[green]{len(found)} database(s) found[/green]")


@app.command()
def sql(
    file: Annotated[Path, typer.Argument(exists=True, help="Database file (or LevelDB directory).")],
    query: Annotated[
        str, typer.Argument(help="SQL to run; tables by their real names, {t:Name} placeholders allowed.")
    ],
    limit: Annotated[int, typer.Option("--limit", "-n", help="Max rows (0 = all).")] = 1000,
    fmt: Annotated[str, typer.Option("--format", "-f", help="table | json | jsonl | csv")] = "table",
    out: Annotated[
        Path | None,
        typer.Option("--out", "-o", help="Write results to a file (csv/xlsx/json/jsonl/txt/pdf by extension)."),
    ] = None,
) -> None:
    """Run read-only SQL against any supported database (tables are materialised into SQLite on demand)."""
    db = _open(file)
    ws = SqlWorkspace()
    try:
        res = ws.query_database(db, query, None if limit == 0 else limit)
    except (SqlError, EdbExplorerError) as exc:
        _fail(exc)
    finally:
        ws.close()
    rows = [dict(zip(res.columns, r, strict=False)) for r in res.rows]
    if out is not None:
        n = export_rows(rows, res.columns, out, out.suffix.lstrip(".") or "csv", title=f"{db.path.name} - query")
        console.print(f"Wrote {n} rows to {out}")
        return
    if fmt == "json":
        console.print_json(json.dumps(rows, default=str))
    elif fmt == "jsonl":
        for r in rows:
            sys.stdout.write(json.dumps(r, default=str, ensure_ascii=False) + "\n")
    elif fmt == "csv":
        import csv

        w = csv.writer(sys.stdout)
        w.writerow(res.columns)
        for rec in res.rows:
            w.writerow([display_value(v, None, 1_000_000) for v in rec])
    else:
        rt = RichTable(title=f"{len(rows)} row(s) in {res.elapsed:.2f}s" + (" (truncated)" if res.truncated else ""))
        for c in res.columns:
            rt.add_column(c, overflow="fold", max_width=60)
        for rec in res.rows:
            rt.add_row(*(display_value(v, None, 200) for v in rec))
        console.print(rt)


@app.command()
def views(
    file: Annotated[Path, typer.Argument(exists=True)],
    view: Annotated[str | None, typer.Argument(help="View id or name to run (omit to list).")] = None,
    limit: Annotated[int, typer.Option("--limit", "-n")] = 1000,
    fmt: Annotated[str, typer.Option("--format", "-f", help="table | json | jsonl | csv")] = "table",
    out: Annotated[Path | None, typer.Option("--out", "-o", help="Write results to a file by extension.")] = None,
) -> None:
    """List or run the artifact views defined for the database's profile (Chrome history, iOS messages, SRUM usage ...)."""
    db = _open(file)
    if view is None:
        rt = RichTable(title=f"{db.path.name} - {db.profile.name}")
        rt.add_column("Id")
        rt.add_column("Name")
        rt.add_column("Description", overflow="fold")
        for v in db.profile.views:
            rt.add_row(v.id, v.name, v.description)
        console.print(rt if db.profile.views else "[yellow]No views for this profile.[/yellow]")
        return
    ws = SqlWorkspace()
    try:
        res = ws.run_view(db, view, None if limit == 0 else limit)
    except (SqlError, EdbExplorerError) as exc:
        _fail(exc)
    finally:
        ws.close()
    rows = [dict(zip(res.columns, r, strict=False)) for r in res.rows]
    if out is not None:
        n = export_rows(rows, res.columns, out, out.suffix.lstrip(".") or "csv", title=f"{db.path.name} - {view}")
        console.print(f"Wrote {n} rows to {out}")
    elif fmt == "json":
        console.print_json(json.dumps(rows, default=str))
    elif fmt == "jsonl":
        for r in rows:
            sys.stdout.write(json.dumps(r, default=str, ensure_ascii=False) + "\n")
    else:
        rt = RichTable(title=f"{view}: {len(rows)} row(s)")
        for c in res.columns:
            rt.add_column(c, overflow="fold", max_width=60)
        for rec in res.rows:
            rt.add_row(*(display_value(v, None, 200) for v in rec))
        console.print(rt)


@app.command()
def stats(
    file: Annotated[Path, typer.Argument(exists=True)],
    table: Annotated[str, typer.Argument()],
    max_rows: Annotated[int, typer.Option("--max-rows", help="Rows to scan (0 = all).")] = 0,
    as_json: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Column statistics: nulls, distinct values, min/max, top values and detected timestamp encodings."""
    db = _open(file)
    try:
        st, n = column_statistics(db, table, max_rows=max_rows or None)
    except EdbExplorerError as exc:
        _fail(exc)
    if as_json:
        console.print_json(json.dumps({"rows_scanned": n, "columns": [x.to_dict() for x in st]}, default=str))
        return
    rt = RichTable(title=f"{table}: {n:,} rows scanned")
    for c in ("Column", "Type", "Non-null", "Nulls", "Distinct", "Min", "Max", "Timestamps", "Top values"):
        rt.add_column(c, overflow="fold", justify="right" if c in ("Non-null", "Nulls", "Distinct") else "left")
    for x in st:
        d = x.to_dict()
        ts = (
            f"{x.timestamp_kind}: {x.timestamp_min} → {x.timestamp_max}"
            if x.timestamp_min
            else (x.timestamp_kind or "")
        )
        rt.add_row(
            x.name,
            x.type,
            f"{x.non_null:,}",
            f"{x.nulls:,}",
            f"{x.distinct:,}" + ("" if x.distinct_exact else "+"),
            str(d["min"] or "")[:40],
            str(d["max"] or "")[:40],
            ts,
            ", ".join(f"{v} ({c})" for v, c in x.top_values[:3]),
        )
    console.print(rt)


@app.command()
def timeline(
    files: Annotated[list[Path], typer.Argument(exists=True, help="One or more databases.")],
    out: Annotated[
        Path | None, typer.Option("--out", "-o", help="Write events to csv/xlsx/json/jsonl/txt/pdf by extension.")
    ] = None,
    start: Annotated[str | None, typer.Option(help="Only events at/after this ISO date.")] = None,
    end: Annotated[str | None, typer.Option(help="Only events at/before this ISO date.")] = None,
    limit: Annotated[int, typer.Option("--limit", "-n", help="Max events.")] = 100_000,
    table: Annotated[list[str] | None, typer.Option("--table", "-t", help="Restrict to these tables.")] = None,
    system: Annotated[bool, typer.Option("--system")] = False,
    fmt: Annotated[str, typer.Option("--format", "-f", help="table | jsonl")] = "table",
) -> None:
    """Build a unified timeline from every timestamp column of every table across the given databases."""
    from datetime import datetime, timezone

    session = Session()
    opened, errors = session.open_many(files)
    for pth, err in errors.items():
        err_console.print(f"[red]skipped[/red] {pth}: {err}")
    if not opened:
        _fail(RuntimeError("no database could be opened"))

    def parse(v: str | None) -> datetime | None:
        if not v:
            return None
        dt = datetime.fromisoformat(v)
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)

    with console.status("Building timeline…") as status:
        events, truncated = build_timeline(
            opened,
            table,
            parse(start),
            parse(end),
            limit,
            include_system=system,
            progress=lambda m: _status_tick(status, m),
        )
    rows = [e.to_dict() for e in events]
    if out is not None:
        n = export_rows(
            rows,
            ["timestamp", "database", "table", "row", "column", "kind", "summary"],
            out,
            out.suffix.lstrip(".") or "csv",
            title="Timeline",
        )
        console.print(f"Wrote {n:,} events to {out}" + (" (limit reached)" if truncated else ""))
        return
    if fmt == "jsonl":
        for r in rows:
            sys.stdout.write(json.dumps(r, ensure_ascii=False) + "\n")
        return
    rt = RichTable(title=f"{len(rows):,} events" + (" (limit reached)" if truncated else ""))
    for c in ("Timestamp", "Database", "Table", "Row", "Column", "Summary"):
        rt.add_column(c, overflow="fold", max_width=70)
    for e in events[:2000]:
        rt.add_row(e.timestamp, e.database, e.table, str(e.row), e.column, e.summary)
    console.print(rt)


@app.command()
def summary(
    file: Annotated[Path, typer.Argument(exists=True)],
    count: Annotated[bool, typer.Option("--count", help="Count records in every table.")] = False,
) -> None:
    """Analysis overview: detected type, tables with timestamp columns and sampled date ranges (JSON)."""
    db = _open(file)
    console.print_json(json.dumps(database_summary(db, count=count), default=str))


@app.command()
def formats() -> None:
    """List the database formats this tool can open."""
    rt = RichTable(title="Supported formats")
    rt.add_column("Kind")
    rt.add_column("Name")
    rt.add_column("Typical files", overflow="fold")
    rt.add_column("Extensions")
    for k in KINDS:
        rt.add_row(k.id, k.name, k.description, " ".join(k.extensions))
    console.print(rt)


@app.command()
def timestamp(value: Annotated[str, typer.Argument(help="Decimal, float or 0x-hex value.")]) -> None:
    """Interpret a numeric timestamp as FILETIME / OLE date / Unix / WebKit and more."""
    try:
        console.print_json(json.dumps(interpret_timestamp(value)))
    except (ValueError, OverflowError) as exc:
        _fail(exc)


def _status_tick(status: Any, message: str) -> bool:
    status.update(message)
    return True


def main() -> None:
    app()


if __name__ == "__main__":
    main()
