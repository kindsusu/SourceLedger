"""MCP follow-up: coverage gaps, bounded retries and untrusted browser evidence."""
from __future__ import annotations

from datetime import datetime, timezone, timedelta
from decimal import Decimal, InvalidOperation
import hashlib
import json
from pathlib import Path
import re
from urllib.parse import urlsplit

from . import assistant_runtime as runtime
from .assistant_workspace import workspace_root, _managed_output, relative_file
from .models import Candidate, CollectionConfig, Product, Source, stable_id
from .research_plans import canonical_url, confirmed_snapshot
from .storage import Store, workspace_lock
from .scope_conditions import assess_conditions
from .site_collection import HOST_ADAPTERS
from .coverage import unknown_section
from .collection_issues import MAX_RETRY_CHAIN, RETRYABLE_CODES

RETRYABLE = {"no_data", "blocked", "failed", "timeout", "tool_unavailable", "budget_exhausted", "unprocessed", "interrupted"}


def _all_issues(store, run_id):
    rows = []
    while True:
        batch = store.collection_issues(run_id, offset=len(rows), limit=500)
        rows.extend(batch)
        if len(batch) < 500:
            return rows


def _incomplete_sections(page):
    sections = page.get('sections') or ([unknown_section()] if page.get('status') in {'visited', 'no_data'} else [])
    return [section for section in sections
            if isinstance(section, dict) and section.get("status") != "complete"]


def _retryable_page(page):
    return page.get("status") in RETRYABLE or (
        page.get("status") in {"visited", "no_data"} and any(s.get('status') == 'partial' for s in _incomplete_sections(page)))


def completed(root, job_id):
    job = runtime.get_job(root, job_id)
    result = job.get("result")
    if job.get("operation") != "research_plan" or job.get("status") != "succeeded" or not isinstance(result, dict):
        raise ValueError("A completed research-plan job is required")
    output = _managed_output(workspace_root(root), Path(result["output_dir"]), "research output")
    return job, result, output


def batch_coverage(output, entry):
    """Page coverage of one batch run: the persisted run coverage, else its checkpoint result."""
    store = Store(output)
    try:
        coverage = store.run_coverage(entry["run_id"]) if entry.get("run_id") else []
    finally:
        store.close()
    if coverage:
        return coverage
    from .catalog_checkpoint import read_checkpoint
    state = read_checkpoint(output, entry["checkpoint_id"]) if entry.get("checkpoint_id") else None
    result = (state or {}).get("result") or {}
    return result["coverage"] if isinstance(result.get("coverage"), list) else []


def _batch_ledger(root, job):
    from .research_batch import read_batch_journal, _output
    plan_id = job["arguments"]["plan_id"]
    journal = read_batch_journal(root, plan_id, job["id"])
    if not journal or not journal.get("batches"):
        raise ValueError("Batch job has no completed batches yet")
    return _output(workspace_root(root), plan_id), journal


def _blocking(issues, url):
    return [issue for issue in issues if issue['page_url'] == url and issue['status'] != 'resolved'
            and issue['reason_code'] not in RETRYABLE_CODES]


def retryable_urls(root, job_id):
    """Approved pages this plan or batch job may retry without review, in job order."""
    job = runtime.get_job(root, job_id)
    if job.get("operation") == "research_batch":
        output, journal = _batch_ledger(root, job)
        runs = [(entry["run_id"], batch_coverage(output, entry)) for entry in journal["batches"]]
    else:
        _, result, output = completed(root, job_id)
        runs = [(result["run_id"], result["result"]["coverage"])]
    urls = []
    store = Store(output)
    try:
        for run_id, coverage in runs:
            issues = _all_issues(store, run_id)
            urls.extend(page["url"] for page in coverage if _retryable_page(page) and not _blocking(issues, page["url"]))
    finally:
        store.close()
    return list(dict.fromkeys(urls))


def retryable_batch_urls(root, job_id):
    return retryable_urls(root, job_id)


