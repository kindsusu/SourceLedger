from __future__ import annotations

from contextlib import closing, contextmanager
from http.client import HTTPConnection
import json
import sqlite3
import threading

from su_crawler import collection_attention, web_server
from su_crawler.storage import Store

PLAN_ID = "plan-" + "e" * 32
JOB_ID = "e" * 32
URL = "https://example.test/login"


@contextmanager
def running_server(tmp_path):
    server = web_server.build_web_server(tmp_path, port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def bootstrap(server):
    connection = HTTPConnection("127.0.0.1", server.server_port, timeout=5)
    connection.request("GET", "/api/bootstrap")
    response = connection.getresponse()
    body = json.loads(response.read().decode("utf-8"))
    connection.close()
    assert response.status == 200
    return body


def finished_job(tmp_path, monkeypatch):
    """A finished research_plan job with one auth-required page and one open issue."""
    output = tmp_path / "catalog-prices" / PLAN_ID
    store = Store(output)
    try:
        store.db.execute("PRAGMA foreign_keys=OFF")  # no runs row needed for this fixture
        with store.db:
            store.db.execute("INSERT INTO catalog_checkpoints(id,data) VALUES(?,?)", (
                JOB_ID, json.dumps({"coverage": [{"url": URL, "status": "needs_auth"}], "queue": []})))
            store.db.execute(
                """INSERT INTO collection_issues
                (id,run_id,page_url,section_key,option_key,reason_code,reason,status,
                 attempts,next_action,evidence,scope,created_at,updated_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                ("issue-1", "run-1", URL, "", "", "needs_auth", "login", "open", 1,
                 "provide_authorized_access", "{}", "{}", "now", "now"))
    finally:
        store.close()
    job = {"id": JOB_ID, "operation": "research_plan", "status": "succeeded",
           "updated_at": "2026-01-01T00:00:00Z", "arguments": {"plan_id": PLAN_ID},
           "result": {"run_id": "run-1"}}
    monkeypatch.setattr(web_server, "list_jobs", lambda *_a, **_k: [job])
    monkeypatch.setattr(collection_attention, "get_job", lambda *_a, **_k: job)
    return job, output


def spy_attention(monkeypatch):
    calls = []
    real = collection_attention.attention_for_job

    def spy(*args, **kwargs):
        calls.append(args)
        return real(*args, **kwargs)

    monkeypatch.setattr(collection_attention, "attention_for_job", spy)
    return calls


def test_unchanged_finished_job_computes_attention_once(tmp_path, monkeypatch):
    job, _ = finished_job(tmp_path, monkeypatch)
    calls = spy_attention(monkeypatch)
    with running_server(tmp_path) as server:
        first = bootstrap(server)
        second = bootstrap(server)
        third = bootstrap(server)
    assert len(calls) == 1
    assert first["collection_attention"] == second["collection_attention"] == third["collection_attention"]
    assert first["collection_attention"]["total"] == 1
    assert first["collection_attention"]["items"][0]["url"] == URL
    assert set(first["collection_attention"]) == {"items", "total", "truncated", "collection_continues"}


def test_changed_updated_at_or_status_recomputes_attention(tmp_path, monkeypatch):
    job, _ = finished_job(tmp_path, monkeypatch)
    calls = spy_attention(monkeypatch)
    with running_server(tmp_path) as server:
        bootstrap(server)
        job["updated_at"] = "2026-01-01T00:00:05Z"
        bootstrap(server)
        assert len(calls) == 2
        job["status"] = "running"
        data = bootstrap(server)
        assert len(calls) == 3
        assert data["collection_attention"]["collection_continues"] is True
        bootstrap(server)
        assert len(calls) == 3


def test_issue_resolution_without_job_change_recomputes_attention(tmp_path, monkeypatch):
    job, output = finished_job(tmp_path, monkeypatch)
    calls = spy_attention(monkeypatch)
    with running_server(tmp_path) as server:
        assert bootstrap(server)["collection_attention"]["total"] == 1
        assert bootstrap(server)["collection_attention"]["total"] == 1
        assert len(calls) == 1
        # Triage from another connection/process: the job receipt is untouched.
        # (Store.update_collection_issue needs a revalidated later run; this writes
        # the same resulting row state.)
        with closing(sqlite3.connect(output / "prices.sqlite3")) as db, db:
            db.execute("UPDATE collection_issues SET status='resolved' WHERE id='issue-1'")
        data = bootstrap(server)
        assert len(calls) == 2
        assert data["collection_attention"]["total"] == 0
        bootstrap(server)
        assert len(calls) == 2


def test_attention_cache_is_bounded_and_evicts_oldest(tmp_path, monkeypatch):
    monkeypatch.setattr(collection_attention, "attention_for_job", lambda *_a, **_k: {"items": []})
    with running_server(tmp_path) as server:
        for index in range(web_server.ATTENTION_CACHE_MAX + 10):
            server.attention_for({"id": f"job-{index}", "operation": "agent", "status": "succeeded",
                                  "updated_at": "t"})
        keys = [key[0] for key in server._attention_cache]
        assert len(keys) == web_server.ATTENTION_CACHE_MAX
        assert "job-0" not in keys and f"job-{web_server.ATTENTION_CACHE_MAX + 9}" in keys
