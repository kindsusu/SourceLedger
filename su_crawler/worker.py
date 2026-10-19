"""Independent queue worker; start outside the MCP client's process tree."""
from __future__ import annotations
import argparse
import json
import os
import re
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from .config import config_fingerprint, load_config
from .pipeline import execute
from .models import utc_now
from .storage import Store, workspace_lock


def atomic_json(path: Path, data: dict):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)


def worker_status(output: Path) -> dict:
    try:
        data = json.loads((output / "worker" / "heartbeat.json").read_text(encoding="utf-8"))
        age = (datetime.now(timezone.utc) - datetime.fromisoformat(data["at"])).total_seconds()
        return {**data, "online": 0 <= age < 15 and data["status"] != "stopped"}
    except (OSError, ValueError, KeyError):
        return {"online": False, "status": "unavailable"}


def listen(config_path: Path, *, idle_timeout: float = 0):
    config = load_config(config_path)
    output = Path(config.output_dir)
    fingerprint = config_fingerprint(config)
    with workspace_lock(output / "worker"):
        dispatch = output / "dispatch"
        dispatch.mkdir(parents=True, exist_ok=True)
        heartbeat = output / "worker" / "heartbeat.json"
        state = {"pid": os.getpid(), "status": "idle", "run_id": None, "config_hash": fingerprint}
        stop = threading.Event()

        def beat():
            while not stop.is_set():
                atomic_json(heartbeat, {**state, "at": utc_now()})
                stop.wait(1)

        thread = threading.Thread(target=beat, daemon=True)
        thread.start()
        last_work = time.monotonic()
        try:
            while not idle_timeout or time.monotonic() - last_work < idle_timeout:
                for path in sorted(dispatch.glob("*.json")):
                    try:
                        receipt = json.loads(path.read_text(encoding="utf-8"))
                    except (OSError, ValueError):
                        continue
                    if receipt.get("status") not in {"queued", "starting"}:
                        continue
                    run_id = path.stem
                    if not re.fullmatch(r"[a-f0-9]{32}", run_id) or receipt.get("config_hash") != fingerprint:
                        atomic_json(path, {**receipt, "status": "failed", "reason": "Run ID or configuration does not match"})
                        continue
                    state.update(status="working", run_id=run_id)
                    atomic_json(path, {**receipt, "status": "starting", "at": utc_now()})
                    try:
                        store = Store(output)
                        try:
                            exists = store.db.execute("SELECT 1 FROM runs WHERE id=?", (run_id,)).fetchone()
                        finally:
                            store.close()
                        result = execute(config, resume_id=run_id) if exists else execute(config, new_run_id=run_id)
                        atomic_json(path, result)
                    except Exception as exc:
                        atomic_json(path, {**receipt, "status": "failed", "reason": type(exc).__name__, "at": utc_now()})
                    finally:
                        state.update(status="idle", run_id=None)
                        last_work = time.monotonic()
                stop.wait(0.2)
        finally:
            stop.set()
            thread.join(timeout=3)
            atomic_json(heartbeat, {**state, "status": "stopped", "at": utc_now()})


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--idle-timeout", type=float, default=0)
    args = parser.parse_args()
    listen(args.config, idle_timeout=args.idle_timeout)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
