"""Conservative, evidence-based assessment of structured product conditions.

An absent field is unknown, never a match.  This module does not turn a page
title, search result, or seller hostname into product evidence.
"""
from __future__ import annotations

import re
import unicodedata
from typing import Any


FIELDS = ("brand", "manufacturer", "seller", "model", "material", "condition", "category", "name", "other")
OPERATORS = ("equals", "not_equals", "contains", "not_contains")
CONDITION_SCHEMA = {
    "type": "array", "maxItems": 20,
    "items": {"type": "object", "additionalProperties": False,
              "required": ["field", "operator", "value"],
              "properties": {"field": {"type": "string", "enum": list(FIELDS)},
                             "operator": {"type": "string", "enum": list(OPERATORS)},
                             "value": {"type": "string", "minLength": 1, "maxLength": 160}}},
}


def normalize_text(value: Any) -> str:
    """Normalize Unicode and whitespace for comparisons only; raw data stays intact."""
    return " ".join(unicodedata.normalize("NFKC", str(value)).casefold().split())


def _scripts(value: str) -> set[str]:
    scripts = set()
    for char in value:
        code = ord(char)
        if 0xAC00 <= code <= 0xD7AF:
            scripts.add("hangul")
        elif 0x3040 <= code <= 0x30FF or 0x4E00 <= code <= 0x9FFF:
            scripts.add("cjk")
        elif "a" <= char.casefold() <= "z":
            scripts.add("latin")
    return scripts


def _cross_script(a: str, b: str) -> bool:
    left, right = _scripts(a), _scripts(b)
    return bool(left and right and left.isdisjoint(right))


def _group_identity(value: str) -> bool:
    text = normalize_text(value)
    return bool(re.search(r"\b(?:or|competitors?|rivals?|other\s+(?:brands?|companies|manufacturers))\b|"
                          r"또는|혹은|경쟁\s*(?:사|업체|하는|브랜드)|제조사들|브랜드들|업체들|"
                          r"または|あるいは|競合|他社", text))


def normalize_conditions(value: Any) -> list[dict[str, str]]:
    if not isinstance(value, list) or len(value) > 20:
        raise ValueError("conditions must be a list of at most 20 items")
    normalized = []
    for item in value:
        if not isinstance(item, dict) or set(item) != {"field", "operator", "value"}:
            raise ValueError("Invalid condition")
        field, operator, raw = item["field"], item["operator"], item["value"]
        if (field not in FIELDS or operator not in OPERATORS or not isinstance(raw, str)
                or not raw.strip() or len(raw) > 160 or any(unicodedata.category(ch) == "Cc" for ch in raw)):
            raise ValueError("Invalid condition")
        normalized.append({"field": field, "operator": operator, "value": raw.strip()})
    return normalized


def _material(value: str) -> str | None:
    text = normalize_text(value)
    if re.fullmatch(r"(?:cotton|면|綿|コットン)\s*100\s*%|100\s*%\s*(?:cotton|면|綿|コットン)", text):
        return "cotton:100%"
    # An unfamiliar but simple material label can be compared literally. Do
    # not interpret negations, blends, or multi-material descriptions.
    if (text and not re.search(r"\b(?:not|non|no|blend|mixed|without)\b|아니|없음|혼방|混紡|非|無|不含|[,/+&;]", text)
            and len(text) <= 160):
        return "literal:" + text
    return None


def _condition(value: str) -> str | None:
    text = normalize_text(value)
    aliases = {
        "new": {"new", "brand new", "새 제품", "새제품", "신품", "新品", "未使用", "https://schema.org/newcondition", "http://schema.org/newcondition"},
        "used": {"used", "secondhand", "second hand", "중고", "중고품", "中古", "https://schema.org/usedcondition", "http://schema.org/usedcondition"},
        "refurbished": {"refurbished", "renewed", "재생품", "리퍼", "整備済", "https://schema.org/refurbishedcondition", "http://schema.org/refurbishedcondition"},
    }
    for canonical, values in aliases.items():
        if text in values:
            return canonical
    if re.fullmatch(r"새\s*제품이며\s*중고\s*(?:가|는|이)?\s*아닙니다", text):
        return "new"
    return None


def _condition_conflicts(value: str) -> bool:
    text = normalize_text(value)
    if _condition(text):
        return False
    new = bool(re.search(r"\bnew\b|새\s*제품|신품|新品", text)) and not bool(re.search(r"not\s+new|새\s*제품\s*이?\s*아니", text))
    used = bool(re.search(r"\bused\b|중고|中古", text)) and not bool(re.search(r"not\s+used|중고\s*(?:가|는|이)?\s*(?:아니|아닙|아님|않)", text))
    refurbished = bool(re.search(r"\brefurbished\b|재생품|リファービッシュ", text))
    return sum((new, used, refurbished)) > 1


