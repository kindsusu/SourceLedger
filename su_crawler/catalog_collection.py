"""Bounded collection of pages selected in a confirmed research plan.

Only known adapters and embedded JSON-LD Product/Offer records are interpreted.
Other page content remains source evidence and an explicit coverage gap.
"""
from __future__ import annotations

from collections import deque
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, replace
import json
import math
import re
from pathlib import Path
import time
from urllib.parse import urljoin, urlsplit

from bs4 import BeautifulSoup

from .collectors import collect
from .extraction import extract
from .models import Candidate, CollectionConfig, FetchResult, Product, Source, stable_id
from .pipeline import execute, _candidate_quality, _has_sufficient_evidence
from .research import _atomic_write
from .research_plans import canonical_url
from .site_collection import HOST_ADAPTERS, _ready_recipe, _sections, _save_coverage
from .scope_conditions import assess_conditions, normalize_conditions, normalize_text
from .catalog_checkpoint import read_checkpoint, write_checkpoint
from .catalog_scheduler import HostGate, PageLedger, fetch_http
from .storage import Store


def _url(value: str) -> str:
    return canonical_url(value)


def _type(node: dict, name: str) -> bool:
    values = node.get("@type", [])
    return name in ([values] if isinstance(values, str) else values if isinstance(values, list) else [])


def _nodes(value, depth=0):
    if depth > 8:
        return
    if isinstance(value, list):
        for item in value[:1000]:
            yield from _nodes(item, depth + 1)
    elif isinstance(value, dict):
        yield value
        for key in ("@graph", "hasVariant", "itemListElement", "item"):
            child = value.get(key)
            if isinstance(child, (dict, list)):
                yield from _nodes(child, depth + 1)


def _scalar(value):
    return value if isinstance(value, (str, int, float)) and not isinstance(value, bool) else None


def _named(value):
    return _scalar(value.get("name")) if isinstance(value, dict) else _scalar(value)


def _jsonld_candidates(result: FetchResult) -> list[Candidate]:
    if "html" not in result.media_type.lower():
        return []
    soup = BeautifulSoup(result.content, "html.parser")
    found: list[tuple[str, dict]] = []
    for script_index, script in enumerate(soup.select('script[type="application/ld+json"]'), 1):
        try:
            data = json.loads(script.string or script.get_text(), parse_float=str)
        except (ValueError, TypeError):
            continue
        for node_index, node in enumerate(_nodes(data), 1):
            if _type(node, "Product"):
                found.append((f"jsonld:{script_index}/{node_index}", node))
    candidates = []
    for base, node in found:
        if node.get("hasVariant") and not node.get("offers"):
            continue
        offers = node.get("offers")
        offers = offers if isinstance(offers, list) else [offers] if isinstance(offers, dict) else [{}]
        for index, offer in enumerate(offers, 1):
            if not isinstance(offer, dict):
                continue
            locator = f"{base}/offer:{index}"
            fields, specs, evidence = {}, {}, {}

            def add(name, value, *, spec=False, at=None):
                value = _scalar(value)
                if value is None:
                    return
                key = f"spec:{name}" if spec else name
                (specs if spec else fields)[name] = value
                evidence[key] = {"location": f"{locator}/{at or name}", "raw": value,
                                 "display_state": "unconfirmed", "proof_kind": "embedded_metadata"}

            for key in ("name", "description", "sku", "model", "mpn", "gtin", "gtin8", "gtin12", "gtin13", "gtin14"):
                add(key, node.get(key), at=f"Product.{key}")
            add("manufacturer", _named(node.get("manufacturer")), at="Product.manufacturer")
            add("brand", _named(node.get("brand")), at="Product.brand")
            add("seller", _named(offer.get("seller")), at="Offer.seller")
            add("condition", node.get("itemCondition"), at="Product.itemCondition")
            for key in ("category", "color", "size", "material", "pattern", "weight"):
                add(key, node.get(key), spec=True, at=f"Product.{key}")
            for key in ("color", "size", "material"):
                add(f"offer_{key}", offer.get(key), spec=True, at=f"Offer.{key}")
            attributes = node.get("additionalProperty")
            for attribute in attributes if isinstance(attributes, list) else [attributes]:
                if isinstance(attribute, dict) and _scalar(attribute.get("name")):
                    add(str(attribute["name"]), attribute.get("value"), spec=True, at="Product.additionalProperty")
            add("offer_sku", offer.get("sku"), at="Offer.sku")
            add("option", offer.get("name"), at="Offer.name")
            add("availability", offer.get("availability"), at="Offer.availability")
            add("offer_condition", offer.get("itemCondition"), at="Offer.itemCondition")
            add("valid_to", offer.get("priceValidUntil"), at="Offer.priceValidUntil")
            add("currency", offer.get("priceCurrency"), at="Offer.priceCurrency")
            if _type(offer, "AggregateOffer"):
                for key in ("lowPrice", "highPrice", "offerCount"):
                    add(key, offer.get(key), spec=True, at=f"AggregateOffer.{key}")
            else:
                add("price", offer.get("price"), at="Offer.price")
                price_spec = offer.get("priceSpecification")
                if isinstance(price_spec, dict):
                    if "price" not in fields:
                        add("price", price_spec.get("price"), at="Offer.priceSpecification.price")
                    if "currency" not in fields:
                        add("currency", price_spec.get("priceCurrency"), at="Offer.priceSpecification.priceCurrency")
            flags = ["embedded JSON-LD price has not been visually verified"]
            if _type(offer, "AggregateOffer"):
                flags.append("AggregateOffer is a range, not an exact offer price")
            if len(found) > 1 or len(offers) > 1:
                flags.append("multiple embedded products or offers need selection review")
            candidates.append(Candidate(fields, evidence, locator, "json_ld_product_offer", specs,
                                        source_visibility="unconfirmed", evidence_mode="static_html",
                                        review_flags=flags))
    return candidates


