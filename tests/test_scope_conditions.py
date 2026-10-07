"""Conservative structured scope matching across retail categories."""
import pytest

from su_crawler.models import Candidate
from su_crawler.scope_conditions import assess_conditions, normalize_conditions


def product(fields=None, specs=None, *, attested=True):
    fields, specs = fields or {}, specs or {}
    evidence = ({**{key: {"raw": value, "location": f"product.{key}"} for key, value in fields.items()},
                 **{f"spec:{key}": {"raw": value, "location": f"spec.{key}"} for key, value in specs.items()}}
                if attested else {})
    return Candidate(fields, evidence, "fixture", "jsonld", specs)


def check(field, value, candidate, operator="equals"):
    return assess_conditions(candidate, [{"field": field, "operator": operator, "value": value}])["status"]


def test_brand_requires_brand_evidence_and_never_uses_name_or_other_brand():
    assert check("brand", "오뚜기", product({"name": "오뚜기 컵밥"})) == "unknown"
    assert check("brand", "오뚜기", product({"name": "컵밥", "brand": "다른 회사"})) == "excluded"
    assert check("brand", "오뚜기", product({"brand": "오뚜기"})) == "matched"


def test_exact_model_never_matches_suffix_or_substring():
    assert check("model", "6204-2RS", product({"mpn": "6204-2RS-C3"})) == "excluded"
    assert check("model", "6204-2RS", product({"mpn": "6204-2RS-C3"}), "contains") == "matched"
    assert check("model", "6204-2RS", product({"mpn": "6204-2RS-C3"}), "not_contains") == "excluded"
    assert check("model", "6204-2RS", product({"mpn": "６２０４－２ＲＳ"})) == "matched"
    assert check("model", "6204-2RS", product({"name": "Bearing 6204-2RS"})) == "unknown"


@pytest.mark.parametrize("material", ["綿 100%", "綿100％", "cotton 100 %", "100% cotton", "면 100%"])
def test_cotton_material_recognizes_common_order_spacing_and_width(material):
    assert check("material", "cotton 100%", product(specs={"material": material})) == "matched"
    assert check("material", "cotton 100%", product({"name": material})) == "unknown"


def test_generic_material_exact_label_only():
    assert check("material", "stainless steel", product(specs={"material": "Stainless  Steel"})) == "matched"
    assert check("material", "polyester", product(specs={"material": "POLYESTER"})) == "matched"
    assert check("material", "polyester", product(specs={"material": "nylon"})) == "unknown"
    assert check("material", "polyester", product(specs={"material": "polyester blend"})) == "unknown"


def test_new_and_used_negation_do_not_trigger_false_exclusion():
    assert check("condition", "new", product({"condition": "새 제품이며 중고가 아닙니다"})) == "matched"
    assert check("condition", "used", product({"condition": "새 제품이며 중고가 아닙니다"}), "not_equals") == "matched"
    assert check("condition", "new", product({"name": "중고가 아닙니다"})) == "unknown"
    assert check("condition", "new", product({"condition": "https://schema.org/UsedCondition"})) == "excluded"
    assert check("condition", "new", product({"condition": "new and used"})) == "excluded"
    assert check("condition", "new", product({"condition": "not new"})) == "unknown"
    assert check("condition", "new", product({"name": "New Balance 990"})) == "unknown"
    assert check("material", "cotton 100%", product(specs={"material": "not 100% cotton"})) == "unknown"
    assert check("material", "cotton 100%", product(specs={"material": "100% cotton, 20% polyester"})) == "unknown"


def test_multiple_conditions_never_promote_unknown_to_matched():
    outcome = assess_conditions(product({"brand": "오뚜기"}), [
        {"field": "brand", "operator": "equals", "value": "오뚜기"},
        {"field": "condition", "operator": "equals", "value": "new"},
    ])
    assert outcome["status"] == "unknown"
    assert [x["status"] for x in outcome["checks"]] == ["matched", "unknown"]


def test_attestation_conflicts_and_identity_limits():
    assert check("brand", "오뚜기", product({"brand": "오뚜기"}, attested=False)) == "unknown"
    assert check("brand", "오뚜기", product({"brand": "오뚜기"}, {"brand": "타사"})) == "excluded"
    assert check("model", "6204-2RS", product({"sku": "6204-2RS"})) == "unknown"
    assert check("other", "member discount", product({"other": "member discount"})) == "unknown"
    assert assess_conditions(product({"brand": "오뚜기"}), [])["status"] == "not_checked"
    result = assess_conditions(product({"brand": "오뚜기"}), [{"field": "brand", "operator": "equals", "value": "오뚜기"}])
    assert result["checks"][0]["evidence"] == [{"field": "brand", "raw": "오뚜기", "location": "product.brand"}]


def test_group_identity_and_unverified_cross_script_alias_stay_unknown():
    compound = "매일유업 또는 두유 시장에서 매일유업과 경쟁하는 제조사"
    assert check("manufacturer", compound, product({"manufacturer": "정식품"})) == "unknown"
    assert check("brand", "매일유업 혹은 경쟁사", product({"brand": "매일유업"})) == "unknown"
    assert check("brand", "유니클로", product({"brand": "UNIQLO"}), "not_equals") == "unknown"
    assert check("brand", "유니클로", product({"brand": "UNIQLO"})) == "unknown"
    assert check("brand", "유니클로", product({"brand": "다른 브랜드"})) == "excluded"
    assert check("brand", "UNIQLO", product({"brand": "UNIQLO"})) == "matched"
    assert check("brand", "UNIQLO", product({"brand": "GAP"}), "not_equals") == "matched"


def test_reject_invalid_or_excessive_conditions():
    for payload in (None, [{}], [{"field": "brand", "operator": "equals", "value": ""}],
                    [{"field": "brand", "operator": "equals", "value": "a", "extra": 1}],
                    [{"field": "price", "operator": "equals", "value": "1"}],
                    [{"field": "name", "operator": "regex", "value": "a"}],
                    [{"field": "name", "operator": "equals", "value": "a\nb"}],
                    [{"field": "name", "operator": "equals", "value": "a"}] * 21):
        with pytest.raises(ValueError):
            normalize_conditions(payload)
