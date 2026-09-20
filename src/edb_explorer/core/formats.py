"""Row writers for every export format: csv, json, jsonl, txt, xlsx, pdf.

All writers share one tiny interface so the streaming table export, the GUI
"extract selection" action and the report generator use the same code.
"""

from __future__ import annotations

import csv
import json
import os
import re
from abc import ABC, abstractmethod
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from edb_explorer import __app_name__, __version__
from edb_explorer.core.values import BytesMode, display_value, normalize_value

ExportFormat = Literal["csv", "json", "jsonl", "txt", "xlsx", "pdf"]
EXPORT_FORMATS: tuple[str, ...] = ("csv", "xlsx", "json", "jsonl", "txt", "pdf")
FORMAT_LABELS = {
    "csv": "CSV (comma separated)",
    "xlsx": "Excel workbook (.xlsx)",
    "json": "JSON array",
    "jsonl": "JSON Lines (one object per line)",
    "txt": "Plain text table",
    "pdf": "PDF document",
}

_XLSX_ILLEGAL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")
XLSX_CELL_LIMIT = 32_767
PDF_CELL_LIMIT = 120
PDF_COLUMNS_PER_BLOCK = 8


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


class RowWriter(ABC):
    """Write rows one at a time; ``close`` finalises the file and returns the row count."""

    def __init__(
        self,
        output: str | os.PathLike[str],
        columns: list[str],
        types: dict[str, str] | None = None,
        title: str | None = None,
        bytes_mode: BytesMode = "smart",
        with_row_index: bool = True,
    ) -> None:
        self.output = Path(output)
        self.columns = list(columns)
        self.types = types or {}
        self.title = title or self.output.stem
        self.bytes_mode = bytes_mode
        self.with_row_index = with_row_index
        self.count = 0
        self.output.parent.mkdir(parents=True, exist_ok=True)

    @property
    def header(self) -> list[str]:
        return ["_row", *self.columns] if self.with_row_index else list(self.columns)

    def _text_row(self, row: dict[str, Any], max_len: int = 1_000_000) -> list[str]:
        get, types_get = row.get, self.types.get
        cells = ["" if (v := get(c)) is None else display_value(v, types_get(c), max_len) for c in self.columns]
        return [str(row.get("_row", "")), *cells] if self.with_row_index else cells

    def _json_row(self, row: dict[str, Any]) -> dict[str, Any]:
        obj: dict[str, Any] = {}
        if self.with_row_index and "_row" in row:
            obj["_row"] = row["_row"]
        get, types_get, bytes_mode = row.get, self.types.get, self.bytes_mode
        for c in self.columns:
            v = get(c)
            if v is not None:
                obj[c] = normalize_value(v, types_get(c), bytes_mode)
        return obj

    @abstractmethod
    def write(self, row: dict[str, Any]) -> None: ...

    @abstractmethod
    def close(self) -> int: ...

    def __enter__(self) -> RowWriter:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


class CsvWriter(RowWriter):
    def __init__(self, *a: Any, **kw: Any) -> None:
        super().__init__(*a, **kw)
        self._fh = open(self.output, "w", encoding="utf-8", newline="")  # noqa: SIM115
        self._w = csv.writer(self._fh)
        self._w.writerow(self.header)

    def write(self, row: dict[str, Any]) -> None:
        self._w.writerow(self._text_row(row))
        self.count += 1

    def close(self) -> int:
        self._fh.close()
        return self.count


class JsonWriter(RowWriter):
    def __init__(self, *a: Any, lines: bool = False, **kw: Any) -> None:
        super().__init__(*a, **kw)
        self.lines = lines
        self._fh = open(self.output, "w", encoding="utf-8")  # noqa: SIM115
        if not lines:
            self._fh.write("[\n")

    def write(self, row: dict[str, Any]) -> None:
        text = json.dumps(self._json_row(row), ensure_ascii=False, default=str)
        if self.lines:
            self._fh.write(text + "\n")
        else:
            self._fh.write(("" if self.count == 0 else ",\n") + "  " + text)
        self.count += 1

    def close(self) -> int:
        if not self.lines:
            self._fh.write("\n]\n")
        self._fh.close()
        return self.count


