import json

import pytest

from su_crawler import research
from su_crawler.recommendations import (
    create_recommendation_request, list_recommendations, select_recommendations,
    submit_recommendations,
)
from su_crawler import recommendations


def _init(root):
    research.init_workspace(root / "research.json", industry="Pumps", product="Pump X", market="Korea")


def _candidate(suffix="one", **changes):
    return {"name": f"Vendor {suffix}", "url": f"https://vendor.example/{suffix}",
            "reason": "Published product catalog", "evidence_url": f"https://search.example/{suffix}", **changes}


def test_staging_persists_without_registering_and_selection_is_incremental(tmp_path):
    _init(tmp_path)
    request = create_recommendation_request(tmp_path, query="pump vendors", kind="company")["request"]
    assert request["status"] == "pending" and request["topic"]["product"]["name"] == "Pump X"
    ready = submit_recommendations(tmp_path, request_id=request["id"], candidates=[_candidate(), _candidate("two")], note="Assistant candidates")["request"]
    assert ready["status"] == "ready"
    assert len(list_recommendations(tmp_path)["requests"][0]["candidates"]) == 2
    assert research.load_workspace(tmp_path / "research.json")["sources"] == []
    first = select_recommendations(tmp_path, request_id=request["id"], candidate_ids=[ready["candidates"][0]["id"]])
    assert first["added_count"] == 1
    assert first["request"]["candidates"][1]["source_id"] is None
    source = research.load_workspace(tmp_path / "research.json")["sources"][0]
    assert source["provenance"]["query"] == "pump vendors"
    assert source["provenance"]["reason"] == "Published product catalog"
    assert source["provenance"]["evidence_url"] == "https://search.example/one"
    second = select_recommendations(tmp_path, request_id=request["id"], candidate_ids=[ready["candidates"][1]["id"]])
    assert second["added_count"] == 1
    assert len(research.load_workspace(tmp_path / "research.json")["sources"]) == 2
    again = select_recommendations(tmp_path, request_id=request["id"], candidate_ids=[ready["candidates"][0]["id"]])
    assert again["added_count"] == 0 and again["source_ids"] == first["source_ids"]


def test_submission_appends_deduplicates_canonical_urls_and_empty_is_explicit(tmp_path):
    _init(tmp_path)
    request_id = create_recommendation_request(tmp_path, query="pump")["request"]["id"]
    assert submit_recommendations(tmp_path, request_id=request_id, candidates=[])["request"]["status"] == "empty"
    first = submit_recommendations(tmp_path, request_id=request_id, candidates=[_candidate()])["request"]
    again = submit_recommendations(tmp_path, request_id=request_id, candidates=[_candidate(url="https://VENDOR.EXAMPLE:443/one#fragment"), _candidate("two")])["request"]
    assert len(again["candidates"]) == 2
    assert again["candidates"][0]["id"] == first["candidates"][0]["id"]


@pytest.mark.parametrize("candidate", [
    _candidate(url="http://127.0.0.1/x"), _candidate(url="https://localhost/x"),
    _candidate(url="https://u:p@vendor.example/x"), _candidate(evidence_url="file:///tmp/source"),
    _candidate(reason=""), {**_candidate(), "extra": "bad"},
])
def test_invalid_candidate_rejected_without_partial_submission(tmp_path, candidate):
    _init(tmp_path)
    request_id = create_recommendation_request(tmp_path, query="pump")["request"]["id"]
    with pytest.raises(ValueError):
        submit_recommendations(tmp_path, request_id=request_id, candidates=[_candidate(), candidate])
    assert list_recommendations(tmp_path)["requests"][0]["candidates"] == []


