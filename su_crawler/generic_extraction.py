"""Extraction for ordinary shop pages that have no site adapter.

The chain is conservative. Each step either ties a price to what the page
shows (or to a structured record whose amount is also shown), or leaves the
page as an explicit coverage gap:

1. Storefront platforms with stable card markup (Cafe24, WooCommerce).
2. Embedded Product JSON-LD and microdata/Open Graph prices. A rendered
   capture that visibly shows the same amount upgrades the price proof.
3. Repeated product cards: sibling blocks sharing markup that each carry a
   product link and one current price.
4. A single product page: one distinct current price in the main content.

Steps 3 and 4 are heuristics. Their rows stay in review, and their section
carries a suggested ``collection_rules`` entry that can be pinned (after the
user approves the plan revision) so later runs extract deterministically.
"""
from __future__ import annotations

import copy
import re
from collections import OrderedDict
from dataclasses import asdict
from decimal import Decimal, InvalidOperation
from functools import lru_cache
from typing import Any
from urllib.parse import parse_qs, urljoin, urlsplit

from bs4 import Comment, NavigableString, Tag

from .adapters.common import display_state, html_soup, is_struck
from .amounts import ISO_CODES, currency_codes, currency_for, deposit, displayed_amount, find_amounts, price_basis, term_months
from .models import Candidate, FetchResult, Source

GENERIC_VERSION = "1"
HEURISTIC_FLAG = "Price chosen by page-structure heuristic; review once, then pin the suggested rule"
UNCONFIRMED_STRUCTURED_FLAG = "embedded JSON-LD price has not been visually verified"

_SKIP = frozenset({"script", "style", "noscript", "template", "option", "select", "textarea", "head", "title", "svg"})
_DIGIT = re.compile(r"\d")
# Class/id tokens (split on - _ and case changes) and label words near a price.
_CURRENT_TOKENS = frozenset({"sale", "sales", "discount", "discounted", "final", "current", "selling", "special",
                             "now", "deal", "offer", "dc", "member"})
_REFERENCE_TOKENS = frozenset({"origin", "original", "regular", "consumer", "retail", "before", "was", "strike",
                               "old", "msrp", "compare", "list"})
# Not "fee": a rental or subscription fee is the price itself.
_AUX_TOKENS = frozenset({"shipping", "delivery", "point", "points", "reward", "mileage", "coupon", "installment",
                         "unit", "per"})
_CURRENT_LABEL = re.compile(r"판매가|할인가|혜택가|최종가|특가|최저가|sale\s*price|now\b|our\s*price", re.I)
_REFERENCE_LABEL = re.compile(r"정가|소비자가|시중가|기존가|정상가|권장가|\bwas\b|regular\s*price|list\s*price|\bmsrp\b", re.I)
_AUX_LABEL = re.compile(r"^\s*\(?\s*[+＋]|배송비|택배비|적립|포인트|쿠폰|할부|단위\s*가격|(?:\d+\s*(?:g|ml|kg|l|개|매|입|m)\s*당)|"
                        r"shipping|delivery|reward|per\s+\d|/\s*\d+\s*(?:g|ml)\b|unit\s*price", re.I)
_OFF_CONTENT_TOKENS = frozenset({"related", "recommend", "recommended", "similar", "together", "best", "ranking",
                                 "cart", "basket", "wish", "wishlist", "footer", "header", "gnb", "lnb", "banner",
                                 "popup", "modal", "layer", "review", "reviews", "recent", "history", "upsell",
                                 "crosssell", "suggest", "suggestion"})
_SOLD_OUT = re.compile(r"일시\s*품절|품절|sold\s*out|out\s*of\s*stock", re.I)
_ID_KEYS = ("product_no", "productno", "goods_no", "goodsno", "goodscd", "item_id", "itemid", "product_id",
            "productid", "prdno", "pid", "gno", "idx", "no", "id", "sku")
# Headings first: a "title container" class often also wraps a rating badge.
_NAME_SELECTORS = ("[itemprop=name]", "h2", "h3", "h4", "h5", "[class*=name]", "[class*=Name]", "[class*=title]",
                   "[class*=Title]", "h1", "strong")
_STATE_CLASS = re.compile(r"\d|^(?:on|off|active|selected|current|first|last|odd|even|hover|show|hide|open|"
                          r"is-.*|has-.*|swiper-slide-.*|slick-.*)$", re.I)


def _squash(value: str | None) -> str:
    return re.sub(r"\s+", " ", value or "").strip()


def _text(node: Tag | None) -> str:
    return _squash(node.get_text(" ", strip=True)) if node is not None else ""


def _tokens(node: Tag) -> set[str]:
    raw = " ".join([*map(str, node.get("class", [])), str(node.get("id", "")), str(node.get("itemprop", ""))])
    raw = re.sub(r"([a-z])([A-Z])", r"\1 \2", raw)
    return {token.lower() for token in re.split(r"[\s_\-]+", raw) if token}


def _signature(node: Tag) -> tuple[str, tuple[str, ...]]:
    return node.name, tuple(sorted(str(c) for c in node.get("class", []) if not _STATE_CLASS.search(str(c))))


def _css(signature: tuple[str, tuple[str, ...]]) -> str:
    name, classes = signature
    return name + "".join("." + re.sub(r"([^\w-])", r"\\\1", c) for c in classes)


_SCREEN_READER = re.compile(r"sr-only|sr-text|visually-hidden|screen-reader|a11y|(?:^|\s)blind(?:\s|$)", re.I)


def _state(node: Tag | None, mode: str) -> tuple[str, list[dict[str, Any]]]:
    """Display state where ``aria-hidden`` alone does not hide anything.

    Storefronts often mark the visual price aria-hidden and add screen-reader
    text beside it; only a rendered or explicit hidden state hides content.
    """
    state, hidden_by = display_state(node, mode)
    if state == "hidden" and all(set(item["reasons"]) <= {"aria_hidden"} for item in hidden_by):
        return ("visible" if mode == "rendered_dom" else "unconfirmed"), []
    return state, hidden_by


