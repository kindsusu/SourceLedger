"""A rotating banner must not keep a stable product list from settling."""
from __future__ import annotations

import threading
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from su_crawler.collectors import collect
from su_crawler.doctor import doctor
from su_crawler.models import Source

PAGE = b"""<!doctype html><html><body>
<div id="banner">0</div>
<ul class="list"><li class="item"><a href="/p/1">One</a> <span>1,000\xec\x9b\x90</span></li>
<li class="item"><a href="/p/2">Two</a> <span>2,000\xec\x9b\x90</span></li></ul>
<script>let n = 0; setInterval(() => { document.getElementById('banner').textContent = String(++n); }, 100);</script>
</body></html>"""


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(PAGE)

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


def _collect(url, tmp_path, hint):
    return collect(Source(id="ticker", name="ticker", kind="web", location=url + "/list", product_ids=[],
                          allowed_domains=["127.0.0.1"], internal=True, respect_robots=False,
                          timeout_seconds=45, settle_selector=hint), str(tmp_path), "playwright")


def test_product_region_hint_settles_only_after_the_whole_page_timed_out(tmp_path):
    browser = next(item for item in doctor() if item["backend"] == "playwright")
    if browser["status"] != "available":
        pytest.skip(browser["reason"])
    with server() as url:
        result = _collect(url, tmp_path, "ul.list > li.item")
    assert result.status == "fetched", result.message
    settlements = [event for event in result.trace if event.get("event") == "observation_settlement"]
    assert [event["status"] for event in settlements] == ["timeout", "settled"]
    assert settlements[-1]["scope_selector"] == "ul.list > li.item"
    assert not any(event.get("event") == "observation_unsettled" for event in result.trace)
