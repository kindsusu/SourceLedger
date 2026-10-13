"""Offline tests for rendered short rental cards and completion evidence."""
from su_crawler.adapters.gongcar import extract, section_coverage
from su_crawler.models import FetchResult, Source


def _card(name="The New Ray", price="월 44만원부터", hidden=""):
    return (f'<div class="group" {hidden}><h4>{name}<span> · Gasoline</span></h4>'
            f'<span>{price}</span><img alt="{name}"></div>')


def _daily_count(count):
    return (f'<div class="flex items-center gap-2"><div>수입차</div><div>{count}</div></div>')


def _run(path, html, *, count=1, status="complete", backend="playwright"):
    trace = [{"event": "list_expansion", "section": "monthly_catalog" if path == "monthly-rent" else "daily_catalog",
              "status": status, "observed_count": count, "click_count": 1, "reason": "button_absent"}]
    result = FetchResult(source_id="g", status="fetched", backend=backend,
                         final_url="https://gongcar.com/" + path, content=html.encode(), trace=trace)
    source = Source(id="g", name="Gongcar", kind="web", location=result.final_url, product_ids=[])
    return result, extract(result, source)


def test_monthly_starting_price_selected_term_and_coverage():
    html = ('<button>1개월</button><p>총 1종의 차량이 있습니다.</p>'
            '<div class="grid">' + _card() + '</div>')
    result, offers = _run("monthly-rent", html)
    assert len(offers) == 1
    offer = offers[0]
    assert offer.fields["price"] == "440000"
    assert offer.fields["price_basis"] == "monthly"
    assert offer.fields["term_months"] == "1"
    assert offer.fields["item_id"].startswith("local-observation-")
    assert offer.derived_values["price_kind"] == "from"
    assert offer.evidence["price"]["display_state"] == "visible"
    assert section_coverage(result, offers)[0]["status"] == "complete"


def test_daily_price_and_hidden_card():
    html = _daily_count(1) + '<div class="grid"><div class="animate-fade-in">' + _card("BMW 5", "일 19만원") + '</div>' + _card(hidden="hidden") + '</div>'
    result, offers = _run("daily-rent", html)
    assert len(offers) == 1
    assert offers[0].fields["price"] == "190000"
    assert offers[0].fields["price_basis"] == "daily"
    assert "term_months" not in offers[0].fields
    assert "price_kind" not in offers[0].derived_values
    assert section_coverage(result, offers)[0]["status"] == "complete"


def test_declared_count_and_missing_price_fail_closed():
    html = '<button>1개월</button><p>총 2종의 차량이 있습니다.</p><div class="grid">' + _card() + '</div>'
    result, offers = _run("monthly-rent", html)
    coverage = section_coverage(result, offers)[0]
    assert coverage["expected_count"] == 2
    assert coverage["reason"] == "declared_count_not_reached"
    assert coverage["status"] == "partial"
    html = _daily_count(1) + '<div class="grid">' + _card(price="문의 요망") + '</div>'
    result, offers = _run("daily-rent", html)
    assert len(offers) == 1 and "price" not in offers[0].fields
    assert section_coverage(result, offers)[0]["reason"] == "unpriced_cards"


def test_monthly_term_not_inferred_and_static_html_not_complete():
    html = '<div class="grid">' + _card() + '</div>'
    result, offers = _run("monthly-rent", html)
    assert "term_months" not in offers[0].fields
    assert section_coverage(result, offers)[0]["reason"] == "selected_term_unconfirmed"
    result, offers = _run("daily-rent", _daily_count(1) + '<div class="grid">' + _card(price="일 19만원") + '</div>', backend="http")
    assert section_coverage(result, offers)[0]["status"] == "partial"


def test_daily_declared_count_prevents_false_complete():
    html = _daily_count(7) + '<div class="grid">' + _card(price="일 19만원") * 6 + '</div>'
    result, offers = _run("daily-rent", html, count=6)
    coverage = section_coverage(result, offers)[0]
    assert coverage["expected_count"] == 7
    assert coverage["observed_count"] == 6
    assert coverage["reason"] == "declared_count_not_reached"
    assert coverage["status"] == "partial"
    result, offers = _run("daily-rent", '<div class="grid">' + _card(price="일 19만원") + '</div>')
    assert section_coverage(result, offers)[0]["reason"] == "declared_count_unconfirmed"


def test_monthly_detail_four_periods_keep_upfront_role_ambiguous():
    buttons = ''.join(f'<button><span>{term}개월</span><span><span>{price:,}원</span></span></button>'
                      for term, price in ((1, 440000), (6, 420000), (12, 400000), (24, 380000)))
    html = ('<h1>Kia Ray Van</h1><div><span>선납/보증금</span><span>30만원</span></div>'
            '<section><h2>대여 기간</h2>' + buttons + '</section><span>월 렌트료</span>')
    result, offers = _run("monthly-rent/43", html)
    assert [offer.fields["term_months"] for offer in offers] == ["1", "6", "12", "24"]
    assert [offer.fields["price"] for offer in offers] == ["440000", "420000", "400000", "380000"]
    assert all(offer.fields["upfront_payment_amount"] == "300000" for offer in offers)
    assert all(offer.fields["upfront_payment_label"] == "선납/보증금" for offer in offers)
    assert all("deposit_amount" not in offer.fields and "advance_amount" not in offer.fields for offer in offers)
    assert all(offer.evidence["price_basis"]["source_text"] == "월 렌트료" for offer in offers)
    assert section_coverage(result, offers)[0]["status"] == "complete"
    assert section_coverage(result, offers)[0]["expected_count"] == 4
    result, fewer = _run("monthly-rent/43", html.replace("<span>420,000원</span>", "문의 요망"))
    assert section_coverage(result, fewer)[0]["status"] == "partial"


def test_daily_detail_separates_eligibility_from_price_age_basis():
    html = ('<h1>Mercedes C</h1><h4>만 21세 이상만</h4>'
            '<p>· 만 26세 이상 기준 요금이며, 만 26세 미만은 별도 문의</p>'
            '<div><h3>렌트 가격</h3><div><span>일 렌트료</span><span>190,000원</span></div></div>')
    result, offers = _run("daily-rent/5", html)
    assert len(offers) == 1
    assert offers[0].fields["price"] == "190000"
    assert offers[0].fields["price_basis"] == "daily"
    assert offers[0].fields["driver_age_condition"] == "만 21세 이상만"
    assert offers[0].fields["price_age_basis"] == "만 26세 이상 기준 요금"
    assert section_coverage(result, offers)[0]["status"] == "complete"
