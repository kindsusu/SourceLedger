"""Strict validation: preserve source facts but never invent commercial facts."""
from __future__ import annotations

import re
import json
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation

from .models import Candidate, Observation, Product, Source, stable_id

SUPPORTED_CURRENCIES = frozenset({"AUD", "BRL", "CAD", "CHF", "CNY", "DKK", "EUR", "GBP", "HKD", "INR", "JPY", "KRW", "MXN", "NOK", "NZD", "PLN", "SEK", "SGD", "THB", "TRY", "TWD", "USD", "ZAR"})
VALUE_ORIGINS = frozenset({"observed", "calculator_estimate"})
SOURCE_VISIBILITIES = frozenset({"visible", "hidden", "unconfirmed", "not_applicable"})
EVIDENCE_MODES = frozenset({"rendered_dom", "static_html", "structured_record", "document_text", "unknown"})
RENTAL_COMPARISON_FIELDS = (
    "currency", "price_basis", "term_months", "deposit_amount", "advance_amount",
    "annual_mileage_km", "trim", "options", "condition", "insurance", "tax", "availability",
)
RENTAL_CONDITION_FIELDS = frozenset({
    *RENTAL_COMPARISON_FIELDS, "upfront_deposit_amount", "deposit_installment_amount",
    "deposit_installment_months", "deposit_percent", "deposit_percent_basis", "vehicle_value",
    "driver_age_condition", "price_age_basis",
})


def _field(candidate: Candidate, name: str):
    return candidate.specs.get(name.removeprefix("spec:")) if name.startswith("spec:") else candidate.fields.get(name)


def _has_evidence(candidate: Candidate, name: str) -> bool:
    evidence = candidate.evidence.get(name)
    return bool(evidence and str(evidence.get("location", "")).strip() and evidence.get("raw") is not None and not evidence.get("formula_cache_missing"))


def _evidence_matches(candidate: Candidate, name: str, value: object) -> bool:
    """Evidence must attest to the extracted value, rather than merely exist."""
    evidence = candidate.evidence.get(name)
    return _has_evidence(candidate, name) and str(evidence["raw"]).strip() == str(value).strip()


def _field_display_proven(candidate: Candidate, name: str) -> bool:
    """Require presentation proof only when the source is HTML-derived."""
    proof = candidate.evidence.get(name) or {}
    if proof.get("proof_kind") == "source_identity" and proof.get("display_state") == "not_applicable":
        return True
    if candidate.evidence_mode in {"structured_record", "document_text"}:
        return proof.get("display_state") == "not_applicable"
    if candidate.evidence_mode == "unknown":
        # Compatibility for legacy/manual candidates. Real extraction always
        # assigns an explicit mode.
        return True
    return proof.get("display_state") == "visible"


def _comparison_evidence(candidate: Candidate, name: str, value: object) -> bool:
    return _evidence_matches(candidate, name, value) and _field_display_proven(candidate, name)


def _amount(value: object, separator: str) -> tuple[Decimal | None, str | None]:
    if value is None:
        return None, "price missing"
    text = str(value).strip()
    text = re.sub(r"^(?:[A-Z]{3}\s+|[$₩€£¥]\s*)", "", text)
    text = re.sub(r"\s*(?:[A-Z]{3}|원)$", "", text)
    if separator not in (".", ","):
        return None, "unsupported decimal separator"
    decimal = re.escape(separator)
    group = "," if separator == "." else "."
    grouped = rf"\d{{1,3}}(?:\{group}\d{{3}})+(?:{decimal}\d+)?"
    plain = rf"\d+(?:{decimal}\d+)?"
    if not re.fullmatch(rf"(?:{plain}|{grouped})", text):
        return None, "price is not one valid non-negative decimal"
    try:
        amount = Decimal(text.replace(group, "").replace(separator, "."))
    except InvalidOperation:
        return None, "price is not a decimal"
    return (amount, None) if amount.is_finite() and amount >= 0 else (None, "price is not finite and non-negative")


def _positive_quantity(value: object, separator: str) -> tuple[Decimal | None, str | None]:
    amount, problem = _amount(value, separator)
    if problem:
        return None, "pack quantity missing or invalid"
    return (amount, None) if amount is not None and amount > 0 else (None, "pack quantity must be positive")


