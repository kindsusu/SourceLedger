"""Bounded URL discovery; discovered links are candidates, never price evidence."""
from dataclasses import replace
from urllib.parse import urljoin, urlsplit, urldefrag
import xml.etree.ElementTree as ET

from bs4 import BeautifulSoup
from .models import Source, utc_now
from .collectors import collect


def discover(source: Source, base_dir: str, *, limit: int = 100) -> dict:
    if not 1 <= limit <= 1000:
        raise ValueError("limit은 1~1000")
    result = collect(replace(source, recipe=[]), base_dir, "file" if source.kind == "file" else "http")
    if result.status != "fetched":
        return {"status": result.status, "reason": result.message, "urls": []}
    data = result.content
    candidates = []
    stripped = data.lstrip()
    is_xml = "xml" in result.media_type and "html" not in result.media_type or stripped.startswith(b"<?xml") or stripped.startswith(b"<urlset") or stripped.startswith(b"<sitemapindex")
    if is_xml:
        if b"<!DOCTYPE" in data.upper() or b"<!ENTITY" in data.upper():
            raise ValueError("DTD가 포함된 사이트맵은 지원하지 않습니다")
        root = ET.fromstring(data)
        candidates = [(element.text or "").strip() for element in root.iter() if element.tag.rsplit("}", 1)[-1] == "loc"]
    else:
        candidates = [a.get("href") for a in BeautifulSoup(data, "html.parser").select("a[href]")]
    selected = []
    seen = set()
    for href in candidates:
        url = urldefrag(urljoin(result.final_url or source.location, href))[0]
        parsed = urlsplit(url)
        if parsed.scheme not in {"http", "https"} or parsed.username or parsed.password:
            continue
        if parsed.hostname not in source.allowed_domains or url in seen:
            continue
        selected.append(url)
        seen.add(url)
        if len(selected) >= limit:
            break
    return {"status": "discovered", "source_id": source.id, "at": utc_now(), "urls": selected,
            "limit_reached": len(selected) == limit, "note": "후보 URL입니다. 상품 범위와 추출 규칙 확인 후 sources에 추가하세요."}
