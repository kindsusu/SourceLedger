import asyncio
import json
import os
from pathlib import Path
import sys
import subprocess
import time
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


def test_real_stdio_mcp_round_trip_and_independent_worker(tmp_path):
    root = Path(__file__).resolve().parents[1]
    config = json.loads((root / "examples/demo.json").read_text(encoding="utf-8"))
    config["output_dir"] = str(tmp_path / "output")
    for source in config["sources"]:
        source["location"] = str(root / "examples" / source["location"])
        source["file_root"] = str(root / "examples")
    request_started, release_request = threading.Event(), threading.Event()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            request_started.set()
            release_request.wait(10)
            data = (root / "examples/fixtures/catalog.html").read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *args):
            pass

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    http_thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    http_thread.start()
    config["sources"][0].update(kind="web", location=f"http://127.0.0.1:{httpd.server_port}/catalog",
                                  internal=True, allowed_domains=["127.0.0.1"], respect_robots=False, backends=["http"])
    path = tmp_path / "config.json"
    path.write_text(json.dumps(config), encoding="utf-8")

    async def check():
        env = dict(os.environ, PYTHONIOENCODING="utf-8")
        params = StdioServerParameters(command=sys.executable, args=["-m", "su_crawler", "serve-mcp", "--config", str(path)], env=env, cwd=str(root))
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                listing = await session.list_tools()
                assert {t.name for t in listing.tools} >= {"start_collection", "get_price_observations", "export_price_report"}
                started = await session.call_tool("start_collection", {})
                assert not started.isError
                info = json.loads(started.content[0].text)
                # Keep the HTTP request in flight while the entire MCP process closes.
                assert await asyncio.to_thread(request_started.wait, 10)
                return info["id"]

    async def check_results(run_id):
        params = StdioServerParameters(command=sys.executable, args=["-m", "su_crawler", "serve-mcp", "--config", str(path)],
                                       env=dict(os.environ, PYTHONIOENCODING="utf-8"), cwd=str(root))
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                for name in ("get_collection_status", "get_price_observations", "export_price_report"):
                    result = await session.call_tool(name, {"run_id": run_id})
                    assert not result.isError
                    payload = json.loads(result.content[0].text)
                    if name == "get_price_observations":
                        assert payload["total"] == 4
                    elif name == "get_collection_status":
                        assert payload["run"]["report_path"]
                    else:
                        assert Path(payload["report_path"]).is_file()

    # Worker is started independently, outside the MCP SDK's Windows job object.
    worker = subprocess.Popen([sys.executable, "-m", "su_crawler.worker", "--config", str(path), "--idle-timeout", "8"],
                              cwd=str(root), creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    from su_crawler.worker import worker_status
    try:
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and not worker_status(tmp_path / "output")["online"]:
            time.sleep(0.1)
        assert worker_status(tmp_path / "output")["online"]
        run_id = asyncio.run(check())
        assert worker.poll() is None
        release_request.set()
        # Server/client have exited. The independent worker must still finish.
        from su_crawler.storage import Store
        deadline = time.monotonic() + 30
        status = None
        while time.monotonic() < deadline:
            if (tmp_path / "output/prices.sqlite3").is_file():
                store = Store(tmp_path / "output")
                try:
                    status = store.run(run_id)
                    if status["report_path"]:
                        break
                except ValueError:
                    pass
                finally:
                    store.close()
            time.sleep(0.1)
        assert status and status["status"] in {"completed", "partial"}
        assert status["report_path"] and Path(status["report_path"]).exists()
        asyncio.run(check_results(run_id))
        assert worker.wait(timeout=15) == 0
    finally:
        release_request.set()
        httpd.shutdown()
        http_thread.join(timeout=2)
        if worker.poll() is None:
            worker.terminate()
            worker.wait(timeout=10)


def test_worker_offline_status(tmp_path):
    from su_crawler.worker import worker_status
    assert worker_status(tmp_path)["online"] is False


def test_mcp_refuses_queue_without_worker(tmp_path):
    from su_crawler.mcp_server import build_server
    root = Path(__file__).resolve().parents[1]
    config = json.loads((root / "examples/demo.json").read_text(encoding="utf-8"))
    config["output_dir"] = str(tmp_path / "output")
    path = tmp_path / "config.json"
    path.write_text(json.dumps(config), encoding="utf-8")
    server = build_server(path)
    import pytest
    with pytest.raises(Exception, match="independent worker"):
        asyncio.run(server.call_tool("start_collection", {}))
    assert not list((tmp_path / "output/dispatch").glob("*.json"))
