"""Evidence-first XLSX export.  This module never invents a price or a value."""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import json
import os
from pathlib import Path
import tempfile
from typing import Any
from urllib.parse import urlparse
import zipfile

import xlsxwriter

from .models import CollectionConfig


SHEETS = ("Run Summary", "Collection Status", "Price Comparison", "Observation History", "Source Evidence", "Review Required")


def _text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
    return str(value)


def _utc(value: Any) -> str:
    """Make the UTC basis visible without guessing a missing time zone."""
    if value is None or value == "":
        return ""
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.isoformat() + " (timezone unknown)"
        return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    return _text(value)


def _as_datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc) if value.tzinfo else None
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed.astimezone(timezone.utc) if parsed.tzinfo else None
    except ValueError:
        return None


def _write_utc(ws: Any, row: int, col: int, value: Any, date_fmt: Any, text_fmt: Any) -> None:
    parsed = _as_datetime(value)
    if parsed is not None:
        # Excel dates are timezone-naive. The column title states UTC explicitly.
        ws.write_datetime(row, col, parsed.replace(tzinfo=None), date_fmt)
    else:
        _write_value(ws, row, col, _utc(value), text_fmt)


def _safe_url(value: Any) -> str | None:
    value = _text(value).strip()
    if urlparse(value).scheme.lower() in {"http", "https"}:
        return value
    return None


def _decimal(value: Any) -> Decimal | None:
    if value is None or value == "":
        return None
    try:
        result = Decimal(str(value))
        return result if result.is_finite() else None
    except (InvalidOperation, ValueError):
        return None


def _write_decimal(ws: Any, row: int, col: int, value: Any, number_fmt: Any) -> None:
    """Use a numeric cell when Excel can preserve it; always keep raw elsewhere."""
    dec = _decimal(value)
    if dec is None:
        ws.write_blank(row, col, None)
        return
    # Excel stores IEEE-754 numbers.  Avoid silently rounding long Decimal IDs/prices.
    digits = len(dec.as_tuple().digits)
    if digits <= 15:
        ws.write_number(row, col, float(dec), number_fmt)
    else:
        ws.write_blank(row, col, None)


def _write_value(ws: Any, row: int, col: int, value: Any, text_fmt: Any | None = None) -> None:
    if value is None or value == "":
        ws.write_blank(row, col, None, text_fmt)
        return
    ws.write_string(row, col, _text(value), text_fmt)


def _setup_sheet(ws: Any, headers: list[str], formats: dict[str, Any], demo: bool) -> int:
    start = 0
    if demo:
        ws.merge_range(0, 0, 0, max(0, len(headers) - 1), "Demo data — not market prices", formats["banner"])
        start = 1
    for col, header in enumerate(headers):
        ws.write(start, col, header, formats["header"])
        ws.set_column(col, col, 16)
    ws.freeze_panes(start + 1, 0)
    return start + 1


def _finish_sheet(ws: Any, header_row: int, data_end: int, last_col: int) -> None:
    """Filter exactly the materialized table, not all blank worksheet rows."""
    ws.autofilter(header_row, 0, max(header_row, data_end - 1), last_col)


def _raw_price(obs: dict[str, Any]) -> Any:
    raw = obs.get("raw_fields")
    return raw.get("price") if isinstance(raw, dict) else None


def _validate_xlsx(path: Path) -> None:
    with zipfile.ZipFile(path) as archive:
        if archive.testzip() is not None or "[Content_Types].xml" not in archive.namelist():
            raise ValueError("invalid XLSX archive")


