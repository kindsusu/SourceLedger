"""Offline, workspace-scoped staging of assistant-supplied source recommendations."""
from __future__ import annotations

from pathlib import Path
from typing import Any
import hashlib
import json
import re
import uuid

from . import research
from .assistant_workspace import research_path, workspace_guard
from .models import utc_now
from .search import _public_url


MAX_REQUESTS = 100
MAX_CANDIDATES = 50
MAX_FILE_BYTES = 512 * 1024
_REQUEST_KEYS = {"id", "query", "kind", "status", "created_at", "updated_at", "note", "topic", "topic_fingerprint", "candidates"}
_CANDIDATE_KEYS = {"id", "name", "url", "reason", "evidence_url", "source_id"}


def _text(value: Any, label: str, maximum: int, *, empty: bool = False) -> str:
    if not isinstance(value, str) or len(value) > maximum or (not empty and not value.strip()):
        raise ValueError(f"{label} must be a string of at most {maximum} characters" + ("" if empty else " and cannot be empty"))
    if any(ord(char) < 32 and char not in "\n\t" for char in value) or "\x7f" in value:
        raise ValueError(f"{label} contains control characters")
    return value.strip()


def _url(value: Any, label: str) -> str:
    raw = _text(value, label, 2048)
    canonical = _public_url(raw, [])
    if canonical is None:
        raise ValueError(f"{label} must be a public HTTP(S) URL without credentials")
    return canonical


def _topic(workspace: dict[str, Any]) -> dict[str, Any]:
    return {"industry": workspace["industry"], "market": workspace["market"],
            "product": {"id": workspace["product"]["id"], "name": workspace["product"]["name"]}}


