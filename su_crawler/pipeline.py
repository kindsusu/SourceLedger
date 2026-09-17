from __future__ import annotations

from collections import defaultdict
from dataclasses import asdict, replace
from datetime import datetime, timezone, timedelta
from pathlib import Path
import hashlib
import json
import time
import uuid
import re

from .config import config_fingerprint
from .models import Candidate, CollectionConfig, FetchResult, Product, Source, stable_id, utc_now
from .storage import Store, workspace_lock

TERMINAL = {"verified", "review", "price_unavailable", "product_not_found", "blocked", "needs_auth", "tool_unavailable", "policy_denied", "timeout", "failed", "budget_exhausted"}


def _matches(candidate: Candidate, product: Product) -> bool:
    return all(str(candidate.fields.get(key, "")).strip().casefold() == str(value).strip().casefold()
               for key, value in product.identifiers.items())


def _save_evidence(directory: Path, result: FetchResult, source: Source) -> tuple[str, str]:
    digest = hashlib.sha256(result.content).hexdigest()
    dest = directory / "evidence" / digest[:2]
    dest.mkdir(parents=True, exist_ok=True)
    suffix = {"text/html": ".html", "text/csv": ".csv", "application/json": ".json", "application/pdf": ".pdf",
              "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": ".xlsx"}.get(result.media_type.split(";")[0], ".bin")
    path = dest / (digest + suffix)
    if not path.exists():
        path.write_bytes(result.content)
    receipt = dest / f"{digest}.{stable_id(result.fetched_at, source.id)}.json"
    receipt.write_text(json.dumps({"source_id": source.id, "url": result.final_url, "at": result.fetched_at,
        "backend": result.backend, "hash": digest, "recipe_version": source.recipe_version, "account_scope": source.account_scope,
        "trace": result.trace}, ensure_ascii=False, indent=2), encoding="utf-8")
    if result.screenshot:
        receipt.with_suffix(".png").write_bytes(result.screenshot)
    return str(path), digest


def _conflicts(rows: list[dict]):
    groups = defaultdict(list)
    for row in rows:
        if row["status"] != "verified":
            continue
        fields = row["raw_fields"]
        # Different option/quantity/tax conditions are not contradictions.
        key = json.dumps({k: v for k, v in fields.items() if k not in {"price", "amount", "name", "description"}}, sort_keys=True, default=str)
        groups[key].append(row)
    for group in groups.values():
        if len({r["amount"] for r in group}) > 1:
            for row in group:
                row.update(status="review", reason="같은 상품·조건에서 원문 가격이 상충함", amount=None,
                           normalized_amount=None, comparable=False, comparison_key=None)


def report_for_run(config: CollectionConfig, store: Store, run_id: str) -> dict:
    from .report import export_report
    from .validation import _expiry
    run = store.run(run_id)
    if run["config_hash"] != config_fingerprint(config):
        raise ValueError("원래 실행 설정과 일치하지 않습니다")
    sources = {s.id: s for s in config.sources}
    rows = store.observations(run_id)
    now = datetime.now(timezone.utc)
    for row in rows:
        try:
            captured = datetime.fromisoformat(row["collected_at"].replace("Z", "+00:00"))
            if captured.tzinfo is None or captured > now + timedelta(minutes=5):
                row.update(freshness="unknown", comparable=False, reason=row["reason"] + "; 수집시각 확인 필요")
            elif now - captured > timedelta(hours=sources[row["source_id"]].freshness_hours):
                row.update(freshness="stale", comparable=False, reason=row["reason"] + "; 재확인 기한 초과")
        except (ValueError, KeyError):
            row.update(freshness="unknown", comparable=False)
        expired, expiry_problem = _expiry(row.get("raw_fields", {}).get("valid_to"), now.isoformat())
        if expired or expiry_problem:
            row.update(comparable=False, reason=row["reason"] + "; 보고서 시점 유효기한 만료 또는 확인 필요")
    path = Path(config.output_dir) / f"{'demo-' if config.demo else ''}prices-{run_id}.xlsx"
    export_report(config, run, store.tasks(run_id), rows, path)
    with store.db:
        store.db.execute("UPDATE runs SET report_path=? WHERE id=?", (str(path), run_id))
    return store.run(run_id)


