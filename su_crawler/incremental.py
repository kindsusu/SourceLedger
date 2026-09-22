"""Opt-in conditional retrieval and extraction reuse, never stale quote reuse."""
from __future__ import annotations

from dataclasses import asdict, replace
import hashlib
import json
from pathlib import Path
from typing import Callable

from .models import Candidate, FetchResult, Source, stable_id


def extraction_version() -> str:
    root = Path(__file__).parent
    files = [root / 'extraction.py', root / 'models.py']
    files.extend(sorted((root / 'adapters').glob('*.py')))
    digest = hashlib.sha256()
    for path in files:
        digest.update(path.name.encode())
        digest.update(path.read_bytes())
    return digest.hexdigest()


def cache_key(source: Source) -> str:
    scope = asdict(source)
    # Extraction depends on the source rules, not which products subsequently
    # match its raw candidates, nor a per-call remaining time budget.
    for key in ('product_ids', 'timeout_seconds', 'max_attempts', 'min_interval_seconds'):
        scope.pop(key, None)
    return stable_id('source-snapshot-v1', scope, extraction_version())


def eligible(source: Source, backend: str) -> bool:
    return (source.incremental and backend == 'http' and source.kind == 'web'
            and not source.recipe and not source.profile_dir and not source.internal
            and source.account_scope == 'public')


def _candidate_hash(values: list[dict]) -> str:
    return hashlib.sha256(json.dumps(values, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def load_snapshot(store, directory: Path, source: Source, backend: str) -> dict | None:
    if not eligible(source, backend):
        return None
    saved = store.source_snapshot(cache_key(source))
    if not saved:
        return None
    try:
        evidence = Path(saved['evidence_path']).resolve()
        if not evidence.is_relative_to((directory / 'evidence').resolve()):
            return None
        content = evidence.read_bytes()
        if hashlib.sha256(content).hexdigest() != saved['sha256']:
            return None
        if _candidate_hash(saved['candidates']) != saved['candidate_sha256']:
            return None
        candidates = [Candidate(**value) for value in saved['candidates']]
        return {**saved, 'content': content, 'parsed_candidates': candidates}
    except (OSError, ValueError, KeyError, TypeError):
        return None


def conditional_fetch(collector: Callable, source: Source, base_dir: str,
                      backend: str, saved: dict | None) -> FetchResult:
    validators = saved.get('http_metadata', {}) if saved else {}
    # Do not conditionally fetch an originally redirected URL. A 304 at another
    # location cannot revalidate the original representation.
    if saved and saved.get('final_url') != source.location:
        validators = {}
    if validators:
        result = collector(source, base_dir, backend, validators=validators)
    else:
        result = collector(source, base_dir, backend)
    if result.status != 'not_modified':
        return result
    if not saved or not validators or result.final_url != saved.get('final_url'):
        return replace(result, status='failed', content=b'',
                       message='Conditional response has no matching retained evidence',
                       trace=[*result.trace, {'code': 'cache_evidence_unavailable'}])
    return replace(result, status='fetched', content=saved['content'],
                   media_type=saved['media_type'],
                   http_metadata={**validators, **result.http_metadata},
                   trace=[*result.trace, {'code': 'http_not_modified',
                       'original_fetched_at': saved['fetched_at'], 'sha256': saved['sha256']}])


def extract_current(result: FetchResult, source: Source, saved: dict | None, extractor: Callable) -> list[Candidate]:
    digest = hashlib.sha256(result.content).hexdigest()
    if (saved and digest == saved['sha256'] and result.final_url == saved['final_url']
            and result.media_type == saved['media_type']):
        result.trace.append({'code': 'extraction_reused', 'sha256': digest})
        return saved['parsed_candidates']
    return extractor(result, source)


def save_snapshot(store, source: Source, result: FetchResult,
                  candidates: list[Candidate], evidence_path: str, digest: str) -> None:
    if not eligible(source, result.backend) or result.status != 'fetched':
        return
    if any(event.get('code') == 'extraction_error' for event in result.trace):
        return
    serialized = [asdict(candidate) for candidate in candidates]
    store.save_source_snapshot(cache_key(source), {
        'evidence_path': evidence_path, 'sha256': digest,
        'candidates': serialized, 'candidate_sha256': _candidate_hash(serialized),
        'final_url': result.final_url, 'fetched_at': result.fetched_at,
        'media_type': result.media_type, 'http_metadata': result.http_metadata,
    })
