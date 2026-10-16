"""Full durable comparisons, not truncated receipts, drive web and MCP pages."""
import asyncio
import json

import pytest

from su_crawler import assistant_runtime as runtime
from su_crawler.catalog_refresh import save_refresh_run
from su_crawler.collection_changes import list_collection_changes
from test_web_server import running_server, request, json_response


def change_job(root, *, rows=125):
    job_id = 'd' * 32
    output = root / 'catalog-prices' / 'plan-test'
    items = [{'status': 'price_changed' if i % 2 else 'added', 'product_name': f'Part {i}',
              'url': f'https://example.test/{i}', 'before': None,
              'after': {'fields': {'name': f'Part {i}', 'price': str(i), 'currency': 'USD'}, 'specs': {}}}
             for i in range(rows)]
    save_refresh_run(output, 'run-1', {'item_changes': items})
    job = {'id': job_id, 'operation': 'research_plan', 'status': 'succeeded', 'arguments': {},
           'created_at': '2026-10-16T00:00:00+00:00'}
    runtime._write_json(runtime._job_path(root, job_id), job)
    runtime._write_json(runtime._job_path(root, job_id).with_name('result.json'),
                        {'run_id': 'run-1', 'output_dir': str(output),
                         'result': {'refresh': {'item_changes': items[:1], 'item_changes_truncated': rows - 1}}})
    return job_id, output


def test_durable_pagination_and_filter_do_not_lose_truncated_items(tmp_path):
    job_id, _ = change_job(tmp_path)
    page = list_collection_changes(tmp_path, job_id, offset=100, limit=50)
    assert page['available'] and page['missing_runs'] == 0
    assert page['total'] == 125 and len(page['items']) == 25 and not page['has_more']
    assert page['items'][0]['product_name'] == 'Part 100'
    filtered = list_collection_changes(tmp_path, job_id, status='price_changed', offset=50, limit=50)
    assert filtered['total'] == 62 and len(filtered['items']) == 12
    assert filtered['total_all'] == 125 and filtered['counts']['added'] == 63
    assert {item['status'] for item in filtered['items']} == {'price_changed'}


@pytest.mark.parametrize('kwargs', [{'offset': -1}, {'offset': True}, {'limit': 0}, {'limit': 201},
                                    {'limit': True}, {'status': 'deleted'}])
def test_invalid_change_pages_rejected(tmp_path, kwargs):
    with pytest.raises(ValueError):
        list_collection_changes(tmp_path, 'd' * 32, **kwargs)


def test_legacy_run_is_not_presented_as_no_changes(tmp_path):
    job_id, output = change_job(tmp_path)
    save_refresh_run(output, 'run-1', {'changes': [], 'counts': {'unchanged': 125}})
    value = list_collection_changes(tmp_path, job_id)
    assert value['available'] is False and value['missing_runs'] == 1
    assert value['items'] == []


def test_batch_changes_read_all_run_ledgers(tmp_path):
    job_id, output = change_job(tmp_path)
    save_refresh_run(output, 'run-2', {'item_changes': [{'status': 'unavailable', 'before': None, 'after': None}]})
    job_path = runtime._job_path(tmp_path, job_id)
    job = json.loads(job_path.read_text(encoding='utf-8'))
    job.update(operation='research_batch', arguments={'plan_id': 'plan-test'})
    runtime._write_json(job_path, job)
    runtime._write_json(output / 'batch-runs' / f'{job_id}.json',
                        {'schema': 'source-ledger/research-batch/v1', 'job_id': job_id,
                         'batches': [{'run_id': 'run-1'}, {'run_id': 'run-2'}, {'run_id': 'run-1'}]})
    value = list_collection_changes(tmp_path, job_id, status='unavailable')
    assert value['total_all'] == 126 and value['total'] == 1
    assert value['items'][0]['run_id'] == 'run-2'


def test_output_escape_is_rejected(tmp_path):
    job_id, _ = change_job(tmp_path)
    runtime._write_json(runtime._job_path(tmp_path, job_id).with_name('result.json'),
                        {'run_id': 'run-1', 'output_dir': str(tmp_path.parent)})
    with pytest.raises(ValueError):
        list_collection_changes(tmp_path, job_id)


def test_web_and_mcp_share_filtered_change_values(tmp_path):
    job_id, _ = change_job(tmp_path)
    expected = list_collection_changes(tmp_path, job_id, status='added', offset=60, limit=10)
    with running_server(tmp_path) as server:
        code, page = json_response(request(server, 'GET', f'/api/jobs/{job_id}/changes?status=added&offset=60&limit=10'))
        assert code == 200 and page == expected
        for query in ('offset=one', 'limit=201', 'status=deleted', 'offset=1&offset=2', 'secret=x'):
            assert request(server, 'GET', f'/api/jobs/{job_id}/changes?{query}')[0] == 400
    pytest.importorskip('mcp')
    from su_crawler.assistant_server import build_assistant_server
    mcp = build_assistant_server(tmp_path)
    response = asyncio.run(mcp.call_tool('get_collection_changes',
        {'job_id': job_id, 'status': 'added', 'offset': 60, 'limit': 10}))[1]
    assert response == expected