def _joined(node: Tag) -> str:
    """Text as rendered inline: adjacent tags join ("$249" + ".99"), source spaces stay."""
    return _squash(node.get_text(""))


def _shown(node: Tag | None, mode: str) -> str | None:
    """Visible text of a node, without hidden or screen-reader-only descendants."""
    if node is None or _state(node, mode)[0] == "hidden":
        return None
    parts = [str(value) for value in node.descendants
             if isinstance(value, NavigableString) and not isinstance(value, Comment) and str(value).strip()
             and isinstance(value.parent, Tag) and value.parent.name not in _SKIP
             and _state(value.parent, mode)[0] != "hidden"
             and not _SCREEN_READER.search(" ".join(map(str, value.parent.get("class", []))))]
    return _squash(" ".join(parts)) or None


class _Entry:
    """One minimal element whose own text shows at least one amount."""

    def __init__(self, node: Tag, text: str, mode: str):
        self.node, self.text = node, text
        self.ancestors = {id(parent) for parent in node.parents}
        self.single, _ = displayed_amount(text)
        self.struck = is_struck(node)
        self.hidden = (_state(node, mode)[0] == "hidden"
                       or bool(_SCREEN_READER.search(" ".join(map(str, node.get("class", []))))))
        tokens = _tokens(node) | (_tokens(node.parent) if isinstance(node.parent, Tag) else set())
        label = re.sub(r"[\d,.\s원₩￦$＄€£￡¥￥円]+", " ", _shown(node, mode) or "")
        self.current_hint = bool(tokens & _CURRENT_TOKENS) or bool(_CURRENT_LABEL.search(label))
        self.reference_hint = not self.current_hint and (
            bool(tokens & _REFERENCE_TOKENS) or bool(_REFERENCE_LABEL.search(label)))
        self.auxiliary = bool(_tokens(node) & _AUX_TOKENS) or bool(_AUX_LABEL.search(text))

    @property
    def usable(self) -> bool:
        return (self.single is not None and not self.struck and not self.hidden and not self.auxiliary
                and not self.reference_hint)

    def within(self, node: Tag) -> bool:
        return self.node is node or id(node) in self.ancestors


class _Page:
    def __init__(self, content: bytes, url: str, backend: str):
        self.soup = html_soup(content)
        self.url = url
        self.host = urlsplit(url).hostname or ""
        self.mode = "rendered_dom" if backend == "playwright" else "static_html"
        self._entries: list[_Entry] | None = None
        self._hint: Any = False

    @property
    def entries(self) -> list[_Entry]:
        if self._entries is None:
            found: OrderedDict[int, _Entry] = OrderedDict()
            for string in self.soup.find_all(string=_DIGIT):
                parent = string.parent
                if isinstance(string, Comment) or not isinstance(parent, Tag) or parent.name in _SKIP:
                    continue
                node = parent
                for _ in range(4):
                    if not isinstance(node, Tag) or node.name in {"body", "html", "[document]"} or node.name in _SKIP:
                        break
                    text = _joined(node)
                    if len(text) > 80:
                        break
                    amounts = find_amounts(text)
                    if amounts:
                        # A split decimal ("$249" then ".99") completes in a parent.
                        for _ in range(2):
                            outer = node.parent
                            if not isinstance(outer, Tag) or outer.name in {"body", "html"}:
                                break
                            outer_text = _joined(outer)
                            wider = find_amounts(outer_text) if len(outer_text) <= 80 else []
                            if (len(amounts) == 1 and len(wider) == 1 and wider[0]["token"] != amounts[0]["token"]
                                    and wider[0]["token"].startswith(amounts[0]["token"])):
                                node, text, amounts = outer, outer_text, wider
                            else:
                                break
                        if id(node) not in found:
                            found[id(node)] = _Entry(node, text, self.mode)
                        break
                    node = node.parent
            # An ancestor of another price element is a container, not a price.
            containers = {key for entry in found.values() for key in entry.ancestors if key in found}
            self._entries = [entry for key, entry in found.items() if key not in containers][:3000]
        return self._entries

    @property
    def currency_hint(self) -> tuple[str, str] | None:
        """(ISO code, location) from explicit page metadata, if unambiguous."""
        if self._hint is False:
            hints: dict[str, str] = {}
            for selector in ('meta[property="product:price:currency"]', 'meta[property="og:price:currency"]',
                             '[itemprop="priceCurrency"]'):
                for node in self.soup.select(selector)[:20]:
                    value = str(node.get("content") or node.get_text(strip=True)).strip().upper()
                    if value in ISO_CODES:
                        hints.setdefault(value, f"css:{selector}")
            for script in self.soup.select('script[type="application/ld+json"]')[:20]:
                for value in re.findall(r'"priceCurrency"\s*:\s*"([A-Za-z]{3})"', script.get_text()):
                    if value.upper() in ISO_CODES:
                        hints.setdefault(value.upper(), "json-ld:priceCurrency")
            self._hint = next(iter(hints.items())) if len(hints) == 1 else None
        return self._hint

    def link(self, node: Tag) -> str | None:
        anchors = ([node] if node.name == "a" and node.get("href") else []) + node.select("a[href]")[:20]
        for anchor in anchors:
            href = str(anchor.get("href", "")).strip()
            if not href or href.startswith(("#", "javascript:", "mailto:", "tel:")):
                continue
            absolute = urljoin(self.url, href).split("#", 1)[0]
            parts = urlsplit(absolute)
            if parts.scheme in {"http", "https"} and parts.hostname == self.host:
                return absolute
        return None

    def within(self, node: Tag) -> list[_Entry]:
        return [entry for entry in self.entries if entry.within(node)]


def item_identity(url: str) -> str:
    """A stable product identity from a product URL (query id or path)."""
    parts = urlsplit(url)
    query = {key.lower(): values for key, values in parse_qs(parts.query).items()}
    for key in _ID_KEYS:
        values = query.get(key)
        if values and values[0].strip() and len(values[0]) <= 80:
            return f"{key}={values[0].strip()}"
    return f"path:{parts.path.rstrip('/') or '/'}"


