"""Resumable, evidence-preserving Amazoncar public-catalog collection.

The input manifest is a fixed comparison cohort. A fresh catalog enumeration is
always stored separately; it never silently changes that cohort's denominator.
This module deliberately produces captures, not normalized SourceLedger prices.
"""

from __future__ import annotations

import argparse
import asyncio
from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import time
from urllib.robotparser import RobotFileParser

BASE = "https://www.amazoncar.co.kr"
PRODUCT_RE = re.compile(r"^/cars/(?:new/\d+/\d+|used/\d+)$")
QUOTE_RE = re.compile(r"(\d+)개월\s+([\d,]+)원")


def utc() -> str:
    return datetime.now(timezone.utc).isoformat()


def write_json(path: Path, data: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    pending = path.with_name(path.name + ".pending")
    pending.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(pending, path)


def read_json(path: Path, default: object = None) -> object:
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else default


def product_id(href: str) -> str:
    if not PRODUCT_RE.fullmatch(href):
        raise ValueError(f"Invalid product path: {href}")
    return href.removeprefix("/cars/").replace("/", "-")


def load_manifest(path: Path) -> list[dict]:
    data = read_json(path)
    if not isinstance(data, dict) or not isinstance(data.get("products"), list):
        raise ValueError("Manifest must contain a products list")
    unique = {}
    for entry in data["products"]:
        href = entry.get("href") if isinstance(entry, dict) else None
        product_id(href)
        if href in unique:
            raise ValueError(f"Duplicate product path: {href}")
        unique[href] = entry
    if not unique:
        raise ValueError("Manifest has no products")
    return list(unique.values())


class EvidenceStore:
    def __init__(self, root: Path):
        self.root = root

    def put(self, data: str | bytes, suffix: str) -> dict:
        payload = data.encode("utf-8") if isinstance(data, str) else data
        digest = hashlib.sha256(payload).hexdigest()
        path = self.root / "evidence" / digest[:2] / (digest + suffix)
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.exists():
            pending = path.with_name(path.name + ".pending")
            pending.write_bytes(payload)
            os.replace(pending, path)
        return {"path": str(path.relative_to(self.root)), "sha256": digest}

    def copy_reference(self, old_root: Path, ref: dict) -> dict:
        source = old_root / ref["path"]
        digest = hashlib.sha256()
        with source.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        if digest.hexdigest() != ref["sha256"]:
            raise ValueError(f"Evidence hash mismatch: {source}")
        destination = self.root / "evidence" / ref["sha256"][:2] / (ref["sha256"] + source.suffix)
        destination.parent.mkdir(parents=True, exist_ok=True)
        if not destination.exists():
            try:
                os.link(source, destination)  # same-volume replay uses no extra blob space
            except OSError:
                pending = destination.with_name(destination.name + ".pending")
                shutil.copyfile(source, pending)
                os.replace(pending, destination)
        return {"path": str(destination.relative_to(self.root)), "sha256": ref["sha256"]}


class Journal:
    def __init__(self, root: Path):
        self.path = root / "stage-events.jsonl"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.started = time.monotonic()

    def event(self, stage: str, event: str, **fields: object) -> None:
        record = {"utc": utc(), "elapsed_seconds": time.monotonic() - self.started,
                  "stage": stage, "event": event, **fields}
        with self.path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(record, ensure_ascii=False) + "\n")


