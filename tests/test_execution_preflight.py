from __future__ import annotations

import asyncio
import pytest

from su_crawler import research_plans
from su_crawler.plan_jobs import preview_execution


def staged_plan(root):
    plan = research_plans.create_plan(root, "Compare clothing prices in Japan")["plan"]
    return research_plans.revise_plan(root, plan["id"], plan["revision"], {
        "topic": {"industry": "clothing", "product": "shirts", "market": "Japan"},
        "added_candidates": [{"url": "https://shop.example/shirts", "kind": "category"},
                             {"url": "https://other.example/shirts", "kind": "category"}],
    })["plan"]


def test_execution_preview_is_read_only_and_revision_pinned(tmp_path):
    plan = staged_plan(tmp_path)
    result = preview_execution(tmp_path, plan["id"], plan["revision"],
                               provider="codex", model="my-selected-model", max_seconds=120.5)["execution_plan"]
    assert result["selected_sites"] == ["shop.example", "other.example"]
    assert result["model"]["selected_model"] == "my-selected-model"
    assert result["execution"]["current_concurrency"] == 2
    assert result["execution"]["recommended_concurrency"] == 2
    assert result["checkpoint"]["planned"] is True
    assert research_plans.get_plan(tmp_path, plan["id"])["plan"] == plan
    assert not list(tmp_path.glob("jobs/*/job.json"))
    with pytest.raises(ValueError, match="changed"):
        preview_execution(tmp_path, plan["id"], plan["revision"] - 1)


def test_mcp_execution_preview_reports_unknown_host_and_budget(tmp_path):
    pytest.importorskip("mcp")
    from su_crawler.assistant_server import build_assistant_server
    plan = staged_plan(tmp_path)
    server = build_assistant_server(tmp_path)
    _, result = asyncio.run(server.call_tool("get_research_execution_plan", {
        "plan_id": plan["id"], "expected_revision": plan["revision"], "max_pages": 1,
    }))
    brief = result["execution_plan"]
    assert brief["model"] is None
    assert brief["within_page_budget"] is False
    assert brief["page_budget_shortfall"] == 1
    assert not list(tmp_path.glob("jobs/*/job.json"))


def test_web_execution_preview_and_stale_revision(tmp_path):
    from test_web_server import running_server, authorized, json_response
    plan = staged_plan(tmp_path)
    args = {"plan_id": plan["id"], "expected_revision": plan["revision"], "provider": "claude"}
    with running_server(tmp_path) as server:
        status, data = json_response(authorized(server, "POST", "/api/plans/execution-preview", args))
        assert status == 200
        assert data["execution_plan"]["model"]["recommended_label"] == "Opus"
        args["expected_revision"] -= 1
        status, _ = json_response(authorized(server, "POST", "/api/plans/execution-preview", args))
        assert status == 400
    assert not list(tmp_path.glob("jobs/*/job.json"))
