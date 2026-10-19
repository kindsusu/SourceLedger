from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import json
import threading

import pytest

from su_crawler import assistant_runtime as runtime, recommendation_jobs, research
from su_crawler.ai_providers import AIGenerationError, save_ai_settings
from su_crawler.assistant_workspace import execute_job, workspace_guard
from su_crawler.recommendations import (
    create_recommendation_request, list_recommendations, select_recommendations,
)
from test_web_server import authorized, json_response, request, running_server


def initialized(root):
    research.init_workspace(root / "research.json", industry="Fixture tools", product="Fixture part", market="Offline")
    return create_recommendation_request(root, query="Fixture suppliers")["request"]


def arguments(item, **changes):
    return {"request_id": item["id"], "provider": "codex", "model": "fixture-model", "timeout_seconds": 30, **changes}


def generation(**changes):
    return {"candidates": [{"name": "Fixture supplier", "url": "https://supplier.example/catalog",
                            "reason": "Fixture reference", "evidence_url": "https://supplier.example/about"}],
            "note": "Unverified test recommendation", "provider": "codex", "requested_model": "fixture-model",
            "actual_model": "fixture-model-snapshot", **changes}


def test_queue_pins_settings_deduplicates_and_stages_without_collecting(tmp_path, monkeypatch):
    item = initialized(tmp_path)
    job = recommendation_jobs.queue_generation(tmp_path, arguments(item))
    duplicate = recommendation_jobs.queue_generation(tmp_path, arguments(item, provider="claude", model="sonnet"))
    assert duplicate["id"] == job["id"]
    save_ai_settings(tmp_path, {"provider": "claude", "model": "opus", "timeout_seconds": 600})
    captured = []

    def generate(req, settings):
        captured.append(settings)
        # The external process must not monopolize the workspace lock.
        with workspace_guard(tmp_path):
            pass
        return generation()

    monkeypatch.setattr(recommendation_jobs, "generate_recommendations", generate)
    result = execute_job(tmp_path, "recommend", job["arguments"], job_id=job["id"])
    assert captured == [{"provider": "codex", "model": "fixture-model", "timeout_seconds": 30}]
    assert result["evidence_status"] == "unverified"
    assert result["result"]["actual_model"] == "fixture-model-snapshot"
    candidates = list_recommendations(tmp_path)["requests"][0]["candidates"]
    assert len(candidates) == 1 and candidates[0]["source_id"] is None
    assert result["result"]["candidate_ids"] == [candidates[0]["id"]]
    assert research.load_workspace(tmp_path / "research.json")["sources"] == []
    assert not list(tmp_path.rglob("*.sqlite3"))
    assert not list(tmp_path.rglob("*.xlsx"))
    select_recommendations(tmp_path, request_id=item["id"], candidate_ids=[candidates[0]["id"]])
    assert len(research.load_workspace(tmp_path / "research.json")["sources"]) == 1


def test_two_tabs_cannot_queue_two_model_invocations(tmp_path):
    item = initialized(tmp_path)

    def queue():
        try:
            return recommendation_jobs.queue_generation(tmp_path, arguments(item))["id"]
        except RuntimeError:
            # The nonblocking runtime lock may ask the second tab to retry.
            return None

    with ThreadPoolExecutor(max_workers=2) as executor:
        ids = list(executor.map(lambda _: queue(), range(2)))
    assert len(set(ids) - {None}) == 1
    assert len(runtime.list_jobs(tmp_path)) == 1


def test_interrupted_recommendation_requires_a_new_explicit_invocation(tmp_path):
    item = initialized(tmp_path)
    job = recommendation_jobs.queue_generation(tmp_path, arguments(item))
    job["status"] = "interrupted"
    runtime._write_job(tmp_path, job)
    with pytest.raises(ValueError, match="no checkpoint"):
        runtime.resume_job(tmp_path, job["id"])
    retried = recommendation_jobs.queue_generation(tmp_path, arguments(item, model="other-model"))
    assert retried["id"] != job["id"] and retried["arguments"]["model"] == "other-model"


def test_topic_change_during_generation_rejects_result_atomically(tmp_path, monkeypatch):
    item = initialized(tmp_path)
    job = recommendation_jobs.queue_generation(tmp_path, arguments(item))

    def generate(req, settings):
        with workspace_guard(tmp_path):
            path = tmp_path / "research.json"
            current = research.load_workspace(path)
            current["industry"] = "Changed industry"
            research._atomic_write(path, current)
        return generation()

    monkeypatch.setattr(recommendation_jobs, "generate_recommendations", generate)
    with pytest.raises(AIGenerationError) as error:
        execute_job(tmp_path, "recommend", job["arguments"], job_id=job["id"])
    assert error.value.code == "result_rejected"
    assert list_recommendations(tmp_path)["requests"][0]["candidates"] == []


