from __future__ import annotations

import json
import sqlite3

from su_crawler.assistant_runtime import submit_job
from su_crawler.collection_attention import attention_for_job
from su_crawler.catalog_collection import collect_plan
from su_crawler.models import FetchResult
from su_crawler.storage import Store


def _checkpoint(root, plan_id, key, coverage):
    output = root / "catalog-prices" / plan_id
    store = Store(output)
    try:
        with store.db:
            store.db.execute("INSERT INTO catalog_checkpoints(id,data) VALUES(?,?)",
                             (key, json.dumps({"coverage": coverage, "queue": []})))
    finally:
        store.close()
    return output


def test_live_attention_uses_actual_challenge_and_keeps_other_pages(tmp_path):
    plan_id = "plan-" + "a" * 32
    job = submit_job(tmp_path, "research_plan", {"plan_id": plan_id})
    _checkpoint(tmp_path, plan_id, job["id"], [
        {"url": "https://example.test/challenge#fragment", "status": "blocked",
         "reason": "HTTP 403", "trace": [{"event": "access_challenge"}]},
        {"url": "https://example.test/generic", "status": "blocked", "reason": "HTTP 403"},
        {"url": "https://example.test/login", "status": "needs_auth"},
        {"url": "https://example.test/other", "status": "visited"},
        {"url": "https://example.test/policy", "status": "policy_denied"},
    ])
    data = attention_for_job(tmp_path, job["id"])
    assert data["total"] == 2
    assert {(item["url"], item["category"]) for item in data["items"]} == {
        ("https://example.test/challenge", "browser_handoff"),
        ("https://example.test/login", "authentication_required")}
    assert all(item["job_id"] == job["id"] and item["action"] == "open_browser"
               and item["status"] == "awaiting_user" for item in data["items"])
    assert "trace" not in json.dumps(data)
    assert attention_for_job(tmp_path, job["id"])["items"] == data["items"]


def test_attention_deduplicates_bounds_and_rejects_unsafe_urls(tmp_path):
    plan_id = "plan-" + "b" * 32
    job = submit_job(tmp_path, "research_plan", {"plan_id": plan_id})
    pages = [{"url": f"https://example.test/{i}", "status": "needs_auth"} for i in range(55)]
    pages += [pages[0], {"url": "file:///private", "status": "needs_auth"}]
    _checkpoint(tmp_path, plan_id, job["id"], pages)
    data = attention_for_job(tmp_path, job["id"])
    assert data["total"] == 55 and len(data["items"]) == 50 and data["truncated"]


