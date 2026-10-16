"""Offline checks for bounded parallel page collection and durable leases."""
from __future__ import annotations

import json
import copy
import threading
import time

import pytest

from su_crawler.catalog_checkpoint import read_checkpoint
from su_crawler.catalog_collection import _reanalyse, collect_plan
from su_crawler.catalog_scheduler import HostGate
from su_crawler.collection_issues import issues_from_coverage
from su_crawler.models import FetchResult
from su_crawler.storage import Store


def _plan(urls):
    return {"id": "p", "state": "confirmed", "revision": 1,
            "topic": {"industry": "retail", "product": "items", "market": "web"},
            "candidates": [{"id": str(i), "url": u, "name": str(i), "selected": True}
                           for i, u in enumerate(urls)]}


def _result(source, backend):
    body = json.dumps({"@type": "Product", "name": source.location,
                       "offers": {"@type": "Offer", "price": "12", "priceCurrency": "USD"}}).encode()
    return FetchResult(source.id, "fetched", backend,
                       content=b'<script type="application/ld+json">' + body + b'</script>',
                       final_url=source.location)


def _observations(output, run_id):
    store = Store(output)
    try:
        return sorted((r["raw_fields"].get("name"), r["raw_fields"].get("price"))
                      for r in store.observations(run_id))
    finally:
        store.close()


def test_parallel_http_really_overlaps_and_matches_sequential(tmp_path):
    urls = ["https://shop.example/a", "https://shop.example/b"]
    sequential = collect_plan(tmp_path, _plan(urls), output_dir=tmp_path / "seq",
                              max_pages=2, follow_links=False,
                              collector=lambda source, base, backend: _result(source, backend))
    barrier = threading.Barrier(2)
    threads = []

    def fetch(source, base, backend):
        if backend == "http":
            threads.append(threading.get_ident())
            barrier.wait(timeout=5)
        return _result(source, backend)

    parallel = collect_plan(tmp_path, _plan(urls), output_dir=tmp_path / "par",
                            max_pages=2, follow_links=False, concurrency=2,
                            checkpoint_id="parallel", collector=fetch)
    assert len(set(threads)) == 2
    assert _observations(tmp_path / "par", parallel["run_id"]) == _observations(tmp_path / "seq", sequential["run_id"])
    assert {x["status"] for x in parallel["coverage"]} == {"visited"}
    assert parallel["execution"]["concurrency"] == 2
    assert parallel["execution"]["browser_concurrency"] == 1
    store = Store(tmp_path / "par")
    try:
        rows = store.db.execute("SELECT state,attempts FROM catalog_page_queue WHERE checkpoint_id='parallel'").fetchall()
        assert len(rows) == 2 and all(row["state"] == "completed" and row["attempts"] == 1 for row in rows)
    finally:
        store.close()


def test_http_candidates_survive_browser_captcha_without_claiming_coverage(tmp_path):
    url = "https://shop.example/product"
    calls = []

    def fetch(source, base, backend):
        calls.append(backend)
        if backend == "http":
            result = _result(source, backend)
            result.trace.append({"event": "http_evidence", "url": url})
            return result
        return FetchResult(source.id, "blocked", backend,
                           message="Access challenge detected",
                           trace=[{"event": "access_challenge", "type": "captcha"}])

    output = tmp_path / "out"
    result = collect_plan(tmp_path, _plan([url]), output_dir=output,
                          max_pages=1, follow_links=False, collector=fetch,
                          checkpoint_id="captcha")
    assert calls == ["http", "playwright"]
    assert _observations(output, result["run_id"]) == [(url, "12")]
    assert result["status"] == "partial"
    page = result["coverage"][0]
    assert page["status"] == "blocked"
    assert page["products"] == 1
    assert page["reason"] == "Access challenge detected"
    assert {event["event"] for event in page["trace"]} >= {"http_evidence", "access_challenge"}

    store = Store(output)
    try:
        assert any(issue["reason_code"] == "browser_handoff"
                   for issue in store.collection_issues(result["run_id"]))
    finally:
        store.close()

    checkpoint = read_checkpoint(output, "captcha")
    captures = copy.deepcopy(checkpoint["captures"])
    coverage = copy.deepcopy(result["coverage"])
    assert [response[0]["status"] for response in captures[url]["responses"]] == ["fetched", "blocked"]
    coverage[0]["status"] = "visited"
    coverage[0]["reason"] = ""
    coverage[0]["trace"] = []
    _reanalyse(captures, coverage, [], [], [])
    assert coverage[0]["status"] == "blocked"
    assert coverage[0]["products"] == 1
    assert coverage[0]["reason"] == "Access challenge detected"
    assert {event["event"] for event in coverage[0]["trace"]} >= {"http_evidence", "access_challenge"}
    store = Store(output)
    try:
        assert any(issue["reason_code"] == "browser_handoff"
                   for issue in issues_from_coverage(store, result["run_id"], coverage))
    finally:
        store.close()


