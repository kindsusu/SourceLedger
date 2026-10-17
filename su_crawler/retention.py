"""Conservative checkpoint maintenance; reports and source evidence are never targets.

Every committed checkpoint remains a root, including completed and historical
versions. Only pages and binary captures unreachable from *all* roots qualify.
"""
from __future__ import annotations

import base64
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import time
import uuid

from .catalog_checkpoint import _blob_path, _is_linklike, _read_blob, checkpoint_lock


_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_BLOB = re.compile(r"([0-9a-f]{64})(\.gz|\.bin)\Z")


def _contained_output(output: str | Path) -> Path:
    supplied = Path(output).expanduser().absolute()
    if _is_linklike(supplied) or not supplied.is_dir():
        raise ValueError("Output must be an existing real directory")
    return supplied.resolve()


def _decode_document(value: str, label: str) -> object:
    try:
        document = json.loads(value)
    except (ValueError, TypeError) as exc:
        raise ValueError(f"Invalid checkpoint JSON: {label}") from exc
    if not isinstance(document, dict):
        raise ValueError(f"Invalid checkpoint document: {label}")
    return document


def _inventory(output: Path) -> tuple[list[Path], list[Path]]:
    root = output / "checkpoint_blobs"
    if _is_linklike(root):
        raise ValueError("Checkpoint blob directory is a symbolic link")
    if not root.exists():
        return [], []
    if not root.is_dir():
        raise ValueError("Checkpoint blob directory is not a directory")
    blobs, pending = [], []
    for shard in root.iterdir():
        if _is_linklike(shard) or not shard.is_dir() or not re.fullmatch(r"[0-9a-f]{2}", shard.name):
            raise ValueError("Unexpected checkpoint blob shard")
        for child in shard.iterdir():
            if _is_linklike(child) or not child.is_file():
                raise ValueError("Unexpected checkpoint blob entry")
            if child.name.startswith(".pending-"):
                pending.append(child)
                continue
            match = _BLOB.fullmatch(child.name)
            if not match or match[1][:2] != shard.name:
                raise ValueError("Unexpected checkpoint blob name")
            blobs.append(child)
    return blobs, pending