# Capture shape intentionally matches the earlier Amazoncar collector. The
# expensive whole-document scan is performed once per settled mode, not on each
# readiness poll.
CAPTURE_JS = r"""() => {
 const visible=e=>!!e.getClientRects().length&&getComputedStyle(e).visibility!=='hidden'&&getComputedStyle(e).display!=='none';
 const bs=[...document.querySelectorAll('button')].filter(visible);
 const conditions=[...document.querySelectorAll('[class*="title-row"]')].filter(visible).map(e=>({label:e.querySelector('[class*="title"]')?.innerText||'',display:e.innerText,selected:[...e.parentElement.querySelectorAll('button[class*="active"],input:checked')].filter(visible).map(x=>({text:x.innerText,html:x.outerHTML})),choices:[...e.parentElement.querySelectorAll('button')].filter(visible).map(x=>x.innerText)}));
 return {text:document.body.innerText,title:[...document.querySelectorAll('h1,h2,h3,[class*="car-name"]')].filter(visible).map(x=>x.innerText),links:[...document.querySelectorAll('a[href]')].filter(visible).map(x=>({text:x.innerText,href:x.getAttribute('href')})),buttons:bs.map(x=>({text:x.innerText,class:x.className,html:x.outerHTML})),conditions,selected:bs.filter(x=>x.className.includes('active')).map(x=>({text:x.innerText,html:x.outerHTML})),inputs:[...document.querySelectorAll('input,select')].filter(visible).map(x=>({html:x.outerHTML,value:x.value,checked:x.checked})),tables:[...document.querySelectorAll('table')].filter(visible).map(x=>({text:x.innerText,html:x.outerHTML,rows:[...x.rows].map(r=>[...r.cells].map(c=>c.innerText))}))};
}"""

# Only the controls needed to establish quote readiness are inspected. A
# A lightweight signature must repeat across successive polls. After a mode
# switch, a changed quote is the normal proof of fresh pricing; an unchanged
# quote takes the slower network-quiescence path and remains flagged for review.
READY_JS = r"""() => {
 const visible=e=>!!e.getClientRects().length&&getComputedStyle(e).visibility!=='hidden';
 const bs=[...document.querySelectorAll('button')].filter(visible);
 const availableModes=bs.map(e=>e.innerText.trim()).filter(s=>['장기렌트','리스'].includes(s));
 const modes=bs.filter(e=>e.className.includes('active')&&['장기렌트','리스'].includes(e.innerText.trim())).map(e=>e.innerText.trim());
 const quotes=bs.map(e=>e.innerText.trim()).filter(s=>/^\d+개월\s+[\d,]+원$/.test(s));
 const conditions=[...document.querySelectorAll('[class*="title-row"]')].filter(visible).map(e=>[e.innerText,[...e.parentElement.querySelectorAll('button[class*="active"],input:checked')].map(x=>x.innerText||x.value)]);
 const links=[...document.querySelectorAll('a[href]')].filter(e=>!!e.getClientRects().length).map(e=>e.getAttribute('href')).filter(x=>/^\/cars\/(new\/\d+\/\d+|used\/\d+)$/.test(x));
 const tableRows=[...document.querySelectorAll('table')].filter(e=>!!e.getClientRects().length).reduce((n,e)=>n+e.rows.length,0);
 const heading=[...document.querySelectorAll('h1,h2,h3,[class*="car-name"]')].filter(e=>!!e.getClientRects().length).map(e=>e.innerText.trim()).filter(Boolean);
 const bad=quotes.some(s=>s.includes('undefined')||s.includes('NaN'));
 return {availableModes,modes,quotes,conditions,links,tableRows,heading,invalid:bad};
}"""

LINKS_JS = r"""() => [...document.querySelectorAll('a[href]')].filter(e=>!!e.getClientRects().length).map(e=>({text:e.innerText,href:e.getAttribute('href')})).filter(x=>/^\/cars\/(new\/\d+\/\d+|used\/\d+)$/.test(x.href))"""


async def wait_ready(page, *, kind: str = "detail", mode: str | None = None, allow_no_quote: bool = False,
                     previous_quotes: list[str] | None = None,
                     timeout_seconds: float = 15) -> dict:
    last = None
    repeats = 0
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        state = await page.evaluate(READY_JS)
        valid = not state["invalid"]
        if kind == "detail":
            valid = valid and bool(state["availableModes"]) and bool(state["heading"])
            valid = valid and (not mode or mode in state["modes"])
            valid = valid and (allow_no_quote or bool(state["quotes"]))
            if previous_quotes:
                valid = valid and state["quotes"] != previous_quotes
        elif kind == "list":
            valid = valid and bool(state["links"])
        elif kind == "tariff":
            valid = valid and state["tableRows"] >= 2
        elif kind == "product":
            valid = valid and bool(state["heading"])
        else:
            raise ValueError(f"Unknown readiness kind: {kind}")
        signature = json.dumps([state["availableModes"], state["modes"], state["quotes"],
                                state["conditions"], state["links"], state["tableRows"],
                                state["heading"]], ensure_ascii=False)
        repeats = repeats + 1 if valid and signature == last else 0
        if repeats >= 3:
            return state
        last = signature
        await page.wait_for_timeout(180)
    raise TimeoutError(f"Amazoncar {kind} controls did not settle: {page.url}, mode={mode}")