def test_same_host_limit_and_transient_retry(tmp_path):
    urls = [f"https://shop.example/{i}" for i in range(4)]
    lock = threading.Lock()
    active = peak = 0
    calls = {}

    def fetch(source, base, backend):
        nonlocal active, peak
        if backend == "http":
            with lock:
                active += 1
                peak = max(peak, active)
                calls[source.location] = calls.get(source.location, 0) + 1
                number = calls[source.location]
            time.sleep(0.35)
            with lock:
                active -= 1
            if source.location == urls[0] and number == 1:
                return FetchResult(source.id, "blocked", "http", message="HTTP 429 rate_limited")
        return _result(source, backend)

    result = collect_plan(tmp_path, _plan(urls), output_dir=tmp_path / "out", max_pages=4,
                          max_seconds=10, concurrency=4, follow_links=False, collector=fetch,
                          checkpoint_id="rate")
    assert peak == 2 and calls[urls[0]] == 2
    assert result["execution"]["transient_retries"] == 1
    assert len(_observations(tmp_path / "out", result["run_id"])) == 4
    store = Store(tmp_path / "out")
    try:
        row = store.db.execute("SELECT attempts,state FROM catalog_page_queue WHERE checkpoint_id=? AND url=?",
                               ("rate", urls[0])).fetchone()
        assert tuple(row) == (2, "completed")
    finally:
        store.close()


def test_parallel_interruption_charges_reserved_pages_and_resumes_queue(tmp_path, monkeypatch):
    urls = [f"https://shop.example/{x}" for x in "abc"]
    output = tmp_path / "out"
    calls = []
    fail = True
    first_attempts = 0
    first_saved = threading.Event()
    from su_crawler import catalog_collection
    original_write = catalog_collection.write_checkpoint

    def write_checkpoint(*args, **kwargs):
        original_write(*args, **kwargs)
        if urls[0] in args[2].get('captures', {}):
            first_saved.set()

    monkeypatch.setattr(catalog_collection, 'write_checkpoint', write_checkpoint)

    def fetch(source, base, backend):
        nonlocal first_attempts
        calls.append((source.location, backend))
        if source.location == urls[0] and backend == "http":
            first_attempts += 1
            if first_attempts == 1:
                return FetchResult(source.id, "blocked", "http", message="HTTP 429 rate_limited")
        if source.location == urls[1] and fail:
            # Test a crash after the first result is committed, independent of
            # which future completes first or how long the rate-limit retry takes.
            assert first_saved.wait(20), 'first page was not checkpointed'
            raise KeyboardInterrupt("simulated worker death")
        return _result(source, backend)

    options = dict(output_dir=output, max_pages=3, max_seconds=180, concurrency=2,
                   checkpoint_id="resume", follow_links=False, collector=fetch)
    with pytest.raises(KeyboardInterrupt):
        collect_plan(tmp_path, _plan(urls), **options)
    state = read_checkpoint(output, "resume")
    assert state["dispatched"] == 2
    assert state["transient_retries"] == 1
    assert urls[1] in state["parallel_leases"]
    fail = False
    calls.clear()
    result = collect_plan(tmp_path, _plan(urls), **options)
    assert {url for url, _ in calls} == {urls[2]}
    assert result["budget"]["pages_used"] == 3
    assert result["budget"]["seconds_used"] >= 60
    assert result["execution"]["transient_retries"] == 1
    assert any(x["url"] == urls[1] and x["status"] == "interrupted" for x in result["coverage"])
    calls.clear()
    assert collect_plan(tmp_path, _plan(urls), **options) == result and not calls