def _links(result: FetchResult, page_url: str, host: str, excluded: set[str]):
    if result.status != "fetched" or "html" not in result.media_type.lower():
        return []
    soup = BeautifulSoup(result.content, "html.parser")
    links = []
    for anchor in soup.select("a[href]"):
        try:
            link = _url(urljoin(page_url, anchor["href"]))
        except ValueError:
            continue
        if HOST_ADAPTERS.get(host) == "jetcar" and not re.fullmatch(r"/sub(?:0201|0301)/\d+/?", urlsplit(link).path):
            continue
        if urlsplit(link).hostname == host and link not in excluded and link not in links:
            links.append(link)
    return links[:100]


def _term_match(candidate: Candidate, includes: list[str], excludes: list[str]) -> bool:
    text = " ".join(str(v) for k, v in candidate.fields.items() if k not in {"price", "currency"} and v is not None)
    text += " " + " ".join(str(v) for v in candidate.specs.values())
    text = normalize_text(text)
    return (not includes or any(term in text for term in includes)) and not any(term in text for term in excludes)


def _products_for(source: Source, candidates: list[Candidate], fallback_name: str) -> list[Product]:
    products, seen = [], set()
    for candidate in candidates:
        fields = candidate.fields
        identity = next(((key, str(fields[key])) for key in ("sku", "item_id", "mpn", "gtin13", "gtin12", "gtin14", "gtin8", "gtin", "model", "name")
                         if fields.get(key) is not None and str(fields[key]).strip()), None)
        if identity is None or identity in seen:
            continue
        seen.add(identity)
        product_id = "catalog_" + stable_id(source.id, *identity)
        products.append(Product(product_id, str(fields.get("name") or fields.get("model") or identity[1]),
                                identifiers={identity[0]: identity[1]},
                                price_profile="rental" if source.adapter else "unit"))
        source.product_ids.append(product_id)
    if not products:
        product_id = "catalog_unresolved_" + stable_id(source.id)
        products.append(Product(product_id, fallback_name, price_profile="rental" if source.adapter else "unit"))
        source.product_ids.append(product_id)
    return products


