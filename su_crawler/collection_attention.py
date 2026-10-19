"""Small, read-only prompts for human action during collection."""
from __future__ import annotations

import hashlib
from contextlib import closing
import json
from pathlib import Path
import re
import sqlite3

from .assistant_runtime import get_job
from .assistant_workspace import workspace_root
from .research_plans import canonical_url


MAX_ITEMS = 50
_PLAN = re.compile(r"plan-[0-9a-f]{32}\Z")


def _empty(continues: bool = False) -> dict:
    return {"items": [], "total": 0, "truncated": False,
            "collection_continues": continues}


def _category(page: dict) -> str | None:
    status = page.get("status")
    if status == "needs_auth":
        return "authentication_required"
    if status != "blocked":
        return None
    trace = page.get("trace")
    if isinstance(trace, list) and any(
            isinstance(event, dict) and event.get("event") == "access_challenge"
            for event in trace):
        return "browser_handoff"
    # Older checkpoints did not always retain the structured event. Require
    # explicit challenge language; HTTP 403 by itself is insufficient.
    reason = str(page.get("reason") or "").lower()[:500]
    if any(marker in reason for marker in (
            "captcha", "interactive challenge", "access_challenge",
            "access challenge", "browser handoff")):
        return "browser_handoff"
    return None


def _coverage(db: sqlite3.Connection, checkpoint_id: str) -> list[dict]:
    try:
        rows = db.execute("""SELECT value FROM catalog_checkpoints,
                          json_each(data, '$.coverage')
                          WHERE catalog_checkpoints.id=?""", (checkpoint_id,)).fetchall()
    except sqlite3.Error:
        return []
    pages = []
    for (raw,) in rows:
        try:
            page = json.loads(raw)
        except (TypeError, ValueError):
            continue
        if isinstance(page, dict):
            pages.append(page)
    return pages


def _issue_statuses(db: sqlite3.Connection, run_ids: list[str]) -> dict[tuple[str, str], set[str]]:
    if not run_ids:
        return {}
    try:
        rows = db.execute(
            "SELECT page_url, reason_code, status FROM collection_issues "
            f"WHERE run_id IN ({','.join('?' for _ in run_ids)}) "
            "AND reason_code IN ('browser_handoff','needs_auth')", run_ids).fetchall()
    except sqlite3.Error:
        return {}
    result: dict[tuple[str, str], set[str]] = {}
    for url, code, status in rows:
        try:
            url = canonical_url(url)
        except (TypeError, ValueError):
            continue
        category = "authentication_required" if code == "needs_auth" else "browser_handoff"
        result.setdefault((url, category), set()).add(status)
    return result


def attention_for_job(root: str | Path, job_id: str, *, _job: dict | None = None) -> dict:
    """Return bounded action prompts without changing collection or issue state."""
    base = workspace_root(root)
    job = _job if _job is not None else get_job(base, job_id)
    operation = job.get("operation")
    continues = operation in {"research_plan", "research_batch"} and job.get("status") == "running"
    if operation not in {"research_plan", "research_batch"}:
        return _empty()
    args = job.get("arguments") or {}
    plan_id = args.get("plan_id")
    if not isinstance(plan_id, str) or not _PLAN.fullmatch(plan_id):
        return _empty(continues)
    directory = base / "catalog-prices" / plan_id
    database = directory / "prices.sqlite3"
    if (directory.is_symlink() or database.is_symlink() or not database.is_file()
            or not database.resolve().is_relative_to(base)):
        return _empty(continues)

    checkpoints: list[str] = []
    run_ids: list[str] = []
    if operation == "research_plan":
        checkpoints = [job_id]
        result = job.get("result") or {}
        if isinstance(result, dict) and isinstance(result.get("run_id"), str):
            run_ids = [result["run_id"]]
    else:
        try:
            from .research_batch import read_batch_journal, _batch_checkpoint
            journal = read_batch_journal(base, plan_id, job_id)
        except (OSError, ValueError):
            journal = None
        if journal:
            batches = journal.get("batches") or []
            checkpoints = [item["checkpoint_id"] for item in batches
                           if isinstance(item, dict) and isinstance(item.get("checkpoint_id"), str)]
            run_ids = [item["run_id"] for item in batches
                       if isinstance(item, dict) and isinstance(item.get("run_id"), str)]
            if job.get("status") == "running" and len(batches) < journal.get("batch_count", 0):
                checkpoints.append(_batch_checkpoint(job_id, len(batches)))
    try:
        with closing(sqlite3.connect(database.resolve().as_uri() + '?mode=ro', uri=True, timeout=0.2)) as db:
            pages = [page for checkpoint in checkpoints for page in _coverage(db, checkpoint)]
            statuses = _issue_statuses(db, run_ids)
    except (sqlite3.Error, OSError, ValueError):
        return _empty(continues)

    # A terminal single run can predate checkpoint coverage. Its result is
    # already scoped to the job and contains the same immutable page summary.
    if not pages and operation == "research_plan":
        result = job.get("result") or {}
        payload = result.get("result") if isinstance(result, dict) else None
        if isinstance(payload, dict) and isinstance(payload.get("coverage"), list):
            pages = payload["coverage"]

    items: dict[tuple[str, str], dict] = {}
    for page in pages:
        if not isinstance(page, dict):
            continue
        category = _category(page)
        if not category:
            continue
        try:
            url = canonical_url(page.get("url"))
        except (TypeError, ValueError):
            continue
        issue_states = statuses.get((url, category), set())
        if issue_states and issue_states <= {"resolved", "ignored"}:
            continue
        key = (url, category)
        digest = hashlib.sha256(json.dumps([job_id, url, category], separators=(",", ":")).encode()).hexdigest()[:32]
        items[key] = {"id": "attention_" + digest, "job_id": job_id,
                      "url": url, "category": category, "action": "open_browser",
                      "status": "awaiting_user"}
    ordered = list(items.values())
    return {"items": ordered[:MAX_ITEMS], "total": len(ordered),
            "truncated": len(ordered) > MAX_ITEMS,
            "collection_continues": continues}
