"""Fail-closed extraction from Gongcar's rendered public quote table."""
from __future__ import annotations

import re

from bs4 import NavigableString, Tag

from ..models import Candidate, FetchResult, Source, stable_id
from .common import decimal_string, evidence, html_soup, is_visible, text, won


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
            raw = text(cell) or ""
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
    values = [text(column.select_one("p")) or text(column) or "" for column in columns]
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


def _public_candidates(table: Tag, table_index: int, visibility: str) -> list[Candidate]:
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
        candidates.append(Candidate(
            fields=fields, evidence=proofs, locator=f"gongcar:public-table:{table_index}:row:{row_index}",
            extraction_method="gongcar_rendered_public_table", value_origin="observed", source_visibility=visibility,
        ))
    return candidates


def extract(result: FetchResult, source: Source) -> list[Candidate]:
    soup = html_soup(result.content)
    visibility = "visible" if result.backend == "playwright" else "unconfirmed"
    candidates: list[Candidate] = []
    for table_index, table in enumerate(soup.select("table"), start=1):
        if not is_visible(table):
            continue
        public = _public_candidates(table, table_index, visibility)
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
            model = text(model_node)
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
            candidates.append(Candidate(fields=fields, evidence=proofs, locator=f"gongcar:table:{table_index}:row:{row_index}", extraction_method="gongcar_rendered_quote_table", value_origin="observed", source_visibility=visibility))
    return candidates
