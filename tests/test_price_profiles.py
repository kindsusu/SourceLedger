import json

import pytest

from su_crawler.config import load_config
from su_crawler.models import Candidate, Product, Source
from su_crawler.validation import validate


def rental_conditions(**changes):
    values = {
        "currency": "KRW",
        "price_basis": "monthly",
        "term_months": 36,
        "deposit_amount": "0",
        "advance_amount": "0",
        "annual_mileage_km": 20000,
        "trim": "Long Range",
        "options": "none",
        "condition": "new",
        "insurance": "excluded",
        "tax": "included",
        "availability": "in_stock",
    }
    values.update(changes)
    return values


def make_candidate(*, conditions=None, price="590000", origin="observed", visibility="visible", derived=None, flags=None):
    fields = {"currency": "KRW"}
    if price is not None:
        fields["price"] = price
    if conditions is not None:
        fields["rental_conditions"] = conditions
    evidence = {key: {"location": "synthetic", "raw": value} for key, value in fields.items()}
    return Candidate(
        fields, evidence, "offer-1", "test", value_origin=origin,
        source_visibility=visibility, derived_values=derived or {}, review_flags=flags or [],
    )


def check(value, product=None):
    return validate(
        value,
        product or Product("car", "Car", price_profile="rental"),
        Source("dealer", "Dealer", "web", "https://example.test", ["car"]),
        run_id="run", task_id="task", evidence_path="evidence.json", evidence_sha256="hash-only",
        collected_at="2026-01-01T00:00:00+00:00", source_url="https://example.test/car",
    )


def test_estimate_is_separate_from_observed_amount_and_never_verified():
    observation = check(make_candidate(
        conditions=rental_conditions(), price=None, origin="calculator_estimate",
        derived={"estimated_price": "612345", "estimated_deposit_amount": "10000000"},
    ))
    assert observation.status == "review"
    assert observation.verification_level == "review"
    assert observation.amount is None and observation.normalized_amount is None
    assert observation.derived_amount == "612345"
    assert observation.derived_values["estimated_deposit_amount"] == "10000000"
    assert not observation.comparable


def test_hidden_observed_price_is_retained_but_not_verified_or_comparable():
    observation = check(make_candidate(conditions=rental_conditions(), visibility="hidden"))
    assert observation.amount == "590000"
    assert observation.status == "review" and observation.verification_level == "review"
    assert not observation.comparable


def test_missing_rental_term_blocks_comparison_without_discarding_valid_price():
    conditions = rental_conditions()
    conditions.pop("term_months")
    observation = check(make_candidate(conditions=conditions))
    assert observation.status == "verified" and observation.amount == "590000"
    assert not observation.comparable
    assert "term_months" in observation.reason


def test_explicit_zero_deposit_is_distinct_from_missing_deposit():
    zero = check(make_candidate(conditions=rental_conditions(deposit_amount="0")))
    missing_conditions = rental_conditions()
    missing_conditions.pop("deposit_amount")
    missing = check(make_candidate(conditions=missing_conditions))
    assert zero.status == "verified" and zero.comparable
    assert zero.rental_conditions["deposit_amount"] == "0"
    assert missing.status == "verified" and not missing.comparable


def test_deposit_advance_and_installment_values_remain_distinct():
    conditions = rental_conditions(
        deposit_amount="5000000", advance_amount="2000000",
        upfront_deposit_amount="3000000", deposit_installment_amount="2000000",
        deposit_installment_months=10,
    )
    observation = check(make_candidate(conditions=conditions))
    assert observation.comparable
    assert observation.rental_conditions["deposit_amount"] == "5000000"
    assert observation.rental_conditions["advance_amount"] == "2000000"
    assert observation.rental_conditions["upfront_deposit_amount"] == "3000000"
    assert observation.rental_conditions["deposit_installment_amount"] == "2000000"