def _expiry(value: object, collected_at: str) -> tuple[bool, str | None]:
    if value in (None, ""):
        return False, None
    try:
        collected = datetime.fromisoformat(collected_at.replace("Z", "+00:00"))
        collected = collected.replace(tzinfo=collected.tzinfo or timezone.utc).astimezone(timezone.utc)
        text = str(value)
        if "T" not in text:
            expiry = datetime.fromisoformat(text + "T23:59:59.999999+00:00")
        else:
            expiry = datetime.fromisoformat(text.replace("Z", "+00:00"))
            expiry = expiry.replace(tzinfo=expiry.tzinfo or timezone.utc).astimezone(timezone.utc)
        return expiry < collected, None
    except ValueError:
        return False, "valid_to is invalid"


def _out_of_stock(value: object) -> bool:
    text = str(value or "").lower()
    return any(marker in text for marker in ("out_of_stock", "outofstock", "out of stock", "discontinued", "품절"))


def _currency_markers(value: object) -> frozenset[str]:
    text = str(value or "").strip()
    codes = re.findall(r"(?<![A-Z])(" + "|".join(SUPPORTED_CURRENCIES) + r")(?![A-Z])", text.upper())
    if codes:
        return frozenset(codes)
    for symbol, codes in (
        ("₩", frozenset({"KRW"})),
        ("€", frozenset({"EUR"})),
        ("£", frozenset({"GBP"})),
        ("¥", frozenset({"JPY", "CNY"})),
        ("$", frozenset({"AUD", "CAD", "HKD", "NZD", "SGD", "USD"})),
    ):
        if symbol in text:
            return codes
    return frozenset({"KRW"}) if text.endswith("원") else frozenset()


def _free_item_explicit(candidate: Candidate) -> bool:
    price_type = candidate.fields.get("price_type")
    free_item = candidate.fields.get("free_item")
    return (
        isinstance(price_type, str) and price_type.strip().casefold() == "free"
    ) or free_item is True or (
        isinstance(free_item, str) and free_item.strip().casefold() in {"true", "yes", "1"}
    )


def _rental_evidence_matches(candidate: Candidate, name: str) -> bool:
    """Accept field-level evidence, or an exact value inside evidenced condition data."""
    nested = candidate.fields.get("rental_conditions")
    value = nested.get(name) if isinstance(nested, dict) and name in nested else candidate.fields.get(name)
    for evidence_name in (f"rental_conditions.{name}", name):
        if _comparison_evidence(candidate, evidence_name, value):
            return True
    evidence = candidate.evidence.get("rental_conditions")
    raw = evidence.get("raw") if evidence else None
    return bool(evidence and str(evidence.get("location", "")).strip() and isinstance(raw, dict)
                and raw.get(name) == value and _field_display_proven(candidate, "rental_conditions"))


def _rental_conditions(candidate: Candidate, separator: str) -> tuple[dict[str, object], list[str], list[str]]:
    nested = candidate.fields.get("rental_conditions")
    if nested is not None and not isinstance(nested, dict):
        return {}, ["rental_conditions must be an object"], []
    conditions = {name: candidate.fields[name] for name in RENTAL_CONDITION_FIELDS if name in candidate.fields}
    problems: list[str] = []
    if isinstance(nested, dict):
        for name, value in nested.items():
            if name in conditions and conditions[name] != value:
                problems.append(f"conflicting rental condition: {name}")
            conditions[name] = value
    if not conditions:
        return {}, [], []
    flags: list[str] = []
    decimal_fields = {
        "deposit_amount", "advance_amount", "upfront_deposit_amount", "deposit_installment_amount",
        "vehicle_value", "deposit_percent",
    }
    integer_fields = {"term_months", "deposit_installment_months", "annual_mileage_km"}
    normalized: dict[str, object] = {}
    for name, value in conditions.items():
        if name in decimal_fields:
            number, problem = _amount(value, separator)
            if problem or number is None:
                problems.append(f"rental condition invalid: {name}")
                normalized[name] = value
            elif name == "vehicle_value" and number <= 0:
                problems.append("rental condition must be positive: vehicle_value")
                normalized[name] = str(number)
            elif name == "deposit_percent" and not Decimal("0") <= number <= Decimal("100"):
                problems.append("rental condition out of range: deposit_percent")
                normalized[name] = str(number)
            else:
                normalized[name] = str(number)
        elif name in integer_fields:
            if isinstance(value, bool) or not re.fullmatch(r"[1-9]\d*", str(value).strip()):
                problems.append(f"rental condition must be a positive integer: {name}")
                normalized[name] = value
            else:
                normalized[name] = int(str(value).strip())
        else:
            normalized[name] = value
    basis = conditions.get("deposit_percent_basis")
    try:
        if basis == "vehicle_value" and all(key in normalized for key in ("deposit_amount", "deposit_percent", "vehicle_value")):
            actual = Decimal(str(normalized["deposit_amount"]))
            expected = Decimal(str(normalized["vehicle_value"])) * Decimal(str(normalized["deposit_percent"])) / Decimal("100")
            if actual != expected:
                flags.append("deposit amount does not match explicit vehicle-value percentage")
    except InvalidOperation:
        pass
    return normalized, problems, flags


