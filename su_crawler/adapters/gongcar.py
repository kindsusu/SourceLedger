"""Fail-closed extraction from Gongcar's rendered public quote table."""
from __future__ import annotations

import re
from urllib.parse import urlparse

from bs4 import NavigableString, Tag

from ..models import Candidate, FetchResult, Source, stable_id
from .common import annotate_evidence, decimal_string, evidence, evidence_mode, html_soup, is_visible, text, visible_text, won


def _rental_path(path: str) -> str:
    """Keep legacy observation locators stable across the site's redirects."""
    path = path.strip("/")
    head, separator, tail = path.partition("/")
    return {"monthly": "monthly-rent", "daily": "daily-rent"}.get(head, head) + separator + tail


def _expanded_rows(table: Tag) -> list[list[tuple[Tag, str]]]:
    pending: dict[int, tuple[int, Tag, str]] = {}
    result: list[list[tuple[Tag, str]]] = []
    for tr in table.select("tr"):
        if not is_visible(tr):
            continue
        row: list[tuple[Tag, str]] = []
        column = 0
        cells = iter(tr.find_all(["th", "td"], recursive=False))
        while True:
            while column in pending:
                left, node, raw = pending[column]
                row.append((node, raw))
                if left <= 1:
                    del pending[column]
                else:
                    pending[column] = (left - 1, node, raw)
                column += 1
            try:
                cell = next(cells)
            except StopIteration:
                break
            raw = _visible_text(cell)
            try:
                span = int(cell.get("colspan", 1) or 1)
                rowspan = int(cell.get("rowspan", 1) or 1)
            except (TypeError, ValueError):
                return []
            if not 1 <= span <= 1000 or not 1 <= rowspan <= 1000:
                return []
            for _ in range(span):
                row.append((cell, raw))
                if rowspan > 1:
                    pending[column] = (rowspan - 1, cell, raw)
                column += 1
        if row:
            result.append(row)
    return result


def _visible_text(node: Tag) -> str:
    """Read rendered cell text without hidden tooltip/template duplicates."""
    parts = [str(value).strip() for value in node.descendants
             if isinstance(value, NavigableString) and str(value).strip()
             and isinstance(value.parent, Tag) and is_visible(value.parent)]
    return re.sub(r"\s+", " ", " ".join(parts)).strip()


def _public_headers(table: Tag) -> list[str] | None:
    header = table.find_previous_sibling("div")
    if not isinstance(header, Tag) or not is_visible(header):
        return None
    columns = header.find_all("div", recursive=False)
    values = [_visible_text(column.select_one("p") or column) for column in columns]
    if len(values) != 7:
        return None
    if not (
        "운전연령" in values[1]
        and "옵션" in values[2]
        and "차량가액" in values[3] and "원" in values[3]
        and "보증금" in values[4] and "원" in values[4]
        and "월" in values[5] and ("렌트" in values[5] or "대여" in values[5]) and "원" in values[5]
    ):
        return None
    return values


def _won_integer(value: str) -> str | None:
    raw = value.strip()
    if not re.fullmatch(r"(?:0|[1-9]\d*|[1-9]\d{0,2}(?:,\d{3})+)", raw):
        return None
    return raw.replace(",", "")


