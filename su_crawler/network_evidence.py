"""Corroborate displayed prices with the site's own product data.

Sources are same-site JSON responses the page loaded and JSON data scripts
embedded in the page (many storefronts render server-side and ship their
product state inline rather than fetching it).

A JSON amount never becomes a price on its own. A row whose shown name and
price match a product record in that data gains a confirmation
(and loses the page-structure heuristic flag, since the site's own data now
agrees with the chosen element). A record with the same name but only other
amounts is a conflict for review. Products present only in JSON are counted
on the trace, never added as rows: recommendation and tracking feeds would
otherwise read as inventory.
"""
from __future__ import annotations

import hashlib
import json
import re
from decimal import Decimal, InvalidOperation
from functools import lru_cache
from typing import Any

from .models import Candidate, FetchResult

# Current-price keys only; list/regular/original/discount amounts are excluded.
PRICE_KEYS = frozenset({"price", "saleprice", "finalprice", "sellprice", "sellingprice", "currentprice",
                        "displayprice", "offerprice", "nowprice", "dcprice", "pricevalue"})
NAME_KEYS = frozenset({"name", "title", "productname", "productnm", "goodsname", "goodsnm", "itemname", "prdnm",
                       "displayname", "productname1"})
PRICE_BLOCKS = frozenset({"prices", "pricing", "priceinfo"})
MAX_NODES = 50_000
MAX_INLINE_BYTES = 5 * 1024 * 1024


def has_product_records(body: bytes) -> bool:
    """True when a JSON body holds at least one named, priced object."""
    return bool(_records((body,)))


def inline_json(soup: Any) -> list[dict[str, Any]]:
    """JSON data scripts embedded in the page (e.g. a framework's initial state)."""
    found = []
    for index, script in enumerate(soup.select('script[type="application/json"], script#__NEXT_DATA__')[:20], 1):
        text = script.string or script.get_text()
        if text and len(text) <= MAX_INLINE_BYTES:
            label = script.get("id") or f"{index}"
            found.append({"url": f"inline:script#{label}", "body": text.encode("utf-8")})
    return found
CONFLICT_FLAG = "network_price_conflict"


def _key(value: str) -> str:
    return re.sub(r"[\s_\-]", "", value).casefold()


def _name(value: Any) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^\w\s]", " ", str(value or ""))).strip().casefold()


def _amount(value: Any) -> Decimal | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        try:
            return Decimal(str(value))
        except InvalidOperation:
            return None
    if isinstance(value, str) and re.fullmatch(r"\s*\d{1,3}(?:,\d{3})*(?:\.\d+)?\s*|\s*\d+(?:\.\d+)?\s*", value):
        return Decimal(value.strip().replace(",", ""))
    if isinstance(value, dict):  # {"price": {"value": 12900}} or {"amount": "12.99"}
        for key in ("value", "amount", "current", "raw"):
            if key in value and not isinstance(value[key], dict):
                return _amount(value[key])
    return None


@lru_cache(maxsize=8)
def _records(bodies: tuple[bytes, ...]) -> tuple[tuple[int, str, str, tuple[tuple[str, Decimal], ...]], ...]:
    """(response index, JSON path, normalized name, ((key, amount), ...)) for product-like objects."""
    found: list[tuple[int, str, str, tuple[tuple[str, Decimal], ...]]] = []
    budget = [MAX_NODES]

    def own_name(value: dict) -> str:
        return next((_name(item) for key, item in value.items()
                     if _key(key) in NAME_KEYS and isinstance(item, str) and item.strip()), "")

    def walk(value: Any, index: int, path: str) -> None:
        if budget[0] <= 0:
            return
        budget[0] -= 1
        if isinstance(value, dict):
            # A product object may keep its title one level down ("copy":
            # {"title": ...}) and its amounts in a "prices" block.
            name = own_name(value) or next((own_name(item) for key, item in value.items()
                                            if isinstance(item, dict) and _key(key) not in PRICE_BLOCKS), "")
            prices = tuple((key, amount) for key, item in value.items()
                           if _key(key) in PRICE_KEYS and (amount := _amount(item)) is not None)
            prices += tuple((f"{block}.{key}", amount) for block, child in value.items()
                            if _key(block) in PRICE_BLOCKS and isinstance(child, dict)
                            for key, item in child.items()
                            if _key(key) in PRICE_KEYS and (amount := _amount(item)) is not None)
            if name and prices:
                found.append((index, path, name, prices))
            for key, item in value.items():
                if isinstance(item, (dict, list)):
                    walk(item, index, f"{path}.{key}")
        elif isinstance(value, list):
            for position, item in enumerate(value[:5000]):
                if isinstance(item, (dict, list)):
                    walk(item, index, f"{path}[{position}]")

    for index, body in enumerate(bodies):
        try:
            data = json.loads(body.decode("utf-8-sig"))
        except (UnicodeDecodeError, ValueError):
            continue
        walk(data, index, "$")
    return tuple(found)


def _same(a: str, b: str) -> bool:
    if a == b:
        return True
    short, long = sorted((a, b), key=len)
    return len(short) >= 6 and short in long


def corroborate(candidates: list[Candidate], result: FetchResult,
                inline: list[dict[str, Any]] | None = None) -> list[Candidate]:
    """Attach site-data confirmations or conflicts to priced candidates in place.

    Sources are the JSON responses the page loaded and JSON data scripts
    embedded in the captured page (``inline``).
    """
    items = [item for item in [*result.network_responses, *(inline or [])] if isinstance(item.get("body"), bytes)]
    if not items or not candidates:
        return candidates
    records = _records(tuple(item["body"] for item in items))
    if not records:
        return candidates
    from .generic_extraction import HEURISTIC_FLAG

    matched: set[int] = set()
    confirmed = conflicts = 0
    for candidate in candidates:
        name = _name(candidate.fields.get("name"))
        try:
            price = Decimal(str(candidate.fields.get("price")))
        except (InvalidOperation, ValueError):
            continue
        same = [position for position, record in enumerate(records) if len(name) >= 4 and _same(name, record[2])]
        if not same:
            continue
        matched.update(same)
        hit = next(((position, key, unit) for position in same for key, amount in records[position][3]
                    for unit, factor in (("major", 1), ("minor", 100)) if amount == price * factor), None)
        if hit is None:
            conflicts += 1
            if CONFLICT_FLAG not in candidate.review_flags:
                candidate.review_flags.append(CONFLICT_FLAG)
            index, path, _, prices = records[same[0]]
            candidate.derived_values["network_price_conflict"] = {
                "url": items[index].get("url"), "path": path,
                "amounts": {key: str(amount) for key, amount in prices}}
            continue
        position, key, unit = hit
        index, path, _, _ = records[position]
        confirmed += 1
        candidate.derived_values["network_confirmation"] = {
            "url": items[index].get("url"), "sha256": hashlib.sha256(items[index]["body"]).hexdigest(),
            "path": path, "field": key, "unit": unit}
        candidate.review_flags = [flag for flag in candidate.review_flags if flag != HEURISTIC_FLAG]
    result.trace.append({"event": "network_corroboration", "records": len(records), "confirmed": confirmed,
                         "conflicts": conflicts, "json_only": len(records) - len(matched)})
    return candidates
