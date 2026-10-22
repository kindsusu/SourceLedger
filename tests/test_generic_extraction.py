"""Adapter-less extraction across product genres, with synthetic pages only."""
from su_crawler import catalog_collection
from su_crawler.amounts import deposit, displayed_amount, price_basis, term_months
from su_crawler.evidence_normalization import evidence_attests
from su_crawler.extraction import extract
from su_crawler.generic_extraction import extract_generic, generic_sections, pagination_links, product_links
from su_crawler.models import FetchResult, Product, Source
from su_crawler.validation import validate


def page(html, url="https://shop.example/list", backend="playwright"):
    return FetchResult("s", "fetched", backend, content=html.encode("utf-8"), final_url=url)


def observe(candidate, profile):
    key = "item_id" if "item_id" in candidate.fields else "url_id"
    product = Product("p", "Product", identifiers={key: candidate.fields[key]}, price_profile=profile)
    source = Source("s", "Shop", "web", "https://shop.example/", ["p"])
    return validate(candidate, product, source, run_id="r", task_id="t", evidence_path="e", evidence_sha256="x",
                    collected_at="2026-10-22T00:00:00+00:00", source_url=source.location)


def test_amounts_accept_one_whole_token_only():
    assert displayed_amount("12,900원")[0]["value"] == "12900"
    assert displayed_amount("€1.234,56")[0]["value"] == "1234.56"
    assert displayed_amount("45만원")[0]["value"] == "450000"
    assert displayed_amount("3,500円")[0]["value"] == "3500"
    assert displayed_amount("12,000원 ~ 15,000원") == (None, "price_range_not_single_amount")
    assert displayed_amount("10,000원~") == (None, "starting_price_qualifier")
    assert displayed_amount("정가 30,000원 판매가 25,000원") == (None, "multiple_amounts_unresolved")
    # An ordinary space never joins two numbers into one amount.
    assert displayed_amount("수량 3 500원")[0]["value"] == "500"
    assert price_basis("월 렌탈료 29,900원") == ("monthly", "월 렌탈료 29,900원")
    assert price_basis("3월 15일 출시 12,000원") == (None, None)
    # Titles that merely mention a period are not billing periods.
    assert price_basis("A Year in Provence £56.88") == (None, None)
    assert price_basis("Monthly Planner $12") == (None, None)
    assert price_basis("월간 잡지 9,000원") == (None, None)
    assert price_basis("$99 billed annually")[0] == "yearly"
    assert price_basis("$12/mo")[0] == "monthly"
    assert term_months("3년 약정") == ("36", "3년 약정")
    assert term_months("36-48개월") == (None, None)
    assert deposit("보증금 100만원 / 월 30만원")[0]["value"] == "1000000"


def test_replayable_normalization_rules():
    assert evidence_attests("price", "₩12,900", "12900", {"rule": "displayed_amount"})
    assert not evidence_attests("price", "12,900원 할인", "12900", {"rule": "displayed_amount"})
    assert evidence_attests("currency", "원", "KRW", {"rule": "currency_marker"})
    assert not evidence_attests("currency", "$", "USD", {"rule": "currency_marker"})
    assert evidence_attests("term_months", "24개월 약정", "24", {"rule": "contract_term"})
    assert evidence_attests("price_basis", "원/월", "monthly", {"rule": "recurring_marker"})
    assert not evidence_attests("price_basis", "/월", "monthly", {"rule": "recurring_marker"})


LIST = """<html><body><header><a href="/cart">장바구니 0원</a></header><ul class="goods-list">{}</ul>
<div class="paging"><a href="/list?page=2">2</a><a href="/list?page=3">3</a></div></body></html>"""
CARD = ('<li class="item"><a href="/goods/view?goodsNo={i}"><img alt="크림 {i}"></a>'
        '<p class="name"><a href="/goods/view?goodsNo={i}">수분 크림 {i}호</a></p>'
        '<div class="price"><del>{ref:,}원</del> <strong class="sale">{price:,}원</strong></div>'
        '<span class="ship">배송비 3,000원</span></li>')


def cosmetics_list(backend="playwright"):
    cards = "".join(CARD.format(i=i, ref=20000 + i * 1000, price=15000 + i * 1000) for i in range(1, 5))
    return page(LIST.format(cards), backend=backend)


