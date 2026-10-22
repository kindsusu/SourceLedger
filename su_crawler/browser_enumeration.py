"""Bounded, evidence-preserving browser enumeration recipes."""
from __future__ import annotations

import hashlib
import time
from typing import Any, Callable

from .browser_settlement import network_state_sync, wait_action_effects_sync

# After an action, stop waiting once the tracked network and DOM have been
# quiet this long (never longer than the recipe's progress wait).
ACTION_QUIET_MS = 500
ACTION_FLOOR_MS = 100
# A pagination or scroll action may take this long to change the listing on
# a tracked page before it is treated as a repeated or non-growing state.
LISTING_CHANGE_WAIT_MS = 3000


class SnapshotBudgetExceeded(Exception):
    """The collection-wide snapshot count or byte limit was reached."""


class ListingChanged(Exception):
    """The observed listing no longer matches the DOM being preserved."""


def _listing_items(items: Any) -> list[dict[str, str]]:
    # One browser evaluation observes the visible rows and their attributes.
    # Text alone would miss a changed product link, ID, or price attribute.
    return items.evaluate_all("els => els.map(el => ({text: el.innerText, html: el.outerHTML}))")


def _after_action(page: Any, remaining_ms: Callable[[], float], wait_ms: int, *,
                  read: Callable[[], Any] | None = None, previous: Any = None,
                  require_change: bool = False) -> dict:
    """Condition wait after an action, bounded by the step and page budgets."""
    budget = max(1, remaining_ms() - 1000)
    limit = min(max(wait_ms, LISTING_CHANGE_WAIT_MS) if require_change else wait_ms, budget)
    return wait_action_effects_sync(page, max_ms=limit, quiet_ms=min(wait_ms, ACTION_QUIET_MS),
                                    floor_ms=min(wait_ms, ACTION_FLOOR_MS), read=read, previous=previous,
                                    require_change=require_change, untracked_ms=min(wait_ms, budget))


def _confirm_last_page(page: Any, container: Any, step: dict[str, Any], items: Any,
                       entries: list[dict[str, str]], remaining_ms: Callable[[], float],
                       window_ms: int) -> tuple[str, str | None]:
    """Confirm a missing or disabled next control is the end of the listing.

    The control must stay absent/disabled, the captured rows unchanged, and the
    tracked network free of pending fetch/XHR and loaders for the stable
    window, counted from the last network activity.
    """
    stable_since = time.monotonic()
    while True:
        if remaining_ms() <= 1200:
            return "deadline", None
        controls = container.locator(step["next_selector"] + ":visible")
        found = controls.count()
        if found > 1:
            return "ambiguous", None
        state = "absent" if found == 0 else "disabled" if controls.is_disabled() else "enabled"
        if state == "enabled":
            return "next_available", state
        if _listing_items(items) != entries:
            return "changed", state
        network = network_state_sync(page)
        now = time.monotonic()
        if network.get("tracked") is True:
            if network.get("pending") or network.get("loading"):
                stable_since = now
            elif isinstance(network.get("idleMs"), (int, float)):
                stable_since = max(stable_since, now - network["idleMs"] / 1000)
        elapsed_ms = (now - stable_since) * 1000
        if elapsed_ms >= window_ms:
            return "confirmed", state
        page.wait_for_timeout(max(1, min(100, window_ms - elapsed_ms, remaining_ms() - 1000)))


def _capture_listing(capture: Callable[..., None], selected: dict[str, str],
                     step: dict[str, Any], entries: list[dict[str, str]]) -> None:
    capture(selected, None, {"selector": step["selector"],
                             "item_selector": step["item_selector"], "entries": entries})


