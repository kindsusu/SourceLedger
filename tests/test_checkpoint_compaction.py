"""Checkpoint blobs preserve exact resume evidence without repeated SQLite copies."""
import base64
import json
from pathlib import Path

import pytest

from su_crawler import catalog_checkpoint as checkpoint
from su_crawler.storage import Store


def _saved_json(output: Path, key: str) -> dict:
    store = Store(output)
    try:
        return json.loads(store.db.execute("SELECT data FROM catalog_checkpoints WHERE id=?", (key,)).fetchone()[0])
    finally:
        store.close()


def test_roundtrip_deduplicates_binary_and_does_not_mutate_state(tmp_path):
    html = b"<html>" + b"priced product " * 500 + b"</html>"
    png = b"\x89PNG\r\n\x1a\n" + bytes(range(256)) * 8
    state = {"captures": {"a": {"content": html, "screenshot": png},
                          "b": {"content": html}}, "dispatched": 2}
    write = checkpoint.write_checkpoint
    write(tmp_path, "run", state)
    assert checkpoint.read_checkpoint(tmp_path, "run") == state
    assert state["captures"]["a"]["content"] is html
    assert state["captures"]["a"]["screenshot"] is png
    row = _saved_json(tmp_path, "run")
    assert '__checkpoint_page__' in row['captures']['a']
    assert checkpoint.read_checkpoint(tmp_path, 'run')['captures']['a']['content'] == checkpoint.read_checkpoint(tmp_path, 'run')['captures']['b']['content']
    assert "__bytes__" not in json.dumps(row)
    blobs = list((tmp_path / "checkpoint_blobs").rglob("*.*"))
    # Bytes are stored as captured so evidence can hard-link the same file.
    assert len(blobs) == 2 and {path.suffix for path in blobs} == {".bin"}

    write(tmp_path, "run", {**state, "dispatched": 3})
    assert list((tmp_path / "checkpoint_blobs").rglob("*.*")) == blobs
    assert checkpoint.read_checkpoint(tmp_path, "run")["dispatched"] == 3


def test_progress_update_does_not_serialize_immutable_pages_again(tmp_path, monkeypatch):
    page = {'content': b'original evidence'}
    state = {'captures': {'url': page}, 'dispatched': 1}
    cache = {}
    checkpoint.write_checkpoint(tmp_path, 'run', state, page_cache=cache)
    def fail(*args):
        raise AssertionError('Unchanged page bytes must not be processed again')
    monkeypatch.setattr(checkpoint, '_write_blob', fail)
    checkpoint.write_checkpoint(tmp_path, 'run', {**state, 'dispatched': 2}, page_cache=cache)
    assert checkpoint.read_checkpoint(tmp_path, 'run')['dispatched'] == 2


def test_checkpoint_page_corruption_fails_closed(tmp_path):
    checkpoint.write_checkpoint(tmp_path, 'run', {'captures': {'url': {'content': b'original'}}})
    store = Store(tmp_path)
    with store.db:
        store.db.execute("UPDATE checkpoint_pages SET data='{}'")
    store.close()
    with pytest.raises(ValueError, match='corrupt'):
        checkpoint.read_checkpoint(tmp_path, 'run')


def test_legacy_base64_checkpoint_remains_readable(tmp_path):
    store = Store(tmp_path)
    try:
        with store.db:
            store.db.execute("INSERT INTO catalog_checkpoints(id,data) VALUES (?,?)",
                             ("legacy", json.dumps({"capture": {"__bytes__": base64.b64encode(b"old\x00data").decode()}})))
    finally:
        store.close()
    assert checkpoint.read_checkpoint(tmp_path, "legacy") == {"capture": b"old\x00data"}


def test_missing_and_modified_blobs_fail_closed(tmp_path):
    checkpoint.write_checkpoint(tmp_path, "run", {"capture": b"a" * 4096})
    ref = _saved_json(tmp_path, "run")["capture"]
    path = tmp_path / Path(ref["__checkpoint_blob__"])
    path.write_bytes(b"wrong")
    with pytest.raises(ValueError, match="corrupt|mismatch"):
        checkpoint.read_checkpoint(tmp_path, "run")
    path.unlink()
    with pytest.raises(ValueError, match="missing"):
        checkpoint.read_checkpoint(tmp_path, "run")


def test_untrusted_relative_path_and_symlink_rejected(tmp_path):
    checkpoint.write_checkpoint(tmp_path, "run", {"capture": b"evidence"})
    store = Store(tmp_path)
    try:
        ref = _saved_json(tmp_path, "run")["capture"]
        ref["__checkpoint_blob__"] = "../../outside.bin"
        with store.db:
            store.db.execute("UPDATE catalog_checkpoints SET data=? WHERE id='run'", (json.dumps({"capture": ref}),))
        with pytest.raises(ValueError, match="Invalid checkpoint blob"):
            checkpoint.read_checkpoint(tmp_path, "run")
    finally:
        store.close()

    other = tmp_path / "other"
    other.mkdir()
    blob_root = tmp_path / "checkpoint_blobs"
    import shutil
    shutil.rmtree(blob_root)
    try:
        blob_root.symlink_to(other, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("Symlinks unavailable on this host")
    with pytest.raises(ValueError, match="symlink"):
        checkpoint.write_checkpoint(tmp_path, "new", {"capture": b"new"})


def test_blob_failure_leaves_previous_checkpoint_committed(tmp_path, monkeypatch):
    checkpoint.write_checkpoint(tmp_path, "run", {"dispatched": 1, "capture": b"first"})
    previous = _saved_json(tmp_path, "run")
    def fail(*args):
        raise OSError("disk full")
    monkeypatch.setattr(checkpoint, "_write_blob", fail)
    with pytest.raises(OSError, match="disk full"):
        checkpoint.write_checkpoint(tmp_path, "run", {"dispatched": 2, "capture": b"second"})
    assert _saved_json(tmp_path, "run") == previous
    assert checkpoint.read_checkpoint(tmp_path, "run") == {"dispatched": 1, "capture": b"first"}


def test_legacy_gzip_blob_is_still_read_and_reused(tmp_path):
    import gzip, hashlib
    data = b"<html>" + b"legacy page " * 200 + b"</html>"
    digest = hashlib.sha256(data).hexdigest()
    legacy = tmp_path / "checkpoint_blobs" / digest[:2] / (digest + ".gz")
    legacy.parent.mkdir(parents=True)
    legacy.write_bytes(gzip.compress(data, mtime=0))
    reference = checkpoint._write_blob(tmp_path, data)
    assert reference["__checkpoint_blob__"].endswith(".gz")  # reused, not rewritten
    assert checkpoint._read_blob(tmp_path, reference) == data
