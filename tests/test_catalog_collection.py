"""Offline approved-plan collection checks; no public market requests."""
from pathlib import Path
import json

import pytest

from su_crawler.catalog_collection import collect_plan
from su_crawler.models import Candidate, FetchResult
from su_crawler.storage import Store


def plan(urls, *, includes=None, excludes=None, excluded_urls=None, conditions=None):
    return {
        "id": "approved-1", "revision": 1, "state": "confirmed", "request_text": "Compare chosen sources",
        "topic": {"industry": "general retail", "product": "selected products", "market": "public web"},
        "include_terms": includes or [], "exclude_terms": excludes or [], "excluded_urls": excluded_urls or [],
        "conditions": conditions or [],
        "candidates": [{"id": str(i), "name": f"Page {i}", "url": url, "evidence_url": url,
                        "reason": "selected", "kind": "product", "selected": True, "origin": "manual"}
                       for i, url in enumerate(urls)],
    }


def html_product(name, *, price=None, currency=None, sku=None, offer_type="Offer", extra=None, links=""):
    offer = {"@type": offer_type}
    if price is not None:
        offer["lowPrice" if offer_type == "AggregateOffer" else "price"] = price
    if currency is not None:
        offer["priceCurrency"] = currency
    product = {"@type": "Product", "name": name, "offers": offer, **(extra or {})}
    if sku is not None:
        product["sku"] = sku
    return (f'<script type="application/ld+json">{json.dumps(product)}</script>{links}').encode()


def fake(pages, calls):
    def collector(source, _base_dir, backend):
        calls.append((source.location, backend, tuple(source.allowed_domains)))
        result = pages.get((source.location, backend))
        if result is None:
            return FetchResult(source.id, "tool_unavailable", backend, message="Offline browser unavailable")
        if isinstance(result, str):
            return FetchResult(source.id, result, backend, message=result)
        return FetchResult(source.id, "fetched", backend, content=result, final_url=source.location)
    return collector


def rows(output, run_id):
    store = Store(Path(output))
    try:
        return store.observations(run_id)
    finally:
        store.close()


@pytest.mark.parametrize("name,attributes", [
    ("Organic oats", {"category": "Grocery", "weight": "500 g"}),
    ("Cotton shirt", {"category": "Clothing", "color": "Blue", "size": "M", "brand": {"name": "Fixture brand"}}),
    ("Resistor kit", {"category": "Components", "mpn": "R-10K", "material": "film"}),
])
def test_jsonld_industries_preserve_raw_fields_and_review(tmp_path, name, attributes):
    url = "https://shop.example/item"
    calls = []
    result = collect_plan(tmp_path, plan([url]), output_dir=tmp_path / "out", max_pages=1,
                          collector=fake({(url, "http"): html_product(name, price="12.30", currency="USD", sku="S-1", extra=attributes)}, calls))
    observations = rows(result["output_dir"], result["run_id"])
    assert result["status"] == "needs_review"
    assert len(observations) == 1
    row = observations[0]
    assert row["raw_fields"]["name"] == name
    assert row["raw_fields"]["price"] == "12.30"
    assert row["raw_fields"]["currency"] == "USD"
    assert row["raw_fields"]["spec:category"] == attributes["category"]
    if "brand" in attributes:
        assert row["raw_fields"]["brand"] == "Fixture brand"
        assert "manufacturer" not in row["raw_fields"]
    assert row["status"] == "review" and not row["comparable"]
    assert row["evidence_mode"] == "static_html"
    assert Path(row["evidence_path"]).is_file()
    assert Path(result["report_path"]).is_file()
    assert {backend for _, backend, _ in calls} == {"http", "playwright"}