def test_explicit_optional_payment_conditions_separate_comparison_groups():
    upfront = check(make_candidate(conditions=rental_conditions(
        deposit_amount="5000000", upfront_deposit_amount="5000000",
    )))
    installment = check(make_candidate(conditions=rental_conditions(
        deposit_amount="5000000", upfront_deposit_amount="1000000",
        deposit_installment_amount="4000000", deposit_installment_months=12,
    )))
    assert upfront.comparable and installment.comparable
    assert upfront.comparison_key != installment.comparison_key
    assert json.loads(installment.comparison_key)["deposit_installment_months"] == 12


def test_exact_deposit_percentage_mismatch_is_flagged_and_originals_preserved():
    conditions = rental_conditions(
        deposit_amount="9000000", deposit_percent="10", vehicle_value="100000000",
        deposit_percent_basis="vehicle_value",
    )
    observation = check(make_candidate(conditions=conditions))
    assert observation.status == "review" and not observation.comparable
    assert observation.rental_conditions["deposit_amount"] == "9000000"
    assert observation.rental_conditions["deposit_percent"] == "10"
    assert any("does not match" in flag for flag in observation.review_flags)


def test_same_price_with_different_conditions_has_stable_distinct_keys():
    first = check(make_candidate(conditions=rental_conditions(options="winter|driver=assist")))
    second = check(make_candidate(conditions=rental_conditions(options="winter", condition="driver=assist|new")))
    assert first.amount == second.amount and first.comparable and second.comparable
    assert first.comparison_key != second.comparison_key
    assert json.loads(first.comparison_key)["options"] == "winter|driver=assist"


def test_review_flag_keeps_observed_amount_but_prevents_comparison():
    observation = check(make_candidate(conditions=rental_conditions(), flags=["ambiguous promotion period"]))
    assert observation.amount == "590000"
    assert observation.status == "review" and not observation.comparable
    assert observation.review_flags == ["ambiguous promotion period"]


def test_unit_profile_and_candidate_defaults_remain_backward_compatible():
    fields = {
        "price": "12", "currency": "USD", "unit": "each", "pack_quantity": "3",
        "price_basis": "pack", "tax": "included", "price_type": "sale",
    }
    value = Candidate(fields, {key: {"location": "x", "raw": item} for key, item in fields.items()}, "x", "test")
    observation = check(value, Product("car", "Car"))
    assert value.value_origin == "observed" and value.source_visibility == "unconfirmed"
    assert observation.status == "verified" and observation.comparable
    assert observation.normalized_amount == "4" and observation.calculation == "amount / pack_quantity"
    assert observation.price_profile == "unit" and observation.verification_level == "evidence_validated"


def test_unit_estimate_also_keeps_derived_amount_out_of_amount():
    value = Candidate(
        {"currency": "USD"}, {"currency": {"location": "x", "raw": "USD"}}, "x", "test",
        value_origin="calculator_estimate", derived_values={"estimated_price": "12.50"},
    )
    observation = check(value, Product("car", "Car"))
    assert observation.status == "review" and not observation.comparable
    assert observation.amount is None and observation.derived_amount == "12.50"


def test_config_validates_profile_adapter_and_incremental_types(tmp_path):
    config = {
        "products": [{"id": "p", "name": "P", "identifiers": {"sku": "p"}, "price_profile": "rental"}],
        "sources": [{
            "id": "s", "name": "S", "kind": "web", "location": "https://example.test", "product_ids": ["p"],
            "allowed_domains": ["example.test"], "adapter": "jetcar", "incremental": True,
        }],
        "output_dir": "out",
    }
    path = tmp_path / "config.json"
    path.write_text(json.dumps(config), encoding="utf-8")
    loaded = load_config(path)
    assert loaded.products[0].price_profile == "rental"
    assert loaded.sources[0].adapter == "jetcar" and loaded.sources[0].incremental is True

    config["sources"][0]["incremental"] = 1
    path.write_text(json.dumps(config), encoding="utf-8")
    with pytest.raises(ValueError, match="incremental"):
        load_config(path)