def test_shared_host_cooldown_does_not_stall_another_host(tmp_path):
    urls = ["https://a.example/first", "https://a.example/second", "https://b.example/third"]
    starts = {}
    attempts = {}
    lock = threading.Lock()

    def fetch(source, base, backend):
        if backend == "http":
            with lock:
                attempts[source.location] = attempts.get(source.location, 0) + 1
                starts.setdefault(source.location, time.monotonic())
                count = attempts[source.location]
            if source.location == urls[0] and count == 1:
                return FetchResult(source.id, "blocked", backend, message="HTTP 429 rate_limited",
                                   http_metadata={"retry_after": "0.8"})
        return _result(source, backend)

    result = collect_plan(tmp_path, _plan(urls), output_dir=tmp_path / "out", max_pages=3,
                          max_seconds=10, concurrency=3, follow_links=False, collector=fetch,
                          checkpoint_id="cooldown")
    assert result["execution"]["transient_retries"] == 1
    assert starts[urls[1]] - starts[urls[0]] >= 0.7
    assert starts[urls[2]] - starts[urls[0]] < 0.5


def test_browser_and_http_share_global_and_host_limits(tmp_path):
    urls = [f"https://shop.example/{i}" for i in range(3)]
    lock = threading.Lock()
    active = peak = 0
    browser_calls = 0

    def fetch(source, base, backend):
        nonlocal active, peak, browser_calls
        with lock:
            active += 1
            peak = max(peak, active)
            browser_calls += backend == "playwright"
        time.sleep(0.32 if backend == "http" else 0.04)
        with lock:
            active -= 1
        return _result(source, backend)

    result = collect_plan(tmp_path, _plan(urls), output_dir=tmp_path / "out", max_pages=3,
                          max_seconds=10, concurrency=3, follow_links=False, collector=fetch,
                          checkpoint_id="slots")
    assert browser_calls >= 1
    assert peak <= 2
    assert {x["status"] for x in result["coverage"]} == {"visited"}


def test_browser_pool_overlaps_and_keeps_approved_report_order(tmp_path):
    urls = [f"https://shop.example/{name}" for name in ("slow", "fast", "last")]
    barrier = threading.Barrier(2)
    browser_threads = set()
    lock = threading.Lock()
    active = peak = 0

    def fetch(source, base, backend):
        nonlocal active, peak
        with lock:
            active += 1
            peak = max(peak, active)
        try:
            if backend == "http":
                return FetchResult(source.id, "fetched", backend, content=b"<html></html>",
                                   final_url=source.location)
            browser_threads.add(threading.get_ident())
            if source.location in urls[:2]:
                barrier.wait(timeout=5)
            if source.location == urls[0]:
                time.sleep(0.1)
            return _result(source, backend)
        finally:
            with lock:
                active -= 1

    result = collect_plan(tmp_path, _plan(urls), output_dir=tmp_path / "pool", max_pages=3,
                          max_seconds=20, concurrency=2, browser_concurrency=2,
                          browser_reuse=True, follow_links=False, collector=fetch,
                          checkpoint_id="pool")
    assert peak == 2 and len(browser_threads) == 2
    assert [row["url"] for row in result["coverage"]] == urls
    assert result["execution"]["browser_concurrency"] == 2
    assert result["execution"]["browser_reuse"] is True
    assert len(_observations(tmp_path / "pool", result["run_id"])) == 3
    with pytest.raises(ValueError, match="checkpoint scope"):
        collect_plan(tmp_path, _plan(urls), output_dir=tmp_path / "pool", max_pages=3,
                     max_seconds=20, concurrency=2, browser_concurrency=1,
                     browser_reuse=True, follow_links=False, collector=fetch,
                     checkpoint_id="pool")


def test_browser_pool_crash_reserves_inflight_without_refetch(tmp_path):
    urls = [f"https://{name}.example/product" for name in ("a", "b", "c")]
    fail = True
    calls = []
    b_started = threading.Event()

    def fetch(source, base, backend):
        calls.append(source.location)
        if fail and source.location == urls[1] and backend == "http":
            b_started.set()
            raise KeyboardInterrupt("worker crash")
        if fail and source.location == urls[0] and backend == "http":
            assert b_started.wait(3)
            time.sleep(0.1)
        return _result(source, backend)

    options = dict(output_dir=tmp_path / "pool", max_pages=3, max_seconds=180,
                   concurrency=2, browser_concurrency=2, browser_reuse=True,
                   follow_links=False, collector=fetch, checkpoint_id="pool")
    with pytest.raises(KeyboardInterrupt):
        collect_plan(tmp_path, _plan(urls), **options)
    state = read_checkpoint(tmp_path / "pool", "pool")
    assert state["dispatched"] == 2
    assert urls[1] in state["parallel_leases"]
    fail = False
    calls.clear()
    result = collect_plan(tmp_path, _plan(urls), **options)
    assert set(calls) == {urls[2]}
    assert any(row["url"] == urls[1] and row["status"] == "interrupted" for row in result["coverage"])


