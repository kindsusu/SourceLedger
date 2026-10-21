"""Replay Amazoncar prices from captured, rendered public HTML.

The mode and selected conditions belong to the snapshot, not the URL. Keep
separate rent/lease snapshots and never synthesize unvisited combinations.
"""
from __future__ import annotations

import re
from urllib.parse import urlparse

from bs4 import BeautifulSoup, Tag

from ..models import Candidate, FetchResult, Source
from .common import display_state, is_visible, visible_text


_DETAIL = re.compile(r"/cars/(?:new/\d+/\d+|used/\d+)/?$")
_PRICE = re.compile(r"([\d,]+)\s*원")
_TERM = re.compile(r"([1-9]\d*)\s*개월")
_DURATIONS = ("12시간", "1~2일", "3~4일", "5~6일", "7일 이상")
_CAPTURE_REVIEW_FLAGS = frozenset({
    "identical_amounts_after_network_quiet_review",
    "no_displayed_quotes_review",
})
_CONDITION_LABELS = {
    "보증금": "deposit_amount",
    "선납금": "advance_amount",
    "운전자 연령": "driver_age_condition",
}


def _soup(result: FetchResult) -> BeautifulSoup:
    # Rendered Amazoncar snapshots are UTF-8 but have no reliable meta charset.
    return BeautifulSoup(result.content.decode("utf-8", errors="replace"), "html.parser")


def _capture_review_flags(result: FetchResult) -> list[str]:
    flags: list[str] = []
    for record in result.trace:
        if not isinstance(record, dict) or record.get("event") != "amazoncar_capture_integrity":
            continue
        values = record.get("flags")
        if not isinstance(values, list):
            continue
        for value in values:
            if isinstance(value, str) and value in _CAPTURE_REVIEW_FLAGS and value not in flags:
                flags.append(value)
    return flags


def _proof(raw: str, location: str, node: Tag, mode: str, *, rule: str | None = None,
           source_text: str | None = None) -> dict[str, object]:
    state, hidden = display_state(node, mode)
    proof: dict[str, object] = {"raw": raw, "location": location, "display_state": state,
                                "proof_kind": "rendered_text" if mode == "rendered_dom" else "html_text"}
    if hidden:
        proof["hidden_by"] = hidden
    if rule:
        proof["normalization"] = {"rule": rule}
    if source_text:
        proof["source_text"] = source_text
    return proof


def _mode(soup: BeautifulSoup) -> tuple[str, str, Tag] | None:
    switch = soup.select_one('div[class*="choice-rent-lease"]')
    if not switch or not is_visible(switch):
        return None
    active = [button for button in switch.select("button")
              if is_visible(button) and any("active" in cls for cls in button.get("class", []))]
    if len(active) != 1:
        return None
    label = visible_text(active[0])
    values = {"장기렌트": "rent", "리스": "lease"}
    return (values[label], label, active[0]) if label in values else None


