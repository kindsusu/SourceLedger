"""Evidence-preserving extraction for files and fetched web responses."""
from __future__ import annotations

import csv
import io
import json
import re
from datetime import date, datetime
from typing import Any

from bs4 import BeautifulSoup
from openpyxl import load_workbook
from openpyxl.utils import get_column_letter
from pypdf import PdfReader

from .models import Candidate, FetchResult, Source


def _text(value: Any) -> str | None:
    if value is None:
        return None
    value = str(value).strip()
    return value or None


def _evidence(location: str, raw: Any) -> dict[str, Any]:
    return {"location": location, "raw": raw}


def _json_value(value: Any) -> Any:
    """Keep file-derived values JSON-serializable without inventing a value."""
    return value.isoformat() if isinstance(value, (datetime, date)) else value


def _candidate_from_mapping(
    values: dict[str, Any], *, locator: str, method: str, columns: dict[str, str] | None = None
) -> Candidate:
    fields: dict[str, Any] = {}
    evidence: dict[str, dict[str, Any]] = {}
    specs: dict[str, str] = {}
    mapping = columns or {key: key for key in values}
    for field, key in mapping.items():
        raw = _json_value(values.get(key))
        if raw is None:
            continue
        if field.startswith("spec:"):
            spec = field.removeprefix("spec:")
            specs[spec] = str(raw)
            evidence[field] = _evidence(locator, raw)
        else:
            fields[field] = raw
            evidence[field] = _evidence(locator, raw)
    return Candidate(fields=fields, evidence=evidence, locator=locator, extraction_method=method, specs=specs)


def _selected_value(node: Any, selector: str) -> Any:
    attr = re.search(r"::attr\(([^)]+)\)$", selector)
    css = selector[: attr.start()] if attr else selector
    selected = node.select_one(css)
    if selected is None:
        return None
    return selected.get(attr.group(1)) if attr else selected.get_text(" ", strip=True)


def _extract_html(result: FetchResult, source: Source) -> list[Candidate]:
    soup = BeautifulSoup(result.content, "html.parser")
    candidates: list[Candidate] = []
    if source.selectors:
        rows = soup.select(source.row_selector) if source.row_selector else [soup]
        for index, row in enumerate(rows, start=1):
            fields: dict[str, Any] = {}
            evidence: dict[str, dict[str, Any]] = {}
            specs: dict[str, str] = {}
            for field, selector in source.selectors.items():
                raw = _selected_value(row, selector)
                if raw is None:
                    continue
                location = f"css:{source.row_selector or ':document'}[{index}] {selector}"
                if field.startswith("spec:"):
                    specs[field.removeprefix("spec:")] = str(raw)
                else:
                    fields[field] = raw
                evidence[field] = _evidence(location, raw)
            if fields or specs:
                candidates.append(Candidate(fields, evidence, f"css-row:{index}", "html_css", specs))
        return candidates

    # JSON-LD is only a fallback when no site-specific selectors were configured.
    product_nodes: list[tuple[int, int, dict[str, Any]]] = []
    for script_index, script in enumerate(soup.select('script[type="application/ld+json"]'), start=1):
        try:
            document = json.loads(script.string or script.get_text(), parse_float=str)
        except (json.JSONDecodeError, TypeError):
            continue
        nodes = document if isinstance(document, list) else document.get("@graph", [document]) if isinstance(document, dict) else []
        for node_index, node in enumerate(nodes, start=1):
            if not isinstance(node, dict) or node.get("@type") not in ("Product", ["Product"]):
                continue
            product_nodes.append((script_index, node_index, node))
    for script_index, node_index, node in product_nodes:
            offers = node.get("offers", [])
            if isinstance(offers, dict):
                offers = [offers]
            if not offers:
                offers = [{}]
            ambiguous = len(product_nodes) > 1 or len(offers) > 1
            for offer_index, offer in enumerate(offers, start=1):
                if not isinstance(offer, dict):
                    offer = {}
                locator = f"jsonld:{script_index}/{node_index}/offer:{offer_index}"
                values = {
                    "price": None if offer.get("@type") == "AggregateOffer" else offer.get("price"),
                    "currency": offer.get("priceCurrency"),
                    "availability": offer.get("availability"),
                    "valid_to": offer.get("priceValidUntil"),
                    "sku": node.get("sku"),
                    "offer_sku": offer.get("sku"),
                    "option": offer.get("name"),
                    "model": node.get("model"),
                    "manufacturer": (node.get("manufacturer") or {}).get("name") if isinstance(node.get("manufacturer"), dict) else node.get("manufacturer"),
                }
                candidate = _candidate_from_mapping(values, locator=locator, method="json_ld_product_offer")
                if offer.get("@type") == "AggregateOffer":
                    candidate.evidence["_extraction"] = _evidence(locator, "AggregateOffer range is not an exact price")
                if ambiguous:
                    candidate.evidence["_selection"] = _evidence(locator, "multiple JSON-LD products or offers require product matching")
                candidates.append(candidate)
    return candidates


