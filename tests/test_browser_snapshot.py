"""Host-browser HTML snapshots: policy, program-side extraction and review-only rows."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from su_crawler.collection_issues import issues_from_coverage
from su_crawler.models import utc_now
from su_crawler.research_followup import submit_browser_snapshot
from test_research_followup import _blocked_job

URL = "https://shop.example.test/item"
SNAPSHOT = ('<!doctype html><html><body><ul class="list">' + "".join(
    f'<li class="item"><a href="/p/{i}"><h3>Lamp {i}</h3></a><span>{i}9,000원</span>'
    f'<span data-sourceledger-computed-hidden="true" hidden>1원</span></li>' for i in range(1, 4))
    + '</ul></body></html>')


def _write(root: Path, name: str, data: bytes) -> str:
    (root / "captures").mkdir(exist_ok=True)
    (root / "captures" / name).write_bytes(data)
    return f"captures/{name}"


def test_snapshot_rows_are_extracted_by_the_program_and_stay_review(tmp_path, monkeypatch):
    job, result = _blocked_job(tmp_path, monkeypatch, URL)
    html = _write(tmp_path, "item.html", SNAPSHOT.encode())
    args = dict(url=URL, captured_at=utc_now(), html_path=html, tool="ego-browser", final_url=URL,
                selections={"#size": "Large"})
    first = submit_browser_snapshot(tmp_path, job["id"], **args)
    assert first["rows"] == 3 and first["status"] == "review"
    assert [row["price"] for row in first["observations"]] == ["19000", "29000", "39000"]
    again = submit_browser_snapshot(tmp_path, job["id"], **args)
    assert again["submission_id"] == first["submission_id"] and again["rows"] == 3  # idempotent
    from su_crawler.storage import Store
    store = Store(Path(result["output_dir"]))
    try:
        rows = [json.loads(item[0]) for item in store.db.execute("SELECT data FROM browser_submissions")]
    finally:
        store.close()
    assert len(rows) == 3
    row = rows[0]
    # Never a rendered capture: static display state, review, not comparable.
    assert row["evidence_mode"] == "static_html" and row["status"] == "review" and not row["comparable"]
    assert any("host browser (ego-browser)" in flag for flag in row["review_flags"])
    receipt = json.loads(Path(row["evidence_artifacts"]["receipt"]["path"]).read_text(encoding="utf-8"))
    assert receipt["backend"] == "host_browser" and receipt["trace"][0]["selections"] == {"#size": "Large"}


def test_snapshot_policy_and_input_checks(tmp_path, monkeypatch):
    job, _ = _blocked_job(tmp_path, monkeypatch, URL)
    html = _write(tmp_path, "item.html", SNAPSHOT.encode())
    base = dict(url=URL, captured_at=utc_now(), html_path=html, tool="ego-browser")
    with pytest.raises(ValueError, match="approved page.s host"):
        submit_browser_snapshot(tmp_path, job["id"], **{**base, "final_url": "https://evil.example/item"})
    with pytest.raises(ValueError, match="tool"):
        submit_browser_snapshot(tmp_path, job["id"], **{**base, "tool": "scraper"})
    with pytest.raises(ValueError, match="coverage"):
        submit_browser_snapshot(tmp_path, job["id"], **{**base, "url": "https://shop.example.test/other"})
    empty = _write(tmp_path, "empty.html", b"<html><body><p>No prices here</p></body></html>")
    with pytest.raises(ValueError, match="No product rows"):
        submit_browser_snapshot(tmp_path, job["id"], **{**base, "html_path": empty})
    with pytest.raises(ValueError):
        submit_browser_snapshot(tmp_path, job["id"], **{**base, "html_path": "../outside.html"})
    # Without a reported final URL the rows say so.
    plain = submit_browser_snapshot(tmp_path, job["id"], **base)
    assert plain["rows"] == 3


def test_host_snapshot_script_marks_display_and_keeps_data_scripts():
    """The script a host browser runs: computed hidden/struck marks, JSON kept, code dropped."""
    from su_crawler.doctor import doctor
    from su_crawler.generic_extraction import extract_generic
    from su_crawler.host_capture_policy import HOST_SNAPSHOT_SCRIPT
    from su_crawler.models import FetchResult
    if next(item for item in doctor() if item["backend"] == "playwright")["status"] != "available":
        pytest.skip("Browser unavailable")
    from playwright.sync_api import sync_playwright
    from su_crawler.browser_runtime import select_browser_runtime
    page_html = ('<html><head><style>.gone{display:none}.was{text-decoration:line-through}</style>'
                 '<script>window.secret = 1;</script>'
                 '<script type="application/json" id="state">{"items":[]}</script></head><body>'
                 '<ul class="list">' + "".join(
                     f'<li class="item"><a href="/p/{i}"><h3>Lamp {i}</h3></a><span class="was">{i}9,900원</span>'
                     f'<span>{i}9,000원</span><span class="gone">1원</span></li>' for i in range(1, 4))
                 + '</ul><input name="q" value="typed text"></body></html>')
    with sync_playwright() as playwright:
        runtime = select_browser_runtime(playwright.chromium)
        browser = playwright.chromium.launch(headless=True, **runtime.launch_options())
        try:
            page = browser.new_page()
            page.set_content(page_html)
            snapshot = page.evaluate("(" + HOST_SNAPSHOT_SCRIPT + ")()")
        finally:
            browser.close()
    assert 'data-sourceledger-computed-hidden="true"' in snapshot
    assert 'data-sourceledger-computed-line-through="true"' in snapshot
    assert 'id="state"' in snapshot and "window.secret" not in snapshot and "typed text" not in snapshot
    rows = extract_generic(FetchResult("s", "fetched", "host_browser", content=snapshot.encode(),
                                       final_url=URL))
    assert [(row.fields["price"], row.fields.get("reference_price")) for row in rows] == [
        ("19000", "19900"), ("29000", "29900"), ("39000", "39900")]


def test_repeated_browser_timeouts_offer_the_host_browser(tmp_path):
    from su_crawler.storage import Store
    page = {"status": "timeout", "reason": "Browser action timed out", "attempts": 2}
    store = Store(tmp_path)
    try:
        with store.db:
            for run_id, started in (("earlier", "2026-10-01T00:00:00+00:00"), ("now", "2026-10-02T00:00:00+00:00")):
                store.db.execute("INSERT INTO runs(id,config_hash,status,started_at,demo) VALUES(?,?,?,?,?)",
                                 (run_id, "x", "completed", started, 0))
        store.save_run_coverage("earlier", [{"url": URL, **page}])
        # One run's HTTP and browser attempts alone never trigger the handoff.
        first = issues_from_coverage(store, "earlier", [{"url": URL, **page}])
        assert first[0]["reason_code"] == "transient_failure"
        issues = issues_from_coverage(store, "now", [{"url": URL, **page}, {"url": URL + "2", **page}])
    finally:
        store.close()
    by_url = {issue["page_url"]: issue for issue in issues}
    assert (by_url[URL]["reason_code"], by_url[URL]["next_action"]) == ("browser_handoff", "submit_browser_evidence")
    assert by_url[URL + "2"]["reason_code"] == "transient_failure"