def _selected_conditions(soup: BeautifulSoup, evidence_mode: str) -> tuple[dict[str, str], dict[str, dict[str, object]], list[str]]:
    fields: dict[str, str] = {}
    evidence: dict[str, dict[str, object]] = {}
    flags: list[str] = []
    for section in soup.select('div[class*="default-box"]'):
        if not is_visible(section):
            continue
        title = section.select_one('div[class*="title-row"] [class*="title"]')
        label = visible_text(title)
        if label not in {*_CONDITION_LABELS, "연간 약정운행거리"}:
            continue
        # Only a choice box's active button is a selection. The title-row
        # summary button repeats the value without proving its chosen state.
        choices = section.select_one('[class*="choice-box-with-info"]')
        selected = [button for button in choices.select("button")
                    if is_visible(button) and any("active" in cls for cls in button.get("class", []))] if choices else []
        if label == "운전자 연령" and not selected and choices is None:
            # Used stock pages render the fixed age as default text, with no
            # selectable control. Both duplicated displayed values must agree.
            fixed = [node for node in section.select('[class*="default-text"]')
                     if is_visible(node) and visible_text(node)]
            values = {visible_text(node) for node in fixed}
            if len(values) == 1 and fixed:
                name = _CONDITION_LABELS[label]
                fields[name] = visible_text(fixed[0]) or ""
                evidence[name] = _proof(fields[name],
                                        'css:div[class*="default-box"]:운전자 연령 [class*="default-text"]',
                                        fixed[0], evidence_mode)
                continue
        if len(selected) != 1:
            flags.append(f"ambiguous_selected_condition:{label}")
            continue
        button = selected[0]
        raw = visible_text(button) or ""
        location = f'css:div[class*="default-box"]:{label} button[class*="active"]'
        if label == "연간 약정운행거리":
            match = re.fullmatch(r"([\d,]+)\s*km", raw, re.IGNORECASE)
            if match:
                fields["annual_mileage_km"] = match.group(1).replace(",", "")
                evidence["annual_mileage_km"] = _proof(raw, location, button, evidence_mode,
                                                       rule="kilometers")
            else:
                flags.append("unparsed_selected_condition:annual_mileage")
            continue
        name = _CONDITION_LABELS[label]
        if name in {"deposit_amount", "advance_amount"}:
            price_span = button.select_one('[class*="price"]')
            match = _PRICE.fullmatch(visible_text(price_span) or "")
            if not match or price_span is None:
                flags.append(f"unparsed_selected_condition:{label}")
                continue
            fields[name] = match.group(1).replace(",", "")
            evidence[name] = _proof(visible_text(price_span) or "", location + ' [class*="price"]',
                                    price_span, evidence_mode, rule="korean_won_amount", source_text=raw)
        elif raw:
            fields[name] = raw
            evidence[name] = _proof(raw, location, button, evidence_mode)
    return fields, evidence, flags


