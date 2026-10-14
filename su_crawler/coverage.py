"""Coverage is independent of request success and observation verification."""
from collections import Counter

POLICY_VERSION = "coverage-v1"


def unknown_section(reason="No validated enumeration of this page's products and options", *, extracted_count=0):
    return {"section": "page_scope", "status": "unknown", "observed_count": None,
            "expected_count": None, "extracted_count": extracted_count, "price_count": None,
            "unparsed_count": None, "reason": reason, "scope": "observed_page"}


def page_status(page):
    if page.get("status") == "excluded":
        return "excluded"
    if page.get("status") not in {"visited", "fetched", "no_data"}:
        return "partial"
    sections = page.get("sections") or []
    if any(s.get("status") == "partial" for s in sections):
        return "partial"
    if not sections or any(s.get("status") != "complete" for s in sections):
        return "unknown"
    return "complete"


def summarize(pages):
    counts = Counter(page_status(page) for page in pages)
    return {key: counts[key] for key in ("complete", "partial", "unknown", "excluded")}


def apply_status(status, pages):
    counts = summarize(pages)
    if counts["partial"]:
        return "partial"
    if counts["unknown"] and status == "completed":
        return "needs_review"
    return status
