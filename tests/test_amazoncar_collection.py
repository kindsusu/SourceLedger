"""Offline checks for the fixed-cohort Amazoncar capture runner."""

from pathlib import Path

import pytest

from su_crawler.amazoncar_collection import (
    EvidenceStore,
    Journal,
    extract,
    load_manifest,
    main,
    product_id,
    read_json,
    replay,
    summarize,
    wait_mode_transition,
    wait_ready,
    discover_groups,
    write_json,
)


def sample_item():
    return {"href": "/cars/new/000001/01", "text": "Sample trim"}


def sample_capture(root: Path):
    store = EvidenceStore(root)
    text = "장기렌트\n36개월\n630,740원\n* 부가세 포함"
    return {
        "label": "new-000001-01-rent",
        "url": "https://www.amazoncar.co.kr/cars/new/000001/01",
        "captured_utc": "2026-01-01T01:02:03+00:00",
        "html": store.put("<button>36개월 630,740원</button>", ".html"),
        "rendered_text": store.put(text, ".txt"),
        "selection_proof": store.put('{"mode":"rent"}', ".json"),
        "text": text,
        "conditions": [{"label": "보증금", "display": "0원", "selected": [], "choices": ["0원"]}],
        "selected": [{"text": "장기렌트", "html": "<button class=active>장기렌트</button>"}],
        "buttons": [
            {"text": "36개월 630,740원", "html": "<button>36개월 630,740원</button>", "class": ""},
            {"text": "옵션 + 100,000원", "html": '<button class="_btn-option_">옵션</button>', "class": ""},
        ],
    }


def test_manifest_requires_unique_valid_product_paths(tmp_path):
    path = tmp_path / "manifest.json"
    write_json(path, {"products": [sample_item()]})
    assert load_manifest(path) == [sample_item()]
    assert product_id(sample_item()["href"]) == "new-000001-01"
    write_json(path, {"products": [sample_item(), sample_item()]})
    with pytest.raises(ValueError, match="Duplicate"):
        load_manifest(path)
    write_json(path, {"products": [{"href": "https://example.com/bad"}]})
    with pytest.raises(ValueError, match="Invalid"):
        load_manifest(path)


def test_quote_and_option_keep_original_evidence_and_conditions(tmp_path):
    capture = sample_capture(tmp_path)
    quotes, options = extract(capture, "new-000001-01", "rent")
    assert len(quotes) == 1
    assert quotes[0]["price_raw"] == "630,740원"
    assert quotes[0]["term_months"] == 36
    assert quotes[0]["currency"] is None
    assert quotes[0]["conditions"] == capture["conditions"]
    assert quotes[0]["evidence"]["html"] == capture["html"]
    assert len(options) == 1
    assert options[0]["surcharge_raw"] == "+ 100,000원"


def test_replay_preserves_capture_time_and_verifies_evidence(tmp_path):
    old = tmp_path / "old"
    out = tmp_path / "new"
    cap = sample_capture(old)
    quotes, options = extract(cap, "new-000001-01", "rent")
    entry = {"href": sample_item()["href"], "data": {
        "id": "new-000001-01", "catalog_identity": sample_item()["href"],
        "captures": [cap], "quotes": quotes, "options": options,
    }}
    source = old / "checkpoints" / "details" / "new-000001-01.json"
    write_json(source, entry)
    source_bytes = source.read_bytes()
    result = replay(old, out, [sample_item()], Journal(out))
    assert result["missing"] == []
    assert source.read_bytes() == source_bytes
    replayed = read_json(out / "checkpoints" / "details" / "new-000001-01.json")["data"]
    assert replayed["captures"][0]["captured_utc"] == "2026-01-01T01:02:03+00:00"
    assert replayed["quotes"][0]["evidence"]["html"] == replayed["captures"][0]["html"]
    assert summarize([sample_item()], out)["counts"]["quotes"] == 1
    assert summarize([sample_item()], out)["complete_fixed_cohort"] is True
    # A bad original hash must fail rather than silently accepting old data.
    bad = read_json(source)
    bad["data"]["captures"][0]["html"]["sha256"] = "0" * 64
    write_json(source, bad)
    (out / "checkpoints" / "details" / "new-000001-01.json").unlink()
    with pytest.raises(ValueError, match="hash mismatch"):
        replay(old, out, [sample_item()], Journal(out))


def test_reconciliation_keeps_original_denominator(tmp_path):
    item = sample_item()
    other = {"href": "/cars/used/123456", "text": "Later stock"}
    first = summarize([item], tmp_path, final_items=[other])
    assert first["counts"]["expected"] == 1
    assert first["missing_product_ids"] == [item["href"]]
    assert first["final_catalog"]["additions"] == [other["href"]]
    assert first["final_catalog"]["removals"] == [item["href"]]


