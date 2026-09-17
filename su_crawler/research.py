"""Bounded research workspace management.

Research workspaces hold human-supplied intent and source candidates.  They are
deliberately separate from :class:`CollectionConfig`, which remains the exact
contract used by the collection pipeline.
"""
from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator
from urllib.parse import urlsplit, urlunsplit
import json
import os
import re
import uuid

from .discovery import discover
from .models import Source, stable_id, utc_now


SCHEMA_VERSION = 1
MAX_SOURCES = 1000
_WORKSPACE_KEYS = {
    "schema_version", "locale", "industry", "market", "product", "sources",
    "created_at", "updated_at",
}
_SOURCE_SCOPES = {"public", "internal"}


def _path(value: str | Path) -> Path:
    return Path(value).expanduser().resolve()


@contextmanager
def _locked(path: Path) -> Iterator[None]:
    """Use a sibling OS lock so concurrent writers cannot lose updates."""
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_name(path.name + ".lock")
    handle = lock_path.open("a+b")
    try:
        try:
            handle.seek(0, os.SEEK_END)
            if handle.tell() == 0:
                handle.write(b"0")
                handle.flush()
            handle.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise RuntimeError(f"Workspace is busy: {path}") from exc
        yield
    finally:
        handle.close()


def _atomic_write(path: Path, value: dict[str, Any]) -> None:
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


def _required_text(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} is required")
    return value.strip()


def _string_map(value: Any, label: str, *, allow_empty: bool) -> dict[str, str]:
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be an object")
    result: dict[str, str] = {}
    for key, item in value.items():
        if not isinstance(key, str) or not key.strip() or not isinstance(item, str) or not item.strip():
            raise ValueError(f"{label} keys and values must be non-empty strings")
        normalized_key = key.strip()
        if normalized_key in result:
            raise ValueError(f"{label} contains duplicate keys after trimming")
        result[normalized_key] = item.strip()
    if not allow_empty and not result:
        raise ValueError(f"{label} must contain at least one exact value")
    return result


def _canonical_url(value: str) -> tuple[str, str]:
    raw = _required_text(value, "url")
    if "\\" in raw or any(ord(character) < 32 or ord(character) == 127 for character in raw):
        raise ValueError("Source URL must not contain control characters or backslashes")
    parsed = urlsplit(raw)
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
        raise ValueError("Source URL must use HTTP or HTTPS")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("Source URL must not contain credentials")
    try:
        port = parsed.port
    except ValueError as exc:
        raise ValueError("Source URL has an invalid port") from exc
    scheme = parsed.scheme.lower()
    hostname = parsed.hostname.rstrip(".").lower()
    if not hostname or any(character.isspace() for character in hostname):
        raise ValueError("Source URL has an invalid hostname")
    rendered_host = f"[{hostname}]" if ":" in hostname else hostname
    if port is not None and not ((scheme == "http" and port == 80) or (scheme == "https" and port == 443)):
        rendered_host = f"{rendered_host}:{port}"
    path = parsed.path or "/"
    return urlunsplit((scheme, rendered_host, path, parsed.query, "")), hostname


