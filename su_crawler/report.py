"""Evidence-first XLSX export.  This module never invents a price or a value."""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from io import BytesIO
import json
import os
from pathlib import Path
import tempfile
from typing import Any
from urllib.parse import urlparse
import zipfile

import xlsxwriter

from .models import CollectionConfig
from .xlsx_sheets import SheetSpec, cell_text, replace_sheets, validate_package, write_xlsxwriter


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


def _write_decimal(ws: Any, row: int, col: int, value: Any, number_fmt: Any) -> float | None:
    """Use a numeric cell when Excel can preserve it; always keep raw elsewhere.

    Returns the number written, or None for a blank cell.
    """
    dec = _decimal(value)
    if dec is None:
        ws.write_blank(row, col, None)
        return None
    # Excel stores IEEE-754 numbers.  Avoid silently rounding long Decimal IDs/prices.
    digits = len(dec.as_tuple().digits)
    if digits <= 15:
        places = max(0, -dec.normalize().as_tuple().exponent)
        cell_format = number_fmt.get(places, number_fmt['scientific']) if isinstance(number_fmt, dict) else number_fmt
        ws.write_number(row, col, float(dec), cell_format)
        return float(dec)
    ws.write_blank(row, col, None)
    return None


def _write_value(ws: Any, row: int, col: int, value: Any, text_fmt: Any | None = None) -> str | None:
    """Write literal text; return exactly what the cell holds (None when blank)."""
    if value is None or value == "":
        ws.write_blank(row, col, None, text_fmt)
        return None
    # Excel cells hold 32,767 characters. Cut explicitly with a visible marker;
    # SQLite keeps the full field evidence and raw values.
    text = cell_text(_text(value))
    ws.write_string(row, col, text, text_fmt)
    return text


def _write_link(ws: Any, row: int, col: int, value: Any, link_fmt: Any, text_fmt: Any) -> str | None:
    """Write a safe http(s) link, or the literal text when Excel cannot hold the link."""
    url = _safe_url(value)
    display = cell_text(_text(value))
    # xlsxwriter skips URLs over Excel's 2,079-character limit; keep them as text.
    if url and len(url) <= 2079 and cell_text(url) == url:
        if ws.write_url(row, col, url, link_fmt, display) >= 0:
            return display
    return _write_value(ws, row, col, value, text_fmt)


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


HIDDEN_PRICE_NOTE = "Price field evidence is hidden in the source display; amount left blank for review."
HIDDEN_ESTIMATE_NOTE = "Estimate field evidence is hidden in the source display; estimate left blank for review."


def _field_hidden(obs: dict[str, Any], name: str) -> bool:
    """A field captured from a hidden DOM node is never shown as an amount."""
    evidence = obs.get("evidence")
    proof = evidence.get(name) if isinstance(evidence, dict) else None
    return isinstance(proof, dict) and proof.get("display_state") == "hidden"


def _hidden_notes(obs: dict[str, Any]) -> list[str]:
    notes = []
    if _field_hidden(obs, "price"):
        notes.append(HIDDEN_PRICE_NOTE)
    if obs.get("value_origin") == "calculator_estimate" and _field_hidden(obs, "derived_values.estimated_price"):
        notes.append(HIDDEN_ESTIMATE_NOTE)
    return notes


def _reason(obs: dict[str, Any]) -> Any:
    notes = _hidden_notes(obs)
    if not notes:
        return obs.get("reason")
    return "; ".join(([str(obs["reason"])] if obs.get("reason") else []) + notes)


def _observed_amount(obs: dict[str, Any], *, include_review: bool = False) -> Any:
    if ((obs.get("status") == "verified" or include_review and obs.get("status") == "review")
            and obs.get("value_origin", "observed") == "observed"
            and obs.get("source_visibility") != "hidden"
            and not _field_hidden(obs, "price")):
        return obs.get("amount")
    return None


def _estimated_amount(obs: dict[str, Any]) -> Any:
    if (obs.get("value_origin") == "calculator_estimate" and obs.get("source_visibility") != "hidden"
            and not _field_hidden(obs, "derived_values.estimated_price")):
        return obs.get("derived_amount")
    return None


def _comparison_evidence_ok(obs: dict[str, Any]) -> bool:
    mode = obs.get("evidence_mode", "unknown")
    if mode == "static_html":
        return False
    if mode == "rendered_dom":
        return obs.get("source_visibility") == "visible"
    return mode in {"unknown", "structured_record", "document_text"}


