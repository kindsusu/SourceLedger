"""Rendered monthly and daily Gongcar catalog cards, with explicit coverage."""
from __future__ import annotations

import re

from bs4 import Tag

from ..models import Candidate, FetchResult, stable_id
from .common import annotate_evidence, decimal_string, evidence, html_soup, is_visible, visible_text, won


_PRICE = re.compile(r"^(월|일)\s*([\d,]+(?:\.\d+)?)\s*만원\s*(부터)?$")
_COUNT = re.compile(r"^총\s*(\d+)\s*종의\s*차량이\s*있습니다\.?$")
_TERM = re.compile(r"^(\d+)\s*개월$")
_DETAIL_PRICE = re.compile(r"^([\d,]+)\s*원$")


def _monthly_period_buttons(soup: Tag) -> list[Tag]:
    headings = [node for node in soup.select("h2") if is_visible(node)
                and visible_text(node) == "대여 기간"]
    if len(headings) != 1:
        return []
    section = headings[0].find_parent("section")
    return [button for button in section.find_all("button") if is_visible(button)] if section else []


def _monthly_basis_label(soup: Tag) -> Tag | None:
    labels = [node for node in soup.select("span") if is_visible(node)
              and visible_text(node) == "월 렌트료"]
    return labels[0] if labels else None


def _grid(soup: Tag) -> Tag | None:
    grids = [node for node in soup.select("div.grid") if is_visible(node)
             and any(is_visible(child) and child.find("h4") for child in node.find_all("div", recursive=False))]
    return grids[0] if len(grids) == 1 else None


def _cards(soup: Tag) -> list[Tag]:
    grid = _grid(soup)
    if grid is None:
        return []
    # Daily cards have a fade-in wrapper; monthly cards are direct children.
    return [node for node in grid.select("div.group") if is_visible(node)
            and len(node.find_all("h4")) == 1]


def _selected_term(soup: Tag) -> tuple[str, str] | None:
    matches = []
    for button in soup.select("button"):
        raw = visible_text(button) if is_visible(button) else None
        match = _TERM.fullmatch(raw or "")
        if match:
            matches.append((match.group(1), raw))
    return matches[0] if len(matches) == 1 else None


def _declared_count(soup: Tag) -> int | None:
    values = set()
    for node in soup.select("p"):
        raw = visible_text(node) if is_visible(node) else None
        match = _COUNT.fullmatch(raw or "")
        if match:
            values.add(int(match.group(1)))
    if len(values) == 1:
        return next(iter(values))
    # Daily catalog renders the active category title and count as sibling divs.
    # This is independent of how many card nodes have hydrated so far.
    category_counts = set()
    for row in soup.select("div.flex.items-center.gap-2"):
        if not is_visible(row):
            continue
        children = row.find_all("div", recursive=False)
        if len(children) != 2:
            continue
        title = visible_text(children[0]) or ""
        number = visible_text(children[1]) or ""
        if title in {"수입차", "국산차"} and re.fullmatch(r"\d+", number):
            category_counts.add(int(number))
    return next(iter(category_counts)) if len(category_counts) == 1 else None


