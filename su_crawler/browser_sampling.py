"""Sampled browser verification for adapters whose static HTML can be complete.

Jetcar detail pages usually expose the same rental rows in static HTML as in
the rendered page. Catalog collection may therefore accept a complete HTTP
extraction and skip the browser, while a deterministic sample of the job's
pages still runs the browser and compares the commercial rows. Any mismatch
switches the rest of the job back to browser capture for every page.

HTTP-accepted rows keep their static-HTML evidence mode and unconfirmed
visibility; this module never relabels them as rendered evidence.
"""
from __future__ import annotations

from collections import Counter
from contextlib import contextmanager
from contextvars import ContextVar
import hashlib
import json
import math
from threading import Lock
from typing import Any, Iterable, Iterator
from urllib.parse import urlsplit


DEFAULT_BROWSER_SAMPLE_RATIO = 0.1
MIN_SAMPLED_PAGES = 5
SAMPLED_ADAPTERS = frozenset({'jetcar'})
# Commercial identity of one extracted row. Evidence, locators and review
# flags legitimately differ between static and rendered captures.
COMPARED_FIELDS = ('price', 'currency', 'price_basis', 'term_months', 'deposit_amount',
                   'annual_mileage_km', 'name', 'odometer_km', 'availability')
COUNTER_KEYS = ('jetcar_http_accepted', 'jetcar_browser_sampled', 'jetcar_sample_mismatches',
                'jetcar_sample_unverified')


def validate_sample_ratio(value: Any) -> float:
    """Return a ratio in (0, 1]; 0 is refused so some verification always runs."""
    if (isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value)
            or not 0 < value <= 1):
        raise ValueError("browser_sample_ratio must be greater than 0 and at most 1")
    return float(value)


def _adapter_for(url: str) -> str | None:
    from .site_collection import HOST_ADAPTERS
    return HOST_ADAPTERS.get(urlsplit(url).hostname)


def _rank(fingerprint: str, url: str) -> int:
    return int.from_bytes(hashlib.sha256(f"{fingerprint}\n{url}".encode('utf-8')).digest()[:8], 'big')


class BrowserSamplingPolicy:
    """Job-scoped, thread-safe sampling decisions and counters."""

    def __init__(self, ratio: float, fingerprint: str, urls: Iterable[str], *, fallback: bool = False):
        self.ratio = validate_sample_ratio(ratio)
        self.fingerprint = str(fingerprint)
        self.enabled = self.ratio < 1
        population = sorted({url for url in urls if _adapter_for(url) in SAMPLED_ADAPTERS})
        size = min(len(population), max(MIN_SAMPLED_PAGES, math.ceil(self.ratio * len(population))))
        ranked = sorted(population, key=lambda url: (_rank(self.fingerprint, url), url))
        self.population = frozenset(population)
        self.sampled = frozenset(ranked[:size])
        self.fallback = bool(fallback)
        self.counters = Counter({key: 0 for key in COUNTER_KEYS})
        self._lock = Lock()

    def is_sampled(self, url: str) -> bool:
        if url in self.population:
            return url in self.sampled
        # Pages discovered after the job started have no fixed population;
        # sample them by the same stable hash at the configured ratio.
        return _rank(self.fingerprint, url) < self.ratio * 2 ** 64

    def decide(self, url: str) -> str | None:
        """For a complete HTTP extraction: 'accept', 'sample', 'browser', or None (disabled)."""
        if not self.enabled:
            return None
        with self._lock:
            if self.fallback:
                return 'browser'
            if self.is_sampled(url):
                self.counters['jetcar_browser_sampled'] += 1
                return 'sample'
            self.counters['jetcar_http_accepted'] += 1
            return 'accept'

    def record(self, outcome: str) -> None:
        with self._lock:
            if outcome == 'mismatch':
                self.counters['jetcar_sample_mismatches'] += 1
                self.fallback = True
            elif outcome == 'unverified':
                self.counters['jetcar_sample_unverified'] += 1

    def mark_fallback(self) -> None:
        with self._lock:
            self.fallback = True

    def state(self) -> dict:
        with self._lock:
            return {'ratio': self.ratio, 'enabled': self.enabled, 'fallback': self.fallback,
                    'sampled_pages': len(self.sampled), 'population': len(self.population),
                    **dict(self.counters)}


_active: ContextVar[BrowserSamplingPolicy | None] = ContextVar('sourceledger_browser_sampling', default=None)


@contextmanager
def browser_sampling_scope(policy: BrowserSamplingPolicy) -> Iterator[BrowserSamplingPolicy]:
    """Share one sampling policy across every batch collected inside this scope."""
    token = _active.set(policy)
    try:
        yield policy
    finally:
        _active.reset(token)


def active_sampling_policy() -> BrowserSamplingPolicy | None:
    return _active.get()


def _fields(candidate: Any) -> dict:
    fields = candidate.get('fields') if isinstance(candidate, dict) else getattr(candidate, 'fields', None)
    return fields if isinstance(fields, dict) else {}


def commercial_rows(candidates: Iterable[Any]) -> Counter:
    """Multiset of commercial row identities; values compare as exact text."""
    return Counter(json.dumps([None if _fields(c).get(key) is None else str(_fields(c)[key])
                               for key in COMPARED_FIELDS], ensure_ascii=False)
                   for c in candidates)


def rows_match(http_candidates: Iterable[Any], browser_candidates: Iterable[Any]) -> bool:
    return commercial_rows(http_candidates) == commercial_rows(browser_candidates)


def execution_counters(events: Iterable[dict], policy: BrowserSamplingPolicy | None) -> dict:
    """Per-run counters from page trace events plus the job's fallback state."""
    counts = Counter({key: 0 for key in COUNTER_KEYS})
    for event in events:
        kind = event.get('event')
        if kind == 'browser_skipped' and event.get('reason') == 'http_complete_jetcar':
            counts['jetcar_http_accepted'] += 1
        elif kind == 'browser_sample_verification':
            counts['jetcar_browser_sampled'] += 1
            if event.get('result') == 'mismatch':
                counts['jetcar_sample_mismatches'] += 1
            elif event.get('result') == 'unverified':
                counts['jetcar_sample_unverified'] += 1
    state = policy.state() if policy is not None else {}
    return {**dict(counts), 'jetcar_browser_fallback': bool(state.get('fallback')),
            'browser_sample_ratio': state.get('ratio', DEFAULT_BROWSER_SAMPLE_RATIO),
            'jetcar_http_sampling': ('disabled' if not state.get('enabled', False) else
                                     'browser_fallback' if state.get('fallback') else 'active')}
