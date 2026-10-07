"""Editable, revisioned research plans. Suggestions are never observations."""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator
from urllib.parse import urlsplit, urlunsplit
import hashlib
import ipaddress
import json
import re
import sqlite3
import uuid

from .assistant_workspace import workspace_guard


MAX_REQUEST = 12000
MAX_ITEMS = 100
MAX_DOCUMENT_BYTES = 256 * 1024
_PREVIEW_KEYS = {"summary", "topic", "categories", "candidates", "note", "include_terms", "exclude_terms"}
_EDIT_KEYS = {"request_text", "include_terms", "exclude_terms", "topic", "summary", "categories",
              "selected_candidate_ids", "removed_candidate_ids", "added_candidates"}
_CANDIDATE_KEYS = {"name", "url", "evidence_url", "reason", "kind"}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _object(value: Any, label: str, allowed: set[str]) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) - allowed:
        raise ValueError(f"{label} must be an object with only allowed fields")
    return value


def _text(value: Any, label: str, maximum: int, *, required: bool = False) -> str:
    if not isinstance(value, str) or len(value) > maximum or (required and not value.strip()):
        raise ValueError(f"{label} must be {'nonempty ' if required else ''}text of at most {maximum} characters")
    if any(ord(c) < 32 and c not in "\r\n\t" for c in value):
        raise ValueError(f"{label} contains a control character")
    return value.strip()


def _strings(value: Any, label: str, maximum: int = 160) -> list[str]:
    if not isinstance(value, list) or len(value) > MAX_ITEMS:
        raise ValueError(f"{label} must be a list of at most {MAX_ITEMS} strings")
    result: list[str] = []
    seen: set[str] = set()
    for item in value:
        clean = _text(item, label, maximum, required=True)
        if clean.casefold() not in seen:
            result.append(clean)
            seen.add(clean.casefold())
    return result


def _merged(left: list[str], right: list[str]) -> list[str]:
    combined = {}
    for term in left + right:
        combined.setdefault(term.casefold(), term)
    return _strings(list(combined.values()), "terms")


def _topic(value: Any) -> dict[str, str]:
    data = _object(value, "topic", {"industry", "product", "market"})
    return {key: _text(data.get(key, ""), key, 160) for key in ("industry", "product", "market")}


def canonical_url(value: Any) -> str:
    """Accept only direct, public HTTP(S) URLs and remove their fragments."""
    raw = _text(value, "URL", 2048, required=True)
    if re.search(r"[\s\\]", raw):
        raise ValueError("URL contains whitespace or a backslash")
    try:
        parts = urlsplit(raw)
        scheme = parts.scheme.lower()
        host = parts.hostname
        port = parts.port
    except ValueError as exc:
        raise ValueError("Invalid URL") from exc
    if scheme not in {"http", "https"} or not host or parts.username is not None or parts.password is not None:
        raise ValueError("URL must be public HTTP(S) without credentials")
    if host.lower().rstrip(".") == "localhost" or host.lower().rstrip(".").endswith((".local", ".localhost")):
        raise ValueError("URL must have a public host")
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        try:
            host = host.encode("idna").decode("ascii").lower()
        except UnicodeError as exc:
            raise ValueError("Invalid URL host") from exc
        if "." not in host or not re.fullmatch(r"[a-z0-9.-]+", host) or ".." in host:
            raise ValueError("URL must have a public host")
        host = host.rstrip(".")
        if not host or any(not label or label.startswith("-") or label.endswith("-") for label in host.split(".")):
            raise ValueError("Invalid URL host")
    else:
        if not address.is_global:
            raise ValueError("URL must have a public host")
        host = f"[{address.compressed}]" if address.version == 6 else str(address)
    if port is not None and not 1 <= port <= 65535:
        raise ValueError("Invalid URL port")
    authority = host + (f":{port}" if port is not None and port != {"http": 80, "https": 443}[scheme] else "")
    return urlunsplit((scheme, authority, parts.path or "/", parts.query, ""))


def _candidate(value: Any, origin: str) -> dict[str, Any]:
    data = _object(value, "candidate", _CANDIDATE_KEYS)
    url = canonical_url(data.get("url"))
    kind = data.get("kind", "site") if origin == "user" else data.get("kind")
    if kind not in {"site", "product", "category"}:
        raise ValueError("candidate kind must be site, product, or category")
    evidence = canonical_url(data.get("evidence_url", url) if origin == "user" else data.get("evidence_url"))
    name = _text(data.get("name", ""), "candidate name", 200)
    reason = _text(data.get("reason", ""), "candidate reason", 1000)
    if origin == "ai" and not name:
        raise ValueError("AI candidate name is required")
    if origin == "user" and not name:
        name = urlsplit(url).hostname or url
    return {"id": "target-" + hashlib.sha256(url.encode("utf-8")).hexdigest()[:24],
            "name": name, "url": url, "evidence_url": evidence, "reason": reason,
            "kind": kind, "selected": True, "origin": origin}


