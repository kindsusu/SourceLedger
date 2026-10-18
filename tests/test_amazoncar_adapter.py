"""Amazoncar replay is tied to displayed source tokens, not prior quote JSON."""
from __future__ import annotations

from su_crawler.adapters.amazoncar import extract
from su_crawler.models import FetchResult, Product, Source
from su_crawler.validation import validate


DETAIL_URL = "https://www.amazoncar.co.kr/cars/new/007303/10"
SHORT_URL = "https://www.amazoncar.co.kr/rentPrice"


def _detail(*, mode: str = "rent", first_price: str = "357,720원", duplicate: bool = False,
            include_price: bool = True, fixed_age: bool = False) -> str:
    rent_class = "_active_qx_1" if mode == "rent" or duplicate else ""
    lease_class = "_active_qx_1" if mode == "lease" or duplicate else ""
    price = f'<div><span class="_price_qx_1">{first_price}</span></div>' if include_price else ""
    age = ('<div class="_title-row_x"><span class="_title_x">운전자 연령</span>'
           '<div><span class="_default-text_x">만 26세 이상</span></div></div>'
           '<span class="_default-text_x">만 26세 이상</span>' if fixed_age else
           '<div class="_title-row_x"><span class="_title_x">운전자 연령</span></div>'
           '<div class="_choice-box-with-info_x"><button class="_active_x">만 26세 이상</button></div>')
    return f"""<html><body><h1 class="_name_qx_1">베뉴 플럭스</h1>
    <div class="_default-box_x"><div class="_title-row_x"><span class="_title_x">보증금</span></div>
      <div class="_choice-box-with-info_x"><button class="_active_x"><span class="_price_x">3,660,000원</span><span>15%</span></button></div></div>
    <div class="_default-box_x"><div class="_title-row_x"><span class="_title_x">선납금</span></div>
      <div class="_choice-box-with-info_x"><button class="_active_x"><span class="_price_x">0원</span><span>0%</span></button></div></div>
    <div class="_default-box_x">{age}</div>
    <div class="_default-box_x"><div class="_title-row_x"><span class="_title_x">연간 약정운행거리</span></div>
      <div class="_choice-box-with-info_x"><button class="_active_x">10,000km</button></div></div>
    <div class="_choice-rent-lease_x"><button class="{rent_class}">장기렌트</button><button class="{lease_class}">리스</button></div>
    <div class="_choice-box_x"><p class="_title_x">대여 개월을 선택하세요</p><div>
      <button><span>82개월</span>{price}</button>
      <button><span>72개월</span><div><span class="_price_x">373,120원</span></div></button>
    </div><p class="_info_x">* 부가세 포함</p></div>
    <script>const falseQuote = '99개월 1,000원';</script>
    </body></html>"""


def _extract(html: str, url: str = DETAIL_URL, backend: str = "playwright"):
    source = Source("s", "Amazoncar", "web", url, ["p"], adapter="amazoncar")
    result = FetchResult("s", "fetched", backend, html.encode("utf-8"), final_url=url)
    return extract(result, source), source


def test_rent_and_lease_from_active_rendered_state_and_original_field_evidence():
    rent, source = _extract(_detail())
    lease, _ = _extract(_detail(mode="lease"))
    assert [c.fields["term_months"] for c in rent] == ["82", "72"]
    assert [c.fields["price"] for c in rent] == ["357720", "373120"]
    assert all(c.fields["contract_mode"] == "장기렌트" for c in rent)
    assert all(c.fields["contract_mode"] == "리스" for c in lease)
    assert rent[0].fields["item_id"] == "new/007303/10"
    assert rent[0].fields["name"] == "베뉴 플럭스"
    assert rent[0].evidence["name"]["raw"] == "베뉴 플럭스"
    assert rent[0].fields["deposit_amount"] == "3660000"
    assert rent[0].fields["advance_amount"] == "0"
    assert rent[0].fields["annual_mileage_km"] == "10000"
    assert rent[0].evidence["annual_mileage_km"]["raw"] == "10,000km"
    assert rent[0].evidence["price"]["raw"] == "357,720원"
    assert rent[0].evidence["price"]["normalization"] == {"rule": "korean_won_amount"}
    assert rent[0].evidence["term_months"]["raw"] == "82개월"
    assert rent[0].evidence["currency"]["raw"] == "원"
    assert "price_basis" not in rent[0].fields
    obs = validate(rent[0], Product("p", "venue", identifiers={"item_id": "new/007303/10"},
                                    price_profile="rental"), source, run_id="r", task_id="t",
                   evidence_path="e", evidence_sha256="h", collected_at="2026-10-17T00:00:00Z",
                   source_url=DETAIL_URL)
    assert obs.status == "verified"
    assert obs.amount == "357720" and obs.currency == "KRW"
    assert not obs.comparable  # Billing frequency is not bound to the quote.
    used_url = "https://www.amazoncar.co.kr/cars/used/059898"
    used, _ = _extract(_detail(), used_url)
    assert used[0].fields["item_id"] == "used/059898"


