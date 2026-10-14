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
    assert values[0].evidence_mode == "static_html"
    assert values[0].evidence["price"]["display_state"] == "unconfirmed"
    assert "price" not in values[1].fields
    assert "AggregateOffer" in values[1].evidence["_extraction"]["raw"]


def test_html_selector_attribute_and_specs():
    result = FetchResult("s", "fetched", "http", b'<div class="row"><b class="p">10</b><i data-c="KRW"></i><span class="m">M1</span></div>')
    values = extract(result, source(row_selector=".row", selectors={"price": ".p", "currency": "i::attr(data-c)", "spec:size": ".m"}))
    assert values[0].fields == {"price": "10", "currency": "KRW"}
    assert values[0].specs == {"size": "M1"}
    assert values[0].evidence_mode == "static_html"
    assert values[0].evidence["price"]["display_state"] == "unconfirmed"
    assert values[0].evidence["currency"]["proof_kind"] == "dom_attribute"


def test_rendered_html_records_field_level_hidden_state_and_ancestors():
    html = b'<main hidden data-sourceledger-computed-hidden="true"><span class="price">9</span></main><span class="shown">10</span>'
    result = FetchResult("s", "fetched", "playwright", html)
    values = extract(result, source(selectors={"price": ".price", "currency": ".shown"}))
    assert values[0].evidence_mode == "rendered_dom"
    assert values[0].source_visibility == "hidden"
    assert values[0].fields["price"] == "9"
    assert values[0].evidence["price"]["raw"] == "9"
    assert values[0].evidence["price"]["display_state"] == "hidden"
    assert any("computed_hidden_snapshot" in item["reasons"] for item in values[0].evidence["price"]["hidden_by"])
    assert values[0].evidence["currency"]["display_state"] == "visible"


def test_rendered_selector_text_excludes_hidden_descendants():
    html = b'<div class="price"><span data-sourceledger-computed-hidden="true">999</span><span>12</span></div>'
    value = extract(FetchResult("s", "fetched", "playwright", html), source(selectors={"price": ".price"}))[0]
    assert value.fields["price"] == "12"
    assert value.evidence["price"]["raw"] == "12"
    assert value.evidence["price"]["display_state"] == "visible"


def test_csv_uses_configured_headers_only():
    result = FetchResult("s", "fetched", "file", b'Cost,Curr,Ignore\n10,USD,x\n', "text/csv")
    value = extract(result, source(columns={"price": "Cost", "currency": "Curr"}))[0]
    assert value.fields == {"price": "10", "currency": "USD"}
    assert value.evidence_mode == "structured_record"
    assert value.source_visibility == "not_applicable"
    assert value.evidence["price"]["display_state"] == "not_applicable"


def test_pdf_named_groups_are_source_values(monkeypatch):
    class Page:
        def extract_text(self): return "SKU A-1 costs 12.50 USD"
    class Reader:
        pages = [Page()]
    monkeypatch.setattr("su_crawler.extraction.PdfReader", lambda _: Reader())
    result = FetchResult("s", "fetched", "file", b"%PDF", "application/pdf")
    value = extract(result, source(pdf_pattern=r"SKU (?P<sku>\S+) costs (?P<price>\S+) (?P<currency>\S+)"))[0]
    assert value.fields == {"sku": "A-1", "price": "12.50", "currency": "USD"}
    assert value.evidence_mode == "document_text"
    assert value.source_visibility == "not_applicable"


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


def test_product_group_variants_are_shared_with_catalog_extraction():
    html = b'''<script type="application/ld+json">{"@type":"ProductGroup","name":"Shirt","hasVariant":[
      {"@type":"Product","name":"Shirt M","sku":"M","size":"M","offers":{"@type":"Offer","price":"10","priceCurrency":"USD"}},
      {"@type":"Product","name":"Shirt L","sku":"L","size":"L","offers":{"@type":"Offer","price":"12","priceCurrency":"USD"}}
    ]}</script>'''
    values = extract(FetchResult("s", "fetched", "http", html), source())
    assert [(value.fields["sku"], value.specs["size"], value.fields["price"]) for value in values] == [
        ("M", "M", "10"), ("L", "L", "12")]
    assert all("_selection" in value.evidence for value in values)