def _path(root: Path) -> Path:
    db = root / "plans.sqlite3"
    for suffix in ("", "-journal", "-wal", "-shm"):
        child = Path(str(db) + suffix)
        if child.is_symlink():
            raise ValueError("plans database and sidecars must not be symbolic links")
    if db.exists() and not db.is_file():
        raise ValueError("plans database must be a file")
    return db


@contextmanager
def _db(root: str | Path) -> Iterator[sqlite3.Connection]:
    # The existing workspace lock serializes all plan read/write transactions.
    with workspace_guard(root) as base:
        path = _path(base)
        conn = sqlite3.connect(path, timeout=2)
        try:
            conn.execute("CREATE TABLE IF NOT EXISTS plan_revisions ("
                         "plan_id TEXT NOT NULL, revision INTEGER NOT NULL, body TEXT NOT NULL, "
                         "PRIMARY KEY (plan_id, revision))")
            yield conn
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()


def _id(value: Any) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"plan-[0-9a-f]{32}", value):
        raise ValueError("Invalid plan ID")
    return value


def _revision(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError("revision must be a positive integer")
    return value


def _load(db: sqlite3.Connection, plan_id: str, revision: int | None = None) -> dict[str, Any]:
    if revision is None:
        row = db.execute("SELECT body FROM plan_revisions WHERE plan_id=? ORDER BY revision DESC LIMIT 1", (plan_id,)).fetchone()
    else:
        row = db.execute("SELECT body FROM plan_revisions WHERE plan_id=? AND revision=?", (plan_id, revision)).fetchone()
    if row is None:
        raise ValueError("Research plan or revision not found")
    return json.loads(row[0])


def _save(db: sqlite3.Connection, plan: dict[str, Any]) -> None:
    encoded = json.dumps(plan, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    if len(encoded.encode("utf-8")) > MAX_DOCUMENT_BYTES:
        raise ValueError("Research plan exceeds size limit")
    db.execute("INSERT INTO plan_revisions (plan_id,revision,body) VALUES (?,?,?)",
               (plan["id"], plan["revision"], encoded))


def _current(db: sqlite3.Connection, plan_id: str, expected_revision: int) -> dict[str, Any]:
    plan = _load(db, _id(plan_id))
    if plan["revision"] != _revision(expected_revision):
        raise ValueError("Research plan revision changed; reload before editing")
    return plan


def _next(plan: dict[str, Any]) -> dict[str, Any]:
    return {**plan, "revision": plan["revision"] + 1, "updated_at": _now(), "confirmed_at": None}


def create_plan(root: str | Path, request_text: str, include_terms: list[str] | None = None,
                exclude_terms: list[str] | None = None) -> dict[str, Any]:
    request = _text(request_text, "request_text", MAX_REQUEST, required=True)
    included = _strings(include_terms if include_terms is not None else [], "include_terms")
    excluded = _strings(exclude_terms if exclude_terms is not None else [], "exclude_terms")
    with _db(root) as db:
        now = _now()
        plan = {"id": "plan-" + uuid.uuid4().hex, "revision": 1, "state": "draft",
                "request_text": request, "include_terms": included, "exclude_terms": excluded,
                "topic": _topic({}), "summary": "", "categories": [],
                "candidates": [], "excluded_urls": [], "note": "", "created_at": now,
                "updated_at": now, "confirmed_at": None}
        _save(db, plan)
    return {"plan": plan}


def list_plans(root: str | Path) -> dict[str, Any]:
    with _db(root) as db:
        rows = db.execute("SELECT body FROM plan_revisions AS p WHERE revision=("
                          "SELECT MAX(revision) FROM plan_revisions WHERE plan_id=p.plan_id) "
                          "ORDER BY rowid DESC LIMIT 100").fetchall()
    return {"plans": [json.loads(row[0]) for row in rows]}


def get_plan(root: str | Path, plan_id: str) -> dict[str, Any]:
    with _db(root) as db:
        return {"plan": _load(db, _id(plan_id))}


def revise_plan(root: str | Path, plan_id: str, expected_revision: int, changes: dict[str, Any]) -> dict[str, Any]:
    edit = _object(changes, "changes", _EDIT_KEYS)
    if not edit:
        raise ValueError("changes must not be empty")
    with _db(root) as db:
        plan = _next(_current(db, plan_id, expected_revision))
        for key in ("request_text", "summary"):
            if key in edit:
                plan[key] = _text(edit[key], key, MAX_REQUEST if key == "request_text" else 3000,
                                  required=key == "request_text")
        for key in ("include_terms", "exclude_terms", "categories"):
            if key in edit:
                plan[key] = _strings(edit[key], key, 300 if key == "categories" else 160)
        if "topic" in edit:
            plan["topic"] = _topic({**plan["topic"], **_object(edit["topic"], "topic", {"industry", "product", "market"})})
        candidates = [dict(item) for item in plan["candidates"]]
        ids = {item["id"] for item in candidates}
        if "selected_candidate_ids" in edit:
            selected = set(_strings(edit["selected_candidate_ids"], "selected_candidate_ids", 100))
            if selected - ids:
                raise ValueError("Unknown selected candidate ID")
            for item in candidates:
                item["selected"] = item["id"] in selected
        if "removed_candidate_ids" in edit:
            removed = set(_strings(edit["removed_candidate_ids"], "removed_candidate_ids", 100))
            if removed - ids:
                raise ValueError("Unknown removed candidate ID")
            plan["excluded_urls"] = list(dict.fromkeys(plan["excluded_urls"] +
                                                       [item["url"] for item in candidates if item["id"] in removed]))
            candidates = [item for item in candidates if item["id"] not in removed]
        if "added_candidates" in edit:
            additions = edit["added_candidates"]
            if not isinstance(additions, list) or len(additions) > MAX_ITEMS:
                raise ValueError("added_candidates must be a bounded list")
            for raw in additions:
                item = _candidate(raw, "user")
                if item["url"] in plan["excluded_urls"]:
                    plan["excluded_urls"].remove(item["url"])
                candidates = [existing for existing in candidates if existing["id"] != item["id"]]
                candidates.append(item)
        if len(candidates) > MAX_ITEMS:
            raise ValueError("Too many candidates")
        plan["candidates"] = candidates
        plan["state"] = "preview" if candidates else "draft"
        _save(db, plan)
    return {"plan": plan}


def submit_plan_preview(root: str | Path, plan_id: str, expected_revision: int,
                        preview: dict[str, Any]) -> dict[str, Any]:
    data = _object(preview, "preview", _PREVIEW_KEYS)
    summary = _text(data.get("summary", ""), "summary", 3000)
    topic = _topic(data.get("topic", {}))
    categories = _strings(data.get("categories", []), "categories", 300)
    note = _text(data.get("note", ""), "note", 3000)
    included = _strings(data.get("include_terms", []), "include_terms")
    excluded = _strings(data.get("exclude_terms", []), "exclude_terms")
    raw_candidates = data.get("candidates", [])
    if not isinstance(raw_candidates, list) or len(raw_candidates) > MAX_ITEMS:
        raise ValueError("candidates must be a bounded list")
    proposed = [_candidate(item, "ai") for item in raw_candidates]
    with _db(root) as db:
        plan = _next(_current(db, plan_id, expected_revision))
        previous = {item["url"]: item for item in plan["candidates"]}
        combined: dict[str, dict[str, Any]] = {}
        for item in proposed:
            if item["url"] in plan["excluded_urls"]:
                continue
            if item["url"] in previous:
                item["selected"] = previous[item["url"]]["selected"]
                if previous[item["url"]]["origin"] == "user":
                    item = previous[item["url"]]
            combined[item["url"]] = item
        # Keep manually entered targets even when a later model response omits them.
        for item in plan["candidates"]:
            if item["origin"] == "user" and item["url"] not in plan["excluded_urls"]:
                combined[item["url"]] = item
        if len(combined) > MAX_ITEMS:
            raise ValueError("Too many candidates")
        plan.update(summary=summary, topic=topic, categories=categories, note=note,
                    include_terms=_merged(plan["include_terms"], included),
                    exclude_terms=_merged(plan["exclude_terms"], excluded),
                    candidates=list(combined.values()), state="preview" if combined else "draft")
        _save(db, plan)
    return {"plan": plan}


def confirm_plan(root: str | Path, plan_id: str, expected_revision: int,
                 user_confirmed: bool) -> dict[str, Any]:
    if user_confirmed is not True:
        raise ValueError("Explicit user confirmation is required")
    with _db(root) as db:
        plan = _next(_current(db, plan_id, expected_revision))
        if not all(plan["topic"].values()):
            raise ValueError("industry, product, and market are required")
        if not any(item["selected"] for item in plan["candidates"]):
            raise ValueError("At least one selected candidate is required")
        plan["state"] = "confirmed"
        plan["confirmed_at"] = plan["updated_at"]
        _save(db, plan)
    return {"plan": plan}


def confirmed_snapshot(root: str | Path, plan_id: str, revision: int) -> dict[str, Any]:
    with _db(root) as db:
        plan = _load(db, _id(plan_id), _revision(revision))
    if plan["state"] != "confirmed":
        raise ValueError("Requested research plan revision is not confirmed")
    return plan


def fingerprint(plan: dict[str, Any]) -> str:
    if not isinstance(plan, dict):
        raise ValueError("plan must be an object")
    encoded = json.dumps(plan, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()