async def navigate(page, url: str, *, kind: str = "detail", allow_no_quote: bool = False) -> None:
    response = await page.goto(url, wait_until="domcontentloaded", timeout=60_000)
    if response and response.status >= 400:
        raise RuntimeError(f"HTTP {response.status} for {url}")
    await wait_ready(page, kind=kind, allow_no_quote=allow_no_quote)


async def wait_mode_transition(page, label: str, previous_quotes: list[str], *,
                               changed_timeout: float = 3, empty_timeout: float = 9) -> tuple[dict, str]:
    """Settle a switched mode without assuming different modes always cost different amounts.

    Equal quote arrays are allowed only after network quiescence, and are
    returned with an explicit review flag. A mode with no quote is also flagged.
    """
    try:
        state = await wait_ready(page, mode=label, previous_quotes=previous_quotes,
                                 timeout_seconds=changed_timeout)
        return state, "changed_after_switch" if previous_quotes else "first_quotes_after_switch"
    except TimeoutError:
        # An unchanged amount or an empty quote control may be a hydration gap.
        # Wait for the old quotes to change before considering either fallback.
        try:
            state = await wait_ready(page, mode=label, previous_quotes=previous_quotes,
                                     timeout_seconds=empty_timeout)
            return state, "changed_after_switch" if state["quotes"] else "no_displayed_quotes_review"
        except TimeoutError:
            state = await wait_ready(page, mode=label, allow_no_quote=True,
                                     timeout_seconds=3)
        if not state["quotes"]:
            try:
                await page.wait_for_load_state("networkidle", timeout=5_000)
            except Exception as exc:
                raise RuntimeError(f"Empty quote transition not proved for {label}") from exc
            state = await wait_ready(page, mode=label, allow_no_quote=True, timeout_seconds=3)
            if not state["quotes"]:
                return state, "no_displayed_quotes_review"
        if state["quotes"] != previous_quotes:
            return state, "changed_after_switch"
        # The active mode alone does not establish that the old amounts were
        # replaced. A quiet network plus repeated stable controls establishes
        # a credible equal-valued result, but it still deserves human review.
        await page.wait_for_load_state("networkidle", timeout=5_000)
        state = await wait_ready(page, mode=label, timeout_seconds=3)
        if state["quotes"] != previous_quotes:
            return state, "changed_after_switch"
        return state, "identical_amounts_after_network_quiet_review"


async def capture(page, store: EvidenceStore, label: str) -> dict:
    data = await page.evaluate(CAPTURE_JS)
    data.update(url=page.url, captured_utc=utc(), label=label,
                html=store.put(await page.content(), ".html"),
                rendered_text=store.put(data["text"], ".txt"))
    proof = {key: data[key] for key in ("conditions", "selected", "inputs")}
    data["selection_proof"] = store.put(json.dumps(proof, ensure_ascii=False), ".json")
    return data


def extract(capture_data: dict, pid: str, mode: str) -> tuple[list[dict], list[dict]]:
    ref = {key: capture_data[key] for key in ("url", "captured_utc", "html", "rendered_text", "selection_proof")}
    quotes, options = [], []
    for index, button in enumerate(capture_data["buttons"]):
        match = QUOTE_RE.fullmatch(button["text"])
        if match:
            quotes.append({"product_id": pid, "mode": mode, "term_months": int(match[1]),
                           "amount": match[2].replace(",", ""), "currency": None,
                           "currency_reason": "Only Korean won symbol/text rendered; no ISO currency code observed",
                           "raw": button["text"], "price_raw": match[2] + "원",
                           "period_raw": match[1] + "개월", "locator": f"visible button index {index}",
                           "conditions": capture_data["conditions"], "selected": capture_data["selected"],
                           "vat_raw": next((line for line in capture_data["text"].splitlines()
                                            if "부가세 포함" in line and line.strip().startswith("*")), None),
                           "evidence": ref})
        if "_btn-option_" in button["html"]:
            values = re.findall(r"[+]?\s*[\d,]+원", button["text"])
            options.append({"product_id": pid, "mode": mode, "raw": button["text"],
                            "surcharge_raw": values[-1].strip() if values else None,
                            "html": button["html"], "evidence": ref,
                            "locator": f"visible button index {index}"})
    return quotes, options


