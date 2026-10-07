"""Offline browser contract for the beginner research-plan flow."""
from __future__ import annotations

import copy

import pytest

from test_web_browser import launch_browser
from test_web_server import running_server


def test_plan_preserves_detailed_request_and_confirms_before_start(tmp_path):
    playwright = pytest.importorskip("playwright.sync_api")
    with running_server(tmp_path) as server, playwright.sync_playwright() as driver:
        browser = launch_browser(driver)
        page = browser.new_page(viewport={"width": 1280, "height": 900})
        page.set_default_timeout(8000)
        errors = []
        page.on("pageerror", lambda exc: errors.append(str(exc)))
        calls = []
        plan = None
        archived = []
        job = {"id": "preview-old", "operation": "plan_preview", "status": "failed",
               "error": "Provider unavailable", "arguments": {"plan_id": "plan-old"},
               "result": {}, "created_at": "2026-09-01T00:00:00Z"}
        data = {
            "csrf_token": "test-token", "status": "unconfigured", "version": "test",
            "workspace_root": str(tmp_path), "worker": {"status": "stopped"},
            "jobs": [], "plan_jobs": [job], "research_plans": {"plans": []},
            "recommendation_jobs": [], "recommendations": {"requests": []},
            "mcp_available": True,
            "ai": {"settings": {"provider": "codex", "model": "", "timeout_seconds": 180},
                   "providers": [{"id": "codex", "label": "Codex", "available": True,
                                  "models": [], "message": ""}]},
        }

        def route_api(route):
            nonlocal plan, job
            call = route.request
            path = call.url.split("/api/", 1)[1]
            if call.method == "GET" and path == "bootstrap":
                data["research_plans"] = {"plans": ([plan] if plan else []) + archived}
                route.fulfill(json=copy.deepcopy(data))
                return
            if call.method == "GET" and path.startswith("jobs/"):
                route.fulfill(json=copy.deepcopy(job))
                return
            body = call.post_data_json
            calls.append((path, copy.deepcopy(body)))
            if path == "plans":
                if plan:
                    archived.insert(0, copy.deepcopy(plan))
                plan = {"id": f"plan-{len(archived) + 1}", "revision": 1, "state": "draft",
                        "request_text": body["request_text"], "summary": "", "note": "",
                        "topic": {"industry": "", "product": "", "market": ""},
                        "categories": [], "include_terms": [], "exclude_terms": [],
                        "candidates": [], "excluded_urls": []}
                route.fulfill(json={"plan": copy.deepcopy(plan)})
            elif path == "plans/preview":
                assert body["expected_revision"] == plan["revision"]
                plan.update(revision=plan["revision"] + 1, summary="Range of test devices",
                            topic={"industry": "Devices", "product": "Test instruments", "market": "Japan"},
                            categories=["Bench instruments"])
                if "https://example.invalid/product" not in plan["excluded_urls"]:
                    plan["candidates"] = [{"id": "candidate-1", "name": "Sample product",
                                           "url": "https://example.invalid/product", "evidence_url": "https://example.invalid/evidence",
                                           "reason": "Product page", "kind": "product", "selected": False, "origin": "ai"}]
                plan["state"] = "preview" if plan["candidates"] else "draft"
                route.fulfill(json={"job": {"id": "preview-1", "operation": "plan_preview", "status": "succeeded"}, "worker": {"status": "idle"}})
            elif path == "plans/edit":
                assert body["expected_revision"] == plan["revision"]
                changes = body["changes"]
                plan.update({key: copy.deepcopy(value) for key, value in changes.items()
                             if key in {"summary", "topic", "categories", "include_terms", "exclude_terms", "request_text"}})
                selected = set(changes.get("selected_candidate_ids", []))
                for candidate in plan["candidates"]:
                    candidate["selected"] = candidate["id"] in selected
                removed = set(changes.get("removed_candidate_ids", []))
                plan["excluded_urls"].extend(candidate["url"] for candidate in plan["candidates"] if candidate["id"] in removed)
                plan["candidates"] = [candidate for candidate in plan["candidates"] if candidate["id"] not in removed]
                plan["revision"] += 1
                plan["state"] = "preview" if plan["candidates"] else "draft"
                route.fulfill(json={"plan": copy.deepcopy(plan)})
            elif path == "plans/confirm":
                assert body == {"plan_id": plan["id"], "expected_revision": plan["revision"], "user_confirmed": True}
                assert plan["topic"] == {"industry": "Devices", "product": "Test instruments", "market": "Japan"}
                assert plan["candidates"][0]["selected"] is True
                plan["revision"] += 1
                plan["state"] = "confirmed"
                data["status"] = "configured"
                data["workspace"] = {"industry": "Devices", "market": "Japan", "product": {"name": "Test instruments", "identifiers": {}, "required_specs": {}}, "sources": []}
                route.fulfill(json={"plan": copy.deepcopy(plan)})
            elif path == "plans/start":
                assert plan["state"] == "confirmed"
                assert body["expected_revision"] == plan["revision"]
                job = {"id": "job-1", "operation": "research_plan", "status": "queued", "arguments": body,
                       "result": {}, "created_at": "2026-10-01T00:00:00Z"}
                data["plan_jobs"] = [job]
                route.fulfill(json={"job": copy.deepcopy(job), "worker": {"status": "running"}})
            else:
                route.fulfill(status=404, json={"error": f"Unexpected {path}"})

        try:
            page.route("**/api/**", route_api)
            page.goto(server.url)
            playwright.expect(page.locator("#plan-view")).to_be_visible()
            page.locator('[data-route="runs"]').click()
            playwright.expect(page.locator("#job-list")).to_contain_text("Plan preview")
            playwright.expect(page.locator("#legacy-guided-run")).to_be_hidden()
            page.locator('#job-list [data-job-id="preview-old"]').click()
            playwright.expect(page.locator("#job-detail")).to_contain_text("Provider unavailable")
            page.locator('[data-route="plan"]').click()
            playwright.expect(page.locator("#plan-ai-path")).to_have_value("host")
            page.locator("#plan-ai-path").select_option("local")
            detailed = "Research test instruments sold in Japan.\nInclude bench models with USB output.\nExclude used units and keep original price evidence."
            page.locator("#plan-request-text").fill(detailed)
            page.locator("#plan-create").click()
            playwright.expect(page.locator("#plan-candidates .plan-candidate")).to_have_count(1)
            playwright.expect(page.locator("#plan-candidates a.plan-url")).to_have_count(2)
            assert calls[0] == ("plans", {"request_text": detailed})
            page.locator("#plan-categories").fill("Bench instruments\nPortable instruments")
            page.locator("#plan-candidates [data-plan-select]").check()
            page.locator("#language-select").select_option("ko")
            playwright.expect(page.locator("#plan-categories")).to_have_value("Bench instruments\nPortable instruments")
            playwright.expect(page.locator("#plan-candidates [data-plan-select]")).to_be_checked()
            page.locator("#plan-start").click()
            playwright.expect(page.locator("#job-detail")).to_contain_text("job-1")
            assert [path for path, _ in calls] == ["plans", "plans/preview", "plans/edit", "plans/confirm", "plans/start"]
            assert calls[2][1]["changes"]["categories"] == ["Bench instruments", "Portable instruments"]
            page.goto(f"{server.url}/#plan")
            page.locator("#plan-candidates [data-plan-remove]").click()
            page.locator("#plan-preview").click()
            playwright.expect(page.locator("#plan-candidates .plan-candidate")).to_have_count(0)
            playwright.expect(page.locator("#plan-copy-text")).to_contain_text("revision 6")
            assert "https://example.invalid/product" in plan["excluded_urls"]
            assert [path for path, _ in calls][-2:] == ["plans/edit", "plans/preview"]
            revised_request = detailed + "\nPrioritize rechargeable options."
            page.locator("#plan-request-text").fill(revised_request)
            page.locator("#plan-create").click()
            playwright.expect(page.locator("#plan-copy-text")).to_contain_text("revision 8")
            assert [path for path, _ in calls][-2:] == ["plans/edit", "plans/preview"]
            assert calls[-2][1]["changes"]["request_text"] == revised_request
            assert plan["id"] == "plan-1" and "https://example.invalid/product" in plan["excluded_urls"]
            page.locator("#plan-categories").fill("Unsaved category")
            plan["revision"] += 1  # An assistant updated this plan while the form had unsaved edits.
            page.evaluate("loadBootstrap({quiet: true})")
            playwright.expect(page.locator("#plan-categories")).to_have_value("Unsaved category")
            playwright.expect(page.locator("#plan-error")).to_contain_text("변경")
            for width in (320, 375, 768):
                page.set_viewport_size({"width": width, "height": 900})
                assert page.evaluate("document.documentElement.scrollWidth <= innerWidth"), width
            page.locator("#plan-new-request").click()
            playwright.expect(page.locator("#plan-request-text")).to_have_value("")
            page.evaluate("loadBootstrap({quiet: true})")
            playwright.expect(page.locator("#plan-request-text")).to_have_value("")
            page.locator("#plan-ai-path").select_option("host")
            page.locator("#plan-request-text").fill("Research portable meters in Japan")
            page.locator("#plan-create").click()
            playwright.expect(page.locator(".plan-handoff details")).to_have_attribute("open", "")
            assert [path for path, _ in calls][-1] == "plans"  # Host path never launches a model.
            page.locator("#plan-summary").fill("Portable meter range")
            page.locator("#plan-change-text").fill("Prefer rechargeable products")
            page.evaluate("window.__copied = ''; navigator.clipboard.writeText = async value => { window.__copied = value; }")
            page.locator("#plan-copy").click()
            playwright.expect(page.locator("#plan-status")).to_contain_text("복사")
            copied = page.evaluate("window.__copied")
            assert "plan-2 at revision 2" in copied
            assert "Portable meter range" in copied
            assert "Prefer rechargeable products" in copied
            assert [path for path, _ in calls][-1] == "plans/edit"
            page.locator("#plan-history").select_option("plan-1")
            playwright.expect(page.locator("#plan-request-text")).to_have_value(revised_request)
            page.evaluate("loadBootstrap({quiet: true})")
            playwright.expect(page.locator("#plan-request-text")).to_have_value(revised_request)
            assert errors == []
        finally:
            browser.close()


