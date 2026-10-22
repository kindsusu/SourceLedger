"""Adapter for public Jetcar vehicle detail HTML."""
from __future__ import annotations

import re
from decimal import Decimal
from urllib.parse import urlparse

from bs4 import BeautifulSoup, Tag

from ..models import Candidate, FetchResult, Source
from .common import (annotate_evidence, decimal_string, evidence, evidence_mode, html_soup, is_visible,
                     unstruck_text, visible_text, won)


def _table(soup: BeautifulSoup, *needles: str) -> Tag | None:
    return next((table for table in soup.select("table") if is_visible(table) and all(n in (visible_text(table) or "") for n in needles)), None)


_DETAIL_PATH = re.compile(r"/(?:sub0201|sub0301|sub0701)/\d+/?$")


def is_detail_url(url: str) -> bool:
    """Single route contract for discovery and detail extraction."""
    return bool(_DETAIL_PATH.fullmatch(urlparse(url).path))


def _actual_id(result: FetchResult, source: Source, soup: BeautifulSoup) -> tuple[str | None, str | None]:
    canonical = soup.select_one('link[rel="canonical"]')
    urls = [canonical.get("href") if canonical else None, result.final_url, source.location]
    for url in urls:
        path = urlparse(str(url or "")).path
        match = _DETAIL_PATH.fullmatch(path)
        if match:
            return re.search(r"/(\d+)/?$", path).group(1), str(url)
    return None, None


def _rent_rows(table: Tag | None):
    """Yield visible data rows, including rows the current parser cannot decode.

    Struck-through text (a crossed-out list price) is not part of the row.
    """
    for index, row in enumerate(table.select("tr") if table else [], start=1):
        if not is_visible(row):
            continue
        raw = unstruck_text(row) or ""
        if re.search(r"\d+\s*개월", raw) or re.search(r"월\s*[\d,]+\s*원", raw):
            yield index, raw, row


_MONTHLY_PRICE = re.compile(r"월\s*([\d,]+)\s*원")
_MONTHLY_QUALIFIED = re.compile(r"월\s*[\d,.]+\s*(?:만\s*)?원?\s*([~∼〜]\s*\d?|[\-–—]\s*\d|부터|이상)")


def _row_price(raw: str) -> tuple[str | None, str | None]:
    """Return the row's single monthly price, or why none can be recorded."""
    qualified = _MONTHLY_QUALIFIED.search(raw)
    if qualified:
        ranged = re.search(r"\d$", qualified.group(1))
        return None, "price_range_not_single_amount" if ranged else "starting_price_qualifier"
    amounts = {decimal_string(match.group(1)) for match in _MONTHLY_PRICE.finditer(raw)}
    amounts.discard(None)
    if not amounts:
        return None, "price_inquiry_or_unavailable"
    if len(amounts) > 1:
        return None, "multiple_amounts_unresolved"
    return next(iter(amounts)), None


def _availability_text(row: Tag, raw: str) -> str:
    """The displayed non-price wording (e.g. 문의) from the row's price cell."""
    cells = [unstruck_text(cell) for cell in row.find_all(["td", "th"], recursive=False)]
    cells = [cell for cell in cells if cell and not re.search(r"\d+\s*개월", cell) and "보증금" not in cell
             and not re.search(r"연\s*[\d,.]+\s*만?\s*[Kk][Mm]", cell)]
    return cells[0] if len(cells) == 1 else raw


_BARE_AMOUNT = re.compile(r"(?:\d{1,3}(?:,\d{3})+|\d+)")


def _columns(row: Tag | None) -> dict[str, str]:
    """Cells of a headed quote table (보증금 | 계약기간 | 월 렌트비 | 주행거리) by header."""
    table = row.find_parent("table") if row is not None else None
    head = table.select("thead th") if table is not None else []
    cells = row.find_all("td", recursive=False) if row is not None else []
    if not head or len(head) != len(cells):
        return {}
    return {re.sub(r"\s+", " ", th.get_text(" ", strip=True)): unstruck_text(td) or "" for th, td in zip(head, cells)}


def _column_price(columns: dict[str, str]) -> str | None:
    """A bare amount under a 월 렌트비/월 렌트료 header (the header carries 월 and the won unit is implied)."""
    raw = next((value for header, value in columns.items() if re.fullmatch(r"월\s*렌트[비료]", header)), None)
    return raw.replace(",", "") if raw and _BARE_AMOUNT.fullmatch(raw) else None


def _column_deposit(columns: dict[str, str]) -> tuple[str | None, str | None]:
    raw = columns.get("보증금")
    return (won(raw), raw) if raw else (None, None)


