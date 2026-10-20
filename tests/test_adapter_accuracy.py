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


# --- Items 2 and 4: Jetcar struck prices, ambiguous amounts and inquiry rows ---

JETCAR_URL = "https://www.jetcar.kr/sub0301/987654"


def _jetcar(rows: str):
    from pathlib import Path

    html = (Path(__file__).parent / "fixtures" / "adapters" / "jetcar_detail.html").read_text(encoding="utf-8")
    html = html.replace("</table>\n<table hidden>", rows + "</table>\n<table hidden>", 1)
    result = FetchResult("s", "fetched", "playwright", html.encode("utf-8"), final_url=JETCAR_URL)
    return result, extract(result, Source("s", "jetcar", "web", JETCAR_URL, ["p"], adapter="jetcar"))


def test_jetcar_ignores_struck_list_price_in_rent_row():
    from su_crawler.adapters.jetcar import section_coverage

    result, values = _jetcar('<tr><td>보증금 50 만원</td><td>36개월</td>'
                             '<td><del>월 700,000 원</del> 월 650,000 원</td></tr>')
    assert [v.fields["price"] for v in values] == ["682965", "598846", "650000"]
    assert values[2].review_flags == []
    assert section_coverage(result, values)[0]["status"] == "complete"


@pytest.mark.parametrize("cell,flag", [
    ("월 600,000원~650,000원", "price_range_not_single_amount"),
    ("월 50~60만원", "price_range_not_single_amount"),
    ("정가 월 700,000원 할인 월 650,000원", "multiple_amounts_unresolved"),
    ("월 650,000원부터", "starting_price_qualifier"),
])
def test_jetcar_ambiguous_monthly_price_is_null_with_reason(cell, flag):
    _, values = _jetcar(f'<tr><td>보증금 50 만원</td><td>36개월</td><td>{cell}</td></tr>')
    assert len(values) == 3
    row = values[2]
    assert row.fields["term_months"] == "36"
    assert "price" not in row.fields and "currency" not in row.fields
    assert row.evidence["price"]["raw"] is None
    assert row.review_flags == [flag]


def test_jetcar_term_with_inquiry_price_is_kept_as_null_price_candidate():
    from su_crawler.models import Product
    from su_crawler.validation import validate

    result, values = _jetcar('<tr><td>보증금 50 만원</td><td>36개월</td><td>월 문의</td></tr>')
    assert len(values) == 3
    row = values[2]
    assert row.locator.endswith(":rent-row:4")
    assert row.fields["term_months"] == "36"
    assert row.fields["deposit_amount"] == "500000"
    assert row.fields["availability"] == "월 문의"
    assert "price" not in row.fields and "currency" not in row.fields and "price_basis" not in row.fields
    assert row.review_flags == ["price_inquiry_or_unavailable"]
    source = Source("s", "jetcar", "web", JETCAR_URL, ["p"], adapter="jetcar")
    observation = validate(row, Product("p", "car", price_profile="rental"), source, run_id="r", task_id="t",
                           evidence_path="e", evidence_sha256="h", collected_at="2026-10-20T00:00:00Z",
                           source_url=JETCAR_URL)
    assert observation.status == "review" and observation.amount is None and not observation.comparable
    assert "review flag: price_inquiry_or_unavailable" in observation.reason
    assert not any(part.startswith("evidence") for part in observation.reason.split("; "))


# --- Item 3: identities never depend on the displayed price ---

def _gongcar_ids(path: str, html: str) -> list[str]:
    return [value.fields["item_id"] for value in _gongcar(html, f"https://gongcar.kr/{path}")]


def _popular_card(price: str) -> str:
    return ('<section><h2>인기 차량 한눈에 보기</h2><div class="grid">'
            f'<button><img alt="Model A"><span>Model A</span><span>Gasoline</span><span>월 {price}원~</span></button>'
            f'<button><img alt="Model A"><span>Model A</span><span>Gasoline</span><span>월 {price}원~</span></button>'
            '</div><p>* 48개월 / 보증금 30% 기준 | 최근 업데이트 : 26.10.12</p></section>')


def _monthly_card(price: str) -> str:
    return ('<button>1개월</button><p>총 1종의 차량이 있습니다.</p><div class="grid">'
            f'<div class="group"><h4>Model B<span> · Gasoline</span></h4><span>월 {price}만원부터</span></div></div>')


def _monthly_detail(price: str) -> str:
    return ('<h1>Model C</h1><section><h2>대여 기간</h2>'
            f'<button><span>1개월</span><span><span>{price}원</span></span></button>'
            f'<button><span>6개월</span><span><span>{price}원</span></span></button>'
            '</section><span>월 렌트료</span>')


def _daily_detail(price: str) -> str:
    return ('<h1>Model D</h1><div><h3>렌트 가격</h3><div><span>일 렌트료</span>'
            f'<span>{price}원</span></div></div>')


def _landing(price: str) -> str:
    return ('<section aria-labelledby="sec-price"><div id="deals">'
            f'<button class="deal"><span class="deal-name">Model E</span><span class="deal-spec">Gasoline</span>'
            f'<span class="deal-price">월 {price}원~</span></button></div>'
            '<p class="deal-basis-line">48개월 / 보증금 30% / 만 26세</p></section>')


@pytest.mark.parametrize("path,builder,before,after", [
    ("rentpass", _popular_card, "290,000", "299,000"),
    ("monthly-rent", _monthly_card, "44", "45"),
    ("monthly-rent/43", _monthly_detail, "440,000", "450,000"),
    ("daily-rent/5", _daily_detail, "190,000", "200,000"),
    ("rentpass", _landing, "300,000", "310,000"),
])
def test_gongcar_price_change_keeps_item_identity(path, builder, before, after):
    old_ids, new_ids = _gongcar_ids(path, builder(before)), _gongcar_ids(path, builder(after))
    assert old_ids and old_ids == new_ids
    assert len(set(old_ids)) == len(old_ids)


def _public_table(second_age: str, second_value: str) -> str:
    header = ''.join(f'<div><p>{label}</p></div>' for label in (
        "Synthetic", "운전연령", "옵션", "차량가액(원)", "보증금(원)", "월 렌트료(원)", ""))
    rows = (f'<tr><td>Model F</td><td>만26세이상</td><td>기본</td><td>40,000,000</td><td>0</td><td>700,000</td><td></td></tr>'
            f'<tr><td>Model F</td><td>{second_age}</td><td>기본</td><td>{second_value}</td><td>0</td><td>750,000</td><td></td></tr>')
    return f'<div class="quote-columns">{header}</div><table><tbody>{rows}</tbody></table>'


@pytest.mark.parametrize("age,value", [("만21세이상", "40,000,000"), ("만26세이상", "41,000,000")])
def test_gongcar_public_rows_differing_only_by_age_or_vehicle_value_stay_distinct(age, value):
    values = _gongcar(_public_table(age, value), "https://gongcarrent.kr/public-quote")
    assert len(values) == 2
    assert values[0].fields["item_id"] != values[1].fields["item_id"]
