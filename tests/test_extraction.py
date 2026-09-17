from su_crawler.extraction import extract
from su_crawler.models import FetchResult, Source


def source(**kwargs):
    return Source("s", "S", "web", "https://example.test", ["p"], **kwargs)


def test_jsonld_offer_is_separate_and_aggregate_is_not_price():
    html = b'''<script type="application/ld+json">{"@type":"Product","sku":"A","offers":[{"@type":"Offer","price":"12.50","priceCurrency":"EUR"},{"@type":"AggregateOffer","lowPrice":"1"}]}</script>'''
    values = extract(FetchResult("s", "fetched", "http", html), source())
    assert len(values) == 2
    assert values[0].fields["price"] == "12.50"
    assert values[0].evidence["price"]["location"].startswith("jsonld:")
    assert "price" not in values[1].fields
    assert "AggregateOffer" in values[1].evidence["_extraction"]["raw"]


def test_html_selector_attribute_and_specs():
    result = FetchResult("s", "fetched", "http", b'<div class="row"><b class="p">10</b><i data-c="KRW"></i><span class="m">M1</span></div>')
    values = extract(result, source(row_selector=".row", selectors={"price": ".p", "currency": "i::attr(data-c)", "spec:size": ".m"}))
    assert values[0].fields == {"price": "10", "currency": "KRW"}
    assert values[0].specs == {"size": "M1"}


def test_csv_uses_configured_headers_only():
    result = FetchResult("s", "fetched", "file", b'Cost,Curr,Ignore\n10,USD,x\n', "text/csv")
    value = extract(result, source(columns={"price": "Cost", "currency": "Curr"}))[0]
    assert value.fields == {"price": "10", "currency": "USD"}


def test_pdf_named_groups_are_source_values(monkeypatch):
    class Page:
        def extract_text(self): return "SKU A-1 costs 12.50 USD"
    class Reader:
        pages = [Page()]
    monkeypatch.setattr("su_crawler.extraction.PdfReader", lambda _: Reader())
    result = FetchResult("s", "fetched", "file", b"%PDF", "application/pdf")
    value = extract(result, source(pdf_pattern=r"SKU (?P<sku>\S+) costs (?P<price>\S+) (?P<currency>\S+)"))[0]
    assert value.fields == {"sku": "A-1", "price": "12.50", "currency": "USD"}


def test_pdf_without_pattern_does_not_emit_empty_match(monkeypatch):
    class Reader:
        pages = []
    monkeypatch.setattr("su_crawler.extraction.PdfReader", lambda _: Reader())
    assert extract(FetchResult("s", "fetched", "file", b"%PDF", "application/pdf"), source()) == []


def test_json_decimal_preserves_source_digits():
    result = FetchResult("s", "fetched", "http", b'{"price": 0.12345678901234567890123456789}', "application/json")
    value = extract(result, source())[0]
    assert value.fields["price"] == "0.12345678901234567890123456789"
    assert value.evidence["price"]["raw"] == value.fields["price"]
