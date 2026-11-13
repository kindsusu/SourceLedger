"""Offline wire and policy tests. Only the local server test opens sockets."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack, contextmanager, nullcontext
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import ipaddress
import json
import threading

import httpx
import pytest

from su_crawler import collectors, host_capture_policy, request_profile as rp
from su_crawler.models import Source

TARGET, OTHER = "shop.example.test", "other.example.test"
PROFILE = rp.RequestProfile(frozenset([TARGET]))
OVERRIDE = rp.RequestProfile(PROFILE.hosts, override_disallow_hosts=PROFILE.hosts)


def source(url, *, internal=False, domains=None, **settings):
    return Source("test", "test", "web", url, [], internal=internal,
                  allowed_domains=domains or [TARGET, OTHER], timeout_seconds=25, **settings)


@pytest.fixture
def config(tmp_path, monkeypatch):
    # Never depend on settings from the developer checkout.
    path = tmp_path / rp.LOCAL_FILE
    path.parent.mkdir()
    monkeypatch.delenv("SOURCELEDGER_REQUEST_PROFILE", raising=False)
    def save(value):
        path.write_text(json.dumps(value), encoding="utf-8")
        return rp.load_profile(tmp_path)
    return save


def test_config_scope_and_precedence(config, monkeypatch, tmp_path):
    profile = config({"hosts": [TARGET.upper()], "override_disallow_hosts": [TARGET]})
    assert profile == OVERRIDE
    assert not profile.selected("https://sub." + TARGET)
    assert not profile.selected("https://" + TARGET + ".example.test")
    assert not profile.selected("file://" + TARGET)
    assert not profile.selected("https://" + TARGET + "@" + OTHER)
    priority = tmp_path / "override.json"
    priority.write_text('{"hosts":[]}', encoding="utf-8")
    monkeypatch.setenv("SOURCELEDGER_REQUEST_PROFILE", str(priority))
    assert not rp.load_profile(tmp_path).hosts


@pytest.mark.parametrize("setting", [
    {"hosts": ["*.example.test"]}, {"hosts": ["https://shop.example.test"]},
    {"hosts": [TARGET], "override_disallow_hosts": [OTHER]},
    {"hosts": [TARGET], "user_agent": rp.DESKTOP_UA + " research"},
    {"hosts": [TARGET], "user_agent": rp.DESKTOP_UA + "\r\nX-Test: injected"},
])
def test_invalid_config_is_rejected(config, setting):
    with pytest.raises(ValueError):
        config(setting)


def public_dns(monkeypatch):
    monkeypatch.setattr(collectors, "_resolved_addresses", lambda *args: {ipaddress.ip_address("93.184.216.34")})


def client(handler):
    return httpx.Client(transport=httpx.MockTransport(handler), headers={"User-Agent": collectors.USER_AGENT})


@pytest.mark.parametrize("identity", ["source-ledger", "Mozilla", "*"])
def test_cached_disallow_and_explicit_override(monkeypatch, identity):
    public_dns(monkeypatch)
    seen = []
    def handler(req):
        seen.append((req.url.path, req.headers["user-agent"]))
        return httpx.Response(200, text=f"User-agent: {identity}\nDisallow: /private\n")
    url = f"https://{TARGET}/private"
    with client(handler) as c, collectors.robots_policy_scope():
        for _ in range(2):
            with pytest.raises(collectors.PolicyError, match="robots.txt"):
                collectors._get_following_policy(c, source(url), url, enforce_robots=True, profile=PROFILE)
        assert seen == [("/robots.txt", rp.DESKTOP_UA)]
        for _ in range(2):
            page, trace = collectors._get_following_policy(c, source(url), url, enforce_robots=True, profile=OVERRIDE)
            page.close()
            assert any(e["event"] == "robots_disallow_override" for e in trace)
        assert seen[1:] == [("/robots.txt", rp.DESKTOP_UA), ("/private", rp.DESKTOP_UA), ("/private", rp.DESKTOP_UA)]


@pytest.mark.parametrize("status", [302, 304, 401, 403, 429, 503])
def test_unavailable_robots_cannot_be_overridden(monkeypatch, status):
    public_dns(monkeypatch)
    seen = []
    def handler(req):
        seen.append(req.url.path)
        return httpx.Response(status)
    url = f"https://{TARGET}/private"
    with client(handler) as c, collectors.robots_policy_scope():
        for _ in range(2):
            with pytest.raises(collectors.RobotsUnavailable):
                collectors._get_following_policy(c, source(url), url, enforce_robots=False, profile=OVERRIDE)
    assert seen == ["/robots.txt"]


def test_rechecks_redirect_scope_and_preserves_shared_headers(monkeypatch):
    public_dns(monkeypatch)
    seen = []
    def handler(req):
        host, path = req.url.host, req.url.path
        seen.append((host, path, req.headers["user-agent"], req.headers.get("if-none-match")))
        other = OTHER if host == TARGET else TARGET
        if path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nAllow: /\n")
        if path == "/start":
            return httpx.Response(302, headers={"Location": f"https://{other}/end"})
        return httpx.Response(200)
    with client(handler) as c, ThreadPoolExecutor(max_workers=2) as executor:
        def fetch(host):
            url = f"https://{host}/start"
            page, _ = collectors._get_following_policy(c, source(url), url, headers={"If-None-Match":host},
                                                       enforce_robots=True, profile=PROFILE)
            page.close()
        list(executor.map(fetch, [TARGET, OTHER]))
        assert c.headers["User-Agent"] == collectors.USER_AGENT
    for host in [TARGET, OTHER]:
        rows = [(path, ua, tag) for h, path, ua, tag in seen if h == host]
        expected = rp.DESKTOP_UA if host == TARGET else collectors.USER_AGENT
        assert sorted(rows) == sorted([("/robots.txt", expected, None)] * 2 +
                                      [("/start", expected, host), ("/end", expected, None)])


def test_override_cannot_follow_to_denied_or_unapproved_host(monkeypatch):
    public_dns(monkeypatch)
    seen = []
    url = f"https://{TARGET}/private"
    def handler(req):
        seen.append((req.url.host, req.url.path))
        if req.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nDisallow: /private")
        return httpx.Response(302, headers={"Location": f"https://{OTHER}/private"})
    with client(handler) as c:
        for domains in [[TARGET, OTHER], [TARGET]]:
            seen.clear()
            with pytest.raises(collectors.PolicyError):
                collectors._get_following_policy(c, source(url, domains=domains), url, enforce_robots=True, profile=OVERRIDE)
            assert (OTHER, "/private") not in seen and ((OTHER, "/robots.txt") in seen) == (OTHER in domains)


def test_internal_and_host_browser_preflight(monkeypatch, config, tmp_path):
    public_dns(monkeypatch)
    config({"hosts": [TARGET], "override_disallow_hosts": [TARGET]})
    seen = []
    def handler(req):
        seen.append((req.url.path, req.headers["user-agent"]))
        return httpx.Response(200, text="User-agent: *\nDisallow: /private")
    monkeypatch.setattr(collectors, "_new_http_client", lambda _: client(handler))
    url = f"https://{TARGET}/private"
    assert host_capture_policy._live_robots_check(TARGET, tmp_path)(url) == ("allowed", rp.OVERRIDE_REASON)
    assert collectors.collect(source(url, internal=True), str(tmp_path), "http").status == "policy_denied"
    assert seen == [("/robots.txt", rp.DESKTOP_UA), ("/robots.txt", collectors.USER_AGENT)]


@contextmanager
def loopback(visited):
    links = {}
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            visited.append((self.path, self.headers["User-Agent"]))
            if self.path in links and self.path != "/page":
                self.send_response(302)
                self.send_header("Location", links[self.path])
                self.end_headers()
                return
            body = (b"User-agent: *\nDisallow: /start\n" if self.path == "/robots.txt" else
                    f'<html><body>ok<script src="{links["/page"]}"></script></body></html>'.encode()
                    if self.path == "/page" else b"void 0;")
            self.send_response(200)
            self.send_header("Content-Type", "text/html" if self.path == "/page" else "application/javascript" if self.path == "/asset.js" else "text/plain")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        def log_message(self, *args):
            pass
    srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    try:
        yield srv.server_port, links
    finally:
        srv.shutdown()
        srv.server_close()
        t.join(2)


@pytest.mark.parametrize("mode", ["http", "playwright", "persistent", "reuse", "crawl4ai"])
def test_wire_on_two_loopback_servers(monkeypatch, tmp_path, mode):
    if mode != "http":
        from su_crawler.browser_runtime import BrowserRuntime
        from su_crawler.doctor import doctor
        # Chromium otherwise blocks loopback subresources from intercepted pages.
        # This flag is confined to the synthetic server fixture.
        launch = BrowserRuntime.launch_options
        monkeypatch.setattr(BrowserRuntime, "launch_options", lambda runtime:
                            {**launch(runtime), "args": ["--disable-features=LocalNetworkAccessChecks"]})
        browser = next(b for b in doctor() if b["backend"] == "playwright")
        if browser["status"] != "available":
            pytest.skip(browser["reason"])
    monkeypatch.setenv("SOURCELEDGER_HEADLESS", "1")
    # Model two public origins without contacting one. The only admissible IP is loopback.
    monkeypatch.setattr(collectors, "_is_public", lambda a: a.is_loopback)
    profile = rp.RequestProfile(frozenset({"127.0.0.1"}), override_disallow_hosts=frozenset({"127.0.0.1"}))
    monkeypatch.setattr(collectors, "load_profile", lambda _: profile)
    chosen, ordinary = [], []
    with ExitStack() as stack:
        a, links = stack.enter_context(loopback(chosen))
        b, others = stack.enter_context(loopback(ordinary))
        # Selected -> unselected redirect -> selected page loading a third-party script.
        links["/start"] = f"http://localhost:{b}/redirect"
        others["/redirect"] = f"http://127.0.0.1:{a}/page"
        links["/page"] = f"http://localhost:{b}/asset.js"
        src = source(f"http://127.0.0.1:{a}/start", domains=["127.0.0.1", "localhost"],
                     profile_dir=str(tmp_path/"browser") if mode == "persistent" else None)
        backend = mode if mode in {"http", "crawl4ai"} else "playwright"
        with collectors.browser_session() if mode == "reuse" else nullcontext():
            result = collectors.collect(src, str(tmp_path), backend)
    assert result.status == "fetched", (result.message, result.trace)
    assert any(e["event"] == "robots_disallow_override" for e in result.trace)
    for path in ("/start", "/robots.txt", "/page"):
        assert (path, rp.DESKTOP_UA) in chosen
    assert ("/robots.txt", collectors.USER_AGENT) in ordinary
    if mode == "http":
        assert ("/redirect", collectors.USER_AGENT) in ordinary
    else:
        # Headless Chromium has a distinct native UA. The profile never rewrites subresources.
        ua = next(value for path, value in ordinary if path == "/redirect")
        asset_ua = next(value for path, value in ordinary if path == "/asset.js")
        assert "HeadlessChrome/" in ua and "HeadlessChrome/" in asset_ua
    if mode == "crawl4ai":
        assert result.backend == "playwright" and result.trace[0]["event"] == "scoped_browser_renderer"



def test_dedicated_catalog_browser_redirects_and_robots(tmp_path, monkeypatch):
    import asyncio
    pytest.importorskip('playwright')
    from playwright.async_api import async_playwright
    from su_crawler import amazoncar_collection as catalog
    from su_crawler.browser_runtime import select_browser_runtime

    selected, ordinary = [], []
    with ExitStack() as stack:
        a, links = stack.enter_context(loopback(selected))
        b, other_links = stack.enter_context(loopback(ordinary))
        base = f"http://127.0.0.1:{a}"
        monkeypatch.setattr(catalog, "BASE", base)
        links["/start"] = f"http://localhost:{b}/redirect"
        other_links["/redirect"] = base + "/done"
        prof = rp.RequestProfile(frozenset(["127.0.0.1"]))
        async def exercise():
            async with async_playwright() as pw:
                runtime = select_browser_runtime(pw.chromium)
                if not runtime:
                    pytest.skip("Chromium unavailable")
                browser = await pw.chromium.launch(headless=True, **runtime.launch_options())
                try:
                    page = await browser.new_page()
                    await catalog._profile_catalog_page(page, prof)
                    store = catalog.EvidenceStore(tmp_path)
                    with pytest.raises(RuntimeError, match="robots.txt disallows"):
                        await catalog.check_robots(page, store, ["/start"], prof)
                    opt = rp.RequestProfile(prof.hosts, override_disallow_hosts=prof.hosts)
                    await catalog.check_robots(page, store, ["/start"], opt)
                    assert json.loads((tmp_path/"robots.json").read_text("utf-8"))["disallow_overrides"] == ["/start"]
                    await page.goto(base + "/start")
                    assert page.url == base + "/done"
                finally:
                    await browser.close()
        asyncio.run(exercise())
    assert [ua for path, ua in selected if path == "/robots.txt"] == [rp.DESKTOP_UA] * 2
    assert ("/start", rp.DESKTOP_UA) in selected and ("/done", rp.DESKTOP_UA) in selected
    assert any(path == "/redirect" and "HeadlessChrome/" in ua for path, ua in ordinary)



def test_redirected_robots_uses_destination_headers_and_original_scope(monkeypatch):
    public_dns(monkeypatch)
    seen = []
    def handler(req):
        seen.append((req.url.host, req.url.path, req.headers["user-agent"]))
        if req.url.host == TARGET:
            return httpx.Response(302, headers={"Location": f"https://{OTHER}/policy.txt"})
        return httpx.Response(200, text="User-agent: source-ledger\nDisallow: /catalog")
    url = f"https://{TARGET}/catalog"
    with client(handler) as c:
        with pytest.raises(collectors.PolicyError):
            collectors._get_following_policy(c, source(url), url, enforce_robots=True, profile=PROFILE)
    assert seen == [(TARGET, "/robots.txt", rp.DESKTOP_UA), (OTHER, "/policy.txt", collectors.USER_AGENT)]


@pytest.mark.parametrize(("code", "expected"), [(401, "needs_auth"), (403, "blocked"), (429, "blocked")])
def test_disallow_override_does_not_retry_page_access_errors(monkeypatch, config, tmp_path, code, expected):
    public_dns(monkeypatch)
    config({"hosts": [TARGET], "override_disallow_hosts": [TARGET]})
    seen = []
    def handler(req):
        seen.append(req.url.path)
        return (httpx.Response(200, text="User-agent: *\nDisallow: /") if req.url.path == "/robots.txt"
                else httpx.Response(code))
    monkeypatch.setattr(collectors, "_new_http_client", lambda _: client(handler))
    result = collectors.collect(source(f"https://{TARGET}/private"), str(tmp_path), "http")
    assert result.status == expected and seen == ["/robots.txt", "/private"]