def _rent_identity(index: int, raw: str, row: Tag | None = None) -> tuple[int, str, str, str | None, str | None] | None:
    """Use the displayed row and contract conditions, not its price alone."""
    columns = _columns(row)
    term = re.search(r"(\d+)\s*개월", raw)
    price, _ = _row_price(raw)
    price = price or _column_price(columns)
    if not term or not price:
        return None
    deposit_match = re.search(r"보증금\s*[\d,.]+\s*(?:만원|원)", raw)
    deposit = won(deposit_match.group()) if deposit_match else None
    if deposit_match and deposit is None:
        return None
    if not deposit_match:
        deposit, deposit_raw = _column_deposit(columns)
        if deposit_raw and deposit is None:
            return None
    annual_match = re.search(r"연\s*([\d,.]+)\s*(만)?\s*[Kk][Mm]", raw)
    annual = None
    if annual_match:
        annual_value = decimal_string(annual_match.group(1), multiplier=10_000 if annual_match.group(2) else 1)
        if annual_value is None or Decimal(annual_value) != Decimal(annual_value).to_integral_value():
            return None
        annual = str(int(Decimal(annual_value)))
    return index, term.group(1), price, deposit, annual


def section_coverage(result: FetchResult, candidates: list[Candidate]) -> list[dict[str, object]]:
    """Compare visible rental terms with extracted term/price identities."""
    table = _table(html_soup(result.content), "보증금", "개월")
    if table is None:
        return [{"section": "jetcar_rent_terms", "status": "unknown", "reason": "rent_table_not_discovered",
                 "observed_count": 0, "expected_count": None, "extracted_count": len(candidates),
                 "price_count": sum(c.fields.get("price") is not None for c in candidates),
                 "unparsed_count": 0, "extracted_locators": []}]
    raw_rows = list(_rent_rows(table))
    expected: dict[tuple[int, str, str, str | None, str | None], str] = {}
    unparsed = 0
    for index, raw, row in raw_rows:
        identity = _rent_identity(index, raw, row)
        if identity is None:
            unparsed += 1
            continue
        expected[identity] = f"row:{index}|term:{identity[1]}|price:{identity[2]}|deposit:{identity[3]}|annual_km:{identity[4]}"
    matched: list[str] = []
    remaining = dict(expected)
    for candidate in candidates:
        locator_match = re.search(r":rent-row:(\d+)$", candidate.locator)
        if locator_match is None:
            continue
        key = (int(locator_match.group(1)), candidate.fields.get("term_months"),
               candidate.fields.get("price"), candidate.fields.get("deposit_amount"),
               candidate.fields.get("annual_mileage_km"))
        if key in remaining:
            del remaining[key]
            matched.append(candidate.locator)
    missing = len(remaining) + unparsed
    if not raw_rows:
        status, reason = "unknown", "rent_rows_not_observed"
    elif missing:
        status, reason = "partial", "rent_rows_not_fully_extracted"
    elif len(matched) != len(raw_rows):
        status, reason = "partial", "rent_row_identity_mismatch"
    else:
        status, reason = "complete", "visible_rent_rows_matched"
    return [{"section": "jetcar_rent_terms", "status": status, "reason": reason,
             "observed_count": len(raw_rows), "expected_count": len(raw_rows),
             "extracted_count": len(matched), "price_count": len(matched),
             "unparsed_count": missing, "expected_identities": [expected[key] for key in sorted(expected)],
             "extracted_locators": matched}]


