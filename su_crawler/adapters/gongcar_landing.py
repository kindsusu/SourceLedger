"""Observed Gongcar landing-page offers introduced in October 2026."""
from __future__ import annotations

import re

from ..models import Candidate, FetchResult, stable_id
from .common import annotate_evidence, decimal_string, evidence, html_soup, is_visible, visible_text


PRICE = re.compile(r"월\s*([\d,]+)\s*원\s*(~)?")


def matches(soup) -> bool:
    return len(soup.select('section[aria-labelledby="sec-price"] #deals')) == 1


def _section(soup):
    return soup.select_one('section[aria-labelledby="sec-price"]')


def _cards(section):
    return section.select('#deals > button.deal:not(.deal-all)')


def _key(path):
    return "monthly_catalog" if path == "monthly-rent" else f"{path}_popular"


def extract(result: FetchResult, path: str, mode: str, visibility: str) -> list[Candidate]:
    section = _section(html_soup(result.content))
    if section is None or not is_visible(section):
        return []
    basis_nodes = [node for node in section.select('.deal-basis-line') if is_visible(node)]
    basis = visible_text(basis_nodes[0]) if len(basis_nodes) == 1 else ""
    candidates = []
    identifiers = [str(c.get("data-id")) for c in _cards(section) if c.get("data-id")]
    for index, card in enumerate(_cards(section), 1):
        if not is_visible(card):
            continue
        names = [node for node in card.select('.deal-name') if is_visible(node)]
        if len(names) != 1 or not visible_text(names[0]):
            continue
        raw = visible_text(card) or ""
        locator = f"gongcar:{path}:popular:card:{index}"
        item_id = 'local-observation-gongcar-' + stable_id(path, index, raw, basis)
        fields = {"item_id": item_id, "name": visible_text(names[0]), "section": _key(path)}
        proofs = {"item_id": evidence(locator, item_id, raw, proof_kind="local_observation"),
                  "name": evidence(locator + '/name', fields['name'], fields['name'], raw),
                  "section": evidence(locator, _key(path), raw)}

        def add(key, value, source, suffix="conditions"):
            fields[key] = value
            proofs[key] = evidence(locator + '/' + suffix, value, source, raw)

        if card.get('data-id'):
            add('observed_source_id', str(card['data-id']), str(card['data-id']), 'data-id')
        specs = [node for node in card.select('.deal-spec') if is_visible(node)]
        spec = visible_text(specs[0]) if len(specs) == 1 else ""
        if spec:
            add('options', spec, spec, 'spec')
        if basis:
            add('price_conditions', basis, basis)
            age = re.search(r'만\s*\d+\s*세', basis)
            if age:
                add('price_age_basis', age.group(), basis)
            upfront = re.search(r'(보증금|선납금)\s*(\d+(?:\.\d+)?)\s*%', basis)
            if upfront:
                add('deposit_percent' if upfront[1] == '보증금' else 'advance_percent', upfront[2], basis)
            # Preserve the published mileage wording without deriving a numeric limit.
            mileage = re.search(r'연\s*[\d,.]+\s*만?\s*km', basis)
            if mileage:
                add('annual_mileage_condition', mileage.group(), basis)
        terms = re.findall(r'(\d+)\s*개월', spec or "") or re.findall(r'(\d+)\s*개월', basis or "")
        if len(terms) == 1:
            add('term_months', terms[0], spec if re.search(r'\d+\s*개월', spec or "") else basis)
        prices = [visible_text(n) or "" for n in card.select('.deal-price') if is_visible(n)]
        flags = ['cross_run_identity_uncertain']
        derived = {'identity_scope': 'local_observation'}
        public_id = str(card.get('data-id') or '')
        if public_id and identifiers.count(public_id) == 1:
            fields['item_id'] = f'gongcar:{path}:{public_id}'
            proofs['item_id'] = evidence(locator + '/data-id', fields['item_id'], public_id, raw,
                                         proof_kind='source_identity')
            flags = []
            derived = {'identity_scope': 'source'}
        match = PRICE.fullmatch(prices[0]) if len(prices) == 1 else None
        if match and decimal_string(match[1]):
            add('price', decimal_string(match[1]), prices[0], 'price')
            add('currency', 'KRW', prices[0], 'price')
            add('price_basis', 'monthly', prices[0], 'price')
            if match[2]:
                derived['price_kind'] = 'from'
                flags.append('starting_price_not_fixed_quote')
        else:
            flags.append('current_price_role_unresolved')
        if not basis:
            flags.append('price_conditions_unconfirmed')
        annotate_evidence(proofs, mode, source_identity={'item_id', 'section', 'observed_source_id'},
                          displayed_fields=set(proofs) - {'item_id', 'section', 'observed_source_id'})
        candidates.append(Candidate(fields=fields, evidence=proofs, locator=locator,
                                    extraction_method='gongcar_landing_deal_cards', value_origin='observed',
                                    source_visibility=visibility, derived_values=derived,
                                    review_flags=flags, evidence_mode=mode))
    return candidates


def coverage(result: FetchResult, candidates: list[Candidate], path: str) -> list[dict]:
    section = _section(html_soup(result.content))
    key = _key(path)
    cards = _cards(section)
    observed = sum(is_visible(card) for card in cards)
    counts = {int(m[1]) for node in section.select('.deal-all')
              if (m := re.search(r'전체\s*(\d+)\s*개\s*차종', visible_text(node) or ""))}
    expected = next(iter(counts)) if len(counts) == 1 else None
    extracted = [c for c in candidates if c.fields.get('section') == key]
    priced = sum('price' in c.fields for c in extracted)
    events = [e for e in result.trace if e.get('event') == 'list_expansion'
              and e.get('section') in {'popular', key}]
    event = events[-1] if events else {}
    status, reason = 'partial' if observed else 'unknown', 'list_expansion_unconfirmed'
    if observed < len(cards):
        reason = 'hidden_cards_not_expanded'
    elif expected is None:
        reason = 'declared_count_unconfirmed'
    elif observed != expected:
        reason = 'declared_count_not_reached'
    elif len(extracted) != observed:
        reason = 'unparsed_cards'
    elif priced != observed:
        reason = 'unpriced_cards'
    elif any('price_conditions_unconfirmed' in c.review_flags for c in extracted):
        reason = 'price_conditions_unconfirmed'
    elif result.backend == 'playwright' and event.get('status') == 'complete' and event.get('observed_count') == observed:
        status, reason = 'complete', 'declared_cards_parsed'
    return [{'section': key, 'observed_count': observed, 'expected_count': expected,
             'extracted_count': len(extracted), 'price_count': priced,
             'unparsed_count': max(0, observed - len(extracted)), 'status': status,
             'reason': reason, 'click_count': event.get('click_count')}]