def _validate_xlsx(path: Path, *, deep: bool = True) -> None:
    """Check the package; ``deep`` re-reads every member to verify its CRC."""
    validate_package(Path(path), deep=deep)


def _coverage_spec(coverage: list[dict[str, Any]]) -> SheetSpec:
    """Catalog section audit; coverage is page based with several sections each."""
    rows: list[list[Any]] = [["Page URL", "Section", "Status", "Observed offers", "Extracted offers",
                              "Current prices", "Unparsed offers", "Expansion clicks", "Reason",
                              "Evidence reference", "Snapshot reference", "Expected offers"]]
    for page in coverage:
        if not isinstance(page, dict):
            continue
        sections = page.get("sections")
        if not isinstance(sections, list):
            sections = []
        sections = [section for section in sections if isinstance(section, dict)]
        if not sections:
            from .coverage import page_status
            status = page_status(page) if page.get("status") else "unknown"
            sections = [{"section": "Page scope", "status": status,
                         "reason": page.get("reason") or ("Page excluded from the requested scope." if status == "excluded"
                                                          else "Page collection was interrupted." if status == "partial"
                                                          else "No section inventory recorded; completeness not checked.")}]
        for section in sections:
            evidence = section.get("evidence") or section.get("evidence_ref") or section.get("evidence_path")
            snapshot = section.get("snapshot_path") or section.get("snapshot_ref") or page.get("evidence_path")
            rows.append([_text(page.get("url") or page.get("source_url")),
                         _text(section.get("section")), _text(section.get("status") or "unknown"),
                         section.get("observed_count"), section.get("extracted_count"),
                         section.get("price_count"), section.get("unparsed_count"),
                         section.get("click_count"), _text(section.get("reason")),
                         _text(evidence), _text(snapshot), section.get("expected_count")])
    widths = dict(enumerate((48, 28, 16, 18, 18, 18, 18, 18, 48, 50, 50, 18), 1))
    return SheetSpec("Catalog coverage", rows, widths=widths, freeze=(2, 1),
                     auto_filter=f"A1:L{len(rows)}", row_styles={1: "header_blue"})