class TxtWriter(RowWriter):
    """Aligned plain-text table (buffers rows to compute column widths)."""

    MAX_WIDTH = 60

    def __init__(self, *a: Any, **kw: Any) -> None:
        super().__init__(*a, **kw)
        self._rows: list[list[str]] = []

    def write(self, row: dict[str, Any]) -> None:
        self._rows.append([c.replace("\t", " ") for c in self._text_row(row, self.MAX_WIDTH)])
        self.count += 1

    def close(self) -> int:
        header = self.header
        widths = [min(self.MAX_WIDTH, len(h)) for h in header]
        for r in self._rows:
            for i, cell in enumerate(r):
                widths[i] = max(widths[i], min(self.MAX_WIDTH, len(cell)))

        def line(cells: list[str]) -> str:
            return " | ".join(c[: widths[i]].ljust(widths[i]) for i, c in enumerate(cells)).rstrip()

        with open(self.output, "w", encoding="utf-8") as fh:
            fh.write(f"{self.title}\n")
            fh.write(f"Generated by {__app_name__} {__version__} on {_now()} - {self.count} row(s)\n\n")
            fh.write(line(header) + "\n")
            fh.write("-+-".join("-" * w for w in widths) + "\n")
            for r in self._rows:
                fh.write(line(r) + "\n")
        return self.count


class XlsxWriter(RowWriter):
    """Streaming Excel writer (openpyxl write-only mode handles millions of rows)."""

    def __init__(self, *a: Any, sheet: str | None = None, workbook: Any = None, **kw: Any) -> None:
        super().__init__(*a, **kw)
        from openpyxl import Workbook
        from openpyxl.cell import WriteOnlyCell
        from openpyxl.styles import Font, PatternFill

        self._owns_workbook = workbook is None
        self._wb = workbook or Workbook(write_only=True)
        self._ws = self._wb.create_sheet(title=safe_sheet_name(sheet or self.title, self._wb))
        self._cell_cls = WriteOnlyCell
        bold = Font(bold=True)
        fill = PatternFill("solid", fgColor="DDE6F0")
        cells = []
        for h in self.header:
            c = WriteOnlyCell(self._ws, value=h)
            c.font = bold
            c.fill = fill
            cells.append(c)
        self._ws.append(cells)
        self._ws.freeze_panes = "A2"

    def _cell(self, v: Any, ctype: str | None) -> Any:
        if v is None:
            return None
        if isinstance(v, bool | int | float) and ctype != "DateTime":
            if isinstance(v, int) and abs(v) >= 2**53:
                return str(v)  # Excel cannot hold full 64-bit integers
            return v
        text = display_value(v, ctype, XLSX_CELL_LIMIT)
        return _XLSX_ILLEGAL.sub("", text)[:XLSX_CELL_LIMIT]

    def write(self, row: dict[str, Any]) -> None:
        get, types_get, cell = row.get, self.types.get, self._cell
        cells = [None if (v := get(c)) is None else cell(v, types_get(c)) for c in self.columns]
        if self.with_row_index:
            cells.insert(0, row.get("_row"))
        self._ws.append(cells)
        self.count += 1

    def close(self) -> int:
        if self._owns_workbook:
            self._wb.save(self.output)
        return self.count


