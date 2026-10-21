"""Durable, page-scoped refresh decisions for approved catalog collections.

Older confirmed snapshots without ``refresh_policy`` intentionally run in full
mode. Newly reviewed plans should write the normalized default explicitly.

Full collection is the default. Incremental mode only revalidates retained
evidence of plain public HTTP pages (no adapter, recipe, selectors or
profile) with ETag/Last-Modified validators. It is deliberately not faster for
adapter pages such as Gongcar or Jetcar: their observations come from a
rendered browser capture (list expansion, price sheets, visible-state proof),
which has no HTTP validator and must be visited again to prove what is
displayed now. A 304 for the page's HTML would not show that the rendered
inventory, prices or conditions are unchanged, so adapter pages are always
recollected in full and only their comparison with the baseline is
incremental.
"""
from __future__ import annotations

from collections import Counter, defaultdict, deque
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
        return {'mode': 'full', 'full_interval_days': 7}
    if not isinstance(value, dict) or set(value) - {'mode', 'full_interval_days'}:
        raise ValueError('refresh_policy must be an object with mode and full_interval_days')
    mode = value.get('mode', 'full')
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


# Offer conditions that identify a row only when one product lists several
# offers for the same term and basis (see item_changes).
VARIANT_FIELDS = ('rental_duration', 'deposit_amount', 'deposit_percent', 'advance_amount',
                  'advance_percent', 'annual_mileage_km', 'driver_age_condition', 'vehicle_value')


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


def item_changes(previous: list[dict], current: list[dict], *, url='', inventory_complete=False,
                 baseline=False, successful=True, previous_checked_at=None, checked_at=None,
                 failure_reason='page_unavailable') -> list[dict]:
    """Pair only unique observed identifiers with identical offer variants.

    A duplicate identifier/variant group cannot establish a one-to-one match,
    even if subtracting unchanged rows would leave only one possible partner.
    """
    # Price type (sale, list, inquiry, etc.) describes the offer, not a
    # numeric price or currency change. Keep it among observed conditions.
    price_fields = {'price', 'currency'}
    def identity(item):
        fields = item.get('fields', {})
        return next((f'{key}:{fields[key]}' for key in ('sku', 'item_id', 'mpn', 'gtin')
                     if fields.get(key) is not None and str(fields[key]).strip()), None)

    def key(item):
        observed = identity(item)
        # Contract durations are separately offered rows for the same source
        # product. The explicit duration identifies that variant; mutable
        # commercial conditions such as deposits and mileage never do.
        fields = item.get('fields', {})
        term = fields.get('term_months')
        # Contract type and price basis distinguish offers; a monthly rental
        # must not be paired with a daily or lease offer sharing a product ID.
        offer_kind = (fields.get('contract_mode'), fields.get('price_basis'))
        return (observed, _canonical(item.get('specs', {})), _canonical(term),
                _canonical(offer_kind)) if observed else None

    old_base = [key(item) for item in previous]
    new_base = [key(item) for item in current]
    old_base_counts, new_base_counts = Counter(old_base), Counter(new_base)

    def refined(item, base):
        # Some pages list several offers per product and term that differ only
        # in deposit, advance, mileage, age or rental duration. Only for such a
        # shared key do those conditions identify the row; a unique product
        # keeps reporting a deposit change as a condition change.
        if base is None or (old_base_counts[base] <= 1 and new_base_counts[base] <= 1):
            return base
        fields = item.get('fields', {})
        return base + (_canonical({name: fields.get(name) for name in VARIANT_FIELDS}),)

    old_keys = [refined(item, base) for item, base in zip(previous, old_base)]
    new_keys = [refined(item, base) for item, base in zip(current, new_base)]
    old_counts, new_counts = Counter(old_keys), Counter(new_keys)
    exact_indices = defaultdict(deque)
    identity_indices = {}
    for index, (item, item_key) in enumerate(zip(previous, old_keys)):
        exact_indices[_canonical(item)].append(index)
        if item_key and old_counts[item_key] == 1:
            identity_indices[item_key] = index
    matched = set()
    rows = []

    def row(status, before, after, reason):
        item = after or before or {}
        old_fields, new_fields = (before or {}).get('fields', {}), (after or {}).get('fields', {})
        changed = ([name for name in sorted(set(old_fields) | set(new_fields))
                    if old_fields.get(name) != new_fields.get(name)] if before and after else [])
        if before and after and before.get('specs', {}) != after.get('specs', {}):
            changed.append('specs')
        condition_fields = sorted((set(old_fields) | set(new_fields)) -
                                  (price_fields | {'name', 'sku', 'item_id', 'mpn', 'gtin'})) if before and after else []
        if before and after and (before.get('specs') or after.get('specs')):
            condition_fields.append('specs')
        incomplete_conditions = any(old_fields.get(name) in (None, '') or new_fields.get(name) in (None, '')
                                    for name in condition_fields if name != 'specs')
        if 'specs' in condition_fields:
            old_specs, new_specs = before.get('specs') or {}, after.get('specs') or {}
            incomplete_conditions |= any(old_specs.get(name) in (None, '') or new_specs.get(name) in (None, '')
                                         for name in set(old_specs) | set(new_specs))
        rows.append({'status': status, 'url': url, 'identity': identity(item),
                     'product_name': item.get('fields', {}).get('name') or '',
                     'before': before, 'after': after, 'changed_fields': changed, 'reason': reason,
                     # Equality is limited to observed fields, never a claim
                     # that unrecorded commercial terms are identical.
                     'condition_comparison': ('incomplete' if incomplete_conditions else
                                              'observed_fields_only' if condition_fields else
                                              'not_observed') if before and after else 'not_comparable',
                     'previous_checked_at': previous_checked_at, 'checked_at': checked_at})

    if not successful:
        for item in previous or [None]:
            row('unavailable', item, None, failure_reason)
        return rows
    for item, item_key in zip(current, new_keys):
        exact = exact_indices.get(_canonical(item))
        while exact and exact[0] in matched:
            exact.popleft()
        if exact:
            index = exact.popleft()
            matched.add(index)
            row('unchanged', previous[index], item, 'same_observed_fields')
            continue
        index = identity_indices.get(item_key) if item_key and new_counts[item_key] == 1 else None
        if index is not None and index not in matched:
            matched.add(index)
            old = previous[index]
            old_fields, new_fields = old.get('fields', {}), item.get('fields', {})
            price_changed = any(old_fields.get(name) != new_fields.get(name) for name in price_fields)
            # A renamed product title is recorded in changed_fields, but it
            # does not establish that its commercial offer terms changed.
            condition_keys = (set(old_fields) | set(new_fields)) - (price_fields | {
                'name', 'sku', 'item_id', 'mpn', 'gtin'})
            other_changed = any(old_fields.get(name) != new_fields.get(name)
                                for name in condition_keys)
            other_changed |= old.get('specs', {}) != item.get('specs', {})
            status = ('price_and_conditions_changed' if price_changed and other_changed else
                      'price_changed' if price_changed else 'conditions_changed')
            reason = ('observed_price_and_conditions_changed' if status == 'price_and_conditions_changed' else
                      'observed_price_or_currency_changed' if price_changed else 'observed_conditions_changed')
            row(status, old, item, reason)
        else:
            row('baseline' if baseline else 'added', None, item,
                'first_successful_capture' if baseline else 'new_observed_item')
    for index, item in enumerate(previous):
        if index in matched:
            continue
        row('removed_candidate' if inventory_complete else 'unavailable', item, None,
            'absent_from_complete_inventory_review_required' if inventory_complete else 'not_observed_in_current_capture')
    return rows


