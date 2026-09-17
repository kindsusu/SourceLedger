from __future__ import annotations

import json
from pathlib import Path

import pytest

from su_crawler import agent, research


def _workspace(tmp_path: Path, *urls: str) -> tuple[Path, list[str]]:
    workspace = tmp_path / "research.json"
    research.init_workspace(workspace, industry="Parts", product="Exact part", market="Korea")
    research.set_product(workspace, identifiers={"model": "TEST-A"})
    source_ids = []
    for number, url in enumerate(urls, 1):
        value = research.add_source(workspace, url=url, scope="internal", name=f"Source {number}")
        source_ids.append(value["sources"][-1]["id"])
    return workspace, source_ids


def _proposal(task_dir: Path, source_id: str) -> dict:
    draft = task_dir / "collection.draft.json"
    task_dir.mkdir(parents=True, exist_ok=True)
    draft.write_text(json.dumps({"sources": []}), encoding="utf-8")
    return {"eligible_for_verification": False, "draft_path": str(draft), "model_calls": 0,
            "reason": "comparison conditions absent"}


def test_agent_pins_workspace_and_refuses_changed_topic_or_source(tmp_path, monkeypatch):
    workspace, source_ids = _workspace(tmp_path, "https://vendor.example/a")
    monkeypatch.setattr(agent, "_aggregate", lambda *args: None)
    monkeypatch.setattr("su_crawler.proposals.propose_source", lambda *args, **kwargs: _proposal(Path(kwargs["output_dir"]), kwargs["source_id"]))
    state = agent.run_agent(workspace, run_dir=tmp_path / "run", source_ids=source_ids, max_steps=0)
    assert state["status"] == "paused"
    research.set_product(workspace, identifiers={"model": "CHANGED"})
    with pytest.raises(ValueError, match="topic or product changed"):
        agent.run_agent(workspace, run_dir=tmp_path / "run", resume=True)


def test_agent_reserves_no_model_call_without_model_config(tmp_path, monkeypatch):
    workspace, source_ids = _workspace(tmp_path, "https://vendor.example/a")
    seen = []

    def propose(*args, **kwargs):
        seen.append(kwargs["max_model_calls"])
        return _proposal(Path(kwargs["output_dir"]), kwargs["source_id"])

    monkeypatch.setattr("su_crawler.proposals.propose_source", propose)
    monkeypatch.setattr(agent, "_aggregate", lambda *args: None)
    state = agent.run_agent(workspace, run_dir=tmp_path / "run", source_ids=source_ids, max_model_calls=0)
    assert seen == [0]
    assert state["usage"]["model_calls"] == 0
    assert state["status"] == "needs_review"


def test_agent_activation_requires_samples_before_any_source_work(tmp_path, monkeypatch):
    workspace, source_ids = _workspace(tmp_path, "https://vendor.example/a")
    monkeypatch.setattr("su_crawler.proposals.propose_source", lambda *args, **kwargs: pytest.fail("proposal must not run"))
    with pytest.raises(ValueError, match="known samples"):
        agent.run_agent(workspace, run_dir=tmp_path / "run", source_ids=source_ids, activate=True)
    assert not (tmp_path / "run" / "agent-run.json").exists()


def test_agent_enforces_selected_source_limit(tmp_path):
    workspace, source_ids = _workspace(tmp_path, "https://vendor.example/a", "https://vendor.example/b")
    with pytest.raises(ValueError, match="exceed max_sources"):
        agent.run_agent(workspace, run_dir=tmp_path / "run", source_ids=source_ids, max_sources=1)


def test_agent_resume_uses_the_saved_model_budget_not_new_defaults(tmp_path, monkeypatch):
    workspace, source_ids = _workspace(tmp_path, "https://vendor.example/a")
    model = tmp_path / "model.json"
    model.write_text("{}", encoding="utf-8")
    seen = []

    def propose(*args, **kwargs):
        seen.append(kwargs["max_model_calls"])
        return _proposal(Path(kwargs["output_dir"]), kwargs["source_id"])

    monkeypatch.setattr("su_crawler.proposals.propose_source", propose)
    monkeypatch.setattr(agent, "_aggregate", lambda *args: None)
    paused = agent.run_agent(workspace, run_dir=tmp_path / "run", source_ids=source_ids,
                             model_config_path=model, max_model_calls=1, max_steps=0)
    assert paused["status"] == "paused"
    resumed = agent.run_agent(workspace, run_dir=tmp_path / "run", resume=True)
    assert seen == [1]
    assert resumed["usage"]["model_calls"] == 0


def test_agent_rejects_a_proposed_draft_changed_before_aggregate(tmp_path):
    workspace, source_ids = _workspace(tmp_path, "https://vendor.example/a")
    draft = tmp_path / "draft.json"
    draft.write_text('{"sources": []}', encoding="utf-8")
    state = {"workspace_path": str(workspace), "tasks": [{
        "source_id": source_ids[0],
        "source_fingerprint": agent.stable_id(research.load_workspace(workspace)["sources"][0]),
        "proposal": {"draft_path": str(draft)}, "draft_sha256": agent._hash_file(draft),
    }], "inputs": {"samples": {"path": None}}, "activate": False}
    draft.write_text('{"sources": ["changed"]}', encoding="utf-8")
    with pytest.raises(ValueError, match="Proposed draft changed"):
        agent._aggregate(state, tmp_path / "run", research.load_workspace(workspace), 10)
