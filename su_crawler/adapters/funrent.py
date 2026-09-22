"""Rendered-state adapter for Funrent's public estimator."""
from __future__ import annotations

import re

from bs4 import BeautifulSoup, Tag

from ..models import Candidate, FetchResult, Source
from .common import decimal_string, evidence, html_soup, is_visible, text, won


def _selected(select: Tag | None) -> Tag | None:
    if select is None:
        return None
    selected = select.select("option[selected]")
    return selected[0] if len(selected) == 1 else None


def extract(result: FetchResult, source: Source) -> list[Candidate]:
    soup = html_soup(result.content)
    visibility = "visible" if result.backend == "playwright" else "unconfirmed"
    car, dep, term = (_selected(soup.select_one(selector)) for selector in ("#estCar", "#estDep", "#estTerm"))
    amount_node = soup.select_one("#estAmt")
    if not all((car, dep, term, amount_node)) or not is_visible(amount_node):
        return []
    amount_raw = text(amount_node)
    basis_text = f"{text(soup.select_one('#estOut .cap')) or ''} {amount_raw or ''}"
    if not amount_raw or amount_raw == "—" or "원" not in amount_raw or "월" not in basis_text:
        return []
    price = won(amount_raw)
    car_id = car.get("value")
    dep_id = dep.get("value")
    term_id = term.get("value")
    if not price or not all((car_id, dep_id, term_id)):
        return []
    item_id = f"{car_id}:deposit={dep_id}:term={term_id}"
    fields = {"item_id": item_id, "name": text(car), "currency": "KRW", "price_basis": "monthly", "term_months": decimal_string(str(term_id))}
    proofs = {
        "item_id": evidence("css:#estCar/#estDep/#estTerm selected options", item_id, f"{text(car)} | {text(dep)} | {text(term)}"),
        "name": evidence("css:#estCar option[selected]", fields["name"], text(car)),
        "derived_values.estimated_price": evidence("css:#estAmt", price, amount_raw),
        "currency": evidence("css:#estAmt", "KRW", "원", amount_raw),
        "price_basis": evidence("css:#estOut .cap,#estAmt", "monthly", basis_text),
        "term_months": evidence("css:#estTerm option[selected]", fields["term_months"], text(term)),
    }
    dep_percent = re.search(r"(\d+(?:\.\d+)?)\s*%", text(dep) or "")
    if dep_percent:
        fields["deposit_percent"] = dep_percent.group(1)
        proofs["deposit_percent"] = evidence("css:#estDep option[selected]", fields["deposit_percent"], text(dep))
    dep_line = soup.select_one("#estDepLine")
    dep_raw = text(dep_line)
    if dep_raw and is_visible(dep_line):
        fields["displayed_deposit"] = dep_raw
        proofs["displayed_deposit"] = evidence("css:#estDepLine", dep_raw, dep_raw)
    note = soup.select_one("#estNote")
    note_raw = text(note)
    if note_raw and is_visible(note):
        if "보험료" in note_raw:
            fields["insurance"] = note_raw
            proofs["insurance"] = evidence("css:#estNote", note_raw, note_raw)
        if "자동차세" in note_raw:
            fields["tax"] = note_raw
            proofs["tax"] = evidence("css:#estNote", note_raw, note_raw)
    derived = {"estimated_price": price}
    flags = ["calculator_estimate"]
    if "~" in amount_raw or "부터" in amount_raw:
        derived["price_qualifier"] = "from"
        flags.append("starting_estimate_not_fixed_quote")
    if dep_raw and "약" in dep_raw:
        flags.append("displayed_approximate_deposit")
    return [Candidate(fields=fields, evidence=proofs, locator=f"funrent:{item_id}", extraction_method="funrent_rendered_calculator", value_origin="calculator_estimate", source_visibility=visibility, derived_values=derived, review_flags=flags)]
