"""Offline checks for bounded parallel page collection and durable leases."""
from __future__ import annotations

import json
import threading
import time

import pytest

from su_crawler.catalog_checkpoint import read_checkpoint
from su_crawler.catalog_collection import collect_plan
from su_crawler.catalog_scheduler import HostGate
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


def test_parallel_interruption_charges_reserved_pages_and_resumes_queue(tmp_path):
    urls = [f"https://shop.example/{x}" for x in "abc"]
    output = tmp_path / "out"
    calls = []
    fail = True
    first_attempts = 0

    def fetch(source, base, backend):
        nonlocal first_attempts
        calls.append((source.location, backend))
        if source.location == urls[0] and backend == "http":
            first_attempts += 1
            if first_attempts == 1:
                return FetchResult(source.id, "blocked", "http", message="HTTP 429 rate_limited")
        if source.location == urls[1] and fail:
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
