"""Policy-aware source collectors.

Collection is deliberately separate from parsing.  These functions return the
source bytes and observations about the fetch; they never invent fields.
"""
from __future__ import annotations

import asyncio
from contextlib import ExitStack
import importlib.util
import ipaddress
import mimetypes
import socket
import re
import time
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlsplit
from urllib.robotparser import RobotFileParser

import httpx

from .models import FetchResult, Source, resolve_path, utc_now
from .browser_runtime import browser_headless, select_browser_runtime


USER_AGENT = "source-ledger/0.3 (+authorized price research)"
MAX_RESPONSE_BYTES = 20 * 1024 * 1024
MAX_REDIRECTS = 10
WELL_KNOWN_NAT64 = ipaddress.ip_network("64:ff9b::/96")
_PASSWORD_FIELD = re.compile(r"<input\b[^>]*(?:type\s*=\s*['\"]?password|name\s*=\s*['\"]password['\"]?)", re.I)


class PolicyError(ValueError):
    """A policy denial with a stable, safe-to-record reason code."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _policy_error(code: str, message: str) -> PolicyError:
    return PolicyError(code, message)


def _policy_trace(error: PolicyError) -> list[dict[str, Any]]:
    return [{"event": "policy_denied", "code": error.code, "at": utc_now()}]


def _policy_result(source: Source, backend: str, error: PolicyError, *, trace: list[dict[str, Any]] | None = None) -> FetchResult:
    entries = list(trace or [])
    entries.extend(_policy_trace(error))
    return _result(source, backend, "policy_denied", message=f"Request denied by policy ({error.code})", trace=entries)


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
) -> tuple[httpx.Response, list[dict[str, Any]]]:
    trace: list[dict[str, Any]] = []
    current = url
    for _ in range(MAX_REDIRECTS + 1):
        _check_url(source, current)
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
    try:
        response, _ = _get_following_policy(client, source, robots_url)
        if response.status_code in {404, 410}:
            response.close()
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
        data = _read_limited(response).decode(response.encoding or "utf-8", errors="replace")
        parser = RobotFileParser()
        parser.set_url(robots_url)
        parser.parse(data.splitlines())
        return ("allowed", "robots.txt allows collection") if parser.can_fetch(USER_AGENT, url) else ("disallowed", "robots.txt disallows collection")
    except (httpx.HTTPError, OSError, ValueError, PolicyError):
        return "unavailable", "Unable to check robots.txt"


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
    for response_name, key in (("etag", "etag"), ("last-modified", "last_modified")):
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
        with httpx.Client(
            timeout=timeout,
            follow_redirects=False,
            headers={"User-Agent": USER_AGENT, "Accept": "text/html,application/xhtml+xml,application/json,*/*;q=0.8"},
            trust_env=False,
        ) as client:
            if source.respect_robots:
                robots, reason = _robots_decision(client, source, source.location)
                if robots == "disallowed":
                    return _result(source, "http", "policy_denied", message="robots.txt does not allow collection")
                if robots == "unavailable":
                    return _result(source, "http", "failed", message=reason)
            response, trace = _get_following_policy(client, source, source.location, headers=_conditional_headers(validators))
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
                response.close()
                code = "rate_limited" if status == 429 else "access_forbidden"
                trace.append({"event": "access_blocked", "code": code, "at": utc_now()})
                return _result(source, "http", "blocked", final_url=final_url, message=f"HTTP {status} {code}", trace=trace, http_metadata=metadata)
            if response.status_code >= 400:
                status = response.status_code
                response.close()
                return _result(source, "http", "failed", final_url=final_url, message=f"HTTP {status}", trace=trace)
            content = _read_limited(response)
            media = response.headers.get("content-type", "").split(";", 1)[0] or _media_type(final_url)
            if media in {"text/html", "application/xhtml+xml"}:
                if _is_auth_page(content, response.encoding or "utf-8"):
                    return _result(source, "http", "needs_auth", content=content, media_type=media, final_url=final_url, message="Login page detected", trace=trace, http_metadata=metadata)
            return _result(source, "http", "fetched", content=content, media_type=media, final_url=final_url, trace=trace, http_metadata=metadata)
    except PolicyError as exc:
        return _policy_result(source, "http", exc)
    except httpx.TimeoutException:
        return _result(source, "http", "timeout", message="Request timed out")
    except (httpx.HTTPError, OSError, ValueError) as exc:
        return _result(source, "http", "failed", message=f"Collection failed ({type(exc).__name__})", trace=[{"event": "collection_failed", "code": "network_or_response_error", "at": utc_now()}])


def _safe_step(step: dict[str, Any]) -> tuple[str, str]:
    action = str(step.get("action", ""))
    if action not in {"click", "select", "fill", "wait_for", "assert_text"}:
        raise _policy_error("browser_action_denied", "Browser action is not allowed")
    selector = step.get("selector")
    if not isinstance(selector, str) or not selector.strip():
        raise _policy_error("invalid_recipe", "Browser action requires a selector")
    return action, selector


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


def _evidence_html_snapshot(page: Any) -> bytes:
    """Serialize live selection state without retaining free-form input values."""
    html = page.evaluate(
        """() => {
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
          return '<!doctype html>\\n' + clone.outerHTML;
        }"""
    )
    return str(html).encode("utf-8")


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
    deadline = time.monotonic() + source.timeout_seconds

    def remaining_ms() -> float:
        remaining = (deadline - time.monotonic()) * 1000
        if remaining <= 0:
            raise PlaywrightTimeoutError("collection deadline exceeded")
        return remaining

    context = None
    browser = None
    try:
        with sync_playwright() as pw, ExitStack() as cleanup:
            # Unroute and close while Playwright's driver is still running.
            # An outer finally executes too late: __exit__ already stops it.
            cleanup.callback(lambda: _close_browser_resources(context, browser))
            runtime = select_browser_runtime(pw.chromium)
            if runtime is None:
                return _result(source, "playwright", "tool_unavailable", message="No supported Chromium browser executable was found", trace=trace)
            launch_args: dict[str, Any] = {"headless": browser_headless(), **runtime.launch_options()}
            if source.profile_dir:
                profile = resolve_path(base_dir, source.profile_dir)
                profile.mkdir(parents=True, exist_ok=True)
                context = pw.chromium.launch_persistent_context(str(profile), timeout=remaining_ms(), **launch_args)
            else:
                browser = pw.chromium.launch(timeout=remaining_ms(), **launch_args)
                context = browser.new_context()

            # Recipes may change filters but must never submit a form or create
            # external side effects. This runs before any page JavaScript.
            context.add_init_script("document.addEventListener('submit', event => event.preventDefault(), true)")

            def guard(route: Any, request: Any) -> None:
                if request.is_navigation_request() and request.frame == request.frame.page.main_frame:
                    try:
                        _check_url(source, request.url)
                    except PolicyError:
                        trace.append({"event": "navigation_denied", "url": _safe_url(request.url), "at": utc_now()})
                        route.abort("blockedbyclient")
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
            if source.respect_robots:
                with httpx.Client(timeout=httpx.Timeout(min(source.timeout_seconds, max(0.1, deadline - time.monotonic()))), follow_redirects=False, headers={"User-Agent": USER_AGENT}, trust_env=False) as client:
                    robots, reason = _robots_decision(client, source, source.location)
                    if robots == "disallowed":
                        return _result(source, "playwright", "policy_denied", message="robots.txt does not allow collection", trace=trace)
                    if robots == "unavailable":
                        return _result(source, "playwright", "failed", message=reason, trace=trace)
            navigation = page.goto(source.location, wait_until="domcontentloaded", timeout=remaining_ms())
            _check_url(source, page.url)
            status = navigation.status if navigation else 0
            trace.append({"event": "navigate", "url": _safe_url(page.url), "status": status, "at": utc_now()})
            if status == 401:
                return _result(source, "playwright", "needs_auth", final_url=page.url, message="HTTP 401 authentication required", trace=trace)
            if status in {403, 429}:
                return _result(source, "playwright", "blocked", final_url=page.url, message=f"HTTP {status} access restricted", trace=trace)
            if status >= 400:
                return _result(source, "playwright", "failed", final_url=page.url, message=f"HTTP {status}", trace=trace)

            for index, step in enumerate(source.recipe):
                action, selector = _safe_step(step)
                requested_ms = float(step.get("timeout_seconds", source.timeout_seconds)) * 1000
                timeout_ms = min(requested_ms, remaining_ms())
                locator = page.locator(selector).first
                if action == "click":
                    locator.click(timeout=timeout_ms)
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
                # Inputs and expected text are deliberately excluded from trace.
                trace.append({"event": "recipe_step", "index": index, "action": action, "selector": _safe_selector(selector), "url": _safe_url(page.url), "at": utc_now()})
                _check_url(source, page.url)
                latest_status = document_status.get("latest", 0)
                if latest_status == 401:
                    return _result(source, "playwright", "needs_auth", final_url=page.url, message="HTTP 401 authentication required", trace=trace)
                if latest_status in {403, 429}:
                    return _result(source, "playwright", "blocked", final_url=page.url, message=f"HTTP {latest_status} access restricted", trace=trace)
                if latest_status >= 400:
                    return _result(source, "playwright", "failed", final_url=page.url, message=f"HTTP {latest_status}", trace=trace)

            html = _evidence_html_snapshot(page)
            if _is_auth_page(html):
                return _result(source, "playwright", "needs_auth", content=html, media_type="text/html", final_url=page.url, message="Login page detected", trace=trace)
            screenshot = page.screenshot(full_page=True, timeout=remaining_ms())
            final_url = page.url
            _check_url(source, final_url)
            trace.append({"event": "selected_control_states", "controls": _selected_control_states(page, remaining_ms()), "at": utc_now()})
            return _result(source, "playwright", "fetched", content=html, media_type="text/html", final_url=final_url, screenshot=screenshot, trace=trace)
    except PlaywrightTimeoutError:
        return _result(source, "playwright", "timeout", message="Browser action timed out", trace=trace)
    except PolicyError as exc:
        return _policy_result(source, "playwright", exc, trace=trace)
    except AssertionError as exc:
        return _result(source, "playwright", "failed", message=str(exc), trace=trace)
    except Exception as exc:
        text = str(exc)
        if "Executable doesn't exist" in text or "browserType.launch" in text:
            return _result(source, "playwright", "tool_unavailable", message="Playwright browser executable is unavailable", trace=trace)
        return _result(source, "playwright", "failed", message=f"Browser collection failed ({type(exc).__name__})", trace=trace)


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
