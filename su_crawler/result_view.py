"""Read-only presentation: offers and supporting evidence are different records.

Association never changes an observed value or promotes evidence to verified.
"""
from copy import deepcopy
from decimal import Decimal, InvalidOperation
import re
import unicodedata

from .validation import RENTAL_COMPARISON_FIELDS

FIELD_LABELS = {"price": "Price", "currency": "Currency", "price_basis": "Price basis", "term_months": "Contract term (months)",
                "deposit_amount": "Deposit", "advance_amount": "Advance payment", "annual_mileage_km": "Annual mileage (km)",
                "deposit_percent": "Deposit (%)",
                "item_id": "Item ID", "sku": "SKU", "model": "Model", "spec:size": "Size", "spec:color": "Color",
                "offer_sku": "Offer SKU", "mpn": "Manufacturer part number", "quantity_tier": "Quantity tier",
                "min_order": "Minimum order", "member_condition": "Member condition", "membership": "Membership",
                "trim": "Trim", "options": "Options", "condition": "Product condition", "insurance": "Insurance",
                "tax": "Tax", "availability": "Availability", "unit": "Unit", "pack_quantity": "Pack quantity", "price_type": "Price type"}

# These attributes identify independently offered variants across industries.
# Price alone is not an identity attribute: two captures of one offer may disagree.
VARIANT_FIELDS = ("item_id", "sku", "offer_sku", "model", "mpn", "gtin", "gtin8", "gtin12", "gtin13", "gtin14", "options",
                  "term_months", "deposit_amount", "deposit_percent", "annual_mileage_km",
                  "price_basis", "unit", "pack_quantity", "price_type", "quantity_tier", "min_order",
                  "member_condition", "membership")


def variant_keys(*field_sets):
    """Shared offer identity dimensions, including source-specific specifications."""
    dynamic = {str(key) for fields in field_sets for key in fields if str(key).startswith("spec:")}
    return (*VARIANT_FIELDS, *sorted(dynamic))


def _present(value):
    return value is not None and value != ""


def _variant_signature(fields):
    return tuple((key, _value(key, fields[key])) for key in variant_keys(fields) if _present(fields.get(key)))


def _variant_conflicts(submitted, base):
    return [key for key in variant_keys(submitted, base)
            if _present(submitted.get(key)) and _present(base.get(key))
            and _value(key, submitted[key]) != _value(key, base[key])]


def _text(value):
    return " ".join(unicodedata.normalize("NFKC", str(value)).casefold().split())


def _value(key, value):
    text = _text(value)
    if key in {"term_months", "deposit_installment_months"}:
        match = re.fullmatch(r"(\d+)\s*(?:개월|months?|か月)?", text)
        return match[1] if match else text
    if key in {"price", "deposit_amount", "annual_mileage_km"}:
        match = re.fullmatch(r"([\d,.]+)\s*(만)?\s*(?:원|krw|km)?", text)
        if match:
            try:
                return Decimal(match[1].replace(",", "")) * (10000 if match[2] else 1)
            except InvalidOperation:
                pass
    return text


def _fields(row):
    raw = dict(row.get("raw_fields") or {})
    interpretations = (row.get("derived_values") or {}).get("literal_interpretations") or {}
    for key in ("term_months", "deposit_amount", "deposit_percent", "price_basis"):
        if key in interpretations:
            raw[key] = interpretations[key]
    for alias, key in (("deposit", "deposit_amount"), ("annual_mileage", "annual_mileage_km"), ("option", "options"), ("size", "spec:size"), ("color", "spec:color")):
        if alias == "deposit" and "deposit_percent" in raw:
            continue
        if alias in raw and key not in raw:
            raw[key] = raw[alias]
    return raw


