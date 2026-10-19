"""Checkpointed, bounded orchestration; proposals and evidence remain distinct."""
from __future__ import annotations

from pathlib import Path
import hashlib
import json
import math
import time
import uuid

from .activation import _atomic_json, activate_config, verify_config
from .models import stable_id, utc_now
from .research import _atomic_write, _locked, load_workspace


SCHEMA = "source-ledger/agent-run/v1"
DONE = {"proposed", "needs_review", "failed"}


def _hash_file(path: Path | None) -> str | None:
    return hashlib.sha256(path.read_bytes()).hexdigest() if path is not None else None


def _topic(workspace: dict) -> str:
    return stable_id({key: workspace[key] for key in ("industry", "market", "product")})


def _integer(value, label: str, lower: int, upper: int):
    if isinstance(value, bool) or not isinstance(value, int) or not lower <= value <= upper:
        raise ValueError(f"{label} must be an integer from {lower} to {upper}")


def _checkpoint(path: Path, state: dict) -> None:
    state["updated_at"] = utc_now()
    _atomic_write(path, state)


def _check_inputs(state: dict) -> None:
    for key, saved in state["inputs"].items():
        if saved["path"] and _hash_file(Path(saved["path"])) != saved["sha256"]:
            raise ValueError(f"Agent input changed: {key}; start a new run")


def _aggregate(state: dict, directory: Path, workspace: dict, seconds: float) -> None:
    """Collect all selected sources into one durable ledger and one XLSX report.

    Failed proposals remain configured sources in this final collection so that
    the report includes their missing prices and collection outcomes.
    """
    from .config import load_config
    from .storage import Store
    sources = []
    by_id = {source["id"]: source for source in workspace["sources"]}
    for task in state["tasks"]:
        candidate = by_id[task["source_id"]]
        if stable_id(candidate) != task["source_fingerprint"]:
            raise ValueError("A selected source changed before final collection")
        draft = task.get("proposal", {}).get("draft_path")
        if draft:
            if _hash_file(Path(draft)) != task.get("draft_sha256"):
                raise ValueError("Proposed draft changed; start a new agent run")
            document = json.loads(Path(draft).read_text(encoding="utf-8"))
            if len(document["sources"]) != 1 or document["sources"][0]["id"] != task["source_id"]:
                raise ValueError("Proposed draft does not match the selected source")
            sources.extend(document["sources"])
        else:
            sources.append({
                "id": candidate["id"], "name": candidate["name"], "kind": "web",
                "location": candidate["location"], "allowed_domains": candidate["allowed_domains"],
                "product_ids": ["product"], "backends": ["http", "playwright"],
                "internal": candidate["internal"], "account_scope": candidate["scope"], "max_attempts": 1,
            })
    document = {
        "name": f"{workspace['industry']} — {workspace['product']['name']} — {workspace['market']}",
        "products": [workspace["product"]], "sources": sources,
        "output_dir": str((Path(state["workspace_path"]).parent / "outputs").resolve()),
        "max_run_seconds": max(0.1, seconds),
    }
    # Final files use unique attempt names, so an interrupted final run never
    # replaces a verified config or receipt on resume.
    prefix = directory / ("collection-" + uuid.uuid4().hex)
    config_path = prefix.with_suffix(".json")
    receipt_path = prefix.with_suffix(".receipt.json")
    _atomic_json(config_path, document)
    sample_path = None
    if state["inputs"]["samples"]["path"]:
        from .activation import SAMPLES_SCHEMA, _load_samples
        expected = {task["source_id"] for task in state["tasks"]}
        samples = [row for row in _load_samples(state["inputs"]["samples"]["path"]) if row["source_id"] in expected]
        if state["activate"] and {row["source_id"] for row in samples} != expected:
            state["activation_blocker"] = "Known samples do not cover every selected source"
        sample_path = prefix.with_suffix(".samples.json")
        _atomic_json(sample_path, {"schema": SAMPLES_SCHEMA, "samples": samples})
    verification = verify_config(config_path, receipt_path=receipt_path, samples_path=sample_path)
    state["collection"] = {"config_path": str(config_path), "receipt_path": str(receipt_path),
                           "run_id": verification["run_id"], "eligible": verification["eligible"],
                           "reasons": verification["reasons"]}
    store = Store(Path(load_config(config_path).output_dir))
    try:
        state["collection"]["report_path"] = store.run(verification["run_id"])["report_path"]
    finally:
        store.close()
    if state["activate"] and verification["eligible"] and not state.get("activation_blocker"):
        active = prefix.with_suffix(".active.json")
        state["collection"]["activation"] = activate_config(config_path, receipt_path=receipt_path, output_path=active)


