"""Opt-in web AI jobs, separate from collection and host-application MCP calls."""
from __future__ import annotations

from pathlib import Path
from typing import Any
import re

from . import assistant_runtime as runtime
from .ai_providers import AIGenerationError, generate_recommendations, normalize_ai_settings
from .assistant_workspace import workspace_root
from .recommendations import (
    get_recommendation_request, request_fingerprint, submit_recommendations,
)
from .research import _locked


def validate_generation_args(args: dict[str, Any], *, prepared: bool = False) -> dict[str, Any]:
    if not isinstance(args, dict):
        raise ValueError("Recommendation job arguments must be an object")
    allowed = {"request_id", "provider", "model", "timeout_seconds"}
    if prepared:
        allowed |= {"request_fingerprint", "_resume", "_attempt"}
    if set(args) - allowed:
        raise ValueError("Unknown recommendation job arguments")
    request_id = args.get("request_id")
    if not isinstance(request_id, str) or not re.fullmatch(r"request-[a-f0-9]{32}", request_id):
        raise ValueError("Invalid recommendation request ID")
    # A direct model invocation must name its provider explicitly.
    if "provider" not in args:
        raise ValueError("Select a web AI provider before running recommendations")
    settings = normalize_ai_settings({key: args[key] for key in ("provider", "model", "timeout_seconds") if key in args})
    clean = {"request_id": request_id, **settings}
    if prepared:
        fingerprint = args.get("request_fingerprint")
        if not isinstance(fingerprint, str) or not re.fullmatch(r"[a-f0-9]{64}", fingerprint):
            raise ValueError("Recommendation job needs a pinned request")
        clean["request_fingerprint"] = fingerprint
    return clean


def prepare_generation_args(root: str | Path, args: dict[str, Any]) -> dict[str, Any]:
    clean = validate_generation_args(args)
    request = get_recommendation_request(root, clean["request_id"])
    return {**clean, "request_fingerprint": request_fingerprint(request)}


def latest_recommendation_jobs(root: str | Path) -> list[dict[str, Any]]:
    """Keep the latest execution visible even when other job types fill Runs."""
    base = runtime._root(workspace_root(root))
    latest: dict[str, dict[str, Any]] = {}
    for path in runtime._job_files(base):
        job = runtime._read_json(path)
        if not job or job.get("operation") != "recommend" or job.get("status") not in runtime.JOB_STATES:
            continue
        arguments = job.get("arguments")
        if not isinstance(arguments, dict):
            continue
        request_id = arguments.get("request_id")
        if not isinstance(request_id, str):
            continue
        previous = latest.get(request_id)
        order = (str(job.get("created_at", "")), str(job.get("id", "")))
        if previous is None or order > (str(previous.get("created_at", "")), str(previous.get("id", ""))):
            latest[request_id] = job
    return sorted(latest.values(), key=lambda item: (item["created_at"], item["id"]), reverse=True)[:100]


def queue_generation(root: str | Path, args: dict[str, Any]) -> dict[str, Any]:
    """Serialize duplicate clicks across browser tabs without holding the research lock."""
    base = workspace_root(root)
    runtime._root(base)
    target = base / "runtime" / "recommend-submit"
    target.parent.mkdir(parents=True, exist_ok=True)
    runtime._assert_lock_safe(target)
    with _locked(target):
        clean = prepare_generation_args(base, args)
        runtime.runtime_status(base)
        for path in runtime._job_files(base):
            job = runtime._read_json(path)
            if (job and job.get("operation") == "recommend" and job.get("status") in {"queued", "running"}
                    and isinstance(job.get("arguments"), dict)
                    and job["arguments"].get("request_id") == clean["request_id"]):
                return job
        return runtime.submit_job(base, "recommend", clean)


def execute_generation(root: str | Path, args: dict[str, Any]) -> dict[str, Any]:
    clean = validate_generation_args(args, prepared=True)
    try:
        request = get_recommendation_request(root, clean["request_id"])
        if request_fingerprint(request) != clean["request_fingerprint"]:
            raise ValueError("Recommendation request changed")
    except ValueError as exc:
        raise AIGenerationError("request_changed", "The request or research topic changed. Create a new recommendation request.") from exc
    settings = {key: clean[key] for key in ("provider", "model", "timeout_seconds")}
    # No workspace lock is held while the provider runs. UI polling and candidate
    # review remain available; submission checks the original topic atomically.
    generated = generate_recommendations(request, settings)
    try:
        staged = submit_recommendations(
            root, request_id=clean["request_id"], candidates=generated["candidates"],
            note=generated["note"], expected_fingerprint=clean["request_fingerprint"],
        )
    except ValueError as exc:
        raise AIGenerationError("result_rejected", "The result could not be staged. Check that the topic is unchanged and the request has capacity, then retry.") from exc
    returned_urls = {candidate["url"] for candidate in generated["candidates"]}
    return {
        "operation": "recommend", "execution_status": "succeeded", "evidence_status": "unverified",
        "result": {"request_id": clean["request_id"], "status": staged["request"]["status"],
                   "candidate_count": len(staged["request"]["candidates"]),
                   "candidate_ids": [candidate["id"] for candidate in staged["request"]["candidates"]
                                     if candidate["url"] in returned_urls],
                   "provider": clean["provider"], "requested_model": clean["model"],
                   "actual_model": generated.get("actual_model")},
    }
