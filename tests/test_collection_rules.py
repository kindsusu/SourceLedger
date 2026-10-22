from pathlib import Path

import pytest

from su_crawler.collection_rules import normalize_rules, apply_rule
from su_crawler.models import Source, FetchResult
from su_crawler import research_plans as plans
from su_crawler.catalog_collection import collect_plan
from su_crawler.storage import Store

URL = 'https://shop.example/product'
RULE = {'recipe': [{'action': 'wait_for', 'selector': '#price'}],
        'selectors': {'name': '#name', 'price': '#price', 'currency': '#currency'}}


def test_rules_are_revision_pinned_and_confirmation_invalidated(tmp_path):
    p = plans.create_plan(tmp_path, 'parts')['plan']
    p = plans.revise_plan(tmp_path, p['id'], p['revision'], {
        'topic': {'industry': 'Parts', 'product': 'Bolt', 'market': 'US'},
        'added_candidates': [{'url': URL}], 'collection_rules': {URL: RULE}})['plan']
    p = plans.confirm_plan(tmp_path, p['id'], p['revision'], True)['plan']
    old = plans.fingerprint(p)
    newer = plans.revise_plan(tmp_path, p['id'], p['revision'], {'collection_rules': {}})['plan']
    assert newer['state'] != 'confirmed'
    assert plans.fingerprint(newer) != old
    assert plans.confirmed_snapshot(tmp_path, p['id'], p['revision'])['collection_rules'] == {URL: RULE}


@pytest.mark.parametrize('rule', [
    {'recipe': [{'action': 'evaluate', 'selector': 'body'}]},
    {'profile_dir': '../outside'}, {'profile_dir': 'C:/profile'},
    {'recipe': [{}]}, {'selectors': {'price': 10}},
])
def test_invalid_rules_rejected(rule):
    with pytest.raises(ValueError):
        normalize_rules({URL: rule}, {URL})


def test_profile_is_existing_scoped_and_not_shared_by_default(tmp_path):
    source = Source('s', 's', 'web', URL, [])
    rules = normalize_rules({URL: {'profile_dir': 'profiles/shop'}}, {URL})
    with pytest.raises(ValueError, match='existing'):
        apply_rule(source, rules, tmp_path)
    (tmp_path / 'profiles/shop').mkdir(parents=True)
    applied = apply_rule(source, rules, tmp_path)
    assert applied.backends == ['playwright'] and applied.account_scope == 'authorized_profile'


def test_catalog_uses_reviewed_rule_without_http_prefetch(tmp_path):
    calls = []
    def fetch(source, base, backend):
        calls.append(backend)
        assert source.selectors == RULE['selectors'] and source.recipe == RULE['recipe']
        return FetchResult(source.id, 'fetched', backend,
            b'<h1 id="name">Bolt</h1><span id="price">12</span><span id="currency">USD</span>', final_url=URL)
    snapshot = {'id': 'rules', 'state': 'confirmed', 'revision': 1,
                'topic': {'industry': 'parts', 'product': 'bolt', 'market': 'US'},
                'candidates': [{'url': URL, 'selected': True}], 'collection_rules': {URL: RULE}}
    result = collect_plan(tmp_path, snapshot, output_dir=tmp_path/'out', max_pages=1,
                          collector=fetch, follow_links=False)
    assert calls == ['playwright']
    store = Store(tmp_path/'out')
    try:
        assert store.observations(result['run_id'])[0]['raw_fields']['price'] == '12'
    finally:
        store.close()


def test_site_pattern_rule_covers_many_pages_and_exact_url_wins():
    from su_crawler.collection_rules import rule_for, rules_for
    lists = [f'https://shop.example/category/{i}' for i in range(3)]
    other = {'selectors': {'name': 'h1', 'price': '.price'}}
    rules = normalize_rules({'site:Shop.example/category/*': RULE, lists[0]: other}, set(lists) | {URL})
    assert set(rules) == {'site:shop.example/category/*', lists[0]}
    assert rule_for(rules, lists[0]) == {'recipe': [], **other}
    assert rule_for(rules, lists[2]) == RULE
    assert rule_for(rules, URL) is None
    # Batches keep only the rules that apply to their own URLs.
    assert set(rules_for(rules, [lists[1]])) == {'site:shop.example/category/*'}
    assert rules_for(rules, [URL]) == {}


@pytest.mark.parametrize('key', ['site:other.example/category/*', 'site:shop.example/nothing/*',
                                 'site:shop.example', 'site:shop.example/a b'])
def test_site_patterns_must_be_wellformed_and_match_a_candidate(key):
    with pytest.raises(ValueError):
        normalize_rules({key: RULE}, {'https://shop.example/category/1'})
