"""Bounded collection of pages selected in a confirmed research plan.

Known site adapters run first; any other page goes through the generic chain
(storefront platforms, structured data, visible-price heuristics). Whatever
cannot be tied to a visible or structured price remains source evidence and
an explicit coverage gap.
"""
from __future__ import annotations

from collections import Counter, deque
from concurrent.futures import ThreadPoolExecutor, Future, wait, FIRST_COMPLETED
from contextlib import ExitStack
from queue import Queue, Empty
from threading import Event, Lock, Thread
from dataclasses import asdict, replace
from datetime import datetime, timedelta, timezone
import json
import math
import re
from pathlib import Path
import time
from urllib.parse import urljoin, urlsplit

from bs4 import BeautifulSoup

from .collectors import collect
from .collectors import browser_session
from .collectors import (HttpClientPool, RobotsPolicyCache, active_job_resources, http_client_scope,
                         robots_policy_scope, _cards_digest)
from .extraction import extract
from .models import Candidate, CollectionConfig, FetchResult, Product, Source, stable_id
from .pipeline import execute, _candidate_quality, _has_sufficient_evidence
from .research import _atomic_write
from .research_plans import canonical_url
from .site_collection import HOST_ADAPTERS, _ready_recipe, _sections, _save_coverage, _unsettled, browser_first
from .browser_sampling import (DEFAULT_BROWSER_SAMPLE_RATIO, SAMPLED_ADAPTERS, BrowserSamplingPolicy,
                               active_sampling_policy, execution_counters, rows_match, validate_sample_ratio)
from .scope_conditions import assess_conditions, normalize_conditions, normalize_text
from .catalog_checkpoint import read_checkpoint, write_checkpoint
from .catalog_scheduler import HostGate, PageLedger, fetch_http
from .storage import Store
from .coverage import summarize, apply_status, unknown_section
from .incremental import extraction_version
from .incremental import conditional_fetch, extract_current
from .catalog_refresh import (normalize_refresh_policy, scope_key, read_baseline, write_baseline,
                              plan_scope_key, read_page_baselines, write_page_baselines, merge_page_baselines,
                              decide, item_changes, semantic_candidates, validated_http, page_baseline,
                              save_refresh_run, source_signature)


def _url(value: str) -> str:
    return canonical_url(value)


def _nodes(value, depth=0):
    from .structured_data import nodes
    yield from nodes(value, depth=depth)


def _jsonld_candidates(result: FetchResult) -> list[Candidate]:
    """Adapter-less extraction: platforms, structured data, then visible-price heuristics."""
    from .generic_extraction import extract_generic
    return extract_generic(result)


def _url_or_none(value: str) -> str | None:
    try:
        return _url(value)
    except ValueError:
        return None


def _links(result: FetchResult, page_url: str, host: str, excluded: set[str]):
    from .adapters.jetcar import is_detail_url
    if result.status != "fetched" or "html" not in result.media_type.lower():
        return []
    links = []
    # Earlier pagination states can contain links absent from the final DOM.
    captures = [(result.content, result.final_url or page_url)]
    captures.extend((state['content'], state.get('final_url') or page_url)
                    for state in result.variant_snapshots)
    seen = set()
    for content, base_url in captures:
        soup = BeautifulSoup(content, "html.parser")
        for anchor in soup.select("a[href]"):
            try:
                link = _url(urljoin(base_url, anchor["href"]))
            except ValueError:
                continue
            if HOST_ADAPTERS.get(host) == "jetcar" and not is_detail_url(link):
                continue
            if urlsplit(link).hostname == host and link not in excluded and link not in seen:
                seen.add(link)
                links.append(link)
    if not HOST_ADAPTERS.get(host):
        # Spend the page budget on products first: card links found on a list
        # page, then its pager, then every other same-host link.
        from .generic_extraction import pagination_links, product_links
        preferred = [link for link in (_url_or_none(value) for value in
                                       [*product_links(result), *pagination_links(result)])
                     if link in seen]
        links = list(dict.fromkeys([*preferred, *links]))
    # Bound work explicitly; a truncated frontier is never an empty remainder.
    if len(links) > 1000:
        result.trace.append({"event": "discovery_truncated", "reason": "Link discovery limit reached",
                             "observed_count": len(links), "retained_count": 1000})
    return links[:1000]


# Optimized workers get this long after the deadline to return a capture
# before their pages are recorded as interrupted and the batch finishes.
# A browser capture can still be snapshotting, closing its context or
# extracting a large expanded list when the deadline passes.
RESULT_GRACE_SECONDS = 30.0
# Browser-first pages keep this much of the batch budget for an HTTP fallback.
HTTP_FALLBACK_RESERVE_SECONDS = 10.0


def _coverage_trace(events):
    """Checkpointed coverage keeps counts and digests, not full card HTML.

    Each page's retained responses (an immutable checkpoint page blob) keep
    the full observed cards for audit; coverage is re-serialized on every
    checkpoint save and must stay small.
    """
    compact = []
    for event in events:
        if isinstance(event, dict) and isinstance(event.get('observed_cards'), list):
            cards = event['observed_cards']
            event = {key: value for key, value in event.items() if key != 'observed_cards'}
            event['observed_cards_count'] = len(cards)
            if 'observed_cards_sha256' not in event:
                event['observed_cards_sha256'] = _cards_digest(cards)
        compact.append(event)
    return compact


class _WorkerStopped(Exception):
    """A page worker abandoned a queued browser task during shutdown."""


def collection_rules_version():
    """Fingerprint effective extraction, validation, and coverage policy."""
    import hashlib
    root = Path(__file__).parent
    digest = hashlib.sha256(extraction_version().encode())
    for name in ('catalog_collection.py', 'site_collection.py', 'coverage.py', 'validation.py',
                 'pipeline.py', 'report.py', 'result_view.py', 'collection_rules.py', 'collection_issues.py',
                 'browser_enumeration.py', 'browser_workers.py', 'browser_sampling.py', 'catalog_refresh.py',
                 'incremental.py', 'collectors.py'):
        digest.update((root / name).read_bytes())
    return digest.hexdigest()


def _scope_candidates(candidates, includes, excludes, conditions):
    kept, assessments = [], []
    for candidate in candidates:
        name = str(candidate.fields.get('name') or candidate.fields.get('model') or '')[:160]
        if not _term_match(candidate, includes, excludes):
            assessments.append({'name': name, 'locator': candidate.locator, 'status': 'excluded',
                                'reason': 'explicit text filter', 'checks': []})
            continue
        assessment = assess_conditions(candidate, conditions)
        assessments.append({'name': name, 'locator': candidate.locator, 'status': assessment['status'],
                            'reason': assessment.get('reason', ''), 'checks': assessment['checks']})
        if assessment['status'] == 'excluded':
            continue
        candidate.derived_values['scope_assessment'] = assessment
        flag = 'Research-plan scope and natural-language conditions have not been independently verified'
        if flag not in candidate.review_flags:
            candidate.review_flags.append(flag)
        if assessment['status'] in {'unknown', 'not_checked'}:
            flag = 'Structured product conditions lack evidence'
            if flag not in candidate.review_flags:
                candidate.review_flags.append(flag)
        kept.append(candidate)
    return kept, assessments


