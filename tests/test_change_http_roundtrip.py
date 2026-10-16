"""A real local HTTP catalog changes between collection runs; no model API.

Only the fixture DNS and its explicit internal authorization are injected.
HTTP fetching, parsing, comparison, SQLite, XLSX and the web endpoint are real.
"""
from collections import Counter
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import socket
import threading

from openpyxl import load_workbook

from su_crawler import catalog_collection, assistant_runtime as runtime
from su_crawler import collectors, research_plans as plans
from su_crawler.assistant_workspace import execute_job
from su_crawler.collection_changes import list_collection_changes
from su_crawler.models import FetchResult
from su_crawler.plan_jobs import queue_plan_job
from test_web_server import running_server, request, json_response


def test_real_http_price_addition_condition_and_failure(tmp_path, monkeypatch):
    state = {'changed': False, 'failure': False, 'requests': []}

    def product(sku, price, brand='Maker A'):
        return {'@type': 'Product', 'sku': sku, 'name': f'Test part {sku}', 'brand': brand,
                'offers': {'@type': 'Offer', 'price': price, 'priceCurrency': 'USD'}}

    class Catalog(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            state['requests'].append((state['changed'], self.path))
            if self.path == '/robots.txt':
                body, status = b'User-agent: *\nAllow: /\n', 200
            elif self.path == '/unavailable' and state['failure']:
                body, status = b'Temporary server failure', 503
            else:
                if self.path == '/unavailable':
                    products = [product('E', '50')]
                else:
                    products = [product('A', '120' if state['changed'] else '100'),
                                product('B', '200', 'Maker B' if state['changed'] else 'Maker A')]
                    if state['changed']:
                        products.append(product('D', '15'))
                body = ('<html><script type="application/ld+json">' + json.dumps(products) + '</script></html>').encode()
                status = 200
            self.send_response(status)
            self.send_header('Content-Type', 'text/plain' if self.path == '/robots.txt' else 'text/html')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    httpd = ThreadingHTTPServer(('127.0.0.1', 0), Catalog)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    original_dns = socket.getaddrinfo
    monkeypatch.setattr(socket, 'getaddrinfo', lambda host, port, *args, **kwargs:
        original_dns('127.0.0.1' if host in ('changes.example.test', b'changes.example.test') else host,
                     port, *args, **kwargs))

    def authorized_fixture(source, base_dir, backend):
        if backend != 'http':
            return FetchResult(source.id, 'tool_unavailable', backend, message='HTTP-only controlled experiment')
        assert source.location.startswith(f'http://changes.example.test:{httpd.server_port}/')
        return collectors.collect(replace(source, internal=True), base_dir, 'http')

    monkeypatch.setattr(catalog_collection, 'collect', authorized_fixture)
    try:
        base = f'http://changes.example.test:{httpd.server_port}'
        plan = plans.create_plan(tmp_path, 'Controlled local change verification')['plan']
        plan = plans.revise_plan(tmp_path, plan['id'], plan['revision'], {
            'topic': {'industry': 'Test fixtures', 'product': 'Parts', 'market': 'Test only'},
            'added_candidates': [{'url': base + path} for path in ('/catalog', '/unavailable')],
        })['plan']
        plan = plans.confirm_plan(tmp_path, plan['id'], plan['revision'], True)['plan']
        assert plan['refresh_policy']['mode'] == 'full'

        def run():
            job = queue_plan_job(tmp_path, 'research_plan', {
                'plan_id': plan['id'], 'expected_revision': plan['revision'], 'max_pages': 2})
            result = execute_job(tmp_path, 'research_plan', job['arguments'], job_id=job['id'])
            job['status'] = 'succeeded'
            runtime._write_json(runtime._job_path(tmp_path, job['id']), job)
            runtime._write_json(runtime._job_path(tmp_path, job['id']).with_name('result.json'), result)
            return job['id'], result

        first_id, first = run()
        initial = list_collection_changes(tmp_path, first_id)
        assert initial['counts']['baseline'] == 3 and initial['counts']['added'] == 0
        state['changed'] = True
        state['failure'] = True
        second_id, second = run()
        changed = list_collection_changes(tmp_path, second_id, status='changes')
        assert Counter(row['status'] for row in changed['items']) == {
            'price_changed': 1, 'conditions_changed': 1, 'added': 1, 'unavailable': 1}
        by_id = {row['identity']: row for row in changed['items']}
        assert by_id['sku:A']['before']['fields']['price'] == '100'
        assert by_id['sku:A']['after']['fields']['price'] == '120'
        assert by_id['sku:B']['changed_fields'] == ['brand']
        assert by_id['sku:E']['before']['fields']['price'] == '50' and by_id['sku:E']['after'] is None
        assert by_id['sku:D']['before'] is None
        assert all(row['previous_checked_at'] for row in changed['items'] if row['before'])
        assert first['result']['refresh']['effective_mode'] == second['result']['refresh']['effective_mode'] == 'full'
        with running_server(tmp_path) as ui:
            code, served = json_response(request(ui, 'GET', f'/api/jobs/{second_id}/changes?status=changes'))
            assert code == 200 and served == changed
        wb = load_workbook(second['report_path'], read_only=True, data_only=False)
        try:
            rows = list(wb['Item Changes'].values)
            assert len(rows) == 5
            price = next(row for row in rows[1:] if row[2] == 'sku:A')
            missing = next(row for row in rows[1:] if row[2] == 'sku:E')
            assert price[3:5] == ('100', '120')
            assert missing[3:5] == ('50', None)
        finally:
            wb.close()
        assert (False, '/catalog') in state['requests'] and (True, '/catalog') in state['requests']
        assert (True, '/unavailable') in state['requests']
        # A failure elsewhere must not make the successful page report the
        # same price change again when the unavailable page recovers.
        state['failure'] = False
        recovered_id, recovered = run()
        recovery = list_collection_changes(tmp_path, recovered_id)
        assert recovery['counts']['unchanged'] == 4
        assert recovery['counts']['price_changed'] == recovery['counts']['added'] == 0
        recovered_a = next(row for row in recovery['items'] if row['identity'] == 'sku:A')
        assert recovered_a['before']['fields']['price'] == '120'
        assert recovered_a['previous_checked_at'] == by_id['sku:A']['checked_at']
        (tmp_path / 'experiment-summary.json').write_text(json.dumps({
            'kind': 'controlled_local_http_not_external_market_change', 'requests': state['requests'],
            'initial_job': first_id, 'changed_job': second_id,
            'counts': changed['counts'], 'changes': changed['items'],
            'first_report': first['report_path'], 'changed_report': second['report_path'],
            'recovered_job': recovered_id, 'recovered_counts': recovery['counts'],
            'recovered_report': recovered['report_path'],
        }, ensure_ascii=False, indent=2), encoding='utf-8')
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=3)
