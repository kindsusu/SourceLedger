from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
import json
import os
import sqlite3

from .models import utc_now


class RunBusyError(RuntimeError):
    pass


@contextmanager
def workspace_lock(directory: Path):
    """OS lock released on process death; stale lock files don't block a restart."""
    directory.mkdir(parents=True, exist_ok=True)
    handle = (directory / "collection.lock").open("a+b")
    try:
        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write(b"0")
            handle.flush()
        handle.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise RunBusyError("Collection is already running in this output directory") from exc
        yield
    finally:
        handle.close()


class Store:
    def __init__(self, directory: Path):
        directory.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(directory / "prices.sqlite3", timeout=15)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript("""
        CREATE TABLE IF NOT EXISTS runs (
            id TEXT PRIMARY KEY, config_hash TEXT NOT NULL, status TEXT NOT NULL,
            started_at TEXT NOT NULL, finished_at TEXT, demo INTEGER NOT NULL,
            report_path TEXT, reason TEXT NOT NULL DEFAULT ''
        );
        CREATE TABLE IF NOT EXISTS tasks (
            id TEXT PRIMARY KEY, run_id TEXT NOT NULL REFERENCES runs(id),
            source_id TEXT NOT NULL, product_id TEXT NOT NULL, status TEXT NOT NULL,
            attempts INTEGER NOT NULL DEFAULT 0, reason TEXT NOT NULL DEFAULT '',
            backend TEXT, updated_at TEXT NOT NULL,
            UNIQUE(run_id, source_id, product_id)
        );
        CREATE TABLE IF NOT EXISTS observations (
            id TEXT PRIMARY KEY, run_id TEXT NOT NULL REFERENCES runs(id),
            task_id TEXT NOT NULL REFERENCES tasks(id), data TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS attempts (
            id INTEGER PRIMARY KEY, run_id TEXT NOT NULL REFERENCES runs(id),
            source_id TEXT NOT NULL, backend TEXT NOT NULL,
            status TEXT NOT NULL, at TEXT NOT NULL, data TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS source_snapshots (
            cache_key TEXT PRIMARY KEY, data TEXT NOT NULL
        );
        """)

    def close(self):
        self.db.close()

    def source_snapshot(self, cache_key: str) -> dict | None:
        row = self.db.execute("SELECT data FROM source_snapshots WHERE cache_key=?", (cache_key,)).fetchone()
        if row is None:
            return None
        try:
            value = json.loads(row[0])
            return value if isinstance(value, dict) else None
        except (ValueError, TypeError):
            return None

    def save_source_snapshot(self, cache_key: str, data: dict) -> None:
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO source_snapshots VALUES(?,?)",
                            (cache_key, json.dumps(data, ensure_ascii=False)))

    def run(self, run_id: str) -> dict:
        row = self.db.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
        if row is None:
            raise ValueError("Run ID does not exist")
        return dict(row)

    def tasks(self, run_id: str) -> list[dict]:
        return [dict(r) for r in self.db.execute("SELECT * FROM tasks WHERE run_id=? ORDER BY source_id,product_id", (run_id,))]

    def observations(self, run_id: str) -> list[dict]:
        return [json.loads(r[0]) for r in self.db.execute("SELECT data FROM observations WHERE run_id=? ORDER BY id", (run_id,))]

    def update_task(self, task_id: str, status: str, reason: str = "", backend: str | None = None, attempt: bool = False):
        self.db.execute("UPDATE tasks SET status=?, reason=?, backend=?, updated_at=?, attempts=attempts+? WHERE id=?",
                        (status, reason, backend, utc_now(), int(attempt), task_id))
        self.db.commit()

    def finish_task(self, task_id: str, status: str, reason: str, backend: str, observations: list[dict]):
        with self.db:
            self.db.execute("DELETE FROM observations WHERE task_id=?", (task_id,))
            for row in observations:
                self.db.execute("INSERT INTO observations(id,run_id,task_id,data) VALUES(?,?,?,?)",
                                (row["id"], row["run_id"], task_id, json.dumps(row, ensure_ascii=False)))
            self.db.execute("UPDATE tasks SET status=?,reason=?,backend=?,updated_at=? WHERE id=?",
                            (status, reason, backend, utc_now(), task_id))
