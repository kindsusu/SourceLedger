from __future__ import annotations

import json
import sqlite3

import pytest

from su_crawler.assistant_runtime import submit_job
from su_crawler.job_progress import research_progress
from su_crawler.storage import Store


def test_research_progress_reads_checkpoint_counts_without_returning_capture(tmp_path):
    plan_id = "plan-" + "a" * 32
    job = submit_job(tmp_path, "research_plan", {"plan_id": plan_id, "max_pages": 6})
    output = tmp_path / "catalog-prices" / plan_id
    store = Store(output)
    try:
        checkpoint = {
            "elapsed": 12.75, "inflight": "https://example.test/current",
            "queue": [["https://example.test/next", "selected"]],
            "collection_finished": False,
            "coverage": [
                {"status": "visited", "scope_counts": {"matched": 2}},
                {"status": "visited", "scope_counts": {"unknown": 1}},
                {"status": "no_data"}, {"status": "blocked"}, {"status": "excluded"},
            ],
            "captures": {"https://example.test/one": {"html": "SECRET-HTML" * 100_000}},
        }
        with store.db:
            store.db.execute("INSERT INTO catalog_checkpoints(id,data) VALUES(?,?)",
                             (job["id"], json.dumps(checkpoint)))
    finally:
        store.close()
    progress = research_progress(tmp_path, job["id"])
    assert progress["status"] == "queued"
    assert progress["checkpoint_available"] is True
    assert progress["page_counts"] == {
        "queued": 1, "running": 1, "completed": 1, "review": 2,
        "failed": 1, "excluded": 1,
    }
    assert progress["checkpoint_elapsed_seconds"] == 12.8
    assert progress["report_ready"] is False
    assert len(json.dumps(progress)) < 1500
    assert "SECRET-HTML" not in json.dumps(progress)

    with sqlite3.connect(output / "prices.sqlite3") as db:
        db.execute("CREATE TABLE catalog_page_queue (checkpoint_id TEXT, state TEXT)")
        db.executemany("INSERT INTO catalog_page_queue VALUES (?,?)", [
            (job["id"], state) for state in
            ("queued", "leased", "leased", "completed", "review", "failed")
        ])
    parallel = research_progress(tmp_path, job["id"])
    assert parallel["page_counts"] == {
        "queued": 1, "running": 2, "completed": 1, "review": 1,
        "failed": 1, "excluded": 1,
    }


def test_research_progress_terminal_status_does_not_claim_verified_prices(tmp_path, monkeypatch):
    from su_crawler import job_progress

    report = tmp_path / "prices.xlsx"
    report.write_bytes(b"fixture")
    job_id = "b" * 32
    monkeypatch.setattr(job_progress, "get_job", lambda *_: {
        "id": job_id, "operation": "research_plan", "status": "succeeded",
        "attempt": 1, "arguments": {"plan_id": "plan-" + "b" * 32},
        "created_at": "2026-01-01T00:00:00+00:00", "updated_at": "2026-01-01T00:02:00+00:00",
        "result": {"execution_status": "succeeded", "evidence_status": "needs_review",
                   "report_path": str(report),
                   "result": {"coverage": [{"status": "visited"}, {"status": "no_data"}],
                              "html": "SECRET-EVIDENCE" * 10_000}},
    })
    progress = research_progress(tmp_path, job_id)
    assert progress["completed"] and progress["report_ready"]
    assert progress["page_counts"]["completed"] == 1
    assert progress["page_counts"]["review"] == 1
    assert progress["evidence_status"] == "needs_review"
    assert "verified" not in json.dumps(progress)
    assert "SECRET-EVIDENCE" not in json.dumps(progress)
    assert progress["report_resource_uri"] == f"sourceledger://reports/{job_id}"


def test_unknown_job_progress_fails_closed(tmp_path):
    with pytest.raises(FileNotFoundError):
        research_progress(tmp_path, "c" * 32)
