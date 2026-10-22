from su_crawler.adapters import jetcar
from su_crawler.models import FetchResult, Source

URL = "https://www.jetcar.kr/sub0201/1967"


def _result(html: str, url: str = URL) -> FetchResult:
    return FetchResult("s", "fetched", "http", html.encode("utf-8"), "text/html", url)


def _source(url: str = URL) -> Source:
    return Source("s", "x", "web", url, ["p"], adapter="jetcar")


HEADED = """<html><head><link rel="canonical" href="https://www.jetcar.kr/sub0201/1967"></head><body>
<h1>Model A > 신차장기렌트</h1>
<table class="rent-quote-table"><thead><tr><th class="deposit">보증금</th><th class="period">계약기간</th>
<th>월 렌트비</th><th>주행거리</th></tr></thead><tbody>
<tr><td class="deposit rq_dep">50만원</td><td class="period rq_month">60개월</td><td class="rq_price"><b>400,000</b></td><td class="rq_mileage">연 2.5만km</td></tr>
<tr><td class="deposit rq_dep">50만원</td><td class="period rq_month">12개월</td><td class="rq_price"><b>문의</b></td><td class="rq_mileage">연 2.5만km</td></tr>
</tbody></table></body></html>"""


def test_headed_quote_table_reads_bare_amounts_under_the_monthly_header():
    result = _result(HEADED)
    candidates = jetcar.extract(result, _source())
    priced, inquiry = candidates
    assert (priced.fields["term_months"], priced.fields["price"], priced.fields["deposit_amount"],
            priced.fields["annual_mileage_km"]) == ("60", "400000", "500000", "25000")
    assert "price" not in inquiry.fields and inquiry.review_flags == ["price_inquiry_or_unavailable"]
    assert inquiry.fields["deposit_amount"] == "500000"
    section = jetcar.section_coverage(result, candidates)[0]
    assert section["status"] == "partial" and section["observed_count"] == 2


def test_headed_quote_table_is_complete_when_every_row_has_an_amount():
    html = HEADED.replace("<b>문의</b>", "<b>450,000</b>")
    result = _result(html)
    assert jetcar.section_coverage(result, jetcar.extract(result, _source()))[0]["status"] == "complete"


def test_headed_quote_table_ignores_struck_through_amount():
    html = HEADED.replace("<b>400,000</b>", "<del>999,000</del><b>400,000</b>")
    priced = jetcar.extract(_result(html), _source())[0]
    assert priced.fields["price"] == "400000"