def validate_enumeration_step(step: dict[str, Any]) -> None:
    action = step.get("action")
    if action == "enumerate_options":
        selectors = step.get("selectors")
        if not isinstance(selectors, list) or not 1 <= len(selectors) <= 3 or any(
            not isinstance(value, str) or not value.strip() for value in selectors
        ) or len(set(selectors)) != len(selectors):
            raise ValueError("enumerate_options requires 1-3 distinct native select selectors")
        maximum = step.get("max_variants", 50)
        if isinstance(maximum, bool) or not isinstance(maximum, int) or not 1 <= maximum <= 100:
            raise ValueError("max_variants must be an integer from 1 to 100")
    elif action == "paginate":
        for key in ("selector", "item_selector", "next_selector"):
            if not isinstance(step.get(key), str) or not step[key].strip():
                raise ValueError(f"paginate requires {key}")
        maximum = step.get("max_pages", 20)
        if isinstance(maximum, bool) or not isinstance(maximum, int) or not 1 <= maximum <= 100:
            raise ValueError("max_pages must be an integer from 1 to 100")
    elif action == "scroll_list":
        for key in ("selector", "item_selector"):
            if not isinstance(step.get(key), str) or not step[key].strip():
                raise ValueError(f"scroll_list requires {key}")
        end = step.get("end_selector")
        if end is not None and (not isinstance(end, str) or not end.strip()):
            raise ValueError("end_selector must be a nonempty selector")
        for key, default, limit in (("max_scrolls", 20, 100), ("max_items", 1000, 1000)):
            value = step.get(key, default)
            if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= limit:
                raise ValueError(f"{key} must be an integer from 1 to {limit}")
    elif action == "enumerate_buttons":
        for key in ("selector", "option_selector"):
            if not isinstance(step.get(key), str) or not step[key].strip():
                raise ValueError(f"enumerate_buttons requires {key}")
        maximum = step.get("max_variants", 50)
        if isinstance(maximum, bool) or not isinstance(maximum, int) or not 1 <= maximum <= 100:
            raise ValueError("max_variants must be an integer from 1 to 100")
    else:
        raise ValueError("Unsupported enumeration action")
    wait = step.get("progress_wait_seconds", 1)
    if isinstance(wait, bool) or not isinstance(wait, (int, float)) or not 0 < wait <= 10:
        raise ValueError("progress_wait_seconds must be between 0 and 10")
    ready = step.get("ready_selector")
    if ready is not None and (not isinstance(ready, str) or not ready.strip()):
        raise ValueError("ready_selector must be a nonempty selector")
    if "price_selector" in step and (action not in {"enumerate_options", "enumerate_buttons"} or not isinstance(
            step["price_selector"], str) or not step["price_selector"].strip() or len(step["price_selector"]) > 1000):
        raise ValueError("price_selector must be a nonempty selector on enumerate_options or enumerate_buttons")


def _price_reader(page: Any, selector: str) -> Callable[[], str]:
    # Hidden copies (e.g. templates for other options) are not the quoted price.
    locator = page.locator(selector)
    return lambda: "\n".join(" ".join(text.split()) for text in locator.evaluate_all(
        """els => els.filter(el => el.getClientRects().length && getComputedStyle(el).visibility !== 'hidden')
                  .map(el => el.innerText || '')""") if text.strip())


def _settle_price(page: Any, remaining_ms: Callable[[], float], wait_ms: int,
                  read: Callable[[], str], previous: str) -> tuple[str, str]:
    """Classify whether the displayed price refreshed after one selection.

    A change is accepted only once the tracked network and the price text are
    quiet. An unchanged price is accepted as identical only after a second
    quiet window; it may still be a stale value, so callers flag it for review.
    """
    first = _after_action(page, remaining_ms, wait_ms, read=read, previous=previous, require_change=True)
    value = read()
    if first["status"] == "settled" and value and value != previous:
        return "changed", value
    second = first
    if first["status"] != "untracked" and remaining_ms() > 1200:
        # A full quiet window must fit inside this wait even for short steps.
        second = _after_action(page, remaining_ms, max(wait_ms, 2 * ACTION_QUIET_MS), read=read)
        value = read()
    if not value:
        return "no_price_displayed", value
    if second["status"] == "untracked":
        return "untracked", value
    if second["status"] == "settled":
        # A late but quiet refresh is still a confirmed change.
        return ("identical_after_quiet" if value == previous else "changed"), value
    return "timeout", value


