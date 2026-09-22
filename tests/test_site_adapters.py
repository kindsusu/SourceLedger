from pathlib import Path

import pytest

from su_crawler.adapters import ADAPTER_VERSION, available_adapters
from su_crawler.extraction import extract
from su_crawler.models import FetchResult, Product, Source
from su_crawler.validation import validate


FIXTURES = Path(__file__).parent / "fixtures" / "adapters"


def run(name, fixture, url):
    source = Source("s", name, "web", url, ["p"], adapter=name)
    result = FetchResult("s", "fetched", "playwright", (FIXTURES / fixture).read_bytes(), final_url=url)
    return extract(result, source)


def assert_complete_evidence(candidate):
    assert all(field in candidate.evidence for field in candidate.fields)
    assert all(proof.get("location") and "raw" in proof for proof in candidate.evidence.values())


def test_registry_has_versioned_render_requirements():
    metadata = available_adapters()
    assert ADAPTER_VERSION == "1"
    assert set(metadata) == {"jetcar", "gongcar", "funrent"}
    assert metadata["jetcar"]["requires_rendered_html"] is False
    assert metadata["gongcar"]["requires_rendered_html"] is True


def test_jetcar_emits_each_displayed_term_with_actual_listing_identity():
    values = run("jetcar", "jetcar_detail.html", "https://www.jetcar.kr/sub0301/5874")
    assert [v.fields["term_months"] for v in values] == ["12", "24"]
    assert [v.fields["price"] for v in values] == ["450000", "410000"]
    assert values[0].fields["item_id"] == "5874"
    assert values[0].fields["deposit_amount"] == "500000"
    assert values[0].fields["odometer_km"] == "1750"
    assert "annual_mileage_km" not in values[0].fields
    assert values[0].fields["currency"] == "KRW"
    assert "condition" not in values[0].fields
    assert values[0].fields["tax"] == "월 렌트비 부가세 포함가격 입니다."
    assert_complete_evidence(values[0])


def test_jetcar_no_price_is_meaningful_review_candidate_and_ignores_script():
    value = run("jetcar", "jetcar_no_price.html", "https://www.jetcar.kr/sub0201/9001")[0]
    assert value.fields["item_id"] == "9001"
    assert "price" not in value.fields
    assert value.evidence["price"]["raw"] is None
    assert value.review_flags == ["missing_displayed_price"]


def test_jetcar_static_html_does_not_claim_computed_visibility():
    url = "https://www.jetcar.kr/sub0301/5874"
    source = Source("s", "jetcar", "web", url, ["p"], adapter="jetcar")
    result = FetchResult("s", "fetched", "http", (FIXTURES / "jetcar_detail.html").read_bytes(), final_url=url)
    assert all(value.source_visibility == "unconfirmed" for value in extract(result, source))


def test_gongcar_expands_rowspan_and_excludes_hidden_or_script_quotes():
    values = run("gongcar", "gongcar_rendered.html", "https://gongcarrent.kr/detail-quote")
    assert len(values) == 2
    assert [v.fields["price"] for v in values] == ["680000", "640000"]
    assert values[1].fields["options"] == "기본 옵션"
    assert all("UNSAFE" not in v.fields["item_id"] for v in values)
    assert_complete_evidence(values[1])


def test_gongcar_public_layout_uses_external_headers_and_expands_rowspans():
    values = run("gongcar", "gongcar_public_layout.html", "https://gongcarrent.kr/public-quote")
    assert len(values) == 4
    assert [value.fields["price"] for value in values] == ["510000", "470000", "535000", "489000"]
    assert [value.fields["deposit_amount"] for value in values] == ["700000", "2468000", "700000", "2519000"]
    assert values[0].fields["model"] == "2031 오로라 (전기 스탠다드)"
    assert values[0].fields["trim"] == "전기 스탠다드"
    assert values[0].fields["options"] == "안전 패키지 (긴급제동+후방센서)"
    assert values[0].fields["vehicle_value"] == "24680000"
    assert values[0].fields["driver_age_condition"] == "만22세이상"
    assert values[0].fields["price_age_basis"] == "만 27세 기준"
    assert values[0].fields["price_basis"] == "monthly"
    assert "term_months" not in values[0].fields and "deposit_percent" not in values[0].fields
    assert len({value.fields["item_id"] for value in values}) == 4
    assert "9999999" not in values[0].fields["price"]
    assert_complete_evidence(values[0])


def test_gongcar_changed_public_header_fails_closed():
    html = (FIXTURES / "gongcar_public_layout.html").read_text(encoding="utf-8").replace("차량가액(원)", "상품 설명")
    source = Source("s", "gongcar", "web", "https://gongcarrent.kr/public-quote", ["p"], adapter="gongcar")
    result = FetchResult("s", "fetched", "playwright", html.encode("utf-8"), final_url=source.location)
    assert extract(result, source) == []


