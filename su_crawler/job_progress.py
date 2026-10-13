"""Small, evidence-neutral progress views for durable assistant jobs."""
from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
import sqlite3
from typing import Any

from .assistant_runtime import get_job
from .assistant_workspace import workspace_root


_FAILED = {"blocked", "policy_denied", "needs_auth", "failed", "timeout",
           "tool_unavailable", "budget_exhausted", "interrupted"}
_TERMINAL = {"succeeded", "failed", "interrupted"}


def _elapsed_seconds(created_at: Any) -> float | None:
    if not isinstance(created_at, str):
        return None
    try:
        created = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
        if created.tzinfo is None:
            return None
        return round(max(0.0, (datetime.now(timezone.utc) - created).total_seconds()), 1)
    except ValueError:
        return None


def _coverage_counts(coverage: list[dict[str, Any]]) -> dict[str, int]:
    counts = Counter()
    for page in coverage:
        if not isinstance(page, dict):
            continue
        status = page.get("status")
        if status == "excluded":
            counts["excluded"] += 1
        elif status == "unprocessed":
            counts["queued"] += 1
        elif status in _FAILED:
            counts["failed"] += 1
        elif status == "no_data" or (status == "visited" and (
                any(page.get("scope_counts", {}).get(key, 0) for key in ("unknown", "not_checked"))
                or any(item.get("status") != "complete" for item in page.get("sections", [])))):
            counts["review"] += 1
        elif status == "visited":
            counts["completed"] += 1
    return {key: counts[key] for key in ("queued", "completed", "review", "failed", "excluded")}


def _checkpoint_summary(root: Path, plan_id: str, job_id: str) -> dict[str, Any] | None:
    if not isinstance(plan_id, str) or not plan_id.startswith("plan-") or len(plan_id) != 37:
        return None
    directory = root / "catalog-prices" / plan_id
    database = directory / "prices.sqlite3"
    if directory.is_symlink() or database.is_symlink() or not database.is_file() or not database.resolve().is_relative_to(root):
        return None
    # JSON1 reads scalar metadata directly from SQLite. Binary capture blobs are
    # never decoded, and the raw checkpoint JSON is never returned to MCP clients.
    with sqlite3.connect(f"file:{database.as_posix()}?mode=ro", uri=True, timeout=2) as db:
        row = db.execute("""
            SELECT json_extract(data, '$.elapsed'), json_extract(data, '$.inflight'),
                   json_array_length(data, '$.queue'), json_extract(data, '$.collection_finished')
            FROM catalog_checkpoints WHERE id=?
        """, (job_id,)).fetchone()
        if row is None:
            return None
        pages = db.execute("""
            SELECT json_extract(value, '$.status'), COUNT(*) FROM catalog_checkpoints,
                   json_each(data, '$.coverage') WHERE catalog_checkpoints.id=? GROUP BY 1
        """, (job_id,)).fetchall()
        review_detail = db.execute("""
            SELECT COUNT(*) FROM catalog_checkpoints, json_each(data, '$.coverage')
            WHERE catalog_checkpoints.id=? AND json_extract(value, '$.status')='visited'
              AND (COALESCE(json_extract(value, '$.scope_counts.unknown'),0)>0
                   OR COALESCE(json_extract(value, '$.scope_counts.not_checked'),0)>0
                   OR EXISTS (SELECT 1 FROM json_each(value, '$.sections')
                              WHERE json_extract(value, '$.status')!='complete'))
        """, (job_id,)).fetchone()[0]
        queue_states = []
        if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='catalog_page_queue'").fetchone():
            queue_states = db.execute("""
                SELECT state, COUNT(*) FROM catalog_page_queue
                WHERE checkpoint_id=? GROUP BY state
            """, (job_id,)).fetchall()
    statuses = Counter({status: count for status, count in pages if status})
    failed = sum(statuses[status] for status in _FAILED)
    review = statuses["no_data"] + review_detail
    summary = {
        "queued": row[2] or 0,
        "running": 1 if row[1] else 0,
        "completed": max(0, statuses["visited"] - review_detail),
        "review": review,
        "failed": failed,
        "excluded": statuses["excluded"],
        "checkpoint_elapsed_seconds": round(float(row[0] or 0), 1),
        "collection_finished": bool(row[3]),
    }
    if queue_states:
        # Parallel runs write per-URL lease transitions. This table reflects
        # current work more precisely than the periodically saved checkpoint.
        states = Counter({state: count for state, count in queue_states})
        summary.update(queued=states["queued"], running=states["leased"],
                       completed=states["completed"], review=states["review"],
                       failed=states["failed"] + states["interrupted"])
    return summary


def research_progress(root: str | Path, job_id: str) -> dict[str, Any]:
    """Return a bounded summary; never interpret execution as price verification."""
    base = workspace_root(root)
    job = get_job(base, job_id)  # Runtime validates job IDs and workspace scope.
    if job.get("operation") == "research_batch":
        from .research_batch import batch_progress
        return batch_progress(base, job_id)
    status = job["status"]
    result = job.get("result") if isinstance(job.get("result"), dict) else {}
    payload = result.get("result") if isinstance(result.get("result"), dict) else {}
    counts = None
    checkpoint = None
    if job.get("operation") == "research_plan":
        plan_id = job.get("arguments", {}).get("plan_id", "")
        try:
            checkpoint = _checkpoint_summary(base, plan_id, job_id)
        except (sqlite3.Error, OSError, ValueError):
            checkpoint = None
        if checkpoint is not None:
            counts = {key: checkpoint[key] for key in ("queued", "running", "completed", "review", "failed", "excluded")}
        elif isinstance(payload.get("coverage"), list):
            counts = {"running": 0, **_coverage_counts(payload["coverage"])}
    report_path = result.get("report_path")
    report_ready = False
    if isinstance(report_path, str):
        path = Path(report_path)
        report_ready = (path.is_file() and not path.is_symlink() and path.resolve().is_relative_to(base))
    output = {
        "id": job["id"], "operation": job.get("operation"), "status": status,
        "completed": status in _TERMINAL, "attempt": job.get("attempt"),
        "updated_at": job.get("updated_at"),
        "elapsed_seconds_since_submission": _elapsed_seconds(job.get("created_at")),
        "page_counts": counts, "checkpoint_available": checkpoint is not None,
        "execution_status": result.get("execution_status"),
        "evidence_status": result.get("evidence_status"),
        "report_ready": report_ready,
        "report_path": str(Path(report_path).resolve()) if report_ready else None,
        "report_resource_uri": f"sourceledger://reports/{job_id}" if report_ready else None,
    }
    if checkpoint is not None:
        output["checkpoint_elapsed_seconds"] = checkpoint["checkpoint_elapsed_seconds"]
        output["collection_finished"] = checkpoint["collection_finished"]
    if job.get("error"):
        output["error"] = str(job["error"])[:500]
    return output
