"""Offline recovery and evidence tests; external models/services are never used."""
import asyncio
import json
from pathlib import Path

import pytest

from su_crawler import assistant_runtime as runtime, catalog_collection, research_plans as plans
from su_crawler.assistant_server import build_assistant_server
from su_crawler.assistant_workspace import execute_job
from su_crawler.catalog_checkpoint import read_checkpoint
from su_crawler.models import FetchResult, utc_now
from su_crawler.plan_jobs import queue_plan_job
from su_crawler.research_followup import research_gaps, submit_browser_evidence
from su_crawler.storage import Store
from test_catalog_collection import plan, html_product


def completed_job(root, monkeypatch, urls):
    p = plans.create_plan(root, "Inspect selected fixture products in Korea")["plan"]
    p = plans.revise_plan(root, p["id"], p["revision"], {
        "topic": {"industry": "retail", "product": "fixtures", "market": "Korea"},
        "added_candidates": [{"url": url, "name": "Fixture"} for url in urls]})["plan"]
    p = plans.confirm_plan(root, p["id"], p["revision"], True)["plan"]
    job = queue_plan_job(root, "research_plan", {"plan_id": p["id"], "expected_revision": p["revision"], "max_pages": len(urls)})
    result = execute_job(root, "research_plan", job["arguments"], job_id=job["id"])
    job["status"] = "succeeded"
    runtime._write_json(runtime._job_path(root, job["id"]), job)
    runtime._write_json(runtime._job_path(root, job["id"]).with_name("result.json"), result)
    return job, result


def test_browser_submission_mcp_is_review_idempotent_and_in_xlsx(tmp_path, monkeypatch):
    url = "https://shop.example/item"
    monkeypatch.setattr(catalog_collection, "collect", lambda s, b, backend: FetchResult(s.id, "fetched", backend, content=b"<html>No metadata</html>", final_url=s.location))
    job, result = completed_job(tmp_path, monkeypatch, [url])
    assert research_gaps(tmp_path, job["id"])["items"][0]["status"] == "no_data"
    server = build_assistant_server(tmp_path)
    payload = dict(job_id=job["id"], url=url, captured_at=utc_now(), page_text="Widget option A 12.30 USD", fields={"name": "Widget", "price": "12.30", "currency": "USD", "option": "option A"}, locator="price card A")
    first = asyncio.run(server.call_tool("submit_browser_evidence", payload))[1]
    second = asyncio.run(server.call_tool("submit_browser_evidence", payload))[1]
    assert first["submission_id"] == second["submission_id"]
    assert first["observation"]["status"] == "review" and not first["observation"]["comparable"]
    assert first["observation"]["amount"] == "12.30"
    presentation = asyncio.run(server.call_tool("get_job_observations", {"job_id": job["id"]}))[1]
    assert presentation["evidence_count"] == 1 and presentation["unlinked_evidence_count"] == 1
    assert all(r["extraction_method"] != "host_browser_submission" for r in presentation["rows"])
    from openpyxl import load_workbook
    wb = load_workbook(result["report_path"], read_only=True, data_only=True)
    try:
        values = [str(v) for ws in wb for row in ws.values for v in row if v is not None]
        assert "12.30" in values and first["submission_id"] in values
    finally:
        wb.close()
    with pytest.raises(ValueError, match="exact support"):
        submit_browser_evidence(tmp_path, **{**payload, "fields": {"name": "Widget", "price": "999"}})
    with pytest.raises(ValueError, match="approved page"):
        submit_browser_evidence(tmp_path, **{**payload, "url": "https://other.example/item"})
    with pytest.raises(ValueError, match="timezone"):
        submit_browser_evidence(tmp_path, **{**payload, "captured_at": "2026-01-01T00:00:00"})
    missing = submit_browser_evidence(tmp_path, **{**payload, "page_text": "Widget price on request", "fields": {"name": "Widget"}})
    assert missing["observation"]["amount"] is None and missing["observation"]["currency"] is None


def test_resume_keeps_completed_pages_and_charges_interrupted_page(tmp_path):
    urls = ["https://shop.example/a", "https://shop.example/b", "https://shop.example/c"]
    calls = []
    def fetch(source, base, backend):
        calls.append(source.location)
        if source.location == urls[1]:
            raise KeyboardInterrupt("simulate hard process death")
        return FetchResult(source.id, "fetched", backend, content=html_product(source.location, price="10", currency="USD"), final_url=source.location)
    output = tmp_path / "out"
    with pytest.raises(KeyboardInterrupt):
        catalog_collection.collect_plan(tmp_path, plan(urls), output_dir=output, max_pages=3, max_seconds=180, collector=fetch, checkpoint_id="run")
    assert read_checkpoint(output, "run")["dispatched"] == 2
    calls.clear()
    result = catalog_collection.collect_plan(tmp_path, plan(urls), output_dir=output, max_pages=3, max_seconds=180, collector=fetch, checkpoint_id="run")
    assert set(calls) == {urls[2]}
    assert result["budget"]["pages_used"] == 3 and result["budget"]["seconds_used"] >= 60
    assert result["status"] == "partial"
    assert any(p["status"] == "interrupted" for p in result["coverage"])
    calls.clear()
    again = catalog_collection.collect_plan(tmp_path, plan(urls), output_dir=output, max_pages=3, max_seconds=180, collector=fetch, checkpoint_id="run")
    assert again == result and not calls
    with pytest.raises(ValueError, match="budget changed"):
        catalog_collection.collect_plan(tmp_path, plan(urls), output_dir=output, max_pages=4, max_seconds=180, collector=fetch, checkpoint_id="run")


