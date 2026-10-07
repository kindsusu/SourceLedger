"""Durable page captures. Restarting a worker never resets its fetch budget."""
from __future__ import annotations

import base64
import json
from pathlib import Path

from .storage import Store


def _encode(value):
    if isinstance(value, bytes):
        return {"__bytes__": base64.b64encode(value).decode("ascii")}
    raise TypeError("Unsupported checkpoint value")


def _decode(value):
    if set(value) == {"__bytes__"}:
        return base64.b64decode(value["__bytes__"], validate=True)
    return value


def read_checkpoint(output: Path, key: str) -> dict | None:
    store = Store(output)
    try:
        row = store.db.execute("SELECT data FROM catalog_checkpoints WHERE id=?", (key,)).fetchone()
        return json.loads(row[0], object_hook=_decode) if row else None
    finally:
        store.close()


def write_checkpoint(output: Path, key: str, state: dict) -> None:
    store = Store(output)
    try:
        with store.db:
            store.db.execute("INSERT OR REPLACE INTO catalog_checkpoints(id,data) VALUES(?,?)",
                             (key, json.dumps(state, ensure_ascii=False, default=_encode)))
    finally:
        store.close()