def research_gaps(root, job_id, *, offset=0, limit=50):
    if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
        raise ValueError("offset must be non-negative")
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 200:
        raise ValueError("limit must be between 1 and 200")
    job = runtime.get_job(root, job_id)
    if job.get("operation") == "research_batch":
        output, journal = _batch_ledger(root, job)
        items = []
        for entry in journal["batches"]:
            coverage = batch_coverage(output, entry)
            store = Store(output)
            try:
                rows = store.observations(entry["run_id"])
                issue_rows = _all_issues(store, entry["run_id"])
            finally:
                store.close()
            for item in _gap_items(coverage, rows, issue_rows):
                items.append({**item, "batch_index": entry.get("index"), "run_id": entry["run_id"]})
        from collections import Counter
        return {"job_id": job_id, "total": len(items), "offset": offset, "limit": limit,
                "items": items[offset:offset + limit],
                "status_counts": dict(Counter(item["status"] for item in items)),
                "retryable_count": sum(item["retryable"] for item in items),
                "budget": {"total_collection_seconds": round(sum(entry.get("budget_seconds_used", 0)
                                                                 for entry in journal["batches"]), 3)},
                "note": "Browser submissions remain review-only. A page visit is not proof of every option or contract condition."}
    job, result, output = completed(root, job_id)
    store = Store(output)
    try:
        rows = store.observations(result["run_id"])
        issue_rows = _all_issues(store, result["run_id"])
    finally:
        store.close()
    items = _gap_items(result["result"]["coverage"], rows, issue_rows)
    return {"job_id": job_id, "total": len(items), "offset": offset, "limit": limit,
            "items": items[offset:offset + limit], "budget": result["result"].get("budget"),
            "note": "Browser submissions remain review-only. A page visit is not proof of every option or contract condition."}


def _gap_items(coverage, rows, issue_rows):
    items = []
    from .result_view import build_result_view
    by_url = {}
    for row in rows:
        by_url.setdefault(row["source_url"], []).append(row)
    for page in coverage:
        page_issues = [issue for issue in issue_rows if issue['page_url'] == page['url'] and issue['status'] != 'resolved']
        page_rows = by_url.get(page["url"], [])
        displayed = build_result_view(page_rows)
        unresolved = [row for row in displayed["rows"] if row["status"] != "verified" or not row.get("comparable")]
        incomplete = _incomplete_sections(page)
        if page["status"] == "excluded" or (page["status"] == "visited" and not unresolved and not incomplete):
            continue
        status = "partial" if any(s.get('status') == 'partial' for s in incomplete) and page["status"] in {"visited", "no_data"} else (
            page["status"] if page["status"] != "visited" else "needs_review")
        items.append({"url": page["url"], "status": status, "reason": page.get("reason", ""),
                      "sections": page.get("sections") or incomplete,
                      "retryable": (all(issue['reason_code'] in RETRYABLE_CODES for issue in page_issues)
                                    if page_issues else _retryable_page(page)),
                      "collection_issues": page_issues,
                      "next_action": "inspect_scope_and_recipe" if incomplete and all(s.get('status') == 'unknown' for s in incomplete) else "review_conditions" if status == "needs_review" else
                                     "resolve_access" if status in {"policy_denied", "needs_auth"} else "retry_or_browser_evidence",
                      "review_count": len(unresolved),
                      "unlinked_evidence_count": displayed["unlinked_evidence_count"],
                      "review_reasons": list(dict.fromkeys(row["reason"] for row in unresolved))[:10],
                      "browser_submissions": sum(row.get("extraction_method") == "host_browser_submission" for row in page_rows)})
    return items


