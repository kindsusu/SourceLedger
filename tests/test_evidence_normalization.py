"""Replaying explicit source-text conversions must not relax evidence checks."""
from copy import deepcopy
import hashlib

from su_crawler.evidence_normalization import evidence_attests
from su_crawler.models import Candidate, CollectionConfig, Product, Source
from su_crawler.validation import validate


def _check(candidate):
    return validate(
        candidate, Product("car", "Car", price_profile="rental"),
        Source("dealer", "Dealer", "web", "https://example.test", ["car"]),
        run_id="run", task_id="task", evidence_path="evidence.json", evidence_sha256="hash",
        collected_at="2026-01-01T00:00:00+00:00", source_url="https://example.test/car",
    )


def _full_candidate():
    conditions = {
        "currency": "KRW", "price_basis": "monthly", "term_months": 84,
        "deposit_amount": "7710000", "advance_amount": "0",
        "annual_mileage_km": 10000, "trim": "Base", "options": "none",
        "condition": "new", "insurance": "included", "tax": "included",
        "availability": "in_stock",
    }
    fields = {"price": "7710000", "currency": "KRW", "rental_conditions": conditions}
    evidence = {name: {"location": "quote", "raw": deepcopy(value), "display_state": "visible"}
                for name, value in fields.items()}
    evidence["price"].update(raw="7,710,000원", normalization={"rule": "korean_won_amount"})
    evidence["currency"].update(raw="원", normalization={"rule": "korean_won_currency"})
    proof = evidence["rental_conditions"]
    proof["raw"].update(currency="원", term_months="84개월", deposit_amount="7,710,000원",
                        annual_mileage_km="10,000km", tax="부가세 포함")
    proof["normalization"] = {"fields": {
        "currency": "korean_won_currency", "term_months": "korean_months",
        "deposit_amount": "korean_won_amount", "annual_mileage_km": "kilometers",
        "tax": "korean_vat_included",
    }}
    return Candidate(fields, evidence, "offer", "test", source_visibility="visible", evidence_mode="rendered_dom")


def test_declared_rules_replay_only_allowed_full_source_tokens():
    assert evidence_attests("price", "7,710,000원", "7710000", {"rule": "korean_won_amount"})
    assert evidence_attests("term_months", "84개월", 84, {"rule": "korean_months"})
    assert evidence_attests("currency", "원", "KRW", {"rule": "korean_won_currency"})
    assert evidence_attests("tax", "부가세 포함", "included", {"rule": "korean_vat_included"})
    assert evidence_attests("tax", "부가세 별도", "excluded", {"rule": "korean_vat_excluded"})
    assert evidence_attests("annual_mileage_km", "10,000km", 10000, {"rule": "kilometers"})
    for raw in ("7,710,000원부터", "월 7,710,000원", "7,71,000원", "7,710,000 달러"):
        assert not evidence_attests("price", raw, "7710000", {"rule": "korean_won_amount"})
    assert not evidence_attests("term_months", "84개월", 48, {"rule": "korean_months"})
    assert not evidence_attests("tax", "부가세 별도", "included", {"rule": "korean_vat_included"})
    assert not evidence_attests("tax", "부가세 포함", "excluded", {"rule": "korean_vat_excluded"})
    assert not evidence_attests("annual_mileage_km", "10,00km", 10000, {"rule": "kilometers"})
    assert not evidence_attests("currency", "¥", "KRW", {"rule": "korean_won_currency"})
    assert not evidence_attests("currency", "원/USD", "KRW", {"rule": "korean_won_currency"})


def test_rule_misuse_or_unknown_rule_does_not_prove_value():
    assert not evidence_attests("tax", "84개월", "84", {"rule": "korean_months"})
    assert not evidence_attests("term_months", "10,000km", 10000, {"rule": "kilometers"})
    assert not evidence_attests("spec:price", "7,710,000원", "7710000", {"rule": "korean_won_amount"})
    assert not evidence_attests("price", "7,710,000원", "7710000", {"rule": "unknown"})
    assert not evidence_attests("price", "7,710,000원", "7710000", {"rule": "korean_won_amount", "fallback": "guess"})
    assert not evidence_attests("price", "7,710,000원", "7710000")


def test_normalized_rental_evidence_verifies_with_original_preserved():
    candidate = _full_candidate()
    observation = _check(candidate)
    assert observation.status == "verified" and observation.comparable
    assert observation.amount == "7710000"
    assert observation.rental_conditions["term_months"] == 84
    assert observation.evidence["price"]["raw"] == "7,710,000원"
    assert observation.evidence["rental_conditions"]["raw"]["tax"] == "부가세 포함"


def test_explicit_vat_exclusion_is_replayed_without_guessing():
    candidate = _full_candidate()
    candidate.fields["rental_conditions"]["tax"] = "excluded"
    proof = candidate.evidence["rental_conditions"]
    proof["raw"]["tax"] = "부가세 별도"
    proof["normalization"]["fields"]["tax"] = "korean_vat_excluded"
    observation = _check(candidate)
    assert observation.status == "verified" and observation.comparable
    assert observation.rental_conditions["tax"] == "excluded"


def test_wrong_value_rule_or_missing_condition_cannot_compare():
    value = _full_candidate()
    value.evidence["rental_conditions"]["raw"]["term_months"] = "48개월"
    assert _check(value).status == "review"
    assert not _check(value).comparable

    value = _full_candidate()
    value.evidence["price"]["normalization"] = {"rule": "korean_months"}
    assert _check(value).status == "review"

    value = _full_candidate()
    value.fields["rental_conditions"].pop("insurance")
    value.evidence["rental_conditions"]["raw"].pop("insurance")
    observation = _check(value)
    assert not observation.comparable
    assert "rental comparison condition missing, invalid, or unproven: insurance" in observation.reason


def test_activation_replays_normalization_from_same_evidence_bytes(tmp_path, monkeypatch):
    import su_crawler.extraction as extraction
    from su_crawler.activation import _evidence_problem

    candidate = _full_candidate()
    # This synthetic extractor represents deterministic parsing of these
    # bytes. Activation must still compare the re-extracted values and rules.
    def replay(fetch, _source):
        return [deepcopy(candidate)] if b"7,710,000" in fetch.content else []

    monkeypatch.setattr(extraction, "extract", replay)
    candidate.evidence_mode = "unknown"
    product = Product("car", "Car", price_profile="rental")
    source = Source("dealer", "Dealer", "web", "https://example.test/car", ["car"])
    config = CollectionConfig("test", [product], [source], str(tmp_path), str(tmp_path))
    content = tmp_path / "quote.html"
    content.write_bytes("7,710,000원 / 84개월 / 10,000km".encode())
    digest = hashlib.sha256(content.read_bytes()).hexdigest()
    row = validate(candidate, product, source, run_id="run", task_id="task", evidence_path=str(content),
                   evidence_sha256=digest, collected_at="2026-01-01T00:00:00+00:00",
                   source_url="https://example.test/car").to_dict()
    assert _evidence_problem(row, config) is None
    tampered = deepcopy(row)
    tampered["evidence"]["price"]["normalization"]["rule"] = "korean_months"
    assert _evidence_problem(tampered, config) == "stored observation does not re-extract from source evidence"
