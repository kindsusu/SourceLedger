"""UI translations must not change evidence, requests, or unsaved user choices."""
from __future__ import annotations

import copy
from pathlib import Path
import re

import pytest

from test_web_browser import launch_browser
from test_web_server import running_server


@pytest.fixture
def localized_ui(tmp_path):
    playwright = pytest.importorskip("playwright.sync_api")
    with running_server(tmp_path) as server, playwright.sync_playwright() as driver:
        browser = launch_browser(driver)
        context = browser.new_context(viewport={"width": 1440, "height": 1000}, locale="ja-JP")
        page = context.new_page()
        page.set_default_timeout(8000)
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        yield page, server.url, playwright.expect, errors
        browser.close()


def test_language_setup_persistence_and_storage_fallback(localized_ui):
    page, url, expect, errors = localized_ui
    page.goto(f"{url}/#overview")
    expect(page.get_by_label("Language", exact=True)).to_have_value("en")
    expect(page.get_by_role("button", name="Create workspace")).to_be_visible()
    page.locator('#setup-form input[name="industry"]').fill("Original Industry 산업")
    page.locator("#language-select").select_option("ko")
    expect(page.locator("html")).to_have_attribute("lang", "ko")
    expect(page.locator("#setup-form")).to_contain_text("산업")
    expect(page.locator('#setup-form input[name="industry"]')).to_have_value("Original Industry 산업")
    assert page.evaluate("localStorage.getItem('sourceledger.ui.language')") == "ko"
    page.reload()
    expect(page.locator("#language-select")).to_have_value("ko")
    expect(page.locator("html")).to_have_attribute("lang", "ko")
    page.locator("#language-select").select_option("ja")
    expect(page.locator("#setup-form")).to_contain_text("業界")
    assert page.get_by_label("言語", exact=True).count() == 1
    for width in (320, 375, 768, 1440):
        page.set_viewport_size({"width": width, "height": 1000})
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth"), width
    # A preference set by another tab updates this tab without reloading its form.
    page.locator('#setup-form input[name="product"]').fill("Untouched product 製品")
    other = page.context.new_page()
    other.goto(url)
    other.locator("#language-select").select_option("en")
    expect(page.locator("html")).to_have_attribute("lang", "en")
    expect(page.locator('#setup-form input[name="product"]')).to_have_value("Untouched product 製品")
    other.close()
    page.evaluate("localStorage.setItem('sourceledger.ui.language', 'unsupported')")
    page.reload()
    expect(page.locator("html")).to_have_attribute("lang", "en")
    page.add_init_script("Object.defineProperty(window, 'localStorage', {get() {throw new Error('Storage disabled');}})")
    page.reload()
    page.locator("#language-select").select_option("ja")
    expect(page.locator("html")).to_have_attribute("lang", "ja")
    assert errors == []