def test_terminal_attention_filters_resolved_issue(tmp_path, monkeypatch):
    from su_crawler import collection_attention

    plan_id = "plan-" + "c" * 32
    job_id = "c" * 32
    output = _checkpoint(tmp_path, plan_id, job_id, [
        {"url": "https://example.test/solved", "status": "needs_auth"},
        {"url": "https://example.test/pending", "status": "blocked",
         "trace": [{"event": "access_challenge"}]},
    ])
    with sqlite3.connect(output / "prices.sqlite3") as db:
        db.execute("""INSERT INTO collection_issues
            (id,run_id,page_url,section_key,option_key,reason_code,reason,status,
             attempts,next_action,evidence,scope,created_at,updated_at)
             VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            ("issue-1", "run-1", "https://example.test/solved", "", "", "needs_auth",
             "login", "resolved", 1, "provide_authorized_access", "{}", "{}", "now", "now"))
    monkeypatch.setattr(collection_attention, "get_job", lambda *_: {
        "id": job_id, "operation": "research_plan", "status": "succeeded",
        "arguments": {"plan_id": plan_id}, "result": {"run_id": "run-1"}})
    data = attention_for_job(tmp_path, job_id)
    assert data["total"] == 1
    assert data["items"][0]["url"] == "https://example.test/pending"
    assert data["collection_continues"] is False


def test_noncollection_attention_is_empty(tmp_path):
    job = submit_job(tmp_path, "agent", {})
    assert attention_for_job(tmp_path, job["id"])["items"] == []


def test_real_collector_alerts_after_blocked_page_while_next_page_runs(tmp_path):
    plan_id = "plan-" + "d" * 32
    job = submit_job(tmp_path, "research_plan", {"plan_id": plan_id})
    first, second = "https://example.test/challenge", "https://example.test/other"
    snapshot = {
        "id": plan_id, "revision": 1, "state": "confirmed",
        "topic": {"industry": "Retail", "product": "Product", "market": "US"},
        "candidates": [{"url": url, "selected": True} for url in (first, second)],
    }
    seen = []

    def fetch(source, _base, backend):
        if source.location == first:
            return FetchResult(source.id, "blocked", backend, message="access challenge",
                               trace=[{"event": "access_challenge"}])
        if source.location == second and backend == "http":
            live_job = {**job, "status": "running"}
            seen.append(attention_for_job(tmp_path, job["id"], _job=live_job))
            return FetchResult(source.id, "fetched", backend,
                               content=b'<script type="application/ld+json">'
                                       b'{"@type":"Product","name":"Item","offers":'
                                       b'{"@type":"Offer","price":"10","priceCurrency":"USD"}}'
                                       b'</script>', final_url=second)
        raise AssertionError("Unexpected browser fallback")

    result = collect_plan(tmp_path, snapshot,
                          output_dir=tmp_path / "catalog-prices" / plan_id,
                          max_pages=2, checkpoint_id=job["id"], follow_links=False,
                          collector=fetch)
    assert len(seen) == 1
    assert seen[0]["collection_continues"] is True
    assert [(item["url"], item["category"]) for item in seen[0]["items"]] == [
        (first, "browser_handoff")]
    assert {page["url"]: page["status"] for page in result["coverage"]} == {
        first: "blocked", second: "visited"}


def test_batch_attention_combines_completed_and_current_and_filters_resolved(tmp_path, monkeypatch):
    from su_crawler import collection_attention, research_batch

    plan_id = "plan-" + "e" * 32
    job_id = "e" * 32
    completed = research_batch._batch_checkpoint(job_id, 0)
    current = research_batch._batch_checkpoint(job_id, 1)
    output = _checkpoint(tmp_path, plan_id, completed, [
        {"url": "https://example.test/solved", "status": "needs_auth"},
        {"url": "https://example.test/pending", "status": "needs_auth"},
    ])
    _checkpoint(tmp_path, plan_id, current, [
        {"url": "https://example.test/current", "status": "blocked",
         "trace": [{"event": "access_challenge"}]},
    ])
    journal_path = output / "batch-runs" / f"{job_id}.json"
    journal_path.parent.mkdir()
    journal_path.write_text(json.dumps({
        "schema": "source-ledger/research-batch/v1", "job_id": job_id,
        "batch_count": 2, "batches": [{"checkpoint_id": completed, "run_id": "run-1"}],
    }), encoding="utf-8")
    with sqlite3.connect(output / "prices.sqlite3") as db:
        db.execute("""INSERT INTO collection_issues
            (id,run_id,page_url,section_key,option_key,reason_code,reason,status,
             attempts,next_action,evidence,scope,created_at,updated_at)
             VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            ("issue-1", "run-1", "https://example.test/solved", "", "", "needs_auth",
             "login", "resolved", 1, "provide_authorized_access", "{}", "{}", "now", "now"))
    monkeypatch.setattr(collection_attention, "get_job", lambda *_: {
        "id": job_id, "operation": "research_batch", "status": "running",
        "arguments": {"plan_id": plan_id}})
    data = attention_for_job(tmp_path, job_id)
    assert data["collection_continues"] is True
    assert {item["url"] for item in data["items"]} == {
        "https://example.test/pending", "https://example.test/current"}