def test_reuse_with_one_browser_slot_retains_two_http_workers(tmp_path):
    urls = ["https://shop.example/a", "https://shop.example/b"]
    barrier = threading.Barrier(2)
    lock = threading.Lock()
    browser_active = browser_peak = 0

    def fetch(source, base, backend):
        nonlocal browser_active, browser_peak
        if backend == "http":
            barrier.wait(timeout=5)
            return FetchResult(source.id, "fetched", backend, content=b"<html></html>",
                               final_url=source.location)
        with lock:
            browser_active += 1
            browser_peak = max(browser_peak, browser_active)
        time.sleep(0.05)
        with lock:
            browser_active -= 1
        return _result(source, backend)

    result = collect_plan(tmp_path, _plan(urls), output_dir=tmp_path / "one", max_pages=2,
                          max_seconds=20, concurrency=2, browser_concurrency=1,
                          browser_reuse=True, follow_links=False, collector=fetch)
    assert browser_peak == 1
    assert len(_observations(tmp_path / "one", result["run_id"])) == 2


def test_waiting_same_host_does_not_hold_global_slot():
    gate = HostGate(concurrency=3, per_host_concurrency=2, min_interval_seconds=0)
    deadline = time.monotonic() + 3
    first = gate.enter("a.example", deadline)
    second = gate.enter("a.example", deadline)
    waiting = threading.Event()
    acquired = threading.Event()

    def wait_for_a():
        waiting.set()
        permit = gate.enter("a.example", deadline)
        if permit is not None:
            acquired.set()
            permit.release()

    worker = threading.Thread(target=wait_for_a)
    worker.start()
    try:
        assert waiting.wait(1)
        other = gate.enter("b.example", time.monotonic() + 0.5)
        assert other is not None
        other.release()
        assert not acquired.is_set()
    finally:
        first.release()
        second.release()
        worker.join(timeout=3)
    assert acquired.is_set()


def test_browser_session_start_failure_is_reported_without_waiting_budget(tmp_path, monkeypatch):
    from contextlib import contextmanager

    @contextmanager
    def broken_session(**kwargs):
        raise RuntimeError("session setup failed")
        yield

    monkeypatch.setattr("su_crawler.catalog_collection.browser_session", broken_session)
    start = time.monotonic()
    with pytest.raises(RuntimeError, match="session setup failed"):
        collect_plan(tmp_path, _plan(["https://shop.example/a"]),
                     output_dir=tmp_path / "out", max_pages=1, max_seconds=120,
                     concurrency=2, browser_reuse=True, checkpoint_id="start-failed",
                     collector=lambda source, base, backend: _result(source, backend))
    assert time.monotonic() - start < 10


def test_slow_browser_session_cleanup_finishes_before_final_checkpoint(tmp_path, monkeypatch):
    from contextlib import contextmanager
    workers = []
    exited = threading.Event()

    @contextmanager
    def slow_session(**kwargs):
        workers.append(threading.current_thread())
        try:
            yield
        finally:
            # Chromium teardown can legitimately exceed the old two-second join.
            time.sleep(2.1)
            exited.set()

    monkeypatch.setattr('su_crawler.catalog_collection.browser_session', slow_session)
    output = tmp_path / 'out'
    result = collect_plan(tmp_path, _plan(['https://shop.example/a']), output_dir=output,
                          max_pages=1, max_seconds=30, concurrency=2, browser_reuse=True,
                          follow_links=False, checkpoint_id='slow-cleanup',
                          collector=lambda source, base, backend: _result(source, backend))
    assert exited.is_set()
    assert workers and all(not worker.is_alive() for worker in workers)
    checkpoint = read_checkpoint(output, 'slow-cleanup')
    assert checkpoint['result'] == result
    assert not checkpoint['parallel_leases']
    assert len(_observations(output, result['run_id'])) == 1
