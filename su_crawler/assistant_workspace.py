"""Workspace-scoped business operations for the local assistant connector."""
from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator
import hashlib
import json
import math
import os
import re
import time


OPERATIONS = frozenset({"discover", "propose", "agent", "collect_sites", "verify", "run", "export"})
MAX_SECONDS = 3600.0
MAX_PAGE_COUNT = 50
MAX_SOURCE_COUNT = 50
MAX_CONFIG_TASKS = 1000
MAX_REPORT_BYTES = 50 * 1024 * 1024


def _reject_symlink_path(path: Path, label: str, *, boundary: Path | None = None) -> None:
    """Reject an existing symbolic-link component before resolving a path."""
    candidate = path.expanduser().absolute()
    if boundary is None:
        if candidate.is_symlink():
            raise ValueError(f"{label} must not be a symbolic link")
        return
    boundary = boundary.resolve()
    try:
        relative = candidate.relative_to(boundary)
    except ValueError as exc:
        raise ValueError(f"{label} must stay inside the assistant workspace") from exc
    current = boundary
    for part in relative.parts:
        current = current / part
        if current.is_symlink():
            raise ValueError(f"{label} must not contain symbolic links")


def workspace_root(value: str | Path) -> Path:
    supplied = Path(value)
    _reject_symlink_path(supplied, "workspace root")
    root = supplied.expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    if not root.is_dir():
        raise ValueError("workspace root must be a directory")
    return root


def research_path(root: str | Path) -> Path:
    path = workspace_root(root) / "research.json"
    if path.is_symlink():
        raise ValueError("research workspace must not be a symbolic link")
    if path.with_name(path.name + ".lock").is_symlink():
        raise ValueError("research workspace lock must not be a symbolic link")
    return path


def _contained(root: Path, path: Path, label: str) -> Path:
    resolved = path.resolve()
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise ValueError(f"{label} must stay inside the assistant workspace") from exc
    return resolved


def _managed_output(root: Path, path: Path, label: str) -> Path:
    """Reject predictable link escapes below a connector-managed output tree."""
    unresolved = path.expanduser().absolute()
    _reject_symlink_path(unresolved, label, boundary=root)
    output = _contained(root, unresolved, label)
    if output.exists() and not output.is_dir():
        raise ValueError(f"{label} must be a directory")
    if output.is_dir():
        checked = 0
        pending = [output]
        while pending:
            directory = pending.pop()
            with os.scandir(directory) as entries:
                for entry in entries:
                    checked += 1
                    if checked > 100_000:
                        raise ValueError(f"{label} contains too many existing entries to validate")
                    child = Path(entry.path)
                    if entry.is_symlink():
                        raise ValueError(f"{label} must not contain symbolic links")
                    _contained(root, child, label)
                    if entry.is_dir(follow_symlinks=False):
                        pending.append(child)
    return output


def relative_file(root: str | Path, value: str, label: str) -> Path:
    base = workspace_root(root)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} is required")
    supplied = Path(value)
    if supplied.is_absolute():
        raise ValueError(f"{label} must be a workspace-relative path")
    unresolved = base / supplied
    _reject_symlink_path(unresolved, label, boundary=base)
    path = _contained(base, unresolved, label)
    if not path.is_file():
        raise ValueError(f"{label} does not exist: {value}")
    return path


def workspace_fingerprint(root: str | Path) -> str | None:
    path = research_path(root)
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None


@contextmanager
def workspace_guard(root: str | Path) -> Iterator[Path]:
    """Serialize assistant mutations and job input snapshots across processes."""
    base = workspace_root(root)
    path = base / ".assistant-workspace.lock"
    if path.is_symlink():
        raise ValueError("assistant workspace lock must not be a symbolic link")
    handle = path.open("a+b")
    try:
        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write(b"0")
            handle.flush()
        handle.seek(0)
        deadline = time.monotonic() + 2
        while True:
            try:
                handle.seek(0)
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError as exc:
                if time.monotonic() >= deadline:
                    raise RuntimeError("Assistant workspace is busy; retry after the current operation finishes") from exc
                time.sleep(0.05)
        yield base
    finally:
        handle.close()


