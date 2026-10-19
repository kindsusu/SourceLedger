"""Offline browser checks for recommendation handoff and selected research targets."""
from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
import copy
from pathlib import Path
import threading

import pytest

from su_crawler.browser_runtime import select_browser_runtime
from su_crawler.web_server import build_web_server


def test_recommendation_selection_and_guided_run(tmp_path):
    playwright = pytest.importorskip("playwright.sync_api")
    server = build_web_server(tmp_path, port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    data = {
        "csrf_token": "test-token", "status": "configured", "version": "test",
        "workspace_root": str(tmp_path), "worker": {"status": "stopped"}, "jobs": [],
        "workspace": {"industry": "Fixtures", "market": "Offline", "product": {
            "name": "Test part", "identifiers": {"model": "TEST-A"}, "required_specs": {},
        }, "sources": []},
        "recommendations": {"requests": []},
    }
    captured = []

    def route_api(route):
        request = route.request
        path = request.url.split("/api/", 1)[1]
        if request.method == "GET" and path == "bootstrap":
            route.fulfill(json=copy.deepcopy(data))
            return
        body = request.post_data_json
        captured.append((path, body))
        if path == "recommendations":
            item = {"id": f"request-{len(data['recommendations']['requests']) + 1}",
                    "kind": body["kind"], "query": body["query"], "status": "pending",
                    "created_at": "2026-10-01T00:00:00Z", "updated_at": "2026-10-01T00:00:00Z",
                    "note": "", "candidates": []}
            data["recommendations"]["requests"].append(item)
            route.fulfill(json={"request": item})
        elif path == "recommendations/select":
            item = next(item for item in data["recommendations"]["requests"] if item["id"] == body["request_id"])
            ids = []
            for candidate in item["candidates"]:
                if candidate["id"] in body["candidate_ids"]:
                    candidate["source_id"] = f"source-{candidate['id']}"
                    ids.append(candidate["source_id"])
                    data["workspace"]["sources"].append({"id": candidate["source_id"],
                        "name": candidate["name"], "location": candidate["url"],
                        "scope": "public", "status": "candidate", "kind": "web"})
            route.fulfill(json={"request": item, "source_ids": ids, "added_count": len(ids)})
        elif path == "jobs":
            route.fulfill(json={"id": "job-test", "operation": "agent", "status": "queued"})
        elif path == "sources":
            source = {"id": f"source-direct-{len(data['workspace']['sources'])}",
                      "name": body.get("name", "Direct"), "location": body["url"],
                      "scope": body["scope"], "status": "candidate", "kind": "web"}
            data["workspace"]["sources"].append(source)
            route.fulfill(json=source)
        else:
            route.fulfill(status=404, json={"error": "Unexpected API call"})

    try:
        with playwright.sync_playwright() as driver:
            runtime = select_browser_runtime(driver.chromium)
            if runtime is None:
                pytest.skip("No local Chromium runtime installed")
            browser = driver.chromium.launch(headless=True, **runtime.launch_options())
            page = browser.new_page(viewport={"width": 1440, "height": 900})
            errors = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.route("**/api/**", route_api)
            page.goto(f"{server.url}/#sources")
            page.locator('#source-form input[name="name"]').fill("Acme")
            page.locator('#source-form select[name="scope"]').select_option("internal")
            page.get_by_role("button", name="Create company request").click()
            playwright.expect(page.locator("#source-form [data-form-error]")).to_contain_text("authorized internal URL")
            assert captured == []
            page.locator('#source-form select[name="scope"]').select_option("public")
            page.get_by_role("button", name="Create company request").click()
            playwright.expect(page.locator("#recommendation-list")).to_contain_text("Acme")
            assert captured[-1] == ("recommendations", {"query": "Acme", "kind": "company"})
            assert data["workspace"]["sources"] == []

            page.locator('#recommendation-form input[name="query"]').fill("servo motor vendors")
            page.get_by_role("button", name="Create recommendation request").click()
            playwright.expect(page.locator("#recommendation-list")).to_contain_text("servo motor vendors")
            assert captured[-1] == ("recommendations", {"query": "servo motor vendors", "kind": "keyword"})
            first = data["recommendations"]["requests"][0]
            first["status"] = "ready"
            first["candidates"] = [
                {"id": "a", "name": "<img src=x onerror=alert(1)>", "url": "https://one.invalid/catalog",
                 "reason": "Catalog search result", "evidence_url": "https://one.invalid/evidence", "source_id": None},
                {"id": "b", "name": "Two", "url": "https://two.invalid/catalog",
                 "reason": "Supplier page", "evidence_url": "https://two.invalid/evidence", "source_id": None},
            ]
            page.evaluate("loadBootstrap({quiet:true})")
            cards = page.locator('.recommendation-request[data-request-id="request-1"]')
            playwright.expect(cards.locator('[data-candidate-id="a"]')).not_to_be_checked()
            assert cards.locator("img").count() == 0
            screenshot_dir = Path(__file__).resolve().parents[1] / ".tmp"
            screenshot_dir.mkdir(exist_ok=True)
            page.screenshot(path=str(screenshot_dir / "recommendations-desktop.png"), full_page=True)
            page.set_viewport_size({"width": 375, "height": 900})
            page.screenshot(path=str(screenshot_dir / "recommendations-mobile.png"), full_page=True)
            page.set_viewport_size({"width": 1440, "height": 900})
            cards.locator('[data-candidate-id="a"]').check()
            page.locator('#source-form input[name="url"]').fill("https://unsaved.invalid/")
            page.locator('#source-form input[name="url"]').focus()
            page.evaluate("loadBootstrap({quiet:true})")
            playwright.expect(page.locator('#source-form input[name="url"]')).to_have_value("https://unsaved.invalid/")
            playwright.expect(page.locator('#source-form input[name="url"]')).to_be_focused()
            playwright.expect(cards.locator('[data-candidate-id="a"]')).to_be_checked()
            cards.get_by_role("button", name="Add selected to research list").click()
            playwright.expect(page.locator("#source-list .source-item")).to_have_count(1)
            assert captured[-1] == ("recommendations/select", {"request_id": "request-1", "candidate_ids": ["a"]})
            assert first["candidates"][1]["source_id"] is None

            # A later submission remains visible and cannot reset an existing target choice.
            first["candidates"].append({"id": "c", "name": "Three", "url": "https://three.invalid/",
                "reason": "Additional result", "evidence_url": "https://three.invalid/evidence", "source_id": None})
            page.locator('[data-run-source-id="source-a"]').uncheck()
            page.locator('[data-run-source-id="source-a"]').focus()
            page.evaluate("loadBootstrap({quiet:true})")
            playwright.expect(page.locator('[data-run-source-id="source-a"]')).to_be_focused()
            page.locator('#recommendation-form input[name="query"]').fill("unfinished query")
            page.locator('#recommendation-form input[name="query"]').focus()
            page.evaluate("loadBootstrap({quiet:true})")
            playwright.expect(page.locator('[data-run-source-id="source-a"]')).not_to_be_checked()
            playwright.expect(cards.locator('[data-candidate-id="c"]')).not_to_be_checked()
            playwright.expect(page.locator('#recommendation-form input[name="query"]')).to_have_value("unfinished query")
            playwright.expect(page.locator('#recommendation-form input[name="query"]')).to_be_focused()
            page.reload()
            playwright.expect(page.locator('[data-run-source-id="source-a"]')).not_to_be_checked()
            page.locator("#source-filter").fill("absent")
            playwright.expect(page.locator("#source-selection-summary")).to_contain_text("hidden selections")
            page.locator("#source-filter").fill("")
            page.locator('[data-run-source-id="source-a"]').check()
            page.locator('nav a[data-route="runs"]').click()
            page.locator("#legacy-guided-run > summary").click()
            page.get_by_role("button", name="Queue research run").click()
            assert ("jobs", {"operation": "agent", "arguments": {
                "source_ids": ["source-a"], "max_sources": 1,
                "max_seconds": 120, "max_model_calls": 0,
            }}) in captured
            page.locator('nav a[data-route="sources"]').click()
            for width in (375, 768, 1024, 1440):
                page.set_viewport_size({"width": width, "height": 900})
                assert page.evaluate("document.documentElement.scrollWidth <= innerWidth"), width
            assert not errors
            browser.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_real_browser_mcp_recommendation_handoff(tmp_path):
    """The UI and MCP use one durable workspace; suggestions register only when chosen."""
    pytest.importorskip("mcp")
    playwright = pytest.importorskip("playwright.sync_api")
    from su_crawler.assistant_server import build_assistant_server
    from su_crawler.research import load_workspace

    def call_tool(server, name, arguments):
        # Playwright's synchronous API owns an event loop in this thread.
        # Execute the MCP coroutine on a separate thread, as an independent client would.
        def invoke():
            _, result = asyncio.run(server.call_tool(name, arguments))
            return result
        with ThreadPoolExecutor(max_workers=1) as executor:
            return executor.submit(invoke).result(timeout=15)

    def submit(server, request_id, candidates):
        return call_tool(server, "submit_source_recommendations", {
            "request_id": request_id, "candidates": candidates,
        })

    server = build_web_server(tmp_path, port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with playwright.sync_playwright() as driver:
            runtime = select_browser_runtime(driver.chromium)
            if runtime is None:
                pytest.skip("No local Chromium runtime installed")
            browser = driver.chromium.launch(headless=True, **runtime.launch_options())
            page = browser.new_page(viewport={"width": 1024, "height": 900})
            page.goto(f"{server.url}/#overview")
            page.locator("#setup-form").get_by_label("Industry", exact=True).fill("Fixture machinery")
            page.locator("#setup-form").get_by_label("Product", exact=True).fill("Fixture part")
            page.locator("#setup-form").get_by_label("Research region", exact=True).fill("Offline market")
            page.get_by_role("button", name="Create workspace").click()
            page.locator('nav a[data-route="sources"]').click()
            page.locator('#recommendation-form input[name="query"]').fill("Fixture suppliers")
            page.get_by_role("button", name="Create recommendation request").click()
            playwright.expect(page.locator("#recommendation-list")).to_contain_text("Fixture suppliers")

            mcp_server = build_assistant_server(tmp_path)
            listed = call_tool(mcp_server, "list_source_recommendation_requests", {})
            request_id = listed["requests"][0]["id"]
            assert listed["requests"][0]["status"] == "pending"
            assert load_workspace(tmp_path / "research.json")["sources"] == []

            first = {"name": "Fixture One", "url": "https://one.invalid/catalog",
                     "reason": "Fixture catalog", "evidence_url": "https://one.invalid/about"}
            second = {"name": "Fixture Two", "url": "https://two.invalid/catalog",
                      "reason": "Fixture catalog", "evidence_url": "https://two.invalid/about"}
            submitted = submit(mcp_server, request_id, [first, second])["request"]
            assert submitted["status"] == "ready"
            page.evaluate("loadBootstrap({quiet:true})")
            card = page.locator(f'.recommendation-request[data-request-id="{request_id}"]')
            playwright.expect(card.locator(".candidate-item")).to_have_count(2)
            playwright.expect(card.locator('[data-candidate-id]').first).not_to_be_checked()
            playwright.expect(card.locator('[data-candidate-id]').nth(1)).not_to_be_checked()
            first_id = submitted["candidates"][0]["id"]
            card.locator(f'[data-candidate-id="{first_id}"]').check()
            card.get_by_role("button", name="Add selected to research list").click()
            playwright.expect(page.locator("#source-list .source-item")).to_have_count(1)
            assert [item["location"] for item in load_workspace(tmp_path / "research.json")["sources"]] == [first["url"]]

            third = {"name": "Fixture Three", "url": "https://three.invalid/catalog",
                     "reason": "Later fixture result", "evidence_url": "https://three.invalid/about"}
            later = submit(mcp_server, request_id, [third])["request"]
            assert len(later["candidates"]) == 3
            page.evaluate("loadBootstrap({quiet:true})")
            playwright.expect(card.locator(".candidate-item")).to_have_count(3)
            third_id = later["candidates"][2]["id"]
            card.locator(f'[data-candidate-id="{third_id}"]').check()
            card.get_by_role("button", name="Add selected to research list").click()
            playwright.expect(page.locator("#source-list .source-item")).to_have_count(2)
            locations = [item["location"] for item in load_workspace(tmp_path / "research.json")["sources"]]
            assert locations == [first["url"], third["url"]]
            page.reload()
            playwright.expect(page.locator("#source-list .source-item")).to_have_count(2)
            playwright.expect(page.locator(f'.recommendation-request[data-request-id="{request_id}"] .candidate-item')).to_have_count(3)
            assert [item["location"] for item in load_workspace(tmp_path / "research.json")["sources"]] == locations
            browser.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