def _reanalyse(captures, coverage, includes, excludes, conditions):
    """Rebuild candidates from retained bytes; never refetch or alter capture times."""
    by_url = {page['url']: page for page in coverage}
    rebuilt = {}
    for url, data in captures.items():
        source = Source(**{**data['source'], 'product_ids': []})
        responses, best, best_score = [], None, (-1,)
        raw_http_candidates = None
        for raw_result, _ in data['responses']:
            result = FetchResult(**raw_result)
            prior_extraction_failure = any(e.get('code') == 'extraction_error' for e in result.trace)
            result.trace = [e for e in result.trace if e.get('event') not in {'extraction_gap', 'jsonld_truncated'}
                            and e.get('code') not in {'extraction_reused', 'extraction_error'}]
            if prior_extraction_failure:
                # The retained fetch succeeded; only its old extraction failed.
                result.status, result.message = 'fetched', ''
            candidates = []
            if result.status == 'fetched':
                try:
                    candidates = extract(result, source) if source.adapter or source.selectors or getattr(result, 'variant_snapshots', []) else []
                    if not candidates:
                        candidates = _jsonld_candidates(result)
                    if result.backend == 'http':
                        raw_http_candidates = [asdict(candidate) for candidate in candidates]
                except Exception as exc:
                    candidates = []
                    result.status = 'failed'
                    result.message = f'Extraction needs review: {type(exc).__name__}'
                    result.trace.append({'code': 'extraction_error', 'type': type(exc).__name__})
            sections = _sections(result, candidates, source.adapter) if result.status == 'fetched' else []
            candidates, assessments = _scope_candidates(candidates, includes, excludes, conditions)
            score = (result.status == 'fetched', bool(candidates), sum(s['status'] == 'complete' for s in sections),
                     max((_candidate_quality(c) for c in candidates), default=(-1,)), len(candidates))
            if best is None or score > best_score:
                best, best_score = (result, candidates, assessments, sections), score
            responses.append((asdict(result), [asdict(c) for c in candidates]))
        if best is None:
            raise ValueError('Checkpoint has no retained response to reanalyse')
        result, candidates, assessments, sections = best
        challenge = next((raw for raw, _ in reversed(responses)
                          if any(event.get('event') == 'access_challenge'
                                 for event in raw.get('trace', []))), None)
        products = _products_for(source, candidates, source.name)
        rebuilt[url] = {'source': asdict(source), 'products': [asdict(p) for p in products],
                        'raw_http_candidates': raw_http_candidates,
                        'chosen_candidates': [asdict(c) for c in candidates], 'responses': responses}
        page = by_url.get(url)
        if page is not None:
            coverage_status = challenge['status'] if challenge else result.status
            coverage_message = challenge['message'] if challenge else result.message
            page.update(sections=sections, products=len(candidates), products_extracted=len(assessments),
                         status=coverage_status if coverage_status != 'fetched' else
                                'visited' if candidates else 'no_data',
                         reason=coverage_message or
                                ('No supported structured product data or no products satisfying the explicit scope'
                                 if not candidates and coverage_status == 'fetched' else ''),
                         trace=_coverage_trace(event for response, _ in responses for event in response.get('trace', [])),
                        scope_counts={key: sum(a['status'] == key for a in assessments)
                                      for key in ('matched', 'unknown', 'excluded', 'not_checked')},
                        scope_assessments=assessments[:100], scope_assessments_truncated=max(0, len(assessments)-100),
                        excluded_locators=[a['locator'] for a in assessments if a['status'] == 'excluded'],
                        reanalysed=True)
    return rebuilt


def _term_match(candidate: Candidate, includes: list[str], excludes: list[str]) -> bool:
    text = " ".join(str(v) for k, v in candidate.fields.items() if k not in {"price", "currency"} and v is not None)
    text += " " + " ".join(str(v) for v in candidate.specs.values())
    text = normalize_text(text)
    return (not includes or any(term in text for term in includes)) and not any(term in text for term in excludes)


def _price_profile(source: Source, candidate: Candidate) -> str:
    """Vehicle adapters keep the rental contract; other offers are sales or recurring fees."""
    from .validation import RECURRING_BASES
    if source.adapter:
        return "rental"
    return "recurring" if candidate.fields.get("price_basis") in RECURRING_BASES else "sale"


def _products_for(source: Source, candidates: list[Candidate], fallback_name: str) -> list[Product]:
    products, seen = [], set()
    for candidate in candidates:
        fields = candidate.fields
        identity = next(((key, str(fields[key])) for key in ("sku", "item_id", "mpn", "gtin13", "gtin12", "gtin14", "gtin8", "gtin",
                                                             "url_id", "model", "name")
                         if fields.get(key) is not None and str(fields[key]).strip()), None)
        if identity is None or identity in seen:
            continue
        seen.add(identity)
        product_id = "catalog_" + stable_id(source.id, *identity)
        products.append(Product(product_id, str(fields.get("name") or fields.get("model") or identity[1]),
                                identifiers={identity[0]: identity[1]},
                                price_profile=_price_profile(source, candidate)))
        source.product_ids.append(product_id)
    if not products:
        product_id = "catalog_unresolved_" + stable_id(source.id)
        products.append(Product(product_id, fallback_name, price_profile="rental" if source.adapter else "sale"))
        source.product_ids.append(product_id)
    return products


