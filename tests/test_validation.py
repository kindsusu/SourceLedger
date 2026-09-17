from su_crawler.models import Candidate, Product, Source
from su_crawler.validation import validate


def check(candidate, product=Product("p", "P"), **source_values):
    source = Source("s", "S", "web", "https://x", ["p"], **source_values)
    return validate(candidate, product, source, run_id="r", task_id="t", evidence_path="e", evidence_sha256="h", collected_at="2026-01-01T00:00:00+00:00", source_url="https://x")


def candidate(fields, specs=None):
    evidence = {key: {"location": "x", "raw": value} for key, value in fields.items()}
    for key, value in (specs or {}).items(): evidence[f"spec:{key}"] = {"location": "x", "raw": value}
    return Candidate(fields, evidence, "x", "test", specs or {})


def test_missing_currency_and_zero_without_free_are_review():
    observation = check(candidate({"price": "0", "unit": "each", "pack_quantity": "1", "price_basis": "each", "tax": "included", "price_type": "sale"}))
    assert observation.status == "review" and not observation.comparable


def test_identifier_conflict_is_product_not_found():
    observation = check(candidate({"price": "1", "currency": "USD", "sku": "wrong"}), Product("p", "P", identifiers={"sku": "right"}))
    assert observation.status == "product_not_found"


def test_conditions_gate_comparison_and_expiry():
    base = {"price": "10,50", "currency": "EUR", "unit": "each", "pack_quantity": "2", "price_basis": "pack", "tax": "included", "price_type": "sale", "valid_to": "2020-01-01"}
    observation = check(candidate(base), decimal_separator=",")
    assert observation.amount == "10.50" and not observation.comparable and observation.status == "review"


def test_required_spec_has_to_match():
    observation = check(candidate({"price": "10", "currency": "USD"}, {"size": "L"}), Product("p", "P", required_specs={"size": "M"}))
    assert observation.status == "review"


def test_populated_field_without_evidence_cannot_verify():
    raw = {"price": "10", "currency": "USD", "unit": "each", "pack_quantity": "1", "price_basis": "each", "tax": "included", "price_type": "sale"}
    value = candidate(raw)
    value.evidence.pop("price_type")
    assert check(value).status == "review"


def test_amount_grammar_currency_and_quantity_are_strict():
    common = {"currency": "USD", "unit": "each", "pack_quantity": "1", "price_basis": "each", "tax": "included", "price_type": "sale"}
    assert check(candidate({**common, "price": "-100"})).status == "review"
    assert check(candidate({**common, "price": "12,34"})).status == "review"
    assert check(candidate({**common, "price": "1e3"})).status == "review"
    assert check(candidate({**common, "price": "1", "currency": "$"})).status == "review"
    assert not check(candidate({**common, "price": "10", "pack_quantity": "0"})).comparable


def test_verified_observation_can_be_noncomparable_and_normalizes_pack():
    raw = {"price": "12", "currency": "USD", "unit": "each", "pack_quantity": "3", "price_basis": "pack", "tax": "included", "price_type": "sale"}
    observation = check(candidate(raw))
    assert observation.status == "verified" and observation.comparable
    assert observation.normalized_amount == "4" and observation.calculation == "amount / pack_quantity"
    sparse = check(candidate({"price": "12", "currency": "USD"}))
    assert sparse.status == "verified" and not sparse.comparable and sparse.normalized_amount is None


def test_invalid_valid_to_and_out_of_stock_prevent_comparison():
    raw = {"price": "12", "currency": "USD", "unit": "each", "pack_quantity": "1", "price_basis": "each", "tax": "included", "price_type": "sale", "valid_to": "not-date"}
    assert check(candidate(raw)).status == "review"
    raw.pop("valid_to")
    raw["availability"] = "out_of_stock"
    observation = check(candidate(raw))
    assert observation.status == "verified" and not observation.comparable


def test_comparison_key_scopes_product_account_and_specs():
    raw = {"price": "12", "currency": "USD", "unit": "each", "pack_quantity": "1", "price_basis": "each", "tax": "included", "price_type": "sale"}
    observation = check(candidate(raw, {"color": "red"}), Product("product-7", "P"), account_scope="internal-a")
    assert "product_id=product-7" in observation.comparison_key
    assert "account_scope=internal-a" in observation.comparison_key
    assert "spec:color=red" in observation.comparison_key


def test_price_decoration_requires_matching_explicit_currency():
    common = {"unit": "each", "pack_quantity": "1", "price_basis": "each", "tax": "included", "price_type": "sale"}
    assert check(candidate({**common, "price": "₩1,000", "currency": "KRW"})).amount == "1000"
    assert check(candidate({**common, "price": "$12.99", "currency": "KRW"})).status == "review"
    assert check(candidate({**common, "price": "1,000원"})).status == "review"


def test_schema_org_outofstock_and_no_price_evidence_are_review():
    raw = {"price": "10", "currency": "USD", "unit": "each", "pack_quantity": "1", "price_basis": "each", "tax": "included", "price_type": "sale", "availability": "https://schema.org/OutOfStock"}
    assert not check(candidate(raw)).comparable
    no_price = candidate({"currency": "USD"})
    assert check(no_price).status == "review"


def test_price_basis_is_required_and_only_pack_prices_are_divided():
    common = {"price": "12", "currency": "USD", "unit": "each", "pack_quantity": "3", "tax": "included", "price_type": "sale"}
    missing_basis = check(candidate(common))
    assert missing_basis.status == "verified" and not missing_basis.comparable
    assert missing_basis.normalized_amount is None and missing_basis.calculation is None
    each = check(candidate({**common, "price_basis": "each"}))
    assert each.comparable and each.normalized_amount == "12" and each.calculation == "amount (each)"


def test_currency_marker_accepts_compatible_symbols_and_all_supported_iso_codes():
    common = {"unit": "each", "pack_quantity": "1", "price_basis": "each", "tax": "included", "price_type": "sale"}
    assert check(candidate({**common, "price": "$12", "currency": "CAD"})).status == "verified"
    assert check(candidate({**common, "price": "¥12", "currency": "CNY"})).status == "verified"
    assert check(candidate({**common, "price": "12", "currency": "PLN"})).status == "verified"
    assert check(candidate({**common, "price": "$12", "currency": "KRW"})).status == "review"


def test_evidence_mismatch_and_shipping_zero_do_not_verify_price():
    raw = {"price": "0", "currency": "USD", "unit": "each", "pack_quantity": "1", "price_basis": "each", "tax": "included", "price_type": "sale", "shipping": "free shipping"}
    assert check(candidate(raw)).status == "review"
    mismatch = candidate({**raw, "price": "10"})
    mismatch.evidence["price"]["raw"] = "11"
    assert check(mismatch).status == "review"
    free = {**raw, "price_type": "free", "free_item": True}
    assert check(candidate(free)).status == "verified"