def _price_evidence(status: str, selector: str, value: str) -> dict[str, str]:
    evidence = {"status": status, "selector": selector}
    if value:
        evidence["text_sha256"] = hashlib.sha256(value.encode("utf-8")).hexdigest()
    return evidence


def _options(select: Any) -> list[dict[str, str]]:
    return select.evaluate("""el => Array.from(el.options).map(o => ({
      value:o.value, label:(o.textContent||'').trim(),
      skip: o.disabled || o.parentElement?.disabled ? 'disabled' :
        o.hidden || getComputedStyle(o).display === 'none' ? 'hidden' :
        o.value === '' ? 'placeholder' : ''
    }))""")


def enumerate_options(
    page: Any, step: dict[str, Any], remaining_ms: Callable[[], float],
    capture: Callable[..., None], trace: list[dict[str, Any]],
    unavailable_markers: list[str] | None = None,
) -> None:
    """Visit only currently enabled native-select combinations, including dependent selects."""
    validate_enumeration_step(step)
    selectors = step["selectors"]
    max_variants = step.get("max_variants", 50)
    wait_ms = int(step.get("progress_wait_seconds", 1) * 1000)
    markers = [value.casefold() for value in (unavailable_markers or []) if value]
    captured = 0
    unavailable = 0
    unavailable_labels: list[dict[str, str]] = []
    skipped_options: list[dict[str, str]] = []
    status, reason = "unknown", "no_options"
    start_url = page.url
    seen: set[tuple[tuple[str, str], ...]] = set()
    price_selector = step.get("price_selector")
    read_price = _price_reader(page, price_selector) if price_selector else None
    last_price = ""
    price_counts: dict[str, int] = {}

    def descend(depth: int, selections: dict[str, str], values: dict[str, str],
                settlement: dict[str, str] | None = None) -> None:
        nonlocal captured, unavailable, status, reason, last_price
        if remaining_ms() <= 1200:
            status, reason = ("partial" if captured else "unknown"), "deadline"
            return
        if page.url != start_url:
            status, reason = ("partial" if captured else "unknown"), "unexpected_navigation"
            return
        if depth == len(selectors):
            key = tuple(values.items())
            if key not in seen:
                seen.add(key)
                if settlement is None:
                    capture(dict(selections), dict(values))
                else:
                    capture(dict(selections), dict(values), price_settlement=dict(settlement))
                    price_counts[settlement["status"]] = price_counts.get(settlement["status"], 0) + 1
                captured += 1
            return
        selector = selectors[depth]
        loc = page.locator(selector)
        if loc.count() != 1 or loc.evaluate("el => el.tagName") != "SELECT":
            status, reason = ("partial" if captured else "unknown"), "ambiguous_or_non_native_select"
            return
        choices = _options(loc)
        if not any(not choice["skip"] for choice in choices):
            skipped_options.extend({"selector": selector, "label": choice["label"], "reason": choice["skip"]}
                                   for choice in choices)
            status, reason = ("partial" if captured else "unknown"), "no_enabled_options"
            return
        for choice in choices:
            label = choice["label"]
            if choice["skip"]:
                skipped_options.append({"selector": selector, "label": label, "reason": choice["skip"]})
                continue
            if any(marker in label.casefold() for marker in markers):
                unavailable += 1
                unavailable_labels.append({"selector": selector, "label": label})
                continue
            if captured >= max_variants:
                status, reason = "partial", "variant_budget"
                return
            if remaining_ms() <= 1200:
                status, reason = ("partial" if captured else "unknown"), "deadline"
                return
            loc.select_option(choice["value"], timeout=remaining_ms())
            observed = None
            if read_price is None:
                _after_action(page, remaining_ms, wait_ms)
            else:
                # Parent selections update the baseline too, so a leaf is
                # compared with the price shown immediately before it.
                outcome, last_price = _settle_price(page, remaining_ms, wait_ms, read_price, last_price)
                observed = _price_evidence(outcome, price_selector, last_price)
            if step.get("ready_selector"):
                page.locator(step["ready_selector"]).wait_for(state="visible", timeout=remaining_ms())
            if loc.input_value(timeout=remaining_ms()) != choice["value"]:
                status, reason = ("partial" if captured else "unknown"), "selection_reset"
                return
            if any(page.locator(prior).input_value(timeout=remaining_ms()) != value for prior, value in values.items()):
                status, reason = ("partial" if captured else "unknown"), "dependent_selection_reset"
                return
            # Dependent choices are read only after the preceding selection settles.
            descend(depth + 1, {**selections, selector: label}, {**values, selector: choice["value"]}, observed)
            if status in {"partial", "unknown"} and reason != "no_options":
                return

    try:
        if read_price is not None:
            last_price = read_price()
        descend(0, {}, {})
        if reason == "no_options" and captured:
            status, reason = "complete", "options_exhausted"
    except SnapshotBudgetExceeded:
        status, reason = ("partial" if captured else "unknown"), "snapshot_budget"
    except Exception as exc:
        status, reason = ("partial" if captured else "unknown"), (
            "action_timeout" if type(exc).__name__.endswith("TimeoutError") else "browser_error"
        )
    event = {"event": "option_enumeration", "status": status, "reason": reason,
             "captured_count": captured, "unavailable_count": unavailable,
             "unavailable_labels": unavailable_labels[:100],
             "skipped_options": skipped_options[:100]}
    if price_selector:
        event["price_settlement_counts"] = price_counts
    trace.append(event)


