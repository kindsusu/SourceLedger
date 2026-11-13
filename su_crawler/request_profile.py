"""Local, exact-host request identities. Profiles do not grant network access."""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit


# Standard desktop Chrome wire format (also usable by HTTP-only installations).
DESKTOP_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/157.0.0.0 Safari/537.36")
_BROWSER = re.compile(
    r"Mozilla/5\.0 \((?:Windows NT [\d.]+; Win64; x64|X11; Linux x86_64|Macintosh; Intel Mac OS X [\d_]+)\) "
    r"AppleWebKit/537\.36 \(KHTML, like Gecko\) Chrome/[\d.]+ Safari/537\.36(?: Edg/[\d.]+)?"
)
_DNS_LABEL = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?")
OVERRIDE_REASON = "local exact-host override of robots.txt Disallow"
LOCAL_FILE = Path(".sourceledger") / "request-profile.json"


def _hosts(values: object) -> frozenset[str]:
    if not isinstance(values, list):
        raise ValueError("Expected a list of exact DNS hostnames")
    result = set()
    for value in values:
        host = value.lower() if isinstance(value, str) else ""
        if (len(host) > 253 or "." not in host or not all(_DNS_LABEL.fullmatch(s) for s in host.split("."))
                or host.rsplit(".", 1)[1].isdigit()):
            raise ValueError("Use exact DNS hostnames, without wildcard, port or URL")
        result.add(host)
    return frozenset(result)


@dataclass(frozen=True)
class RequestProfile:
    hosts: frozenset[str] = frozenset()
    user_agent: str = DESKTOP_UA
    override_disallow_hosts: frozenset[str] = frozenset()

    def selected(self, url: str) -> bool:
        parts = urlsplit(url)
        return parts.scheme in {"http", "https"} and parts.hostname in self.hosts

    def robots(self, parser, url: str, previous: str) -> tuple[str, str]:
        identities = (previous,)
        if self.selected(url):
            identities = (*identities, "source-ledger", self.user_agent)
        if all(parser.can_fetch(identity, url) for identity in identities):
            return "allowed", "robots.txt allows collection"
        if self.selected(url) and urlsplit(url).hostname in self.override_disallow_hosts:
            return "allowed", OVERRIDE_REASON
        return "disallowed", "robots.txt disallows collection"


def load_profile(base_dir: str | Path | None = None) -> RequestProfile:
    """An explicit file wins over workspace and checkout settings. Never merge them."""
    explicit = os.environ.get("SOURCELEDGER_REQUEST_PROFILE")
    repo_file = Path(__file__).resolve().parents[1] / LOCAL_FILE
    workspace_file = Path(base_dir) / LOCAL_FILE if base_dir is not None else None
    path = Path(explicit) if explicit else (
        workspace_file if workspace_file is not None and workspace_file.is_file() else repo_file)
    if explicit and not path.is_absolute():
        raise ValueError("SOURCELEDGER_REQUEST_PROFILE must be an absolute path")
    defaults = frozenset()
    if not explicit and not path.is_file():
        return RequestProfile(defaults)
    raw = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(raw, dict) or raw.keys() - {"hosts", "user_agent", "override_disallow_hosts"}:
        raise ValueError("Invalid request-profile.json fields")
    hosts = _hosts(raw.get("hosts", sorted(defaults)))
    override = _hosts(raw.get("override_disallow_hosts", []))
    ua = raw.get("user_agent", DESKTOP_UA)
    if not isinstance(ua, str) or len(ua) > 256 or not _BROWSER.fullmatch(ua):
        raise ValueError("Expected an unbranded desktop Chrome or Edge User-Agent")
    if not override <= hosts:
        raise ValueError("Robots overrides must be a subset of selected hosts")
    return RequestProfile(hosts, ua, override)
