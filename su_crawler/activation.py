"""Run-bound verification receipts and safe activation of collection configs.

A receipt is a local audit record.  It proves that the referenced local run and
evidence matched the config when checked; it is not a signed attestation.
"""
from __future__ import annotations

from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any
import hashlib
import json
import os
import tempfile

from .config import config_fingerprint, load_config
from .models import CollectionConfig, FetchResult, resolve_path, stable_id
from .pipeline import execute
from .storage import Store


RECEIPT_SCHEMA = "source-ledger/verification-receipt/v1"
SAMPLES_SCHEMA = "source-ledger/known-samples/v1"
MAX_RECEIPT_AGE_HOURS = 24.0


def _json_hash(value: Any) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _file_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _aware_time(value: str, label: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (AttributeError, ValueError) as exc:
        raise ValueError(f"Invalid {label} timestamp") from exc
    if parsed.tzinfo is None:
        raise ValueError(f"Invalid {label} timestamp: timezone is required")
    return parsed.astimezone(timezone.utc)


def _atomic_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        # link() publishes the fully written inode without replacing an existing
        # path.  This makes receipts and activated configs immutable even when
        # two processes race after an earlier existence check.
        os.link(temporary, path)
        Path(temporary).unlink()
    except BaseException:
        try:
            Path(temporary).unlink()
        except FileNotFoundError:
            pass
        raise


def _evidence_problem(row: dict[str, Any], config: CollectionConfig,
                      cache: dict | None = None) -> str | None:
    source = next((item for item in config.sources if item.id == row.get("source_id")), None)
    product = next((item for item in config.products if item.id == row.get("product_id")), None)
    if source is None or product is None:
        return "observation references an unknown source or product"
    path_text = row.get("evidence_path")
    expected_hash = row.get("evidence_sha256")
    if not path_text or not expected_hash:
        return "observation has no source evidence hash"
    path = Path(path_text)
    if not path.is_file():
        return "source evidence file is missing"
    # A cache lives only for this assessment; a later activation always reads
    # evidence again. Thousands of quote rows can share one source file.
    cache = cache if cache is not None else {}
    resolved = str(path.resolve())
    hash_key = ('hash', resolved)
    if hash_key not in cache:
        cache[hash_key] = _file_hash(path)
    if cache[hash_key] != expected_hash:
        return "source evidence hash mismatch"
    # Bind the stored fields to the evidence bytes using the same deterministic
    # extractor.  A matching file hash alone cannot detect a modified database.
    from .extraction import extract
    media_type = {
        ".html": "text/html", ".htm": "text/html", ".csv": "text/csv",
        ".json": "application/json", ".pdf": "application/pdf", ".xlsx":
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    }.get(path.suffix.lower(), "application/octet-stream")
    # Adapter visibility depends on how these bytes were captured. Rebuild that
    # context from the run-bound fetch receipt instead of inventing a browser
    # capture or downgrading a retained rendered snapshot to a static response.
    backend = 'verification'
    final_url = row.get('source_url') or source.location
    if source.adapter:
        receipt_path = path.with_name(f"{expected_hash}.{stable_id(row.get('collected_at'), source.id)}.json")
        receipt_key = ('receipt', str(receipt_path))
        if receipt_key not in cache:
            try:
                cache[receipt_key] = json.loads(receipt_path.read_text(encoding='utf-8'))
            except (OSError, ValueError):
                return 'source fetch receipt is missing or invalid'
        receipt = cache[receipt_key]
        if (not isinstance(receipt, dict) or receipt.get('hash') != expected_hash
                or receipt.get('source_id') != source.id or receipt.get('at') != row.get('collected_at')
                or receipt.get('url') != final_url or receipt.get('account_scope') != source.account_scope
                or receipt.get('recipe_version') != source.recipe_version
                or receipt.get('backend') not in {'file', 'http', 'playwright', 'crawl4ai'}):
            return 'source fetch receipt does not match the observation'
        backend = receipt['backend']
    extract_key = ('extract', resolved, expected_hash, _json_hash(asdict(source)), backend, final_url)
    if extract_key not in cache:
        cache[extract_key] = extract(FetchResult(source.id, "fetched", backend,
            content=path.read_bytes(), media_type=media_type, final_url=final_url), source)
    candidates = cache[extract_key]
    bound = []
    for candidate in candidates:
        candidate_raw = {
            **candidate.fields,
            **{f"spec:{key.removeprefix('spec:')}": value for key, value in candidate.specs.items()},
        }
        if candidate.locator == row.get("locator") and candidate_raw == (row.get("raw_fields") or {}) and candidate.evidence == (row.get("evidence") or {}):
            bound.append(candidate)
    if not bound:
        return "stored observation does not re-extract from source evidence"
    from .validation import validate
    candidate = bound[0]
    recomputed = validate(
        candidate, product, source, run_id=row.get("run_id", ""), task_id=row.get("task_id", ""),
        evidence_path=row["evidence_path"], evidence_sha256=row["evidence_sha256"],
        collected_at=row.get("collected_at", ""), source_url=row.get("source_url", ""),
    ).to_dict()
    semantics = {
        "id", "status", "reason", "raw_fields", "evidence", "locator", "extraction_method",
        "amount", "currency", "unit", "pack_quantity", "normalized_amount", "calculation",
        "comparable", "comparison_key",
    }
    semantics.update(key for key in (
        'value_origin', 'source_visibility', 'derived_values', 'derived_amount',
        'price_profile', 'verification_level', 'rental_conditions', 'review_flags'
    ) if key in row)
    if any(recomputed.get(key) != row.get(key) for key in semantics):
        return "stored observation semantics do not match source evidence"
    raw_fields = row.get("raw_fields") or {}
    evidence = row.get("evidence") or {}
    for key, expected in product.identifiers.items():
        actual = raw_fields.get(key)
        proof = evidence.get(key)
        if str(actual).strip() != str(expected).strip():
            return f"identifier mismatch: {key}"
        if not isinstance(proof, dict) or not str(proof.get("location", "")).strip():
            return f"identifier evidence missing: {key}"
        if proof.get("raw") is None or str(proof["raw"]).strip() != str(actual).strip():
            return f"identifier evidence mismatch: {key}"
    return None


def _current_problem(row: dict[str, Any], config: CollectionConfig, now: datetime) -> str | None:
    source = next((item for item in config.sources if item.id == row.get("source_id")), None)
    if source is None:
        return "observation references an unknown source"
    try:
        collected = _aware_time(row["collected_at"], "observation")
    except (KeyError, ValueError) as exc:
        return str(exc)
    if collected > now + timedelta(minutes=5):
        return "observation timestamp is in the future"
    if now - collected > timedelta(hours=source.freshness_hours):
        return "observation is stale"
    from .validation import _expiry
    expired, problem = _expiry((row.get("raw_fields") or {}).get("valid_to"), now.isoformat())
    if problem:
        return "observation valid_to is invalid"
    if expired:
        return "observation commercial validity has expired"
    return None


def _load_samples(path: str | Path | None) -> list[dict[str, str]]:
    if path is None:
        return []
    raw = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    if not isinstance(raw, dict) or raw.get("schema") != SAMPLES_SCHEMA or not isinstance(raw.get("samples"), list):
        raise ValueError(f"Known samples must use schema {SAMPLES_SCHEMA}")
    allowed = {"source_id", "product_id", "price", "currency"}
    samples: list[dict[str, str]] = []
    for index, item in enumerate(raw["samples"]):
        if not isinstance(item, dict) or set(item) != allowed or any(item.get(key) is None for key in allowed):
            raise ValueError(f"Known sample {index + 1} must contain exactly source_id, product_id, price, currency")
        try:
            amount = Decimal(str(item["price"]))
        except InvalidOperation as exc:
            raise ValueError(f"Known sample {index + 1} price is not a decimal") from exc
        if not amount.is_finite() or amount < 0:
            raise ValueError(f"Known sample {index + 1} price must be a finite non-negative decimal")
        samples.append({key: str(item[key]) for key in allowed})
    return samples


def _assess(config: CollectionConfig, run: dict[str, Any], tasks: list[dict[str, Any]],
            observations: list[dict[str, Any]], samples: list[dict[str, str]], now: datetime) -> tuple[list[str], list[dict[str, Any]]]:
    reasons: list[str] = []
    expected_pairs = {(source.id, product_id) for source in config.sources for product_id in source.product_ids}
    assigned_products = {product_id for _, product_id in expected_pairs}
    for product in config.products:
        if product.id not in assigned_products:
            reasons.append(f"product is not assigned to any source: {product.id}")
    task_pairs = {(task["source_id"], task["product_id"]) for task in tasks}
    if run.get("status") != "completed":
        reasons.append(f"run is not completed: {run.get('status')}")
    if task_pairs != expected_pairs:
        reasons.append("run tasks do not exactly cover all configured source/product pairs")
    unfinished = [task for task in tasks if task.get("status") != "verified"]
    if unfinished:
        reasons.append("not all configured source/product tasks completed as verified")

    eligible_rows: dict[tuple[str, str], list[dict[str, Any]]] = {}
    proofs: list[dict[str, Any]] = []
    evidence_cache: dict = {}
    for row in observations:
        pair = (row.get("source_id"), row.get("product_id"))
        problem = _evidence_problem(row, config, evidence_cache) or _current_problem(row, config, now)
        if row.get("status") == "verified" and row.get("comparable") is True and problem is None:
            eligible_rows.setdefault(pair, []).append(row)
            proofs.append({
                "observation_id": row.get("id"),
                "source_id": pair[0],
                "product_id": pair[1],
                "collected_at": row.get("collected_at"),
                "evidence_path": str(Path(row["evidence_path"]).resolve()),
                "evidence_sha256": row.get("evidence_sha256"),
                "observation_sha256": _json_hash(row),
            })
        elif pair in expected_pairs and problem:
            reasons.append(f"{pair[0]}/{pair[1]}: {problem}")
    missing = sorted(expected_pairs - set(eligible_rows))
    reasons.extend(f"{source_id}/{product_id}: no current verified comparable observation" for source_id, product_id in missing)

    for sample in samples:
        pair = (sample["source_id"], sample["product_id"])
        if pair not in expected_pairs:
            reasons.append(f"known sample references an unconfigured pair: {pair[0]}/{pair[1]}")
            continue
        try:
            expected_amount = Decimal(sample["price"])
            matched = any(
                Decimal(str(row.get("amount"))) == expected_amount and str(row.get("currency")) == sample["currency"]
                for row in eligible_rows.get(pair, []) if row.get("amount") is not None
            )
        except InvalidOperation:
            matched = False
        if not matched:
            reasons.append(f"known sample mismatch: {pair[0]}/{pair[1]}")
    return list(dict.fromkeys(reasons)), proofs


def verify_config(config_path: str | Path, *, receipt_path: str | Path,
                  samples_path: str | Path | None = None) -> dict[str, Any]:
    """Execute a config and write a local receipt describing activation eligibility."""
    config_file = Path(config_path).resolve()
    receipt_file = Path(receipt_path).resolve()
    config = load_config(config_file)
    protected = {config_file}
    if samples_path is not None:
        protected.add(Path(samples_path).resolve())
    for source in config.sources:
        if source.kind == "file":
            root = Path(source.file_root) if source.file_root else Path(config.base_dir)
            protected.add(resolve_path(str(root), source.location))
    if receipt_file in protected:
        raise ValueError("Receipt path cannot overwrite an input config, samples file, or source artifact")
    if receipt_file.exists():
        raise FileExistsError(f"Verification receipt already exists: {receipt_file}")
    samples = _load_samples(samples_path)
    run = execute(config)
    store = Store(Path(config.output_dir))
    try:
        tasks = store.tasks(run["id"])
        observations = store.observations(run["id"])
    finally:
        store.close()
    now = datetime.now(timezone.utc)
    reasons, proofs = _assess(config, run, tasks, observations, samples, now)
    receipt: dict[str, Any] = {
        "schema": RECEIPT_SCHEMA,
        "config_fingerprint": config_fingerprint(config),
        "config_path": str(config_file),
        "run_id": run["id"],
        "validated_at": now.isoformat(),
        "max_receipt_age_hours": min(MAX_RECEIPT_AGE_HOURS, *(source.freshness_hours for source in config.sources)),
        "eligible": not reasons,
        "reasons": reasons,
        "evidence_proofs": proofs,
        "known_samples_sha256": _file_hash(Path(samples_path).resolve()) if samples_path is not None else None,
        "known_samples_path": str(Path(samples_path).resolve()) if samples_path is not None else None,
        "known_samples": samples,
        "note": "Local audit record; not a cryptographic attestation.",
    }
    _atomic_json(receipt_file, receipt)
    return receipt


def _activated_document(config: CollectionConfig) -> dict[str, Any]:
    products = [asdict(product) for product in config.products]
    sources: list[dict[str, Any]] = []
    for source in config.sources:
        item = asdict(source)
        if source.kind == "file":
            root = Path(source.file_root).resolve() if source.file_root else Path(config.base_dir).resolve()
            item["file_root"] = str(root)
            item["location"] = str(resolve_path(str(root), source.location))
        sources.append(item)
    return {
        "name": config.name,
        "products": products,
        "sources": sources,
        "output_dir": str(Path(config.output_dir).resolve()),
        "demo": config.demo,
        "max_run_seconds": config.max_run_seconds,
    }


def activate_config(config_path: str | Path, *, receipt_path: str | Path,
                    output_path: str | Path) -> dict[str, Any]:
    """Revalidate a receipt and atomically create a location-independent config."""
    config_file = Path(config_path).resolve()
    receipt_file = Path(receipt_path).resolve()
    output_file = Path(output_path).resolve()
    if len({config_file, receipt_file, output_file}) != 3:
        raise ValueError("Input config, receipt, and activation output must be different files")
    if output_file.exists():
        raise FileExistsError(f"Activation output already exists: {output_file}")
    config = load_config(config_file)
    receipt = json.loads(receipt_file.read_text(encoding="utf-8-sig"))
    if receipt.get("schema") != RECEIPT_SCHEMA:
        raise ValueError("Unsupported verification receipt schema")
    if receipt.get("eligible") is not True or receipt.get("reasons"):
        raise ValueError("Verification receipt is not eligible for activation")
    if receipt.get("config_fingerprint") != config_fingerprint(config):
        raise ValueError("Config changed after verification")
    validated_at = _aware_time(receipt.get("validated_at"), "receipt")
    now = datetime.now(timezone.utc)
    max_age = min(MAX_RECEIPT_AGE_HOURS, *(source.freshness_hours for source in config.sources))
    if validated_at > now + timedelta(minutes=5) or now - validated_at > timedelta(hours=max_age):
        raise ValueError("Verification receipt is stale")

    store = Store(Path(config.output_dir))
    try:
        run = store.run(receipt["run_id"])
        tasks = store.tasks(receipt["run_id"])
        observations = store.observations(receipt["run_id"])
    finally:
        store.close()
    if run.get("config_hash") != config_fingerprint(config):
        raise ValueError("Verified run does not match the current config")
    recorded_samples = receipt.get("known_samples")
    if not isinstance(recorded_samples, list):
        raise ValueError("Verification receipt has no canonical known-samples record")
    sample_path = receipt.get("known_samples_path")
    sample_hash = receipt.get("known_samples_sha256")
    if (sample_path is None) != (sample_hash is None):
        raise ValueError("Verification receipt has an incomplete known-samples reference")
    if sample_path is not None:
        sample_file = Path(sample_path)
        if not sample_file.is_file() or _file_hash(sample_file) != sample_hash:
            raise ValueError("Known samples changed or are missing after verification")
        if _load_samples(sample_file) != recorded_samples:
            raise ValueError("Known samples do not match the verification receipt")
    reasons, current_proofs = _assess(config, run, tasks, observations, recorded_samples, now)
    if reasons:
        raise ValueError("Verified run is no longer eligible: " + "; ".join(reasons))
    if current_proofs != receipt.get("evidence_proofs"):
        raise ValueError("Verified observations or evidence changed after verification")
    document = _activated_document(config)
    _atomic_json(output_file, document)
    return {
        "status": "activated",
        "output_path": str(output_file),
        "run_id": receipt["run_id"],
        "config_fingerprint": receipt["config_fingerprint"],
    }
