"""Compact MCP responses, URL-file import, unattended CLI runs and batch follow-up.

All data is synthetic; collection uses a local HTTP fixture server or a patched
collector boundary. No live website is contacted.
"""
from __future__ import annotations

import asyncio
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import threading
import urllib.request
from urllib.parse import urlsplit

import pytest

pytest.importorskip("mcp")

from su_crawler import assistant_runtime as runtime, catalog_collection, research_plans as plans
from su_crawler.assistant_server import build_assistant_server
from su_crawler.cli import main
from su_crawler.models import FetchResult
from mcp_budget_fixtures import synthetic_batch_job, synthetic_urls

BUDGET = 3000


def _call(server, name, arguments):
    return asyncio.run(server.call_tool(name, arguments))[1]


def _size(value) -> int:
    return len(json.dumps(value, ensure_ascii=False))


@pytest.mark.parametrize("count", [20, 525])
def test_default_responses_stay_within_budget(tmp_path, count):
    data = synthetic_batch_job(tmp_path, count)
    plan, job_id = data["plan"], data["job"]["id"]
    server = build_assistant_server(tmp_path)
    budgets = {
        "get_research_batch_progress": {"job_id": job_id},
        "get_research_progress": {"job_id": job_id},
        "get_research_batch_reports": {"job_id": job_id},
        "get_research_plan": {"plan_id": plan["id"]},
        "list_research_plans": {},
        "get_workspace_status": {},
        "get_job_status": {"job_id": job_id},
        "list_recent_jobs": {},
        "wait_for_job": {"job_id": job_id, "timeout_seconds": 1},
        "get_research_batch_execution_plan": {"plan_id": plan["id"], "expected_revision": plan["revision"]},
    }
    for name, arguments in budgets.items():
        response = _call(server, name, arguments)
        assert _size(response) < BUDGET, (name, _size(response))
    progress = _call(server, "get_research_batch_progress", {"job_id": job_id})
    assert progress["target_count"] == count and progress["completed_batches"] == progress["batch_count"]
    assert sum(progress["coverage_counts"].values()) == count
    assert progress["refresh"]["item_changes_total"] > 0 and "item_changes" not in progress
    assert progress["url_fingerprint"] == plans.url_fingerprint(synthetic_urls(count))
    reports = _call(server, "get_research_batch_reports", {"job_id": job_id})
    assert reports["final_report_path"].endswith(".xlsx") and reports["dashboard_path"].endswith(".html")
    assert len(reports["batches"]) == min(10, reports["total"])
    assert all("refresh" not in item for item in reports["batches"])
    summary = _call(server, "get_research_plan", {"plan_id": plan["id"]})["plan"]
    assert summary["counts"]["selected"] == count and "candidates" not in summary
    assert sum(summary["host_counts"].values()) == count
    status = _call(server, "get_job_status", {"job_id": job_id})
    assert "result" not in status and "arguments" not in status
    assert status["result_summary"]["target_count"] == count
    full = _call(server, "get_job_status", {"job_id": job_id, "include_result": True, "include_arguments": True})
    assert full["result"]["result"]["target_count"] == count and full["arguments"]["plan_id"] == plan["id"]


def test_plan_candidates_are_paged_only_on_request(tmp_path):
    plan = plans.create_plan(tmp_path, "Synthetic paging request")["plan"]
    server = build_assistant_server(tmp_path)
    urls = synthetic_urls(45)
    updated = _call(server, "update_research_plan", {
        "plan_id": plan["id"], "expected_revision": plan["revision"],
        "changes": {"added_candidates": [{"url": url} for url in urls]}})
    assert "candidates" not in updated and updated["plan"]["counts"]["candidates"] == 45
    page = _call(server, "get_research_plan", {"plan_id": plan["id"], "include_candidates": True,
                                               "offset": 40, "limit": 20})
    assert [item["url"] for item in page["candidates"]] == urls[40:]
    assert page["candidate_total"] == 45 and page["candidate_has_more"] is False


