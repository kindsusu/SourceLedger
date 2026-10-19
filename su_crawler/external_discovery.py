"""Normalize optional host-provided Exa MCP leads for research-plan previews.

This module does not connect to Exa or fetch pages. Its output is a set of
unverified plan candidates, never collected observations or prices.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any
from urllib.parse import urlsplit

from .research_plans import canonical_url


MAX_EXA_CANDIDATES = 20
_EXA_TOOLS = frozenset({"web_search_exa", "web_fetch_exa"})
_KINDS = frozenset({"site", "category", "product"})


def _retrieval_time(value: Any) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or len(value) > 64:
        raise ValueError("retrieved_at must be a timezone-aware ISO 8601 timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("retrieved_at must be a timezone-aware ISO 8601 timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("retrieved_at must be a timezone-aware ISO 8601 timestamp")
    return value


def _display_name(value: Any, url: str) -> str:
    if value is None or value == "":
        return urlsplit(url).hostname or url
    if not isinstance(value, str) or not value.strip() or len(value) > 200:
        raise ValueError("Exa result title must be nonempty text of at most 200 characters")
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise ValueError("Exa result title contains a control character")
    return value.strip()


def normalize_exa_candidates(results: list[dict[str, Any]], *, tool: str,
                             retrieved_at: str | None = None) -> list[dict[str, str]]:
    """Convert structured Exa leads into ``submit_research_preview`` candidates.

    Each result needs a direct public ``url``; ``title``/``name``, ``kind``,
    ``retrieved_at`` and ``truncated`` are optional. A per-result timestamp
    overrides the batch timestamp. Unknown Exa snippets/content, prices and
    scores are deliberately ignored. Missing metadata is reported as unknown.
    The caller must supply the result objects from its own Exa MCP session and
    still submit a full preview with explicit market and user review.
    """
    if not isinstance(tool, str) or tool not in _EXA_TOOLS:
        raise ValueError("tool must be web_search_exa or web_fetch_exa")
    if not isinstance(results, list) or len(results) > MAX_EXA_CANDIDATES:
        raise ValueError(f"results must be a list of at most {MAX_EXA_CANDIDATES} items")
    batch_time = _retrieval_time(retrieved_at)
    candidates: list[dict[str, str]] = []
    seen: set[str] = set()
    for result in results:
        if not isinstance(result, dict):
            raise ValueError("Each Exa result must be an object")
        url = canonical_url(result.get("url"))
        evidence_raw = result.get("source_url", url)
        evidence_url = canonical_url(evidence_raw)
        if evidence_url != url:
            raise ValueError("Exa source_url must identify the same result URL")
        kind = result.get("kind", "site")
        if not isinstance(kind, str) or kind not in _KINDS:
            raise ValueError("Exa candidate kind must be site, category, or product")
        truncated = result.get("truncated")
        if truncated is not None and not isinstance(truncated, bool):
            raise ValueError("truncated must be true, false, or null")
        timestamp = _retrieval_time(result.get("retrieved_at", batch_time))
        name = _display_name(result.get("title", result.get("name")), url)
        if url in seen:
            continue
        seen.add(url)
        reason = (f"Unverified Exa MCP discovery lead via {tool}; "
                  f"retrieved_at={timestamp or 'unknown'}; "
                  f"content_truncated={'unknown' if truncated is None else str(truncated).lower()}. "
                  "Open the original URL and verify any price and commercial conditions from source evidence.")
        candidates.append({"name": name,
                           "url": url, "evidence_url": evidence_url,
                           "kind": kind, "reason": reason})
    return candidates
