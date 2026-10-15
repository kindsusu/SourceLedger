"""Reviewed repeat settings are shared by UI, MCP, and collection snapshots."""
import asyncio

import pytest

from su_crawler import research_plans as plans
from su_crawler.execution_policy import build_execution_brief


def test_refresh_policy_requires_revision_review_and_survives_preview(tmp_path):
    plan = plans.create_plan(tmp_path, 'Inspect components')['plan']
    assert plan['refresh_policy'] == {'mode': 'incremental', 'full_interval_days': 7}
    plan = plans.revise_plan(tmp_path, plan['id'], plan['revision'], {
        'topic': {'industry': 'Parts', 'product': 'Bolt', 'market': 'US'},
        'added_candidates': [{'url': 'https://example.test/catalog', 'kind': 'category'}],
        'refresh_policy': {'mode': 'incremental', 'full_interval_days': 14},
    })['plan']
    plan = plans.confirm_plan(tmp_path, plan['id'], plan['revision'], True)['plan']
    original = plan
    plan = plans.revise_plan(tmp_path, plan['id'], plan['revision'], {
        'refresh_policy': {'mode': 'full', 'full_interval_days': 14}})['plan']
    assert plan['state'] == 'preview' and plan['confirmed_at'] is None
    assert plans.confirmed_snapshot(tmp_path, original['id'], original['revision'])['refresh_policy']['mode'] == 'incremental'
    brief = build_execution_brief(plan)
    assert brief['refresh_policy'] == {'mode': 'full', 'full_interval_days': 14}
    plan = plans.submit_plan_preview(tmp_path, plan['id'], plan['revision'], {
        'topic': plan['topic'], 'candidates': [], 'summary': 'Refined request'})['plan']
    assert plan['refresh_policy']['mode'] == 'full'


@pytest.mark.parametrize('value', [{'mode': 'skip'}, {'full_interval_days': 0},
    {'full_interval_days': True}, {'full_interval_days': 1.2}, {'full_interval_days': 366},
    {'secret': 'no'}, []])
def test_invalid_repeat_settings_rejected_without_revision_change(tmp_path, value):
    plan = plans.create_plan(tmp_path, 'Inspect components')['plan']
    with pytest.raises(ValueError):
        plans.revise_plan(tmp_path, plan['id'], plan['revision'], {'refresh_policy': value})
    assert plans.get_plan(tmp_path, plan['id'])['plan']['revision'] == 1


def test_mcp_repeat_policy_roundtrip(tmp_path):
    pytest.importorskip('mcp')
    from su_crawler.assistant_server import build_assistant_server
    server = build_assistant_server(tmp_path)
    def call(name, args):
        return asyncio.run(server.call_tool(name, args))[1]
    plan = call('create_research_plan', {'request_text': 'Inspect parts'})['plan']
    changed = call('update_research_plan', {'plan_id': plan['id'], 'expected_revision': plan['revision'],
        'changes': {'refresh_policy': {'mode': 'incremental', 'full_interval_days': 2}}})['plan']
    brief = call('get_research_execution_plan', {'plan_id': plan['id'], 'expected_revision': changed['revision']})
    assert brief['execution_plan']['refresh_policy']['full_interval_days'] == 2


def test_batch_refresh_summary_is_bounded_and_preserves_totals(tmp_path):
    from su_crawler.research_batch import _summary
    batches = []
    for mode in ('full', 'incremental'):
        batches.append({'status': 'completed', 'coverage_counts': {'visited': 40},
            'coverage_summary': {'complete': 40}, 'target_count': 40, 'budget_seconds_used': 1,
            'refresh': {'effective_mode': mode, 'reason': 'first_run' if mode == 'full' else 'scheduled_incremental',
                'counts': {'added': 2, 'unchanged': 38},
                'changes': [{'url': f'https://example.test/{mode}/{i}'} for i in range(40)],
                'changes_truncated': 0}})
    summary = _summary({'batches': batches, 'batch_count': 2, 'target_count': 80,
        'job_id': 'test', 'started_at': None, 'finished_at': None}, output=tmp_path)
    refresh = summary['refresh']
    assert refresh['effective_mode'] == 'mixed'
    assert refresh['counts'] == {'added': 4, 'unchanged': 76}
    assert len(refresh['changes']) == 50 and refresh['changes_truncated'] == 30


def test_web_refresh_policy_roundtrip(tmp_path):
    from test_web_server import running_server, authorized, json_response
    with running_server(tmp_path) as server:
        status, created = json_response(authorized(server, 'POST', '/api/plans', {'request_text': 'Inspect parts'}))
        assert status == 200
        plan = created['plan']
        status, edited = json_response(authorized(server, 'POST', '/api/plans/edit', {
            'plan_id': plan['id'], 'expected_revision': plan['revision'],
            'changes': {'refresh_policy': {'mode': 'incremental', 'full_interval_days': 3}}}))
        assert status == 200
        assert edited['plan']['refresh_policy']['full_interval_days'] == 3
