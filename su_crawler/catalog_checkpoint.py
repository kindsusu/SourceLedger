"""Durable page captures. Restarting a worker never resets its fetch budget.

Binary captures live in immutable, content-addressed files. The SQLite row is
committed only after those files are durable, so a restart cannot observe a
checkpoint that points to a partially written capture.
"""
from __future__ import annotations

import base64
from contextlib import contextmanager
import gzip
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import tempfile

from .storage import Store, workspace_lock


_BLOB_ROOT = "checkpoint_blobs"
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")


def _is_linklike(path: Path) -> bool:
    """Include Windows junctions and other reparse points, even if dangling."""
    if path.is_symlink():
        return True
    try:
        return bool(getattr(path.lstat(), "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))
    except FileNotFoundError:
        return False


@contextmanager
def checkpoint_lock(output: Path):
    """Serialize checkpoint readers, writers and retention on this output."""
    directory = Path(output).expanduser().absolute() / '.checkpoint-maintenance'
    lock = directory / 'collection.lock'
    if _is_linklike(directory) or _is_linklike(lock):
        raise ValueError('Checkpoint maintenance lock must not be a symbolic link')
    with workspace_lock(directory):
        yield


def _blob_path(output: Path, digest: str, suffix: str) -> Path:
    if not isinstance(digest, str) or not _DIGEST.fullmatch(digest) or suffix not in {".gz", ".bin"}:
        raise ValueError("Invalid checkpoint blob reference")
    root = Path(output).resolve()
    blob_root = root / _BLOB_ROOT
    shard = blob_root / digest[:2]
    path = shard / (digest + suffix)
    # Reject links at each level, including dangling links. A checkpoint must
    # never read or write outside its own output directory.
    if any(_is_linklike(part) for part in (blob_root, shard, path)):
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

    # Store bytes as captured. Source evidence must stay a plain file whose
    # SHA-256 is the file's own hash, and an identical uncompressed blob is
    # hard-linked into evidence, so the page is kept once instead of as a
    # compressed checkpoint copy plus a raw evidence copy. Older ``.gz`` blobs
    # are still read and reused.
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
    with checkpoint_lock(output):
        return _read_checkpoint_unlocked(output, key)


def _read_checkpoint_unlocked(output: Path, key: str) -> dict | None:
    store = Store(output)
    try:
        row = store.db.execute("SELECT data FROM catalog_checkpoints WHERE id=?", (key,)).fetchone()
        if row is None:
            return None
        def decode(value):
            if set(value) == {'__checkpoint_page__'}:
                digest = value['__checkpoint_page__']
                if not isinstance(digest, str) or not _DIGEST.fullmatch(digest):
                    raise ValueError('Invalid checkpoint page reference')
                saved = store.db.execute('SELECT data FROM checkpoint_pages WHERE digest=?', (digest,)).fetchone()
                if saved is None or hashlib.sha256(saved[0].encode('utf-8')).hexdigest() != digest:
                    raise ValueError('Checkpoint page is missing or corrupt')
                return json.loads(saved[0], object_hook=lambda item: _decode(output, item))
            return _decode(output, value)
        return json.loads(row[0], object_hook=decode)
    finally:
        store.close()


def write_checkpoint(output: Path, key: str, state: dict, *, page_cache: dict | None = None) -> None:
    """Page bodies are immutable; callers using page_cache must replace changed pages.

    The optional cache is coordinator-local and scoped to one output/checkpoint.
    It avoids serializing every prior page on each progress update.
    """
    with checkpoint_lock(output):
        _write_checkpoint_unlocked(output, key, state, page_cache=page_cache)
    if isinstance(state.get('result'), dict):
        # Maintenance takes the same lock, after the finalized state is durable.
        # Policy errors are receipts, never collection failures.
        from .retention import run_retention_policy
        run_retention_policy(output)


def _write_checkpoint_unlocked(output: Path, key: str, state: dict, *, page_cache: dict | None = None) -> None:
    store = Store(output)
    try:
        def encode(value):
            if isinstance(value, bytes):
                return _write_blob(output, value)
            raise TypeError("Unsupported checkpoint value")

        store.db.execute('CREATE TABLE IF NOT EXISTS checkpoint_pages (digest TEXT PRIMARY KEY, data TEXT NOT NULL)')
        document = dict(state)
        pages = {}
        cache_updates = {}
        for url, page in state.get('captures', {}).items():
            saved = page_cache.get(url) if page_cache is not None else None
            if saved is not None and saved[0] is page:
                digest = saved[1]
            else:
                serialized_page = json.dumps(page, ensure_ascii=False, default=encode)
                digest = hashlib.sha256(serialized_page.encode('utf-8')).hexdigest()
                store.db.execute('INSERT OR IGNORE INTO checkpoint_pages(digest,data) VALUES(?,?)', (digest, serialized_page))
                if page_cache is not None:
                    cache_updates[url] = (page, digest)
            pages[url] = {'__checkpoint_page__': digest}
        if 'captures' in state:
            document['captures'] = pages
        serialized = json.dumps(document, ensure_ascii=False, default=encode)
        with store.db:
            store.db.execute("INSERT OR REPLACE INTO catalog_checkpoints(id,data) VALUES(?,?)",
                             (key, serialized))
        if page_cache is not None:
            page_cache.update(cache_updates)
    finally:
        store.close()
