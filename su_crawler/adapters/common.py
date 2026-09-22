"""Shared, deterministic helpers for first-party site adapters."""
from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation
from typing import Any

from bs4 import Tag, UnicodeDammit


HIDDEN_CLASSES = {"hidden", "d-none", "is-hidden", "sr-only", "inactive", "is-inactive"}


def html_soup(content: bytes):
    """Decode captured HTML using declarations/detection before parsing."""
    from bs4 import BeautifulSoup

    decoded = UnicodeDammit(content, is_html=True).unicode_markup
    return BeautifulSoup(decoded if decoded is not None else content, "html.parser")


def text(node: Any) -> str | None:
    if node is None:
        return None
    value = re.sub(r"\s+", " ", node.get_text(" ", strip=True)).strip()
    return value or None


def is_visible(node: Tag | None) -> bool:
    """Reject markup explicitly hidden in the captured DOM.

    A static response cannot prove computed CSS visibility, so callers should use
    this only for rendered evidence or explicit HTML hidden state.
    """
    current = node
    while isinstance(current, Tag):
        style = re.sub(r"\s+", "", str(current.get("style", "")).lower())
        classes = {str(value).lower() for value in current.get("class", [])}
        if current.has_attr("hidden") or current.get("aria-hidden") == "true" or current.get("data-active") == "false":
            return False
        if "display:none" in style or "visibility:hidden" in style or classes & HIDDEN_CLASSES:
            return False
        current = current.parent if isinstance(current.parent, Tag) else None
    return True


def decimal_string(raw: str | None, *, multiplier: int = 1) -> str | None:
    if not raw:
        return None
    match = re.search(r"-?[\d,]+(?:\.\d+)?", raw)
    if not match:
        return None
    try:
        value = Decimal(match.group().replace(",", "")) * multiplier
    except InvalidOperation:
        return None
    return format(value, "f")


def won(raw: str | None) -> str | None:
    if not raw:
        return None
    match = re.search(r"([\d,]+(?:\.\d+)?)\s*(만원|원)", raw)
    if not match:
        return None
    return decimal_string(match.group(1), multiplier=10_000 if match.group(2) == "만원" else 1)


def evidence(location: str, value: Any, source_text: str | None = None, context: str | None = None) -> dict[str, Any]:
    proof: dict[str, Any] = {"location": location, "raw": value}
    if source_text is not None:
        proof["source_text"] = source_text
    if context is not None:
        proof["context"] = context
    return proof