@pytest.mark.parametrize("old,new", [
    ("<p>운전연령</p>", "<p>옵션</p>"),
    ("<p>차량가액(원)</p>", "<p>차량가액</p>"),
    ("<p>보증금(원)</p>", "<p>보증금</p>"),
    ("월 렌트료(원)", "월 렌트료"),
])
def test_gongcar_public_header_order_and_currency_are_strict(old, new):
    html = (FIXTURES / "gongcar_public_layout.html").read_text(encoding="utf-8").replace(old, new, 1)
    source = Source("s", "gongcar", "web", "https://gongcarrent.kr/public-quote", ["p"], adapter="gongcar")
    result = FetchResult("s", "fetched", "playwright", html.encode("utf-8"), final_url=source.location)
    assert extract(result, source) == []


@pytest.mark.parametrize("old,new", [
    ("700,000</td><td><span hidden", "700,000 ~ 900,000</td><td><span hidden"),
    ("700,000</td><td>535,000", "12%</td><td>535,000"),
])
def test_gongcar_public_money_cells_reject_ranges_and_percentages(old, new):
    html = (FIXTURES / "gongcar_public_layout.html").read_text(encoding="utf-8").replace(old, new, 1)
    source = Source("s", "gongcar", "web", "https://gongcarrent.kr/public-quote", ["p"], adapter="gongcar")
    result = FetchResult("s", "fetched", "playwright", html.encode("utf-8"), final_url=source.location)
    values = extract(result, source)
    assert len(values) == 3
    assert all(value.fields["deposit_amount"] not in {"700000 ~ 900000", "12"} for value in values)


def test_gongcar_public_model_without_parenthetical_trim_does_not_invent_trim():
    html = (FIXTURES / "gongcar_public_layout.html").read_text(encoding="utf-8").replace(
        "2031 오로라 (전기 스탠다드)", "2031 오로라 스탠다드"
    )
    source = Source("s", "gongcar", "web", "https://gongcarrent.kr/public-quote", ["p"], adapter="gongcar")
    result = FetchResult("s", "fetched", "playwright", html.encode("utf-8"), final_url=source.location)
    values = extract(result, source)
    assert values and all("trim" not in value.fields for value in values)


@pytest.mark.parametrize("adapter,fixture,url", [
    ("gongcar", "gongcar_rendered.html", "https://gongcarrent.kr/detail-quote"),
    ("funrent", "funrent_rendered.html", "https://go.funrentcar.com/"),
])
def test_render_dependent_adapters_do_not_claim_static_http_visibility(adapter, fixture, url):
    source = Source("s", adapter, "web", url, ["p"], adapter=adapter)
    content = (FIXTURES / fixture).read_bytes()
    static = extract(FetchResult("s", "fetched", "http", content, final_url=url), source)
    rendered = extract(FetchResult("s", "fetched", "playwright", content, final_url=url), source)
    assert static and rendered
    assert all(value.source_visibility == "unconfirmed" for value in static)
    assert all(value.source_visibility == "visible" for value in rendered)


def test_funrent_rendered_selection_is_estimate_and_preserves_approximation():
    value = run("funrent", "funrent_rendered.html", "https://go.funrentcar.com/")[0]
    assert value.fields["item_id"] == "casper:deposit=10:term=36"
    assert "price" not in value.fields
    assert value.derived_values["estimated_price"] == "428000"
    assert value.evidence["derived_values.estimated_price"]["source_text"] == "428,000원 ~"
    assert value.fields["displayed_deposit"] == "보증금 약 150만원"
    assert "deposit_amount" not in value.fields
    assert value.value_origin == "calculator_estimate"
    assert "displayed_approximate_deposit" in value.review_flags
    assert_complete_evidence(value)
    observation = validate(
        value, Product("p", "Casper", identifiers={"item_id": value.fields["item_id"]}, price_profile="rental"),
        Source("s", "funrent", "web", "https://go.funrentcar.com/", ["p"], adapter="funrent"),
        run_id="r", task_id="t", evidence_path="e", evidence_sha256="h",
        collected_at="2026-09-22T00:00:00Z", source_url="https://go.funrentcar.com/",
    )
    assert observation.amount is None
    assert observation.derived_amount == "428000"


def test_funrent_unrendered_or_ambiguous_selected_state_fails_closed():
    assert run("funrent", "funrent_unrendered.html", "https://go.funrentcar.com/") == []
    html = (FIXTURES / "funrent_rendered.html").read_text(encoding="utf-8").replace('<option value="10" selected>', '<option value="10">')
    source = Source("s", "funrent", "web", "https://go.funrentcar.com/", ["p"], adapter="funrent")
    assert extract(FetchResult("s", "fetched", "playwright", html.encode()), source) == []
    no_month = (FIXTURES / "funrent_rendered.html").read_text(encoding="utf-8").replace("실시간 월 대여료", "실시간 견적")
    assert extract(FetchResult("s", "fetched", "playwright", no_month.encode()), source) == []


def test_unknown_adapter_is_rejected():
    source = Source("s", "S", "web", "https://example.test", ["p"], adapter="unknown")
    with pytest.raises(ValueError, match="Unknown source adapter"):
        extract(FetchResult("s", "fetched", "http", b"<p>x</p>"), source)
