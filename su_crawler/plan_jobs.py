"""Preview and collection jobs pinned to user-reviewed research plan revisions."""
from __future__ import annotations

import math
from pathlib import Path
import re
from typing import Any

from . import assistant_runtime as runtime
from . import research_plans as plans
from .ai_providers import AIGenerationError, normalize_ai_settings
from .assistant_workspace import _managed_output, research_path, workspace_guard, workspace_root
from .research import _locked, init_workspace


def _validate(args: dict, operation: str, *, prepared: bool = False) -> dict:
    allowed = {"plan_id", "expected_revision"}
    allowed |= {"provider", "model", "timeout_seconds"} if operation == "plan_preview" else {"max_pages", "max_seconds"}
    if prepared:
        allowed |= {"plan_fingerprint", "_resume", "_attempt"}
    if not isinstance(args, dict) or set(args) - allowed:
        raise ValueError("Invalid research plan job arguments")
    plan_id, revision = args.get("plan_id"), args.get("expected_revision")
    if not isinstance(plan_id, str) or not re.fullmatch(r"plan-[0-9a-f]{32}", plan_id):
        raise ValueError("Invalid research plan ID")
    if isinstance(revision, bool) or not isinstance(revision, int) or revision < 1:
        raise ValueError("expected_revision must be a positive integer")
    clean = {"plan_id": plan_id, "expected_revision": revision}
    if operation == "plan_preview":
        if "provider" not in args:
            raise ValueError("Select an AI provider before generating a preview")
        clean.update(normalize_ai_settings({key: args[key] for key in ("provider", "model", "timeout_seconds") if key in args}))
    else:
        pages, seconds = args.get("max_pages", 10), args.get("max_seconds", 120)
        if isinstance(pages, bool) or not isinstance(pages, int) or not 1 <= pages <= 50:
            raise ValueError("max_pages must be an integer from 1 to 50")
        if isinstance(seconds, bool) or not isinstance(seconds, (int, float)) or not math.isfinite(seconds) or not 1 <= seconds <= 600:
            raise ValueError("max_seconds must be between 1 and 600")
        clean.update(max_pages=pages, max_seconds=seconds)
    if prepared:
        digest = args.get("plan_fingerprint")
        if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise ValueError("A pinned research plan is required")
        clean["plan_fingerprint"] = digest
    return clean


def latest_plan_jobs(root: str | Path) -> list[dict[str, Any]]:
    base = runtime._root(workspace_root(root))
    latest = {}
    for path in runtime._job_files(base):
        job = runtime._read_json(path)
        if not job or job.get("operation") not in {"plan_preview", "research_plan"} or job.get("status") not in runtime.JOB_STATES:
            continue
        args = job.get("arguments", {})
        key = (args.get("plan_id"), job["operation"])
        if not isinstance(key[0], str):
            continue
        if key not in latest or (job["created_at"], job["id"]) > (latest[key]["created_at"], latest[key]["id"]):
            latest[key] = job
    return sorted(latest.values(), key=lambda job: (job["created_at"], job["id"]), reverse=True)[:200]


def confirm_research(root: str | Path, **args) -> dict:
    result = plans.confirm_plan(root, **args)
    # Ask for the first-run topic in the preview; initialize legacy tools only
    # after the user has reviewed it. Later plans have independent topics.
    try:
        with workspace_guard(root) as base:
            path = research_path(base)
            if not path.exists():
                init_workspace(path, **result["plan"]["topic"], locale="en")
        result["legacy_workspace_status"] = "available"
    except (OSError, ValueError, RuntimeError):
        # Confirmation is already durable and plan collection does not depend on
        # research.json. Do not report a failed confirmation or force a stale retry.
        result["legacy_workspace_status"] = "unavailable"
        result["note"] = "Plan confirmed. Advanced workspace setup is unavailable; plan collection can still start."
    return result


def queue_plan_job(root: str | Path, operation: str, args: dict) -> dict:
    if operation not in {"plan_preview", "research_plan"}:
        raise ValueError("Unknown research plan operation")
    clean = _validate(args, operation)
    base = workspace_root(root)
    runtime._root(base)
    target = base / "runtime" / "plan-submit"
    runtime._assert_lock_safe(target)
    with _locked(target):
        plan = plans.get_plan(base, clean["plan_id"])["plan"]
        if plan["revision"] != clean["expected_revision"]:
            raise ValueError("Research plan changed; review the latest revision before continuing")
        if operation == "research_plan" and plan["state"] != "confirmed":
            raise ValueError("Review and confirm the research plan before starting")
        if operation == "research_plan" and sum(item["selected"] for item in plan["candidates"]) > clean["max_pages"]:
            raise ValueError("Selected targets exceed max_pages; increase the page budget or select fewer targets")
        clean["plan_fingerprint"] = plans.fingerprint(plan)
        runtime.runtime_status(base)
        for path in runtime._job_files(base):
            job = runtime._read_json(path)
            if (job and job.get("operation") == operation and job.get("status") in {"queued", "running"}
                    and job.get("arguments", {}).get("plan_id") == clean["plan_id"]
                    and job["arguments"].get("expected_revision") == clean["expected_revision"]):
                return job
        return runtime.submit_job(base, operation, clean)


def start_plan_job(root: str | Path, operation: str, args: dict) -> dict:
    job = queue_plan_job(root, operation, args)
    return {"job": job, "worker": runtime.start_worker(root)}


def execute_plan_job(root: str | Path, operation: str, args: dict) -> dict:
    clean = _validate(args, operation, prepared=True)
    if operation == "plan_preview":
        from .ai_providers import generate_plan_preview
        plan = plans.get_plan(root, clean["plan_id"])["plan"]
        if plans.fingerprint(plan) != clean["plan_fingerprint"]:
            raise AIGenerationError("request_changed", "The research plan changed. Generate a new preview.")
        generated = generate_plan_preview(plan, {k: clean[k] for k in ("provider", "model", "timeout_seconds")})
        preview = {k: generated[k] for k in ("summary", "topic", "categories", "include_terms", "exclude_terms", "candidates", "note")}
        try:
            result = plans.submit_plan_preview(root, clean["plan_id"], clean["expected_revision"], preview)
        except ValueError as exc:
            raise AIGenerationError("result_rejected", "The research plan changed while AI was working. Review your edits and generate a new preview.") from exc
        return {"operation": operation, "execution_status": "succeeded", "evidence_status": "unverified",
                "result": {"plan_id": clean["plan_id"], "revision": result["plan"]["revision"],
                           "candidate_count": len(result["plan"]["candidates"]), "provider": clean["provider"],
                           "requested_model": clean["model"], "actual_model": generated.get("actual_model")}}
    from .catalog_collection import collect_plan
    snapshot = plans.confirmed_snapshot(root, clean["plan_id"], clean["expected_revision"])
    if plans.fingerprint(snapshot) != clean["plan_fingerprint"]:
        raise ValueError("Confirmed research plan fingerprint does not match")
    base = workspace_root(root)
    output = _managed_output(base, base / "catalog-prices" / clean["plan_id"], "research plan output")
    result = collect_plan(base, snapshot, output_dir=output, max_pages=clean["max_pages"], max_seconds=clean["max_seconds"])
    return {"operation": operation, "execution_status": "succeeded", "evidence_status": result.get("status", "needs_review"),
            "plan_id": clean["plan_id"], "plan_revision": clean["expected_revision"],
            "plan_fingerprint": clean["plan_fingerprint"], "run_id": result["run_id"],
            "output_dir": str(output), "report_path": result["report_path"], "result": result}
