"""Durable, evidence-linked collection gaps, separate from immutable run coverage."""
from __future__ import annotations

import hashlib
import json


RETRYABLE_CODES = {'transient_failure', 'exhausted_limit'}
MAX_RETRY_CHAIN = 3


def _identity(run_id: str, url: str, section: str, option: str, code: str) -> str:
    payload = json.dumps([run_id, url, section, option, code], ensure_ascii=False)
    return 'issue_' + hashlib.sha256(payload.encode('utf-8')).hexdigest()[:32]


def _classify(status: str, reason: str, *, section: bool = False) -> tuple[str, str]:
    text = f'{status} {reason}'.lower()
    if status == 'policy_denied' or any(word in text for word in ('robots', 'policy_denied', 'forbidden by policy')):
        return 'policy_denied', 'review_policy'
    if status == 'needs_auth' or any(word in text for word in ('login required', 'authentication required', 'sign in required', '401 unauthorized')):
        return 'needs_auth', 'provide_authorized_access'
    if any(word in text for word in ('browser handoff', 'captcha', 'interactive challenge', 'access_challenge', 'access challenge', '403 forbidden', 'http 403')) or (status == 'blocked' and '403' in text):
        return 'browser_handoff', 'submit_browser_evidence'
    if status == 'tool_unavailable' or any(word in text for word in ('browser_launch', 'browser launch', 'browser unavailable')):
        return 'browser_environment', 'diagnose_browser_environment'
    if '429' in text or 'rate limit' in text:
        return 'transient_failure', 'retry_after_backoff'
    if any(word in text for word in ('limit', 'budget', 'truncat', 'max_clicks', 'max_items', 'max_pages')):
        return 'exhausted_limit', 'review_limits_then_retry'
    if any(word in text for word in ('structure changed', 'selector', 'recipe drift', 'dom changed')):
        return 'structure_changed', 'inspect_recipe'
    if section and status == 'unknown':
        return 'unknown_enumeration', 'inspect_scope_and_recipe'
    if status in {'failed', 'timeout', 'tool_unavailable', 'interrupted', 'blocked', 'unprocessed'} or any(
            word in text for word in ('button_stalled', 'request timed out', 'temporary failure')):
        return 'transient_failure', 'retry_with_budget'
    if status in {'unknown', 'no_data'}:
        return 'unknown_enumeration', 'inspect_scope_and_recipe'
    return 'unverified_option', 'inspect_source_evidence'


def _scope(page: dict) -> dict:
    return {key: page[key] for key in ('source_id', 'adapter', 'recipe_fingerprint', 'plan_fingerprint')
            if page.get(key) is not None}


def _prior_sections(store, run_id: str, page: dict) -> list[dict]:
    """Compare only an explicit recipe or a stable source and adapter on the same URL."""
    scope = _scope(page)
    if not scope.get('recipe_fingerprint') and not (scope.get('source_id') and scope.get('adapter')):
        return []
    rows = store.db.execute('''SELECT rc.data FROM run_coverage rc JOIN runs r ON r.id=rc.run_id
                               WHERE rc.run_id<>? AND r.started_at <= (SELECT started_at FROM runs WHERE id=?)
                               ORDER BY r.started_at DESC LIMIT 30''', (run_id, run_id))
    for row in rows:
        for prior in json.loads(row[0]):
            if prior.get('url') != page.get('url'):
                continue
            old_scope = _scope(prior)
            if scope.get('recipe_fingerprint'):
                comparable = old_scope.get('recipe_fingerprint') == scope['recipe_fingerprint']
            else:
                comparable = (old_scope.get('source_id') == scope['source_id'] and
                              old_scope.get('adapter') == scope['adapter'])
            if scope.get('plan_fingerprint') or old_scope.get('plan_fingerprint'):
                comparable = comparable and old_scope.get('plan_fingerprint') == scope.get('plan_fingerprint')
            if comparable and prior.get('sections'):
                return prior['sections']
    return []


def _timed_out_before(store, run_id: str, url: str) -> bool:
    """True when an earlier run recorded the same page as a timeout."""
    if store is None or not url:
        return False
    rows = store.db.execute('''SELECT rc.data FROM run_coverage rc JOIN runs r ON r.id=rc.run_id
                               WHERE rc.run_id<>? AND r.started_at <= (SELECT started_at FROM runs WHERE id=?)
                               ORDER BY r.started_at DESC LIMIT 30''', (run_id, run_id))
    return any(prior.get('url') == url and prior.get('status') == 'timeout'
               for row in rows for prior in json.loads(row[0]))