def _extract_csv(result: FetchResult, source: Source) -> list[Candidate]:
    text = result.content.decode("utf-8-sig", errors="replace")
    return [
        _candidate_from_mapping(dict(row), locator=f"csv:row:{index}", method="csv", columns=source.columns)
        for index, row in enumerate(csv.DictReader(io.StringIO(text)), start=2)
    ]


def _extract_xlsx(result: FetchResult, source: Source) -> list[Candidate]:
    values_book = load_workbook(io.BytesIO(result.content), read_only=True, data_only=True)
    formulas_book = load_workbook(io.BytesIO(result.content), read_only=True, data_only=False)
    try:
        sheet_name = source.sheet or values_book.sheetnames[0]
        values_ws, formulas_ws = values_book[sheet_name], formulas_book[sheet_name]
        value_rows, formula_rows = values_ws.iter_rows(values_only=True), formulas_ws.iter_rows(values_only=True)
        headers = next(value_rows, ())
        next(formula_rows, ())
        header_index = {str(value): index for index, value in enumerate(headers) if value is not None}
        candidates: list[Candidate] = []
        for row_number, (row, formula_row) in enumerate(zip(value_rows, formula_rows), start=2):
            fields: dict[str, Any] = {}
            evidence: dict[str, dict[str, Any]] = {}
            specs: dict[str, str] = {}
            for field, header in source.columns.items():
                index = header_index.get(header)
                if index is None or index >= len(row):
                    continue
                raw, formula = row[index], formula_row[index]
                location = f"xlsx:{sheet_name}!{get_column_letter(index + 1)}{row_number}"
                if isinstance(formula, str) and formula.startswith("=") and raw is None:
                    evidence[field] = {**_evidence(location, formula), "formula_cache_missing": True}
                    continue
                if raw is None:
                    continue
                raw = _json_value(raw)
                if field.startswith("spec:"):
                    specs[field.removeprefix("spec:")] = str(raw)
                else:
                    fields[field] = raw
                evidence[field] = _evidence(location, raw)
            if fields or specs or evidence:
                candidates.append(Candidate(fields, evidence, f"xlsx:{sheet_name}:row:{row_number}", "xlsx", specs))
        return candidates
    finally:
        values_book.close()
        formulas_book.close()


def _extract_json(result: FetchResult, source: Source) -> list[Candidate]:
    payload = json.loads(result.content.decode("utf-8-sig"), parse_float=str)
    rows = payload if isinstance(payload, list) else payload.get("items", payload.get("products", [payload])) if isinstance(payload, dict) else []
    return [
        _candidate_from_mapping(row, locator=f"json:item:{index}", method="json", columns=source.columns)
        for index, row in enumerate(rows, start=1) if isinstance(row, dict)
    ]


def _extract_pdf(result: FetchResult, source: Source) -> list[Candidate]:
    if not source.pdf_pattern:
        return []
    reader = PdfReader(io.BytesIO(result.content))
    pattern = re.compile(source.pdf_pattern or "", re.MULTILINE)
    candidates: list[Candidate] = []
    any_text = False
    for page_number, page in enumerate(reader.pages, start=1):
        text = page.extract_text() or ""
        any_text = any_text or bool(text.strip())
        for match_index, match in enumerate(pattern.finditer(text), start=1):
            values = {key: value for key, value in match.groupdict().items() if value is not None}
            candidates.append(_candidate_from_mapping(values, locator=f"pdf:page:{page_number}:match:{match_index}", method="pdf_text_pattern"))
    if not candidates and not any_text:
        return [Candidate({}, {"_extraction": {"location": "pdf", "raw": "no embedded text; OCR not implemented"}}, "pdf", "pdf_ocr_not_implemented")]
    return candidates


def extract(result: FetchResult, source: Source) -> list[Candidate]:
    """Extract only source-supplied values; invalid/unavailable fetches produce no values."""
    if result.status != "fetched":
        return []
    media_type = result.media_type.lower()
    if "html" in media_type:
        return _extract_html(result, source)
    if "csv" in media_type:
        return _extract_csv(result, source)
    if "spreadsheet" in media_type or "excel" in media_type or result.content[:2] == b"PK":
        return _extract_xlsx(result, source)
    if "json" in media_type:
        return _extract_json(result, source)
    if "pdf" in media_type or result.content[:4] == b"%PDF":
        return _extract_pdf(result, source)
    return []
