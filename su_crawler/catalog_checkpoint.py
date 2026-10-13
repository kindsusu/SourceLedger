"""Durable page captures. Restarting a worker never resets its fetch budget.

Binary captures live in immutable, content-addressed files. The SQLite row is
committed only after those files are durable, so a restart cannot observe a
checkpoint that points to a partially written capture.
"""
from __future__ import annotations

import base64
import gzip
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile

from .storage import Store


_BLOB_ROOT = "checkpoint_blobs"
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_COMPRESSED_SIGNATURES = (b"\x89PNG\r\n\x1a\n", b"\xff\xd8\xff", b"RIFF", b"PK\x03\x04", b"%PDF")


def _blob_path(output: Path, digest: str, suffix: str) -> Path:
    if not isinstance(digest, str) or not _DIGEST.fullmatch(digest) or suffix not in {".gz", ".bin"}:
        raise ValueError("Invalid checkpoint blob reference")
    root = Path(output).resolve()
    blob_root = root / _BLOB_ROOT
    shard = blob_root / digest[:2]
    path = shard / (digest + suffix)
    # Reject links at each level, including dangling links. A checkpoint must
    # never read or write outside its own output directory.
    if any(part.is_symlink() for part in (blob_root, shard, path)):
        raise ValueError("Checkpoint blob path is a symlink")
    if not path.resolve().is_relative_to(root):
        raise ValueError("Checkpoint blob path escapes output directory")
    return path


def _write_blob(output: Path, data: bytes) -> dict:
    digest = hashlib.sha256(data).hexdigest()
    for suffix in (".gz", ".bin"):
        existing = _blob_path(output, digest, suffix)
        if existing.is_file():
            # Skip repeat I/O during every page checkpoint. Reads validate the
            # digest and fail closed if an existing file was damaged.
            return {"__checkpoint_blob__": f"{_BLOB_ROOT}/{digest[:2]}/{digest}{suffix}",
                    "sha256": digest, "size": len(data)}

    if len(data) > 512 and not data.startswith(_COMPRESSED_SIGNATURES):
        compressed = gzip.compress(data, compresslevel=6, mtime=0)
        payload, suffix = (compressed, ".gz") if len(compressed) < len(data) else (data, ".bin")
    else:
        payload, suffix = data, ".bin"
    path = _blob_path(output, digest, suffix)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="wb", dir=path.parent, prefix=".pending-", delete=False) as handle:
            temporary = Path(handle.name)
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        # Another worker may have installed the same digest while we wrote.
        # In either case the content is identical; replacement is atomic.
        if path.is_symlink():
            raise ValueError("Checkpoint blob path is a symlink")
        os.replace(temporary, path)
        temporary = None
        if os.name != "nt":
            directory_fd = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return {"__checkpoint_blob__": f"{_BLOB_ROOT}/{digest[:2]}/{digest}{suffix}",
            "sha256": digest, "size": len(data)}


def _read_blob(output: Path, value: dict) -> bytes:
    if set(value) != {"__checkpoint_blob__", "sha256", "size"}:
        raise ValueError("Invalid checkpoint blob reference")
    relative, digest, size = value["__checkpoint_blob__"], value["sha256"], value["size"]
    if not isinstance(relative, str) or isinstance(size, bool) or not isinstance(size, int) or size < 0:
        raise ValueError("Invalid checkpoint blob reference")
    suffix = Path(relative).suffix
    path = _blob_path(output, digest, suffix)
    expected = f"{_BLOB_ROOT}/{digest[:2]}/{digest}{suffix}"
    if relative != expected:
        raise ValueError("Invalid checkpoint blob path")
    try:
        payload = path.read_bytes()
    except FileNotFoundError as exc:
        raise ValueError("Checkpoint blob is missing") from exc
    try:
        data = gzip.decompress(payload) if suffix == ".gz" else payload
    except (OSError, EOFError) as exc:
        raise ValueError("Checkpoint blob is corrupt") from exc
    if len(data) != size or hashlib.sha256(data).hexdigest() != digest:
        raise ValueError("Checkpoint blob hash or size mismatch")
    return data


def _decode(output: Path, value: dict):
    if set(value) == {"__bytes__"}:
        return base64.b64decode(value["__bytes__"], validate=True)
    if "__checkpoint_blob__" in value:
        return _read_blob(output, value)
    return value


def read_checkpoint(output: Path, key: str) -> dict | None:
    store = Store(output)
    try:
        row = store.db.execute("SELECT data FROM catalog_checkpoints WHERE id=?", (key,)).fetchone()
        return json.loads(row[0], object_hook=lambda value: _decode(output, value)) if row else None
    finally:
        store.close()


def write_checkpoint(output: Path, key: str, state: dict) -> None:
    store = Store(output)
    try:
        def encode(value):
            if isinstance(value, bytes):
                return _write_blob(output, value)
            raise TypeError("Unsupported checkpoint value")

        serialized = json.dumps(state, ensure_ascii=False, default=encode)
        with store.db:
            store.db.execute("INSERT OR REPLACE INTO catalog_checkpoints(id,data) VALUES(?,?)",
                             (key, serialized))
    finally:
        store.close()