def test_retry_only_failed_page_reuses_success_and_rejects_new_url(tmp_path, monkeypatch):
    urls = ["https://shop.example/a", "https://shop.example/b"]
    calls = []
    def fetch(s, b, backend):
        calls.append(s.location)
        return FetchResult(s.id, "fetched", backend, content=html_product("Widget", price="10", currency="USD") if s.location == urls[0] else b"<html>no metadata</html>", final_url=s.location)
    monkeypatch.setattr(catalog_collection, "collect", fetch)
    job, result = completed_job(tmp_path, monkeypatch, urls)
    evidence = submit_browser_evidence(tmp_path, job["id"], url=urls[1], captured_at=utc_now(),
                                      page_text="Widget unavailable", fields={"name": "Widget"}, locator="card")
    args = {"plan_id": job["arguments"]["plan_id"], "expected_revision": job["arguments"]["expected_revision"], "retry_of": job["id"], "retry_urls": [urls[1]], "max_pages": 1}
    with pytest.raises(ValueError, match="unresolved pages"):
        queue_plan_job(tmp_path, "research_plan", {**args, "retry_urls": ["https://shop.example/new"]})
    retry = queue_plan_job(tmp_path, "research_plan", args)
    calls.clear()
    combined = execute_job(tmp_path, "research_plan", retry["arguments"], job_id=retry["id"])
    assert set(calls) == {urls[1]}
    assert {p["url"] for p in combined["result"]["coverage"]} == set(urls)
    store = Store(Path(combined["output_dir"]))
    try:
        rows = store.observations(combined["run_id"])
        assert sum(r["extraction_method"] == "json_ld_product_offer" for r in rows) == 1
        supplements = [r for r in rows if r["extraction_method"] == "host_browser_submission"]
        assert len(supplements) == 1 and supplements[0]["status"] == "review"
        assert supplements[0]["derived_values"]["parent_submission_id"] == evidence["submission_id"]
    finally:
        store.close()


def test_plan_resume_mcp_starts_worker_but_does_not_reset_budget(tmp_path, monkeypatch):
    monkeypatch.setattr(catalog_collection, "collect", lambda s, b, backend: FetchResult(s.id, "failed", backend))
    job, result = completed_job(tmp_path, monkeypatch, ["https://shop.example/item"])
    job["status"] = "interrupted"
    runtime._write_json(runtime._job_path(tmp_path, job["id"]), job)
    launches = []
    monkeypatch.setattr(runtime, "start_worker", lambda root: launches.append(root))
    server = build_assistant_server(tmp_path)
    queued = asyncio.run(server.call_tool("resume_job_execution", {"job_id": job["id"]}))[1]
    assert queued["status"] == "queued" and launches
    assert queued["arguments"] == job["arguments"]
    resumed = execute_job(tmp_path, "research_plan", queued["arguments"], job_id=job["id"])
    assert resumed["run_id"] == result["run_id"]


def test_jetcar_discovery_does_not_report_navigation_as_missing_products():
    from su_crawler.catalog_collection import _links
    url = "https://www.jetcar.kr/sub0201/2159"
    result = FetchResult("s", "fetched", "http", content=b'<a href="/content/provision">Terms</a><a href="/sub0201/2160">Car</a><a href="/bbs/login.php">Login</a>')
    assert _links(result, url, "www.jetcar.kr", set()) == ["https://www.jetcar.kr/sub0201/2160"]


def test_restart_after_ledger_commit_reuses_sealed_run(tmp_path, monkeypatch):
    url = "https://shop.example/a"
    calls = []
    def fetch(s, b, backend):
        calls.append(s.location)
        return FetchResult(s.id, "fetched", backend, content=html_product("Widget", price="10", currency="USD", links='<a href="/b">B</a>'), final_url=s.location)
    execute = catalog_collection.execute
    def interrupted(*args, **kwargs):
        execute(*args, **kwargs)
        raise KeyboardInterrupt("report receipt not yet committed")
    monkeypatch.setattr(catalog_collection, "execute", interrupted)
    kwargs = dict(output_dir=tmp_path / "out", max_pages=1, collector=fetch, checkpoint_id="receipt")
    with pytest.raises(KeyboardInterrupt):
        catalog_collection.collect_plan(tmp_path, plan([url]), **kwargs)
    assert read_checkpoint(tmp_path / "out", "receipt")["collection_finished"]
    monkeypatch.setattr(catalog_collection, "execute", execute)
    calls.clear()
    result = catalog_collection.collect_plan(tmp_path, plan([url]), **kwargs)
    assert not calls and len(result["coverage"]) == 2
    store = Store(tmp_path / "out")
    try:
        assert store.db.execute("SELECT count(*) FROM runs").fetchone()[0] == 1
        assert len(store.observations(result["run_id"])) == 1
    finally:
        store.close()