def _proof(location: str, raw: Any, *, node: Tag | None = None, mode: str = "static_html",
           source_text: str | None = None, rule: str | None = None, kind: str = "rendered_text") -> dict[str, Any]:
    proof: dict[str, Any] = {"location": location, "raw": raw}
    if source_text is not None and source_text != raw:
        proof["source_text"] = source_text
    if rule:
        proof["normalization"] = {"rule": rule}
    state, hidden_by = _state(node, mode) if node is not None else ("unconfirmed", [])
    proof["display_state"] = state
    if hidden_by:
        proof["hidden_by"] = hidden_by
    proof["proof_kind"] = kind
    return proof


def _identity_proof(location: str, value: str) -> dict[str, Any]:
    return {"location": location, "raw": value, "display_state": "not_applicable", "proof_kind": "source_identity"}


def _row(locator: str, method: str, page: _Page, *, heuristic: bool) -> dict:
    return {"fields": {}, "evidence": {}, "specs": {}, "locator": locator, "method": method,
            "review_flags": [HEURISTIC_FLAG] if heuristic else [], "mode": page.mode,
            "visibility": "visible" if page.mode == "rendered_dom" else "unconfirmed"}


def _add_price(row: dict, page: _Page, entry: _Entry, location: str) -> None:
    fields, proofs, amount = row["fields"], row["evidence"], entry.single
    fields["price"] = amount["value"]
    proofs["price"] = _proof(location, amount["token"], node=entry.node, mode=page.mode,
                             source_text=entry.text, rule="displayed_amount")
    codes = currency_codes(amount["marker"])
    if len(codes) == 1:
        fields["currency"] = next(iter(codes))
        proofs["currency"] = _proof(location, amount["marker"], node=entry.node, mode=page.mode,
                                    source_text=entry.text, rule="currency_marker")
        return
    hint = page.currency_hint
    code = currency_for(amount["marker"], hint[0] if hint else None)
    if code:
        # The visible symbol is shared by several currencies; page metadata
        # names which one. Both facts are kept on the proof.
        fields["currency"] = code
        proofs["currency"] = {**_proof(f"{hint[1]} + {location}", code, node=entry.node, mode=page.mode,
                                       kind="marker_with_page_currency"), "marker": amount["marker"]}
    else:
        row["review_flags"].append("currency_marker_ambiguous")


def _add_conditions(row: dict, page: _Page, context: Tag, location: str) -> None:
    """Recurring basis, contract term, deposit and sold-out text near a price."""
    text = _text(context)[:400]
    fields, proofs = row["fields"], row["evidence"]
    basis, marker = price_basis(text)
    if basis:
        fields["price_basis"] = basis
        proofs["price_basis"] = _proof(location, marker, node=context, mode=page.mode, source_text=text[:200],
                                       rule="recurring_marker")
        term, token = term_months(text)
        if term:
            fields["term_months"] = term
            proofs["term_months"] = _proof(location, token, node=context, mode=page.mode, source_text=text[:200],
                                           rule="contract_term")
        amount, label = deposit(text)
        if amount:
            fields["deposit_amount"] = amount["value"]
            proofs["deposit_amount"] = _proof(location, amount["token"], node=context, mode=page.mode,
                                              source_text=f"{label} {amount['token']}", rule="displayed_amount")
    sold = _SOLD_OUT.search(text)
    if sold:
        fields["availability"] = sold.group()
        proofs["availability"] = _proof(location, sold.group(), node=context, mode=page.mode)
    elif re.search(r"sold-?out|outofstock|out-of-stock", " ".join(map(str, context.get("class", []))), re.I):
        fields["availability"] = "out_of_stock"
        proofs["availability"] = {"location": f"{location}[class]", "raw": "out_of_stock",
                                  "display_state": "not_applicable", "proof_kind": "dom_attribute"}


def _discount_pair(entries: list[_Entry], context_text: str) -> tuple[_Entry, _Entry] | None:
    """Two prices joined by a displayed discount rate (e.g. 18,500 at 10% = 16,650).

    Static HTML often strikes the list price only through CSS; the shown rate
    still proves which amount is current.
    """
    values = {}
    for entry in entries:
        values.setdefault(Decimal(entry.single["value"]), entry)
    if len(values) != 2:
        return None
    low, high = sorted(values)
    for rate in re.findall(r"(?<![\d.])(\d{1,2})\s*%", context_text):
        expected = high * (100 - int(rate)) / 100
        if 0 < int(rate) < 100 and abs(expected - low) <= max(Decimal(1), high * Decimal("0.005")):
            return values[low], values[high]
    return None


def _pick(entries: list[_Entry], context_text: str = "") -> tuple[_Entry | None, _Entry | None, str | None]:
    """One current price and at most one reference price, or why none."""
    def key(entry):
        return entry.single["value"], currency_codes(entry.single["marker"])

    usable = [entry for entry in entries if entry.usable]
    chosen = reference = None
    if len({key(entry) for entry in usable}) == 1:
        chosen = usable[0]
    elif usable:
        hinted = [entry for entry in usable if entry.current_hint]
        if len({key(entry) for entry in hinted}) == 1:
            chosen = hinted[0]
        elif (pair := _discount_pair(usable, context_text)) is not None:
            chosen, reference = pair
    else:
        # A sole shown price is the price even under a "regular price" label
        # (often screen-reader text on a product that is not on sale).
        plain = [entry for entry in entries if entry.single and not entry.struck and not entry.hidden
                 and not entry.auxiliary]
        if len({key(entry) for entry in plain}) == 1:
            chosen = plain[0]
    if chosen is None:
        return None, None, "multiple_amounts_unresolved" if usable else "no_current_price"
    if reference is None:
        references = [entry for entry in entries
                      if (entry.struck or entry.reference_hint) and not entry.hidden and entry.single
                      and key(entry) != key(chosen)]
        reference = references[0] if len({key(entry) for entry in references}) == 1 else None
    return chosen, reference, None


