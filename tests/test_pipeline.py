from dataclasses import replace
from pathlib import Path
import json
import pytest

from su_crawler.config import load_config
from su_crawler.models import Candidate, FetchResult, Product
from su_crawler.pipeline import _candidate_quality, _conflicts, _has_sufficient_evidence, execute, report_for_run
from su_crawler.storage import Store, workspace_lock, RunBusyError

ROOT = Path(__file__).resolve().parents[1]


def demo(tmp_path):
    return replace(load_config(ROOT / "examples/demo.json"), output_dir=str(tmp_path))


def test_end_to_end_and_resume_does_not_duplicate(tmp_path):
    config = demo(tmp_path)
    result = execute(config, max_tasks=1)
    assert result["status"] == "paused"
    store = Store(tmp_path)
    assert len(store.tasks(result["id"])) == 5
    before = store.observations(result["id"])
    assert len(before) == 1
    store.close()
    final = execute(config, resume_id=result["id"])
    assert final["status"] in {"completed", "partial"}
    assert Path(final["report_path"]).is_file()
    execute(config, resume_id=result["id"], collector=lambda *a: pytest.fail("completed run recollected"))
    store = Store(tmp_path)
    rows = store.observations(result["id"])
    assert len(rows) == 4
    assert len({r["id"] for r in rows}) == 4
    assert sum(r["status"] == "verified" for r in rows) == 1
    catalog = next(r for r in rows if r["source_id"] == "catalog" and r["product_id"] == "demo_a")
    assert catalog["evidence_mode"] == "static_html"
    assert catalog["source_visibility"] == "unconfirmed"
    assert catalog["status"] == "review" and catalog["comparable"] is False
    assert all(r["amount"] is None for r in rows if r["product_id"] == "demo_b")
    assert all(Path(r["evidence_path"]).is_file() for r in rows)
    store.close()


def test_partial_source_failure_does_not_abort(tmp_path):
    config = demo(tmp_path)
    from su_crawler.collectors import collect
    def collector(source, base, backend):
        return FetchResult(source.id, "needs_auth", backend, message="인증 필요") if source.id == "catalog" else collect(source, base, backend)
    result = execute(config, collector=collector)
    store = Store(tmp_path)
    assert result["status"] == "partial"
    assert len(store.tasks(result["id"])) == 5
    assert len(store.observations(result["id"])) == 2
    assert Path(result["report_path"]).is_file()
    store.close()


def test_changed_config_resume_and_lock(tmp_path):
    config = demo(tmp_path)
    run = execute(config, max_tasks=1)
    with pytest.raises(ValueError):
        execute(replace(config, name="changed"), resume_id=run["id"])
    with workspace_lock(tmp_path):
        with pytest.raises(RunBusyError):
            execute(config)


def test_stale_reexport_preserves_observation(tmp_path):
    config = demo(tmp_path)
    run = execute(config)
    store = Store(tmp_path)
    row = next(r for r in store.observations(run["id"]) if r["status"] == "verified")
    row["collected_at"] = "2000-01-01T00:00:00+00:00"
    with store.db:
        store.db.execute("UPDATE observations SET data=? WHERE id=?", (json.dumps(row), row["id"]))
    report_for_run(config, store, run["id"])
    assert next(r for r in store.observations(run["id"]) if r["id"] == row["id"])["amount"] == row["amount"]
    from openpyxl import load_workbook
    wb = load_workbook(run["report_path"], read_only=True)
    assert any("stale" in str(r) for r in wb["Observation History"].values)
    wb.close()
    store.close()


def test_http_failure_browser_fallback(tmp_path):
    config = demo(tmp_path)
    source = replace(config.sources[0], kind="web", location="https://example.com/item", backends=["http", "playwright"], min_interval_seconds=0)
    config = replace(config, sources=[source])
    calls = []
    def collector(source, base, backend):
        calls.append(backend)
        if backend == "http":
            return FetchResult(source.id, "blocked", backend, message="403")
        return FetchResult(source.id, "fetched", backend, content=(ROOT / "examples/fixtures/catalog.html").read_bytes(), final_url=source.location)
    result = execute(config, collector=collector)
    assert calls[:2] == ["http", "playwright"]
    store = Store(tmp_path)
    assert any(r["amount"] == "12000" for r in store.observations(result["id"]))
    store.close()


