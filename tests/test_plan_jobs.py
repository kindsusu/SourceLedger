"""Plan revision boundaries across HTTP, MCP, the durable worker and price reports."""
import asyncio
from pathlib import Path

import pytest

from su_crawler import plan_jobs as jobs, research_plans as plans
from su_crawler.assistant_workspace import execute_job, result_observations, result_report
from su_crawler.models import FetchResult
from test_web_server import running_server, request, authorized, json_response


def preview():
    return {"summary": "Selected shop's cotton clothing, exclude used items.",
            "topic": {"industry": "Clothing", "product": "Cotton clothing", "market": "Japan"},
            "categories": ["Shirts"], "include_terms": [], "exclude_terms": ["used"],
            "candidates": [{"name": "Shop", "url": "https://shop.example/shirt", "evidence_url": "https://shop.example/shirt",
                            "reason": "Referenced product", "kind": "product"}], "note": "Unverified reference"}


def ready(root):
    plan = plans.create_plan(root, "Cotton clothing in Japan.\r\nOnly this shop; exclude used items.")["plan"]
    return plans.submit_plan_preview(root, plan["id"], plan["revision"], preview())["plan"]


def test_confirmation_stale_revision_and_duplicate_start(tmp_path):
    plan = ready(tmp_path)
    args = {"plan_id": plan["id"], "expected_revision": plan["revision"]}
    with pytest.raises(ValueError, match="confirm"):
        jobs.queue_plan_job(tmp_path, "research_plan", args)
    with pytest.raises(ValueError, match="confirmation"):
        jobs.confirm_research(tmp_path, **args, user_confirmed=False)
    approved = jobs.confirm_research(tmp_path, **args, user_confirmed=True)["plan"]
    assert (tmp_path / "research.json").is_file()
    with pytest.raises(ValueError, match="changed"):
        jobs.queue_plan_job(tmp_path, "research_plan", args)
    args["expected_revision"] = approved["revision"]
    first = jobs.queue_plan_job(tmp_path, "research_plan", args)
    assert jobs.queue_plan_job(tmp_path, "research_plan", args)["id"] == first["id"]
    assert first["arguments"]["plan_fingerprint"] == plans.fingerprint(approved)


def test_confirm_survives_optional_legacy_setup_failure(tmp_path, monkeypatch):
    plan = ready(tmp_path)
    def fail(*args, **kwargs):
        raise OSError("fixture failure")
    monkeypatch.setattr(jobs, "init_workspace", fail)
    result = jobs.confirm_research(tmp_path, plan_id=plan["id"], expected_revision=plan["revision"], user_confirmed=True)
    assert result["plan"]["state"] == "confirmed"
    assert result["legacy_workspace_status"] == "unavailable"
    assert jobs.queue_plan_job(tmp_path, "research_plan", {"plan_id": plan["id"], "expected_revision": result["plan"]["revision"]})["status"] == "queued"


def test_reject_selected_targets_over_budget_before_worker(tmp_path):
    plan = ready(tmp_path)
    plan = plans.revise_plan(tmp_path, plan["id"], plan["revision"], {"added_candidates": [{"url": "https://second.example/item"}]})["plan"]
    plan = jobs.confirm_research(tmp_path, plan_id=plan["id"], expected_revision=plan["revision"], user_confirmed=True)["plan"]
    with pytest.raises(ValueError, match="exceed max_pages"):
        jobs.queue_plan_job(tmp_path, "research_plan", {"plan_id": plan["id"], "expected_revision": plan["revision"], "max_pages": 1})
    assert jobs.runtime.list_jobs(tmp_path) == []


def test_worker_uses_confirmed_snapshot_after_edits_and_exposes_report(tmp_path, monkeypatch):
    plan = ready(tmp_path)
    plan = jobs.confirm_research(tmp_path, plan_id=plan["id"], expected_revision=plan["revision"], user_confirmed=True)["plan"]
    job = jobs.queue_plan_job(tmp_path, "research_plan", {"plan_id": plan["id"], "expected_revision": plan["revision"], "max_pages": 1})
    plans.revise_plan(tmp_path, plan["id"], plan["revision"], {"removed_candidate_ids": [plan["candidates"][0]["id"]]})
    calls = []
    def fetch(source, base_dir, backend):
        calls.append(source.location)
        content = b'<script type="application/ld+json">{"@type":"Product","name":"Cotton shirt","offers":{"@type":"Offer","price":"19.00","priceCurrency":"JPY"}}</script>'
        return FetchResult(source.id, "fetched", backend, content=content, final_url=source.location)
    monkeypatch.setattr("su_crawler.catalog_collection.collect", fetch)
    result = execute_job(tmp_path, "research_plan", job["arguments"], job_id=job["id"])
    assert result["plan_revision"] == plan["revision"]
    assert set(calls) == {"https://shop.example/shirt"}
    observations = result_observations(tmp_path, {"result": result})
    assert observations["total"] == 1
    assert observations["rows"][0]["raw_fields"]["name"] == "Cotton shirt"
    assert observations["rows"][0]["status"] == "review"
    assert Path(result_report(tmp_path, {"result": result})["path"]).is_file()


