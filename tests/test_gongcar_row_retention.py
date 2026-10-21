"""Gongcar offers without a numeric price are kept for review, and identities ignore grid position."""
from pathlib import Path

from su_crawler.extraction import extract
from su_crawler.adapters.common import disambiguate_identical_ids
from su_crawler.models import Candidate, FetchResult, Source

FIXTURES = Path(__file__).parent / "fixtures" / "adapters"


def _public(html: str):
    source = Source("s", "gongcar", "web", "https://gongcarrent.kr/public-quote", ["p"], adapter="gongcar")
    return extract(FetchResult("s", "fetched", "playwright", html.encode("utf-8"), final_url=source.location), source)


def test_public_table_inquiry_price_row_is_kept_with_null_price():
    base = (FIXTURES / "gongcar_public_layout.html").read_text(encoding="utf-8")
    before = _public(base)
    price = before[0].fields["price"]
    formatted = f"{int(price):,}"
    assert formatted in base
    after = _public(base.replace(formatted, "문의", 1))
    assert len(after) == len(before)
    inquiry = [value for value in after if "price" not in value.fields]
    assert len(inquiry) == 1
    row = inquiry[0]
    assert row.fields["availability"] == "문의" and "currency" not in row.fields
    assert "price_inquiry_or_unavailable" in row.review_flags
    assert row.evidence["price"]["raw"] is None


def test_identical_cards_get_occurrence_suffix_and_distinct_cards_keep_ids():
    def card(item_id):
        return Candidate(fields={"item_id": item_id}, evidence={"item_id": {"raw": item_id}}, locator=item_id,
                         extraction_method="synthetic")

    cards = disambiguate_identical_ids([card("a"), card("b"), card("a")])
    assert [c.fields["item_id"] for c in cards] == ["a", "b", "a#2"]
    assert cards[2].evidence["item_id"]["raw"] == "a#2"
    reordered = disambiguate_identical_ids([card("b"), card("a")])
    assert [c.fields["item_id"] for c in reordered] == ["b", "a"]


def _rentpass(*names: str) -> str:
    cards = "".join(f'<button><img alt="{name}"><span>{name}</span><span>Gasoline</span><span>월 300,000원~</span></button>'
                    for name in names)
    return ('<section><h2>인기 차량 한눈에 보기</h2><div class="grid">' + cards +
            '</div><p>* 48개월 / 보증금 30% 기준 | 최근 업데이트 : 26.10.12</p></section>')


def test_reordering_distinct_cards_keeps_each_identity():
    source = Source("s", "gongcar", "web", "https://gongcar.kr/rentpass", ["p"], adapter="gongcar")

    def ids(html):
        result = FetchResult("s", "fetched", "playwright", html.encode("utf-8"), final_url=source.location)
        return {value.fields["name"]: value.fields["item_id"] for value in extract(result, source)}

    assert ids(_rentpass("Model A", "Model B")) == ids(_rentpass("Model B", "Model A"))