def test_import_plan_urls_preserves_order_and_reports_problems(tmp_path):
    server = build_assistant_server(tmp_path)
    plan = _call(server, "create_research_plan", {"request_text": "Synthetic import request"})["plan"]
    (tmp_path / "lists").mkdir()
    lines = ["# operator list", "https://beta-lease.example/catalog/sub0301",
             "https://ALPHA-rent.example/catalog/sub0201#top", "", "not a url",
             "https://beta-lease.example/catalog/sub0301", "http://localhost/private",
             "https://gamma-cars.example/catalog/sub0001", "https://gamma-cars.example/catalog/sub0002"]
    (tmp_path / "lists" / "urls.txt").write_text("\n".join(lines), encoding="utf-8")
    result = _call(server, "import_plan_urls", {"plan_id": plan["id"], "expected_revision": plan["revision"],
                                                "path": "lists/urls.txt"})
    expected = ["https://beta-lease.example/catalog/sub0301", "https://alpha-rent.example/catalog/sub0201",
                "https://gamma-cars.example/catalog/sub0001", "https://gamma-cars.example/catalog/sub0002"]
    imported = result["import"]
    assert imported["count"] == 4 and imported["added"] == 4
    assert imported["first_urls"] == expected[:3] and imported["last_urls"] == expected[-3:]
    assert imported["duplicate_count"] == 1 and imported["duplicates"][0]["location"] == "line 6"
    assert imported["invalid_count"] == 2 and {item["location"] for item in imported["invalid"]} == {"line 5", "line 7"}
    assert imported["file_url_fingerprint"] == plans.url_fingerprint(expected)
    assert result["plan"]["url_fingerprint"] == plans.url_fingerprint(expected)
    stored = plans.get_plan(tmp_path, plan["id"])["plan"]
    assert [item["url"] for item in stored["candidates"]] == expected
    assert _size(result) < BUDGET

    csv_file = tmp_path / "urls.csv"
    csv_file.write_text("name,URL\nB,https://delta-mobility.example/a\nA,https://alpha-rent.example/catalog/sub0201\n",
                        encoding="utf-8")
    replaced = _call(server, "import_plan_urls", {"plan_id": plan["id"], "expected_revision": stored["revision"],
                                                  "path": str(csv_file), "replace": True})
    assert replaced["import"]["already_present"] == 1 and replaced["import"]["removed"] == 3
    stored = plans.get_plan(tmp_path, plan["id"])["plan"]
    assert [item["url"] for item in stored["candidates"]] == ["https://delta-mobility.example/a",
                                                             "https://alpha-rent.example/catalog/sub0201"]
    for document in (["https://x.example/1", "https://x.example/2"], {"urls": ["https://x.example/1", "https://x.example/2"]}):
        path = tmp_path / "urls.json"
        path.write_text(json.dumps(document), encoding="utf-8")
        assert plans.parse_url_file(path)["urls"] == ["https://x.example/1", "https://x.example/2"]
    (tmp_path / "bad.csv").write_text("link\nhttps://x.example/1\n", encoding="utf-8")
    with pytest.raises(ValueError, match="url"):
        plans.parse_url_file(tmp_path / "bad.csv")
    with pytest.raises(Exception, match="inside the assistant workspace"):
        _call(server, "import_plan_urls", {"plan_id": plan["id"], "expected_revision": stored["revision"],
                                           "path": "../outside.txt"})