def _fingerprint(topic: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(topic, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()


def _sidecar(root: Path) -> Path:
    path = root / "recommendations.json"
    if path.is_symlink():
        raise ValueError("recommendations file must not be a symbolic link")
    if path.exists() and not path.is_file():
        raise ValueError("recommendations file must be a regular file")
    return path


def _validate_candidate(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != _CANDIDATE_KEYS:
        raise ValueError("Recommendation candidate fields are invalid")
    if not isinstance(value["id"], str) or not re.fullmatch(r"candidate-[a-f0-9]{32}", value["id"]):
        raise ValueError("Recommendation candidate id is invalid")
    _text(value["name"], "name", 200)
    _text(value["reason"], "reason", 2000)
    if _url(value["url"], "url") != value["url"] or _url(value["evidence_url"], "evidence_url") != value["evidence_url"]:
        raise ValueError("Recommendation URL is not canonical")
    source_id = value["source_id"]
    if source_id is not None and (not isinstance(source_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]+", source_id)):
        raise ValueError("Recommendation source id is invalid")
    return value


def _validate(document: Any) -> dict[str, Any]:
    if not isinstance(document, dict) or set(document) != {"schema_version", "requests"} or document["schema_version"] != 1:
        raise ValueError("Recommendations schema is invalid")
    requests = document["requests"]
    if not isinstance(requests, list) or len(requests) > MAX_REQUESTS:
        raise ValueError("Recommendations request count is invalid")
    ids: set[str] = set()
    for request in requests:
        if not isinstance(request, dict) or set(request) != _REQUEST_KEYS:
            raise ValueError("Recommendation request fields are invalid")
        request_id = request["id"]
        if not isinstance(request_id, str) or not re.fullmatch(r"request-[a-f0-9]{32}", request_id) or request_id in ids:
            raise ValueError("Recommendation request id is invalid or duplicated")
        ids.add(request_id)
        _text(request["query"], "query", 1000)
        _text(request["note"], "note", 2000, empty=True)
        for key in ("created_at", "updated_at"):
            _text(request[key], key, 64)
        if request["kind"] not in {"keyword", "company"} or request["status"] not in {"pending", "ready", "empty"}:
            raise ValueError("Recommendation kind or status is invalid")
        topic = request["topic"]
        if (not isinstance(topic, dict) or set(topic) != {"industry", "market", "product"}
                or not isinstance(topic["product"], dict) or set(topic["product"]) != {"id", "name"}
                or topic["product"]["id"] != "product"):
            raise ValueError("Recommendation topic is invalid")
        _text(topic["industry"], "industry", 1000)
        _text(topic["market"], "market", 1000)
        _text(topic["product"]["name"], "product name", 1000)
        if request["topic_fingerprint"] != _fingerprint(topic):
            raise ValueError("Recommendation topic fingerprint is invalid")
        candidates = request["candidates"]
        if not isinstance(candidates, list) or len(candidates) > MAX_CANDIDATES:
            raise ValueError("Recommendation candidate count is invalid")
        candidate_ids: set[str] = set()
        urls: set[str] = set()
        for candidate in candidates:
            _validate_candidate(candidate)
            if candidate["id"] in candidate_ids or candidate["url"] in urls:
                raise ValueError("Recommendation candidates contain duplicates")
            candidate_ids.add(candidate["id"])
            urls.add(candidate["url"])
        if request["status"] == "pending" and candidates or request["status"] == "ready" and not candidates or request["status"] == "empty" and candidates:
            raise ValueError("Recommendation status does not match candidates")
    return document


def _load(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"schema_version": 1, "requests": []}
    if path.stat().st_size > MAX_FILE_BYTES:
        raise ValueError("Recommendations file is too large")
    try:
        document = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError("Recommendations file is unreadable or invalid JSON") from exc
    return _validate(document)


def _serialized_size(document: dict[str, Any]) -> int:
    """Match research._atomic_write's UTF-8, indentation, and trailing newline."""
    return len((json.dumps(document, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))


def _save(path: Path, document: dict[str, Any]) -> None:
    _validate(document)
    if _serialized_size(document) > MAX_FILE_BYTES:
        raise ValueError("Recommendations file is too large")
    _sidecar(path.parent)
    research._atomic_write(path, document)


def _request(document: dict[str, Any], request_id: str) -> dict[str, Any]:
    if not isinstance(request_id, str):
        raise ValueError("request_id is invalid")
    match = next((item for item in document["requests"] if item["id"] == request_id), None)
    if match is None:
        raise ValueError(f"Unknown recommendation request: {request_id}")
    return match


def _check_topic(request: dict[str, Any], workspace: dict[str, Any]) -> None:
    if request["topic_fingerprint"] != _fingerprint(_topic(workspace)):
        raise ValueError("Workspace topic changed since this recommendation request")


def list_recommendations(root: str | Path) -> dict[str, Any]:
    with workspace_guard(root) as base:
        research.load_workspace(research_path(base))
        return {"requests": _load(_sidecar(base))["requests"]}


def create_recommendation_request(root: str | Path, *, query: str, kind: str = "keyword") -> dict[str, Any]:
    query = _text(query, "query", 1000)
    if kind not in {"keyword", "company"}:
        raise ValueError("kind must be keyword or company")
    with workspace_guard(root) as base:
        workspace = research.load_workspace(research_path(base))
        path = _sidecar(base)
        document = _load(path)
        if len(document["requests"]) >= MAX_REQUESTS:
            raise ValueError("Recommendation request capacity reached")
        now = utc_now()
        topic = _topic(workspace)
        request = {"id": f"request-{uuid.uuid4().hex}", "query": query, "kind": kind,
                   "status": "pending", "created_at": now, "updated_at": now, "note": "",
                   "topic": topic, "topic_fingerprint": _fingerprint(topic), "candidates": []}
        document["requests"].append(request)
        _save(path, document)
        return {"request": request}


def submit_recommendations(root: str | Path, *, request_id: str, candidates: list[dict], note: str = "") -> dict[str, Any]:
    note = _text(note, "note", 2000, empty=True)
    if not isinstance(candidates, list) or len(candidates) > MAX_CANDIDATES:
        raise ValueError("candidates must be a list with at most 50 entries")
    normalized = []
    for item in candidates:
        if not isinstance(item, dict) or set(item) != {"name", "url", "reason", "evidence_url"}:
            raise ValueError("Candidate must contain exactly name, url, reason, and evidence_url")
        normalized.append({"name": _text(item["name"], "name", 200), "url": _url(item["url"], "url"),
                           "reason": _text(item["reason"], "reason", 2000),
                           "evidence_url": _url(item["evidence_url"], "evidence_url")})
    with workspace_guard(root) as base:
        workspace = research.load_workspace(research_path(base))
        path = _sidecar(base)
        document = _load(path)
        request = _request(document, request_id)
        _check_topic(request, workspace)
        known = {item["url"] for item in request["candidates"]}
        additions = []
        for item in normalized:
            if item["url"] not in known:
                known.add(item["url"])
                additions.append({"id": f"candidate-{uuid.uuid4().hex}", **item, "source_id": None})
        if len(request["candidates"]) + len(additions) > MAX_CANDIDATES:
            raise ValueError("Recommendation candidate capacity reached")
        request["candidates"].extend(additions)
        request["status"] = "ready" if request["candidates"] else "empty"
        request["note"] = note
        request["updated_at"] = utc_now()
        _save(path, document)
        return {"request": request}


def select_recommendations(root: str | Path, *, request_id: str, candidate_ids: list[str]) -> dict[str, Any]:
    if not isinstance(candidate_ids, list) or not candidate_ids or len(candidate_ids) > MAX_CANDIDATES or any(not isinstance(item, str) for item in candidate_ids):
        raise ValueError("candidate_ids must be a non-empty list of at most 50 IDs")
    if len(set(candidate_ids)) != len(candidate_ids):
        raise ValueError("candidate_ids must not contain duplicates")
    with workspace_guard(root) as base:
        workspace_file = research_path(base)
        path = _sidecar(base)
        document = _load(path)
        request = _request(document, request_id)
        with research._locked(workspace_file):
            workspace = research._load_unlocked(workspace_file)
            _check_topic(request, workspace)
            candidates = {item["id"]: item for item in request["candidates"]}
            if any(item not in candidates for item in candidate_ids):
                raise ValueError("Unknown recommendation candidate ID")
            selected = [candidates[item] for item in candidate_ids]
            by_url = {item["location"]: item for item in workspace["sources"]}
            additions = []
            source_ids = []
            now = utc_now()
            for candidate in selected:
                existing = by_url.get(candidate["url"])
                if existing is not None:
                    if existing["scope"] != "public":
                        raise ValueError("Recommendation conflicts with an internal source")
                    source = existing
                else:
                    source = research._source_record(
                        url=candidate["url"], scope="public", name=candidate["name"],
                        provenance={"method": "assistant_recommendation", "request_id": request_id,
                                    "query": request["query"], "reason": candidate["reason"],
                                    "evidence_url": candidate["evidence_url"], "at": now},
                    )
                    by_url[source["location"]] = source
                    additions.append(source)
                if candidate["source_id"] is not None and candidate["source_id"] != source["id"]:
                    raise ValueError("Recommendation source mapping conflicts with workspace")
                source_ids.append(source["id"])
            if len(workspace["sources"]) + len(additions) > research.MAX_SOURCES:
                raise ValueError("Research source capacity reached")
            # Validate both complete documents before changing either on disk.
            updated_workspace = {**workspace, "sources": [*workspace["sources"], *additions],
                                 "updated_at": now if additions else workspace["updated_at"]}
            research._validate_workspace(updated_workspace)
            for candidate, source_id in zip(selected, source_ids):
                candidate["source_id"] = source_id
            request["updated_at"] = now
            _validate(document)
            if _serialized_size(document) > MAX_FILE_BYTES:
                raise ValueError("Recommendations file is too large")
            if additions:
                research._atomic_write(workspace_file, updated_workspace)
            _save(path, document)
            return {"request": request, "source_ids": source_ids, "added_count": len(additions)}