def test_missing_values_and_aggregate_are_never_fabricated(tmp_path):
    urls = ["https://a.example/no-price", "https://b.example/range"]
    pages = {(urls[0], "http"): html_product("No price"),
             (urls[1], "http"): html_product("Range", price="5", offer_type="AggregateOffer")}
    result = collect_plan(tmp_path, plan(urls), output_dir=tmp_path / "out", max_pages=2, collector=fake(pages, []))
    observations = rows(result["output_dir"], result["run_id"])
    assert len(observations) == 2
    assert all(row["amount"] is None and row["currency"] is None and not row["comparable"] for row in observations)
    assert all(row["status"] == "review" for row in observations)
    assert all("item_id" not in row["raw_fields"] and "sku" not in row["raw_fields"] for row in observations)
    assert next(row for row in observations if row["raw_fields"]["name"] == "Range")["raw_fields"]["spec:lowPrice"] == "5"


def test_jsonld_variants_and_offer_options_remain_distinct(tmp_path):
    url = "https://shop.example/shirt"
    data = {"@type": "Product", "name": "Shirt", "hasVariant": [
        {"@type": "Product", "name": "Shirt blue", "sku": "BLUE-M", "size": "M",
         "offers": {"@type": "Offer", "name": "Blue medium", "price": "19", "priceCurrency": "USD"}},
        {"@type": "Product", "name": "Shirt red", "sku": "RED-L", "size": "L",
         "offers": {"@type": "Offer", "name": "Red large", "price": "21", "priceCurrency": "USD"}},
    ]}
    page = f'<script type="application/ld+json">{json.dumps(data)}</script>'.encode()
    result = collect_plan(tmp_path, plan([url]), output_dir=tmp_path / "out", max_pages=1,
                          collector=fake({(url, "http"): page}, []))
    observations = rows(result["output_dir"], result["run_id"])
    assert {row["raw_fields"]["sku"] for row in observations} == {"BLUE-M", "RED-L"}
    assert {row["raw_fields"]["option"] for row in observations} == {"Blue medium", "Red large"}
    assert all(row["status"] == "review" and not row["comparable"] for row in observations)


def test_exclusions_page_limit_and_cross_host_discovery(tmp_path):
    first = "https://shop.example/start"
    second = "https://shop.example/next"
    forbidden = "https://shop.example/forbidden"
    external = "https://other.example/product"
    links = f'<a href="/next">Next</a><a href="/forbidden">Forbidden</a><a href="{external}">Other</a>'
    calls = []
    pages = {(first, "http"): html_product("Oats", price="3", currency="USD", links=links),
             (second, "http"): html_product("Excluded shirt", price="9", currency="USD")}
    result = collect_plan(tmp_path, plan([first, forbidden], includes=["oats"], excluded_urls=[forbidden]),
                          output_dir=tmp_path / "out", max_pages=1, collector=fake(pages, calls))
    assert {url for url, _, _ in calls} == {first}
    assert any(item["url"] == forbidden and item["status"] == "excluded" for item in result["coverage"])
    assert any(item["url"] == second and item["status"] == "unprocessed" for item in result["coverage"])
    assert all(item["url"] != external for item in result["coverage"])
    assert result["status"] == "partial"


def test_exclude_term_and_blocked_fallback(tmp_path):
    urls = ["https://shop.example/shirt", "https://blocked.example/item"]
    calls = []
    pages = {(urls[0], "http"): html_product("Red shirt", price="25", currency="USD"),
             (urls[1], "http"): "policy_denied"}
    result = collect_plan(tmp_path, plan(urls, excludes=["SHIRT"]), output_dir=tmp_path / "out",
                          max_pages=2, collector=fake(pages, calls))
    assert {item["status"] for item in result["coverage"]} == {"no_data", "policy_denied"}
    assert (urls[1], "playwright", ("blocked.example",)) not in calls
    assert rows(result["output_dir"], result["run_id"]) == []
    assert result["status"] == "partial"


