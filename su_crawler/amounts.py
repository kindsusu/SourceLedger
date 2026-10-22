"""Displayed money amounts and recurring-price conditions for any shop page.

Each parser accepts one whole, unambiguous token and otherwise says why it did
not choose a value. The conversions are replayable from preserved source text
through ``evidence_normalization`` rules, so nothing here can invent a price.
"""
from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation

# Markers that name exactly one currency. "$" and "¥" are shared by several
# currencies and need a page-level hint (structured data or page language).
# Fullwidth signs (￦ ＄ ￥ ￡) appear on Korean and Japanese storefronts.
UNAMBIGUOUS_MARKERS = {"₩": "KRW", "￦": "KRW", "원": "KRW", "€": "EUR", "£": "GBP", "￡": "GBP", "円": "JPY", "元": "CNY"}
_DOLLAR = frozenset({"USD", "CAD", "AUD", "NZD", "SGD", "HKD"})
AMBIGUOUS_MARKERS = {"$": _DOLLAR, "＄": _DOLLAR, "¥": frozenset({"JPY", "CNY"}), "￥": frozenset({"JPY", "CNY"})}
ISO_CODES = frozenset({"AUD", "CAD", "CHF", "CNY", "EUR", "GBP", "HKD", "JPY", "KRW", "NZD", "SGD", "USD", "TWD"})

# Thousands groups use "," "." or a no-break space (never an ordinary space,
# which separates unrelated numbers in flattened card text).
_NUMBER = r"\d{1,3}(?:[,.  ]\d{3})+(?:[.,]\d{1,2})?|\d+(?:[.,]\d{1,2})?"
_PREFIX = r"(?P<pre>₩|￦|\$|＄|€|£|￡|¥|￥|(?:%s)\s?)" % "|".join(sorted(ISO_CODES))
_SUFFIX = r"(?P<suf>\s?(?:만\s*원|원|円|元|€|(?:%s)\b))" % "|".join(sorted(ISO_CODES))
_AMOUNT = re.compile(
    rf"(?:{_PREFIX}\s*(?P<a>{_NUMBER})(?:\s*(?P<man>만)\s*원?)?)|(?:(?P<b>{_NUMBER}){_SUFFIX})"
)
_RANGE = re.compile(rf"(?:{_NUMBER})\s*(?:만\s*원|원|円|€)?\s*[~∼〜\-–—]\s*(?:₩|￦|\$|＄|€|£|￡|¥|￥)?\s*\d")
_FROM = re.compile(r"[~∼〜]\s*$|부터|이상|^\s*from\b|\bstarting\s+(?:at|from)\b|\bfrom\s*(?:₩|￦|\$|＄|€|£|￡|¥|￥)", re.I)


def _number(raw: str, marker: str) -> Decimal | None:
    """Read grouping and decimals from the token's own shape.

    ``1.234,56`` (comma decimal) and ``1,234.56`` are both accepted; a lone
    separator followed by exactly three digits is a thousands group.
    """
    text = re.sub(r"[  ]", "", raw)
    if re.fullmatch(r"\d{1,3}(?:\.\d{3})+,\d{1,2}", text):
        text = text.replace(".", "").replace(",", ".")
    elif re.fullmatch(r"\d{1,3}(?:,\d{3})+(?:\.\d{1,2})?|\d{1,3}(?:\.\d{3})+", text):
        # "1.234" is a group only for currencies without minor units in use.
        text = text.replace(",", "") if "," in text else (
            text.replace(".", "") if marker in {"₩", "￦", "원", "KRW", "円", "JPY"} or len(text.split(".")) > 2 else text)
    elif re.fullmatch(r"\d+,\d{1,2}", text):
        text = text.replace(",", ".")
    elif not re.fullmatch(r"\d+(?:\.\d{1,2})?", text):
        return None
    try:
        value = Decimal(text)
    except InvalidOperation:
        return None
    return value if value.is_finite() and value >= 0 else None


def _plain(value: Decimal) -> str:
    text = format(value.normalize(), "f")
    return text if "." not in text else text.rstrip("0").rstrip(".")


def find_amounts(text: str | None) -> list[dict]:
    """Every currency-marked amount in ``text`` with its exact token."""
    found = []
    for match in _AMOUNT.finditer(text or ""):
        marker = (match.group("pre") or match.group("suf") or "").strip()
        marker = "원" if marker.replace(" ", "") == "만원" else marker
        raw_number = match.group("a") or match.group("b")
        value = _number(raw_number, marker)
        if value is None:
            continue
        if match.group("man") or (match.group("suf") or "").replace(" ", "") == "만원":
            value *= 10_000
        found.append({"token": match.group().strip(), "value": _plain(value), "marker": marker,
                      "start": match.start(), "end": match.end()})
    return found


def displayed_amount(text: str | None, *, allow_from: bool = False) -> tuple[dict | None, str | None]:
    """Exactly one displayed amount, or the reason none was chosen."""
    if not text or not text.strip():
        return None, "no_amount"
    if _RANGE.search(text):
        return None, "price_range_not_single_amount"
    amounts = find_amounts(text)
    if not amounts:
        return None, "no_amount"
    if len({(a["value"], currency_codes(a["marker"])) for a in amounts}) > 1:
        return None, "multiple_amounts_unresolved"
    if not allow_from and _FROM.search(text):
        return None, "starting_price_qualifier"
    return amounts[0], None


def currency_codes(marker: str) -> frozenset[str]:
    marker = marker.strip()
    if marker in UNAMBIGUOUS_MARKERS:
        return frozenset({UNAMBIGUOUS_MARKERS[marker]})
    if marker.upper() in ISO_CODES:
        return frozenset({marker.upper()})
    return AMBIGUOUS_MARKERS.get(marker, frozenset())


