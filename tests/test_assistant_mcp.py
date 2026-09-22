from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

pytest.importorskip("mcp")
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from su_crawler.assistant_server import build_assistant_server


def _call(server, name, arguments):
    _, structured = asyncio.run(server.call_tool(name, arguments))
    return structured


def test_assistant_mcp_onboarding_and_offline_queue(tmp_path):
    server = build_assistant_server(tmp_path)
    assert _call(server, "get_workspace_status", {})["status"] == "needs_setup"
    initialized = _call(server, "initialize_workspace", {
        "industry": "vehicle rental", "product": "compact car", "market": "KR", "locale": "ko",
    })
    assert initialized["workspace"]["industry"] == "vehicle rental"
    _call(server, "set_product_identity", {"identifiers": {"model": "EXACT-1"}})
    source = _call(server, "add_source_candidate", {
        "url": "https://example.test/prices", "scope": "public", "name": "Fixture",
    })["source"]
    queued = _call(server, "queue_source_proposal", {"source_id": source["id"]})
    assert queued["status"] == "queued"
    assert queued["worker"]["status"] == "stopped"
    assert queued["arguments"]["max_model_calls"] == 0
    assert _call(server, "get_job_status", {"job_id": queued["id"]})["status"] == "queued"
    assert _call(server, "list_recent_jobs", {})["jobs"][0]["id"] == queued["id"]


def test_server_tool_contract_and_registered_report_resource(tmp_path):
    server = build_assistant_server(tmp_path)
    tools = {tool.name for tool in asyncio.run(server.list_tools())}
    assert {
        "get_workspace_status", "initialize_workspace", "set_product_identity", "add_source_candidate",
        "queue_source_discovery", "queue_source_proposal", "queue_research_agent", "queue_supported_sites",
        "queue_verification", "queue_collection", "queue_report_export", "get_job_status",
        "list_recent_jobs", "resume_job_execution", "get_job_observations", "get_job_report",
    } == tools
    resources = asyncio.run(server.list_resource_templates())
    assert any(str(item.uriTemplate) == "sourceledger://reports/{job_id}" for item in resources)


def test_real_stdio_disconnect_does_not_cancel_independent_collection(tmp_path):
    root = Path(__file__).resolve().parents[1]
    request_started, release = threading.Event(), threading.Event()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            request_started.set()
            release.wait(10)
            body = (root / "examples/fixtures/catalog.html").read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    from su_crawler.research import init_workspace
    init_workspace(tmp_path / "research.json", industry="fixture", product="part", market="test")
    config = {
        "name": "assistant fixture", "demo": True, "output_dir": "output", "max_run_seconds": 20,
        "products": [{"id": "part", "name": "Part A", "identifiers": {"model": "TEST-A"}}],
        "sources": [{
            "id": "local", "name": "local", "kind": "web",
            "location": f"http://127.0.0.1:{httpd.server_port}/catalog", "product_ids": ["part"],
            "allowed_domains": ["127.0.0.1"], "backends": ["http"], "respect_robots": False,
            "internal": True, "max_attempts": 1, "row_selector": ".product",
            "selectors": {"model": ".model", "price": ".price", "currency": ".currency", "unit": ".unit",
                          "pack_quantity": ".pack", "tax": ".tax", "price_type": ".type", "price_basis": ".basis"},
        }],
    }
    (tmp_path / "config.json").write_text(json.dumps(config), encoding="utf-8")
    from su_crawler.assistant_runtime import runtime_status, start_worker, stop_worker
    worker = start_worker(tmp_path)
    deadline = time.monotonic() + 10
    while worker["status"] == "starting" and time.monotonic() < deadline:
        time.sleep(0.05)
        worker = runtime_status(tmp_path)
    assert worker["status"] in {"idle", "running"}
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "su_crawler", "serve-assistant", "--workspace-root", str(tmp_path)],
        env=dict(os.environ, PYTHONIOENCODING="utf-8"), cwd=str(root),
    )

    async def submit_and_disconnect():
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                response = await session.call_tool("queue_collection", {"config_path": "config.json"})
                job_id = json.loads(response.content[0].text)["id"]
                assert await asyncio.to_thread(request_started.wait, 10)
                return job_id

    job_id = asyncio.run(submit_and_disconnect())
    try:
        release.set()

        async def reconnect():
            async with stdio_client(params) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    deadline = time.monotonic() + 20
                    while time.monotonic() < deadline:
                        response = await session.call_tool("get_job_status", {"job_id": job_id})
                        job = json.loads(response.content[0].text)
                        if job["status"] == "succeeded":
                            metadata = await session.call_tool("get_job_report", {"job_id": job_id})
                            report = json.loads(metadata.content[0].text)
                            resource = await session.read_resource(report["resource_uri"])
                            return job, report, resource
                        await asyncio.sleep(0.1)
                    raise AssertionError("assistant job did not finish")

        job, report, resource = asyncio.run(reconnect())
        assert job["result"]["execution_status"] == "succeeded"
        assert report["size"] > 0 and len(report["sha256"]) == 64
        assert resource.contents[0].blob
    finally:
        release.set()
        stop_worker(tmp_path)
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and runtime_status(tmp_path)["status"] != "stopped":
            time.sleep(0.05)
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)
