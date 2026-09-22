"""Shared, deterministic helpers for first-party site adapters."""
from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation
from typing import Any

from bs4 import NavigableString, Tag, UnicodeDammit


HIDDEN_CLASSES = {"hidden", "d-none", "is-hidden", "sr-only", "inactive", "is-inactive"}
EVIDENCE_MODES = frozenset({"rendered_dom", "static_html", "structured_record", "document_text", "unknown"})


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


def visible_text(node: Tag | None) -> str | None:
    """Read text while excluding descendants with explicit/snapshotted hidden state."""
    if node is None or not is_visible(node):
        return None
    parts = [str(value).strip() for value in node.descendants
             if isinstance(value, NavigableString) and str(value).strip()
             and isinstance(value.parent, Tag) and is_visible(value.parent)]
    value = re.sub(r"\s+", " ", " ".join(parts)).strip()
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
        if (current.has_attr("hidden") or current.get("aria-hidden") == "true"
                or current.get("data-active") == "false"
                or current.get("data-sourceledger-computed-hidden") == "true"):
            return False
        if "display:none" in style or "visibility:hidden" in style or classes & HIDDEN_CLASSES:
            return False
        current = current.parent if isinstance(current.parent, Tag) else None
    return True


def evidence_mode(result: Any, *, html: bool = True) -> str:
    """Describe what the captured bytes can prove about presentation."""
    if not html:
        return "unknown"
    return "rendered_dom" if result.backend == "playwright" else "static_html"


def display_state(node: Tag | None, mode: str) -> tuple[str, list[dict[str, Any]]]:
    """Return field-level display state and the ancestor facts behind it.

    Rendered snapshots preserve computed-hidden nodes with a dedicated marker.
    Static markup can prove that something is hidden, but cannot prove that an
    otherwise ordinary node was actually displayed by the browser.
    """
    hidden_by: list[dict[str, Any]] = []
    current = node
    depth = 0
    while isinstance(current, Tag):
        style = re.sub(r"\s+", "", str(current.get("style", "")).lower())
        classes = {str(value).lower() for value in current.get("class", [])}
        reasons: list[str] = []
        if current.has_attr("hidden"):
            reasons.append("hidden_attribute")
        if current.get("aria-hidden") == "true":
            reasons.append("aria_hidden")
        if current.get("data-active") == "false":
            reasons.append("inactive_state")
        if "display:none" in style:
            reasons.append("inline_display_none")
        if "visibility:hidden" in style:
            reasons.append("inline_visibility_hidden")
        if classes & HIDDEN_CLASSES:
            reasons.append("hidden_class")
        if current.get("data-sourceledger-computed-hidden") == "true":
            reasons.append("computed_hidden_snapshot")
        if reasons:
            hidden_by.append({"depth": depth, "tag": current.name, "reasons": reasons})
        current = current.parent if isinstance(current.parent, Tag) else None
        depth += 1
    if hidden_by:
        return "hidden", hidden_by
    if mode == "rendered_dom":
        return "visible", []
    if mode == "static_html":
        return "unconfirmed", []
    return "not_applicable", []


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


def evidence(location: str, value: Any, source_text: str | None = None, context: str | None = None,
             *, display: str | None = None, hidden_by: list[dict[str, Any]] | None = None,
             proof_kind: str | None = None) -> dict[str, Any]:
    proof: dict[str, Any] = {"location": location, "raw": value}
    if source_text is not None:
        proof["source_text"] = source_text
    if context is not None:
        proof["context"] = context
    if display is not None:
        proof["display_state"] = display
    if hidden_by:
        proof["hidden_by"] = hidden_by
    if proof_kind is not None:
        proof["proof_kind"] = proof_kind
    return proof


def displayed_evidence(location: str, value: Any, node: Tag | None, mode: str,
                       source_text: str | None = None, context: str | None = None,
                       *, proof_kind: str = "rendered_text") -> dict[str, Any]:
    state, hidden_by = display_state(node, mode)
    return evidence(location, value, source_text, context, display=state, hidden_by=hidden_by,
                    proof_kind=proof_kind)


def annotate_evidence(proofs: dict[str, dict[str, Any]], mode: str,
                      *, source_identity: set[str] | None = None,
                      displayed_fields: set[str] | None = None) -> dict[str, dict[str, Any]]:
    """Attach a conservative per-field display contract to adapter evidence."""
    identities = source_identity or set()
    displayed = displayed_fields or set()
    for field, proof in proofs.items():
        if field.startswith("_"):
            continue
        if field in identities:
            proof.setdefault("display_state", "not_applicable")
            proof.setdefault("proof_kind", "source_identity")
        else:
            visible = mode == "rendered_dom" and field in displayed
            proof.setdefault("display_state", "visible" if visible else "unconfirmed")
            proof.setdefault("proof_kind", "rendered_text" if visible else "html_text")
    return proofs
