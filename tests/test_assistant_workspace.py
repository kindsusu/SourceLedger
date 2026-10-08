from __future__ import annotations

import json
from pathlib import Path

import pytest

from su_crawler.assistant_workspace import (
    _compact_observation, execute_job, prepare_job_args, result_observations, result_report,
    validate_config, workspace_root,
)
from su_crawler.research import init_workspace


ROOT = Path(__file__).resolve().parents[1]


def _workspace(tmp_path: Path) -> None:
    init_workspace(tmp_path / "research.json", industry="rental", product="car", market="KR")


def _config(tmp_path: Path, **updates) -> Path:
    document = json.loads((ROOT / "examples/demo.json").read_text(encoding="utf-8"))
    document["output_dir"] = "output"
    for source in document["sources"]:
        source["location"] = str((ROOT / "examples" / source["location"]).resolve())
        source["file_root"] = str((ROOT / "examples").resolve())
    document.update(updates)
    path = tmp_path / "config.json"
    path.write_text(json.dumps(document), encoding="utf-8")
    return path


def _contained_config(tmp_path: Path, **updates) -> Path:
    config = _config(tmp_path, **updates)
    document = json.loads(config.read_text(encoding="utf-8"))
    fixtures = tmp_path / "fixtures"
    fixtures.mkdir(exist_ok=True)
    (fixtures / "catalog.html").write_text("fixture", encoding="utf-8")
    for source in document["sources"]:
        source.update(location="catalog.html", file_root="fixtures")
    config.write_text(json.dumps(document), encoding="utf-8")
    return config


def test_prepare_job_pins_workspace_config_and_samples(tmp_path):
    _workspace(tmp_path)
    config = _config(tmp_path)
    # External fixture roots are correctly rejected by the assistant boundary.
    with pytest.raises(ValueError, match="inside"):
        prepare_job_args(tmp_path, "run", {"config_path": config.name})
    document = json.loads(config.read_text(encoding="utf-8"))
    fixtures = tmp_path / "fixtures"
    fixtures.mkdir()
    (fixtures / "catalog.html").write_text("fixture", encoding="utf-8")
    for source in document["sources"]:
        source.update(location="catalog.html", file_root="fixtures")
    config.write_text(json.dumps(document), encoding="utf-8")
    prepared = prepare_job_args(tmp_path, "run", {"config_path": config.name, "max_tasks": 1})
    assert len(prepared["workspace_fingerprint"]) == 64
    assert len(prepared["config_fingerprint"]) == 64


def test_config_budget_limits_are_enforced(tmp_path):
    _workspace(tmp_path)
    config = _config(tmp_path, max_run_seconds=3601)
    with pytest.raises(ValueError, match="max_run_seconds"):
        validate_config(tmp_path, config.name)


def test_symlinked_workspace_and_files_are_rejected(tmp_path):
    real = tmp_path / "real"
    real.mkdir()
    linked = tmp_path / "linked"
    try:
        linked.symlink_to(real, target_is_directory=True)
    except OSError:
        pytest.skip("directory symbolic links unavailable")
    with pytest.raises(ValueError, match="symbolic link"):
        workspace_root(linked)


def test_report_metadata_is_hashed_and_size_bounded(tmp_path, monkeypatch):
    report = tmp_path / "report.xlsx"
    report.write_bytes(b"xlsx")
    job = {"result": {"report_path": str(report)}}
    metadata = result_report(tmp_path, job)
    assert metadata["size"] == 4 and len(metadata["sha256"]) == 64
    monkeypatch.setattr("su_crawler.assistant_workspace.MAX_REPORT_BYTES", 3)
    with pytest.raises(ValueError, match="exceeds"):
        result_report(tmp_path, job)


