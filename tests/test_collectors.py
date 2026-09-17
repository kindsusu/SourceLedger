from __future__ import annotations

import threading
import socket
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from su_crawler.collectors import collect
from su_crawler.doctor import doctor
from su_crawler.models import Source


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/ok":
            self._send(200, b"<html><body>price 123</body></html>")
        elif self.path == "/auth":
            self._send(401, b"auth")
        elif self.path == "/blocked":
            self._send(403, b"blocked")
        elif self.path == "/limited":
            self._send(429, b"limited")
        elif self.path == "/login":
            self._send(200, b'<html><input type="password"></html>')
        elif self.path == "/shop-with-login-link":
            self._send(200, b'<html><nav><a href="/login">Log in</a></nav><main>price 321</main></html>')
        elif self.path == "/redirect":
            self.send_response(302)
            self.send_header("Location", "/ok")
            self.end_headers()
        elif self.path == "/robots.txt":
            self._send(200, b"User-agent: *\nDisallow: /forbidden\n")
        elif self.path == "/forbidden":
            self._send(200, b"should not fetch")
        else:
            self._send(404, b"missing")

    def _send(self, status: int, body: bytes):
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


def source(url: str, *, internal: bool, robots: bool = False) -> Source:
    return Source(
        id="test",
        name="test",
        kind="web",
        location=url,
        product_ids=[],
        allowed_domains=["127.0.0.1"],
        internal=internal,
        respect_robots=robots,
        timeout_seconds=2,
    )


def test_public_source_cannot_reach_private_address(tmp_path):
    with server() as url:
        result = collect(source(url + "/ok", internal=False), str(tmp_path), "http")
    assert result.status == "policy_denied"
    assert "정책" in result.message


def test_internal_source_rejects_link_local_and_special_addresses(monkeypatch, tmp_path):
    def address_info(host, port, **kwargs):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (host, port))]

    monkeypatch.setattr(socket, "getaddrinfo", address_info)
    for address in ("169.254.169.254", "0.0.0.0", "224.0.0.1"):
        result = collect(source(f"http://{address}/metadata", internal=True), str(tmp_path), "http")
        assert result.status == "policy_denied"


def test_internal_source_keeps_loopback_allowed(tmp_path):
    with server() as url:
        assert collect(source(url + "/ok", internal=True), str(tmp_path), "http").status == "fetched"


def test_internal_source_is_explicit_and_statuses_are_classified(tmp_path):
    with server() as url:
        assert collect(source(url + "/ok", internal=True), str(tmp_path), "http").status == "fetched"
        assert collect(source(url + "/auth", internal=True), str(tmp_path), "http").status == "needs_auth"
        assert collect(source(url + "/blocked", internal=True), str(tmp_path), "http").status == "blocked"
        assert collect(source(url + "/limited", internal=True), str(tmp_path), "http").status == "blocked"
        assert collect(source(url + "/login", internal=True), str(tmp_path), "http").status == "needs_auth"
        assert collect(source(url + "/shop-with-login-link", internal=True), str(tmp_path), "http").status == "fetched"


def test_redirect_is_checked_and_traced(tmp_path):
    with server() as url:
        result = collect(source(url + "/redirect", internal=True), str(tmp_path), "http")
    assert result.status == "fetched"
    assert result.final_url.endswith("/ok")
    assert [event["status"] for event in result.trace] == [302, 200]


def test_robots_policy(tmp_path):
    with server() as url:
        result = collect(source(url + "/forbidden", internal=True, robots=True), str(tmp_path), "http")
    assert result.status == "policy_denied"
    assert "robots.txt" in result.message


def test_robots_fetch_failure_is_not_reported_as_explicit_denial(monkeypatch, tmp_path):
    monkeypatch.setattr("su_crawler.collectors._robots_decision", lambda *args: ("unavailable", "robots.txt 확인 실패"))
    with server() as url:
        result = collect(source(url + "/ok", internal=True, robots=True), str(tmp_path), "http")
    assert result.status == "failed"
    assert result.message == "robots.txt 확인 실패"


def test_file_stays_within_base_directory(tmp_path):
    good = tmp_path / "prices.csv"
    good.write_bytes(b"sku,price\na,10\n")
    allowed = Source(id="f", name="file", kind="file", location="prices.csv", product_ids=[])
    result = collect(allowed, str(tmp_path), "file")
    assert result.status == "fetched"
    assert result.media_type == "text/csv"
    assert result.content == good.read_bytes()

    outside = Source(id="f2", name="file", kind="file", location="../outside.csv", product_ids=[])
    assert collect(outside, str(tmp_path), "file").status == "policy_denied"


def test_explicit_file_root_allows_only_that_tree(tmp_path):
    root = tmp_path / "internal"
    root.mkdir()
    (root / "prices.csv").write_bytes(b"sku,price\na,10\n")
    allowed = Source(id="f", name="file", kind="file", location="prices.csv", product_ids=[])
    allowed.file_root = "internal"
    assert collect(allowed, str(tmp_path), "file").status == "fetched"
    allowed.location = "../outside.csv"
    assert collect(allowed, str(tmp_path), "file").status == "policy_denied"


def test_doctor_is_explicit_about_optional_and_unsupported_tools():
    report = {item["backend"]: item for item in doctor()}
    assert report["file"]["status"] == "available"
    assert report["http"]["status"] == "available"
    assert report["ego_lite"]["status"] == "unsupported"
    assert report["ego_lite"]["connected"] is False
    assert "Windows" in report["ego_lite"]["reason"]
    assert report["agent_reach"]["connected"] is False