def test_repeated_cards_take_current_price_and_suggest_a_rule():
    result = cosmetics_list()
    candidates = extract_generic(result)
    assert [c.fields["price"] for c in candidates] == ["16000", "17000", "18000", "19000"]
    first = candidates[0]
    assert first.fields["currency"] == "KRW" and first.fields["reference_price"] == "21000"
    assert first.fields["url_id"] == "goodsno=1" and first.evidence_mode == "rendered_dom"
    assert any("heuristic" in flag for flag in first.review_flags)
    section = generic_sections(result)[0]
    assert section["status"] == "unknown" and section["observed_count"] == 4
    assert section["suggested_rule"] == {"row_selector": "ul.goods-list > li.item",
                                         "selectors": {"name": "p.name", "price": "strong.sale",
                                                       "product_url": "a::attr(href)"}}
    assert product_links(result)[0] == "https://shop.example/goods/view?goodsNo=1"
    assert pagination_links(result) == ["https://shop.example/list?page=2", "https://shop.example/list?page=3"]


def test_discovery_spends_page_budget_on_products_before_menus():
    result = cosmetics_list()
    links = catalog_collection._links(result, result.final_url, "shop.example", set())
    assert links[0].endswith("goodsNo=1") and links.index("https://shop.example/cart") > 5


def test_recurring_rental_cards_from_any_genre():
    cards = "".join(f'<div class="card"><a href="/rental/{i}"><h3 class="title">정수기 {i}</h3></a>'
                    f'<p class="cond">36개월 약정</p><p class="fee">월 {29900 + i * 1000:,}원</p></div>'
                    for i in range(1, 4))
    candidates = extract_generic(page(f'<section class="list">{cards}</section>', "https://rent.example/list"))
    assert [c.fields["price"] for c in candidates] == ["30900", "31900", "32900"]
    assert {(c.fields["price_basis"], c.fields["term_months"]) for c in candidates} == {("monthly", "36")}
    assert catalog_collection._price_profile(Source("s", "s", "web", "https://rent.example/", []),
                                             candidates[0]) == "recurring"


JSONLD = ('<html><head><script type="application/ld+json">{"@type":"Product","name":"Desk Lamp",'
          '"offers":{"@type":"Offer","price":"49.99","priceCurrency":"USD"}}</script></head>'
          '<body><h1>Desk Lamp</h1><span class="price">$49.99</span></body></html>')


def test_structured_price_is_proven_only_by_a_rendered_matching_amount():
    rendered = extract_generic(page(JSONLD, "https://store.example/products/desk-lamp"))[0]
    assert rendered.fields["price"] == "49.99" and rendered.fields["currency"] == "USD"
    assert rendered.evidence["price"]["display_state"] == "visible" and rendered.review_flags == []
    assert rendered.evidence["price"]["structured_location"].startswith("jsonld:")
    observation = observe(rendered, "sale")
    assert observation.status == "verified" and observation.comparable
    static = extract_generic(page(JSONLD, "https://store.example/products/desk-lamp", backend="http"))[0]
    assert "embedded JSON-LD price has not been visually verified" in static.review_flags
    assert not observe(static, "sale").comparable
    other = extract_generic(page(JSONLD.replace("$49.99", "$59.99"), "https://store.example/products/desk-lamp"))[0]
    assert "embedded JSON-LD price has not been visually verified" in other.review_flags


def test_single_product_ignores_reference_reward_and_related_prices():
    html = ('<html><body><nav><span>₩0</span></nav><h1>무선 이어폰 X</h1><div class="info">'
            '<span class="consumer">정가 129,000원</span><span class="sale-price">판매가 99,000원</span>'
            '<span>적립금 990원</span><span>+5,000원</span></div>'
            '<aside class="related"><span>59,000원</span></aside></body></html>')
    result = page(html, "https://audio.example/product/detail.html?product_no=77")
    [candidate] = extract_generic(result)
    assert candidate.fields["price"] == "99000" and candidate.fields["reference_price"] == "129000"
    assert candidate.fields["url_id"] == "product_no=77"
    assert generic_sections(result)[0]["suggested_rule"] == {"selectors": {"name": "h1", "price": "span.sale-price"}}