def extract(result: FetchResult, path: str, mode: str, visibility: str) -> list[Candidate]:
    soup = html_soup(result.content)
    basis = "monthly" if path == "monthly-rent" else "daily"
    term = _selected_term(soup) if basis == "monthly" else None
    candidates = []
    for index, card in enumerate(_cards(soup), start=1):
        names = [node for node in card.find_all("h4") if is_visible(node)]
        if len(names) != 1:
            continue
        name = visible_text(names[0])
        if not name:
            continue
        raw_card = visible_text(card) or ""
        locator = f"gongcar:{path}:card:{index}"
        item_id = "local-observation-gongcar-" + stable_id(path, index, raw_card)
        section = "monthly_catalog" if basis == "monthly" else "daily_catalog"
        fields = {"item_id": item_id, "name": name, "section": section}
        proofs = {"item_id": evidence(locator, item_id, raw_card, proof_kind="local_observation"),
                  "name": evidence(f"{locator}/h4", name, visible_text(names[0]), raw_card),
                  "section": evidence(locator, section, raw_card)}
        flags = ["cross_run_identity_uncertain"]
        if term:
            fields["term_months"] = term[0]
            proofs["term_months"] = evidence(f"gongcar:{path}:selected-term", term[0], term[1])
        price_nodes = [node for node in card.select("span") if is_visible(node)
                       and _PRICE.fullmatch(visible_text(node) or "")]
        price_nodes = [node for node in price_nodes if not any(
            isinstance(parent, Tag) and parent.name == "span" and _PRICE.fullmatch(visible_text(parent) or "")
            for parent in node.parents if parent is not card)]
        if len(price_nodes) == 1:
            raw = visible_text(price_nodes[0]) or ""
            match = _PRICE.fullmatch(raw)
            if match and ((match.group(1) == "월") == (basis == "monthly")):
                price = decimal_string(match.group(2), multiplier=10_000)
                if price:
                    fields.update(price=price, currency="KRW", price_basis=basis)
                    proofs.update(price=evidence(f"{locator}/price", price, raw, raw_card),
                                  currency=evidence(f"{locator}/price", "KRW", raw, raw_card),
                                  price_basis=evidence(f"{locator}/price", basis, raw, raw_card))
                    if match.group(3):
                        flags.append("starting_price_not_fixed_quote")
        if "price" not in fields:
            flags.append("current_price_role_unresolved")
        derived = {"identity_scope": "local_observation"}
        if "starting_price_not_fixed_quote" in flags:
            derived["price_kind"] = "from"
        annotate_evidence(proofs, mode, source_identity={"item_id", "section"},
                          displayed_fields=set(proofs) - {"item_id", "section"})
        candidates.append(Candidate(fields=fields, evidence=proofs, locator=locator,
                                    extraction_method=f"gongcar_{path}_catalog_cards",
                                    value_origin="observed", source_visibility=visibility,
                                    derived_values=derived, review_flags=flags, evidence_mode=mode))
    return candidates


def section_coverage(result: FetchResult, candidates: list[Candidate], path: str) -> list[dict[str, object]]:
    soup = html_soup(result.content)
    section = "monthly_catalog" if path == "monthly-rent" else "daily_catalog"
    observed = len(_cards(soup))
    expected = _declared_count(soup)
    extracted = [candidate for candidate in candidates if candidate.fields.get("section") == section]
    priced = sum(candidate.fields.get("price") is not None for candidate in extracted)
    events = [event for event in result.trace if event.get("event") == "list_expansion"
              and event.get("section") == section]
    event = events[-1] if events else None
    click_count = event.get("click_count") if event else None
    status, reason = "unknown", "cards_not_observed"
    if observed:
        status, reason = "partial", "list_expansion_unconfirmed"
        if expected is not None and observed < expected:
            reason = "declared_count_not_reached"
        elif len(extracted) < observed:
            reason = "unparsed_cards"
        elif priced < observed:
            reason = "unpriced_cards"
        elif path == "monthly-rent" and _selected_term(soup) is None:
            reason = "selected_term_unconfirmed"
        elif expected is None:
            reason = "declared_count_unconfirmed"
        elif (result.backend == "playwright" and event and event.get("status") == "complete"
              and event.get("observed_count") == observed
              and expected == observed):
            status, reason = "complete", str(event.get("reason") or "list_expansion_complete")
        elif event:
            reason = str(event.get("reason") or "list_expansion_unconfirmed")
    return [{"section": section, "observed_count": observed, "expected_count": expected,
             "extracted_count": len(extracted), "price_count": priced,
             "unparsed_count": max(0, observed - len(extracted)), "status": status,
             "reason": reason, "click_count": click_count}]


