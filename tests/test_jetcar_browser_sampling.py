"""Jetcar HTTP acceptance with sampled browser verification; offline fixtures only."""
from __future__ import annotations

from collections import Counter
from pathlib import Path

import pytest

from su_crawler.browser_sampling import BrowserSamplingPolicy, rows_match, validate_sample_ratio
from su_crawler.catalog_collection import collect_plan
from su_crawler.models import Candidate, FetchResult
from su_crawler.storage import Store


FIXTURE = Path(__file__).parent / "fixtures" / "adapters" / "jetcar_detail.html"


def jetcar_url(index: int) -> str:
    return f"https://www.jetcar.kr/sub0301/{100000 + index}"


def jetcar_html(url: str, *, price: str = "682,965", rent_table: bool = True) -> bytes:
    text = FIXTURE.read_text(encoding="utf-8").replace("987654", url.rsplit("/", 1)[1]).replace("682,965", price)
    if not rent_table:
        text = text.replace("보증금", "조건")
    return text.encode("utf-8")


class Site:
    """Synthetic Jetcar pages; HTTP and browser bodies are controlled per URL."""

    def __init__(self, *, browser_price=None, http_status=None, browser_status=None, http_partial=()):
        self.browser_price = browser_price or {}
        self.http_status = http_status or {}
        self.browser_status = browser_status or {}
        self.http_partial = set(http_partial)
        self.calls: list[tuple[str, str]] = []

    def __call__(self, source, _base_dir, backend):
        url = source.location
        self.calls.append((url, backend))
        status = (self.http_status if backend == "http" else self.browser_status).get(url)
        if status:
            return FetchResult(source.id, status, backend, message=f"synthetic {status}")
        if backend == "http":
            body = jetcar_html(url, rent_table=url not in self.http_partial)
        else:
            body = jetcar_html(url, price=self.browser_price.get(url, "682,965"))
        return FetchResult(source.id, "fetched", backend, content=body, media_type="text/html", final_url=url)

    def browser_urls(self) -> list[str]:
        return [url for url, backend in self.calls if backend == "playwright"]


def plan(urls):
    return {"id": "approved-jetcar", "revision": 1, "state": "confirmed", "request_text": "Compare rentals",
            "topic": {"industry": "Vehicle rental", "product": "Passenger vehicle", "market": "South Korea"},
            "include_terms": [], "exclude_terms": [], "excluded_urls": [], "conditions": [],
            "candidates": [{"id": str(i), "name": f"Vehicle {i}", "url": url, "evidence_url": url,
                            "reason": "selected", "kind": "product", "selected": True, "origin": "manual"}
                           for i, url in enumerate(urls)]}


def events(result, url, name):
    page = next(item for item in result["coverage"] if item["url"] == url)
    return [event for event in page["trace"] if event.get("event") == name]


def observations(output, run_id):
    store = Store(Path(output))
    try:
        return store.observations(run_id)
    finally:
        store.close()


def test_sample_selection_is_deterministic_bounded_and_rotates():
    urls = [jetcar_url(i) for i in range(100)]
    first = BrowserSamplingPolicy(0.1, "job-a", urls)
    assert len(first.sampled) == 10
    assert first.sampled == BrowserSamplingPolicy(0.1, "job-a", list(reversed(urls))).sampled
    assert first.sampled != BrowserSamplingPolicy(0.1, "job-b", urls).sampled
    assert len(BrowserSamplingPolicy(0.1, "job-a", urls[:30]).sampled) == 5
    assert BrowserSamplingPolicy(0.1, "job-a", urls[:3]).sampled == set(urls[:3])
    assert len(BrowserSamplingPolicy(0.25, "job-a", urls).sampled) == 25
    mixed = BrowserSamplingPolicy(0.1, "job-a", urls[:8] + ["https://gongcar.kr/rentpass", "https://shop.example/a"])
    assert mixed.population == set(urls[:8]) and len(mixed.sampled) == 5
    assert BrowserSamplingPolicy(1.0, "job-a", urls).decide(urls[0]) is None


@pytest.mark.parametrize("value", [0, 0.0, -0.1, 1.01, float("nan"), True, "0.1", None])
def test_sample_ratio_validation(value, tmp_path):
    with pytest.raises(ValueError, match="browser_sample_ratio"):
        validate_sample_ratio(value)
    with pytest.raises(ValueError, match="browser_sample_ratio"):
        collect_plan(tmp_path, plan([jetcar_url(0)]), output_dir=tmp_path / "out", browser_sample_ratio=value)


def test_commercial_row_comparison_is_a_multiset_on_commercial_fields():
    def candidate(fields, **extra):
        return Candidate(fields=fields, evidence={}, extraction_method="fixture", **extra)
    row = candidate({"price": "1", "currency": "KRW", "term_months": "12", "name": "A"}, locator="x")
    same = candidate({"price": "1", "currency": "KRW", "term_months": "12", "name": "A", "fuel": "LPG"},
                     locator="other", evidence_mode="rendered_dom")
    assert rows_match([row], [same])
    assert rows_match([{"fields": row.fields}], [same])
    assert not rows_match([row], [same, same])
    assert not rows_match([row], [candidate({**row.fields, "price": "2"}, locator="y")])


