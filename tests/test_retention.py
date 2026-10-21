"""Retention removes only unreachable checkpoint internals."""
from __future__ import annotations

import json
import os
from pathlib import Path
import time

import pytest

from su_crawler.catalog_checkpoint import checkpoint_lock, read_checkpoint, write_checkpoint
from su_crawler import retention
from su_crawler.retention import audit_storage, maintain_storage
from su_crawler.storage import RunBusyError, Store


def _seed(output: Path):
    report = output / "catalog_1.xlsx"
    report.write_bytes(b"report stays")
    evidence = output / "evidence" / "aa"
    evidence.mkdir(parents=True)
    source = evidence / "page.html"
    source.write_bytes(b"source stays")
    store = Store(output)
    with store.db:
        store.db.execute("INSERT INTO runs(id,config_hash,status,started_at,demo) VALUES(?,?,?,?,?)",
                         ("run", "hash", "completed", "2026-10-16T00:00:00+00:00", 0))
    store.close()
    old = {"captures": {"a": {"content": b"old" * 300}}}
    current = {"captures": {"a": {"content": b"current" * 300}}}
    write_checkpoint(output, "current", old)
    write_checkpoint(output, "current", current)
    write_checkpoint(output, "history", {"captures": {"b": {"content": b"historical" * 300}}})
    return report, source, current


def test_prunes_only_unreachable_checkpoint_material(tmp_path):
    report, source, current = _seed(tmp_path)
    pending = next((tmp_path / "checkpoint_blobs").iterdir()) / ".pending-abandoned"
    pending.write_bytes(b"interrupted write")
    old = time.time() - 9 * 86400
    os.utime(pending, (old, old))
    recent = pending.with_name(".pending-recent")
    recent.write_bytes(b"active grace period")
    before = audit_storage(tmp_path)
    assert before["mode"] == "dry_run"
    assert before["checkpoint_count"] == 2
    assert before["orphan_pages"] == 1
    assert before["orphan_blobs"] == 1
    assert before["stale_pending"] == 1
    assert pending.exists()

    result = maintain_storage(tmp_path, apply=True)
    assert result["removed_pages"] == 1
    assert result["removed_files"] == 2
    assert not pending.exists() and recent.exists()
    assert read_checkpoint(tmp_path, "current") == current
    assert read_checkpoint(tmp_path, "history")["captures"]["b"]["content"] == b"historical" * 300
    assert report.read_bytes() == b"report stays"
    assert source.read_bytes() == b"source stays"
    store = Store(tmp_path)
    try:
        assert store.run("run")["status"] == "completed"
    finally:
        store.close()
    assert maintain_storage(tmp_path, apply=True)["removed_files"] == 0


def test_corrupt_root_aborts_before_removal(tmp_path):
    _seed(tmp_path)
    store = Store(tmp_path)
    with store.db:
        store.db.execute("UPDATE catalog_checkpoints SET data=? WHERE id='current'",
                         (json.dumps({"capture": {"__checkpoint_blob__": "../escape", "sha256": "0" * 64, "size": 1}}),))
    store.close()
    original = sorted(path.name for path in (tmp_path / "checkpoint_blobs").rglob("*.*"))
    with pytest.raises(ValueError, match="blob"):
        maintain_storage(tmp_path, apply=True)
    assert sorted(path.name for path in (tmp_path / "checkpoint_blobs").rglob("*.*")) == original


def test_busy_writer_prevents_maintenance(tmp_path):
    _seed(tmp_path)
    with checkpoint_lock(tmp_path):
        with pytest.raises(RunBusyError):
            maintain_storage(tmp_path, apply=True)


def test_blob_symlink_aborts_before_removal(tmp_path):
    _seed(tmp_path)
    blob = next(path for path in (tmp_path / "checkpoint_blobs").rglob("*.bin"))
    outside = tmp_path.parent / "outside-checkpoint-example"
    outside.write_bytes(b"outside")
    blob.unlink()
    try:
        blob.symlink_to(outside)
    except (OSError, NotImplementedError):
        outside.unlink()
        pytest.skip("Symlinks unavailable")
    try:
        with pytest.raises(ValueError, match="blob entry"):
            maintain_storage(tmp_path, apply=True)
        assert outside.read_bytes() == b"outside"
    finally:
        blob.unlink()
        outside.unlink()


def test_junction_like_blob_shard_aborts_before_removal(tmp_path, monkeypatch):
    _seed(tmp_path)
    shard = next((tmp_path / "checkpoint_blobs").iterdir())
    original = retention._is_linklike
    monkeypatch.setattr(retention, "_is_linklike", lambda path: path == shard or original(path))
    with pytest.raises(ValueError, match="blob shard"):
        maintain_storage(tmp_path, apply=True)


def test_final_checkpoint_runs_daily_policy_and_keeps_history(tmp_path):
    write_checkpoint(tmp_path, "run", {"capture": b"old"})
    write_checkpoint(tmp_path, "run", {"capture": b"new", "result": {"status": "complete"}})
    status = json.loads((tmp_path / ".checkpoint-maintenance" / "status.json").read_text(encoding="utf-8"))
    assert status["status"] == "completed"
    assert status["summary"]["orphan_blobs"] == 1
    assert read_checkpoint(tmp_path, "run")["capture"] == b"new"
    write_checkpoint(tmp_path, "historical", {"capture": b"history", "result": {"status": "complete"}})
    assert read_checkpoint(tmp_path, "historical")["capture"] == b"history"
    assert json.loads((tmp_path / ".checkpoint-maintenance" / "status.json").read_text(encoding="utf-8"))["at"] == status["at"]


def test_invalid_policy_records_error_without_failing_checkpoint(tmp_path):
    (tmp_path / "storage-policy.json").write_text('{"grace_days": 0}', encoding="utf-8")
    write_checkpoint(tmp_path, "run", {"capture": b"source", "result": {"status": "complete"}})
    status = json.loads((tmp_path / ".checkpoint-maintenance" / "status.json").read_text(encoding="utf-8"))
    assert status["status"] == "error"
    assert read_checkpoint(tmp_path, "run")["capture"] == b"source"


def test_conflicting_shared_blob_reference_prevents_removal(tmp_path):
    _seed(tmp_path)
    write_checkpoint(tmp_path, 'shared', {'a': b'shared bytes', 'b': b'shared bytes'})
    store = Store(tmp_path)
    row = json.loads(store.db.execute("SELECT data FROM catalog_checkpoints WHERE id='shared'").fetchone()[0])
    row['b']['size'] += 1
    with store.db:
        store.db.execute("UPDATE catalog_checkpoints SET data=? WHERE id='shared'", (json.dumps(row),))
    store.close()
    before = sorted((tmp_path / 'checkpoint_blobs').rglob('*.*'))
    with pytest.raises(ValueError, match='Conflicting'):
        maintain_storage(tmp_path, apply=True)
    assert sorted((tmp_path / 'checkpoint_blobs').rglob('*.*')) == before


def test_policy_does_not_write_receipt_through_linklike_output(tmp_path, monkeypatch):
    original = retention._is_linklike
    monkeypatch.setattr(retention, '_is_linklike', lambda path: path == tmp_path or original(path))
    assert retention.run_retention_policy(tmp_path)['status'] == 'error'
    assert not (tmp_path / '.checkpoint-maintenance').exists()