def paginate(
    page: Any, step: dict[str, Any], remaining_ms: Callable[[], float],
    capture: Callable[..., None], trace: list[dict[str, Any]],
) -> None:
    """Capture each distinct visible page of a scoped listing."""
    validate_enumeration_step(step)
    container = page.locator(step["selector"])
    count = 0
    status, reason = "unknown", "missing_items"
    seen: set[str] = set()
    wait_ms = int(step.get("progress_wait_seconds", 1) * 1000)
    try:
        if container.count() != 1:
            reason = "ambiguous_container"
        else:
            for index in range(step.get("max_pages", 20)):
                if remaining_ms() <= 1200:
                    status, reason = ("partial" if count else "unknown"), "deadline"
                    break
                items = container.locator(step["item_selector"] + ":visible")
                entries = _listing_items(items)
                if not entries:
                    status, reason = ("partial" if count else "unknown"), "missing_items"
                    break
                signature = hashlib.sha256(repr(entries).encode("utf-8")).hexdigest()
                if signature in seen:
                    status, reason = "partial", "repeated_page"
                    break
                seen.add(signature)
                try:
                    _capture_listing(capture, {"page": str(index + 1)}, step, entries)
                except ListingChanged:
                    status, reason = ("partial" if count else "unknown"), "list_changed_during_capture"
                    break
                count += 1
                next_control = container.locator(step["next_selector"] + ":visible")
                found = next_control.count()
                if found > 1:
                    status, reason = "partial", "ambiguous_next"
                    break
                if found == 0 or next_control.is_disabled():
                    # A missing or disabled control may be transient while the
                    # page loads; completion needs a positive stable window.
                    outcome, state = _confirm_last_page(page, container, step, items, entries,
                                                        remaining_ms, wait_ms)
                    if outcome == "confirmed":
                        status, reason = "complete", "next_absent" if state == "absent" else "next_disabled"
                        break
                    if outcome != "next_available":
                        status, reason = "partial", {"changed": "list_changed_after_capture",
                                                     "ambiguous": "ambiguous_next"}.get(outcome, "completion_unconfirmed")
                        break
                if index + 1 >= step.get("max_pages", 20):
                    status, reason = "partial", "page_budget"
                    break
                old_url = page.url
                next_control.click(timeout=remaining_ms())
                _after_action(page, remaining_ms, wait_ms, read=lambda: _listing_items(items),
                              previous=entries, require_change=True)
                # Caller rechecks navigated URL against source policy before
                # accepting evidence; the route guard checks each request.
                if page.url != old_url and not page.url.startswith("http"):
                    status, reason = "partial", "unexpected_navigation"
                    break
            else:
                status, reason = "partial", "page_budget"
    except SnapshotBudgetExceeded:
        status, reason = ("partial" if count else "unknown"), "snapshot_budget"
    except Exception as exc:
        status, reason = ("partial" if count else "unknown"), (
            "action_timeout" if type(exc).__name__.endswith("TimeoutError") else "browser_error"
        )
    trace.append({"event": "pagination", "status": status, "reason": reason, "captured_count": count})


