from __future__ import annotations

import json
import hashlib
import sqlite3

import pytest

from su_crawler.research_plans import (
    confirm_plan, confirmed_snapshot, create_plan, fingerprint, get_plan, list_plans,
    revise_plan, submit_plan_preview,
)


def _proposal(url="https://example.test/catalog"):
    return {"summary": "Candidate sources for review", "topic": {"industry": "rental", "product": "car", "market": "KR"},
            "categories": ["rental sites"], "candidates": [{"name": "Example", "url": url,
            "evidence_url": url, "reason": "Potential public catalog", "kind": "site"}], "note": "Review manually"}


def test_terms_merge_after_deduplication_and_windows_multiline(tmp_path):
    terms = [f"term-{i}" for i in range(60)]
    plan = create_plan(tmp_path, "Detailed request\r\nPreserve all conditions", terms)["plan"]
    result = submit_plan_preview(tmp_path, plan["id"], 1, {**_proposal(), "include_terms": terms})["plan"]
    assert result["include_terms"] == terms
    assert result["request_text"] == "Detailed request\r\nPreserve all conditions"
    assert result["conditions"] == []
    with pytest.raises(ValueError, match="public host"):
        submit_plan_preview(tmp_path, plan["id"], 2, _proposal("https://foo.localhost./"))


def test_plan_works_before_legacy_setup_and_preserves_research_file(tmp_path):
    research = tmp_path / "research.json"
    research.write_text('{"industry":"existing","product":{"name":"thing"},"market":"US"}', encoding="utf-8")
    original = research.read_bytes()
    created = create_plan(tmp_path, "Compare several products", ["small"], ["used"])["plan"]
    assert created["topic"] == {"industry": "", "product": "", "market": ""}
    assert created["state"] == "draft" and created["revision"] == 1
    assert get_plan(tmp_path, created["id"])["plan"] == created
    assert list_plans(tmp_path)["plans"] == [created]
    assert research.read_bytes() == original
    assert (tmp_path / "plans.sqlite3").is_file()


def test_new_plan_does_not_inherit_another_plan_topic_or_change_its_revisions(tmp_path):
    first = create_plan(tmp_path, "Find rental cars")["plan"]
    edited = revise_plan(tmp_path, first["id"], 1, {"topic": {"industry": "rental"}})["plan"]
    assert edited["topic"] == {"industry": "rental", "product": "", "market": ""}

    second = create_plan(tmp_path, "Find medical devices")["plan"]
    assert second["topic"] == {"industry": "", "product": "", "market": ""}
    assert get_plan(tmp_path, first["id"])["plan"] == edited
    assert get_plan(tmp_path, second["id"])["plan"] == second
    assert [plan["id"] for plan in list_plans(tmp_path)["plans"]] == [second["id"], first["id"]]

    preview = submit_plan_preview(tmp_path, second["id"], 1, {
        **_proposal(), "topic": {"industry": "healthcare", "product": "device", "market": "US"},
    })["plan"]
    assert preview["topic"] == {"industry": "healthcare", "product": "device", "market": "US"}
    assert get_plan(tmp_path, first["id"])["plan"] == edited


def test_cas_confirmation_and_historic_snapshot(tmp_path):
    initial = create_plan(tmp_path, "Explore rental cars")["plan"]
    pid = initial["id"]
    preview = submit_plan_preview(tmp_path, pid, 1, _proposal())["plan"]
    with pytest.raises(ValueError, match="revision changed"):
        revise_plan(tmp_path, pid, 1, {"summary": "stale"})
    with pytest.raises(ValueError, match="revision changed"):
        submit_plan_preview(tmp_path, pid, 1, _proposal())
    with pytest.raises(ValueError, match="confirmation"):
        confirm_plan(tmp_path, pid, 2, False)
    with pytest.raises(ValueError, match="not confirmed"):
        confirmed_snapshot(tmp_path, pid, 2)
    approved = confirm_plan(tmp_path, pid, 2, True)["plan"]
    assert approved["revision"] == 3 and approved["state"] == "confirmed"
    assert len(fingerprint(approved)) == 64
    with pytest.raises(ValueError, match="revision changed"):
        confirm_plan(tmp_path, pid, 2, True)
    edited = revise_plan(tmp_path, pid, 3, {"request_text": "New scope"})["plan"]
    assert edited["state"] == "preview" and edited["confirmed_at"] is None
    assert confirmed_snapshot(tmp_path, pid, 3) == approved
    assert get_plan(tmp_path, pid)["plan"] == edited


def test_regeneration_preserves_selection_exclusions_and_manual_target(tmp_path):
    pid = create_plan(tmp_path, "Find public catalogs", ["explicit"], ["do not include"])["plan"]["id"]
    first = submit_plan_preview(tmp_path, pid, 1, _proposal())["plan"]
    target = first["candidates"][0]
    edited = revise_plan(tmp_path, pid, 2, {"selected_candidate_ids": [], "added_candidates": [
        {"url": "https://manual.example.test/item"} ]})["plan"]
    assert edited["candidates"][1]["origin"] == "user"
    assert edited["candidates"][1]["name"] == "manual.example.test"
    assert edited["candidates"][1]["evidence_url"] == "https://manual.example.test/item"
    regenerated = submit_plan_preview(tmp_path, pid, 3, {**_proposal(), "include_terms": ["AI term"],
        "exclude_terms": ["AI excluded"]})["plan"]
    assert next(item for item in regenerated["candidates"] if item["id"] == target["id"])["selected"] is False
    assert len(regenerated["candidates"]) == 2
    assert regenerated["include_terms"] == ["explicit"]
    assert regenerated["exclude_terms"] == ["do not include"]
    removed = revise_plan(tmp_path, pid, 4, {"removed_candidate_ids": [target["id"]]})["plan"]
    after = submit_plan_preview(tmp_path, pid, 5, _proposal())["plan"]
    assert [item["origin"] for item in after["candidates"]] == ["user"]
    assert removed["excluded_urls"] == [target["url"]]


