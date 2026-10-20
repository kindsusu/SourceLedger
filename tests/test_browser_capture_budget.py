"""Local-fixture regressions for final evidence capture after list expansion."""
from __future__ import annotations

import threading
import time
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from su_crawler import collectors
from su_crawler.collectors import collect
from su_crawler.doctor import doctor
from su_crawler.models import Source


# Every click loads one more card over the network; the list cannot finish
# inside the page budget, and a request is usually in flight at the deadline.
ENDLESS = b"""<!doctype html><html><body><div id="list">
<div class="card">Car 1</div><button class="more" onclick="grow(this)">More</button></div>
<script>let n = 1; function grow(button) {
  fetch('/slow').then(r => r.text()).then(() => { const e = document.createElement('div');
    e.className = 'card'; e.textContent = 'Car ' + (++n); button.before(e); });
}</script></body></html>"""

# Expansion completes, then a request never finishes, so the page never settles.
UNSETTLED = b"""<!doctype html><html><body><div id="list">
<div class="card">Car 1</div><button class="more" onclick="finish(this)">More</button></div>
<script>function finish(button) {
  for (let i = 2; i <= 40; i++) { const e = document.createElement('div'); e.className = 'card';
    e.textContent = 'Car ' + i; button.before(e); }
  button.remove();
  setTimeout(() => fetch('/hang').catch(() => {}), 50);
}</script></body></html>"""


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/slow":
            time.sleep(0.9)
            body = b"ok"
        elif self.path == "/hang":
            time.sleep(20)
            body = b"late"
        elif self.path == "/endless":
            body = ENDLESS
        else:
            body = UNSETTLED
        try:
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except OSError:
            pass

    def log_message(self, *args):
        pass


@contextmanager
def server():
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    httpd.daemon_threads = True
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{httpd.server_port}"
    finally:
        httpd.shutdown()
        thread.join(timeout=2)


def _source(url, timeout, progress=1):
    return Source(id="budget", name="budget", kind="web", location=url, product_ids=[],
                  allowed_domains=["127.0.0.1"], internal=True, respect_robots=False,
                  timeout_seconds=timeout,
                  recipe=[{"action": "expand_list", "selector": "#list", "item_selector": ".card",
                           "button_selector": ".more", "initial_wait_seconds": 1,
                           "progress_wait_seconds": progress}])


@pytest.fixture(autouse=True)
def _browser():
    if next(item for item in doctor() if item["backend"] == "playwright")["status"] != "available":
        pytest.skip("Browser unavailable")


def test_expansion_that_uses_the_budget_still_keeps_expanded_dom(tmp_path):
    with server() as url:
        result = collect(_source(url + "/endless", 16, progress=3), str(tmp_path), "playwright")
    assert result.status == "fetched", (result.message, result.trace[-3:])
    expansion = next(event for event in result.trace if event["event"] == "list_expansion")
    # Budget exhaustion is honest: the list is incomplete, not complete.
    assert expansion["status"] == "partial"
    assert expansion["observed_count"] > 5
    # Every observed card is in the retained final DOM, not just the first one.
    assert result.content.count(b'class="card"') >= expansion["observed_count"]


def test_settle_timeout_retains_expanded_snapshot_for_review(tmp_path):
    with server() as url:
        result = collect(_source(url + "/unsettled", 10), str(tmp_path), "playwright")
    assert result.status == "fetched", (result.message, result.trace[-3:])
    assert result.message == collectors.UNSETTLED_MESSAGE
    assert result.content.count(b'class="card"') == 40
    events = [event["event"] for event in result.trace]
    assert "observation_unsettled" in events
    settlement = [event for event in result.trace if event["event"] == "observation_settlement"]
    assert settlement and all(event["status"] == "timeout" for event in settlement)
    expansion = next(event for event in result.trace if event["event"] == "list_expansion")
    assert (expansion["status"], expansion["observed_count"]) == ("complete", 40)


def test_unsettled_capture_is_never_complete_and_rows_need_review():
    from pathlib import Path
    from su_crawler.extraction import extract
    from su_crawler.models import FetchResult
    from su_crawler.site_collection import UNSETTLED_FLAG, _sections

    url = "https://gongcar.kr/rentpass"
    payload = (Path(__file__).parent / "fixtures" / "adapters" / "gongcar_rentpass.html").read_bytes()
    trace = [{"event": "list_expansion", "section": "popular", "status": "complete",
              "reason": "button_absent", "observed_count": 3, "click_count": 2},
             {"event": "list_expansion", "section": "promotion", "status": "complete",
              "reason": "button_absent", "observed_count": 1, "click_count": 1}]
    source = Source(id="s", name="s", kind="web", location=url, product_ids=[], adapter="gongcar")
    settled = FetchResult("s", "fetched", "playwright", content=payload, final_url=url,
                          media_type="text/html", trace=list(trace))
    assert {s["status"] for s in _sections(settled, extract(settled, source), "gongcar")} == {"complete"}
    unsettled = FetchResult("s", "fetched", "playwright", content=payload, final_url=url, media_type="text/html",
                            trace=[*trace, {"event": "observation_unsettled", "reason": "settle_timeout"}])
    candidates = extract(unsettled, source)
    sections = _sections(unsettled, candidates, "gongcar")
    assert candidates and all(UNSETTLED_FLAG in candidate.review_flags for candidate in candidates)
    assert {(s["status"], s["reason"]) for s in sections} == {("partial", "observation_unsettled")}
