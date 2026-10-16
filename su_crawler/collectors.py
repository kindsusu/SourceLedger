"""Policy-aware source collectors.

Collection is deliberately separate from parsing.  These functions return the
source bytes and observations about the fetch; they never invent fields.
"""
from __future__ import annotations

import asyncio
from contextlib import ExitStack, contextmanager
import hashlib
import importlib.util
import ipaddress
import mimetypes
import socket
import re
import time
import threading
from contextvars import ContextVar
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlsplit
from urllib.robotparser import RobotFileParser

import httpx

from .models import FetchResult, Source, resolve_path, utc_now
from .browser_runtime import browser_headless, select_browser_runtime
from .browser_enumeration import ListingChanged, SnapshotBudgetExceeded, enumerate_buttons, enumerate_options, paginate, scroll_list, validate_enumeration_step
from .access_challenges import detect_challenge


USER_AGENT = "source-ledger/0.3 (+authorized price research)"
MAX_RESPONSE_BYTES = 20 * 1024 * 1024
MAX_VARIANT_SNAPSHOTS = 200
MAX_VARIANT_BYTES = 32 * 1024 * 1024
MAX_REDIRECTS = 10
WELL_KNOWN_NAT64 = ipaddress.ip_network("64:ff9b::/96")
_PASSWORD_FIELD = re.compile(r"<input\b[^>]*(?:type\s*=\s*['\"]?password|name\s*=\s*['\"]password['\"]?)", re.I)


class PolicyError(ValueError):
    """A policy denial with a stable, safe-to-record reason code."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class RobotsUnavailable(Exception):
    """The site's robots policy could not be checked safely."""


class RobotsPolicyCache:
    """A bounded cache owned by one collection job, shareable across workers."""

    def __init__(self, ttl_seconds: float = 300.0) -> None:
        self.ttl_seconds = ttl_seconds
        self._entries: dict[tuple[Any, ...], tuple[float, RobotFileParser | None, str]] = {}
        self._lock = threading.Lock()

    def get(self, key: tuple[Any, ...]) -> tuple[RobotFileParser | None, str] | None:
        with self._lock:
            entry = self._entries.get(key)
            if entry is None:
                return None
            if entry[0] <= time.monotonic():
                del self._entries[key]
                return None
            return entry[1], entry[2]

    def put(self, key: tuple[Any, ...], parser: RobotFileParser | None, reason: str) -> None:
        with self._lock:
            if len(self._entries) >= 256:
                oldest = min(self._entries, key=lambda item: self._entries[item][0])
                del self._entries[oldest]
            self._entries[key] = (time.monotonic() + self.ttl_seconds, parser, reason)


_robots_cache: ContextVar[RobotsPolicyCache | None] = ContextVar("robots_policy_cache", default=None)


@contextmanager
def robots_policy_scope(cache: RobotsPolicyCache | None = None) -> Any:
    """Reuse robots rules within this job only; pass cache to parallel workers."""
    shared = cache or RobotsPolicyCache()
    token = _robots_cache.set(shared)
    try:
        yield shared
    finally:
        _robots_cache.reset(token)


def _policy_error(code: str, message: str) -> PolicyError:
    return PolicyError(code, message)


def _policy_trace(error: PolicyError) -> list[dict[str, Any]]:
    return [{"event": "policy_denied", "code": error.code, "at": utc_now()}]


def _policy_result(source: Source, backend: str, error: PolicyError, *, trace: list[dict[str, Any]] | None = None,
                   variant_snapshots: list[dict[str, Any]] | None = None) -> FetchResult:
    entries = list(trace or [])
    entries.extend(_policy_trace(error))
    return _result(source, backend, "policy_denied", message=f"Request denied by policy ({error.code})",
                   trace=entries, variant_snapshots=variant_snapshots or [])


def _host_allowed(host: str, allowed_domains: list[str]) -> bool:
    if not allowed_domains:
        return False
    normalized = host.rstrip(".").lower()
    for item in allowed_domains:
        allowed = item.split(":", 1)[0].rstrip(".").lower()
        if normalized == allowed or normalized.endswith("." + allowed):
            return True
    return False


def _resolved_addresses(host: str, port: int) -> set[ipaddress.IPv4Address | ipaddress.IPv6Address]:
    try:
        return {
            ipaddress.ip_address(record[4][0].split("%", 1)[0])
            for record in socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
        }
    except (socket.gaierror, ValueError) as exc:
        raise _policy_error("dns_resolution_failed", "Unable to resolve host address") from exc


def _embedded_ipv4(address: ipaddress.IPv4Address | ipaddress.IPv6Address) -> ipaddress.IPv4Address | None:
    """Return IPv4 carried by standardized mapped or well-known NAT64 IPv6."""
    if not isinstance(address, ipaddress.IPv6Address):
        return None
    if address.ipv4_mapped is not None:
        return address.ipv4_mapped
    if address in WELL_KNOWN_NAT64:
        return ipaddress.IPv4Address(int(address) & 0xFFFFFFFF)
    return None


def _is_public_ipv4(address: ipaddress.IPv4Address) -> bool:
    return address.is_global and not (
        address.is_unspecified or address.is_multicast or address.is_link_local or
        address.is_loopback or address.is_private or address.is_reserved
    )