def _name_node(card: Tag, mode: str) -> Tag | None:
    for selector in _NAME_SELECTORS:
        for node in card.select(selector)[:10]:
            text = _shown(node, mode)
            if text and 2 <= len(text) <= 200 and not find_amounts(text):
                return node
    return None


def _own_entries(page: _Page, card: Tag, product_url: str | None) -> list[_Entry]:
    """Prices in a card, without those inside a link to another product (related items)."""
    entries = page.within(card)
    if not product_url:
        return entries
    own = []
    for entry in entries:
        anchor = entry.node if entry.node.name == "a" else entry.node.find_parent("a")
        inside = anchor is not None and anchor is not card and any(parent is card for parent in anchor.parents)
        href = str(anchor.get("href", "")).strip() if inside and anchor.has_attr("href") else ""
        if href and not href.startswith(("#", "javascript:")):
            target = urljoin(page.url, href).split("#", 1)[0]
            if target != product_url and item_identity(target) != item_identity(product_url):
                continue
        own.append(entry)
    return own


def _offer_row(page: _Page, card: Tag, entries: list[_Entry], *, locator: str, method: str, heuristic: bool,
               name_node: Tag | None = None, product_url: str | None = None,
               context: Tag | None = None) -> tuple[dict | None, _Entry | None]:
    chosen, reference, problem = _pick(entries, _text(context if context is not None else card)[:600])
    row = _row(locator, method, page, heuristic=heuristic)
    fields, proofs = row["fields"], row["evidence"]
    name_node = name_node if name_node is not None else _name_node(card, page.mode)
    name = _shown(name_node, page.mode) if name_node is not None else None
    if not name:
        image = card.select_one("img[alt]")
        if image is not None and _squash(str(image.get("alt"))):
            name_node, name = image, _squash(str(image.get("alt")))
    if not name:
        return None, chosen
    fields["name"] = name
    proofs["name"] = _proof(f"{locator} name", name, node=name_node, mode=page.mode,
                            kind="rendered_text" if name_node.name != "img" else "dom_attribute")
    if name_node.name == "img" and proofs["name"]["display_state"] == "visible":
        proofs["name"]["display_state"] = "unconfirmed"  # alt text is not displayed text
    if product_url:
        identity = item_identity(product_url)
        fields["url_id"] = identity
        proofs["url_id"] = _identity_proof(f"{locator} a[href]", identity)
        fields["product_url"] = product_url
        proofs["product_url"] = _identity_proof(f"{locator} a[href]", product_url)
    if chosen is None:
        proofs["price"] = {"location": f"{locator} price", "raw": None, "reason": problem}
        row["review_flags"].append(problem)
    else:
        _add_price(row, page, chosen, f"{locator} price")
        if reference is not None:
            fields["reference_price"] = reference.single["value"]
            proofs["reference_price"] = _proof(f"{locator} reference_price", reference.single["token"],
                                               node=reference.node, mode=page.mode, source_text=reference.text,
                                               rule="displayed_amount")
    _add_conditions(row, page, context if context is not None else card, locator)
    return row, chosen


def _relative_selector(card: Tag, node: Tag | None) -> str | None:
    if node is None or node is card:
        return None
    try:
        own = _css(_signature(node))
        if card.select(own)[:1] == [node]:
            return own
        chain, current = [], node
        while isinstance(current, Tag) and current is not card and len(chain) < 6:
            chain.append(_css(_signature(current)))
            current = current.parent
        selector = ":scope > " + " > ".join(reversed(chain))
        return selector if current is card and card.select(selector)[:1] == [node] else None
    except Exception:  # soupsieve rejects unusual class names
        return None


def _unique_selector(page: _Page, node: Tag | None) -> str | None:
    chain, current = [], node
    try:
        while isinstance(current, Tag) and current.name not in {"body", "html", "[document]"} and len(chain) < 8:
            ident = str(current.get("id", ""))
            if ident and re.fullmatch(r"[A-Za-z][\w-]{0,60}", ident) and not re.search(r"\d{3,}", ident):
                chain.append("#" + ident)
            else:
                chain.append(_css(_signature(current)))
            selector = " > ".join(reversed(chain))
            if page.soup.select(selector) == [node]:
                return selector
            current = current.parent
    except Exception:
        return None
    return None


# -- 1. storefront platforms ---------------------------------------------------

def _cafe24_labelled(card: Tag, label: str) -> Tag | None:
    """The value node of a card spec line such as ``판매가 : 405,000원``."""
    for item in card.select("ul.spec li, .spec li, li[class*=product_]"):
        title = item.select_one(".title")
        if title is not None and label in title.get_text(" ", strip=True):
            values = [child for child in item.find_all("span", recursive=False)
                      if child.get_text(strip=True) and "title" not in (child.get("class") or [])]
            return values[-1] if values else None
    return None