def currency_for(marker: str, hint: str | None = None) -> str | None:
    """ISO code for a marker; an ambiguous marker needs a matching page hint."""
    codes = currency_codes(marker)
    if len(codes) == 1:
        return next(iter(codes))
    return hint if hint in codes else None


_MARKETS = {
    "USD": ("us", "usa", "u.s.", "u.s.a.", "united states", "america", "미국"),
    "CAD": ("ca", "canada", "캐나다"), "AUD": ("au", "australia", "호주"),
    "NZD": ("nz", "new zealand", "뉴질랜드"), "SGD": ("sg", "singapore", "싱가포르"),
    "HKD": ("hk", "hong kong", "홍콩"), "JPY": ("jp", "japan", "일본"), "CNY": ("cn", "china", "중국"),
    "KRW": ("kr", "korea", "south korea", "republic of korea", "한국", "대한민국"),
}


def market_currency(market: str | None) -> str | None:
    """The currency of a plan's market named by country or ISO code, else None."""
    text = " ".join(str(market or "").split()).strip().casefold()
    if text.upper() in ISO_CODES:
        return text.upper()
    return next((code for code, names in _MARKETS.items() if text in names), None)


def amount_from_token(token: str) -> str | None:
    """Replay a preserved amount token (used by evidence normalization)."""
    amounts = find_amounts(token)
    if len(amounts) != 1 or amounts[0]["start"] != 0 or amounts[0]["end"] != len(token.strip()):
        return None
    return amounts[0]["value"]


# -- recurring prices --------------------------------------------------------

_BASIS = (
    # A bare "월" counts only directly before an amount, and the marker keeps
    # that amount so the evidence replays on its own.
    ("monthly", re.compile(r"(?:^|(?<=[\s(/·|]))월\s*(?:렌탈료|렌트료|렌탈|렌트|구독료|구독|이용료|요금|납부금|납입금|대여료|사용료)?"
                           r"\s*[:：]?\s*₩?\s*\d[\d,]*(?:\.\d+)?\s*(?:만\s*원|원)?"
                           # Other forms must follow an amount: a title such as
                           # "A Year in Provence" or "월간 잡지" names no billing period.
                           r"|(?:\d|원)\s*(?:/|per\s+|a\s+)\s*(?:월|개월|mo\b|month\b)|매월\s*(?:결제|납부|청구)|월\s*납부"
                           r"|billed\s+monthly", re.I)),
    ("yearly", re.compile(r"(?:\d|원)\s*(?:/|per\s+|a\s+)\s*(?:년|yr\b|year\b)|billed\s+(?:annually|yearly)"
                          r"|연간?\s*(?:구독료|이용료)", re.I)),
    ("weekly", re.compile(r"(?:\d|원)\s*(?:/|per\s+|a\s+)\s*(?:주|wk\b|week\b)|billed\s+weekly", re.I)),
    ("daily", re.compile(r"(?:\d|원)\s*(?:/|per\s+|a\s+)\s*(?:일|day\b)|1일\s*(?:대여료|이용료)|일\s*대여료", re.I)),
)
_TERM_MONTHS = re.compile(r"(\d{1,3})\s*개월\s*(?:약정|계약|의무|렌탈|렌트|구독|이용|할부)?")
_TERM_YEARS = re.compile(r"(\d{1,2})\s*년\s*(?:약정|계약|의무|렌탈|렌트)")
_TERM_EN = re.compile(r"(\d{1,3})[\s-]*months?\b(?:\s*(?:contract|term|plan|commitment))?", re.I)
_DEPOSIT = re.compile(r"(보증금|선납금|deposit|down\s*payment)", re.I)


def price_basis(text: str | None) -> tuple[str | None, str | None]:
    """One recurring basis named in a short price context, with its marker."""
    if not text:
        return None, None
    hits = {}
    for basis, pattern in _BASIS:
        match = pattern.search(text)
        if match:
            hits[basis] = match.group().strip()
    return next(iter(hits.items())) if len(hits) == 1 else (None, None)


def term_months(text: str | None) -> tuple[str | None, str | None]:
    """Exactly one contract term in months, never the first of several."""
    if not text or re.search(r"\d+\s*[-~∼]\s*\d+\s*(?:개월|months?)", text, re.I):
        return None, None
    terms: dict[str, str] = {}
    for match in _TERM_MONTHS.finditer(text):
        terms.setdefault(str(int(match.group(1))), match.group())
    for match in _TERM_YEARS.finditer(text):
        terms.setdefault(str(int(match.group(1)) * 12), match.group())
    for match in _TERM_EN.finditer(text):
        terms.setdefault(str(int(match.group(1))), match.group())
    terms.pop("0", None)
    return next(iter(terms.items())) if len(terms) == 1 else (None, None)


def deposit(text: str | None) -> tuple[dict | None, str | None]:
    """The single amount that directly follows one deposit label."""
    if not text:
        return None, None
    labels = list(_DEPOSIT.finditer(text))
    if len(labels) != 1:
        return None, None
    tail = text[labels[0].end():labels[0].end() + 30]
    amounts = find_amounts(tail)
    if not amounts or amounts[0]["start"] > 6:
        return None, None
    return amounts[0], labels[0].group()


def term_from_token(token: str) -> str | None:
    """Replay a preserved contract-term token (used by evidence normalization)."""
    value, matched = term_months(token)
    return value if matched is not None and matched.strip() == token.strip() else None
