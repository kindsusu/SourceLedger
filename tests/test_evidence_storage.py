import hashlib
import json
from pathlib import Path

from su_crawler.models import FetchResult, Source
from su_crawler.pipeline import _save_evidence, _store_capture


def test_identical_screenshots_share_bytes_but_keep_capture_receipts(tmp_path):
    source = Source('s', 'shop', 'web', 'https://example.org', [])
    for index in range(2):
        result = FetchResult('s', 'fetched', 'playwright', content=b'<p>100</p>',
                             screenshot=b'png bytes', media_type='text/html',
                             fetched_at=f'2026-10-17T00:00:0{index}+00:00')
        _save_evidence(tmp_path, result, source)
    receipts = list((tmp_path/'evidence').rglob('*.json'))
    assert len(receipts) == 2
    assert len(list((tmp_path/'evidence').rglob('*.png'))) == 1
    records = [json.loads(p.read_text('utf-8')) for p in receipts]
    assert records[0]['at'] != records[1]['at']
    assert records[0]['artifacts']['screenshot'] == records[1]['artifacts']['screenshot']
    for record in records:
        for capture in record['artifacts'].values():
            assert hashlib.sha256(Path(capture['path']).read_bytes()).hexdigest() == capture['sha256']


def test_capture_repair_does_not_mutate_other_hardlink(tmp_path):
    import os
    path, digest = _store_capture(tmp_path, b'correct', '.png')
    path.write_bytes(b'damaged')
    sibling = tmp_path/'other-capture.png'
    os.link(path, sibling)
    repaired, _ = _store_capture(tmp_path, b'correct', '.png')
    assert repaired.read_bytes() == b'correct'
    assert sibling.read_bytes() == b'damaged'
    assert not list(path.parent.glob('.capture-*'))


def test_capture_rejects_junction_like_directory(tmp_path, monkeypatch):
    import pytest
    from su_crawler import pipeline

    root = tmp_path / 'evidence'
    monkeypatch.setattr(pipeline, '_is_linklike', lambda path: path == root)
    with pytest.raises(ValueError, match='cannot be a link'):
        _store_capture(tmp_path, b'evidence', '.png')
    assert not root.exists()
