"""One confirmed URL inventory, collected in durable bounded server-side batches."""
from __future__ import annotations

from collections import Counter
from pathlib import Path
from typing import Any
import hashlib
import json
import math
import re
import sqlite3

from . import assistant_runtime as runtime
from . import research_plans as plans
from .assistant_workspace import _managed_output, workspace_root
from .browser_sampling import (COUNTER_KEYS as _SAMPLING_COUNTERS, DEFAULT_BROWSER_SAMPLE_RATIO,
                               BrowserSamplingPolicy, browser_sampling_scope, validate_sample_ratio)
from .models import utc_now
from .research import _atomic_write, _locked


OPERATION = "research_batch"
MAX_TARGETS = plans.MAX_CANDIDATES
_PLAN_ID = re.compile(r"plan-[0-9a-f]{32}\Z")
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")


class BatchStopped(RuntimeError):
    """The worker was asked to stop at a durable batch boundary."""


def _selected(plan: dict) -> list[dict]:
    excluded = set(plan.get("excluded_urls", []))
    chosen: dict[str, dict] = {}
    for item in plan["candidates"]:
        if item["selected"] and item["url"] not in excluded:
            chosen[item["url"]] = item
    if not 1 <= len(chosen) <= MAX_TARGETS:
        raise ValueError(f"Select between 1 and {MAX_TARGETS} approved URLs")
    return list(chosen.values())


def _validate(args: dict, *, prepared: bool = False) -> dict:
    allowed = {"plan_id", "expected_revision", "batch_size", "max_seconds", "concurrency",
               "browser_reuse", "browser_concurrency", "browser_sample_ratio", "retry_of", "retry_urls"}
    if prepared:
        allowed |= {"plan_fingerprint", "url_fingerprint", "_resume", "_attempt"}
    if not isinstance(args, dict) or set(args) - allowed:
        raise ValueError("Invalid batch run arguments")
    plan_id = args.get("plan_id")
    revision = args.get("expected_revision")
    if not isinstance(plan_id, str) or not _PLAN_ID.fullmatch(plan_id):
        raise ValueError("Invalid research plan ID")
    if isinstance(revision, bool) or not isinstance(revision, int) or revision < 1:
        raise ValueError("expected_revision must be a positive integer")
    size = args.get("batch_size", 20)
    seconds = args.get("max_seconds", 600)
    concurrency = args.get("concurrency", 2)
    browser_reuse = args.get("browser_reuse", True)
    browser_concurrency = args.get("browser_concurrency", 1)
    if isinstance(size, bool) or not isinstance(size, int) or not 1 <= size <= 50:
        raise ValueError("batch_size must be an integer from 1 to 50")
    if isinstance(seconds, bool) or not isinstance(seconds, (float, int)) or not math.isfinite(seconds) or not 1 <= seconds <= 600:
        raise ValueError("max_seconds must be between 1 and 600")
    if isinstance(concurrency, bool) or not isinstance(concurrency, int) or not 1 <= concurrency <= 4:
        raise ValueError("concurrency must be an integer from 1 to 4")
    if not isinstance(browser_reuse, bool):
        raise ValueError("browser_reuse must be a boolean")
    if isinstance(browser_concurrency, bool) or not isinstance(browser_concurrency, int) or not 1 <= browser_concurrency <= min(2, concurrency):
        raise ValueError("browser_concurrency must be 1 or 2 and no greater than concurrency")
    clean = {"plan_id": plan_id, "expected_revision": revision, "batch_size": size,
             "max_seconds": seconds, "concurrency": concurrency,
             "browser_reuse": browser_reuse, "browser_concurrency": browser_concurrency,
             "browser_sample_ratio": validate_sample_ratio(args.get("browser_sample_ratio",
                                                                    DEFAULT_BROWSER_SAMPLE_RATIO))}
    if "retry_of" in args or "retry_urls" in args:
        parent, urls = args.get("retry_of"), args.get("retry_urls")
        if not isinstance(parent, str) or not re.fullmatch(r"[0-9a-f]{32}", parent):
            raise ValueError("retry_of must be a batch job ID")
        if not isinstance(urls, list) or not 1 <= len(urls) <= MAX_TARGETS:
            raise ValueError(f"retry_urls must list 1 to {MAX_TARGETS} URLs")
        clean.update(retry_of=parent, retry_urls=list(dict.fromkeys(plans.canonical_url(url) for url in urls)))
    if prepared:
        for key in ("plan_fingerprint", "url_fingerprint"):
            if not isinstance(args.get(key), str) or not _DIGEST.fullmatch(args[key]):
                raise ValueError("A pinned batch scope is required")
            clean[key] = args[key]
    return clean


