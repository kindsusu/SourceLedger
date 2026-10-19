"""Small MCP surface. A fixed local config is the only collection scope."""
from __future__ import annotations
import json
from pathlib import Path
import uuid

from .config import config_fingerprint, load_config
from .models import utc_now
from .storage import Store
from .worker import atomic_json, worker_status


def build_server(config_path: Path, *, port: int = 8765):
    from mcp.server.fastmcp import FastMCP
    from mcp.types import ToolAnnotations
    config = load_config(config_path)
    output = Path(config.output_dir)
    output.mkdir(parents=True, exist_ok=True)
    server = FastMCP("su-price-crawler", host="127.0.0.1", port=port)

    @server.tool(annotations=ToolAnnotations(readOnlyHint=True))
    def get_capabilities() -> dict:
        """Configured collection scope and local tool status; does not guarantee site access."""
        from .doctor import doctor
        return {"name": config.name, "demo": config.demo, "products": len(config.products), "sources": len(config.sources), "backends": doctor(), "worker": worker_status(output)}

    @server.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, openWorldHint=True))
    def start_collection() -> dict:
        """Register configured-scope tasks with an independently running worker; no UWS or external AI calls."""
        worker = worker_status(output)
        if not worker["online"] or worker.get("config_hash") != config_fingerprint(config):
            raise ValueError("An independent worker with the same configuration is required. Run python -m su_crawler.worker --config <config-file> in another terminal")
        run_id = uuid.uuid4().hex
        dispatch = output / "dispatch"
        dispatch.mkdir(parents=True, exist_ok=True)
        receipt = {"id": run_id, "status": "queued", "at": utc_now(), "config_hash": config_fingerprint(config)}
        path = dispatch / f"{run_id}.json"
        atomic_json(path, receipt)
        return receipt

    @server.tool(annotations=ToolAnnotations(readOnlyHint=True))
    def get_collection_status(run_id: str) -> dict:
        """Retrieve run status and product-level missing or review reasons."""
        if not run_id.isalnum() or len(run_id) > 80:
            raise ValueError("Invalid run ID")
        store = Store(output)
        try:
            worker = worker_status(output)
            path = output / "dispatch" / f"{run_id}.json"
            receipt = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else None
            try:
                run, tasks = store.run(run_id), store.tasks(run_id)
            except ValueError:
                if not receipt:
                    raise
                run, tasks = receipt, []
            if receipt and receipt.get("status") == "failed":
                run = {**run, "status": "failed", "reason": receipt.get("reason", "Worker failed")}
            elif run["status"] in {"queued", "starting", "running"} and not worker["online"]:
                run = {**run, "status": "interrupted", "reason": "Worker did not respond. Restart a worker with the same configuration to resume"}
            return {"run": run, "tasks": tasks, "worker": worker}
        finally:
            store.close()

    @server.tool(annotations=ToolAnnotations(readOnlyHint=True))
    def get_price_observations(run_id: str, offset: int = 0, limit: int = 50) -> dict:
        """Retrieve observations with verification status and evidence. Do not fill unverified values."""
        if offset < 0 or not 1 <= limit <= 200:
            raise ValueError("offset>=0, limit=1..200")
        store = Store(output)
        try:
            store.run(run_id)
            rows = store.observations(run_id)
            return {"total": len(rows), "offset": offset, "rows": rows[offset:offset+limit]}
        finally:
            store.close()

    @server.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False))
    def export_price_report(run_id: str) -> dict:
        """Regenerate an XLSX report from the database. Returned path is local, not a remote download link."""
        from .pipeline import report_for_run
        from .storage import workspace_lock
        with workspace_lock(output):
            store = Store(output)
            try:
                return report_for_run(config, store, run_id)
            finally:
                store.close()

    return server
