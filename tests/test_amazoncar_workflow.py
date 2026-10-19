"""Frozen Amazoncar captures must round-trip without contacting the website."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from su_crawler.amazoncar_workflow import run


DETAIL_URL = "https://www.amazoncar.co.kr/cars/new/010695/04"
DETAIL_HTML = """<!doctype html><html><body>
<div class="choice-rent-lease"><button class="active">장기렌트</button><button>리스</button></div>
<div class="choice-box"><p class="title">대여 개월을 선택하세요</p>
  <p class="info">부가세 포함</p>
  <button><span>36개월</span><span class="price">630,740원</span></button>
</div></body></html>"""
SHORT_URL = "https://www.amazoncar.co.kr/rentPrice"
SHORT_HTML = """<!doctype html><html><body><p>부가세 별도</p>
<table><thead><tr><th>대여 기간별 일일 요금</th><th>차종</th>
<th>12시간</th><th>1~2일</th><th>3~4일</th><th>5~6일</th><th>7일 이상</th></tr></thead>
<tbody><tr><td>소형</td><td>모닝</td><td>10000</td><td>12000</td><td>13000</td><td>14000</td><td>15000</td></tr></tbody>
</table></body></html>"""


def _json(path: Path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")


def _fixture(tmp_path: Path):
    capture_root = tmp_path / "captures"
    capture_root.mkdir()

    def snapshot(filename, content, label, url):
        path = capture_root / filename
        path.write_bytes(content.encode("utf-8"))
        return {"label": label, "url": url, "captured_utc": "2026-10-17T12:00:00+00:00",
                "html": {"path": filename, "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}}

    detail = snapshot("detail.html", DETAIL_HTML, "new-010695-04-rent", DETAIL_URL)
    short = snapshot("short.html", SHORT_HTML, "short-term", SHORT_URL)
    checkpoint = {"href": "/cars/new/010695/04", "data": {
        "id": "new-010695-04", "name": ["Test vehicle"], "captures": [detail],
        "quotes": [{"mode": "rent", "term_months": 36, "amount": 630740}],
        "options": [], "mode_coverage": {"rent": "captured"},
    }}
    _json(capture_root / "checkpoints" / "details" / "new-010695-04.json", checkpoint)
    _json(capture_root / "short-term-captures.json", [short])
    manifest = capture_root / "manifest.json"
    _json(manifest, {"products": [{"href": "/cars/new/010695/04", "text": "Test vehicle"}]})
    return capture_root, manifest, checkpoint


def test_offline_capture_roundtrip_and_optional_single_export(tmp_path):
    captures, manifest, _ = _fixture(tmp_path)
    before = {p.relative_to(captures): hashlib.sha256(p.read_bytes()).hexdigest()
              for p in captures.rglob("*") if p.is_file()}
    summary = run(captures, manifest, tmp_path / "output")
    assert summary["captured_products"] == 1
    assert summary["missing_product_ids"] == []
    assert summary["quote_rows"] == 1 and summary["short_term_rows"] == 5
    assert summary["price_reextraction_differences"] == []
    assert summary["evidence_replay_failures"] == []
    assert summary["evidence_gate_pass"] is True
    assert summary["status_counts"].get("verified", 0) >= 1
    assert summary["status_counts"].get("review", 0) >= 1  # tariff has no proven currency
    assert (tmp_path / "output" / "Amazoncar.xlsx").is_file()
    after = {p.relative_to(captures): hashlib.sha256(p.read_bytes()).hexdigest()
             for p in captures.rglob("*") if p.is_file()}
    assert after == before

    no_export = run(captures, manifest, tmp_path / "without-xlsx", export=False)
    assert no_export["evidence_gate_pass"] is True
    assert not (tmp_path / "without-xlsx" / "Amazoncar.xlsx").exists()
    assert (tmp_path / "without-xlsx" / "prices.sqlite3").is_file()


def test_missing_and_tampered_snapshot_fail_before_ingestion(tmp_path):
    captures, manifest, _ = _fixture(tmp_path)
    (captures / "detail.html").write_text("changed", encoding="utf-8")
    with pytest.raises(ValueError, match="Evidence hash mismatch"):
        run(captures, manifest, tmp_path / "tampered")
    (captures / "detail.html").unlink()
    with pytest.raises(FileNotFoundError):
        run(captures, manifest, tmp_path / "missing")


def test_original_quote_drift_blocks_evidence_gate(tmp_path):
    captures, manifest, checkpoint = _fixture(tmp_path)
    checkpoint["data"]["quotes"][0]["amount"] = 999999
    _json(captures / "checkpoints" / "details" / "new-010695-04.json", checkpoint)
    summary = run(captures, manifest, tmp_path / "drift", export=False)
    assert summary["price_reextraction_differences"]
    assert summary["evidence_gate_pass"] is False


def test_uncertain_mode_transition_survives_receipt_replay(tmp_path):
    captures, manifest, checkpoint = _fixture(tmp_path)
    checkpoint["data"]["captures"][0]["integrity_flags"] = ["identical_amounts_after_network_quiet_review"]
    _json(captures / "checkpoints" / "details" / "new-010695-04.json", checkpoint)
    summary = run(captures, manifest, tmp_path / "uncertain", export=False)
    assert summary["evidence_gate_pass"] is False
    assert summary["evidence_replay_failures"] == []
    assert summary["status_counts"] == {"review": 6}
    assert any(gap["reason"] == "capture_transition_needs_review" for gap in summary["gaps"])