def test_command_rejects_same_replay_directory(tmp_path):
    manifest = tmp_path / "manifest.json"
    write_json(manifest, {"products": [sample_item()]})
    with pytest.raises(SystemExit):
        main(["--manifest", str(manifest), "--output", str(tmp_path), "--replay", str(tmp_path)])


def run_immediate(coroutine):
    """Drive fake async methods without a Windows socket-backed event loop."""
    with pytest.raises(StopIteration) as stopped:
        coroutine.send(None)
    return stopped.value.value


class HydratingPage:
    url = "https://www.amazoncar.co.kr/cars/new/000001/01"

    def __init__(self, states):
        self.states = list(states)
        self.index = 0

    async def evaluate(self, _script):
        state = self.states[min(self.index, len(self.states) - 1)]
        self.index += 1
        return state

    async def wait_for_timeout(self, _milliseconds):
        return None

    async def wait_for_load_state(self, state, timeout):
        assert state == "networkidle"
        assert timeout == 5_000
        self.network_idle_checked = True


def ready_state(*, modes=(), quotes=(), links=(), heading=(), rows=0):
    return {"availableModes": list(modes), "modes": list(modes),
            "quotes": list(quotes), "conditions": [], "links": list(links),
            "tableRows": rows, "heading": list(heading), "invalid": False}


def test_detail_readiness_rejects_hydration_gap_and_stale_mode_quote():
    empty = ready_state()
    rendered = ready_state(modes=["장기렌트"], quotes=["36개월 630,740원"], heading=["Sample car"])
    page = HydratingPage([empty, empty, rendered, rendered, rendered, rendered])
    state = run_immediate(wait_ready(page, mode="장기렌트", timeout_seconds=1))
    assert state["quotes"] == ["36개월 630,740원"]
    assert page.index >= 6

    stale = ready_state(modes=["리스"], quotes=["36개월 630,740원"], heading=["Sample car"])
    changed = ready_state(modes=["리스"], quotes=["36개월 620,000원"], heading=["Sample car"])
    page = HydratingPage([stale, stale, changed, changed, changed, changed])
    state = run_immediate(wait_ready(page, mode="리스", previous_quotes=stale["quotes"],
                                     timeout_seconds=1))
    assert state["quotes"] == ["36개월 620,000원"]


def test_list_and_tariff_readiness_require_actual_content():
    empty = ready_state()
    listed = ready_state(links=["/cars/new/000001/01"])
    page = HydratingPage([empty, listed, listed, listed, listed])
    assert run_immediate(wait_ready(page, kind="list", timeout_seconds=1))["links"]
    tariff = ready_state(rows=54)
    page = HydratingPage([empty, tariff, tariff, tariff, tariff])
    assert run_immediate(wait_ready(page, kind="tariff", timeout_seconds=1))["tableRows"] == 54


def test_group_enumeration_stops_cyclic_carousel_and_preserves_standalone(monkeypatch, tmp_path):
    import su_crawler.amazoncar_collection as module

    async def noop(*args, **kwargs):
        return None

    async def snapshot(*args, **kwargs):
        return {"url": "https://www.amazoncar.co.kr/cars/new/000001/01", "html": {"path": "proof.html"}}

    monkeypatch.setattr(module, "navigate", noop)
    monkeypatch.setattr(module, "capture", snapshot)

    class Control:
        def __init__(self, page, selector):
            self.page, self.selector = page, selector
        async def all_text_contents(self): return []
        async def count(self): return int(self.page.has_navigation)
        async def is_visible(self): return True
        async def is_enabled(self): return True
        async def click(self): self.page.index += 1

    class Page:
        def __init__(self, pages, has_navigation):
            self.pages, self.has_navigation, self.index = pages, has_navigation, 0
        def locator(self, selector): return Control(self, selector)
        async def evaluate(self, script): return self.pages[self.index % len(self.pages)]
        async def wait_for_function(self, *args, **kwargs): return None

    first = sample_item()
    second = {"href": "/cars/new/000002/01", "text": "Other trim"}
    page = Page([[first], [second]], True)
    result = run_immediate(discover_groups(page, first, EvidenceStore(tmp_path)))
    assert {x["href"] for x in result["trims"]} == {first["href"], second["href"]}
    assert result["groups"][0]["termination"] == "carousel_cycle"
    assert len(result["groups"][0]["pages"]) == 2
    standalone = run_immediate(discover_groups(Page([[]], False), first, EvidenceStore(tmp_path)))
    assert standalone["trims"] == [first]
    assert standalone["groups"][0]["termination"] == "standalone_detail_no_trim_controls"
    assert standalone["groups"][0]["pages"][0]["capture"]["html"]
    with pytest.raises(RuntimeError, match="no visible product links"):
        run_immediate(discover_groups(Page([[]], True), first, EvidenceStore(tmp_path)))