async def collect_detail(page, item: dict, store: EvidenceStore) -> dict:
    href = item["href"]
    pid = product_id(href)
    await navigate(page, BASE + href, allow_no_quote=True)
    captures, quotes, options, coverage = [], [], [], {}
    for mode, label in (("rent", "장기렌트"), ("lease", "리스")):
        button = page.get_by_role("button", name=label, exact=True)
        if not await button.count():
            coverage[mode] = {"status": "unavailable", "reason": "mode control absent"}
            continue
        before = await page.evaluate(READY_JS)
        switched = label not in before["modes"]
        if switched:
            await button.click()
        if switched:
            _, transition = await wait_mode_transition(page, label, before["quotes"])
        else:
            try:
                await wait_ready(page, mode=label)
                transition = "already_selected"
            except TimeoutError:
                await wait_ready(page, mode=label, allow_no_quote=True)
                transition = "no_displayed_quotes_review"
        cap = await capture(page, store, pid + "-" + mode)
        found_quotes, found_options = extract(cap, pid, mode)
        if not any(x["text"] == label for x in cap["selected"]):
            raise RuntimeError(f"Captured wrong quote mode for {href}: {mode}")
        coverage[mode] = {"status": "captured" if found_quotes else "no_displayed_quotes",
                          "quotes": len(found_quotes),
                          "quote_transition": transition,
                          "active_mode": [x["text"] for x in cap["selected"]
                                          if x["text"] in ("장기렌트", "리스")]}
        if transition.endswith("_review"):
            cap.setdefault("integrity_flags", []).append(transition)
            for quote in found_quotes:
                quote.setdefault("review_flags", []).append(transition)
        captures.append(cap)
        quotes.extend(found_quotes)
        options.extend(found_options)
    return {"id": pid, "catalog_identity": href, "url": BASE + href, "listing": item,
            "name": captures[0]["title"] if captures else [], "mode_coverage": coverage,
            "captures": captures, "quotes": quotes, "options": options}


async def check_robots(page, store: EvidenceStore, product_paths: list[str]) -> None:
    response = await page.goto(BASE + "/robots.txt", wait_until="domcontentloaded", timeout=30_000)
    text = await page.locator("body").inner_text()
    if response is None or response.status != 200:
        raise RuntimeError("robots.txt unavailable")
    parser = RobotFileParser()
    parser.parse(text.splitlines())
    paths = ["/list/NewCars", "/list/UsedCars", "/rentPrice", *product_paths]
    prohibited = [path for path in paths if not parser.can_fetch("SourceLedgerPublicCatalog", BASE + path)]
    if prohibited:
        raise RuntimeError(f"robots.txt disallows {prohibited[:3]}")
    write_json(store.root / "robots.json", {"url": BASE + "/robots.txt", "utc": utc(),
                                             "text": text, "content": store.put(text, ".txt")})