def test_switch_preserves_forms_candidates_and_machine_values(localized_ui, tmp_path):
    page, url, expect, errors = localized_ui
    record = {
        "id": "request-1", "kind": "keyword", "query": "Original query 日本語",
        "status": "ready", "created_at": "2026-10-01T00:00:00Z", "note": "Original evidence note",
        "candidates": [{"id": "candidate-1", "name": "Ready", "url": "https://example.invalid/candidate",
                        "evidence_url": "https://example.invalid/about", "reason": "Original source text", "source_id": None}],
    }
    data = {
        "csrf_token": "test-token", "status": "configured", "version": "test",
        "workspace_root": str(tmp_path), "worker": {"status": "stopped"},
        "jobs": [], "recommendation_jobs": [], "mcp_available": True,
        "workspace": {"industry": "Original Industry", "market": "Offline", "product": {
            "name": "Original Product", "identifiers": {"model": "PART-1"}, "required_specs": {},
        }, "sources": [{"id": "source-1", "name": "Public", "location": "https://example.invalid/source",
                         "scope": "public", "kind": "web", "status": "candidate"}]},
        "recommendations": {"requests": [record]},
        "ai": {"settings": {"provider": "codex", "model": "", "timeout_seconds": 180},
               "providers": [{"id": "codex", "label": "Codex CLI", "available": True,
                              "models": [{"id": "model-exact", "label": "Model Exact"}]}]},
    }
    job = {"id": "job-1", "operation": "agent", "status": "succeeded", "attempt": 1,
           "created_at": "2026-10-01T00:00:00Z", "updated_at": "2026-10-01T00:01:00Z",
           "result": {"run_id": "run-original", "output_dir": "outputs", "evidence_status": "needs_review"}}
    data["jobs"] = [job]
    observations = {"total": 1, "offset": 0, "limit": 50, "rows": [{
        "source_name": "Observed", "source_url": "https://example.invalid/evidence",
        "value_origin": "observed", "amount": "1000.50", "currency": "USD", "status": "needs_review",
        "collected_at": "2026-10-01T00:00:00Z",
    }]}
    captured = []

    def route_api(route):
        call = route.request
        path = call.url.split("/api/", 1)[1]
        if call.method == "GET" and path == "bootstrap":
            route.fulfill(json=copy.deepcopy(data))
        elif call.method == "GET" and path == "jobs/job-1":
            route.fulfill(json=job)
        elif call.method == "GET" and path.startswith("jobs/job-1/observations?"):
            route.fulfill(json=observations)
        elif call.method == "POST":
            captured.append((path, call.post_data_json))
            if path == "ai/settings":
                data["ai"]["settings"] = call.post_data_json
                route.fulfill(json=call.post_data_json)
            elif path == "connections":
                route.fulfill(json={"snippet": '{"command":"sourceledger","args":["serve-assistant"]}'})
            else:
                route.fulfill(status=400, json={"error": "A fixture error: <script>bad()</script>"})
        else:
            route.fulfill(status=404, json={"error": "Unexpected fixture request"})

    page.route("**/api/**", route_api)
    page.goto(f"{url}/#sources")
    candidate = page.locator('[data-candidate-id="candidate-1"]')
    source = page.locator('[data-run-source-id="source-1"]')
    expect(candidate).to_be_visible()
    candidate.check()
    source.uncheck()
    page.locator('#source-form input[name="name"]').fill("Unsaved supplier")
    page.locator('#source-form input[name="url"]').fill("https://unsaved.invalid")
    page.locator('#source-form select[name="scope"]').select_option("internal")
    page.locator('#recommendation-form input[name="query"]').fill("Unsaved keyword")
    identity_value = page.locator("#identifier-rows .key-value-row").first.locator("input").nth(1)
    identity_value.fill("UNSAVED-MODEL")
    page.locator(".handoff details summary").click()
    for language in ("ko", "ja", "en"):
        page.locator("#language-select").select_option(language)
        expect(candidate).to_be_checked()
        expect(source).not_to_be_checked()
        expect(page.locator('#source-form input[name="name"]')).to_have_value("Unsaved supplier")
        expect(page.locator('#source-form input[name="url"]')).to_have_value("https://unsaved.invalid")
        expect(page.locator('#source-form select[name="scope"]')).to_have_value("internal")
        expect(page.locator('#recommendation-form input[name="query"]')).to_have_value("Unsaved keyword")
        expect(identity_value).to_have_value("UNSAVED-MODEL")
        expect(page.locator(".handoff details")).to_have_attribute("open", "")
        expect(page.locator(".candidate-item strong")).to_have_text("Ready")
        expect(page.locator(".source-item h4")).to_have_text("Public")
        expect(page.locator(".recommendation-request h4")).to_have_text("Original query 日本語")
        expect(page.locator(".handoff")).to_contain_text("submit_source_recommendations")
        assert page.url.endswith("#sources")
    assert captured == [], "Switching languages must not submit work or call a model"

    page.locator('nav a[data-route="connections"]').click()
    page.locator("#ai-model-choice").select_option("__custom__")
    page.locator("#ai-custom-model").fill("exact-custom-model")
    page.locator(".ai-advanced summary").click()
    page.locator("#ai-timeout").fill("240")
    page.locator("#connection-client").select_option("claude-code")
    page.locator('#connection-form button[type="submit"]').click()
    expect(page.locator("#connection-snippet")).to_contain_text("serve-assistant")
    snippet = page.locator("#connection-snippet").inner_text()
    expect(page.locator("#toast-region")).to_contain_text("Connection snippet generated.")
    page.locator("#language-select").select_option("ko")
    expect(page.locator("#toast-region")).not_to_contain_text("Connection snippet generated.")
    expect(page.locator("#ai-custom-model")).to_have_value("exact-custom-model")
    expect(page.locator("#ai-model-choice")).to_have_value("__custom__")
    expect(page.locator("#ai-timeout")).to_have_value("240")
    expect(page.locator(".ai-advanced")).to_have_attribute("open", "")
    expect(page.locator("#connection-client")).to_have_value("claude-code")
    expect(page.locator("#connection-snippet")).to_have_text(snippet)
    page.locator('#ai-settings-form button[type="submit"]').click()
    expect(page.locator('#ai-settings-form button[type="submit"]')).to_be_enabled()
    assert captured[-1] == ("ai/settings", {"provider": "codex", "model": "exact-custom-model", "timeout_seconds": 240})

    # Visible validation is retranslated, while input values survive an app/storage event.
    page.locator("#ai-timeout").fill("29")
    page.locator('#ai-settings-form button[type="submit"]').click()
    alert = page.locator("#ai-settings-form [data-form-error]")
    expect(alert).to_be_visible()
    korean_error = alert.inner_text()
    page.locator("#ai-timeout").focus()
    page.evaluate("SourceLedgerI18n.setLanguage('ja')")
    expect(page.locator("#ai-timeout")).to_be_focused()
    expect(page.locator("#ai-timeout")).to_have_value("29")
    expect(alert).not_to_have_text(korean_error)
    expect(alert).to_contain_text("30")

    page.locator('nav a[data-route="runs"]').click()
    page.locator('[data-job-id="job-1"]').click()
    expect(page.locator("#job-detail table tbody tr")).to_have_count(1)
    page.locator("#runs-view details.advanced:has(#advanced-form) > summary").click()
    page.locator("#advanced-operation").select_option("run")
    page.locator('#advanced-fields input[name="config_path"]').fill("configs/untouched.json")
    page.locator('#advanced-fields input[name="max_tasks"]').fill("7")

    for language in ("ko", "ja"):
        page.locator("#language-select").select_option(language)
        for screen in ("overview", "sources", "runs", "connections"):
            page.locator(f'nav a[data-route="{screen}"]').click()
            for width in (375, 768, 1440):
                page.set_viewport_size({"width": width, "height": 1000})
                assert page.evaluate("document.documentElement.scrollWidth <= innerWidth"), (language, screen, width)
                if width in (375, 1440):
                    page.screenshot(path=str(tmp_path / f"{language}-{screen}-{width}.png"), full_page=True)
            if screen == "runs":
                expect(page.locator('#advanced-fields input[name="config_path"]')).to_have_value("configs/untouched.json")
                expect(page.locator('#advanced-fields input[name="max_tasks"]')).to_have_value("7")
                cells = page.locator("#job-detail table tbody tr").first.locator("td")
                expect(cells.nth(2)).to_have_text("Observed")
                expect(cells.nth(3)).to_have_text("1000.50")
                expect(cells.nth(4)).to_have_text("—")
                expect(cells.nth(5)).to_have_text("USD")
                expect(cells.nth(7)).not_to_have_text("needs_review")
    assert errors == []