def test_observation_pages_bound_primary_and_browser_evidence_independently(tmp_path):
    from su_crawler.storage import Store

    output = tmp_path / "output"
    store = Store(output)
    try:
        store.db.execute("INSERT INTO runs(id,config_hash,status,started_at,demo) VALUES(?,?,?,?,?)",
                         ("run-1", "hash", "succeeded", "2026-01-01T00:00:00+00:00", 0))
        store.db.execute("INSERT INTO tasks(id,run_id,source_id,product_id,status,updated_at) VALUES(?,?,?,?,?,?)",
                         ("task-1", "run-1", "source", "product", "succeeded", "2026-01-01T00:00:00+00:00"))
        for index in range(3):
            offer = {"id": f"offer-{index}", "source_url": "https://example.test/a", "price_profile": "rental",
                     "amount": "290000", "status": "review", "raw_fields": {"name": f"Car {index}", "price": "290000"},
                     "source_excerpt": "x" * 50_000}
            store.db.execute("INSERT INTO observations(id,run_id,task_id,data) VALUES(?,?,?,?)",
                             (offer["id"], "run-1", "task-1", json.dumps(offer)))
            evidence = {"id": f"capture-{index}", "source_url": "https://example.test/a",
                        "extraction_method": "host_browser_submission", "raw_fields": {"name": f"Different {index}"},
                        "source_excerpt": "y" * 50_000}
            store.db.execute("INSERT INTO browser_submissions(id,run_id,data) VALUES(?,?,?)",
                             (evidence["id"], "run-1", json.dumps(evidence)))
        store.db.commit()
    finally:
        store.close()
    job = {"result": {"run_id": "run-1", "output_dir": str(output)}}
    page = result_observations(tmp_path, job, offset=1, limit=1, evidence_offset=2, evidence_limit=1,
                               detail=False)
    assert page["total"] == 3 and page["unlinked_evidence_count"] == 3
    assert len(page["rows"]) == 1 and page["rows"][0]["id"] == "offer-1"
    assert len(page["evidence_submissions"]) == 1 and page["evidence_submissions"][0]["id"] == "capture-2"
    assert "source_excerpt" not in str(page)
    full = result_observations(tmp_path, job, offset=1, limit=1, evidence_offset=2, evidence_limit=1)
    assert full["rows"][0]["source_excerpt"] == "x" * 50_000
    assert len(full["evidence_submissions"]) == 1
    with pytest.raises(ValueError, match="evidence_offset"):
        result_observations(tmp_path, job, evidence_offset=-1)


def test_compact_browser_candidate_keeps_price_qualifier_and_provenance():
    row = {"id": "browser-1", "source_url": "https://gongcar.kr/rentpass", "amount": "290000",
           "verification_level": "review", "primary_candidate": True, "price_kind": "from",
           "collected_at": "2026-01-01T00:00:00+00:00", "evidence_path": "browser-1.json",
           "raw_fields": {"name": "Car", "price": "290,000원~", "contract_term": "48개월",
                          "deposit": "30%", "options": "x" * 500},
           "derived_values": {"price_kind": "from", "literal_interpretations": {
               "price": "290000", "term_months": "48", "deposit_percent": "30"},
               "source_excerpt": "hidden" * 10000},
           "source_excerpt": "hidden" * 10000}
    compact = _compact_observation(row)
    assert compact["verification_level"] == "review" and compact["primary_candidate"]
    assert compact["price_kind"] == "from"
    assert compact["raw_fields"]["contract_term"] == "48개월"
    assert compact["raw_fields"]["deposit"] == "30%"
    assert compact["interpretation"]["literal_interpretations"]["deposit_percent"] == "30"
    assert compact["raw_fields"]["options"].endswith("[truncated; request detail=true]")
    assert "source_excerpt" not in str(compact)


def test_changed_config_is_rejected_at_execution(tmp_path):
    _workspace(tmp_path)
    config = _contained_config(tmp_path)
    document = json.loads(config.read_text(encoding="utf-8"))
    arguments = prepare_job_args(tmp_path, "run", {"config_path": "config.json", "max_tasks": 1})
    document["name"] = "changed"
    config.write_text(json.dumps(document), encoding="utf-8")
    with pytest.raises(ValueError, match="Configuration changed"):
        execute_job(tmp_path, "run", arguments, job_id="a" * 32)