async def discover_catalog(page, kind: str, store: EvidenceStore, journal: Journal) -> dict:
    await navigate(page, BASE + "/list/" + kind, kind="list")
    seen = {}
    same = 0
    last_height = None
    history = []
    for iteration in range(250):
        for link in await page.evaluate(LINKS_JS):
            seen[link["href"]] = link
        scroll = await page.evaluate("""() => {const es=[document.scrollingElement,...document.querySelectorAll('*')].filter(e=>e.scrollHeight>e.clientHeight+20&&['auto','scroll'].includes(getComputedStyle(e).overflowY));es.forEach(e=>e.scrollTop=e.scrollHeight);window.scrollTo(0,document.body.scrollHeight);return es.map(e=>[e.scrollTop,e.scrollHeight,e.clientHeight])}""")
        signature = json.dumps([len(seen), scroll])
        same = same + 1 if signature == last_height else 0
        history.append({"iteration": iteration, "count": len(seen), "scroll": scroll, "utc": utc()})
        if same >= 6:
            break
        last_height = signature
        await page.wait_for_timeout(450)
    result = {"entries": list(seen.values()), "termination": {
        "reason": "six unchanged bottom-scroll passes" if same >= 6 else "iteration limit",
        "stable_passes": same, "history": history}, "capture": await capture(page, store, "catalog-" + kind)}
    journal.event("catalog", "catalog_section", kind=kind, count=len(seen), stable_passes=same)
    if not seen or same < 6:
        raise RuntimeError(f"Catalog {kind} was empty or did not reach its end")
    return result


async def discover_groups(page, item: dict, store: EvidenceStore) -> dict:
    await navigate(page, BASE + item["href"], allow_no_quote=True)
    category_names = await page.locator('button[class*="category-item"]').all_text_contents() or [None]
    found, checks = {}, []
    for category in category_names:
        if category is not None:
            await page.get_by_role("button", name=category, exact=True).click()
            await page.wait_for_timeout(500)
        expected_match = re.search(r"\((\d+)\)\s*$", category or "")
        expected = int(expected_match[1]) if expected_match else None
        group_links, pages, signatures = {}, [], set()
        termination = "limit"
        for number in range(40):
            links = [x for x in await page.evaluate(LINKS_JS) if x["href"].startswith("/cars/new/")]
            if not links:
                # Some public catalog entries are standalone detail pages,
                # with no sibling trim navigation at all. Preserve the seed
                # and the observed absence of those controls as evidence.
                navigation = page.locator('[class*="trim-slider-box"],button[class*="category-item"]')
                if category is None and await navigation.count() == 0:
                    links = [item]
                    termination = "standalone_detail_no_trim_controls"
                else:
                    raise RuntimeError(f"Trim group has no visible product links: {item['href']}, {category}")
            signature = tuple(sorted(x["href"] for x in links))
            if signature in signatures:
                termination = "carousel_cycle"
                break
            signatures.add(signature)
            for link in links:
                group_links[link["href"]] = link
                found[link["href"]] = link
            pages.append({"index": number, "links": links,
                          "capture": {key: value for key, value in
                                      (await capture(page, store, f"trim-page-{number}")).items()
                                      if key in ("url", "captured_utc", "html", "rendered_text", "selection_proof")}})
            if termination == "standalone_detail_no_trim_controls":
                break
            if expected is not None and len(group_links) >= expected:
                termination = "displayed_count_reached"
                break
            right = page.locator('[class*="trim-slider-box"] button[class*="right"]')
            if not await right.count() or not await right.is_visible() or not await right.is_enabled():
                termination = "carousel_end"
                break
            await right.click()
            try:
                await page.wait_for_function(
                    """previous => JSON.stringify([...document.querySelectorAll('a[href]')].filter(e=>!!e.getClientRects().length && /^\\/cars\\/new\\/\\d+\\/\\d+$/.test(e.getAttribute('href'))).map(e=>e.getAttribute('href'))) !== previous""",
                    arg=json.dumps([x["href"] for x in links]), timeout=5_000)
            except Exception:
                termination = "stalled_with_next_control"
                break
        checks.append({"group": category, "expected": expected, "found": len(group_links),
                       "links": list(group_links.values()), "pages": pages,
                       "termination": termination,
                       "count_match": expected is None or len(group_links) == expected})
    return {"seed": item, "groups": checks, "trims": list(found.values())}


