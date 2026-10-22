"""Offline tests for bounded browser option and page enumeration."""
from __future__ import annotations

import time
import threading
import os
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from bs4 import BeautifulSoup
from browser_diagnostics import capture_browser_diagnostics

from su_crawler.browser_enumeration import enumerate_buttons, enumerate_options, paginate, scroll_list, validate_enumeration_step
from su_crawler.browser_runtime import select_browser_runtime
from su_crawler.collectors import _evidence_html_snapshot, collect
from su_crawler.extraction import extract
from su_crawler.models import FetchResult, Source


@pytest.fixture
def page(request):
    pytest.importorskip("playwright")
    from playwright.sync_api import sync_playwright

    with sync_playwright() as pw:
        runtime = select_browser_runtime(pw.chromium)
        if runtime is None:
            if os.environ.get("CI") == "true":
                pytest.fail("Chromium unavailable in CI")
            pytest.skip("Chromium unavailable")
        try:
            browser = pw.chromium.launch(headless=True, timeout=5000, **runtime.launch_options())
        except Exception as exc:
            if os.environ.get("CI") == "true":
                pytest.fail(f"Chromium launch failed in CI: {type(exc).__name__}")
            pytest.skip(f"Chromium unavailable: {type(exc).__name__}")
        try:
            with capture_browser_diagnostics(browser.new_page(), request) as test_page:
                yield test_page
        finally:
            browser.close()


def _remaining(seconds=8):
    deadline = time.monotonic() + seconds
    return lambda: (deadline - time.monotonic()) * 1000


def _wait_for_scroll_mutation(page, monkeypatch, predicate):
    """Exercise evidence preservation after a confirmed real scroll mutation.

    Browser/compositor event delivery is not guaranteed within the recipe's
    50 ms progress interval, especially on loaded CI workers. This fixture
    synchronizes the test page's mutation without changing production waits.
    """
    locator_type = type(page.locator('.item'))
    original_scroll = locator_type.scroll_into_view_if_needed

    def scroll_then_wait(locator, *args, **kwargs):
        original_scroll(locator, *args, **kwargs)
        page.wait_for_function(predicate, timeout=3000)

    monkeypatch.setattr(locator_type, 'scroll_into_view_if_needed', scroll_then_wait)


def test_dependent_native_options_keep_distinct_html_and_candidate_provenance(page):
    page.set_content("""<select id=brand><option value=''>Choose</option><option value=a>A</option>
      <option value=b>B</option></select><select id=trim></select><strong id=price></strong>
      <script>
      function update(){const b=document.querySelector('#brand').value;
        document.querySelector('#trim').innerHTML=b==='a' ?
        '<option value=basic>Basic</option><option value=deluxe>Deluxe</option>' :
        '<option value=only>Only</option>';
        document.querySelector('#price').textContent=b==='a'?'100':'200'}
          document.querySelector('#brand').onchange=update;update();
      </script>""")
    snapshots = []
    trace = []
    enumerate_options(page, {"action": "enumerate_options", "selectors": ["#brand", "#trim"],
                             "progress_wait_seconds": 0.05}, _remaining(),
                      lambda selected, values: snapshots.append({"content": _evidence_html_snapshot(page),
                                                         "final_url": "https://example.org/p", "selections": selected,
                                                         "selection_values": values}), trace)
    assert trace[-1]["status"] == "complete"
    assert len(snapshots) == 3
    assert [s["selections"]["#trim"] for s in snapshots] == ["Basic", "Deluxe", "Only"]
    assert b'selected=""' in snapshots[1]["content"]
    source = Source("s", "S", "web", "https://example.org/p", ["p"], selectors={"price": "#price"})
    fetched = FetchResult("s", "fetched", "playwright", variant_snapshots=snapshots)
    candidates = extract(fetched, source)
    assert [c.fields["price"] for c in candidates] == ["100", "100", "200"]
    assert [c.derived_values["capture_snapshot_index"] for c in candidates] == [0, 1, 2]
    assert candidates[2].specs["selected:#brand"] == "B"
    assert candidates[2].evidence["spec:selected:#brand"]["raw"] == "B"
    assert candidates[2].specs["selected_value:#trim"] == "only"
    assert candidates[2].evidence["_selection"]["raw"]["#trim"] == "Only"


