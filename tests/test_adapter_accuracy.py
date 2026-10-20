"""Regression tests for adapter price/term accuracy (synthetic fixtures only)."""
from __future__ import annotations

import pytest

from su_crawler.adapters import common
from su_crawler.extraction import extract
from su_crawler.models import FetchResult, Source


def _gongcar(html: str, url: str = "https://gongcarrent.kr/detail-quote", backend: str = "playwright"):
    source = Source("s", "gongcar", "web", url, ["p"], adapter="gongcar")
    result = FetchResult("s", "fetched", backend, html.encode("utf-8"), final_url=url)
    return extract(result, source)


# --- Item 1: generic Gongcar quote tables with one price column per term ---

GROUPED_TERMS = """<!doctype html><h1 data-car-id="SYN-1">Synthetic Car A</h1>
<table><thead>
<tr><th rowspan="2">옵션</th><th rowspan="2">보증금</th><th colspan="3">월 렌트료</th></tr>
<tr><th>36개월</th><th>48개월</th><th>60개월</th></tr></thead><tbody>
<tr><td>기본 옵션</td><td>보증금 0원</td><td>월 700,000원</td><td>월 650,000원</td><td>월 600,000원</td></tr>
<tr><td>고급 옵션</td><td>보증금 0원</td><td>월 800,000원</td><td>월 750,000원</td><td>월 700,000원</td></tr>
</tbody></table>"""


def test_gongcar_quote_table_emits_every_term_column_with_term_identity():
    values = _gongcar(GROUPED_TERMS)
    assert [(v.fields["options"], v.fields["term_months"], v.fields["price"]) for v in values] == [
        ("기본 옵션", "36", "700000"), ("기본 옵션", "48", "650000"), ("기본 옵션", "60", "600000"),
        ("고급 옵션", "36", "800000"), ("고급 옵션", "48", "750000"), ("고급 옵션", "60", "700000"),
    ]
    assert len({v.fields["item_id"] for v in values}) == 6
    assert len({v.locator for v in values}) == 6
    assert ":term=48:column=4" in values[1].fields["item_id"]
    assert values[1].locator.endswith(":column:4")
    assert values[1].evidence["term_months"]["source_text"] == "월 렌트료 / 48개월"
    assert all(v.evidence["term_months"]["display_state"] == "visible" for v in values)


def test_gongcar_quote_table_parses_inline_term_headers_and_keeps_unknown_term_null():
    html = """<!doctype html><h1 data-car-id="SYN-2">Synthetic Car B</h1>
<table><tr><th>옵션</th><th>보증금</th><th>월 렌트료(36개월)</th><th>월 렌트료(48개월)</th><th>월 대여료</th></tr>
<tr><td>기본</td><td>보증금 0원</td><td>월 500,000원</td><td>월 450,000원</td><td>월 400,000원</td></tr></table>"""
    values = _gongcar(html)
    assert [v.fields.get("term_months") for v in values] == ["36", "48", None]
    assert [v.fields["price"] for v in values] == ["500000", "450000", "400000"]
    assert "term_months" not in values[2].evidence
    assert values[2].fields["item_id"].endswith(":term=:column=5")


def test_gongcar_single_price_column_keeps_legacy_identity():
    values = _gongcar("""<!doctype html><h1 data-car-id="SYN-3">Synthetic Car C</h1>
<table><tr><th>옵션</th><th>보증금</th><th>월 렌트료</th></tr>
<tr><td>기본</td><td>보증금 0원</td><td>월 500,000원</td></tr></table>""")
    assert len(values) == 1
    assert values[0].fields["item_id"] == "SYN-3:option=기본:deposit=보증금 0원"
    assert values[0].locator == "gongcar:table:1:row:2"
    assert "term_months" not in values[0].fields


# --- Item 2: struck-through list prices, ranges and multiple amounts ---

@pytest.mark.parametrize("raw,expected,reason", [
    ("월 450,000원", "450000", None),
    ("45만원", "450000", None),
    ("450,000원 (450,000원)", "450000", None),
    ("정가 500,000원 할인 450,000원", None, "multiple_amounts_unresolved"),
    ("50~60만원", None, "price_range_not_single_amount"),
    ("500,000원 - 600,000원", None, "price_range_not_single_amount"),
    ("월 450,000원~", None, "starting_price_qualifier"),
    ("45만원부터", None, "starting_price_qualifier"),
    ("50만원 이상", None, "starting_price_qualifier"),
    ("문의", None, "no_won_amount"),
])
def test_won_never_picks_one_of_several_amounts(raw, expected, reason):
    assert common.won_amount(raw) == (expected, reason)
    assert common.won(raw) == expected


def test_won_allow_from_accepts_single_floor_amount_only():
    assert common.won("월 450,000원~", allow_from=True) == "450000"
    assert common.won("45만원부터", allow_from=True) == "450000"
    assert common.won("50~60만원", allow_from=True) is None


@pytest.mark.parametrize("struck", [
    "<del>월 500,000원</del>",
    "<s>월 500,000원</s>",
    "<strike>월 500,000원</strike>",
    '<span class="line-through">월 500,000원</span>',
    '<span style="text-decoration: line-through">월 500,000원</span>',
    '<span data-sourceledger-computed-line-through="true">월 500,000원</span>',
])
def test_gongcar_quote_table_ignores_struck_list_price(struck):
    values = _gongcar(f"""<!doctype html><h1 data-car-id="SYN-4">Synthetic Car D</h1>
<table><tr><th>옵션</th><th>보증금</th><th>월 렌트료</th></tr>
<tr><td>기본</td><td>보증금 0원</td><td>{struck} 월 450,000원</td></tr></table>""")
    assert len(values) == 1
    assert values[0].fields["price"] == "450000"
    assert values[0].evidence["price"]["source_text"] == "월 450,000원"
    assert values[0].review_flags == []


@pytest.mark.parametrize("cell,reason", [
    ("월 50~60만원", "price_range_not_single_amount"),
    ("정가 500,000원 할인 450,000원", "multiple_amounts_unresolved"),
])
def test_gongcar_quote_table_records_ambiguous_price_for_review(cell, reason):
    values = _gongcar(f"""<!doctype html><h1 data-car-id="SYN-5">Synthetic Car E</h1>
<table><tr><th>옵션</th><th>보증금</th><th>월 렌트료</th></tr>
<tr><td>기본</td><td>보증금 0원</td><td>{cell}</td></tr></table>""")
    assert len(values) == 1
    assert "price" not in values[0].fields and "currency" not in values[0].fields
    assert values[0].evidence["price"]["raw"] is None
    assert values[0].evidence["price"]["source_text"] == cell
    assert values[0].review_flags == [reason]


def test_gongcar_quote_table_floor_price_is_flagged_not_fixed():
    values = _gongcar("""<!doctype html><h1 data-car-id="SYN-6">Synthetic Car F</h1>
<table><tr><th>옵션</th><th>보증금</th><th>월 렌트료</th></tr>
<tr><td>기본</td><td>보증금 0원</td><td>월 450,000원~</td></tr></table>""")
    assert values[0].fields["price"] == "450000"
    assert values[0].derived_values["price_kind"] == "from"
    assert "starting_price_not_fixed_quote" in values[0].review_flags
