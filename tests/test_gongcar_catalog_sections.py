"""Four public Gongcar sections and honest discovery coverage."""
from su_crawler.adapters.gongcar import extract, section_coverage
from su_crawler.models import FetchResult, Source


def _result(path: str, html: str, trace=None):
    url = f"https://gongcar.kr/{path}"
    result = FetchResult("s", "fetched", "playwright", html.encode("utf-8"), final_url=url,
                         trace=trace or [])
    source = Source("s", "gongcar", "web", url, ["p"], adapter="gongcar")
    return result, extract(result, source)


def _popular(basis: str, *, duplicate=False):
    second = '<button><img alt="Model A"><span>Model A</span><span>월 320,000원~</span></button>' if duplicate else ''
    return f'''<section><h2>인기 차량 한눈에 보기</h2><div class="grid">
    <button><img alt="Model A"><span>Model A</span><span>월 290,000원~</span></button>{second}
    </div><p>* 48개월 / {basis} 기준 | 최근 업데이트 : 26.10.12</p></section>'''


def _promotion(basis: str, *, ambiguous=False):
    price = ('<span>월 400,000원</span><span>월 350,000원</span>' if ambiguous else
             '<span class="line-through">월 400,000원</span><span>월 350,000원</span>')
    return f'''<section><h2>전기차 특가</h2><div style="overflow:hidden">
    <button><img alt="Model B"><p>Model B</p>{price}</button>
    <button hidden><img alt="Hidden"><p>Hidden</p><span>월 1원</span></button>
    </div><p>* 48개월 / {basis} 기준 | 최근 업데이트 : 26.10.12</p></section>'''


def test_rentpass_sections_preserve_distinct_offers_and_price_roles():
    html = _popular("보증금 30%", duplicate=True) + _promotion("보증금 0원")
    result, offers = _result("rentpass", html, [{"event": "list_expansion", "section": "popular",
                                                  "status": "complete", "reason": "button_disappeared",
                                                  "observed_count": 2, "click_count": 1},
                                                 {"event": "promotion_inspection", "section": "promotion",
                                                  "status": "complete", "reason": "overflow_inspected",
                                                  "observed_count": 1}])
    assert len(offers) == 3
    assert offers[0].fields["name"] == offers[1].fields["name"]
    assert offers[0].fields["item_id"] != offers[1].fields["item_id"]
    assert all(offer.fields["item_id"].startswith("local-observation-") for offer in offers)
    assert [offer.fields["price"] for offer in offers] == ["290000", "320000", "350000"]
    assert offers[2].fields["reference_price"] == "400000"
    assert offers[2].fields["deposit_amount"] == "0"
    assert "deposit_percent" not in offers[2].fields
    assert offers[0].fields["update_text"] == "26.10.12"
    assert [entry["status"] for entry in section_coverage(result, offers)] == ["complete", "complete"]
    assert section_coverage(result, offers)[0]["click_count"] == 1


def test_longterm_advance_is_separate_and_unconfirmed_roles_remain_unpriced():
    result, offers = _result("longterm", _popular("선납금 30%") +
                             _promotion("선납금 30%", ambiguous=True))
    assert len(offers) == 2
    assert all(offer.fields["advance_percent"] == "30" for offer in offers)
    assert all("deposit_percent" not in offer.fields for offer in offers)
    assert "price" not in offers[1].fields
    assert "current_price_role_unresolved" in offers[1].review_flags
    coverage = section_coverage(result, offers)
    assert coverage[0]["status"] == "partial"
    assert coverage[1]["observed_count"] == 1
    assert coverage[1]["price_count"] == 0
    assert coverage[1]["status"] == "partial"


def test_missing_or_unparsed_section_does_not_claim_completion():
    result, offers = _result("rentpass", _popular("보증금 30%").replace("월 290,000원~", "상담가"))
    coverage = section_coverage(result, offers)
    assert coverage[0]["observed_count"] == 1
    assert coverage[0]["price_count"] == 0
    assert coverage[0]["status"] == "partial"
    assert coverage[1]["status"] == "unknown"
    assert coverage[1]["reason"] == "section_not_discovered"


def test_popular_completion_requires_matching_expansion_count():
    html = _popular("보증금 30%", duplicate=True)
    result, offers = _result("rentpass", html, [{"event": "list_expansion", "section": "popular",
                                                 "status": "complete", "observed_count": 1,
                                                 "reason": "button_disappeared", "click_count": 1}])
    assert section_coverage(result, offers)[0]["status"] == "partial"


def test_visible_name_survives_whitespace_alt_and_missing_lazy_image():
    html = '''<section><h2>인기 차량 한눈에 보기</h2><div class="grid">
    <button><img alt="더 뉴  K8"><span class="truncate">더 뉴 K8</span><span>월 630,000원~</span></button>
    <button><div class="image-placeholder"></div><span class="truncate">G80 EV</span>
    <span>월 1,465,000원~</span></button></div>
    <p>* 48개월 / 선납금 30% 기준</p></section>'''
    result, offers = _result("longterm", html)
    assert [offer.fields["name"] for offer in offers] == ["더 뉴 K8", "G80 EV"]
    assert [offer.fields["price"] for offer in offers] == ["630000", "1465000"]
    assert section_coverage(result, offers)[0]["unparsed_count"] == 0


def test_promotion_grid_without_images_and_repeated_public_model_id():
    html = '''<section><h2>전기차 특가</h2><div class="grid" style="overflow:hidden">
    <button data-car-id="model-42"><p class="truncate">Model A</p>
    <span class="line-through">월 400,000원</span><span>월 350,000원</span></button>
    <button data-car-id="model-42"><p class="truncate">Model A</p>
    <span class="line-through">월 420,000원</span><span>월 360,000원</span></button>
    <button hidden data-car-id="model-42"><p class="truncate">Hidden</p><span>월 1원</span></button>
    </div><p>* 48개월 / 보증금 0원 기준</p></section>'''
    result, offers = _result("rentpass", html)
    assert len(offers) == 2
    assert [offer.fields["price"] for offer in offers] == ["350000", "360000"]
    assert [offer.fields["reference_price"] for offer in offers] == ["400000", "420000"]
    assert [offer.fields["observed_source_id"] for offer in offers] == ["model-42", "model-42"]
    assert len({offer.fields["item_id"] for offer in offers}) == 2
    assert all(offer.fields["item_id"].startswith("local-observation-") for offer in offers)
    assert all(offer.evidence["item_id"]["proof_kind"] == "local_observation" for offer in offers)
    assert all(offer.evidence["observed_source_id"]["proof_kind"] == "source_identity" for offer in offers)
    assert section_coverage(result, offers)[1]["observed_count"] == 2
