from __future__ import annotations

import threading
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from su_crawler.collectors import collect
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