def scroll_list(
    page: Any, step: dict[str, Any], remaining_ms: Callable[[], float],
    capture: Callable[..., None], trace: list[dict[str, Any]],
) -> None:
    """Scroll an append-only list; stable growth alone never proves completion."""
    validate_enumeration_step(step)
    container = page.locator(step["selector"])
    count = 0
    status, reason = "unknown", "missing_items"
    previous: list[dict[str, str]] = []
    captured = 0
    start_url = page.url
    wait_ms = int(step.get("progress_wait_seconds", 1) * 1000)
    try:
        if container.count() != 1:
            reason = "ambiguous_container"
        else:
            for index in range(step.get("max_scrolls", 20) + 1):
                if remaining_ms() <= 1200:
                    status, reason = ("partial" if count else "unknown"), "deadline"
                    break
                if page.url != start_url:
                    status, reason = ("partial" if count else "unknown"), "unexpected_navigation"
                    break
                cards = container.locator(step["item_selector"] + ":visible")
                current = _listing_items(cards)
                if len(current) > step.get("max_items", 1000):
                    status, reason = "partial", "item_budget"
                    break
                if not current:
                    status, reason = ("partial" if count else "unknown"), "missing_items"
                    break
                if previous and (len(current) < len(previous) or current[:len(previous)] != previous):
                    status, reason = "partial", "list_replaced"
                    break
                # Save each verified append-only state before scrolling. A site
                # may replace the list on the next interaction; in that case
                # the final DOM is not evidence of previously observed cards.
                try:
                    _capture_listing(capture, {"scroll_items": str(len(current))}, step, current)
                except ListingChanged:
                    status, reason = ("partial" if captured else "unknown"), "list_changed_during_capture"
                    break
                count = len(current)
                previous = current
                captured += 1
                end_selector = step.get("end_selector")
                if end_selector and container.locator(end_selector + ":visible").count() == 1:
                    status, reason = (("complete", "explicit_end") if _listing_items(cards) == current
                                      else ("partial", "list_changed_after_capture"))
                    break
                if index == step.get("max_scrolls", 20):
                    status, reason = "partial", "scroll_budget"
                    break
                cards.last.scroll_into_view_if_needed(timeout=remaining_ms())
                _after_action(page, remaining_ms, wait_ms, read=lambda: _listing_items(cards),
                              previous=previous, require_change=True)
                if cards.count() == count and _listing_items(cards) == previous:
                    status, reason = "partial", "no_growth_unconfirmed"
                    break
    except SnapshotBudgetExceeded:
        status, reason = ("partial" if count else "unknown"), "snapshot_budget"
    except Exception as exc:
        status, reason = ("partial" if count else "unknown"), (
            "action_timeout" if type(exc).__name__.endswith("TimeoutError") else "browser_error"
        )
    trace.append({"event": "scroll_list", "status": status, "reason": reason,
                  "observed_count": count, "captured_count": captured})