def test_browser_shell_does_not_discard_partial_http_evidence(tmp_path):
    config = demo(tmp_path)
    source = replace(config.sources[0], kind="web", location="https://example.com/item", backends=["http", "playwright"],
                     row_selector=".item", selectors={"model": ".model", "price": ".price", "currency": ".currency"},
                     min_interval_seconds=0, max_attempts=1)
    from su_crawler.models import Product
    config = replace(config, sources=[replace(source, product_ids=["a", "b"])],
                     products=[Product("a", "A", identifiers={"model": "A"}), Product("b", "B", identifiers={"model": "B"})])
    page = b'<div class="item"><b class="model">A</b><b class="price">12000</b><b class="currency">KRW</b></div>'
    def collector(source, base, backend):
        return FetchResult(source.id, "fetched", backend, content=page if backend == "http" else b'<div>Loading</div>',
                           media_type="text/html", final_url=source.location)
    result = execute(config, collector=collector)
    store = Store(tmp_path)
    rows = store.observations(result["id"])
    assert len(rows) == 1 and rows[0]["amount"] == "12000"
    assert next(t for t in store.tasks(result["id"]) if t["product_id"] == "a")["backend"] == "http"
    store.close()


def test_complementary_backends_keep_each_products_original_evidence(tmp_path):
    from su_crawler.models import Product

    config = demo(tmp_path)
    source = replace(
        config.sources[0], kind="web", location="https://example.com/item",
        backends=["http", "playwright"], product_ids=["a", "b"], row_selector=".item",
        selectors={"model": ".model", "price": ".price", "currency": ".currency"},
        min_interval_seconds=0, max_attempts=1,
    )
    config = replace(config, sources=[source], products=[
        Product("a", "A", identifiers={"model": "A"}),
        Product("b", "B", identifiers={"model": "B"}),
    ])
    pages = {
        "http": b'<div class="item"><b class="model">A</b><b class="price">12000</b><b class="currency">KRW</b></div>',
        "playwright": b'<div class="item"><b class="model">B</b><b class="price">23000</b><b class="currency">KRW</b></div>',
    }

    def collector(source, base, backend):
        return FetchResult(source.id, "fetched", backend, content=pages[backend],
                           media_type="text/html", final_url=source.location)

    result = execute(config, collector=collector)
    store = Store(tmp_path)
    rows = {row["product_id"]: row for row in store.observations(result["id"])}
    tasks = {task["product_id"]: task for task in store.tasks(result["id"])}
    store.close()

    assert rows["a"]["amount"] == "12000" and tasks["a"]["backend"] == "http"
    assert rows["b"]["amount"] == "23000" and tasks["b"]["backend"] == "playwright"
    assert rows["a"]["evidence_sha256"] != rows["b"]["evidence_sha256"]
    assert Path(rows["a"]["evidence_path"]).read_bytes() == pages["http"]
    assert Path(rows["b"]["evidence_path"]).read_bytes() == pages["playwright"]


def test_conflict_review_preserves_each_observed_amount():
    rows = [
        {"status": "verified", "amount": "10", "normalized_amount": "10", "comparable": True,
         "comparison_key": "same", "verification_level": "evidence_validated",
         "raw_fields": {"price": "10", "currency": "USD", "condition": "new"}},
        {"status": "verified", "amount": "12", "normalized_amount": "12", "comparable": True,
         "comparison_key": "same", "verification_level": "evidence_validated",
         "raw_fields": {"price": "12", "currency": "USD", "condition": "new"}},
    ]
    _conflicts(rows)
    assert [row["amount"] for row in rows] == ["10", "12"]
    assert all(row["status"] == "review" and row["normalized_amount"] is None for row in rows)
    assert all(not row["comparable"] and row["comparison_key"] is None for row in rows)
    assert all(row["verification_level"] == "review" for row in rows)