def extract_long_term(result: FetchResult, source: Source) -> list[Candidate]:
    path = urlparse(result.final_url or source.location).path
    match = _DETAIL.fullmatch(path)
    if not match:
        return []
    soup = _soup(result)
    selected_mode = _mode(soup)
    if selected_mode is None:
        return []
    mode, mode_raw, mode_button = selected_mode
    expected_mode = next((value for value in ("rent", "lease") if source.id.endswith("-" + value)), None)
    if expected_mode is not None and mode != expected_mode:
        return []
    proof_mode = "rendered_dom" if result.backend == "playwright" else "static_html"
    item_id = path.removeprefix("/cars/").rstrip("/")
    name_nodes = [node for node in soup.select('h1[class*="name"]') if is_visible(node) and visible_text(node)]
    name_node = name_nodes[0] if len(name_nodes) == 1 else None
    conditions, condition_proofs, condition_flags = _selected_conditions(soup, proof_mode)
    capture_flags = _capture_review_flags(result)
    boxes = [box for box in soup.select('div[class*="choice-box"]')
             if is_visible(box) and "대여 개월을 선택하세요" in (visible_text(box.select_one('p[class*="title"]')) or "")]
    if len(boxes) != 1:
        return []
    box = boxes[0]
    note = next((node for node in box.select('p[class*="info"]')
                 if "부가세 포함" in (visible_text(node) or "")), None)
    results: list[Candidate] = []
    for index, button in enumerate(box.select("button"), start=1):
        if not is_visible(button):
            continue
        raw = visible_text(button) or ""
        term_match = _TERM.search(raw)
        if not term_match:
            continue
        price_span = button.select_one('[class*="price"]')
        price_raw = visible_text(price_span)
        price_match = _PRICE.fullmatch(price_raw or "")
        term_node = next((s for s in button.find_all("span") if _TERM.fullmatch(visible_text(s) or "")), None)
        locator = f'css:div[class*="choice-box"]:term[{index}]'
        fields: dict[str, str] = {"item_id": item_id, "contract_mode": mode_raw}
        evidence: dict[str, dict[str, object]] = {
            "item_id": {"location": "url:path", "raw": item_id, "source_text": result.final_url or source.location,
                        "display_state": "not_applicable", "proof_kind": "source_identity"},
            "contract_mode": _proof(mode_raw, 'css:div[class*="choice-rent-lease"] button[class*="active"]',
                                    mode_button, proof_mode),
        }
        terms = set(_TERM.findall(raw))
        if term_node is not None:
            fields["term_months"] = term_match.group(1)
            evidence["term_months"] = _proof(visible_text(term_node) or "", locator + " span:term", term_node,
                                             proof_mode, rule="korean_months", source_text=raw)
        elif len(terms) == 1:
            # Displayed in the button text, but not as its own label element.
            fields["term_months"] = next(iter(terms))
            evidence["term_months"] = _proof(term_match.group(0), locator, button, proof_mode,
                                             rule="korean_months", source_text=raw)
        fields.update(conditions)
        evidence.update(condition_proofs)
        if name_node is not None:
            fields["name"] = visible_text(name_node) or ""
            evidence["name"] = _proof(fields["name"], 'css:h1[class*="name"]', name_node, proof_mode)
        flags = [*condition_flags, *capture_flags]
        if term_node is None:
            # The period label is not its own element, so the price cannot be
            # bound to it exactly. Keep the displayed option with a null price.
            flags.append("term_label_not_exact_span")
            evidence["price"] = {"raw": None, "location": locator, "source_text": raw,
                                 "display_state": display_state(button, proof_mode)[0],
                                 "proof_kind": "rendered_text" if proof_mode == "rendered_dom" else "html_text"}
        elif price_match and price_span is not None:
            fields["price"] = price_match.group(1).replace(",", "")
            evidence["price"] = _proof(price_raw or "", locator + ' span[class*="price"]', price_span,
                                       proof_mode, rule="korean_won_amount", source_text=raw)
            fields["currency"] = "KRW"
            evidence["currency"] = _proof("원", locator + ' span[class*="price"]:suffix', price_span,
                                          proof_mode, rule="korean_won_currency", source_text=price_raw)
        else:
            flags.append("term_without_numeric_price")
        if note is not None:
            fields["tax"] = "included"
            evidence["tax"] = _proof("부가세 포함", 'css:div[class*="choice-box"] p[class*="info"]',
                                     note, proof_mode, rule="korean_vat_included", source_text=visible_text(note))
        # "대여 개월을 선택하세요" gives contract length, not billing frequency.
        # SourceLedger must leave price_basis absent until a bound monthly
        # statement is captured and verified for this quote.
        results.append(Candidate(fields, evidence, locator + f":{mode}", "amazoncar_rendered_detail",
                                 source_visibility="visible" if proof_mode == "rendered_dom" else "unconfirmed",
                                 evidence_mode=proof_mode, review_flags=flags))
    return results