def compact_gap_item(item):
    """MCP projection: issue identifiers and actions, not issue evidence payloads."""
    issues = item.get("collection_issues") or []
    value = {key: item[key] for key in ("url", "status", "reason", "retryable", "next_action", "review_count",
                                         "unlinked_evidence_count", "browser_submissions", "batch_index", "run_id")
             if key in item}
    value["review_reasons"] = [str(reason)[:200] for reason in item.get("review_reasons", [])[:3]]
    value["sections"] = [{key: section.get(key) for key in ("section", "status", "reason") if key in section}
                         for section in (item.get("sections") or [])[:5] if isinstance(section, dict)]
    value["collection_issues"] = [{key: issue.get(key) for key in ("id", "category", "reason_code", "status", "next_action")
                                   if key in issue} for issue in issues[:5]]
    value["collection_issue_count"] = len(issues)
    return value


def validate_retry(root, args):
    job, result, output = completed(root, args["retry_of"])
    chain = 0
    ancestor = job
    seen = set()
    while ancestor.get('arguments', {}).get('retry_of'):
        parent_id = ancestor['arguments']['retry_of']
        if parent_id in seen:
            raise ValueError('Retry history contains a cycle')
        seen.add(parent_id)
        chain += 1
        if chain >= MAX_RETRY_CHAIN:
            raise ValueError(f'Retry chain limit ({MAX_RETRY_CHAIN}) reached; inspect evidence or change the collection approach')
        ancestor = runtime.get_job(root, parent_id)
    for key in ("plan_id", "expected_revision", "plan_fingerprint"):
        if job["arguments"].get(key) != args.get(key):
            raise ValueError("Retry must use the same confirmed plan revision")
    store = Store(output)
    try:
        issues = _all_issues(store, result['run_id'])
        persisted = store.run_coverage(result['run_id'])
    finally:
        store.close()
    reported_allowed = {page['url'] for page in result['result']['coverage'] if _retryable_page(page)}
    persisted_allowed = {page['url'] for page in persisted if _retryable_page(page)} if persisted else reported_allowed
    if not set(args['retry_urls']) <= reported_allowed & persisted_allowed:
        raise ValueError('Retry only unresolved pages reported by this job; review-only prices need evidence, not blind retries')
    for url in args['retry_urls']:
        active = [issue for issue in issues if issue['page_url'] == url and issue['status'] != 'resolved']
        blocked = [issue for issue in active if issue['reason_code'] not in RETRYABLE_CODES]
        if blocked:
            codes = ', '.join(sorted({issue['reason_code'] for issue in blocked}))
            raise ValueError(f'Retry requires review before another attempt: {codes}')


def list_collection_issues(root, job_id, *, status=None, offset=0, limit=50):
    """Read durable issue triage for a completed research-plan job."""
    if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
        raise ValueError('offset must be non-negative')
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 200:
        raise ValueError('limit must be between 1 and 200')
    job = runtime.get_job(root, job_id)
    if job.get('operation') == 'research_batch':
        from .research_batch import read_batch_journal
        plan_id = job['arguments']['plan_id']
        journal = read_batch_journal(root, plan_id, job_id)
        run_ids = [batch['run_id'] for batch in (journal or {}).get('batches', []) if batch.get('run_id')]
        output = _managed_output(workspace_root(root), workspace_root(root) / 'catalog-prices' / plan_id,
                                 'batch run output')
        store = Store(output)
        try:
            rows = store.collection_issues_for_runs(run_ids, status=status, offset=offset, limit=limit + 1)
            total = store.collection_issue_count(run_ids, status=status)
        finally:
            store.close()
        return {'job_id': job_id, 'run_ids': run_ids, 'items': rows[:limit],
                'collection_issues': rows[:limit], 'total': total,
                'offset': offset, 'limit': limit, 'has_more': len(rows) > limit}
    _, result, output = completed(root, job_id)
    store = Store(output)
    try:
        rows = store.collection_issues(result['run_id'], status=status, offset=offset, limit=limit + 1)
        total = store.collection_issue_count([result['run_id']], status=status)
    finally:
        store.close()
    return {'job_id': job_id, 'run_id': result['run_id'], 'items': rows[:limit],
            'collection_issues': rows[:limit], 'total': total,
            'offset': offset, 'limit': limit, 'has_more': len(rows) > limit}