def build_result_view(rows):
    offers = [deepcopy(row) for row in rows if row.get("extraction_method") != "host_browser_submission"]
    submissions = [deepcopy(row) for row in rows if row.get("extraction_method") == "host_browser_submission"]
    for row in offers:
        row["supporting_evidence"] = []
        row["price_kind"] = (row.get("derived_values") or {}).get("price_kind")
    unlinked = []
    for evidence in submissions:
        fields = _fields(evidence)
        candidates = []
        for row in offers:
            base = _fields(row)
            if row.get("source_url") != evidence.get("source_url") or not fields.get("name") or _text(base.get("name", "")) != _text(fields["name"]):
                continue
            if _variant_conflicts(fields, base) or any(
                _present(fields.get(key)) and not _present(base.get(key)) for key in variant_keys(fields)
            ):
                continue
            candidates.append(row)
        explicit = (evidence.get("derived_values") or {}).get("target_observation_id")
        if explicit:
            candidates = [row for row in candidates if row["id"] == explicit]
        if len(candidates) != 1:
            evidence["association_status"] = "ambiguous" if candidates else "unmatched"
            unlinked.append(evidence)
            continue
        row = candidates[0]
        evidence["target_observation_id"] = row["id"]
        evidence["association_status"] = "linked_by_identity"
        evidence["conflicts"] = []
        base = _fields(row)
        for key in ("price", "currency"):
            if key in fields and key in base and _value(key, fields[key]) != _value(key, base[key]):
                evidence["conflicts"].append({"field": key, "observed": base[key], "submitted": fields[key]})
        row["supporting_evidence"].append(evidence)
    for row in offers:
        fields = _fields(row)
        required = RENTAL_COMPARISON_FIELDS if row.get("price_profile") == "rental" else ("currency", "unit", "pack_quantity", "tax", "price_type", "price_basis")
        display_fields = ("price", *required) + (("deposit_percent",) if "deposit_percent" in fields else ())
        row["field_status"] = {key: {"label": FIELD_LABELS.get(key, key), "value": fields.get(key), "status": "recorded" if fields.get(key) not in (None, "") else "missing"}
                               for key in display_fields}
        conflicts = [conflict for evidence in row["supporting_evidence"] for conflict in evidence["conflicts"]]
        for conflict in conflicts:
            if conflict["field"] in row["field_status"]:
                row["field_status"][conflict["field"]]["status"] = "conflict"
        row["conflicts"] = conflicts
        row["missing_conditions"] = [key for key in required if fields.get(key) in (None, "")]
        row["result_status"] = "conflict" if conflicts else "price_unavailable" if row.get("amount") is None else "verified" if row.get("status") == "verified" else "review"
        if conflicts:
            row["comparable"] = False
            row["comparison_key"] = None
    # A capture with no unique offer match is still a useful, review-only
    # candidate. Keep it in the separate evidence list for provenance, while
    # showing it once in Results under its original submission ID.
    for evidence in list(unlinked):
        if evidence.get("association_status") != "unmatched":
            continue
        if (evidence.get("derived_values") or {}).get("target_observation_id"):
            # An explicit target that failed identity checks is a disputed
            # association, not proof that a new product variant exists.
            continue
        raw_fields = evidence.get("raw_fields") or {}
        if not _present(raw_fields.get("price")):
            continue
        same_identity = [row for row in offers if row.get("source_url") == evidence.get("source_url")
                         and _text((row.get("raw_fields") or {}).get("name", "")) == _text(raw_fields.get("name", ""))]
        # Suppress an identical browser candidate while retaining its evidence.
        submitted_fields = _fields(evidence)
        matching_browser_candidates = [row for row in same_identity if row.get("primary_candidate")
                                       and _variant_signature(_fields(row)) == _variant_signature(submitted_fields)
                                       and _value("price", (row.get("raw_fields") or {}).get("price", ""))
                                       == _value("price", raw_fields.get("price", ""))]
        if len(matching_browser_candidates) == 1:
            target = matching_browser_candidates[0]
            evidence["target_observation_id"] = target["id"]
            evidence["association_status"] = "linked_by_browser_identity"
            evidence["conflicts"] = []
            target["supporting_evidence"].append(evidence)
            unlinked.remove(evidence)
            continue
        # Admit an unmatched capture only when an explicit option/condition in
        # its source text distinguishes it from *every* known offer. Missing
        # attributes never prove a new variant, and an ambiguous association
        # stays in the evidence list instead of becoming another result.
        if same_identity and not all(
            _variant_conflicts(submitted_fields, _fields(row))
            for row in same_identity
        ):
            continue
        candidate = deepcopy(evidence)
        candidate["primary_candidate"] = True
        candidate["price_kind"] = (candidate.get("derived_values") or {}).get("price_kind")
        candidate["status"] = "review"
        candidate["verification_level"] = "review"
        candidate["comparable"] = False
        candidate["comparison_key"] = None
        candidate["supporting_evidence"] = []
        candidate["conflicts"] = []
        fields = _fields(candidate)
        required = RENTAL_COMPARISON_FIELDS if candidate.get("price_profile") == "rental" else ("currency", "unit", "pack_quantity", "tax", "price_type", "price_basis")
        interpreted = (candidate.get("derived_values") or {}).get("literal_interpretations") or {}
        candidate["field_status"] = {
            key: {"label": FIELD_LABELS.get(key, key), "value": fields.get(key),
                  "status": "missing" if fields.get(key) in (None, "") else "derived" if key != "price" and key in interpreted else "recorded"}
            for key in ("price", *required, "deposit_percent")
        }
        candidate["missing_conditions"] = [key for key in required if fields.get(key) in (None, "")]
        candidate["result_status"] = "review" if fields.get("price") not in (None, "") else "price_unavailable"
        offers.append(candidate)
    return {"rows": offers, "evidence_submissions": unlinked,
            "result_count": len(offers), "evidence_count": len(submissions),
            "linked_evidence_count": len(submissions) - len(unlinked), "unlinked_evidence_count": len(unlinked)}