def _platform_cards(page: _Page) -> dict | None:
    cafe24: OrderedDict[str, Tag] = OrderedDict()
    for node in page.soup.select('li[id^="anchorBoxId_"], li[data-no]'):
        raw_id = str(node.get("id", ""))
        number = raw_id.removeprefix("anchorBoxId_") if raw_id.startswith("anchorBoxId_") else str(node.get("data-no", ""))
        if number.isdigit() and node.select_one(".name"):
            cafe24.setdefault(number, node)
    if cafe24:
        rows = []
        for number, card in cafe24.items():
            entries = page.within(card)
            sale = _cafe24_labelled(card, "할인판매가") or _cafe24_labelled(card, "판매가")
            regular = _cafe24_labelled(card, "소비자가")
            for entry in entries:
                if sale is not None and (entry.within(sale) or sale in entry.node.descendants):
                    entry.current_hint, entry.reference_hint = True, False
                elif regular is not None and (entry.within(regular) or regular in entry.node.descendants):
                    entry.current_hint, entry.reference_hint = False, True
            # Cafe24 prefixes the name with a (usually hidden) "상품명" label span.
            holder = card.select_one(".name a") or card.select_one(".name")
            spans = [span for span in holder.find_all("span")
                     if "title" not in (span.get("class") or []) and span.get_text(strip=True)]
            name_node = spans[-1] if spans else holder
            row, _ = _offer_row(page, card, entries, locator=f"cafe24:list:{number}", method="cafe24_category_cards",
                                heuristic=False, name_node=name_node, product_url=page.link(card))
            if row is not None:
                row["fields"]["item_id"] = f"product_no={number}"
                row["evidence"]["item_id"] = _identity_proof(f"css:li[{number}]", f"product_no={number}")
                rows.append(row)
        return _result("platform_cards", rows, len(cafe24), complete=True, platform="cafe24",
                       settle_selector='li[id^="anchorBoxId_"]')
    woo: OrderedDict[str, tuple[Tag, Tag]] = OrderedDict()
    for card in page.soup.select("li.product"):
        title = card.select_one(".woocommerce-loop-product__title")
        post = next((c[5:] for c in map(str, card.get("class", [])) if re.fullmatch(r"post-\d+", c)), None)
        if title is not None and post:
            woo.setdefault(post, (card, title))
    if woo:
        rows = []
        for post, (card, title) in woo.items():
            box = card.select_one(".price")
            entries = page.within(card)
            for entry in entries:
                if entry.node.name == "del" or entry.node.find_parent("del") is not None:
                    entry.current_hint, entry.reference_hint = False, True
                elif entry.node.name == "ins" or entry.node.find_parent("ins") is not None:
                    entry.current_hint, entry.reference_hint = True, False
                # WooCommerce marks the del/ins pair aria-hidden and adds
                # screen-reader text; display is judged on the .price box.
                if entry.hidden and box is not None and entry.within(box) and display_state(box, page.mode)[0] != "hidden":
                    entry.hidden = False
            row, _ = _offer_row(page, card, entries, locator=f"woocommerce:post-{post}",
                                method="woocommerce_product_cards", heuristic=False, name_node=title,
                                product_url=page.link(card))
            if row is not None:
                row["fields"]["item_id"] = f"post-{post}"
                row["evidence"]["item_id"] = _identity_proof(f"css:li.post-{post}", f"post-{post}")
                rows.append(row)
        return _result("platform_cards", rows, len(woo), complete=True, platform="woocommerce",
                       settle_selector="li.product")
    return None


# -- 2. structured data ---------------------------------------------------------

def _decimal(value: Any) -> Decimal | None:
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


def _confirm_structured(page: _Page, rows: list[dict]) -> None:
    """Prove structured prices with the same amounts rendered on the page.

    One shared price is matched to a shown amount in the main content. Rows
    with different prices (a list page) are matched one by one to a shown
    amount whose surrounding card names the same product.
    """
    priced = [row for row in rows if _decimal(row["fields"].get("price")) is not None]
    if page.mode != "rendered_dom" or not priced:
        return
    if len({_decimal(r["fields"]["price"]) for r in priced}) == 1:
        target = _decimal(priced[0]["fields"]["price"])
        entry = next((entry for entry in page.entries if entry.usable and not _off_content(entry.node)
                      and Decimal(entry.single["value"]) == target), None)
        if entry is not None:
            for row in priced:
                _upgrade_structured(page, row, entry)
        return
    for row in priced:
        name = _squash(str(row["fields"].get("name") or "")).casefold()
        if len(name) < 3:
            continue
        target = _decimal(row["fields"]["price"])
        for entry in page.entries:
            if not entry.usable or Decimal(entry.single["value"]) != target:
                continue
            parent, depth = entry.node.parent, 0
            while isinstance(parent, Tag) and depth < 6 and parent.name not in {"body", "html"}:
                if name in _squash(parent.get_text(" "))[:3000].casefold():
                    break
                parent, depth = parent.parent, depth + 1
            else:
                continue
            if isinstance(parent, Tag) and parent.name not in {"body", "html"}:
                _upgrade_structured(page, row, entry)
                break


def _upgrade_structured(page: _Page, row: dict, entry: _Entry) -> None:
    amount = entry.single
    codes = currency_codes(amount["marker"])
    fields, proofs = row["fields"], row["evidence"]
    declared = fields.get("currency")
    if declared is not None and str(declared).upper() not in codes:
        return  # the visible marker names another currency
    structured = proofs.get("price", {}).get("location")
    fields["price"] = amount["value"]
    proofs["price"] = {**_proof("visible price matching structured offer", amount["token"], node=entry.node,
                                mode=page.mode, source_text=entry.text, rule="displayed_amount"),
                       "structured_location": structured}
    if len(codes) == 1:
        fields["currency"] = next(iter(codes))
        proofs["currency"] = _proof("visible price marker", amount["marker"], node=entry.node, mode=page.mode,
                                    source_text=entry.text, rule="currency_marker")
    elif declared is not None:
        proofs["currency"] = {**_proof(f"{proofs['currency']['location']} + visible price marker", str(declared),
                                       node=entry.node, mode=page.mode, kind="marker_with_structured_currency"),
                              "marker": amount["marker"]}
    row["review_flags"] = [flag for flag in row["review_flags"] if flag != UNCONFIRMED_STRUCTURED_FLAG]
    row["mode"], row["visibility"] = "rendered_dom", "visible"
    context = entry.node.parent if isinstance(entry.node.parent, Tag) else entry.node
    _add_conditions(row, page, context, "visible price context")


