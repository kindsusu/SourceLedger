"""Shared contracts. Unknown source values remain None, never defaults."""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Any
from pathlib import Path
from datetime import datetime, timezone
import hashlib
import json


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def stable_id(*parts: Any) -> str:
    return hashlib.sha256(json.dumps(parts, ensure_ascii=False, sort_keys=True, default=str).encode()).hexdigest()[:24]


@dataclass
class Product:
    id: str
    name: str
    identifiers: dict[str, str] = field(default_factory=dict)
    required_specs: dict[str, str] = field(default_factory=dict)
    comparison_fields: list[str] = field(default_factory=lambda: ["currency", "unit", "pack_quantity", "tax", "price_type", "price_basis"])
    price_profile: str = "unit"


@dataclass
class Source:
    id: str
    name: str
    kind: str  # web, file
    location: str
    product_ids: list[str]
    backends: list[str] = field(default_factory=lambda: ["http", "playwright"])
    selectors: dict[str, str] = field(default_factory=dict)
    columns: dict[str, str] = field(default_factory=dict)
    recipe: list[dict[str, Any]] = field(default_factory=list)
    profile_dir: str | None = None
    allowed_domains: list[str] = field(default_factory=list)
    internal: bool = False
    timeout_seconds: float = 30
    max_attempts: int = 2
    min_interval_seconds: float = 1
    freshness_hours: float = 24
    respect_robots: bool = True
    decimal_separator: str = "."
    sheet: str | None = None
    file_root: str | None = None
    row_selector: str | None = None
    pdf_pattern: str | None = None
    account_scope: str = "public"
    recipe_version: str = "1"
    unavailable_markers: list[str] = field(default_factory=lambda: ["가격 문의", "견적 문의", "문의 요망", "Contact for price", "Request a quote"])
    adapter: str | None = None
    incremental: bool = False


@dataclass
class CollectionConfig:
    name: str
    products: list[Product]
    sources: list[Source]
    output_dir: str
    base_dir: str
    demo: bool = False
    max_run_seconds: float = 600


@dataclass
class FetchResult:
    source_id: str
    status: str  # fetched, blocked, needs_auth, timeout, tool_unavailable, policy_denied, failed
    backend: str
    content: bytes = b""
    media_type: str = "text/html"
    final_url: str = ""
    fetched_at: str = field(default_factory=utc_now)
    message: str = ""
    screenshot: bytes | None = None
    trace: list[dict[str, Any]] = field(default_factory=list)
    http_metadata: dict[str, str] = field(default_factory=dict)
    evidence_artifacts: dict[str, dict[str, str]] = field(default_factory=dict)


@dataclass
class Candidate:
    """Raw fields tied to a specific row/product block. No inferred values."""
    fields: dict[str, Any]
    evidence: dict[str, dict[str, Any]]  # field -> {location, raw}
    locator: str
    extraction_method: str
    specs: dict[str, str] = field(default_factory=dict)
    value_origin: str = "observed"
    source_visibility: str = "unconfirmed"
    derived_values: dict[str, Any] = field(default_factory=dict)
    review_flags: list[str] = field(default_factory=list)
    evidence_mode: str = "unknown"


@dataclass
class Observation:
    id: str
    run_id: str
    task_id: str
    product_id: str
    source_id: str
    source_name: str
    source_url: str
    collected_at: str
    status: str  # verified, review, price_unavailable, product_not_found
    reason: str
    raw_fields: dict[str, Any]
    evidence: dict[str, dict[str, Any]]
    evidence_path: str
    evidence_sha256: str
    locator: str
    extraction_method: str
    amount: str | None = None  # Decimal strings; no float math
    currency: str | None = None
    unit: str | None = None
    pack_quantity: str | None = None
    normalized_amount: str | None = None
    calculation: str | None = None
    comparable: bool = False
    comparison_key: str | None = None
    freshness: str = "observed"
    value_origin: str = "observed"
    source_visibility: str = "unconfirmed"
    derived_values: dict[str, Any] = field(default_factory=dict)
    review_flags: list[str] = field(default_factory=list)
    derived_amount: str | None = None
    price_profile: str = "unit"
    verification_level: str = "review"
    rental_conditions: dict[str, Any] | None = None
    evidence_mode: str = "unknown"
    evidence_artifacts: dict[str, dict[str, str]] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def resolve_path(base_dir: str, value: str) -> Path:
    p = Path(value).expanduser()
    return p.resolve() if p.is_absolute() else (Path(base_dir) / p).resolve()