def replay(old_root: Path, output: Path, items: list[dict], journal: Journal) -> dict:
    store = EvidenceStore(output)
    old_details = old_root / "details-checkpoint.json"
    aggregate = None
    missing = []
    for index, item in enumerate(items, 1):
        href = item["href"]
        pid = product_id(href)
        target = output / "checkpoints" / "details" / (pid + ".json")
        if target.exists():
            continue
        source = old_root / "checkpoints" / "details" / (pid + ".json")
        if source.exists():
            entry = read_json(source)
            value = entry["data"]
        else:
            if aggregate is None:
                aggregate = read_json(old_details, {})
            value = aggregate.get(href)
        if not isinstance(value, dict):
            missing.append(href)
            continue
        # Preserve the original observation time and raw payload. Evidence is
        # copied into the new content-addressed store after hash verification.
        value = json.loads(json.dumps(value, ensure_ascii=False))
        for cap in value.get("captures", []):
            for key in ("html", "rendered_text", "selection_proof"):
                cap[key] = store.copy_reference(old_root, cap[key])
            refs = {key: cap[key] for key in ("url", "captured_utc", "html", "rendered_text", "selection_proof")}
            for quote in value.get("quotes", []):
                if quote.get("mode") == cap["label"].rsplit("-", 1)[-1]:
                    quote["evidence"] = refs
            for option in value.get("options", []):
                if option.get("mode") == cap["label"].rsplit("-", 1)[-1]:
                    option["evidence"] = refs
        write_json(target, {"href": href, "data": value, "replayed_from": str(old_root)})
        if index % 100 == 0:
            journal.event("replay", "progress", processed=index, expected=len(items))
    for filename in ("short-term-captures.json", "short_term.json", "product-introduction.json"):
        source = old_root / filename
        if not source.exists():
            continue
        data = read_json(source)
        # Captures retain original timestamps. Only evidence blobs needed by
        # the standalone replay directory are copied, with hash checks.
        def remap(node):
            if isinstance(node, dict):
                if "path" in node and "sha256" in node:
                    return store.copy_reference(old_root, node)
                return {key: remap(value) for key, value in node.items()}
            if isinstance(node, list):
                return [remap(value) for value in node]
            return node
        write_json(output / filename, remap(data))
    return {"missing": missing}