def _jsonld_rows(page: _Page, content: bytes, backend: str) -> tuple[list[dict], list[dict]]:
    from .structured_data import extract_jsonld

    probe = FetchResult("generic", "fetched", backend, content=content, final_url=page.url)
    rows = []
    for candidate in extract_jsonld(probe):
        if not any(candidate.fields.get(key) not in (None, "") for key in
                   ("name", "price", "sku", "mpn", "gtin", "gtin8", "gtin12", "gtin13", "gtin14")):
            continue  # a variant stub (often only a URL) states nothing to record
        data = asdict(candidate)
        rows.append({"fields": data["fields"], "evidence": data["evidence"], "specs": data["specs"],
                     "locator": data["locator"], "method": data["extraction_method"],
                     "review_flags": data["review_flags"], "mode": data["evidence_mode"],
                     "visibility": data["source_visibility"]})
    identity_keys = ("sku", "mpn", "gtin", "gtin8", "gtin12", "gtin13", "gtin14")
    for index, row in enumerate(rows, 1):
        for key in identity_keys:
            if key in row["evidence"]:
                # Embedded identifiers are source identity, not displayed claims.
                row["evidence"][key].update(display_state="not_applicable", proof_kind="source_identity")
        if not any(row["fields"].get(key) not in (None, "") for key in identity_keys):
            identity = item_identity(page.url)
            if len(rows) > 1:
                identity += "#" + str(row["fields"].get("name") or index)
            row["fields"]["url_id"] = identity
            row["evidence"]["url_id"] = _identity_proof("url", identity)
    return rows, probe.trace


def _microdata_rows(page: _Page) -> list[dict]:
    values: dict[str, str] = {}
    for selector in ('[itemprop="price"]', 'meta[property="product:price:amount"]', 'meta[property="og:price:amount"]'):
        for node in page.soup.select(selector)[:20]:
            raw = str(node.get("content") or node.get_text(" ", strip=True)).strip()
            if re.fullmatch(r"\d+(?:\.\d+)?", raw):
                values.setdefault(raw, f"css:{selector}")
    if len(values) != 1:
        return []
    price, location = next(iter(values.items()))
    row = _row("microdata:product", "microdata_product_offer", page, heuristic=False)
    fields, proofs = row["fields"], row["evidence"]
    for selector in ('[itemprop="name"]', "h1"):
        node = page.soup.select_one(selector)
        if node is not None and _shown(node, page.mode):
            fields["name"] = _shown(node, page.mode)
            proofs["name"] = _proof(f"css:{selector}", fields["name"], node=node, mode=page.mode)
            break
    else:
        meta = page.soup.select_one('meta[property="og:title"]')
        if meta is None or not _squash(str(meta.get("content", ""))):
            return []
        fields["name"] = _squash(str(meta["content"]))
        proofs["name"] = {"location": 'css:meta[property="og:title"]', "raw": fields["name"],
                          "display_state": "unconfirmed", "proof_kind": "embedded_metadata"}
    fields["price"] = price
    proofs["price"] = {"location": location, "raw": price, "display_state": "unconfirmed",
                       "proof_kind": "embedded_metadata"}
    hint = page.currency_hint
    if hint:
        fields["currency"] = hint[0]
        proofs["currency"] = {"location": hint[1], "raw": hint[0], "display_state": "unconfirmed",
                              "proof_kind": "embedded_metadata"}
    identity = item_identity(page.url)
    fields["url_id"] = identity
    proofs["url_id"] = _identity_proof("url", identity)
    row["review_flags"].append(UNCONFIRMED_STRUCTURED_FLAG)
    row["mode"], row["visibility"] = "static_html", "unconfirmed"
    return [row]


# -- 3. repeated cards ---------------------------------------------------------

def _repeated_cards(page: _Page) -> dict | None:
    groups: OrderedDict[tuple, OrderedDict[int, Tag]] = OrderedDict()
    for entry in page.entries:
        if not entry.usable:
            continue
        node, depth = entry.node, 0
        while isinstance(node.parent, Tag) and node.parent.name not in {"body", "html", "[document]"} and depth < 12:
            groups.setdefault((_signature(node.parent), _signature(node)), OrderedDict())[id(node)] = node
            node, depth = node.parent, depth + 1
    best, best_rank = None, None
    for key, members in groups.items():
        cards = [card for card in members.values()
                 if not any("clone" in str(c).lower() for c in card.get("class", [])) and page.link(card)]
        if len(cards) < 3:
            continue
        ids = {id(card) for card in cards}
        if any(id(parent) in ids for card in cards for parent in card.parents):
            continue  # nested members are not one list of sibling cards
        usable_counts = [sum(entry.usable for entry in page.within(card)) for card in cards]
        if sum(1 <= count <= 3 for count in usable_counts) < max(3, int(len(cards) * 0.8)):
            continue
        rank = (len(cards), -len(list(cards[0].parents)))
        if best_rank is None or rank > best_rank:
            best, best_rank = (key, cards), rank
    if best is None:
        return None
    (parent_signature, card_signature), cards = best
    rows, seen, selectors = [], set(), {}
    for index, card in enumerate(cards, 1):
        url = page.link(card)
        identity = item_identity(url) if url else f"card:{index}"
        if identity in seen:
            continue  # carousels repeat the same product
        seen.add(identity)
        entries = _own_entries(page, card, url)
        row, chosen = _offer_row(page, card, entries, locator=f"generic:card:{identity}",
                                 method="generic_repeated_cards", heuristic=True, product_url=url)
        if row is None:
            continue
        rows.append(row)
        if "name" not in selectors:
            name = _relative_selector(card, _name_node(card, page.mode))
            price = _relative_selector(card, chosen.node) if chosen is not None else None
            if name and price:
                selectors = {"name": name, "price": price, "product_url": "a::attr(href)"}
    rule = ({"row_selector": f"{_css(parent_signature)} > {_css(card_signature)}", "selectors": selectors}
            if selectors else None)
    return _result("repeated_cards", rows, len(seen), complete=False, suggested_rule=rule,
                   settle_selector=f"{_css(parent_signature)} > {_css(card_signature)}",
                   reason="heuristic_card_list")


# -- 4. single product page ----------------------------------------------------

