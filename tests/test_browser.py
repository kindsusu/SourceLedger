from __future__ import annotations

import threading
import time
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from su_crawler.collectors import _close_browser_resources, _evidence_html_snapshot, collect
from su_crawler.browser_runtime import select_browser_runtime
from su_crawler.doctor import doctor
from su_crawler.models import Source


PAGE = b"""<!doctype html><html><body>
<select id="size"><option value="small">Small</option><option value="large">Large</option></select>
<button id="apply" onclick="document.querySelector('#price').textContent = document.querySelector('#size').value === 'large' ? '200' : '100'">Apply</button>
<div id="price">100</div>
</body></html>"""


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/robots.txt":
            body = b"User-agent: *\nDisallow: /robots-blocked\n"
            status = 200
        elif self.path == "/blocked":
            body = b"blocked"
            status = 403
        elif self.path == "/delayed":
            body = b"""<html><body><select id='term'><option>12 months</option><option>24 months</option></select>
            <div id='price'>100</div><div aria-busy='true' id='loader'>Calculating</div>
            <script>setTimeout(() => {document.querySelector('#term').selectedIndex=1;
            document.querySelector('#price').textContent='125';
            document.querySelector('#loader').remove()}, 900)</script></body></html>"""
            status = 200
        elif self.path == "/beacon":
            body = b"""<html><body><div id='price'>100</div>
            <script>fetch('/hang', {keepalive: true, method: 'GET'}).catch(() => {});</script></body></html>"""
            status = 200
        elif self.path == "/promo":
            body = b"""<html><body><div id='price'>100</div>
            <div id='promo' style='position:fixed;inset:0;background:#fff'><button id='close'
              onclick="document.getElementById('promo').remove()">close</button></div>
            <div id='shield' style='position:fixed;inset:0;z-index:9'></div>
            <script>setTimeout(() => document.getElementById('shield').remove(), 1500)</script></body></html>"""
            status = 200
        elif self.path == "/hang":
            time.sleep(12)
            body = b"late"
            status = 200
        else:
            body = PAGE
            status = 200
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
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


def test_visible_browser_applies_recipe_and_saves_evidence(tmp_path):
    browser = next(item for item in doctor() if item["backend"] == "playwright")
    if browser["status"] != "available":
        pytest.skip(browser["reason"])
    with server() as url:
        source = Source(
            id="browser-test",
            name="browser test",
            kind="web",
            location=url,
            product_ids=[],
            allowed_domains=["127.0.0.1"],
            internal=True,
            respect_robots=False,
            timeout_seconds=5,
            recipe=[
                {"action": "select", "selector": "#size", "value": "large"},
                {"action": "click", "selector": "#apply"},
                {"action": "assert_text", "selector": "#price", "text": "200"},
            ],
        )
        result = collect(source, str(tmp_path), "playwright")

    assert result.status == "fetched", result.message
    assert b'<div id="price">200</div>' in result.content
    assert result.screenshot and result.screenshot.startswith(b"\x89PNG")
    assert not (tmp_path / ".su_crawler").exists()
    assert [step["action"] for step in result.trace if step["event"] == "recipe_step"] == ["select", "click", "assert_text"]
    assert all("value" not in step and "text" not in step for step in result.trace)


def test_browser_classifies_http_block_and_obeys_robots(tmp_path):
    browser = next(item for item in doctor() if item["backend"] == "playwright")
    if browser["status"] != "available":
        pytest.skip(browser["reason"])
    with server() as url:
        blocked = Source(
            id="browser-blocked",
            name="browser blocked",
            kind="web",
            location=url + "/blocked",
            product_ids=[],
            allowed_domains=["127.0.0.1"],
            internal=True,
            respect_robots=False,
            timeout_seconds=5,
        )
        assert collect(blocked, str(tmp_path), "playwright").status == "blocked"

        blocked.location = url + "/robots-blocked"
        blocked.respect_robots = True
        result = collect(blocked, str(tmp_path), "playwright")
        assert result.status == "policy_denied"
        assert "robots.txt" in result.message


def test_browser_snapshot_preserves_choice_identity_and_marks_computed_hidden_without_input_secrets():
    browser_status = next(item for item in doctor() if item["backend"] == "playwright")
    if browser_status["status"] != "available":
        pytest.skip(browser_status["reason"])
    from playwright.sync_api import sync_playwright

    with sync_playwright() as pw:
        runtime = select_browser_runtime(pw.chromium)
        assert runtime is not None
        browser = pw.chromium.launch(headless=True, **runtime.launch_options())
        page = browser.new_page()
        page.set_content("""
          <style>.css-hidden { display: none }</style>
          <select id="term"><option value="36">36 months</option><option value="48">48 months</option></select>
          <input id="deposit" type="radio" name="deposit" value="deposit-10" checked>
          <input id="extra" type="checkbox" value="winter-pack" checked>
          <input id="free" type="text" value="customer-entered secret">
          <input id="password" type="password" value="password secret">
          <input id="credential" type="hidden" value="hidden credential">
          <div id="quote" class="css-hidden">999,999 won</div>
        """)
        page.select_option("#term", "48")
        snapshot = _evidence_html_snapshot(page).decode("utf-8")
        browser.close()

    assert '<option value="48" selected="">48 months</option>' in snapshot
    assert 'id="deposit" type="radio" name="deposit" value="deposit-10" checked=""' in snapshot
    assert 'id="extra" type="checkbox" value="winter-pack" checked=""' in snapshot
    assert "customer-entered secret" not in snapshot
    assert "password secret" not in snapshot
    assert "hidden credential" not in snapshot
    assert 'id="quote" class="css-hidden" hidden="" data-sourceledger-computed-hidden="true"' in snapshot


