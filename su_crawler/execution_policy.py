"""Read-only preflight for a reviewed research plan.

Report the actual bounded collection settings before starting work.
"""
from __future__ import annotations

from urllib.parse import urlsplit
import math

from .ai_providers import MODEL_PATTERN, model_recommendation
from .research_plans import canonical_url


def build_execution_brief(plan: dict, provider: str = "connected", model: str = "",
                          max_pages: int = 10, max_seconds: float = 120,
                          follow_links: bool = False, concurrency: int = 2) -> dict:
    """Return model and selected-site facts to show before a collection run."""
    if not isinstance(plan, dict):
        raise ValueError("Research plan must be an object")
    if provider not in {"connected", "codex", "claude"}:
        raise ValueError("provider must be connected, codex, or claude")
    if isinstance(max_pages, bool) or not isinstance(max_pages, int) or not 1 <= max_pages <= 50:
        raise ValueError("max_pages must be an integer from 1 to 50")
    if isinstance(max_seconds, bool) or not isinstance(max_seconds, (int, float)) or not math.isfinite(max_seconds) or not 1 <= max_seconds <= 600:
        raise ValueError("max_seconds must be between 1 and 600")
    if not isinstance(follow_links, bool):
        raise ValueError("follow_links must be a boolean")
    if isinstance(concurrency, bool) or not isinstance(concurrency, int) or not 1 <= concurrency <= 4:
        raise ValueError("concurrency must be an integer from 1 to 4")
    if not isinstance(model, str) or (model and not MODEL_PATTERN.fullmatch(model)):
        raise ValueError("model must be a safe model ID")
    candidates = plan.get("candidates", [])
    if not isinstance(candidates, list):
        raise ValueError("Research plan candidates must be a list")
    chosen = []
    seen = set()
    excluded = {canonical_url(url) for url in plan.get("excluded_urls", [])}
    for candidate in candidates:
        if not isinstance(candidate, dict):
            raise ValueError("Research plan candidate must be an object")
        if candidate.get("selected") is not True:
            continue
        url = canonical_url(candidate.get("url"))
        if url in seen or url in excluded:
            continue
        seen.add(url)
        name = candidate.get("name")
        chosen.append({"name": name.strip() if isinstance(name, str) and name.strip() else urlsplit(url).hostname,
                       "url": url, "host": urlsplit(url).hostname})

    targets = chosen
    sites = []
    site_hosts = set()
    for target in targets:
        if target["host"] not in site_hosts:
            sites.append(target["host"])
            site_hosts.add(target["host"])
    models = ({"codex": model_recommendation("codex"),
               "claude": model_recommendation("claude")})
    if provider != "connected":
        models[provider] = model_recommendation(provider, model)
    return {
        "plan_id": plan.get("id"), "plan_revision": plan.get("revision"),
        "plan_state": plan.get("state"),
        "provider": provider, "model": models[provider] if provider != "connected" else None,
        "model_recommendations": models,
        "host_model_note": "Connected AI app model is selected in that app; SourceLedger cannot set or verify it before execution.",
        "collection_model_note": "The collector itself does not call a model. Local AI settings select the preview model; the connected host handles orchestration.",
        "selected_sites": sites, "selected_targets": targets,
        "selected_target_count": len(chosen), "within_page_budget": len(chosen) <= max_pages,
        "page_budget_shortfall": max(0, len(chosen) - max_pages),
        "max_pages": max_pages, "max_seconds": max_seconds, "follow_links": follow_links,
        "checkpoint": {"planned": True, "scope": "per_run", "resume": "retry_failed_urls"},
        "execution": {"current_mode": "parallel" if concurrency > 1 else "sequential",
                      "current_concurrency": concurrency,
                      "recommended_mode": "bounded_parallel", "recommended_concurrency": 2,
                      "per_host_concurrency": min(concurrency, 2), "browser_concurrency": 1,
                      "host_interval_seconds": 0.25 if concurrency > 1 else 0,
                      "max_attempts": 2 if concurrency > 1 else 1},
        "steps": ["Review the selected model and approved URLs",
                  "Collect within page and time budgets while saving checkpoints",
                  "Compare source prices and conditions with preserved evidence",
                  "Report coverage, missing fields and changes; export SQLite-backed XLSX"],
        "verification_note": "Job success is not price verification. Missing fields remain missing; differences and incomplete conditions remain review items.",
        "ready": plan.get("state") == "confirmed" and bool(targets) and len(targets) <= max_pages,
    }
