"""Browser session lifetime and isolation, using a local page only."""

from __future__ import annotations

import pytest
import time

from su_crawler.collectors import _ReusableBrowser, _close_reusable_context, browser_session, collect
from su_crawler.doctor import doctor
from su_crawler.models import Source

from test_browser import server


def test_reused_browser_keeps_contexts_independent(tmp_path, monkeypatch):
    # CI and background workers may have no interactive desktop.
    monkeypatch.setenv("SOURCELEDGER_HEADLESS", "1")
    browser = next(item for item in doctor() if item["backend"] == "playwright")
    if browser["status"] != "available":
        pytest.skip(browser["reason"])
    with server() as url:
        source = Source(
            id="reuse-test", name="reuse test", kind="web", location=url,
            product_ids=[], allowed_domains=["127.0.0.1"], internal=True,
            respect_robots=False, timeout_seconds=8,
            recipe=[{"action": "select", "selector": "#size", "value": "large"}],
        )
        with browser_session():
            first = collect(source, str(tmp_path), "playwright")
            source.recipe = []
            second = collect(source, str(tmp_path), "playwright")

    assert first.status == second.status == "fetched", (first.status, first.message, first.trace, second.status, second.message, second.trace)
    assert b'value="large" selected' in first.content
    assert b'value="large" selected' not in second.content
    first_ready = next(event for event in first.trace if event["event"] == "browser_context_ready")
    second_ready = next(event for event in second.trace if event["event"] == "browser_context_ready")
    assert first_ready["launch_ms"] > 0 and first_ready["reused"] is False
    assert second_ready["launch_ms"] == 0 and second_ready["reused"] is True


def test_session_closes_only_outer_scope(monkeypatch):
    from su_crawler import collectors

    closed = []
    monkeypatch.setattr(collectors._ReusableBrowser, "close", lambda self: closed.append(self))
    with browser_session():
        outer = collectors._browser_thread_state.session
        with browser_session():
            assert collectors._browser_thread_state.session is outer
        assert closed == []
    assert closed == [outer]
    assert collectors._browser_thread_state.session is None


def test_failed_context_cleanup_retires_session(monkeypatch):
    retired = []
    monkeypatch.setattr(_ReusableBrowser, "close", lambda self: retired.append(self))

    class BrokenContext:
        def unroute_all(self, **kwargs):
            pass

        def close(self):
            raise RuntimeError("browser disconnected")

    session = _ReusableBrowser()
    _close_reusable_context(BrokenContext(), session)
    assert retired == [session]


def test_startup_budget_includes_driver_start(monkeypatch):
    from playwright import sync_api
    from su_crawler import collectors

    events = []

    class Driver:
        class Chromium:
            def launch(self, **kwargs):
                raise AssertionError("Launch should not be reached after deadline")

        chromium = Chromium()

        def stop(self):
            events.append("stop")

    class Starter:
        def start(self):
            time.sleep(0.02)
            return Driver()

    monkeypatch.setattr(sync_api, "sync_playwright", lambda: Starter())
    class Runtime:
        def launch_options(self):
            return {}

    monkeypatch.setattr(collectors, "select_browser_runtime", lambda chromium: Runtime())
    with pytest.raises(sync_api.TimeoutError):
        _ReusableBrowser().context(1)
    assert events == ["stop"]