def execute(config: CollectionConfig, *, resume_id: str | None = None, new_run_id: str | None = None, collector=None, max_tasks: int | None = None) -> dict:
    """Bounded, resumable collection. max_tasks is useful for supervised batches."""
    from .collectors import collect
    from .extraction import extract
    from .validation import validate
    from .report import export_report

    collector = collector or collect
    directory = Path(config.output_dir)
    with workspace_lock(directory):
        store = Store(directory)
        try:
            fingerprint = config_fingerprint(config)
            run_id = resume_id or new_run_id or uuid.uuid4().hex
            if not re.fullmatch(r"[a-zA-Z0-9_-]{1,80}", run_id):
                raise ValueError("실행 ID 형식이 올바르지 않습니다")
            if resume_id:
                run = store.run(run_id)
                if run["config_hash"] != fingerprint:
                    raise ValueError("설정이 변경되어 재개할 수 없습니다. 새 실행을 시작하세요")
                if run["status"] in {"completed", "partial"} and run["report_path"] and Path(run["report_path"]).is_file():
                    return run
            else:
                with store.db:
                    store.db.execute("INSERT INTO runs(id,config_hash,status,started_at,demo) VALUES(?,?,?,?,?)",
                                     (run_id, fingerprint, "running", utc_now(), int(config.demo)))
                    for source in config.sources:
                        for product_id in source.product_ids:
                            store.db.execute("INSERT INTO tasks(id,run_id,source_id,product_id,status,updated_at) VALUES(?,?,?,?,?,?)",
                                             (stable_id(run_id, source.id, product_id), run_id, source.id, product_id, "queued", utc_now()))
            with store.db:
                store.db.execute("UPDATE runs SET status='running',finished_at=NULL WHERE id=?", (run_id,))
            deadline = time.monotonic() + config.max_run_seconds
            processed = 0
            paused = False
            product_map = {p.id: p for p in config.products}
            for source in config.sources:
                tasks = [t for t in store.tasks(run_id) if t["source_id"] == source.id and t["status"] not in TERMINAL]
                if not tasks:
                    continue
                if max_tasks is not None and processed >= max_tasks:
                    paused = True
                    break
                if max_tasks is not None:
                    tasks = tasks[:max_tasks - processed]
                if time.monotonic() >= deadline:
                    for task in tasks:
                        store.update_task(task["id"], "budget_exhausted", "실행 시간 한도 도달")
                    continue
                backends = ["file"] if source.kind == "file" else source.backends
                # A supplied recipe requires the browser state after the clicks.
                if source.recipe:
                    backends = [b for b in backends if b == "playwright"] or ["playwright"]
                best: tuple[FetchResult, list[Candidate]] | None = None
                last = FetchResult(source.id, "failed", backends[0], message="수집하지 못함")
                used = max(t["attempts"] for t in tasks)
                max_calls = source.max_attempts * len(backends)
                for call in range(used, max_calls):
                    if time.monotonic() >= deadline:
                        last = FetchResult(source.id, "budget_exhausted", "none", message="실행 시간 한도 도달")
                        break
                    backend = backends[call % len(backends)]
                    for task in tasks:
                        store.update_task(task["id"], "running", backend=backend, attempt=True)
                    bounded = replace(source, timeout_seconds=min(source.timeout_seconds, max(0.1, deadline - time.monotonic())))
                    try:
                        last = collector(bounded, config.base_dir, backend)
                    except Exception as exc:
                        # Exception payloads can contain credential-bearing URLs; report type only.
                        last = FetchResult(source.id, "failed", backend, message=f"수집기 오류: {type(exc).__name__}")
                    with store.db:
                        store.db.execute("INSERT INTO attempts(run_id,source_id,backend,status,at,data) VALUES(?,?,?,?,?,?)",
                                         (run_id, source.id, backend, last.status, last.fetched_at,
                                          json.dumps({"message": last.message, "trace": last.trace}, ensure_ascii=False)))
                    if last.status == "fetched":
                        try:
                            candidates = extract(last, source)
                        except Exception as exc:
                            candidates = []
                            last.message = f"추출 규칙 확인 필요: {type(exc).__name__}"
                        best = (last, candidates)
                        matching = [c for c in candidates if any(_matches(c, product_map[t["product_id"]]) for t in tasks)]
                        if matching and all(any(_matches(c, product_map[t["product_id"]]) for c in matching) for t in tasks):
                            # Complete raw rows still pass the same validation below.
                            if all(c.fields.get("price") is not None for c in matching) or source.kind == "file":
                                break
                        if source.kind == "file":
                            break
                    if last.status in {"policy_denied", "needs_auth"}:
                        # Auth can still be handled by an explicitly configured browser profile.
                        if last.status == "policy_denied" or backend == "playwright":
                            break
                    if call + 1 < max_calls:
                        time.sleep(min(source.min_interval_seconds, max(0, deadline - time.monotonic())))
                if best:
                    fetched, candidates = best
                    evidence_path, digest = _save_evidence(directory, fetched, source)
                    for task in tasks:
                        product = product_map[task["product_id"]]
                        matches = [c for c in candidates if _matches(c, product)]
                        rows = [validate(c, product, source, run_id=run_id, task_id=task["id"], evidence_path=evidence_path,
                                         evidence_sha256=digest, collected_at=fetched.fetched_at,
                                         source_url=fetched.final_url or source.location).to_dict() for c in matches]
                        _conflicts(rows)
                        if rows:
                            state = "verified" if all(r["status"] == "verified" for r in rows) else "price_unavailable" if all(r["status"] == "price_unavailable" for r in rows) else "review"
                            reason = "; ".join(sorted({r["reason"] for r in rows if r["reason"]}))
                        else:
                            known_candidates = [c for c in candidates if c.fields]
                            state = "product_not_found" if known_candidates else "review"
                            reason = "지정 상품 식별자가 원문에서 확인되지 않음" if known_candidates else (fetched.message or "추출 후보 없음: CSS·열 매핑·PDF 규칙 또는 OCR 확인 필요")
                        store.finish_task(task["id"], state, reason, fetched.backend, rows)
                else:
                    for task in tasks:
                        store.finish_task(task["id"], last.status if last.status in TERMINAL else "failed", last.message, last.backend, [])
                processed += len(tasks)
            tasks = store.tasks(run_id)
            paused = paused or any(t["status"] in {"queued", "running"} for t in tasks)
            if paused:
                state = "paused"
            else:
                state = "completed" if all(t["status"] in {"verified", "price_unavailable", "product_not_found"} for t in tasks) else "partial"
            with store.db:
                store.db.execute("UPDATE runs SET status=?, finished_at=? WHERE id=?", (state, utc_now(), run_id))
            return report_for_run(config, store, run_id)
        finally:
            store.close()