def test_option_budget_and_unavailable_label(page):
    page.set_content("""<select id=o><option value=a>A</option><option value=b>B</option>
      <option value=c>Request a quote</option></select>""")
    trace, selected = [], []
    enumerate_options(page, {"action": "enumerate_options", "selectors": ["#o"],
                             "max_variants": 1, "progress_wait_seconds": 0.01},
                      _remaining(), lambda labels, values: selected.append(labels), trace, ["Request a quote"])
    assert (trace[-1]["status"], trace[-1]["reason"], len(selected)) == ("partial", "variant_budget", 1)


def test_async_dependent_options_wait_for_ready_state(page):
    page.set_content("""<select id=make><option value=''>Choose</option><option value=a>A</option></select>
      <select id=trim><option value=old>Old</option></select><div id=ready hidden>Ready</div>
      <script>document.querySelector('#make').onchange=()=>setTimeout(()=>{
        document.querySelector('#trim').innerHTML='<option value=new>New</option>';
        document.querySelector('#ready').hidden=false},100)</script>""")
    selected, trace = [], []
    enumerate_options(page, {"action": "enumerate_options", "selectors": ["#make", "#trim"],
                             "ready_selector": "#ready", "progress_wait_seconds": 0.01},
                      _remaining(), lambda labels, values: selected.append(labels), trace)
    assert selected == [{"#make": "A", "#trim": "New"}], trace
    assert trace[-1]["status"] == "complete"


def test_duplicate_labels_remain_distinct_and_reset_is_unknown(page):
    page.set_content("""<select id=o><option value=''>Choose</option>
      <option value=a>Same</option><option value=b>Same</option></select>""")
    selected, trace = [], []
    enumerate_options(page, {"action": "enumerate_options", "selectors": ["#o"],
                             "progress_wait_seconds": 0.01}, _remaining(), lambda labels, values: selected.append(labels), trace)
    assert selected == [{"#o": "Same"}, {"#o": "Same"}]
    assert trace[-1]["captured_count"] == 2
    page.set_content("""<select id=o onchange="this.value=''">
      <option value=''>Choose</option><option value=a>A</option></select>""")
    selected, trace = [], []
    enumerate_options(page, {"action": "enumerate_options", "selectors": ["#o"],
                             "progress_wait_seconds": 0.01}, _remaining(), lambda labels, values: selected.append(labels), trace)
    assert selected == []
    assert (trace[-1]["status"], trace[-1]["reason"]) == ("unknown", "selection_reset")


def test_paginate_captures_replaced_pages_and_stops_at_disabled_next(page):
    page.set_content("""<div id=list><article class=item>A</article><button class=next onclick="
      document.querySelector('.item').textContent='B';this.disabled=true">Next</button></div>""")
    snapshots, trace = [], []
    paginate(page, {"action": "paginate", "selector": "#list", "item_selector": ".item",
                    "next_selector": ".next", "progress_wait_seconds": 0.01},
             _remaining(), lambda selected, values, expected: snapshots.append((_evidence_html_snapshot(page, expected), selected)), trace)
    assert [b"A" in entry[0] for entry in snapshots] == [True, False]
    assert [entry[1]["page"] for entry in snapshots] == ["1", "2"]
    assert (trace[-1]["status"], trace[-1]["reason"]) == ("complete", "next_disabled")


def test_scroll_without_sentinel_is_partial_and_single_snapshot(page):
    page.set_content("""<div id=list><article class=item>A</article></div>""")
    snapshots, trace = [], []
    scroll_list(page, {"action": "scroll_list", "selector": "#list", "item_selector": ".item",
                       "progress_wait_seconds": 0.01}, _remaining(),
                lambda selected, values, expected: snapshots.append(selected), trace)
    assert len(snapshots) == 1
    assert (trace[-1]["status"], trace[-1]["reason"]) == ("partial", "no_growth_unconfirmed")


