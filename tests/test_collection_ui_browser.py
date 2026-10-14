"""Offline browser checks for the advanced recipe editor and issue ledger."""
from __future__ import annotations

import json
import threading

import pytest

from su_crawler.web_server import build_web_server
from test_web_browser import launch_browser


def test_collection_rules_persist_and_issues_are_actionable(tmp_path):
    playwright = pytest.importorskip("playwright.sync_api")
    workspace = tmp_path / "workspace"
    server = build_web_server(workspace, port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    url = "https://example.test/catalog"
    initial_rule = {url: {"recipe": [{"action": "click", "selector": "button.more"}], "selectors": {"price": ".price"}}}
    edited_rule = {url: {"recipe": [{"action": "click", "selector": "button.next"}], "selectors": {"price": ".price"},
                         "row_selector": ".item"}}
    plan = {"id": "plan-rules", "revision": 1, "state": "preview", "request_text": "Find parts",
            "summary": "Parts", "topic": {"industry": "Parts", "product": "Fixtures", "market": "US"},
            "categories": [], "include_terms": [], "exclude_terms": [], "conditions": [],
            "collection_rules": initial_rule, "excluded_urls": [],
            "candidates": [{"id": "candidate-1", "name": "Catalog", "url": url, "selected": True,
                            "origin": "user", "kind": "catalog"}]}
    job = {"id": "job-issues", "operation": "research_plan", "status": "succeeded", "attempt": 1,
           "created_at": "2026-10-14T00:00:00Z", "updated_at": "2026-10-14T00:01:00Z",
           "result": {"run_id": "run-issues", "output_dir": str(workspace / "outputs"),
                      "result": {"coverage": [{"url": url, "status": "partial", "collection_issues": []}]}}}
    batch_job = {"id": "job-batch-issues", "operation": "research_batch", "status": "succeeded",
                 "attempt": 1, "arguments": {"plan_id": "plan-rules"},
                 "created_at": "2026-10-14T00:02:00Z", "updated_at": "2026-10-14T00:03:00Z",
                 "result": {"execution_status": "succeeded"}}
    edits = []
    issue_updates = []
    recovery_requests = []
    issue_status = {"value": "needs_review"}

    def bootstrap(route):
        route.fulfill(json={"csrf_token": "test-token", "version": "test", "workspace_root": str(workspace),
                            "status": "needs_setup", "workspace": None, "research": None,
                            "research_plans": {"plans": [plan]}, "plan_jobs": [job, batch_job], "jobs": [],
                            "worker": {"status": "stopped"}, "mcp_available": False})

    def edit(route):
        request = route.request.post_data_json
        edits.append(request)
        plan.update(request["changes"])
        plan["revision"] += 1
        route.fulfill(json={"plan": plan})

    def issues(route):
        if route.request.method == "POST":
            issue_updates.append(route.request.post_data_json)
            issue_status["value"] = issue_updates[-1]["status"]
            route.fulfill(json={"status": issue_status["value"], "report_refresh": "pending"})
            return
        route.fulfill(json={"collection_issues": [{"id": "issue-1", "url": url,
            "section": "Offers", "status": issue_status["value"], "category": "browser_handoff",
            "reason": "More button stalled", "next_action": "Open in browser",
            "attempt_count": 2, "evidence_ref": "evidence/offers.html"}], "total": 1})

    def browser_recovery(route):
        recovery_requests.append(route.request.post_data_json)
        route.fulfill(json={"instructions": "Ask connected AI to inspect the approved page and report visible evidence.",
                            "url": url, "status": "prepared", "attempts_remaining": 2,
                            "automatic_solver_available": False})

    try:
        with playwright.sync_playwright() as driver:
            browser = launch_browser(driver)
            page = browser.new_page(viewport={"width": 1280, "height": 900})
            page.route("**/api/bootstrap", bootstrap)
            page.route("**/api/plans/edit", edit)
            page.route("**/api/jobs/job-issues/issues?*", issues)
            page.route("**/api/jobs/job-issues/issues", issues)
            page.route("**/api/jobs/job-issues/browser-recovery", browser_recovery)
            page.route("**/api/jobs/job-batch-issues/issues?*", lambda route: route.fulfill(json={
                "collection_issues": [{"id": "batch-issue", "url": url, "section": "Batch offers",
                                       "status": "open", "reason": "Batch browser handoff",
                                       "next_action": "Review batch page", "attempt_count": 1}], "total": 1}))
            page.route("**/api/jobs/job-issues/observations?*", lambda route: route.fulfill(json={
                "rows": [], "total": 0, "offset": 0, "limit": 50}))
            page.route("**/api/jobs/job-issues", lambda route: route.fulfill(json=job))
            page.route("**/api/jobs/job-batch-issues", lambda route: route.fulfill(json=batch_job))
            page.goto(f"{server.url}/#plan")
            editor = page.locator("#plan-collection-rules")
            playwright.expect(editor).to_have_value(json.dumps(initial_rule, indent=2))
            assert not page.locator("#plan-collection-rules-details").get_attribute("open")
            page.locator("#plan-collection-rules-details summary").click()
            editor.fill(json.dumps(edited_rule))
            page.locator("#plan-save").click()
            playwright.expect(editor).to_have_value(json.dumps(edited_rule, indent=2))
            assert edits[-1]["changes"]["collection_rules"] == edited_rule
            page.reload()
            playwright.expect(editor).to_have_value(json.dumps(edited_rule, indent=2))
            page.goto(f"{server.url}/#runs")
            page.locator('#job-list [data-job-id="job-issues"]').click()
            detail = page.locator("#job-detail")
            playwright.expect(detail).to_contain_text("Collection issues")
            playwright.expect(detail).to_contain_text("Open in browser")
            playwright.expect(detail).to_contain_text("More button stalled")
            playwright.expect(detail).to_contain_text("Needs review")
            playwright.expect(detail).to_contain_text("Showing 1–1 of 1 issues")
            playwright.expect(detail).to_contain_text("evidence/offers.html")
            detail.get_by_role("button", name="Prepare browser assistance").click()
            playwright.expect(detail).to_contain_text("Ask connected AI to inspect the approved page")
            playwright.expect(detail).to_contain_text("cannot launch the AI")
            assert detail.get_by_role("link", name="Open approved page").get_attribute("href") == url
            assert recovery_requests == [{"issue_id": "issue-1"}]
            page.evaluate("() => { Object.defineProperty(navigator, 'clipboard', {value: {writeText: () => Promise.reject(new Error('clipboard blocked'))}, configurable: true}); }")
            detail.get_by_role("button", name="Copy instructions").click()
            playwright.expect(detail).to_contain_text("Ask connected AI to inspect the approved page")
            detail.get_by_role("button", name="Reopen").click()
            playwright.expect(detail).to_contain_text("Mark for review")
            playwright.expect(detail).to_contain_text("Ask connected AI to inspect the approved page")
            playwright.expect(detail.locator(".collection-issue-row")).to_contain_text("Open")
            playwright.expect(detail).to_contain_text("Issue status was saved in SQLite")
            playwright.expect(detail).to_contain_text("Close the workbook")
            assert issue_updates == [{"issue_id": "issue-1", "status": "open"}]
            page.locator('#job-list [data-job-id="job-batch-issues"]').click()
            playwright.expect(detail).to_contain_text("Batch browser handoff")
            playwright.expect(detail).to_contain_text("Review batch page")
            assert detail.get_by_role("button", name="Prepare browser assistance").count() == 0
            browser.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
