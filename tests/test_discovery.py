from su_crawler.discovery import discover
from su_crawler.models import Source


def test_sitemap_candidates_are_bounded_and_not_prices(tmp_path):
    path = tmp_path / "sitemap.xml"
    path.write_text('<?xml version="1.0"?><urlset><url><loc>https://example.com/a</loc></url><url><loc>https://other.com/b</loc></url><url><loc>https://example.com/c</loc></url></urlset>')
    source = Source("s", "s", "file", str(path), ["p"], allowed_domains=["example.com"])
    result = discover(source, str(tmp_path), limit=1)
    assert result["urls"] == ["https://example.com/a"]
    assert result["limit_reached"]
    assert "price" not in result


def test_blocked_http_uses_browser_and_keeps_allowlist(monkeypatch):
    from su_crawler.models import FetchResult
    calls = []

    def collect(source, base_dir, backend):
        calls.append(backend)
        assert source.recipe == []
        if backend == "http":
            return FetchResult(source.id, "blocked", backend, message="403")
        return FetchResult(source.id, "fetched", backend, final_url=source.location,
                           content=b'<a href="/item">Product</a><a href="https://other.com/item">Other</a>')

    monkeypatch.setattr("su_crawler.discovery.collect", collect)
    source = Source("s", "s", "web", "https://example.com/catalog", ["p"], allowed_domains=["example.com"],
                    recipe=[{"action": "click", "selector": "#buy"}])
    result = discover(source, ".")
    assert calls == ["http", "playwright"]
    assert result["urls"] == ["https://example.com/item"]
    assert result["backend"] == "playwright"


def test_policy_denial_does_not_try_another_backend(monkeypatch):
    from su_crawler.models import FetchResult
    calls = []

    def collect(source, base_dir, backend):
        calls.append(backend)
        return FetchResult(source.id, "policy_denied", backend, message="robots.txt")

    monkeypatch.setattr("su_crawler.discovery.collect", collect)
    source = Source("s", "s", "web", "https://example.com/catalog", ["p"], allowed_domains=["example.com"])
    result = discover(source, ".")
    assert calls == ["http"]
    assert result["status"] == "policy_denied"