def test_scroll_explicit_end_is_complete(page):
    page.set_content("""<div id=list><article class=item>A</article><div class=end>End</div></div>""")
    snapshots, trace = [], []
    scroll_list(page, {"action": "scroll_list", "selector": "#list", "item_selector": ".item",
                       "end_selector": ".end"}, _remaining(),
                lambda selected, values, expected: snapshots.append(selected), trace)
    assert (len(snapshots), trace[-1]["status"]) == (1, "complete")


@pytest.mark.parametrize('mutation_delay_ms', [0, 120])
def test_scroll_replacement_preserves_prior_evidence(page, monkeypatch, mutation_delay_ms):
    page.set_viewport_size({"width": 320, "height": 240})
    page.set_content("""<div id=list><article class=item>A</article>
      <article class=item style='margin-top:700px;height:40px'>B</article></div>
      <script>addEventListener('scroll', () => {
        if (scrollY > 0) setTimeout(() => {
          document.querySelector('#list').innerHTML='<article class=item>C</article>';
        }, MUTATION_DELAY);
      }, {once:true})</script>""".replace('MUTATION_DELAY', str(mutation_delay_ms)))
    assert page.evaluate("window.scrollY") == 0
    assert page.locator(".item").last.evaluate("el => el.getBoundingClientRect().top > innerHeight")
    _wait_for_scroll_mutation(page, monkeypatch, "document.querySelector('#list').textContent === 'C'")
    snapshots, trace = [], []
    scroll_list(page, {"action": "scroll_list", "selector": "#list", "item_selector": ".item",
                       "progress_wait_seconds": 0.05}, _remaining(),
                lambda selected, values, expected: snapshots.append(_evidence_html_snapshot(page, expected)), trace)
    assert (trace[-1]["status"], trace[-1]["reason"]) == ("partial", "list_replaced")
    assert len(snapshots) == 1
    assert [node.get_text(strip=True) for node in BeautifulSoup(snapshots[0], "html.parser").select("#list .item")] == ["A", "B"]


def test_scroll_mutation_between_count_and_snapshot_keeps_no_mixed_evidence(page):
    page.set_content("<div id=list><article class=item>A</article></div>")
    snapshots, trace = [], []

    def mutate_then_capture(selected, values, expected):
        page.locator(".item").evaluate("el => el.textContent = 'B'")
        snapshots.append(_evidence_html_snapshot(page, expected))

    scroll_list(page, {"action": "scroll_list", "selector": "#list", "item_selector": ".item",
                       "progress_wait_seconds": 0.01}, _remaining(), mutate_then_capture, trace)
    assert snapshots == []
    assert (trace[-1]["status"], trace[-1]["reason"], trace[-1]["captured_count"]) == (
        "unknown", "list_changed_during_capture", 0)


def test_paginate_mutation_between_count_and_snapshot_keeps_no_mixed_evidence(page):
    page.set_content("<div id=list><article class=item>A</article><button class=next>Next</button></div>")
    snapshots, trace = [], []

    def mutate_then_capture(selected, values, expected):
        page.locator(".item").evaluate("el => el.textContent = 'B'")
        snapshots.append(_evidence_html_snapshot(page, expected))

    paginate(page, {"action": "paginate", "selector": "#list", "item_selector": ".item",
                    "next_selector": ".next", "progress_wait_seconds": 0.01},
             _remaining(), mutate_then_capture, trace)
    assert snapshots == []
    assert (trace[-1]["status"], trace[-1]["reason"], trace[-1]["captured_count"]) == (
        "unknown", "list_changed_during_capture", 0)