def _off_content(node: Tag) -> bool:
    current, depth = node, 0
    # <body>/<html> classes describe the whole page ("has-related-articles").
    while isinstance(current, Tag) and current.name not in {"body", "html"} and depth < 15:
        if current.name in {"nav", "header", "footer", "aside"} or _tokens(current) & _OFF_CONTENT_TOKENS:
            return True
        current, depth = current.parent, depth + 1
    return False


def _heading(page: _Page) -> Tag | None:
    return next((node for node in page.soup.select("h1")
                 if _shown(node, page.mode) and not _off_content(node)), None)


def _product_row(page: _Page, heading: Tag, entries: list[_Entry], region: Tag) -> dict | None:
    chosen, _, _ = _pick(entries, _text(region)[:600])
    if chosen is None:
        return None
    context = chosen.node.parent if isinstance(chosen.node.parent, Tag) else chosen.node
    row, _ = _offer_row(page, region, entries, locator="generic:product", method="generic_single_product",
                        heuristic=True, name_node=heading, context=context)
    if row is None:
        return None
    identity = item_identity(page.url)
    row["fields"]["url_id"] = identity
    row["evidence"]["url_id"] = _identity_proof("url", identity)
    name, price = _unique_selector(page, heading), _unique_selector(page, chosen.node)
    rule = {"selectors": {"name": name, "price": price}} if name and price else None
    return _result("single_product", [row], 1, complete=False, suggested_rule=rule, reason="heuristic_single_price")


def _heading_product(page: _Page) -> dict | None:
    """A product page: the title's own block shows exactly one current price.

    Checked before card lists so "related" or "recently viewed" cards on a
    product page are not mistaken for the page's subject.
    """
    heading = _heading(page)
    region = heading.parent if heading is not None else None
    for _ in range(4):
        if not isinstance(region, Tag) or region.name in {"body", "html", "[document]"}:
            return None
        # Zero amounts beside a title are tax lines or trials, not the price.
        entries = [entry for entry in page.within(region) if not _off_content(entry.node)
                   and not (entry.single and Decimal(entry.single["value"]) == 0)]
        usable = {entry.single["value"] for entry in entries if entry.usable}
        if len(usable) > 1 and _discount_pair([e for e in entries if e.usable], _text(region)[:600]) is None:
            return None
        if usable:
            return _product_row(page, heading, entries, region)
        region = region.parent
    return None


def _single_product(page: _Page) -> dict | None:
    heading = _heading(page)
    if heading is None:
        return None
    entries = [entry for entry in page.entries if not _off_content(entry.node)]
    found = _product_row(page, heading, entries, page.soup) if entries else None
    if found is not None:
        return found
    if not entries:
        return None
    _, _, problem = _pick(entries)
    return {"method": "single_product", "platform": None, "rows": [],
            "trace": [{"event": "extraction_gap", "reason": problem}],
            "section": _section("single_product", 1, 0, [], status="unknown", reason=problem)}


_LINE_TAIL = re.compile(r"[\s:：\-–—·|/]*(?:매월|월|연간|연|년|per|monthly)?[\s:：\-–—·|/]*$", re.I)


def _price_lines(page: _Page) -> dict | None:
    """A price table written as text lines ("스탠다드 : 월 13,500원"), e.g. plan pages."""
    lines, seen = [], set()
    for entry in page.entries:
        # A zero amount is a free trial or promotion line, not a plan price.
        if not entry.usable or _off_content(entry.node) or Decimal(entry.single["value"]) == 0:
            continue
        text = _shown(entry.node, page.mode) or ""
        amounts = find_amounts(text)
        if len(amounts) != 1:
            continue
        label = _LINE_TAIL.sub("", text[:amounts[0]["start"]]).strip()
        if not (2 <= len(label) <= 60) or not re.search(r"[^\W\d_]", label) or label in seen:
            continue
        seen.add(label)
        lines.append((label, entry))
    if len(lines) < 2:
        return None
    rows = []
    for label, entry in lines:
        identity = f"{item_identity(page.url)}#{label}"
        row, _ = _offer_row(page, entry.node, [entry], locator=f"generic:line:{label}", method="generic_price_lines",
                            heuristic=True, name_node=entry.node, context=entry.node)
        if row is None:
            continue
        row["fields"]["name"] = label
        row["evidence"]["name"] = _proof(f"generic:line:{label} name", label, node=entry.node, mode=page.mode,
                                         source_text=entry.text)
        row["fields"]["url_id"] = identity
        row["evidence"]["url_id"] = _identity_proof("url + line label", identity)
        rows.append(row)
    return _result("price_lines", rows, len(lines), complete=False, reason="heuristic_price_lines")


# -- assembly ------------------------------------------------------------------

def _section(method: str, observed: int | None, extracted: int, locators: list[str], *, status: str,
             reason: str, **extra) -> dict:
    section = {"section": f"generic_{method}", "status": status, "reason": reason, "observed_count": observed,
               "expected_count": observed if status == "complete" else None, "extracted_count": extracted,
               "unparsed_count": None if observed is None else max(0, observed - extracted),
               "extracted_locators": locators, "scope": "observed_page", "method": method}
    section.update({key: value for key, value in extra.items() if value is not None})
    return section


def _result(method: str, rows: list[dict], observed: int | None, *, complete: bool, platform: str | None = None,
            suggested_rule: dict | None = None, reason: str | None = None,
            settle_selector: str | None = None) -> dict:
    """Platform card lists can be complete; heuristic and structured sections stay unknown."""
    if complete:
        status = "complete" if rows and len(rows) == observed else "partial"
        reason = "all_items_extracted" if status == "complete" else "items_not_fully_extracted"
    else:
        status = "unknown"
    return {"method": method, "platform": platform, "rows": rows, "trace": [],
            "section": _section(method, observed, len(rows), [row["locator"] for row in rows], status=status,
                                reason=reason or "unknown", platform=platform, suggested_rule=suggested_rule,
                                settle_selector=settle_selector,
                                price_count=sum(row["fields"].get("price") is not None for row in rows))}