def test_equal_mode_prices_require_network_quiet_and_remain_reviewable():
    same = ready_state(modes=["리스"], quotes=["36개월 630,740원"], heading=["Sample car"])
    page = HydratingPage([same])
    state, status = run_immediate(wait_mode_transition(
        page, "리스", ["36개월 630,740원"], changed_timeout=0.001))
    assert state["quotes"] == ["36개월 630,740원"]
    assert status == "identical_amounts_after_network_quiet_review"
    assert page.network_idle_checked


def test_delayed_new_price_is_not_recorded_as_empty():
    empty = ready_state(modes=["리스"], heading=["Sample car"])
    priced = ready_state(modes=["리스"], quotes=["36개월 620,000원"], heading=["Sample car"])
    page = HydratingPage([empty] * 20 + [priced] * 10)
    state, status = run_immediate(wait_mode_transition(
        page, "리스", ["36개월 630,740원"], changed_timeout=0.001,
        empty_timeout=1))
    assert state["quotes"] == ["36개월 620,000원"]
    assert status == "changed_after_switch"


def test_atomic_capture_keeps_displayed_price_and_html_from_one_dom(tmp_path):
    from su_crawler.amazoncar_collection import capture, EvidenceStore

    playwright = pytest.importorskip("playwright.async_api")

    async def check():
        async with playwright.async_playwright() as driver:
            browser = await driver.chromium.launch(headless=True)
            try:
                page = await browser.new_page()
                await page.set_content("""<h1>Example car</h1>
                    <button class='active'>장기렌트</button>
                    <button>36개월 630,740원</button>""")
                result = await capture(page, EvidenceStore(tmp_path), "example-rent")
                html = (tmp_path / result["html"]["path"]).read_text(encoding="utf-8")
                assert "36개월 630,740원" in html
                assert "36개월 630,740원" in [x["text"] for x in result["buttons"]]
            finally:
                await browser.close()

    import asyncio
    asyncio.run(check())


def test_no_displayed_quote_is_kept_as_reviewable_mode(tmp_path, monkeypatch):
    from su_crawler import amazoncar_collection as collection

    playwright = pytest.importorskip("playwright.async_api")

    async def navigate_empty(page, url, **kwargs):
        await page.set_content("<h1>Example car</h1><button class='active'>장기렌트</button>")

    async def ready_empty(page, *, allow_no_quote=False, **kwargs):
        if not allow_no_quote:
            raise TimeoutError("No displayed quote")
        return await page.evaluate(collection.READY_JS)

    monkeypatch.setattr(collection, "navigate", navigate_empty)
    monkeypatch.setattr(collection, "wait_ready", ready_empty)

    async def check():
        async with playwright.async_playwright() as driver:
            browser = await driver.chromium.launch(headless=True)
            try:
                page = await browser.new_page()
                await collection.install_tracker_async(page)
                result = await collection.collect_detail(
                    page, {"href": "/cars/used/057846"}, collection.EvidenceStore(tmp_path))
                assert result["mode_coverage"]["rent"]["status"] == "no_displayed_quotes"
                assert result["quotes"] == []
                assert result["captures"][0]["settlement"]["status"] == "settled"
                assert result["captures"][0]["integrity_flags"] == ["no_displayed_quotes_review"]
            finally:
                await browser.close()

    import asyncio
    asyncio.run(check())


def test_direct_navigate_installs_network_tracker_before_navigation(monkeypatch):
    from su_crawler import amazoncar_collection as collection
    from su_crawler.browser_settlement import TRACKER_INIT_JS

    calls = []

    class Page:
        async def add_init_script(self, script):
            calls.append(("init", script))

        async def evaluate(self, script):
            calls.append(("evaluate", script))

        async def goto(self, url, **kwargs):
            calls.append(("goto", url))
            return None

    async def ready(*args, **kwargs):
        return {}

    monkeypatch.setattr(collection, "wait_ready", ready)
    page = Page()
    run_immediate(collection.navigate(page, "https://example.test/item"))
    run_immediate(collection.navigate(page, "https://example.test/item-2"))
    assert [kind for kind, _ in calls] == ["init", "evaluate", "goto", "goto"]
    assert calls[0][1] == TRACKER_INIT_JS