def test_import_then_confirm_matches_batch_execution_pin(tmp_path):
    server = build_assistant_server(tmp_path)
    plan = _call(server, "create_research_plan", {"request_text": "Synthetic pinned import"})["plan"]
    urls = synthetic_urls(30)
    (tmp_path / "urls.txt").write_text("\n".join(urls), encoding="utf-8")
    imported = _call(server, "import_plan_urls", {"plan_id": plan["id"], "expected_revision": plan["revision"],
                                                  "path": "urls.txt"})["plan"]
    topic = _call(server, "update_research_plan", {"plan_id": plan["id"], "expected_revision": imported["revision"],
                                                   "changes": {"topic": {"industry": "Rental", "product": "Cars",
                                                                         "market": "Testland"}}})["plan"]
    confirmed = _call(server, "confirm_research_plan", {"plan_id": plan["id"], "expected_revision": topic["revision"],
                                                        "user_confirmed": True})["plan"]
    brief = _call(server, "get_research_batch_execution_plan", {"plan_id": plan["id"],
                                                                "expected_revision": confirmed["revision"]})
    assert brief["url_fingerprint"] == confirmed["url_fingerprint"] == plans.url_fingerprint(urls)
    assert brief["first_urls"] == urls[:3] and brief["last_urls"] == urls[-3:]


class _FixtureSite:
    """Serve synthetic product pages on 127.0.0.1 and route plan hosts to it."""

    def __init__(self):
        self.requests = []
        site = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                site.requests.append(self.path)
                number = self.path.rsplit("/", 1)[-1]
                product = {"@type": "Product", "name": f"Synthetic Item {number}", "sku": f"SYN-{number}",
                           "offers": {"@type": "Offer", "price": f"{10 + len(number)}.50", "priceCurrency": "USD"}}
                body = f'<html><script type="application/ld+json">{json.dumps(product)}</script></html>'.encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    def fetch(self, source, base_dir, backend):
        # Plan URLs must be public; the fixture boundary maps their path to the local server.
        path = urlsplit(source.location).path
        with urllib.request.urlopen(f"http://127.0.0.1:{self.httpd.server_port}{path}", timeout=5) as response:
            content = response.read()
        return FetchResult(source.id, "fetched", backend, content=content, final_url=source.location)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=5)


def test_collect_plan_cli_runs_unattended_and_reuses_confirmation(tmp_path, monkeypatch, capsys):
    workspace = tmp_path / "ws"
    urls = [f"https://shop.example/items/{index}" for index in range(5)]
    url_file = tmp_path / "urls.txt"
    url_file.write_text("\n".join(urls) + "\n", encoding="utf-8")
    common = ["collect-plan", "--workspace-root", str(workspace), "--urls-file", str(url_file),
              "--industry", "Retail", "--product", "Synthetic items", "--market", "Testland",
              "--batch-size", "2", "--max-seconds", "60", "--json"]
    with _FixtureSite() as site:
        monkeypatch.setattr(catalog_collection, "collect", site.fetch)
        assert main(common) == 2
        assert "--yes" in capsys.readouterr().err
        assert not (workspace / "plans.sqlite3").exists()

        assert main([*common, "--yes"]) == 0
        output = capsys.readouterr().out.strip().splitlines()
        assert len(output) == 1
        summary = json.loads(output[0])
        assert summary["status"] == "succeeded" and summary["operation"] == "research_batch"
        assert summary["target_count"] == 5 and summary["completed_batches"] == 3
        assert summary["coverage_counts"] == {"visited": 5}
        assert summary["observation_counts"]["total"] == 5
        assert Path(summary["final_report_path"]).is_file() and Path(summary["dashboard_path"]).is_file()
        assert summary["errors"] == [] and summary["plan"]["created"] is True
        assert summary["plan"]["url_fingerprint"] == summary["url_fingerprint"] == plans.url_fingerprint(urls)
        assert {path.rsplit("/", 1)[-1] for path in site.requests} == {str(index) for index in range(5)}
        assert runtime.runtime_status(workspace)["status"] == "stopped"

        plan_id = summary["plan"]["id"]
        assert main([*common, "--yes", "--plan-id", plan_id]) == 0
        again = json.loads(capsys.readouterr().out)
        assert again["plan"]["reused_confirmation"] is True
        assert again["plan"]["revision"] == summary["plan"]["revision"]
        assert again["job_id"] != summary["job_id"]

    assert main(["assistant", "wait", "--workspace-root", str(workspace), "--job-id", summary["job_id"],
                 "--timeout", "5", "--json"]) == 0
    waited = json.loads(capsys.readouterr().out)
    assert waited["status"] == "succeeded" and waited["final_report_path"] == summary["final_report_path"]