def _inspect(output: Path, grace_days: int) -> tuple[dict, list[str], list[Path], list[Path]]:
    database = output / "prices.sqlite3"
    for suffix in ("", "-wal", "-shm", "-journal"):
        if _is_linklike(Path(str(database) + suffix)):
            raise ValueError("Checkpoint database contains a symbolic link")
    blobs, pending = _inventory(output)
    if not database.is_file():
        if blobs or pending:
            raise ValueError("Checkpoint files have no ledger to validate against")
        return ({"checkpoint_count": 0, "page_count": 0, "blob_count": 0,
                 "orphan_pages": 0, "orphan_blobs": 0, "stale_pending": 0,
                 "orphan_page_bytes": 0, "removable_file_bytes": 0,
                 "files_preserved": ["prices.sqlite3", "*.xlsx", "evidence/**", "jobs/**"]}, [], [], [])

    with sqlite3.connect(database.as_uri() + "?mode=ro", uri=True, timeout=5) as db:
        tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if "catalog_checkpoints" not in tables:
            if blobs or pending or "checkpoint_pages" in tables:
                raise ValueError("Checkpoint storage has no checkpoint roots")
            checkpoints = []
        else:
            checkpoints = db.execute("SELECT id,data FROM catalog_checkpoints").fetchall()
        pages = dict(db.execute("SELECT digest,data FROM checkpoint_pages")) if "checkpoint_pages" in tables else {}
        if len(pages) != (db.execute("SELECT count(*) FROM checkpoint_pages").fetchone()[0]
                          if "checkpoint_pages" in tables else 0):
            raise ValueError("Duplicate checkpoint page digest")
        for digest, data in pages.items():
            if not isinstance(digest, str) or not _DIGEST.fullmatch(digest):
                raise ValueError("Invalid checkpoint page digest")
            if hashlib.sha256(data.encode("utf-8")).hexdigest() != digest:
                raise ValueError("Checkpoint page hash mismatch")
            _decode_document(data, digest)

    live_pages: set[str] = set()
    live_blobs: set[Path] = set()
    def walk(value: object):
        if isinstance(value, list):
            for child in value:
                walk(child)
            return
        if not isinstance(value, dict):
            return
        if "__checkpoint_page__" in value:
            if set(value) != {"__checkpoint_page__"} or not isinstance(value["__checkpoint_page__"], str):
                raise ValueError("Invalid checkpoint page reference")
            digest = value["__checkpoint_page__"]
            if not _DIGEST.fullmatch(digest) or digest not in pages:
                raise ValueError("Checkpoint page is missing")
            if digest not in live_pages:
                live_pages.add(digest)
                walk(_decode_document(pages[digest], digest))
            return
        if "__checkpoint_blob__" in value:
            if set(value) != {"__checkpoint_blob__", "sha256", "size"}:
                raise ValueError("Invalid checkpoint blob reference")
            digest = value["sha256"]
            relative = value["__checkpoint_blob__"]
            if not isinstance(digest, str) or not _DIGEST.fullmatch(digest) or not isinstance(relative, str):
                raise ValueError("Invalid checkpoint blob reference")
            path = _blob_path(output, digest, Path(relative).suffix)
            if relative != path.relative_to(output).as_posix():
                raise ValueError("Invalid checkpoint blob path")
            if path not in live_blobs:
                _read_blob(output, value)  # Validate referenced bytes before authorizing any removal.
                live_blobs.add(path)
            return
        if "__bytes__" in value:
            if set(value) != {"__bytes__"} or not isinstance(value["__bytes__"], str):
                raise ValueError("Invalid legacy checkpoint bytes")
            try:
                base64.b64decode(value["__bytes__"], validate=True)
            except ValueError as exc:
                raise ValueError("Invalid legacy checkpoint bytes") from exc
            return
        for child in value.values():
            walk(child)

    for key, data in checkpoints:
        walk(_decode_document(data, str(key)))
    orphan_pages = sorted(set(pages) - live_pages)
    orphan_blobs = [path for path in blobs if path not in live_blobs]
    cutoff = time.time() - timedelta(days=grace_days).total_seconds()
    stale_pending = [path for path in pending if path.stat().st_mtime <= cutoff]
    removable = orphan_blobs + stale_pending
    summary = {
        "checkpoint_count": len(checkpoints), "page_count": len(pages),
        "blob_count": len(blobs), "orphan_pages": len(orphan_pages),
        "orphan_blobs": len(orphan_blobs), "stale_pending": len(stale_pending),
        "orphan_page_bytes": sum(len(pages[digest].encode("utf-8")) for digest in orphan_pages),
        "removable_file_bytes": sum(path.stat().st_size for path in removable),
        "files_preserved": ["prices.sqlite3", "*.xlsx", "evidence/**", "jobs/**"],
    }
    return summary, orphan_pages, orphan_blobs, stale_pending


def maintain_storage(output: str | Path, *, apply: bool = False, grace_days: int = 7) -> dict:
    """Inspect or prune unreachable checkpoint internals in one output directory.

    An apply never removes a checkpoint root, a report, an observation, a
    receipt, or a referenced capture. Busy writers cause the call to fail.
    """
    if not isinstance(apply, bool) or isinstance(grace_days, bool) or not isinstance(grace_days, int) or not 1 <= grace_days <= 365:
        raise ValueError("apply must be boolean and grace_days must be 1 to 365")
    root = _contained_output(output)
    with checkpoint_lock(root):
        summary, orphan_pages, orphan_blobs, pending = _inspect(root, grace_days)
        summary = {"output_dir": str(root), "mode": "apply" if apply else "dry_run", **summary}
        if not apply:
            return summary
        database = root / "prices.sqlite3"
        if orphan_pages:
            with sqlite3.connect(database, timeout=5) as db:
                with db:
                    db.executemany("DELETE FROM checkpoint_pages WHERE digest=?", ((item,) for item in orphan_pages))
        for path in orphan_blobs + pending:
            if _is_linklike(path) or not path.is_file() or not path.resolve().is_relative_to(root / "checkpoint_blobs"):
                raise ValueError("Checkpoint file changed during maintenance")
            path.unlink()
        return {**summary, "removed_pages": len(orphan_pages),
                "removed_files": len(orphan_blobs) + len(pending)}