def _validate_source(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError("Each source must be an object")
    location, hostname = _canonical_url(value.get("location", ""))
    scope = value.get("scope")
    if scope not in _SOURCE_SCOPES:
        raise ValueError("Source scope must be public or internal")
    if value.get("kind") != "web" or value.get("status") != "candidate":
        raise ValueError("Research sources must be candidate web sources")
    if value.get("location") != location or value.get("allowed_domains") != [hostname]:
        raise ValueError("Source URL or allowlist is not canonical")
    if value.get("internal") is not (scope == "internal"):
        raise ValueError("Source internal flag does not match its scope")
    if value.get("product_ids") != ["product"]:
        raise ValueError("Research sources must target the workspace product")
    return value


def _validate_workspace(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError("Workspace must be a JSON object")
    if set(value) != _WORKSPACE_KEYS or "analysis_purpose" in value:
        raise ValueError("Workspace fields do not match schema version 1")
    if value.get("schema_version") != SCHEMA_VERSION:
        raise ValueError("Unsupported workspace schema version")
    if value.get("locale") not in {"en", "ko"}:
        raise ValueError("locale must be en or ko")
    _required_text(value.get("industry"), "industry")
    _required_text(value.get("market"), "market")
    _required_text(value.get("created_at"), "created_at")
    _required_text(value.get("updated_at"), "updated_at")
    product = value.get("product")
    if not isinstance(product, dict) or set(product) != {"id", "name", "identifiers", "required_specs"}:
        raise ValueError("Workspace product does not match schema version 1")
    if product.get("id") != "product":
        raise ValueError("Workspace product id must be product")
    _required_text(product.get("name"), "product")
    _string_map(product.get("identifiers"), "identifiers", allow_empty=True)
    _string_map(product.get("required_specs"), "required_specs", allow_empty=True)
    sources = value.get("sources")
    if not isinstance(sources, list) or len(sources) > MAX_SOURCES:
        raise ValueError(f"sources must be a list with at most {MAX_SOURCES} entries")
    seen_ids: set[str] = set()
    seen_urls: set[str] = set()
    for source in sources:
        _validate_source(source)
        source_id = source.get("id")
        if not isinstance(source_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]+", source_id):
            raise ValueError("Source id is invalid")
        if source_id in seen_ids or source["location"] in seen_urls:
            raise ValueError("Workspace contains duplicate sources")
        seen_ids.add(source_id)
        seen_urls.add(source["location"])
    return value


def _load_unlocked(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise ValueError(f"Workspace does not exist: {path}")
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
    except json.JSONDecodeError as exc:
        raise ValueError("Workspace is not valid JSON") from exc
    return _validate_workspace(value)


def init_workspace(
    path: str | Path, *, industry: str, product: str, market: str, locale: str = "en"
) -> dict[str, Any]:
    """Create a research workspace without implying that collection is ready."""
    target = _path(path)
    if locale not in {"en", "ko"}:
        raise ValueError("locale must be en or ko")
    now = utc_now()
    value = {
        "schema_version": SCHEMA_VERSION,
        "locale": locale,
        "industry": _required_text(industry, "industry"),
        "market": _required_text(market, "market"),
        "product": {
            "id": "product",
            "name": _required_text(product, "product"),
            "identifiers": {},
            "required_specs": {},
        },
        "sources": [],
        "created_at": now,
        "updated_at": now,
    }
    _validate_workspace(value)
    with _locked(target):
        if target.exists():
            raise FileExistsError(f"Workspace already exists: {target}")
        _atomic_write(target, value)
    return value


def load_workspace(path: str | Path) -> dict[str, Any]:
    """Load and validate a workspace. This performs no network calls."""
    return _load_unlocked(_path(path))


def research_status(path: str | Path) -> dict[str, Any]:
    """Return explicit readiness gates and the intentionally absent search provider."""
    value = load_workspace(path)
    blockers: list[str] = []
    if not value["product"]["identifiers"]:
        blockers.append("missing_exact_product_identifiers")
    if not value["sources"]:
        blockers.append("missing_source_candidates")
    blockers.append("no_verified_source")
    if value["locale"] == "ko":
        next_actions = []
        if not value["product"]["identifiers"]:
            next_actions.append("정확한 모델, SKU 또는 다른 상품 식별자를 설정하세요.")
        if not value["sources"]:
            next_actions.append("명시적인 공개 또는 승인된 내부 출처 후보를 추가하세요.")
        else:
            next_actions.append("후보 출처 ID를 사용해 제한된 링크 발견을 실행하거나 출처 규칙을 검증하세요.")
        next_actions.append("대표 근거와 추출 규칙을 별도로 검증한 뒤 정확 수집 설정을 활성화하세요.")
    else:
        next_actions = []
        if not value["product"]["identifiers"]:
            next_actions.append("Set an exact model, SKU, or other product identifier.")
        if not value["sources"]:
            next_actions.append("Add an explicit public or authorized internal source candidate.")
        else:
            next_actions.append("Use a candidate source ID for bounded link discovery or source-rule verification.")
        next_actions.append("Verify representative evidence and extraction rules before activating an exact collection config.")
    return {
        "schema_version": value["schema_version"],
        "locale": value["locale"],
        "ready": False,
        "candidate_source_count": len(value["sources"]),
        "verified_source_count": 0,
        "blockers": blockers,
        "focus": "research candidates only; verified config tracked separately via receipts",
        "sources": [{
            "id": source["id"], "location": source["location"],
            "status": source["status"], "scope": source["scope"],
        } for source in value["sources"]],
        "next_actions": next_actions,
        "search": {"status": "search_provider_unconfigured", "network_calls": 0},
    }


def _source_record(*, url: str, scope: str, name: str | None, provenance: dict[str, Any]) -> dict[str, Any]:
    location, hostname = _canonical_url(url)
    if scope not in _SOURCE_SCOPES:
        raise ValueError("scope must be public or internal")
    return {
        "id": f"source-{stable_id(location)}",
        "name": _required_text(name, "name") if name is not None else hostname,
        "kind": "web",
        "location": location,
        "allowed_domains": [hostname],
        "product_ids": ["product"],
        "scope": scope,
        "internal": scope == "internal",
        "status": "candidate",
        "provenance": provenance,
    }


def add_source(
    path: str | Path, *, url: str, scope: str = "public", name: str | None = None
) -> dict[str, Any]:
    """Register an explicit source candidate without contacting it."""
    target = _path(path)
    now = utc_now()
    candidate = _source_record(
        url=url, scope=scope, name=name,
        provenance={"method": "explicit", "at": now},
    )
    with _locked(target):
        value = _load_unlocked(target)
        for source in value["sources"]:
            if source["location"] == candidate["location"]:
                if source["scope"] != candidate["scope"]:
                    raise ValueError("The same source URL cannot use different scopes")
                return value
        if len(value["sources"]) >= MAX_SOURCES:
            raise ValueError(f"Workspace cannot contain more than {MAX_SOURCES} sources")
        value["sources"].append(candidate)
        value["updated_at"] = now
        _validate_workspace(value)
        _atomic_write(target, value)
        return value


def set_product(
    path: str | Path, *, identifiers: dict[str, str], required_specs: dict[str, str] | None = None
) -> dict[str, Any]:
    """Set exact, explicit product identity without deriving it from the free-text name."""
    target = _path(path)
    exact = _string_map(identifiers, "identifiers", allow_empty=False)
    specs = None if required_specs is None else _string_map(required_specs, "required_specs", allow_empty=True)
    with _locked(target):
        value = _load_unlocked(target)
        value["product"]["identifiers"] = exact
        if specs is not None:
            value["product"]["required_specs"] = specs
        value["updated_at"] = utc_now()
        _validate_workspace(value)
        _atomic_write(target, value)
        return value


def discover_candidates(path: str | Path, *, source_id: str, limit: int = 100) -> dict[str, Any]:
    """Discover bounded same-host links from one explicit seed and store only candidates."""
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= MAX_SOURCES:
        raise ValueError(f"limit must be between 1 and {MAX_SOURCES}")
    target = _path(path)
    snapshot = load_workspace(target)
    parent = next((item for item in snapshot["sources"] if item["id"] == source_id), None)
    if parent is None:
        raise ValueError(f"Unknown source id: {source_id}")
    seed = Source(
        id=parent["id"], name=parent["name"], kind="web", location=parent["location"],
        product_ids=["product"], allowed_domains=list(parent["allowed_domains"]),
        internal=parent["internal"], respect_robots=True, backends=["http", "playwright"],
    )
    result = discover(seed, str(target.parent), limit=limit)
    if result.get("status") != "discovered":
        return {
            "status": result.get("status", "failed"), "source_id": source_id,
            "added": [], "reason": result.get("reason", "Discovery did not complete"),
        }

    discovered_at = utc_now()
    additions: list[dict[str, Any]] = []
    skipped_scope_conflicts: list[str] = []
    with _locked(target):
        value = _load_unlocked(target)
        current_parent = next((item for item in value["sources"] if item["id"] == source_id), None)
        if current_parent is None or current_parent["location"] != parent["location"] or current_parent["scope"] != parent["scope"]:
            raise RuntimeError("Seed source changed during discovery")
        by_url = {item["location"]: item for item in value["sources"]}
        parent_host = parent["allowed_domains"][0]
        for raw_url in result.get("urls", [])[:limit]:
            try:
                location, hostname = _canonical_url(raw_url)
            except (TypeError, ValueError):
                continue
            if hostname != parent_host:
                continue
            existing = by_url.get(location)
            if existing is not None:
                if existing["scope"] != parent["scope"]:
                    skipped_scope_conflicts.append(location)
                continue
            if len(value["sources"]) >= MAX_SOURCES:
                break
            candidate = _source_record(
                url=location, scope=parent["scope"], name=None,
                provenance={"method": "seed_discovery", "discovered_from": source_id, "at": discovered_at},
            )
            value["sources"].append(candidate)
            by_url[location] = candidate
            additions.append(candidate)
        if additions:
            value["updated_at"] = discovered_at
            _validate_workspace(value)
            _atomic_write(target, value)
    return {
        "status": "discovered", "source_id": source_id, "added": additions,
        "added_count": len(additions), "skipped_scope_conflicts": skipped_scope_conflicts,
        "limit_reached": bool(result.get("limit_reached")),
    }


def generate_draft(path: str | Path, *, output_path: str | Path) -> dict[str, Any]:
    """Generate a strict CollectionConfig-shaped draft without claiming readiness."""
    workspace_path = _path(path)
    destination = _path(output_path)
    if destination == workspace_path:
        raise ValueError("Draft path must be different from the research workspace")
    with _locked(workspace_path):
        value = _load_unlocked(workspace_path)
        identifiers = value["product"]["identifiers"]
        if not identifiers:
            raise ValueError("Exact product identifiers are required before generating a draft")
        if not value["sources"]:
            raise ValueError("At least one explicit source candidate is required before generating a draft")
        config = {
            "name": f"{value['industry']} — {value['product']['name']} — {value['market']}",
            "output_dir": str((workspace_path.parent / "outputs").resolve()),
            "products": [{
                "id": "product", "name": value["product"]["name"],
                "identifiers": dict(identifiers),
                "required_specs": dict(value["product"]["required_specs"]),
            }],
            "sources": [{
                "id": source["id"], "name": source["name"], "kind": "web",
                "location": source["location"], "allowed_domains": list(source["allowed_domains"]),
                "product_ids": ["product"], "backends": ["http", "playwright"],
                "internal": source["internal"], "account_scope": source["scope"],
                "selectors": {}, "recipe": [],
            } for source in value["sources"][:MAX_SOURCES]],
        }
        with _locked(destination):
            if destination.exists():
                raise FileExistsError(f"Draft already exists: {destination}")
            _atomic_write(destination, config)
    return {
        "ready": False,
        "warning": "Draft only: source extraction rules and representative evidence are not verified.",
        "path": str(destination),
        "source_count": len(config["sources"]),
        "config": config,
    }