def triage_collection_issue(root, job_id, issue_id, *, status, evidence=None):
    """Manual triage may mark review; resolution always needs cited evidence."""
    job = runtime.get_job(root, job_id)
    if job.get('operation') == 'research_batch':
        from .research_batch import read_batch_journal
        plan_id = job['arguments']['plan_id']
        journal = read_batch_journal(root, plan_id, job_id)
        allowed_runs = {batch['run_id'] for batch in (journal or {}).get('batches', []) if batch.get('run_id')}
        output = _managed_output(workspace_root(root), workspace_root(root) / 'catalog-prices' / plan_id,
                                 'batch run output')
    else:
        _, result, output = completed(root, job_id)
        allowed_runs = {result['run_id']}
    with workspace_lock(output):
        store = Store(output)
        try:
            row = store._collection_issue(issue_id) if store.db.execute(
                'SELECT 1 FROM collection_issues WHERE id=?', (issue_id,)).fetchone() else None
            if row is None or row['run_id'] not in allowed_runs:
                raise ValueError('Collection issue does not belong to this job')
            run = store.run(row['run_id'])
            report_path = Path(run['report_path']).absolute() if run.get('report_path') else None
            if report_path is not None:
                if report_path.parent != output or report_path.is_symlink() or not report_path.is_file():
                    raise ValueError('Collection report is missing or outside the managed output')
            updated = store.update_collection_issue(issue_id, status=status, evidence=evidence)
            if report_path is not None:
                from .report import append_collection_issues
                try:
                    append_collection_issues(report_path, _all_issues(store, row['run_id']))
                except (PermissionError, OSError) as exc:
                    updated['report_refresh'] = 'pending'
                    updated['report_refresh_reason'] = type(exc).__name__
                else:
                    updated['report_refresh'] = 'updated'
            else:
                updated['report_refresh'] = 'unavailable'
            return updated
        finally:
            store.close()


def _config(output, run_id, root):
    raw = json.loads((output / f"collection-{run_id}.json").read_text(encoding="utf-8"))
    config = CollectionConfig(raw["name"], [Product(**p) for p in raw["products"]],
                            [Source(**s) for s in raw["sources"]], str(output), str(workspace_root(root)),
                            demo=raw.get("demo", False), max_run_seconds=raw.get("max_run_seconds", 60))
    # Old catalogs used the workspace root; portable exported catalogs use
    # their own directory. Accept only an exact match to the recorded run.
    from .config import config_fingerprint
    store = Store(output)
    try:
        expected = store.run(run_id)['config_hash']
    finally:
        store.close()
    if config_fingerprint(config) != expected:
        config.base_dir = str(output)
    if config_fingerprint(config) != expected:
        raise ValueError('Configuration does not match the original run')
    return config


