"""MCP follow-up: coverage gaps, bounded retries and untrusted browser evidence."""
from __future__ import annotations

from datetime import datetime, timezone, timedelta
import hashlib
import json
from pathlib import Path
from urllib.parse import urlsplit

from . import assistant_runtime as runtime
from .assistant_workspace import workspace_root, _managed_output, relative_file
from .models import Candidate, CollectionConfig, Product, Source, stable_id
from .research_plans import canonical_url, confirmed_snapshot
from .storage import Store, workspace_lock
from .scope_conditions import assess_conditions
from .site_collection import HOST_ADAPTERS

RETRYABLE = {"no_data", "blocked", "failed", "timeout", "tool_unavailable", "budget_exhausted", "unprocessed", "interrupted"}


def completed(root, job_id):
    job = runtime.get_job(root, job_id)
    result = job.get("result")
    if job.get("operation") != "research_plan" or job.get("status") != "succeeded" or not isinstance(result, dict):
        raise ValueError("A completed research-plan job is required")
    output = _managed_output(workspace_root(root), Path(result["output_dir"]), "research output")
    return job, result, output


def research_gaps(root, job_id, *, offset=0, limit=50):
    if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
        raise ValueError("offset must be non-negative")
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 200:
        raise ValueError("limit must be between 1 and 200")
    job, result, output = completed(root, job_id)
    store = Store(output)
    try:
        rows = store.observations(result["run_id"])
    finally:
        store.close()
    items = []
    from .result_view import build_result_view
    for page in result["result"]["coverage"]:
        page_rows = [row for row in rows if row["source_url"] == page["url"]]
        displayed = build_result_view(page_rows)
        unresolved = [row for row in displayed["rows"] if row["status"] != "verified" or not row.get("comparable")]
        if page["status"] == "excluded" or (page["status"] == "visited" and not unresolved):
            continue
        status = page["status"] if page["status"] != "visited" else "needs_review"
        items.append({"url": page["url"], "status": status, "reason": page.get("reason", ""),
                      "retryable": status in RETRYABLE,
                      "next_action": "review_conditions" if status == "needs_review" else
                                     "resolve_access" if status in {"policy_denied", "needs_auth"} else "retry_or_browser_evidence",
                      "review_count": len(unresolved),
                      "unlinked_evidence_count": displayed["unlinked_evidence_count"],
                      "review_reasons": list(dict.fromkeys(row["reason"] for row in unresolved))[:10],
                      "browser_submissions": sum(row.get("extraction_method") == "host_browser_submission" for row in page_rows)})
    return {"job_id": job_id, "total": len(items), "offset": offset, "limit": limit,
            "items": items[offset:offset + limit], "budget": result["result"].get("budget"),
            "note": "Browser submissions remain review-only. A page visit is not proof of every option or contract condition."}


def validate_retry(root, args):
    job, result, _ = completed(root, args["retry_of"])
    for key in ("plan_id", "expected_revision", "plan_fingerprint"):
        if job["arguments"].get(key) != args.get(key):
            raise ValueError("Retry must use the same confirmed plan revision")
    allowed = {page["url"] for page in result["result"]["coverage"] if page["status"] in RETRYABLE}
    if not set(args["retry_urls"]) <= allowed:
        raise ValueError("Retry only unresolved pages reported by this job; review-only prices need evidence, not blind retries")


def _config(output, run_id, root):
    raw = json.loads((output / f"collection-{run_id}.json").read_text(encoding="utf-8"))
    return CollectionConfig(raw["name"], [Product(**p) for p in raw["products"]],
                            [Source(**s) for s in raw["sources"]], str(output), str(workspace_root(root)),
                            demo=raw.get("demo", False), max_run_seconds=raw.get("max_run_seconds", 60))