def test_agent_pause_resumes_same_checkpoint(monkeypatch, tmp_path):
    _workspace(tmp_path)
    workspace = json.loads((tmp_path / "research.json").read_text(encoding="utf-8"))
    workspace["product"]["identifiers"] = {"model": "A"}
    workspace["sources"] = [{
        "id": "source-a", "name": "A", "kind": "web", "location": "https://example.test/a",
        "allowed_domains": ["example.test"], "product_ids": ["product"], "scope": "public",
        "internal": False, "status": "candidate", "provenance": {"method": "explicit", "at": workspace["created_at"]},
    }]
    (tmp_path / "research.json").write_text(json.dumps(workspace), encoding="utf-8")
    calls = []

    def fake_agent(workspace_path, **kwargs):
        calls.append(kwargs)
        state = Path(kwargs["run_dir"]) / "agent-run.json"
        state.parent.mkdir(parents=True, exist_ok=True)
        state.write_text("{}", encoding="utf-8")
        return {"status": "paused" if len(calls) == 1 else "needs_review"}

    monkeypatch.setattr("su_crawler.agent.run_agent", fake_agent)
    arguments = prepare_job_args(tmp_path, "agent", {"source_ids": ["source-a"], "max_steps": 1})
    first = execute_job(tmp_path, "agent", arguments, job_id="b" * 32)
    second = execute_job(tmp_path, "agent", arguments, job_id="b" * 32)
    assert first["evidence_status"] == "paused" and second["evidence_status"] == "needs_review"
    assert calls[0]["resume"] is False
    assert calls[1]["resume"] is True and calls[1]["max_steps"] is None


def test_interrupted_retry_without_checkpoint_starts_agent_once(monkeypatch, tmp_path):
    _workspace(tmp_path)
    workspace = json.loads((tmp_path / "research.json").read_text(encoding="utf-8"))
    workspace["product"]["identifiers"] = {"model": "A"}
    workspace["sources"] = []
    (tmp_path / "research.json").write_text(json.dumps(workspace), encoding="utf-8")
    calls = []
    monkeypatch.setattr("su_crawler.agent.run_agent", lambda *args, **kwargs: calls.append(kwargs) or {"status": "needs_review"})
    arguments = prepare_job_args(tmp_path, "agent", {"max_sources": 1})
    arguments["_resume"] = True
    execute_job(tmp_path, "agent", arguments, job_id="1" * 32)
    assert calls[0]["resume"] is False


def test_interrupted_retry_without_run_row_starts_collection_once(monkeypatch, tmp_path):
    _workspace(tmp_path)
    _contained_config(tmp_path)
    calls = []
    monkeypatch.setattr("su_crawler.pipeline.execute", lambda config, **kwargs: calls.append(kwargs) or {
        "id": "2" * 32, "status": "partial",
    })
    arguments = prepare_job_args(tmp_path, "run", {"config_path": "config.json"})
    arguments["_resume"] = True
    execute_job(tmp_path, "run", arguments, job_id="2" * 32)
    assert calls == [{"resume_id": None, "new_run_id": "2" * 32, "max_tasks": None}]


def test_changed_samples_are_rejected_and_ineligible_is_not_execution_failure(monkeypatch, tmp_path):
    _workspace(tmp_path)
    _contained_config(tmp_path)
    samples = tmp_path / "samples.json"
    samples.write_text('{"schema":"source-ledger/known-samples/v1","samples":[]}', encoding="utf-8")
    arguments = prepare_job_args(tmp_path, "verify", {
        "config_path": "config.json", "samples_path": "samples.json",
    })
    samples.write_text("{}", encoding="utf-8")
    with pytest.raises(ValueError, match="Samples changed"):
        execute_job(tmp_path, "verify", arguments, job_id="c" * 32)
    samples.write_text('{"schema":"source-ledger/known-samples/v1","samples":[]}', encoding="utf-8")
    arguments = prepare_job_args(tmp_path, "verify", {
        "config_path": "config.json", "samples_path": "samples.json",
    })
    monkeypatch.setattr("su_crawler.activation.verify_config", lambda *args, **kwargs: {
        "status": "needs_review", "eligible": False, "reasons": ["fixture requires review"],
    })
    completed = execute_job(tmp_path, "verify", arguments, job_id="d" * 32)
    assert completed["execution_status"] == "succeeded"
    assert completed["evidence_status"] == "ineligible"


