"""Evidence-preserving, review-only source extraction rule proposals."""
from __future__ import annotations

from dataclasses import asdict
from pathlib import Path
from typing import Any
import hashlib
import json
import math
import os
import re
import time
import uuid

from bs4 import BeautifulSoup

from .collectors import collect
from .extraction import extract
from .model_provider import load_model_config, propose_with_ollama
from .models import FetchResult, Product, Source
from .research import load_workspace
from .validation import validate


_BASE_FIELDS = {
    "price", "currency", "unit", "pack_quantity", "tax", "price_type", "price_basis",
    "availability", "valid_to", "sku", "offer_sku", "model", "manufacturer", "option",
    "quantity_tier", "min_order", "member_condition", "membership", "shipping", "delivery",
}
_TOKEN_FIELDS = {
    "price": "price", "amount": "price", "currency": "currency", "unit": "unit",
    "pack": "pack_quantity", "packquantity": "pack_quantity",
    "tax": "tax", "pricetype": "price_type", "pricebasis": "price_basis", "basis": "price_basis",
    "availability": "availability", "stock": "availability", "validto": "valid_to",
    "sku": "sku", "offersku": "offer_sku", "model": "model", "manufacturer": "manufacturer",
    "brand": "manufacturer", "option": "option", "shipping": "shipping", "delivery": "delivery",
}


def _atomic_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("x", encoding="utf-8", newline="\n") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def _safe_reason(value: str) -> str:
    return re.sub(r"<[^>]{0,500}>", "", value).strip()[:500]


def _identifier_match(candidate: Any, identifiers: dict[str, str]) -> bool:
    return bool(identifiers) and all(str(candidate.fields.get(key, "")).strip() == expected for key, expected in identifiers.items())


def _allowed_fields(workspace: dict[str, Any]) -> list[str]:
    product = workspace["product"]
    return sorted(_BASE_FIELDS | set(product["identifiers"]) | {f"spec:{key}" for key in product["required_specs"]})


def _field_for_node(node: Any, allowed: set[str]) -> str | None:
    candidates: list[str] = []
    for attribute in ("data-field", "itemprop", "id"):
        value = node.get(attribute)
        if isinstance(value, str):
            candidates.append(value)
    classes = node.get("class", [])
    if isinstance(classes, list):
        candidates.extend(str(value) for value in classes)
    for raw in candidates:
        normalized = re.sub(r"[^a-z0-9]", "", raw.casefold())
        direct = next((field for field in allowed if re.sub(r"[^a-z0-9]", "", field.casefold()) == normalized), None)
        if direct:
            return direct
        mapped = _TOKEN_FIELDS.get(normalized)
        if mapped in allowed:
            return mapped
    return None


def _selector_for_node(node: Any) -> str | None:
    for attribute in ("data-field", "itemprop"):
        value = node.get(attribute)
        if isinstance(value, str) and value and '"' not in value and "\\" not in value:
            return f'[{attribute}="{value}"]'
    identifier = node.get("id")
    if isinstance(identifier, str) and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_-]*", identifier):
        return f"#{identifier}"
    for css_class in node.get("class", []) if isinstance(node.get("class", []), list) else []:
        if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_-]*", str(css_class)):
            return f".{css_class}"
    return None


def _semantic_rules(html: bytes, allowed_fields: list[str], identifiers: dict[str, str]) -> dict[str, Any] | None:
    soup = BeautifulSoup(html, "html.parser")
    allowed = set(allowed_fields)
    selectors: dict[str, str] = {}
    identifier_nodes: list[Any] = []
    for node in soup.find_all(True):
        field = _field_for_node(node, allowed)
        selector = _selector_for_node(node)
        if not field or not selector or field in selectors:
            continue
        text = node.get_text(" ", strip=True)
        if field in identifiers and text != identifiers[field]:
            continue
        selectors[field] = selector
        if field in identifiers:
            identifier_nodes.append(node)
    if not identifiers or not all(key in selectors for key in identifiers) or "price" not in selectors:
        return None
    row_selector = None
    if identifier_nodes:
        ancestor = identifier_nodes[0]
        while ancestor and getattr(ancestor, "name", None) not in {"body", "html", "[document]"}:
            candidate_selector = _selector_for_node(ancestor)
            if candidate_selector:
                matches = soup.select(candidate_selector)
                if len(matches) == 1 and all(matches[0].select_one(selector) for selector in selectors.values()):
                    row_selector = candidate_selector
                    break
            ancestor = ancestor.parent
    if row_selector is None:
        return None
    return {"row_selector": row_selector, "selectors": selectors}


