"""Durable, page-scoped refresh decisions for approved catalog collections.

Older confirmed snapshots without ``refresh_policy`` intentionally run in full
mode. Newly reviewed plans should write the normalized default explicitly.
"""
from __future__ import annotations

from collections import Counter
from contextlib import closing
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import sqlite3

from .models import Candidate, FetchResult, Source, stable_id
from .storage import Store


def normalize_refresh_policy(value: dict | None) -> dict:
    if value is None:
        return {'mode': 'incremental', 'full_interval_days': 7}
    if not isinstance(value, dict) or set(value) - {'mode', 'full_interval_days'}:
        raise ValueError('refresh_policy must be an object with mode and full_interval_days')
    mode = value.get('mode', 'incremental')
    days = value.get('full_interval_days', 7)
    if mode not in {'incremental', 'full'} or isinstance(days, bool) or not isinstance(days, int) or not 1 <= days <= 365:
        raise ValueError('refresh_policy requires incremental/full mode and full_interval_days from 1 to 365')
    return {'mode': mode, 'full_interval_days': days}


def _stamp() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse(value: str | None):
    try:
        parsed = datetime.fromisoformat(value.replace('Z', '+00:00')) if value else None
        return parsed if parsed and parsed.tzinfo else None
    except (TypeError, ValueError):
        return None


def scope_key(snapshot: dict, chosen: list[str], *, follow_links: bool) -> str:
    # Selected URL set separates independent batches of the same reviewed plan.
    scope = {key: snapshot.get(key) for key in ('id', 'topic', 'include_terms', 'exclude_terms',
                                                'conditions', 'excluded_urls', 'collection_rules')}
    return stable_id('catalog-refresh-scope-v1', scope, sorted(chosen), follow_links)


def _table(store: Store):
    store.db.execute('''CREATE TABLE IF NOT EXISTS catalog_refresh_baselines (
        scope_key TEXT PRIMARY KEY, data TEXT NOT NULL)''')
    store.db.execute('''CREATE TABLE IF NOT EXISTS catalog_refresh_runs (
        run_id TEXT PRIMARY KEY, data TEXT NOT NULL)''')
    store.db.commit()


def read_baseline(directory: Path, key: str) -> dict | None:
    store = Store(directory)
    try:
        _table(store)
        row = store.db.execute('SELECT data FROM catalog_refresh_baselines WHERE scope_key=?', (key,)).fetchone()
        return json.loads(row[0]) if row else None
    finally:
        store.close()


def write_baseline(directory: Path, key: str, value: dict):
    store = Store(directory)
    try:
        _table(store)
        with store.db:
            store.db.execute('INSERT OR REPLACE INTO catalog_refresh_baselines(scope_key,data) VALUES(?,?)',
                             (key, json.dumps(value, ensure_ascii=False, sort_keys=True)))
    finally:
        store.close()


def save_refresh_run(directory: Path, run_id: str, refresh: dict):
    store = Store(directory)
    try:
        _table(store)
        with store.db:
            store.db.execute('INSERT OR REPLACE INTO catalog_refresh_runs(run_id,data) VALUES(?,?)',
                             (run_id, json.dumps(refresh, ensure_ascii=False, sort_keys=True)))
    finally:
        store.close()


def refresh_for_run(directory: Path, run_id: str) -> dict | None:
    database = directory / 'prices.sqlite3'
    if not database.is_file():
        return None
    # Report regeneration can already own a transaction on the ledger. Reading
    # refresh metadata must not open a second writer or run schema migrations.
    with closing(sqlite3.connect(database.resolve().as_uri() + '?mode=ro', uri=True)) as db:
        if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='catalog_refresh_runs'").fetchone():
            return None
        row = db.execute('SELECT data FROM catalog_refresh_runs WHERE run_id=?', (run_id,)).fetchone()
        return json.loads(row[0]) if row else None


