from __future__ import annotations

import json

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
    with pytest.raises(ValueError, match="public host"):
        submit_plan_preview(tmp_path, plan["id"], 2, _proposal("https://foo.localhost./"))


def test_plan_works_before_legacy_setup_and_preserves_research_file(tmp_path):
    research = tmp_path / "research.json"
    research.write_text('{"industry":"existing","product":{"name":"thing"},"market":"US"}', encoding="utf-8")
    original = research.read_bytes()
    created = create_plan(tmp_path, "Compare several products", ["small"], ["used"])["plan"]
    assert created["topic"] == {"industry": "existing", "product": "thing", "market": "US"}
    assert created["state"] == "draft" and created["revision"] == 1
    assert get_plan(tmp_path, created["id"])["plan"] == created
    assert list_plans(tmp_path)["plans"] == [created]
    assert research.read_bytes() == original
    assert (tmp_path / "plans.sqlite3").is_file()


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
    assert regenerated["include_terms"] == ["explicit", "AI term"]
    assert regenerated["exclude_terms"] == ["do not include", "AI excluded"]
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