def _url_fingerprint(selected: list[dict]) -> str:
    encoded = json.dumps([item["url"] for item in selected], ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _output(root: Path, plan_id: str) -> Path:
    return _managed_output(root, root / "catalog-prices" / plan_id, "batch run output")


def _journal_path(root: Path, plan_id: str, job_id: str) -> Path:
    if not isinstance(job_id, str) or not re.fullmatch(r"[0-9a-f]{32}", job_id):
        raise ValueError("Invalid batch job ID")
    return _output(root, plan_id) / "batch-runs" / f"{job_id}.json"


def _batch_checkpoint(job_id: str, index: int) -> str:
    return hashlib.sha256(f"sourceledger-batch-v1:{job_id}:{index}".encode()).hexdigest()[:32]


def queue_batch_run(root: str | Path, args: dict) -> dict:
    clean = _validate(args)
    base = workspace_root(root)
    runtime._root(base)
    target = base / "runtime" / "plan-submit"
    runtime._assert_lock_safe(target)
    with _locked(target):
        plan = plans.confirmed_snapshot(base, clean["plan_id"], clean["expected_revision"])
        current = plans.get_plan(base, clean["plan_id"])["plan"]
        if current["revision"] != clean["expected_revision"]:
            raise ValueError("Research plan changed; review the latest revision before continuing")
        selected = _selected(plan)
        clean["plan_fingerprint"] = plans.fingerprint(plan)
        clean["url_fingerprint"] = _url_fingerprint(selected)
        runtime.runtime_status(base)
        for path in runtime._job_files(base):
            job = runtime._read_json(path)
            if (job and job.get("operation") in {OPERATION, "research_plan"}
                    and job.get("status") in {"queued", "running"}
                    and job.get("arguments", {}).get("plan_id") == clean["plan_id"]):
                if job.get("operation") == OPERATION and job["arguments"] == clean:
                    return job
                raise ValueError("Another collection for this plan is active")
        return runtime.submit_job(base, OPERATION, clean)


def start_batch_run(root: str | Path, args: dict) -> dict:
    job = queue_batch_run(root, args)
    return {"job": job, "worker": runtime.start_worker(root)}


def _initial_journal(job_id: str, clean: dict, selected: list[dict]) -> dict:
    return {"schema": "source-ledger/research-batch/v1", "job_id": job_id,
            "plan_id": clean["plan_id"], "plan_revision": clean["expected_revision"],
            "plan_fingerprint": clean["plan_fingerprint"], "url_fingerprint": clean["url_fingerprint"],
            "target_count": len(selected), "batch_size": clean["batch_size"],
            "batch_count": math.ceil(len(selected) / clean["batch_size"]),
            "max_seconds_per_batch": clean["max_seconds"], "concurrency": clean["concurrency"],
            "browser_reuse": clean["browser_reuse"], "browser_concurrency": clean["browser_concurrency"],
            "browser_sample_ratio": clean["browser_sample_ratio"],
            "started_at": utc_now(), "finished_at": None, "batches": []}


def _summary(journal: dict, *, output: Path) -> dict[str, Any]:
    batches = journal["batches"]
    coverage = Counter()
    observations = Counter()
    timings = Counter()
    browser_counts = Counter()
    peak_browser = 0
    sampling = Counter({key: 0 for key in _SAMPLING_COUNTERS})
    sampling_fallback = False
    coverage_summary = Counter()
    refresh_counts = Counter()
    refresh_modes = Counter()
    refresh_reasons = Counter()
    refresh_changes = []
    refresh_total_changes = 0
    refresh_item_counts = Counter()
    refresh_items = []
    refresh_total_items = 0
    for batch in batches:
        refresh = batch.get("refresh") or {}
        if refresh:
            refresh_counts.update(refresh.get("counts", {}))
            refresh_modes[refresh.get("effective_mode", "unknown")] += 1
            refresh_reasons[refresh.get("reason", "unknown")] += 1
            changes = refresh.get("changes", [])
            refresh_total_changes += len(changes) + refresh.get("changes_truncated", 0)
            refresh_changes.extend(changes[:max(0, 50 - len(refresh_changes))])
            refresh_item_counts.update(refresh.get('item_counts', {}))
            items = refresh.get('item_changes', [])
            refresh_total_items += refresh.get('item_changes_total', len(items) + refresh.get('item_changes_truncated', 0))
            refresh_items.extend(items[:max(0, 100 - len(refresh_items))])
        coverage.update(batch["coverage_counts"])
        observations.update(batch.get("observation_counts", {}))
        if 'coverage_summary' in batch:
            coverage_summary.update(batch['coverage_summary'])
        else:
            counts = batch.get('coverage_counts', {})
            coverage_summary.update({'unknown': counts.get('visited', 0) + counts.get('no_data', 0),
                                     'excluded': counts.get('excluded', 0),
                                     'partial': sum(value for key, value in counts.items()
                                                    if key not in {'visited', 'no_data', 'excluded'})})
        execution = batch.get("execution", {})
        timings.update(execution.get("timings", {}))
        browser_counts.update({key: execution.get(key, 0) for key in
                               ("browser_pages", "browser_launches", "browser_reused_contexts")})
        peak_browser = max(peak_browser, execution.get("peak_browser_concurrency", 0))
        sampling.update({key: execution.get(key, 0) for key in _SAMPLING_COUNTERS})
        sampling_fallback = sampling_fallback or bool(execution.get("jetcar_browser_fallback"))
    complete = len(batches) == journal["batch_count"]
    partial = bool(coverage_summary['partial']) or any(batch["status"] == "partial" for batch in batches)
    return {"status": ("running" if not complete else "partial" if partial else
                       "needs_review" if coverage_summary['unknown'] or any(batch["status"] == "needs_review" for batch in batches) else "completed"),
            "target_count": journal["target_count"], "batch_count": journal["batch_count"],
            "concurrency": journal.get("concurrency", 2),
            "browser_reuse": journal.get("browser_reuse", False),
            "browser_concurrency": journal.get("browser_concurrency", 1),
            "measured_peak_browser_concurrency": peak_browser,
            "browser_counts": dict(browser_counts),
            "jetcar_sampling": {**dict(sampling), "jetcar_browser_fallback": sampling_fallback,
                                "browser_sample_ratio": journal.get("browser_sample_ratio")},
            "timings": {key: round(value, 3) for key, value in timings.items()},
            "completed_batches": len(batches), "coverage_counts": dict(coverage),
            "observation_counts": dict(observations),
            "incomplete_sections": sum(batch.get("incomplete_sections", 0) for batch in batches),
            "unknown_sections": sum(batch.get('unknown_sections', batch.get('coverage_counts', {}).get('visited', 0)
                                               + batch.get('coverage_counts', {}).get('no_data', 0)) for batch in batches),
            "coverage_summary": {key: coverage_summary[key] for key in ('complete', 'partial', 'unknown', 'excluded')},
            "refresh": {"policy": next((batch['refresh']['policy'] for batch in batches if (batch.get('refresh') or {}).get('policy')), None),
                        "effective_mode": next(iter(refresh_modes)) if len(refresh_modes) == 1 else "mixed" if refresh_modes else "unknown",
                        "modes": dict(refresh_modes), "reasons": dict(refresh_reasons),
                        "counts": dict(refresh_counts), "changes": refresh_changes,
                        "changes_truncated": max(0, refresh_total_changes - len(refresh_changes)),
                        "item_counts": dict(refresh_item_counts), "item_changes": refresh_items,
                        "item_changes_total": refresh_total_items,
                        "item_changes_truncated": max(0, refresh_total_items - len(refresh_items)),
                        "scope": "Approved batch URLs only; full details are in each batch report."},
            "pending_targets": max(0, journal["target_count"] - sum(batch["target_count"] for batch in batches)),
            "reports_ready": len(batches), "output_dir": str(output),
            "final_report_path": (journal.get("final_artifacts") or {}).get("report_path"),
            "dashboard_path": (journal.get("final_artifacts") or {}).get("dashboard_path"),
            "journal_path": str(output / "batch-runs" / (journal["job_id"] + ".json")),
            "started_at": journal["started_at"], "finished_at": journal["finished_at"],
            "total_collection_seconds": round(sum(batch["budget_seconds_used"] for batch in batches), 3)}


def read_batch_journal(root: str | Path, plan_id: str, job_id: str) -> dict | None:
    base = workspace_root(root)
    path = _journal_path(base, plan_id, job_id)
    if path.is_symlink() or not path.is_file():
        return None
    data = runtime._read_json(path)
    if not data or data.get("schema") != "source-ledger/research-batch/v1" or data.get("job_id") != job_id:
        raise ValueError("Batch journal is invalid")
    return data


def batch_progress(root: str | Path, job_id: str) -> dict:
    job = runtime.get_job(root, job_id)
    if job["operation"] != OPERATION:
        raise ValueError("Job is not a research batch run")
    args = job["arguments"]
    from .collection_attention import attention_for_job
    attention = attention_for_job(root, job_id)
    journal = read_batch_journal(root, args["plan_id"], job_id)
    if journal is None:
        return {"id": job_id, "status": job["status"], "completed_batches": 0,
                "batch_count": None, "target_count": None, "reports_ready": 0, "attention": attention}
    summary = _summary(journal, output=_output(workspace_root(root), args["plan_id"]))
    current_index = len(journal["batches"]) if job["status"] == "running" and len(journal["batches"]) < journal["batch_count"] else None
    current_pages = None
    if current_index is not None:
        from .job_progress import _checkpoint_summary
        try:
            current_pages = _checkpoint_summary(workspace_root(root), args["plan_id"],
                                                _batch_checkpoint(job_id, current_index))
        except (OSError, ValueError, sqlite3.Error):
            current_pages = None
    return {"id": job_id, "job_status": job["status"], **summary,
            "attention": attention,
            "current_batch_index": current_index, "current_batch_pages": current_pages,
            **({"error": job["error"]} if job.get("error") else {})}


def list_batch_reports(root: str | Path, job_id: str, *, offset: int = 0, limit: int = 50) -> dict:
    job = runtime.get_job(root, job_id)
    if job["operation"] != OPERATION:
        raise ValueError("Job is not a research batch run")
    if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0 or isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 50:
        raise ValueError("offset must be nonnegative and limit must be 1 to 50")
    journal = read_batch_journal(root, job["arguments"]["plan_id"], job_id)
    batches = journal["batches"] if journal else []
    return {"id": job_id, "total": len(batches), "offset": offset, "limit": limit,
            "final_report_path": ((journal or {}).get("final_artifacts") or {}).get("report_path"),
            "dashboard_path": ((journal or {}).get("final_artifacts") or {}).get("dashboard_path"),
            "batches": batches[offset:offset + limit]}


def _observation_counts(output: Path, run_id: str) -> dict[str, int]:
    database = output / "prices.sqlite3"
    if database.is_symlink() or not database.is_file():
        raise ValueError("Batch ledger is missing")
    with sqlite3.connect(f"file:{database.as_posix()}?mode=ro", uri=True, timeout=5) as db:
        total, priced, review, verified, raw_price_present = db.execute("""
            SELECT COUNT(*),
                   COALESCE(SUM(CASE WHEN json_extract(data,'$.amount') IS NOT NULL
                                     AND COALESCE(json_extract(data,'$.currency'),'') <> '' THEN 1 ELSE 0 END),0),
                   COALESCE(SUM(CASE WHEN json_extract(data,'$.status') = 'review' THEN 1 ELSE 0 END),0),
                   COALESCE(SUM(CASE WHEN json_extract(data,'$.status') = 'verified' THEN 1 ELSE 0 END),0),
                   COALESCE(SUM(CASE WHEN COALESCE(json_extract(data,'$.raw_fields.price'),'') <> '' THEN 1 ELSE 0 END),0)
            FROM observations WHERE run_id=?
        """, (run_id,)).fetchone()
        browser_submissions = db.execute("SELECT COUNT(*) FROM browser_submissions WHERE run_id=?", (run_id,)).fetchone()[0]
    return {"total": total, "priced": priced, "review": review, "verified": verified,
            "raw_price_present": raw_price_present, "browser_submissions": browser_submissions}


def execute_batch_run(root: str | Path, args: dict, *, job_id: str) -> dict:
    from .catalog_collection import collect_plan, collection_rules_version

    clean = _validate(args, prepared=True)
    if "retry_of" in clean:
        return execute_batch_retry(root, clean, job_id=job_id)
    base = workspace_root(root)
    plan = plans.confirmed_snapshot(base, clean["plan_id"], clean["expected_revision"])
    selected = _selected(plan)
    if plans.fingerprint(plan) != clean["plan_fingerprint"] or _url_fingerprint(selected) != clean["url_fingerprint"]:
        raise ValueError("Confirmed batch plan fingerprint does not match")
    output = _output(base, clean["plan_id"])
    path = _journal_path(base, clean["plan_id"], job_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.parent.is_symlink():
        raise ValueError("Batch run directory must not be a symbolic link")
    journal = read_batch_journal(base, clean["plan_id"], job_id)
    if journal is None:
        journal = _initial_journal(job_id, clean, selected)
        _atomic_write(path, journal)
    if (journal["plan_fingerprint"] != clean["plan_fingerprint"] or journal["url_fingerprint"] != clean["url_fingerprint"]
            or journal["batch_size"] != clean["batch_size"] or journal["max_seconds_per_batch"] != clean["max_seconds"]
            or journal["concurrency"] != clean["concurrency"]
            or journal.get("browser_reuse", False) != clean["browser_reuse"]
            or journal.get("browser_concurrency", 1) != clean["browser_concurrency"]
            or journal["target_count"] != len(selected)):
        raise ValueError("Batch journal scope or budget changed")
    if len(journal["batches"]) > journal["batch_count"]:
        raise ValueError("Batch journal exceeds approved scope")
    # Journals written before sampling existed record the ratio now in force.
    journal.setdefault("browser_sample_ratio", clean["browser_sample_ratio"])
    rules_version = collection_rules_version()
    if journal.get('rules_version') != rules_version:
        if journal['batches']:
            history = journal.setdefault('prior_results', [])
            history.append({'rules_version': journal.get('rules_version', 'legacy'),
                            'batches': journal['batches'], 'finished_at': journal.get('finished_at')})
            journal['batches'] = []
            journal['finished_at'] = None
            journal.pop('final_artifacts', None)
        journal['rules_version'] = rules_version
        _atomic_write(path, journal)
    from .collectors import collection_job_scope

    # Jetcar browser verification is sampled across the whole job, not per
    # batch. A resumed job keeps a recorded mismatch fallback and samples its
    # remaining pages again; it never assumes an unrecorded prior pass.
    done = len(journal["batches"])
    sampling = BrowserSamplingPolicy(
        clean["browser_sample_ratio"], f"{job_id}:{clean['plan_fingerprint']}",
        [item["url"] for item in selected[done * clean["batch_size"]:]],
        fallback=any((batch.get("execution") or {}).get("jetcar_browser_fallback") for batch in journal["batches"]))
    # One robots cache and pooled HTTP client for the whole job, not per batch.
    with collection_job_scope(), browser_sampling_scope(sampling):
        for index in range(len(journal["batches"]), journal["batch_count"]):
            owner = runtime._read_json(runtime._job_path(base, job_id)) or {}
            token = owner.get("worker_token")
            stop = runtime._read_json(base / "runtime" / "stop.json") or {}
            if token and stop.get("token") == token:
                raise BatchStopped("Worker stopped between batches; resume the same job")
            subset = selected[index * clean["batch_size"]:(index + 1) * clean["batch_size"]]
            snapshot = {**plan, "candidates": subset,
                        "collection_rules": {url: rule for url, rule in plan.get('collection_rules', {}).items()
                                             if url in {item['url'] for item in subset}}}
            checkpoint_id = _batch_checkpoint(job_id, index)
            started_at = utc_now()
            result = collect_plan(base, snapshot, output_dir=output, max_pages=len(subset),
                                  max_seconds=clean["max_seconds"], checkpoint_id=checkpoint_id,
                                  follow_links=False, concurrency=clean["concurrency"],
                                  browser_reuse=clean["browser_reuse"], browser_concurrency=clean["browser_concurrency"],
                                  browser_sample_ratio=clean["browser_sample_ratio"])
            if (result.get("execution") or {}).get("jetcar_browser_fallback"):
                sampling.mark_fallback()  # A replayed finished batch may have recorded it.
            counts = Counter(item.get("status", "unknown") for item in result.get("coverage", []))
            observations = _observation_counts(output, result["run_id"])
            incomplete_sections = sum(1 for page in result.get("coverage", [])
                                      for section in page.get("sections", []) if section.get("status") != "complete")
            from .coverage import summarize
            unknown_sections = sum(1 for page in result.get('coverage', [])
                                   for section in (page.get('sections') or [{'status': 'unknown'}])
                                   if page.get('status') != 'excluded' and section.get('status') == 'unknown')
            journal["batches"].append({"index": index, "target_count": len(subset),
                                       "checkpoint_id": checkpoint_id, "run_id": result["run_id"],
                                       "status": result["status"], "coverage_counts": dict(counts),
                                       "observation_counts": observations,
                                       "incomplete_sections": incomplete_sections,
                                       "unknown_sections": unknown_sections,
                                       "coverage_summary": result.get('coverage_summary', summarize(result.get('coverage', []))),
                                       "budget_seconds_used": round(float(result["budget"]["seconds_used"]), 3),
                                       "execution": result.get("execution", {}),
                                       "refresh": result.get("refresh"),
                                       "started_at": started_at, "finished_at": utc_now(),
                                       "report_path": result["report_path"]})
            _atomic_write(path, journal)
    from .final_report import finalize_reports
    # Only this journal's completed runs belong in the final deliverable. Never
    # glob the output directory: it also holds prior revisions and retries.
    journal.pop('final_artifacts', None)
    _atomic_write(path, journal)
    artifacts = finalize_reports(
        [Path(item['report_path']) for item in journal['batches']],
        output / f"final-{job_id}.xlsx",
        metadata={**_summary(journal, output=output), "topic": plan.get('topic', {}),
                  "finished_at": utc_now()},
    )
    journal['final_artifacts'] = artifacts
    journal["finished_at"] = utc_now()
    _atomic_write(path, journal)
    summary = _summary(journal, output=output)
    return {"operation": OPERATION, "execution_status": "succeeded", "evidence_status": summary["status"],
            "plan_id": clean["plan_id"], "plan_revision": clean["expected_revision"],
            "plan_fingerprint": clean["plan_fingerprint"], "output_dir": str(output),
            "report_path": artifacts["report_path"], "dashboard_path": artifacts["dashboard_path"],
            "result": summary}


# --- Bounded retries of an approved batch inventory ---------------------------------------

def _retry_checkpoint(job_id: str, index: int) -> str:
    return hashlib.sha256(f"sourceledger-batch-retry-v1:{job_id}:{index}".encode()).hexdigest()[:32]


def _parent_batch(root: Path, parent_id: str) -> tuple[dict, dict]:
    parent = runtime.get_job(root, parent_id)
    if parent.get("operation") != OPERATION or parent.get("status") != "succeeded":
        raise ValueError("A succeeded research batch job is required for a batch retry")
    journal = read_batch_journal(root, parent["arguments"]["plan_id"], parent_id)
    if not journal or len(journal["batches"]) != journal["batch_count"] or not journal.get("finished_at"):
        raise ValueError("The parent batch journal is incomplete; resume it instead of retrying")
    return parent, journal


def queue_batch_retry(root: str | Path, parent_id: str, urls: list[str] | None = None,
                      *, max_seconds: float | None = None) -> dict:
    """Queue a retry of retryable pages from a finished batch job under the same pinned scope.

    Without ``urls`` every retryable page reported by the job is selected. The retry
    is itself a ``research_batch`` job whose journal merges retried batches into the
    parent results, so every batch follow-up tool works on it.
    """
    from .research_followup import retryable_batch_urls
    base = workspace_root(root)
    runtime._root(base)
    parent, _ = _parent_batch(base, parent_id)
    allowed = retryable_batch_urls(base, parent_id)
    if urls is None:
        chosen = allowed
        if not chosen:
            raise ValueError("This batch job reports no retryable pages; inspect get_research_gaps instead")
    else:
        if not isinstance(urls, list) or not urls:
            raise ValueError("urls must be a non-empty list, or omitted to retry every retryable page")
        requested = {plans.canonical_url(url) for url in urls}
        if not requested <= set(allowed):
            raise ValueError("Retry only unresolved pages reported by this job; review-only prices need evidence, not blind retries")
        chosen = [url for url in allowed if url in requested]
    args = {key: parent["arguments"][key] for key in ("plan_id", "expected_revision", "batch_size", "max_seconds",
                                                       "concurrency", "browser_reuse", "browser_concurrency",
                                                       "browser_sample_ratio")
            if key in parent["arguments"]}
    if max_seconds is not None:
        args["max_seconds"] = max_seconds
    clean = _validate({**args, "retry_of": parent_id, "retry_urls": chosen})
    clean["plan_fingerprint"] = parent["arguments"]["plan_fingerprint"]
    clean["url_fingerprint"] = parent["arguments"]["url_fingerprint"]
    target = base / "runtime" / "plan-submit"
    runtime._assert_lock_safe(target)
    with _locked(target):
        runtime.runtime_status(base)
        for path in runtime._job_files(base):
            job = runtime._read_json(path)
            if (job and job.get("operation") in {OPERATION, "research_plan"}
                    and job.get("status") in {"queued", "running"}
                    and job.get("arguments", {}).get("plan_id") == clean["plan_id"]):
                if job.get("operation") == OPERATION and job["arguments"] == clean:
                    return job
                raise ValueError("Another collection for this plan is active")
        return runtime.submit_job(base, OPERATION, clean)


def start_batch_retry(root: str | Path, parent_id: str, urls: list[str] | None = None,
                      *, max_seconds: float | None = None) -> dict:
    job = queue_batch_retry(root, parent_id, urls, max_seconds=max_seconds)
    return {"job": job, "retry_url_count": len(job["arguments"]["retry_urls"]), "worker": runtime.start_worker(root)}


def execute_batch_retry(root: str | Path, clean: dict, *, job_id: str) -> dict:
    from .catalog_collection import collect_plan
    from .final_report import finalize_reports
    from .coverage import summarize
    base = workspace_root(root)
    parent, parent_journal = _parent_batch(base, clean["retry_of"])
    pinned = {key: parent["arguments"].get(key) for key in ("plan_id", "expected_revision", "plan_fingerprint",
                                                              "url_fingerprint", "batch_size")}
    if any(clean[key] != value for key, value in pinned.items()):
        raise ValueError("Retry must use the parent's pinned plan revision, URL list and batch size")
    plan = plans.confirmed_snapshot(base, clean["plan_id"], clean["expected_revision"])
    selected = _selected(plan)
    if plans.fingerprint(plan) != clean["plan_fingerprint"] or _url_fingerprint(selected) != clean["url_fingerprint"]:
        raise ValueError("Confirmed batch plan fingerprint does not match")
    position = {item["url"]: index for index, item in enumerate(selected)}
    groups: dict[int, list[str]] = {}
    for url in clean["retry_urls"]:
        if url not in position:
            raise ValueError("Retry URL is outside the approved inventory")
        groups.setdefault(position[url] // clean["batch_size"], []).append(url)
    output = _output(base, clean["plan_id"])
    path = _journal_path(base, clean["plan_id"], job_id)
    journal = read_batch_journal(base, clean["plan_id"], job_id)
    if journal is None:
        journal = {**{key: parent_journal[key] for key in ("schema", "plan_id", "plan_revision", "plan_fingerprint",
                                                            "url_fingerprint", "target_count", "batch_size",
                                                            "batch_count", "concurrency", "browser_reuse",
                                                            "browser_concurrency", "rules_version")
                      if key in parent_journal},
                   "job_id": job_id, "retry_of": clean["retry_of"], "retry_urls": clean["retry_urls"],
                   "max_seconds_per_batch": clean["max_seconds"], "started_at": utc_now(), "finished_at": None,
                   "batches": [{**item, "inherited_from": item.get("inherited_from", clean["retry_of"])}
                               for item in parent_journal["batches"]]}
        _atomic_write(path, journal)
    sampling = BrowserSamplingPolicy(clean["browser_sample_ratio"], f"{job_id}:{clean['plan_fingerprint']}",
                                     clean["retry_urls"])
    for index in sorted(groups):
        if journal["batches"][index].get("retry_job") == job_id:
            continue  # Finished before an interruption; its checkpoint is final.
        parent_entry = parent_journal["batches"][index]
        subset = selected[index * clean["batch_size"]:(index + 1) * clean["batch_size"]]
        snapshot = {**plan, "candidates": subset,
                    "collection_rules": {url: rule for url, rule in plan.get("collection_rules", {}).items()
                                         if url in {item["url"] for item in subset}}}
        checkpoint_id = _retry_checkpoint(job_id, index)
        started_at = utc_now()
        with browser_sampling_scope(sampling):
            result = collect_plan(base, snapshot, output_dir=output, max_pages=len(groups[index]),
                                  max_seconds=clean["max_seconds"], checkpoint_id=checkpoint_id,
                                  retry_urls=groups[index], parent_checkpoint_id=parent_entry["checkpoint_id"],
                                  follow_links=False, concurrency=clean["concurrency"],
                                  browser_reuse=clean["browser_reuse"], browser_concurrency=clean["browser_concurrency"],
                                  browser_sample_ratio=clean["browser_sample_ratio"])
        coverage = result.get("coverage", [])
        journal["batches"][index] = {
            "index": index, "target_count": len(subset), "checkpoint_id": checkpoint_id,
            "run_id": result["run_id"], "status": result["status"],
            "coverage_counts": dict(Counter(item.get("status", "unknown") for item in coverage)),
            "observation_counts": _observation_counts(output, result["run_id"]),
            "incomplete_sections": sum(1 for page in coverage for section in page.get("sections", [])
                                       if section.get("status") != "complete"),
            "unknown_sections": sum(1 for page in coverage for section in (page.get("sections") or [{"status": "unknown"}])
                                    if page.get("status") != "excluded" and section.get("status") == "unknown"),
            "coverage_summary": result.get("coverage_summary", summarize(coverage)),
            "budget_seconds_used": round(float(result["budget"]["seconds_used"]), 3),
            "execution": result.get("execution", {}), "refresh": result.get("refresh"),
            "started_at": started_at, "finished_at": utc_now(), "report_path": result["report_path"],
            "retry_job": job_id, "retry_of_run_id": parent_entry.get("run_id"), "retried_urls": len(groups[index])}
        _atomic_write(path, journal)
    journal.pop("final_artifacts", None)
    artifacts = finalize_reports(
        [Path(item["report_path"]) for item in journal["batches"]], output / f"final-{job_id}.xlsx",
        metadata={**_summary(journal, output=output), "topic": plan.get("topic", {}), "finished_at": utc_now()})
    journal["final_artifacts"] = artifacts
    journal["finished_at"] = utc_now()
    _atomic_write(path, journal)
    summary = _summary(journal, output=output)
    summary.update(retry_of=clean["retry_of"], retry_url_count=len(clean["retry_urls"]),
                   retried_batches=sorted(groups))
    return {"operation": OPERATION, "execution_status": "succeeded", "evidence_status": summary["status"],
            "plan_id": clean["plan_id"], "plan_revision": clean["expected_revision"],
            "plan_fingerprint": clean["plan_fingerprint"], "output_dir": str(output), "retry_of": clean["retry_of"],
            "report_path": artifacts["report_path"], "dashboard_path": artifacts["dashboard_path"], "result": summary}


# --- Compact views for assistant hosts ----------------------------------------------------

_TERMINAL = {"succeeded", "failed", "interrupted"}


def _refresh_counts(refresh: dict | None) -> dict:
    refresh = refresh or {}
    return {"effective_mode": refresh.get("effective_mode"), "counts": refresh.get("counts", {}),
            "item_counts": refresh.get("item_counts", {}),
            "item_changes_total": refresh.get("item_changes_total", 0),
            "page_changes_total": len(refresh.get("changes", [])) + refresh.get("changes_truncated", 0)}


def _compact_attention(attention: dict | None) -> dict:
    attention = attention or {}
    items = attention.get("items", [])
    return {"total": attention.get("total", 0), "truncated": attention.get("truncated", False),
            "collection_continues": attention.get("collection_continues", False),
            "categories": dict(Counter(item.get("category") for item in items)),
            "first": [{"url": item.get("url"), "category": item.get("category")} for item in items[:3]]}


def _compact_change(item: dict) -> dict:
    def offer(value):
        fields = (value or {}).get("fields", {})
        return {key: fields[key] for key in ("price", "currency", "term_months") if key in fields} if value else None
    return {"status": item.get("status"), "url": item.get("url"), "product_name": str(item.get("product_name", ""))[:120],
            "changed_fields": item.get("changed_fields", [])[:8], "before": offer(item.get("before")),
            "after": offer(item.get("after")), "run_id": item.get("run_id")}


def compact_batch_progress(root: str | Path, job_id: str, *, include_changes: bool = False,
                           offset: int = 0, limit: int = 20) -> dict:
    """Small polling view: counts, per-status URL counts, elapsed time and artifact paths.

    Change rows are opt-in and paged from SQLite (``include_changes``); per-URL
    rows are available through get_research_gaps and get_collection_changes.
    """
    if not isinstance(include_changes, bool):
        raise ValueError("include_changes must be a boolean")
    offset, limit = plans._page_bounds(offset, limit)
    if limit > 50:
        raise ValueError("limit must be at most 50")
    job = runtime.get_job(root, job_id)
    if job["operation"] != OPERATION:
        raise ValueError("Job is not a research batch run")
    from .job_progress import _elapsed_seconds
    full = batch_progress(root, job_id)
    args = job["arguments"]
    value = {"id": job_id, "job_status": job["status"], "completed": job["status"] in _TERMINAL,
             "attempt": job.get("attempt"), "updated_at": job.get("updated_at"),
             "elapsed_seconds_since_submission": _elapsed_seconds(job.get("created_at")),
             "plan_id": args["plan_id"], "plan_revision": args["expected_revision"],
             "url_fingerprint": args.get("url_fingerprint")}
    if "retry_of" in args:
        value.update(retry_of=args["retry_of"], retry_url_count=len(args.get("retry_urls", [])))
    for key in ("status", "target_count", "batch_count", "completed_batches", "pending_targets", "reports_ready",
                "coverage_counts", "coverage_summary", "observation_counts", "incomplete_sections",
                "unknown_sections", "total_collection_seconds", "started_at", "finished_at",
                "current_batch_index", "final_report_path", "dashboard_path", "jetcar_sampling"):
        if key in full:
            value[key] = full[key]
    pages = full.get("current_batch_pages")
    if isinstance(pages, dict):
        value["current_batch_pages"] = {key: pages[key] for key in ("queued", "running", "completed", "review",
                                                                    "failed", "excluded") if key in pages}
    if "refresh" in full:
        value["refresh"] = _refresh_counts(full["refresh"])
    value["attention"] = _compact_attention(full.get("attention"))
    if job.get("error"):
        value["error"] = str(job["error"])[:500]
    if include_changes:
        from .collection_changes import list_collection_changes
        changes = list_collection_changes(root, job_id, status="changes", offset=offset, limit=limit)
        value["item_changes"] = {"items": [_compact_change(item) for item in changes["items"]],
                                 "total": changes["total"], "offset": offset, "limit": limit,
                                 "has_more": changes["has_more"]}
    value["next"] = ("Page per-URL status with get_research_gaps and change rows with get_collection_changes "
                     "(or include_changes=true). A succeeded job is not verified evidence.")
    return value


def compact_batch_reports(root: str | Path, job_id: str, *, offset: int = 0, limit: int = 10) -> dict:
    """Final workbook/dashboard paths plus a small page of per-batch report files."""
    full = list_batch_reports(root, job_id, offset=offset, limit=limit)
    job = runtime.get_job(root, job_id)
    output = _output(workspace_root(root), job["arguments"]["plan_id"])

    def name(path):
        candidate = Path(path)
        return candidate.name if candidate.parent == output else str(candidate)
    return {"id": job_id, "output_dir": str(output),
            "final_report_path": full["final_report_path"], "dashboard_path": full["dashboard_path"],
            "total": full["total"], "offset": offset, "limit": limit, "has_more": offset + limit < full["total"],
            "batches": [{"index": item.get("index"), "run_id": item.get("run_id"), "status": item.get("status"),
                         "target_count": item.get("target_count"),
                         "observations": (item.get("observation_counts") or {}).get("total"),
                         "report_file": name(item["report_path"]) if item.get("report_path") else None,
                         **({"retry_job": item["retry_job"]} if item.get("retry_job") else {})}
                        for item in full["batches"]],
            "note": "report_file names are inside output_dir. Use final_report_path for the combined workbook."}


def collection_summary(root: str | Path, job_id: str) -> dict:
    """One compact JSON summary of a plan or batch collection job for unattended runs."""
    from .job_progress import _elapsed_seconds
    from .assistant_workspace import job_result_summary
    job = runtime.get_job(root, job_id)
    args = job.get("arguments") or {}
    result = job.get("result") if isinstance(job.get("result"), dict) else {}
    payload = result.get("result") if isinstance(result.get("result"), dict) else {}
    value = {"job_id": job_id, "operation": job.get("operation"), "status": job["status"],
             "completed": job["status"] in _TERMINAL, "attempt": job.get("attempt"),
             "plan_id": args.get("plan_id"), "plan_revision": args.get("expected_revision"),
             "url_fingerprint": args.get("url_fingerprint"),
             "created_at": job.get("created_at"), "updated_at": job.get("updated_at"),
             "elapsed_seconds_since_submission": _elapsed_seconds(job.get("created_at")),
             "execution_status": result.get("execution_status"), "evidence_status": result.get("evidence_status")}
    if job.get("operation") == OPERATION:
        journal = read_batch_journal(root, args["plan_id"], job_id)
        if journal is not None:
            summary = _summary(journal, output=_output(workspace_root(root), args["plan_id"]))
            for key in ("target_count", "batch_count", "completed_batches", "pending_targets", "coverage_counts",
                        "coverage_summary", "observation_counts", "incomplete_sections", "unknown_sections",
                        "total_collection_seconds", "final_report_path", "dashboard_path", "jetcar_sampling"):
                value[key] = summary[key]
            value["changes"] = _refresh_counts(summary["refresh"])
    else:
        value.update(job_result_summary(payload))
        value.update(final_report_path=result.get("report_path"), dashboard_path=result.get("dashboard_path"),
                     run_id=result.get("run_id"))
        if isinstance(payload.get("refresh"), dict):
            value["changes"] = _refresh_counts(payload["refresh"])
    value["errors"] = [str(job["error"])[:500]] if job.get("error") else []
    value["note"] = "Execution status is not price verification; review evidence_status, gaps and the workbook."
    return value
