from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
import hashlib
import json

import pytest

from su_crawler.activation import SAMPLES_SCHEMA, activate_config, verify_config
from su_crawler.config import load_config
from su_crawler.pipeline import execute
from su_crawler.storage import Store


def _write_fixture(tmp_path: Path, *, products: tuple[str, ...] = ("A",)) -> Path:
    rows = ["model,price,currency,unit,pack,tax,type,basis"]
    rows.extend(f"TEST-{item},{10 if item == 'A' else 20},USD,each,1,exclusive,list,each" for item in products)
    (tmp_path / "prices.csv").write_text("\n".join(rows) + "\n", encoding="utf-8")
    config = {
        "name": "Activation fixture",
        "output_dir": "workspace",
        "products": [
            {"id": item.lower(), "name": item, "identifiers": {"model": f"TEST-{item}"}}
            for item in products
        ],
        "sources": [{
            "id": "supplier", "name": "Supplier", "kind": "file", "location": "prices.csv",
            "product_ids": [item.lower() for item in products],
            "columns": {"model": "model", "price": "price", "currency": "currency", "unit": "unit",
                        "pack_quantity": "pack", "tax": "tax", "price_type": "type", "price_basis": "basis"},
            "freshness_hours": 24,
        }],
    }
    path = tmp_path / "draft.json"
    path.write_text(json.dumps(config), encoding="utf-8")
    return path


def _samples(tmp_path: Path, *, price: str = "10") -> Path:
    path = tmp_path / "samples.json"
    path.write_text(json.dumps({"schema": SAMPLES_SCHEMA, "samples": [{
        "source_id": "supplier", "product_id": "a", "price": price, "currency": "USD",
    }]}), encoding="utf-8")
    return path


def test_verify_activate_and_run_from_another_directory(tmp_path, monkeypatch):
    config_path = _write_fixture(tmp_path)
    inputs = tmp_path / "inputs"
    inputs.mkdir()
    (tmp_path / "prices.csv").rename(inputs / "prices.csv")
    draft = json.loads(config_path.read_text(encoding="utf-8"))
    draft["sources"][0]["file_root"] = "inputs"
    config_path.write_text(json.dumps(draft), encoding="utf-8")
    receipt_path = tmp_path / "receipt.json"
    result = verify_config(config_path, receipt_path=receipt_path, samples_path=_samples(tmp_path))
    assert result["eligible"] is True
    assert result["evidence_proofs"]

    activated = tmp_path / "released" / "active.json"
    activation = activate_config(config_path, receipt_path=receipt_path, output_path=activated)
    assert activation["status"] == "activated"
    active_raw = json.loads(activated.read_text(encoding="utf-8"))
    assert Path(active_raw["sources"][0]["location"]) == (inputs / "prices.csv").resolve()
    assert Path(active_raw["sources"][0]["file_root"]) == inputs.resolve()
    monkeypatch.chdir(tmp_path / "released")
    run = execute(load_config(activated))
    assert run["status"] == "completed"


def test_known_sample_mismatch_writes_ineligible_receipt(tmp_path):
    config_path = _write_fixture(tmp_path)
    result = verify_config(config_path, receipt_path=tmp_path / "receipt.json", samples_path=_samples(tmp_path, price="99"))
    assert result["eligible"] is False
    assert any("known sample mismatch" in reason for reason in result["reasons"])
    with pytest.raises(ValueError, match="not eligible"):
        activate_config(config_path, receipt_path=tmp_path / "receipt.json", output_path=tmp_path / "active.json")


def test_missing_identifier_and_incomplete_product_are_ineligible(tmp_path):
    config_path = _write_fixture(tmp_path, products=("A", "B"))
    # Remove B and the identifier column from A, leaving both configured tasks incomplete.
    (tmp_path / "prices.csv").write_text(
        "model,price,currency,unit,pack,tax,type,basis\n,10,USD,each,1,exclusive,list,each\n",
        encoding="utf-8",
    )
    result = verify_config(config_path, receipt_path=tmp_path / "receipt.json")
    assert result["eligible"] is False
    assert any("a" in reason and "no current" in reason for reason in result["reasons"])
    assert any("b" in reason and "no current" in reason for reason in result["reasons"])


def test_activation_rejects_config_recipe_and_evidence_changes(tmp_path):
    config_path = _write_fixture(tmp_path)
    receipt_path = tmp_path / "receipt.json"
    result = verify_config(config_path, receipt_path=receipt_path)
    assert result["eligible"] is True

    raw = json.loads(config_path.read_text(encoding="utf-8"))
    raw["sources"][0]["recipe_version"] = "2"
    config_path.write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(ValueError, match="Config changed"):
        activate_config(config_path, receipt_path=receipt_path, output_path=tmp_path / "changed.json")

    raw["sources"][0].pop("recipe_version")
    config_path.write_text(json.dumps(raw), encoding="utf-8")
    proof_path = Path(result["evidence_proofs"][0]["evidence_path"])
    proof_path.write_bytes(proof_path.read_bytes() + b"tampered")
    with pytest.raises(ValueError, match="hash mismatch"):
        activate_config(config_path, receipt_path=receipt_path, output_path=tmp_path / "tampered.json")


