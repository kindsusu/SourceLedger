"""Offline browser checks for bounded public list expansion."""
from __future__ import annotations

import time

import pytest

from su_crawler.browser_runtime import select_browser_runtime
from su_crawler.collectors import PolicyError, _expand_list, _expand_options


def _step(**overrides):
    return {"action": "expand_list", "selector": "#popular", "item_selector": ".card",
            "button_selector": ".more", "initial_wait_seconds": 0.5,
            "progress_wait_seconds": 0.5, **overrides}


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
            pytest.skip(f"Chromium launch unavailable: {type(exc).__name__}")
        try:
            yield browser.new_page()
        finally:
            browser.close()


def _run(page, step=None, *, max_seconds=10):
    trace = []
    deadline = time.monotonic() + max_seconds
    _expand_list(page, step or _step(), lambda: (deadline - time.monotonic()) * 1000, trace)
    return trace[-1]


@pytest.mark.parametrize(("total", "expected_clicks"), [(18, 4), (97, 24)])
def test_expands_scoped_button_until_absent(page, total, expected_clicks):
    page.set_content(f'''<button id="unrelated">More</button><div id="popular" data-total="{total}"><div class="cards"></div>
      <button class="more" onclick="add()">More</button></div>
      <script>let n=0;const total=Number(document.querySelector('#popular').dataset.total);function add(){{const cards=document.querySelector('#popular .cards');
      for(let i=0;i<4 && n<total;i++) {{let e=document.createElement('div');e.className='card';e.textContent='Car '+(++n);cards.append(e)}}
      if(n===total)document.querySelector('#popular .more').remove()}}setTimeout(add,50)</script>''')
    # Large catalogs use the production page budget. This checks completion,
    # not machine speed; separate tests assert deadline and click-budget exits.
    result = _run(page, _step(progress_wait_seconds=5), max_seconds=120)
    assert (result["status"], result["reason"], result["observed_count"], result["click_count"]) == (
        "complete", "button_absent", total, expected_clicks)


def test_missing_cards_are_unknown_unless_explicitly_empty(page):
    page.set_content('<div id="popular"><button class="more">More</button></div>')
    assert _run(page)["status"] == "unknown"
    page.set_content('<div id="popular"><span class="empty">No cars</span></div>')
    result = _run(page, _step(empty_selector=".empty"))
    assert (result["status"], result["reason"]) == ("complete", "explicit_empty")


def test_replacement_and_no_progress_are_partial(page):
    page.set_content('''<div id="popular"><div class="cards"><div class="card">A</div></div>
      <button class="more" onclick="document.querySelector('.cards').innerHTML='<div class=card>B</div>'">More</button></div>''')
    result = _run(page)
    assert result["reason"] == "list_replaced"
    assert result["observed_count"] == 2
    assert len(result["observed_cards"]) == 2
    page.set_content('<div id="popular"><div class="card">A</div><button class="more">More</button></div>')
    assert _run(page)["reason"] == "no_progress"


def test_delayed_append_and_all_filter(page):
    page.set_content('''<div id="popular"><select class="category"><option value="other" selected>Other</option>
      <option value="all">All</option></select><div class="card">A</div>
      <button class="more" onclick="setTimeout(() => {let e=document.createElement('div');
      e.className='card';e.textContent='B';document.querySelector('#popular').append(e);
      document.querySelector('#popular .more').remove()},100)">More</button></div>''')
    result = _run(page, _step(all_selector=".category", all_value="all"))
    assert (result["status"], result["observed_count"], result["click_count"]) == ("complete", 2, 1)
    assert page.locator(".category").input_value() == "all"


def test_temporary_button_absence_is_not_completion(page):
    page.set_content('''<div id="popular"><div class="card">A</div>
      <button class="more" onclick="this.style.display='none';setTimeout(() => {
      this.style.display='';this.before(Object.assign(document.createElement('div'),
      {className:'card',textContent:'B'}));this.remove()},150)">More</button></div>''')
    result = _run(page, _step(section="promotion"))
    assert (result["section"], result["status"], result["observed_count"]) == ("promotion", "complete", 2)


def test_button_reappears_after_loading_pause_and_expansion_continues(page):
    page.set_content('''<div id="popular"><div class="cards"></div><button class="more" onclick="add()">More</button></div>
      <script>let n=0, clicks=0;function add(){
        const box=document.querySelector('#popular .cards');
        for(let i=0;i<4;i++){const e=document.createElement('div');e.className='card';e.textContent='Car '+(++n);box.append(e)}
        if(++clicks===6){const button=document.querySelector('.more');button.style.display='none';
          setTimeout(()=>button.style.display='',1100)}
        if(clicks===7)document.querySelector('.more').remove()
      }setTimeout(add,20)</script>''')
    # The old 0.4-second absence check stopped after five clicks and missed the rest.
    result = _run(page, _step(progress_wait_seconds=2), max_seconds=20)
    assert (result["status"], result["reason"], result["observed_count"], result["click_count"]) == (
        "complete", "button_absent", 28, 6)


def test_button_reappears_after_no_progress_window(page):
    page.set_content('''<div id="popular"><div class="card">A</div><button class="more"
      onclick="if(!window.clicked){window.clicked=true;this.style.display='none';
      setTimeout(()=>this.style.display='',800)}else{this.before(Object.assign(document.createElement('div'),
      {className:'card',textContent:'B'}));this.remove()}">More</button></div>''')
    # No card is added by the first click. The button reappears only while the
    # terminal check is polling, after the shorter progress window has expired.
    result = _run(page, _step(progress_wait_seconds=0.5), max_seconds=8)
    assert (result["status"], result["reason"], result["observed_count"], result["click_count"]) == (
        "complete", "button_absent", 2, 2)