def test_jsonld_direct_price_preserved_and_unit_tiers_keep_quantity_conditions():
    html = b'''<script type="application/ld+json">{"@type":"Product","sku":"BEANS","offers":{
      "@type":"Offer","price":"20","priceCurrency":"USD","priceSpecification":[
        {"@type":"DeliveryChargeSpecification","price":"5","priceCurrency":"USD"},
        {"@type":"UnitPriceSpecification","price":"18","priceCurrency":"USD",
         "eligibleQuantity":{"minValue":2,"unitText":"bag"}},
        {"@type":"UnitPriceSpecification","price":"16","priceCurrency":"USD",
         "eligibleQuantity":{"minValue":5,"unitText":"bag"}}
      ]}}</script>'''
    result = FetchResult("s", "fetched", "http", html)
    values = extract(result, source())
    assert sorted(value.fields["price"] for value in values) == ["16", "18", "20"]
    assert {v.specs.get("eligibleQuantity_minValue") for v in values} == {None, "2", "5"}
    assert all(v.fields["currency"] == "USD" for v in values)
    assert any(event["reason"] == "separate_fee" for event in result.trace)


def test_jsonld_untyped_price_components_do_not_become_invented_total():
    html = b'''<script type="application/ld+json">{"@type":"Product","sku":"A","offers":{
      "@type":"Offer","priceSpecification":[{"price":"10"},{"price":"5"}]}}</script>'''
    result = FetchResult("s", "fetched", "http", html)
    values = extract(result, source())
    assert len(values) == 1 and "price" not in values[0].fields
    assert sum(event.get("reason") == "ambiguous_price_component" for event in result.trace) == 2


def test_jsonld_traversal_limit_is_reported():
    import json

    html = ('<script type="application/ld+json">' + json.dumps([
        {"@type": "Product", "sku": str(i), "offers": {"price": str(i)}} for i in range(1001)
    ]) + '</script>').encode()
    result = FetchResult("s", "fetched", "http", html)
    values = extract(result, source())
    assert len(values) == 1000
    assert any(event["event"] == "jsonld_truncated" and event["observed_count"] == 1001
               for event in result.trace)


def test_jsonld_offers_limit_is_reported():
    import json

    payload = {"@type": "Product", "sku": "A", "offers": [
        {"@type": "Offer", "price": str(i), "priceCurrency": "USD"} for i in range(1001)]}
    html = ('<script type="application/ld+json">' + json.dumps(payload) + '</script>').encode()
    result = FetchResult("s", "fetched", "http", html)
    values = extract(result, source())
    assert len(values) == 1000
    assert any(event["event"] == "jsonld_truncated" and event["reason"] == "offers_limit"
               and event["observed_count"] == 1001 for event in result.trace)


def test_jsonld_page_wide_candidate_limit_across_nested_variants():
    import json

    payload = {"@type": "ProductGroup", "hasVariant": [
        {"@type": "Product", "sku": sku, "offers": [
            {"@type": "Offer", "price": str(i), "priceCurrency": "USD"} for i in range(700)]}
        for sku in ("M", "L")]}
    html = ('<script type="application/ld+json">' + json.dumps(payload) + '</script>').encode()
    result = FetchResult("s", "fetched", "http", html)
    values = extract(result, source())
    assert len(values) == 1000
    assert len({value.locator for value in values}) == 1000
    assert any(event["event"] == "jsonld_truncated" and event["reason"] == "page_candidate_limit"
               and event["processed_count"] == 1000 for event in result.trace)


def test_jsonld_page_wide_offer_limit_even_when_offers_are_invalid():
    import json

    payload = {"@type": "ProductGroup", "hasVariant": [
        {"@type": "Product", "sku": sku, "offers": [None] * 700} for sku in ("M", "L")]}
    html = ('<script type="application/ld+json">' + json.dumps(payload) + '</script>').encode()
    result = FetchResult("s", "fetched", "http", html)
    assert extract(result, source()) == []
    assert any(event["event"] == "jsonld_truncated" and event["reason"] == "page_offer_limit"
               and event["processed_count"] == 1000 for event in result.trace)
