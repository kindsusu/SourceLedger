"""Read persisted item comparisons through the same UI and MCP boundary."""
from __future__ import annotations

from pathlib import Path

from .assistant_runtime import get_job
from .assistant_workspace import _managed_output, workspace_root
from .catalog_refresh import refresh_for_run


CHANGE_STATUSES = frozenset({
    'baseline', 'added', 'price_changed', 'price_and_conditions_changed',
    'conditions_changed', 'unavailable',
    'removed_candidate', 'unchanged',
})


def list_collection_changes(root, job_id: str, *, offset=0, limit=50, status='all') -> dict:
    """Page complete ledger comparisons, never just the bounded job receipt."""
    if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
        raise ValueError('offset must be a non-negative integer')
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 200:
        raise ValueError('limit must be between 1 and 200')
    if not isinstance(status, str) or status not in CHANGE_STATUSES | {'all', 'changes'}:
        raise ValueError('Invalid change status')
    base = workspace_root(root)
    job = get_job(base, job_id)
    result = job.get('result') or {}
    if job.get('operation') == 'research_batch':
        from .research_batch import read_batch_journal, _output
        plan_id = job['arguments']['plan_id']
        journal = read_batch_journal(base, plan_id, job_id)
        run_ids = list(dict.fromkeys(batch['run_id'] for batch in (journal or {}).get('batches', [])
                                    if batch.get('run_id')))
        output = _output(base, plan_id)
    elif result.get('run_id') and result.get('output_dir'):
        output = _managed_output(base, Path(result['output_dir']), 'change report output')
        run_ids = [result['run_id']]
    else:
        output, run_ids = None, []
    items, available_runs = [], 0
    for run_id in run_ids:
        refresh = refresh_for_run(output, run_id)
        if refresh is None or 'item_changes' not in refresh:
            continue
        available_runs += 1
        items.extend({**item, 'run_id': run_id} for item in refresh['item_changes'])
    counts = {key: sum(item.get('status') == key for item in items) for key in sorted(CHANGE_STATUSES)}
    total_all = len(items)
    if status == 'changes':
        items = [item for item in items if item.get('status') not in {'baseline', 'unchanged'}]
    elif status != 'all':
        items = [item for item in items if item.get('status') == status]
    return {'job_id': job_id, 'items': items[offset:offset + limit], 'total': len(items),
            'total_all': total_all, 'offset': offset, 'limit': limit, 'status': status,
            'counts': counts, 'available': available_runs > 0,
            'missing_runs': len(run_ids) - available_runs,
            'has_more': offset + limit < len(items),
            'note': 'Approved and visited URLs only. Missing observations are not confirmed removals.'}