def compare(previous: list[dict], current: list[dict], *, inventory_complete=False) -> dict:
    """Compare multisets; locator, evidence path and capture time do not affect change."""
    rows = item_changes(previous, current, inventory_complete=inventory_complete)
    totals = Counter(row['status'] for row in rows)
    return {'added': totals['added'], 'changed': totals['price_changed'] + totals['price_and_conditions_changed'] + totals['conditions_changed'],
            'unchanged': totals['unchanged'], 'removed_candidate': totals['removed_candidate'],
            'unavailable': totals['unavailable']}


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
        restored = {**saved, 'content': content}
        raw = saved.get('raw_candidates')
        if raw is not None and stable_id(raw) == saved.get('candidate_fingerprint'):
            restored['parsed_candidates'] = [Candidate(**item) for item in raw]
        return restored
    except (OSError, ValueError, TypeError):
        return None


def page_baseline(source: Source, responses: list[tuple[FetchResult, list[Candidate]]],
                  chosen: list[Candidate], directory: Path, *, previous: dict | None = None,
                  raw_http_candidates: list[dict] | None = None) -> dict:
    from .pipeline import _save_evidence
    data = {'candidates': semantic_candidates(chosen), 'source_signature': source_signature(source),
            'http': None}
    http = next((result for result, _ in responses if result.backend == 'http' and result.status == 'fetched'), None)
    if (http and not http.variant_snapshots and not source.recipe and not source.adapter
            and not source.selectors and not source.profile_dir):
        if any(event.get('code') == 'extraction_error' for event in http.trace):
            return data
        path, digest = _save_evidence(directory, http, source)
        prior = (previous or {}).get('http') or {}
        revalidated = next((event for event in http.trace if event.get('code') == 'http_not_modified'), None)
        data['http'] = {'evidence_path': path, 'sha256': digest, 'final_url': http.final_url,
                        'media_type': http.media_type, 'http_metadata': http.http_metadata,
                        'fetched_at': (prior.get('fetched_at') or revalidated.get('original_fetched_at'))
                                      if revalidated else http.fetched_at,
                        'checked_at': http.fetched_at}
        if raw_http_candidates is not None:
            data['http'].update(raw_candidates=raw_http_candidates,
                                candidate_fingerprint=stable_id(raw_http_candidates),
                                extraction_trace=[event for event in http.trace if event.get('event') in
                                                  {'extraction_gap', 'jsonld_truncated'}])
    return data