def test_inflight_preview_cannot_overwrite_edits(tmp_path, monkeypatch):
    from su_crawler import ai_providers
    plan = plans.create_plan(tmp_path, "Detailed request\nOnly these products")["plan"]
    job = jobs.queue_plan_job(tmp_path, "plan_preview", {"plan_id": plan["id"], "expected_revision": plan["revision"], "provider": "codex"})
    def generate(document, settings):
        assert document["request_text"] == "Detailed request\nOnly these products"
        plans.revise_plan(tmp_path, plan["id"], plan["revision"], {"exclude_terms": ["used"]})
        return {**preview(), "actual_model": None}
    monkeypatch.setattr(ai_providers, "generate_plan_preview", generate)
    with pytest.raises(ai_providers.AIGenerationError, match="changed"):
        jobs.execute_plan_job(tmp_path, "plan_preview", job["arguments"])
    assert plans.get_plan(tmp_path, plan["id"])["plan"]["candidates"] == []


def test_http_plan_without_onboarding_then_explicit_start(tmp_path, monkeypatch):
    monkeypatch.setattr(jobs.runtime, "start_worker", lambda root: {"status": "idle"})
    with running_server(tmp_path) as server:
        status, boot = json_response(request(server, "GET", "/api/bootstrap"))
        assert status == 200 and boot["research_plans"] == {"plans": []}
        status, value = json_response(authorized(server, "POST", "/api/plans", {"request_text": "Groceries\nOnly oats from this shop."}))
        plan = value["plan"]
        assert status == 200 and not (tmp_path / "research.json").exists()
        status, _ = json_response(authorized(server, "POST", "/api/plans/start", {"plan_id": plan["id"], "expected_revision": plan["revision"]}))
        assert status == 400
        plan = plans.submit_plan_preview(tmp_path, plan["id"], plan["revision"], preview())["plan"]
        status, value = json_response(authorized(server, "POST", "/api/plans/confirm", {"plan_id": plan["id"], "expected_revision": plan["revision"], "user_confirmed": True}))
        assert status == 200
        plan = value["plan"]
        status, value = json_response(authorized(server, "POST", "/api/plans/start", {"plan_id": plan["id"], "expected_revision": plan["revision"]}))
        assert status == 200 and value["worker"]["status"] == "idle"
        assert value["job"]["operation"] == "research_plan"
        assert len(json_response(request(server, "GET", "/api/plans"))[1]["plans"]) == 1


def test_mcp_preview_edit_confirmation_without_external_model(tmp_path, monkeypatch):
    pytest.importorskip("mcp")
    from su_crawler.assistant_server import build_assistant_server
    monkeypatch.setattr(jobs.runtime, "start_worker", lambda root: {"status": "idle"})
    server = build_assistant_server(tmp_path)
    def call(name, args):
        return asyncio.run(server.call_tool(name, args))[1]
    plan = call("create_research_plan", {"request_text": "Clothes\nDetailed conditions stay here."})["plan"]
    assert not (tmp_path / "research.json").exists()
    plan = call("submit_research_preview", {"plan_id": plan["id"], "expected_revision": plan["revision"], "preview": preview()})["plan"]
    plan = call("update_research_plan", {"plan_id": plan["id"], "expected_revision": plan["revision"], "changes": {"exclude_terms": ["used", "damaged"]}})["plan"]
    assert call("list_recent_jobs", {})["jobs"] == []
    plan = call("confirm_research_plan", {"plan_id": plan["id"], "expected_revision": plan["revision"], "user_confirmed": True})["plan"]
    result = call("start_research_plan", {"plan_id": plan["id"], "expected_revision": plan["revision"]})
    assert result["job"]["arguments"]["plan_fingerprint"] == plans.fingerprint(plan)
    assert len(call("list_research_plans", {})["plans"]) == 1