def detail_coverage(result: FetchResult, candidates: list[Candidate], path: str) -> list[dict[str, object]]:
    soup = html_soup(result.content)
    monthly = path.startswith("monthly-rent/")
    section = "monthly_detail" if monthly else "daily_detail"
    if monthly:
        observed = len(_monthly_period_buttons(soup))
    else:
        labels = [node for node in soup.select("span") if is_visible(node)
                  and visible_text(node) == "일 렌트료"
                  and isinstance(node.parent, Tag)
                  and node.parent.select_one("span")]
        observed = 1 if labels else 0
    extracted = [candidate for candidate in candidates if candidate.fields.get("section") == section]
    priced = sum(candidate.fields.get("price") is not None for candidate in extracted)
    status, reason = "unknown", "detail_price_not_observed"
    if observed:
        status, reason = "partial", "unparsed_detail_prices"
        if result.backend != "playwright":
            reason = "rendered_visibility_unconfirmed"
        elif len(extracted) == observed and priced == observed:
            status, reason = "complete", "visible_detail_prices_parsed"
    return [{"section": section, "observed_count": observed, "expected_count": observed if observed else None,
             "extracted_count": len(extracted), "price_count": priced,
             "unparsed_count": max(0, observed - len(extracted)), "status": status,
             "reason": reason, "click_count": None}]


def extract_monthly_detail(result: FetchResult, path: str, mode: str, visibility: str) -> list[Candidate]:
    """Each rendered period button is a distinct observed monthly offer."""
    soup = html_soup(result.content)
    names = [node for node in soup.select("h1") if is_visible(node) and visible_text(node)]
    buttons = _monthly_period_buttons(soup)
    basis_label = _monthly_basis_label(soup)
    if len(names) != 1 or not buttons or basis_label is None:
        return []
    name = visible_text(names[0]) or ""
    upfront_rows = [node.parent for node in soup.select("span") if is_visible(node)
                    and visible_text(node) == "선납/보증금" and isinstance(node.parent, Tag)]
    upfront = None
    if len(upfront_rows) == 1:
        row = upfront_rows[0]
        amounts = [node for node in row.find_all("span") if is_visible(node)
                   and re.fullmatch(r"[\d,]+\s*만원", visible_text(node) or "")]
        if len(amounts) == 1:
            upfront = (visible_text(amounts[0]) or "", won(visible_text(amounts[0])))
    candidates = []
    for index, button in enumerate(buttons, start=1):
        labels = [node for node in button.find_all("span") if is_visible(node)
                  and _TERM.fullmatch(visible_text(node) or "")]
        prices = [node for node in button.find_all("span") if is_visible(node)
                  and _DETAIL_PRICE.fullmatch(visible_text(node) or "")
                  and not any(isinstance(child, Tag) and _DETAIL_PRICE.fullmatch(visible_text(child) or "")
                              for child in node.find_all("span"))]
        if len(labels) != 1 or len(prices) != 1:
            continue
        term = _TERM.fullmatch(visible_text(labels[0]) or "").group(1)
        price_raw = visible_text(prices[0]) or ""
        amount = decimal_string(_DETAIL_PRICE.fullmatch(price_raw).group(1))
        if amount is None:
            continue
        locator = f"gongcar:{path}:period:{index}"
        raw_button = visible_text(button) or ""
        item_id = "local-observation-gongcar-" + stable_id(path, term, raw_button, name)
        fields = {"item_id": item_id, "name": name, "section": "monthly_detail",
                  "term_months": term, "price": amount, "currency": "KRW", "price_basis": "monthly"}
        proofs = {"item_id": evidence(locator, item_id, raw_button, proof_kind="local_observation"),
                  "name": evidence(f"{locator}/h1", name, visible_text(names[0])),
                  "section": evidence(locator, "monthly_detail", raw_button),
                  "term_months": evidence(f"{locator}/period", term, visible_text(labels[0]), raw_button),
                  "price": evidence(f"{locator}/price", amount, price_raw, raw_button),
                  "currency": evidence(f"{locator}/price", "KRW", price_raw, raw_button),
                  "price_basis": evidence(f"gongcar:{path}:monthly-price-label", "monthly",
                                          visible_text(basis_label), raw_button)}
        if upfront and upfront[1]:
            fields["upfront_payment_label"] = "선납/보증금"
            fields["upfront_payment_amount"] = upfront[1]
            proofs["upfront_payment_label"] = evidence("gongcar:monthly-detail:rental-info", "선납/보증금", "선납/보증금")
            proofs["upfront_payment_amount"] = evidence("gongcar:monthly-detail:rental-info", upfront[1], upfront[0])
        annotate_evidence(proofs, mode, source_identity={"item_id", "section"},
                          displayed_fields=set(proofs) - {"item_id", "section"})
        candidates.append(Candidate(fields=fields, evidence=proofs, locator=locator,
                                    extraction_method="gongcar_monthly_detail_periods",
                                    value_origin="observed", source_visibility=visibility,
                                    derived_values={"identity_scope": "local_observation"},
                                    review_flags=["cross_run_identity_uncertain",
                                                  "deposit_or_advance_role_unresolved"],
                                    evidence_mode=mode))
    return candidates