def test_activation_rejects_database_tampering_and_stale_receipt(tmp_path):
    config_path = _write_fixture(tmp_path)
    receipt_path = tmp_path / "receipt.json"
    receipt = verify_config(config_path, receipt_path=receipt_path)
    config = load_config(config_path)
    store = Store(Path(config.output_dir))
    row = store.observations(receipt["run_id"])[0]
    row["raw_fields"]["price"] = "999"
    row["amount"] = "999"
    with store.db:
        store.db.execute("UPDATE observations SET data=? WHERE id=?", (json.dumps(row), row["id"]))
    store.close()
    with pytest.raises(ValueError, match="does not re-extract"):
        activate_config(config_path, receipt_path=receipt_path, output_path=tmp_path / "db-tampered.json")

    # Restore a clean run/receipt, then age only the receipt.
    receipt_path.unlink()
    receipt = verify_config(config_path, receipt_path=receipt_path)
    receipt["validated_at"] = (datetime.now(timezone.utc) - timedelta(hours=25)).isoformat()
    receipt_path.write_text(json.dumps(receipt), encoding="utf-8")
    with pytest.raises(ValueError, match="receipt is stale"):
        activate_config(config_path, receipt_path=receipt_path, output_path=tmp_path / "stale.json")


def test_activation_refuses_overwrite_and_path_aliases(tmp_path):
    config_path = _write_fixture(tmp_path)
    receipt_path = tmp_path / "receipt.json"
    verify_config(config_path, receipt_path=receipt_path)
    existing = tmp_path / "active.json"
    existing.write_text("keep", encoding="utf-8")
    with pytest.raises(FileExistsError):
        activate_config(config_path, receipt_path=receipt_path, output_path=existing)
    with pytest.raises(ValueError, match="different files"):
        activate_config(config_path, receipt_path=receipt_path, output_path=config_path)
    with pytest.raises(FileExistsError, match="already exists"):
        verify_config(config_path, receipt_path=receipt_path)
    with pytest.raises(ValueError, match="source artifact"):
        verify_config(config_path, receipt_path=tmp_path / "prices.csv")


def test_activation_rejects_changed_observation_with_recomputed_evidence_hash(tmp_path):
    config_path = _write_fixture(tmp_path)
    receipt_path = tmp_path / "receipt.json"
    receipt = verify_config(config_path, receipt_path=receipt_path)
    config = load_config(config_path)
    evidence_path = Path(receipt["evidence_proofs"][0]["evidence_path"])
    evidence_path.write_text(evidence_path.read_text(encoding="utf-8").replace(",10,", ",11,"), encoding="utf-8")
    changed_hash = hashlib.sha256(evidence_path.read_bytes()).hexdigest()
    store = Store(Path(config.output_dir))
    row = store.observations(receipt["run_id"])[0]
    row["evidence_sha256"] = changed_hash
    with store.db:
        store.db.execute("UPDATE observations SET data=? WHERE id=?", (json.dumps(row), row["id"]))
    store.close()
    with pytest.raises(ValueError, match="does not re-extract"):
        activate_config(config_path, receipt_path=receipt_path, output_path=tmp_path / "active.json")


def test_activation_rejects_changed_derived_semantics(tmp_path):
    config_path = _write_fixture(tmp_path)
    receipt_path = tmp_path / "receipt.json"
    receipt = verify_config(config_path, receipt_path=receipt_path)
    config = load_config(config_path)
    store = Store(Path(config.output_dir))
    row = store.observations(receipt["run_id"])[0]
    row["amount"] = "999"
    row["normalized_amount"] = "999"
    with store.db:
        store.db.execute("UPDATE observations SET data=? WHERE id=?", (json.dumps(row), row["id"]))
    store.close()
    with pytest.raises(ValueError, match="semantics do not match"):
        activate_config(config_path, receipt_path=receipt_path, output_path=tmp_path / "active.json")


def test_activation_rechecks_samples_and_commercial_expiry(tmp_path, monkeypatch):
    config_path = _write_fixture(tmp_path)
    sample_path = _samples(tmp_path)
    receipt_path = tmp_path / "receipt.json"
    verify_config(config_path, receipt_path=receipt_path, samples_path=sample_path)
    sample_path.write_text(sample_path.read_text(encoding="utf-8").replace('"10"', '"11"'), encoding="utf-8")
    with pytest.raises(ValueError, match="Known samples changed"):
        activate_config(config_path, receipt_path=receipt_path, output_path=tmp_path / "sample-changed.json")

    # New evidence is valid when verified, but expires before activation.
    second = tmp_path / "expiry"
    second.mkdir()
    expiring_config = _write_fixture(second)
    csv = second / "prices.csv"
    expires = datetime.now(timezone.utc) + timedelta(hours=1)
    csv.write_text(csv.read_text(encoding="utf-8").replace("basis\n", "basis,valid_to\n").replace("each\n", f"each,{expires.isoformat()}\n"), encoding="utf-8")
    raw = json.loads(expiring_config.read_text(encoding="utf-8"))
    raw["sources"][0]["columns"]["valid_to"] = "valid_to"
    expiring_config.write_text(json.dumps(raw), encoding="utf-8")
    expiry_receipt = second / "receipt.json"
    result = verify_config(expiring_config, receipt_path=expiry_receipt)
    assert result["eligible"] is True
    import su_crawler.activation as activation_module
    class FutureDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            value = expires + timedelta(hours=1)
            return value if tz is None else value.astimezone(tz)
    monkeypatch.setattr(activation_module, "datetime", FutureDateTime)
    with pytest.raises(ValueError, match="commercial validity has expired"):
        activate_config(expiring_config, receipt_path=expiry_receipt, output_path=second / "active.json")


def test_verify_rejects_unassigned_product(tmp_path):
    config_path = _write_fixture(tmp_path)
    raw = json.loads(config_path.read_text(encoding="utf-8"))
    raw["products"].append({"id": "orphan", "name": "Orphan", "identifiers": {"model": "ORPHAN"}})
    config_path.write_text(json.dumps(raw), encoding="utf-8")
    result = verify_config(config_path, receipt_path=tmp_path / "receipt.json")
    assert result["eligible"] is False
    assert "product is not assigned to any source: orphan" in result["reasons"]
