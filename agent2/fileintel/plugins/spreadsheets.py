# Author: Aarav Shah
# Portfolio: aaravshah1311.is-great.net
# github: github.com/aaravshah1311

"""
agent2/fileintel/plugins/spreadsheets.py
────────────────────────────────────────
Spreadsheets plugin: XLSX, XLS, CSV, ODS, TSV.

Operations
  read ............ first N rows as a preview
  analyze ......... shape, per-column type/null/min/max/mean, sheet names
  formula_audit ... list formula cells (XLSX) and flag error values
  clean ........... drop fully-empty rows/cols, strip whitespace → new file
  convert ......... xlsx↔csv, csv→xlsx, →pdf (table render)
  export_csv ...... dump a sheet to CSV

CSV/TSV use the stdlib `csv` module (always available). XLSX uses openpyxl
(lazy). XLS (xlrd) and ODS (odfpy) degrade gracefully.
"""

from __future__ import annotations

import csv
import statistics
from pathlib import Path

from agent2.fileintel.base import Capability, PluginContext, require
from agent2.fileintel.errors import BackendError, UnsupportedFormat

_SHEET_FORMATS = ["xlsx", "xls", "csv", "ods", "tsv"]
_PREVIEW_ROWS = 50


# ── Load rows (format-agnostic) ──────────────────────────────────────────────────

def _load_rows(path: str, fmt: str, max_rows: int | None = None) -> tuple[list[list], list[str]]:
    """Return (rows, sheet_names). rows is a list of lists (all cells as-is)."""
    if fmt in ("csv", "tsv"):
        delim = "\t" if fmt == "tsv" else ","
        rows = []
        with open(path, encoding="utf-8", errors="replace", newline="") as fh:
            for i, row in enumerate(csv.reader(fh, delimiter=delim)):
                rows.append(row)
                if max_rows and i + 1 >= max_rows:
                    break
        return rows, ["Sheet1"]
    if fmt == "xlsx":
        openpyxl = require("openpyxl")
        wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
        ws = wb[options_sheet(wb)]
        rows = []
        for i, row in enumerate(ws.iter_rows(values_only=True)):
            rows.append([("" if c is None else c) for c in row])
            if max_rows and i + 1 >= max_rows:
                break
        names = wb.sheetnames
        wb.close()
        return rows, names
    if fmt == "xls":
        xlrd = require("xlrd")
        book = xlrd.open_workbook(path)
        sheet = book.sheet_by_index(0)
        rows = [[sheet.cell_value(r, c) for c in range(sheet.ncols)]
                for r in range(min(sheet.nrows, max_rows or sheet.nrows))]
        return rows, book.sheet_names()
    if fmt == "ods":
        # odfpy path
        require("odf", "odfpy")
        from odf.opendocument import load  # type: ignore
        from odf.table import Table, TableRow, TableCell  # type: ignore
        from odf.text import P  # type: ignore
        doc = load(path)
        tables = doc.spreadsheet.getElementsByType(Table)
        if not tables:
            raise BackendError("ODS has no tables")
        rows = []
        for tr in tables[0].getElementsByType(TableRow):
            cells = [
                "".join(str(p) for p in tc.getElementsByType(P))
                for tc in tr.getElementsByType(TableCell)
            ]
            rows.append(cells)
            if max_rows and len(rows) >= max_rows:
                break
        return rows, [t.getAttribute("name") for t in tables]
    raise UnsupportedFormat(f"No spreadsheet loader for '.{fmt}'.")


def options_sheet(wb):
    return wb.sheetnames[0]


def _fmt_of(path: str) -> str:
    from agent2.fileintel.detector import detect
    return detect(path)["format"]


# ── Operations ───────────────────────────────────────────────────────────────────

def op_read(path: str, options: dict, ctx: PluginContext) -> dict:
    fmt = _fmt_of(path)
    ctx.progress("Reading rows…")
    rows, sheets = _load_rows(path, fmt, max_rows=_PREVIEW_ROWS)
    header = rows[0] if rows else []
    return {"format": fmt, "sheets": sheets, "columns": len(header),
            "header": [str(h) for h in header],
            "preview": [[str(c) for c in r] for r in rows[:_PREVIEW_ROWS]],
            "preview_rows": len(rows)}


def op_analyze(path: str, options: dict, ctx: PluginContext) -> dict:
    fmt = _fmt_of(path)
    ctx.progress("Loading data…")
    rows, sheets = _load_rows(path, fmt)
    if not rows:
        return {"rows": 0, "columns": 0, "sheets": sheets}
    header = [str(h) for h in rows[0]]
    data = rows[1:]
    ctx.progress("Profiling columns…")
    cols = []
    for ci, name in enumerate(header):
        values = [r[ci] for r in data if ci < len(r) and str(r[ci]).strip() != ""]
        nums = []
        for v in values:
            try:
                nums.append(float(str(v).replace(",", "")))
            except Exception:
                pass
        col = {"name": name, "non_null": len(values),
               "nulls": len(data) - len(values),
               "type": "numeric" if nums and len(nums) >= len(values) * 0.8 else "text"}
        if nums:
            col.update({"min": min(nums), "max": max(nums),
                        "mean": round(statistics.fmean(nums), 4)})
        cols.append(col)
    return {"sheets": sheets, "rows": len(data), "columns": len(header),
            "column_stats": cols}


