"""Bounded HTTP fetch scheduling and durable per-page state for catalog runs."""
from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from threading import Lock, Semaphore
import time
import math
from urllib.parse import urlsplit

import httpx

from .models import FetchResult, Source
from .storage import Store


def _stamp() -> str:
    return datetime.now(timezone.utc).isoformat()


def _lease_end(seconds: float) -> str:
    return (datetime.now(timezone.utc) + timedelta(seconds=seconds)).isoformat()


class PageLedger:
    """Only the coordinator writes this SQLite queue; fetch workers never touch it."""

    def __init__(self, output: Path, checkpoint_id: str):
        self.store = Store(output)
        self.key = checkpoint_id
        self.store.db.execute("""CREATE TABLE IF NOT EXISTS catalog_page_queue (
            checkpoint_id TEXT NOT NULL, url TEXT NOT NULL, state TEXT NOT NULL,
            attempts INTEGER NOT NULL DEFAULT 0, leased_at TEXT, leased_until TEXT,
            updated_at TEXT NOT NULL,
            detail TEXT NOT NULL DEFAULT '', PRIMARY KEY(checkpoint_id, url))""")
        columns = {row[1] for row in self.store.db.execute("PRAGMA table_info(catalog_page_queue)")}
        if "leased_until" not in columns:
            self.store.db.execute("ALTER TABLE catalog_page_queue ADD COLUMN leased_until TEXT")
        self.store.db.commit()

    def set(self, url: str, state: str, *, attempts: int | None = None,
            lease_seconds: float = 0, detail: str = "") -> None:
        if state not in {"queued", "leased", "completed", "review", "failed", "interrupted"}:
            raise ValueError("Invalid page state")
        now = _stamp()
        with self.store.db:
            self.store.db.execute("""INSERT INTO catalog_page_queue
                (checkpoint_id,url,state,attempts,leased_at,leased_until,updated_at,detail)
                VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(checkpoint_id,url) DO UPDATE SET
                state=excluded.state,
                attempts=CASE WHEN ? IS NULL THEN catalog_page_queue.attempts ELSE excluded.attempts END,
                leased_at=excluded.leased_at,leased_until=excluded.leased_until,
                updated_at=excluded.updated_at,detail=excluded.detail""",
                (self.key, url, state, attempts or 0, now if state == "leased" else None,
                 _lease_end(lease_seconds) if state == "leased" else None, now, detail, attempts))

    def reconcile(self, checkpoint: dict | None, queued: list[str]) -> None:
        """The capture checkpoint owns completion; SQLite lease rows are advisory."""
        if checkpoint:
            captures = set(checkpoint.get("captures", {}))
            statuses = {row.get("url"): row.get("status") for row in checkpoint.get("coverage", [])}
            for url in captures:
                status = statuses.get(url)
                self.set(url, "completed" if status == "visited" else "review" if status == "no_data" else "failed",
                         detail=status or "captured")
            for url in set(checkpoint.get("visited", [])) - captures:
                self.set(url, "interrupted", detail="Lease has no committed capture; retry explicitly")
        for url in queued:
            self.set(url, "queued")

    def close(self) -> None:
        self.store.close()


class _Permit:
    def __init__(self, host: Semaphore, global_limit: Semaphore):
        self.host = host
        self.global_limit = global_limit

    def release(self):
        self.host.release()
        self.global_limit.release()


class _HostState:
    def __init__(self, concurrency: int):
        self.semaphore = Semaphore(concurrency)
        self.lock = Lock()
        self.next_start = 0.0
        self.cooldown_until = 0.0


