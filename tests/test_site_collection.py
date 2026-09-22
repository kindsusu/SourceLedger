from pathlib import Path

import pytest

from su_crawler.models import FetchResult
from su_crawler.research import init_workspace
from su_crawler.site_collection import collect_sites
from su_crawler.storage import Store


FIXTURES = Path(__file__).parent / "fixtures" / "adapters"
URLS = {
    "jetcar": "https://www.jetcar.kr/sub0301/5874",
    "gongcar": "https://gongcarrent.kr/detail-quote",
    "funrent": "https://go.funrentcar.com/",
}
FILES = {
    URLS["jetcar"]: "jetcar_detail.html",
    URLS["gongcar"]: "gongcar_rendered.html",
    URLS["funrent"]: "funrent_rendered.html",
}


def workspace(tmp_path):
    path = tmp_path / "workspace.json"
    init_workspace(path, industry="Vehicle rental", product="Passenger vehicle", market="South Korea")
    return path


class FixtureCollector:
    def __init__(self, *, fail_http_for=(), empty=()):
        self.fail_http_for = set(fail_http_for)
        self.empty = set(empty)
        self.calls = []

    def __call__(self, source, base, backend, *, validators=None):
        self.calls.append((source.location, backend, validators))
        if source.location in self.fail_http_for and backend == "http":
            return FetchResult(source.id, "failed", backend, final_url=source.location, message="synthetic HTTP failure")
        content = b"<html><body>unknown page</body></html>" if source.location in self.empty else (FIXTURES / FILES[source.location]).read_bytes()
        return FetchResult(
            source.id, "fetched", backend, content=content, media_type="text/html",
            final_url=source.location, http_metadata={"etag": '"fixture-v1"'} if backend == "http" else {},
        )


def test_three_adapters_flow_to_sqlite_and_xlsx_with_estimate_separation(tmp_path):
    output = tmp_path / "prices"
    result = collect_sites(
        workspace(tmp_path), urls=list(URLS.values()), output_dir=output,
        collector=FixtureCollector(fail_http_for={URLS["gongcar"], URLS["funrent"]}),
    )
    assert Path(result["report_path"]).is_file()
    assert Path(result["config_path"]).is_file()
    assert {row["adapter"] for row in result["coverage"]} == {"jetcar", "gongcar", "funrent"}
    assert all(row["items"] > 0 and not row["coverage_gap"] for row in result["coverage"])

    store = Store(output)
    rows = store.observations(result["id"])
    store.close()
    assert {row["source_name"] for row in rows} == {"jetcar", "gongcar", "funrent"}
    estimates = [row for row in rows if row["value_origin"] == "calculator_estimate"]
    assert estimates and all(row["amount"] is None and row["derived_amount"] for row in estimates)
    assert all(not row["comparable"] and row["status"] != "verified" for row in estimates)


def test_no_identifier_becomes_explicit_coverage_gap(tmp_path):
    output = tmp_path / "prices"
    collector = FixtureCollector(empty={URLS["funrent"]})
    result = collect_sites(workspace(tmp_path), urls=[URLS["funrent"]], output_dir=output, collector=collector)
    assert result["coverage"][0]["coverage_gap"] is True
    assert result["coverage"][0]["items"] == 0
    assert 'Select a vehicle' in result['coverage'][0]['reason']
    store = Store(output)
    tasks = store.tasks(result["id"])
    store.close()
    assert len(tasks) == 1 and tasks[0]["status"] == "review"
    assert Path(result["report_path"]).is_file()


@pytest.mark.parametrize("urls,max_pages,match", [
    (["https://unsupported.example/item"], 5, "supported site adapter"),
    ([URLS["jetcar"]], 0, "max_pages"),
    ("https://www.jetcar.kr/sub0301/5874", 5, "urls must be a list"),
])
def test_bad_inputs_fail_before_collection(tmp_path, urls, max_pages, match):
    with pytest.raises(ValueError, match=match):
        collect_sites(workspace(tmp_path), urls=urls, max_pages=max_pages, collector=FixtureCollector())


