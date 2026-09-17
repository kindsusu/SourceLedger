from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import json

import pytest

import su_crawler.research as research


def initialize(path: Path, **overrides):
    values = {"industry": "Industrial pumps", "product": "Process pump", "market": "South Korea"}
    values.update(overrides)
    return research.init_workspace(path, **values)


def test_init_defaults_to_english_and_korean_changes_only_locale(tmp_path):
    english_path = tmp_path / "english.json"
    korean_path = tmp_path / "korean.json"
    english = initialize(english_path)
    korean = initialize(korean_path, locale="ko")

    assert english["locale"] == "en"
    assert korean["locale"] == "ko"
    assert "analysis_purpose" not in english
    assert set(english) == set(korean)
    assert english["product"] == {
        "id": "product", "name": "Process pump", "identifiers": {}, "required_specs": {},
    }
    assert research.load_workspace(english_path) == english
    status = research.research_status(english_path)
    assert status["ready"] is False
    assert status["blockers"] == [
        "missing_exact_product_identifiers", "missing_source_candidates", "no_verified_source",
    ]
    assert status["focus"] == "research candidates only; verified config tracked separately via receipts"
    assert status["sources"] == []
    assert status["next_actions"][0] == "Set an exact model, SKU, or other product identifier."
    assert status["search"] == {"status": "search_provider_unconfigured", "network_calls": 0}
    ko_status = research.research_status(korean_path)
    assert ko_status["next_actions"][0] == "정확한 모델, SKU 또는 다른 상품 식별자를 설정하세요."


@pytest.mark.parametrize("field", ["industry", "product", "market"])
def test_init_requires_all_research_fields_without_leaving_partial_file(tmp_path, field):
    path = tmp_path / "workspace.json"
    values = {"industry": "Pumps", "product": "Pump", "market": "KR"}
    values[field] = "  "
    with pytest.raises(ValueError, match="required"):
        research.init_workspace(path, **values)
    assert not path.exists()


def test_init_refuses_overwrite_and_concurrent_creators_have_one_winner(tmp_path):
    existing = tmp_path / "existing.json"
    before = initialize(existing)
    with pytest.raises(FileExistsError):
        initialize(existing, product="Different product")
    assert research.load_workspace(existing) == before

    concurrent = tmp_path / "concurrent.json"

    def create():
        try:
            initialize(concurrent)
            return "created"
        except (FileExistsError, RuntimeError):
            return "refused"

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(lambda _: create(), range(2)))
    assert outcomes.count("created") == 1
    assert outcomes.count("refused") == 1
    assert research.load_workspace(concurrent)["product"]["name"] == "Process pump"


def test_atomic_write_failure_preserves_existing_workspace(tmp_path, monkeypatch):
    path = tmp_path / "workspace.json"
    before = initialize(path)
    real_replace = research.os.replace

    def fail_replace(source, destination):
        if Path(destination) == path:
            raise OSError("simulated replacement failure")
        return real_replace(source, destination)

    monkeypatch.setattr(research.os, "replace", fail_replace)
    with pytest.raises(OSError, match="replacement"):
        research.set_product(path, identifiers={"model": "PX-100"})
    assert research.load_workspace(path) == before
    assert not list(tmp_path.glob(".*.tmp"))


def test_add_source_is_offline_canonical_and_deduplicated(tmp_path, monkeypatch):
    path = tmp_path / "workspace.json"
    initialize(path)

    def unexpected_network(*args, **kwargs):
        pytest.fail("add_source must not perform network access")

    monkeypatch.setattr(research, "discover", unexpected_network)
    value = research.add_source(
        path, url="HTTPS://Example.COM:443/catalog/item?pack=10#price", name="Vendor catalog",
    )
    assert len(value["sources"]) == 1
    source = value["sources"][0]
    assert source["location"] == "https://example.com/catalog/item?pack=10"
    assert source["allowed_domains"] == ["example.com"]
    assert source["status"] == "candidate"
    assert source["scope"] == "public"
    assert source["provenance"]["method"] == "explicit"

    again = research.add_source(path, url="https://example.com/catalog/item?pack=10#other")
    assert len(again["sources"]) == 1
    with pytest.raises(ValueError, match="different scopes"):
        research.add_source(path, url="https://example.com/catalog/item?pack=10", scope="internal")
    with pytest.raises(ValueError, match="credentials"):
        research.add_source(path, url="https://user:secret@example.com/item")
    for malformed in ("https://example.com/line\nbreak", "https://example.com/a\\b"):
        with pytest.raises(ValueError, match="control characters or backslashes"):
            research.add_source(path, url=malformed)

    status = research.research_status(path)
    assert status["sources"] == [{
        "id": source["id"], "location": source["location"],
        "status": "candidate", "scope": "public",
    }]
    assert any("candidate source ID" in action for action in status["next_actions"])


