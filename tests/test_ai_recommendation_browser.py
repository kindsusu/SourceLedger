"""Offline browser coverage for local AI runs and the existing MCP handoff."""
from __future__ import annotations

import copy
from pathlib import Path
import threading

import pytest

from su_crawler.browser_runtime import select_browser_runtime
from su_crawler.web_server import build_web_server


def test_ai_settings_run_recovery_and_host_handoff(tmp_path):
    playwright = pytest.importorskip("playwright.sync_api")
    server = build_web_server(tmp_path, port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    request = {
        "id": "request-1", "kind": "keyword", "query": "servo motor vendors",
        "status": "pending", "created_at": "2026-10-01T00:00:00Z",
        "note": "", "candidates": [],
    }
    data = {
        "csrf_token": "test-token", "status": "configured", "version": "test",
        "workspace_root": str(tmp_path), "worker": {"status": "stopped"},
        "jobs": [], "recommendation_jobs": [], "mcp_available": True,
        "workspace": {"industry": "Fixtures", "market": "Offline", "product": {
            "name": "Test part", "identifiers": {}, "required_specs": {},
        }, "sources": []},
        "recommendations": {"requests": [request]},
        "ai": {"settings": {"provider": "codex", "model": "", "timeout_seconds": 180},
               "providers": [
                   {"id": "codex", "label": "Codex CLI", "available": True,
                    "models": [{"id": "gpt-test", "label": "GPT Test"}], "message": ""},
                   {"id": "claude", "label": "Claude Code CLI", "available": False,
                    "models": [{"id": "sonnet", "label": "Sonnet"}], "message": "CLI not found"},
               ]},
    }
    captured = []
    run_error = {"message": None}

    def route_api(route):
        call = route.request
        path = call.url.split("/api/", 1)[1]
        if call.method == "GET" and path == "bootstrap":
            route.fulfill(json=copy.deepcopy(data))
            return
        body = call.post_data_json
        captured.append((path, body))
        if path == "ai/settings":
            data["ai"]["settings"] = body
            route.fulfill(json=body)
        elif path == "recommendations/run":
            if run_error["message"]:
                route.fulfill(status=409, json={"error": run_error["message"]})
                return
            job = {"id": f"job-{len(data['jobs']) + 1}", "operation": "recommend",
                   "status": "queued", "arguments": body,
                   "created_at": "2026-10-01T00:00:00Z"}
            data["jobs"].insert(0, job)
            data["recommendation_jobs"] = [job]
            route.fulfill(json={"job": job, "worker": {"status": "running"}})
        else:
            route.fulfill(status=404, json={"error": f"Unexpected {path}"})

    try:
        with playwright.sync_playwright() as driver:
            runtime = select_browser_runtime(driver.chromium)
            if runtime is None:
                pytest.skip("No local Chromium runtime installed")
            browser = driver.chromium.launch(headless=True, **runtime.launch_options())
            page = browser.new_page(viewport={"width": 1440, "height": 900})
            page.set_default_timeout(8000)
            errors = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.route("**/api/**", route_api)
            page.goto(f"{server.url}/#sources")
            card = page.locator('.recommendation-request[data-request-id="request-1"]')
            playwright.expect(card.get_by_role("button", name="Run in web UI")).to_be_enabled()
            playwright.expect(card).to_contain_text("Provider default")
            card.get_by_text("View request text").click()
            playwright.expect(card).to_contain_text("submit_source_recommendations")
            page.evaluate("window.__copied = ''; navigator.clipboard.writeText = async value => { window.__copied = value; }")
            card.get_by_role("button", name="Copy request").click()
            assert "SourceLedger recommendation request ID" in page.evaluate("window.__copied")

            page.locator('nav a[data-route="connections"]').click()
            playwright.expect(page.locator("#ai-provider-status")).to_contain_text("Sign-in is checked when you run")
            page.locator("#ai-provider").select_option("claude")
            playwright.expect(page.locator("#ai-provider-status")).to_contain_text("unavailable")
            page.locator("#ai-model-choice").select_option("__custom__")
            page.locator("#ai-custom-model").fill("claude-custom-test")
            page.locator("#ai-custom-model").focus()
            page.evaluate("loadBootstrap({quiet:true})")
            playwright.expect(page.locator("#ai-custom-model")).to_have_value("claude-custom-test")
            playwright.expect(page.locator("#ai-custom-model")).to_be_focused()
            page.get_by_text("Advanced timeout", exact=True).click()
            page.locator("#ai-timeout").fill("240")
            page.get_by_role("button", name="Save AI settings").click()
            playwright.expect(page.locator("#ai-settings-form [data-form-error]")).to_be_hidden()
            assert captured[-1] == ("ai/settings", {"provider": "claude", "model": "claude-custom-test", "timeout_seconds": 240})
            page.locator('nav a[data-route="sources"]').click()
            playwright.expect(card.get_by_role("button", name="Run in web UI")).to_be_disabled()
            playwright.expect(card).to_contain_text("claude-custom-test")

            page.locator('nav a[data-route="connections"]').click()
            page.locator("#ai-provider").select_option("codex")
            page.locator("#ai-model-choice").select_option("gpt-test")
            page.get_by_role("button", name="Save AI settings").click()
            page.locator('nav a[data-route="sources"]').click()
            page.locator('#recommendation-form input[name="query"]').fill("unfinished query")
            page.locator('#recommendation-form input[name="query"]').focus()
            card.get_by_role("button", name="Run in web UI").click()
            playwright.expect(card.get_by_role("button", name="Recommendation running…")).to_be_disabled()
            assert captured[-1] == ("recommendations/run", {"request_id": "request-1", "provider": "codex", "model": "gpt-test", "timeout_seconds": 240})
            page.locator('#recommendation-form input[name="query"]').focus()
            page.evaluate("loadBootstrap({quiet:true})")
            playwright.expect(page.locator('#recommendation-form input[name="query"]')).to_have_value("unfinished query")
            playwright.expect(page.locator('#recommendation-form input[name="query"]')).to_be_focused()
            assert data["workspace"]["sources"] == []

            job = data["jobs"][0]
            job["status"] = "failed"
            job["error"] = "CLI login required"
            page.evaluate("loadBootstrap({quiet:true})")
            playwright.expect(card.get_by_role("button", name="Retry in web UI")).to_be_enabled()
            playwright.expect(card.get_by_role("alert")).to_contain_text("CLI login required")
            run_error["message"] = "CLI is unavailable"
            card.get_by_role("button", name="Retry in web UI").click()
            playwright.expect(card.get_by_role("alert").last).to_contain_text("CLI is unavailable")
            run_error["message"] = None
            card.get_by_role("button", name="Retry in web UI").click()
            playwright.expect(card.get_by_role("button", name="Recommendation running…")).to_be_disabled()
            job = data["jobs"][0]
            job["status"] = "succeeded"
            request["status"] = "ready"
            request["candidates"] = [{"id": "candidate-1", "name": "Example",
                "url": "https://example.invalid", "evidence_url": "https://example.invalid/about",
                "reason": "Source page", "source_id": None}]
            page.evaluate("loadBootstrap({quiet:true})")
            playwright.expect(card.locator(".candidate-item")).to_have_count(1)
            playwright.expect(card.locator('[data-candidate-id="candidate-1"]')).not_to_be_checked()
            assert data["workspace"]["sources"] == []
            for width in (375, 768, 1024, 1440):
                page.set_viewport_size({"width": width, "height": 900})
                assert page.evaluate("document.documentElement.scrollWidth <= innerWidth"), width
            assert errors == []
            browser.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_real_web_ai_job_review_and_model_receipt(tmp_path, monkeypatch):
    """Real browser/API/worker/store round trip; only model output is synthetic."""
    from su_crawler import ai_providers, assistant_runtime, recommendation_jobs, research
    playwright = pytest.importorskip("playwright.sync_api")
    monkeypatch.setattr(ai_providers, "_codex_command", lambda: ["fixture.exe"])
    monkeypatch.setattr(ai_providers, "_claude_command", lambda: None)
    monkeypatch.setattr(ai_providers, "_codex_models", lambda: [{"id": "fixture-model", "label": "Fixture model"}])
    entered, release = threading.Event(), threading.Event()
    workers, captured = [], []

    def generate(item, settings):
        captured.append((item, settings))
        entered.set()
        assert release.wait(15)
        return {"candidates": [{"name": "Fixture supplier", "url": "https://supplier.invalid/catalog",
                                "reason": "Synthetic catalog reference", "evidence_url": "https://supplier.invalid/about"}],
                "note": "Offline validation fixture", "actual_model": "fixture-model-snapshot"}

    def start(root):
        token = "c" * 32
        research._atomic_write(root / "runtime" / "state.json", {"token": token})
        worker = threading.Thread(target=assistant_runtime._run_worker, args=(root, token, 0.1), daemon=True)
        workers.append(worker)
        worker.start()
        return {"status": "starting"}

    monkeypatch.setattr(recommendation_jobs, "generate_recommendations", generate)
    monkeypatch.setattr("su_crawler.web_server.start_worker", start)
    server = build_web_server(tmp_path, port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with playwright.sync_playwright() as driver:
            runtime = select_browser_runtime(driver.chromium)
            if runtime is None:
                pytest.skip("No local Chromium runtime installed")
            browser = driver.chromium.launch(headless=True, **runtime.launch_options())
            page = browser.new_page(viewport={"width": 1440, "height": 1000})
            page.set_default_timeout(8000)
            errors = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.goto(f"{server.url}/#overview")
            page.locator("#setup-form").get_by_label("Industry", exact=True).fill("Fixture machinery")
            page.locator("#setup-form").get_by_label("Product", exact=True).fill("Fixture part")
            page.locator("#setup-form").get_by_label("Research region", exact=True).fill("Offline market")
            page.get_by_role("button", name="Create workspace").click()
            page.locator('nav a[data-route="connections"]').click()
            page.locator("#ai-model-choice").select_option("fixture-model")
            page.get_by_role("button", name="Save AI settings").click()
            playwright.expect(page.get_by_role("button", name="Save AI settings")).to_be_enabled()
            assert ai_providers.get_ai_configuration(tmp_path)["settings"]["model"] == "fixture-model"
            screenshots = Path.cwd() / ".tmp"
            screenshots.mkdir(exist_ok=True)
            page.screenshot(path=str(screenshots / "ai-connections-desktop.png"), full_page=True)
            page.locator('nav a[data-route="sources"]').click()
            page.locator('#recommendation-form input[name="query"]').fill("Fixture suppliers")
            page.get_by_role("button", name="Create recommendation request").click()
            card = page.locator(".recommendation-request")
            playwright.expect(card).to_contain_text("Fixture suppliers")
            card.get_by_role("button", name="Run in web UI").click()
            assert entered.wait(3)
            playwright.expect(card.get_by_role("button", name="Recommendation running…")).to_be_disabled()
            assert research.load_workspace(tmp_path / "research.json")["sources"] == []
            release.set()
            playwright.expect(card.locator(".candidate-item")).to_have_count(1, timeout=10000)
            assert captured[0][1] == {"provider": "codex", "model": "fixture-model", "timeout_seconds": 180}
            playwright.expect(card.locator("[data-candidate-id]")).not_to_be_checked()
            assert research.load_workspace(tmp_path / "research.json")["sources"] == []
            card.locator("[data-candidate-id]").check()
            card.get_by_role("button", name="Add selected to research list").click()
            playwright.expect(page.locator("#source-list .source-item")).to_have_count(1)
            for width in (375, 768, 1024, 1440):
                page.set_viewport_size({"width": width, "height": 1000})
                assert page.evaluate("document.documentElement.scrollWidth <= innerWidth"), width
            page.screenshot(path=str(screenshots / "ai-sources-desktop.png"), full_page=True)
            page.set_viewport_size({"width": 375, "height": 900})
            page.screenshot(path=str(screenshots / "ai-sources-mobile.png"), full_page=True)
            page.locator('nav a[data-route="runs"]').click()
            page.get_by_role("button", name="Recommend job, Succeeded").click()
            playwright.expect(page.locator("#job-detail")).to_contain_text("fixture-model-snapshot")
            assert not list(tmp_path.rglob("prices.sqlite3")) and not list(tmp_path.rglob("*.xlsx"))
            assert errors == []
            browser.close()
    finally:
        release.set()
        for worker in workers:
            worker.join(timeout=3)
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
