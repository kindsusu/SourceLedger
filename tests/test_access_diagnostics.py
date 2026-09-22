from __future__ import annotations

import socket
import ipaddress
import threading
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from su_crawler.collectors import _check_url, _is_forbidden_even_internal, _is_public, collect
from su_crawler.doctor import doctor
from su_crawler.models import Source


class Handler(BaseHTTPRequestHandler):
    robots_status = 404

    def do_GET(self):
        if self.path == "/robots.txt":
            self._send(self.robots_status, b"robots")
        elif self.path == "/etag":
            if self.headers.get("If-None-Match") == '"v1"':
                self.send_response(304)
                self.send_header("ETag", '"v1"')
                self.end_headers()
            else:
                self._send(200, b"fresh", etag='"v1"', modified="Wed, 21 Oct 2015 07:28:00 GMT")
        elif self.path == "/limited":
            self._send(429, b"slow down")
        elif self.path == "/controls":
            self._send(200, b'''<html><body><label for="size">Size</label><select id="size"><option>Small</option><option>Large</option></select><label for="in-stock">In stock</label><input id="in-stock" type="checkbox"><input type="email" value="contact@example.test"><textarea>private note</textarea></body></html>''')
        else:
            self._send(200, b"ok")

    def _send(self, status: int, body: bytes, *, etag: str | None = None, modified: str | None = None):
        self.send_response(status)
        self.send_header("Content-Type", "text/html")
        self.send_header("Content-Length", str(len(body)))
        if etag:
            self.send_header("ETag", etag)
        if modified:
            self.send_header("Last-Modified", modified)
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


def source(url: str, *, robots: bool = False) -> Source:
    return Source(id="access", name="access", kind="web", location=url, product_ids=[], allowed_domains=["127.0.0.1"], internal=True, respect_robots=robots)


def test_policy_errors_have_safe_stable_codes(monkeypatch, tmp_path):
    denied = collect(source("https://user:very-secret@example.invalid/private?token=never-log"), str(tmp_path), "http")
    assert denied.status == "policy_denied"
    assert "credentials_in_url" in denied.message
    assert "very-secret" not in denied.message and "never-log" not in denied.message
    assert denied.trace[-1]["code"] == "credentials_in_url"

    def no_dns(*args, **kwargs):
        raise socket.gaierror("secret resolver context")

    monkeypatch.setattr(socket, "getaddrinfo", no_dns)
    unresolved = collect(source("https://127.0.0.1/"), str(tmp_path), "http")
    assert unresolved.trace[-1]["code"] == "dns_resolution_failed"
    assert "secret resolver context" not in unresolved.message


def test_public_nat64_and_ipv4_mapped_dns_answers_are_classified_by_embedded_ipv4(monkeypatch):
    source_value = Source(
        id="nat64", name="nat64", kind="web", location="https://vendor.example/item",
        product_ids=[], allowed_domains=["vendor.example"], internal=False, respect_robots=False,
    )

    def public_answers(host, port, **kwargs):
        return [
            (socket.AF_INET6, socket.SOCK_STREAM, 6, "", ("64:ff9b::deed:4eeb", port, 0, 0)),
            (socket.AF_INET6, socket.SOCK_STREAM, 6, "", ("::ffff:222.237.78.235", port, 0, 0)),
        ]

    monkeypatch.setattr(socket, "getaddrinfo", public_answers)
    _check_url(source_value, source_value.location)
    for value in ("64:ff9b::deed:4eeb", "::ffff:222.237.78.235"):
        address = ipaddress.ip_address(value)
        assert _is_public(address)
        assert not _is_forbidden_even_internal(address)


def test_nat64_and_ipv4_mapped_private_or_special_embedded_ipv4_are_denied(monkeypatch):
    source_value = Source(
        id="nat64", name="nat64", kind="web", location="https://vendor.example/item",
        product_ids=[], allowed_domains=["vendor.example"], internal=True, respect_robots=False,
    )
    embedded = ["10.0.0.1", "127.0.0.1", "169.254.1.1", "224.0.0.1", "240.0.0.1"]
    for ipv4 in embedded:
        address = ipaddress.ip_address(ipv4)
        for wrapped in (ipaddress.IPv6Address(int(ipaddress.ip_address("64:ff9b::")) + int(address)), ipaddress.ip_address(f"::ffff:{ipv4}")):
            assert _is_forbidden_even_internal(wrapped)

    def private_answer(host, port, **kwargs):
        return [(socket.AF_INET6, socket.SOCK_STREAM, 6, "", ("64:ff9b::a00:1", port, 0, 0))]

    monkeypatch.setattr(socket, "getaddrinfo", private_answer)
    with pytest.raises(ValueError, match="Special-use"):
        _check_url(source_value, source_value.location)


def test_robots_only_absence_allows_and_auth_is_unavailable(tmp_path):
    with server() as url:
        allowed = collect(source(url + "/anything", robots=True), str(tmp_path), "http")
        assert allowed.status == "fetched"
        Handler.robots_status = 401
        unavailable = collect(source(url + "/anything", robots=True), str(tmp_path), "http")
        assert unavailable.status == "failed"
        assert unavailable.message == "robots.txt access is unavailable"
    Handler.robots_status = 404


def test_conditional_http_revalidation_and_block_codes(tmp_path):
    with server() as url:
        first = collect(source(url + "/etag"), str(tmp_path), "http")
        assert first.status == "fetched"
        assert first.http_metadata == {"etag": '"v1"', "last_modified": "Wed, 21 Oct 2015 07:28:00 GMT"}
        unchanged = collect(source(url + "/etag"), str(tmp_path), "http", validators={"etag": '"v1"', "last_modified": "bad\r\nheader"})
        assert unchanged.status == "not_modified"
        assert unchanged.content == b""
        assert unchanged.http_metadata == {"etag": '"v1"'}
        limited = collect(source(url + "/limited"), str(tmp_path), "http")
        assert limited.status == "blocked"
        assert limited.trace[-1]["code"] == "rate_limited"


def test_browser_evidence_preserves_live_selection_without_free_text(tmp_path):
    browser = next(item for item in doctor() if item["backend"] == "playwright")
    if browser["status"] != "available":
        import pytest

        pytest.skip(browser["reason"])
    with server() as url:
        result = collect(
            Source(
                id="controls", name="controls", kind="web", location=url + "/controls", product_ids=[],
                allowed_domains=["127.0.0.1"], internal=True, respect_robots=False, timeout_seconds=5,
                recipe=[{"action": "select", "selector": "#size", "value": "Large"}, {"action": "click", "selector": "#in-stock"}],
            ),
            str(tmp_path), "playwright",
        )
    assert result.status == "fetched", result.message
    assert b"<option selected=\"\">Large</option>" in result.content
    assert b"checked" in result.content
    assert b"contact@example.test" not in result.content and b"private note" not in result.content
    state = next(event for event in result.trace if event["event"] == "selected_control_states")
    assert {control["type"] for control in state["controls"]} == {"select-one", "checkbox"}
    assert all("value" not in control for control in state["controls"])
