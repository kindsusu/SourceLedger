import pytest

from su_crawler.plan_jobs import _validate
from su_crawler.execution_policy import build_execution_brief


def arguments(**extra):
    return {"plan_id": "plan-" + "a" * 32, "expected_revision": 1, **extra}


def test_browser_settings_survive_prepared_job_validation():
    clean = _validate(arguments(concurrency=2, browser_reuse=True, browser_concurrency=2), "research_plan")
    clean["plan_fingerprint"] = "b" * 64
    resumed = _validate(clean, "research_plan", prepared=True)
    assert resumed == clean
    brief = build_execution_brief({"candidates": []}, concurrency=2,
                                  browser_reuse=resumed["browser_reuse"],
                                  browser_concurrency=resumed["browser_concurrency"])
    assert brief["execution"]["browser_concurrency"] == 2
    assert brief["execution"]["browser_reuse"] is True


def test_old_jobs_keep_serial_browser_settings():
    clean = _validate(arguments(plan_fingerprint="b" * 64), "research_plan", prepared=True)
    assert clean["concurrency"] == 1
    assert clean["browser_concurrency"] == 1
    assert clean["browser_reuse"] is False


@pytest.mark.parametrize("extra", [
    {"browser_concurrency": True}, {"browser_concurrency": 0},
    {"browser_concurrency": 3}, {"browser_concurrency": 1.5},
    {"browser_concurrency": 2, "concurrency": 1}, {"browser_reuse": "true"},
])
def test_invalid_browser_limits_rejected_in_preview_and_start(extra):
    with pytest.raises(ValueError):
        _validate(arguments(**extra), "research_plan")
    with pytest.raises(ValueError):
        build_execution_brief({"candidates": []}, **extra)