def extract_daily_detail(result: FetchResult, path: str, mode: str, visibility: str) -> list[Candidate]:
    soup = html_soup(result.content)
    names = [node for node in soup.select("h1") if is_visible(node) and visible_text(node)]
    price_headings = [node for node in soup.select("h3") if is_visible(node)
                      and visible_text(node) == "렌트 가격"]
    if len(names) != 1 or len(price_headings) != 1:
        return []
    container = price_headings[0].parent.parent
    rows = [node for node in container.find_all("div") if is_visible(node)
            and len(node.find_all("span", recursive=False)) == 2
            and visible_text(node.find_all("span", recursive=False)[0]) == "일 렌트료"]
    if len(rows) != 1:
        return []
    amount_node = rows[0].find_all("span", recursive=False)[1]
    amount_raw = visible_text(amount_node) or ""
    match = _DETAIL_PRICE.fullmatch(amount_raw)
    if not match:
        return []
    price = decimal_string(match.group(1))
    if price is None:
        return []
    name = visible_text(names[0]) or ""
    locator = f"gongcar:{path}:daily-price"
    item_id = "local-observation-gongcar-" + stable_id(path, name, amount_raw)
    fields = {"item_id": item_id, "name": name, "section": "daily_detail", "price": price,
              "currency": "KRW", "price_basis": "daily"}
    proofs = {"item_id": evidence(locator, item_id, amount_raw, proof_kind="local_observation"),
              "name": evidence(f"{locator}/h1", name, visible_text(names[0])),
              "section": evidence(locator, "daily_detail", amount_raw),
              "price": evidence(f"{locator}/price", price, amount_raw),
              "currency": evidence(f"{locator}/price", "KRW", amount_raw),
              "price_basis": evidence(f"{locator}/price", "daily", "일 렌트료")}
    eligibility = [node for node in soup.select("h4") if is_visible(node)
                   and re.fullmatch(r"만\s*\d+\s*세\s*이상만", visible_text(node) or "")]
    if len(eligibility) == 1:
        raw = visible_text(eligibility[0]) or ""
        fields["driver_age_condition"] = raw
        proofs["driver_age_condition"] = evidence(f"{locator}/eligibility", raw, raw)
    age_notes = [node for node in soup.select("p") if is_visible(node)
                 and re.search(r"만\s*\d+\s*세\s*이상\s*기준\s*요금", visible_text(node) or "")]
    if len(age_notes) == 1:
        raw = visible_text(age_notes[0]) or ""
        basis = re.search(r"만\s*\d+\s*세\s*이상\s*기준\s*요금", raw).group(0)
        fields["price_age_basis"] = basis
        proofs["price_age_basis"] = evidence(f"{locator}/price-age-note", basis, raw)
    annotate_evidence(proofs, mode, source_identity={"item_id", "section"},
                      displayed_fields=set(proofs) - {"item_id", "section"})
    return [Candidate(fields=fields, evidence=proofs, locator=locator,
                      extraction_method="gongcar_daily_detail_price", value_origin="observed",
                      source_visibility=visibility, derived_values={"identity_scope": "local_observation"},
                      review_flags=["cross_run_identity_uncertain"], evidence_mode=mode)]
