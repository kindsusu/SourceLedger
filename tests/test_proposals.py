from __future__ import annotations

import hashlib
import json
import math

import pytest

from su_crawler.models import FetchResult
from su_crawler import proposals, research


def workspace(tmp_path, html_source="https://example.com/p"):
    path = tmp_path / "research.json"
    research.init_workspace(path, industry="Parts", product="Exact part", market="Korea")
    research.set_product(path, identifiers={"model": "TEST-A"}, required_specs={"grade": "A"})
    data = research.add_source(path, url=html_source, name="Example")
    return path, data["sources"][0]["id"]


def fetched(source, html, backend="http"):
    return FetchResult(
        source_id=source.id, status="fetched", backend=backend, content=html.encode(),
        media_type="text/html", final_url=source.location, fetched_at="2026-01-01T00:00:00+00:00",
    )


def test_semantic_proposal_preserves_evidence_and_never_fabricates_conditions(tmp_path, monkeypatch):
    path, source_id = workspace(tmp_path)
    html = '''<article class="product"><span data-field="model">TEST-A</span>
      <span itemprop="price">12000</span><span class="currency">KRW</span>
      <span data-field="grade">A</span></article>'''
    calls = []
    monkeypatch.setattr(proposals, "collect", lambda source, base, backend: calls.append(backend) or fetched(source, html, backend))
    result = proposals.propose_source(path, source_id=source_id, output_dir=tmp_path / "proposal", max_model_calls=0)
    assert result["status"] == "needs_review"
    assert result["method"] == "semantic"
    assert calls == ["http", "playwright"]
    assert result["model_calls"] == 0
    assert result["draft_path"] and result["eligible_for_verification"] is False
    assert "pack_quantity" not in result["rules"]["selectors"]
    evidence = (tmp_path / "proposal" / "evidence.playwright.html").read_bytes()
    assert result["evidence_sha256"] == hashlib.sha256(evidence).hexdigest()
    assert result["preview_observations"][0]["raw_fields"]["price"] == "12000"
    assert result["preview_observations"][0]["evidence_path"] == result["evidence_path"]
    assert result["preview_observations"][0]["evidence_sha256"] == result["evidence_sha256"]
    assert result["preview_observations"][0]["comparable"] is False
    draft = json.loads((tmp_path / "proposal" / "collection.draft.json").read_text(encoding="utf-8"))
    assert draft["sources"][0]["account_scope"] == "public"
    assert draft["sources"][0]["location"] == "https://example.com/p"
    assert draft["output_dir"] == str((tmp_path / "proposal" / "collection").resolve())


@pytest.mark.parametrize("browser_status", ["timeout", "tool_unavailable", "fetched"])
def test_failed_or_empty_browser_keeps_http_preview_with_original_evidence(tmp_path, monkeypatch, browser_status):
    path, source_id = workspace(tmp_path)
    html = '''<article class="product"><span data-field="model">TEST-A</span>
      <span class="price">12000</span><span class="currency">KRW</span></article>'''
    calls = []
    def capture(source, base, backend):
        calls.append(backend)
        if backend == "http":
            return fetched(source, html, backend)
        return FetchResult(source.id, browser_status, backend, content=b"<html>empty</html>")
    monkeypatch.setattr(proposals, "collect", capture)
    result = proposals.propose_source(path, source_id=source_id, output_dir=tmp_path / "p", max_model_calls=0)
    assert calls == ["http", "playwright"]
    assert result["status"] == "needs_review"
    assert result["capture"]["backend"] == "http"
    assert result["evidence_path"].endswith("evidence.http.html")
    preview = result["preview_observations"][0]
    assert preview["raw_fields"]["price"] == "12000"
    assert preview["evidence_sha256"] == result["evidence_sha256"] == hashlib.sha256(html.encode()).hexdigest()
    assert preview["evidence_path"] == result["evidence_path"]
    assert preview["source_visibility"] == "unconfirmed"
    assert result["eligible_for_verification"] is False


def test_browser_policy_denial_blocks_earlier_unconfirmed_proposal(tmp_path, monkeypatch):
    path, source_id = workspace(tmp_path)
    html = '<article class="product"><b class="model">TEST-A</b><b class="price">12000</b></article>'
    monkeypatch.setattr(proposals, "collect", lambda source, base, backend:
                        fetched(source, html) if backend == "http" else
                        FetchResult(source.id, "policy_denied", backend, message="Destination denied"))
    result = proposals.propose_source(path, source_id=source_id, output_dir=tmp_path / "p")
    assert result["status"] == "blocked"
    assert result["draft_path"] is None
    assert result["preview_observations"] == []
    assert result["eligible_for_verification"] is False


def test_jsonld_exact_product_is_structured_but_missing_conditions_remain_review(tmp_path, monkeypatch):
    path, source_id = workspace(tmp_path)
    data = {"@type": "Product", "model": "TEST-A", "offers": {"@type": "Offer", "price": "9.99", "priceCurrency": "USD"}}
    html = f'<script type="application/ld+json">{json.dumps(data)}</script>'
    monkeypatch.setattr(proposals, "collect", lambda source, base, backend: fetched(source, html, backend))
    result = proposals.propose_source(path, source_id=source_id, output_dir=tmp_path / "p")
    assert result["method"] == "structured"
    assert result["rules"] == {"row_selector": None, "selectors": {}}
    assert result["status"] == "needs_review"
    assert result["eligible_for_verification"] is False


