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
        """설정된 수집 범위와 로컬 도구 설치 상태. 사이트 접속 성공을 보증하지 않음."""
        from .doctor import doctor
        return {"name": config.name, "demo": config.demo, "products": len(config.products), "sources": len(config.sources), "backends": doctor(), "worker": worker_status(output)}

    @server.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, openWorldHint=True))
    def start_collection() -> dict:
        """미리 설정한 범위의 작업을 독립 실행 중인 작업자에게 등록. UWS/외부 AI 호출 없음."""
        worker = worker_status(output)
        if not worker["online"] or worker.get("config_hash") != config_fingerprint(config):
            raise ValueError("같은 설정의 독립 작업자가 필요합니다. 별도 터미널에서 python -m su_crawler.worker --config <설정파일> 을 실행하세요")
        run_id = uuid.uuid4().hex
        dispatch = output / "dispatch"
        dispatch.mkdir(parents=True, exist_ok=True)
        receipt = {"id": run_id, "status": "queued", "at": utc_now(), "config_hash": config_fingerprint(config)}
        path = dispatch / f"{run_id}.json"
        atomic_json(path, receipt)
        return receipt

    @server.tool(annotations=ToolAnnotations(readOnlyHint=True))
    def get_collection_status(run_id: str) -> dict:
        """실행 상태와 상품별 누락·검토 사유를 조회."""
        if not run_id.isalnum() or len(run_id) > 80:
            raise ValueError("잘못된 실행 ID")
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
                run = {**run, "status": "failed", "reason": receipt.get("reason", "작업자 실패")}
            elif run["status"] in {"queued", "starting", "running"} and not worker["online"]:
                run = {**run, "status": "interrupted", "reason": "작업자 응답 없음. 동일 설정으로 작업자를 다시 실행하면 재개합니다"}
            return {"run": run, "tasks": tasks, "worker": worker}
        finally:
            store.close()

    @server.tool(annotations=ToolAnnotations(readOnlyHint=True))
    def get_price_observations(run_id: str, offset: int = 0, limit: int = 50) -> dict:
        """검증 상태·근거 포함 관측 조회. 미확인 수치를 채우지 마세요."""
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
        """DB에서 엑셀을 다시 생성. 반환 경로는 로컬 경로이며 원격 다운로드 링크가 아님."""
        from .pipeline import report_for_run
        from .storage import workspace_lock
        with workspace_lock(output):
            store = Store(output)
            try:
                return report_for_run(config, store, run_id)
            finally:
                store.close()

    return server