def op_formula_audit(path: str, options: dict, ctx: PluginContext) -> dict:
    fmt = _fmt_of(path)
    if fmt != "xlsx":
        raise UnsupportedFormat("formula_audit is only supported for .xlsx")
    openpyxl = require("openpyxl")
    ctx.progress("Scanning formula cells…")
    wb = openpyxl.load_workbook(path, data_only=False)
    formulas, errors = [], []
    for ws in wb.worksheets:
        for row in ws.iter_rows():
            for cell in row:
                v = cell.value
                if isinstance(v, str) and v.startswith("="):
                    formulas.append(f"{ws.title}!{cell.coordinate}: {v}")
                if isinstance(v, str) and v.startswith("#") and v.endswith("!"):
                    errors.append(f"{ws.title}!{cell.coordinate}: {v}")
    wb.close()
    return {"formula_count": len(formulas), "formulas": formulas[:500],
            "error_cells": errors}


def op_clean(path: str, options: dict, ctx: PluginContext) -> dict:
    fmt = _fmt_of(path)
    ctx.progress("Loading data…")
    rows, _ = _load_rows(path, fmt)
    ctx.progress("Removing empty rows/cols and trimming whitespace…")
    # strip each cell; drop fully-empty rows
    cleaned = []
    for r in rows:
        stripped = [str(c).strip() for c in r]
        if any(stripped):
            cleaned.append(stripped)
    # drop fully-empty trailing columns
    width = max((len(r) for r in cleaned), default=0)
    keep = [ci for ci in range(width)
            if any(ci < len(r) and r[ci] for r in cleaned)]
    cleaned = [[r[ci] if ci < len(r) else "" for ci in keep] for r in cleaned]
    out = options.get("output_path") or str(
        Path(path).with_name(Path(path).stem + "_cleaned.csv"))
    with open(out, "w", encoding="utf-8", newline="") as fh:
        csv.writer(fh).writerows(cleaned)
    return {"output_path": out, "rows": len(cleaned),
            "columns": len(keep), "to_format": "csv"}


def op_export_csv(path: str, options: dict, ctx: PluginContext) -> dict:
    fmt = _fmt_of(path)
    ctx.progress("Exporting to CSV…")
    rows, _ = _load_rows(path, fmt)
    out = options.get("output_path") or str(Path(path).with_suffix(".csv"))
    with open(out, "w", encoding="utf-8", newline="") as fh:
        csv.writer(fh).writerows([[str(c) for c in r] for r in rows])
    return {"output_path": out, "rows": len(rows), "to_format": "csv"}


def op_convert(path: str, options: dict, ctx: PluginContext) -> dict:
    to = (options.get("to_format") or "csv").lower().lstrip(".")
    fmt = _fmt_of(path)
    if to == "csv":
        return op_export_csv(path, options, ctx)
    if to == "xlsx":
        openpyxl = require("openpyxl")
        ctx.progress("Building workbook…")
        rows, _ = _load_rows(path, fmt)
        wb = openpyxl.Workbook()
        ws = wb.active
        for r in rows:
            ws.append(list(r))
        out = options.get("output_path") or str(Path(path).with_suffix(".xlsx"))
        wb.save(out)
        return {"output_path": out, "rows": len(rows), "to_format": "xlsx"}
    if to == "pdf":
        # Render a simple table PDF via reportlab.
        rl = require("reportlab.platypus", "reportlab")
        from reportlab.lib.pagesizes import LETTER, landscape  # type: ignore
        ctx.progress("Rendering table PDF…")
        rows, _ = _load_rows(path, fmt, max_rows=1000)
        out = options.get("output_path") or str(Path(path).with_suffix(".pdf"))
        doc = rl.SimpleDocTemplate(out, pagesize=landscape(LETTER))
        table = rl.Table([[str(c)[:40] for c in r] for r in rows])
        doc.build([table])
        return {"output_path": out, "to_format": "pdf"}
    raise BackendError(f"No spreadsheet conversion path to '{to}'.")


def register(reg) -> None:
    reg.register("spreadsheets", _SHEET_FORMATS, [
        Capability("read", op_read, backend="native"),
        Capability("analyze", op_analyze, backend="native"),
        Capability("clean", op_clean, backend="native"),
        Capability("export_csv", op_export_csv, backend="native"),
        Capability("convert", op_convert, backend="native"),
    ])
    reg.register("spreadsheets-xlsx", ["xlsx"], [
        Capability("formula_audit", op_formula_audit, backend="openpyxl"),
    ])