def test_wrong_identifier_falls_back_once_then_no_model_when_budget_zero(tmp_path, monkeypatch):
    path, source_id = workspace(tmp_path)
    html = '<div><span class="model">OTHER</span><span class="price">10</span></div>'
    calls = []
    monkeypatch.setattr(proposals, "collect", lambda source, base, backend: calls.append(backend) or fetched(source, html, backend))
    monkeypatch.setattr(proposals, "propose_with_ollama", lambda *a, **k: pytest.fail("model called"))
    result = proposals.propose_source(
        path, source_id=source_id, output_dir=tmp_path / "p", model_config_path=tmp_path / "missing.json", max_model_calls=0,
    )
    assert calls == ["http", "playwright"]
    assert result["model_calls"] == 0 and result["draft_path"] is None


def test_policy_denied_is_terminal_and_prior_output_rejected(tmp_path, monkeypatch):
    path, source_id = workspace(tmp_path)
    calls = []
    def deny(source, base, backend):
        calls.append(backend)
        return FetchResult(source.id, "policy_denied", backend, message="<html>secret detail</html>")
    monkeypatch.setattr(proposals, "collect", deny)
    output = tmp_path / "p"
    result = proposals.propose_source(path, source_id=source_id, output_dir=output)
    assert result["status"] == "blocked" and calls == ["http"]
    assert "<html>" not in result["reason"]
    with pytest.raises(FileExistsError):
        proposals.propose_source(path, source_id=source_id, output_dir=output)


def test_model_call_count_includes_failed_http_and_rejects_wrong_identifier(tmp_path, monkeypatch):
    path, source_id = workspace(tmp_path)
    html = '<div class="item"><span class="model">OTHER</span><span class="price">10</span></div>'
    monkeypatch.setattr(proposals, "collect", lambda source, base, backend: fetched(source, html, backend))
    monkeypatch.setattr(proposals, "load_model_config", lambda path: object())
    def fail(*args, **kwargs): raise RuntimeError("HTTP 500 <html>sensitive</html>")
    monkeypatch.setattr(proposals, "propose_with_ollama", fail)
    result = proposals.propose_source(
        path, source_id=source_id, output_dir=tmp_path / "p", model_config_path=tmp_path / "model.json",
    )
    assert result["model_calls"] == 1
    assert result["errors"][0]["type"] == "RuntimeError"
    assert "<html>" not in result["errors"][0]["message"]


def test_comparable_model_selectors_are_eligible_only_after_actual_extraction(tmp_path, monkeypatch):
    path, source_id = workspace(tmp_path)
    html = '''<article class="item"><i class="a">TEST-A</i><i class="b">12000</i>
      <i class="c">KRW</i><i class="d">each</i><i class="e">1</i>
      <i class="f">included</i><i class="g">retail</i><i class="h">each</i><i class="i">A</i></article>'''
    monkeypatch.setattr(proposals, "collect", lambda source, base, backend: fetched(source, html, backend))
    monkeypatch.setattr(proposals, "load_model_config", lambda path: object())
    rules = {"row_selector": ".item", "selectors": {
        "model": ".a", "price": ".b", "currency": ".c", "unit": ".d",
        "pack_quantity": ".e", "tax": ".f", "price_type": ".g",
        "price_basis": ".h", "spec:grade": ".i",
    }}
    monkeypatch.setattr(proposals, "propose_with_ollama", lambda *a, **k: (rules, {"eval_count": 7}))
    result = proposals.propose_source(
        path, source_id=source_id, output_dir=tmp_path / "p", model_config_path=tmp_path / "model.json",
    )
    assert result["method"] == "ollama" and result["model_calls"] == 1
    assert result["preview_observations"][0]["comparable"] is True
    assert result["eligible_for_verification"] is True
    assert result["status"] == "proposed"
    assert "Model-proposed rules require source/sample verification" in result["reason"]


def test_requires_exact_identifier_and_empty_output(tmp_path):
    path = tmp_path / "research.json"
    research.init_workspace(path, industry="Parts", product="Part", market="Korea")
    data = research.add_source(path, url="https://example.com")
    with pytest.raises(ValueError, match="identifiers"):
        proposals.propose_source(path, source_id=data["sources"][0]["id"], output_dir=tmp_path / "p")
    with pytest.raises(ValueError, match="finite"):
        proposals.propose_source(path, source_id=data["sources"][0]["id"], output_dir=tmp_path / "q", timeout_seconds=math.inf)


def test_semantic_rules_never_combine_different_product_containers(tmp_path, monkeypatch):
    path, source_id = workspace(tmp_path)
    html = '''<main><article class="first"><span class="model">TEST-A</span></article>
      <article class="second"><span class="price">999</span></article></main>'''
    monkeypatch.setattr(proposals, "collect", lambda source, base, backend: fetched(source, html, backend))
    result = proposals.propose_source(path, source_id=source_id, output_dir=tmp_path / "p", max_model_calls=0)
    assert result["rules"] is None
    assert result["eligible_for_verification"] is False
