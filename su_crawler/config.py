from __future__ import annotations

from dataclasses import asdict
from pathlib import Path
from urllib.parse import urlsplit
import json
import math
import re

from .models import CollectionConfig, Product, Source, resolve_path, stable_id

PRICE_PROFILES = frozenset({"unit", "rental"})
SOURCE_ADAPTERS = frozenset({"jetcar", "gongcar", "funrent"})


def load_config(path: str | Path) -> CollectionConfig:
    path = Path(path).resolve()
    raw = json.loads(path.read_text(encoding="utf-8-sig"))
    allowed = {"name", "products", "sources", "output_dir", "demo", "max_run_seconds"}
    if set(raw) - allowed:
        raise ValueError(f"Unknown configuration keys: {sorted(set(raw) - allowed)}")
    products = [Product(**v) for v in raw["products"]]
    sources = [Source(**v) for v in raw["sources"]]
    if not products or not sources:
        raise ValueError("At least one product and one source are required")
    for items in (products, sources):
        ids = [i.id for i in items]
        if len(set(ids)) != len(ids) or any(not re.fullmatch(r"[A-Za-z0-9_-]+", i) for i in ids):
            raise ValueError("Product and source IDs must be unique and use letters, numbers, underscores, or hyphens")
    product_ids = {p.id for p in products}
    for product in products:
        if not product.identifiers or any(not str(v).strip() for v in product.identifiers.values()):
            raise ValueError(f"Product {product.id}: identifiers are required for source matching")
        if not isinstance(product.price_profile, str) or product.price_profile not in PRICE_PROFILES:
            raise ValueError(f"Product {product.id}: price_profile must be unit or rental")
    for source in sources:
        if source.kind not in {"web", "file"}:
            raise ValueError(f"Unsupported source type: {source.kind}")
        if source.adapter is not None and (not isinstance(source.adapter, str) or source.adapter not in SOURCE_ADAPTERS):
            raise ValueError(f"Source {source.id}: adapter must be jetcar, gongcar, funrent, or null")
        if not isinstance(source.incremental, bool):
            raise ValueError(f"Source {source.id}: incremental must be true or false")
        if not source.product_ids or set(source.product_ids) - product_ids:
            raise ValueError(f"Source {source.id}: configure at least one product ID")
        if len(source.product_ids) != len(set(source.product_ids)):
            raise ValueError("Source product IDs must not be duplicated")
        if isinstance(source.max_attempts, bool) or not isinstance(source.max_attempts, int) or not 1 <= source.max_attempts <= 5:
            raise ValueError("max_attempts must be an integer from 1 to 5")
        for key in ("timeout_seconds", "freshness_hours", "min_interval_seconds"):
            value = getattr(source, key)
            if not isinstance(value, (float, int)) or not math.isfinite(value) or value < 0 or (key != "min_interval_seconds" and value == 0):
                raise ValueError(f"Source {source.id}: configure {key}")
        if source.decimal_separator not in {".", ","}:
            raise ValueError("decimal_separator must be . or ,")
        if source.kind == "web":
            parsed = urlsplit(source.location)
            if parsed.scheme not in {"https", "http"} or not parsed.hostname or parsed.username or parsed.password:
                raise ValueError("Web sources must use an HTTP(S) URL without credentials")
            if not source.allowed_domains or parsed.hostname.lower() not in [d.lower() for d in source.allowed_domains]:
                raise ValueError(f"Source {source.id}: add the URL host to allowed_domains")
            if not source.backends or set(source.backends) - {"http", "playwright", "crawl4ai"}:
                raise ValueError("Supported collectors are http, playwright, and crawl4ai")
        if source.profile_dir:
            source.profile_dir = str(resolve_path(str(path.parent), source.profile_dir))
        if source.file_root:
            source.file_root = str(resolve_path(str(path.parent), source.file_root))
    seconds = raw.get("max_run_seconds", 600)
    if not isinstance(seconds, (int, float)) or not math.isfinite(seconds) or seconds <= 0:
        raise ValueError("max_run_seconds must be positive")
    return CollectionConfig(
        name=raw.get("name", path.stem), products=products, sources=sources,
        output_dir=str(resolve_path(str(path.parent), raw.get("output_dir", "outputs"))),
        base_dir=str(path.parent), demo=raw.get("demo", False), max_run_seconds=seconds,
    )


def config_fingerprint(config: CollectionConfig) -> str:
    return stable_id(asdict(config))