def test_selection_prevalidates_ids_scope_and_capacity(tmp_path, monkeypatch):
    _init(tmp_path)
    request_id = create_recommendation_request(tmp_path, query="pump")["request"]["id"]
    candidates = submit_recommendations(tmp_path, request_id=request_id, candidates=[_candidate(), _candidate("two")])["request"]["candidates"]
    with pytest.raises(ValueError, match="Unknown"):
        select_recommendations(tmp_path, request_id=request_id, candidate_ids=[candidates[0]["id"], "bad"])
    assert research.load_workspace(tmp_path / "research.json")["sources"] == []
    research.add_source(tmp_path / "research.json", url="https://vendor.example/two", scope="internal")
    with pytest.raises(ValueError, match="internal"):
        select_recommendations(tmp_path, request_id=request_id, candidate_ids=[item["id"] for item in candidates])
    assert len(research.load_workspace(tmp_path / "research.json")["sources"]) == 1
    with monkeypatch.context() as patch:
        patch.setattr(research, "MAX_SOURCES", 1)
        with pytest.raises(ValueError, match="capacity"):
            select_recommendations(tmp_path, request_id=request_id, candidate_ids=[candidates[0]["id"]])
    assert list_recommendations(tmp_path)["requests"][0]["candidates"][0]["source_id"] is None


def test_changed_topic_rejects_mutation_and_corrupt_sidecar(tmp_path):
    _init(tmp_path)
    request_id = create_recommendation_request(tmp_path, query="pump")["request"]["id"]
    research.set_product(tmp_path / "research.json", identifiers={"model": "P-1"})
    assert submit_recommendations(tmp_path, request_id=request_id, candidates=[_candidate()])["request"]["status"] == "ready"
    workspace = research.load_workspace(tmp_path / "research.json")
    workspace["market"] = "Japan"
    research._atomic_write(tmp_path / "research.json", workspace)
    with pytest.raises(ValueError, match="topic changed"):
        submit_recommendations(tmp_path, request_id=request_id, candidates=[_candidate("two")])
    with pytest.raises(ValueError, match="topic changed"):
        select_recommendations(tmp_path, request_id=request_id, candidate_ids=["bad"])
    sidecar = tmp_path / "recommendations.json"
    value = json.loads(sidecar.read_text(encoding="utf-8"))
    value["unexpected"] = True
    sidecar.write_text(json.dumps(value), encoding="utf-8")
    with pytest.raises(ValueError, match="schema"):
        list_recommendations(tmp_path)


def test_sidecar_symlink_rejected(tmp_path):
    _init(tmp_path)
    outside = tmp_path.parent / f"{tmp_path.name}-outside.json"
    outside.write_text("sentinel", encoding="utf-8")
    try:
        (tmp_path / "recommendations.json").symlink_to(outside)
    except OSError:
        pytest.skip("symbolic links unavailable")
    with pytest.raises(ValueError, match="symbolic link"):
        create_recommendation_request(tmp_path, query="pump")
    assert outside.read_text(encoding="utf-8") == "sentinel"


def test_selection_rejects_real_serialized_size_before_source_write(tmp_path, monkeypatch):
    _init(tmp_path)
    request_id = create_recommendation_request(tmp_path, query="pump")["request"]["id"]
    candidate = submit_recommendations(tmp_path, request_id=request_id, candidates=[_candidate()])["request"]["candidates"][0]
    before = (tmp_path / "recommendations.json").read_bytes()
    # The compact representation fits but the indented on-disk form does not.
    document = json.loads(before)
    document["requests"][0]["candidates"][0]["source_id"] = f"source-{research.stable_id(candidate['url'])}"
    compact = len(json.dumps(document, ensure_ascii=False).encode("utf-8"))
    pretty = recommendations._serialized_size(document)
    limit = max(len(before), compact) + 1
    assert limit < pretty
    monkeypatch.setattr(recommendations, "MAX_FILE_BYTES", limit)
    with pytest.raises(ValueError, match="too large"):
        select_recommendations(tmp_path, request_id=request_id, candidate_ids=[candidate["id"]])
    assert research.load_workspace(tmp_path / "research.json")["sources"] == []
    assert (tmp_path / "recommendations.json").read_bytes() == before