def test_collect_plan_rejects_invalid_url_file_before_any_plan(tmp_path, capsys):
    workspace = tmp_path / "ws"
    url_file = tmp_path / "urls.txt"
    url_file.write_text("https://shop.example/1\nhttps://shop.example/sub 0201\n", encoding="utf-8")
    code = main(["collect-plan", "--workspace-root", str(workspace), "--urls-file", str(url_file),
                 "--industry", "Retail", "--product", "Items", "--market", "Testland", "--yes"])
    assert code == 2 and "invalid entries" in capsys.readouterr().err
    assert not (workspace / "plans.sqlite3").exists()


def test_batch_job_follow_up_gaps_observations_and_retry(tmp_path, monkeypatch):
    from su_crawler import research_batch as batch
    urls = [f"https://shop.example/items/{index}" for index in range(5)]
    created = plans.create_plan(tmp_path, "Synthetic batch follow-up")["plan"]
    revised = plans.import_urls(tmp_path, created["id"], created["revision"], urls)["plan"]
    revised = plans.revise_plan(tmp_path, revised["id"], revised["revision"], {
        "topic": {"industry": "Retail", "product": "Items", "market": "Testland"}})["plan"]
    plan = plans.confirm_plan(tmp_path, revised["id"], revised["revision"], True)["plan"]
    failing = {urls[3]}

    def fetch(source, base_dir, backend):
        if source.location in failing:
            return FetchResult(source.id, "failed", backend, message="synthetic outage")
        number = source.location.rsplit("/", 1)[-1]
        product = {"@type": "Product", "name": f"Item {number}", "sku": f"SKU-{number}",
                   "offers": {"@type": "Offer", "price": "12", "priceCurrency": "USD"}}
        return FetchResult(source.id, "fetched", backend, final_url=source.location,
                           content=f'<script type="application/ld+json">{json.dumps(product)}</script>'.encode())

    monkeypatch.setattr(catalog_collection, "collect", fetch)
    # Run every job in this process so the patched collector boundary applies.
    monkeypatch.setattr(runtime, "start_worker", lambda root: {"status": "stopped"})
    job = batch.queue_batch_run(tmp_path, {"plan_id": plan["id"], "expected_revision": plan["revision"],
                                           "batch_size": 2, "max_seconds": 60, "concurrency": 1})
    assert runtime.run_inline_worker(tmp_path)
    assert runtime.get_job(tmp_path, job["id"])["status"] == "succeeded"
    server = build_assistant_server(tmp_path)

    gaps = _call(server, "get_research_gaps", {"job_id": job["id"]})
    failed = [item for item in gaps["items"] if item["url"] == urls[3]]
    assert failed and failed[0]["retryable"] and failed[0]["batch_index"] == 1
    assert gaps["retryable_count"] >= 1 and _size(gaps) < BUDGET * 3

    observations = _call(server, "get_job_observations", {"job_id": job["id"]})
    assert observations["total"] == 4 and len(observations["run_ids"]) == 3
    assert observations["coverage"] == [] and observations["coverage_total"] == 5
    paged = _call(server, "get_job_observations", {"job_id": job["id"], "include_coverage": True,
                                                   "coverage_offset": 2, "coverage_limit": 2})
    assert [item["url"] for item in paged["coverage"]] == urls[2:4]
    progress = _call(server, "get_research_batch_progress", {"job_id": job["id"], "include_changes": True})
    assert progress["item_changes"]["offset"] == 0 and _size(progress) < BUDGET * 2

    failing.clear()
    retried = _call(server, "retry_research_pages", {"job_id": job["id"], "max_seconds": 30})
    assert retried["retry_url_count"] == 1 and retried["job"]["retry_of"] == job["id"]
    assert runtime.run_inline_worker(tmp_path)
    retry_job = runtime.get_job(tmp_path, retried["job"]["id"])
    assert retry_job["status"] == "succeeded", retry_job.get("error")
    result = retry_job["result"]["result"]
    assert result["retried_batches"] == [1] and result["coverage_counts"] == {"visited": 5}
    assert result["observation_counts"]["total"] == 5
    assert Path(retry_job["result"]["report_path"]).is_file()
    journal = batch.read_batch_journal(tmp_path, plan["id"], retry_job["id"])
    assert journal["batches"][1]["retry_job"] == retry_job["id"]
    assert journal["batches"][0]["inherited_from"] == job["id"]
    after = _call(server, "get_research_gaps", {"job_id": retry_job["id"]})
    assert not [item for item in after["items"] if item["url"] == urls[3] and item["status"] == "failed"]
    with pytest.raises(Exception, match="no retryable pages"):
        _call(server, "retry_research_pages", {"job_id": retry_job["id"]})