def _coverage_issues(coverage: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [issue for page in coverage if isinstance(page, dict)
            for issue in (page.get("collection_issues") or []) if isinstance(issue, dict)]


def _issues_spec(issues: list[dict[str, Any]]) -> SheetSpec:
    rows: list[list[Any]] = [["Issue ID", "Page URL", "Section", "Status", "Category", "Reason",
                              "Next action", "Attempts", "Evidence reference"]]
    for issue in issues:
        if not isinstance(issue, dict):
            continue
        rows.append([_text(issue.get("id")), _text(issue.get("url")), _text(issue.get("section")),
                     _text(issue.get("status")), _text(issue.get("category")), _text(issue.get("reason")),
                     _text(issue.get("next_action")), issue.get("attempt_count"),
                     _text(issue.get("evidence_ref"))])
    widths = dict(enumerate((30, 52, 26, 16, 24, 52, 52, 12, 48), 1))
    return SheetSpec("Collection Issues", rows, widths=widths, freeze=(2, 1),
                     auto_filter=f"A1:I{len(rows)}", row_styles={1: "header_blue"})


def _refresh_specs(refresh: dict[str, Any]) -> list[SheetSpec]:
    """Present revalidation separately from prices; source strings remain literal."""
    from .result_view import FIELD_LABELS
    incremental = (refresh.get('policy') or {}).get('mode', refresh.get('effective_mode')) == 'incremental'
    rows: list[list[Any]] = [
        ["Scope", refresh.get('scope')],
        ["Mode", refresh.get('effective_mode'), "Reason", refresh.get('reason')],
        ["Last full check (UTC)", refresh.get('last_full_at'),
         "Next interval full check (incremental only, UTC)",
         refresh.get('next_full_due_at') if incremental else None],
        ["Note", "Removed candidates are review items, not confirmed discontinued products. Revalidated evidence is not a fresh source capture.",
         "Approved URLs rechecked", refresh.get('approved_urls_rechecked'),
         "Inventory completeness verified", refresh.get('inventory_completeness_verified')],
        ["Page URL", "Check status", "Added", "Changed", "Unchanged", "Unavailable", "Removed candidate",
         "Original capture (UTC)", "Last checked (UTC)"],
    ]
    for item in refresh.get('changes', []):
        rows.append([item.get(key) for key in ('url', 'status', 'added', 'changed', 'unchanged', 'unavailable',
                     'removed_candidate', 'original_fetched_at', 'checked_at')])
    widths = {1: 38, 8: 38, 9: 38, **{col: 18 for col in range(2, 8)}}
    changes = SheetSpec("Collection Changes", rows, widths=widths, freeze=(6, 1),
                        auto_filter=f"A5:I{max(5, len(rows))}", row_styles={5: "bold"})

    items: list[list[Any]] = [['Status', 'Product', 'Source identity', 'Previous price', 'Current price',
                               'Previous currency', 'Current currency', 'Previous conditions', 'Current conditions',
                               'Changed fields', 'Reason', 'Page URL', 'Previous checked (UTC)', 'Checked (UTC)']]
    status_labels = {'baseline': 'First capture', 'added': 'Product added', 'price_changed': 'Price changed',
                     'price_and_conditions_changed': 'Price and observed conditions changed',
                     'conditions_changed': 'Conditions changed', 'unavailable': 'Unavailable for comparison',
                     'removed_candidate': 'Possible removal — review required', 'unchanged': 'Unchanged'}
    reason_labels = {
        'same_observed_fields': 'The observed product fields are unchanged.',
        'observed_price_or_currency_changed': 'The displayed price or currency changed.',
        'observed_price_and_conditions_changed': 'Displayed price or currency and observed conditions changed; the offers may not be comparable.',
        'observed_conditions_changed': 'Observed product or commercial conditions changed.',
        'first_successful_capture': 'First successful capture establishes the comparison baseline.',
        'new_observed_item': 'This product was observed now and was not matched to a previous product.',
        'absent_from_complete_inventory_review_required': 'Not found in the complete captured inventory. Review possible removal; this does not confirm sold out or discontinued.',
        'not_observed_in_current_capture': 'The previous product was not observed in this capture. Its current availability is unknown.',
        'page_unavailable': 'The page could not be collected successfully. Current product values are unknown.',
        'page_not_rechecked': 'The page was not rechecked in this run. Current product values are unknown.',
        'unverified_empty_capture': 'No products were extracted, but an empty inventory was not verified. The previous baseline is retained.',
    }

    def label(key):
        return FIELD_LABELS.get(key, key.removeprefix('spec:').replace('_', ' ').capitalize())

    def display(value):
        if value is None:
            return ''
        if isinstance(value, (list, tuple)):
            return ', '.join(display(part) for part in value)
        if isinstance(value, dict):
            return '; '.join(f'{label(key)}: {display(part)}' for key, part in value.items())
        return str(value)

    def conditions(value):
        if value is None:
            return None
        fields = {key: val for key, val in value.get('fields', {}).items()
                  if key not in {'name', 'price', 'currency', 'sku', 'item_id', 'mpn', 'gtin'}}
        lines = [f'{label(key)}: {display(val)}' for key, val in fields.items()]
        lines.extend(f'{label("spec:" + key)}: {display(val)}' for key, val in value.get('specs', {}).items())
        return '\n'.join(lines) or None

    for item in refresh.get('item_changes', []):
        before, after = (item.get('before') or {}).get('fields', {}), (item.get('after') or {}).get('fields', {})
        status = status_labels.get(item.get('status'), label(item.get('status') or ''))
        reason = reason_labels.get(item.get('reason'), display(item.get('reason')).replace('_', ' '))
        if item.get('status') in {'price_changed', 'price_and_conditions_changed'}:
            old_price, new_price = before.get('price'), after.get('price')
            if old_price != new_price:
                if new_price in (None, ''):
                    status, reason = 'Price no longer displayed', 'The previously observed price is no longer displayed. The current price is unknown.'
                elif any(term in str(new_price).casefold() for term in ('inquiry', 'quote', 'contact', '문의', '견적', 'price on request')):
                    status, reason = 'Price inquiry required', 'The source now displays inquiry wording instead of the previous price. No numeric current price is inferred.'
                elif old_price in (None, ''):
                    status, reason = 'Price now displayed', 'A price is now displayed; the previous capture did not display a price.'
                else:
                    reason = 'The displayed price changed.'
                if before.get('currency') != after.get('currency'):
                    reason += ' The displayed currency also changed.'
            else:
                status = 'Currency changed'
                reason = 'The displayed currency changed; the displayed price text is unchanged.'
            if item.get('status') == 'price_and_conditions_changed':
                status = 'Price and observed conditions changed'
                reason += ' Recorded conditions also changed; this is not a like-for-like price comparison.'
        comparison = item.get('condition_comparison')
        if comparison == 'not_observed':
            reason += ' Other offer conditions were not recorded; identical terms cannot be confirmed.'
        elif comparison == 'incomplete':
            reason += ' Some condition values are missing in one capture; identical terms cannot be confirmed.'
        elif comparison == 'observed_fields_only':
            reason += ' Only recorded condition fields were compared; unrecorded terms remain unknown.'
        items.append([status, item.get('product_name'), item.get('identity'),
                      before.get('price'), after.get('price'), before.get('currency'), after.get('currency'),
                      conditions(item.get('before')), conditions(item.get('after')),
                      ', '.join(label(key) for key in item.get('changed_fields', [])), reason, item.get('url'),
                      item.get('previous_checked_at'), item.get('checked_at')])
    item_widths = {col: (48 if col in {8, 9, 11, 12} else 26) for col in range(1, 15)}
    item_sheet = SheetSpec("Item Changes", items, widths=item_widths, freeze=(2, 1),
                           auto_filter=f"A1:N{max(1, len(items))}", row_styles={1: "header_navy_wrap"},
                           cell_style="wrap_top", row_heights={1: 32})
    return [changes, item_sheet]


def append_section_coverage(path: str | Path, coverage: list[dict[str, Any]]) -> Path:
    """Replace the catalog section audit sheet in an existing report atomically.

    Coverage is page based; each page may contain several section records. The
    issue ledger embedded in the coverage is replaced in the same write.
    """
    return replace_sheets(path, [_coverage_spec(coverage), _issues_spec(_coverage_issues(coverage))])


def append_collection_issues(path: str | Path, issues: list[dict[str, Any]]) -> Path:
    """Replace the actionable issue ledger in an existing XLSX report atomically."""
    return replace_sheets(path, [_issues_spec(issues)])


def append_refresh_summary(path: str | Path, refresh: dict[str, Any]) -> Path:
    """Present revalidation separately from prices; source strings remain literal."""
    return replace_sheets(path, _refresh_specs(refresh))


def export_report(
    config: CollectionConfig,
    run: dict[str, Any],
    tasks: list[dict[str, Any]],
    observations: list[dict[str, Any]],
    output_path: Path,
    *, supplemental_sheets: list[dict[str, Any]] | None = None,
    section_coverage: list[dict[str, Any]] | None = None,
    collection_issues: list[dict[str, Any]] | None = None,
) -> Path:
    """Write an auditable report atomically and return its final path.

    Unknown values stay blank.  The input observations must already contain
    only extracted source facts; this exporter does no estimation or matching.
    ``section_coverage`` and ``collection_issues`` add the same audit sheets
    as ``append_section_coverage``/``append_collection_issues`` in this one
    write, so a pipeline export never re-opens the finished workbook.
    """
    return _export(config, run, tasks, observations, output_path, supplemental_sheets=supplemental_sheets,
                   section_coverage=section_coverage, collection_issues=collection_issues)[0]


def _export(
    config: CollectionConfig,
    run: dict[str, Any],
    tasks: list[dict[str, Any]],
    observations: list[dict[str, Any]],
    output_path: Path,
    *, supplemental_sheets: list[dict[str, Any]] | None = None,
    section_coverage: list[dict[str, Any]] | None = None,
    collection_issues: list[dict[str, Any]] | None = None,
    deep_validation: bool = True,
) -> tuple[Path, dict[str, Any]]:
    """Export and return the dashboard inputs read from what was written.

    The summary mirrors ``final_report.workbook_summary`` exactly, so the
    finished workbook does not have to be parsed again for the dashboard.
    """
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    demo = bool(run.get("demo", config.demo))
    by_source = {source.id: source for source in config.sources}
    by_product = {product.id: product for product in config.products}
    from .result_view import build_result_view, FIELD_LABELS, variant_keys
    presentation = build_result_view(observations)
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
            "number": {places: wb.add_format({"num_format": "#,##0" + ("." + "0" * places if places else ""), "valign": "top"}) for places in range(16)},
            "date": wb.add_format({"num_format": "yyyy-mm-dd hh:mm:ss \"UTC\"", "valign": "top"}),
            "note": wb.add_format({"valign": "top", "text_wrap": True, "font_color": "#7F6000"}),
            "link": wb.add_format({"font_color": "blue", "underline": 1, "valign": "top", "text_wrap": True}),
        }
        fmts['number']['scientific'] = wb.add_format({'num_format': '0.##############E+00', 'valign': 'top'})

        # Show collected offers and distinct browser-only review candidates.
        headers = ["Product", "Source", "Recorded Price (not a final quote)", "Currency", "Result Status",
                   "Recorded Conditions", "Missing Conditions", "Supporting Evidence Count", "Conflicting Values", "Source URL", "Observation ID",
                   "Price Field Evidence", "Declared Normalization", "Comparison Readiness"]
        ws = wb.add_worksheet("Results"); row = _setup_sheet(ws, headers, fmts, demo)
        result_cells: list[list[Any]] = []
        for obs in presentation["rows"]:
            fields = obs.get("raw_fields") or {}
            recorded = "\n".join(f'{item["label"]}: {item["value"]}' + (" (normalized from browser text)" if item["status"] == "derived" else "")
                                  for key, item in obs["field_status"].items() if key not in {"price", "currency"} and item["status"] != "missing")
            # A browser-only review candidate may not have a price profile.
            # Keep explicit size/option/term conditions visible in the Results
            # sheet rather than hiding them in the evidence JSON.
            variant_values = dict(fields)
            for alias, canonical in (("size", "spec:size"), ("color", "spec:color"), ("option", "options"),
                                     ("deposit", "deposit_amount"), ("annual_mileage", "annual_mileage_km")):
                if canonical not in variant_values and alias in variant_values:
                    variant_values[canonical] = variant_values[alias]
            extras = "\n".join(f"{FIELD_LABELS.get(key, key)}: {variant_values[key]}" for key in variant_keys(variant_values)
                               if key not in obs["field_status"] and variant_values.get(key) not in (None, ""))
            if extras:
                recorded = recorded + ("\n" if recorded else "") + extras
            if obs.get("primary_candidate") and fields.get("price") not in (None, ""):
                recorded = f'Displayed price text (review required): {fields["price"]}' + ("\n" + recorded if recorded else "")
            if obs.get("price_kind") == "from":
                recorded = "Displayed price: starting at (source uses ~); exact quote unknown" + ("\n" + recorded if recorded else "")
            if _hidden_notes(obs):
                recorded = "\n".join(_hidden_notes(obs)) + ("\n" + recorded if recorded else "")
            values = [fields.get("name") or obs.get("product_id"), obs.get("source_name"),
                      _observed_amount(obs, include_review=True), obs.get("currency"), obs["result_status"],
                      recorded, "\n".join(FIELD_LABELS.get(key, key) for key in obs["missing_conditions"]), len(obs["supporting_evidence"]),
                      "\n".join(f'{FIELD_LABELS.get(c["field"], c["field"])}: {c["observed"]} / {c["submitted"]}' for c in obs["conflicts"]), obs.get("source_url"), obs["id"],
                      obs["field_validation"]["price_evidence"], obs["field_validation"]["normalization"],
                      obs["field_validation"]["comparison"]]
            written = []
            for col, value in enumerate(values):
                if col in (2, 7): written.append(_write_decimal(ws, row, col, value, fmts["number"]))
                elif col == 9: written.append(_write_link(ws, row, col, value, fmts["link"], fmts["text"]))
                else: written.append(_write_value(ws, row, col, value, fmts["text"]))
            result_cells.append([written[9], written[2] is not None])
            row += 1
        ws.set_column(0, 1, 28); ws.set_column(2, 4, 23); ws.set_column(5, 6, 46)
        ws.set_column(7, 7, 20); ws.set_column(8, 10, 42)
        ws.set_column(11, 13, 26)
        _finish_sheet(ws, 1 if demo else 0, row, len(headers) - 1)
        if presentation["evidence_count"]:
            headers = ["Evidence ID", "Linked Observation ID", "Association", "Product", "Submitted Fields (unverified)", "Conflicts", "Source URL", "Evidence File"]
            ws = wb.add_worksheet("Supporting Evidence"); row = _setup_sheet(ws, headers, fmts, demo)
            evidence_rows = presentation["evidence_submissions"] + [e for item in presentation["rows"] for e in item["supporting_evidence"]]
            for evidence in evidence_rows:
                values = [evidence["id"], evidence.get("target_observation_id"), evidence["association_status"],
                          (evidence.get("raw_fields") or {}).get("name"), evidence.get("raw_fields"), evidence.get("conflicts"), evidence.get("source_url"), evidence.get("evidence_path")]
                for col, value in enumerate(values): _write_value(ws, row, col, value, fmts["text"])
                row += 1
            ws.set_column(0, 3, 26); ws.set_column(4, 7, 48)
            _finish_sheet(ws, 1 if demo else 0, row, len(headers) - 1)

        # 1. Run summary
        ws = wb.add_worksheet(SHEETS[0]); row = _setup_sheet(ws, ["Field", "Value"], fmts, demo)
        summary = [("Run ID", run.get("id")), ("Status", run.get("status")), ("Started at (UTC)", _utc(run.get("started_at"))),
                   ("Finished at (UTC)", _utc(run.get("finished_at"))), ("Demo data", "Yes" if demo else "No"),
                   ("Planned collection tasks", len(coverage_rows)), ("Result rows", presentation["result_count"]),
                   ("Supporting evidence", presentation["evidence_count"]), ("Audit records", len(observations)),
                   ("Products", len(config.products)), ("Sources", len(config.sources)),
                   ("Calculator estimates", sum(o.get("value_origin") == "calculator_estimate" for o in observations)),
                   ("Evidence verification", "Validated means source fields passed checks; it does not certify a final commercial quote."),
                   ("Blank values", "Unknown values stay blank. Zero appears only when explicitly provided by the source.")]
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
        comparable = [o for o in presentation["rows"] if o.get("comparable") is True and _comparison_evidence_ok(o) and o.get("freshness") == "observed" and _decimal(_observed_amount(o)) is not None and o.get("comparison_key")]
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
                elif col == 12: _write_link(ws, row, col, value, fmts["link"], fmts["text"])
                else: _write_value(ws, row, col, value, fmts["text"])
            row += 1
        ws.set_column(0, 3, 22); ws.set_column(4, 6, 18); ws.set_column(7, 10, 17); ws.set_column(11, 11, 23); ws.set_column(12, 12, 46); ws.set_column(13, 16, 14)
        _finish_sheet(ws, 1 if demo else 0, row, 16)

        # 4. Full raw observation history, including unavailable/review rows.
        history_headers = ["Observation ID", "Task ID", "Product", "Source", "Status", "Reason", "Collected at (UTC)", "Amount", "Raw Amount", "Normalized Amount", "Calculation", "Currency", "Unit", "Comparable", "Freshness", "Extraction Method", "Raw Fields JSON", "Price Profile", "Value Origin", "Source Visibility", "Verification Level", "Estimated Amount (not observed)", "Derived Values JSON", "Review Flags", "Rental Conditions JSON", "Unverified Observed Amount"]
        history_headers.extend(["Evidence Mode", "Reference Price (observed)", "Advance Percent (observed)"])
        ws = wb.add_worksheet(SHEETS[3]); row = _setup_sheet(ws, history_headers, fmts, demo)
        for obs in observations:
            values = [obs.get("id"), obs.get("task_id"), by_product.get(obs.get("product_id"), None).name if obs.get("product_id") in by_product else obs.get("product_id"), obs.get("source_name") or obs.get("source_id"), obs.get("status"), _reason(obs), obs.get("collected_at"), _observed_amount(obs), _raw_price(obs), obs.get("normalized_amount"), obs.get("calculation"), obs.get("currency"), obs.get("unit"), obs.get("comparable"), obs.get("freshness"), obs.get("extraction_method"), obs.get("raw_fields"), obs.get("price_profile", "unit"), obs.get("value_origin", "observed"), obs.get("source_visibility"), obs.get("verification_level"), _estimated_amount(obs), obs.get("derived_values"), obs.get("review_flags"), obs.get("rental_conditions")]
            values.append(_observed_amount(obs, include_review=True) if obs.get('status') == 'review' else None)
            values.append(obs.get('evidence_mode', 'unknown'))
            values.extend([(obs.get("raw_fields") or {}).get("reference_price"),
                           (obs.get("rental_conditions") or {}).get("advance_percent", (obs.get("raw_fields") or {}).get("advance_percent"))])
            for col, value in enumerate(values):
                if col in (7, 21, 25): _write_decimal(ws, row, col, value, fmts["number"])
                elif col == 9: _write_decimal(ws, row, col, value, fmts["number"])
                elif col == 6: _write_utc(ws, row, col, value, fmts["date"], fmts["text"])
                else: _write_value(ws, row, col, value, fmts["note"] if col in (5, 16) else fmts["text"])
            row += 1
        ws.set_column(0, 1, 26); ws.set_column(2, 4, 18); ws.set_column(5, 5, 38); ws.set_column(6, 15, 17); ws.set_column(16, 16, 54)
        ws.set_column(17, 21, 23); ws.set_column(22, 24, 48); ws.set_column(25, 25, 25)
        _finish_sheet(ws, 1 if demo else 0, row, len(history_headers) - 1)

        # 5. One row per observation with locator, raw evidence and hash/file reference.
        evidence_headers = ["Observation ID", "Product", "Source", "Source URL", "Evidence File", "SHA-256", "Locator", "Extraction Method", "Field Evidence JSON", "Raw Fields JSON", "Evidence Mode", "Capture Receipt", "Receipt SHA-256", "Screenshot File", "Screenshot SHA-256"]
        ws = wb.add_worksheet(SHEETS[4]); row = _setup_sheet(ws, evidence_headers, fmts, demo)
        evidence_urls: list[Any] = []
        for obs in observations:
            values = [obs.get("id"), by_product.get(obs.get("product_id"), None).name if obs.get("product_id") in by_product else obs.get("product_id"), obs.get("source_name") or obs.get("source_id"), obs.get("source_url"), obs.get("evidence_path"), obs.get("evidence_sha256"), obs.get("locator"), obs.get("extraction_method"), obs.get("evidence"), obs.get("raw_fields")]
            artifacts = obs.get('evidence_artifacts') or {}
            receipt = artifacts.get('receipt') or {}
            screenshot = artifacts.get('screenshot') or {}
            values.extend([obs.get('evidence_mode', 'unknown'), receipt.get('path'), receipt.get('sha256'), screenshot.get('path'), screenshot.get('sha256')])
            for col, value in enumerate(values):
                if col == 3: evidence_urls.append(_write_link(ws, row, col, value, fmts["link"], fmts["text"]))
                else: _write_value(ws, row, col, value, fmts["text"])
            row += 1
        ws.set_column(0, 2, 22); ws.set_column(3, 4, 44); ws.set_column(5, 7, 26); ws.set_column(8, 9, 54)
        ws.set_column(10, 10, 22); ws.set_column(11, 14, 44)
        _finish_sheet(ws, 1 if demo else 0, row, len(evidence_headers) - 1)

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
                values = ["Observation", obs.get("id"), obs.get("product_id"), obs.get("source_name") or obs.get("source_id"), obs.get("status"), _reason(obs), _raw_price(obs), obs.get("source_url"), obs.get("collected_at")]
                for col, value in enumerate(values):
                    if col == 7: _write_link(ws, row, col, value, fmts["link"], fmts["text"])
                    elif col == 8: _write_utc(ws, row, col, value, fmts["date"], fmts["text"])
                    else: _write_value(ws, row, col, value, fmts["note"] if col == 5 else fmts["text"])
                row += 1
        ws.set_column(0, 0, 12); ws.set_column(1, 3, 22); ws.set_column(4, 4, 16); ws.set_column(5, 5, 42); ws.set_column(6, 8, 28)
        _finish_sheet(ws, 1 if demo else 0, row, 8)

        # Rental terms deserve separate, readable columns rather than forcing
        # operators to interpret JSON or treating monthly rent as a unit price.
        rentals = [o for o in presentation["rows"] if o.get("price_profile") == "rental"]
        if rentals:
            headers = ["Product", "Source", "Observed Monthly Price", "Estimated Monthly Price (not observed)",
                       "Currency", "Term (months)", "Deposit Amount", "Advance Amount", "Annual Mileage (km)",
                       "Trim", "Options", "Vehicle Condition", "Insurance", "Tax", "Availability",
                       "Upfront Deposit Amount", "Deposit Installment Amount", "Deposit Installment Months",
                       "Deposit Percent", "Displayed Deposit (raw)", "Value Origin", "Source Visibility",
                       "Status", "Comparable", "Freshness", "Collected at (UTC)", "Source URL", "Review Reason", "Observation ID"]
            headers.extend(["Reference Price (observed)", "Advance Percent (observed)"])
            ws = wb.add_worksheet("Rental Quotes"); row = _setup_sheet(ws, headers, fmts, demo)
            for obs in rentals:
                terms = obs.get("rental_conditions") or {}
                monthly = terms.get("price_basis") == "monthly"
                product = by_product.get(obs.get("product_id"))
                values = [product.name if product else obs.get("product_id"), obs.get("source_name") or obs.get("source_id"),
                          _observed_amount(obs, include_review=True) if monthly else None, _estimated_amount(obs) if monthly else None,
                          obs.get("currency"), terms.get("term_months"), terms.get("deposit_amount"), terms.get("advance_amount"),
                          terms.get("annual_mileage_km"), terms.get("trim"), terms.get("options"), terms.get("condition"),
                          terms.get("insurance"), terms.get("tax"), terms.get("availability"), terms.get("upfront_deposit_amount"),
                          terms.get("deposit_installment_amount"), terms.get("deposit_installment_months"), terms.get("deposit_percent"),
                          (obs.get("raw_fields") or {}).get("displayed_deposit"), obs.get("value_origin"), obs.get("source_visibility"),
                          obs.get("status"), obs.get("comparable"), obs.get("freshness"), obs.get("collected_at"),
                          obs.get("source_url"), _reason(obs), obs.get("id")]
                values.extend([(obs.get("raw_fields") or {}).get("reference_price"),
                               terms.get("advance_percent", (obs.get("raw_fields") or {}).get("advance_percent"))])
                for col, value in enumerate(values):
                    if col in (2, 3, 5, 6, 7, 8, 15, 16, 17, 18): _write_decimal(ws, row, col, value, fmts["number"])
                    elif col == 25: _write_utc(ws, row, col, value, fmts["date"], fmts["text"])
                    elif col == 26: _write_link(ws, row, col, value, fmts["link"], fmts["text"])
                    else: _write_value(ws, row, col, value, fmts["note"] if col == 27 else fmts["text"])
                row += 1
            ws.set_row(1 if demo else 0, 44)
            ws.set_column(0, 1, 24); ws.set_column(2, 3, 26); ws.set_column(4, 8, 19)
            ws.set_column(9, 14, 27); ws.set_column(15, 18, 23); ws.set_column(19, 19, 38)
            ws.set_column(20, 25, 23); ws.set_column(26, 27, 52); ws.set_column(28, 28, 26)
            ws.freeze_panes(2 if demo else 1, 2)
            _finish_sheet(ws, 1 if demo else 0, row, len(headers) - 1)
        # Site-specific detail and audit tables join the same atomic export.
        # No second workbook import/save is needed to append final results.
        for table in supplemental_sheets or []:
            headers = table["headers"]
            ws = wb.add_worksheet(table["name"])
            row = _setup_sheet(ws, headers, fmts, demo)
            for values in table["rows"]:
                for col, value in enumerate(values):
                    if isinstance(value, (int, float, Decimal)) and not isinstance(value, bool):
                        _write_decimal(ws, row, col, value, fmts["number"])
                    else:
                        _write_value(ws, row, col, value, fmts["text"])
                row += 1
            ws.set_column(0, len(headers) - 1, 26)
            _finish_sheet(ws, 1 if demo else 0, row, len(headers) - 1)
        # Coverage and issue audits join the same write instead of a later
        # load/save of the whole workbook.
        audit_specs = []
        if section_coverage is not None:
            audit_specs.append(_coverage_spec(section_coverage))
        if collection_issues is not None:
            audit_specs.append(_issues_spec(collection_issues))
        spec_formats: dict[str, Any] = {}
        for spec in audit_specs:
            write_xlsxwriter(wb, spec, spec_formats)
        sheet_names = [sheet.name for sheet in wb.worksheets()]
        wb.close()
        _validate_xlsx(tmp_path, deep=deep_validation)
        os.replace(tmp_path, output_path)
        summary = {"result_rows": result_cells, "price_column": True,
                   "evidence_urls": [] if result_cells else evidence_urls,
                   "categories": None, "task_count": len(coverage_rows), "comparison_count": len(selected),
                   "demo": demo, "sheet_names": sheet_names}
        return output_path, summary
    except Exception:
        try: tmp_path.unlink(missing_ok=True)
        finally: raise
