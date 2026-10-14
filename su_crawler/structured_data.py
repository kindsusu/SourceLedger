"""Conservative, shared extraction of embedded Product/Offer JSON-LD.

Embedded metadata is evidence, but cannot establish that a price was displayed.
Traversal limits are reported on the fetch trace instead of silently implying
that a bounded document was read in full.
"""
from __future__ import annotations

import json
from typing import Any, Iterator

from .adapters.common import html_soup
from .models import Candidate, FetchResult


_PAGE_CANDIDATE_LIMIT = 1000


def _type(node: dict[str, Any], name: str) -> bool:
    value = node.get("@type", [])
    values = [value] if isinstance(value, str) else value if isinstance(value, list) else []
    return any(isinstance(item, str) and item.rstrip("/#").rsplit("/", 1)[-1].rsplit("#", 1)[-1] == name
               for item in values)


def nodes(value: Any, diagnostics: list[dict[str, Any]] | None = None,
          depth: int = 0) -> Iterator[dict[str, Any]]:
    """Yield bounded structured-data nodes and record every truncation."""
    if depth > 8:
        if diagnostics is not None:
            diagnostics.append({"event": "jsonld_truncated", "reason": "depth_limit", "depth": depth})
        return
    if isinstance(value, list):
        if len(value) > 1000 and diagnostics is not None:
            diagnostics.append({"event": "jsonld_truncated", "reason": "array_limit",
                                "observed_count": len(value), "processed_count": 1000})
        for item in value[:1000]:
            yield from nodes(item, diagnostics, depth + 1)
    elif isinstance(value, dict):
        yield value
        for key in ("@graph", "hasVariant", "itemListElement", "item"):
            child = value.get(key)
            if isinstance(child, (dict, list)):
                yield from nodes(child, diagnostics, depth + 1)


def _scalar(value: Any) -> str | int | float | None:
    return value if isinstance(value, (str, int, float)) and not isinstance(value, bool) else None


def _named(value: Any) -> str | int | float | None:
    return _scalar(value.get("name")) if isinstance(value, dict) else _scalar(value)