def decide(policy: dict, baseline: dict | None, rules_version: str, *, manual_full=False, now=None) -> dict:
    now = now or datetime.now(timezone.utc)
    last = _parse((baseline or {}).get('last_full_at'))
    due = last + timedelta(days=policy['full_interval_days']) if last else None
    reason = ('manual_full' if manual_full or policy['mode'] == 'full' else
              'first_run' if not baseline else
              'rules_changed' if baseline.get('rules_version') != rules_version else
              'no_valid_baseline' if not baseline.get('valid') else
              'interval_elapsed' if due is None or now >= due else 'scheduled_incremental')
    return {'effective_mode': 'incremental' if reason == 'scheduled_incremental' else 'full',
            'reason': reason, 'last_full_at': last.isoformat() if last else None,
            'next_full_due_at': due.isoformat() if due else None,
            'checked_at': now.isoformat()}


def _canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), default=str)


def semantic_candidates(candidates: list[Candidate] | list[dict]) -> list[dict]:
    values = []
    for item in candidates:
        if isinstance(item, Candidate):
            fields, specs = item.fields, item.specs
        else:
            fields, specs = item.get('fields', {}), item.get('specs', {})
        values.append({'fields': fields, 'specs': specs})
    return values


def source_signature(source: Source) -> str:
    """Identify the effective extraction and browser configuration for a page."""
    return stable_id(source.recipe, source.selectors, source.row_selector,
                     source.recipe_version, source.adapter, source.backends,
                     source.profile_dir, source.account_scope)


def compare(previous: list[dict], current: list[dict], *, inventory_complete=False) -> dict:
    """Compare multisets; locator, evidence path and capture time do not affect change."""
    before, after = Counter(map(_canonical, previous)), Counter(map(_canonical, current))
    same = before & after
    added = after - same
    missing = before - same
    unchanged = sum(same.values())
    # A changed item requires a stable observed identifier. Otherwise report
    # separate added/unavailable items instead of guessing identity.
    def identity(raw):
        fields = json.loads(raw).get('fields', {})
        return next(((key, str(fields[key])) for key in ('sku', 'item_id', 'mpn', 'gtin')
                     if fields.get(key) is not None and str(fields[key]).strip()), None)
    changed = 0
    for new in list(added):
        possible = [old for old in missing if missing[old] > 0 and identity(new)
                    and identity(new) == identity(old)]
        # Sellers sometimes reuse a SKU across variants. Prefer matching specs,
        # and leave ambiguous identities as added/unavailable rather than pair
        # different options according to response order.
        same_specs = [old for old in possible if json.loads(old).get('specs') == json.loads(new).get('specs')]
        options = same_specs or possible
        match = options[0] if len(options) == 1 else None
        if match:
            n = min(added[new], missing[match])
            added[new] -= n
            missing[match] -= n
            changed += n
    n_added, n_missing = sum(added.values()), sum(missing.values())
    return {'added': n_added, 'changed': changed, 'unchanged': unchanged,
            'removed_candidate': n_missing if inventory_complete else 0,
            'unavailable': 0 if inventory_complete else n_missing}


def validated_http(page: dict, directory: Path) -> dict | None:
    saved = page.get('http') or {}
    path = saved.get('evidence_path')
    if not path:
        return None
    try:
        evidence = Path(path).resolve()
        if not evidence.is_relative_to((directory / 'evidence').resolve()):
            return None
        content = evidence.read_bytes()
        if hashlib.sha256(content).hexdigest() != saved.get('sha256'):
            return None
        return {**saved, 'content': content}
    except (OSError, ValueError, TypeError):
        return None


def page_baseline(source: Source, responses: list[tuple[FetchResult, list[Candidate]]],
                  chosen: list[Candidate], directory: Path, *, previous: dict | None = None) -> dict:
    from .pipeline import _save_evidence
    data = {'candidates': semantic_candidates(chosen), 'source_signature': source_signature(source),
            'http': None}
    http = next((result for result, _ in responses if result.backend == 'http' and result.status == 'fetched'), None)
    if http and not source.recipe and not source.adapter and not source.selectors and not source.profile_dir:
        path, digest = _save_evidence(directory, http, source)
        prior = (previous or {}).get('http') or {}
        revalidated = next((event for event in http.trace if event.get('code') == 'http_not_modified'), None)
        data['http'] = {'evidence_path': path, 'sha256': digest, 'final_url': http.final_url,
                        'media_type': http.media_type, 'http_metadata': http.http_metadata,
                        'fetched_at': (prior.get('fetched_at') or revalidated.get('original_fetched_at'))
                                      if revalidated else http.fetched_at,
                        'checked_at': http.fetched_at}
    return data
