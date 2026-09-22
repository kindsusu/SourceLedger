from __future__ import annotations

from contextlib import contextmanager
from http.client import HTTPConnection
from pathlib import Path
import json
import threading

import pytest

from su_crawler import web_server


@contextmanager
def running_server(tmp_path, monkeypatch=None):
    server = web_server.build_web_server(tmp_path, port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def request(server, method, path, body=None, *, token=None, origin=None, headers=None):
    data = body if isinstance(body, bytes) else (None if body is None else json.dumps(body).encode("utf-8"))
    values = dict(headers or {})
    if data is not None:
        values.setdefault("Content-Type", "application/json")
        values["Content-Length"] = str(len(data))
    if token is not None:
        values["X-SourceLedger-Token"] = token
    if origin is not None:
        values["Origin"] = origin
    connection = HTTPConnection("127.0.0.1", server.server_port, timeout=3)
    connection.request(method, path, body=data, headers=values)
    response = connection.getresponse()
    raw = response.read()
    result = (response.status, dict(response.getheaders()), raw)
    connection.close()
    return result


def json_response(result):
    return result[0], json.loads(result[2].decode("utf-8"))


def authorized(server, method, path, body):
    return request(server, method, path, body, token=server.csrf_token, origin=server.url)


def test_bootstrap_and_onboarding_round_trip(tmp_path):
    with running_server(tmp_path) as server:
        status, bootstrap = json_response(request(server, "GET", "/api/bootstrap"))
        assert status == 200
        assert bootstrap["status"] == "needs_setup"
        assert bootstrap["workspace"] is None and bootstrap["research"] is None
        assert bootstrap["csrf_token"] == server.csrf_token
        assert bootstrap["workspace_root"] == str(tmp_path.resolve())

        status, created = json_response(authorized(server, "POST", "/api/workspace", {
            "industry": "Industrial tools", "product": "Torque wrench", "market": "Korea",
        }))
        assert status == 200 and created["workspace"]["locale"] == "en"
        status, product = json_response(authorized(server, "POST", "/api/product", {
            "identifiers": {"model": "TW-100"}, "required_specs": {"drive": "1/2 inch"},
        }))
        assert status == 200 and product["product"]["identifiers"] == {"model": "TW-100"}
        status, source = json_response(authorized(server, "POST", "/api/sources", {
            "url": "https://example.com/catalog", "name": "Catalog",
        }))
        assert status == 200 and source["source"]["location"] == "https://example.com/catalog"

        status, state = json_response(request(server, "GET", "/api/state"))
        assert status == 200 and state["status"] == "configured"
        assert state["workspace"]["product"]["identifiers"]["model"] == "TW-100"
        assert state["research"]["candidate_source_count"] == 1


def test_job_submission_prepares_before_publishing(tmp_path, monkeypatch):
    published = []

    def fake_prepare(root, operation, arguments):
        assert operation == "discover" and arguments == {"source_id": "source-1"}
        return {"source_id": "source-1", "workspace_fingerprint": "abc"}

    def fake_submit(root, operation, arguments):
        # The guard must have been released before the runtime job becomes visible.
        with web_server.workspace_guard(root):
            pass
        published.append((operation, arguments))
        return {"id": "job-1", "operation": operation, "arguments": arguments, "status": "queued"}

    monkeypatch.setattr(web_server, "prepare_job_args", fake_prepare)
    monkeypatch.setattr(web_server, "submit_job", fake_submit)
    monkeypatch.setattr(web_server, "runtime_status", lambda root: {"status": "stopped"})
    with running_server(tmp_path) as server:
        status, result = json_response(authorized(server, "POST", "/api/jobs", {
            "operation": "discover", "arguments": {"source_id": "source-1"},
        }))
    assert status == 200 and result["id"] == "job-1"
    assert published == [("discover", {"source_id": "source-1", "workspace_fingerprint": "abc"})]


@pytest.mark.parametrize("headers,token,origin", [
    ({"Host": "evil.example"}, "valid", "valid"),
    ({"Sec-Fetch-Site": "cross-site"}, "valid", "valid"),
    ({}, "wrong", "valid"),
    ({}, "valid", "http://evil.example"),
])
def test_post_rejects_cross_site_bad_host_and_bad_token(tmp_path, headers, token, origin):
    with running_server(tmp_path) as server:
        actual_token = server.csrf_token if token == "valid" else token
        actual_origin = server.url if origin == "valid" else origin
        status, value = json_response(request(server, "POST", "/api/workspace", {
            "industry": "I", "product": "P", "market": "M",
        }, token=actual_token, origin=actual_origin, headers=headers))
        assert status == 403 and "error" in value
        assert not (tmp_path / "research.json").exists()


def test_bad_json_types_unknown_fields_and_traversal_are_bounded(tmp_path):
    with running_server(tmp_path) as server:
        status, value = json_response(authorized(server, "POST", "/api/jobs", {
            "operation": "discover", "arguments": [],
        }))
        assert status == 400 and "object" in value["error"]
        status, value = json_response(authorized(server, "POST", "/api/worker", {
            "action": "stop", "unexpected": True,
        }))
        assert status == 400 and "Unknown fields" in value["error"]
        status, _, _ = request(server, "GET", "/../pyproject.toml")
        assert status == 404
        status, _, _ = request(server, "GET", "/%2e%2e/pyproject.toml")
        assert status == 404

        raw = b'{"operation":"agent","arguments":{"max_seconds":1e999}}'
        status, value = json_response(request(
            server, "POST", "/api/jobs", token=server.csrf_token, origin=server.url,
            headers={"Content-Type": "application/json"}, body=raw,
        ))
        assert status == 400 and "finite" in value["error"]


def test_invalid_content_type_with_body_returns_stable_closed_response(tmp_path):
    with running_server(tmp_path) as server:
        payload = b'{"industry":"I","product":"P","market":"M"}'
        status, headers, body = request(
            server, "POST", "/api/workspace", body=payload, token=server.csrf_token, origin=server.url,
            headers={"Content-Type": "text/plain"},
        )
    assert status == 400
    assert json.loads(body)["error"] == "Content-Type must be application/json"
    assert headers["Connection"] == "close"


def test_permission_error_after_json_read_does_not_read_body_twice(tmp_path, monkeypatch):
    def denied(self, path, value):
        raise PermissionError("denied after parsing")

    monkeypatch.setattr(web_server.SourceLedgerHandler, "_post_route", denied)
    with running_server(tmp_path) as server:
        status, headers, body = authorized(server, "POST", "/api/workspace", {
            "industry": "I", "product": "P", "market": "M",
        })
    assert status == 403
    assert json.loads(body)["error"] == "denied after parsing"
    assert headers["Connection"] == "close"


def test_connections_only_generates_workspace_snippet(tmp_path, monkeypatch):
    config_home = tmp_path / "codex-home"
    config_home.mkdir()
    config = config_home / "config.toml"
    config.write_text("model = \"keep-me\"\n", encoding="utf-8")
    monkeypatch.setenv("CODEX_HOME", str(config_home))
    monkeypatch.setattr("su_crawler.connections.importlib.util.find_spec", lambda name: object())

    with running_server(tmp_path / "workspace") as server:
        status, result = json_response(authorized(server, "POST", "/api/connections", {"client": "codex"}))
        assert status == 200
        assert "[mcp_servers.sourceledger]" in result["snippet"]
        snippet = Path(result["snippet_path"])
        assert snippet.parent == (tmp_path / "workspace" / "connections").resolve()
    assert config.read_text(encoding="utf-8") == "model = \"keep-me\"\n"


def test_report_download_uses_registered_xlsx_only(tmp_path, monkeypatch):
    report = tmp_path / "report.xlsx"
    report.write_bytes(b"xlsx fixture")
    monkeypatch.setattr(web_server, "get_job", lambda root, job_id: {"id": job_id, "status": "succeeded"})
    monkeypatch.setattr(web_server, "result_report", lambda root, job: {
        "path": str(report), "size": report.stat().st_size,
        "media_type": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    })
    with running_server(tmp_path) as server:
        status, headers, body = request(server, "GET", "/api/jobs/job-safe/report")
    assert status == 200 and body == b"xlsx fixture"
    assert headers["Content-Disposition"] == 'attachment; filename="job-safe.xlsx"'
    assert headers["X-Content-Type-Options"] == "nosniff"