def test_collector_exception_is_preserved_without_aborting_run(tmp_path):
    class BrokenCollector:
        def __call__(self, source, base, backend, **kwargs):
            raise RuntimeError("secret payload must not be exposed")

    output = tmp_path / "prices"
    result = collect_sites(workspace(tmp_path), urls=[URLS["jetcar"]], output_dir=output, collector=BrokenCollector())
    coverage = result["coverage"][0]
    assert coverage["status"] == "failed"
    assert coverage["reason"] == "Collector error: RuntimeError"
    assert "secret" not in str(result)
    assert result["status"] == "partial" and Path(result["report_path"]).is_file()


def test_incremental_200_then_304_reuses_extraction_and_evidence(tmp_path, monkeypatch):
    import su_crawler.extraction as extraction
    import su_crawler.site_collection as sites

    output = tmp_path / "prices"
    path = workspace(tmp_path)
    calls = []
    original = extraction.extract

    def tracked(result, source):
        calls.append(result.content)
        return original(result, source)

    monkeypatch.setattr(extraction, "extract", tracked)
    monkeypatch.setattr(sites, "extract", tracked)

    class ConditionalCollector:
        def __init__(self):
            self.validators = []

        def __call__(self, source, base, backend, *, validators=None):
            self.validators.append(validators)
            if validators:
                return FetchResult(source.id, "not_modified", backend, final_url=source.location, http_metadata={"etag": '"v1"'})
            return FetchResult(
                source.id, "fetched", backend, content=(FIXTURES / FILES[source.location]).read_bytes(),
                media_type="text/html", final_url=source.location, http_metadata={"etag": '"v1"'},
            )

    collector = ConditionalCollector()
    first = collect_sites(path, urls=[URLS["jetcar"]], output_dir=output, collector=collector)
    second = collect_sites(path, urls=[URLS["jetcar"]], output_dir=output, collector=collector)
    assert collector.validators == [None, {"etag": '"v1"'}]
    assert len(calls) == 1
    assert any(trace.get("code") == "extraction_reused" for trace in second["coverage"][0]["trace"])
    store = Store(output)
    first_rows = store.observations(first["id"])
    second_rows = store.observations(second["id"])
    store.close()
    assert first_rows[0]["evidence_path"] == second_rows[0]["evidence_path"]
    assert first_rows[0]["evidence_sha256"] == second_rows[0]["evidence_sha256"]


def test_earlier_fetched_candidates_survive_later_backend_failure(tmp_path):
    class LaterFailure(FixtureCollector):
        def __call__(self, source, base, backend, *, validators=None):
            if backend == "playwright":
                return FetchResult(source.id, "failed", backend, final_url=source.location, message="browser failed")
            result = super().__call__(source, base, backend, validators=validators)
            # A displayed identity without a price deliberately causes fallback.
            result.content = (FIXTURES / "jetcar_no_price.html").read_bytes()
            return result

    output = tmp_path / "prices"
    result = collect_sites(workspace(tmp_path), urls=[URLS["jetcar"]], output_dir=output, collector=LaterFailure())
    assert result["coverage"][0]["status"] == "fetched"
    assert result["coverage"][0]["items"] == 1
    store = Store(output)
    tasks = store.tasks(result["id"])
    store.close()
    assert tasks[0]["backend"] == "http"
    assert tasks[0]["attempts"] == 2


def test_prefetched_pipeline_never_calls_collector_or_extractor_again(tmp_path, monkeypatch):
    import su_crawler.extraction as extraction
    monkeypatch.setattr(extraction, 'extract', lambda *a: pytest.fail('pipeline re-extracted prefetched candidates'))
    collector = FixtureCollector()
    result = collect_sites(workspace(tmp_path), urls=[URLS['jetcar']], output_dir=tmp_path / 'prices', collector=collector)
    assert len(collector.calls) == 1
    assert result['coverage'][0]['items'] == 1