def test_ambiguous_page_is_a_gap_not_a_guess():
    result = page('<html><body><h1>세트</h1><p>A 10,000원</p><p>B 15,000원</p></body></html>', "https://x.example/p/1")
    assert extract_generic(result) == []
    assert result.trace == [{"event": "extraction_gap", "reason": "multiple_amounts_unresolved"}]
    assert generic_sections(result)[0]["status"] == "unknown"


def test_platform_cards_are_complete_and_strip_labels():
    woo = ('<ul class="products"><li class="product post-11"><a href="/p/a"><h2 class="woocommerce-loop-product__title">'
           '책 A</h2><span class="price"><del aria-hidden="true"><span class="amount">₩18,000</span></del>'
           '<ins aria-hidden="true"><span class="amount">₩16,200</span></ins></span></a></li>'
           '<li class="product post-12 outofstock"><a href="/p/b"><h2 class="woocommerce-loop-product__title">책 B</h2>'
           '<span class="price"><span class="amount">₩22,000</span></span></a></li></ul>')
    result = page(woo, "https://books.example/shop/")
    books = extract_generic(result)
    assert [(c.fields["item_id"], c.fields["price"]) for c in books] == [("post-11", "16200"), ("post-12", "22000")]
    assert books[0].fields["reference_price"] == "18000" and books[1].fields["availability"] == "out_of_stock"
    assert generic_sections(result)[0]["status"] == "complete"
    cafe24 = ('<ul class="prdList"><li id="anchorBoxId_101"><div class="name"><a href="/product/a/101/">'
              '<span class="title displaynone">상품명</span> <span>라면 세트</span></a></div><ul class="spec">'
              '<li><strong class="title">판매가</strong> <span>12,900원</span></li>'
              '<li><strong class="title">소비자가</strong> <span>15,000원</span></li></ul></li></ul>')
    [ramen] = extract_generic(page(cafe24, "https://food.example/category/ramen/42/"))
    assert ramen.fields["name"] == "라면 세트" and ramen.fields["price"] == "12900"
    assert ramen.fields["item_id"] == "product_no=101" and ramen.review_flags == []


def test_sale_and_recurring_profiles_compare_only_proven_conditions():
    [book] = extract_generic(page('<ul class="products"><li class="product post-9"><a href="/p/c">'
                                  '<h2 class="woocommerce-loop-product__title">책 C</h2><span class="price">'
                                  '<span class="amount">₩9,000</span></span></a></li></ul>'))
    sale = observe(book, "sale")
    assert sale.status == "verified" and sale.comparable and sale.normalized_amount == "9000"
    assert '"currency":"KRW"' in sale.comparison_key
    cards = "".join(f'<div class="card"><a href="/r/{i}"><h3 class="title">공기청정기 {i}</h3></a>'
                    f'<p class="fee">월 19,900원 · 60개월 약정</p></div>' for i in range(1, 4))
    rental = extract_generic(page(f'<section class="list">{cards}</section>', "https://rent.example/list"))[0]
    rental.review_flags.clear()  # as after the suggested rule was reviewed
    recurring = observe(rental, "recurring")
    assert recurring.comparable and recurring.calculation == "amount per monthly period"
    assert '"term_months":"60"' in recurring.comparison_key
    rental.fields["price_basis"] = "weekly"
    assert not observe(rental, "recurring").comparable


def test_pinned_selectors_prove_currency_from_the_price_marker():
    source = Source("s", "Shop", "web", "https://shop.example/list", [], row_selector="ul.goods-list > li.item",
                    selectors={"name": "p.name", "price": "strong.sale"})
    candidates = extract(cosmetics_list(), source)
    assert candidates[0].fields["price"] == "16,000원" and candidates[0].fields["currency"] == "KRW"
    assert evidence_attests("currency", candidates[0].evidence["currency"]["raw"], "KRW",
                            candidates[0].evidence["currency"]["normalization"])


# -- cases found while checking public storefronts (synthetic markup) ---------