def _validate_rules(result: FetchResult, source: Source, product: Product, rules: dict[str, Any], *, evidence_path: str, evidence_sha256: str) -> tuple[list[Any], list[dict[str, Any]]]:
    source.row_selector = rules.get("row_selector")
    source.selectors = dict(rules.get("selectors", {}))
    if source.selectors and "html" in result.media_type.lower():
        soup = BeautifulSoup(result.content, "html.parser")
        try:
            rows = soup.select(source.row_selector) if source.row_selector else [soup]
        except Exception as exc:
            raise ValueError("row_selector is not valid CSS") from exc
        if not rows or len(rows) > 1000:
            raise ValueError("row_selector does not identify a bounded source scope")
        for field, selector in source.selectors.items():
            matches = 0
            for row in rows:
                try:
                    selected = row.select(selector.split("::attr(", 1)[0])
                except Exception as exc:
                    raise ValueError(f"Selector for {field} is not valid CSS") from exc
                if len(selected) > 1:
                    raise ValueError(f"Selector for {field} is not unique within its row scope")
                matches += len(selected)
            if matches == 0:
                raise ValueError(f"Selector for {field} does not exist in its row scope")
    try:
        candidates = extract(result, source)
    except Exception as exc:
        raise ValueError(f"Selectors could not be evaluated ({type(exc).__name__})") from exc
    exact = [candidate for candidate in candidates if _identifier_match(candidate, product.identifiers)]
    if not exact:
        raise ValueError("Selectors did not extract the exact configured product identifiers")
    if not all(candidate.fields.get("price") is not None for candidate in exact):
        raise ValueError("Selectors did not extract a price from every exact product candidate")
    if len(exact) > 100:
        raise ValueError("Selectors produced too many exact product candidates")
    previews: list[dict[str, Any]] = []
    for index, candidate in enumerate(exact, start=1):
        observation = validate(
            candidate, product, source, run_id="proposal", task_id=f"preview-{index}",
            evidence_path=evidence_path, evidence_sha256=evidence_sha256, collected_at=result.fetched_at,
            source_url=result.final_url or source.location,
        )
        previews.append(asdict(observation))
    return exact, previews


def _draft(workspace_path: Path, workspace: dict[str, Any], source_record: dict[str, Any], rules: dict[str, Any], output: Path) -> Path:
    collection_dir = (output / "collection").resolve()
    destination = output / "collection.draft.json"
    config = {
        "name": f"{workspace['industry']} — {workspace['product']['name']} — {workspace['market']}",
        "output_dir": str(collection_dir),
        "products": [{
            "id": "product", "name": workspace["product"]["name"],
            "identifiers": dict(workspace["product"]["identifiers"]),
            "required_specs": dict(workspace["product"]["required_specs"]),
        }],
        "sources": [{
            "id": source_record["id"], "name": source_record["name"], "kind": "web",
            "location": source_record["location"], "allowed_domains": list(source_record["allowed_domains"]),
            "product_ids": ["product"], "backends": ["http", "playwright"],
            "internal": source_record["internal"], "account_scope": source_record["scope"],
            "row_selector": rules.get("row_selector"), "selectors": rules.get("selectors", {}), "recipe": [],
        }],
    }
    _atomic_json(destination, config)
    return destination.resolve()