def _public_candidates(table: Tag, table_index: int, visibility: str, mode: str) -> list[Candidate]:
    headers = _public_headers(table)
    if headers is None:
        return []
    rows = _expanded_rows(table)
    if not rows:
        return []
    model_col, age_col, option_col, vehicle_col, deposit_col, price_col = range(6)
    price_basis = headers[price_col]
    age_basis_match = re.search(r"만\s*\d+\s*세\s*기준", price_basis)
    price_age_basis = age_basis_match.group(0) if age_basis_match else None
    candidates: list[Candidate] = []
    for row_index, row in enumerate(rows, start=1):
        if len(row) != 7 or any(index >= len(row) for index in range(6)):
            continue
        values = [_visible_text(node) for node, _ in row]
        model, driver_age, option = values[model_col], values[age_col], values[option_col]
        vehicle_value = _won_integer(values[vehicle_col])
        deposit = _won_integer(values[deposit_col])
        price = _won_integer(values[price_col])
        if not all((model, driver_age, option, vehicle_value, deposit, price)):
            continue
        trim_match = re.search(r"\(([^()]*)\)\s*$", model)
        trim = trim_match.group(1).strip() if trim_match else None
        item_id = "gongcar-row-" + stable_id(model, trim, option, values[deposit_col])
        raw_row = " | ".join(values)
        location = f"table:{table_index}/tbody/tr:{row_index}"
        fields = {
            "item_id": item_id, "name": model, "model": model,
            "driver_age_condition": driver_age, "options": option,
            "vehicle_value": vehicle_value, "deposit_amount": deposit,
            "price": price, "currency": "KRW", "price_basis": "monthly",
        }
        if price_age_basis:
            fields["price_age_basis"] = price_age_basis
        if trim:
            fields["trim"] = trim
        proofs = {
            "item_id": evidence(location, item_id, raw_row),
            "name": evidence(f"{location}/td:{model_col + 1}", model, values[model_col], raw_row),
            "model": evidence(f"{location}/td:{model_col + 1}", model, values[model_col], raw_row),
            "driver_age_condition": evidence(f"{location}/td:{age_col + 1}", driver_age, values[age_col], raw_row),
            "options": evidence(f"{location}/td:{option_col + 1}", option, values[option_col], raw_row),
            "vehicle_value": evidence(f"{location}/td:{vehicle_col + 1}", vehicle_value, values[vehicle_col], raw_row),
            "deposit_amount": evidence(f"{location}/td:{deposit_col + 1}", deposit, values[deposit_col], raw_row),
            "price": evidence(f"{location}/td:{price_col + 1}", price, values[price_col], raw_row),
            "currency": evidence(f"table:{table_index}/header:{price_col + 1}", "KRW", headers[price_col]),
            "price_basis": evidence(f"table:{table_index}/header:{price_col + 1}", "monthly", headers[price_col]),
        }
        if trim:
            proofs["trim"] = evidence(f"{location}/td:{model_col + 1}", trim, values[model_col], raw_row)
        if price_age_basis:
            proofs["price_age_basis"] = evidence(f"table:{table_index}/header:{price_col + 1}", price_age_basis, headers[price_col])
        annotate_evidence(proofs, mode, source_identity={"item_id"}, displayed_fields=set(proofs) - {"item_id"})
        candidates.append(Candidate(
            fields=fields, evidence=proofs, locator=f"gongcar:public-table:{table_index}:row:{row_index}",
            extraction_method="gongcar_rendered_public_table", value_origin="observed", source_visibility=visibility,
            evidence_mode=mode,
        ))
    return candidates


_POPULAR_HEADING = re.compile(r"인기\s*차량\s*한눈에\s*보기")
_PRICE = re.compile(r"^월\s*([\d,]+)\s*원\s*(~)?$")
_FOOTNOTE = re.compile(r"(\d+)\s*개월\s*/\s*(보증금|선납금)\s*(\d+(?:\.\d+)?)\s*(%|원)\s*기준")


def _section(soup, kind: str) -> Tag | None:
    headings = [node for node in soup.select("h2,button > span") if is_visible(node) and
                (_POPULAR_HEADING.search(visible_text(node) or "") if kind == "popular"
                 else "특가" in (visible_text(node) or "") and
                 not _POPULAR_HEADING.search(visible_text(node) or ""))]
    sections = [node.find_parent("section") for node in headings]
    sections = [node for node in sections if node is not None]
    # The title sometimes lives in a nested hero. Pick the deepest section
    # that actually contains promotional vehicle cards.
    sections = [node for node in sections if _cards(node, kind)]
    unique = list(dict.fromkeys(id(node) for node in sections))
    return next((node for node in sections if id(node) == unique[0]), None) if len(unique) == 1 else None


def _cards(section: Tag | None, kind: str) -> list[Tag]:
    if section is None:
        return []
    if kind == "popular":
        grids = [node for node in section.select("div.grid") if is_visible(node)
                 and node.find_all("button", recursive=False)]
        return grids[0].find_all("button", recursive=False) if len(grids) == 1 else []
    # Product grids identify promotional cards even when lazy images are absent.
    # The older layout has no grid, so retain its image-marked card fallback.
    grids = [node for node in section.select("div.grid") if is_visible(node)
             and node.find_all("button", recursive=False)]
    if grids:
        return [card for grid in grids for card in grid.find_all("button", recursive=False)]
    return [node for node in section.select("button") if node.select_one("img[alt]")]


def _basis(section: Tag | None) -> tuple[str, str, str, str] | None:
    if section is None:
        return None
    matches = []
    for node in section.select("p"):
        raw = visible_text(node)
        match = _FOOTNOTE.search(raw or "") if is_visible(node) else None
        if match:
            matches.append((match.group(1), match.group(2), match.group(3), match.group(4), raw))
    return matches[0] if len(matches) == 1 else None


