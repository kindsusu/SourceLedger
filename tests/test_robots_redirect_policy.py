from __future__ import annotations

import httpx
import pytest

from su_crawler import collectors
from su_crawler.doctor import doctor
from su_crawler.models import Source


def _source(url: str) -> Source:
    return Source(
        id="redirect", name="redirect", kind="web", location=url,
        product_ids=[], allowed_domains=["one.test", "two.test"],
        respect_robots=True,
    )


def test_redirect_target_robots_checked_before_fetch(monkeypatch):
    monkeypatch.setattr(collectors, "_check_url", lambda *args: None)
    requested: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        requested.append(url)
        if url == "https://one.test/robots.txt":
            return httpx.Response(200, text="User-agent: *\nAllow: /\n")
        if url == "https://one.test/start":
            return httpx.Response(302, headers={"Location": "https://two.test/private"})
        if url == "https://two.test/robots.txt":
            return httpx.Response(200, text="User-agent: *\nDisallow: /private\n")
        raise AssertionError(f"Unexpected request: {url}")

    with collectors.robots_policy_scope(), httpx.Client(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(collectors.PolicyError, match="robots.txt"):
            collectors._get_following_policy(client, _source("https://one.test/start"), "https://one.test/start", enforce_robots=True)
    assert requested == [
        "https://one.test/robots.txt", "https://one.test/start", "https://two.test/robots.txt",
    ]


def test_robots_cache_is_scoped_and_policy_is_checked_per_path(monkeypatch):
    monkeypatch.setattr(collectors, "_check_url", lambda *args: None)
    robots_fetches = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal robots_fetches
        if request.url.path == "/robots.txt":
            robots_fetches += 1
            return httpx.Response(200, text="User-agent: *\nDisallow: /private\n")
        return httpx.Response(200, text="ok")

    source = _source("https://one.test/open")
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        with collectors.robots_policy_scope():
            assert collectors._robots_decision(client, source, "https://one.test/open")[0] == "allowed"
            assert collectors._robots_decision(client, source, "https://one.test/private")[0] == "disallowed"
        with collectors.robots_policy_scope():
            assert collectors._robots_decision(client, source, "https://one.test/open")[0] == "allowed"
    assert robots_fetches == 2


def test_doctor_launch_check_is_opt_in(monkeypatch):
    monkeypatch.setattr("su_crawler.doctor._playwright_browser_ready", lambda: (True, "executable found"))
    calls: list[bool] = []

    def probe(*, headless: bool) -> tuple[bool, str]:
        calls.append(headless)
        return False, "Chromium could not start"

    monkeypatch.setattr("su_crawler.doctor._probe_browser_launch", probe)
    browser = next(item for item in doctor() if item["backend"] == "playwright")
    assert browser["launch_verified"] is None
    assert browser["executable_found"] is True
    assert browser["connected"] is False
    assert calls == []
    browser = next(item for item in doctor(verify_browser_launch=True, browser_display_mode="headless") if item["backend"] == "playwright")
    assert browser["launch_verified"] is False
    assert browser["status"] == "unavailable"
    assert calls == [True]
