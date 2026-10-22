"""Assistant-proposed collection rules: page context, safety checks and a trial gate.

A rule the host assistant proposes is stored in the plan with ``origin``
``"proposed"`` and is never applied until a trial verifies it:

1. one live capture of an approved page with the rule,
2. the evidence saved, and the saved bytes re-extracted to the same rows,
3. at least one priced row, no access challenge, a settled page,
4. a trial receipt that records the rule's fingerprint.

Only ``trial_rule`` writes trial records; plan edits cannot. The plan
revision that records a trial still needs the user's confirmation before a
job uses the rule, and every job re-checks the receipt before applying it.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import replace
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from bs4 import Comment, NavigableString, Tag

from .models import FetchResult, Source, stable_id, utc_now

ORIGINS = frozenset({"reviewed", "proposed"})
TRIAL_KEYS = frozenset({"status", "reason", "url", "at", "rule_fingerprint", "receipt", "receipt_sha256",
                        "evidence_sha256", "rows", "priced_rows", "sample"})
# Steps of a proposed rule may never press anything that buys, signs in or submits.
FORBIDDEN_TARGET = re.compile(r"submit|buy|purchase|checkout|cart|basket|payment|\bpay\b|order|log-?in|sign-?in|"
                              r"sign-?up|register|subscribe|구매|결제|주문|장바구니|로그인|회원가입|가입하기|구독", re.I)
CONTEXT_CHARS = 60_000


def rule_fingerprint(rule: dict) -> str:
    return stable_id({key: value for key, value in rule.items() if key not in {"origin", "trial"}})


def validate_proposed(rule: dict) -> None:
    """Reject proposed rules that type, use a signed-in profile or press risky controls."""
    if rule.get("profile_dir"):
        raise ValueError("A proposed rule cannot use a browser profile")
    for step in rule.get("recipe", []):
        if step.get("action") == "fill":
            raise ValueError("A proposed rule cannot type into the page")
        targets = []
        for key, value in step.items():
            if "selector" in key:
                targets.extend(value if isinstance(value, list) else [value])
        if any(isinstance(target, str) and FORBIDDEN_TARGET.search(target) for target in targets):
            raise ValueError("A proposed rule cannot press purchase, sign-in, submit or subscription controls")


def normalize_trial(value: Any) -> dict:
    if not isinstance(value, dict) or set(value) - TRIAL_KEYS or value.get("status") not in {"verified", "failed"}:
        raise ValueError("A rule trial must be a recorded trial result")
    return value


def _receipt_problem(rule: dict, output: Path | None) -> str | None:
    trial = rule.get("trial") or {}
    if trial.get("status") != "verified":
        return "rule_not_trial_verified"
    if trial.get("rule_fingerprint") != rule_fingerprint(rule):
        return "rule_changed_after_trial"
    if output is None:
        return None
    receipt = output / "rule-trials" / str(trial.get("receipt", ""))
    try:
        data = receipt.read_bytes() if receipt.resolve().parent == (output / "rule-trials").resolve() else b""
    except OSError:
        data = b""
    if not data or hashlib.sha256(data).hexdigest() != trial.get("receipt_sha256"):
        return "trial_receipt_missing_or_changed"
    try:
        stored = json.loads(data)
    except ValueError:
        return "trial_receipt_mismatch"
    if stored.get("rule_fingerprint") != trial["rule_fingerprint"] or stored.get("status") != "verified":
        return "trial_receipt_mismatch"
    return None


def active_rules(rules: dict, output: Path | None = None) -> tuple[dict, dict]:
    """Split rules into those a job may apply and proposed ones held back (with the reason)."""
    active, held = {}, {}
    for key, rule in rules.items():
        problem = _receipt_problem(rule, output) if rule.get("origin") == "proposed" else None
        if problem:
            held[key] = problem
        else:
            active[key] = rule
    return active, held


# -- page context for a proposal ---------------------------------------------

_DROP = ("script", "style", "noscript", "svg", "template", "iframe", "link", "meta", "head")


def _label(node: Tag) -> str:
    label = node.name
    if node.get("id"):
        label += "#" + str(node["id"])
    classes = [str(c) for c in node.get("class", [])][:4]
    label += "".join("." + c for c in classes)
    for attribute in ("type", "name", "role", "aria-label", "data-testid"):
        if node.get(attribute):
            label += f'[{attribute}="{str(node[attribute])[:40]}"]'
    if node.name == "a" and node.get("href"):
        label += f'[href="{urlsplit(str(node["href"])).path[:80]}"]'
    return label


def _own_text(node: Tag) -> str:
    text = " ".join(str(child).strip() for child in node.children
                    if isinstance(child, NavigableString) and not isinstance(child, Comment) and str(child).strip())
    return re.sub(r"\s+", " ", text)[:80]


def page_skeleton(soup: Any) -> str:
    """An indented outline of the page: tags, ids, classes and short own text.

    Runs of siblings with the same shape keep their first three members and a
    count, so a long product grid still fits the budget.
    """
    for node in soup.select(",".join(_DROP)):
        node.decompose()
    lines: list[str] = []
    size = [0]

    def emit(line: str) -> bool:
        if size[0] + len(line) > CONTEXT_CHARS:
            return False
        lines.append(line)
        size[0] += len(line) + 1
        return True

    def walk(node: Tag, depth: int) -> bool:
        text = _own_text(node)
        if not emit("  " * depth + _label(node) + (f": {text}" if text else "")):
            return False
        if depth >= 30:
            return True
        children = [child for child in node.children if isinstance(child, Tag)]
        index = 0
        while index < len(children):
            child = children[index]
            shape = (child.name, tuple(child.get("class", [])))
            run = index
            while run < len(children) and (children[run].name, tuple(children[run].get("class", []))) == shape:
                run += 1
            for member in children[index:min(run, index + 3)]:
                if not walk(member, depth + 1):
                    return False
            if run - index > 3 and not emit("  " * (depth + 1) + f"... {run - index - 3} more {_label(child)}"):
                return False
            index = run
        return True

    body = soup.body or soup
    walk(body, 0)
    return "\n".join(lines)


def interactive_controls(soup: Any) -> list[dict[str, Any]]:
    controls = []
    for node in soup.select("select")[:20]:
        controls.append({"kind": "select", "element": _label(node),
                         "options": [re.sub(r"\s+", " ", option.get_text(" ", strip=True))[:60]
                                     for option in node.select("option")[:30]]})
    for node in soup.select("button, [role=button], a[class*=more], a[class*=next], [class*=pagination] a")[:60]:
        text = re.sub(r"\s+", " ", node.get_text(" ", strip=True))[:60]
        controls.append({"kind": "button" if node.name != "a" else "link", "element": _label(node), "text": text,
                         "blocked_for_proposals": bool(FORBIDDEN_TARGET.search(_label(node) + " " + text))})
    return controls


def page_context(result: FetchResult) -> dict[str, Any]:
    """What the assistant sees to propose a rule: outline, controls and what extraction found."""
    from .adapters.common import html_soup
    from .generic_extraction import extract_generic, generic_sections

    rows = extract_generic(replace(result, trace=[]))
    sections = generic_sections(result)
    return {"final_url": result.final_url, "skeleton": page_skeleton(html_soup(result.content)),
            "controls": interactive_controls(html_soup(result.content)),
            "generic_extraction": [{key: section.get(key) for key in ("method", "status", "observed_count",
                                                                   "suggested_rule", "settle_selector")}
                                   for section in sections],
            "sample_rows": [{key: row.fields.get(key) for key in ("name", "price", "currency", "price_basis")}
                            for row in rows[:5]]}


def _source(plan: dict, url: str) -> Source:
    from .amounts import market_currency
    host = urlsplit(url).hostname or ""
    return Source("rule_trial_" + stable_id(url), host, "web", url, [], allowed_domains=[host],
                  backends=["playwright"], max_attempts=1, timeout_seconds=90, min_interval_seconds=0,
                  market_currency=market_currency((plan.get("topic") or {}).get("market")))


def _approved_url(plan: dict, url: str) -> str:
    from .research_plans import canonical_url
    url = canonical_url(url)
    if url not in {candidate["url"] for candidate in plan.get("candidates", [])}:
        raise ValueError("The URL must be one of the plan's candidate URLs")
    return url


def prepare_context(root: str | Path, plan_id: str, url: str, *, collector=None) -> dict[str, Any]:
    """Capture one approved page with the browser and return its proposal context."""
    from . import research_plans as plans
    from .assistant_workspace import workspace_root
    from .collectors import collect
    base = workspace_root(root)
    plan = plans.get_plan(base, plan_id)["plan"]
    url = _approved_url(plan, url)
    result = (collector or collect)(_source(plan, url), str(base), "playwright")
    if result.status != "fetched":
        return {"url": url, "status": result.status, "message": result.message}
    return {"url": url, "status": result.status, **page_context(result)}


# -- trial ---------------------------------------------------------------------

def _rows(result: FetchResult, source: Source) -> list:
    from .extraction import extract
    from .generic_extraction import apply_market_currency, extract_generic
    found = extract(result, source) if source.selectors or result.variant_snapshots else []
    return found or apply_market_currency(extract_generic(result, source), source.market_currency)


def _semantic(rows: list) -> list:
    return [(row.locator, sorted((key, str(value)) for key, value in row.fields.items())) for row in rows]


def trial_rule(root: str | Path, plan_id: str, expected_revision: int, rule_key: str, url: str | None = None,
               *, collector=None) -> dict[str, Any]:
    """Run one rule once on an approved page and record the verified or failed trial in a new revision."""
    from . import research_plans as plans
    from .assistant_workspace import workspace_root
    from .collection_rules import apply_rule, is_pattern, pattern_matches
    from .collectors import collect
    from .pipeline import _save_evidence
    from .research_batch import _output
    base = workspace_root(root)
    plan = plans.get_plan(base, plan_id)["plan"]
    if plan["revision"] != expected_revision:
        raise ValueError("Plan revision changed; reload the plan")
    rules = dict(plan.get("collection_rules") or {})
    if rule_key not in rules:
        raise ValueError("No collection rule with that key")
    rule = {key: value for key, value in rules[rule_key].items() if key != "trial"}
    if url is None:
        url = rule_key if not is_pattern(rule_key) else next(
            (candidate["url"] for candidate in plan.get("candidates", []) if pattern_matches(rule_key, candidate["url"])), "")
    url = _approved_url(plan, url)
    if is_pattern(rule_key) and not pattern_matches(rule_key, url) or not is_pattern(rule_key) and url != rule_key:
        raise ValueError("The trial URL must be a page this rule applies to")
    source = apply_rule(_source(plan, url), {url: rule}, base)
    result = (collector or collect)(source, str(base), "playwright")
    output = _output(base, plan_id)
    record: dict[str, Any] = {"url": url, "at": utc_now(), "rule_fingerprint": rule_fingerprint(rule)}
    if result.status != "fetched":
        record.update(status="failed", reason=f"capture {result.status}: {result.message}"[:300])
    else:
        rows = _rows(result, source)
        path, digest = _save_evidence(output / "rule-trials", result, source)
        replayed = _rows(replace(result, content=Path(path).read_bytes(), trace=[]), source)
        priced = sum(row.fields.get("price") is not None for row in rows)
        problems = [reason for reason, failed in (
            ("no priced rows", not priced),
            ("saved evidence re-extracts to different rows", _semantic(rows) != _semantic(replayed)),
            ("page did not settle", any(event.get("event") == "observation_unsettled" for event in result.trace)),
            ("access challenge", any(event.get("event") == "access_challenge" for event in result.trace)),
        ) if failed]
        record.update(status="failed" if problems else "verified", reason="; ".join(problems),
                      evidence_sha256=digest, rows=len(rows), priced_rows=priced,
                      sample=[{key: row.fields.get(key) for key in ("name", "price", "currency")} for row in rows[:3]])
    receipt_name = f"{record['rule_fingerprint']}.{stable_id(record['at'], url)}.json"
    receipt = output / "rule-trials" / receipt_name
    receipt.parent.mkdir(parents=True, exist_ok=True)
    data = json.dumps({**record, "rule_key": rule_key, "rule": rule}, ensure_ascii=False, indent=2).encode("utf-8")
    receipt.write_bytes(data)
    record.update(receipt=receipt_name, receipt_sha256=hashlib.sha256(data).hexdigest())
    rules[rule_key] = {**rule, "trial": record}
    revised = plans.revise_plan(base, plan_id, expected_revision, {"collection_rules": rules}, trial_writer=True)
    return {"trial": record, "plan": revised["plan"]}
