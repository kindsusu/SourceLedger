from __future__ import annotations

import json

import pytest

from su_crawler import model_provider


def config_file(tmp_path, **updates):
    value = {
        "provider": "ollama", "endpoint": "http://127.0.0.1:11434/api/chat",
        "model": "operator-selected", "timeout_seconds": 30,
        "max_input_chars": 30000, "num_predict": 1200, "local_only": True,
    }
    value.update(updates)
    path = tmp_path / "model.json"
    path.write_text(json.dumps(value), encoding="utf-8")
    return path


def test_config_rejects_non_loopback_and_unknown_fields(tmp_path, monkeypatch):
    with pytest.raises(ValueError, match="loopback"):
        model_provider.load_model_config(config_file(tmp_path, endpoint="http://model.example/api/chat"))
    path = config_file(tmp_path, extra=True)
    with pytest.raises(ValueError, match="fields"):
        model_provider.load_model_config(path)


def test_local_call_uses_structured_non_streaming_protocol_and_strips_secrets(tmp_path, monkeypatch):
    cfg = model_provider.load_model_config(config_file(tmp_path))
    captured = {}

    class Response:
        is_redirect = False
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def raise_for_status(self): pass
        def iter_bytes(self):
            yield json.dumps({"done": True, "message": {"content": json.dumps({"row_selector": ".item", "selectors": {"model": ".model", "price": ".price"}})}, "eval_count": 20}).encode()

    class Client:
        def __init__(self, **kwargs): captured["client"] = kwargs
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def stream(self, method, url, json):
            assert method == "POST"
            captured["url"], captured["payload"] = url, json
            return Response()

    monkeypatch.setattr(model_provider.httpx, "Client", Client)
    proposal, usage = model_provider.propose_with_ollama(
        '<script>steal()</script><input value="secret"><div data-token="x" class="model">A</div>',
        config=cfg, allowed_fields=["model", "price"], identifiers={"model": "A"}, timeout_seconds=5,
    )
    assert proposal["selectors"]["price"] == ".price"
    assert usage == {"eval_count": 20}
    assert captured["payload"]["stream"] is False
    assert captured["payload"]["format"]["additionalProperties"] is False
    sent = captured["payload"]["messages"][1]["content"]
    assert "steal" not in sent and "secret" not in sent and "data-token" not in sent
    assert "tools" not in captured["payload"]


def test_cloud_models_and_missing_local_attestation_are_rejected(tmp_path):
    with pytest.raises(ValueError, match="cloud"):
        model_provider.load_model_config(config_file(tmp_path, model="large-cloud-preview"))
    with pytest.raises(ValueError, match="local_only"):
        model_provider.load_model_config(config_file(tmp_path, local_only=False))
    with pytest.raises(ValueError, match="unauthenticated"):
        model_provider.load_model_config(config_file(tmp_path, endpoint="http://@127.0.0.1:11434/api/chat"))
    with pytest.raises(ValueError, match="port"):
        model_provider.load_model_config(config_file(tmp_path, endpoint="http://127.0.0.1:99999/api/chat"))


def test_model_response_rejects_extra_or_malicious_fields():
    with pytest.raises(ValueError):
        model_provider.validate_model_proposal(
            {"row_selector": None, "selectors": {"price": ".p"}, "recipe": [{"action": "click"}]},
            allowed_fields=["price"],
        )
    with pytest.raises(ValueError):
        model_provider.validate_model_proposal(
            {"row_selector": None, "selectors": {"url": "https://bad.example"}}, allowed_fields=["price"],
        )
    with pytest.raises(ValueError, match="forbidden"):
        model_provider.validate_model_proposal(
            {"row_selector": None, "selectors": {"price": "javascript:alert(1)"}}, allowed_fields=["price"],
        )
