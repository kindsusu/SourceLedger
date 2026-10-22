"""Unattended, model-free collection of an operator-approved URL list file.

The operator's explicit ``--yes`` plus the URL file are the approval. This module
never searches, never calls a model, and never infers missing values.
"""
from __future__ import annotations

from contextlib import redirect_stdout
from pathlib import Path
from typing import Any
import sys
import time

from . import assistant_runtime as runtime
from . import research_plans as plans
from .assistant_workspace import TERMINAL_STATES, workspace_root

DEFAULT_WAIT_SECONDS = 7200.0


def _topic(industry: str, product: str, market: str) -> dict[str, str]:
    topic = {"industry": industry, "product": product, "market": market}
    for key, value in topic.items():
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"--{key} is required")
        topic[key] = value.strip()
    return topic


def _drain_inline(root: Path) -> bool:
    # Collector libraries must not corrupt the single JSON summary on stdout.
    with redirect_stdout(sys.stderr):
        return runtime.run_inline_worker(root)


def wait_for_completion(root: str | Path, job_id: str, *, timeout: float = DEFAULT_WAIT_SECONDS,
                        inline: bool = False, poll_seconds: float = 2.0) -> tuple[dict[str, Any], str]:
    """Block until the job is terminal or ``timeout`` elapses; no 30-second cap.

    With ``inline`` a queued job is executed in this process when no worker owns
    the workspace. Returns the job and ``completed`` or ``timeout``.
    """
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not 0 < timeout <= 86400:
        raise ValueError("timeout must be between 0 and 86400 seconds")
    base = workspace_root(root)
    deadline = time.monotonic() + timeout
    while True:
        job = runtime.get_job(base, job_id)
        if job["status"] in TERMINAL_STATES:
            return job, "completed"
        if inline and job["status"] == "queued" and _drain_inline(base):
            continue
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return job, "timeout"
        time.sleep(min(poll_seconds, remaining))


def summary_for(root: str | Path, job_id: str, *, wait_state: str) -> dict[str, Any]:
    from .research_batch import collection_summary
    value = collection_summary(root, job_id)
    value["wait"] = wait_state
    if wait_state == "timeout":
        value["worker"] = runtime.runtime_status(root)
        value["errors"] = value["errors"] + ["Timed out waiting; the job continues in its worker. "
                                             "Run source-ledger assistant wait again."]
    return value


def exit_code(summary: dict[str, Any]) -> int:
    if summary.get("wait") == "timeout":
        return 3
    return 0 if summary.get("status") == "succeeded" else 1


def collect_url_file(root: str | Path, *, urls_file: str | Path, industry: str, product: str, market: str,
                     approved: bool, plan_id: str | None = None, batch_size: int = 20, max_seconds: float = 600,
                     concurrency: int = 2, browser_concurrency: int = 1, browser_reuse: bool = False,
                     timeout: float = DEFAULT_WAIT_SECONDS, browser_sample_ratio: float = 0.1) -> dict[str, Any]:
    """Create or reuse a plan, import the file, confirm, run one batch job and summarize it."""
    if approved is not True:
        raise ValueError("Refusing to collect without --yes. The URL file and --yes are the operator approval.")
    topic = _topic(industry, product, market)
    parsed = plans.parse_url_file(Path(urls_file).expanduser())
    if parsed["invalid"]:
        first = parsed["invalid"][0]
        raise ValueError(f"URL file has {len(parsed['invalid'])} invalid entries "
                         f"(first: {first['location']}: {first['reason']}); fix the file before collecting")
    urls = parsed["urls"]
    if not urls:
        raise ValueError("URL file contains no URLs")
    base = workspace_root(root)
    from .plan_jobs import confirm_research
    from .research_batch import queue_batch_run
    created = plan_id is None
    if created:
        request = (f"Unattended collection of an operator-approved URL list ({len(urls)} URLs) "
                   f"for {topic['product']} in {topic['market']} ({topic['industry']}).")
        plan = plans.create_plan(base, request)["plan"]
    else:
        plan = plans.get_plan(base, plan_id)["plan"]
    # The operator approves the URL list itself, with no natural-language
    # conditions: every product shown on those pages is in scope.
    reused = (not created and plan["state"] == "confirmed" and plan["topic"] == topic
              and plans.selected_urls(plan) == urls and plan.get("scope_mode") == "approved_pages")
    if not reused:
        changes = {"scope_mode": "approved_pages"}
        if plan["topic"] != topic:
            changes["topic"] = topic
        plan = plans.revise_plan(base, plan["id"], plan["revision"], changes)["plan"]
        plan = plans.import_urls(base, plan["id"], plan["revision"], urls, replace=True)["plan"]
        plan = confirm_research(base, plan_id=plan["id"], expected_revision=plan["revision"],
                                user_confirmed=True)["plan"]
    job = queue_batch_run(base, {"plan_id": plan["id"], "expected_revision": plan["revision"],
                                 "batch_size": batch_size, "max_seconds": max_seconds,
                                 "concurrency": concurrency, "browser_reuse": browser_reuse,
                                 "browser_concurrency": browser_concurrency,
                                 "browser_sample_ratio": browser_sample_ratio})
    _, state = wait_for_completion(base, job["id"], timeout=timeout, inline=True)
    summary = summary_for(base, job["id"], wait_state=state)
    summary["plan"] = {"id": plan["id"], "revision": plan["revision"], "created": created,
                       "reused_confirmation": reused, "url_count": len(urls),
                       "url_fingerprint": plans.url_fingerprint(urls),
                       "duplicates_skipped": len(parsed["duplicates"])}
    return summary