def _values(candidate: Any, field: str) -> tuple[list[dict[str, str]], bool]:
    fields = candidate.fields
    specs = candidate.specs
    mapping = {
        "brand": ((fields, "brand"), (specs, "brand")),
        "manufacturer": ((fields, "manufacturer"), (specs, "manufacturer")),
        "seller": ((fields, "seller"), (specs, "seller")),
        "model": ((fields, "model"), (fields, "mpn")),
        "material": ((specs, "material"), (specs, "offer_material"), (fields, "material")),
        "condition": ((fields, "condition"), (fields, "offer_condition"), (fields, "item_condition"),
                      (specs, "condition"), (specs, "item_condition")),
        "category": ((specs, "category"), (fields, "category")),
        "name": ((fields, "name"),),
        "other": (),
    }
    values, unattested = [], False
    evidence = candidate.evidence or {}
    for source, key in mapping[field]:
        raw = source.get(key)
        if raw is None or not str(raw).strip():
            continue
        evidence_key = f"spec:{key}" if source is specs else key
        proof = evidence.get(evidence_key)
        if (not isinstance(proof, dict) or not str(proof.get("location") or "").strip()
                or proof.get("raw") is None or str(proof["raw"]) != str(raw)):
            unattested = True
            continue
        values.append({"field": evidence_key, "raw": str(raw), "location": str(proof["location"])})
    return values, unattested


def assess_conditions(candidate: Any, conditions: list[dict[str, str]]) -> dict[str, Any]:
    """Return matched, unknown, or excluded with a reason per condition."""
    if not conditions:
        return {"status": "not_checked", "checks": [], "reason": "No structured product conditions were provided"}
    results = []
    for item in conditions:
        field, operator, target = item["field"], item["operator"], item["value"]
        if field in {"brand", "manufacturer", "seller", "model"} and _group_identity(target):
            results.append({"condition": item, "status": "unknown", "reason": "Unsupported group identity condition", "evidence": []})
            continue
        if field == "other":
            results.append({"condition": item, "status": "unknown", "reason": "Other conditions need manual review", "evidence": []})
            continue
        evidence, unattested = _values(candidate, field)
        raw_values = [proof["raw"] for proof in evidence]
        values = [normalize_text(value) for value in raw_values]
        wanted = normalize_text(target)
        if unattested:
            results.append({"condition": item, "status": "unknown", "reason": f"{field} value lacks source evidence", "evidence": evidence})
            continue
        if field in {"condition", "material"}:
            canonical = _condition if field == "condition" else _material
            target_value = canonical(target)
            recognized = [canonical(value) for value in raw_values]
            values = [value for value in recognized if value]
            if field == "condition" and (any(_condition_conflicts(value) for value in raw_values) or len(set(values)) > 1):
                results.append({"condition": item, "status": "excluded", "reason": "condition evidence conflicts", "evidence": evidence})
                continue
            if not target_value or not values or len(values) != len(raw_values):
                results.append({"condition": item, "status": "unknown", "reason": f"{field} evidence is absent or unrecognized", "evidence": evidence})
                continue
            wanted = target_value
            if field == "material" and (wanted.startswith("literal:") or any(value.startswith("literal:") for value in values)):
                if any(value != wanted for value in values):
                    results.append({"condition": item, "status": "unknown", "reason": "Material vocabulary differs; no safe alias", "evidence": evidence})
                    continue
        elif not values:
            results.append({"condition": item, "status": "unknown", "reason": f"{field} evidence is absent", "evidence": evidence})
            continue
        elif field not in {"name"} and len(set(values)) > 1:
            status = "unknown" if any(_cross_script(a, b) for a in values for b in values) else "excluded"
            results.append({"condition": item, "status": status,
                            "reason": f"{field} evidence has unresolved aliases" if status == "unknown" else f"{field} evidence conflicts",
                            "evidence": evidence})
            continue
        if field in {"brand", "manufacturer", "seller", "model"} and all(value != wanted for value in values):
            if any(_cross_script(value, wanted) for value in values):
                results.append({"condition": item, "status": "unknown", "reason": "Identity may use an unverified cross-script alias", "evidence": evidence})
                continue
        if operator in {"equals", "not_equals"}:
            relation = any(value == wanted for value in values)
        else:
            relation = any(wanted in value for value in values)
        matched = relation if operator in {"equals", "contains"} else not relation
        results.append({"condition": item, "status": "matched" if matched else "excluded", "evidence": evidence,
                        "reason": "attested field matches" if matched else "attested field conflicts"})
    status = "excluded" if any(row["status"] == "excluded" for row in results) else "unknown" if any(row["status"] == "unknown" for row in results) else "matched"
    return {"status": status, "checks": results}
