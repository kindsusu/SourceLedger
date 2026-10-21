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

from bs4 import Comment, Tag

from .adapters.common import display_state, html_soup, is_struck, visible_text
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
_NAME_SELECTORS = ("[itemprop=name]", "[class*=name]", "[class*=Name]", "[class*=title]", "[class*=Title]",
                   "h2", "h3", "h4", "h5", "h1", "strong")
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


class _Entry:
    """One minimal element whose own text shows at least one amount."""

    def __init__(self, node: Tag, text: str, mode: str):
        self.node, self.text = node, text
        self.ancestors = {id(parent) for parent in node.parents}
        self.single, _ = displayed_amount(text)
        self.struck = is_struck(node)
        self.hidden = display_state(node, mode)[0] == "hidden"
        tokens = _tokens(node) | (_tokens(node.parent) if isinstance(node.parent, Tag) else set())
        label = re.sub(r"[\d,.\s원₩$€£¥円]+", " ", text)
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
                    text = _text(node)
                    if len(text) > 80:
                        break
                    if find_amounts(text):
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
    state, hidden_by = display_state(node, mode) if node is not None else ("unconfirmed", [])
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


def _pick(entries: list[_Entry]) -> tuple[_Entry | None, _Entry | None, str | None]:
    """One current price and at most one reference price, or why none."""
    def key(entry):
        return entry.single["value"], currency_codes(entry.single["marker"])

    usable = [entry for entry in entries if entry.usable]
    chosen = None
    if len({key(entry) for entry in usable}) == 1:
        chosen = usable[0]
    elif usable:
        hinted = [entry for entry in usable if entry.current_hint]
        if len({key(entry) for entry in hinted}) == 1:
            chosen = hinted[0]
    if chosen is None:
        return None, None, "multiple_amounts_unresolved" if usable else "no_current_price"
    references = [entry for entry in entries
                  if (entry.struck or entry.reference_hint) and not entry.hidden and entry.single
                  and key(entry) != key(chosen)]
    reference = references[0] if len({key(entry) for entry in references}) == 1 else None
    return chosen, reference, None


def _name_node(card: Tag) -> Tag | None:
    for selector in _NAME_SELECTORS:
        for node in card.select(selector)[:10]:
            text = visible_text(node)
            if text and 2 <= len(text) <= 200 and not find_amounts(text):
                return node
    return None


def _offer_row(page: _Page, card: Tag, entries: list[_Entry], *, locator: str, method: str, heuristic: bool,
               name_node: Tag | None = None, product_url: str | None = None,
               context: Tag | None = None) -> tuple[dict | None, _Entry | None]:
    chosen, reference, problem = _pick(entries)
    row = _row(locator, method, page, heuristic=heuristic)
    fields, proofs = row["fields"], row["evidence"]
    name_node = name_node if name_node is not None else _name_node(card)
    name = visible_text(name_node) if name_node is not None else None
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
        return _result("platform_cards", rows, len(cafe24), complete=True, platform="cafe24")
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
        return _result("platform_cards", rows, len(woo), complete=True, platform="woocommerce")
    return None


# -- 2. structured data ---------------------------------------------------------

def _confirm_structured(page: _Page, rows: list[dict]) -> None:
    """Prove a structured price with the same amount rendered on the page."""
    priced = [row for row in rows if row["fields"].get("price") is not None]
    if page.mode != "rendered_dom" or not priced or len({str(r["fields"]["price"]) for r in priced}) != 1:
        return
    try:
        target = Decimal(str(priced[0]["fields"]["price"]))
    except (InvalidOperation, ValueError):
        return
    entry = next((entry for entry in page.entries
                  if entry.usable and not _off_content(entry.node) and Decimal(entry.single["value"]) == target), None)
    if entry is None:
        return
    amount = entry.single
    codes = currency_codes(amount["marker"])
    for row in priced:
        fields, proofs = row["fields"], row["evidence"]
        declared = fields.get("currency")
        if declared is not None and str(declared).upper() not in codes:
            continue  # the visible marker names another currency
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
        if node is not None and visible_text(node):
            fields["name"] = visible_text(node)
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
        entries = page.within(card)
        row, chosen = _offer_row(page, card, entries, locator=f"generic:card:{identity}",
                                 method="generic_repeated_cards", heuristic=True, product_url=url)
        if row is None:
            continue
        rows.append(row)
        if "name" not in selectors:
            name = _relative_selector(card, _name_node(card))
            price = _relative_selector(card, chosen.node) if chosen is not None else None
            if name and price:
                selectors = {"name": name, "price": price, "product_url": "a::attr(href)"}
    rule = ({"row_selector": f"{_css(parent_signature)} > {_css(card_signature)}", "selectors": selectors}
            if selectors else None)
    return _result("repeated_cards", rows, len(seen), complete=False, suggested_rule=rule,
                   reason="heuristic_card_list")


# -- 4. single product page ----------------------------------------------------

def _off_content(node: Tag) -> bool:
    current, depth = node, 0
    while isinstance(current, Tag) and depth < 15:
        if current.name in {"nav", "header", "footer", "aside"} or _tokens(current) & _OFF_CONTENT_TOKENS:
            return True
        current, depth = current.parent, depth + 1
    return False


def _single_product(page: _Page) -> dict | None:
    heading = next((node for node in page.soup.select("h1") if visible_text(node) and not _off_content(node)), None)
    if heading is None:
        return None
    entries = [entry for entry in page.entries if not _off_content(entry.node)]
    chosen, _, problem = _pick(entries)
    if chosen is None:
        if not entries:
            return None
        return {"method": "single_product", "platform": None, "rows": [],
                "trace": [{"event": "extraction_gap", "reason": problem}],
                "section": _section("single_product", 1, 0, [], status="unknown", reason=problem)}
    context = chosen.node.parent if isinstance(chosen.node.parent, Tag) else chosen.node
    row, _ = _offer_row(page, page.soup, entries, locator="generic:product", method="generic_single_product",
                        heuristic=True, name_node=heading, context=context)
    identity = item_identity(page.url)
    row["fields"]["url_id"] = identity
    row["evidence"]["url_id"] = _identity_proof("url", identity)
    name, price = _unique_selector(page, heading), _unique_selector(page, chosen.node)
    rule = {"selectors": {"name": name, "price": price}} if name and price else None
    return _result("single_product", [row], 1, complete=False, suggested_rule=rule, reason="heuristic_single_price")


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
            suggested_rule: dict | None = None, reason: str | None = None) -> dict:
    """Platform card lists can be complete; heuristic and structured sections stay unknown."""
    if complete:
        status = "complete" if rows and len(rows) == observed else "partial"
        reason = "all_items_extracted" if status == "complete" else "items_not_fully_extracted"
    else:
        status = "unknown"
    return {"method": method, "platform": platform, "rows": rows, "trace": [],
            "section": _section(method, observed, len(rows), [row["locator"] for row in rows], status=status,
                                reason=reason or "unknown", platform=platform, suggested_rule=suggested_rule,
                                price_count=sum(row["fields"].get("price") is not None for row in rows))}


@lru_cache(maxsize=4)
def _analyse(content: bytes, url: str, backend: str) -> dict:
    page = _Page(content, url, backend)
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
    for step in (_repeated_cards, _single_product):
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
    return [Candidate(row["fields"], row["evidence"], row["locator"], row["method"], row["specs"],
                      source_visibility=row["visibility"], review_flags=row["review_flags"], evidence_mode=row["mode"])
            for row in copy.deepcopy(analysis["rows"])]


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
