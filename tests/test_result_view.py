from copy import deepcopy

from su_crawler.result_view import build_result_view


def offer(term, **fields):
    return {"id": "offer-" + term, "source_url": "https://shop.example/car", "amount": "680000", "currency": "KRW",
            "status": "review", "price_profile": "rental", "extraction_method": "adapter", "comparable": False,
            "raw_fields": {"name": "Car", "price": "680000", "term_months": term, "deposit_amount": "680000", **fields}}


def evidence(**fields):
    return {"id": "proof", "source_url": "https://shop.example/car", "extraction_method": "host_browser_submission",
            "raw_fields": {"name": "Car", "price": "680,000 원", **fields}}


def test_three_terms_one_supporting_capture_no_inferred_values():
    rows = [offer("36"), offer("48"), offer("60"), evidence(term_months="60 개월", deposit="68 만원")]
    original = deepcopy(rows)
    view = build_result_view(rows)
    assert view["result_count"] == 3 and view["linked_evidence_count"] == 1 and not view["evidence_submissions"]
    linked = next(row for row in view["rows"] if row["id"] == "offer-60")
    assert len(linked["supporting_evidence"]) == 1 and not linked["conflicts"]
    assert linked["field_status"]["insurance"]["status"] == "missing"
    assert "insurance" not in linked["raw_fields"]
    assert rows == original and linked["status"] == "review"


def test_ambiguous_option_and_wrong_product_never_merge():
    assert build_result_view([offer("36"), offer("60"), evidence()])["unlinked_evidence_count"] == 1
    assert build_result_view([offer("60", options="A"), evidence(term_months="60", option="B")])["unlinked_evidence_count"] == 1
    assert build_result_view([offer("60"), evidence(name="Other")])["unlinked_evidence_count"] == 1


def test_conflicting_price_remains_visible_and_never_overwrites_source():
    row = offer("60")
    row.update(status="verified", comparable=True, comparison_key="group")
    view = build_result_view([row, evidence(term_months="60 개월", price="700000")])
    result = view["rows"][0]
    assert result["amount"] == "680000" and result["raw_fields"]["price"] == "680000"
    assert result["result_status"] == "conflict" and not result["comparable"]
    assert result["field_status"]["price"]["status"] == "conflict"
    assert result["conflicts"][0]["submitted"] == "700000"