def _price_nodes(card: Tag) -> list[tuple[Tag, str, bool]]:
    amounts = []
    for node in card.select("span,del,s"):
        if not is_visible(node):
            continue
        raw = visible_text(node) or ""
        if _PRICE.fullmatch(raw):
            if any(_PRICE.fullmatch(visible_text(parent) or "") for parent in node.parents
                   if isinstance(parent, Tag) and parent.name in {"span", "del", "s"} and parent is not card):
                continue
            struck = (node.name in {"del", "s"} or "line-through" in node.get("class", [])
                      or "line-through" in str(node.get("style", "")))
            amounts.append((node, raw, struck))
    return amounts


def _card_candidate(card: Tag, path: str, kind: str, index: int,
                    basis: tuple[str, str, str, str, str], visibility: str, mode: str) -> Candidate | None:
    image = card.select_one("img[alt]")
    image_name = re.sub(r"\s+", " ", str(image.get("alt", "")).strip()) if image else ""
    labelled = [node for node in card.select("span,p") if is_visible(node)
                and "truncate" in node.get("class", []) and not _PRICE.fullmatch(visible_text(node) or "")]
    if len(labelled) == 1:
        name_nodes = labelled
    else:
        name_nodes = [node for node in card.select("span,p") if is_visible(node)
                      and visible_text(node) == image_name]
    if len(name_nodes) != 1:
        return None
    name_node = name_nodes[0]
    name = visible_text(name_node) or ""
    if not name:
        return None
    amounts = _price_nodes(card)
    current = [(node, raw) for node, raw, struck in amounts if not struck]
    original = [(node, raw) for node, raw, struck in amounts if struck]
    ambiguous_prices = len(current) > 1 or len(original) > 1
    if ambiguous_prices:
        current, original = [], []
    term, condition, percentage, unit, footnote = basis
    if kind == "popular" and unit != "%":
        return None
    if kind == "promotion" and condition == "보증금" and unit == "원" and percentage != "0":
        return None
    locator = f"gongcar:{path}:{kind}:card:{index}"
    card_text = visible_text(card) or ""
    public_id_attribute = "data-car-id" if card.get("data-car-id") else "data-item-id"
    public_id = card.get(public_id_attribute)
    item_id = "local-observation-gongcar-" + stable_id(path, kind, str(index), card_text, footnote)
    fields = {"item_id": item_id, "name": name, "currency": "KRW",
              "price_basis": "monthly", "term_months": term, "section": f"{path}_{kind}"}
    proofs = {"item_id": evidence(locator, item_id, card_text, proof_kind="local_observation"),
              "name": evidence(f"{locator}/name", name, visible_text(name_node), card_text),
              "section": evidence(locator, fields["section"], card_text),
              "term_months": evidence(f"gongcar:{path}:{kind}:footnote", term, footnote)}
    if public_id:
        fields["observed_source_id"] = str(public_id)
        proofs["observed_source_id"] = evidence(f"{locator}/{public_id_attribute}", str(public_id), str(public_id),
                                                 card_text, proof_kind="source_identity")
    if condition == "보증금":
        field = "deposit_percent" if unit == "%" else "deposit_amount"
    else:
        field = "advance_percent" if unit == "%" else "advance_amount"
    fields[field] = percentage
    proofs[field] = evidence(f"gongcar:{path}:{kind}:footnote", percentage, footnote)
    if current:
        node, raw = current[0]
        match = _PRICE.fullmatch(raw)
        if match:
            fields["price"] = decimal_string(match.group(1))
            proofs["price"] = evidence(f"{locator}/price", fields["price"], raw, card_text)
            proofs["currency"] = evidence(f"{locator}/price", "KRW", raw, card_text)
            proofs["price_basis"] = evidence(f"{locator}/price", "monthly", raw, card_text)
    if original:
        node, raw = original[0]
        match = _PRICE.fullmatch(raw)
        if match:
            fields["reference_price"] = decimal_string(match.group(1))
            proofs["reference_price"] = evidence(f"{locator}/reference_price", fields["reference_price"], raw, card_text)
    if "currency" not in proofs:
        fields.pop("currency")
        fields.pop("price_basis")
    option_node = name_node.find_next_sibling("span") if kind == "popular" else None
    option = visible_text(option_node) if option_node and is_visible(option_node) else None
    if option and not _PRICE.fullmatch(option):
        fields["options"] = option
        proofs["options"] = evidence(f"{locator}/option", option, option, card_text)
    derived = {"identity_scope": "local_observation"}
    flags = ["cross_run_identity_uncertain"]
    if current and _PRICE.fullmatch(current[0][1]).group(2):
        derived["price_kind"] = "from"
        flags.append("starting_price_not_fixed_quote")
    if not current:
        flags.append("current_price_role_unresolved")
    if ambiguous_prices:
        flags.append("multiple_unresolved_price_nodes")
    if original and not current:
        flags.append("reference_price_without_current_price")
    if kind == "promotion" and current and not original:
        flags.append("promotion_price_role_unconfirmed")
    update_nodes = [node for node in card.find_parent("section").select("p")
                    if is_visible(node) and "최근 업데이트" in (visible_text(node) or "")]
    if len(update_nodes) == 1:
        update_raw = visible_text(update_nodes[0]) or ""
        update_match = re.search(r"최근 업데이트\s*:\s*([^|]+)", update_raw)
        if update_match:
            fields["update_text"] = update_match.group(1).strip()
            proofs["update_text"] = evidence(f"gongcar:{path}:{kind}:footnote", fields["update_text"], update_raw)
    annotate_evidence(proofs, mode, source_identity={"item_id", "section", "observed_source_id"},
                      displayed_fields=set(proofs) - {"item_id", "section", "observed_source_id"})
    return Candidate(fields=fields, evidence=proofs, locator=locator,
                     extraction_method=f"gongcar_{path}_{kind}_cards", value_origin="observed",
                     source_visibility=visibility, derived_values=derived, review_flags=flags, evidence_mode=mode)