def test_replay_changes_with_source_and_rejects_unparsed_or_ambiguous_controls():
    updated, _ = _extract(_detail(first_price="456,780원"))
    assert updated[0].fields["price"] == "456780"
    assert updated[0].evidence["price"]["raw"] == "456,780원"
    missing, _ = _extract(_detail(include_price=False))
    assert "price" not in missing[0].fields
    assert "term_without_numeric_price" in missing[0].review_flags
    ambiguous, _ = _extract(_detail(duplicate=True))
    assert ambiguous == []
    hidden, _ = _extract(_detail().replace("_price_qx_1\">357,720원", "_price_qx_1\" hidden>357,720원"))
    assert "price" not in hidden[0].fields


def test_static_html_does_not_prove_browser_visibility():
    candidates, _ = _extract(_detail(), backend="http")
    assert candidates[0].source_visibility == "unconfirmed"
    assert candidates[0].evidence["price"]["display_state"] == "unconfirmed"


def test_used_fixed_age_text_is_evidence_when_displayed_copies_agree():
    values, _ = _extract(_detail(fixed_age=True), "https://www.amazoncar.co.kr/cars/used/051037")
    assert values[0].fields["driver_age_condition"] == "만 26세 이상"
    assert "ambiguous_selected_condition:운전자 연령" not in values[0].review_flags
    conflicting = _detail(fixed_age=True).replace(
        '<span class="_default-text_x">만 26세 이상</span></div></div>',
        '<span class="_default-text_x">만 21세 이상</span></div></div>',
    )
    values, _ = _extract(conflicting, "https://www.amazoncar.co.kr/cars/used/051037")
    assert "driver_age_condition" not in values[0].fields
    assert "ambiguous_selected_condition:운전자 연령" in values[0].review_flags


def test_capture_integrity_trace_keeps_unchanged_mode_amounts_in_review():
    source = Source("used-051037-lease", "Amazoncar", "web", DETAIL_URL, ["p"], adapter="amazoncar")
    result = FetchResult(source.id, "fetched", "playwright", _detail(mode="lease").encode("utf-8"),
                         final_url=DETAIL_URL,
                         trace=[{"event": "amazoncar_capture_integrity", "flags": [
                             "identical_amounts_after_network_quiet_review",
                             "identical_amounts_after_network_quiet_review",
                             "unrelated_flag",
                         ]}])
    candidates = extract(result, source)
    assert len(candidates) == 2
    assert all(c.review_flags == ["identical_amounts_after_network_quiet_review"] for c in candidates)
    observation = validate(candidates[0], Product("p", "venue", price_profile="rental"), source,
                           run_id="r", task_id="t", evidence_path="e", evidence_sha256="h",
                           collected_at="2026-10-18T00:00:00Z", source_url=DETAIL_URL)
    assert observation.status == "review"
    assert observation.amount == "357720"


def test_capture_integrity_trace_without_review_flag_keeps_verified_observation():
    source = Source("used-051037-rent", "Amazoncar", "web", DETAIL_URL, ["p"], adapter="amazoncar")
    result = FetchResult(source.id, "fetched", "playwright", _detail().encode("utf-8"),
                         final_url=DETAIL_URL,
                         trace=[{"event": "amazoncar_capture_integrity", "flags": []},
                                {"event": "other", "flags": ["no_displayed_quotes_review"]}])
    candidate = extract(result, source)[0]
    assert candidate.review_flags == []
    observation = validate(candidate, Product("p", "venue", price_profile="rental"), source,
                           run_id="r", task_id="t", evidence_path="e", evidence_sha256="h",
                           collected_at="2026-10-18T00:00:00Z", source_url=DETAIL_URL)
    assert observation.status == "verified"


def test_short_term_tariff_expands_rowspan_retains_duplicate_and_no_currency_guess():
    headers = "".join(f"<td>{duration}</td>" for duration in
                      ("12시간", "1~2일", "3~4일", "5~6일", "7일 이상"))
    prices = "<td>76,000</td><td>95,000</td><td>86,000</td><td>81,000</td><td>76,000</td>"
    html = f"""<html><body><p>부가세 별도</p><table><tbody>
    <tr><td>구분</td><td>차종</td>{headers}</tr>
    <tr><td rowspan="2">소형</td><td>모닝, 레이</td>{prices}</tr>
    <tr><td>모닝, 레이</td>{prices}</tr>
    </tbody></table></body></html>"""
    values, _ = _extract(html, SHORT_URL)
    assert len(values) == 10
    assert all(values[0].fields[key] == expected for key, expected in {
        "price": "76,000", "item_id": "모닝, 레이", "vehicle_class": "모닝, 레이",
        "tariff_category": "소형", "rental_duration": "12시간", "tax": "excluded",
    }.items())
    assert values[0].locator != values[5].locator
    assert values[5].evidence["tariff_category"]["raw"] == "소형"
    assert "currency" not in values[0].fields
    assert "price_basis" not in values[0].fields
