"""Independent, industry-neutral coverage and retained-evidence regressions."""
import json
import hashlib

import pytest

from su_crawler import catalog_collection as catalog
from su_crawler.coverage import page_status, summarize
from su_crawler.models import FetchResult
from su_crawler.site_collection import _sections
from su_crawler.storage import Store


def plan(url):
    return {'id': 'coverage-test', 'revision': 1, 'state': 'confirmed',
            'topic': {'industry': 'retail', 'product': 'clothing', 'market': 'public'},
            'candidates': [{'url': url, 'name': 'Shirt', 'selected': True}]}


def product(sku, price):
    return {'@type': 'Product', 'name': 'Shirt', 'sku': sku,
            'offers': {'@type': 'Offer', 'price': price, 'priceCurrency': 'USD'}}


def document(value, extra=''):
    return (f'<script type="application/ld+json">{json.dumps(value)}</script>' + extra).encode()


def test_metadata_price_does_not_prove_displayed_option_coverage(tmp_path):
    url = 'https://store.example/shirt'
    html = document(product('M', '10'), '<table>' + ''.join(
        f'<tr><td>Option {i}</td><td>{10+i} USD</td></tr>' for i in range(5)) + '</table>')
    def fetch(source, base, backend):
        return FetchResult(source.id, 'fetched', backend, html, final_url=url)
    result = catalog.collect_plan(tmp_path, plan(url), output_dir=tmp_path / 'out',
                                  max_pages=1, follow_links=False, collector=fetch)
    assert result['status'] == 'needs_review'
    assert result['coverage_summary'] == {'complete': 0, 'partial': 0, 'unknown': 1, 'excluded': 0}
    section = result['coverage'][0]['sections'][0]
    assert section['status'] == 'unknown' and section['observed_count'] is None
    store = Store(tmp_path / 'out')
    try:
        assert store.run_coverage(result['run_id'])[0]['coverage_status'] == 'unknown'
    finally:
        store.close()


def test_product_after_100_navigation_links_is_discovered_and_larger_limit_is_explicit():
    url = 'https://store.example/list'
    links = ''.join(f'<a href="/menu/{i}">Menu</a>' for i in range(100))
    result = FetchResult('s', 'fetched', 'http', (links+'<a href="/product/last">Product</a>').encode())
    assert 'https://store.example/product/last' in catalog._links(result, url, 'store.example', set())
    result.content = ''.join(f'<a href="/product/{i}">Product</a>' for i in range(1001)).encode()
    assert len(catalog._links(result, url, 'store.example', set())) == 1000
    assert any(s['status'] == 'partial' for s in _sections(result, [], None))


def test_jsonld_limit_becomes_partial_coverage():
    result = FetchResult('s', 'fetched', 'http', document([product(str(i), '10') for i in range(1001)]))
    candidates = catalog._jsonld_candidates(result)
    assert len(candidates) == 1000
    sections = _sections(result, candidates, None)
    assert page_status({'status': 'visited', 'sections': sections}) == 'partial'


def test_changed_rules_reanalyse_without_fetch_and_preserve_original_run(tmp_path, monkeypatch):
    url = 'https://store.example/shirt'
    html = document([product('M', '10'), product('L', '12')])
    calls = []
    def fetch(source, base, backend):
        calls.append(backend)
        return FetchResult(source.id, 'fetched', backend, html, final_url=url,
                           fetched_at='2026-10-01T00:00:00+00:00')
    original = catalog._jsonld_candidates
    monkeypatch.setattr(catalog, 'collection_rules_version', lambda: 'old-rules')
    monkeypatch.setattr(catalog, '_jsonld_candidates', lambda result: original(result)[:1])
    options = dict(output_dir=tmp_path / 'out', max_pages=1, follow_links=False,
                   checkpoint_id='replay', collector=fetch)
    first = catalog.collect_plan(tmp_path, plan(url), **options)
    calls.clear()
    assert catalog.collect_plan(tmp_path, plan(url), **options) == first
    assert calls == []
    monkeypatch.setattr(catalog, 'collection_rules_version', lambda: 'new-rules')
    monkeypatch.setattr(catalog, '_jsonld_candidates', original)
    second = catalog.collect_plan(tmp_path, plan(url), **options)
    assert calls == [] and second['reanalysed_retained_evidence'] is True
    assert first['run_id'] != second['run_id']
    store = Store(tmp_path / 'out')
    try:
        old = store.observations(first['run_id'])
        new = store.observations(second['run_id'])
        assert len(old) == 1 and len(new) == 2
        assert {row['raw_fields']['sku'] for row in new} == {'M', 'L'}
        assert all(row['collected_at'] == old[0]['collected_at'] for row in new)
        assert all(row['evidence_sha256'] == old[0]['evidence_sha256'] for row in new)
    finally:
        store.close()


def test_legacy_pages_and_failures_are_not_counted_as_complete():
    assert summarize([{'status': 'visited'}, {'status': 'timeout'}, {'status': 'excluded'}]) == {
        'complete': 0, 'partial': 1, 'unknown': 1, 'excluded': 1}


@pytest.mark.parametrize('difference', ['price', 'evidence', 'missing', 'none'])
def test_final_ledger_must_match_values_and_source_evidence(tmp_path, monkeypatch, difference):
    from su_crawler import site_collection, report
    from su_crawler.models import Candidate
    result = FetchResult('s', 'fetched', 'playwright', b'<html>captured quote</html>')
    fields = {'section': 'monthly_detail', 'price': '100', 'term_months': '3'}
    candidate = Candidate(fields=fields, evidence={}, locator='offer:1', extraction_method='fixture')
    monkeypatch.setattr('su_crawler.adapters.gongcar.section_coverage',
                        lambda *args: [{'section': 'monthly_detail', 'status': 'complete'}])
    sections = site_collection._sections(result, [candidate], 'gongcar')
    assert sections[0]['extracted_locators'] == ['offer:1']
    row = {'source_id': 's', 'locator': 'offer:1', 'raw_fields': dict(fields),
           'evidence_sha256': hashlib.sha256(result.content).hexdigest()}
    if difference == 'price':
        row['raw_fields']['price'] = '200'
    elif difference == 'evidence':
        row['evidence_sha256'] = hashlib.sha256(b'different page').hexdigest()
    class Ledger:
        def __init__(self, *args): pass
        def observations(self, *args): return [] if difference == 'missing' else [row]
        def save_run_coverage(self, *args): pass
        def close(self): pass
    monkeypatch.setattr(site_collection, 'Store', Ledger)
    monkeypatch.setattr('su_crawler.collection_issues.sync_coverage_issues', lambda *args: [])
    monkeypatch.setattr(report, 'append_section_coverage', lambda *args: None)
    coverage = [{'source_id': 's', 'status': 'visited', 'sections': sections}]
    site_collection._save_coverage(tmp_path, {'id': 'r', 'report_path': 'unused'}, coverage)
    assert coverage[0]['coverage_status'] == ('complete' if difference == 'none' else 'partial')
    assert coverage[0]['coverage_gap'] == (difference != 'none')