def audit_storage(output: str | Path) -> dict:
    """Read-only equivalent of maintain_storage's default dry run."""
    return maintain_storage(output, apply=False)


def run_retention_policy(output: str | Path) -> dict:
    """Best-effort daily cleanup after a completed checkpoint save.

    An invalid policy or damaged checkpoint never fails the collection. The
    status receipt makes skipped maintenance visible to the operator.
    """
    root = Path(output).expanduser().absolute()
    directory = root / ".checkpoint-maintenance"
    receipt = directory / "status.json"
    policy_file = root / "storage-policy.json"
    status: dict = {"status": "skipped", "at": datetime.now(timezone.utc).isoformat()}
    try:
        if _is_linklike(root):
            raise ValueError("Output must be an existing real directory")
        if any(_is_linklike(path) for path in (policy_file, receipt, directory)):
            raise ValueError("Storage policy paths must not be symbolic links")
        policy = {"automatic_cleanup": True, "grace_days": 7, "maintenance_interval_hours": 24}
        if policy_file.exists():
            raw = json.loads(policy_file.read_text(encoding="utf-8"))
            if not isinstance(raw, dict) or set(raw) - set(policy):
                raise ValueError("Invalid storage policy fields")
            policy.update(raw)
        if (not isinstance(policy["automatic_cleanup"], bool)
                or isinstance(policy["grace_days"], bool) or not isinstance(policy["grace_days"], int)
                or not 1 <= policy["grace_days"] <= 365
                or isinstance(policy["maintenance_interval_hours"], bool)
                or not isinstance(policy["maintenance_interval_hours"], int)
                or not 1 <= policy["maintenance_interval_hours"] <= 720):
            raise ValueError("Invalid storage policy values")
        if not policy["automatic_cleanup"]:
            return {**status, "reason": "disabled"}
        if receipt.is_file():
            previous = json.loads(receipt.read_text(encoding="utf-8"))
            if not isinstance(previous, dict):
                raise ValueError("Invalid storage maintenance receipt")
            last = previous.get("last_success_at")
            if last:
                elapsed = (datetime.now(timezone.utc) - datetime.fromisoformat(last)).total_seconds()
                if elapsed < policy["maintenance_interval_hours"] * 3600:
                    return {**status, "reason": "interval", "last_success_at": last}
        summary = maintain_storage(root, apply=True, grace_days=policy["grace_days"])
        status = {"status": "completed", "at": datetime.now(timezone.utc).isoformat(),
                  "last_success_at": datetime.now(timezone.utc).isoformat(), "summary": summary}
    except Exception as exc:
        status = {"status": "error", "at": datetime.now(timezone.utc).isoformat(),
                  "error_type": type(exc).__name__, "reason": str(exc)[:250]}
    try:
        if not _is_linklike(directory) and not _is_linklike(receipt):
            directory.mkdir(parents=True, exist_ok=True)
            temporary = directory / (".status-" + uuid.uuid4().hex + ".tmp")
            try:
                temporary.write_text(json.dumps(status, ensure_ascii=False, indent=2), encoding="utf-8")
                os.replace(temporary, receipt)
            finally:
                temporary.unlink(missing_ok=True)
    except (OSError, ValueError):
        pass
    return status