def test_confirmation_gates_and_strict_candidate_boundary(tmp_path):
    pid = create_plan(tmp_path, "Research")["plan"]["id"]
    with pytest.raises(ValueError, match="industry"):
        confirm_plan(tmp_path, pid, 1, True)
    for bad in (
        {**_proposal(), "price": "12"},
        {**_proposal(), "candidates": [{**_proposal()["candidates"][0], "currency": "USD"}]},
        _proposal("file:///etc/passwd"),
        _proposal("http://127.0.0.1/private"),
        _proposal("https://user:pass@example.test/a"),
    ):
        with pytest.raises(ValueError):
            submit_plan_preview(tmp_path, pid, 1, bad)
    assert get_plan(tmp_path, pid)["plan"]["revision"] == 1
    with pytest.raises(ValueError):
        revise_plan(tmp_path, pid, 1, {"added_candidates": [{"url": "https://x.test", "price": "99"}]})
    preview = submit_plan_preview(tmp_path, pid, 1, _proposal())["plan"]
    assert "price" not in json.dumps(preview)
    unselected = revise_plan(tmp_path, pid, 2, {"selected_candidate_ids": []})["plan"]
    with pytest.raises(ValueError, match="selected"):
        confirm_plan(tmp_path, pid, 3, True)
    assert unselected["revision"] == 3


def test_database_symlinks_are_rejected(tmp_path):
    outside = tmp_path.parent / (tmp_path.name + "-outside-db")
    outside.write_bytes(b"sentinel")
    link = tmp_path / "plans.sqlite3"
    try:
        link.symlink_to(outside)
    except OSError:
        pytest.skip("file symlinks unavailable")
    with pytest.raises(ValueError, match="symbolic links"):
        create_plan(tmp_path, "Research")
    assert outside.read_bytes() == b"sentinel"


def test_structured_conditions_survive_edits_confirmation_and_legacy_revisions(tmp_path):
    draft = create_plan(tmp_path, "Japan competitor cotton shirt, new only")["plan"]
    conditions = [
        {"field": "brand", "operator": "not_equals", "value": "Uniqlo"},
        {"field": "material", "operator": "contains", "value": "cotton 100%"},
        {"field": "condition", "operator": "equals", "value": "new"},
    ]
    staged = submit_plan_preview(tmp_path, draft["id"], 1, {**_proposal(), "conditions": conditions})["plan"]
    assert staged["conditions"] == conditions
    edited = revise_plan(tmp_path, draft["id"], 2, {"conditions": conditions[:2]})["plan"]
    assert edited["conditions"] == conditions[:2]
    regenerated = submit_plan_preview(tmp_path, draft["id"], 3, {**_proposal(), "conditions": conditions})["plan"]
    assert regenerated["conditions"] == conditions[:2]
    confirmed = confirm_plan(tmp_path, draft["id"], 4, True)["plan"]
    assert confirmed_snapshot(tmp_path, draft["id"], 5)["conditions"] == conditions[:2]
    assert confirmed["conditions"] == conditions[:2]

    legacy = create_plan(tmp_path, "Old record")["plan"]
    legacy_body = {key: value for key, value in legacy.items() if key not in {"conditions", "conditions_user_edited"}}
    with sqlite3.connect(tmp_path / "plans.sqlite3") as db:
        db.execute("UPDATE plan_revisions SET body=? WHERE plan_id=? AND revision=1",
                   (json.dumps(legacy_body), legacy["id"]))
    assert get_plan(tmp_path, legacy["id"])["plan"]["conditions"] == []
    assert next(item for item in list_plans(tmp_path)["plans"] if item["id"] == legacy["id"])["conditions"] == []
    expected = hashlib.sha256(json.dumps(legacy_body, ensure_ascii=False, sort_keys=True,
                                        separators=(",", ":"), allow_nan=False).encode("utf-8")).hexdigest()
    assert fingerprint(get_plan(tmp_path, legacy["id"])["plan"]) == expected


def test_missing_market_discards_ai_candidates_but_preserves_manual_source(tmp_path):
    draft = create_plan(tmp_path, "Groceries with unspecified sales region")["plan"]
    manual = revise_plan(tmp_path, draft["id"], 1, {"added_candidates": [
        {"url": "https://manual.example.test/product"}]})["plan"]
    proposal = {**_proposal(), "topic": {"industry": "groceries", "product": "food", "market": ""},
                "note": "", "conditions": [{"field": "condition", "operator": "equals", "value": "new"}]}
    staged = submit_plan_preview(tmp_path, draft["id"], 2, proposal)["plan"]
    assert [item["url"] for item in staged["candidates"]] == [manual["candidates"][0]["url"]]
    assert staged["candidates"][0]["origin"] == "user"
    assert "geographic market" in staged["note"]
    with pytest.raises(ValueError, match="market"):
        confirm_plan(tmp_path, draft["id"], 3, True)


def test_missing_market_clarification_note_stays_within_limit(tmp_path):
    draft = create_plan(tmp_path, "Groceries")["plan"]
    proposal = {**_proposal(), "topic": {"industry": "food", "product": "groceries", "market": ""},
                "note": "a" * 3000}
    staged = submit_plan_preview(tmp_path, draft["id"], 1, proposal)["plan"]
    assert len(staged["note"]) <= 3000
    assert staged["note"].endswith("Which geographic market should be researched?")