def test_unchecked_candidate_cannot_be_rediscovered(tmp_path):
    first, unchecked = "https://shop.example/start", "https://shop.example/unchecked"
    snapshot = plan([first, unchecked])
    snapshot["candidates"][1]["selected"] = False
    calls = []
    page = html_product("Selected product", links='<a href="/unchecked">Unchecked</a>')
    result = collect_plan(tmp_path, snapshot, output_dir=tmp_path / "out", collector=fake({(first, "http"): page}, calls))
    assert {url for url, _, _ in calls} == {first}
    assert any(item["url"] == unchecked and item["status"] == "excluded" for item in result["coverage"])


def test_item_list_products_and_public_nondefault_port(tmp_path):
    url = "https://shop.example:8443/catalog"
    data = {"@type": "ItemList", "itemListElement": [
        {"@type": "ListItem", "item": {"@type": "Product", "name": "Part A", "sku": "A1",
         "offers": {"@type": "Offer", "price": "7", "priceCurrency": "EUR"}}},
        {"@type": "ListItem", "item": {"@type": "Product", "name": "Part B", "sku": "B2",
         "offers": {"@type": "Offer", "price": "8", "priceCurrency": "EUR"}}},
    ]}
    page = f'<script type="application/ld+json">{json.dumps(data)}</script>'.encode()
    calls = []
    result = collect_plan(tmp_path, plan([url]), output_dir=tmp_path / "out", max_pages=1,
                          collector=fake({(url, "http"): page}, calls))
    assert {row["raw_fields"]["sku"] for row in rows(result["output_dir"], result["run_id"])} == {"A1", "B2"}
    assert all(allowed == ("shop.example",) for _, _, allowed in calls)


def test_known_adapter_price_stays_review_when_plan_conditions_unchecked(tmp_path, monkeypatch):
    url = "https://known.example/item"
    monkeypatch.setitem(__import__("su_crawler.catalog_collection", fromlist=["HOST_ADAPTERS"]).HOST_ADAPTERS,
                        "known.example", "jetcar")
    candidate = Candidate({"item_id": "REAL-1", "name": "Model", "price": "10", "currency": "USD"},
                          {key: {"location": "css:.item", "raw": value, "display_state": "visible"}
                           for key, value in {"item_id": "REAL-1", "name": "Model", "price": "10", "currency": "USD"}.items()},
                          "css:.item", "known_adapter", source_visibility="visible", evidence_mode="rendered_dom")
    monkeypatch.setattr("su_crawler.catalog_collection.extract", lambda _result, _source: [candidate])
    snapshot = plan([url])
    snapshot["request_text"] = "Only delivery included and no membership restriction"
    result = collect_plan(tmp_path, snapshot, output_dir=tmp_path / "out", max_pages=1,
                          collector=fake({(url, "http"): b"<html>fixture</html>"}, []))
    observation = rows(result["output_dir"], result["run_id"])[0]
    assert observation["raw_fields"]["price"] == "10"
    assert observation["status"] == "review" and not observation["comparable"]
    assert "natural-language conditions" in observation["reason"]
    assert result["plan_scope"]["request_text"] == snapshot["request_text"]


def test_requires_confirmed_plan_and_selected_limit(tmp_path):
    snapshot = plan(["https://a.example/a", "https://a.example/b"])
    snapshot["state"] = "draft"
    with pytest.raises(ValueError, match="confirmed"):
        collect_plan(tmp_path, snapshot, output_dir=tmp_path / "out", collector=fake({}, []))
    snapshot["state"] = "confirmed"
    with pytest.raises(ValueError, match="exceed"):
        collect_plan(tmp_path, snapshot, output_dir=tmp_path / "out", max_pages=1, collector=fake({}, []))