def _catalog_candidates(soup, path: str, visibility: str, mode: str) -> list[Candidate]:
    candidates = []
    for kind in ("popular", "promotion"):
        section = _section(soup, kind)
        basis = _basis(section)
        if basis is None:
            continue
        for index, card in enumerate(_cards(section, kind), start=1):
            if not is_visible(card):
                continue
            candidate = _card_candidate(card, path, kind, index, basis, visibility, mode)
            if candidate:
                candidates.append(candidate)
    return candidates


def section_coverage(result: FetchResult, candidates: list[Candidate]) -> list[dict[str, object]]:
    """Report discovery separately from extraction and browser list completion."""
    soup = html_soup(result.content)
    path = _rental_path(urlparse(result.final_url).path)
    from .gongcar_price_sheet import matches as sheet_matches, coverage as sheet_coverage
    if sheet_matches(soup):
        return sheet_coverage(result, candidates, path)
    from .gongcar_landing import matches, coverage as landing_coverage
    if matches(soup):
        return landing_coverage(result, candidates, path)
    if path in {"monthly-rent", "daily-rent"}:
        from .gongcar_short_rental import section_coverage as short_coverage
        return short_coverage(result, candidates, path)
    if re.fullmatch(r"(?:monthly-rent|daily-rent)/[^/]+", path):
        from .gongcar_short_rental import detail_coverage
        return detail_coverage(result, candidates, path)
    if path not in {"rentpass", "longterm"}:
        return []
    coverage = []
    for kind in ("popular", "promotion"):
        key = f"{path}_{kind}"
        section = _section(soup, kind)
        cards = [card for card in _cards(section, kind) if is_visible(card)]
        observed = len(cards)
        extracted = [candidate for candidate in candidates if candidate.fields.get("section") == key]
        price_count = sum(candidate.fields.get("price") is not None for candidate in extracted)
        unparsed = max(0, observed - len(extracted))
        status, reason = "unknown", "section_not_discovered"
        click_count = None
        if section is not None:
            status, reason = "partial", "section_discovered"
            if not observed:
                status, reason = "unknown", "cards_not_observed"
            elif unparsed:
                reason = "unparsed_cards"
            elif kind == "promotion":
                events = [event for event in result.trace if event.get("event") in {"promotion_inspection", "list_expansion"}
                          and event.get("section") in {"promotion", key}]
                event = events[-1] if events else None
                click_count = event.get("click_count") if event else None
                if (result.backend == "playwright" and event and event.get("status") == "complete"
                        and event.get("observed_count") == observed):
                    status, reason = "complete", str(event.get("reason") or "overflow_inspected")
                else:
                    reason = "promotion_overflow_unconfirmed"
            else:
                events = [event for event in result.trace if event.get("event") == "list_expansion"
                          and event.get("section") == "popular"]
                event = events[-1] if events else None
                click_count = event.get("click_count") if event else None
                if (result.backend == "playwright" and event and event.get("status") == "complete"
                        and event.get("observed_count") == observed):
                    status, reason = "complete", str(event.get("reason") or "list_expansion_complete")
                else:
                    reason = str(event.get("reason") or "list_expansion_unconfirmed") if event else "list_expansion_unconfirmed"
        coverage.append({"section": key, "observed_count": observed, "extracted_count": len(extracted),
                         "price_count": price_count, "unparsed_count": unparsed,
                         "status": status, "reason": reason, "click_count": click_count})
    return coverage


