"""Job-scoped HTTP connection and robots.txt reuse, against a local server only."""
from __future__ import annotations

import threading
import time
from collections import Counter
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from su_crawler.collectors import (RobotsPolicyCache, collect, collection_job_scope, http_client_scope,
                                   robots_policy_scope)
from su_crawler.doctor import doctor
from su_crawler.models import Source


def _server(robots_status: int):
    requests = Counter()
    ports = set()

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_GET(self):
            requests[self.path] += 1
            ports.add(self.client_address[1])
            if self.path == "/robots.txt":
                status, body = robots_status, b"User-agent: *\nAllow: /\n"
            else:
                status, body = 200, b"<html><body><div id='price'>100</div></body></html>"
            self.send_response(status)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    @contextmanager
    def run():
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        httpd.daemon_threads = True
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        try:
            yield f"http://127.0.0.1:{httpd.server_port}"
        finally:
            httpd.shutdown()
            thread.join(timeout=2)

    return run, requests, ports


def _source(url, path, timeout=5):
    return Source(id="reuse" + path.replace("/", "-"), name="reuse", kind="web", location=url + path,
                  product_ids=[], allowed_domains=["127.0.0.1"], internal=True,
                  respect_robots=True, timeout_seconds=timeout)


@pytest.mark.parametrize("status,message", [(403, "robots.txt access is unavailable"),
                                            (503, "robots.txt server error")])
def test_unavailable_robots_answer_is_cached_within_the_job(tmp_path, status, message):
    run, requests, _ = _server(status)
    with run() as url, robots_policy_scope():
        results = [collect(_source(url, f"/page-{i}"), str(tmp_path), "http") for i in range(3)]
    assert [(r.status, r.message) for r in results] == [("failed", message)] * 3
    assert requests["/robots.txt"] == 1
    assert not any(path.startswith("/page") for path in requests)


def test_transient_robots_failure_expires_quickly(tmp_path):
    run, requests, _ = _server(503)
    cache = RobotsPolicyCache(failure_ttl_seconds=0.05)
    with run() as url, robots_policy_scope(cache):
        collect(_source(url, "/a"), str(tmp_path), "http")
        time.sleep(0.1)
        collect(_source(url, "/b"), str(tmp_path), "http")
    assert requests["/robots.txt"] == 2


def test_job_scope_reuses_one_connection_for_robots_and_pages(tmp_path):
    run, requests, ports = _server(200)
    with run() as url:
        with collection_job_scope():
            results = [collect(_source(url, f"/page-{i}"), str(tmp_path), "http") for i in range(3)]
        pooled_ports = set(ports)
        ports.clear()
        for i in range(2):
            collect(_source(url, f"/fresh-{i}"), str(tmp_path), "http")
    assert all(result.status == "fetched" for result in results)
    assert requests["/robots.txt"] == 3  # one for the job scope, one per unscoped call
    assert len(pooled_ports) == 1
    assert len(ports) >= 2  # without a job scope each call opens its own client


def test_pooled_client_still_honors_each_source_timeout(tmp_path):
    with http_client_scope() as pool:
        source = Source(id="slow", name="slow", kind="web", location="http://127.0.0.1:9/never",
                        product_ids=[], allowed_domains=["127.0.0.1"], internal=True,
                        respect_robots=False, timeout_seconds=0.5)
        started = time.monotonic()
        result = collect(source, str(tmp_path), "http")
    assert result.status in {"failed", "timeout"}
    assert time.monotonic() - started < 5
    assert pool._client is None  # the owning scope closed the client


def test_browser_navigation_guard_reuses_job_robots_decision(tmp_path):
    if next(item for item in doctor() if item["backend"] == "playwright")["status"] != "available":
        pytest.skip("Browser unavailable")
    run, requests, _ = _server(200)
    with run() as url, robots_policy_scope():
        results = [collect(_source(url, f"/page-{i}", timeout=10), str(tmp_path), "playwright") for i in range(2)]
    assert [result.status for result in results] == ["fetched", "fetched"]
    # Route callbacks run outside this thread's context; they must still
    # use the job cache instead of fetching robots.txt for every navigation.
    assert requests["/robots.txt"] == 1
