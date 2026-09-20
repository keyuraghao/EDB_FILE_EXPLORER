"""Streaming export of tables (or arbitrary row sets) to csv / json / jsonl / txt / xlsx / pdf."""

from __future__ import annotations

import os
import re
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any

from edb_explorer.core.database import EdbDatabase, RowFilter
from edb_explorer.core.exceptions import ExportError
from edb_explorer.core.formats import EXPORT_FORMATS, FORMAT_LABELS, ExportFormat, XlsxWriter, make_writer
from edb_explorer.core.values import BytesMode

ProgressCallback = Callable[[int], bool]  # receives rows written; return False to cancel

_SAFE_NAME = re.compile(r"[^A-Za-z0-9._{}\-]+")

__all__ = [
    "EXPORT_FORMATS",
    "FORMAT_LABELS",
    "ExportFormat",
    "export_database",
    "export_rows",
    "export_table",
    "safe_filename",
]


def safe_filename(name: str) -> str:
    return _SAFE_NAME.sub("_", name)[:150] or "table"


def export_rows(
    rows: Iterable[dict[str, Any]],
    columns: list[str],
    output: str | os.PathLike[str],
    fmt: str = "csv",
    types: dict[str, str] | None = None,
    title: str | None = None,
    bytes_mode: BytesMode = "smart",
    progress: ProgressCallback | None = None,
) -> int:
    """Write an in-memory / iterable row set (e.g. the GUI selection) to ``output``."""
    try:
        with make_writer(fmt, output, columns, types, title, bytes_mode) as w:
            for row in rows:
                w.write(row)
                if progress and w.count % 500 == 0 and not progress(w.count):
                    break
            return w.count
    except OSError as exc:
        raise ExportError(f"Cannot write {output}: {exc}") from exc


def export_table(
    db: EdbDatabase,
    table: str,
    output: str | os.PathLike[str],
    fmt: str = "csv",
    columns: list[str] | None = None,
    row_filter: RowFilter | None = None,
    bytes_mode: BytesMode = "smart",
    limit: int | None = None,
    progress: ProgressCallback | None = None,
    workbook: Any = None,
) -> int:
    """Stream one table to ``output``; returns the number of rows written."""
    real = db.resolve_table_name(table)
    info = db.table(real)
    cols = columns or info.column_names
    types = db.column_types(real)
    # Writers look cells up by name, so projecting every row onto the full column list is wasted work;
    # keep it when a subset was requested or when a filter runs (it must see exactly the projected row).
    wanted = cols if row_filter is not None or (columns and set(columns) != set(types)) else None
    title = f"{db.path.name} - {info.display_name}"
    kw: dict[str, Any] = {}
    if fmt == "xlsx":
        kw = {"sheet": info.display_name, "workbook": workbook}
    elif fmt == "pdf":
        kw = {"subtitle": f"Table {real} from {db.path}"}
    try:
        with make_writer(fmt, output, cols, types, title, bytes_mode, **kw) as w:
            for index, values in db.iter_records(real, columns=wanted, stop=limit, row_filter=row_filter):
                w.write({"_row": index, **values})
                if progress and w.count % 500 == 0 and not progress(w.count):
                    break
            return w.count
    except OSError as exc:
        raise ExportError(f"Cannot write {output}: {exc}") from exc


def export_database(
    db: EdbDatabase,
    output_dir: str | os.PathLike[str],
    fmt: str = "csv",
    include_system: bool = False,
    tables: list[str] | None = None,
    progress: Callable[[str, int], bool] | None = None,
    single_workbook: bool = True,
) -> dict[str, int]:
    """Export every (or the selected) table into ``output_dir``; returns {table: rows}.

    For ``xlsx`` with ``single_workbook`` the whole database becomes one workbook
    (``<name>.xlsx``) with a sheet per table.
    """
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)
    names = [db.resolve_table_name(t) for t in tables] if tables else db.table_names(include_system)
    results: dict[str, int] = {}
    workbook = None
    if fmt == "xlsx" and single_workbook:
        from openpyxl import Workbook

        workbook = Workbook(write_only=True)
    for name in names:
        target = out / f"{safe_filename(name)}.{fmt}"
        cb = (lambda n, _name=name: progress(_name, n)) if progress else None
        results[name] = export_table(db, name, target, fmt, progress=cb, workbook=workbook)
        if progress and not progress(name, results[name]):
            break
    if workbook is not None:
        try:
            workbook.save(out / f"{safe_filename(db.path.stem)}.xlsx")
        except OSError as exc:
            raise ExportError(f"Cannot write workbook: {exc}") from exc
    return results


__all__ += ["XlsxWriter"]