@lru_cache(maxsize=4)
def _analyse(content: bytes, url: str, backend: str) -> dict:
    from .network_evidence import inline_json
    page = _Page(content, url, backend)
    return {**_run_steps(page, content, backend), "inline_json": inline_json(page.soup)}


def _run_steps(page: _Page, content: bytes, backend: str) -> dict:
    platform = _platform_cards(page)
    if platform is not None and platform["rows"]:
        return platform
    structured, trace = _jsonld_rows(page, content, backend)
    if not structured:
        structured = _microdata_rows(page)
    if structured:
        _confirm_structured(page, structured)
    if any(row["fields"].get("price") is not None for row in structured):
        # Embedded records do not enumerate what the page displays.
        result = _result("structured_data", structured, None, complete=False,
                         reason="structured_records_only")
        result["trace"] = trace
        return result
    for step in (_heading_product, _repeated_cards, _price_lines, _single_product):
        found = step(page)
        if found is not None and found["rows"]:
            found["trace"] = trace + found["trace"]
            return found
    if structured:  # records without a price still identify the product
        result = _result("structured_data", structured, None, complete=False,
                         reason="structured_records_without_price")
        result["trace"] = trace
        return result
    gap = _single_product(page) if page.entries else None
    if gap is not None:
        gap["trace"] = trace + gap["trace"]
        return gap
    return {"method": None, "platform": None, "rows": [], "trace": trace, "section": None}


def _applicable(result: FetchResult) -> bool:
    return result.status == "fetched" and "html" in result.media_type.lower() and bool(result.content)


def _analysis(result: FetchResult, source: Source | None = None) -> dict:
    return _analyse(result.content, result.final_url or (source.location if source else ""), result.backend)


def extract_generic(result: FetchResult, source: Source | None = None) -> list[Candidate]:
    """Candidates for an adapter-less HTML page (empty when nothing is provable)."""
    if not _applicable(result):
        return []
    analysis = _analysis(result, source)
    result.trace.extend(copy.deepcopy(analysis["trace"]))
    candidates = [Candidate(row["fields"], row["evidence"], row["locator"], row["method"], row["specs"],
                            source_visibility=row["visibility"], review_flags=row["review_flags"],
                            evidence_mode=row["mode"])
                  for row in copy.deepcopy(analysis["rows"])]
    from .network_evidence import corroborate
    return corroborate(candidates, result, analysis["inline_json"])


def apply_market_currency(candidates: list[Candidate], code: str | None) -> list[Candidate]:
    """Resolve "$" or "¥" with the currency of the plan's market, which the user approved.

    Only a shown marker that names several currencies including ``code``
    qualifies; the proof keeps both the marker and the market source.
    """
    if not code:
        return candidates
    for candidate in candidates:
        proof = candidate.evidence.get("price") or {}
        if candidate.fields.get("currency") is not None or not isinstance(proof.get("raw"), str):
            continue
        amount, _ = displayed_amount(proof["raw"])
        codes = currency_codes(amount["marker"]) if amount else frozenset()
        if len(codes) < 2 or code not in codes:
            continue
        candidate.fields["currency"] = code
        candidate.evidence["currency"] = {"location": f"plan:topic.market + {proof.get('location')}", "raw": code,
                                          "marker": amount["marker"], "display_state": proof.get("display_state"),
                                          "proof_kind": "marker_with_plan_market"}
        candidate.review_flags = [flag for flag in candidate.review_flags if flag != "currency_marker_ambiguous"]
    return candidates


def generic_sections(result: FetchResult) -> list[dict]:
    """Coverage of the generic extraction for this capture (no trace changes)."""
    if not _applicable(result):
        return []
    section = _analysis(result)["section"]
    return [copy.deepcopy(section)] if section else []


def platform_complete(sections: list[dict]) -> bool:
    """True when the page is a deterministic platform card list extracted in full."""
    generic = [s for s in sections if str(s.get("section", "")).startswith("generic_")]
    return bool(generic) and all(s.get("method") == "platform_cards" and s.get("status") == "complete" for s in generic)


def product_links(result: FetchResult) -> list[str]:
    """Product URLs found on extracted list cards (list page -> product pages)."""
    if not _applicable(result):
        return []
    analysis = _analysis(result)
    if analysis["method"] not in {"platform_cards", "repeated_cards"}:
        return []
    return list(OrderedDict.fromkeys(row["fields"]["product_url"] for row in analysis["rows"]
                                     if row["fields"].get("product_url")))


_PAGINATION_TOKENS = frozenset({"pagination", "paging", "pager", "paginate", "page", "pages", "pagenav"})
_NEXT_TEXT = re.compile(r"^(?:다음|다음\s*페이지|next|next\s*page|›|»|>)$", re.I)


def pagination_links(result: FetchResult, limit: int = 20) -> list[str]:
    """Same-host links of a list's pager (rel=next, "다음"/"Next", pager blocks)."""
    if not _applicable(result):
        return []
    soup = html_soup(result.content)
    host = urlsplit(result.final_url).hostname
    links: OrderedDict[str, None] = OrderedDict()
    for anchor in soup.select("a[href]"):
        href = str(anchor.get("href", "")).strip()
        if not href or href.startswith(("#", "javascript:", "mailto:", "tel:")):
            continue
        rel = {str(value).lower() for value in (anchor.get("rel") or [])}
        in_pager = any(_tokens(parent) & _PAGINATION_TOKENS for parent in [anchor, *list(anchor.parents)[:3]]
                       if isinstance(parent, Tag))
        if "next" in rel or in_pager or _NEXT_TEXT.match(_text(anchor)):
            absolute = urljoin(result.final_url, href).split("#", 1)[0]
            if urlsplit(absolute).hostname == host and absolute != result.final_url:
                links.setdefault(absolute)
        if len(links) >= limit:
            break
    return list(links)


__all__ = ["GENERIC_VERSION", "extract_generic", "generic_sections", "item_identity", "pagination_links",
           "platform_complete", "product_links"]