def extract_jsonld(result: FetchResult) -> list[Candidate]:
    """Return only explicit product prices and distinct, conditioned unit tiers."""
    if result.status != "fetched" or "html" not in result.media_type.lower():
        return []
    soup = html_soup(result.content)
    found: list[tuple[str, dict[str, Any]]] = []
    diagnostics: list[dict[str, Any]] = []
    product_limit_reached = False
    for script_index, script in enumerate(soup.select('script[type="application/ld+json"]'), 1):
        try:
            data = json.loads(script.string or script.get_text(), parse_float=str)
        except (ValueError, TypeError):
            diagnostics.append({"event": "extraction_gap", "reason": "invalid_json", "script_index": script_index})
            continue
        for node_index, node in enumerate(nodes(data, diagnostics), 1):
            if _type(node, "Product"):
                found.append((f"jsonld:{script_index}/{node_index}", node))
                if len(found) > _PAGE_CANDIDATE_LIMIT:
                    diagnostics.append({"event": "jsonld_truncated", "reason": "product_node_limit",
                                        "processed_count": _PAGE_CANDIDATE_LIMIT})
                    product_limit_reached = True
                    break
        if product_limit_reached:
            break
    if product_limit_reached:
        found = found[:_PAGE_CANDIDATE_LIMIT]

    def append_candidate(candidate: Candidate) -> bool:
        if len(candidates) >= _PAGE_CANDIDATE_LIMIT:
            diagnostics.append({"event": "jsonld_truncated", "reason": "page_candidate_limit",
                                "processed_count": _PAGE_CANDIDATE_LIMIT})
            result.trace.extend(diagnostics)
            return False
        candidates.append(candidate)
        return True

    candidates: list[Candidate] = []
    processed_offers = 0
    processed_price_specs = 0
    for base, node in found:
        if node.get("hasVariant") and not node.get("offers"):
            continue
        offers_raw = node.get("offers")
        offers = offers_raw if isinstance(offers_raw, list) else [offers_raw] if isinstance(offers_raw, dict) else [{}]
        if len(offers) > 1000:
            diagnostics.append({"event": "jsonld_truncated", "reason": "offers_limit", "locator": base,
                                "observed_count": len(offers), "processed_count": 1000})
            offers = offers[:1000]
        for offer_index, offer in enumerate(offers, 1):
            if len(candidates) >= _PAGE_CANDIDATE_LIMIT:
                diagnostics.append({"event": "jsonld_truncated", "reason": "page_candidate_limit",
                                    "processed_count": _PAGE_CANDIDATE_LIMIT})
                result.trace.extend(diagnostics)
                return candidates
            if processed_offers >= _PAGE_CANDIDATE_LIMIT:
                diagnostics.append({"event": "jsonld_truncated", "reason": "page_offer_limit",
                                    "processed_count": _PAGE_CANDIDATE_LIMIT})
                result.trace.extend(diagnostics)
                return candidates
            processed_offers += 1
            if not isinstance(offer, dict):
                diagnostics.append({"event": "extraction_gap", "reason": "non_object_offer", "locator": base})
                continue
            locator = f"{base}/offer:{offer_index}"
            fields: dict[str, Any] = {}
            specs: dict[str, str] = {}
            proofs: dict[str, dict[str, Any]] = {}

            def add(name: str, value: Any, *, spec: bool = False, at: str | None = None,
                    target_fields: dict[str, Any] = fields,
                    target_specs: dict[str, str] = specs,
                    target_proofs: dict[str, dict[str, Any]] = proofs,
                    path: str = locator) -> None:
                raw = _scalar(value)
                if raw is None:
                    return
                key = f"spec:{name}" if spec else name
                if spec:
                    target_specs[name] = str(raw)
                else:
                    target_fields[name] = raw
                target_proofs[key] = {"location": f"{path}/{at or name}", "raw": raw,
                                      "display_state": "unconfirmed", "proof_kind": "embedded_metadata"}

            for key in ("name", "description", "sku", "model", "mpn", "gtin", "gtin8", "gtin12", "gtin13", "gtin14"):
                add(key, node.get(key), at=f"Product.{key}")
            add("manufacturer", _named(node.get("manufacturer")), at="Product.manufacturer")
            add("brand", _named(node.get("brand")), at="Product.brand")
            add("seller", _named(offer.get("seller")), at="Offer.seller")
            add("condition", node.get("itemCondition"), at="Product.itemCondition")
            for key in ("category", "color", "size", "material", "pattern", "weight"):
                add(key, node.get(key), spec=True, at=f"Product.{key}")
            for key in ("color", "size", "material"):
                add(f"offer_{key}", offer.get(key), spec=True, at=f"Offer.{key}")
            attributes = node.get("additionalProperty")
            for attribute in attributes if isinstance(attributes, list) else [attributes]:
                if isinstance(attribute, dict) and _scalar(attribute.get("name")):
                    add(str(attribute["name"]), attribute.get("value"), spec=True, at="Product.additionalProperty")
            for name, key in (("offer_sku", "sku"), ("option", "name"), ("availability", "availability"),
                              ("offer_condition", "itemCondition"), ("valid_to", "priceValidUntil"),
                              ("currency", "priceCurrency")):
                add(name, offer.get(key), at=f"Offer.{key}")
            aggregate = _type(offer, "AggregateOffer")
            if aggregate:
                for key in ("lowPrice", "highPrice", "offerCount"):
                    add(key, offer.get(key), spec=True, at=f"AggregateOffer.{key}")
            else:
                add("price", offer.get("price"), at="Offer.price")
            flags = ["embedded JSON-LD price has not been visually verified"]
            if aggregate:
                flags.append("AggregateOffer is a range, not an exact offer price")
                proofs["_extraction"] = {"location": locator, "raw": "AggregateOffer range is not an exact price"}
            if len(found) > 1 or len(offers) > 1:
                flags.append("multiple embedded products or offers need selection review")
                proofs["_selection"] = {"location": locator, "raw": "multiple JSON-LD products or offers require product matching"}

            price_spec_raw = offer.get("priceSpecification")
            price_specs = (price_spec_raw if isinstance(price_spec_raw, list)
                           else [price_spec_raw] if isinstance(price_spec_raw, dict) else [])
            if price_spec_raw is not None and not isinstance(price_spec_raw, (dict, list)):
                diagnostics.append({"event": "extraction_gap", "reason": "unsupported_price_specification", "locator": locator})
            if isinstance(price_spec_raw, list) and len(price_spec_raw) > 1000:
                diagnostics.append({"event": "jsonld_truncated", "reason": "price_specification_limit",
                                    "locator": locator, "observed_count": len(price_spec_raw), "processed_count": 1000})
                price_specs = price_spec_raw[:1000]

            emitted_tiers = 0
            for spec_index, price_spec in enumerate(price_specs, 1):
                if processed_price_specs >= _PAGE_CANDIDATE_LIMIT:
                    diagnostics.append({"event": "jsonld_truncated", "reason": "price_specification_page_limit",
                                        "processed_count": _PAGE_CANDIDATE_LIMIT})
                    result.trace.extend(diagnostics)
                    return candidates
                processed_price_specs += 1
                spec_path = f"{locator}/priceSpecification:{spec_index}"
                if not isinstance(price_spec, dict):
                    diagnostics.append({"event": "extraction_gap", "reason": "non_object_price_specification", "locator": spec_path})
                    continue
                if _type(price_spec, "DeliveryChargeSpecification") or _type(price_spec, "PaymentChargeSpecification"):
                    diagnostics.append({"event": "extraction_gap", "reason": "separate_fee", "locator": spec_path})
                    continue
                declared_type = price_spec.get("@type")
                if declared_type and not (_type(price_spec, "PriceSpecification")
                                          or _type(price_spec, "UnitPriceSpecification")):
                    diagnostics.append({"event": "extraction_gap", "reason": "unsupported_price_specification_type",
                                        "locator": spec_path})
                    continue
                if aggregate:
                    diagnostics.append({"event": "extraction_gap", "reason": "aggregate_price_specification", "locator": spec_path})
                    continue
                conditioned = _type(price_spec, "UnitPriceSpecification") or any(
                    key in price_spec for key in ("eligibleQuantity", "referenceQuantity", "eligibleTransactionVolume")
                )
                # An unqualified scalar specification is the offer's sole main
                # price only when no direct price is present. Arrays with several
                # unqualified specifications cannot safely identify the total.
                if not conditioned and len(price_specs) == 1 and "price" not in fields:
                    add("price", price_spec.get("price"), at="Offer.priceSpecification.price")
                    if "currency" not in fields:
                        add("currency", price_spec.get("priceCurrency"), at="Offer.priceSpecification.priceCurrency")
                    continue
                if not conditioned:
                    diagnostics.append({"event": "extraction_gap", "reason": "ambiguous_price_component", "locator": spec_path})
                    continue
                if _scalar(price_spec.get("price")) is None:
                    diagnostics.append({"event": "extraction_gap", "reason": "unit_price_missing", "locator": spec_path})
                    continue
                tier_fields, tier_specs, tier_proofs = dict(fields), dict(specs), dict(proofs)
                add("price", price_spec.get("price"), at="UnitPriceSpecification.price",
                    target_fields=tier_fields, target_specs=tier_specs, target_proofs=tier_proofs, path=spec_path)
                if _scalar(price_spec.get("priceCurrency")) is not None:
                    add("currency", price_spec.get("priceCurrency"), at="UnitPriceSpecification.priceCurrency",
                        target_fields=tier_fields, target_specs=tier_specs, target_proofs=tier_proofs, path=spec_path)
                for condition in ("name", "priceType", "unitCode", "unitText", "billingIncrement"):
                    add(condition, price_spec.get(condition), spec=True, at=f"UnitPriceSpecification.{condition}",
                        target_fields=tier_fields, target_specs=tier_specs, target_proofs=tier_proofs, path=spec_path)
                for quantity_name in ("eligibleQuantity", "referenceQuantity", "eligibleTransactionVolume"):
                    quantity = price_spec.get(quantity_name)
                    if isinstance(quantity, dict):
                        for attr in ("value", "minValue", "maxValue", "unitCode", "unitText"):
                            add(f"{quantity_name}_{attr}", quantity.get(attr), spec=True,
                                at=f"UnitPriceSpecification.{quantity_name}.{attr}", target_fields=tier_fields,
                                target_specs=tier_specs, target_proofs=tier_proofs, path=spec_path)
                tier_flags = flags + ["unit price tier requires condition review"]
                if not append_candidate(Candidate(tier_fields, tier_proofs, spec_path, "json_ld_product_offer",
                                                  tier_specs, source_visibility="unconfirmed", evidence_mode="static_html",
                                                  review_flags=tier_flags)):
                    return candidates
                emitted_tiers += 1
            if not emitted_tiers or "price" in fields or aggregate:
                if not append_candidate(Candidate(fields, proofs, locator, "json_ld_product_offer", specs,
                                                  source_visibility="unconfirmed", evidence_mode="static_html",
                                                  review_flags=flags)):
                    return candidates
    result.trace.extend(diagnostics)
    return candidates
