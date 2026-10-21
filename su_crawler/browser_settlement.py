"""Bounded browser settling for dynamic, publicly visible observations.

The tracker is installed before navigation and observes fetch/XHR initiated by
the page. A quiet network alone is insufficient: callers provide a narrow DOM
probe and a readiness predicate for the particular observation. The result is
an observation confidence signal, never proof that a site's future price is
immutable. A deadline is always enforced.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
import json
import time
from typing import Any
from weakref import WeakSet


TRACKER_INIT_JS = r"""(() => {
  if (window.__sourceLedgerNetwork) return;
  const state = {pending: 0, lastActivity: performance.now(), version: 0};
  const changed = () => { state.lastActivity = performance.now(); state.version++; };
  window.__sourceLedgerNetwork = state;
  if (typeof window.fetch === 'function') {
    const originalFetch = window.fetch;
    window.fetch = function (...args) {
      // keepalive fetches are fire-and-forget beacons (ads, analytics). Under
      // Playwright request interception their promise can stay pending forever,
      // which would block every observation on pages that load such tags.
      const request = args[0];
      if ((args[1] && args[1].keepalive) || (typeof Request !== 'undefined'
          && request instanceof Request && request.keepalive)) {
        return originalFetch.apply(this, args);
      }
      state.pending++; changed();
      let finished = false;
      const finish = () => {
        if (finished) return;
        finished = true;
        state.pending = Math.max(0, state.pending - 1); changed();
      };
      try {
        return Promise.resolve(originalFetch.apply(this, args)).then(response => {
          // The fetch promise resolves on response headers. Price JSON may
          // still be streaming. Drain a clone one chunk at a time without
          // materializing the whole body; the caller keeps its own stream.
          // Long-lived streams remain pending until the bounded caller wait
          // times out; they are never silently treated as a settled price.
          if (response.body) {
            try {
              const reader = response.clone().body.getReader();
              (async () => {
                try {
                  while (!(await reader.read()).done) { /* discard each chunk */ }
                } catch (_) {
                  // A failed body is no longer pending; caller handles failure.
                } finally {
                  reader.releaseLock();
                  finish();
                }
              })();
            } catch (_) {
              finish();
            }
          } else {
            finish();
          }
          return response;
        }, error => {
          finish();
          throw error;
        });
      } catch (error) {
        finish();
        throw error;
      }
    };
  }
  if (typeof XMLHttpRequest !== 'undefined') {
    const originalSend = XMLHttpRequest.prototype.send;
    XMLHttpRequest.prototype.send = function (...args) {
      state.pending++; changed();
      this.addEventListener('loadend', () => {
        state.pending = Math.max(0, state.pending - 1); changed();
      }, {once: true});
      try { return originalSend.apply(this, args); }
      catch (error) {
        state.pending = Math.max(0, state.pending - 1); changed();
        throw error;
      }
    };
  }
})();"""

_async_installed_pages: WeakSet[Any] = WeakSet()

SETTLEMENT_STATE_JS = r"""() => {
 const state = window.__sourceLedgerNetwork;
 const visible = element => {
   if (!element.getClientRects().length) return false;
   const style = getComputedStyle(element);
   return style.visibility !== 'hidden' && style.display !== 'none' && style.opacity !== '0';
 };
 const loaders = [...document.querySelectorAll(
   '[aria-busy="true"],[role="progressbar"],[data-loading="true"],.loading,.spinner'
 )].filter(visible).length;
 return {tracked: !!state, pending: state?.pending ?? null,
   idleMs: state ? performance.now() - state.lastActivity : null,
   version: state?.version ?? null, loading: loaders};
}"""

MARK_ACTION_JS = r"""() => {
 const state = window.__sourceLedgerNetwork;
 if (state) {state.lastActivity = performance.now(); state.version++;}
}"""


def signature_of(value: Any) -> str:
    """Canonical signature; probes should omit clocks and unrelated animations."""
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _settled(probe: Any, network: dict, *, ready: Callable[[Any], bool],
             quiet_ms: int, signature: str, previous: str | None,
             require_change: bool) -> bool:
    return (bool(ready(probe)) and network.get("tracked") is True
            and network.get("pending") == 0 and network.get("loading") == 0
            and isinstance(network.get("idleMs"), (int, float))
            and network["idleMs"] >= quiet_ms
            and (not require_change or signature != previous))


def wait_settled_sync(page: Any, read: Callable[[], Any], *,
                      ready: Callable[[Any], bool], signature: Callable[[Any], str] = signature_of,
                      timeout_seconds: float = 15, quiet_ms: int = 750,
                      stable_ms: int = 600, poll_ms: int = 150,
                      previous_signature: str | None = None,
                      require_change: bool = False) -> dict:
    """Return a bounded result. Callers must handle ``status=timeout`` as review."""
    deadline = time.monotonic() + timeout_seconds
    stable_since = None
    last_signature = None
    last_version = None
    last = None
    while True:
        probe = read()
        network = page.evaluate(SETTLEMENT_STATE_JS)
        current = signature(probe)
        eligible = _settled(probe, network, ready=ready, quiet_ms=quiet_ms,
                            signature=current, previous=previous_signature,
                            require_change=require_change)
        now = time.monotonic()
        stable_since = (stable_since if stable_since is not None and eligible
                        and current == last_signature and network.get("version") == last_version
                        else now if eligible else None)
        last_signature = current
        last_version = network.get("version")
        last = {"status": "settled" if stable_since is not None and
                (now - stable_since) * 1000 >= stable_ms else "waiting",
                "probe": probe, "network": network, "signature": current,
                "elapsed_seconds": max(0, timeout_seconds - max(0, deadline - now))}
        if last["status"] == "settled":
            return last
        if now >= deadline:
            last["status"] = "timeout"
            return last
        page.wait_for_timeout(min(poll_ms, max(1, int((deadline - now) * 1000))))


def network_state_sync(page: Any) -> dict:
    """Return the tracker state, or an empty dict when it cannot be read."""
    try:
        state = page.evaluate(SETTLEMENT_STATE_JS)
    except Exception:
        return {}
    return state if isinstance(state, dict) else {}


def wait_action_effects_sync(page: Any, *, max_ms: float, quiet_ms: float,
                             floor_ms: float = 100, read: Callable[[], Any] | None = None,
                             previous: Any = None, require_change: bool = False,
                             poll_ms: int = 100, untracked_ms: float | None = None) -> dict:
    """Wait for the effects of one page action instead of a fixed sleep.

    The action is marked on the tracker first, so idle time counts from the
    action. The wait ends once no fetch/XHR or visible loader has been active
    for ``quiet_ms``, the optional ``read()`` value has been unchanged for
    ``quiet_ms`` (and differs from ``previous`` when ``require_change``), and
    ``floor_ms`` has elapsed. It never exceeds ``max_ms``. A page without the
    tracker keeps the previous fixed wait (``untracked_ms``, default
    ``max_ms``): absence of network evidence is not evidence of quiet.
    """
    started = time.monotonic()
    deadline = started + max(0.0, max_ms) / 1000
    try:
        page.evaluate(MARK_ACTION_JS)
    except Exception:
        pass
    last_value: Any = object()
    value_since = started
    while True:
        network = network_state_sync(page)
        if network.get("tracked") is not True:
            fixed = max_ms if untracked_ms is None else min(max_ms, untracked_ms)
            page.wait_for_timeout(max(1, int(fixed - (time.monotonic() - started) * 1000)))
            return {"status": "untracked", "elapsed_seconds": time.monotonic() - started}
        try:
            value = read() if read is not None else None
        except Exception:
            value = object()  # e.g. an execution context replaced by navigation
        now = time.monotonic()
        if value != last_value:
            last_value, value_since = value, now
        if ((now - started) * 1000 >= floor_ms
                and (not require_change or value != previous)
                and network.get("pending") == 0 and network.get("loading") == 0
                and isinstance(network.get("idleMs"), (int, float)) and network["idleMs"] >= quiet_ms
                and (now - value_since) * 1000 >= quiet_ms):
            return {"status": "settled", "elapsed_seconds": now - started}
        if now >= deadline:
            return {"status": "timeout", "elapsed_seconds": now - started}
        page.wait_for_timeout(max(1, min(poll_ms, int((deadline - now) * 1000))))


async def wait_settled_async(page: Any, read: Callable[[], Awaitable[Any]], *,
                             ready: Callable[[Any], bool], signature: Callable[[Any], str] = signature_of,
                             timeout_seconds: float = 15, quiet_ms: int = 750,
                             stable_ms: int = 600, poll_ms: int = 150,
                             previous_signature: str | None = None,
                             require_change: bool = False) -> dict:
    deadline = time.monotonic() + timeout_seconds
    stable_since = None
    last_signature = None
    last_version = None
    while True:
        probe = await read()
        network = await page.evaluate(SETTLEMENT_STATE_JS)
        current = signature(probe)
        eligible = _settled(probe, network, ready=ready, quiet_ms=quiet_ms,
                            signature=current, previous=previous_signature,
                            require_change=require_change)
        now = time.monotonic()
        stable_since = (stable_since if stable_since is not None and eligible
                        and current == last_signature and network.get("version") == last_version
                        else now if eligible else None)
        last_signature = current
        last_version = network.get("version")
        result = {"status": "settled" if stable_since is not None and
                  (now - stable_since) * 1000 >= stable_ms else "waiting",
                  "probe": probe, "network": network, "signature": current,
                  "elapsed_seconds": max(0, timeout_seconds - max(0, deadline - now))}
        if result["status"] == "settled":
            return result
        if now >= deadline:
            result["status"] = "timeout"
            return result
        await page.wait_for_timeout(min(poll_ms, max(1, int((deadline - now) * 1000))))


def install_tracker_sync(page: Any) -> None:
    page.add_init_script(TRACKER_INIT_JS)
    page.evaluate(TRACKER_INIT_JS)


async def install_tracker_async(page: Any) -> None:
    if page in _async_installed_pages:
        return
    await page.add_init_script(TRACKER_INIT_JS)
    await page.evaluate(TRACKER_INIT_JS)
    _async_installed_pages.add(page)