def test_same_text_changed_product_attributes_are_not_mixed_into_snapshot(page):
    page.set_content("<div id=list><article class=item data-price='100'><a href='/a'>Same</a></article></div>")
    snapshots, trace = [], []

    def mutate_then_capture(selected, values, expected):
        page.locator(".item").evaluate("el => { el.dataset.price = '200'; el.querySelector('a').href = '/b'; }")
        snapshots.append(_evidence_html_snapshot(page, expected))

    scroll_list(page, {"action": "scroll_list", "selector": "#list", "item_selector": ".item",
                       "progress_wait_seconds": 0.01}, _remaining(), mutate_then_capture, trace)
    assert snapshots == []
    assert (trace[-1]["status"], trace[-1]["reason"]) == ("unknown", "list_changed_during_capture")


def test_scroll_after_saved_state_detects_same_text_attribute_replacement(page, monkeypatch):
    page.set_viewport_size({"width": 320, "height": 240})
    page.set_content("""<div id=list><article class=item><a href='/a'>Same</a></article>
      <article class=item style='margin-top:700px;height:40px'>B</article></div>
      <script>addEventListener('scroll', () => {
        if (scrollY > 0) document.querySelector('a').href='/b';
      }, {once:true})</script>""")
    _wait_for_scroll_mutation(page, monkeypatch, "document.querySelector('#list a').getAttribute('href') === '/b'")
    snapshots, trace = [], []
    scroll_list(page, {"action": "scroll_list", "selector": "#list", "item_selector": ".item",
                       "progress_wait_seconds": 0.05}, _remaining(),
                lambda selected, values, expected: snapshots.append(_evidence_html_snapshot(page, expected)), trace)
    assert len(snapshots) == 1
    soup = BeautifulSoup(snapshots[0], "html.parser")
    assert soup.select_one("#list a")["href"] == "/a"
    assert (trace[-1]["status"], trace[-1]["reason"]) == ("partial", "list_replaced")


@pytest.mark.parametrize("mode", ["scroll_end", "next_disabled", "next_absent"])
def test_completion_rechecks_listing_after_evidence_capture(page, mode):
    if mode == "scroll_end":
        page.set_content("<div id=list><article class=item data-price='100'>Same</article><div class=end>End</div></div>")
        step = {"action": "scroll_list", "selector": "#list", "item_selector": ".item",
                "end_selector": ".end", "progress_wait_seconds": 0.01}
        run = scroll_list
    else:
        button = "<button class=next disabled>Next</button>" if mode == "next_disabled" else ""
        page.set_content(f"<div id=list><article class=item data-price='100'>Same</article>{button}</div>")
        step = {"action": "paginate", "selector": "#list", "item_selector": ".item",
                "next_selector": ".next", "progress_wait_seconds": 0.01}
        run = paginate
    snapshots, trace = [], []

    def capture_then_mutate(selected, values, expected):
        snapshots.append(_evidence_html_snapshot(page, expected))
        page.locator(".item").evaluate("el => el.dataset.price = '200'")

    run(page, step, _remaining(), capture_then_mutate, trace)
    assert len(snapshots) == 1
    assert BeautifulSoup(snapshots[0], "html.parser").select_one("#list .item")["data-price"] == "100"
    assert (trace[-1]["status"], trace[-1]["reason"]) == ("partial", "list_changed_after_capture")


def test_scroll_snapshots_deduplicate_existing_rows():
    source = Source("s", "S", "web", "https://example.org/list", ["p"],
                    selectors={"name": ".name"}, row_selector="article")
    snapshots = [
        {"content": b"<article><span class=name>A</span></article>", "selections": {"scroll_items": "1"}},
        {"content": b"<article><span class=name>A</span></article><article><span class=name>B</span></article>",
         "selections": {"scroll_items": "2"}},
    ]
    candidates = extract(FetchResult("s", "fetched", "playwright", variant_snapshots=snapshots), source)
    assert [candidate.fields["name"] for candidate in candidates] == ["A", "B"]
    assert [candidate.derived_values["capture_snapshot_index"] for candidate in candidates] == [0, 1]