def test_stale_request_is_rejected_before_model_call_but_identity_changes_are_allowed(tmp_path, monkeypatch):
    item = initialized(tmp_path)
    job = recommendation_jobs.queue_generation(tmp_path, arguments(item))
    captured = []
    monkeypatch.setattr(recommendation_jobs, "generate_recommendations", lambda *args: captured.append(args) or generation(candidates=[]))
    research.set_product(tmp_path / "research.json", identifiers={"model": "ABC"})
    execute_job(tmp_path, "recommend", job["arguments"], job_id=job["id"])
    assert len(captured) == 1
    sidecar = tmp_path / "recommendations.json"
    value = json.loads(sidecar.read_text(encoding="utf-8"))
    value["requests"][0]["query"] = "Different query"
    research._atomic_write(sidecar, value)
    with pytest.raises(AIGenerationError) as error:
        execute_job(tmp_path, "recommend", job["arguments"], job_id=job["id"])
    assert error.value.code == "request_changed" and len(captured) == 1


def test_worker_records_only_safe_provider_failure_and_keeps_existing_candidates(tmp_path, monkeypatch):
    from su_crawler.recommendations import submit_recommendations
    item = initialized(tmp_path)
    submit_recommendations(tmp_path, request_id=item["id"], candidates=generation()["candidates"])
    job = recommendation_jobs.queue_generation(tmp_path, arguments(item))

    def fail(*args):
        raise AIGenerationError("authentication_required", "Sign in to the selected CLI and retry.") from RuntimeError("secret-token-example")

    monkeypatch.setattr(recommendation_jobs, "generate_recommendations", fail)
    token = "a" * 32
    research._atomic_write(tmp_path / "runtime" / "state.json", {"token": token})
    assert runtime._run_worker(tmp_path, token, 0.1) == 0
    receipt = runtime.get_job(tmp_path, job["id"])
    assert receipt["status"] == "failed" and receipt["error"]["code"] == "authentication_required"
    assert receipt["error"]["message"] == "Sign in to the selected CLI and retry."
    assert "secret-token-example" not in json.dumps(receipt)
    assert len(list_recommendations(tmp_path)["requests"][0]["candidates"]) == 1


def test_http_generation_returns_quickly_and_polling_works_during_real_worker(tmp_path, monkeypatch):
    item = initialized(tmp_path)
    entered, release = threading.Event(), threading.Event()
    threads = []

    def generate(*args):
        entered.set()
        assert release.wait(5)
        return generation()

    def start(root):
        token = "b" * 32
        research._atomic_write(tmp_path / "runtime" / "state.json", {"token": token})
        thread = threading.Thread(target=runtime._run_worker, args=(tmp_path, token, 0.1), daemon=True)
        threads.append(thread)
        thread.start()
        return {"status": "starting"}

    monkeypatch.setattr(recommendation_jobs, "generate_recommendations", generate)
    monkeypatch.setattr("su_crawler.web_server.start_worker", start)
    try:
        with running_server(tmp_path) as server:
            payload = arguments(item)
            assert request(server, "POST", "/api/recommendations/run", payload)[0] == 403
            status, response = json_response(authorized(server, "POST", "/api/recommendations/run", payload))
            assert status == 200 and response["job"]["status"] == "queued"
            assert entered.wait(3)
            status, state = json_response(request(server, "GET", "/api/bootstrap"))
            assert status == 200 and state["recommendation_jobs"][0]["status"] == "running"
            assert state["workspace"]["sources"] == []
            release.set()
            threads[0].join(timeout=3)
            status, state = json_response(request(server, "GET", "/api/bootstrap"))
            assert status == 200 and state["recommendation_jobs"][0]["status"] == "succeeded"
            assert state["recommendations"]["requests"][0]["status"] == "ready"
    finally:
        release.set()
        for thread in threads:
            thread.join(timeout=3)


def test_http_settings_validation_no_arbitrary_commands_or_implicit_model_calls(tmp_path):
    item = initialized(tmp_path)
    with running_server(tmp_path) as server:
        settings = {"provider": "claude", "model": "sonnet", "timeout_seconds": 90}
        assert request(server, "POST", "/api/ai/settings", settings)[0] == 403
        status, config = json_response(authorized(server, "POST", "/api/ai/settings", settings))
        assert status == 200 and config["settings"] == settings
        assert runtime.list_jobs(tmp_path) == []
        for extra in ({"provider": "unknown"}, {"model": "--dangerous"}, {"command": "whoami"},
                      {"timeout_seconds": 601}, {"request_fingerprint": "a" * 64}):
            assert authorized(server, "POST", "/api/recommendations/run", {**arguments(item), **extra})[0] == 400
        assert runtime.list_jobs(tmp_path) == []
