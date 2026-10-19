"""Offline browser coverage for durable product comparisons in run details."""
from urllib.parse import parse_qs, urlsplit

import pytest

from test_web_browser import launch_browser
from test_web_server import running_server


def comparison(status, number=0):
    before = {'fields': {'name': 'Part', 'sku': f'SKU-{number}', 'price': '100.00', 'currency': 'USD', 'min_order': '1'}, 'specs': {'size': 'M'}}
    after = {'fields': {**before['fields'], 'price': '120.00', 'min_order': '10'}, 'specs': {'size': 'M'}}
    if status in ('baseline', 'added'):
        before = None
    if status in ('unavailable', 'removed_candidate'):
        after = None
    if status == 'conditions_changed':
        after['fields']['price'] = '100.00'
        after['fields']['name'] = 'Renamed part'
    if status == 'unchanged':
        after = before
    return {'status': status, 'product_name': f'<img src=x onerror=alert(1)> Part {number}', 'identity': f'sku:SKU-{number}',
            'url': 'javascript:alert(1)' if status == 'unavailable' else 'https://example.test/catalog',
            'before': before, 'after': after, 'changed_fields': ['price', 'min_order'] if status in ('price_changed', 'price_and_conditions_changed') else ['name'] if status == 'conditions_changed' else [],
            'condition_comparison': 'observed_fields_only' if status == 'price_and_conditions_changed' else None,
            'reason': {'baseline': 'first_successful_capture', 'added': 'new_observed_item',
                       'unavailable': 'not_observed_in_current_capture', 'removed_candidate': 'absent_from_complete_inventory_review_required',
                       'unchanged': 'same_observed_fields', 'conditions_changed': 'observed_conditions_changed',
                       'price_changed': 'observed_price_or_currency_changed',
                       'price_and_conditions_changed': 'observed_price_and_conditions_changed'}[status],
            'previous_checked_at': '2026-10-01T01:00:00Z' if before else None,
            'checked_at': '2026-10-02T01:00:00Z'}