def export_report(
    config: CollectionConfig,
    run: dict[str, Any],
    tasks: list[dict[str, Any]],
    observations: list[dict[str, Any]],
    output_path: Path,
) -> Path:
    """Write an auditable report atomically and return its final path.

    Unknown values stay blank.  The input observations must already contain
    only extracted source facts; this exporter does no estimation or matching.
    """
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    demo = bool(run.get("demo", config.demo))
    by_source = {source.id: source for source in config.sources}
    by_product = {product.id: product for product in config.products}
    # A report must expose an uncreated task as a coverage gap, rather than
    # silently treating the absence of an observation as a zero-price result.
    task_keys = {(t.get("product_id"), t.get("source_id")) for t in tasks}
    coverage_rows = list(tasks)
    for source in config.sources:
        for product_id in source.product_ids:
            if product_id in by_product and (product_id, source.id) not in task_keys:
                coverage_rows.append({
                    "id": "", "product_id": product_id, "source_id": source.id,
                    "status": "not_planned", "attempts": None,
                    "reason": "No collection task exists for the planned product-source combination.",
                    "backend": "", "updated_at": "",
                })

    fd, tmp_name = tempfile.mkstemp(prefix=output_path.stem + ".", suffix=".xlsx", dir=output_path.parent)
    os.close(fd)
    tmp_path = Path(tmp_name)
    try:
        wb = xlsxwriter.Workbook(tmp_path, {"strings_to_formulas": False, "strings_to_urls": False})
        fmts = {
            "header": wb.add_format({"bold": True, "font_color": "#FFFFFF", "bg_color": "#1F4E78", "border": 1, "text_wrap": True}),
            "banner": wb.add_format({"bold": True, "font_color": "#9C0006", "bg_color": "#FFC7CE", "align": "center"}),
            "text": wb.add_format({"valign": "top", "text_wrap": True}),
            "number": wb.add_format({"num_format": "#,##0.################", "valign": "top"}),
            "date": wb.add_format({"num_format": "yyyy-mm-dd hh:mm:ss \"UTC\"", "valign": "top"}),
            "note": wb.add_format({"valign": "top", "text_wrap": True, "font_color": "#7F6000"}),
            "link": wb.add_format({"font_color": "blue", "underline": 1, "valign": "top", "text_wrap": True}),
        }

        # 1. Run summary
        ws = wb.add_worksheet(SHEETS[0]); row = _setup_sheet(ws, ["Field", "Value"], fmts, demo)
        summary = [("Run ID", run.get("id")), ("Status", run.get("status")), ("Started at (UTC)", _utc(run.get("started_at"))),
                   ("Finished at (UTC)", _utc(run.get("finished_at"))), ("Demo data", "Yes" if demo else "No"),
                   ("Planned collection tasks", len(coverage_rows)), ("Observations", len(observations)), ("Products", len(config.products)), ("Sources", len(config.sources))]
        for key, value in summary:
            _write_value(ws, row, 0, key, fmts["text"]); _write_value(ws, row, 1, value, fmts["text"]); row += 1
        ws.set_column(0, 0, 24); ws.set_column(1, 1, 48)
        _finish_sheet(ws, 1 if demo else 0, row, 1)

        # 2. Planned coverage and task outcomes (never drop missing work).
        ws = wb.add_worksheet(SHEETS[1]); row = _setup_sheet(ws, ["Task ID", "Product", "Source", "Status", "Attempts", "Reason", "Backend", "Updated at (UTC)"], fmts, demo)
        for task in coverage_rows:
            values = [task.get("id"), by_product.get(task.get("product_id"), None).name if task.get("product_id") in by_product else task.get("product_id"),
                      by_source.get(task.get("source_id"), None).name if task.get("source_id") in by_source else task.get("source_id"), task.get("status"), task.get("attempts"), task.get("reason"), task.get("backend"), task.get("updated_at")]
            for col, value in enumerate(values):
                if col == 4 and value not in (None, ""):
                    _write_decimal(ws, row, col, value, fmts["number"])
                elif col == 7: _write_utc(ws, row, col, value, fmts["date"], fmts["text"])
                else: _write_value(ws, row, col, value, fmts["note"] if col == 5 else fmts["text"])
            row += 1
        ws.set_column(0, 0, 26); ws.set_column(1, 2, 22); ws.set_column(3, 4, 14); ws.set_column(5, 5, 42); ws.set_column(6, 7, 25)
        _finish_sheet(ws, 1 if demo else 0, row, 7)

        # 3. Only verified, fresh, explicitly comparable observations.
        ws = wb.add_worksheet(SHEETS[2]); row = _setup_sheet(ws, ["Comparison Key", "Product ID", "Product", "Source", "Price", "Raw Price", "Normalized Amount", "Calculation", "Currency", "Unit", "Pack Quantity", "Collected at (UTC)", "Evidence URL", "Group Count", "Minimum", "Maximum", "Median"], fmts, demo)
        comparable = [o for o in observations if o.get("comparable") is True and o.get("status") == "verified" and o.get("freshness") == "observed" and _decimal(o.get("amount")) is not None and o.get("comparison_key")]
        # One latest verified observation per (comparison key, product, source).
        latest: dict[tuple[str, str, str], dict[str, Any]] = {}
        for obs in comparable:
            key = (_text(obs.get("comparison_key")), _text(obs.get("product_id")), _text(obs.get("source_id")))
            prior = latest.get(key)
            if prior is None or (_as_datetime(obs.get("collected_at")) or datetime.min.replace(tzinfo=timezone.utc)) > (_as_datetime(prior.get("collected_at")) or datetime.min.replace(tzinfo=timezone.utc)):
                latest[key] = obs
        selected = list(latest.values())
        grouped: dict[tuple[str, str], list[Decimal]] = {}
        for obs in selected:
            grouped.setdefault((_text(obs.get("comparison_key")), _text(obs.get("product_id"))), []).append(_decimal(obs.get("amount")))
        for obs in selected:
            stats = sorted(grouped[(_text(obs.get("comparison_key")), _text(obs.get("product_id")))])
            middle = (stats[(len(stats)-1)//2] + stats[len(stats)//2]) / 2
            values = [obs.get("comparison_key"), obs.get("product_id"), by_product.get(obs.get("product_id"), None).name if obs.get("product_id") in by_product else obs.get("product_id"), obs.get("source_name") or by_source.get(obs.get("source_id"), None).name if obs.get("source_id") in by_source else obs.get("source_id"), obs.get("amount"), _raw_price(obs), obs.get("normalized_amount"), obs.get("calculation"), obs.get("currency"), obs.get("unit"), obs.get("pack_quantity"), obs.get("collected_at"), obs.get("source_url"), len(stats), stats[0], stats[-1], middle]
            for col, value in enumerate(values):
                if col in (4, 6, 14, 15, 16): _write_decimal(ws, row, col, value, fmts["number"])
                elif col == 13: _write_decimal(ws, row, col, value, fmts["number"])
                elif col == 11: _write_utc(ws, row, col, value, fmts["date"], fmts["text"])
                elif col == 12 and _safe_url(value): ws.write_url(row, col, _safe_url(value), fmts["link"], _text(value))
                else: _write_value(ws, row, col, value, fmts["text"])
            row += 1
        ws.set_column(0, 3, 22); ws.set_column(4, 6, 18); ws.set_column(7, 10, 17); ws.set_column(11, 11, 23); ws.set_column(12, 12, 46); ws.set_column(13, 16, 14)
        _finish_sheet(ws, 1 if demo else 0, row, 16)

        # 4. Full raw observation history, including unavailable/review rows.
        ws = wb.add_worksheet(SHEETS[3]); row = _setup_sheet(ws, ["Observation ID", "Task ID", "Product", "Source", "Status", "Reason", "Collected at (UTC)", "Amount", "Raw Amount", "Normalized Amount", "Calculation", "Currency", "Unit", "Comparable", "Freshness", "Extraction Method", "Raw Fields JSON"], fmts, demo)
        for obs in observations:
            values = [obs.get("id"), obs.get("task_id"), by_product.get(obs.get("product_id"), None).name if obs.get("product_id") in by_product else obs.get("product_id"), obs.get("source_name") or obs.get("source_id"), obs.get("status"), obs.get("reason"), obs.get("collected_at"), obs.get("amount") if obs.get("status") == "verified" else None, _raw_price(obs), obs.get("normalized_amount"), obs.get("calculation"), obs.get("currency"), obs.get("unit"), obs.get("comparable"), obs.get("freshness"), obs.get("extraction_method"), obs.get("raw_fields")]
            for col, value in enumerate(values):
                if col == 7: _write_decimal(ws, row, col, value, fmts["number"])
                elif col == 9: _write_decimal(ws, row, col, value, fmts["number"])
                elif col == 6: _write_utc(ws, row, col, value, fmts["date"], fmts["text"])
                else: _write_value(ws, row, col, value, fmts["note"] if col in (5, 16) else fmts["text"])
            row += 1
        ws.set_column(0, 1, 26); ws.set_column(2, 4, 18); ws.set_column(5, 5, 38); ws.set_column(6, 15, 17); ws.set_column(16, 16, 54)
        _finish_sheet(ws, 1 if demo else 0, row, 16)

        # 5. One row per observation with locator, raw evidence and hash/file reference.
        ws = wb.add_worksheet(SHEETS[4]); row = _setup_sheet(ws, ["Observation ID", "Product", "Source", "Source URL", "Evidence File", "SHA-256", "Locator", "Extraction Method", "Field Evidence JSON", "Raw Fields JSON"], fmts, demo)
        for obs in observations:
            values = [obs.get("id"), by_product.get(obs.get("product_id"), None).name if obs.get("product_id") in by_product else obs.get("product_id"), obs.get("source_name") or obs.get("source_id"), obs.get("source_url"), obs.get("evidence_path"), obs.get("evidence_sha256"), obs.get("locator"), obs.get("extraction_method"), obs.get("evidence"), obs.get("raw_fields")]
            for col, value in enumerate(values):
                if col == 3 and _safe_url(value): ws.write_url(row, col, _safe_url(value), fmts["link"], _text(value))
                else: _write_value(ws, row, col, value, fmts["text"])
            row += 1
        ws.set_column(0, 2, 22); ws.set_column(3, 4, 44); ws.set_column(5, 7, 26); ws.set_column(8, 9, 54)
        _finish_sheet(ws, 1 if demo else 0, row, 9)

        # 6. All non-final rows needing a human, with explicit task failures too.
        ws = wb.add_worksheet(SHEETS[5]); row = _setup_sheet(ws, ["Type", "ID", "Product", "Source", "Status", "Reason", "Raw Amount", "Evidence URL", "Updated at (UTC)"], fmts, demo)
        for task in coverage_rows:
            if task.get("status") not in {"verified", "completed"}:
                values = ["Task", task.get("id"), task.get("product_id"), task.get("source_id"), task.get("status"), task.get("reason"), None, None, task.get("updated_at")]
                for col, value in enumerate(values):
                    if col == 8: _write_utc(ws, row, col, value, fmts["date"], fmts["text"])
                    else: _write_value(ws, row, col, value, fmts["note"] if col == 5 else fmts["text"])
                row += 1
        for obs in observations:
            if not (obs.get("status") == "verified" and obs.get("freshness") == "observed" and obs.get("comparable") is True):
                values = ["Observation", obs.get("id"), obs.get("product_id"), obs.get("source_name") or obs.get("source_id"), obs.get("status"), obs.get("reason"), _raw_price(obs), obs.get("source_url"), obs.get("collected_at")]
                for col, value in enumerate(values):
                    if col == 7 and _safe_url(value): ws.write_url(row, col, _safe_url(value), fmts["link"], _text(value))
                    elif col == 8: _write_utc(ws, row, col, value, fmts["date"], fmts["text"])
                    else: _write_value(ws, row, col, value, fmts["note"] if col == 5 else fmts["text"])
                row += 1
        ws.set_column(0, 0, 12); ws.set_column(1, 3, 22); ws.set_column(4, 4, 16); ws.set_column(5, 5, 42); ws.set_column(6, 8, 28)
        _finish_sheet(ws, 1 if demo else 0, row, 8)
        wb.close()
        _validate_xlsx(tmp_path)
        os.replace(tmp_path, output_path)
        return output_path
    except Exception:
        try: tmp_path.unlink(missing_ok=True)
        finally: raise