def test_catalogs_cover_all_languages_and_interpolation(localized_ui):
    page, _, _, errors = localized_ui
    web = Path(__file__).resolve().parents[1] / "su_crawler" / "web"
    # Inspect registration data in an isolated blank document, independent of app state.
    page.evaluate("window.catalogs = []; window.SourceLedgerI18n = {register: value => catalogs.push(value)}")
    for name in ("static-messages.js", "app-messages.js", "plan-messages.js"):
        page.add_script_tag(path=str(web / name))
    catalogs = page.evaluate("catalogs")
    for catalog in catalogs:
        assert set(catalog) == {"en", "ko", "ja"}
        assert set(catalog["en"]) == set(catalog["ko"]) == set(catalog["ja"])
        for key, english in catalog["en"].items():
            for locale in ("ko", "ja"):
                assert catalog[locale][key].strip(), (locale, key)
                assert set(re.findall(r"\{\w+\}", english)) == set(re.findall(r"\{\w+\}", catalog[locale][key])), (locale, key)
    static = (web / "index.html").read_text(encoding="utf-8")
    keys = {key for catalog in catalogs for key in catalog["en"]}
    for key in re.findall(r'data-i18n(?:-placeholder|-aria-label|-title)?="([^"]+)"', static):
        assert key in keys, key
    dynamic = (web / "app.js").read_text(encoding="utf-8")
    for key in re.findall(r'(?<![\w.])t\("([^"]+)"', dynamic):
        assert f"app.{key}" in keys, key
    page.add_script_tag(path=str(web / "i18n.js"))
    page.evaluate("""() => {
      SourceLedgerI18n.register({en: {'only.english':'Fallback {name}'}, ja: {'greeting':'名前: {name}'}});
      SourceLedgerI18n.initialize();
      SourceLedgerI18n.setLanguage('ja');
    }""")
    assert page.evaluate("SourceLedgerI18n.t('only.english', {name:'$& <script>'})") == "Fallback $& <script>"
    assert page.evaluate("SourceLedgerI18n.t('missing.key')") == "missing.key"
    assert page.evaluate("SourceLedgerI18n.getLocale()") == "ja-JP"
    assert errors == []