def test_navigation_during_terminal_check_is_partial(page):
    page.set_content('''<div id="popular"><div class="card">A</div><button class="more"
      onclick="this.remove();setTimeout(()=>location.hash='next-page',700)">More</button></div>''')
    result = _run(page, _step(progress_wait_seconds=0.5), max_seconds=8)
    assert (result["status"], result["reason"]) == ("partial", "unexpected_navigation")


def test_button_absence_near_deadline_is_partial(page):
    page.set_content('<div id="popular"><div class="card">A</div></div>')
    trace = []
    _expand_list(page, _step(), lambda: 100, trace)
    assert (trace[-1]["status"], trace[-1]["reason"], trace[-1]["observed_count"]) == (
        "partial", "completion_unconfirmed", 1)


def test_loading_button_absence_at_deadline_is_unconfirmed(page):
    page.set_content('''<div id="popular"><div class="card">A</div><button class="more"
      onclick="this.style.display='none';setTimeout(()=>this.style.display='',3000)">More</button></div>''')
    result = _run(page, _step(progress_wait_seconds=2), max_seconds=2)
    assert (result["status"], result["reason"], result["observed_count"]) == (
        "partial", "completion_unconfirmed", 1)


def test_budget_and_ambiguous_container(page):
    page.set_content('''<div id="popular"><div class="card">A</div><button class="more"
      onclick="let e=document.createElement('div');e.className='card';e.textContent=Math.random();this.before(e)">More</button></div>''')
    result = _run(page, _step(max_clicks=2))
    assert (result["status"], result["reason"], result["click_count"]) == ("partial", "click_budget", 2)
    page.set_content('<div id="popular"></div><div id="popular"></div>')
    assert _run(page)["reason"] == "ambiguous_container"


def test_invalid_budgets_are_rejected():
    for options in ({"max_clicks": 101}, {"max_items": 1001}, {"max_clicks": True},
                    {"item_selector": ""}, {"all_selector": "select"}, {"section": "Bad Section"},
                    {"trigger": "submit"}, {"trigger": []}):
        with pytest.raises(PolicyError):
            _expand_options(_step(**options))


def test_scroll_sentinel_expands_without_clicking(page):
    page.set_content('''<div id="popular"><div class="card" style="height:1800px">A</div>
      <p class="more" onclick="document.body.dataset.clicked='yes'">Load more</p></div>
      <script>new IntersectionObserver((entries, observer) => {
        if (entries.some(e => e.isIntersecting)) {
          const sentinel = document.querySelector('.more');
          sentinel.before(Object.assign(document.createElement('div'), {className:'card',textContent:'B'}));
          observer.disconnect(); sentinel.remove();
        }
      }).observe(document.querySelector('.more'));</script>''')
    result = _run(page, _step(trigger="scroll", progress_wait_seconds=2))
    assert (result["status"], result["observed_count"]) == ("complete", 2)
    assert page.locator('body').get_attribute('data-clicked') is None


_NETWORK_PAGE = b"""<!doctype html><html><body><div id="popular"><div class="cards"><div class="card">Car 0</div></div>
<button class="more" onclick="more(this)">More</button></div>
<script>let n = 0, clicks = 0;
function more(button) {
  clicks++;
  const slow = clicks === Number(new URLSearchParams(location.search).get('slow_click') || 0);
  if (slow) button.style.display = 'none';
  fetch('/more?ms=' + (slow ? 5000 : 150)).then(r => r.text()).then(() => {
    const box = document.querySelector('.cards');
    for (let i = 0; i < 4; i++) { const e = document.createElement('div'); e.className = 'card';
      e.textContent = 'Car ' + (++n); box.append(e); }
    button.style.display = '';
    if (clicks === 3) button.remove();
  });
}</script></body></html>"""


@pytest.fixture
def network_list_url():
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from urllib.parse import parse_qs, urlsplit

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            parts = urlsplit(self.path)
            if parts.path == "/more":
                time.sleep(int(parse_qs(parts.query)["ms"][0]) / 1000)
                body = b"ok"
            else:
                body = _NETWORK_PAGE
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


def _tracked(page, url):
    from su_crawler.browser_settlement import install_tracker_sync
    install_tracker_sync(page)
    page.goto(url)


def test_network_driven_list_confirms_end_after_quiet_floor(page, network_list_url):
    _tracked(page, network_list_url)
    started = time.monotonic()
    result = _run(page, _step(progress_wait_seconds=8), max_seconds=60)
    elapsed = time.monotonic() - started
    assert (result["status"], result["reason"], result["observed_count"], result["click_count"]) == (
        "complete", "button_absent", 13, 3)
    assert result["network_driven"] is True
    # The full 8-second window is not needed once the tracked network has
    # stayed quiet; the old fixed window alone took at least 8 seconds.
    assert elapsed < 7.5


def test_pending_request_with_hidden_button_is_not_list_end(page, network_list_url):
    # The second load hides the button for 5 seconds while its request is
    # pending; a 2-second stable window alone would have declared the end.
    _tracked(page, network_list_url + "?slow_click=2")
    result = _run(page, _step(progress_wait_seconds=2), max_seconds=60)
    assert (result["status"], result["reason"], result["observed_count"], result["click_count"]) == (
        "complete", "button_absent", 13, 3)


def test_final_trace_records_card_digest(page):
    page.set_content('<div id="popular"><div class="card">A</div></div>')
    result = _run(page, _step(progress_wait_seconds=0.5))
    assert result["status"] == "complete"
    assert len(result["observed_cards_sha256"]) == 64
