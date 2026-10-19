"""Browser settling must observe a delayed price, including on generic sites."""

from __future__ import annotations

import pytest

from su_crawler.browser_settlement import (install_tracker_sync,
                                           wait_settled_sync)


def test_delayed_generic_price_and_visible_loader_are_not_accepted_early():
    playwright = pytest.importorskip("playwright.sync_api")
    with playwright.sync_playwright() as driver:
        browser = driver.chromium.launch(headless=True)
        try:
            page = browser.new_page()
            install_tracker_sync(page)
            page.set_content("""<!doctype html><html><body>
              <strong id='price'>100</strong><div id='load' role='progressbar'>Calculating</div>
              <script>setTimeout(() => {
                document.querySelector('#price').textContent = '125';
                document.querySelector('#load').remove();
              }, 400);</script></body></html>""")
            result = wait_settled_sync(
                page, lambda: page.locator("#price").inner_text(),
                ready=lambda value: bool(value), timeout_seconds=3,
                quiet_ms=150, stable_ms=200, poll_ms=50)
            assert result["status"] == "settled"
            assert result["probe"] == "125"
            assert result["network"]["loading"] == 0
        finally:
            browser.close()


def test_unfinished_loader_times_out_instead_of_emitting_value():
    playwright = pytest.importorskip("playwright.sync_api")
    with playwright.sync_playwright() as driver:
        browser = driver.chromium.launch(headless=True)
        try:
            page = browser.new_page()
            install_tracker_sync(page)
            page.set_content("<div id='price'>100</div><div aria-busy='true'>Updating</div>")
            result = wait_settled_sync(
                page, lambda: page.locator("#price").inner_text(),
                ready=lambda value: bool(value), timeout_seconds=.2,
                quiet_ms=0, stable_ms=0, poll_ms=20)
            assert result["status"] == "timeout"
            assert result["network"]["loading"] == 1
        finally:
            browser.close()


def test_cross_origin_fetch_body_remains_pending_after_headers():
    playwright = pytest.importorskip("playwright.sync_api")
    with playwright.sync_playwright() as driver:
        browser = driver.chromium.launch(headless=True)
        try:
            page = browser.new_page()
            page.set_content("<strong id='price'>100</strong>")
            page.evaluate("""() => {
              Response.prototype.arrayBuffer = () => { throw Error('whole-body buffer forbidden'); };
              window.fetch = () => Promise.resolve(new Response(new ReadableStream({
                start(controller) { setTimeout(() => {
                  controller.enqueue(new TextEncoder().encode('125'));
                  controller.close();
                }, 400); }
              })));
            }""")
            install_tracker_sync(page)
            page.evaluate("""() => {
              fetch('https://prices.example.test/calculate').then(r => r.text())
                .then(value => { document.querySelector('#price').textContent = value; });
            }""")
            assert page.evaluate("() => window.__sourceLedgerNetwork.pending") == 1
            result = wait_settled_sync(
                page, lambda: page.locator("#price").inner_text(),
                ready=lambda value: bool(value), timeout_seconds=3,
                quiet_ms=50, stable_ms=100, poll_ms=50)
            assert result["status"] == "settled"
            assert result["probe"] == "125"
        finally:
            browser.close()
