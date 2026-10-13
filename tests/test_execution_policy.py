import pytest

from su_crawler.execution_policy import build_execution_brief


def _plan():
    return {"id": "plan-demo", "revision": 3, "state": "confirmed", "candidates": [
        {"name": "First", "url": "https://one.example/products", "selected": True},
        {"name": "Duplicate", "url": "https://one.example/products#top", "selected": True},
        {"name": "Second", "url": "https://two.example/items", "selected": True},
        {"name": "Excluded", "url": "https://excluded.example/", "selected": False},
    ]}


def test_preflight_keeps_selected_sites_and_reports_bounded_parallel():
    brief = build_execution_brief(_plan(), provider="connected", max_pages=2)
    assert brief["ready"] is True
    assert brief["selected_sites"] == ["one.example", "two.example"]
    assert [item["name"] for item in brief["selected_targets"]] == ["First", "Second"]
    assert brief["model"] is None
    assert brief["model_recommendations"]["codex"]["recommended_label"] == "Sol"
    assert brief["model_recommendations"]["claude"]["recommended_label"] == "Opus"
    assert brief["execution"]["current_concurrency"] == 2
    assert brief["execution"]["browser_concurrency"] == 1
    assert brief["execution"]["recommended_concurrency"] == 2
    assert brief["checkpoint"]["planned"] is True


def test_preflight_respects_explicit_model_and_budget():
    brief = build_execution_brief(_plan(), provider="claude", model="sonnet", max_pages=1)
    assert brief["model"]["selected_model"] == "sonnet"
    assert brief["model"]["selection_source"] == "user"
    assert brief["ready"] is False
    assert brief["page_budget_shortfall"] == 1
    assert brief["selected_target_count"] == 2
    assert len(brief["selected_targets"]) == 2


@pytest.mark.parametrize("change", [{"max_pages": True}, {"max_seconds": 0},
                                   {"follow_links": "no"}, {"provider": "api"},
                                   {"concurrency": 0}, {"concurrency": True}, {"concurrency": 5}])
def test_preflight_rejects_invalid_options(change):
    with pytest.raises(ValueError):
        build_execution_brief(_plan(), **change)


def test_preflight_exclusions_and_fractional_budget_match_collector():
    plan = _plan()
    plan["excluded_urls"] = ["https://one.example/products"]
    brief = build_execution_brief(plan, max_seconds=120.5)
    assert brief["selected_sites"] == ["two.example"]
    assert brief["max_seconds"] == 120.5
    with pytest.raises(ValueError):
        build_execution_brief(plan, provider="connected", model="bad model\n")
