"""Simple bounded collection for known page formats, with ordinary pipeline output."""
from __future__ import annotations

from dataclasses import asdict, replace
from copy import deepcopy
from pathlib import Path
import math
import hashlib
import json
import time
from urllib.parse import urlsplit

from .collectors import collect, robots_policy_scope
from .extraction import extract
from .models import CollectionConfig, FetchResult, Product, Source, stable_id
from .pipeline import execute, _candidate_quality, _has_sufficient_evidence
from .research import _canonical_url, _atomic_write, load_workspace
from .storage import Store, workspace_lock
from .incremental import load_snapshot, conditional_fetch, extract_current
from .coverage import unknown_section, summarize, apply_status, page_status


HOST_ADAPTERS = {
    'jetcar.kr': 'jetcar', 'www.jetcar.kr': 'jetcar',
    'jetcar.co.kr': 'jetcar', 'www.jetcar.co.kr': 'jetcar',
    'jetcar2.co.kr': 'jetcar', 'www.jetcar2.co.kr': 'jetcar',
    'gongcarrent.kr': 'gongcar', 'www.gongcarrent.kr': 'gongcar',
    'gongcar.kr': 'gongcar', 'www.gongcar.kr': 'gongcar',
    'go.funrentcar.com': 'funrent',
    'www.amazoncar.co.kr': 'amazoncar', 'amazoncar.co.kr': 'amazoncar',
}


# Adapters whose sections can be complete only from a rendered browser capture
# (every Gongcar coverage rule requires backend == "playwright"; its rendered
# DOM also outranks static HTML). For these, the browser runs first and HTTP
# is only a fallback when the browser fails, times out, is unavailable, or
# extracts nothing. Jetcar sections can be complete from static HTML, so it
# keeps HTTP first and stops early only with complete, sufficient evidence.
# Catalog collection derives products from the chosen response only, so a
# skipped HTTP response cannot remove an observation row there.
BROWSER_FIRST_ADAPTERS = frozenset({'gongcar'})


def browser_first(source) -> bool:
    return (source.adapter in BROWSER_FIRST_ADAPTERS and bool(source.recipe) and not source.profile_dir
            and 'playwright' in source.backends and 'http' in source.backends)


def _ready_recipe(adapter: str, url: str = '') -> list[dict]:
    path = urlsplit(url).path.rstrip('/')
    if adapter == 'amazoncar':
        selector = 'table tbody tr' if path == '/rentPrice' else 'div[class*="choice-rent-lease"] button[class*="active"]'
        return [{'action': 'wait_for', 'selector': selector, 'state': 'visible'}]
    path = path.replace('/monthly/', '/monthly-rent/').replace('/daily/', '/daily-rent/')
    if path == '/monthly':
        path = '/monthly-rent'
    elif path == '/daily':
        path = '/daily-rent'
    price_sheet_steps = []
    if adapter == 'gongcar' and path in {'/longterm', '/rentpass', '/monthly-rent'}:
        price_sheet_steps = [
            {'action': 'click', 'selector': '#promo.is-on button[data-act="promo-close"]',
             'if_visible': True, 'appearance_wait_seconds': 2, 'timeout_seconds': 2},
            {'action': 'click', 'selector': '#deals > button[data-act="price-page"]',
             'if_visible': True, 'timeout_seconds': 5}]
    if adapter == 'gongcar' and path in {'/monthly-rent', '/daily-rent'}:
        monthly = path == '/monthly-rent'
        return [{'action': 'expand_list',
                 'section': 'monthly_catalog' if monthly else 'daily_catalog',
                 'selector': 'body',
                 'item_selector': ':is(div.grid div.cursor-pointer:has(> div > h4), #deals > button.deal:not(.deal-all))',
                 'button_selector': 'p:text-is("더 보기...")' if monthly else 'button:has(> span:text-is("펼쳐보기"))',
                 'trigger': 'scroll' if monthly else 'click',
                 'max_clicks': 100, 'max_items': 1000}] + (price_sheet_steps if monthly else [])
    if adapter == 'gongcar' and (path.startswith('/monthly-rent/') or path.startswith('/daily-rent/')):
        return [{'action': 'wait_for', 'selector': 'main h1', 'state': 'visible'},
                {'action': 'wait_for', 'selector': 'main h3:text-is("렌트 가격")', 'state': 'visible'}]
    if adapter == 'gongcar' and urlsplit(url).path.rstrip('/') in {'/rentpass', '/longterm'}:
        return price_sheet_steps + [{'action': 'expand_list',
                 'selector': 'section:has(h2:has-text("인기 차량")):not(:has(section h2:has-text("인기 차량")))',
                 'item_selector': ':is(:scope > div > div.grid > button, #deals > button.deal:not(.deal-all))',
                 'button_selector': 'button:text-is("더보기"), button:text-is("더 보기")',
                 'max_clicks': 100, 'max_items': 1000},
                {'action': 'expand_list', 'section': 'promotion',
                 'selector': 'section:has(button > span:has-text("특가")):not(:has(section button > span:has-text("특가")))',
                 'item_selector': 'div.grid > button',
                 'button_selector': 'button:text-is("더보기"), button:text-is("더 보기"), button[aria-label="다음"]',
                 'max_clicks': 100, 'max_items': 1000}]
    selectors = {
        'gongcar': ('table tbody tr td:visible, '
                    'section:has(h2:has-text("인기 차량")) > div > div.grid > button:visible'),
        'funrent': '#estCar',
    }
    return [{'action': 'wait_for', 'selector': selectors[adapter], 'state': 'visible'}] if adapter in selectors else []