async def run_live(items: list[dict], output: Path, concurrency: int, journal: Journal) -> dict:
    from playwright.async_api import async_playwright

    store = EvidenceStore(output)
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        pages = [await browser.new_page(viewport={"width": 1440, "height": 1000})
                 for _ in range(concurrency)]
        catalog_page = await browser.new_page(viewport={"width": 1440, "height": 1000})
        try:
            await check_robots(catalog_page, store, [x["href"] for x in items])
            journal.event("collection", "start", expected=len(items), concurrency=concurrency)
            queue = asyncio.Queue()
            for item in items:
                target = output / "checkpoints" / "details" / (product_id(item["href"]) + ".json")
                old = read_json(target, {})
                if not old or "error" in old.get("data", {}):
                    queue.put_nowait(item)
            completed = len(items) - queue.qsize()
            processed_lock = asyncio.Lock()

            async def worker(number: int, page) -> None:
                nonlocal completed
                while True:
                    try:
                        item = queue.get_nowait()
                    except asyncio.QueueEmpty:
                        return
                    href = item["href"]
                    result = None
                    for attempt in range(1, 4):
                        try:
                            result = await collect_detail(page, item, store)
                            break
                        except Exception as exc:
                            result = {"error": str(exc), "attempt": attempt, "utc": utc()}
                            journal.event("collection", "retry", href=href, attempt=attempt, error=str(exc))
                            if attempt < 3:
                                await page.wait_for_timeout(500 * attempt)
                    target = output / "checkpoints" / "details" / (product_id(href) + ".json")
                    write_json(target, {"href": href, "data": result})
                    async with processed_lock:
                        completed += 1
                        journal.event("collection", "product", href=href, completed=completed,
                                      expected=len(items), ok="error" not in result, worker=number)
                    queue.task_done()
                    await page.wait_for_timeout(250)  # bounded per-worker rate limit

            await asyncio.gather(*(worker(i + 1, page) for i, page in enumerate(pages)))
            journal.event("collection", "end", completed=completed, expected=len(items))
            journal.event("short_term", "start")
            await navigate(catalog_page, BASE + "/rentPrice", kind="tariff")
            short_capture = await capture(catalog_page, store, "short-term-default")
            write_json(output / "short-term-captures.json", [short_capture])
            write_json(output / "short_term.json", [{"group": "default", "tables": short_capture["tables"],
                       "raw": short_capture["text"], "evidence": {key: short_capture[key]
                       for key in ("url", "captured_utc", "html", "rendered_text")}}])
            journal.event("short_term", "end", tables=len(short_capture["tables"]))
            await navigate(catalog_page, BASE + "/product", kind="product")
            write_json(output / "product-introduction.json",
                       await capture(catalog_page, store, "product-introduction"))
            journal.event("reconciliation", "start")
            new = await discover_catalog(catalog_page, "NewCars", store, journal)
            used = await discover_catalog(catalog_page, "UsedCars", store, journal)
            write_json(output / "inventory-final-new.json", new)
            write_json(output / "inventory-final-used.json", used)
            seed_queue = asyncio.Queue()
            for seed in new["entries"]:
                seed_queue.put_nowait(seed)
            groups = {}

            async def group_worker(page):
                while True:
                    try:
                        seed = seed_queue.get_nowait()
                    except asyncio.QueueEmpty:
                        return
                    for attempt in range(1, 3):
                        try:
                            groups[seed["href"]] = await discover_groups(page, seed, store)
                            break
                        except Exception as exc:
                            groups[seed["href"]] = {"error": str(exc), "attempt": attempt}
                            journal.event("reconciliation", "group_retry", seed=seed["href"], attempt=attempt)
                    write_json(output / "checkpoints" / "groups" / (product_id(seed["href"]) + ".json"),
                               {"href": seed["href"], "data": groups[seed["href"]]})
                    seed_queue.task_done()

            await asyncio.gather(*(group_worker(page) for page in pages))
            if len(groups) != len(new["entries"]):
                raise RuntimeError("Final catalog group enumeration did not process every seed")
            # Every seed remains in the final inventory even if its group
            # carousel fails. That failure is explicit in reconciliation.
            new_products = {x["href"]: x for value in groups.values() for x in value.get("trims", [])}
            for seed in new["entries"]:
                new_products.setdefault(seed["href"], seed)
            final_items = [*new_products.values(), *used["entries"]]
            write_json(output / "manifest-final.json", {"utc": utc(), "products": final_items,
                       "failed_groups": [href for href, value in groups.items() if "error" in value],
                       "group_count_mismatches": [{"seed": href, "group": group["group"],
                                                   "expected": group["expected"], "found": group["found"]}
                                                  for href, value in groups.items() for group in value.get("groups", [])
                                                  if not group["count_match"]],
                       "unfinished_carousels": [{"seed": href, "group": group["group"],
                                                  "termination": group["termination"]}
                                                 for href, value in groups.items() for group in value.get("groups", [])
                                                 if group["termination"] in ("limit", "stalled_with_next_control")]})
            journal.event("reconciliation", "end", products=len(final_items))
            return {"final_items": final_items,
                    "failed_groups": [href for href, value in groups.items() if "error" in value],
                    "group_count_mismatches": [{"seed": href, "group": group["group"]}
                                               for href, value in groups.items() for group in value.get("groups", [])
                                               if not group["count_match"]],
                    "unfinished_carousels": [{"seed": href, "group": group["group"]}
                                              for href, value in groups.items() for group in value.get("groups", [])
                                              if group["termination"] in ("limit", "stalled_with_next_control")],
                    "catalog_stable": True}
        finally:
            await catalog_page.close()
            for page in pages:
                await page.close()
            await browser.close()


