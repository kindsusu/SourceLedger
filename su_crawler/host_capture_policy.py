"""Policy guard for host-browser captures: the host may not bypass the program's own collection policy."""
from __future__ import annotations

import ipaddress
from collections.abc import Callable
from urllib.parse import urlsplit

from .models import Source
from .research_plans import canonical_url

ROBOTS_TIMEOUT_SECONDS = 10.0
# Coverage states that exist only after the page itself answered, so robots.txt
# allowed the request. Every other state (failed, timeout, unprocessed, ...) may
# have stopped before or at robots.txt and needs a fresh decision.
ROBOTS_PROVEN_STATUSES = frozenset({"visited", "no_data", "blocked"})


def _ip_literal(host: str):
    try:
        return ipaddress.ip_address(host)
    except ValueError:
        return None


def _live_robots_check(approved_host: str) -> Callable[[str], tuple[str, str]]:
    """Robots decision through the collector's own helper, client factory and user agent."""
    from . import collectors
    source = Source("host_capture_" + approved_host, approved_host, "web", f"https://{approved_host}/", [],
                    allowed_domains=[approved_host], timeout_seconds=ROBOTS_TIMEOUT_SECONDS)

    def check(url: str) -> tuple[str, str]:
        with collectors._new_http_client(ROBOTS_TIMEOUT_SECONDS) as client:
            return collectors._robots_decision(client, source, url, None, ROBOTS_TIMEOUT_SECONDS)
    return check


def _check_url_shape(url: str, approved_url: str) -> str:
    """Offline URL checks (http/https, no credentials, same site, public IP literal); returns the final host."""
    from .collectors import _host_allowed, _is_forbidden_even_internal, _is_public
    if not isinstance(url, str) or not url:
        raise ValueError("Final URL must be a non-empty string")
    try:
        parsed = urlsplit(url)
        host = parsed.hostname
        _ = parsed.port
    except ValueError as exc:
        raise ValueError("Final URL is invalid") from exc
    if parsed.scheme.lower() not in {"http", "https"} or not host:
        raise ValueError("Final URL must be an http(s) URL with a host")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("Final URL must not contain credentials")
    approved_host = urlsplit(approved_url).hostname or ""
    address, approved_address = _ip_literal(host), _ip_literal(approved_host)
    if address is not None or approved_address is not None:
        same_site = host.lower() == approved_host.lower()
    else:
        same_site = _host_allowed(host, [approved_host])
    if not same_site:
        raise ValueError("Final URL host must stay on the approved page's host or its subdomains")
    if address is not None and (_is_forbidden_even_internal(address) or not _is_public(address)):
        raise ValueError("Final URL must not be a private, local or special-use IP address")
    return host.lower()


def check_host_capture(page: dict, approved_url: str, final_url: str | None = None, *,
                       robots_check: Callable[[str], tuple[str, str]] | None = None) -> dict:
    """Apply the collector's robots/domain/address policy to a host capture; raise ValueError on any violation.

    ``page`` is the job's coverage entry for ``approved_url``. Pages the program itself refused cannot be
    backfilled through a host browser. A final URL that differs from the approved URL (a redirect) must stay on
    the approved host and be allowed by robots.txt, as must a page with no robots allow on record. No DNS
    resolution happens here.
    """
    status = (page or {}).get("status")
    if status == "policy_denied":
        raise ValueError("Page was denied by collection policy (robots, domain or address) and cannot be backfilled with host-browser evidence")
    final = approved_url if final_url is None else final_url
    checks = ["page_not_policy_denied"]
    host = _check_url_shape(final, approved_url)
    checks += ["http_scheme", "no_credentials", "approved_host_scope", "public_address"]
    canonical_final, canonical_approved = canonical_url(final), canonical_url(approved_url)
    redirected = canonical_final != canonical_approved
    # Only a page response proves the collector's robots check passed. A page that
    # was never attempted, or failed before or at robots.txt, has no allow on record.
    pending = ([canonical_approved] if status not in ROBOTS_PROVEN_STATUSES else []) + ([canonical_final] if redirected else [])
    robots = "not_needed"
    for target in pending:
        decision, reason = (robots_check or _live_robots_check(urlsplit(approved_url).hostname or host))(target)
        if decision == "disallowed":
            raise ValueError("URL is disallowed by robots.txt; host-browser evidence cannot bypass robots policy")
        if decision != "allowed":
            raise ValueError(f"Robots policy could not be verified ({reason or decision}); redirected captures and pages without a recorded robots allow need a verifiable robots decision")
        robots = "allowed"
    if pending:
        checks.append("robots_allowed")
    # A host that omits final_url leaves redirects unchecked; record that rather
    # than assume the browser stayed on the approved URL.
    return {"approved_url": canonical_approved, "final_url": canonical_final, "redirected": redirected,
            "final_url_reported": final_url is not None, "robots": robots, "checks": checks}