def propose_source(
    workspace_path: str | Path, *, source_id: str, output_dir: str | Path,
    model_config_path: str | Path | None = None, timeout_seconds: float = 30,
    max_model_calls: int = 1,
) -> dict[str, Any]:
    """Propose review-only extraction rules without altering the workspace or live data."""
    if isinstance(timeout_seconds, bool) or not isinstance(timeout_seconds, (int, float)) or not math.isfinite(timeout_seconds) or not 0 < timeout_seconds <= 120:
        raise ValueError("timeout_seconds must be finite and between 0 and 120")
    if isinstance(max_model_calls, bool) or not isinstance(max_model_calls, int) or not 0 <= max_model_calls <= 1:
        raise ValueError("max_model_calls must be 0 or 1")
    workspace_file = Path(workspace_path).expanduser().resolve()
    output = Path(output_dir).expanduser().resolve()
    workspace = load_workspace(workspace_file)
    identifiers = workspace["product"]["identifiers"]
    if not identifiers:
        raise ValueError("Explicit exact product identifiers are required")
    record = next((item for item in workspace["sources"] if item["id"] == source_id), None)
    if record is None:
        raise ValueError(f"Unknown source id: {source_id}")
    try:
        output.mkdir(parents=True, exist_ok=False)
    except FileExistsError as exc:
        raise FileExistsError("Proposal output directory must not already exist") from exc
    source = Source(
        id=record["id"], name=record["name"], kind="web", location=record["location"],
        product_ids=["product"], allowed_domains=list(record["allowed_domains"]), internal=record["internal"],
        account_scope=record["scope"], respect_robots=True, backends=["http", "playwright"],
    )
    product = Product(
        id="product", name=workspace["product"]["name"], identifiers=dict(identifiers),
        required_specs=dict(workspace["product"]["required_specs"]),
    )
    started = time.monotonic()
    attempts: list[dict[str, Any]] = []
    fetched: FetchResult | None = None
    rules: dict[str, Any] | None = None
    method = "none"
    model_calls = 0
    model_usage = None
    errors: list[dict[str, str]] = []
    allowed_fields = _allowed_fields(workspace)

    for backend in ("http", "playwright"):
        remaining = timeout_seconds - (time.monotonic() - started)
        if remaining <= 0:
            attempts.append({"backend": backend, "status": "timeout", "reason": "Overall proposal deadline exceeded"})
            break
        source.timeout_seconds = remaining
        result = collect(source, str(workspace_file.parent), backend)
        attempts.append({
            "backend": backend, "status": result.status, "final_url": result.final_url,
            "fetched_at": result.fetched_at, "reason": _safe_reason(result.message),
        })
        if result.status == "policy_denied":
            fetched = None
            break
        if result.status != "fetched":
            continue
        fetched = result
        evidence_name = f"evidence.{backend}.html"
        evidence_path = output / evidence_name
        evidence_path.write_bytes(result.content)
        evidence_hash = hashlib.sha256(result.content).hexdigest()
        structured = extract(result, Source(**{**asdict(source), "selectors": {}, "row_selector": None}))
        exact_structured = [candidate for candidate in structured if _identifier_match(candidate, identifiers) and candidate.fields.get("price") is not None]
        structured_choice = None
        if exact_structured:
            structured_choice = {"row_selector": None, "selectors": {}}
            _, structured_previews = _validate_rules(
                result, source, product, structured_choice,
                evidence_path=str(evidence_path.resolve()), evidence_sha256=evidence_hash,
            )
            if all(preview.get("comparable") is True for preview in structured_previews):
                rules, method, previews = structured_choice, "structured", structured_previews
                break
        semantic = _semantic_rules(result.content, allowed_fields, identifiers)
        if semantic:
            try:
                _, previews = _validate_rules(
                    result, source, product, semantic,
                    evidence_path=str(evidence_path.resolve()), evidence_sha256=evidence_hash,
                )
                rules, method = semantic, "semantic"
                break
            except ValueError as exc:
                errors.append({"type": "selector_validation", "message": str(exc)})
        if structured_choice is not None:
            rules, method, previews = structured_choice, "structured", structured_previews
            break
        if backend == "playwright":
            break

    evidence_path_value = None
    evidence_hash_value = None
    previews = locals().get("previews", [])
    if fetched is not None:
        evidence_path_value = str((output / f"evidence.{fetched.backend}.html").resolve())
        evidence_hash_value = hashlib.sha256(fetched.content).hexdigest()

    if rules is None and fetched is not None and model_config_path is not None and max_model_calls > 0:
        remaining = timeout_seconds - (time.monotonic() - started)
        if remaining > 0:
            try:
                config = load_model_config(model_config_path)
                model_calls = 1
                candidate_rules, model_usage = propose_with_ollama(
                    fetched.content.decode("utf-8", errors="replace"), config=config,
                    allowed_fields=allowed_fields, identifiers=identifiers, timeout_seconds=remaining,
                )
                _, previews = _validate_rules(
                    fetched, source, product, candidate_rules,
                    evidence_path=evidence_path_value or "", evidence_sha256=evidence_hash_value or "",
                )
                rules, method = candidate_rules, "ollama"
            except Exception as exc:
                errors.append({"type": type(exc).__name__, "message": _safe_reason(str(exc))})

    terminal_policy = bool(attempts and attempts[-1]["status"] == "policy_denied")
    draft_path = None
    if rules is not None:
        draft_path = str(_draft(workspace_file, workspace, record, rules, output))
        missing = sorted({field for field in product.comparison_fields if not any(field in preview.get("raw_fields", {}) for preview in previews)})
        comparable = bool(previews) and all(preview.get("comparable") is True for preview in previews)
        eligible = comparable
        status = "proposed" if eligible else "needs_review"
        reason = "Extraction rules proposed; fresh source and known-sample verification are required."
        if method == "ollama":
            reason += " Model-proposed rules require source/sample verification before any activation decision."
        if missing:
            reason += " Comparison conditions remain unobserved: " + ", ".join(missing)
    elif terminal_policy:
        status, reason = "blocked", attempts[-1]["reason"] or "Collection was denied by source policy"
    elif fetched is None:
        status, reason = "blocked", "No source evidence could be collected"
    else:
        status, reason = "needs_review", "No exact, evidence-backed selector proposal was found"

    proposal = {
        "schema": "source-ledger/source-proposal/v1", "status": status, "method": method,
        "source_id": source_id, "source_url": record["location"], "source_scope": record["scope"],
        "rules": rules, "evidence_path": evidence_path_value, "evidence_sha256": evidence_hash_value,
        "capture": {"backend": fetched.backend, "fetched_at": fetched.fetched_at, "final_url": fetched.final_url} if fetched else None,
        "preview_observations": previews, "draft_path": draft_path, "model_calls": model_calls,
        "model_usage": model_usage, "fetch_attempts": attempts, "errors": errors,
        "reason": reason, "eligible_for_verification": bool(draft_path is not None and locals().get("eligible", False)),
    }
    proposal_path = output / "proposal.json"
    proposal["proposal_path"] = str(proposal_path.resolve())
    _atomic_json(proposal_path, proposal)
    return proposal