def _literal_interpretations(fields):
    """Interpret bounded, explicit display syntax without replacing source text."""
    normalized = {}
    price_kind = None
    currency_marker = None
    price = fields.get("price", "").strip()
    match = re.fullmatch(r"(?:월\s*)?(?:₩\s*)?([\d,]+)\s*(원|KRW)?\s*(~|～|부터)?", price, re.IGNORECASE)
    if match and re.fullmatch(r"\d{1,3}(?:,\d{3})+|\d+", match[1]):
        normalized["price"] = match[1].replace(",", "")
        price_kind = "from" if match[3] else "listed"
        if price.startswith("월"):
            normalized["price_basis"] = "monthly"
        if (match[2] and match[2].upper() == "KRW") or "₩" in price or match[2] == "원":
            currency_marker = "KRW"
    for alias in ("term_months", "contract_term_months", "contract_term", "term", "terms"):
        literal = fields.get(alias, "").strip()
        match = re.fullmatch(r"([1-9]\d*)\s*(?:개월|months?)?", literal, re.IGNORECASE)
        if match:
            normalized["term_months"] = match[1]
            break
    for alias in ("deposit_percent", "deposit"):
        literal = fields.get(alias, "").strip()
        match = re.fullmatch(r"(?:보증금\s*)?(\d+(?:\.\d+)?)\s*%", literal)
        if match:
            normalized["deposit_percent"] = match[1]
            break
    for alias in ("deposit_amount", "deposit"):
        literal = fields.get(alias, "").strip()
        match = re.fullmatch(r"(?:보증금\s*)?([\d,]+(?:\.\d+)?)\s*(만)?\s*(?:원|KRW)?", literal, re.IGNORECASE)
        if match and re.fullmatch(r"\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?", match[1]):
            try:
                number = Decimal(match[1].replace(",", "")) * (10000 if match[2] else 1)
            except InvalidOperation:
                continue
            normalized["deposit_amount"] = format(number, "f")
            break
    for alias in ("terms", "contract_term"):
        literal = fields.get(alias, "").strip()
        combined = re.fullmatch(r"([1-9]\d*)\s*개월\s*/\s*보증금\s*(\d+(?:\.\d+)?)\s*%\s*기준", literal)
        if combined and normalized.get("term_months") in (None, combined[1]) and normalized.get("deposit_percent") in (None, combined[2]):
            normalized["term_months"] = combined[1]
            normalized["deposit_percent"] = combined[2]
    return normalized, price_kind, currency_marker


def _browser_price_profile(config, source, normalized):
    if source.adapter:
        return "rental"
    profiles = {product.price_profile for product in config.products}
    if profiles == {"rental"}:
        return "rental"
    has_term = "term_months" in normalized
    has_contract_condition = any(key in normalized for key in ("deposit_percent", "deposit_amount", "price_basis"))
    return "rental" if has_term and has_contract_condition else "unit"