def enumerate_buttons(
    page: Any, step: dict[str, Any], remaining_ms: Callable[[], float],
    capture: Callable[..., None], trace: list[dict[str, Any]],
    unavailable_markers: list[str] | None = None,
) -> None:
    """Select one reviewed group of non-submit buttons carrying data-value."""
    validate_enumeration_step(step)
    container = page.locator(step["selector"])
    count = 0
    status, reason = "unknown", "missing_options"
    skipped: list[dict[str, str]] = []
    markers = [value.casefold() for value in (unavailable_markers or []) if value]
    start_url = page.url
    price_selector = step.get("price_selector")
    read_price = _price_reader(page, price_selector) if price_selector else None
    price_counts: dict[str, int] = {}
    wait_ms = int(step.get("progress_wait_seconds", 1) * 1000)
    try:
        if container.count() != 1:
            reason = "ambiguous_container"
        else:
            last_price = read_price() if read_price is not None else ""
            choices = container.locator(step["option_selector"]).evaluate_all("""els => els.map(el => ({
              value:el.getAttribute('data-value') || '', label:(el.innerText || '').trim(),
              allowed:el.tagName === 'BUTTON' && el.type === 'button' && !el.disabled &&
                getComputedStyle(el).display !== 'none' && getComputedStyle(el).visibility !== 'hidden'
            }))""")
            for choice_index, choice in enumerate(choices):
                if not choice["allowed"] or not choice["value"]:
                    skipped.append({"label": choice["label"], "reason": "disabled_or_unsafe"})
                    continue
                if any(marker in choice["label"].casefold() for marker in markers):
                    skipped.append({"label": choice["label"], "reason": "unavailable"})
                    continue
                if count >= step.get("max_variants", 50):
                    status, reason = "partial", "variant_budget"
                    break
                if remaining_ms() <= 1200:
                    status, reason = ("partial" if count else "unknown"), "deadline"
                    break
                matches = container.locator(step["option_selector"])
                if matches.count() != len(choices) or matches.nth(choice_index).get_attribute("data-value") != choice["value"]:
                    status, reason = ("partial" if count else "unknown"), "ambiguous_option"
                    break
                matches.nth(choice_index).click(timeout=remaining_ms())
                observed = None
                if read_price is None:
                    _after_action(page, remaining_ms, wait_ms)
                else:
                    outcome, last_price = _settle_price(page, remaining_ms, wait_ms, read_price, last_price)
                    observed = _price_evidence(outcome, price_selector, last_price)
                if step.get("ready_selector"):
                    page.locator(step["ready_selector"]).wait_for(state="visible", timeout=remaining_ms())
                if matches.nth(choice_index).get_attribute("data-value") != choice["value"] or not matches.nth(choice_index).is_visible():
                    status, reason = ("partial" if count else "unknown"), "option_replaced"
                    break
                if page.url != start_url:
                    status, reason = ("partial" if count else "unknown"), "unexpected_navigation"
                    break
                if observed is None:
                    capture({step["selector"]: choice["label"]}, {step["selector"]: choice["value"]})
                else:
                    capture({step["selector"]: choice["label"]}, {step["selector"]: choice["value"]},
                            price_settlement=observed)
                    price_counts[observed["status"]] = price_counts.get(observed["status"], 0) + 1
                count += 1
            else:
                if count:
                    status, reason = "complete", "options_exhausted"
    except SnapshotBudgetExceeded:
        status, reason = ("partial" if count else "unknown"), "snapshot_budget"
    except Exception as exc:
        status, reason = ("partial" if count else "unknown"), (
            "action_timeout" if type(exc).__name__.endswith("TimeoutError") else "browser_error"
        )
    event = {"event": "button_enumeration", "status": status, "reason": reason,
             "captured_count": count, "skipped_options": skipped[:100]}
    if price_selector:
        event["price_settlement_counts"] = price_counts
    trace.append(event)