def test_language_change_during_requests_and_errors(localized_ui, tmp_path):
    page, url, expect, errors = localized_ui
    job = {"id": "job-pending", "operation": "agent", "status": "interrupted", "result": {}}
    data = {
        "csrf_token": "test", "status": "configured", "workspace_root": str(tmp_path),
        "worker": {"status": "stopped"}, "jobs": [job], "mcp_available": True,
        "workspace": {"industry": "Fixtures", "market": "Offline", "product": {
            "name": "Test part", "identifiers": {"model": "PART-1"}, "required_specs": {},
        }, "sources": [{"id": "source-1", "name": "Fixture", "location": "https://example.invalid"}]},
        "recommendations": {"requests": [{"id": "request-1", "kind": "keyword", "query": "Fixtures",
            "status": "ready", "candidates": [{"id": "candidate-1", "name": "Fixture",
            "url": "https://example.invalid", "evidence_url": "https://example.invalid/about"}]}]},
    }
    pending = []
    bootstrap_error = {"message": None}

    def route_api(route):
        path = route.request.url.split("/api/", 1)[1]
        if route.request.method == "POST":
            pending.append(route)  # Hold the response while the operator switches language.
        elif path == "bootstrap":
            if bootstrap_error["message"]:
                route.fulfill(status=503, json={"error": bootstrap_error["message"]})
            else:
                route.fulfill(json=copy.deepcopy(data))
        elif path == "jobs/job-pending":
            route.fulfill(json=job)
        else:
            route.fulfill(status=404, json={"error": "File not found"})

    page.route("**/api/**", route_api)
    page.goto(f"{url}/#sources")
    discover = page.locator('[data-source-action="discover"]')
    discover.click()
    page.locator("#language-select").select_option("ja")
    expect(discover).to_be_disabled()
    discover.evaluate("button => button.click()")
    assert len(pending) == 1
    pending.pop().fulfill(status=400, json={"error": "Fixture detail: <b>original</b>"})
    expect(discover).to_be_enabled()
    expect(page.locator("#toast-region")).to_contain_text("エラー")
    expect(page.locator("#toast-region")).to_contain_text("<b>original</b>")
    assert page.locator("#toast-region b").count() == 0

    page.locator('[data-candidate-id="candidate-1"]').check()
    add = page.locator('[data-add-request="request-1"]')
    add.click()
    page.locator("#language-select").select_option("ko")
    expect(add).to_be_disabled()
    assert len(pending) == 1
    pending.pop().fulfill(status=500, json={"error": "Internal server error"})
    expect(add).to_be_enabled()
    expect(page.locator("#toast-region")).to_contain_text("서버 내부 오류")
    page.locator("#language-select").select_option("en")
    expect(page.locator("#toast-region")).to_contain_text("Internal server error")

    page.locator('nav a[data-route="runs"]').click()
    page.locator("#runs-view details.advanced:has(#advanced-form) > summary").click()
    page.locator("#advanced-operation").select_option("run")
    config = page.locator('#advanced-fields input[name="config_path"]')
    config.fill("configs/original.json")
    page.locator('#advanced-form button[type="submit"]').click()
    page.locator("#language-select").select_option("ja")
    expect(config).to_have_value("configs/original.json")
    expect(config).to_be_disabled()
    assert len(pending) == 1
    pending.pop().fulfill(status=404, json={"error": "File not found"})
    expect(config).to_be_enabled()
    expect(page.locator("#advanced-form [data-form-error]")).to_contain_text("ファイル")
    page.locator("#language-select").select_option("ko")
    expect(page.locator("#advanced-form [data-form-error]")).to_contain_text("파일을 찾을 수 없습니다")

    page.locator('[data-job-id="job-pending"]').click()
    resume = page.locator('[data-resume-job="job-pending"]')
    resume.click()
    page.locator("#language-select").select_option("ja")
    expect(resume).to_be_disabled()
    assert len(pending) == 1
    pending.pop().fulfill(status=500, json={"error": "Internal server error"})
    expect(resume).to_be_enabled()

    # A failure before bootstrap must still allow language selection, not worker execution.
    bootstrap_error["message"] = "Fixture offline"
    page.reload()
    expect(page.locator("#global-message")).to_be_visible()
    expect(page.locator("#worker-toggle")).to_be_disabled()
    page.locator("#language-select").select_option("ko")
    expect(page.locator("#global-message-text")).to_have_text("오류: Fixture offline")
    expect(page.locator("#worker-toggle")).to_be_disabled()
    page.locator("#language-select").select_option("en")
    expect(page.locator("#global-message-text")).to_have_text("Error: Fixture offline")
    assert errors == []
