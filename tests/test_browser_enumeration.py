"""Offline tests for bounded browser option and page enumeration."""
from __future__ import annotations

import time
import threading
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from su_crawler.browser_enumeration import enumerate_buttons, enumerate_options, paginate, scroll_list, validate_enumeration_step
from su_crawler.browser_runtime import select_browser_runtime
from su_crawler.collectors import _evidence_html_snapshot, collect
from su_crawler.extraction import extract
from su_crawler.models import FetchResult, Source


@pytest.fixture
def page():
    pytest.importorskip("playwright")
    from playwright.sync_api import sync_playwright

    with sync_playwright() as pw:
        runtime = select_browser_runtime(pw.chromium)
        if runtime is None:
            pytest.skip("Chromium unavailable")
        try:
            browser = pw.chromium.launch(headless=True, timeout=5000, **runtime.launch_options())
        except Exception as exc:
            pytest.skip(f"Chromium unavailable: {type(exc).__name__}")
        try:
            yield browser.new_page()
        finally:
            browser.close()


def _remaining(seconds=8):
    deadline = time.monotonic() + seconds
    return lambda: (deadline - time.monotonic()) * 1000


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
             _remaining(), lambda selected: snapshots.append((_evidence_html_snapshot(page), selected)), trace)
    assert [b"A" in entry[0] for entry in snapshots] == [True, False]
    assert [entry[1]["page"] for entry in snapshots] == ["1", "2"]
    assert (trace[-1]["status"], trace[-1]["reason"]) == ("complete", "next_disabled")


def test_scroll_without_sentinel_is_partial_and_single_snapshot(page):
    page.set_content("""<div id=list><article class=item>A</article></div>""")
    snapshots, trace = [], []
    scroll_list(page, {"action": "scroll_list", "selector": "#list", "item_selector": ".item",
                       "progress_wait_seconds": 0.01}, _remaining(), snapshots.append, trace)
    assert len(snapshots) == 1
    assert (trace[-1]["status"], trace[-1]["reason"]) == ("partial", "no_growth_unconfirmed")


def test_scroll_explicit_end_is_complete(page):
    page.set_content("""<div id=list><article class=item>A</article><div class=end>End</div></div>""")
    snapshots, trace = [], []
    scroll_list(page, {"action": "scroll_list", "selector": "#list", "item_selector": ".item",
                       "end_selector": ".end"}, _remaining(), snapshots.append, trace)
    assert (len(snapshots), trace[-1]["status"]) == (1, "complete")


def test_scroll_replacement_preserves_prior_evidence(page):
    page.set_content("""<div id=list><article class=item>A</article><article class=item style='height:900px'>B</article></div>
      <script>new IntersectionObserver(entries=>{if(entries.some(e=>e.isIntersecting))
        document.querySelector('#list').innerHTML='<article class=item>C</article>'})
        .observe(document.querySelectorAll('.item')[1])</script>""")
    snapshots, trace = [], []
    scroll_list(page, {"action": "scroll_list", "selector": "#list", "item_selector": ".item",
                       "progress_wait_seconds": 0.05}, _remaining(),
                lambda selected: snapshots.append(_evidence_html_snapshot(page)), trace)
    assert trace[-1]["status"] == "partial"
    assert snapshots and b">A</article>" in snapshots[0]


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
])
def test_invalid_enumeration_budgets_and_selectors(step):
    with pytest.raises(ValueError):
        validate_enumeration_step(step)