def extract(result: FetchResult, source: Source) -> list[Candidate]:
    soup = html_soup(result.content)
    mode = evidence_mode(result)
    visibility = "visible" if mode == "rendered_dom" else "unconfirmed"
    path = _rental_path(urlparse(result.final_url or source.location).path)
    from .gongcar_price_sheet import matches as sheet_matches, extract as sheet_extract
    if sheet_matches(soup):
        return sheet_extract(result, path, mode, visibility)
    from .gongcar_landing import matches, extract as landing_extract
    if matches(soup):
        return landing_extract(result, path, mode, visibility)
    if re.fullmatch(r"monthly-rent/[^/]+", path):
        from .gongcar_short_rental import extract_monthly_detail
        return extract_monthly_detail(result, path, mode, visibility)
    if re.fullmatch(r"daily-rent/[^/]+", path):
        from .gongcar_short_rental import extract_daily_detail
        return extract_daily_detail(result, path, mode, visibility)
    if path in {"monthly-rent", "daily-rent"}:
        from .gongcar_short_rental import extract as short_extract
        return short_extract(result, path, mode, visibility)
    if path in {"rentpass", "longterm"}:
        catalog = _catalog_candidates(soup, path, visibility, mode)
        if catalog:
            return catalog
    candidates: list[Candidate] = []
    for table_index, table in enumerate(soup.select("table"), start=1):
        if not is_visible(table):
            continue
        public = _public_candidates(table, table_index, visibility, mode)
        if public:
            candidates.extend(public)
            continue
        rows = _expanded_rows(table)
        header_at = next((i for i, row in enumerate(rows) if any("월" in raw and ("렌트" in raw or "대여" in raw) for _, raw in row)), None)
        if header_at is None:
            continue
        headers = [raw for _, raw in rows[header_at]]
        price_col = next((i for i, raw in enumerate(headers) if "월" in raw and ("렌트" in raw or "대여" in raw)), None)
        if price_col is None:
            continue
        for row_index, row in enumerate(rows[header_at + 1 :], start=header_at + 2):
            if price_col >= len(row) or not is_visible(row[price_col][0]):
                continue
            raw_row = " | ".join(raw for _, raw in row)
            price_raw = row[price_col][1]
            price = won(price_raw)
            if not price:
                continue
            option = row[0][1] if row and row[0][1] else None
            deposit_cell = next((raw for _, raw in row if "보증금" in raw or re.search(r"\b(?:10|20|30)\s*%", raw)), None)
            deposit = won(deposit_cell)
            percent_match = re.search(r"(\d+)\s*%", deposit_cell or "")
            model_node = soup.select_one("[data-car-id], [data-item-id], h1, h2")
            if not is_visible(model_node):
                model_node = None
            model = _visible_text(model_node) if model_node else None
            actual_id = model_node.get("data-car-id") or model_node.get("data-item-id") if model_node else None
            if not actual_id:
                actual_id = table.get("data-car-id") or table.get("data-item-id")
            if not actual_id:
                continue
            item_id = f"{actual_id}:option={option or ''}:deposit={deposit_cell or ''}"
            fields = {"item_id": item_id, "price": price, "currency": "KRW", "price_basis": "monthly"}
            proofs = {
                "item_id": evidence(f"table:{table_index}/tr:{row_index}", item_id, raw_row),
                "price": evidence(f"table:{table_index}/tr:{row_index}/td:{price_col + 1}", price, price_raw, raw_row),
                "currency": evidence(f"table:{table_index}/tr:{row_index}/td:{price_col + 1}", "KRW", "원", raw_row),
                "price_basis": evidence(f"table:{table_index}/thead", "monthly", headers[price_col]),
            }
            for key, value, raw in (("name", model, text(model_node)), ("options", option, option), ("deposit_amount", deposit, deposit_cell), ("deposit_percent", percent_match.group(1) if percent_match else None, deposit_cell)):
                if value is not None:
                    fields[key] = value
                    proofs[key] = evidence(f"table:{table_index}/tr:{row_index}", value, raw, raw_row)
            annotate_evidence(proofs, mode, source_identity={"item_id"}, displayed_fields=set(proofs) - {"item_id"})
            candidates.append(Candidate(fields=fields, evidence=proofs, locator=f"gongcar:table:{table_index}:row:{row_index}", extraction_method="gongcar_rendered_quote_table", value_origin="observed", source_visibility=visibility, evidence_mode=mode))
    return candidates