def _is_public(address: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    embedded = _embedded_ipv4(address)
    if embedded is not None:
        return _is_public_ipv4(embedded)
    return _is_public_ipv4(address) if isinstance(address, ipaddress.IPv4Address) else address.is_global


def _is_forbidden_even_internal(address: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """Reject non-routable special-use ranges that are never source endpoints."""
    embedded = _embedded_ipv4(address)
    if embedded is not None:
        # An IPv6 wrapper must not turn private or special IPv4 into an allowed
        # endpoint. Only globally routable embedded IPv4 is meaningful here.
        return not _is_public_ipv4(embedded)
    return address.is_unspecified or address.is_multicast or address.is_link_local or (address.is_reserved and not address.is_loopback)


def _check_url(source: Source, url: str) -> None:
    parsed = urlsplit(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise _policy_error("invalid_url", "Only HTTP(S) URLs can be collected")
    if parsed.username or parsed.password:
        raise _policy_error("credentials_in_url", "URLs must not contain credentials")
    if not _host_allowed(parsed.hostname, source.allowed_domains):
        raise _policy_error("domain_not_allowed", "Domain is not allowed")
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    addresses = _resolved_addresses(parsed.hostname, port)
    if not addresses or any(_is_forbidden_even_internal(address) for address in addresses):
        raise _policy_error("special_address_denied", "Special-use and link-local addresses are not allowed")
    if not source.internal and any(not _is_public(a) for a in addresses):
        raise _policy_error("private_address_denied", "Public sources cannot access private or local addresses")


def _safe_url(url: str) -> str:
    """Remove credentials, query values, and fragments from observable traces."""
    parsed = urlsplit(url)
    host = parsed.hostname or ""
    if parsed.port:
        host = f"{host}:{parsed.port}"
    return f"{parsed.scheme}://{host}{parsed.path}"


def _safe_selector(selector: str) -> str:
    # Selectors may embed tokens or entered values in attribute selectors.
    scrubbed = re.sub(r"(\[[^]=~|^$*]+(?:[*^$|~]?=))\s*(['\"])[^'\"]*\2", r"\1\2<redacted>\2", selector)
    return scrubbed[:300]


def _is_auth_page(content: bytes, encoding: str = "utf-8") -> bool:
    sample = content[:500_000].decode(encoding, errors="replace")
    return bool(_PASSWORD_FIELD.search(sample))


def _media_type(path_or_url: str, fallback: str = "application/octet-stream") -> str:
    extension = Path(urlsplit(path_or_url).path).suffix.lower()
    stable_types = {
        ".csv": "text/csv",
        ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        ".xls": "application/vnd.ms-excel",
        ".pdf": "application/pdf",
        ".html": "text/html",
        ".htm": "text/html",
        ".json": "application/json",
    }
    if extension in stable_types:
        return stable_types[extension]
    guessed, _ = mimetypes.guess_type(urlsplit(path_or_url).path)
    return guessed or fallback


def _read_limited(response: httpx.Response) -> bytes:
    declared = response.headers.get("content-length")
    if declared:
        try:
            if int(declared) > MAX_RESPONSE_BYTES:
                raise ValueError("Response exceeds the size limit")
        except ValueError as exc:
            if str(exc).startswith("Response"):
                raise
    chunks: list[bytes] = []
    size = 0
    for chunk in response.iter_bytes():
        size += len(chunk)
        if size > MAX_RESPONSE_BYTES:
            raise ValueError("Response exceeds the size limit")
        chunks.append(chunk)
    return b"".join(chunks)


def _result(source: Source, backend: str, status: str, **kwargs: Any) -> FetchResult:
    # The field is supplied by the cache integration when available. Keeping this
    # guard permits collectors to remain usable during rolling upgrades.
    if "http_metadata" in kwargs and "http_metadata" not in FetchResult.__dataclass_fields__:
        kwargs.pop("http_metadata")
    return FetchResult(source_id=source.id, backend=backend, status=status, **kwargs)


def _challenge_trace(challenge: dict[str, str]) -> dict[str, str]:
    return {"event": "access_challenge", **challenge, "at": utc_now()}


class _AccessChallengeFound(Exception):
    def __init__(self, challenge: dict[str, str]) -> None:
        self.challenge = challenge


def _file_collect(source: Source, base_dir: str) -> FetchResult:
    configured_root = getattr(source, "file_root", None)
    base = resolve_path(base_dir, configured_root) if configured_root else Path(base_dir).resolve()
    path = resolve_path(str(base), source.location)
    try:
        path.relative_to(base)
    except ValueError:
        return _result(source, "file", "policy_denied", message="File is outside the configured base directory")
    if not path.is_file():
        return _result(source, "file", "failed", message="File was not found")
    if path.stat().st_size > MAX_RESPONSE_BYTES:
        return _result(source, "file", "failed", message="File exceeds the size limit")
    try:
        return _result(
            source,
            "file",
            "fetched",
            content=path.read_bytes(),
            media_type=_media_type(str(path)),
            final_url=path.as_uri(),
        )
    except OSError as exc:
        return _result(source, "file", "failed", message=f"Unable to read file: {exc}")


def _get_following_policy(
    client: httpx.Client, source: Source, url: str, *, headers: dict[str, str] | None = None,
    enforce_robots: bool = False,
) -> tuple[httpx.Response, list[dict[str, Any]]]:
    trace: list[dict[str, Any]] = []
    current = url
    for _ in range(MAX_REDIRECTS + 1):
        _check_url(source, current)
        if enforce_robots:
            _require_robots_access(client, source, current)
        # Validators can identify a prior response, so never forward them to a
        # redirect target (including another allowed domain).
        request_headers = headers if current == url else None
        response = client.send(client.build_request("GET", current, headers=request_headers), stream=True)
        trace.append({"event": "http", "url": _safe_url(str(response.url)), "status": response.status_code, "at": utc_now()})
        if response.status_code not in {301, 302, 303, 307, 308}:
            return response, trace
        location = response.headers.get("location")
        if not location:
            return response, trace
        response.close()
        current = urljoin(str(response.url), location)
    raise httpx.TooManyRedirects("Redirect limit exceeded")


def _robots_decision(client: httpx.Client, source: Source, url: str) -> tuple[str, str]:
    """Return allowed/disallowed/unavailable without treating fetch errors as rules."""
    parsed = urlsplit(url)
    robots_url = f"{parsed.scheme}://{parsed.netloc}/robots.txt"
    cache = _robots_cache.get()
    key = (parsed.scheme, parsed.netloc.lower(), USER_AGENT, tuple(sorted(source.allowed_domains)), source.internal)
    cached = cache.get(key) if cache is not None else None
    if cached is not None:
        parser, reason = cached
        if parser is None:
            return "unavailable" if reason != "robots.txt not found" else "allowed", reason
        return ("allowed", "robots.txt allows collection") if parser.can_fetch(USER_AGENT, url) else ("disallowed", "robots.txt disallows collection")
    try:
        response, _ = _get_following_policy(client, source, robots_url)
        if response.status_code in {404, 410}:
            response.close()
            if cache is not None:
                cache.put(key, None, "robots.txt not found")
            return "allowed", "robots.txt not found"
        if response.status_code in {401, 403}:
            response.close()
            return "unavailable", "robots.txt access is unavailable"
        if 400 <= response.status_code < 500:
            response.close()
            return "unavailable", "robots.txt request failed"
        if response.status_code >= 500:
            response.close()
            return "unavailable", "robots.txt server error"
        try:
            data = _read_limited(response).decode(response.encoding or "utf-8", errors="replace")
        finally:
            response.close()
        parser = RobotFileParser()
        parser.set_url(robots_url)
        parser.parse(data.splitlines())
        if cache is not None:
            cache.put(key, parser, "robots.txt policy loaded")
        return ("allowed", "robots.txt allows collection") if parser.can_fetch(USER_AGENT, url) else ("disallowed", "robots.txt disallows collection")
    except (httpx.HTTPError, OSError, ValueError, PolicyError):
        return "unavailable", "Unable to check robots.txt"


def _require_robots_access(client: httpx.Client, source: Source, url: str) -> None:
    decision, reason = _robots_decision(client, source, url)
    if decision == "disallowed":
        raise _policy_error("robots_disallowed", "robots.txt does not allow collection")
    if decision == "unavailable":
        raise RobotsUnavailable(reason)


def _safe_validator(value: Any) -> str | None:
    if not isinstance(value, str) or not value or len(value) > 512 or "\r" in value or "\n" in value:
        return None
    return value


def _conditional_headers(validators: dict[str, str] | None) -> dict[str, str]:
    if not validators:
        return {}
    headers: dict[str, str] = {}
    if etag := _safe_validator(validators.get("etag")):
        headers["If-None-Match"] = etag
    if modified := _safe_validator(validators.get("last_modified")):
        headers["If-Modified-Since"] = modified
    return headers


def _http_metadata(response: httpx.Response) -> dict[str, str]:
    metadata: dict[str, str] = {}
    for response_name, key in (("etag", "etag"), ("last-modified", "last_modified"), ("retry-after", "retry_after")):
        if value := _safe_validator(response.headers.get(response_name)):
            metadata[key] = value
    return metadata


def _http_collect(source: Source, validators: dict[str, str] | None = None) -> FetchResult:
    try:
        _check_url(source, source.location)
    except PolicyError as exc:
        return _policy_result(source, "http", exc)
    timeout = httpx.Timeout(source.timeout_seconds)
    try:
        with robots_policy_scope(_robots_cache.get()), httpx.Client(
            timeout=timeout,
            follow_redirects=False,
            headers={"User-Agent": USER_AGENT, "Accept": "text/html,application/xhtml+xml,application/json,*/*;q=0.8"},
            trust_env=False,
        ) as client:
            response, trace = _get_following_policy(client, source, source.location, headers=_conditional_headers(validators), enforce_robots=source.respect_robots)
            final_url = str(response.url)
            metadata = _http_metadata(response)
            if response.status_code == 304:
                response.close()
                return _result(source, "http", "not_modified", final_url=final_url, trace=trace, http_metadata=metadata)
            if response.status_code == 401:
                response.close()
                return _result(source, "http", "needs_auth", final_url=final_url, message="HTTP 401 authentication required", trace=trace)
            if response.status_code in {403, 429}:
                status = response.status_code
                media = response.headers.get("content-type", "").split(";", 1)[0].lower()
                try:
                    challenge = detect_challenge(_read_limited(response), status) if media in {"text/html", "application/xhtml+xml"} else None
                except ValueError:
                    challenge = None
                finally:
                    response.close()
                if challenge:
                    trace.append(_challenge_trace(challenge))
                    return _result(source, "http", "blocked", final_url=final_url, message=f"HTTP {status} access challenge", trace=trace, http_metadata=metadata)
                code = "rate_limited" if status == 429 else "access_forbidden"
                trace.append({"event": "access_blocked", "code": code, "at": utc_now()})
                return _result(source, "http", "blocked", final_url=final_url, message=f"HTTP {status} {code}", trace=trace, http_metadata=metadata)
            if response.status_code >= 400:
                status = response.status_code
                response.close()
                return _result(source, "http", "failed", final_url=final_url, message=f"HTTP {status}", trace=trace, http_metadata=metadata)
            content = _read_limited(response)
            media = response.headers.get("content-type", "").split(";", 1)[0].lower() or _media_type(final_url)
            if media in {"text/html", "application/xhtml+xml"}:
                challenge = detect_challenge(content, response.status_code)
                if challenge:
                    trace.append(_challenge_trace(challenge))
                    return _result(source, "http", "blocked", final_url=final_url, message="Access challenge detected", trace=trace, http_metadata=metadata)
                if _is_auth_page(content, response.encoding or "utf-8"):
                    return _result(source, "http", "needs_auth", content=content, media_type=media, final_url=final_url, message="Login page detected", trace=trace, http_metadata=metadata)
            return _result(source, "http", "fetched", content=content, media_type=media, final_url=final_url, trace=trace, http_metadata=metadata)
    except PolicyError as exc:
        if exc.code == "robots_disallowed":
            return _result(source, "http", "policy_denied", message="robots.txt does not allow collection", trace=_policy_trace(exc))
        return _policy_result(source, "http", exc)
    except RobotsUnavailable as exc:
        return _result(source, "http", "failed", message=str(exc))
    except httpx.TimeoutException:
        return _result(source, "http", "timeout", message="Request timed out")
    except (httpx.HTTPError, OSError, ValueError) as exc:
        return _result(source, "http", "failed", message=f"Collection failed ({type(exc).__name__})", trace=[{"event": "collection_failed", "code": "network_or_response_error", "at": utc_now()}])


def _safe_step(step: dict[str, Any]) -> tuple[str, str]:
    action = str(step.get("action", ""))
    if 'if_visible' in step and (action != 'click' or not isinstance(step['if_visible'], bool)):
        raise _policy_error("invalid_recipe", "if_visible requires a boolean on a click action")
    if 'appearance_wait_seconds' in step:
        wait_seconds = step['appearance_wait_seconds']
        if (action != 'click' or step.get('if_visible') is not True or isinstance(wait_seconds, bool)
                or not isinstance(wait_seconds, (int, float)) or not 0 <= wait_seconds <= 5):
            raise _policy_error('invalid_recipe', 'appearance_wait_seconds requires an optional click and a number from 0 to 5')
    if action not in {"click", "select", "fill", "wait_for", "assert_text", "expand_list", "enumerate_options", "enumerate_buttons", "paginate", "scroll_list"}:
        raise _policy_error("browser_action_denied", "Browser action is not allowed")
    if action in {"enumerate_options", "enumerate_buttons", "paginate", "scroll_list"}:
        try:
            validate_enumeration_step(step)
        except ValueError as exc:
            raise _policy_error("invalid_recipe", str(exc)) from exc
        return action, step["selectors"][0] if action == "enumerate_options" else step["selector"]
    selector = step.get("selector")
    if not isinstance(selector, str) or not selector.strip():
        raise _policy_error("invalid_recipe", "Browser action requires a selector")
    return action, selector


def _expand_options(step: dict[str, Any]) -> tuple[str, str, int, int, str | None, str | None, float, float]:
    """Validate an expansion recipe before interacting with the page."""
    item = step.get("item_selector")
    button = step.get("button_selector")
    if not isinstance(step.get("trigger", "click"), str) or step.get("trigger", "click") not in {"click", "scroll"}:
        raise _policy_error("invalid_recipe", "List expansion trigger must be click or scroll")
    if not isinstance(item, str) or not item.strip() or not isinstance(button, str) or not button.strip():
        raise _policy_error("invalid_recipe", "List expansion requires item and button selectors")
    for key, default, maximum in (("max_clicks", 100, 100), ("max_items", 1000, 1000)):
        value = step.get(key, default)
        if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= maximum:
            raise _policy_error("invalid_recipe", f"{key} must be between 1 and {maximum}")
    empty = step.get("empty_selector")
    all_selector = step.get("all_selector")
    all_value = step.get("all_value")
    if empty is not None and (not isinstance(empty, str) or not empty.strip()):
        raise _policy_error("invalid_recipe", "empty_selector must be a nonempty selector")
    if all_selector is not None and (not isinstance(all_selector, str) or not all_selector.strip()):
        raise _policy_error("invalid_recipe", "all_selector must be a nonempty selector")
    if all_value is not None and not isinstance(all_value, str):
        raise _policy_error("invalid_recipe", "all_value must be a string")
    if (all_selector is None) != (all_value is None):
        raise _policy_error("invalid_recipe", "All selection requires both selector and value")
    section = step.get("section", "popular")
    if not isinstance(section, str) or not re.fullmatch(r"[a-z][a-z0-9_]{0,31}", section):
        raise _policy_error("invalid_recipe", "section must be a short identifier")
    for key, default, maximum in (("initial_wait_seconds", 8, 30), ("progress_wait_seconds", 5, 30)):
        value = step.get(key, default)
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 < value <= maximum:
            raise _policy_error("invalid_recipe", f"{key} must be between 0 and {maximum}")
    return (item, button, step.get("max_clicks", 100), step.get("max_items", 1000), empty, all_selector,
            float(step.get("initial_wait_seconds", 8)), float(step.get("progress_wait_seconds", 5)))


def _expand_list(page: Any, step: dict[str, Any], remaining_ms: Any, trace: list[dict[str, Any]]) -> None:
    """Expand one scoped, append-only list; record incomplete or replaced lists honestly."""
    item_selector, button_selector, max_clicks, max_items, empty_selector, all_selector, initial_wait, progress_wait = _expand_options(step)
    section = step.get("section", "popular")
    container = page.locator(step["selector"])
    count = 0
    clicks = 0
    reason = "unknown"
    status = "unknown"
    observed: dict[str, dict[str, Any]] = {}
    prior: list[str] = []
    replaced = False
    start_url = page.url
    current: list[str] = []

    def scoped_one(selector: str, kind: str) -> Any:
        matches = container.locator(selector)
        if matches.count() != 1:
            raise ValueError(f"ambiguous_{kind}")
        return matches

    def visible_count(selector: str) -> int:
        return container.locator(selector + ":visible").count()

    def observe() -> list[str]:
        nonlocal count, replaced
        cards = container.locator(item_selector + ":visible")
        # A fixed script only reads rendered card evidence; recipe data is a selector.
        batch = cards.evaluate_all("els => els.slice(0, 1001).map(el => ({text: (el.innerText || '').trim(), html: el.outerHTML}))")
        if len(batch) > max_items:
            raise ValueError("item_budget")
        identities: list[str] = []
        for index, card in enumerate(batch):
            html = str(card.get("html", ""))[:16384]
            card_text = str(card.get("text", ""))[:4096]
            # Position disambiguates repeated offers within a batch; text is stable
            # across harmless class changes and supplies a reproducible observation.
            identity = hashlib.sha256(f"{index}\0{card_text}".encode("utf-8")).hexdigest()
            identities.append(identity)
            if identity not in observed:
                observed[identity] = {"index": index, "text": card_text, "html": html}
        if prior and (len(identities) < len(prior) or identities[:len(prior)] != prior):
            replaced = True
        count = max(count, len(observed))
        trace.append({"event": "list_expansion_count", "section": section, "observed_count": count,
                      "visible_count": len(batch), "click_count": clicks, "button_count": visible_count(button_selector), "at": utc_now()})
        prior[:] = identities
        return identities

    def terminal_confirmed() -> bool:
        """Confirm the button and cards stay unchanged for the recipe's progress window."""
        if (remaining_ms() - 1500) / 1000 < progress_wait:
            return False
        stable_since = time.monotonic()
        baseline = prior.copy()
        while True:
            if page.url != start_url or visible_count(button_selector):
                return False
            if observe() != baseline:
                baseline = prior.copy()
                stable_since = time.monotonic()
            if replaced:
                return False
            elapsed = time.monotonic() - stable_since
            if elapsed >= progress_wait:
                return page.url == start_url and visible_count(button_selector) == 0
            available_ms = remaining_ms() - 1500
            if available_ms <= 0:
                return False
            page.wait_for_timeout(min(200, max(1, (progress_wait - elapsed) * 1000), available_ms))

    try:
        if container.count() != 1:
            reason = "ambiguous_container"
            return
        if all_selector:
            control = scoped_one(all_selector, "all_control")
            desired = step["all_value"]
            if control.input_value(timeout=remaining_ms()) != desired:
                control.select_option(desired, timeout=remaining_ms())
                trace.append({"event": "list_expansion_filter", "section": section, "selected": True, "at": utc_now()})
            else:
                trace.append({"event": "list_expansion_filter", "section": section, "selected": False, "at": utc_now()})
        # The heading may appear before cards. A missing list is not an empty list.
        ready_until = time.monotonic() + min(initial_wait, max(0.0, (remaining_ms() - 1500) / 1000))
        while True:
            if page.url != start_url:
                reason = "unexpected_navigation"
                status = "partial" if count else "unknown"
                break
            current = observe()
            if current:
                break
            if empty_selector and visible_count(empty_selector) == 1:
                status, reason = "complete", "explicit_empty"
                break
            if time.monotonic() >= ready_until:
                reason = "initial_cards_timeout"
                break
            page.wait_for_timeout(min(200, max(1, (ready_until - time.monotonic()) * 1000)))
        while current and status == "unknown":
            if replaced:
                status, reason = "partial", "list_replaced"
                break
            buttons = visible_count(button_selector)
            if buttons == 0:
                if terminal_confirmed():
                    status, reason = "complete", "button_absent"
                    break
                if replaced:
                    status, reason = "partial", "list_replaced"
                    break
                if page.url != start_url:
                    status, reason = "partial", "unexpected_navigation"
                    break
                if visible_count(button_selector) == 0:
                    status, reason = "partial", "completion_unconfirmed"
                    break
                current = prior.copy()
                continue
            if buttons != 1:
                status, reason = "partial", "ambiguous_button"
                break
            if clicks >= max_clicks or count >= max_items:
                status, reason = "partial", "click_budget" if clicks >= max_clicks else "item_budget"
                break
            control = scoped_one(button_selector + ":visible", "button")
            if step.get("trigger", "click") == "scroll":
                control.scroll_into_view_if_needed(timeout=remaining_ms())
            else:
                control.click(timeout=remaining_ms())
            clicks += 1
            before = current
            progressed = False
            progress_until = time.monotonic() + min(progress_wait, max(0.0, (remaining_ms() - 1500) / 1000))
            while time.monotonic() < progress_until:
                if page.url != start_url:
                    break
                current = observe()
                if current != before:
                    progressed = True
                    break
                page.wait_for_timeout(min(200, max(1, (progress_until - time.monotonic()) * 1000)))
            if not progressed:
                if page.url != start_url:
                    status, reason = "partial", "unexpected_navigation"
                elif visible_count(button_selector) == 0:
                    if terminal_confirmed():
                        status, reason = "complete", "button_absent"
                    elif page.url != start_url:
                        status, reason = "partial", "unexpected_navigation"
                    elif replaced:
                        status, reason = "partial", "list_replaced"
                    elif visible_count(button_selector) == 1:
                        current = prior.copy()
                        continue
                    else:
                        status, reason = "partial", "completion_unconfirmed"
                else:
                    status, reason = "partial", "no_progress"
                break
    except ValueError as exc:
        reason = str(exc)
        status = "partial" if count else "unknown"
    except Exception as exc:
        # Preserve a page snapshot even when a browser action itself times out.
        reason = "action_timeout" if type(exc).__name__.endswith("TimeoutError") else "browser_error"
        status = "partial" if count else "unknown"
    finally:
        trace.append({"event": "list_expansion", "section": section, "status": status,
                      "reason": reason, "observed_count": count, "click_count": clicks,
                      "replaced": replaced, "observed_cards": list(observed.values()), "at": utc_now()})


def _selected_control_states(page: Any, timeout_ms: float) -> list[dict[str, Any]]:
    """Capture only non-text control state after a recipe, never entered values."""
    states = page.evaluate(
        """() => Array.from(document.querySelectorAll('select, input[type=checkbox], input[type=radio]'))
          .slice(0, 100).map((control, index) => {
            const labels = control.labels ? Array.from(control.labels).map(label => (label.innerText || label.textContent || '').trim()).filter(label => label && !(/[\\w.+-]+@[\\w.-]+\\.[A-Za-z]{2,}/.test(label) || /(?:\\+?\\d[\\d .()-]{6,}\\d)/.test(label))) : [];
            const base = { index, tag: control.tagName.toLowerCase(), type: control.type || 'select', labels: labels.slice(0, 3).map(label => label.slice(0, 160)) };
            if (control.tagName.toLowerCase() === 'select') {
              return { ...base, selected_index: control.selectedIndex, selected_label: control.selectedOptions[0] ? (control.selectedOptions[0].textContent || '').trim().slice(0, 160) : '' };
            }
            return { ...base, checked: Boolean(control.checked) };
          })""",
    )
    return states if isinstance(states, list) else []


def _evidence_html_snapshot(page: Any, listing_expectation: dict[str, Any] | None = None) -> bytes:
    """Serialize live selection state without retaining free-form input values."""
    listing = (page.locator(listing_expectation["selector"]).locator(
        listing_expectation["item_selector"] + ":visible") if listing_expectation else page.locator("html"))
    snapshot = listing.evaluate_all(
        """(elements, expectation) => {
          if (expectation) {
            const entries = elements.map(el => ({text: el.innerText, html: el.outerHTML}));
            if (elements.some(el => !document.documentElement.contains(el)) ||
                JSON.stringify(entries) !== JSON.stringify(expectation.entries)) return { changed: true };
          }
          const clone = document.documentElement.cloneNode(true);
          const originals = Array.from(document.querySelectorAll('select, input, textarea'));
          const copies = Array.from(clone.querySelectorAll('select, input, textarea'));
          originals.forEach((original, index) => {
            const copy = copies[index];
            if (!copy) return;
            const tag = original.tagName.toLowerCase();
            const type = (original.type || '').toLowerCase();
            if (tag === 'select') {
              Array.from(copy.options).forEach((option, optionIndex) => {
                option.toggleAttribute('selected', Boolean(original.options[optionIndex] && original.options[optionIndex].selected));
              });
            } else if (type === 'checkbox' || type === 'radio') {
              copy.toggleAttribute('checked', Boolean(original.checked));
            } else {
              copy.removeAttribute('value');
              copy.value = '';
            }
            if (tag === 'textarea') copy.textContent = '';
          });
          const originalElements = Array.from(document.body ? document.body.querySelectorAll('*') : []);
          const copiedElements = Array.from(clone.querySelectorAll('body *'));
          originalElements.forEach((original, index) => {
            const copy = copiedElements[index];
            if (!copy) return;
            const style = window.getComputedStyle(original);
            const computedHidden = style.display === 'none' || style.visibility === 'hidden' ||
              style.visibility === 'collapse' || style.opacity === '0';
            if (computedHidden) {
              copy.setAttribute('hidden', '');
              copy.setAttribute('data-sourceledger-computed-hidden', 'true');
            }
          });
          return { html: '<!doctype html>\\n' + clone.outerHTML };
        }""", listing_expectation
    )
    if snapshot.get("changed"):
        raise ListingChanged("Listing changed before its evidence was captured")
    return str(snapshot["html"]).encode("utf-8")


def _close_browser_resources(context: Any, browser: Any) -> None:
    """Drain route callbacks before closing their context and browser."""
    if context is not None:
        try:
            context.unroute_all(behavior="ignoreErrors")
        except Exception:
            # Teardown can race an already closed target. Policy failures have
            # already been returned or traced before this lifecycle cleanup.
            pass
        try:
            context.close()
        except Exception:
            pass
    if browser is not None:
        try:
            browser.close()
        except Exception:
            pass


def _close_reusable_context(context: Any, session: "_ReusableBrowser") -> None:
    """Retire a session if its page isolation could not be torn down."""
    try:
        context.unroute_all(behavior="ignoreErrors")
        context.close()
    except Exception:
        session.close()


_browser_thread_state = threading.local()


class _ReusableBrowser:
    """A Chromium process owned exclusively by its calling worker thread."""

    def __init__(self) -> None:
        self.owner = threading.get_ident()
        self.playwright: Any = None
        self.browser: Any = None
        self.last_launch_ms = 0.0
        self.last_context_ms = 0.0

    def context(self, timeout_ms: float) -> Any:
        if threading.get_ident() != self.owner:
            raise RuntimeError("Browser session used from another thread")
        deadline = time.monotonic() + timeout_ms / 1000

        def remaining_ms() -> float:
            remaining = (deadline - time.monotonic()) * 1000
            if remaining <= 0:
                from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

                raise PlaywrightTimeoutError("Browser session startup exceeded collection deadline")
            return remaining

        if self.browser is not None and not self.browser.is_connected():
            self.close()
        if self.browser is None:
            from playwright.sync_api import sync_playwright

            self.playwright = sync_playwright().start()
            try:
                runtime = select_browser_runtime(self.playwright.chromium)
                if runtime is None:
                    raise RuntimeError("No supported Chromium browser executable was found")
                launch_started = time.monotonic()
                self.browser = self.playwright.chromium.launch(
                    timeout=remaining_ms(),
                    headless=browser_headless(),
                    **runtime.launch_options(),
                )
                self.last_launch_ms = round((time.monotonic() - launch_started) * 1000, 1)
            except Exception:
                self.close()
                raise
        else:
            self.last_launch_ms = 0.0
        remaining_ms()
        context_started = time.monotonic()
        context = self.browser.new_context()
        self.last_context_ms = round((time.monotonic() - context_started) * 1000, 1)
        return context

    def close(self) -> None:
        if threading.get_ident() != self.owner:
            raise RuntimeError("Browser session closed from another thread")
        browser, playwright = self.browser, self.playwright
        self.browser = self.playwright = None
        if browser is not None:
            try:
                browser.close()
            except Exception:
                pass
        if playwright is not None:
            try:
                playwright.stop()
            except Exception:
                pass


@contextmanager
def browser_session(reuse: bool = True) -> Any:
    """Reuse Chromium on this thread; each URL still receives a fresh context.

    The caller must enter and leave on the same worker thread. Profile-backed
    sources always use their own persistent context outside this process pool.
    """
    if not reuse:
        yield
        return
    previous = getattr(_browser_thread_state, "session", None)
    if previous is not None:
        # Nested scopes share the same owner and only the outer scope closes it.
        yield
        return
    session = _ReusableBrowser()
    _browser_thread_state.session = session
    try:
        yield
    finally:
        _browser_thread_state.session = None
        session.close()


def _browser_collect(source: Source, base_dir: str) -> FetchResult:
    try:
        _check_url(source, source.location)
    except PolicyError as exc:
        return _policy_result(source, "playwright", exc)
    try:
        from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
        from playwright.sync_api import sync_playwright
    except ImportError:
        return _result(source, "playwright", "tool_unavailable", message="The playwright package is not installed")

    trace: list[dict[str, Any]] = []
    variant_snapshots: list[dict[str, Any]] = []
    variant_bytes = 0
    challenge_seen: dict[str, str] | None = None
    deadline = time.monotonic() + source.timeout_seconds

    def remaining_ms() -> float:
        remaining = (deadline - time.monotonic()) * 1000
        if remaining <= 0:
            raise PlaywrightTimeoutError("collection deadline exceeded")
        return remaining

    def challenge_result(challenge: dict[str, str], final_url: str) -> FetchResult:
        trace.append(_challenge_trace(challenge))
        return _result(source, "playwright", "blocked", final_url=final_url,
                       message="Access challenge detected", trace=trace,
                       variant_snapshots=variant_snapshots)

    context = None
    browser = None
    navigation_robots_failure: list[Exception] = []
    pending_redirect: list[str] = []
    browser_phase = 'startup'

    def require_navigation_policy() -> None:
        if navigation_robots_failure:
            raise navigation_robots_failure[-1]

    try:
        session = None if source.profile_dir else getattr(_browser_thread_state, "session", None)
        with ExitStack() as cleanup:
            cleanup.enter_context(robots_policy_scope(_robots_cache.get()))
            robots_client = None
            if source.respect_robots:
                robots_client = cleanup.enter_context(httpx.Client(
                    timeout=httpx.Timeout(min(source.timeout_seconds, max(0.1, deadline - time.monotonic()))),
                    follow_redirects=False, headers={"User-Agent": USER_AGENT}, trust_env=False,
                ))
                _require_robots_access(robots_client, source, source.location)
            # Unroute and close while Playwright's driver is still running.
            # An outer finally executes too late: __exit__ already stops it.
            if session is not None:
                startup_started = time.monotonic()
                context = session.context(remaining_ms())
                trace.append({"event": "browser_context_ready", "startup_ms": round((time.monotonic() - startup_started) * 1000, 1), "launch_ms": session.last_launch_ms, "context_ms": session.last_context_ms, "reused": session.last_launch_ms == 0, "at": utc_now()})
                cleanup.callback(lambda: _close_reusable_context(context, session))
            else:
                startup_started = time.monotonic()
                pw = cleanup.enter_context(sync_playwright())
                cleanup.callback(lambda: _close_browser_resources(context, browser))
                runtime = select_browser_runtime(pw.chromium)
                if runtime is None:
                    return _result(source, "playwright", "tool_unavailable", message="No supported Chromium browser executable was found", trace=trace)
                launch_args: dict[str, Any] = {"headless": browser_headless(), **runtime.launch_options()}
                launch_started = time.monotonic()
                if source.profile_dir:
                    profile = resolve_path(base_dir, source.profile_dir)
                    profile.mkdir(parents=True, exist_ok=True)
                    context = pw.chromium.launch_persistent_context(str(profile), timeout=remaining_ms(), **launch_args)
                else:
                    browser = pw.chromium.launch(timeout=remaining_ms(), **launch_args)
                    launch_ms = round((time.monotonic() - launch_started) * 1000, 1)
                    context_started = time.monotonic()
                    context = browser.new_context()
                trace.append({"event": "browser_context_ready", "startup_ms": round((time.monotonic() - startup_started) * 1000, 1), "launch_ms": launch_ms if not source.profile_dir else round((time.monotonic() - launch_started) * 1000, 1), "context_ms": round((time.monotonic() - context_started) * 1000, 1) if not source.profile_dir else 0.0, "reused": False, "at": utc_now()})

            # Recipes may change filters but must never submit a form or create
            # external side effects. This runs before any page JavaScript.
            context.add_init_script("document.addEventListener('submit', event => event.preventDefault(), true)")

            def guard(route: Any, request: Any) -> None:
                if request.is_navigation_request() and request.frame == request.frame.page.main_frame:
                    try:
                        if request.method.upper() not in {"GET", "HEAD"}:
                            raise _policy_error("navigation_method_denied", "Browser navigation method is not allowed")
                        _check_url(source, request.url)
                        if robots_client is not None:
                            _require_robots_access(robots_client, source, request.url)
                        # Browser-followed redirects do not reliably trigger a
                        # route callback. Stop at this response and perform the
                        # next navigation explicitly so each hop is checked.
                        response = route.fetch(max_redirects=0, timeout=remaining_ms())
                        if response.status in {301, 302, 303, 307, 308}:
                            location = response.headers.get("location")
                            if location:
                                destination = urljoin(request.url, location)
                                _check_url(source, destination)
                                if robots_client is not None:
                                    _require_robots_access(robots_client, source, destination)
                                pending_redirect.append(destination)
                                trace.append({"event": "navigation_redirect_verified", "url": _safe_url(destination), "at": utc_now()})
                                route.abort("blockedbyclient")
                                return
                        route.fulfill(response=response)
                        return
                    except RobotsUnavailable as exc:
                        navigation_robots_failure.append(exc)
                        trace.append({"event": "navigation_robots_unavailable", "url": _safe_url(request.url), "at": utc_now()})
                        route.abort("blockedbyclient")
                        return
                    except PolicyError as exc:
                        navigation_robots_failure.append(exc)
                        trace.append({"event": "navigation_denied", "url": _safe_url(request.url), "at": utc_now()})
                        route.abort("blockedbyclient")
                        return
                    except Exception:
                        trace.append({"event": "navigation_fetch_failed", "url": _safe_url(request.url), "at": utc_now()})
                        route.abort("failed")
                        return
                elif urlsplit(request.url).scheme in {"http", "https"}:
                    parsed = urlsplit(request.url)
                    try:
                        addresses = _resolved_addresses(parsed.hostname or "", parsed.port or (443 if parsed.scheme == "https" else 80))
                        forbidden = not addresses or any(_is_forbidden_even_internal(address) for address in addresses)
                        private_from_public = not source.internal and any(not _is_public(address) for address in addresses)
                        if forbidden or private_from_public:
                            trace.append({"event": "subresource_denied", "url": _safe_url(request.url), "at": utc_now()})
                            route.abort("blockedbyclient")
                            return
                    except PolicyError:
                        route.abort("blockedbyclient")
                        return
                route.continue_()

            context.route("**/*", guard)
            page = context.new_page()
            document_status: dict[str, int] = {}

            def observe_response(response: Any) -> None:
                try:
                    if response.request.resource_type == "document" and response.frame == page.main_frame:
                        document_status["latest"] = response.status
                except Exception:
                    return

            page.on("response", observe_response)

            def settle_aborted_navigation() -> None:
                # The policy guard aborts a verified redirect. Its error-page
                # commit must finish before the same tab starts the next hop;
                # otherwise that late commit can interrupt destination goto.
                page.wait_for_url(
                    lambda current: str(current).startswith("chrome-error://"),
                    wait_until="commit", timeout=remaining_ms(),
                )
                document_status.clear()
                trace.append({'event': 'navigation_redirect_settled', 'at': utc_now()})

            def navigate_checked(url: str) -> Any:
                target = url
                for _ in range(MAX_REDIRECTS + 1):
                    try:
                        response = page.goto(target, wait_until="domcontentloaded", timeout=remaining_ms())
                    except Exception:
                        if not pending_redirect:
                            raise
                        # Chromium commits its error page after the aborted
                        # request. Wait for that commit before navigating again
                        # in the same tab so sessionStorage and window.name stay.
                        settle_aborted_navigation()
                        response = None
                    require_navigation_policy()
                    if pending_redirect:
                        target = pending_redirect.pop()
                        continue
                    return response
                raise _policy_error("redirect_limit_exceeded", "Redirect limit exceeded")

            navigation_started = time.monotonic()
            browser_phase = 'initial_navigation'
            navigation = navigate_checked(source.location)
            require_navigation_policy()
            trace.append({"event": "navigation_timing", "duration_ms": round((time.monotonic() - navigation_started) * 1000, 1), "at": utc_now()})
            _check_url(source, page.url)
            status = navigation.status if navigation else 0
            trace.append({"event": "navigate", "url": _safe_url(page.url), "status": status, "at": utc_now()})
            if status == 401:
                return _result(source, "playwright", "needs_auth", final_url=page.url, message="HTTP 401 authentication required", trace=trace)
            if status in {403, 429}:
                challenge = detect_challenge(_evidence_html_snapshot(page), status)
                if challenge:
                    return challenge_result(challenge, page.url)
                return _result(source, "playwright", "blocked", final_url=page.url, message=f"HTTP {status} access restricted", trace=trace)
            if status >= 400:
                return _result(source, "playwright", "failed", final_url=page.url, message=f"HTTP {status}", trace=trace)

            initial_challenge = detect_challenge(_evidence_html_snapshot(page), status)
            if initial_challenge:
                return challenge_result(initial_challenge, page.url)

            def capture_variant(selections: dict[str, str], selection_values: dict[str, str] | None = None,
                                listing_expectation: dict[str, Any] | None = None) -> None:
                nonlocal variant_bytes, challenge_seen
                require_navigation_policy()
                _check_url(source, page.url)
                if len(variant_snapshots) >= MAX_VARIANT_SNAPSHOTS:
                    raise SnapshotBudgetExceeded()
                html = _evidence_html_snapshot(page, listing_expectation)
                challenge = detect_challenge(html, document_status.get("latest", 200))
                if challenge:
                    challenge_seen = challenge
                    raise _AccessChallengeFound(challenge)
                if len(html) > MAX_RESPONSE_BYTES or _is_auth_page(html):
                    raise ValueError("unsafe_or_oversized_snapshot")
                if variant_bytes + len(html) > MAX_VARIANT_BYTES:
                    raise SnapshotBudgetExceeded()
                variant_snapshots.append({"content": html, "final_url": page.url,
                                          "fetched_at": utc_now(), "selections": selections,
                                          "selection_values": selection_values or {}})
                variant_bytes += len(html)

            for index, step in enumerate(source.recipe):
                browser_phase = 'recipe_action'
                action, selector = _safe_step(step)
                requested_ms = float(step.get("timeout_seconds", source.timeout_seconds)) * 1000
                timeout_ms = min(requested_ms, remaining_ms())
                locator = page.locator(selector).first
                if action == "click":
                    if step.get('if_visible'):
                        visible = page.locator(f':is({selector}):visible')
                        appearance_wait = float(step.get('appearance_wait_seconds', 0))
                        if appearance_wait and visible.count() == 0:
                            try:
                                visible.first.wait_for(state='visible', timeout=min(appearance_wait * 1000, remaining_ms()))
                            except PlaywrightTimeoutError:
                                pass
                        count = visible.count()
                        if count == 0:
                            trace.append({'event': 'recipe_step_skipped', 'index': index,
                                          'action': action, 'reason': 'not_visible', 'at': utc_now()})
                            continue
                        if count != 1:
                            raise _policy_error('invalid_recipe', 'Optional click target is ambiguous')
                        locator = visible
                    try:
                        locator.click(timeout=timeout_ms)
                    except Exception:
                        if not pending_redirect:
                            raise
                elif action == "select":
                    value = step.get("value")
                    if not isinstance(value, str):
                        raise _policy_error("invalid_recipe", "The select action requires a string value")
                    locator.select_option(value, timeout=timeout_ms)
                elif action == "fill":
                    value = step.get("value")
                    if not isinstance(value, str):
                        raise _policy_error("invalid_recipe", "The fill action requires a string value")
                    locator.fill(value, timeout=timeout_ms)
                elif action == "wait_for":
                    locator.wait_for(state=str(step.get("state", "visible")), timeout=timeout_ms)
                elif action == "assert_text":
                    expected = step.get("text")
                    if not isinstance(expected, str):
                        raise _policy_error("invalid_recipe", "The assert_text action requires string text")
                    actual = locator.inner_text(timeout=timeout_ms)
                    if expected not in actual:
                        raise AssertionError("Browser text assertion failed")
                elif action == "expand_list":
                    _expand_list(page, step, remaining_ms, trace)
                elif action == "enumerate_options":
                    enumerate_options(page, step, remaining_ms, capture_variant, trace, source.unavailable_markers)
                elif action == "enumerate_buttons":
                    enumerate_buttons(page, step, remaining_ms, capture_variant, trace, source.unavailable_markers)
                elif action == "paginate":
                    paginate(page, step, remaining_ms, capture_variant, trace)
                elif action == "scroll_list":
                    scroll_list(page, step, remaining_ms, capture_variant, trace)
                if challenge_seen:
                    return challenge_result(challenge_seen, page.url)
                if pending_redirect:
                    browser_phase = 'recipe_redirect'
                    settle_aborted_navigation()
                    navigate_checked(pending_redirect.pop())
                # Inputs and expected text are deliberately excluded from trace.
                trace.append({"event": "recipe_step", "index": index, "action": action, "selector": _safe_selector(selector), "url": _safe_url(page.url), "at": utc_now()})
                require_navigation_policy()
                _check_url(source, page.url)
                latest_status = document_status.get("latest", 0)
                if latest_status == 401:
                    return _result(source, "playwright", "needs_auth", final_url=page.url, message="HTTP 401 authentication required", trace=trace, variant_snapshots=variant_snapshots)
                if latest_status in {403, 429}:
                    challenge = detect_challenge(_evidence_html_snapshot(page), latest_status)
                    if challenge:
                        return challenge_result(challenge, page.url)
                    return _result(source, "playwright", "blocked", final_url=page.url, message=f"HTTP {latest_status} access restricted", trace=trace, variant_snapshots=variant_snapshots)
                if latest_status >= 400:
                    return _result(source, "playwright", "failed", final_url=page.url, message=f"HTTP {latest_status}", trace=trace, variant_snapshots=variant_snapshots)
                challenge = detect_challenge(_evidence_html_snapshot(page), latest_status or 200)
                if challenge:
                    return challenge_result(challenge, page.url)

            evidence_started = time.monotonic()
            browser_phase = 'evidence_capture'
            require_navigation_policy()
            html = _evidence_html_snapshot(page)
            challenge = detect_challenge(html, document_status.get("latest", 200))
            if challenge:
                return challenge_result(challenge, page.url)
            if _is_auth_page(html):
                return _result(source, "playwright", "needs_auth", final_url=page.url,
                               message="Login page detected", trace=trace, variant_snapshots=variant_snapshots)
            try:
                screenshot = page.screenshot(full_page=True, timeout=remaining_ms())
            except PlaywrightTimeoutError:
                screenshot = None
                trace.append({"event": "screenshot_unavailable", "reason": "deadline", "at": utc_now()})
            final_url = page.url
            _check_url(source, final_url)
            try:
                controls = _selected_control_states(page, remaining_ms())
                trace.append({"event": "selected_control_states", "controls": controls, "at": utc_now()})
            except PlaywrightTimeoutError:
                trace.append({"event": "selected_control_states_unavailable", "reason": "deadline", "at": utc_now()})
            trace.append({"event": "evidence_timing", "duration_ms": round((time.monotonic() - evidence_started) * 1000, 1), "at": utc_now()})
            return _result(source, "playwright", "fetched", content=html, media_type="text/html", final_url=final_url, screenshot=screenshot, trace=trace, variant_snapshots=variant_snapshots)
    except _AccessChallengeFound as exc:
        return challenge_result(exc.challenge, page.url)
    except PlaywrightTimeoutError:
        if navigation_robots_failure:
            failure = navigation_robots_failure[-1]
            if isinstance(failure, RobotsUnavailable):
                return _result(source, "playwright", "failed", message=str(failure), trace=trace, variant_snapshots=variant_snapshots)
            return _policy_result(source, "playwright", failure, trace=trace, variant_snapshots=variant_snapshots)
        return _result(source, "playwright", "timeout", message="Browser action timed out", trace=trace, variant_snapshots=variant_snapshots)
    except PolicyError as exc:
        if exc.code == "robots_disallowed":
            return _result(source, "playwright", "policy_denied", message="robots.txt does not allow collection", trace=trace + _policy_trace(exc), variant_snapshots=variant_snapshots)
        return _policy_result(source, "playwright", exc, trace=trace, variant_snapshots=variant_snapshots)
    except RobotsUnavailable as exc:
        return _result(source, "playwright", "failed", message=str(exc), trace=trace, variant_snapshots=variant_snapshots)
    except AssertionError as exc:
        return _result(source, "playwright", "failed", message=str(exc), trace=trace, variant_snapshots=variant_snapshots)
    except Exception as exc:
        if navigation_robots_failure:
            failure = navigation_robots_failure[-1]
            if isinstance(failure, RobotsUnavailable):
                return _result(source, "playwright", "failed", message=str(failure), trace=trace, variant_snapshots=variant_snapshots)
            return _policy_result(source, "playwright", failure, trace=trace, variant_snapshots=variant_snapshots)
        text = str(exc)
        if session is not None:
            try:
                if session.browser is not None and not session.browser.is_connected():
                    session.close()
            except Exception:
                session.close()
        if "Executable doesn't exist" in text or "No supported Chromium browser executable" in text:
            return _result(source, "playwright", "tool_unavailable", message="Playwright browser executable is unavailable", trace=trace, variant_snapshots=variant_snapshots)
        if "browsertype.launch" in text.casefold() or "browsertype.launchpersistentcontext" in text.casefold():
            trace.append({"event": "browser_launch_failed", "code": "browser_launch_failed", "at": utc_now()})
            return _result(source, "playwright", "failed", message="Chromium could not start in the selected display mode; run the optional browser launch check", trace=trace, variant_snapshots=variant_snapshots)
        lowered = text.casefold()
        error_code = ('navigation_interrupted' if 'interrupted by another navigation' in lowered else
                      'navigation_aborted' if 'net::err_aborted' in lowered else
                      'execution_context_destroyed' if 'execution context was destroyed' in lowered else
                      'browser_target_closed' if 'target page, context or browser has been closed' in lowered else
                      'browser_error')
        # Browser exception text may contain URLs, input values or credentials.
        # Emit only a bounded classification and phase, never the raw message.
        trace.append({'event': 'browser_error', 'code': error_code, 'phase': browser_phase,
                      'exception_type': type(exc).__name__, 'at': utc_now()})
        return _result(source, "playwright", "failed", message=f"Browser collection failed ({error_code})", trace=trace, variant_snapshots=variant_snapshots)


async def _crawl4ai_run(url: str, timeout_seconds: float) -> tuple[str, str]:
    from crawl4ai import AsyncWebCrawler, BrowserConfig, CacheMode, CrawlerRunConfig

    async with AsyncWebCrawler(config=BrowserConfig(headless=True)) as crawler:
        result = await asyncio.wait_for(
            crawler.arun(url=url, config=CrawlerRunConfig(cache_mode=CacheMode.BYPASS, check_robots_txt=True)),
            timeout=timeout_seconds,
        )
        if not result.success:
            raise RuntimeError(result.error_message or "Crawl4AI collection failed")
        return result.html, result.url or url


def _crawl4ai_collect(source: Source) -> FetchResult:
    if importlib.util.find_spec("crawl4ai") is None:
        return _result(source, "crawl4ai", "tool_unavailable", message="The crawl4ai package is not installed")
    # Validate redirect policy with the strict HTTP collector before handing the
    # verified final URL to the optional browser adapter.
    preflight = _http_collect(source)
    if preflight.status != "fetched":
        preflight.backend = "crawl4ai"
        return preflight
    try:
        html, final_url = asyncio.run(_crawl4ai_run(preflight.final_url, source.timeout_seconds))
        _check_url(source, final_url)
        return _result(source, "crawl4ai", "fetched", content=html.encode("utf-8"), media_type="text/html", final_url=final_url, trace=preflight.trace)
    except RuntimeError as exc:
        return _result(source, "crawl4ai", "failed", message=str(exc), trace=preflight.trace)
    except PolicyError:
        return _result(source, "crawl4ai", "policy_denied", message="Request denied by URL policy", trace=preflight.trace)
    except Exception as exc:
        return _result(source, "crawl4ai", "failed", message=f"Crawl4AI collection failed ({type(exc).__name__})", trace=preflight.trace)


def collect(source: Source, base_dir: str, backend: str, *, validators: dict[str, str] | None = None) -> FetchResult:
    """Collect one source with an explicitly selected backend."""
    if backend == "file":
        if source.kind != "file":
            return _result(source, backend, "policy_denied", message="The file backend can only read file sources")
        return _file_collect(source, base_dir)
    if source.kind == "file":
        return _result(source, backend, "policy_denied", message="File sources can only be read by the file backend")
    if backend == "http":
        return _http_collect(source, validators=validators)
    if backend == "playwright":
        return _browser_collect(source, base_dir)
    if backend == "crawl4ai":
        return _crawl4ai_collect(source)
    return _result(source, backend, "tool_unavailable", message=f"Unsupported backend: {backend}")
