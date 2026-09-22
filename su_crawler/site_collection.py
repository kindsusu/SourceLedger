"""Simple bounded collection for known page formats, with ordinary pipeline output."""
from __future__ import annotations

from dataclasses import asdict, replace
from copy import deepcopy
from pathlib import Path
import math
import time
from urllib.parse import urlsplit

from .collectors import collect
from .extraction import extract
from .models import CollectionConfig, FetchResult, Product, Source, stable_id
from .pipeline import execute, _candidate_quality, _has_sufficient_evidence
from .research import _canonical_url, _atomic_write, load_workspace
from .storage import Store, workspace_lock
from .incremental import load_snapshot, conditional_fetch, extract_current


HOST_ADAPTERS = {
    'jetcar.kr': 'jetcar', 'www.jetcar.kr': 'jetcar',
    'jetcar.co.kr': 'jetcar', 'www.jetcar.co.kr': 'jetcar',
    'jetcar2.co.kr': 'jetcar', 'www.jetcar2.co.kr': 'jetcar',
    'gongcarrent.kr': 'gongcar', 'www.gongcarrent.kr': 'gongcar',
    'go.funrentcar.com': 'funrent',
}


def _ready_recipe(adapter: str) -> list[dict]:
    selectors = {'gongcar': 'table tbody tr td', 'funrent': '#estCar'}
    return [{'action': 'wait_for', 'selector': selectors[adapter], 'state': 'visible'}] if adapter in selectors else []


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
    with workspace_lock(destination):
        store = Store(destination)
        try:
            for url in locations:
                host = urlsplit(url).hostname
                adapter = HOST_ADAPTERS[host]
                sid = 'site_' + stable_id(url)
                source = Source(sid, adapter, 'web', url, [], adapter=adapter,
                                allowed_domains=[host], max_attempts=1, incremental=incremental)
                candidates = []
                result = FetchResult(sid, 'budget_exhausted', 'none', message='Collection time limit reached')
                prefetched_responses: list[tuple[FetchResult, list]] = []
                best: tuple[FetchResult, list] | None = None
                best_score: tuple = (-1,)
                attempts = []
                for backend in source.backends:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        break
                    bounded = replace(source, timeout_seconds=min(30, remaining))
                    if backend == 'playwright':
                        bounded = replace(bounded, recipe=_ready_recipe(adapter))
                    # Pipeline snapshots are keyed with the one backend that
                    # actually produced the retained response.
                    cache_source = replace(source, backends=[backend])
                    saved = load_snapshot(store, destination, cache_source, backend)
                    try:
                        current = conditional_fetch(collector, bounded, str(Path(workspace_path).resolve().parent), backend, saved)
                    except Exception as exc:
                        current = FetchResult(sid, 'failed', backend, message=f'Collector error: {type(exc).__name__}')
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
                        score = (max(per_identity.values(), default=(-1,)), len(per_identity), len(identified))
                        if best is None or score > best_score:
                            best, best_score = (result, current_candidates), score
                        if identified and all(_has_sufficient_evidence(
                                [candidate for candidate in identified
                                 if str(candidate.fields['item_id']) == identity], source.decimal_separator)
                                for identity in per_identity):
                            break
                    if result.status == 'policy_denied':
                        break
                if best is not None:
                    result, candidates = best
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
                    products.append(Product(pid, str(name), identifiers={'item_id': identity}, price_profile='rental'))
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
                                 'coverage_gap': not bool(identities),
                                 'reason': result.message, 'trace': result.trace})
        finally:
            store.close()
    config = CollectionConfig(f"{workspace['industry']} — {workspace['product']['name']} — {workspace['market']}",
        products, sources, str(destination), str(Path(workspace_path).resolve().parent),
        max_run_seconds=max(5, deadline - time.monotonic()))
    # The response has already been freshly fetched and extracted above. The
    # ordinary pipeline saves evidence and performs fresh validation.
    result = execute(config, prefetched=prefetched)
    config_path = destination / f"collection-{result['id']}.json"
    document = asdict(config)
    document.pop('base_dir')
    _atomic_write(config_path, document)
    return {**result, 'config_path': str(config_path), 'coverage': coverage,
            'scope': 'Supplied pages only. Item counts are not whole-site coverage.',
            'note': 'Calculator estimates remain separate from observed prices. Unselected or unsupported pages need review.'}
