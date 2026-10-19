"""Small, field-scoped conversions that can be replayed from preserved source text.

An extractor must declare the rule beside the verbatim ``evidence.raw`` value.
Unknown rules and rule/field combinations never establish evidence.
"""
from __future__ import annotations

import re
from typing import Any


_MONEY_FIELDS = frozenset({
    "price", "deposit_amount", "advance_amount", "upfront_deposit_amount",
    "deposit_installment_amount", "vehicle_value",
})
_MONTH_FIELDS = frozenset({"term_months", "deposit_installment_months"})
_FIELD_RULES = {
    "korean_won_amount": _MONEY_FIELDS,
    "korean_months": _MONTH_FIELDS,
    "korean_won_currency": frozenset({"currency"}),
    "korean_vat_included": frozenset({"tax"}),
    "korean_vat_excluded": frozenset({"tax"}),
    "kilometers": frozenset({"annual_mileage_km"}),
}


def _field_name(name: str) -> str:
    return name.removeprefix("rental_conditions.")


def _rule_name(metadata: Any) -> str | None:
    if not isinstance(metadata, dict) or set(metadata) != {"rule"}:
        return None
    return metadata["rule"] if isinstance(metadata["rule"], str) else None


def normalized_evidence(name: str, raw: Any, metadata: Any) -> str | None:
    """Return the only value justified by an explicit rule, or None on failure.

    Deliberately accepts whole source tokens only: e.g. ``원`` proves KRW,
    while a nearby price string cannot silently supply another field.
    """
    rule = _rule_name(metadata)
    field = _field_name(name)
    if rule is None or field not in _FIELD_RULES.get(rule, ()) or not isinstance(raw, str):
        return None
    text = raw.strip()
    if rule == "korean_won_amount":
        match = re.fullmatch(r"((?:\d{1,3}(?:,\d{3})+|\d+))\s*원", text)
        return match.group(1).replace(",", "") if match else None
    if rule == "korean_months":
        match = re.fullmatch(r"([1-9]\d*)\s*개월", text)
        return match.group(1) if match else None
    if rule == "korean_won_currency":
        return "KRW" if text in {"원", "₩", "KRW"} else None
    if rule == "korean_vat_included":
        return "included" if text == "부가세 포함" else None
    if rule == "korean_vat_excluded":
        return "excluded" if text == "부가세 별도" else None
    if rule == "kilometers":
        match = re.fullmatch(r"((?:\d{1,3}(?:,\d{3})+|\d+))\s*km", text, flags=re.IGNORECASE)
        return match.group(1).replace(",", "") if match else None
    return None


def evidence_attests(name: str, raw: Any, value: Any, metadata: Any = None) -> bool:
    """Check verbatim equality or replay a declared, allowed conversion."""
    if metadata is None:
        return str(raw).strip() == str(value).strip()
    normalized = normalized_evidence(name, raw, metadata)
    return normalized is not None and normalized == str(value).strip()


def object_evidence_attests(name: str, raw: Any, value: Any, metadata: Any = None) -> bool:
    """Validate every changed member of a structured evidence object."""
    if not isinstance(raw, dict) or not isinstance(value, dict) or raw.keys() != value.keys():
        return False
    if metadata is None:
        return raw == value
    if not isinstance(metadata, dict) or set(metadata) != {"fields"} or not isinstance(metadata["fields"], dict):
        return False
    rules = metadata["fields"]
    if not set(rules).issubset(raw):
        return False
    return all(evidence_attests(f"{name}.{field}", source, value[field], {"rule": rules[field]} if field in rules else None)
               for field, source in raw.items())