def test_discovery_is_bounded_deduplicated_and_records_provenance(tmp_path, monkeypatch):
    path = tmp_path / "workspace.json"
    initialize(path)
    value = research.add_source(path, url="https://example.com/catalog#top")
    source_id = value["sources"][0]["id"]
    calls = []

    def fake_discover(source, base_dir, *, limit):
        calls.append((source, base_dir, limit))
        return {
            "status": "discovered",
            "urls": [
                "https://example.com/catalog#duplicate-parent",
                "https://example.com/bad\\path",
                "https://example.com/item-a#price",
                "https://example.com/item-a#reviews",
                "https://other.example/item-b",
                "https://example.com/item-c",
            ],
            "limit_reached": True,
        }

    monkeypatch.setattr(research, "discover", fake_discover)
    result = research.discover_candidates(path, source_id=source_id, limit=4)
    assert len(calls) == 1
    assert calls[0][0].allowed_domains == ["example.com"]
    assert calls[0][0].backends == ["http", "playwright"]
    assert calls[0][2] == 4
    assert result["added_count"] == 1
    added = result["added"][0]
    assert added["location"] == "https://example.com/item-a"
    assert added["status"] == "candidate"
    assert added["provenance"]["method"] == "seed_discovery"
    assert added["provenance"]["discovered_from"] == source_id
    assert "price" not in added
    stored = research.load_workspace(path)
    assert len(stored["sources"]) == 2
    assert all(item["status"] == "candidate" for item in stored["sources"])


def test_product_maps_reject_duplicate_keys_after_trimming(tmp_path):
    path = tmp_path / "workspace.json"
    initialize(path)
    with pytest.raises(ValueError, match="duplicate keys"):
        research.set_product(path, identifiers={"model": "PX-100", " model ": "PX-200"})
    assert research.load_workspace(path)["product"]["identifiers"] == {}


def test_exact_identifiers_gate_draft_and_free_text_never_becomes_identifier(tmp_path):
    path = tmp_path / "workspace.json"
    output = tmp_path / "collection.draft.json"
    initialize(path, product="A useful but ambiguous pump name")
    research.add_source(path, url="https://example.com/items/pump")

    with pytest.raises(ValueError, match="Exact product identifiers"):
        research.generate_draft(path, output_path=output)
    assert not output.exists()

    updated = research.set_product(
        path, identifiers={"model": "PX-100"}, required_specs={"voltage": "220V"},
    )
    assert updated["product"]["identifiers"] == {"model": "PX-100"}
    result = research.generate_draft(path, output_path=output)
    assert result["ready"] is False
    assert "not verified" in result["warning"]
    draft = json.loads(output.read_text(encoding="utf-8"))
    assert draft["products"][0]["name"] == "A useful but ambiguous pump name"
    assert draft["products"][0]["identifiers"] == {"model": "PX-100"}
    assert draft["products"][0]["identifiers"] != {"name": updated["product"]["name"]}
    assert draft["sources"][0]["selectors"] == {}
    assert draft["sources"][0]["recipe"] == []
    assert draft["sources"][0]["backends"] == ["http", "playwright"]
    assert draft["output_dir"] == str((tmp_path / "outputs").resolve())
    with pytest.raises(FileExistsError):
        research.generate_draft(path, output_path=output)
    with pytest.raises(ValueError, match="different"):
        research.generate_draft(path, output_path=path)


def test_workspace_rejects_unknown_or_policy_changing_fields(tmp_path):
    path = tmp_path / "workspace.json"
    value = initialize(path)
    value["analysis_purpose"] = "pricing"
    path.write_text(json.dumps(value), encoding="utf-8")
    with pytest.raises(ValueError, match="schema"):
        research.load_workspace(path)