def test_reviewed_custom_buttons_with_data_value(page):
    page.set_content("""<div id=options><button type=button data-value=a onclick="
      document.querySelector('#price').textContent='100'">A</button>
      <button type=button data-value=b onclick="document.querySelector('#price').textContent='200'">B</button>
      <button type=submit data-value=unsafe>Unsafe</button></div><strong id=price></strong>""")
    snapshots, trace = [], []
    enumerate_buttons(page, {"action": "enumerate_buttons", "selector": "#options",
                             "option_selector": "button", "progress_wait_seconds": 0.01},
                      _remaining(), lambda selected, values: snapshots.append((_evidence_html_snapshot(page), selected, values)), trace)
    assert len(snapshots) == 2
    assert b"200" in snapshots[1][0]
    assert trace[-1]["status"] == "complete"
    assert trace[-1]["skipped_options"][0]["label"] == "Unsafe"


@contextmanager
def _local_option_page():
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            body = b"""<!doctype html><select id=o><option value=''>Choose</option>
              <option value=a>A</option><option value=b>B</option></select>
              <strong id=price>100</strong><script>document.querySelector('#o').onchange=()=>{
              document.querySelector('#price').textContent=document.querySelector('#o').value==='b'?'200':'100'
              }</script>"""
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        thread.join(timeout=2)


def test_collector_emits_option_snapshots_and_selected_values(tmp_path, monkeypatch):
    monkeypatch.setenv("SOURCELEDGER_HEADLESS", "1")
    with _local_option_page() as url:
        source = Source("s", "S", "web", url, ["p"], selectors={"price": "#price"},
                        allowed_domains=["127.0.0.1"], internal=True, respect_robots=False,
                        timeout_seconds=20, recipe=[{"action": "enumerate_options", "selectors": ["#o"],
                                                     "progress_wait_seconds": 0.01}])
        result = collect(source, str(tmp_path), "playwright")
    assert result.status == "fetched", result.message
    assert [s["selection_values"]["#o"] for s in result.variant_snapshots] == ["a", "b"]
    candidates = extract(result, source)
    assert [c.fields["price"] for c in candidates] == ["100", "200"]
    assert [c.specs["selected_value:#o"] for c in candidates] == ["a", "b"]
    assert all("selection_price_settlement_unconfirmed" in c.review_flags for c in candidates)
    # Without a reviewed price element the legacy evidence shape is unchanged.
    assert all("price_settlement" not in s for s in result.variant_snapshots)
    assert all("price_settlement" not in c.derived_values for c in candidates)
    assert all("price_settlement_counts" not in e for e in result.trace if e.get("event") == "option_enumeration")


def test_assertion_failure_retains_only_prior_option_captures(tmp_path, monkeypatch):
    monkeypatch.setenv("SOURCELEDGER_HEADLESS", "1")
    with _local_option_page() as url:
        source = Source("s", "S", "web", url, ["p"], allowed_domains=["127.0.0.1"],
                        internal=True, respect_robots=False, timeout_seconds=20,
                        recipe=[{"action": "enumerate_options", "selectors": ["#o"],
                                 "progress_wait_seconds": 0.01},
                                {"action": "assert_text", "selector": "#price", "text": "never present"}])
        result = collect(source, str(tmp_path), "playwright")
    assert result.status == "failed"
    assert result.content == b""
    assert len(result.variant_snapshots) == 2
    assert [s["selection_values"]["#o"] for s in result.variant_snapshots] == ["a", "b"]
    assert extract(result, source) == []


@pytest.mark.parametrize("step", [
    {"action": "enumerate_options", "selectors": ["#a", "#a"]},
    {"action": "enumerate_options", "selectors": ["#a"], "max_variants": 101},
    {"action": "paginate", "selector": "#list", "item_selector": ".item", "next_selector": ".next", "max_pages": 0},
    {"action": "enumerate_options", "selectors": ["#a"], "price_selector": ""},
    {"action": "enumerate_options", "selectors": ["#a"], "price_selector": "  "},
    {"action": "enumerate_options", "selectors": ["#a"], "price_selector": None},
    {"action": "enumerate_buttons", "selector": "#o", "option_selector": "button", "price_selector": 5},
    {"action": "paginate", "selector": "#list", "item_selector": ".item", "next_selector": ".next",
     "price_selector": "#price"},
])
def test_invalid_enumeration_budgets_and_selectors(step):
    with pytest.raises(ValueError):
        validate_enumeration_step(step)


