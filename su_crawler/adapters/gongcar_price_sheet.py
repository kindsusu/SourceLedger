"""Only rendered, explicitly labelled full-price-sheet cells are observations."""
from __future__ import annotations

import re

from ..models import Candidate, FetchResult, stable_id
from .common import annotate_evidence, decimal_string, evidence, html_soup, is_visible, visible_text


def sheet(soup):
    nodes = [n for n in soup.select('#sheet-prices') if is_visible(n) and n.select('#pp-list .pp-car')]
    return nodes[0] if len(nodes) == 1 else None


def matches(soup):
    return sheet(soup) is not None


def extract(result: FetchResult, path: str, mode: str, visibility: str) -> list[Candidate]:
    modal = sheet(html_soup(result.content))
    if modal is None:
        return []
    meta = visible_text(modal.select_one('#pp-meta')) or ''
    foot = visible_text(modal.select_one('#pp-foot')) or ''
    candidates = []
    identifiers = [str(n.get('data-id')) for n in modal.select('#pp-list > .pp-car button[data-id]')]
    for car_index, car in enumerate(modal.select('#pp-list > .pp-car'), 1):
        if not is_visible(car):
            continue
        names = car.select('.pp-car-head b')
        tables = car.select('table.pp-table')
        if len(names) != 1 or len(tables) != 1:
            continue
        name = visible_text(names[0]) or ''
        spec = visible_text(car.select_one('.pp-car-head small')) or ''
        control = car.select_one('button[data-id]')
        source_id = str(control.get('data-id')) if control else None
        if source_id and identifiers.count(source_id) != 1:
            source_id = None
        headers = tables[0].select('thead th')
        header = visible_text(headers[0]) if headers else ''
        matrix = header == '조건'
        if not name or len(headers) < 2 or header not in {'조건', '보증금', '이용 기간'}:
            continue
        terms = [re.fullmatch(r'(\d+)\s*개월', visible_text(h) or '') for h in headers[1:]] if matrix else [None]
        if matrix and not all(terms):
            continue
        for row_index, row in enumerate(tables[0].select('tbody tr'), 1):
            label = visible_text(row.find('th')) or ''
            row_term = re.fullmatch(r'(\d+)\s*개월', label) if header == '이용 기간' else None
            if header == '이용 기간':
                if not row_term:
                    continue
            elif not re.fullmatch(r'무보증|최소 보증금|보증금\s*0원|(?:보증금|선납금)\s*\d+(?:\.\d+)?%', label):
                continue
            cells = row.find_all('td', recursive=False)
            if len(cells) != len(terms):
                continue
            for column, (cell, term) in enumerate(zip(cells, terms), 1):
                if not is_visible(cell):
                    continue
                raw = visible_text(cell) or ''
                amount = re.fullmatch(r'([\d,]+)\s*(원)?', raw)
                if not (amount or raw in {'문의', '재고문의'}):
                    continue
                locator = f'gongcar:{path}:sheet:car:{car_index}:row:{row_index}:column:{column}'
                term_value = term[1] if term else row_term[1] if row_term else None
                local_id = 'local-observation-gongcar-' + stable_id(path, car_index, name, spec, label, term_value)
                item_id = f'gongcar:{path}:{source_id}:term={term_value or ""}:basis={label}' if source_id else local_id
                fields = {'item_id': item_id, 'name': name, 'section': path.replace('-rent','') + '_price_sheet',
                          'row_condition': label, 'price_conditions': foot}
                if header != '이용 기간':
                    fields['payment_condition'] = label
                if term_value:
                    fields['term_months'] = term_value
                proofs = {key: evidence(locator, value, {'name': name, 'term_months': visible_text(headers[column]) if matrix else label,
                          'payment_condition': label, 'row_condition': label, 'price_conditions': foot}.get(key, name), raw)
                          for key, value in fields.items()}
                proofs['item_id'] = evidence(locator, item_id, source_id or name,
                                            proof_kind='source_identity' if source_id else 'local_observation')
                if spec:
                    fields['options'] = spec
                    proofs['options'] = evidence(locator + '/spec', spec, spec)
                if meta:
                    fields['price_basis_text'] = meta
                    proofs['price_basis_text'] = evidence(locator + '/meta', meta, meta)
                if source_id:
                    fields['observed_source_id'] = source_id
                    proofs['observed_source_id'] = evidence(locator + '/data-id', source_id, source_id, proof_kind='source_identity')
                match = re.fullmatch(r'(보증금|선납금)\s*(\d+(?:\.\d+)?)%', label)
                if match:
                    key = 'deposit_percent' if match[1] == '보증금' else 'advance_percent'
                    fields[key] = match[2]
                    proofs[key] = evidence(locator + '/condition', match[2], label)
                zero_deposit = re.fullmatch(r'보증금\s*(0)원', label)
                if zero_deposit:
                    fields['deposit_amount'] = zero_deposit[1]
                    proofs['deposit_amount'] = evidence(locator + '/condition', zero_deposit[1], label)
                age = re.search(r'만\s*\d+\s*세(?:\s*이상)?', foot)
                if age:
                    fields['price_age_basis'] = age.group()
                    proofs['price_age_basis'] = evidence(locator + '/footnote', age.group(), foot)
                mileage = re.search(r'연\s*[\d,.]+\s*만?\s*km', foot)
                if mileage:
                    fields['annual_mileage_condition'] = mileage.group()
                    proofs['annual_mileage_condition'] = evidence(locator + '/footnote', mileage.group(), foot)
                if amount and ('원' in meta or amount[2]) and '월 렌트료' in meta:
                    fields.update(price=decimal_string(amount[1]), currency='KRW', price_basis='monthly')
                    proofs.update(price=evidence(locator, fields['price'], raw),
                                  currency=evidence(locator + '/meta', 'KRW', meta if '원' in meta else raw),
                                  price_basis=evidence(locator + '/meta', 'monthly', meta))
                else:
                    fields['availability'] = raw
                    proofs['availability'] = evidence(locator, raw, raw)
                annotate_evidence(proofs, mode, source_identity={'item_id', 'section', 'observed_source_id'},
                                  displayed_fields=set(proofs) - {'item_id', 'section', 'observed_source_id'})
                candidates.append(Candidate(fields=fields, evidence=proofs, locator=locator,
                                  extraction_method='gongcar_rendered_price_sheet', value_origin='observed',
                                  source_visibility=visibility, evidence_mode=mode,
                                  derived_values={'identity_scope': 'source' if source_id else 'local_observation'},
                                  review_flags=([] if source_id else ['cross_run_identity_uncertain'])
                                  + ([] if foot and meta else ['price_conditions_unconfirmed'])))
    return candidates