@pytest.mark.parametrize("concurrency,browser_reuse", [(1, False), (2, True)])
def test_complete_http_skips_browser_except_for_the_sample(tmp_path, concurrency, browser_reuse):
    urls = [jetcar_url(i) for i in range(30)]
    site = Site()
    result = collect_plan(tmp_path, plan(urls), output_dir=tmp_path / "out", max_pages=30, max_seconds=300,
                          follow_links=False, collector=site, checkpoint_id="jetcar-sampling",
                          concurrency=concurrency, browser_reuse=browser_reuse,
                          browser_concurrency=1)
    sampled = BrowserSamplingPolicy(0.1, "jetcar-sampling", urls).sampled
    assert set(site.browser_urls()) == sampled and len(site.browser_urls()) == 5
    for url in urls:
        if url in sampled:
            (verification,) = events(result, url, "browser_sample_verification")
            assert verification == {"event": "browser_sample_verification", "result": "match",
                                    "http_rows": 2, "browser_rows": 2}
            assert not events(result, url, "browser_skipped")
        else:
            assert events(result, url, "browser_skipped") == [{"event": "browser_skipped",
                                                               "reason": "http_complete_jetcar"}]
    execution = result["execution"]
    assert {key: execution[key] for key in ("jetcar_http_accepted", "jetcar_browser_sampled",
                                            "jetcar_sample_mismatches", "jetcar_sample_unverified",
                                            "jetcar_browser_fallback", "browser_sample_ratio",
                                            "jetcar_http_sampling")} == {
        "jetcar_http_accepted": 25, "jetcar_browser_sampled": 5, "jetcar_sample_mismatches": 0,
        "jetcar_sample_unverified": 0, "jetcar_browser_fallback": False, "browser_sample_ratio": 0.1,
        "jetcar_http_sampling": "active"}
    rows = observations(tmp_path / "out", result["run_id"])
    assert len(rows) == 60
    # Accepted static rows are never relabelled as rendered evidence.
    accepted = {f"jetcar:{url.rsplit('/', 1)[1]}" for url in urls if url not in sampled}
    static = [row for row in rows if row["locator"].rsplit(":rent-row:", 1)[0] in accepted]
    assert len(static) == 50
    assert {row.get("evidence_mode") for row in static} == {"static_html"}
    assert {row.get("source_visibility") for row in static} == {"unconfirmed"}
    assert all(row.get("status") != "verified" for row in static)


def test_ratio_one_keeps_browser_for_every_page_and_the_same_rows(tmp_path):
    urls = [jetcar_url(i) for i in range(12)]
    always, sampled = Site(), Site()
    full = collect_plan(tmp_path / "a", plan(urls), output_dir=tmp_path / "a" / "out", max_pages=12,
                        max_seconds=300, follow_links=False, collector=always, browser_sample_ratio=1.0)
    fast = collect_plan(tmp_path / "b", plan(urls), output_dir=tmp_path / "b" / "out", max_pages=12,
                        max_seconds=300, follow_links=False, collector=sampled)
    assert always.browser_urls() == urls
    assert len(sampled.browser_urls()) == 5
    assert full["execution"]["jetcar_http_sampling"] == "disabled"
    assert full["execution"]["jetcar_http_accepted"] == full["execution"]["jetcar_browser_sampled"] == 0
    assert not any(event.get("event") in {"browser_skipped", "browser_sample_verification", "browser_required"}
                   for page in full["coverage"] for event in page["trace"])

    def commercial(result, root):
        return Counter((row["locator"], (row.get("raw_fields") or {}).get("price"),
                        (row.get("raw_fields") or {}).get("term_months"))
                       for row in observations(root, result["run_id"]))
    assert commercial(full, tmp_path / "a" / "out") == commercial(fast, tmp_path / "b" / "out")


def test_http_failure_or_partial_sections_use_the_browser(tmp_path):
    urls = [jetcar_url(i) for i in range(12)]
    site = Site(http_status={url: "failed" for url in urls[:6]}, http_partial=urls[6:])
    result = collect_plan(tmp_path, plan(urls), output_dir=tmp_path / "out", max_pages=12, max_seconds=300,
                          follow_links=False, collector=site)
    assert site.browser_urls() == urls
    assert result["execution"]["jetcar_http_accepted"] == 0
    assert result["execution"]["jetcar_browser_sampled"] == 0
    assert not any(events(result, url, "browser_skipped") for url in urls)


def test_sampled_browser_failure_is_unverified_and_keeps_http_rows(tmp_path):
    urls = [jetcar_url(i) for i in range(3)]
    site = Site(browser_status={url: "failed" for url in urls})
    result = collect_plan(tmp_path, plan(urls), output_dir=tmp_path / "out", max_pages=3, max_seconds=300,
                          follow_links=False, collector=site)
    assert site.browser_urls() == urls
    for url in urls:
        (verification,) = events(result, url, "browser_sample_verification")
        assert verification["result"] == "unverified" and verification["reason"] == "failed"
        assert verification["http_rows"] == 2 and verification["browser_rows"] is None
    execution = result["execution"]
    assert execution["jetcar_sample_unverified"] == 3 and execution["jetcar_sample_mismatches"] == 0
    assert execution["jetcar_browser_fallback"] is False
    rows = observations(tmp_path / "out", result["run_id"])
    assert len(rows) == 6 and {row.get("evidence_mode") for row in rows} == {"static_html"}
