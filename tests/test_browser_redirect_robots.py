from __future__ import annotations

from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import threading

import pytest

from su_crawler.collectors import collect
from su_crawler.doctor import doctor
from su_crawler.models import Source


@contextmanager
def _site():
    visited: list[str] = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            visited.append(self.path)
            if self.path == "/robots.txt":
                body = b"User-agent: *\nDisallow: /forbidden\n"
                status = 200
            elif self.path in {"/redirect", "/redirect-allowed", "/redirect-hop", "/hop"}:
                self.send_response(302)
                self.send_header("Location", {
                    "/redirect": "/forbidden", "/redirect-allowed": "/catalog/product/",
                    "/redirect-hop": "/hop", "/hop": "/forbidden",
                }[self.path])
                self.end_headers()
                return
            elif self.path == "/catalog/product/":
                body = (b'<html><body><a id="relative" href="details">Details</a>'
                        b'<span id="location"></span><span id="resolved"></span><span id="session"></span><span id="name"></span>'
                        b'<script>document.querySelector("#location").textContent = window.location.href;'
                        b'document.querySelector("#resolved").textContent = '
                        b'document.querySelector("#relative").href;'
                        b'document.querySelector("#session").textContent = sessionStorage.getItem("sourceledger_test") || "missing";'
                        b'document.querySelector("#name").textContent = window.name || "missing";'
                        b'</script></body></html>')
                status = 200
            elif self.path == "/start-page":
                body = (b'<html><body><a id="go" href="/redirect-allowed">Open product</a>'
                        b'<script>sessionStorage.setItem("sourceledger_test", "kept");'
                        b'window.name = "kept-name";</script></body></html>')
                status = 200
            else:
                body = b"<html>Forbidden destination</html>"
                status = 200
            self.send_response(status)
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
        yield f"http://127.0.0.1:{server.server_port}", visited
    finally:
        server.shutdown()
        thread.join(timeout=2)
        server.server_close()


def test_browser_blocks_robots_denied_redirect_before_destination(monkeypatch, tmp_path):
    browser = next(item for item in doctor() if item["backend"] == "playwright")
    if browser["status"] != "available":
        pytest.skip(browser["reason"])
    monkeypatch.setenv("SOURCELEDGER_HEADLESS", "1")
    with _site() as (url, visited):
        source = Source(
            id="redirect", name="redirect", kind="web", location=url + "/redirect",
            product_ids=[], allowed_domains=["127.0.0.1"], internal=True,
            respect_robots=True, timeout_seconds=10,
        )
        result = collect(source, str(tmp_path), "playwright")
    assert result.status == "policy_denied", (result.message, result.trace)
    assert "/redirect" in visited
    assert "/forbidden" not in visited


def test_browser_follows_allowed_redirect(monkeypatch, tmp_path):
    browser = next(item for item in doctor() if item["backend"] == "playwright")
    if browser["status"] != "available":
        pytest.skip(browser["reason"])
    monkeypatch.setenv("SOURCELEDGER_HEADLESS", "1")
    with _site() as (url, visited):
        source = Source(
            id="allowed", name="allowed", kind="web", location=url + "/redirect-allowed",
            product_ids=[], allowed_domains=["127.0.0.1"], internal=True,
            respect_robots=True, timeout_seconds=10,
            recipe=[
                {"action": "assert_text", "selector": "#location", "text": url + "/catalog/product/"},
                {"action": "assert_text", "selector": "#resolved", "text": url + "/catalog/product/details"},
            ],
        )
        result = collect(source, str(tmp_path), "playwright")
    assert result.status == "fetched", (result.message, result.trace)
    assert result.final_url == url + "/catalog/product/"
    assert url.encode() + b"/catalog/product/" in result.content
    assert url.encode() + b"/catalog/product/details" in result.content
    assert "/catalog/product/" in visited


def test_browser_blocks_second_redirect_hop(monkeypatch, tmp_path):
    browser = next(item for item in doctor() if item["backend"] == "playwright")
    if browser["status"] != "available":
        pytest.skip(browser["reason"])
    monkeypatch.setenv("SOURCELEDGER_HEADLESS", "1")
    with _site() as (url, visited):
        source = Source(
            id="hop", name="hop", kind="web", location=url + "/redirect-hop",
            product_ids=[], allowed_domains=["127.0.0.1"], internal=True,
            respect_robots=True, timeout_seconds=10,
        )
        result = collect(source, str(tmp_path), "playwright")
    assert result.status == "policy_denied", (result.message, result.trace)
    assert "/hop" in visited
    assert "/forbidden" not in visited


def test_browser_recipe_click_keeps_redirect_destination(monkeypatch, tmp_path):
    browser = next(item for item in doctor() if item["backend"] == "playwright")
    if browser["status"] != "available":
        pytest.skip(browser["reason"])
    monkeypatch.setenv("SOURCELEDGER_HEADLESS", "1")
    with _site() as (url, visited):
        source = Source(
            id="click", name="click", kind="web", location=url + "/start-page",
            product_ids=[], allowed_domains=["127.0.0.1"], internal=True,
            respect_robots=True, timeout_seconds=12,
            recipe=[
                {"action": "click", "selector": "#go"},
                {"action": "assert_text", "selector": "#location", "text": url + "/catalog/product/"},
                {"action": "assert_text", "selector": "#session", "text": "kept"},
                {"action": "assert_text", "selector": "#name", "text": "kept-name"},
            ],
        )
        result = collect(source, str(tmp_path), "playwright")
    assert result.status == "fetched", (result.message, result.trace)
    assert result.final_url == url + "/catalog/product/"
    assert "/catalog/product/" in visited
