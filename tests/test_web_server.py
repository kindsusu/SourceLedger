from __future__ import annotations

from contextlib import contextmanager
from concurrent.futures import ThreadPoolExecutor
from http.client import HTTPConnection
from pathlib import Path
import json
import threading

import pytest

from su_crawler import web_server
from su_crawler.research import _locked


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


def request(server, method, path, body=None, *, token=None, origin=None, headers=None, raw_headers=False):
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
    response_headers = response.getheaders()
    result = (response.status, response_headers if raw_headers else dict(response_headers), raw)
    connection.close()
    return result


def json_response(result):
    return result[0], json.loads(result[2].decode("utf-8"))


def authorized(server, method, path, body):
    return request(server, method, path, body, token=server.csrf_token, origin=server.url)


def test_worker_start_retries_real_start_lock_contention(tmp_path, monkeypatch):
    start = tmp_path / "runtime" / "start"
    entered = threading.Event()
    attempts = []

    def start_while_reading(root):
        attempts.append(1)
        try:
            with _locked(start):
                return {"status": "idle", "pid": 1, "current_job_id": None}
        except RuntimeError:
            entered.set()
            raise

    monkeypatch.setattr(web_server, "start_worker", start_while_reading)
    with running_server(tmp_path) as server, ThreadPoolExecutor(max_workers=1) as pool:
        with _locked(start):
            future = pool.submit(authorized, server, "POST", "/api/worker", {"action": "start"})
            assert entered.wait(2)
        status, value = json_response(future.result(timeout=3))
    assert status == 200 and value["status"] == "idle"
    assert len(attempts) >= 2


@pytest.mark.parametrize("transient,expected_attempts", [(True, 8), (False, 1)])
def test_worker_start_retry_is_bounded_and_other_errors_propagate(tmp_path, monkeypatch, transient, expected_attempts):
    attempts = []
    message = (f"Workspace is busy: {tmp_path / 'runtime' / 'start'}" if transient
               else "Assistant worker exited before it became ready")

    def failing_start(root):
        attempts.append(1)
        raise RuntimeError(message)

    monkeypatch.setattr(web_server, "start_worker", failing_start)
    monkeypatch.setattr(web_server.time, "sleep", lambda _delay: None)
    with running_server(tmp_path) as server:
        status, value = json_response(authorized(server, "POST", "/api/worker", {"action": "start"}))
    assert status == 409 and value["error"] == message
    assert len(attempts) == expected_attempts


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


def test_active_ui_port_cannot_be_reused_by_another_server(tmp_path):
    with running_server(tmp_path / "first") as server:
        with pytest.raises(OSError):
            web_server.build_web_server(tmp_path / "second", port=server.server_port)
        status, state = json_response(request(server, "GET", "/api/bootstrap"))
        assert status == 200 and state["workspace_root"] == str((tmp_path / "first").resolve())


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


def test_recommendation_review_selects_only_checked_sources(tmp_path):
    from su_crawler.recommendations import submit_recommendations

    with running_server(tmp_path) as server:
        assert json_response(authorized(server, "POST", "/api/workspace", {
            "industry": "Fixtures", "product": "Parts", "market": "Offline",
        }))[0] == 200
        status, created = json_response(authorized(server, "POST", "/api/recommendations", {
            "query": "Fixture suppliers", "kind": "keyword",
        }))
        assert status == 200
        request_id = created["request"]["id"]
        candidates = [{
            "name": name, "url": f"https://{name}.example/catalog", "reason": "Catalog matches the requested topic",
            "evidence_url": f"https://{name}.example/about",
        } for name in ("first", "second")]
        staged = submit_recommendations(tmp_path, request_id=request_id, candidates=candidates)
        _, before = json_response(request(server, "GET", "/api/bootstrap"))
        assert before["workspace"]["sources"] == []
        assert before["jobs"] == []
        assert len(before["recommendations"]["requests"][0]["candidates"]) == 2

        chosen = staged["request"]["candidates"][1]["id"]
        body = {"request_id": request_id, "candidate_ids": [chosen]}
        # State-changing review requires the same CSRF protection as all other UI writes.
        assert request(server, "POST", "/api/recommendations/select", body)[0] == 403
        status, selected = json_response(authorized(server, "POST", "/api/recommendations/select", body))
        assert status == 200 and selected["added_count"] == 1
        assert json_response(authorized(server, "POST", "/api/recommendations/select", body))[1]["added_count"] == 0
        _, after = json_response(request(server, "GET", "/api/bootstrap"))
        assert [s["location"] for s in after["workspace"]["sources"]] == [candidates[1]["url"]]
        assert after["jobs"] == []

        # Selecting research targets is not a collection instruction. A later explicit
        # run pins only the chosen source IDs, not the unselected recommendations.
        assert authorized(server, "POST", "/api/product", {"identifiers": {"model": "PART-1"}})[0] == 200
        status, queued = json_response(authorized(server, "POST", "/api/jobs", {
            "operation": "agent", "arguments": {"source_ids": selected["source_ids"], "max_sources": 1},
        }))
        assert status == 200 and queued["status"] == "queued"
        assert queued["arguments"]["source_ids"] == selected["source_ids"]
        assert queued["arguments"]["max_sources"] == 1


