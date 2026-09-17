"""Opt-in SearXNG search that records public source candidates only.

This module intentionally does not fetch any search result.  A local or
operator-configured SearXNG instance is the only network peer.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit
import hashlib
import ipaddress
import json
import socket
import time

import httpx

from . import research
from .models import utc_now


MAX_RESPONSE_BYTES = 2 * 1024 * 1024
MAX_QUERY_LENGTH = 1000
_CONFIG_KEYS = {"provider", "endpoint", "allow_private_endpoint", "timeout_seconds", "language", "allowed_result_domains"}


def _safe_endpoint(value: Any, allow_private: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("Search provider endpoint is required")
    if not isinstance(allow_private, bool):
        raise ValueError("allow_private_endpoint must be a boolean")
    try:
        parsed = urlsplit(value.strip())
        hostname = parsed.hostname
        port = parsed.port
    except ValueError as exc:
        raise ValueError("Search provider endpoint is invalid") from exc
    if (parsed.scheme.lower() not in {"http", "https"} or not hostname or parsed.username is not None
            or parsed.password is not None or parsed.query):
        raise ValueError("Search provider endpoint is invalid")
    if parsed.fragment or any(ord(c) < 32 or ord(c) == 127 for c in value):
        raise ValueError("Search provider endpoint is invalid")
    host = hostname.rstrip(".").lower()
    if not host or any(c.isspace() for c in host):
        raise ValueError("Search provider endpoint is invalid")
    private = host == "localhost" or host.endswith(".localhost")
    try:
        literal = ipaddress.ip_address(host)
        private = not literal.is_global
    except ValueError:
        pass
    if private and not allow_private:
        raise ValueError("Private search provider endpoint requires allow_private_endpoint")
    if not private and parsed.scheme.lower() != "https":
        raise ValueError("Public search provider endpoint must use HTTPS")
    rendered_host = f"[{host}]" if ":" in host else host
    if port is not None and not ((parsed.scheme.lower() == "http" and port == 80) or (parsed.scheme.lower() == "https" and port == 443)):
        rendered_host += f":{port}"
    return urlunsplit((parsed.scheme.lower(), rendered_host, parsed.path or "/search", parsed.query, ""))


def _endpoint_dns_is_safe(endpoint: str, allow_private: bool) -> None:
    """Reject endpoint names resolving to unsafe addresses before connecting."""
    host = urlsplit(endpoint).hostname
    assert host is not None
    try:
        addresses = {item[4][0] for item in socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)}
    except OSError as exc:
        raise ValueError("Search provider endpoint could not be resolved") from exc
    if not addresses:
        raise ValueError("Search provider endpoint could not be resolved")
    for address in addresses:
        ip = ipaddress.ip_address(address)
        # These classes never represent a reachable public service.
        if ip.is_link_local or ip.is_multicast or ip.is_unspecified or ip.is_reserved:
            raise ValueError("Search provider endpoint resolves to an unsafe address")
        if not ip.is_global and not allow_private:
            raise ValueError("Private search provider endpoint requires allow_private_endpoint")


def _config(provider_path: str | Path) -> dict[str, Any]:
    path = Path(provider_path).expanduser().resolve()
    try:
        raw = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError("Search provider configuration is unreadable") from exc
    if not isinstance(raw, dict) or set(raw) - _CONFIG_KEYS:
        raise ValueError("Search provider configuration has unsupported fields")
    if raw.get("provider") != "searxng":
        raise ValueError("Search provider must be searxng")
    endpoint = _safe_endpoint(raw.get("endpoint"), raw.get("allow_private_endpoint"))
    timeout = raw.get("timeout_seconds", 15)
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not 0 < timeout <= 60:
        raise ValueError("Search provider timeout_seconds must be between 0 and 60")
    language = raw.get("language", "en")
    if not isinstance(language, str) or not language.strip() or len(language.strip()) > 32:
        raise ValueError("Search provider language is invalid")
    domains = raw.get("allowed_result_domains", [])
    if not isinstance(domains, list) or any(not isinstance(d, str) or not d.strip() for d in domains):
        raise ValueError("allowed_result_domains must be a list of domains")
    normalized = []
    for domain in domains:
        item = domain.strip().rstrip(".").lower()
        if (not item or "/" in item or "\\" in item or ":" in item or "@" in item
                or any(c.isspace() or ord(c) < 32 or ord(c) == 127 for c in item) or item in normalized):
            raise ValueError("allowed_result_domains contains an invalid domain")
        normalized.append(item)
    _endpoint_dns_is_safe(endpoint, raw["allow_private_endpoint"])
    return {"endpoint": endpoint, "timeout": float(timeout), "language": language.strip(), "domains": normalized}


def _query(workspace: dict[str, Any], explicit: str | None) -> str:
    if explicit is not None:
        if not isinstance(explicit, str) or not explicit.strip() or len(explicit) > MAX_QUERY_LENGTH:
            raise ValueError("query must be a non-empty string of at most 1000 characters")
        return explicit.strip()
    parts = [workspace["industry"], workspace["product"]["name"], workspace["market"]]
    parts.extend(workspace["product"]["identifiers"].values())
    result = " ".join(dict.fromkeys(part.strip() for part in parts if part.strip()))
    return result[:MAX_QUERY_LENGTH]


def _topic_digest(workspace: dict[str, Any]) -> str:
    value = {"industry": workspace["industry"], "market": workspace["market"], "product": workspace["product"]}
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def _public_url(value: Any, domains: list[str]) -> str | None:
    if not isinstance(value, str) or not value or "\\" in value or any(ord(c) < 32 or ord(c) == 127 for c in value):
        return None
    try:
        parsed = urlsplit(value)
        host_value = parsed.hostname
        port = parsed.port
    except ValueError:
        return None
    if (parsed.scheme.lower() not in {"http", "https"} or not host_value or parsed.username is not None
            or parsed.password is not None):
        return None
    host = host_value.rstrip(".").lower()
    if not host or any(c.isspace() for c in host):
        return None
    if host == "localhost" or host.endswith(".localhost"):
        return None
    try:
        if not ipaddress.ip_address(host).is_global:
            return None
    except ValueError:
        pass
    if domains and host not in domains:
        return None
    hostpart = f"[{host}]" if ":" in host else host
    if port is not None and not ((parsed.scheme.lower() == "http" and port == 80) or (parsed.scheme.lower() == "https" and port == 443)):
        hostpart += f":{port}"
    return urlunsplit((parsed.scheme.lower(), hostpart, parsed.path or "/", parsed.query, ""))


def _text(value: Any, maximum: int = 1000) -> str:
    return value.strip()[:maximum] if isinstance(value, str) else ""


def search_workspace(workspace_path: str | Path, *, provider_path: str | Path | None = None,
                     query: str | None = None, limit: int = 10,
                     timeout_seconds: float | None = None) -> dict[str, Any]:
    """Search once and add bounded public candidates, never observations or prices."""
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 50:
        raise ValueError("limit must be between 1 and 50")
    if query is not None and (not isinstance(query, str) or not query.strip() or len(query) > MAX_QUERY_LENGTH):
        raise ValueError("query must be a non-empty string of at most 1000 characters")
    if provider_path is None:
        return {"status": "search_provider_unconfigured", "network_calls": 0, "added": [], "added_count": 0, "candidate_ids": []}
    config = _config(provider_path)
    if timeout_seconds is not None:
        if isinstance(timeout_seconds, bool) or not isinstance(timeout_seconds, (int, float)) or not 0 < timeout_seconds <= 60:
            raise ValueError("timeout_seconds must be between 0 and 60")
        config["timeout"] = min(config["timeout"], float(timeout_seconds))
    target = research._path(workspace_path)
    snapshot = research.load_workspace(target)
    search_query = _query(snapshot, query)
    digest = _topic_digest(snapshot)
    started = time.monotonic()
    def timed_out() -> bool:
        return time.monotonic() - started >= config["timeout"]
    try:
        with httpx.Client(timeout=config["timeout"], follow_redirects=False, trust_env=False) as client:
            with client.stream("GET", config["endpoint"], params={"q": search_query, "format": "json", "language": config["language"]}) as response:
                if response.status_code == 403:
                    return {"status": "provider_unavailable", "reason": "SearXNG JSON API is unavailable (HTTP 403)", "network_calls": 1, "added": [], "added_count": 0, "candidate_ids": []}
                if response.status_code != 200:
                    return {"status": "failed", "reason": f"Search provider returned HTTP {response.status_code}", "network_calls": 1, "added": [], "added_count": 0, "candidate_ids": []}
                chunks, size = [], 0
                for chunk in response.iter_bytes():
                    if timed_out():
                        return {"status": "timeout", "reason": "Search request exceeded its total timeout", "network_calls": 1, "added": [], "added_count": 0, "candidate_ids": []}
                    size += len(chunk)
                    if size > MAX_RESPONSE_BYTES:
                        return {"status": "failed", "reason": "Search response exceeded 2 MiB", "network_calls": 1, "added": [], "added_count": 0, "candidate_ids": []}
                    chunks.append(chunk)
        payload = json.loads(b"".join(chunks).decode("utf-8"))
    except httpx.TimeoutException:
        return {"status": "timeout", "reason": "Search request exceeded its total timeout", "network_calls": 1, "added": [], "added_count": 0, "candidate_ids": []}
    except (httpx.HTTPError, UnicodeDecodeError, json.JSONDecodeError):
        return {"status": "failed", "reason": "Search provider returned an invalid response", "network_calls": 1, "added": [], "added_count": 0, "candidate_ids": []}
    rows = payload.get("results") if isinstance(payload, dict) else None
    if not isinstance(rows, list):
        return {"status": "failed", "reason": "Search provider JSON has no results list", "network_calls": 1, "added": [], "added_count": 0, "candidate_ids": []}
    selected: list[tuple[str, str, str, int]] = []
    seen: set[str] = set()
    for rank, row in enumerate(rows, 1):
        if not isinstance(row, dict):
            continue
        url = _public_url(row.get("url"), config["domains"])
        if url and url not in seen:
            seen.add(url)
            selected.append((url, _text(row.get("title")), _text(row.get("content") or row.get("snippet")), rank))
        if len(selected) >= limit:
            break
    now, additions, candidate_ids = utc_now(), [], []
    with research._locked(target):
        if timed_out():
            return {"status": "timeout", "reason": "Search request exceeded its total timeout", "network_calls": 1, "added": [], "added_count": 0, "candidate_ids": []}
        current = research._load_unlocked(target)
        if _topic_digest(current) != digest:
            raise RuntimeError("Workspace topic changed during search")
        existing = {source["location"]: source for source in current["sources"]}
        for url, title, snippet, rank in selected:
            known = existing.get(url)
            if known is not None:
                if known["scope"] == "public":
                    candidate_ids.append(known["id"])
                continue
            if len(current["sources"]) >= research.MAX_SOURCES:
                break
            source = research._source_record(url=url, scope="public", name=title or None, provenance={
                "method": "search", "provider": "searxng", "query": search_query, "rank": rank,
                "at": now, "search_endpoint": config["endpoint"], "title": title, "snippet": snippet,
            })
            current["sources"].append(source)
            existing[url] = source
            additions.append(source)
            candidate_ids.append(source["id"])
        if additions:
            current["updated_at"] = now
            research._validate_workspace(current)
            research._atomic_write(target, current)
    return {"status": "searched", "network_calls": 1, "query": search_query, "added": additions,
            "added_count": len(additions), "candidate_ids": candidate_ids}