def test_structured_scope_preserves_unknown_and_excludes_wrong_brand(tmp_path):
    url = "https://shop.example/cup-rice"
    items = [
        {"@type": "Product", "name": "오뚜기 컵밥", "offers": {"@type": "Offer", "price": "10", "priceCurrency": "KRW"}},
        {"@type": "Product", "name": "컵밥", "brand": {"name": "다른 회사"}, "offers": {"@type": "Offer", "price": "11", "priceCurrency": "KRW"}},
    ]
    page = f'<script type="application/ld+json">{json.dumps(items, ensure_ascii=False)}</script>'.encode()
    snapshot = plan([url], conditions=[{"field": "brand", "operator": "equals", "value": "오뚜기"}])
    result = collect_plan(tmp_path, snapshot, output_dir=tmp_path / "out", max_pages=1,
                          collector=fake({(url, "http"): page}, []))
    coverage = result["coverage"][0]
    assert coverage["products_extracted"] == 2
    assert coverage["scope_counts"] == {"matched": 0, "unknown": 1, "excluded": 1, "not_checked": 0}
    assert {entry["status"] for entry in coverage["scope_assessments"]} == {"unknown", "excluded"}
    observations = rows(result["output_dir"], result["run_id"])
    assert len(observations) == 1
    assert observations[0]["raw_fields"]["name"] == "오뚜기 컵밥"
    assert observations[0]["status"] == "review"
    assert observations[0]["derived_values"]["scope_assessment"]["status"] == "unknown"
    assert result["plan_scope"]["conditions"] == snapshot["conditions"]


def test_explicit_structured_conditions_do_not_infer_new_stock(tmp_path):
    url = "https://shop.example/bearing"
    page = html_product("6204-2RS bearing", price="9", currency="KRW", extra={"mpn": "6204-2RS"})
    snapshot = plan([url], conditions=[{"field": "model", "operator": "equals", "value": "6204-2RS"},
                                       {"field": "condition", "operator": "equals", "value": "new"}])
    result = collect_plan(tmp_path, snapshot, output_dir=tmp_path / "out", max_pages=1,
                          collector=fake({(url, "http"): page}, []))
    assert result["coverage"][0]["scope_counts"]["unknown"] == 1
    assert rows(result["output_dir"], result["run_id"])[0]["status"] == "review"


def test_jsonld_product_condition_attested_and_conflicting_offer_excluded(tmp_path):
    url = "https://shop.example/item"
    page = html_product("Bearing", price="9", currency="KRW", extra={
        "itemCondition": "https://schema.org/NewCondition",
    })
    snapshot = plan([url], conditions=[{"field": "condition", "operator": "equals", "value": "new"}])
    result = collect_plan(tmp_path, snapshot, output_dir=tmp_path / "out", max_pages=1,
                          collector=fake({(url, "http"): page}, []))
    assert result["coverage"][0]["scope_counts"]["matched"] == 1
    assert result["coverage"][0]["scope_assessments"][0]["checks"][0]["evidence"][0]["location"].endswith("Product.itemCondition")
    assert rows(result["output_dir"], result["run_id"])[0]["raw_fields"]["condition"] == "https://schema.org/NewCondition"

    data = {"@type": "Product", "name": "Bearing", "itemCondition": "https://schema.org/NewCondition",
            "offers": {"@type": "Offer", "itemCondition": "https://schema.org/UsedCondition", "price": "9", "priceCurrency": "KRW"}}
    conflicted = f'<script type="application/ld+json">{json.dumps(data)}</script>'.encode()
    other = collect_plan(tmp_path, snapshot, output_dir=tmp_path / "other", max_pages=1,
                         collector=fake({(url, "http"): conflicted}, []))
    assert other["coverage"][0]["scope_counts"]["excluded"] == 1
    assert rows(other["output_dir"], other["run_id"]) == []


def test_no_conditions_are_not_counted_as_matched(tmp_path):
    url = "https://shop.example/item"
    result = collect_plan(tmp_path, plan([url]), output_dir=tmp_path / "out", max_pages=1,
                          collector=fake({(url, "http"): html_product("Oats", price="3", currency="USD")}, []))
    assert result["coverage"][0]["scope_counts"] == {"matched": 0, "unknown": 0, "excluded": 0, "not_checked": 1}
    assert result["coverage"][0]["scope_assessments"][0]["reason"] == "No structured product conditions were provided"