def summarize(items: list[dict], output: Path, *, final_items: list[dict] | None = None,
              replay_missing: list[str] | None = None, reconciliation: dict | None = None) -> dict:
    expected = {item["href"] for item in items}
    captured, failed, empty_captures, no_quote_modes = [], [], [], []
    quote_counts = Counter()
    for item in items:
        path = output / "checkpoints" / "details" / (product_id(item["href"]) + ".json")
        entry = read_json(path)
        if not entry:
            continue
        value = entry["data"]
        if "error" in value:
            failed.append(item["href"])
        else:
            if not value.get("captures"):
                empty_captures.append(item["href"])
                continue
            captured.append(item["href"])
            quote_counts.update(quote["mode"] for quote in value.get("quotes", []))
            for mode, coverage in value.get("mode_coverage", {}).items():
                if coverage.get("status") == "no_displayed_quotes":
                    no_quote_modes.append({"href": item["href"], "mode": mode})
    final_set = {item["href"] for item in (final_items or [])}
    reconciliation = reconciliation or {}
    catalog_issues = {key: reconciliation.get(key, []) for key in
                      ("failed_groups", "group_count_mismatches", "unfinished_carousels")}
    result = {"utc": utc(), "expected_product_ids": sorted(expected),
              "fetched_product_ids": sorted(captured), "failed_product_ids": sorted(failed),
              "missing_product_ids": sorted(expected - set(captured) - set(failed)),
              "counts": {"expected": len(expected), "captured": len(captured),
                         "failed": len(failed), "quotes": sum(quote_counts.values()),
                         "quotes_by_mode": dict(quote_counts),
                         "empty_captures": len(empty_captures), "no_quote_modes": len(no_quote_modes)},
              "empty_capture_product_ids": sorted(empty_captures),
              "no_quote_modes": no_quote_modes,
              "replay_missing": replay_missing or [],
              "final_catalog": {"count": len(final_set) if final_items is not None else None,
                                "additions": sorted(final_set - expected) if final_items is not None else None,
                                "removals": sorted(expected - final_set) if final_items is not None else None},
              "catalog_issues": catalog_issues,
              "complete_fixed_cohort": len(captured) == len(expected) and not failed,
              "complete_current_catalog": final_items is not None
                  and not any(catalog_issues.values()) and not (final_set - set(captured)),
              "normalization_status": "not_run"}
    for name in ("short_term.json", "short-term-captures.json", "product-introduction.json"):
        result.setdefault("supplemental_capture", {})[name] = (output / name).exists()
    if final_items is not None:
        result["complete_current_catalog"] = result["complete_current_catalog"] and all(
            result["supplemental_capture"].values())
    write_json(output / "summary.json", result)
    return result


async def async_main(args: argparse.Namespace) -> dict:
    items = load_manifest(args.manifest)
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    journal = Journal(output)
    journal.event("run", "start", manifest=str(args.manifest.resolve()), expected=len(items),
                  mode="replay" if args.replay else "live")
    live_result = await run_live(items, output, args.concurrency, journal)
    result = summarize(items, output, final_items=live_result["final_items"], reconciliation=live_result)
    journal.event("run", "end", complete=result["complete_fixed_cohort"])
    return result


def replay_main(args: argparse.Namespace) -> dict:
    items = load_manifest(args.manifest)
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    journal = Journal(output)
    journal.event("run", "start", manifest=str(args.manifest.resolve()), expected=len(items), mode="replay")
    journal.event("replay", "start", source=str(args.replay.resolve()))
    replay_result = replay(args.replay.resolve(), output, items, journal)
    journal.event("replay", "end", missing=len(replay_result["missing"]))
    result = summarize(items, output, replay_missing=replay_result["missing"])
    journal.event("run", "end", complete=result["complete_fixed_cohort"])
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True, help="Fixed comparison manifest JSON")
    parser.add_argument("--output", type=Path, required=True, help="New run directory")
    parser.add_argument("--concurrency", type=int, default=2, help="Shared-browser product workers (default: 2)")
    parser.add_argument("--replay", type=Path, help="Offline replay source directory with prior checkpoints")
    args = parser.parse_args(argv)
    if not 1 <= args.concurrency <= 4:
        parser.error("--concurrency must be between 1 and 4")
    if args.replay and args.replay.resolve() == args.output.resolve():
        parser.error("--replay and --output must be different directories")
    try:
        result = replay_main(args) if args.replay else asyncio.run(async_main(args))
    except Exception as exc:
        print(json.dumps({"error": str(exc), "utc": utc()}, ensure_ascii=False), flush=True)
        return 1
    print(json.dumps(result["counts"], ensure_ascii=False), flush=True)
    return 0 if result["complete_fixed_cohort"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