def submit_browser_evidence(root, job_id, *, url, captured_at, page_text, fields, locator,
                            screenshot_path=None):
    """Validate literal field support; never promote host assertions to verified prices."""
    job, result, output = completed(root, job_id)
    url = canonical_url(url)
    pages = result["result"]["coverage"]
    if url not in {page["url"] for page in pages if page["status"] != "excluded"}:
        raise ValueError("Evidence URL must be an approved page recorded in this job's coverage")
    if not isinstance(page_text, str) or not 1 <= len(page_text) <= 200_000:
        raise ValueError("page_text must contain 1 to 200000 characters of browser-captured text")
    if not isinstance(locator, str) or not 1 <= len(locator.strip()) <= 500:
        raise ValueError("A product/option locator is required")
    if not isinstance(fields, dict) or not 1 <= len(fields) <= 40:
        raise ValueError("Provide 1 to 40 literal fields")
    for key, value in fields.items():
        if not isinstance(key, str) or not 1 <= len(key) <= 80 or not isinstance(value, str) or not 1 <= len(value) <= 2000:
            raise ValueError("Field names and values must be bounded strings; omit unknown values")
        if value not in page_text:
            raise ValueError(f"Field {key} has no exact support in page_text; do not infer or normalize values")
    if not fields.get("name"):
        raise ValueError("A source-supported product name is required")
    try:
        captured = datetime.fromisoformat(captured_at.replace("Z", "+00:00"))
    except (AttributeError, TypeError, ValueError) as exc:
        raise ValueError("captured_at must be an ISO timestamp with timezone") from exc
    if captured.tzinfo is None or captured > datetime.now(timezone.utc) + timedelta(minutes=5):
        raise ValueError("Capture time must have a timezone and must not be in the future")
    screenshot = None
    if screenshot_path is not None:
        path = relative_file(root, screenshot_path, "screenshot_path")
        if path.stat().st_size > 10 * 1024 * 1024:
            raise ValueError("Screenshot exceeds 10 MiB")
        screenshot = path.read_bytes()
        if not screenshot.startswith(b"\x89PNG\r\n\x1a\n"):
            raise ValueError("Screenshot must be a PNG capture")
    snapshot = confirmed_snapshot(root, job["arguments"]["plan_id"], job["arguments"]["expected_revision"])
    payload = {"url": url, "captured_at": captured_at, "page_text": page_text, "fields": fields,
               "locator": locator, "screenshot_sha256": hashlib.sha256(screenshot).hexdigest() if screenshot else None}
    submission_id = "browser_" + stable_id(result["run_id"], payload)
    with workspace_lock(output):
        store = Store(output)
        try:
            existing = store.db.execute("SELECT data FROM browser_submissions WHERE id=?", (submission_id,)).fetchone()
            if existing:
                row = json.loads(existing[0])
            else:
                count = store.db.execute("SELECT count(*) FROM browser_submissions WHERE run_id=?", (result["run_id"],)).fetchone()[0]
                if count >= 200:
                    raise ValueError("Browser submission limit reached for this run")
                artifacts = _managed_output(workspace_root(root), output / "browser-evidence", "browser evidence")
                artifacts.mkdir(parents=True, exist_ok=True)
                evidence_path = artifacts / f"{submission_id}.json"
                content = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
                evidence_path.write_bytes(content)
                proof = {key: {"raw": value, "location": locator, "display_state": "unconfirmed",
                               "proof_kind": "assistant_supplied_text"} for key, value in fields.items()}
                normalized, price_kind, currency_marker = _literal_interpretations(fields)
                candidate = Candidate(dict(fields), proof, locator, "host_browser_submission",
                                      review_flags=["Browser text and field association supplied by host AI; independent verification pending"],
                                      evidence_mode="document_text")
                candidate.derived_values["scope_assessment"] = assess_conditions(candidate, snapshot.get("conditions", []))
                candidate.derived_values["literal_interpretations"] = normalized
                if price_kind:
                    candidate.derived_values["price_kind"] = price_kind
                if currency_marker:
                    candidate.derived_values["currency_from_price_marker"] = currency_marker
                host = urlsplit(url).hostname
                source = Source("catalog_source_" + stable_id(url), host, "web", url, [], adapter=HOST_ADAPTERS.get(host))
                config = _config(output, result["run_id"], root)
                product = Product("browser_product_" + stable_id(url, fields["name"], locator), fields["name"],
                                  identifiers={"name": fields["name"]}, price_profile=_browser_price_profile(config, source, normalized))
                from .validation import validate
                observation = validate(candidate, product, source, run_id=result["run_id"], task_id=submission_id,
                                       evidence_path=str(evidence_path), evidence_sha256=hashlib.sha256(content).hexdigest(),
                                       collected_at=captured_at, source_url=url)
                row = observation.to_dict()
                row.update(id=submission_id, status="review", verification_level="review", comparable=False,
                           comparison_key=None)
                if "price" in normalized:
                    row["amount"] = normalized["price"]
                    row["reason"] = row["reason"].replace("price is not one valid non-negative decimal", "displayed amount normalized for review")
                    if price_kind == "from":
                        row["reason"] += "; displayed starting price, not a final quote"
                if screenshot:
                    screen_path = artifacts / f"{submission_id}.png"
                    screen_path.write_bytes(screenshot)
                    row["evidence_artifacts"]["screenshot"] = {"path": str(screen_path), "sha256": payload["screenshot_sha256"]}
                with store.db:
                    store.db.execute("INSERT INTO browser_submissions(id,run_id,data) VALUES(?,?,?)",
                                     (submission_id, result["run_id"], json.dumps(row, ensure_ascii=False)))
            # Regenerate even on an idempotent replay: a prior export may have
            # failed after the SQLite transaction committed.
            from .pipeline import report_for_run
            report_for_run(_config(output, result["run_id"], root), store, result["run_id"])
        finally:
            store.close()
    return {"submission_id": submission_id, "status": "review", "literal_support": "matched",
            "independent_verification": "pending", "observation": row,
            "report_path": result["report_path"]}