class PdfWriter(RowWriter):
    """Landscape PDF with the table split into blocks of columns so wide tables stay legible."""

    def __init__(self, *a: Any, subtitle: str | None = None, **kw: Any) -> None:
        super().__init__(*a, **kw)
        self.subtitle = subtitle
        self._rows: list[list[str]] = []

    def write(self, row: dict[str, Any]) -> None:
        self._rows.append(self._text_row(row, PDF_CELL_LIMIT))
        self.count += 1

    def close(self) -> int:
        from reportlab.lib import colors
        from reportlab.lib.pagesizes import A4, landscape
        from reportlab.lib.styles import getSampleStyleSheet
        from reportlab.lib.units import mm
        from reportlab.platypus import LongTable, PageBreak, Paragraph, SimpleDocTemplate, Spacer, TableStyle

        styles = getSampleStyleSheet()
        doc = SimpleDocTemplate(
            str(self.output),
            pagesize=landscape(A4),
            leftMargin=12 * mm,
            rightMargin=12 * mm,
            topMargin=12 * mm,
            bottomMargin=12 * mm,
            title=self.title,
            author=f"{__app_name__} {__version__}",
        )
        story: list[Any] = [Paragraph(_esc(self.title), styles["Title"])]
        meta = f"{self.count} row(s), {len(self.columns)} column(s) - generated by {__app_name__} {__version__} on {_now()}"
        if self.subtitle:
            meta = f"{_esc(self.subtitle)}<br/>{meta}"
        story.append(Paragraph(meta, styles["Normal"]))
        story.append(Spacer(1, 6 * mm))

        header = self.header
        blocks = [
            list(range(i, min(i + PDF_COLUMNS_PER_BLOCK, len(header))))
            for i in range(0, len(header), PDF_COLUMNS_PER_BLOCK)
        ]
        # keep the row index column in every block
        if self.with_row_index:
            blocks = [[0, *[c for c in b if c != 0]] for b in blocks]
        style = TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#dde6f0")),
                ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
                ("FONTSIZE", (0, 0), (-1, -1), 6.5),
                ("LEADING", (0, 0), (-1, -1), 8),
                ("GRID", (0, 0), (-1, -1), 0.25, colors.HexColor("#b0b8c0")),
                ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#f5f7fa")]),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("LEFTPADDING", (0, 0), (-1, -1), 3),
                ("RIGHTPADDING", (0, 0), (-1, -1), 3),
            ]
        )
        cell_style = styles["BodyText"].clone("cell", fontSize=6.5, leading=8)
        avail = doc.width
        for bi, block in enumerate(blocks):
            if bi:
                story.append(PageBreak())
                story.append(
                    Paragraph(
                        f"{_esc(self.title)} - columns {block[1] if len(block) > 1 else block[0]}..{block[-1]}",
                        styles["Heading3"],
                    )
                )
            data = [[Paragraph(_esc(header[c]), cell_style) for c in block]]
            for r in self._rows:
                data.append([Paragraph(_esc(r[c]), cell_style) for c in block])
            index_w = 16 * mm if self.with_row_index else 0
            others = max(1, len(block) - (1 if self.with_row_index else 0))
            widths = [index_w if (c == 0 and self.with_row_index) else (avail - index_w) / others for c in block]
            table = LongTable(data, colWidths=widths, repeatRows=1)
            table.setStyle(style)
            story.append(table)
        doc.build(story)
        return self.count


def _esc(text: str) -> str:
    return str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def safe_sheet_name(name: str, workbook: Any = None) -> str:
    cleaned = re.sub(r"[\[\]\:\*\?\/\\]", "_", name)[:31] or "Sheet"
    if workbook is None:
        return cleaned
    existing = {ws.title for ws in workbook.worksheets} if hasattr(workbook, "worksheets") else set()
    base, n = cleaned, 2
    while cleaned in existing:
        suffix = f" ({n})"
        cleaned = base[: 31 - len(suffix)] + suffix
        n += 1
    return cleaned


def make_writer(
    fmt: str,
    output: str | os.PathLike[str],
    columns: list[str],
    types: dict[str, str] | None = None,
    title: str | None = None,
    bytes_mode: BytesMode = "smart",
    **kw: Any,
) -> RowWriter:
    fmt = fmt.lower().lstrip(".")
    if fmt == "csv":
        return CsvWriter(output, columns, types, title, bytes_mode)
    if fmt == "json":
        return JsonWriter(output, columns, types, title, bytes_mode, lines=False)
    if fmt == "jsonl":
        return JsonWriter(output, columns, types, title, bytes_mode, lines=True)
    if fmt == "txt":
        return TxtWriter(output, columns, types, title, bytes_mode)
    if fmt == "xlsx":
        return XlsxWriter(output, columns, types, title, bytes_mode, **kw)
    if fmt == "pdf":
        return PdfWriter(output, columns, types, title, bytes_mode, **kw)
    raise ValueError(f"Unsupported export format: {fmt!r} (choose from {', '.join(EXPORT_FORMATS)})")
