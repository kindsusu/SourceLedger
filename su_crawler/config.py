from __future__ import annotations

from dataclasses import asdict
from pathlib import Path
from urllib.parse import urlsplit
import json
import math
import re

from .models import CollectionConfig, Product, Source, resolve_path, stable_id


def load_config(path: str | Path) -> CollectionConfig:
    path = Path(path).resolve()
    raw = json.loads(path.read_text(encoding="utf-8-sig"))
    allowed = {"name", "products", "sources", "output_dir", "demo", "max_run_seconds"}
    if set(raw) - allowed:
        raise ValueError(f"알 수 없는 설정: {sorted(set(raw) - allowed)}")
    products = [Product(**v) for v in raw["products"]]
    sources = [Source(**v) for v in raw["sources"]]
    if not products or not sources:
        raise ValueError("상품과 출처가 각각 하나 이상 필요합니다")
    for items in (products, sources):
        ids = [i.id for i in items]
        if len(set(ids)) != len(ids) or any(not re.fullmatch(r"[A-Za-z0-9_-]+", i) for i in ids):
            raise ValueError("상품/출처 ID는 중복 없는 영문·숫자·밑줄·하이픈이어야 합니다")
    product_ids = {p.id for p in products}
    for product in products:
        if not product.identifiers or any(not str(v).strip() for v in product.identifiers.values()):
            raise ValueError(f"상품 {product.id}: 원문과 대조할 identifiers가 필요합니다")
    for source in sources:
        if source.kind not in {"web", "file"}:
            raise ValueError(f"지원하지 않는 출처 종류: {source.kind}")
        if not source.product_ids or set(source.product_ids) - product_ids:
            raise ValueError(f"출처 {source.id}: 연결할 상품 ID 확인 필요")
        if len(source.product_ids) != len(set(source.product_ids)):
            raise ValueError("출처별 상품 ID 중복")
        if isinstance(source.max_attempts, bool) or not isinstance(source.max_attempts, int) or not 1 <= source.max_attempts <= 5:
            raise ValueError("max_attempts는 1~5 정수입니다")
        for key in ("timeout_seconds", "freshness_hours", "min_interval_seconds"):
            value = getattr(source, key)
            if not isinstance(value, (float, int)) or not math.isfinite(value) or value < 0 or (key != "min_interval_seconds" and value == 0):
                raise ValueError(f"출처 {source.id}: {key} 값 확인 필요")
        if source.decimal_separator not in {".", ","}:
            raise ValueError("decimal_separator는 . 또는 , 입니다")
        if source.kind == "web":
            parsed = urlsplit(source.location)
            if parsed.scheme not in {"https", "http"} or not parsed.hostname or parsed.username or parsed.password:
                raise ValueError("웹 출처는 인증값 없는 HTTP(S) URL이어야 합니다")
            if not source.allowed_domains or parsed.hostname.lower() not in [d.lower() for d in source.allowed_domains]:
                raise ValueError(f"출처 {source.id}: URL 호스트를 allowed_domains에 명시하세요")
            if not source.backends or set(source.backends) - {"http", "playwright", "crawl4ai"}:
                raise ValueError("지원 수집기는 http/playwright/crawl4ai입니다")
        if source.profile_dir:
            source.profile_dir = str(resolve_path(str(path.parent), source.profile_dir))
        if source.file_root:
            source.file_root = str(resolve_path(str(path.parent), source.file_root))
    seconds = raw.get("max_run_seconds", 600)
    if not isinstance(seconds, (int, float)) or not math.isfinite(seconds) or seconds <= 0:
        raise ValueError("max_run_seconds는 양수여야 합니다")
    return CollectionConfig(
        name=raw.get("name", path.stem), products=products, sources=sources,
        output_dir=str(resolve_path(str(path.parent), raw.get("output_dir", "outputs"))),
        base_dir=str(path.parent), demo=raw.get("demo", False), max_run_seconds=seconds,
    )


def config_fingerprint(config: CollectionConfig) -> str:
    return stable_id(asdict(config))