def collect_plan(root: str | Path, snapshot: dict, *, output_dir: str | Path,
                 max_pages: int = 10, max_seconds: float = 120, collector=None,
                 checkpoint_id: str | None = None, retry_urls: list[str] | None = None,
                 parent_checkpoint_id: str | None = None, follow_links: bool = True,
                 concurrency: int = 1, browser_reuse: bool = False,
                 browser_concurrency: int = 1,
                 browser_sample_ratio: float = DEFAULT_BROWSER_SAMPLE_RATIO) -> dict:
    """Collect selected pages and at most ``max_pages`` same-host pages total.

    The returned coverage is page-scoped, never a claim about whole-site coverage.
    ``browser_sample_ratio`` (0 < ratio <= 1) is the share of Jetcar pages whose
    complete static HTML is still verified in the browser; 1.0 always runs the
    browser. An enclosing ``browser_sampling_scope`` (a research batch job)
    supplies one job-wide policy instead.
    """
    if not isinstance(snapshot, dict) or snapshot.get("state") != "confirmed":
        raise ValueError("A confirmed plan snapshot is required")
    topic = snapshot.get("topic") or {}
    if not all(isinstance(topic.get(key), str) and topic[key].strip() for key in ("industry", "product", "market")):
        raise ValueError("Plan topic requires industry, product, and market")
    if not isinstance(max_pages, int) or isinstance(max_pages, bool) or not 1 <= max_pages <= 50:
        raise ValueError("max_pages must be between 1 and 50")
    if not isinstance(follow_links, bool):
        raise ValueError("follow_links must be a boolean")
    if not isinstance(concurrency, int) or isinstance(concurrency, bool) or not 1 <= concurrency <= 4:
        raise ValueError("concurrency must be between 1 and 4")
    if not isinstance(browser_reuse, bool):
        raise ValueError("browser_reuse must be a boolean")
    if isinstance(browser_concurrency, bool) or not isinstance(browser_concurrency, int) or not 1 <= browser_concurrency <= min(2, concurrency):
        raise ValueError("browser_concurrency must be 1 or 2 and no greater than concurrency")
    if not isinstance(max_seconds, (int, float)) or isinstance(max_seconds, bool) or not math.isfinite(max_seconds) or max_seconds <= 0:
        raise ValueError("max_seconds must be a positive finite number")
    browser_sample_ratio = validate_sample_ratio(browser_sample_ratio)
    includes = snapshot.get("include_terms", [])
    excludes = snapshot.get("exclude_terms", [])
    if any(not isinstance(terms, list) or any(not isinstance(x, str) for x in terms) for terms in (includes, excludes)):
        raise ValueError("Plan terms must be lists of strings")
    includes = [normalize_text(x) for x in includes if x.strip()]
    excludes = [normalize_text(x) for x in excludes if x.strip()]
    conditions = normalize_conditions(snapshot.get("conditions", []))
    excluded_urls = {_url(x) for x in snapshot.get("excluded_urls", [])}
    selections = snapshot.get("candidates", [])
    if not isinstance(selections, list):
        raise ValueError("Plan candidates must be a list")
    selected = []
    unchecked = set()
    for item in selections:
        if not isinstance(item, dict):
            raise ValueError("Invalid plan candidate")
        if item.get("selected"):
            selected.append((_url(item["url"]), item))
        else:
            unchecked.add(_url(item["url"]))
    excluded_urls.update(unchecked)
    if not selected:
        raise ValueError("Plan has no selected pages")
    chosen = list(dict.fromkeys(url for url, _ in selected if url not in excluded_urls))
    if not chosen:
        raise ValueError("Every selected page is excluded")
    if len(chosen) > max_pages and retry_urls is None:
        raise ValueError("Selected pages exceed max_pages")
    allowed_hosts = {urlsplit(url).hostname for url in chosen}
    destination = Path(output_dir).resolve()
    base_dir = str(Path(root).resolve())
    from .collection_rules import normalize_rules, apply_rule
    collection_rules = normalize_rules(snapshot.get('collection_rules', {}), {url for url, _ in selected} | unchecked)
    fetch = collector or collect
    deadline = time.monotonic() + max_seconds
    queue = deque((url, "selected") for url in chosen)
    queued = set(chosen)
    visited = set()
    coverage, sources, products, prefetched = [], [], [], {}
    for url in sorted(unchecked):
        coverage.append({"url": url, "status": "excluded", "reason": "candidate not selected"})
    for url, item in selected:
        if url in excluded_urls:
            coverage.append({"url": url, "status": "excluded", "reason": "excluded_urls", "candidate_id": item.get("id")})
    binding = stable_id(snapshot, max_pages, max_seconds, retry_urls, parent_checkpoint_id)
    binding = stable_id(binding, 'catalog-expansion-v2',
                        [(url, _ready_recipe(HOST_ADAPTERS.get(urlsplit(url).hostname), url)) for url in chosen])
    if not follow_links:
        binding = stable_id(binding, "selected_urls_only")
    if concurrency != 1:
        binding = stable_id(binding, "parallel_http_v1", concurrency)
    if browser_reuse or browser_concurrency != 1:
        binding = stable_id(binding, "browser_pool_v1", browser_reuse, browser_concurrency)
    state = read_checkpoint(destination, checkpoint_id) if checkpoint_id else None
    rules_version = collection_rules_version()
    # Legacy snapshots omitted this field and retain their historical full-fetch
    # behavior. The plan editor writes the normalized default for new reviews.
    refresh_policy = (normalize_refresh_policy(snapshot['refresh_policy'])
                      if 'refresh_policy' in snapshot else {'mode': 'full', 'full_interval_days': 7})
    refresh_key = scope_key(snapshot, chosen, follow_links=follow_links)
    pages_key = plan_scope_key(snapshot, follow_links=follow_links)
    if state and 'refresh_baseline' in state:
        prior_baseline = state.get('refresh_baseline')
    else:
        # Batch membership shifts when the approved URL list changes; each
        # selected page still compares with its newest per-URL baseline.
        prior_baseline = merge_page_baselines(read_baseline(destination, refresh_key),
                                              read_page_baselines(destination, pages_key, chosen))
    refresh_decision = (state.get('refresh_decision') if state else None) or decide(
        refresh_policy, prior_baseline, rules_version,
        manual_full=bool(retry_urls is not None))
    baseline_pages = (prior_baseline or {}).get('pages', {})
    retained_http = {url: validated_http(page, destination) for url, page in baseline_pages.items()}
    if not state and refresh_decision['effective_mode'] == 'incremental' and any(
            page.get('http') and retained_http.get(url) is None for url, page in baseline_pages.items()):
        refresh_decision = decide(refresh_policy, {**prior_baseline, 'valid': False}, rules_version)
    def current_fetch(source, base_dir, backend):
        previous_page = baseline_pages.get(source.location) or {}
        saved = (retained_http.get(source.location) if refresh_decision['effective_mode'] == 'incremental'
                 and previous_page.get('source_signature') == source_signature(source) else None)
        if (backend == 'http' and saved and not source.recipe and not source.adapter and not source.selectors
                and not source.profile_dir and source.account_scope == 'public'):
            return conditional_fetch(fetch, source, base_dir, backend, saved)
        return fetch(source, base_dir, backend)

    def extraction_snapshot(source, backend):
        previous = baseline_pages.get(source.location) or {}
        if (backend != 'http' or refresh_decision['effective_mode'] != 'incremental'
                or previous.get('source_signature') != source_signature(source)
                or source.recipe or source.adapter or source.selectors or source.profile_dir
                or source.internal or source.account_scope != 'public'):
            return None
        saved = retained_http.get(source.location)
        return saved if saved and 'parsed_candidates' in saved else None
    reanalysed = False
    if state and state["binding"] != binding:
        raise ValueError("Collection checkpoint scope or budget changed")
    if state and state.get("rules_version") != rules_version:
        # Keep an immutable prior checkpoint/result for audit. The new run ID
        # includes the rules version, so historical observations are preserved.
        old_version = state.get('rules_version', 'legacy')
        write_checkpoint(destination, checkpoint_id + '@' + old_version + '@' + stable_id(state), state)
        state['captures'] = _reanalyse(state['captures'], state['coverage'], includes, excludes, conditions)
        state['result'] = None
        state['rules_version'] = rules_version
        reanalysed = True
    if state and state.get("result"):
        from .final_report import finalize_reports
        cached = state["result"]
        report_path = Path(cached['report_path'])
        # Old checkpoints predate HTML reports; resume repairs the deliverable
        # from the retained workbook without fetching any page again.
        artifacts = finalize_reports([report_path], report_path, metadata={
            "status": cached['status'], "coverage": cached.get('coverage', []),
            "coverage_summary": cached.get('coverage_summary', {}), "target_count": len(selected),
        })
        cached['dashboard_path'] = artifacts['dashboard_path']
        write_checkpoint(destination, checkpoint_id, state)
        return state["result"]
    captures = {}
    elapsed, dispatched = 0.0, 0
    retry_total = 0
    if not state and retry_urls is not None:
        parent = read_checkpoint(destination, parent_checkpoint_id) if parent_checkpoint_id else None
        if not parent or not parent.get("result"):
            raise ValueError("Retry requires a completed parent checkpoint")
        targets = {_url(url) for url in retry_urls}
        allowed = {item["url"] for item in parent["result"]["coverage"] if item["status"] != "excluded"}
        if not targets or len(targets) > max_pages or not targets <= allowed or targets & excluded_urls:
            raise ValueError("Retry URLs must be previously covered, approved pages within the new budget")
        captures = {url: data for url, data in parent["captures"].items() if url not in targets}
        coverage = [item for item in parent["result"]["coverage"] if item["url"] not in targets]
        if parent.get('rules_version') != rules_version:
            captures = _reanalyse(captures, coverage, includes, excludes, conditions)
            reanalysed = True
        visited = set(captures)
        queue = deque((url, "retry") for url in sorted(targets))
        queued = set(visited) | targets
    if state:
        captures, coverage = state["captures"], state["coverage"]
        queue, visited, queued = deque(state["queue"]), set(state["visited"]), set(state["queued"])
        elapsed, dispatched = state["elapsed"], state["dispatched"]
        retry_total = state.get("transient_retries", 0)
        if state.get("inflight"):
            coverage.append({"url": state["inflight"], "status": "interrupted",
                             "reason": "Worker stopped during this page; reserved time was charged. Retry explicitly."})
        for leased_url in state.get("parallel_leases", []):
            coverage.append({"url": leased_url, "status": "interrupted",
                             "reason": "Worker stopped during a reserved page; reserved time was charged. Retry explicitly."})
    for data in captures.values():
        sources.append(Source(**data["source"]))
        products.extend(Product(**item) for item in data["products"])
        prefetched[data["source"]["id"]] = [(FetchResult(**result), [Candidate(**c) for c in candidates])
                                            for result, candidates in data["responses"]]
    # Jetcar HTTP acceptance with sampled browser verification. A job scope
    # shares one policy (and its mismatch fallback) across batches; otherwise
    # this call samples its own remaining pages. A persisted fallback is kept;
    # an unknown prior outcome is never treated as a pass.
    sampling = active_sampling_policy() or BrowserSamplingPolicy(
        browser_sample_ratio, checkpoint_id or binding, [url for url, _ in queue if url not in visited])
    if state and (state.get('browser_sampling') or {}).get('fallback'):
        sampling.mark_fallback()
    started = time.monotonic()
    deadline = started + max(0, max_seconds - elapsed)
    collection_finished = bool(state and state.get("collection_finished"))
    parallel_leases: list[str] = []
    checkpoint_seconds = 0.0
    checkpoint_pages = {}
    browser_active = browser_peak = 0
    browser_active_lock = Lock()
    pending = deque()
    gate = HostGate(concurrency=concurrency)
    # A research batch job may wrap several batches in collection_job_scope();
    # reuse its robots decisions and HTTP connections across batches.
    ambient_robots, ambient_pool = active_job_resources()
    policy_cache = ambient_robots or RobotsPolicyCache()
    http_pool = ambient_pool or HttpClientPool()
    owns_http_pool = ambient_pool is None
    optimized = (browser_reuse or browser_concurrency > 1 or bool(collection_rules)) and not collection_finished
    executor = ThreadPoolExecutor(max_workers=concurrency) if concurrency > 1 and not optimized and not collection_finished else None
    ledger = PageLedger(destination, checkpoint_id) if concurrency > 1 and checkpoint_id else None
    if ledger:
        ledger.reconcile(state, [url for url, _ in queue])

    def save(*, inflight=None, reserve=None, result=None):
        nonlocal checkpoint_seconds
        if checkpoint_id:
            if reserve is None:
                reserve = max((240 if HOST_ADAPTERS.get(urlsplit(u).hostname) == 'gongcar' else 60
                               for u in parallel_leases), default=0)
            checkpoint_started = time.monotonic()
            write_checkpoint(destination, checkpoint_id, {
                "binding": binding, "rules_version": rules_version, "refresh_decision": refresh_decision,
                "refresh_key": refresh_key, "refresh_baseline": prior_baseline,
                "captures": captures, "coverage": coverage,
                "queue": list(queue), "visited": sorted(visited), "queued": sorted(queued),
                "elapsed": min(max_seconds, elapsed + time.monotonic() - started + reserve),
                "dispatched": dispatched, "inflight": inflight, "result": result,
                "transient_retries": retry_total,
                "parallel_leases": list(parallel_leases),
                "browser_sampling": sampling.state(),
                "collection_finished": collection_finished}, page_cache=checkpoint_pages)
            checkpoint_seconds += time.monotonic() - checkpoint_started

    def _capture_page(url, prefetched_http=None, prefetched_http_seconds=0.0):
        # No SQLite, checkpoint, or report state is touched by worker threads.
        nonlocal browser_active, browser_peak
        page_started = time.monotonic()
        fetch_seconds, extraction_seconds = prefetched_http_seconds, 0.0
        host = urlsplit(url).hostname
        adapter = HOST_ADAPTERS.get(host)
        source_id = "catalog_source_" + stable_id(url)
        source = Source(source_id, host, "web", url, [], allowed_domains=[host], adapter=adapter,
                        max_attempts=1, backends=["http", "playwright"], min_interval_seconds=0,
                        recipe=_ready_recipe(adapter, url))
        source = apply_rule(source, collection_rules, base_dir)
        adapter = source.adapter
        responses = []
        raw_http_candidates = None
        chosen_result, chosen_candidates, chosen_assessments, chosen_sections, best_score = None, [], [], [], (-1,)
        prefer_browser = browser_first(source)
        backends = (["playwright", *[b for b in source.backends if b != "playwright"]]
                    if prefer_browser else list(source.backends))
        fallback_reason = None
        sample_decision, http_rows, browser_rows = None, [], []
        for backend in backends:
            backend_started = time.monotonic()
            remaining = deadline - time.monotonic()
            if remaining <= 0 and not (backend == "http" and prefetched_http is not None):
                break
            page_limit = 120 if adapter == 'gongcar' else 30
            if prefer_browser and backend == "playwright" and remaining > 3 * HTTP_FALLBACK_RESERVE_SECONDS:
                # Leave the HTTP fallback a chance near the end of the budget.
                page_limit = min(page_limit, remaining - HTTP_FALLBACK_RESERVE_SECONDS)
            bounded = replace(source, timeout_seconds=max(0.001, min(page_limit, remaining)),
                              recipe=source.recipe if backend == "playwright" else [])

            def fetch_once(backend=backend, bounded=bounded):
                nonlocal browser_active, browser_peak
                if optimized and backend == "playwright" and time.monotonic() >= deadline:
                    return FetchResult(source_id, "budget_exhausted", backend,
                                       message="Collection time limit reached while waiting for browser slot")
                try:
                    permit = gate.enter(host, deadline) if executor is not None or optimized else None
                    counted_browser = False
                    try:
                        if (executor is not None or optimized) and permit is None:
                            return FetchResult(source_id, "budget_exhausted", backend,
                                               message="Collection time limit reached while waiting for host slot")
                        if permit is not None:
                            left = deadline - time.monotonic()
                            if left <= 0:
                                return FetchResult(source_id, "budget_exhausted", backend,
                                                   message="Collection time limit reached before browser fetch")
                            bounded = replace(bounded, timeout_seconds=min(bounded.timeout_seconds, left))
                        if backend == "playwright":
                            with browser_active_lock:
                                browser_active += 1
                                browser_peak = max(browser_peak, browser_active)
                            counted_browser = True
                        return current_fetch(bounded, base_dir, backend)
                    finally:
                        if counted_browser:
                            with browser_active_lock:
                                browser_active -= 1
                        if permit is not None:
                            permit.release()
                except Exception as exc:
                    return FetchResult(source_id, "failed", backend, message=f"Collector error: {type(exc).__name__}")

            if backend == "http" and optimized:
                result, attempts = fetch_http(url, source=source, fetch=current_fetch,
                                              base_dir=base_dir, deadline=deadline, gate=gate)
                retry_counts[url] = attempts
            elif backend == "http" and prefetched_http is not None:
                result = prefetched_http
                backend_started = time.monotonic() - prefetched_http_seconds
            elif optimized and backend == "playwright":
                # Chromium lives on a dedicated browser thread; this page
                # worker keeps HTTP and extraction work off that thread.
                result = run_on_browser(fetch_once)
            else:
                result = fetch_once()
            if backend == "http" and fallback_reason:
                result.trace.append({"event": "http_fallback", "reason": fallback_reason})
            fetch_seconds += time.monotonic() - backend_started
            extraction_started = time.monotonic()
            if result.status == "fetched":
                final = result.final_url or url
                try:
                    if urlsplit(_url(final)).hostname != host or _url(final) in excluded_urls:
                        result = FetchResult(source_id, "policy_denied", backend, message="Redirect left selected host or reached excluded URL")
                except ValueError:
                    result = FetchResult(source_id, "policy_denied", backend, message="Invalid response URL")
            candidates = []
            assessments = []
            sections = []
            unscoped = []
            if backend == "playwright" and bounded.recipe:
                result.trace.append({"event": "collection_recipe", "recipe": bounded.recipe})
            if result.status == "fetched":
                try:
                    def extract_page(response, configured_source):
                        found = extract(response, configured_source) if adapter or configured_source.selectors or getattr(response, 'variant_snapshots', []) else []
                        return found or _jsonld_candidates(response)

                    saved_extraction = extraction_snapshot(source, backend) if not result.variant_snapshots else None
                    candidates = extract_current(result, source, saved_extraction, extract_page)
                    if saved_extraction and any(event.get('code') == 'extraction_reused' for event in result.trace):
                        result.trace.extend(saved_extraction.get('extraction_trace', []))
                    if backend == 'http':
                        # Cache before scope filtering/mutation so excluded rows
                        # and their assessments are reconstructed on every run.
                        raw_http_candidates = [asdict(candidate) for candidate in candidates]
                except Exception as exc:
                    candidates = []
                    result.status = 'failed'
                    result.message = f"Extraction needs review: {type(exc).__name__}"
                    result.trace.append({'code': 'extraction_error', 'type': type(exc).__name__})
                # Section discovery describes the public page. Scope exclusions
                # affect retained observations, never the observed card count.
                sections = _sections(result, candidates, adapter) if result.status == 'fetched' else []
                unscoped = list(candidates)
                candidates, assessments = _scope_candidates(candidates, includes, excludes, conditions)
                if backend == 'playwright' and result.status == 'fetched':
                    browser_rows = unscoped
            responses.append((result, candidates))
            sections = sections if result.status == 'fetched' else []
            score = (result.status == 'fetched', bool(candidates), sum(s['status'] == 'complete' for s in sections), max((_candidate_quality(c) for c in candidates), default=(-1,)), len(candidates))
            if chosen_result is None or score > best_score:
                chosen_result, chosen_candidates, chosen_assessments, chosen_sections, best_score = result, candidates, assessments, sections, score
            if result.status in {"policy_denied", "needs_auth"}:
                break
            if any(event.get('event') == 'access_challenge' for event in result.trace):
                break  # Explicit host-browser recovery owns further challenge attempts.
            extraction_seconds += time.monotonic() - extraction_started
            if prefer_browser and backend == "playwright":
                if result.status == "fetched" and assessments:
                    # Static HTML cannot complete these sections or outrank
                    # the rendered capture, so the HTTP request is skipped.
                    result.trace.append({"event": "http_fallback_skipped", "reason": "browser_capture_usable"})
                    break
                fallback_reason = result.status if result.status != "fetched" else "no_browser_candidates"
                continue
            if (backend == "http" and adapter in SAMPLED_ADAPTERS and sampling.enabled and not prefer_browser
                    and result.status == "fetched" and unscoped
                    and not (source.recipe or source.selectors or source.profile_dir)
                    and sections and all(s['status'] == 'complete' for s in sections)):
                # Complete static HTML: accept it unless this page verifies the
                # job's sample or a sample already mismatched. Accepted rows
                # stay static_html/unconfirmed review evidence.
                sample_decision, http_rows = sampling.decide(url), unscoped
                if sample_decision == 'accept':
                    result.trace.append({"event": "browser_skipped", "reason": "http_complete_jetcar"})
                    break
                if sample_decision == 'browser':
                    result.trace.append({"event": "browser_required", "reason": "jetcar_sample_mismatch_fallback"})
            # Adapter pages may stop after HTTP only when every section is
            # complete and the evidence is sufficient (static HTML is not).
            if (candidates and not (source.recipe or source.selectors or source.profile_dir)
                    and (not source.adapter or (sections and all(s['status'] == 'complete' for s in sections)))
                    and (not sections or all(s['status'] == 'complete' for s in sections))
                    and _has_sufficient_evidence(candidates)):
                break
        if sample_decision == 'sample':
            browser_result = next((r for r, _ in responses if r.backend == 'playwright'), None)
            # An unsettled capture may show a half-updated page; comparing it
            # would turn one slow page into a job-wide browser fallback.
            if browser_result is None or browser_result.status != 'fetched' or _unsettled(browser_result):
                outcome = 'unverified'
            else:
                outcome = 'match' if rows_match(http_rows, browser_rows) else 'mismatch'
            sampling.record(outcome)
            verification = {"event": "browser_sample_verification", "result": outcome,
                            "http_rows": len(http_rows),
                            "browser_rows": len(browser_rows) if outcome != 'unverified' else None}
            if outcome == 'unverified':
                verification["reason"] = browser_result.status if browser_result is not None else "browser_not_run"
            (browser_result or responses[0][0]).trace.append(verification)
        if not responses:
            responses = [(FetchResult(source_id, "budget_exhausted", "none", message="Collection time limit reached"), [])]
        result = chosen_result or responses[-1][0]
        candidates = chosen_candidates
        if result.backend == 'playwright' and source.recipe and not source.recipe_version.startswith('reviewed-'):
            source.recipe_version = 'catalog-expansion-v2'
        result.trace.append({"event": "catalog_page_timing", "elapsed_seconds": round(time.monotonic() - page_started, 3),
                             "fetch_seconds": round(fetch_seconds, 3), "extraction_seconds": round(extraction_seconds, 3)})
        return source, responses, candidates, chosen_assessments, chosen_sections, result, raw_http_candidates

    def capture_page(url, prefetched_http=None, prefetched_http_seconds=0.0):
        with robots_policy_scope(policy_cache), http_client_scope(http_pool):
            return _capture_page(url, prefetched_http, prefetched_http_seconds)

    retry_counts: dict[str, int] = {}
    tasks: Queue = Queue()
    completed: Queue = Queue()
    workers: list[Thread] = []
    browser_tasks: Queue = Queue()
    browser_workers: list[Thread] = []
    browser_failure: list[BaseException] = []
    stop_workers = Event()

    def prefetch_http(page_url, pre_source):
        if browser_first(pre_source):
            # The browser runs first; HTTP is fetched later only as fallback.
            return None, 0, 0.0
        fetch_started = time.monotonic()
        with robots_policy_scope(policy_cache), http_client_scope(http_pool):
            response, attempts = fetch_http(page_url, source=pre_source, fetch=current_fetch,
                                            base_dir=base_dir, deadline=deadline, gate=gate)
        return response, attempts, time.monotonic() - fetch_started

    def run_on_browser(fetch_once):
        """Run one browser fetch on a dedicated browser thread and wait for it."""
        future: Future = Future()
        browser_tasks.put((fetch_once, future))
        while True:
            done, _ = wait([future], timeout=0.25)
            if done:
                return future.result()
            if browser_failure and future.cancel():
                raise browser_failure[0]
            if stop_workers.is_set() and future.cancel():
                raise _WorkerStopped("Collection stopped before this browser capture started")

    def browser_worker():
        # Playwright's sync runtime and Chromium remain on this owning thread.
        # One thread per browser slot: browser_concurrency=1 starts one Chromium.
        try:
            # Context variables do not cross threads; share the job's robots
            # decisions and HTTP connections with this browser thread too.
            with robots_policy_scope(policy_cache), http_client_scope(http_pool), \
                    browser_session(reuse=browser_reuse and fetch is collect):
                while True:
                    item = browser_tasks.get()
                    if item is None:
                        return
                    fetch_once, future = item
                    if not future.set_running_or_notify_cancel():
                        continue
                    try:
                        future.set_result(fetch_once())
                    except BaseException as exc:
                        future.set_exception(exc)
        except BaseException as exc:
            browser_failure.append(exc)
            completed.put((None, None, None, exc))

    def page_worker():
        while True:
            task = tasks.get()
            if task is None or stop_workers.is_set():
                return
            page_url, page_origin = task
            try:
                payload = capture_page(page_url)
                completed.put((page_url, page_origin, payload, None))
            except BaseException as exc:
                completed.put((page_url, page_origin, None, exc))

    if optimized:
        for index in range(browser_concurrency):
            thread = Thread(target=browser_worker, name=f"catalog-browser-{index}", daemon=True)
            thread.start()
            browser_workers.append(thread)
        for index in range(concurrency):
            thread = Thread(target=page_worker, name=f"catalog-page-{index}", daemon=True)
            thread.start()
            workers.append(thread)

    main_browser = ExitStack()
    save()
    try:
      if not optimized and not collection_finished:
        # Sequential and HTTP-prefetch paths capture on this thread. Reuse
        # one Chromium for the batch; every URL still gets a fresh context.
        main_browser.enter_context(browser_session(reuse=fetch is collect))
      while not collection_finished and (queue or pending or (optimized and parallel_leases)) and (pending or parallel_leases or (dispatched < max_pages and time.monotonic() < deadline)):
        prefetched_http = None
        prefetched_http_seconds = 0.0
        retry_count = 0
        captured = None
        if optimized:
            while queue and dispatched < max_pages and len(parallel_leases) < concurrency and time.monotonic() < deadline:
                page_url, page_origin = queue.popleft()
                if page_url in visited:
                    continue
                visited.add(page_url)
                dispatched += 1
                parallel_leases.append(page_url)
                reserve = 240 if HOST_ADAPTERS.get(urlsplit(page_url).hostname) == 'gongcar' else 60
                save()
                if ledger:
                    ledger.set(page_url, "leased", lease_seconds=reserve)
                tasks.put((page_url, page_origin))
            if not parallel_leases:
                break
            try:
                url, origin, captured, error = completed.get(
                    timeout=max(0.001, deadline - time.monotonic()) + RESULT_GRACE_SECONDS)
            except Empty:
                # Keep every capture that did return. Pages still in flight
                # are interrupted (their reserved time stays charged) and the
                # batch is reported normally instead of failing as a whole.
                stop_workers.set()
                for leased_url in list(parallel_leases):
                    coverage.append({"url": leased_url, "status": "interrupted",
                                     "reason": "Page capture did not return before the collection deadline; "
                                               "reserved time was charged. Retry explicitly."})
                    if ledger:
                        ledger.set(leased_url, "interrupted", detail="deadline")
                parallel_leases.clear()
                save()
                break
            if error is not None:
                raise error
            retry_count = retry_counts.pop(url, 0)
            retry_total += max(0, retry_count - 1)
            if ledger:
                ledger.set(url, "leased", attempts=retry_count,
                           lease_seconds=max(0, deadline - time.monotonic()))
        elif executor is not None:
            # Sliding window: refill each free slot instead of waiting for the
            # whole previous batch to drain.
            if len(pending) < concurrency:
                batch = []
                while (queue and dispatched < max_pages and len(pending) + len(batch) < concurrency
                       and time.monotonic() < deadline):
                    page_url, page_origin = queue.popleft()
                    if page_url in visited:
                        continue
                    visited.add(page_url)
                    dispatched += 1
                    parallel_leases.append(page_url)
                    batch.append((page_url, page_origin))
                if batch:
                    # Reserve the longest simultaneous page budget. On crash,
                    # every leased URL is spent and this time remains charged.
                    reserve = max(240 if HOST_ADAPTERS.get(urlsplit(u).hostname) == 'gongcar' else 60
                                  for u in parallel_leases)
                    save(reserve=min(reserve, max(0, deadline - time.monotonic())))
                    for page_url, page_origin in batch:
                        host = urlsplit(page_url).hostname
                        adapter = HOST_ADAPTERS.get(host)
                        pre_source = Source("catalog_source_" + stable_id(page_url), host, "web", page_url, [],
                                            allowed_domains=[host], adapter=adapter, max_attempts=1,
                                            backends=["http", "playwright"], min_interval_seconds=0,
                                            recipe=_ready_recipe(adapter, page_url))
                        if ledger:
                            lease_seconds = 240 if adapter == "gongcar" else 60
                            ledger.set(page_url, "leased", lease_seconds=lease_seconds)
                        future = executor.submit(prefetch_http, page_url, pre_source)
                        pending.append((page_url, page_origin, future))
            if not pending:
                break
            ready, _ = wait([item[2] for item in pending], return_when=FIRST_COMPLETED)
            position = next(i for i, item in enumerate(pending) if item[2] in ready)
            url, origin, future = pending[position]
            del pending[position]
            prefetched_http, retry_count, prefetched_http_seconds = future.result()
            retry_total += max(0, retry_count - 1)
            if ledger:
                ledger.set(url, "leased", attempts=retry_count,
                           lease_seconds=max(0, deadline - time.monotonic()))
        else:
            url, origin = queue.popleft()
            if url in visited:
                continue
            visited.add(url)
            dispatched += 1
            # On a hard crash, conservatively charge the maximum time reserved for
            # the page. Completed pages refund unused time in the next checkpoint.
            page_budget = 240 if HOST_ADAPTERS.get(urlsplit(url).hostname) == 'gongcar' else 60
            save(inflight=url, reserve=min(page_budget, max(0, deadline - time.monotonic())))
        source, responses, candidates, chosen_assessments, chosen_sections, result, raw_http_candidates = captured if captured is not None else capture_page(url, prefetched_http, prefetched_http_seconds)
        # A later access challenge limits page coverage even when an earlier
        # response supplied safe candidates. Keep that response for extraction.
        challenge_result = next((response for response, _ in reversed(responses)
                                 if any(event.get('event') == 'access_challenge'
                                        for event in response.trace)), None)
        coverage_result = challenge_result or result
        host = urlsplit(url).hostname
        source_id = source.id
        sources.append(source)
        page_products = _products_for(source, candidates, next((str(item.get("name")) for candidate_url, item in selected if candidate_url == url), topic["product"]))
        products.extend(page_products)
        prefetched[source_id] = responses
        captures[url] = {"source": asdict(source), "products": [asdict(p) for p in page_products],
                         "raw_http_candidates": raw_http_candidates,
                         "chosen_candidates": [asdict(c) for c in candidates],
                         "responses": [(asdict(r), [asdict(c) for c in cs]) for r, cs in responses]}
        status = coverage_result.status if coverage_result.status != "fetched" else "visited" if candidates else "no_data"
        scope_counts = {key: sum(item["status"] == key for item in chosen_assessments)
                        for key in ("matched", "unknown", "excluded", "not_checked")}
        coverage.append({"url": url, "source_id": source_id, "origin": origin, "status": status,
                         "adapter": source.adapter, "recipe_fingerprint": stable_id(source.recipe, source.selectors, source.row_selector),
                         "plan_fingerprint": stable_id(snapshot.get('conditions', []), includes, excludes),
                         "attempts": len(responses) + max(0, retry_count - 1),
                         "trace": _coverage_trace(event for response, _ in responses for event in response.trace),
                         "sections": chosen_sections,
                         "products": len(candidates), "products_extracted": len(chosen_assessments),
                         "observation_candidates": len(candidates),
                         "identified_products": sum(bool(p.identifiers) for p in page_products),
                         "scope_counts": scope_counts, "scope_assessments": chosen_assessments[:100],
                         "excluded_locators": [a['locator'] for a in chosen_assessments if a['status'] == 'excluded'],
                         "scope_assessments_truncated": max(0, len(chosen_assessments) - 100),
                          "reason": coverage_result.message or ("No supported structured product data or no products satisfying the explicit scope" if not candidates and status == "no_data" else "")})
        if ledger:
            ledger.set(url, "completed" if status == "visited" else "review" if status == "no_data" else "failed",
                       attempts=retry_count, detail=status)
        if result.status == "fetched" and retry_urls is None and follow_links:
            for link in _links(result, result.final_url or url, host, excluded_urls):
                if link not in visited and link not in queued and urlsplit(link).hostname in allowed_hosts:
                    queue.append((link, "discovered"))
                    queued.add(link)
                    if ledger:
                        ledger.set(link, "queued")
            gaps = [event for event in result.trace if event.get('event') == 'discovery_truncated']
            if gaps:
                coverage[-1]['sections'].append({**unknown_section('Link discovery was truncated'),
                                                 'section': 'discovery_limits', 'status': 'partial', 'limits': gaps})
                # Persist the limit evidence, including across rule reanalysis.
                captures[url]['responses'] = [(asdict(r), [asdict(c) for c in cs]) for r, cs in responses]
        if url in parallel_leases:
            parallel_leases.remove(url)
        save()
    finally:
        main_browser.close()
        if workers or browser_workers:
            from .browser_workers import stop_and_join_workers
            stopped = stop_and_join_workers(workers, tasks, stop_workers, timeout=15.0)
            stopped = stop_and_join_workers(browser_workers, browser_tasks, stop_workers, timeout=15.0) and stopped
            if not stopped:
                if ledger is not None:
                    ledger.close()
                raise RuntimeError("Browser worker cleanup did not finish; stop this worker process before retrying")
        if executor is not None:
            executor.shutdown(wait=True, cancel_futures=True)
        if ledger is not None:
            ledger.close()
        if owns_http_pool:
            http_pool.close()
    if not collection_finished:
        for url, origin in queue:
            if url not in visited:
                coverage.append({"url": url, "origin": origin, "status": "unprocessed", "reason": "page or time budget reached"})
        collection_finished = True
        # Seal the exact page set before constructing a run. If report writing
        # is interrupted, a restart must not expand or change that run's config.
        save()
    if concurrency > 1:
        # Completion order varies; the approved inventory controls report order.
        rank = {url: index for index, url in enumerate(chosen)}
        ordered = sorted(captures, key=lambda url: (rank.get(url, len(rank)), url))
        sources = [Source(**captures[url]["source"]) for url in ordered]
        products = [Product(**item) for url in ordered for item in captures[url]["products"]]
        prefetched = {captures[url]["source"]["id"]: [
            (FetchResult(**result), [Candidate(**candidate) for candidate in candidates])
            for result, candidates in captures[url]["responses"]] for url in ordered}
        coverage.sort(key=lambda item: (rank.get(item["url"], len(rank)), item["url"]))
    config = CollectionConfig(f"{topic['industry']} — {topic['product']} — {topic['market']}", products, sources,
                              str(destination), str(destination), max_run_seconds=60)
    collection_wall_seconds = time.monotonic() - started
    timing_totals = {"page_seconds_sum": 0.0, "fetch_seconds_sum": 0.0,
                     "extraction_seconds_sum": 0.0, "browser_startup_seconds_sum": 0.0,
                     "browser_launch_seconds_sum": 0.0, "navigation_seconds_sum": 0.0,
                     "evidence_seconds_sum": 0.0}
    browser_launches = browser_reused_contexts = browser_pages = 0
    http_not_modified_responses = extraction_reused_responses = 0
    sampling_events = []
    for capture in captures.values():
        for response, _ in capture["responses"]:
            for event in response.get("trace", []):
                if event.get("event") in {"browser_skipped", "browser_sample_verification"}:
                    sampling_events.append(event)
                http_not_modified_responses += event.get('code') == 'http_not_modified'
                extraction_reused_responses += event.get('code') == 'extraction_reused'
                kind = event.get("event")
                if kind == "catalog_page_timing":
                    for key, field in (("page_seconds_sum", "elapsed_seconds"),
                                       ("fetch_seconds_sum", "fetch_seconds"),
                                       ("extraction_seconds_sum", "extraction_seconds")):
                        timing_totals[key] += float(event.get(field, 0))
                elif kind == "browser_context_ready":
                    browser_pages += 1
                    browser_launches += bool(event.get("launch_ms", 0))
                    browser_reused_contexts += bool(event.get("reused", False))
                    timing_totals["browser_startup_seconds_sum"] += float(event.get("startup_ms", 0)) / 1000
                    timing_totals["browser_launch_seconds_sum"] += float(event.get("launch_ms", 0)) / 1000
                elif kind in {"navigation_timing", "evidence_timing"}:
                    key = "navigation_seconds_sum" if kind == "navigation_timing" else "evidence_seconds_sum"
                    timing_totals[key] += float(event.get("duration_ms", 0)) / 1000
    run_id = "catalog_" + stable_id(checkpoint_id, rules_version) if checkpoint_id else None
    resume_id = None
    if run_id:
        store = Store(destination)
        try:
            if store.db.execute("SELECT 1 FROM runs WHERE id=?", (run_id,)).fetchone():
                resume_id = run_id
        finally:
            store.close()
    report_started = time.monotonic()
    run = execute(config, prefetched=prefetched, resume_id=resume_id, new_run_id=run_id)
    if parent_checkpoint_id:
        # Supplemental evidence survives a partial retry as review-only history.
        parent = read_checkpoint(destination, parent_checkpoint_id)
        parent_run = parent["result"]["run_id"]
        store = Store(destination)
        try:
            rows = store.db.execute("SELECT id,data FROM browser_submissions WHERE run_id=?", (parent_run,)).fetchall()
            with store.db:
                for original in rows:
                    row = json.loads(original["data"])
                    row["id"] = "browser_" + stable_id(run["id"], original["id"])
                    row["run_id"] = run["id"]
                    row["derived_values"]["parent_submission_id"] = original["id"]
                    store.db.execute("INSERT OR IGNORE INTO browser_submissions(id,run_id,data) VALUES(?,?,?)",
                                     (row["id"], run["id"], json.dumps(row, ensure_ascii=False)))
            if rows:
                from .pipeline import report_for_run
                run = report_for_run(config, store, run["id"])
        finally:
            store.close()
    config_path = destination / f"collection-{run['id']}.json"
    document = asdict(config)
    document.pop("base_dir")
    _atomic_write(config_path, document)
    status = "partial" if any(x["status"] in {"unprocessed", "interrupted", "blocked", "policy_denied", "needs_auth", "failed", "timeout", "tool_unavailable", "budget_exhausted"} for x in coverage) else "needs_review" if any(x["status"] == "no_data" or any(x.get("scope_counts", {}).get(key, 0) for key in ("unknown", "excluded", "not_checked")) for x in coverage) or run["status"] != "completed" else "completed"
    _save_coverage(destination, run, coverage)
    status = apply_status(status, coverage)
    result = {"run_id": run["id"], "output_dir": str(destination), "report_path": run["report_path"],
            "status": status, "coverage": coverage, "coverage_summary": summarize(coverage), "config_path": str(config_path),
            "plan_scope": {"request_text": snapshot.get("request_text", ""), "topic": topic,
                           "categories": snapshot.get("categories", []), "include_terms": snapshot.get("include_terms", []),
                           "exclude_terms": snapshot.get("exclude_terms", []), "conditions": conditions},
            "scope": "Visited pages only; unrelated same-host navigation links may be visited within the page budget. No whole-site completeness claim.",
            "scope_note": "Coverage distinguishes visited pages, extracted products, explicit condition matches, unknowns, and exclusions. Only fields present in product evidence were checked. Other natural-language and commercial conditions still require review."}
    result["budget"] = {"pages_used": dispatched, "max_pages": max_pages,
                        "seconds_used": min(max_seconds, elapsed + time.monotonic() - started), "max_seconds": max_seconds}
    result['rules_version'] = rules_version
    result['reanalysed_retained_evidence'] = reanalysed
    result["execution"] = {"mode": "parallel" if concurrency > 1 else "sequential",
                           "concurrency": concurrency,
                           "per_host_concurrency": min(concurrency, 2),
                           "browser_concurrency": browser_concurrency,
                           "browser_reuse": browser_reuse,
                           "peak_browser_concurrency": browser_peak,
                           "browser_pages": browser_pages,
                           "browser_launches": browser_launches,
                           "browser_reused_contexts": browser_reused_contexts,
                           "http_not_modified_responses": http_not_modified_responses,
                           "extraction_reused_responses": extraction_reused_responses,
                           "timings": {"collection_wall_seconds": round(collection_wall_seconds, 3),
                                       "checkpoint_seconds": round(checkpoint_seconds, 3),
                                       "pipeline_report_seconds": round(time.monotonic() - report_started, 3),
                                       **{key: round(value, 3) for key, value in timing_totals.items()}},
                           "min_host_interval_seconds": 0.25 if concurrency > 1 else 0,
                           "max_transient_attempts": 2 if concurrency > 1 else 1,
                           "transient_retries": retry_total,
                           **execution_counters(sampling_events, sampling)}
    # Compare only this run's captured observations. A failed page never borrows
    # the previous values as if they had just been observed.
    counts = {key: 0 for key in ('added', 'changed', 'unchanged', 'unavailable', 'removed_candidate')}
    changes = []
    next_pages = dict(baseline_pages)
    coverage_by_url = {item['url']: item for item in coverage}
    checked_at = datetime.now(timezone.utc).isoformat()
    compared_urls = set(chosen) | set(captures) | set(baseline_pages)
    all_changes = []
    all_item_changes = []
    item_counts = {key: 0 for key in ('baseline', 'added', 'price_changed', 'price_and_conditions_changed',
                                    'conditions_changed',
                                    'unchanged', 'unavailable', 'removed_candidate')}
    unverified_empty_urls = set()
    for url in sorted(compared_urls):
        previous = baseline_pages.get(url) or {}
        captured = captures.get(url)
        page = coverage_by_url.get(url) or {}
        successful = bool(captured and page.get('status') in {'visited', 'no_data'} and not any(
            event.get('code') == 'extraction_error' for event in page.get('trace', [])))
        page_complete = bool(page.get('sections') and all(
            section.get('status') == 'complete' for section in page['sections']))
        # An unsupported/changed page structure is not proof of an empty
        # inventory, even when no extractor raised an exception.
        if successful and previous.get('candidates') and not captured.get('chosen_candidates') and not page_complete:
            unverified_empty_urls.add(url)
            successful = False
        current = semantic_candidates(captured.get('chosen_candidates', [])) if successful else []
        comparable_inventory = bool(successful and previous.get('inventory_complete') and
                                    page.get('sections') and all(section.get('status') == 'complete'
                                                                  for section in page['sections']))
        rows = item_changes(previous.get('candidates', []), current, url=url,
                            inventory_complete=comparable_inventory, baseline=not bool(previous),
                            successful=successful, checked_at=checked_at,
                            previous_checked_at=previous.get('checked_at') or (previous.get('http') or {}).get('checked_at'),
                            failure_reason='unverified_empty_capture' if url in unverified_empty_urls else
                                           ('page_not_rechecked' if not captured else 'page_unavailable'))
        all_item_changes.extend(rows)
        for item in rows:
            item_counts[item['status']] += 1
        row_counts = Counter(item['status'] for item in rows)
        delta = {key: row_counts[key] for key in counts}
        delta['changed'] = (row_counts['price_changed'] + row_counts['price_and_conditions_changed'] +
                            row_counts['conditions_changed'])
        # Preserve the historical page aggregate's count of prior unavailable
        # items; item_counts also exposes failed pages with no prior products.
        if not successful:
            delta['unavailable'] = len(previous.get('candidates', []))
        for key in counts:
            counts[key] += delta[key]
        original_at = (previous.get('http') or {}).get('fetched_at')
        if successful:
            source = Source(**captured['source'])
            responses = [(FetchResult(**raw), [Candidate(**candidate) for candidate in candidates])
                         for raw, candidates in captured['responses']]
            chosen_candidates = [Candidate(**candidate) for candidate in captured.get('chosen_candidates', [])]
            # A partial page cannot replace a prior complete inventory: doing
            # so would erase candidates before the next complete comparison.
            if not (previous.get('inventory_complete') and not page_complete):
                next_pages[url] = page_baseline(source, responses, chosen_candidates, destination, previous=previous,
                                               raw_http_candidates=captured.get('raw_http_candidates'))
                next_pages[url]['inventory_complete'] = page_complete
                next_pages[url]['checked_at'] = checked_at
            original_at = (next_pages[url].get('http') or {}).get('fetched_at') or original_at
        all_changes.append({'url': url, 'status': 'checked' if successful else 'unavailable',
                            'checked_at': checked_at if successful else None,
                            'original_fetched_at': original_at, **delta})
    changes = all_changes[:50]
    # Keep actionable changes visible when a large inventory response is capped.
    item_order = {name: index for index, name in enumerate(
        ('price_and_conditions_changed', 'price_changed', 'added', 'conditions_changed',
         'unavailable', 'removed_candidate', 'baseline', 'unchanged'))}
    all_item_changes.sort(key=lambda item: item_order[item['status']])
    # A scheduled full baseline advances after all approved pages were fetched.
    # Unknown site-wide inventory does not prevent a successful URL recheck.
    full_success = not unverified_empty_urls and all(coverage_by_url.get(url, {}).get('status') in {'visited', 'no_data'}
                       for url in chosen) and not any(item.get('status') in {'blocked', 'policy_denied',
                       'needs_auth', 'failed', 'timeout', 'tool_unavailable', 'budget_exhausted',
                       'interrupted', 'unprocessed'} or any(event.get('code') == 'extraction_error'
                       for event in item.get('trace', [])) or any(section.get('status') == 'partial'
                       for section in item.get('sections', [])) for item in coverage)
    last_full_at = refresh_decision['last_full_at']
    if full_success:
        if refresh_decision['effective_mode'] == 'full':
            last_full_at = checked_at
        write_baseline(destination, refresh_key, {'rules_version': rules_version,
            'last_full_at': last_full_at, 'valid': bool(last_full_at), 'pages': next_pages})
    elif next_pages != baseline_pages:
        # Each successfully checked page advances its own comparison baseline,
        # even if another page fails. Global full-check validation metadata
        # advances only after the entire scope succeeds. Failed and incomplete
        # pages retain their last trustworthy candidates above.
        partial_baseline = dict(prior_baseline or {
            'rules_version': None, 'last_full_at': None, 'valid': False})
        partial_baseline['pages'] = next_pages
        partial_baseline.pop('pages_only', None)
        write_baseline(destination, refresh_key, partial_baseline)
    write_page_baselines(destination, pages_key, {url: page for url, page in next_pages.items()
                                                  if page is not baseline_pages.get(url)})
    next_due = (datetime.fromisoformat(last_full_at) + timedelta(days=refresh_policy['full_interval_days'])).isoformat() if last_full_at else None
    inventory_verified = bool(full_success and all(
        page.get('sections') and all(section.get('status') == 'complete'
        for section in page['sections']) for page in coverage if page.get('status') in {'visited', 'no_data'}))
    result['refresh'] = {'policy': refresh_policy, 'effective_mode': refresh_decision['effective_mode'],
                         'reason': refresh_decision['reason'], 'checked_at': checked_at,
                         'last_full_at': last_full_at, 'next_full_due_at': next_due,
                         'full_recheck_succeeded': bool(full_success and refresh_decision['effective_mode'] == 'full'),
                         'approved_urls_rechecked': full_success,
                         'inventory_completeness_verified': inventory_verified,
                         'counts': counts, 'changes': changes,
                         'item_counts': item_counts, 'item_changes': all_item_changes[:100],
                         'item_changes_total': len(all_item_changes),
                         'item_changes_truncated': max(0, len(all_item_changes) - 100),
                         'changes_truncated': len(all_changes) - len(changes),
                         'discovered_links': [item['url'] for item in coverage
                                              if item.get('origin') == 'discovered'][:50],
                         'scope': 'Approved and visited URLs only; no whole-site completeness claim.'}
    durable_refresh = {**result['refresh'], 'changes': all_changes, 'changes_truncated': 0,
                       'item_changes': all_item_changes, 'item_changes_truncated': 0}
    save_refresh_run(destination, run['id'], durable_refresh)
    from .report import append_refresh_summary
    append_refresh_summary(run['report_path'], durable_refresh)
    from .final_report import finalize_reports
    artifacts = finalize_reports([Path(run['report_path'])], Path(run['report_path']), metadata={
        "status": result['status'], "coverage": coverage,
        "coverage_summary": result['coverage_summary'],
        "target_count": len(selected), "topic": topic,
    })
    result['dashboard_path'] = artifacts['dashboard_path']
    save(result=result)
    return result
