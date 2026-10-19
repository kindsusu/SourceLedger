"""Bounded URL discovery; discovered links are candidates, never price evidence."""
from dataclasses import replace
from urllib.parse import urljoin, urlsplit, urldefrag
import xml.etree.ElementTree as ET

from bs4 import BeautifulSoup
from .models import Source, utc_now
from .collectors import collect


def _links(result, source: Source, limit: int) -> list[str]:
    data = result.content
    candidates = []
    stripped = data.lstrip()
    is_xml = "xml" in result.media_type and "html" not in result.media_type or stripped.startswith(b"<?xml") or stripped.startswith(b"<urlset") or stripped.startswith(b"<sitemapindex")
    if is_xml:
        if b"<!DOCTYPE" in data.upper() or b"<!ENTITY" in data.upper():
            raise ValueError("Sitemaps containing a DTD are not supported")
        try:
            root = ET.fromstring(data)
        except ET.ParseError as exc:
            raise ValueError("Malformed sitemap XML") from exc
        candidates = [(element.text or "").strip() for element in root.iter() if element.tag.rsplit("}", 1)[-1] == "loc"]
    else:
        candidates = [a.get("href") for a in BeautifulSoup(data, "html.parser").select("a[href]")]
    selected = []
    seen = set()
    for href in candidates:
        if not href or any(ord(char) < 32 or ord(char) == 127 or char == "\\" for char in href):
            continue
        try:
            url = urldefrag(urljoin(result.final_url or source.location, href))[0]
            parsed = urlsplit(url)
            parsed.port  # Validate malformed ports before returning the URL.
        except ValueError:
            continue
        if parsed.scheme not in {"http", "https"} or parsed.username is not None or parsed.password is not None:
            continue
        if parsed.hostname not in {domain.lower() for domain in source.allowed_domains} or url in seen:
            continue
        selected.append(url)
        seen.add(url)
        if len(selected) >= limit:
            break
    return selected


def discover(source: Source, base_dir: str, *, limit: int = 100) -> dict:
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 1000:
        raise ValueError("limit must be between 1 and 1000")
    backends = ["file"] if source.kind == "file" else list(dict.fromkeys(source.backends))
    if not backends or set(backends) - {"file", "http", "playwright", "crawl4ai"}:
        raise ValueError("No supported discovery backend is configured")
    attempts = []
    fetched = None
    selected = []
    # One attempt per configured backend. Discovery never executes price recipes.
    for backend in backends:
        result = collect(replace(source, recipe=[]), base_dir, backend)
        attempts.append({"backend": backend, "status": result.status, "reason": result.message})
        if result.status == "fetched":
            fetched = result
            selected = _links(result, source, limit)
            if selected:
                break
        elif result.status == "policy_denied":
            break
    if fetched is None:
        return {"status": result.status, "reason": result.message, "urls": [], "attempts": attempts}
    return {"status": "discovered", "source_id": source.id, "at": utc_now(), "urls": selected,
            "backend": fetched.backend, "attempts": attempts, "limit_reached": len(selected) == limit,
            "note": "Candidate URLs only. Review product scope and extraction rules before adding them to sources."}