def collect_plan(root: str | Path, snapshot: dict, *, output_dir: str | Path,
                 max_pages: int = 10, max_seconds: float = 120, collector=None,
                 checkpoint_id: str | None = None, retry_urls: list[str] | None = None,
                 parent_checkpoint_id: str | None = None, follow_links: bool = True,
                 concurrency: int = 1) -> dict:
    """Collect selected pages and at most ``max_pages`` same-host pages total.

    The returned coverage is page-scoped, never a claim about whole-site coverage.
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
    if not isinstance(max_seconds, (int, float)) or isinstance(max_seconds, bool) or not math.isfinite(max_seconds) or max_seconds <= 0:
        raise ValueError("max_seconds must be a positive finite number")
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
    state = read_checkpoint(destination, checkpoint_id) if checkpoint_id else None
    if state and state["binding"] != binding:
        raise ValueError("Collection checkpoint scope or budget changed")
    if state and state.get("result"):
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
    started = time.monotonic()
    deadline = started + max(0, max_seconds - elapsed)
    collection_finished = bool(state and state.get("collection_finished"))
    parallel_leases: list[str] = []
    pending = deque()
    gate = HostGate(concurrency=concurrency)
    executor = ThreadPoolExecutor(max_workers=concurrency) if concurrency > 1 and not collection_finished else None
    ledger = PageLedger(destination, checkpoint_id) if concurrency > 1 and checkpoint_id else None
    if ledger:
        ledger.reconcile(state, [url for url, _ in queue])

    def save(*, inflight=None, reserve=None, result=None):
        if checkpoint_id:
            if reserve is None:
                reserve = max((240 if HOST_ADAPTERS.get(urlsplit(u).hostname) == 'gongcar' else 60
                               for u in parallel_leases), default=0)
            write_checkpoint(destination, checkpoint_id, {
                "binding": binding, "captures": captures, "coverage": coverage,
                "queue": list(queue), "visited": sorted(visited), "queued": sorted(queued),
                "elapsed": min(max_seconds, elapsed + time.monotonic() - started + reserve),
                "dispatched": dispatched, "inflight": inflight, "result": result,
                "transient_retries": retry_total,
                "parallel_leases": list(parallel_leases),
                "collection_finished": collection_finished})

    save()
    try:
      while not collection_finished and (queue or pending) and (pending or (dispatched < max_pages and time.monotonic() < deadline)):
        prefetched_http = None
        retry_count = 0
        if executor is not None:
            if not pending:
                batch = []
                while queue and dispatched < max_pages and len(batch) < concurrency and time.monotonic() < deadline:
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
                    reserve = max(240 if HOST_ADAPTERS.get(urlsplit(u).hostname) == 'gongcar' else 60 for u, _ in batch)
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
                        future = executor.submit(fetch_http, page_url, source=pre_source, fetch=fetch,
                                                 base_dir=base_dir, deadline=deadline, gate=gate)
                        pending.append((page_url, page_origin, future))
            if not pending:
                break
            url, origin, future = pending.popleft()
            prefetched_http, retry_count = future.result()
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
        host = urlsplit(url).hostname
        adapter = HOST_ADAPTERS.get(host)
        source_id = "catalog_source_" + stable_id(url)
        source = Source(source_id, host, "web", url, [], allowed_domains=[host], adapter=adapter,
                        max_attempts=1, backends=["http", "playwright"], min_interval_seconds=0,
                        recipe=_ready_recipe(adapter, url))
        responses = []
        chosen_result, chosen_candidates, chosen_assessments, chosen_sections, best_score = None, [], [], [], (-1,)
        for backend in source.backends:
            remaining = deadline - time.monotonic()
            if remaining <= 0 and not (backend == "http" and prefetched_http is not None):
                break
            bounded = replace(source, timeout_seconds=max(0.001, min(120 if adapter == 'gongcar' else 30, remaining)),
                              recipe=source.recipe if backend == "playwright" else [])
            if backend == "http" and prefetched_http is not None:
                result = prefetched_http
            else:
                permit = gate.enter(host, deadline) if executor is not None else None
                if executor is not None and permit is None:
                    result = FetchResult(source_id, "budget_exhausted", backend,
                                         message="Collection time limit reached while waiting for host slot")
                else:
                    try:
                        if permit is not None:
                            remaining = deadline - time.monotonic()
                            if remaining <= 0:
                                result = FetchResult(source_id, "budget_exhausted", backend,
                                                     message="Collection time limit reached before browser fetch")
                            else:
                                result = fetch(replace(bounded, timeout_seconds=min(bounded.timeout_seconds, remaining)),
                                               base_dir, backend)
                        else:
                            result = fetch(bounded, base_dir, backend)
                    except Exception as exc:
                        result = FetchResult(source_id, "failed", backend, message=f"Collector error: {type(exc).__name__}")
                    finally:
                        if permit is not None:
                            permit.release()
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
            if backend == "playwright" and bounded.recipe:
                result.trace.append({"event": "collection_recipe", "recipe": bounded.recipe})
            if result.status == "fetched":
                try:
                    candidates = extract(result, source) if adapter else []
                    if not candidates:
                        candidates = _jsonld_candidates(result)
                except Exception as exc:
                    result.message = f"Extraction needs review: {type(exc).__name__}"
                # Section discovery describes the public page. Scope exclusions
                # affect retained observations, never the observed card count.
                sections = _sections(result, candidates, adapter)
                kept = []
                for candidate in candidates:
                    name = str(candidate.fields.get("name") or candidate.fields.get("model") or "")[:160]
                    if not _term_match(candidate, includes, excludes):
                        assessments.append({"name": name, "locator": candidate.locator, "status": "excluded",
                                            "reason": "explicit text filter", "checks": []})
                        continue
                    assessment = assess_conditions(candidate, conditions)
                    assessments.append({"name": name, "locator": candidate.locator,
                                        "status": assessment["status"], "reason": assessment.get("reason", ""),
                                        "checks": assessment["checks"]})
                    if assessment["status"] == "excluded":
                        continue
                    candidate.derived_values["scope_assessment"] = assessment
                    flag = "Research-plan scope and natural-language conditions have not been independently verified"
                    if flag not in candidate.review_flags:
                        candidate.review_flags.append(flag)
                    if assessment["status"] in {"unknown", "not_checked"}:
                        candidate.review_flags.append("Structured product conditions lack evidence")
                    kept.append(candidate)
                candidates = kept
            responses.append((result, candidates))
            sections = sections if result.status == 'fetched' else []
            score = (sum(s['status'] == 'complete' for s in sections), bool(candidates), max((_candidate_quality(c) for c in candidates), default=(-1,)), len(candidates))
            if chosen_result is None or score > best_score:
                chosen_result, chosen_candidates, chosen_assessments, chosen_sections, best_score = result, candidates, assessments, sections, score
            if result.status in {"policy_denied", "needs_auth"}:
                break
            if candidates and (not sections or all(s['status'] == 'complete' for s in sections)) and _has_sufficient_evidence(candidates):
                break
        if not responses:
            responses = [(FetchResult(source_id, "budget_exhausted", "none", message="Collection time limit reached"), [])]
        result = chosen_result or responses[-1][0]
        candidates = chosen_candidates
        if result.backend == 'playwright' and source.recipe:
            source.recipe_version = 'catalog-expansion-v2'
        sources.append(source)
        page_products = _products_for(source, candidates, next((str(item.get("name")) for candidate_url, item in selected if candidate_url == url), topic["product"]))
        products.extend(page_products)
        prefetched[source_id] = responses
        captures[url] = {"source": asdict(source), "products": [asdict(p) for p in page_products],
                         "responses": [(asdict(r), [asdict(c) for c in cs]) for r, cs in responses]}
        status = result.status if result.status != "fetched" else "visited" if candidates else "no_data"
        scope_counts = {key: sum(item["status"] == key for item in chosen_assessments)
                        for key in ("matched", "unknown", "excluded", "not_checked")}
        coverage.append({"url": url, "source_id": source_id, "origin": origin, "status": status,
                         "sections": chosen_sections,
                         "products": len(candidates), "products_extracted": len(chosen_assessments),
                         "scope_counts": scope_counts, "scope_assessments": chosen_assessments[:100],
                         "scope_assessments_truncated": max(0, len(chosen_assessments) - 100),
                         "reason": result.message or ("No supported structured product data or no products satisfying the explicit scope" if not candidates and status == "no_data" else "")})
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
        if prefetched_http is not None:
            parallel_leases.remove(url)
        save()
    finally:
        if executor is not None:
            executor.shutdown(wait=True, cancel_futures=True)
        if ledger is not None:
            ledger.close()
    if not collection_finished:
        for url, origin in queue:
            if url not in visited:
                coverage.append({"url": url, "origin": origin, "status": "unprocessed", "reason": "page or time budget reached"})
        collection_finished = True
        # Seal the exact page set before constructing a run. If report writing
        # is interrupted, a restart must not expand or change that run's config.
        save()
    config = CollectionConfig(f"{topic['industry']} — {topic['product']} — {topic['market']}", products, sources,
                              str(destination), str(destination), max_run_seconds=60)
    run_id = "catalog_" + stable_id(checkpoint_id) if checkpoint_id else None
    resume_id = None
    if run_id:
        store = Store(destination)
        try:
            if store.db.execute("SELECT 1 FROM runs WHERE id=?", (run_id,)).fetchone():
                resume_id = run_id
        finally:
            store.close()
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
    if any(s['status'] != 'complete' for p in coverage for s in p.get('sections', [])):
        status = 'partial'
    result = {"run_id": run["id"], "output_dir": str(destination), "report_path": run["report_path"],
            "status": status, "coverage": coverage, "config_path": str(config_path),
            "plan_scope": {"request_text": snapshot.get("request_text", ""), "topic": topic,
                           "categories": snapshot.get("categories", []), "include_terms": snapshot.get("include_terms", []),
                           "exclude_terms": snapshot.get("exclude_terms", []), "conditions": conditions},
            "scope": "Visited pages only; unrelated same-host navigation links may be visited within the page budget. No whole-site completeness claim.",
            "scope_note": "Coverage distinguishes visited pages, extracted products, explicit condition matches, unknowns, and exclusions. Only fields present in product evidence were checked. Other natural-language and commercial conditions still require review."}
    result["budget"] = {"pages_used": dispatched, "max_pages": max_pages,
                        "seconds_used": min(max_seconds, elapsed + time.monotonic() - started), "max_seconds": max_seconds}
    result["execution"] = {"mode": "parallel" if concurrency > 1 else "sequential",
                           "concurrency": concurrency,
                           "per_host_concurrency": min(concurrency, 2),
                           "browser_concurrency": 1,
                           "min_host_interval_seconds": 0.25 if concurrency > 1 else 0,
                           "max_transient_attempts": 2 if concurrency > 1 else 1,
                           "transient_retries": retry_total}
    save(result=result)
    return result
