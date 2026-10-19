"""Offline browser checks for nonblocking collection attention."""
from __future__ import annotations

import threading

import pytest

from su_crawler.web_server import build_web_server
from test_web_browser import launch_browser


def test_attention_notification_and_persistent_list(tmp_path):
    playwright = pytest.importorskip("playwright.sync_api")
    server = build_web_server(tmp_path / "workspace", port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    items = [
        {"id": "one", "job_id": "job-one", "url": "https://shop.example.test/a", "category": "browser_handoff", "status": "awaiting_user", "action": "open_browser"},
        {"id": "two", "job_id": "job-two", "url": "https://shop.example.test/b", "category": "browser_handoff", "status": "awaiting_user", "action": "open_browser"},
        {"id": "three", "job_id": "job-three", "url": "javascript:alert(1)", "category": "authentication_required", "status": "awaiting_user", "action": "open_browser"},
    ]
    attention = {"items": items, "total": 3, "truncated": False, "collection_continues": True}
    bootstrap = {"csrf_token": "test", "version": "test", "workspace_root": str(tmp_path), "status": "needs_setup",
                 "workspace": None, "research": None, "jobs": [], "plan_jobs": [], "worker": {"status": "stopped"},
                 "mcp_available": False, "collection_attention": attention,
                 "research_plans": {"plans": [{"id": "p1", "refresh_policy": {"mode": "incremental", "full_interval_days": 7}}]}}
    receipt = {"id": "job-one", "operation": "research_plan", "status": "succeeded", "arguments": {"plan_id": "p1"},
               "result": {"result": {"refresh": {"policy": {"mode": "incremental", "full_interval_days": 7},
                   "effective_mode": "full", "reason": "first_run",
                   "counts": {"added": 2, "changed": 1, "unchanged": 3, "unavailable": 0, "removed_candidate": 0}}}}}
    try:
        with playwright.sync_playwright() as driver:
            browser = launch_browser(driver)
            page = browser.new_page(viewport={"width": 1280, "height": 850})
            errors = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.route("**/api/bootstrap", lambda route: route.fulfill(json=bootstrap))
            page.route("**/api/jobs/job-one", lambda route: route.fulfill(json=receipt))
            page.goto(f"{server.url}/#plan")
            panel = page.locator("#collection-attention")
            notices = page.locator(".attention-notification")
            playwright.expect(panel).to_be_visible()
            playwright.expect(notices).to_have_count(3)
            playwright.expect(panel.locator(".attention-group")).to_have_count(2)
            playwright.expect(panel).to_contain_text("Collection continues on other pages")
            assert panel.locator('a[href^="javascript:"]').count() == 0
            assert panel.locator('a[href="https://shop.example.test/a"]').get_attribute("rel") == "noopener noreferrer"
            assert page.locator("#plan-start").count() == 1
            assert page.locator("[role=dialog]").count() == 0
            assert len(browser.contexts[0].pages) == 1
            notices.first.get_by_role("button", name="Dismiss notification").click()
            playwright.expect(notices).to_have_count(2)
            playwright.expect(panel).to_contain_text("shop.example.test/a")
            page.reload()
            playwright.expect(panel).to_be_visible()
            playwright.expect(notices).to_have_count(0)
            page.locator('a[data-route="sources"]').click()
            playwright.expect(panel).to_be_visible()
            page.locator("#language-select").select_option("ko")
            playwright.expect(panel).to_contain_text("다른 페이지의 수집은 계속됩니다")
            page.locator("#language-select").select_option("ja")
            playwright.expect(panel).to_contain_text("他のページの収集は続行中です")
            page.locator("#language-select").select_option("en")
            panel.get_by_role("button", name="View run").first.focus()
            page.keyboard.press("Enter")
            playwright.expect(page).to_have_url(f"{server.url}/#runs")
            playwright.expect(page.locator("#job-detail")).to_contain_text("job-one")
            playwright.expect(page.locator("#job-detail")).to_contain_text("This run: Full recheck (first run)")
            playwright.expect(page.locator("#job-detail")).to_contain_text("Configured mode: Incremental check")
            refresh = receipt["result"]["result"]["refresh"]
            refresh.update(effective_mode="incremental", reason="scheduled_incremental")
            page.evaluate("() => refreshSelectedJob()")
            playwright.expect(page.locator("#job-detail")).to_contain_text("This run: Incremental check (scheduled incremental check)")
            refresh.update(effective_mode="mixed", reason=None,
                           reasons={"first_run": 1, "scheduled_incremental": 2})
            page.evaluate("() => refreshSelectedJob()")
            playwright.expect(page.locator("#job-detail")).to_contain_text("This run: Mixed full and incremental checks")
            playwright.expect(page.locator("#job-detail")).to_contain_text("Batch reasons: first run: 1 · scheduled incremental check: 2")
            page.locator("#language-select").select_option("ko")
            playwright.expect(page.locator("#job-detail")).to_contain_text("배치별 사유: 첫 실행: 1건 · 예정된 증분 확인: 2건")
            page.locator("#language-select").select_option("ja")
            playwright.expect(page.locator("#job-detail")).to_contain_text("バッチ別の理由: 初回実行: 1件 · 予定された差分確認: 2件")
            page.locator("#language-select").select_option("en")
            attention["items"] = [items[1]]
            attention["total"] = 1
            attention["collection_continues"] = False
            page.evaluate("() => loadBootstrap({quiet: true})")
            playwright.expect(panel.locator(".attention-row")).to_have_count(1)
            playwright.expect(panel).not_to_contain_text("Collection continues on other pages")
            attention["items"] = []
            attention["total"] = 0
            page.evaluate("() => loadBootstrap({quiet: true})")
            playwright.expect(panel).to_be_hidden()
            assert not errors
            browser.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
