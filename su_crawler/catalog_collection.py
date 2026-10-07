"""Bounded collection of pages selected in a confirmed research plan.

Only known adapters and embedded JSON-LD Product/Offer records are interpreted.
Other page content remains source evidence and an explicit coverage gap.
"""
from __future__ import annotations

from collections import deque
from dataclasses import asdict, replace
import json
import math
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
from .site_collection import HOST_ADAPTERS, _ready_recipe


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
        if urlsplit(link).hostname == host and link not in excluded and link not in links:
            links.append(link)
    return links[:100]


def _term_match(candidate: Candidate, includes: list[str], excludes: list[str]) -> bool:
    text = " ".join(str(v) for k, v in candidate.fields.items() if k not in {"price", "currency"} and v is not None)
    text += " " + " ".join(str(v) for v in candidate.specs.values())
    text = text.casefold()
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
                 max_pages: int = 10, max_seconds: float = 120, collector=None) -> dict:
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
    if not isinstance(max_seconds, (int, float)) or isinstance(max_seconds, bool) or not math.isfinite(max_seconds) or max_seconds <= 0:
        raise ValueError("max_seconds must be a positive finite number")
    includes = snapshot.get("include_terms", [])
    excludes = snapshot.get("exclude_terms", [])
    if any(not isinstance(terms, list) or any(not isinstance(x, str) for x in terms) for terms in (includes, excludes)):
        raise ValueError("Plan terms must be lists of strings")
    includes = [x.strip().casefold() for x in includes if x.strip()]
    excludes = [x.strip().casefold() for x in excludes if x.strip()]
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
    if len(chosen) > max_pages:
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
    while queue and len(visited) < max_pages and time.monotonic() < deadline:
        url, origin = queue.popleft()
        if url in visited:
            continue
        visited.add(url)
        host = urlsplit(url).hostname
        adapter = HOST_ADAPTERS.get(host)
        source_id = "catalog_source_" + stable_id(url)
        source = Source(source_id, host, "web", url, [], allowed_domains=[host], adapter=adapter,
                        max_attempts=1, backends=["http", "playwright"], min_interval_seconds=0)
        responses = []
        chosen_result, chosen_candidates, best_score = None, [], (-1,)
        for backend in source.backends:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            bounded = replace(source, timeout_seconds=min(30, remaining))
            if backend == "playwright" and adapter:
                bounded = replace(bounded, recipe=_ready_recipe(adapter))
            try:
                result = fetch(bounded, base_dir, backend)
            except Exception as exc:
                result = FetchResult(source_id, "failed", backend, message=f"Collector error: {type(exc).__name__}")
            if result.status == "fetched":
                final = result.final_url or url
                try:
                    if urlsplit(_url(final)).hostname != host or _url(final) in excluded_urls:
                        result = FetchResult(source_id, "policy_denied", backend, message="Redirect left selected host or reached excluded URL")
                except ValueError:
                    result = FetchResult(source_id, "policy_denied", backend, message="Invalid response URL")
            candidates = []
            if result.status == "fetched":
                try:
                    candidates = extract(result, source) if adapter else []
                    if not candidates:
                        candidates = _jsonld_candidates(result)
                except Exception as exc:
                    result.message = f"Extraction needs review: {type(exc).__name__}"
                candidates = [c for c in candidates if _term_match(c, includes, excludes)]
                for candidate in candidates:
                    flag = "Research-plan scope and natural-language conditions have not been independently verified"
                    if flag not in candidate.review_flags:
                        candidate.review_flags.append(flag)
            responses.append((result, candidates))
            score = (bool(candidates), max((_candidate_quality(c) for c in candidates), default=(-1,)))
            if chosen_result is None or score > best_score:
                chosen_result, chosen_candidates, best_score = result, candidates, score
            if result.status in {"policy_denied", "needs_auth"}:
                break
            if candidates and _has_sufficient_evidence(candidates):
                break
        if not responses:
            responses = [(FetchResult(source_id, "budget_exhausted", "none", message="Collection time limit reached"), [])]
        result = chosen_result or responses[-1][0]
        candidates = chosen_candidates
        sources.append(source)
        products.extend(_products_for(source, candidates, next((str(item.get("name")) for candidate_url, item in selected if candidate_url == url), topic["product"])))
        prefetched[source_id] = responses
        status = result.status if result.status != "fetched" else "visited" if candidates else "no_data"
        coverage.append({"url": url, "source_id": source_id, "origin": origin, "status": status,
                         "products": len(candidates), "reason": result.message or ("No supported structured product data" if not candidates and status == "no_data" else "")})
        if result.status == "fetched":
            for link in _links(result, result.final_url or url, host, excluded_urls):
                if link not in visited and link not in queued and urlsplit(link).hostname in allowed_hosts:
                    queue.append((link, "discovered"))
                    queued.add(link)
    for url, origin in queue:
        if url not in visited:
            coverage.append({"url": url, "origin": origin, "status": "unprocessed", "reason": "page or time budget reached"})
    config = CollectionConfig(f"{topic['industry']} — {topic['product']} — {topic['market']}", products, sources,
                              str(destination), base_dir, max_run_seconds=max(5, deadline - time.monotonic()))
    run = execute(config, prefetched=prefetched)
    config_path = destination / f"collection-{run['id']}.json"
    document = asdict(config)
    document.pop("base_dir")
    _atomic_write(config_path, document)
    status = "partial" if any(x["status"] in {"unprocessed", "blocked", "policy_denied", "needs_auth", "failed", "timeout", "tool_unavailable", "budget_exhausted"} for x in coverage) else "needs_review" if any(x["status"] == "no_data" for x in coverage) or run["status"] != "completed" else "completed"
    return {"run_id": run["id"], "output_dir": str(destination), "report_path": run["report_path"],
            "status": status, "coverage": coverage, "config_path": str(config_path),
            "plan_scope": {"request_text": snapshot.get("request_text", ""), "topic": topic,
                           "categories": snapshot.get("categories", []), "include_terms": snapshot.get("include_terms", []),
                           "exclude_terms": snapshot.get("exclude_terms", [])},
            "scope": "Visited pages only; unrelated same-host navigation links may be visited within the page budget. No whole-site completeness claim.",
            "scope_note": "Research-plan scope and natural-language conditions have not been independently verified."}