class HostGate:
    """Limit global/host concurrency, stagger starts, share host cooldowns."""

    def __init__(self, concurrency: int = 2, per_host_concurrency: int = 2,
                 min_interval_seconds: float = 0.25):
        self.global_limit = Semaphore(concurrency)
        self.hosts: dict[str, _HostState] = {}
        self.lock = Lock()
        self.per_host_concurrency = per_host_concurrency
        self.interval = min_interval_seconds

    def _host(self, name: str) -> _HostState:
        with self.lock:
            if name not in self.hosts:
                self.hosts[name] = _HostState(self.per_host_concurrency)
            return self.hosts[name]

    def enter(self, host: str, deadline: float):
        state = self._host(host)
        while True:
            with state.lock:
                now = time.monotonic()
                delay = max(0.0, state.next_start - now, state.cooldown_until - now)
            if now >= deadline:
                return None
            if delay:
                time.sleep(min(delay, max(0.0, deadline - now)))
                continue
            remaining = deadline - time.monotonic()
            if remaining <= 0 or not state.semaphore.acquire(timeout=remaining):
                return None
            remaining = deadline - time.monotonic()
            if remaining <= 0 or not self.global_limit.acquire(timeout=remaining):
                state.semaphore.release()
                return None
            with state.lock:
                now = time.monotonic()
                ready = now >= state.next_start and now >= state.cooldown_until and now < deadline
                if ready:
                    state.next_start = now + self.interval
                    return _Permit(state.semaphore, self.global_limit)
            state.semaphore.release()
            self.global_limit.release()

    def cooldown(self, host: str, seconds: float) -> None:
        state = self._host(host)
        with state.lock:
            state.cooldown_until = max(state.cooldown_until, time.monotonic() + seconds)


def _retry_delay(result: FetchResult, attempt: int) -> float:
    value = result.http_metadata.get("retry_after", "")
    if isinstance(value, str) and value:
        try:
            seconds = float(value)
        except ValueError:
            try:
                seconds = (parsedate_to_datetime(value) - datetime.now(timezone.utc)).total_seconds()
            except (TypeError, ValueError, OverflowError):
                seconds = -1
        # The caller's deadline bounds waiting. Do not shorten the server's
        # requested cooldown: a long Retry-After may exhaust this run's budget.
        if math.isfinite(seconds) and seconds >= 0:
            return seconds
    return min(2.0, 0.5 * attempt)


def _transient(result: FetchResult) -> bool:
    if any(event.get('event') == 'access_challenge' for event in result.trace):
        return False
    if result.status == "timeout":
        return True
    if result.status == "blocked" and "HTTP 429" in result.message:
        return True
    if result.status == "failed":
        return ("HTTP 5" in result.message or
                any(x.get("code") == "network_or_response_error" for x in result.trace))
    return False


def fetch_http(url: str, *, source: Source, fetch, base_dir: str, deadline: float,
               gate: HostGate, attempts: int = 2) -> tuple[FetchResult, int]:
    """Retry only documented transient responses; never retry policy/auth errors."""
    host = urlsplit(url).hostname or ""
    last: FetchResult | None = None
    count = 0
    for index in range(attempts):
        semaphore = gate.enter(host, deadline)
        if semaphore is None:
            break
        try:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            bounded = replace(source, timeout_seconds=min(120 if source.adapter == "gongcar" else 30, remaining), recipe=[])
            count += 1
            try:
                last = fetch(bounded, base_dir, "http")
            except Exception as exc:
                transient = isinstance(exc, (OSError, TimeoutError, httpx.HTTPError))
                last = FetchResult(source.id, "failed", "http", message=f"Collector error: {type(exc).__name__}",
                                   trace=[{"event": "collection_failed", "code": "network_or_response_error", "at": _stamp()}]
                                   if transient else [])
            transient = _transient(last)
            if transient:
                gate.cooldown(host, _retry_delay(last, index + 1))
        finally:
            semaphore.release()
        if not transient or index + 1 >= attempts:
            break
    if last is None:
        last = FetchResult(source.id, "budget_exhausted", "http", message="Collection time limit reached")
    if count > 1:
        last.trace.append({"event": "transient_retry", "attempts": count, "at": _stamp()})
    return last, count
