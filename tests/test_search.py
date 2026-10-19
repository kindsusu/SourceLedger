from __future__ import annotations

import json
import httpx
import pytest

from su_crawler import research, search


REAL_CLIENT = httpx.Client


def workspace(tmp_path):
    path = tmp_path / "research.json"
    research.init_workspace(path, industry="Pumps", product="Process pump", market="Korea")
    research.set_product(path, identifiers={"model": "PX-100"})
    return path


def provider(tmp_path, **values):
    value = {"provider": "searxng", "endpoint": "http://127.0.0.1:8080/search", "allow_private_endpoint": True}
    value.update(values)
    path = tmp_path / "searxng.json"
    path.write_text(json.dumps(value), encoding="utf-8")
    return path


def response(results):
    return httpx.Response(200, json={"results": results})


def test_unconfigured_is_offline_and_does_not_mutate(tmp_path, monkeypatch):
    path = workspace(tmp_path)
    monkeypatch.setattr(search.httpx, "Client", lambda *a, **k: pytest.fail("network"))
    assert search.search_workspace(path) == {"status": "search_provider_unconfigured", "network_calls": 0, "added": [], "added_count": 0, "candidate_ids": []}
    assert research.load_workspace(path)["sources"] == []


@pytest.mark.parametrize("contents", [
    "not-json", json.dumps({"provider": "other", "endpoint": "https://search.example/search", "allow_private_endpoint": False}),
    json.dumps({"provider": "searxng", "endpoint": "http://search.example/search", "allow_private_endpoint": False}),
    json.dumps({"provider": "searxng", "endpoint": "https://search.example/search", "allow_private_endpoint": "yes"}),
    json.dumps({"provider": "searxng", "endpoint": "https://search.example/search", "allow_private_endpoint": False, "unknown": 1}),
])
def test_invalid_provider_configuration_never_calls_network(tmp_path, monkeypatch, contents):
    path = workspace(tmp_path)
    config = tmp_path / "invalid.json"
    config.write_text(contents, encoding="utf-8")
    monkeypatch.setattr(search.httpx, "Client", lambda *a, **k: pytest.fail("network"))
    with pytest.raises(ValueError):
        search.search_workspace(path, provider_path=config)
    assert research.load_workspace(path)["sources"] == []


def test_result_url_parser_rejects_bad_ipv6_and_empty_userinfo():
    assert search._public_url("https://[bad/x", []) is None
    assert search._public_url("https://@vendor.example/x", []) is None
    assert search._public_url("https://vendor example/x", []) is None


def test_endpoint_rejects_query_and_dns_private_without_opt_in(tmp_path, monkeypatch):
    monkeypatch.setattr(search.socket, "getaddrinfo", lambda *a, **k: [(None, None, None, None, ("10.0.0.1", 0))])
    config = provider(tmp_path, endpoint="https://search.example/search", allow_private_endpoint=False)
    with pytest.raises(ValueError, match="Private"):
        search._config(config)
    config.write_text(json.dumps({"provider": "searxng", "endpoint": "https://search.example/search?token=no", "allow_private_endpoint": False}), encoding="utf-8")
    with pytest.raises(ValueError):
        search._config(config)


def test_search_records_public_candidates_and_no_price(tmp_path, monkeypatch):
    path = workspace(tmp_path)
    calls = []
    transport = httpx.MockTransport(lambda request: (calls.append(request), response([
        {"url": "https://vendor.example/pump#offers", "title": "Vendor pump", "content": "$12"},
        {"url": "https://vendor.example/pump#other", "title": "duplicate"},
    ]))[1])
    monkeypatch.setattr(search.httpx, "Client", lambda **kwargs: REAL_CLIENT(transport=transport, **kwargs))
    result = search.search_workspace(path, provider_path=provider(tmp_path), limit=10)
    assert result["status"] == "searched" and result["added_count"] == 1 and result["network_calls"] == 1
    assert calls[0].url.params["q"] == "Pumps Process pump Korea PX-100"
    assert calls[0].url.params["format"] == "json"
    source = research.load_workspace(path)["sources"][0]
    assert source["location"] == "https://vendor.example/pump"
    assert source["scope"] == "public" and "price" not in source
    assert source["provenance"]["snippet"] == "$12"


@pytest.mark.parametrize("status,expected", [(403, "provider_unavailable"), (500, "failed")])
def test_provider_failures_are_honest(tmp_path, monkeypatch, status, expected):
    transport = httpx.MockTransport(lambda request: httpx.Response(status))
    monkeypatch.setattr(search.httpx, "Client", lambda **kwargs: REAL_CLIENT(transport=transport, **kwargs))
    result = search.search_workspace(workspace(tmp_path), provider_path=provider(tmp_path))
    assert result["status"] == expected and result["network_calls"] == 1


def test_filters_private_credentials_controls_and_domains(tmp_path, monkeypatch):
    transport = httpx.MockTransport(lambda request: response([
        {"url": "http://localhost/x"}, {"url": "http://10.0.0.1/x"}, {"url": "https://u:p@ok.example/x"},
        {"url": "https://bad.example/a\\b"}, {"url": "https://else.example/x"}, {"url": "https://ok.example/x", "title": "OK"},
    ]))
    monkeypatch.setattr(search.httpx, "Client", lambda **kwargs: REAL_CLIENT(transport=transport, **kwargs))
    result = search.search_workspace(workspace(tmp_path), provider_path=provider(tmp_path, allowed_result_domains=["ok.example"]))
    assert result["added_count"] == 1
    assert research.load_workspace(tmp_path / "research.json")["sources"][0]["location"] == "https://ok.example/x"


def test_existing_internal_is_preserved_and_not_returned(tmp_path, monkeypatch):
    path = workspace(tmp_path)
    research.add_source(path, url="https://vendor.example/p", scope="internal")
    transport = httpx.MockTransport(lambda request: response([{"url": "https://vendor.example/p"}]))
    monkeypatch.setattr(search.httpx, "Client", lambda **kwargs: REAL_CLIENT(transport=transport, **kwargs))
    result = search.search_workspace(path, provider_path=provider(tmp_path))
    assert result["added_count"] == 0 and result["candidate_ids"] == []
    assert research.load_workspace(path)["sources"][0]["scope"] == "internal"


def test_malformed_timeout_size_and_no_redirects(tmp_path, monkeypatch):
    path = workspace(tmp_path)
    class TimeoutClient:
        def __init__(self, **kwargs): self.kwargs = kwargs
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def stream(self, *args, **kwargs): raise httpx.TimeoutException("x")
    monkeypatch.setattr(search.httpx, "Client", TimeoutClient)
    assert search.search_workspace(path, provider_path=provider(tmp_path))["status"] == "timeout"
    with pytest.raises(ValueError): search.search_workspace(path, provider_path=provider(tmp_path, unexpected=True))
    assert search._public_url("https://a.example/x", []) == "https://a.example/x"
