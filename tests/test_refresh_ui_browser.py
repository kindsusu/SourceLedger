"""Repeat settings travel from the actual browser form into the reviewed plan."""
import pytest

from su_crawler import research_plans as plans
from test_web_browser import launch_browser
from test_web_server import running_server


def test_browser_updates_repeat_policy_without_starting_collection(tmp_path):
    playwright = pytest.importorskip('playwright.sync_api')
    plan = plans.create_plan(tmp_path, 'Inspect fasteners')['plan']
    plan = plans.revise_plan(tmp_path, plan['id'], plan['revision'], {
        'topic': {'industry': 'Parts', 'product': 'Fasteners', 'market': 'Japan'},
        'added_candidates': [{'url': 'https://example.test/catalog', 'kind': 'category'}],
    })['plan']
    with running_server(tmp_path) as server, playwright.sync_playwright() as driver:
        browser = launch_browser(driver)
        try:
            page = browser.new_page(viewport={'width': 1280, 'height': 900})
            page.goto(server.url + '/#plan')
            playwright.expect(page.locator('#plan-refresh-mode')).to_have_value('full')
            playwright.expect(page.locator('#plan-refresh-days')).to_be_hidden()
            playwright.expect(page.locator('#plan-refresh-days')).to_be_disabled()
            page.locator('#plan-refresh-mode').select_option('incremental')
            playwright.expect(page.locator('#plan-refresh-days')).to_be_visible()
            playwright.expect(page.locator('#plan-refresh-days')).to_have_value('7')
            page.locator('#plan-refresh-days').fill('14')
            page.locator('#plan-save').click()
            playwright.expect(page.locator('#plan-save')).to_be_enabled()
            assert plans.get_plan(tmp_path, plan['id'])['plan']['refresh_policy'] == {
                'mode': 'incremental', 'full_interval_days': 14}
            page.reload()
            playwright.expect(page.locator('#plan-refresh-mode')).to_have_value('incremental')
            playwright.expect(page.locator('#plan-refresh-days')).to_have_value('14')
            page.locator('#plan-refresh-mode').select_option('full')
            page.locator('#plan-save').click()
            playwright.expect(page.locator('#plan-save')).to_be_enabled()
            stored = plans.get_plan(tmp_path, plan['id'])['plan']
            assert stored['refresh_policy']['mode'] == 'full'
            assert stored['state'] == 'preview'
            page.reload()
            playwright.expect(page.locator('#plan-refresh-mode')).to_have_value('full')
            playwright.expect(page.locator('#plan-refresh-days')).to_have_value('14')
            from su_crawler.assistant_runtime import list_jobs
            assert not list_jobs(tmp_path)
        finally:
            browser.close()