_DELAYED_NEXT = b"""<!doctype html><html><body><div id=list><article class=item>A</article></div>
<script>fetch('/slow').then(r => r.text()).then(() => {
  const next = document.createElement('button'); next.className = 'next'; next.textContent = 'Next';
  next.onclick = () => { document.querySelector('.item').textContent = 'B'; next.remove(); };
  document.querySelector('#list').append(next);
});</script></body></html>"""


@contextmanager
def _delayed_next_server():
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path == "/slow":
                time.sleep(1.8)
                body = b"ok"
            else:
                body = _DELAYED_NEXT
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

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    httpd.daemon_threads = True
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{httpd.server_port}/list"
    finally:
        httpd.shutdown()
        thread.join(timeout=2)


def test_paginate_waits_for_pending_load_before_declaring_last_page(page):
    from su_crawler.browser_settlement import install_tracker_sync
    with _delayed_next_server() as url:
        install_tracker_sync(page)
        page.goto(url, wait_until="domcontentloaded")
        selected, trace = [], []
        # The next control is missing for 1.8 s while a request is pending.
        # A fixed one-second wait used to report this as the last page.
        paginate(page, {"action": "paginate", "selector": "#list", "item_selector": ".item",
                        "next_selector": ".next", "progress_wait_seconds": 1}, _remaining(15),
                 lambda labels, values, expected: selected.append(labels), trace)
    assert [entry["page"] for entry in selected] == ["1", "2"]
    assert (trace[-1]["status"], trace[-1]["reason"], trace[-1]["captured_count"]) == ("complete", "next_absent", 2)


def test_paginate_disabled_next_must_stay_disabled_through_the_window(page):
    page.set_content("""<div id=list><article class=item>A</article><button class=next disabled
      onclick="document.querySelector('.item').textContent='B';this.remove()">Next</button></div>
      <script>setTimeout(() => document.querySelector('.next').disabled = false, 300)</script>""")
    selected, trace = [], []
    paginate(page, {"action": "paginate", "selector": "#list", "item_selector": ".item",
                    "next_selector": ".next", "progress_wait_seconds": 1}, _remaining(),
             lambda labels, values, expected: selected.append(labels), trace)
    assert [entry["page"] for entry in selected] == ["1", "2"]
    assert (trace[-1]["status"], trace[-1]["reason"]) == ("complete", "next_absent")


# Synthetic quotes keyed by (plan, term); the "same" variant repeats a quote.
_QUOTES = {("a", "12"): "KRW 1,200", ("a", "24"): "KRW 2,400", ("b", "36"): "KRW 3,600",
           ("b", "12"): "KRW 3,000"}

_QUOTE_FORM = b"""<!doctype html><html><body>
<select id=model><option value=''>Choose</option><option value=a>Synthetic A</option>
<option value=b>Synthetic B</option></select>
<select id=term><option value=''>Choose</option></select>
<strong id=price>Choose a model</strong>
<script>
const variant = new URLSearchParams(location.search).get('v') || '';
const model = document.querySelector('#model'), term = document.querySelector('#term');
const price = document.querySelector('#price');
const terms = {a: ['12', '24'], b: ['36']};
model.onchange = () => {
  term.innerHTML = "<option value=''>Choose</option>" +
    (terms[model.value] || []).map(t => `<option value=${t}>${t} months</option>`).join('');
  price.textContent = 'Choose a term for ' + model.value;
};
term.onchange = () => {
  if (!term.value) return;
  fetch(`/price?m=${model.value}&t=${term.value}&v=${variant}`)
    .then(r => r.text()).then(text => { price.textContent = text; });
};
</script></body></html>"""

