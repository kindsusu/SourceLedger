"""Regression evidence reduced from actual October 16 Gongcar browser captures."""
from pathlib import Path

import pytest
from bs4 import BeautifulSoup

from su_crawler.adapters.gongcar import extract, section_coverage
from su_crawler.models import FetchResult, Source


FIXTURES = Path(__file__).parent / 'fixtures' / 'adapters'


def run(fixture, path, *, html=None, count=None):
    payload = html if html is not None else (FIXTURES / f'gongcar_current_{fixture}.html').read_bytes()
    section = ('monthly_catalog' if path == 'monthly' else
               'daily_catalog' if path == 'daily' else 'popular')
    trace = [{'event': 'list_expansion', 'section': section, 'status': 'complete',
              'observed_count': count, 'click_count': 0}]
    result = FetchResult('gongcar', 'fetched', 'playwright', content=payload,
                         final_url='https://gongcar.kr/' + path, trace=trace)
    source = Source('gongcar', 'Gongcar', 'web', result.final_url, [])
    candidates = extract(result, source)
    return candidates, section_coverage(result, candidates)[0]


@pytest.mark.parametrize('fixture,path,count,price', [
    ('rentpass', 'rentpass', 18, '289000'),
    ('longterm', 'longterm', 20, '167610'),
    ('monthly_rent', 'monthly', 26, '379000'),
    ('daily_rent', 'daily', 7, '190000'),
    ('monthly_rent_38', 'monthly/38', 4, '460000'),
    ('monthly_rent_43', 'monthly/43', 4, '440000'),
    ('monthly_rent_54', 'monthly/54', 4, '480000'),
    ('daily_rent_5', 'daily/5', 1, '190000'),
    ('daily_rent_6', 'daily/6', 1, '200000'),
])
def test_all_nine_retained_legacy_urls_support_current_redirect_targets(fixture, path, count, price):
    candidates, coverage = run(fixture, path, count=count)
    assert len(candidates) == count
    assert coverage['price_count'] == count
    assert candidates[0].fields['price'] == price
    if path == 'longterm':
        assert coverage['expected_count'] == 71
        assert coverage['status'] == 'partial'
        assert coverage['reason'] == 'declared_count_not_reached'
    else:
        assert coverage['status'] == 'complete'


def test_observed_price_conditions_preserve_missing_terms_and_upfront_roles():
    rentpass, _ = run('rentpass', 'rentpass', count=18)
    fields = rentpass[0].fields
    assert fields['deposit_percent'] == '30'
    assert fields['price_age_basis'] == '만 26세'
    assert fields['annual_mileage_condition'] == '연 2만km'
    assert 'term_months' not in fields
    assert 'advance_percent' not in fields
    monthly, _ = run('monthly_rent', 'monthly', count=26)
    assert monthly[0].fields['term_months'] == '24'
    assert monthly[0].fields['price_conditions'] == '만 21세 · 24개월 · 의무보험 포함 기준'
    assert monthly[0].derived_values['price_kind'] == 'from'
    longterm, _ = run('longterm', 'longterm', count=20)
    assert longterm[0].fields['advance_percent'] == '30'
    assert longterm[0].fields['term_months'] == '36'
    assert longterm[2].fields['term_months'] == '48'
    assert longterm[3].fields['observed_source_id'] == 'sorento-hev'
    assert 'deposit_percent' not in longterm[0].fields


def test_hidden_or_unexpanded_landing_cards_cannot_be_complete():
    soup = BeautifulSoup((FIXTURES / 'gongcar_current_rentpass.html').read_bytes(), 'html.parser')
    soup.select_one('#deals > button')['hidden'] = ''
    candidates, coverage = run('rentpass', 'rentpass', html=str(soup).encode(), count=17)
    assert len(candidates) == 17
    assert coverage['status'] == 'partial'
    assert coverage['reason'] == 'hidden_cards_not_expanded'
    soup.select_one('#deals > button').decompose()
    _, coverage = run('rentpass', 'rentpass', html=str(soup).encode(), count=17)
    assert coverage['status'] == 'partial'
    assert coverage['reason'] == 'declared_count_not_reached'


def test_missing_or_ambiguous_price_and_missing_conditions_are_not_inferred():
    soup = BeautifulSoup((FIXTURES / 'gongcar_current_rentpass.html').read_bytes(), 'html.parser')
    soup.select_one('.deal-basis-line').decompose()
    soup.select_one('.deal-price').append(BeautifulSoup('<span> 999,999원</span>', 'html.parser'))
    candidates, coverage = run('rentpass', 'rentpass', html=str(soup).encode(), count=18)
    assert 'price' not in candidates[0].fields
    assert 'term_months' not in candidates[1].fields
    assert 'deposit_percent' not in candidates[1].fields
    assert 'price_conditions_unconfirmed' in candidates[1].review_flags
    assert coverage['status'] == 'partial'
    assert coverage['reason'] == 'unpriced_cards'