def extract(result: FetchResult, source: Source) -> list[Candidate]:
    soup = html_soup(result.content)
    mode = evidence_mode(result)
    visibility = "visible" if mode == "rendered_dom" else "unconfirmed"
    item_id, identity_source = _actual_id(result, source, soup)
    if not item_id:
        return []
    title_node = soup.select_one("h1") or soup.select_one("#hd_h1")
    title_raw = visible_text(title_node)
    name = title_raw.split(">", 1)[0].strip() if title_raw else None
    detail = _table(soup, "차량연식", "주행거리")
    detail_text = visible_text(detail)
    year = mileage = None
    if detail_text:
        match = re.search(r"차량연식\s+주행거리\s+(.+?)\s+([\d,]+)\s*[Kk][Mm]", detail_text)
        if match:
            year, mileage = match.group(1).strip(), decimal_string(match.group(2))
    fuel_table = _table(soup, "차량번호", "연료")
    fuel_text = visible_text(fuel_table)
    fuel_match = re.search(r"연료\s+(휘발유|가솔린|하이브리드|전기|LPG|경유|디젤)", fuel_text or "", re.I)
    option_parts = []
    for node in soup.select(".info-item"):
        raw = visible_text(node)
        if is_visible(node) and raw and raw.startswith("추가옵션"):
            option_parts.append(raw)
    option_table = _table(soup, "외관/내장", "안전장치")
    if visible_text(option_table):
        option_parts.append(visible_text(option_table) or "")
    rent_table = _table(soup, "보증금", "개월")
    common: dict[str, str] = {"item_id": item_id}
    common_evidence = {
        "item_id": evidence("url:path", item_id, identity_source),
    }
    values = [("name", name, title_raw, "css:h1"), ("odometer_km", mileage, detail_text, "table:vehicle"), ("fuel", fuel_match.group(1) if fuel_match else None, fuel_text, "table:fuel")]
    fields = dict(common)
    proofs = dict(common_evidence)
    for key, value, raw, location in values:
        if value is not None:
            fields[key] = value
            proofs[key] = evidence(location, value, raw)
    if year:
        fields["year"] = year
        proofs["year"] = evidence("table:vehicle", year, detail_text)
    if option_parts:
        fields["options"] = " | ".join(option_parts)
        proofs["options"] = evidence("css:.info-item,table:options", fields["options"], " | ".join(option_parts))
    tax_node = next((n for n in soup.select(".cap") if is_visible(n) and "부가세" in (visible_text(n) or "")), None)
    if tax_node:
        fields["tax"] = visible_text(tax_node)
        proofs["tax"] = evidence("css:.cap", fields["tax"], visible_text(tax_node))

    rows = []
    for index, raw, row in _rent_rows(rent_table):
        term = re.search(r"(\d+)\s*개월", raw)
        if not term:
            continue
        price, price_problem = _row_price(raw)
        columns = _columns(row)
        if price is None and _column_price(columns):
            price, price_problem = _column_price(columns), None
        review_flags: list[str] = []
        row_fields, row_proofs = dict(fields), dict(proofs)
        if price is not None:
            row_fields.update(price=price, currency="KRW", price_basis="monthly", term_months=term.group(1))
            for key in ("price", "price_basis", "term_months"):
                row_proofs[key] = evidence(f"table:rent/tr:{index}", row_fields[key], raw)
            row_proofs["currency"] = evidence(f"table:rent/tr:{index}", "KRW", "원", raw)
        else:
            # A displayed term without one numeric monthly price (문의, a range,
            # several amounts) is kept with a null price, never dropped or guessed.
            row_fields["term_months"] = term.group(1)
            row_proofs["term_months"] = evidence(f"table:rent/tr:{index}", term.group(1), raw)
            row_proofs["price"] = evidence(f"table:rent/tr:{index}", None, raw)
            if price_problem == "price_inquiry_or_unavailable":
                row_fields["availability"] = _availability_text(row, raw)
                row_proofs["availability"] = evidence(f"table:rent/tr:{index}", row_fields["availability"], raw)
            review_flags.append(price_problem)
        deposit = re.search(r"보증금\s*[\d,.]+\s*(?:만원|원)", raw)
        if deposit and won(deposit.group()):
            row_fields["deposit_amount"] = won(deposit.group())
            row_proofs["deposit_amount"] = evidence(f"table:rent/tr:{index}", row_fields["deposit_amount"], deposit.group(), raw)
        elif not deposit:
            column_deposit, column_raw = _column_deposit(columns)
            if column_deposit:
                row_fields["deposit_amount"] = column_deposit
                row_proofs["deposit_amount"] = evidence(f"table:rent/tr:{index}/td:보증금", column_deposit, column_raw, raw)
        annual = re.search(r"연\s*([\d,.]+)\s*(만)?\s*[Kk][Mm]", raw)
        if annual:
            normalized = decimal_string(annual.group(1), multiplier=10_000 if annual.group(2) else 1)
            if normalized is not None and Decimal(normalized) == Decimal(normalized).to_integral_value():
                row_fields["annual_mileage_km"] = str(int(Decimal(normalized)))
                row_proofs["annual_mileage_km"] = evidence(f"table:rent/tr:{index}", row_fields["annual_mileage_km"], annual.group(), raw)
            else:
                review_flags.append("annual_mileage_not_integer")
        annotate_evidence(row_proofs, mode, source_identity={"item_id"}, displayed_fields=set(row_proofs) - {"item_id"})
        rows.append(Candidate(fields=row_fields, evidence=row_proofs, locator=f"jetcar:{item_id}:rent-row:{index}", extraction_method="jetcar_visible_html", value_origin="observed", source_visibility=visibility, review_flags=review_flags, evidence_mode=mode))
    if rows:
        return rows
    proofs["price"] = evidence("table:rent", None, visible_text(rent_table) or "No displayed monthly price row")
    annotate_evidence(proofs, mode, source_identity={"item_id"}, displayed_fields=set(proofs) - {"item_id"})
    return [Candidate(fields=fields, evidence=proofs, locator=f"jetcar:{item_id}", extraction_method="jetcar_visible_html_no_price", value_origin="observed", source_visibility=visibility, review_flags=["missing_displayed_price"], evidence_mode=mode)]