def submit_browser_evidence(root, job_id, *, url, captured_at, page_text, fields, locator,
                            screenshot_path=None):
    """Validate literal field support; never promote host assertions to verified prices."""
    job, result, output = completed(root, job_id)
    url = canonical_url(url)
    pages = result["result"]["coverage"]
    if url not in {page["url"] for page in pages if page["status"] != "excluded"}:
        raise ValueError("Evidence URL must be an approved page recorded in this job's coverage")
    if not isinstance(page_text, str) or not 1 <= len(page_text) <= 200_000:
        raise ValueError("page_text must contain 1 to 200000 characters of browser-captured text")
    if not isinstance(locator, str) or not 1 <= len(locator.strip()) <= 500:
        raise ValueError("A product/option locator is required")
    if not isinstance(fields, dict) or not 1 <= len(fields) <= 40:
        raise ValueError("Provide 1 to 40 literal fields")
    for key, value in fields.items():
        if not isinstance(key, str) or not 1 <= len(key) <= 80 or not isinstance(value, str) or not 1 <= len(value) <= 2000:
            raise ValueError("Field names and values must be bounded strings; omit unknown values")
        if value not in page_text:
            raise ValueError(f"Field {key} has no exact support in page_text; do not infer or normalize values")
    if not fields.get("name"):
        raise ValueError("A source-supported product name is required")
    try:
        captured = datetime.fromisoformat(captured_at.replace("Z", "+00:00"))
    except (AttributeError, TypeError, ValueError) as exc:
        raise ValueError("captured_at must be an ISO timestamp with timezone") from exc
    if captured.tzinfo is None or captured > datetime.now(timezone.utc) + timedelta(minutes=5):
        raise ValueError("Capture time must have a timezone and must not be in the future")
    screenshot = None
    if screenshot_path is not None:
        path = relative_file(root, screenshot_path, "screenshot_path")
        if path.stat().st_size > 10 * 1024 * 1024:
            raise ValueError("Screenshot exceeds 10 MiB")
        screenshot = path.read_bytes()
        if not screenshot.startswith(b"\x89PNG\r\n\x1a\n"):
            raise ValueError("Screenshot must be a PNG capture")
    snapshot = confirmed_snapshot(root, job["arguments"]["plan_id"], job["arguments"]["expected_revision"])
    payload = {"url": url, "captured_at": captured_at, "page_text": page_text, "fields": fields,
               "locator": locator, "screenshot_sha256": hashlib.sha256(screenshot).hexdigest() if screenshot else None}
    submission_id = "browser_" + stable_id(result["run_id"], payload)
    with workspace_lock(output):
        store = Store(output)
        try:
            existing = store.db.execute("SELECT data FROM browser_submissions WHERE id=?", (submission_id,)).fetchone()
            if existing:
                row = json.loads(existing[0])
            else:
                count = store.db.execute("SELECT count(*) FROM browser_submissions WHERE run_id=?", (result["run_id"],)).fetchone()[0]
                if count >= 200:
                    raise ValueError("Browser submission limit reached for this run")
                artifacts = _managed_output(workspace_root(root), output / "browser-evidence", "browser evidence")
                artifacts.mkdir(parents=True, exist_ok=True)
                evidence_path = artifacts / f"{submission_id}.json"
                content = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
                evidence_path.write_bytes(content)
                proof = {key: {"raw": value, "location": locator, "display_state": "unconfirmed",
                               "proof_kind": "assistant_supplied_text"} for key, value in fields.items()}
                candidate = Candidate(dict(fields), proof, locator, "host_browser_submission",
                                      review_flags=["Browser text and field association supplied by host AI; independent verification pending"],
                                      evidence_mode="document_text")
                candidate.derived_values["scope_assessment"] = assess_conditions(candidate, snapshot.get("conditions", []))
                host = urlsplit(url).hostname
                source = Source("catalog_source_" + stable_id(url), host, "web", url, [], adapter=HOST_ADAPTERS.get(host))
                product = Product("browser_product_" + stable_id(url, fields["name"], locator), fields["name"],
                                  identifiers={"name": fields["name"]}, price_profile="rental" if source.adapter else "unit")
                from .validation import validate
                observation = validate(candidate, product, source, run_id=result["run_id"], task_id=submission_id,
                                       evidence_path=str(evidence_path), evidence_sha256=hashlib.sha256(content).hexdigest(),
                                       collected_at=captured_at, source_url=url)
                row = observation.to_dict()
                row.update(id=submission_id, status="review", verification_level="review", comparable=False,
                           comparison_key=None)
                if screenshot:
                    screen_path = artifacts / f"{submission_id}.png"
                    screen_path.write_bytes(screenshot)
                    row["evidence_artifacts"]["screenshot"] = {"path": str(screen_path), "sha256": payload["screenshot_sha256"]}
                with store.db:
                    store.db.execute("INSERT INTO browser_submissions(id,run_id,data) VALUES(?,?,?)",
                                     (submission_id, result["run_id"], json.dumps(row, ensure_ascii=False)))
            # Regenerate even on an idempotent replay: a prior export may have
            # failed after the SQLite transaction committed.
            from .pipeline import report_for_run
            report_for_run(_config(output, result["run_id"], root), store, result["run_id"])
        finally:
            store.close()
    return {"submission_id": submission_id, "status": "review", "literal_support": "matched",
            "independent_verification": "pending", "observation": row,
            "report_path": result["report_path"]}