def run_agent(workspace_path, *, run_dir, search_config_path=None, model_config_path=None,
              samples_path=None, source_ids=None, max_sources=3, max_model_calls=0,
              max_seconds=120, activate=False, resume=False, max_steps=None) -> dict:
    """Run one bounded batch; resume reuses saved budgets and pinned source scope.

    max_steps pauses at a source boundary for controlled batches. The total
    max_seconds budget covers active work across resumes, excluding idle time.
    External operations have their own timeouts; this is not a hard process kill.
    """
    from .proposals import propose_source
    from .search import search_workspace

    _integer(max_sources, "max_sources", 1, 50)
    _integer(max_model_calls, "max_model_calls", 0, 50)
    if max_steps is not None:
        _integer(max_steps, "max_steps", 0, 50)
    if isinstance(max_seconds, bool) or not isinstance(max_seconds, (int, float)) or not math.isfinite(max_seconds) or not 1 <= max_seconds <= 3600:
        raise ValueError("max_seconds must be between 1 and 3600")
    workspace_file = Path(workspace_path).resolve()
    directory = Path(run_dir).resolve()
    state_path = directory / "agent-run.json"
    if workspace_file == state_path:
        raise ValueError("Run state cannot overwrite the research workspace")
    with _locked(state_path):
        workspace = load_workspace(workspace_file)
        if not workspace["product"]["identifiers"]:
            raise ValueError("Set exact product identifiers before running the agent")
        if resume:
            if not state_path.is_file():
                raise ValueError("No agent run exists to resume")
            state = json.loads(state_path.read_text(encoding="utf-8"))
            if state.get("schema") != SCHEMA or state.get("workspace_path") != str(workspace_file):
                raise ValueError("Agent run does not match this workspace")
            if state["topic_fingerprint"] != _topic(workspace):
                raise ValueError("Workspace topic or product changed; start a new agent run")
            _check_inputs(state)
            if state["status"] in {"completed", "needs_review", "budget_exhausted"}:
                return state
            # If the process died during an external call, conservatively charge
            # the reserved duration; interruption cannot reset the run budget.
            state["usage"]["elapsed_seconds"] += state.pop("in_flight_seconds", 0)
        else:
            if state_path.exists():
                raise FileExistsError("Agent run already exists; use --resume or a new run directory")
            if activate and samples_path is None:
                raise ValueError("Automatic activation requires a known samples file")
            inputs = {}
            for key, value in (("search", search_config_path), ("model", model_config_path), ("samples", samples_path)):
                path = Path(value).resolve() if value is not None else None
                if path == state_path:
                    raise ValueError("An agent input cannot be the run state")
                inputs[key] = {"path": str(path) if path else None, "sha256": _hash_file(path)}
            if samples_path:
                from .activation import _load_samples
                known_samples = _load_samples(samples_path)
                if activate and not known_samples:
                    raise ValueError("Automatic activation requires non-empty known samples")
            requested = list(dict.fromkeys(source_ids or []))
            known = {source["id"] for source in workspace["sources"]}
            if set(requested) - known:
                raise ValueError("Requested source IDs are not in the workspace")
            if len(requested) > max_sources:
                raise ValueError("Selected sources exceed max_sources")
            state = {
                "schema": SCHEMA, "id": uuid.uuid4().hex, "workspace_path": str(workspace_file),
                "topic_fingerprint": _topic(workspace), "created_at": utc_now(), "status": "running",
                "inputs": inputs, "activate": bool(activate), "requested_source_ids": requested,
                "limits": {"max_sources": max_sources, "max_model_calls": max_model_calls, "max_seconds": max_seconds},
                "usage": {"model_calls": 0, "search_calls": 0, "elapsed_seconds": 0.0},
                "search": None, "tasks": [], "selection_complete": False,
            }
            _checkpoint(state_path, state)
        started = time.monotonic()
        prior_seconds = state["usage"]["elapsed_seconds"]

        def remaining():
            return state["limits"]["max_seconds"] - prior_seconds - (time.monotonic() - started)

        def save():
            state["usage"]["elapsed_seconds"] = prior_seconds + time.monotonic() - started
            _checkpoint(state_path, state)

        try:
            state["status"] = "running"
            if not state["selection_complete"]:
                search_path = state["inputs"]["search"]["path"]
                if state["search"] is None and search_path and not state["requested_source_ids"]:
                    # Reserve before external work. An interrupted search is never
                    # silently reissued on resume.
                    if remaining() <= 0:
                        state["status"] = "budget_exhausted"
                        return state
                    state["search"] = {"status": "interrupted", "reason": "Search did not finish; inspect candidates or start a new run."}
                    state["usage"]["search_calls"] = 1
                    state["in_flight_seconds"] = min(30, remaining())
                    save()
                    state["search"] = search_workspace(workspace_file, provider_path=search_path,
                                                       limit=state["limits"]["max_sources"], timeout_seconds=min(30, remaining()))
                    state["usage"]["search_calls"] = state["search"].get("network_calls", 1)
                    state.pop("in_flight_seconds", None)
                    save()
                elif state["search"] is None:
                    state["search"] = {"status": "not_requested" if state["requested_source_ids"] else "search_provider_unconfigured", "network_calls": 0}
                workspace = load_workspace(workspace_file)
                if _topic(workspace) != state["topic_fingerprint"]:
                    raise ValueError("Workspace topic changed during search")
                requested = state["requested_source_ids"]
                sources = workspace["sources"]
                if requested:
                    by_id = {source["id"]: source for source in sources}
                    sources = [by_id[source_id] for source_id in requested]
                elif state["search"].get("candidate_ids"):
                    ranked = state["search"]["candidate_ids"]
                    sources = sorted(sources, key=lambda item: ranked.index(item["id"]) if item["id"] in ranked else len(ranked))
                state["tasks"] = [{"source_id": source["id"], "source_fingerprint": stable_id(source), "status": "queued"}
                                  for source in sources[:state["limits"]["max_sources"]]]
                state["selection_complete"] = True
                save()
            if state["activate"]:
                from .activation import _load_samples
                covered = {(sample["source_id"], sample["product_id"]) for sample in _load_samples(state["inputs"]["samples"]["path"])}
                required = {(task["source_id"], "product") for task in state["tasks"]}
                if required - covered:
                    state["activation_blocker"] = "Known samples do not cover every selected source/product pair"
                    state["status"] = "needs_review"
                    return state
            processed = 0
            for task in state["tasks"]:
                if task["status"] in DONE:
                    continue
                if max_steps is not None and processed >= max_steps:
                    state["status"] = "paused"
                    break
                if remaining() <= 0:
                    state["status"] = "budget_exhausted"
                    break
                current = load_workspace(workspace_file)
                _check_inputs(state)
                source = next((item for item in current["sources"] if item["id"] == task["source_id"]), None)
                if _topic(current) != state["topic_fingerprint"] or source is None or stable_id(source) != task["source_fingerprint"]:
                    raise ValueError("Selected source or product changed; start a new agent run")
                task_dir = directory / "sources" / task["source_id"] / ("attempt-" + uuid.uuid4().hex)
                model_path = state["inputs"]["model"]["path"]
                allowance = int(bool(model_path) and state["usage"]["model_calls"] < state["limits"]["max_model_calls"])
                state["usage"]["model_calls"] += allowance
                task.update(status="running", attempt_dir=str(task_dir))
                state["in_flight_seconds"] = min(30, remaining())
                save()
                try:
                    proposal = propose_source(workspace_file, source_id=task["source_id"], output_dir=task_dir,
                                              model_config_path=model_path, timeout_seconds=min(30, remaining()), max_model_calls=allowance)
                    actual_calls = proposal.get("model_calls", allowance)
                    if actual_calls not in (0, 1) or actual_calls > allowance:
                        raise RuntimeError("Proposal exceeded its model-call allowance")
                    state["usage"]["model_calls"] -= allowance - actual_calls
                    task["proposal"] = proposal
                    state.pop("in_flight_seconds", None)
                    if proposal.get("draft_path"):
                        task["draft_sha256"] = _hash_file(Path(proposal["draft_path"]))
                    save()
                    if not proposal.get("eligible_for_verification") or not proposal.get("draft_path"):
                        task.update(status="needs_review", reason=proposal.get("reason") or "Source rule needs review")
                    else:
                        task.update(status="proposed", reason="Source-backed rules are ready for final collection verification")
                except (ValueError, OSError, RuntimeError) as exc:
                    # External libraries can include credential URLs in messages.
                    task.update(status="failed", reason=f"Source processing failed ({type(exc).__name__}); inspect local proposal/receipt.")
                processed += 1
                save()
            if state["status"] == "running" and state["tasks"]:
                if remaining() <= 0:
                    state["status"] = "budget_exhausted"
                else:
                    current = load_workspace(workspace_file)
                    _check_inputs(state)
                    if _topic(current) != state["topic_fingerprint"]:
                        raise ValueError("Workspace product changed before final collection")
                    state["in_flight_seconds"] = remaining()
                    save()
                    _aggregate(state, directory, current, remaining())
                    state.pop("in_flight_seconds", None)
            if state["status"] == "running":
                state["status"] = "completed" if state["tasks"] and all(task["status"] == "proposed" for task in state["tasks"]) else "needs_review"
                if not state.get("collection", {}).get("eligible") or state.get("activation_blocker"):
                    state["status"] = "needs_review"
            return state
        finally:
            state.pop("in_flight_seconds", None)
            save()