def test_fullwidth_sign_split_decimal_and_aria_hidden_visual_price():
    assert displayed_amount("￦7,900")[0]["value"] == "7900"
    cards = "".join(f'<div class="card"><a href="/p/{i}"><h3>Chair {i}</h3></a><span class="price">'
                    f'<span aria-hidden="true"><span class="cur">$</span><span class="int">{i}9</span>'
                    f'<span class="dec">.99</span></span><span class="price__sr-text">Price $ {i}9.99</span>'
                    f'</span></div>' for i in range(1, 4))
    candidates = extract_generic(page(f'<main class="grid">{cards}</main>', "https://shop.example/chairs"))
    assert [c.fields["price"] for c in candidates] == ["19.99", "29.99", "39.99"]
    assert candidates[0].evidence["price"]["display_state"] == "visible"


def test_product_page_beats_recommended_cards_and_body_classes_are_not_off_content():
    related = "".join(f'<li><a href="/b/{i}"><h3>Other {i}</h3></a><p class="price">£{i}0.00</p></li>'
                      for i in range(1, 5))
    html = (f'<body class="has-related-articles"><div class="main"><h1>Main Book</h1><p class="price">£45.17</p>'
            f'<table><tr><td>£45.17</td></tr><tr><td>£0.00</td></tr></table></div>'
            f'<section><h2>Recently viewed</h2><ol>{related}</ol></section></body>')
    [candidate] = extract_generic(page(html, "https://shop.example/b/main"))
    assert (candidate.fields["name"], candidate.fields["price"]) == ("Main Book", "45.17")


def test_card_ignores_related_item_link_and_uses_discount_rate():
    cards = "".join(f'<li class="item"><a href="/goods/{i}" class="title">Book {i}</a>'
                    f'<span class="rate">10%</span><strong>16,650원</strong> <span class="dash">18,500원</span>'
                    f'<div>eBook <a href="/goods/{i}00"><span>11,700원</span></a></div></li>' for i in range(1, 4))
    candidates = extract_generic(page(f'<ul class="list">{cards}</ul>', backend="http"))
    assert {(c.fields["price"], c.fields["reference_price"]) for c in candidates} == {("16650", "18500")}


def test_sole_price_under_screen_reader_regular_label_is_the_price():
    cards = "".join(f'<article class="card"><a href="/p/{i}"><h4>Tee {i}</h4></a><span class="price">'
                    f'<span class="sr-only">Regular Price: </span>${30 + i}</span></article>' for i in range(1, 4))
    candidates = extract_generic(page(f'<div class="grid">{cards}</div>', "https://shop.example/c"))
    assert [c.fields["price"] for c in candidates] == ["31", "32", "33"]
    assert candidates[0].fields["name"] == "Tee 1"


def test_plan_price_lines_skip_trial_amounts():
    html = ('<article><h1>Plans</h1><ul><li><span>Basic: 월 7,000원</span></li><li><span>Standard: 월 13,500원</span></li>'
            '<li><span>Extra member: 월 4,000원, 월 5,000원</span></li><li><span>첫 달 ₩0</span></li></ul></article>')
    candidates = extract_generic(page(html, "https://plans.example/prices"))
    assert [(c.fields["name"], c.fields["price"], c.fields["price_basis"]) for c in candidates] == [
        ("Basic", "7000", "monthly"), ("Standard", "13500", "monthly")]


def test_structured_list_rows_confirmed_by_card_name_and_stubs_dropped():
    import json as _json
    items = [{"@type": "Product", "name": f"Set {i}", "offers": {"@type": "Offer", "price": f"{i}9900",
                                                                 "priceCurrency": "KRW"}} for i in range(1, 3)]
    data = {"@type": "ItemList", "itemListElement": items + [{"@type": "Product", "url": "/stub"}]}
    cards = "".join(f'<div class="c"><a href="/s/{i}">Set {i}</a><span>{i}9,900원</span></div>' for i in range(1, 3))
    html = f'<script type="application/ld+json">{_json.dumps(data)}</script><main>{cards}</main>'
    candidates = extract_generic(page(html, "https://toys.example/new"))
    assert [c.fields["price"] for c in candidates] == ["19900", "29900"]
    assert all(c.evidence["price"]["display_state"] == "visible" for c in candidates)