@pytest.mark.parametrize('legacy', [False, '404', 'unavailable'])
def test_product_changes_browser_languages_filter_paging_and_safe_evidence(tmp_path, legacy):
    playwright = pytest.importorskip('playwright.sync_api')
    statuses = ['baseline', 'added', 'price_and_conditions_changed', 'conditions_changed',
                'unavailable', 'removed_candidate', 'unchanged']
    items = [comparison('price_changed', number) for number in range(51)]
    items += [comparison(status, 51 + number) for number, status in enumerate(statuses)]
    # Missing currency is deliberately not supplied from the previous observation.
    items[0]['after']['fields']['currency'] = ''
    refresh = {'policy': {'mode': 'full'}, 'effective_mode': 'full', 'reason': 'manual_full',
               'counts': {}, 'item_counts': {'price_changed': 51, **{status: 1 for status in statuses}},
               'item_changes': items[:2] if legacy else items[:1], 'item_changes_total': len(items),
               'item_changes_truncated': True, 'next_full_due_at': '2026-11-01T00:00:00Z'}
    job = {'id': 'job-changes', 'operation': 'collect_sites', 'status': 'succeeded', 'arguments': {}, 'result': {'refresh': refresh}}
    bootstrap = {'csrf_token': 'test', 'version': 'test', 'workspace_root': str(tmp_path), 'status': 'needs_setup',
                 'workspace': None, 'research': None, 'jobs': [job], 'plan_jobs': [], 'worker': {'status': 'stopped'}, 'mcp_available': False}
    requests = []

    def route_api(route):
        request = route.request
        path = urlsplit(request.url).path
        requests.append((request.method, path))
        if path == '/api/bootstrap':
            route.fulfill(json=bootstrap)
        elif path == '/api/jobs/job-changes':
            route.fulfill(json=job)
        elif path == '/api/jobs/job-changes/changes' and not legacy:
            query = parse_qs(urlsplit(request.url).query)
            status = query.get('status', ['all'])[0]
            filtered = [item for item in items if status == 'all' or item['status'] == status]
            offset, limit = int(query['offset'][0]), int(query['limit'][0])
            route.fulfill(json={'items': filtered[offset:offset + limit], 'total': len(filtered), 'offset': offset, 'limit': limit})
        elif path == '/api/jobs/job-changes/changes' and legacy == 'unavailable':
            route.fulfill(json={'items': [], 'total': 0, 'offset': 0, 'limit': 50, 'available': False, 'missing_runs': 1})
        else:
            route.fulfill(status=404, json={'error': 'Fixture endpoint unavailable'})

    with running_server(tmp_path) as server, playwright.sync_playwright() as driver:
        browser = launch_browser(driver)
        try:
            page = browser.new_page(viewport={'width': 1280, 'height': 900})
            errors = []
            page.on('pageerror', lambda error: errors.append(str(error)))
            page.route('**/api/**', route_api)
            page.goto(server.url + '/#runs')
            page.locator('[data-job-id="job-changes"]').click()
            section = page.locator('.item-changes')
            playwright.expect(section).to_be_visible()
            playwright.expect(section).to_contain_text('120.00 · Not observed')
            playwright.expect(section).to_contain_text('100.00 · USD')
            playwright.expect(section).to_contain_text('Minimum order: 1')
            playwright.expect(section).to_contain_text('Minimum order: 10')
            playwright.expect(section).to_contain_text('<img src=x onerror=alert(1)>')
            assert section.locator('img').count() == 0
            assert page.locator('.refresh-summary').get_by_text('Next full recheck due:', exact=False).count() == 0
            assert 'Pages —' not in page.locator('.refresh-summary').inner_text()
            first_before = section.locator('tbody tr').first.locator('td').nth(1).inner_text()
            assert 'SKU-0' not in first_before
            assert 'Product name:' not in first_before
            assert section.locator('a').first.get_attribute('rel') == 'noopener noreferrer'
            if legacy:
                playwright.expect(section).to_contain_text('product list is truncated')
                if legacy == 'unavailable':
                    playwright.expect(section).to_contain_text('Durable product history is unavailable')
                playwright.expect(section.locator('tbody tr')).to_have_count(2)
            else:
                playwright.expect(section.locator('tbody tr')).to_have_count(50)
                section.locator('[data-change-page="next"]').click()
                playwright.expect(section.locator('tbody tr')).to_have_count(8)
                playwright.expect(section).to_contain_text('51–58 of 58')
                for status in statuses:
                    section.locator('[data-change-filter]').select_option(status)
                    playwright.expect(section.locator('tbody tr')).to_have_count(1)
                    if status == 'conditions_changed':
                        playwright.expect(section.locator('tbody tr td').nth(1)).to_contain_text('Part')
                        playwright.expect(section.locator('tbody tr td').nth(2)).to_contain_text('Renamed part')
                    if status == 'price_and_conditions_changed':
                        for language, label, condition in [
                            ('ko', '가격과 관측 조건 함께 변경', '기록된 조건 항목만 비교했습니다'),
                            ('ja', '価格と観測条件がともに変更', '記録された条件のみ比較しました'),
                            ('en', 'Price and observed conditions changed', 'Only recorded condition fields were compared'),
                        ]:
                            page.locator('#language-select').select_option(language)
                            playwright.expect(section.locator('.change-status')).to_have_text(label)
                            playwright.expect(section).to_contain_text(condition)
                section.locator('[data-change-filter]').select_option('unavailable')
                assert section.locator('a[href^="javascript:"]').count() == 0
                playwright.expect(section).to_contain_text('removal is not confirmed')
                playwright.expect(section).to_contain_text('Check attempted (no product observation):')
                for language, expected in [('ko', '확인 불가'), ('ja', '確認不可'), ('en', 'Unable to verify')]:
                    page.locator('#language-select').select_option(language)
                    playwright.expect(section.locator('.change-status')).to_have_text(expected)
                    assert 'app.change' not in section.inner_text()
                for width in (375, 768, 1280):
                    page.set_viewport_size({'width': width, 'height': 900})
                    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth'), width
            section.locator('[data-change-filter]').select_option('added' if legacy else 'price_changed')
            if legacy:
                playwright.expect(section).to_contain_text('No products match this status')
            if not legacy:
                items[:] = [comparison('unchanged', 99)]
                section.locator('[data-change-filter]').select_option('changes')
                playwright.expect(section).to_contain_text('No product changes were observed')
                section.locator('[data-change-filter]').select_option('all')
                playwright.expect(section.locator('tbody tr')).to_have_count(1)
                playwright.expect(section.locator('.change-status')).to_have_text('Unchanged')
            refresh.pop('item_counts')
            page.evaluate('() => refreshSelectedJob()')
            playwright.expect(page.locator('.refresh-summary')).to_contain_text('Pages — added 0')
            assert not [request for request in requests if request[0] == 'POST']
            assert not errors
        finally:
            browser.close()