def test_recommendation_routes_reject_invalid_payloads(tmp_path):
    with running_server(tmp_path) as server:
        authorized(server, "POST", "/api/workspace", {"industry": "I", "product": "P", "market": "M"})
        for payload in ({"query": ""}, {"query": "Q", "kind": "unknown"}, {"query": "Q", "extra": True}):
            assert authorized(server, "POST", "/api/recommendations", payload)[0] == 400
        result = json_response(authorized(server, "POST", "/api/recommendations", {"query": "Company", "kind": "company"}))[1]
        for ids in ([], "not-a-list", ["unknown"]):
            assert authorized(server, "POST", "/api/recommendations/select", {
                "request_id": result["request"]["id"], "candidate_ids": ids,
            })[0] == 400


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


def test_dashboard_route_serves_only_registered_html_and_links_report(tmp_path, monkeypatch):
    report = tmp_path / "final.xlsx"
    report.write_bytes(b"xlsx fixture")
    dashboard = tmp_path / "final.html"
    dashboard.write_text('<a href="report">Download XLSX</a>', encoding="utf-8")
    job = {"result": {"report_path": str(report), "dashboard_path": str(dashboard)}}
    monkeypatch.setattr(web_server, "get_job", lambda root, job_id: job)
    with running_server(tmp_path) as server:
        status, headers, body = request(server, "GET", "/api/jobs/job-safe/dashboard", raw_headers=True)
        assert status == 200
        policies = [value for name, value in headers if name.lower() == "content-security-policy"]
        assert len(policies) == 1
        assert "style-src 'unsafe-inline'" in policies[0]
        assert "default-src 'none'" in policies[0]
        headers = dict(headers)
        assert headers["Content-Type"] == "text/html; charset=utf-8"
        assert b'href="report"' in body
        assert "script-src" not in headers["Content-Security-Policy"]
        status, normal_headers, body = request(server, "GET", "/api/jobs/job-safe/report", raw_headers=True)
        assert status == 200 and body == b"xlsx fixture"
        normal_policies = [value for name, value in normal_headers if name.lower() == "content-security-policy"]
        assert len(normal_policies) == 1 and "style-src 'self'" in normal_policies[0]
        job["result"]["dashboard_path"] = str(tmp_path / "unregistered.html")
        status, _, _ = request(server, "GET", "/api/jobs/job-safe/dashboard")
        assert status == 400


def test_generated_dashboard_relative_workbook_link_resolves(tmp_path, monkeypatch):
    import re
    from html import unescape
    from urllib.parse import urljoin
    from openpyxl import Workbook
    from su_crawler.final_report import finalize_reports

    report = tmp_path / "final report.xlsx"
    workbook = Workbook()
    workbook.active.title = "Results"
    workbook.active.append(["Product", "Source URL"])
    workbook.active.append(["Synthetic item", "https://synthetic.example/item"])
    workbook.save(report)
    workbook.close()
    artifacts = finalize_reports([report], report)
    job = {"result": {key: str(artifacts[key]) for key in ("report_path", "dashboard_path")}}
    monkeypatch.setattr(web_server, "get_job", lambda root, job_id: job)
    with running_server(tmp_path) as server:
        dashboard_url = "/api/jobs/job-safe/dashboard"
        status, _, body = request(server, "GET", dashboard_url)
        assert status == 200
        href = unescape(re.search(rb"href='([^']+\.xlsx)'", body).group(1).decode("utf-8"))
        status, _, content = request(server, "GET", urljoin(dashboard_url, href))
        assert status == 200 and content == report.read_bytes()



def test_bootstrap_reads_do_not_need_the_workspace_lock(tmp_path):
    """A long job holds the workspace lock; the page refresh must still load."""
    from su_crawler import research_plans
    from su_crawler.ai_providers import get_ai_configuration
    from su_crawler import research
    from su_crawler.assistant_workspace import workspace_guard
    from su_crawler.recommendations import list_recommendations
    research.init_workspace(tmp_path / "research.json", industry="Retail", product="Lamps", market="KR")
    plan = research_plans.create_plan(tmp_path, "Lamps")["plan"]
    with workspace_guard(tmp_path):
        assert list_recommendations(tmp_path) == {"requests": []}
        assert get_ai_configuration(tmp_path)["settings"]
        assert [item["id"] for item in research_plans.list_plans(tmp_path)["plans"]] == [plan["id"]]
