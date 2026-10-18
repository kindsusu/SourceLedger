"""Replay frozen Amazoncar evidence, validate once, and export one audit workbook.

No network or model calls are made here. Fresh acquisition is owned by
amazoncar_collection; this workflow always uses the snapshot's original time.
"""
from __future__ import annotations

import argparse
from collections import Counter
from dataclasses import asdict
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import time

from .activation import _evidence_problem
from .adapters import extract_adapter
from .models import CollectionConfig, FetchResult, Product, Source, stable_id
from .pipeline import execute
from .report import export_report
from .storage import Store


def read(path: Path):
    return json.loads(path.read_text(encoding="utf-8-sig"))


def write(path: Path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    pending = path.with_suffix(path.suffix + ".pending")
    pending.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    pending.replace(path)


def evidence_bytes(root: Path, ref: dict) -> bytes:
    path = (root / ref["path"].replace("\\", "/")).resolve()
    if not path.is_relative_to(root.resolve()):
        raise ValueError("Evidence reference leaves its capture root")
    payload = path.read_bytes()
    if hashlib.sha256(payload).hexdigest() != ref["sha256"]:
        raise ValueError("Evidence hash mismatch: " + str(path))
    return payload


def details(root: Path, items: list[dict]):
    aggregate = None
    for item in items:
        href = item["href"]
        key = href.split("/cars/", 1)[1].replace("/", "-")
        path = root / "checkpoints" / "details" / (key + ".json")
        if path.is_file():
            entry = read(path)
            if entry.get("href") != href:
                raise ValueError("Checkpoint identity mismatch")
            value = entry["data"]
        else:
            if aggregate is None:
                old = root / "details-checkpoint.json"
                aggregate = read(old) if old.is_file() else {}
            value = aggregate.get(href)
        yield item, value


def run(capture_root: Path, manifest: Path, output: Path, *, export: bool = True) -> dict:
    capture_root, output = capture_root.resolve(), output.resolve()
    if capture_root == output:
        raise ValueError("Output must be separate from immutable captured evidence")
    started = time.perf_counter()
    started_utc = datetime.now(timezone.utc).isoformat()
    output.mkdir(parents=True, exist_ok=True)
    if (output / "prices.sqlite3").exists():
        raise ValueError("Use a new output directory; existing verification runs are immutable")
    items = read(manifest)["products"]
    if len({x["href"] for x in items}) != len(items):
        raise ValueError("Fixed manifest contains duplicate IDs")
    products, sources, prefetched = [], [], {}
    catalog, options, conditions, gaps, price_differences = [], [], [], [], []
    quote_count = 0
    captured_ids = []
    expected_original_quotes = 0
    product_by_id = {}

    def add_capture(cap: dict, key: str, profile: str):
        payload = evidence_bytes(capture_root, cap["html"])
        for kind in ("rendered_text", "selection_proof"):
            if cap.get(kind):
                evidence_bytes(capture_root, cap[kind])
        sid = "amazon-" + stable_id(key, cap["label"])
        source = Source(sid, "Amazoncar " + key, "web", cap["url"], [],
                        backends=["playwright"], allowed_domains=["www.amazoncar.co.kr"],
                        adapter="amazoncar", recipe_version="amazoncar-evidence-v1")
        response = FetchResult(sid, "fetched", "playwright", payload, final_url=cap["url"],
                               fetched_at=cap["captured_utc"],
                               trace=[{"event": "captured_selection", "proof": cap.get("selection_proof")},
                                      {"event": "amazoncar_capture_integrity", "flags": cap.get("integrity_flags", [])}])
        if cap.get("integrity_flags"):
            gaps.append({"product": key, "capture": cap["label"],
                         "reason": "capture_transition_needs_review", "flags": cap["integrity_flags"]})
        candidates = extract_adapter(response, source)
        identities = {}
        for candidate in candidates:
            fields = candidate.fields
            identity_key = "item_id" if fields.get("item_id") else "vehicle_class"
            identity = fields.get(identity_key)
            if not identity:
                raise ValueError("Adapter did not preserve a source identity")
            identities[(identity_key, str(identity))] = fields.get("name") or identity
        for (identity_key, identity), name in identities.items():
            pid = "amazon-" + stable_id(key, identity_key, identity)
            if pid not in product_by_id:
                product = Product(pid, str(name), identifiers={identity_key: identity}, price_profile=profile)
                product_by_id[pid] = product
                products.append(product)
            source.product_ids.append(pid)
        if candidates:
            sources.append(source)
            prefetched[sid] = (response, candidates)
        return candidates

    for index, (item, value) in enumerate(details(capture_root, items), 1):
        href = item["href"]
        if not isinstance(value, dict) or "error" in value:
            gaps.append({"product": href, "reason": "missing_capture" if not value else "capture_failed"})
            continue
        caps = value.get("captures", [])
        if not caps:
            gaps.append({"product": href, "reason": "empty_capture"})
            continue
        captured_ids.append(href)
        actual = []
        for cap in caps:
            mode = cap["label"].rsplit("-", 1)[-1]
            candidates = add_capture(cap, value["id"], "rental")
            if not candidates:
                gaps.append({"product": href, "mode": mode, "reason": "no_reextractable_quotes"})
            for candidate in candidates:
                actual.append((mode, str(candidate.fields.get("term_months")), str(candidate.fields.get("price"))))
            for opt in value.get("options", []):
                if opt.get("mode") == mode:
                    options.append([value["id"], mode, opt.get("raw"), opt.get("surcharge_raw"), cap["url"], cap["captured_utc"], cap["html"]["sha256"]])
            for cond in cap.get("conditions", []):
                conditions.append([value["id"], mode, cond.get("label"), cond.get("display"),
                                   cond.get("selected"), cond.get("choices"), cap["url"], cap["captured_utc"]])
        expected = [(q["mode"], str(q["term_months"]), str(q["amount"])) for q in value.get("quotes", [])]
        expected_original_quotes += len(expected)
        if Counter(actual) != Counter(expected):
            price_differences.append({"product": href, "missing": list((Counter(expected) - Counter(actual)).elements()),
                                      "extra": list((Counter(actual) - Counter(expected)).elements())})
        quote_count += len(actual)
        catalog.append([value["id"], href, (value.get("name") or [None])[0], len(caps), len(actual),
                        value.get("mode_coverage"), item.get("text")])
        if index % 100 == 0:
            write(output / "progress.json", {"phase": "offline_extraction", "processed": index, "expected": len(items)})
    short_path = capture_root / "short-term-captures.json"
    short_count = 0
    if short_path.is_file():
        for cap in read(short_path):
            short_count += len(add_capture(cap, "short-term", "unit"))
    else:
        gaps.append({"product": "/rentPrice", "reason": "short_term_capture_missing"})
    extraction_seconds = time.perf_counter() - started
    if not products or not sources:
        raise ValueError("No observations could be reproduced from evidence")
    config = CollectionConfig("Amazoncar evidence replay", products, sources, str(output), str(output), max_run_seconds=7200)
    cfg = asdict(config)
    cfg.pop("base_dir")
    write(output / "config.json", cfg)
    write(output / "progress.json", {"phase": "database_ingestion", "quotes": quote_count, "short_term": short_count})
    phase = time.perf_counter()
    result = execute(config, prefetched=prefetched, export=False)
    ingestion_seconds = time.perf_counter() - phase
    del prefetched
    store = Store(output)
    try:
        rows, tasks = store.observations(result["id"]), store.tasks(result["id"])
        phase = time.perf_counter()
        cache = {}
        replay_failures = []
        for row in rows:
            problem = _evidence_problem(row, config, cache)
            if problem:
                replay_failures.append({"id": row["id"], "reason": problem})
        replay_seconds = time.perf_counter() - phase
        reasons = Counter(reason.strip() for row in rows for reason in row.get("reason", "").split(";") if reason.strip())
        summary = {"started_utc": started_utc, "expected_products": len(items), "captured_products": len(captured_ids),
                   "missing_product_ids": sorted({x["href"] for x in items} - set(captured_ids)),
                   "gaps": gaps, "quote_rows": quote_count, "short_term_rows": short_count,
                   "old_capture_quote_rows": expected_original_quotes, "price_reextraction_differences": price_differences,
                   "observations": len(rows), "status_counts": dict(Counter(r["status"] for r in rows)),
                   "comparable_count": sum(r["comparable"] for r in rows), "reason_counts": dict(reasons),
                   "evidence_replay_failures": replay_failures, "replayed_observations": len(rows),
                   "sqlite_quick_check": store.db.execute("PRAGMA quick_check").fetchone()[0],
                   "run": result, "seconds": {"extraction": extraction_seconds, "ingestion": ingestion_seconds,
                                              "evidence_replay": replay_seconds}}
        summary["observation_count_matches"] = len(rows) == quote_count + short_count
        summary["evidence_gate_pass"] = (not (gaps or price_differences or replay_failures or summary["missing_product_ids"])
                                         and summary["observation_count_matches"] and summary["sqlite_quick_check"] == "ok")
        collection_summary = capture_root / "summary.json"
        if collection_summary.is_file():
            acquisition = read(collection_summary)
            summary["acquisition"] = {key: acquisition.get(key) for key in (
                "counts", "complete_fixed_cohort", "complete_current_catalog", "final_catalog",
                "catalog_issues", "no_quote_modes", "supplemental_capture")}
        write(output / "verification.json", summary)
        if export:
            supplemental = [
                {"name": "Catalog", "headers": ["Product ID", "Path", "Vehicle", "Captured modes", "Quote count", "Mode coverage", "Listing text"], "rows": catalog},
                {"name": "Options", "headers": ["Product ID", "Mode", "Option text", "Surcharge raw", "URL", "Captured UTC", "Evidence hash"], "rows": options},
                {"name": "Conditions", "headers": ["Product ID", "Mode", "Label", "Display raw", "Selected", "Choices", "URL", "Captured UTC"], "rows": conditions},
                {"name": "Acquisition Audit", "headers": ["Metric", "Value"], "rows": [[k, v] for k, v in summary.items() if k != "run"]},
            ]
            path = output / "Amazoncar.xlsx"
            phase = time.perf_counter()
            export_report(config, result, tasks, rows, path, supplemental_sheets=supplemental)
            summary["seconds"]["export"] = time.perf_counter() - phase
            with store.db:
                store.db.execute("UPDATE runs SET report_path=? WHERE id=?", (str(path), result["id"]))
            summary["report_path"] = str(path)
            summary["report_bytes"] = path.stat().st_size
        summary["seconds"]["total"] = time.perf_counter() - started
        summary["ended_utc"] = datetime.now(timezone.utc).isoformat()
        write(output / "verification.json", summary)
        write(output / "progress.json", {"phase": "complete", "gate_pass": summary["evidence_gate_pass"], "seconds": summary["seconds"]})
        return summary
    finally:
        store.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--captures", required=True, type=Path)
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--no-export", action="store_true")
    args = parser.parse_args()
    summary = run(args.captures, args.manifest, args.output, export=not args.no_export)
    print(json.dumps({k: summary[k] for k in ("evidence_gate_pass", "captured_products", "quote_rows", "short_term_rows", "status_counts", "seconds")}, ensure_ascii=False))
    return 0 if summary["evidence_gate_pass"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
