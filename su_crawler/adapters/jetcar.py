"""Adapter for public Jetcar vehicle detail HTML."""
from __future__ import annotations

import re
from urllib.parse import urlparse

from bs4 import BeautifulSoup, Tag

from ..models import Candidate, FetchResult, Source
from .common import annotate_evidence, decimal_string, evidence, evidence_mode, html_soup, is_visible, visible_text, won


def _table(soup: BeautifulSoup, *needles: str) -> Tag | None:
    return next((table for table in soup.select("table") if is_visible(table) and all(n in (visible_text(table) or "") for n in needles)), None)


def _actual_id(result: FetchResult, source: Source, soup: BeautifulSoup) -> tuple[str | None, str | None]:
    canonical = soup.select_one('link[rel="canonical"]')
    urls = [canonical.get("href") if canonical else None, result.final_url, source.location]
    for url in urls:
        path = urlparse(str(url or "")).path
        match = re.search(r"/(sub0[23]01)/(\d+)/?$", path)
        if match:
            return match.group(2), str(url)
    return None, None


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
    for index, row in enumerate(rent_table.select("tr") if rent_table else [], start=1):
        if not is_visible(row):
            continue
        raw = visible_text(row) or ""
        term = re.search(r"(\d+)\s*개월", raw)
        price = re.search(r"월\s*([\d,]+)\s*원", raw)
        if not term or not price:
            continue
        row_fields, row_proofs = dict(fields), dict(proofs)
        row_fields.update(price=decimal_string(price.group(1)), currency="KRW", price_basis="monthly", term_months=term.group(1))
        for key in ("price", "price_basis", "term_months"):
            row_proofs[key] = evidence(f"table:rent/tr:{index}", row_fields[key], raw)
        row_proofs["currency"] = evidence(f"table:rent/tr:{index}", "KRW", "원", raw)
        deposit = re.search(r"보증금\s*[\d,.]+\s*(?:만원|원)", raw)
        if deposit and won(deposit.group()):
            row_fields["deposit_amount"] = won(deposit.group())
            row_proofs["deposit_amount"] = evidence(f"table:rent/tr:{index}", row_fields["deposit_amount"], deposit.group(), raw)
        annual = re.search(r"연\s*([\d,.]+)\s*(만)?\s*[Kk][Mm]", raw)
        if annual:
            row_fields["annual_mileage_km"] = decimal_string(annual.group(1), multiplier=10_000 if annual.group(2) else 1)
            row_proofs["annual_mileage_km"] = evidence(f"table:rent/tr:{index}", row_fields["annual_mileage_km"], annual.group(), raw)
        annotate_evidence(row_proofs, mode, source_identity={"item_id"}, displayed_fields=set(row_proofs) - {"item_id"})
        rows.append(Candidate(fields=row_fields, evidence=row_proofs, locator=f"jetcar:{item_id}:rent-row:{index}", extraction_method="jetcar_visible_html", value_origin="observed", source_visibility=visibility, evidence_mode=mode))
    if rows:
        return rows
    proofs["price"] = evidence("table:rent", None, visible_text(rent_table) or "No displayed monthly price row")
    annotate_evidence(proofs, mode, source_identity={"item_id"}, displayed_fields=set(proofs) - {"item_id"})
    return [Candidate(fields=fields, evidence=proofs, locator=f"jetcar:{item_id}", extraction_method="jetcar_visible_html_no_price", value_origin="observed", source_visibility=visibility, review_flags=["missing_displayed_price"], evidence_mode=mode)]