def validate(candidate: Candidate, product: Product, source: Source, *, run_id: str, task_id: str,
             evidence_path: str, evidence_sha256: str, collected_at: str, source_url: str) -> Observation:
    raw = {**candidate.fields, **{f"spec:{key.removeprefix('spec:')}": value for key, value in candidate.specs.items()}}
    reasons: list[str] = []
    status = "verified"
    review_flags = list(candidate.review_flags)
    value_origin = candidate.value_origin
    source_visibility = candidate.source_visibility
    evidence_mode = candidate.evidence_mode
    if value_origin not in VALUE_ORIGINS:
        status = "review"
        reasons.append("unsupported value_origin")
    if source_visibility not in SOURCE_VISIBILITIES:
        status = "review"
        reasons.append("unsupported source_visibility")
    if evidence_mode not in EVIDENCE_MODES:
        status = "review"
        reasons.append("unsupported evidence_mode")
    if value_origin == "calculator_estimate":
        status = "review"
        reasons.append("calculated estimate is not an observed price")
    if source_visibility == "hidden":
        status = "review"
        reasons.append("source price is hidden")
    if review_flags:
        status = "review"
        reasons.extend(f"review flag: {flag}" for flag in review_flags)
    for key, expected in product.identifiers.items():
        actual = candidate.fields.get(key)
        if actual is not None and str(actual) != str(expected):
            status, reasons = "product_not_found", [f"identifier mismatch: {key}"]
            break
        if actual is None:
            status, reasons = "review", [*reasons, f"identifier missing: {key}"]
        elif not _comparison_evidence(candidate, key, actual):
            status, reasons = "review", [*reasons, f"identifier evidence missing: {key}"]

    if "_selection" in candidate.evidence:
        selection = str(candidate.evidence["_selection"].get("raw", ""))
        identity_resolved = "multiple JSON-LD products" not in selection or (bool(product.identifiers) and all(
            str(candidate.fields.get(key)) == str(expected) for key, expected in product.identifiers.items()
        ))
        offer_resolved = "multiple offers" not in selection or bool(candidate.fields.get("offer_sku") or candidate.fields.get("option"))
        if (not identity_resolved or not offer_resolved) and status != "product_not_found":
            status = "review"
            reasons.append("multiple JSON-LD products or offers are not uniquely resolved")

    amount, currency, expired = None, candidate.fields.get("currency"), False
    if status != "product_not_found":
        for key, expected in product.required_specs.items():
            actual = candidate.specs.get(key)
            if actual is None or str(actual) != str(expected) or not _comparison_evidence(candidate, f"spec:{key}", actual):
                status = "review"
                reasons.append(f"required spec missing, mismatch, or unproven: {key}")
        amount, amount_problem = _amount(candidate.fields.get("price"), source.decimal_separator)
        if value_origin != "observed":
            amount = None
        price_text = str(candidate.fields.get("price", "")).lower()
        unavailable = any(marker.lower() in price_text for marker in source.unavailable_markers)
        price_explicitly_missing = candidate.fields.get("price") is None and _has_evidence(candidate, "price")
        if unavailable:
            status = "price_unavailable" if status == "verified" else status
            reasons.append("source marks price unavailable")
        elif candidate.fields.get("price") is None and value_origin == "observed":
            status = "review"
            reasons.append("price explicitly missing" if price_explicitly_missing else "price extraction unavailable")
        elif amount_problem and value_origin == "observed":
            status = "review"
            reasons.append(amount_problem)
        if currency is not None and str(currency) not in SUPPORTED_CURRENCIES:
            status = "review"
            reasons.append("currency is not an allowed ISO currency code")
        elif amount is not None and currency is None:
            status = "review"
            reasons.append("currency missing; symbol is not a currency code")
        marked_currencies = _currency_markers(candidate.fields.get("price"))
        if marked_currencies and currency is not None and str(currency) not in marked_currencies:
            status = "review"
            reasons.append("price currency marker conflicts with currency field")
        for name, value in candidate.fields.items():
            if value is not None and not _has_evidence(candidate, name):
                status = "review"
                reasons.append(f"evidence missing: {name}")
            elif value is not None and not _evidence_matches(candidate, name, value):
                status = "review"
                reasons.append(f"evidence conflicts with extracted field: {name}")
        for name, value in candidate.specs.items():
            if value is not None and not _evidence_matches(candidate, f"spec:{name}", value):
                status = "review"
                reasons.append(f"evidence conflicts with extracted spec: {name}")
        if amount == 0:
            if not _free_item_explicit(candidate):
                status = "review"
                reasons.append("zero price lacks free-item evidence")
        expired, expiry_problem = _expiry(candidate.fields.get("valid_to"), collected_at)
        if expiry_problem:
            status = "review"
            reasons.append(expiry_problem)
        elif expired:
            status = "review"
            reasons.append("price expired")

    derived_amount = None
    estimated = candidate.derived_values.get("estimated_price")
    if estimated is not None:
        parsed_estimate, estimate_problem = _amount(estimated, source.decimal_separator)
        if estimate_problem:
            status = "review"
            reasons.append("derived estimated_price is invalid")
        elif parsed_estimate is not None:
            derived_amount = str(parsed_estimate)
    rental_conditions, rental_problems, rental_flags = (
        _rental_conditions(candidate, source.decimal_separator) if product.price_profile == "rental" else ({}, [], [])
    )
    if rental_problems or rental_flags:
        status = "review"
        reasons.extend(rental_problems)
        reasons.extend(f"review flag: {flag}" for flag in rental_flags)
        review_flags.extend(rental_flags)
    comparison_values = [f"product_id={product.id}", f"account_scope={source.account_scope}"]
    comparable = status == "verified" and amount is not None
    if value_origin != "observed" or source_visibility == "hidden":
        comparable = False
    if amount is not None and not _comparison_evidence(candidate, "price", candidate.fields.get("price")):
        comparable = False
        reasons.append("price display or record proof is missing")
    if product.price_profile == "rental":
        rental_key = {
            "product_id": product.id,
            "account_scope": source.account_scope,
            "price_profile": product.price_profile,
            "value_origin": value_origin,
        }
        if evidence_mode not in {"structured_record", "document_text"} and source_visibility != "visible":
            comparable = False
            reasons.append("rental HTML source visibility must be visible")
        for name in RENTAL_COMPARISON_FIELDS:
            value = rental_conditions.get(name)
            valid_basis = name != "price_basis" or value == "monthly"
            if value is None or value == "" or not valid_basis or not _rental_evidence_matches(candidate, name):
                comparable = False
                reasons.append(f"rental comparison condition missing, invalid, or unproven: {name}")
            else:
                rental_key[name] = value
        # Explicit optional conditions remain part of offer identity. This
        # keeps installment and upfront variants in separate comparison groups.
        for name, value in sorted(rental_conditions.items()):
            if name not in rental_key and value is not None and value != "":
                if _rental_evidence_matches(candidate, name):
                    rental_key[name] = value
                else:
                    comparable = False
                    reasons.append(f"optional rental comparison condition unproven: {name}")
        if _out_of_stock(rental_conditions.get("availability")):
            comparable = False
            reasons.append("expired or unavailable inventory")
        return Observation(
            id=stable_id(run_id, task_id, product.id, source.id, candidate.locator), run_id=run_id, task_id=task_id,
            product_id=product.id, source_id=source.id, source_name=source.name, source_url=source_url, collected_at=collected_at,
            status=status, reason="; ".join(dict.fromkeys(reasons)) or "source values verified", raw_fields=raw,
            evidence=candidate.evidence, evidence_path=evidence_path, evidence_sha256=evidence_sha256, locator=candidate.locator,
            extraction_method=candidate.extraction_method, amount=str(amount) if amount is not None else None,
            currency=str(currency) if currency is not None else None, comparable=comparable,
            comparison_key=json.dumps(rental_key, ensure_ascii=False, sort_keys=True, separators=(",", ":")) if comparable else None,
            value_origin=value_origin, source_visibility=source_visibility, derived_values=dict(candidate.derived_values),
            review_flags=list(dict.fromkeys(review_flags)), derived_amount=derived_amount, price_profile=product.price_profile,
            verification_level="evidence_validated" if status == "verified" else "review", rental_conditions=rental_conditions or None,
            evidence_mode=evidence_mode,
        )
    price_basis = candidate.fields.get("price_basis")
    price_basis_proven = price_basis in {"pack", "each"} and _comparison_evidence(candidate, "price_basis", price_basis)
    quantity, quantity_problem = _positive_quantity(candidate.fields.get("pack_quantity"), source.decimal_separator)
    if quantity_problem:
        comparable = False
        reasons.append(quantity_problem)
    for name in product.comparison_fields:
        value = candidate.fields.get(name)
        if value is None or not _comparison_evidence(candidate, name, value):
            comparable = False
            reasons.append(f"comparison condition missing: {name}")
        else:
            comparison_values.append(f"{name}={value}")
    if not price_basis_proven:
        comparable = False
        reasons.append("comparison condition missing or invalid: price_basis")
    elif "price_basis" not in product.comparison_fields:
        comparison_values.append(f"price_basis={price_basis}")
    for key, value in sorted(candidate.specs.items()):
        if _comparison_evidence(candidate, f"spec:{key}", value):
            comparison_values.append(f"spec:{key}={value}")
    option_fields = {"option", "options", "quantity_tier", "min_order", "member_condition", "membership", "shipping", "delivery", "tax", "price_type", "availability", "unit"}
    for name, value in sorted(candidate.fields.items()):
        if name in option_fields and value is not None:
            if _comparison_evidence(candidate, name, value):
                comparison_values.append(f"{name}={value}")
            else:
                comparable = False
                reasons.append(f"optional comparison condition unproven: {name}")
    for name in ("model", "manufacturer"):
        value = candidate.fields.get(name)
        if value is not None:
            if _comparison_evidence(candidate, name, value):
                comparison_values.append(f"{name}={value}")
            else:
                comparable = False
                reasons.append(f"identity comparison condition unproven: {name}")
    if expired or _out_of_stock(candidate.fields.get("availability")):
        comparable = False
        reasons.append("expired or unavailable inventory")
    normalized_amount = None
    calculation = None
    if comparable and amount is not None and quantity is not None and price_basis_proven:
        if price_basis == "pack":
            normalized_amount, calculation = str(amount / quantity), "amount / pack_quantity"
        else:
            normalized_amount, calculation = str(amount), "amount (each)"
    return Observation(
        id=stable_id(run_id, task_id, product.id, source.id, candidate.locator), run_id=run_id, task_id=task_id,
        product_id=product.id, source_id=source.id, source_name=source.name, source_url=source_url, collected_at=collected_at,
        status=status, reason="; ".join(dict.fromkeys(reasons)) or "source values verified", raw_fields=raw,
        evidence=candidate.evidence, evidence_path=evidence_path, evidence_sha256=evidence_sha256, locator=candidate.locator,
        extraction_method=candidate.extraction_method, amount=str(amount) if amount is not None else None,
        currency=str(currency) if currency is not None else None, unit=str(candidate.fields["unit"]) if candidate.fields.get("unit") is not None else None,
        pack_quantity=str(candidate.fields["pack_quantity"]) if candidate.fields.get("pack_quantity") is not None else None,
        normalized_amount=normalized_amount, calculation=calculation, comparable=comparable,
        comparison_key="|".join(comparison_values) if comparable else None,
        value_origin=value_origin, source_visibility=source_visibility, derived_values=dict(candidate.derived_values),
        review_flags=list(dict.fromkeys(review_flags)), derived_amount=derived_amount, price_profile=product.price_profile,
        verification_level="evidence_validated" if status == "verified" else "review",
        evidence_mode=evidence_mode,
    )