def coverage(result, candidates, path):
    modal = sheet(html_soup(result.content))
    cars = [c for c in modal.select('#pp-list > .pp-car') if is_visible(c)]
    declared = re.search(r'전\s*(\d+)개\s*차종', visible_text(modal.select_one('#pp-meta')) or '')
    expected_cars = int(declared[1]) if declared else None
    expected_cells = sum(len(row.find_all('td', recursive=False)) for c in cars for row in c.select('tbody tr'))
    visible_cells = sum(is_visible(cell) for c in cars for cell in c.select('tbody td'))
    extracted = [c for c in candidates if c.extraction_method == 'gongcar_rendered_price_sheet']
    priced = sum('price' in c.fields for c in extracted)
    unavailable = sum(c.fields.get('availability') in {'문의', '재고문의'} for c in extracted)
    layouts = {(tuple(visible_text(h) for h in c.select('thead th')),
                tuple(visible_text(h) for h in c.select('tbody th'))) for c in cars}
    complete = (result.backend == 'playwright' and expected_cars == len(cars)
                and len(layouts) == 1 and expected_cells > 0
                and expected_cells == visible_cells == len(extracted) and priced + unavailable == expected_cells)
    complete = complete and not any('price_conditions_unconfirmed' in c.review_flags for c in extracted)
    return [{'section': path.replace('-rent','') + '_price_sheet', 'observed_count': visible_cells,
             'expected_count': expected_cells, 'expected_vehicle_count': expected_cars, 'vehicle_count': len(cars),
             'extracted_count': len(extracted), 'price_count': priced, 'unavailable_count': unavailable,
             'unparsed_count': max(0, expected_cells - len(extracted)),
             'status': 'complete' if complete else 'partial',
             'reason': 'visible_price_sheet_parsed' if complete else 'price_sheet_incomplete'}]