def extract_short_term(result: FetchResult, source: Source) -> list[Candidate]:
    soup = _soup(result)
    proof_mode = "rendered_dom" if result.backend == "playwright" else "static_html"
    table = next((t for t in soup.select("table") if is_visible(t)
                  and all(duration in (visible_text(t) or "") for duration in _DURATIONS)), None)
    if table is None:
        return []
    heading = next((h for h in table.select("tr") if all(duration in (visible_text(h) or "") for duration in _DURATIONS)), None)
    if heading is None:
        return []
    basis_node = next((node for node in table.select("th")
                       if "대여 기간별 일일 요금" in (visible_text(node) or "")), None)
    tax_node = next((node for node in soup.select("p")
                     if (visible_text(node) or "") == "부가세 별도"), None)
    eligibility_node = next((node for node in soup.select("p")
                             if "단기렌트는 아마존카 기존 고객에게만 제공됩니다." in (visible_text(node) or "")), None)
    category = None
    category_node: Tag | None = None
    results: list[Candidate] = []
    rows = list(table.select("tbody tr"))
    for row_index, row in enumerate(rows, start=1):
        if row is heading or not is_visible(row):
            continue
        cells = row.find_all(["td", "th"], recursive=False)
        if len(cells) == 7:
            category = visible_text(cells[0])
            category_node = cells[0]
            vehicle, prices = cells[1], cells[2:]
        elif len(cells) == 6 and category and category_node is not None:
            vehicle, prices = cells[0], cells[1:]
        else:
            continue
        vehicle_raw = visible_text(vehicle)
        if not vehicle_raw or len(prices) != 5:
            continue
        for column, (duration, cell) in enumerate(zip(_DURATIONS, prices), start=1):
            raw = visible_text(cell) or ""
            if not raw:
                continue
            numeric = bool(re.fullmatch(r"[\d,]+", raw))
            locator = f"css:table:tariff tbody tr:{row_index} td:price[{column}]"
            fields = {"item_id": vehicle_raw, "vehicle_class": vehicle_raw,
                      "tariff_category": category, "rental_duration": duration}
            if numeric:
                fields = {"price": raw, **fields}
            else:
                # "문의" or "-": a displayed cell without a numeric tariff stays a
                # null-price candidate with its wording, never 0 or dropped.
                fields["availability"] = raw
            price_proof = _proof(raw, locator, cell, proof_mode)
            if not numeric:
                price_proof.update(raw=None, source_text=raw)
            evidence = {
                "price": price_proof,
                "item_id": _proof(vehicle_raw, f"css:table:tariff tbody tr:{row_index} td:vehicle", vehicle, proof_mode),
                "vehicle_class": _proof(vehicle_raw, f"css:table:tariff tbody tr:{row_index} td:vehicle", vehicle, proof_mode),
                "tariff_category": _proof(category, f"css:table:tariff tbody tr:{row_index} td:category(rowspan)",
                                          category_node, proof_mode),
                "rental_duration": _proof(duration, f"css:table:tariff tbody tr:1 td:duration[{column}]",
                                           heading.find_all(["td", "th"], recursive=False)[column + 1], proof_mode),
            }
            if basis_node is not None:
                fields["tariff_price_basis_raw"] = visible_text(basis_node) or ""
                evidence["tariff_price_basis_raw"] = _proof(fields["tariff_price_basis_raw"],
                                                               "css:table:tariff thead th:daily", basis_node, proof_mode)
            if tax_node is not None:
                fields["tax"] = "excluded"
                evidence["tax"] = _proof("부가세 별도", "css:p:short-term-tax", tax_node, proof_mode,
                                         rule="korean_vat_excluded")
            if not numeric:
                evidence["availability"] = _proof(raw, locator, cell, proof_mode)
            if eligibility_node is not None:
                fields["eligibility"] = "단기렌트는 아마존카 기존 고객에게만 제공됩니다."
                evidence["eligibility"] = _proof(fields["eligibility"], "css:p:short-term-eligibility",
                                                  eligibility_node, proof_mode)
            # The page says "일일 요금" but does not show an ISO currency.
            # Preserve this as a nonmonthly tariff; comparison is reviewed.
            results.append(Candidate(fields, evidence, locator, "amazoncar_rendered_tariff",
                                     source_visibility="visible" if proof_mode == "rendered_dom" else "unconfirmed",
                                     evidence_mode=proof_mode,
                                     review_flags=[] if numeric else ["tariff_price_not_numeric"]))
    return results


def extract(result: FetchResult, source: Source) -> list[Candidate]:
    path = urlparse(result.final_url or source.location).path
    if _DETAIL.fullmatch(path):
        return extract_long_term(result, source)
    if path.rstrip("/") == "/rentPrice":
        return extract_short_term(result, source)
    return []