def test_browser_snapshot_marks_computed_line_through_prices():
    from playwright.sync_api import sync_playwright

    with sync_playwright() as pw:
        runtime = select_browser_runtime(pw.chromium)
        if runtime is None:
            pytest.skip("Browser unavailable")
        browser = pw.chromium.launch(headless=True, **runtime.launch_options())
        try:
            page = browser.new_page()
            page.set_content("""<style>.was { text-decoration: line-through }</style>
              <span id="old" class="was">500,000</span><del id="del">450,000</del>
              <span id="now">400,000</span>""")
            snapshot = _evidence_html_snapshot(page).decode("utf-8")
        finally:
            browser.close()
    assert 'id="old" class="was" data-sourceledger-computed-line-through="true"' in snapshot
    assert 'id="del" data-sourceledger-computed-line-through="true"' in snapshot
    assert '<span id="now">400,000</span>' in snapshot


def test_browser_cleanup_unroutes_with_ignored_late_errors_before_closing():
    events = []

    class Context:
        pages = []

        def unroute_all(self, *, behavior):
            events.append(("unroute_all", behavior))

        def close(self):
            events.append(("context.close", None))

    class Browser:
        def close(self):
            events.append(("browser.close", None))

    _close_browser_resources(Context(), Browser())
    assert events == [("unroute_all", "ignoreErrors"), ("context.close", None), ("browser.close", None)]


def test_generic_collector_waits_for_dynamic_price_and_captures_matching_controls(tmp_path):
    if next(item for item in doctor() if item['backend'] == 'playwright')['status'] != 'available':
        pytest.skip('Browser unavailable')
    with server() as url:
        source = Source(id='dynamic', name='dynamic', kind='web', location=url + '/delayed',
                        product_ids=[], allowed_domains=['127.0.0.1'], internal=True,
                        respect_robots=False, timeout_seconds=10, row_selector='#price')
        result = collect(source, str(tmp_path), 'playwright')
    assert result.status == 'fetched', result.message
    assert b'id="price">125</div>' in result.content
    assert b'<option selected="">24 months</option>' in result.content
    controls = next(row['controls'] for row in result.trace if row['event'] == 'selected_control_states')
    assert controls[0]['selected_label'] == '24 months'
    assert any(row['event'] == 'observation_settlement' and row['status'] == 'settled' for row in result.trace)


def test_keepalive_beacon_does_not_block_observation_settlement(tmp_path):
    if next(item for item in doctor() if item['backend'] == 'playwright')['status'] != 'available':
        pytest.skip('Browser unavailable')
    with server() as url:
        source = Source(id='beacon', name='beacon', kind='web', location=url + '/beacon',
                        product_ids=[], allowed_domains=['127.0.0.1'], internal=True,
                        respect_robots=False, timeout_seconds=10)
        result = collect(source, str(tmp_path), 'playwright')
    assert result.status == 'fetched', result.message
    settlement = next(row for row in result.trace if row['event'] == 'observation_settlement')
    assert settlement['status'] == 'settled' and settlement['pending'] == 0


def test_optional_click_that_is_briefly_unclickable_is_retried(tmp_path):
    if next(item for item in doctor() if item['backend'] == 'playwright')['status'] != 'available':
        pytest.skip('Browser unavailable')
    with server() as url:
        source = Source(id='promo', name='promo', kind='web', location=url + '/promo',
                        product_ids=[], allowed_domains=['127.0.0.1'], internal=True,
                        respect_robots=False, timeout_seconds=20, row_selector='#price',
                        recipe=[{'action': 'click', 'selector': '#close', 'if_visible': True,
                                 'timeout_seconds': 1}])
        result = collect(source, str(tmp_path), 'playwright')
    assert result.status == 'fetched', result.message
    assert any(row['event'] == 'recipe_step_retried' and row['still_visible'] is False for row in result.trace)
    assert b'id="promo"' not in result.content


def test_atomic_snapshot_rejects_changed_observation():
    from playwright.sync_api import sync_playwright
    from su_crawler.collectors import _OBSERVATION_PROBE_JS, _evidence_snapshot
    with sync_playwright() as pw:
        runtime = select_browser_runtime(pw.chromium)
        if runtime is None:
            pytest.skip('Browser unavailable')
        browser = pw.chromium.launch(headless=True, **runtime.launch_options())
        try:
            page = browser.new_page()
            page.set_content('<div id="price">100</div>')
            before = page.evaluate(_OBSERVATION_PROBE_JS, '#price')
            page.locator('#price').evaluate('(node) => node.textContent = "125"')
            assert _evidence_snapshot(page, observation=before, selector='#price') == {'observation_changed': True}
            current = page.evaluate(_OBSERVATION_PROBE_JS, '#price')
            snap = _evidence_snapshot(page, observation=current, selector='#price')
            assert snap['probe'] == current and '125</div>' in snap['html']
        finally:
            browser.close()