_QUOTE_BUTTONS = b"""<!doctype html><html><body>
<div id=options><button type=button data-value=a>Plan A</button>
<button type=button data-value=b>Plan B</button></div><strong id=price>Pick a plan</strong>
<script>document.querySelectorAll('#options button').forEach(button => button.onclick = () =>
  fetch('/price?m=' + button.dataset.value + '&t=12').then(r => r.text())
    .then(text => { document.querySelector('#price').textContent = text; }));</script></body></html>"""


@contextmanager
def _quote_server():
    from urllib.parse import parse_qs, urlsplit

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            parts = urlsplit(self.path)
            if parts.path == "/price":
                time.sleep(0.2)  # the quote arrives after the selection event
                query = {key: values[0] for key, values in parse_qs(parts.query).items()}
                key = (query.get("m", ""), query.get("t", ""))
                body = ("KRW 1,200" if query.get("v") == "same" and key == ("a", "24")
                        else _QUOTES.get(key, "KRW 9,900")).encode("utf-8")
                content_type = "text/plain; charset=utf-8"
            else:
                body = _QUOTE_BUTTONS if parts.path == "/buttons" else _QUOTE_FORM
                content_type = "text/html; charset=utf-8"
            try:
                self.send_response(200)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            except OSError:
                pass

        def log_message(self, *args):
            pass

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    httpd.daemon_threads = True
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{httpd.server_port}"
    finally:
        httpd.shutdown()
        thread.join(timeout=2)


def _settlement_capture(page, snapshots):
    def capture(selected, values, price_settlement=None):
        snapshots.append({"content": _evidence_html_snapshot(page), "final_url": "https://example.org/p",
                          "selections": selected, "selection_values": values,
                          "price_settlement": price_settlement})
    return capture


def _quote_candidates(snapshots):
    source = Source("s", "S", "web", "https://example.org/p", ["p"], selectors={"price": "#price"})
    return extract(FetchResult("s", "fetched", "playwright", variant_snapshots=snapshots), source)


def test_price_selector_accepts_reviewed_selector():
    validate_enumeration_step({"action": "enumerate_options", "selectors": ["#a"], "price_selector": "#price"})
    validate_enumeration_step({"action": "enumerate_buttons", "selector": "#o", "option_selector": "button",
                               "price_selector": ".quote"})


def test_collector_confirms_async_price_change_for_each_dependent_option(tmp_path, monkeypatch):
    monkeypatch.setenv("SOURCELEDGER_HEADLESS", "1")
    with _quote_server() as base:
        source = Source("s", "S", "web", base + "/form", ["p"], selectors={"price": "#price"},
                        allowed_domains=["127.0.0.1"], internal=True, respect_robots=False,
                        timeout_seconds=30, recipe=[{"action": "enumerate_options",
                                                     "selectors": ["#model", "#term"],
                                                     "price_selector": "#price",
                                                     "progress_wait_seconds": 0.2}])
        result = collect(source, str(tmp_path), "playwright")
    assert result.status == "fetched", result.message
    assert [s["selection_values"]["#term"] for s in result.variant_snapshots] == ["12", "24", "36"]
    assert [s["price_settlement"]["status"] for s in result.variant_snapshots] == ["changed"] * 3
    assert all(s["price_settlement"]["selector"] == "#price" and len(s["price_settlement"]["text_sha256"]) == 64
               for s in result.variant_snapshots)
    event = next(e for e in result.trace if e.get("event") == "option_enumeration")
    assert (event["status"], event["price_settlement_counts"]) == ("complete", {"changed": 3})
    candidates = extract(result, source)
    assert [c.fields["price"] for c in candidates] == ["KRW 1,200", "KRW 2,400", "KRW 3,600"]
    assert all("selection_price_settlement_unconfirmed" not in c.review_flags for c in candidates)
    assert all("identical_amounts_after_network_quiet_review" not in c.review_flags for c in candidates)
    assert [c.derived_values["price_settlement"] for c in candidates] == ["changed"] * 3