def test_wait_for_job_accepts_up_to_120_seconds(tmp_path, monkeypatch):
    monkeypatch.setattr("su_crawler.assistant_runtime.get_job", lambda *_: {
        "id": "a" * 32, "operation": "research_batch", "status": "succeeded", "attempt": 1,
        "result": {"execution_status": "succeeded", "evidence_status": "completed",
                   "report_path": "/x/final.xlsx", "result": {"reports_ready": 3, "target_count": 9}}})
    server = build_assistant_server(tmp_path)
    waited = _call(server, "wait_for_job", {"job_id": "a" * 32, "timeout_seconds": 120})
    assert waited["completed"] and waited["result_summary"] == {"reports_ready": 3, "target_count": 9}


def test_claude_code_kit_is_portable_and_least_privilege():
    root = Path(__file__).resolve().parents[1]
    files = [root / "CLAUDE.md", root / ".claude/settings.json", root / ".claude/agents/sourceledger-collector.md",
             root / ".claude/skills/collect/SKILL.md"]
    for path in files:
        text = path.read_text(encoding="utf-8")
        assert r"C:\Users" not in text and "/home/" not in text and "/Users/" not in text, path
    assert (root / "CLAUDE.md").read_text(encoding="utf-8").startswith("@AGENTS.md")
    settings = json.loads(files[1].read_text(encoding="utf-8"))
    assert set(settings) == {"permissions"} and "hooks" not in settings
    assert all(rule.startswith("mcp__sourceledger__") for rule in
               settings["permissions"]["allow"] + settings["permissions"]["ask"])
    assert not any(rule.split("__")[-1].startswith(("confirm", "start", "retry"))
                   for rule in settings["permissions"]["allow"])
    header = files[2].read_text(encoding="utf-8").split("---")[1]
    fields = dict(line.split(": ", 1) for line in header.strip().splitlines())
    assert fields["model"] == "haiku" and fields["tools"] == "mcp__sourceledger__*"


def test_confirmed_summary_carries_pins_used_by_plan_jobs(tmp_path, monkeypatch):
    monkeypatch.setattr(runtime, "start_worker", lambda root: {"status": "idle"})
    server = build_assistant_server(tmp_path)
    plan = _call(server, "create_research_plan", {"request_text": "Synthetic pin check"})["plan"]
    plan = _call(server, "update_research_plan", {"plan_id": plan["id"], "expected_revision": plan["revision"], "changes": {
        "topic": {"industry": "Retail", "product": "Items", "market": "Testland"},
        "added_candidates": [{"url": "https://shop.example/a"}]}})["plan"]
    plan = _call(server, "confirm_research_plan", {"plan_id": plan["id"], "expected_revision": plan["revision"],
                                                   "user_confirmed": True})["plan"]
    started = _call(server, "start_research_plan", {"plan_id": plan["id"], "expected_revision": plan["revision"]})
    assert started["job"]["arguments"]["plan_fingerprint"] == plan["plan_fingerprint"]
    assert plan["plan_fingerprint"] == plans.fingerprint(plans.get_plan(tmp_path, plan["id"])["plan"])
