"""Strict validation: preserve source facts but never invent commercial facts."""
from __future__ import annotations

import re
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation

from .models import Candidate, Observation, Product, Source, stable_id

SUPPORTED_CURRENCIES = frozenset({"AUD", "BRL", "CAD", "CHF", "CNY", "DKK", "EUR", "GBP", "HKD", "INR", "JPY", "KRW", "MXN", "NOK", "NZD", "PLN", "SEK", "SGD", "THB", "TRY", "TWD", "USD", "ZAR"})


def _field(candidate: Candidate, name: str):
    return candidate.specs.get(name.removeprefix("spec:")) if name.startswith("spec:") else candidate.fields.get(name)


def _has_evidence(candidate: Candidate, name: str) -> bool:
    evidence = candidate.evidence.get(name)
    return bool(evidence and str(evidence.get("location", "")).strip() and evidence.get("raw") is not None and not evidence.get("formula_cache_missing"))


def _evidence_matches(candidate: Candidate, name: str, value: object) -> bool:
    """Evidence must attest to the extracted value, rather than merely exist."""
    evidence = candidate.evidence.get(name)
    return _has_evidence(candidate, name) and str(evidence["raw"]).strip() == str(value).strip()


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


def validate(candidate: Candidate, product: Product, source: Source, *, run_id: str, task_id: str,
             evidence_path: str, evidence_sha256: str, collected_at: str, source_url: str) -> Observation:
    raw = {**candidate.fields, **{f"spec:{key.removeprefix('spec:')}": value for key, value in candidate.specs.items()}}
    reasons: list[str] = []
    status = "verified"
    for key, expected in product.identifiers.items():
        actual = candidate.fields.get(key)
        if actual is not None and str(actual) != str(expected):
            status, reasons = "product_not_found", [f"identifier mismatch: {key}"]
            break
        if actual is None:
            status, reasons = "review", [*reasons, f"identifier missing: {key}"]
        elif not _has_evidence(candidate, key):
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
            if actual is None or str(actual) != str(expected) or not _has_evidence(candidate, f"spec:{key}"):
                status = "review"
                reasons.append(f"required spec missing, mismatch, or unproven: {key}")
        amount, amount_problem = _amount(candidate.fields.get("price"), source.decimal_separator)
        price_text = str(candidate.fields.get("price", "")).lower()
        unavailable = any(marker.lower() in price_text for marker in source.unavailable_markers)
        price_explicitly_missing = candidate.fields.get("price") is None and _has_evidence(candidate, "price")
        if unavailable:
            status = "price_unavailable" if status == "verified" else status
            reasons.append("source marks price unavailable")
        elif candidate.fields.get("price") is None:
            status = "review"
            reasons.append("price explicitly missing" if price_explicitly_missing else "price extraction unavailable")
        elif amount_problem:
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

    comparison_values = [f"product_id={product.id}", f"account_scope={source.account_scope}"]
    comparable = status == "verified" and amount is not None
    price_basis = candidate.fields.get("price_basis")
    price_basis_proven = price_basis in {"pack", "each"} and _evidence_matches(candidate, "price_basis", price_basis)
    quantity, quantity_problem = _positive_quantity(candidate.fields.get("pack_quantity"), source.decimal_separator)
    if quantity_problem:
        comparable = False
        reasons.append(quantity_problem)
    for name in product.comparison_fields:
        value = candidate.fields.get(name)
        if value is None or not _has_evidence(candidate, name):
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
        if _has_evidence(candidate, f"spec:{key}"):
            comparison_values.append(f"spec:{key}={value}")
    option_fields = {"option", "options", "quantity_tier", "min_order", "member_condition", "membership", "shipping", "delivery", "tax", "price_type", "availability", "unit"}
    for name, value in sorted(candidate.fields.items()):
        if name in option_fields and value is not None and _has_evidence(candidate, name):
            comparison_values.append(f"{name}={value}")
    for name in ("model", "manufacturer"):
        value = candidate.fields.get(name)
        if value is not None and _has_evidence(candidate, name):
            comparison_values.append(f"{name}={value}")
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
    )