def _positive_number(value: Any, label: str, maximum: float = MAX_SECONDS) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError(f"{label} must be a finite number")
    if not 0 < value <= maximum:
        raise ValueError(f"{label} must be between 0 and {maximum:g}")
    return float(value)


def _bounded_int(value: Any, label: str, upper: int, *, lower: int = 1) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not lower <= value <= upper:
        raise ValueError(f"{label} must be an integer from {lower} to {upper}")
    return value


def _job_id(value: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", value):
        raise ValueError("Invalid job ID")
    return value


def validate_config(root: str | Path, value: str):
    """Load a root-relative collection config and reject paths escaping root."""
    from .config import load_config
    from .models import resolve_path

    base = workspace_root(root)
    path = relative_file(base, value, "config_path")
    config = load_config(path)
    if config.max_run_seconds > MAX_SECONDS:
        raise ValueError(f"config max_run_seconds must not exceed {MAX_SECONDS:g}")
    if len(config.sources) > MAX_SOURCE_COUNT or len(config.products) > MAX_SOURCE_COUNT:
        raise ValueError(f"config may contain at most {MAX_SOURCE_COUNT} sources and products")
    if sum(len(source.product_ids) for source in config.sources) > MAX_CONFIG_TASKS:
        raise ValueError(f"config may create at most {MAX_CONFIG_TASKS} tasks")
    _managed_output(base, Path(config.output_dir), "config output_dir")
    _contained(base, Path(config.base_dir), "config base_dir")
    for source in config.sources:
        if source.timeout_seconds > 300:
            raise ValueError(f"source {source.id} timeout_seconds must not exceed 300")
        if source.file_root:
            file_root = _contained(base, Path(source.file_root), f"source {source.id} file_root")
        else:
            file_root = Path(config.base_dir)
        if source.kind == "file":
            _contained(base, Path(resolve_path(str(file_root), source.location)), f"source {source.id} location")
        if source.profile_dir:
            _contained(base, Path(source.profile_dir), f"source {source.id} profile_dir")
    return path, config


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def prepare_job_args(root: str | Path, operation: str, args: dict[str, Any]) -> dict[str, Any]:
    """Validate submission input and pin every mutable local input."""
    clean = validate_job_args(root, operation, args)
    fingerprint = workspace_fingerprint(root)
    if fingerprint is None:
        raise ValueError("Research workspace needs setup")
    clean["workspace_fingerprint"] = fingerprint
    if operation in {"verify", "run", "export"}:
        config_path, _ = validate_config(root, clean["config_path"])
        clean["config_fingerprint"] = _file_sha256(config_path)
    if operation == "verify" and clean.get("samples_path"):
        samples = relative_file(root, clean["samples_path"], "samples_path")
        clean["samples_fingerprint"] = _file_sha256(samples)
    return clean


def validate_job_args(root: str | Path, operation: str, args: dict[str, Any]) -> dict[str, Any]:
    if operation not in OPERATIONS:
        raise ValueError(f"Unsupported assistant operation: {operation}")
    if not isinstance(args, dict):
        raise ValueError("job arguments must be an object")
    clean = dict(args)
    if clean.get("max_model_calls", 0) != 0 or clean.get("model_config") or clean.get("search_config"):
        raise ValueError("Providers and model calls are not enabled for the local assistant connector")
    for forbidden in ("command", "executable", "cwd", "output_dir", "run_dir", "receipt_path"):
        if forbidden in clean:
            raise ValueError(f"{forbidden} is managed by the assistant workspace")
    if operation == "discover":
        if not isinstance(clean.get("source_id"), str) or not clean["source_id"].strip():
            raise ValueError("source_id is required")
        clean["limit"] = _bounded_int(clean.get("limit", 100), "limit", 1000)
    elif operation == "propose":
        if not isinstance(clean.get("source_id"), str) or not clean["source_id"].strip():
            raise ValueError("source_id is required")
        clean["timeout_seconds"] = _positive_number(clean.get("timeout_seconds", 30), "timeout_seconds", 300)
        clean["max_model_calls"] = 0
    elif operation == "agent":
        clean["max_sources"] = _bounded_int(clean.get("max_sources", 3), "max_sources", MAX_SOURCE_COUNT)
        clean["max_seconds"] = _positive_number(clean.get("max_seconds", 120), "max_seconds")
        if clean.get("max_steps") is not None:
            clean["max_steps"] = _bounded_int(clean["max_steps"], "max_steps", MAX_SOURCE_COUNT)
        source_ids = clean.get("source_ids")
        if source_ids is not None and (not isinstance(source_ids, list) or not source_ids or
                                       len(source_ids) > MAX_SOURCE_COUNT or
                                       any(not isinstance(item, str) or not item.strip() for item in source_ids)):
            raise ValueError(f"source_ids must contain 1 to {MAX_SOURCE_COUNT} source IDs")
        clean["max_model_calls"] = 0
    elif operation == "collect_sites":
        clean["max_pages"] = _bounded_int(clean.get("max_pages", 5), "max_pages", MAX_PAGE_COUNT)
        clean["max_seconds"] = _positive_number(clean.get("max_seconds", 120), "max_seconds")
        urls = clean.get("urls")
        if urls is not None and (not isinstance(urls, list) or not urls or len(urls) > clean["max_pages"] or
                                 any(not isinstance(item, str) or not item.strip() for item in urls)):
            raise ValueError("urls must contain between 1 and max_pages explicit URLs")
        if not isinstance(clean.get("incremental", True), bool):
            raise ValueError("incremental must be true or false")
    else:
        config_path, _ = validate_config(root, clean.get("config_path", ""))
        clean["config_path"] = str(config_path.relative_to(workspace_root(root)))
        if operation == "verify" and clean.get("samples_path") is not None:
            samples = relative_file(root, clean["samples_path"], "samples_path")
            clean["samples_path"] = str(samples.relative_to(workspace_root(root)))
        if operation == "run" and clean.get("max_tasks") is not None:
            clean["max_tasks"] = _bounded_int(clean["max_tasks"], "max_tasks", 1000)
        if operation == "export":
            _job_id(clean.get("run_id", ""))
    allowed = {
        "discover": {"source_id", "limit", "workspace_fingerprint", "_resume", "_attempt"},
        "propose": {"source_id", "timeout_seconds", "max_model_calls", "workspace_fingerprint", "_resume", "_attempt"},
        "agent": {"source_ids", "max_sources", "max_seconds", "max_steps", "max_model_calls", "workspace_fingerprint", "_resume", "_attempt"},
        "collect_sites": {"urls", "max_pages", "max_seconds", "incremental", "workspace_fingerprint", "_resume", "_attempt"},
        "verify": {"config_path", "samples_path", "workspace_fingerprint", "config_fingerprint", "samples_fingerprint", "_resume", "_attempt"},
        "run": {"config_path", "max_tasks", "workspace_fingerprint", "config_fingerprint", "_resume", "_attempt"},
        "export": {"config_path", "run_id", "workspace_fingerprint", "config_fingerprint", "_resume", "_attempt"},
    }[operation]
    unknown = set(clean) - allowed
    if unknown:
        raise ValueError(f"Unknown {operation} arguments: {sorted(unknown)}")
    if "_resume" in clean and not isinstance(clean["_resume"], bool):
        raise ValueError("internal resume marker must be boolean")
    if "_attempt" in clean:
        clean["_attempt"] = _bounded_int(clean["_attempt"], "internal attempt", 1000)
    return clean


def _require_current_workspace(root: Path, expected: str | None) -> str:
    current = workspace_fingerprint(root)
    if current is None:
        raise ValueError("Research workspace needs setup")
    if expected is not None and current != expected:
        raise ValueError("Research workspace changed after this job was submitted; submit a new job")
    return current


def execute_job(root: str | Path, operation: str, args: dict[str, Any], *, job_id: str) -> dict[str, Any]:
    """Execute one runtime-owned job without accepting arbitrary paths or commands."""
    job = _job_id(job_id)
    with workspace_guard(root) as base:
        clean = validate_job_args(base, operation, args)
        fingerprint = _require_current_workspace(base, clean.pop("workspace_fingerprint", None))
        clean.pop("_resume", False)  # A retry request cannot prove that resumable state exists.
        requested_attempt = clean.pop("_attempt", None)
        workspace = research_path(base)
        job_dir = _contained(base, base / "artifacts" / "jobs" / job, "job directory")
        _reject_symlink_path(job_dir, "job directory", boundary=base)
        job_dir.mkdir(parents=True, exist_ok=True)
        prior_attempts = [path for path in job_dir.glob("attempt-*") if path.is_dir()]
        attempt = requested_attempt or (len(prior_attempts) + 1)
        attempt_dir = _contained(base, job_dir / f"attempt-{attempt}", "job attempt directory")
        attempt_dir.mkdir(parents=True, exist_ok=True)
        agent_checkpoint = job_dir / "agent" / "agent-run.json"
        agent_resume = operation == "agent" and agent_checkpoint.is_file() and not agent_checkpoint.is_symlink()
        if agent_resume:
            # A paused batch resumes through its remaining global budget; the
            # original per-invocation step boundary must not pause forever.
            clean.pop("max_steps", None)
        expected_config = clean.pop("config_fingerprint", None)
        expected_samples = clean.pop("samples_fingerprint", None)
        if operation in {"verify", "run", "export"}:
            config_input, _ = validate_config(base, clean["config_path"])
            if not expected_config or _file_sha256(config_input) != expected_config:
                raise ValueError("Configuration changed after this job was submitted; submit a new job")
        if operation == "verify" and clean.get("samples_path"):
            samples_input = relative_file(base, clean["samples_path"], "samples_path")
            if not expected_samples or _file_sha256(samples_input) != expected_samples:
                raise ValueError("Samples changed after this job was submitted; submit a new job")
        if operation == "discover":
            from .research import discover_candidates
            result = discover_candidates(workspace, source_id=clean["source_id"], limit=clean["limit"])
        elif operation == "propose":
            from .proposals import propose_source
            result = propose_source(workspace, source_id=clean["source_id"], output_dir=attempt_dir / "proposal",
                                    max_model_calls=0, timeout_seconds=clean["timeout_seconds"])
        elif operation == "agent":
            from .agent import run_agent
            _managed_output(base, base / "outputs", "agent collection output")
            _managed_output(base, job_dir / "agent", "agent checkpoint directory")
            result = run_agent(workspace, run_dir=job_dir / "agent", source_ids=clean.get("source_ids"),
                               max_sources=clean["max_sources"], max_model_calls=0,
                               max_seconds=clean["max_seconds"], max_steps=clean.get("max_steps"), activate=False,
                               resume=agent_resume)
        elif operation == "collect_sites":
            from .site_collection import collect_sites
            site_output = _managed_output(base, base / "site-prices", "site collection output")
            result = collect_sites(workspace, urls=clean.get("urls"), output_dir=site_output,
                                   max_pages=clean["max_pages"], max_seconds=clean["max_seconds"],
                                   incremental=clean.get("incremental", True))
        else:
            config_path, config = validate_config(base, clean["config_path"])
            if operation == "verify":
                from .activation import verify_config
                samples = relative_file(base, clean["samples_path"], "samples_path") if clean.get("samples_path") else None
                result = verify_config(config_path, receipt_path=attempt_dir / "verification.json", samples_path=samples)
            elif operation == "run":
                from .pipeline import execute
                run_resume = False
                if (Path(config.output_dir) / "prices.sqlite3").is_file():
                    from .storage import Store
                    store = Store(Path(config.output_dir))
                    try:
                        store.run(job)
                        run_resume = True
                    except ValueError:
                        pass
                    finally:
                        store.close()
                result = execute(config, resume_id=job if run_resume else None,
                                 new_run_id=None if run_resume else job, max_tasks=clean.get("max_tasks"))
            else:
                from .pipeline import report_for_run
                from .storage import Store, workspace_lock
                with workspace_lock(Path(config.output_dir)):
                    store = Store(Path(config.output_dir))
                    try:
                        result = report_for_run(config, store, clean["run_id"])
                    finally:
                        store.close()
        if not isinstance(result, dict):
            raise RuntimeError("Assistant operation returned an invalid result")
        metadata = _result_metadata(base, operation, clean, result)
        evidence_status = result.get("status")
        if operation == "verify":
            evidence_status = "eligible" if result.get("eligible") else "ineligible"
        return {"operation": operation, "execution_status": "succeeded",
                "evidence_status": evidence_status or "completed",
                "workspace_fingerprint": fingerprint, "result": result, **metadata}


def _result_metadata(root: Path, operation: str, args: dict[str, Any], result: dict[str, Any]) -> dict[str, Any]:
    metadata: dict[str, Any] = {}
    config_value: str | None = args.get("config_path")
    run_id = result.get("id") or result.get("run_id")
    report_path = result.get("report_path")
    if operation == "agent" and isinstance(result.get("collection"), dict):
        collection = result["collection"]
        config_value = collection.get("config_path")
        run_id = collection.get("run_id")
        report_path = collection.get("report_path")
    elif operation == "collect_sites":
        config_value = result.get("config_path")
    if config_value:
        config_path = Path(config_value)
        if not config_path.is_absolute():
            config_path = root / config_path
        config_path = _contained(root, config_path, "result config path")
        metadata["config_path"] = str(config_path)
        try:
            _, config = validate_config(root, str(config_path.relative_to(root)))
            metadata["output_dir"] = str(Path(config.output_dir))
        except (OSError, ValueError, KeyError):
            if operation in {"run", "verify", "export", "collect_sites"}:
                raise
    if run_id:
        metadata["run_id"] = str(run_id)
    if report_path:
        metadata["report_path"] = str(_contained(root, Path(report_path), "result report path"))
    return metadata


def result_observations(root: str | Path, job: dict[str, Any], *, offset: int = 0, limit: int = 50) -> dict[str, Any]:
    if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
        raise ValueError("offset must be a non-negative integer")
    _bounded_int(limit, "limit", 200)
    result = job.get("result")
    if not isinstance(result, dict) or not result.get("run_id") or not result.get("output_dir"):
        raise ValueError("Completed job has no collection observations")
    base = workspace_root(root)
    output = _managed_output(base, Path(result["output_dir"]), "job output directory")
    if not output.is_dir() or not (output / "prices.sqlite3").is_file():
        raise ValueError("Completed job ledger is missing")
    from .storage import Store
    store = Store(output)
    try:
        store.run(str(result["run_id"]))
        rows = store.observations(str(result["run_id"]))
    finally:
        store.close()
    return {"total": len(rows), "offset": offset, "limit": limit, "rows": rows[offset:offset + limit]}


def result_report(root: str | Path, job: dict[str, Any]) -> dict[str, Any]:
    result = job.get("result")
    if not isinstance(result, dict) or not result.get("report_path"):
        raise ValueError("Completed job has no XLSX report")
    path = _contained(workspace_root(root), Path(result["report_path"]), "job report")
    if path.suffix.lower() != ".xlsx" or not path.is_file():
        raise ValueError("Registered XLSX report is missing")
    size = path.stat().st_size
    if size > MAX_REPORT_BYTES:
        raise ValueError(f"Registered XLSX report exceeds the {MAX_REPORT_BYTES // (1024 * 1024)} MiB resource limit")
    return {"path": str(path), "size": size, "sha256": _file_sha256(path),
            "media_type": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"}