def _offer_fingerprint(fields, evidence_sha256):
    payload = json.dumps([fields, evidence_sha256], ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(payload.encode('utf-8')).hexdigest()


UNSETTLED_FLAG = 'Browser observation did not settle before capture'


def _unsettled(result):
    return any(event.get('event') == 'observation_unsettled' for event in result.trace)


def _sections(result, candidates, adapter):
    """Describe section coverage; an unsettled capture is never complete.

    Candidates from an unsettled browser capture receive a review flag here so
    every caller (fresh capture, retained-evidence reanalysis, site collection)
    treats them alike.
    """
    unsettled = _unsettled(result)
    if unsettled:
        for candidate in candidates:
            if UNSETTLED_FLAG not in candidate.review_flags:
                candidate.review_flags.append(UNSETTLED_FLAG)
    sections = _adapter_sections(result, candidates, adapter)
    if unsettled:
        for section in sections:
            if section.get('status') == 'complete':
                section.update(status='partial', reason='observation_unsettled')
    return sections


def _adapter_sections(result, candidates, adapter):
    if adapter == 'gongcar':
        from .adapters.gongcar import section_coverage
        sections = section_coverage(result, candidates)
    elif adapter == 'jetcar':
        from .adapters.jetcar import section_coverage
        sections = section_coverage(result, candidates)
    elif not adapter:
        from .generic_extraction import generic_sections
        sections = generic_sections(result)
    else:
        sections = []
    if not sections:
        sections = [unknown_section(extracted_count=len(candidates))]
    for event in result.trace:
        if event.get('event') in {'option_enumeration', 'pagination', 'scroll_list', 'button_enumeration'}:
            sections.append({'section': event['event'], 'status': event.get('status', 'unknown'),
                             'reason': event.get('reason', 'Enumeration unconfirmed'),
                             'observed_count': event.get('observed_count', event.get('captured_count')),
                             'expected_count': None, 'extracted_count': len(candidates),
                             'extracted_locators': [c.locator for c in candidates],
                             'enumeration': event, 'scope': 'reviewed_controls_only',
                             'count_unit': ('items' if event['event'] == 'scroll_list' else
                                            'pages' if event['event'] == 'pagination' else 'selected_states')})
    gaps = [event for event in result.trace if event.get('event') in {'extraction_gap', 'jsonld_truncated', 'discovery_truncated'}]
    if gaps:
        sections.append({**unknown_section('; '.join(str(e.get('reason') or e.get('event')) for e in gaps)),
                         'section': 'processing_limits', 'status': 'partial', 'limits': gaps})
    digest = hashlib.sha256(result.content).hexdigest()
    for section in sections:
        expected_locators = set(section.get('extracted_locators', []))
        matched = [candidate for candidate in candidates
                   if candidate.locator in expected_locators
                   or candidate.fields.get('section') == section['section']]
        if matched:
            section['extracted_locators'] = [candidate.locator for candidate in matched]
            section['expected_offers'] = [
                {'locator': candidate.locator,
                 'fingerprint': _offer_fingerprint(
                     {**candidate.fields, **{f"spec:{key.removeprefix('spec:')}": value
                                              for key, value in candidate.specs.items()}},
                     _candidate_evidence_digest(result, candidate, digest))}
                for candidate in matched]
        kind = str(section['section']).rsplit('_', 1)[-1]
        events = [event for event in result.trace if event.get('event') == 'list_expansion'
                  and event.get('section') in {kind, section['section']}]
        section['click_count'] = events[-1].get('click_count', 0) if events else 0
    return sections


def _candidate_evidence_digest(result, candidate, default):
    index = candidate.derived_values.get('capture_snapshot_index')
    if isinstance(index, int) and not isinstance(index, bool) and 0 <= index < len(result.variant_snapshots):
        return hashlib.sha256(result.variant_snapshots[index]['content']).hexdigest()
    return default


def _save_coverage(destination, run, coverage):
    from .report import append_section_coverage
    store = Store(destination)
    try:
        rows = store.observations(run['id'])
        for page in coverage:
            if page.get('status') not in {'visited', 'fetched', 'no_data', 'excluded'} and page.get('source_id'):
                refs = []
                for attempt in store.db.execute('SELECT data FROM attempts WHERE run_id=? AND source_id=?',
                                                (run['id'], page['source_id'])):
                    refs.extend(json.loads(attempt[0]).get('prior_capture_evidence', []))
                if refs:
                    page['prior_capture_evidence'] = refs
                    page['evidence_path'] = '; '.join(dict.fromkeys(ref['receipt_path'] for ref in refs))
            if not page.get('sections') and page.get('status') != 'excluded':
                page['sections'] = [unknown_section(page.get('reason') or 'Page scope has not been verified')]
            page_rows = [row for row in rows if row.get('source_id') == page.get('source_id')]
            for section in page.get('sections', []):
                locators = section.get('extracted_locators')
                if locators:
                    retained = {row.get('locator') for row in page_rows}
                    excluded = set(page.get('excluded_locators', []))
                    excluded.update(a['locator'] for a in page.get('scope_assessments', []) if a.get('status') == 'excluded')
                    missing = set(locators) - retained - excluded
                    if missing:
                        section.update(status='partial', retained_count=len(set(locators) & retained),
                                       reason='Some extracted offer rows were not retained in the final ledger',
                                       missing_locators=sorted(missing))
                    expected_offers = [offer for offer in section.get('expected_offers', [])
                                       if offer['locator'] not in excluded]
                    retained_offers = {(row.get('locator'), _offer_fingerprint(
                        row.get('raw_fields') or {}, row.get('evidence_sha256'))) for row in page_rows}
                    mismatched = [offer['locator'] for offer in expected_offers
                                  if (offer['locator'], offer['fingerprint']) not in retained_offers]
                    if mismatched:
                        section.update(status='partial',
                                       reason='Stored offer values or evidence differ from the inspected section',
                                       mismatched_locators=sorted(set(mismatched)))
                section_rows = [row for row in page_rows if (row.get('raw_fields') or {}).get('section') == section.get('section')]
                evidence = sorted({row['evidence_path'] for row in section_rows if row.get('evidence_path')})
                if not evidence:
                    evidence = sorted({row['evidence_path'] for row in page_rows if row.get('evidence_path')})
                section['evidence_ref'] = '; '.join(evidence)
            page['coverage_status'] = page_status(page)
            page['coverage_gap'] = page['coverage_status'] in {'partial', 'unknown'}
        from .collection_issues import sync_coverage_issues
        issues = sync_coverage_issues(store, run['id'], coverage)
        for page in coverage:
            page['collection_issues'] = [issue for issue in issues if issue.get('page_url', issue.get('url')) == page.get('url')]
        store.save_run_coverage(run['id'], coverage)
    finally:
        store.close()
    _atomic_write(Path(destination) / f"coverage-{run['id']}.json", {'coverage': coverage})
    if any(page.get('sections') for page in coverage):
        append_section_coverage(run['report_path'], coverage)


def collect_sites(workspace_path: str | Path, *, urls: list[str] | None = None,
                  output_dir: str | Path | None = None, max_pages: int = 5,
                  max_seconds: float = 120, incremental: bool = True,
                  collector=None) -> dict:
    """Collect exactly the supplied pages, never claim a whole-site crawl.

    Publicly extracted item identifiers form the normal product scope. The
    first run still requires an industry/product/market research workspace.
    Page selection is explicit; this entry point never submits a quote form.
    """
    if isinstance(max_pages, bool) or not isinstance(max_pages, int) or not 1 <= max_pages <= 50:
        raise ValueError('max_pages must be an integer from 1 to 50')
    if isinstance(max_seconds, bool) or not isinstance(max_seconds, (int, float)) or not math.isfinite(max_seconds) or max_seconds <= 0:
        raise ValueError('max_seconds must be a positive finite number')
    if not isinstance(incremental, bool):
        raise ValueError('incremental must be true or false')
    if urls is not None and (not isinstance(urls, list) or any(not isinstance(url, str) for url in urls)):
        raise ValueError('urls must be a list of page URL strings')
    workspace = load_workspace(workspace_path)
    supplied = urls if urls is not None else [s['location'] for s in workspace['sources'] if s['scope'] == 'public']
    locations = list(dict.fromkeys(_canonical_url(url)[0] for url in supplied))
    if not locations or len(locations) > max_pages:
        raise ValueError('Supply between 1 and max_pages supported page URLs')
    unsupported = [urlsplit(url).hostname for url in locations if urlsplit(url).hostname not in HOST_ADAPTERS]
    if unsupported:
        raise ValueError('No supported site adapter for: ' + ', '.join(sorted(set(unsupported))))
    destination = Path(output_dir).resolve() if output_dir else Path(workspace_path).resolve().parent / 'site-prices'
    collector = collector or collect
    deadline = time.monotonic() + max_seconds
    sources, products, prefetched, coverage = [], [], {}, []
    # Release the output lock before execute() acquires its own lock.
    with workspace_lock(destination), robots_policy_scope():
        store = Store(destination)
        try:
            for url in locations:
                host = urlsplit(url).hostname
                adapter = HOST_ADAPTERS[host]
                sid = 'site_' + stable_id(url)
                source = Source(sid, adapter, 'web', url, [], adapter=adapter,
                                allowed_domains=[host], max_attempts=1, incremental=incremental,
                                recipe=_ready_recipe(adapter, url))
                candidates = []
                result = FetchResult(sid, 'budget_exhausted', 'none', message='Collection time limit reached')
                prefetched_responses: list[tuple[FetchResult, list]] = []
                best: tuple[FetchResult, list] | None = None
                best_score: tuple = (-1,)
                attempts = []
                # Unlike catalog collection, products here come from every
                # response, so an HTTP-only item would be lost if HTTP were
                # skipped. Keep both backends in the original order.
                for backend in source.backends:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        break
                    bounded = replace(source, timeout_seconds=min(120 if adapter == 'gongcar' else 30, remaining),
                                      recipe=source.recipe if backend == 'playwright' else [])
                    # Pipeline snapshots are keyed with the one backend that
                    # actually produced the retained response.
                    cache_source = replace(source, backends=[backend], recipe=[] if backend == 'http' else source.recipe)
                    saved = load_snapshot(store, destination, cache_source, backend)
                    try:
                        current = conditional_fetch(collector, bounded, str(Path(workspace_path).resolve().parent), backend, saved)
                    except Exception as exc:
                        current = FetchResult(sid, 'failed', backend, message=f'Collector error: {type(exc).__name__}')
                    if backend == 'playwright' and bounded.recipe:
                        current.trace.append({'event': 'collection_recipe', 'recipe': bounded.recipe})
                    result = current
                    attempt = {'backend': backend, 'status': current.status, 'at': current.fetched_at,
                               'message': current.message, 'trace': deepcopy(current.trace)}
                    attempts.append(attempt)
                    # Preserve failed and successful backend attempts in the
                    # prefetched contract so execute() records each one.
                    prefetched_responses.append((result, []))
                    if result.status == 'fetched':
                        try:
                            current_candidates = extract_current(result, source, saved, extract)
                        except Exception as exc:
                            current_candidates = []
                            result.message = 'Supported page format changed; extraction needs review'
                            result.trace.append({'code': 'extraction_error', 'type': type(exc).__name__})
                        attempt['message'] = result.message
                        attempt['trace'] = deepcopy(result.trace)
                        # Retain every usable response: execute() chooses the
                        # strongest response independently for each product.
                        prefetched_responses[-1] = (result, current_candidates)
                        identified = [c for c in current_candidates if c.fields.get('item_id') is not None and str(c.fields['item_id']).strip()]
                        per_identity = {}
                        for candidate in identified:
                            identity = str(candidate.fields['item_id'])
                            per_identity[identity] = max(
                                per_identity.get(identity, (-1,)),
                                _candidate_quality(candidate, source.decimal_separator),
                            )
                        # Proof quality leads; identity/row counts only break
                        # ties and cannot let hidden stale rows beat rendered UI.
                        sections = _sections(result, current_candidates, adapter)
                        score = (sum(s['status'] == 'complete' for s in sections), max(per_identity.values(), default=(-1,)), len(per_identity), len(identified))
                        if best is None or score > best_score:
                            best, best_score = (result, current_candidates), score
                        if (not sections or all(s['status'] == 'complete' for s in sections)) and identified and all(_has_sufficient_evidence(
                                [candidate for candidate in identified
                                 if str(candidate.fields['item_id']) == identity], source.decimal_separator)
                                for identity in per_identity):
                            break
                    if result.status == 'policy_denied':
                        break
                if best is not None:
                    result, candidates = best
                if result.backend == 'playwright' and source.recipe:
                    source.recipe_version = 'catalog-expansion-v2'
                if result.status == 'fetched' and not candidates and not result.message:
                    result.message = ('Select a vehicle in the calculator with a reviewed browser recipe; no selected quote was found.'
                                      if adapter == 'funrent' else 'No supported quote rows found; the rendered page format or selection needs review.')
                result.trace.append({'event': 'prefetched_attempts', 'attempts': attempts})
                identities = {}
                for response, response_candidates in prefetched_responses:
                    if response.status != 'fetched':
                        continue
                    for candidate in response_candidates:
                        identity = candidate.fields.get('item_id')
                        if identity is not None and str(identity).strip():
                            identities[str(identity)] = candidate.fields.get('name') or candidate.fields.get('model') or str(identity)
                for identity, name in identities.items():
                    pid = 'item_' + stable_id(sid, identity)
                    profile = 'unit' if adapter == 'amazoncar' and urlsplit(url).path.rstrip('/') == '/rentPrice' else 'rental'
                    products.append(Product(pid, str(name), identifiers={'item_id': identity}, price_profile=profile))
                    source.product_ids.append(pid)
                if not source.product_ids:
                    # An unresolved task is an explicit coverage gap, not an
                    # invented product or price observation.
                    pid = 'unresolved_' + stable_id(sid)
                    products.append(Product(pid, workspace['product']['name'], identifiers={'item_id': '__unresolved__'}, price_profile='rental'))
                    source.product_ids.append(pid)
                # execute() receives all usable responses so products missing
                # from a rendered fallback keep their original HTTP evidence.
                sources.append(source)
                prefetched[sid] = prefetched_responses or [(result, candidates)]
                coverage.append({'source_id': sid, 'adapter': adapter, 'url': url,
                                 'status': result.status, 'items': len(identities),
                                 'sections': _sections(result, candidates, adapter),
                                 'coverage_gap': not bool(identities) or any(s['status'] != 'complete' for s in _sections(result, candidates, adapter)),
                                 'reason': result.message, 'trace': result.trace})
        finally:
            store.close()
    config = CollectionConfig(f"{workspace['industry']} — {workspace['product']['name']} — {workspace['market']}",
        products, sources, str(destination), str(destination),
        max_run_seconds=max(5, deadline - time.monotonic()))
    # The response has already been freshly fetched and extracted above. The
    # ordinary pipeline saves evidence and performs fresh validation.
    result = execute(config, prefetched=prefetched)
    _save_coverage(destination, result, coverage)
    result['status'] = apply_status(result['status'], coverage)
    from .final_report import finalize_reports
    artifacts = finalize_reports([Path(result['report_path'])], Path(result['report_path']), metadata={
        'status': result['status'], 'coverage': coverage,
        'coverage_summary': summarize(coverage), 'target_count': len(locations),
    })
    result['dashboard_path'] = artifacts['dashboard_path']
    config_path = destination / f"collection-{result['id']}.json"
    document = asdict(config)
    document.pop('base_dir')
    _atomic_write(config_path, document)
    return {**result, 'config_path': str(config_path), 'coverage': coverage, 'coverage_summary': summarize(coverage),
            'scope': 'Supplied pages only. Item counts are not whole-site coverage.',
            'note': 'Calculator estimates remain separate from observed prices. Unselected or unsupported pages need review.'}