def test_expired_at_reexport_is_excluded_without_rewriting_history(tmp_path):
    from openpyxl import load_workbook
    config = demo(tmp_path)
    run = execute(config)
    store = Store(tmp_path)
    row = next(r for r in store.observations(run["id"]) if r["status"] == "verified")
    row["raw_fields"]["valid_to"] = "2000-01-01"
    with store.db:
        store.db.execute("UPDATE observations SET data=? WHERE id=?", (json.dumps(row), row["id"]))
    report_for_run(config, store, run["id"])
    wb = load_workbook(run["report_path"], read_only=True)
    assert wb["Price Comparison"].max_row == 2  # banner + header; the only comparable record is expired
    assert any("validity date at report time" in str(r) for r in wb["Review Required"].values)
    wb.close()
    assert next(r for r in store.observations(run["id"]) if r["id"] == row["id"])["comparable"] is True
    store.close()


def test_prefetched_responses_choose_quality_per_product_and_keep_original_evidence(tmp_path):
    config = demo(tmp_path)
    source = replace(
        config.sources[0], kind="web", location="https://example.com/items",
        backends=["http", "playwright"], product_ids=["a", "b"], max_attempts=1,
    )
    config = replace(config, sources=[source], products=[
        Product("a", "A", identifiers={"model": "A"}),
        Product("b", "B", identifiers={"model": "B"}),
    ])

    def candidate(model, price, locator, visibility, mode):
        fields = {"model": model, "price": price, "currency": "KRW"}
        return Candidate(fields, {key: {"location": locator, "raw": value,
                                        "display_state": visibility} for key, value in fields.items()},
                         locator, mode, source_visibility=visibility, evidence_mode=mode)

    http_candidates = [
        candidate("A", "100", "http:a:monthly", "hidden", "static_html"),
        candidate("A", "90", "http:a:prepay", "hidden", "static_html"),
        candidate("B", "200", "http:b", "hidden", "static_html"),
    ]
    browser_candidates = [candidate("A", "110", "browser:a", "visible", "rendered_dom")]
    http = FetchResult(source.id, "fetched", "http", content=b"static", final_url=source.location)
    browser = FetchResult(source.id, "fetched", "playwright", content=b"rendered", final_url=source.location,
                          screenshot=b"png bytes")

    result = execute(config, prefetched={source.id: [(http, http_candidates), (browser, browser_candidates)]})
    store = Store(tmp_path)
    tasks = {task["product_id"]: task for task in store.tasks(result["id"])}
    rows = store.observations(result["id"])
    store.close()

    assert tasks["a"]["backend"] == "playwright"
    assert tasks["b"]["backend"] == "http"
    assert [row["locator"] for row in rows if row["product_id"] == "a"] == ["browser:a"]
    assert [row["locator"] for row in rows if row["product_id"] == "b"] == ["http:b"]
    rendered = next(row for row in rows if row["product_id"] == "a")
    artifacts = rendered["evidence_artifacts"]
    assert set(artifacts) == {"content", "receipt", "screenshot"}
    receipt = json.loads(Path(artifacts["receipt"]["path"]).read_text(encoding="utf-8"))
    assert receipt["artifacts"] == {key: artifacts[key] for key in ("content", "screenshot")}


@pytest.mark.parametrize(("price", "separator"), [
    ("12,000", "."),
    ("KRW 12000", "."),
    ("12.000,50", ","),
])
def test_candidate_quality_uses_validation_price_parser(price, separator):
    candidate = Candidate(
        {"model": "A", "price": price},
        {"model": {"location": "row", "raw": "A", "display_state": "visible"},
         "price": {"location": "row", "raw": price, "display_state": "visible"}},
        "row", "html_css", source_visibility="visible", evidence_mode="rendered_dom",
    )
    assert _candidate_quality(candidate, separator)[0] == 1
    assert _has_sufficient_evidence([candidate], separator)


@pytest.mark.parametrize("proof", [
    {"location": "", "raw": "12000", "display_state": "visible"},
    {"location": "row", "raw": "999", "display_state": "visible"},
    {"location": "row", "raw": "12000", "display_state": "hidden"},
])
def test_candidate_quality_rejects_unproven_price(proof):
    candidate = Candidate(
        {"model": "A", "price": "12000"}, {"price": proof}, "row", "html_css",
        source_visibility="visible", evidence_mode="rendered_dom",
    )
    assert _candidate_quality(candidate)[0] == 0
    assert not _has_sufficient_evidence([candidate])