def test_collect_sites_uses_stable_workspace_ledger(monkeypatch, tmp_path):
    _workspace(tmp_path)
    destinations = []

    def fake_collect(workspace_path, **kwargs):
        destinations.append(Path(kwargs["output_dir"]))
        return {"status": "partial", "run_id": "fixture"}

    monkeypatch.setattr("su_crawler.site_collection.collect_sites", fake_collect)
    arguments = prepare_job_args(tmp_path, "collect_sites", {
        "urls": ["https://www.jetcar.kr/a"], "max_pages": 1,
    })
    execute_job(tmp_path, "collect_sites", arguments, job_id="e" * 32)
    execute_job(tmp_path, "collect_sites", arguments, job_id="f" * 32)
    assert destinations == [tmp_path / "site-prices", tmp_path / "site-prices"]


def test_existing_output_symlink_cannot_escape_workspace(tmp_path):
    _workspace(tmp_path)
    config = _contained_config(tmp_path)
    outside = tmp_path.parent / (tmp_path.name + "-outside")
    outside.mkdir()
    output = tmp_path / "output"
    try:
        output.symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("directory symbolic links unavailable")
    with pytest.raises(ValueError, match="inside"):
        validate_config(tmp_path, config.name)


def test_output_child_symlink_cannot_write_outside_workspace(tmp_path):
    _workspace(tmp_path)
    config = _contained_config(tmp_path)
    outside = tmp_path.parent / (tmp_path.name + "-sentinel")
    outside.mkdir()
    sentinel = outside / "prices.sqlite3"
    sentinel.write_text("unchanged", encoding="utf-8")
    output = tmp_path / "output"
    output.mkdir()
    linked = output / "prices.sqlite3"
    try:
        linked.symlink_to(sentinel)
    except OSError:
        pytest.skip("file symbolic links unavailable")
    with pytest.raises(ValueError, match="symbolic links"):
        validate_config(tmp_path, config.name)
    assert sentinel.read_text(encoding="utf-8") == "unchanged"


def test_stable_site_output_symlink_is_rejected_before_collection(monkeypatch, tmp_path):
    _workspace(tmp_path)
    outside = tmp_path.parent / (tmp_path.name + "-site-sentinel")
    outside.mkdir()
    site_output = tmp_path / "site-prices"
    try:
        site_output.symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("directory symbolic links unavailable")
    monkeypatch.setattr("su_crawler.site_collection.collect_sites", lambda *args, **kwargs: pytest.fail("must not collect"))
    arguments = prepare_job_args(tmp_path, "collect_sites", {"urls": ["https://www.jetcar.kr/a"], "max_pages": 1})
    with pytest.raises(ValueError, match="symbolic links"):
        execute_job(tmp_path, "collect_sites", arguments, job_id="3" * 32)


def test_dangling_research_lock_is_rejected_before_creation(tmp_path):
    from su_crawler.assistant_workspace import research_path

    outside = tmp_path.parent / (tmp_path.name + "-lock-target")
    linked = tmp_path / "research.json.lock"
    try:
        linked.symlink_to(outside)
    except OSError:
        pytest.skip("file symbolic links unavailable")
    with pytest.raises(ValueError, match="lock"):
        research_path(tmp_path)
    assert not outside.exists()


def test_agent_aggregate_output_symlink_is_rejected(monkeypatch, tmp_path):
    _workspace(tmp_path)
    outside = tmp_path.parent / (tmp_path.name + "-agent-sentinel")
    outside.mkdir()
    try:
        (tmp_path / "outputs").symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("directory symbolic links unavailable")
    monkeypatch.setattr("su_crawler.agent.run_agent", lambda *args, **kwargs: pytest.fail("must not run"))
    arguments = prepare_job_args(tmp_path, "agent", {"max_sources": 1})
    with pytest.raises(ValueError, match="symbolic links"):
        execute_job(tmp_path, "agent", arguments, job_id="4" * 32)