def issues_from_coverage(store, run_id: str, coverage: list[dict]) -> list[dict]:
    issues: list[dict] = []
    for page in coverage:
        if page.get('status') == 'excluded':
            continue
        url = page.get('url') or page.get('source_url') or ''
        scope = _scope(page)
        page_status = page.get('status', 'unknown')
        sections = page.get('sections') or []
        if page_status not in {'visited', 'fetched'}:
            trace_hint = json.dumps(page.get('trace') or [], ensure_ascii=False, default=str)[:10000]
            code, action = _classify(page_status, f"{page.get('reason') or ''} {trace_hint}")
            if page_status == 'timeout' and _timed_out_before(store, run_id, url):
                # The program's own browser timed out on this page in an earlier
                # run too (not a launch failure): offer the host browser instead
                # of another identical retry.
                code, action = 'browser_handoff', 'submit_browser_evidence'
            issues.append(_issue(run_id, url, '', '', code, str(page.get('reason') or page_status),
                                 action, {'page_status': page_status, 'evidence_ref': page.get('evidence_path'),
                                          'trace': page.get('trace', [])}, scope,
                                 attempts=page.get('attempts', 0)))
        if not sections and page_status in {'visited', 'fetched'}:
            sections = [{'section': 'page_scope', 'status': 'unknown',
                         'reason': 'No validated enumeration of page products and options'}]
        # A failed fetch cannot enumerate sections. Its page-level issue owns
        # recovery; synthetic unknown sections would incorrectly block retry.
        if page_status not in {'visited', 'fetched', 'no_data'}:
            sections = []
        for section in sections:
            if section.get('status') == 'complete':
                continue
            name = str(section.get('section') or 'page_scope')
            reason = str(section.get('reason') or section.get('status') or 'Coverage unconfirmed')
            code, action = _classify(str(section.get('status') or 'unknown'), reason, section=True)
            evidence = {key: section[key] for key in ('evidence_ref', 'expected_count', 'observed_count',
                        'extracted_count', 'price_count', 'unparsed_count', 'limits', 'trace',
                        'unavailable', 'skipped', 'unavailable_options', 'skipped_options',
                        'missing_locators', 'mismatched_locators', 'enumeration',
                        # Generic extraction: which method chose the prices and the
                        # collection_rules entry that would pin it after approval.
                        'method', 'platform', 'suggested_rule') if key in section}
            issues.append(_issue(run_id, url, name, '', code, reason, action, evidence, scope,
                                 attempts=page.get('attempts', 0)))
            for key in ('missing_locators', 'mismatched_locators'):
                for locator in section.get(key) or []:
                    issues.append(_issue(run_id, url, name, str(locator), 'unverified_option',
                                         f'{key}: {locator}', 'inspect_source_evidence',
                                         {'evidence_ref': section.get('evidence_ref'), 'locator': locator}, scope))
        old = _prior_sections(store, run_id, page)
        current = {str(s.get('section')): s for s in sections}
        if page_status not in {'visited', 'fetched', 'no_data'}:
            continue
        for previous in old:
            name = str(previous.get('section') or '')
            if previous.get('status') != 'complete' or not name:
                continue
            present = current.get(name)
            if present is None or present.get('status') == 'unknown':
                issues.append(_issue(run_id, url, name, '', 'structure_changed',
                                     'Previously complete comparable section is absent or unrecognized',
                                     'inspect_recipe', {'prior_section': previous, 'current_section': present}, scope))
    return issues


def _issue(run_id, url, section, option, code, reason, action, evidence, scope, attempts=0):
    return {'id': _identity(run_id, url, section, option, code), 'run_id': run_id,
            'page_url': url, 'section_key': section, 'option_key': option,
            'reason_code': code, 'reason': reason, 'next_action': action,
            'evidence': evidence, 'scope': scope,
            'attempts': attempts if isinstance(attempts, int) and not isinstance(attempts, bool) and attempts >= 0 else 0}


def sync_coverage_issues(store, run_id: str, coverage: list[dict]) -> list[dict]:
    """Called after save_run_coverage; safe to repeat and never resolves by count."""
    issues = issues_from_coverage(store, run_id, coverage)
    store.save_collection_issues(issues)
    return [store._collection_issue(issue_id) for issue_id in dict.fromkeys(issue['id'] for issue in issues)]