def test_plan_create_waits_for_bootstrap_and_shows_create_error(tmp_path):
    playwright = pytest.importorskip("playwright.sync_api")
    with running_server(tmp_path) as server, playwright.sync_playwright() as driver:
        browser = launch_browser(driver)
        page = browser.new_page(viewport={"width": 1100, "height": 800})
        page.set_default_timeout(8000)
        held = []
        held_create = []
        posts = []
        hold_bootstrap = True
        data = {
            "csrf_token": "ready-token", "status": "unconfigured", "version": "test",
            "workspace_root": str(tmp_path), "worker": {"status": "stopped"},
            "jobs": [], "plan_jobs": [], "research_plans": {"plans": []},
            "recommendation_jobs": [], "recommendations": {"requests": []},
            "mcp_available": True,
            "ai": {"settings": {"provider": "codex", "model": "", "timeout_seconds": 180},
                   "providers": [{"id": "codex", "label": "Codex", "available": False,
                                  "models": [], "message": ""}]},
        }

        def route_api(route):
            path = route.request.url.split("/api/", 1)[1]
            if route.request.method == "GET" and path == "bootstrap":
                if hold_bootstrap:
                    held.append(route)
                else:
                    route.fulfill(json=copy.deepcopy(data))
                return
            posts.append(path)
            if path == "plans":
                held_create.append(route)
            else:
                route.fulfill(status=403, json={"error": "Fixture create denied"})

        try:
            page.route("**/api/**", route_api)
            page.goto(server.url)
            playwright.expect(page.locator("#plan-view")).to_be_visible()
            playwright.expect(page.locator("#plan-create")).to_be_disabled()
            page.locator("#plan-request-text").fill("Research portable meters")
            page.evaluate("document.getElementById('plan-request-form').requestSubmit()")
            playwright.expect(page.locator("#plan-request-error")).to_contain_text("still loading")
            assert posts == []
            assert held
            hold_bootstrap = False
            for route in held:
                route.fulfill(json=copy.deepcopy(data))
            playwright.expect(page.locator("#plan-create")).to_be_enabled()
            playwright.expect(page.locator("#plan-request-text")).to_have_value("Research portable meters")
            page.locator("#plan-create").click()
            playwright.expect(page.locator("#plan-create")).to_be_disabled()
            page.evaluate("loadBootstrap({quiet: true})")
            playwright.expect(page.locator("#plan-create")).to_be_disabled()
            assert held_create
            held_create[0].fulfill(status=403, json={"error": "Fixture create denied"})
            playwright.expect(page.locator("#plan-request-error")).to_be_visible()
            playwright.expect(page.locator("#plan-request-error")).to_contain_text("Fixture create denied")
            playwright.expect(page.locator("#plan-workspace")).to_be_hidden()
            playwright.expect(page.locator("#plan-create")).to_be_enabled()
            assert posts == ["plans"]
        finally:
            browser.close()