def test_missing_basis_is_partial_and_monthly_detail_price_age_is_observed():
    soup = BeautifulSoup((FIXTURES / 'gongcar_current_rentpass.html').read_bytes(), 'html.parser')
    soup.select_one('.deal-basis-line').decompose()
    _, coverage = run('rentpass', 'rentpass', html=str(soup).encode(), count=18)
    assert coverage['status'] == 'partial'
    assert coverage['reason'] == 'price_conditions_unconfirmed'
    candidates, _ = run('monthly_rent_43', 'monthly/43', count=4)
    assert all(c.fields['price_age_basis'] == '만 26세 이상 기준 요금' for c in candidates)


def test_recipe_supports_current_and_legacy_paths_without_clicking_quote_cards():
    from su_crawler.site_collection import _ready_recipe
    assert _ready_recipe('gongcar', 'https://gongcar.kr/monthly') == _ready_recipe('gongcar', 'https://gongcar.kr/monthly-rent')
    assert _ready_recipe('gongcar', 'https://gongcar.kr/daily/5') == _ready_recipe('gongcar', 'https://gongcar.kr/daily-rent/5')
    recipe = _ready_recipe('gongcar', 'https://gongcar.kr/monthly')[0]
    assert recipe['selector'] == 'body'
    assert '#deals > button.deal:not(.deal-all)' in recipe['item_selector']
    assert 'price-page' not in recipe['button_selector']


def test_full_longterm_sheet_all_vehicles_periods_and_payment_conditions():
    candidates, coverage = run('longterm_sheet', 'longterm')
    assert coverage['vehicle_count'] == coverage['expected_vehicle_count'] == 71
    assert coverage['extracted_count'] == 639
    assert coverage['price_count'] == 617
    assert coverage['unavailable_count'] == 22
    assert coverage['status'] == 'complete'
    sorento = [c for c in candidates if c.fields.get('observed_source_id') == 'sorento']
    assert len(sorento) == 9
    assert len({c.fields['item_id'] for c in sorento}) == 9
    assert {c.fields['term_months'] for c in sorento} == {'36', '48', '60'}
    assert {c.fields['payment_condition'] for c in sorento} == {'무보증', '보증금 30%', '선납금 30%'}
    assert all('보험료·자동차세 포함' in c.fields['price_basis_text'] for c in sorento)
    assert all('연 1만km' in c.fields['price_conditions'] for c in sorento)
    assert all('price' not in c.fields for c in candidates if c.fields.get('availability') == '문의')
    assert all('price_kind' not in c.derived_values for c in candidates)


def test_source_ids_stay_stable_after_price_changes_and_duplicates_fall_back():
    candidates, _ = run('rentpass', 'rentpass', count=18)
    soup = BeautifulSoup((FIXTURES / 'gongcar_current_rentpass.html').read_bytes(), 'html.parser')
    soup.select_one('.deal-price').string = '월 299,000원~'
    changed, _ = run('rentpass', 'rentpass', html=str(soup).encode(), count=18)
    assert candidates[0].fields['item_id'] == changed[0].fields['item_id']
    assert changed[0].fields['price'] == '299000'
    assert changed[0].derived_values['price_kind'] == 'from'
    soup.select('#deals > button')[1]['data-id'] = 'morning'
    ambiguous, _ = run('rentpass', 'rentpass', html=str(soup).encode(), count=18)
    assert ambiguous[0].fields['item_id'].startswith('local-observation-')
    assert 'cross_run_identity_uncertain' in ambiguous[0].review_flags


def test_full_sheet_missing_cells_and_unknown_conditions_fail_closed():
    soup = BeautifulSoup((FIXTURES / 'gongcar_current_longterm_sheet.html').read_bytes(), 'html.parser')
    soup.select_one('tbody th').string = '알 수 없는 조건'
    candidates, coverage = run('longterm_sheet', 'longterm', html=str(soup).encode())
    assert len(candidates) == 636
    assert coverage['status'] == 'partial'
    assert coverage['unparsed_count'] == 3


@pytest.mark.parametrize('fixture,path,count,priced,unavailable', [
    ('rentpass_sheet', 'rentpass', 72, 54, 18),
    ('monthly_sheet', 'monthly', 104, 104, 0),
])
def test_other_full_price_sheets_preserve_each_observed_condition(fixture, path, count, priced, unavailable):
    candidates, coverage = run(fixture, path)
    assert len(candidates) == count
    assert coverage['price_count'] == priced
    assert coverage['unavailable_count'] == unavailable
    assert coverage['status'] == 'complete'
    if path == 'rentpass':
        assert all('term_months' not in c.fields for c in candidates)
        minimum = next(c for c in candidates if c.fields['payment_condition'] == '최소 보증금')
        assert 'deposit_amount' not in minimum.fields
        assert 'deposit_percent' not in minimum.fields
        assert minimum.fields['price'] == '379000'
        assert candidates[0].fields['availability'] == '재고문의'
    else:
        assert {c.fields['term_months'] for c in candidates} == {'1', '6', '12', '24'}
        assert all('deposit_amount' not in c.fields and 'deposit_percent' not in c.fields for c in candidates)
