"""JSON the page loaded corroborates shown prices; it never supplies them."""
from __future__ import annotations

import json
import threading
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from su_crawler.generic_extraction import HEURISTIC_FLAG, extract_generic
from su_crawler.models import FetchResult, Source
from su_crawler.network_evidence import CONFLICT_FLAG

CARDS = "".join(f'<div class="card"><a href="/p/{i}"><h3>Desk Lamp {i}</h3></a><span>{i}9,000원</span></div>'
                for i in range(1, 4))
HTML = f'<main class="grid">{CARDS}</main>'.encode()


def page(records, *, html=HTML, url="https://shop.example/lamps"):
    body = json.dumps({"data": {"items": records}}).encode()
    return FetchResult("s", "fetched", "playwright", content=html, final_url=url,
                       network_responses=[{"url": "https://shop.example/api/items", "status": 200,
                                           "content_type": "application/json", "body": body}])


def test_matching_record_confirms_shown_price_and_lifts_heuristic_flag():
    result = page([{"productName": f"Desk Lamp {i}", "salePrice": i * 10000 + 9000, "listPrice": 99000}
                   for i in range(1, 4)])
    candidates = extract_generic(result)
    assert [c.fields["price"] for c in candidates] == ["19000", "29000", "39000"]
    confirmation = candidates[0].derived_values["network_confirmation"]
    assert confirmation["field"] == "salePrice" and confirmation["path"] == "$.data.items[0]"
    assert HEURISTIC_FLAG not in candidates[0].review_flags
    assert result.trace[-1] == {"event": "network_corroboration", "records": 3, "confirmed": 3,
                                "conflicts": 0, "json_only": 0}


def test_minor_units_match_and_other_amounts_conflict():
    result = page([{"title": "Desk Lamp 1", "price": 1900000},          # cents of 19000
                   {"title": "Desk Lamp 2", "price": 25000, "listPrice": 29000},  # list price is not current
                   {"title": "Other Product", "price": 1}])
    first, second, third = extract_generic(result)
    assert first.derived_values["network_confirmation"]["unit"] == "minor"
    assert CONFLICT_FLAG in second.review_flags and HEURISTIC_FLAG in second.review_flags
    assert second.derived_values["network_price_conflict"]["amounts"] == {"price": "25000"}
    assert "network_confirmation" not in third.derived_values and HEURISTIC_FLAG in third.review_flags
    assert result.trace[-1]["json_only"] == 1


def test_json_only_products_never_become_rows_and_bad_json_is_ignored():
    result = page([{"name": f"Recommended {i}", "price": i} for i in range(5)], html=b"<main><h1>Empty</h1></main>")
    assert extract_generic(result) == []
    broken = FetchResult("s", "fetched", "playwright", content=HTML, final_url="https://shop.example/x",
                         network_responses=[{"url": "u", "status": 200, "content_type": "application/json",
                                             "body": b"{not json"}])
    assert [c.fields["price"] for c in extract_generic(broken)] == ["19000", "29000", "39000"]


# -- live browser: bounded capture of same-site JSON -------------------------

PAGE = b"""<!doctype html><html><body><main class="grid"></main><script>
fetch('/api/items').then(r => r.json()).then(data => {
  document.querySelector('main').innerHTML = data.items.map((item, i) =>
    `<div class="card"><a href="/p/${i}"><h3>${item.name}</h3></a><span>${item.label}</span></div>`).join('');
  document.body.setAttribute('data-ready', '1');
});
</script></body></html>"""
ITEMS = json.dumps({"items": [{"name": f"Desk Lamp {i}", "price": i * 10000 + 9000,
                               "label": f"{i}9,000원"} for i in range(1, 4)]}).encode()


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        body, kind = (ITEMS, "application/json") if self.path.startswith("/api/") else (PAGE, "text/html; charset=utf-8")
        self.send_response(200)
        self.send_header("Content-Type", kind)
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


@contextmanager
def server():
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{httpd.server_port}"
    finally:
        httpd.shutdown()
        thread.join(timeout=2)


def test_browser_keeps_same_site_json_and_receipt_lists_it(tmp_path):
    from su_crawler.collectors import collect
    from su_crawler.doctor import doctor
    from su_crawler.pipeline import _save_evidence
    browser = next(item for item in doctor() if item["backend"] == "playwright")
    if browser["status"] != "available":
        pytest.skip(browser["reason"])
    with server() as url:
        source = Source(id="lamps", name="lamps", kind="web", location=url + "/lamps", product_ids=[],
                        allowed_domains=["127.0.0.1"], internal=True, respect_robots=False, timeout_seconds=45,
                        recipe=[{"action": "wait_for", "selector": "body[data-ready]"}])
        result = collect(source, str(tmp_path), "playwright")
    assert result.status == "fetched", result.message
    assert [item["url"].rsplit("/", 2)[-2:] for item in result.network_responses] == [["api", "items"]]
    candidates = extract_generic(result)
    assert all("network_confirmation" in c.derived_values for c in candidates) and len(candidates) == 3
    _save_evidence(tmp_path / "out", result, source)
    receipt = json.loads(open(result.evidence_artifacts["receipt"]["path"], encoding="utf-8").read())
    assert receipt["network_responses"][0]["size"] == len(ITEMS)
    # Observation artifacts keep their content/receipt/screenshot contract.
    assert set(result.evidence_artifacts) == {"content", "receipt", "screenshot"}


def test_inline_page_state_and_nested_price_blocks_corroborate():
    state = {"props": {"pageProps": {"wall": [
        {"copy": {"title": f"Desk Lamp {i}"}, "prices": {"currentPrice": i * 10000 + 9000, "initialPrice": 99000}}
        for i in range(1, 4)]}}}
    html = HTML + f'<script id="__NEXT_DATA__" type="application/json">{json.dumps(state)}</script>'.encode()
    result = FetchResult("s", "fetched", "http", content=html, final_url="https://shop.example/lamps")
    candidates = extract_generic(result)
    confirmation = candidates[0].derived_values["network_confirmation"]
    assert confirmation["url"] == "inline:script#__NEXT_DATA__" and confirmation["field"] == "prices.currentPrice"
    assert all(HEURISTIC_FLAG not in c.review_flags for c in candidates)


def test_only_product_json_is_retained():
    from su_crawler.network_evidence import has_product_records
    assert has_product_records(json.dumps({"items": [{"name": "Lamp", "price": 10}]}).encode())
    assert not has_product_records(json.dumps({"loginErrorModalTitle": "x", "flags": {"a": True}}).encode())