def test_unchanged_quote_after_quiet_network_is_flagged_for_review(page):
    from su_crawler.browser_settlement import install_tracker_sync
    with _quote_server() as base:
        install_tracker_sync(page)
        page.goto(base + "/form?v=same", wait_until="domcontentloaded")
        snapshots, trace = [], []
        enumerate_options(page, {"action": "enumerate_options", "selectors": ["#model", "#term"],
                                 "price_selector": "#price", "progress_wait_seconds": 0.2},
                          _remaining(30), _settlement_capture(page, snapshots), trace)
    assert [s["price_settlement"]["status"] for s in snapshots] == ["changed", "identical_after_quiet", "changed"]
    assert trace[-1]["price_settlement_counts"] == {"changed": 2, "identical_after_quiet": 1}
    candidates = _quote_candidates(snapshots)
    assert [c.fields["price"] for c in candidates] == ["KRW 1,200", "KRW 1,200", "KRW 3,600"]
    assert candidates[1].review_flags == ["identical_amounts_after_network_quiet_review"]
    assert candidates[1].derived_values["price_settlement"] == "identical_after_quiet"
    assert candidates[0].review_flags == [] and candidates[2].review_flags == []


def test_custom_buttons_confirm_async_price_change(page):
    from su_crawler.browser_settlement import install_tracker_sync
    with _quote_server() as base:
        install_tracker_sync(page)
        page.goto(base + "/buttons", wait_until="domcontentloaded")
        snapshots, trace = [], []
        enumerate_buttons(page, {"action": "enumerate_buttons", "selector": "#options", "option_selector": "button",
                                 "price_selector": "#price", "progress_wait_seconds": 0.2},
                          _remaining(20), _settlement_capture(page, snapshots), trace)
    assert [s["price_settlement"]["status"] for s in snapshots] == ["changed", "changed"]
    assert (trace[-1]["status"], trace[-1]["price_settlement_counts"]) == ("complete", {"changed": 2})
    candidates = _quote_candidates(snapshots)
    assert [c.fields["price"] for c in candidates] == ["KRW 1,200", "KRW 3,000"]
    assert all(c.review_flags == [] for c in candidates)


def test_missing_price_element_keeps_unconfirmed_flag(page):
    from su_crawler.browser_settlement import install_tracker_sync
    with _quote_server() as base:
        install_tracker_sync(page)
        page.goto(base + "/buttons", wait_until="domcontentloaded")
        snapshots, trace = [], []
        enumerate_buttons(page, {"action": "enumerate_buttons", "selector": "#options",
                                 "option_selector": "button[data-value=a]", "price_selector": "#missing-quote",
                                 "progress_wait_seconds": 0.2},
                          _remaining(20), _settlement_capture(page, snapshots), trace)
    assert [s["price_settlement"] for s in snapshots] == [{"status": "no_price_displayed", "selector": "#missing-quote"}]
    assert trace[-1]["price_settlement_counts"] == {"no_price_displayed": 1}
    candidates = _quote_candidates(snapshots)
    assert candidates[0].review_flags == ["selection_price_settlement_unconfirmed"]
    assert candidates[0].derived_values["price_settlement"] == "no_price_displayed"


def test_untracked_page_cannot_confirm_price_change(page):
    page.set_content("""<select id=o><option value=''>Choose</option><option value=a>A</option></select>
      <strong id=price>none</strong><script>document.querySelector('#o').onchange=()=>{
      document.querySelector('#price').textContent='KRW 100'}</script>""")
    snapshots, trace = [], []
    enumerate_options(page, {"action": "enumerate_options", "selectors": ["#o"], "price_selector": "#price",
                             "progress_wait_seconds": 0.05}, _remaining(), _settlement_capture(page, snapshots), trace)
    assert [s["price_settlement"]["status"] for s in snapshots] == ["untracked"]
    assert _quote_candidates(snapshots)[0].review_flags == ["selection_price_settlement_unconfirmed"]
