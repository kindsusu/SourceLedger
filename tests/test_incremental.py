from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from su_crawler.incremental import cache_key
from su_crawler.models import Candidate, CollectionConfig, FetchResult, Product, Source
from su_crawler.pipeline import execute
from su_crawler.storage import Store


def _candidate(*, valid_to: str | None = None) -> Candidate:
    fields = {
        "model": "P-1", "price": "10", "currency": "USD", "unit": "each",
        "pack_quantity": "1", "tax": "included", "price_type": "sale", "price_basis": "each",
    }
    if valid_to is not None:
        fields["valid_to"] = valid_to
    return Candidate(
        fields=fields,
        evidence={key: {"location": f"css:{key}", "raw": value} for key, value in fields.items()},
        locator="css-row:1", extraction_method="test",
    )


def _config(tmp_path: Path, source: Source) -> CollectionConfig:
    return CollectionConfig(
        name="incremental", output_dir=str(tmp_path), base_dir=str(tmp_path),
        products=[Product(id="p", name="P", identifiers={"model": "P-1"})], sources=[source], max_run_seconds=30,
    )


def _source(**changes) -> Source:
    values = dict(
        id="source", name="source", kind="web", location="https://catalog.example.test/items", product_ids=["p"],
        allowed_domains=["catalog.example.test"], backends=["http"], respect_robots=False,
        incremental=True, account_scope="public", min_interval_seconds=0, max_attempts=1,
        selectors={"model": ".model", "price": ".price"},
    )
    values.update(changes)
    return Source(**values)


class _Collector:
    def __init__(self, responses: list[tuple[str, bytes]]):
        self.responses = list(responses)
        self.calls: list[dict[str, str] | None] = []

    def __call__(self, source, base, backend, *, validators=None):
        self.calls.append(validators)
        status, content = self.responses.pop(0)
        return FetchResult(
            source.id, status, backend, content=content, media_type="text/html", final_url=source.location,
            http_metadata={"etag": '"v1"'} if status in {"fetched", "not_modified"} else {},
        )


def _patch_extractor(monkeypatch, candidate: Candidate):
    import su_crawler.extraction as extraction

    calls = []
    original = extraction.extract

    def tracked(result, source):
        calls.append(result.content)
        return [_candidate_clone(candidate)]

    monkeypatch.setattr(extraction, "extract", tracked)
    return calls, original


def _candidate_clone(candidate: Candidate) -> Candidate:
    return Candidate(dict(candidate.fields), dict(candidate.evidence), candidate.locator, candidate.extraction_method,
                     dict(candidate.specs), candidate.value_origin, candidate.source_visibility,
                     dict(candidate.derived_values), list(candidate.review_flags))


def test_304_reuses_evidence_extracts_once_and_creates_fresh_run(monkeypatch, tmp_path):
    source = _source()
    config = _config(tmp_path, source)
    extracted, _ = _patch_extractor(monkeypatch, _candidate())
    collector = _Collector([("fetched", b"same"), ("not_modified", b"")])

    first = execute(config, collector=collector, new_run_id="first")
    second = execute(config, collector=collector, new_run_id="second")

    assert collector.calls == [None, {"etag": '"v1"'}]
    assert len(extracted) == 1
    store = Store(tmp_path)
    first_row = store.observations(first["id"])[0]
    second_row = store.observations(second["id"])[0]
    assert first_row["evidence_path"] == second_row["evidence_path"]
    assert first_row["evidence_sha256"] == second_row["evidence_sha256"]
    assert first_row["collected_at"] != second_row["collected_at"]
    assert {first["id"], second["id"]} == {run["id"] for run in (store.run("first"), store.run("second"))}
    store.close()


def test_same_bytes_reuses_extraction_but_changed_bytes_reextracts(monkeypatch, tmp_path):
    source = _source()
    config = _config(tmp_path, source)
    extracted, _ = _patch_extractor(monkeypatch, _candidate())
    collector = _Collector([("fetched", b"one"), ("fetched", b"one"), ("fetched", b"two")])

    execute(config, collector=collector, new_run_id="one")
    execute(config, collector=collector, new_run_id="two")
    execute(config, collector=collector, new_run_id="three")

    assert len(extracted) == 2
    assert collector.calls == [None, {"etag": '"v1"'}, {"etag": '"v1"'}]


def test_corrupt_evidence_and_source_scope_changes_do_not_send_validators(monkeypatch, tmp_path):
    source = _source()
    config = _config(tmp_path, source)
    _patch_extractor(monkeypatch, _candidate())
    collector = _Collector([("fetched", b"original"), ("fetched", b"replacement"), ("fetched", b"missing-file"), ("fetched", b"new-scope")])
    execute(config, collector=collector, new_run_id="one")

    store = Store(tmp_path)
    saved = store.source_snapshot(cache_key(source))
    Path(saved["evidence_path"]).write_bytes(b"altered")
    store.close()
    execute(config, collector=collector, new_run_id="two")

    store = Store(tmp_path)
    replacement = store.source_snapshot(cache_key(source))
    Path(replacement["evidence_path"]).unlink()
    store.close()
    execute(config, collector=collector, new_run_id="missing")

    changed = replace(source, recipe_version="2")
    execute(_config(tmp_path, changed), collector=collector, new_run_id="three")
    assert collector.calls == [None, None, None, None]


@pytest.mark.parametrize("changed", [
    {"selectors": {"model": ".model", "price": ".new-price"}},
    {"account_scope": "member"},
])
def test_extractor_config_and_account_scope_invalidate_cache(monkeypatch, tmp_path, changed):
    source = _source()
    _patch_extractor(monkeypatch, _candidate())
    collector = _Collector([("fetched", b"one"), ("fetched", b"two")])
    execute(_config(tmp_path, source), collector=collector, new_run_id="one")
    execute(_config(tmp_path, replace(source, **changed)), collector=collector, new_run_id="two")
    assert collector.calls == [None, None]


def test_default_incremental_false_and_browser_are_always_fresh(monkeypatch, tmp_path):
    source = _source(incremental=False)
    _patch_extractor(monkeypatch, _candidate())
    collector = _Collector([("fetched", b"one"), ("fetched", b"two")])
    execute(_config(tmp_path, source), collector=collector, new_run_id="one")
    execute(_config(tmp_path, source), collector=collector, new_run_id="two")
    assert collector.calls == [None, None]

    browser = replace(source, incremental=True, backends=["playwright"])
    browser_collector = _Collector([("fetched", b"one"), ("fetched", b"two")])
    execute(_config(tmp_path / "browser", browser), collector=browser_collector, new_run_id="one")
    execute(_config(tmp_path / "browser", browser), collector=browser_collector, new_run_id="two")
    assert browser_collector.calls == [None, None]


def test_cached_content_still_runs_fresh_validation(monkeypatch, tmp_path):
    source = _source()
    validations = []
    import su_crawler.validation as validation

    original = validation.validate

    def tracked(*args, **kwargs):
        result = original(*args, **kwargs)
        validations.append(result.status)
        return result

    monkeypatch.setattr(validation, "validate", tracked)
    _patch_extractor(monkeypatch, _candidate(valid_to="2000-01-01"))
    collector = _Collector([("fetched", b"same"), ("not_modified", b"")])
    first = execute(_config(tmp_path, source), collector=collector, new_run_id="one")
    second = execute(_config(tmp_path, source), collector=collector, new_run_id="two")
    assert validations == ["review", "review"]
    store = Store(tmp_path)
    assert store.observations(first["id"])[0]["status"] == store.observations(second["id"])[0]["status"] == "review"
    store.close()
