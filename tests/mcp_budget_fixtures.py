"""Synthetic, offline batch-job fixtures for MCP response-size budgets.

Every host, product and price below is invented. Nothing here opens the web.
"""
from __future__ import annotations

from pathlib import Path
import math

from su_crawler import assistant_runtime as runtime, research_batch as batch, research_plans as plans

HOSTS = ("alpha-rent.example", "beta-lease.example", "gamma-cars.example", "delta-mobility.example")


def synthetic_urls(count: int) -> list[str]:
    return [f"https://{HOSTS[index % len(HOSTS)]}/catalog/sub{index:04d}?page={index % 7}" for index in range(count)]


def confirmed_plan(root: Path, count: int) -> dict:
    created = plans.create_plan(root, "Synthetic rental price inventory for budget tests")["plan"]
    revised = plans.revise_plan(root, created["id"], created["revision"], {
        "topic": {"industry": "Vehicle rental", "product": "Synthetic cars", "market": "Testland"},
        "summary": "Synthetic approved inventory.",
        "added_candidates": [{"url": url, "name": f"Synthetic page {index}",
                              "reason": "Operator-approved synthetic page for response budget tests"}
                             for index, url in enumerate(synthetic_urls(count))],
    })["plan"]
    return plans.confirm_plan(root, revised["id"], revised["revision"], True)["plan"]


def _item(index: int, price: int) -> dict:
    return {"fields": {"name": f"Synthetic Car {index} Premium Trim", "price": str(price), "currency": "KRW",
                       "term_months": "48", "deposit_percent": "30", "annual_mileage_km": "20000",
                       "sku": f"SYN-{index:05d}-48", "price_basis": "monthly"},
            "specs": {"fuel": "gasoline", "seats": "5"}}


def _refresh(batch_index: int, size: int) -> dict:
    changes = [{"url": f"https://{HOSTS[i % 4]}/catalog/sub{batch_index * size + i:04d}",
                "status": "changed", "before_count": 40, "after_count": 41,
                "reason": "observed_price_or_currency_changed"} for i in range(size)]
    items = []
    for i in range(100):
        number = batch_index * 1000 + i
        items.append({"status": "price_changed", "url": changes[i % size]["url"],
                      "identity": f"sku:SYN-{number:05d}-48", "product_name": f"Synthetic Car {number} Premium Trim",
                      "before": _item(number, 400000 + i), "after": _item(number, 410000 + i),
                      "changed_fields": ["price"], "reason": "observed_price_or_currency_changed",
                      "condition_comparison": "observed_fields_only",
                      "previous_checked_at": "2026-01-01T00:00:00+00:00",
                      "checked_at": "2026-01-08T00:00:00+00:00"})
    return {"policy": {"mode": "full", "full_interval_days": 7}, "effective_mode": "full",
            "reason": "full_policy", "checked_at": "2026-01-08T00:00:00+00:00",
            "last_full_at": "2026-01-08T00:00:00+00:00", "next_full_due_at": "2026-01-15T00:00:00+00:00",
            "full_recheck_succeeded": True, "approved_urls_rechecked": True,
            "inventory_completeness_verified": False,
            "counts": {"changed": size, "unchanged": 0},
            "changes": changes, "item_counts": {"price_changed": 300, "unchanged": 400},
            "item_changes": items, "item_changes_total": 700, "item_changes_truncated": 600,
            "changes_truncated": 0, "discovered_links": [],
            "scope": "Approved and visited URLs only; no whole-site completeness claim."}


def synthetic_batch_job(root: Path, count: int = 525, *, batch_size: int = 20, finished: bool = True) -> dict:
    """Write a confirmed plan, a pinned batch job, its journal and result receipt."""
    plan = confirmed_plan(root, count)
    job = batch.queue_batch_run(root, {"plan_id": plan["id"], "expected_revision": plan["revision"],
                                       "batch_size": batch_size, "max_seconds": 600, "concurrency": 2})
    selected = batch._selected(plan)
    journal = batch._initial_journal(job["id"], job["arguments"], selected)
    output = batch._output(runtime._root(root), plan["id"])
    total_batches = math.ceil(count / batch_size)
    for index in range(total_batches):
        size = len(selected[index * batch_size:(index + 1) * batch_size])
        journal["batches"].append({
            "index": index, "target_count": size, "checkpoint_id": batch._batch_checkpoint(job["id"], index),
            "run_id": f"run-{index:04d}-{'f' * 24}", "status": "completed",
            "coverage_counts": {"visited": size - 2, "no_data": 1, "failed": 1},
            "observation_counts": {"total": 44, "priced": 40, "review": 44, "verified": 0,
                                   "raw_price_present": 41, "browser_submissions": 0},
            "incomplete_sections": 3, "unknown_sections": 1,
            "coverage_summary": {"complete": size - 3, "partial": 1, "unknown": 1, "excluded": 1},
            "budget_seconds_used": 98.5,
            "execution": {"timings": {"fetch": 70.1, "extract": 12.2, "browser_wait": 30.4},
                          "browser_pages": 4, "browser_launches": 1, "browser_reused_contexts": 0,
                          "peak_browser_concurrency": 1},
            "refresh": _refresh(index, size),
            "started_at": "2026-01-08T00:00:00+00:00", "finished_at": "2026-01-08T00:01:40+00:00",
            "report_path": str(output / f"report-run-{index:04d}.xlsx")})
    if finished:
        journal["final_artifacts"] = {"report_path": str(output / f"final-{job['id']}.xlsx"),
                                      "dashboard_path": str(output / f"final-{job['id']}.html")}
        journal["finished_at"] = "2026-01-08T00:45:00+00:00"
    path = batch._journal_path(runtime._root(root), plan["id"], job["id"])
    path.parent.mkdir(parents=True, exist_ok=True)
    batch._atomic_write(path, journal)
    record = runtime._read_json(runtime._job_path(runtime._root(root), job["id"]))
    record.update(status="succeeded" if finished else "running", attempt=1)
    runtime._write_json(runtime._job_path(runtime._root(root), job["id"]), record)
    if finished:
        summary = batch._summary(journal, output=output)
        runtime._write_json(runtime._job_path(runtime._root(root), job["id"]).with_name("result.json"), {
            "operation": "research_batch", "execution_status": "succeeded", "evidence_status": summary["status"],
            "plan_id": plan["id"], "plan_revision": plan["revision"], "plan_fingerprint": job["arguments"]["plan_fingerprint"],
            "output_dir": str(output), "report_path": journal["final_artifacts"]["report_path"],
            "dashboard_path": journal["final_artifacts"]["dashboard_path"], "result": summary})
    return {"plan": plan, "job": record, "output": output, "journal": journal}
