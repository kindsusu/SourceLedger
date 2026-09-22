"""Durable single-worker runtime for the local SourceLedger assistant."""
from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator
import argparse
import ctypes
import json
import os
import subprocess
import sys
import time
import uuid

from .models import utc_now
from .research import _atomic_write, _locked


OPERATIONS = frozenset({"discover", "propose", "agent", "collect_sites", "verify", "run", "export"})
JOB_STATES = frozenset({"queued", "running", "succeeded", "failed", "interrupted"})
MAX_ARGUMENT_BYTES = 256 * 1024
MAX_JOBS_LIMIT = 100
POLL_SECONDS = 0.1
HEARTBEAT_SECONDS = 2.0
STARTUP_SECONDS = 15.0


def _root(value: str | Path) -> Path:
    root = Path(value).expanduser().resolve()
    for child in (root / "runtime", root / "jobs"):
        if child.is_symlink() or (child.exists() and child.resolve().parent != root):
            raise ValueError("Assistant runtime directories must not be symbolic links")
    return root


def _job_id(value: str) -> str:
    if not isinstance(value, str) or len(value) != 32 or any(char not in "0123456789abcdef" for char in value):
        raise ValueError("job_id must be a lowercase UUID hex value")
    return value


def _read_json(path: Path) -> dict[str, Any] | None:
    if not path.is_file() or path.is_symlink():
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def _write_json(path: Path, value: dict[str, Any]) -> None:
    """Atomically write JSON, tolerating brief Windows reader contention."""
    for attempt in range(5):
        try:
            _atomic_write(path, value)
            return
        except PermissionError:
            if attempt == 4:
                raise
            time.sleep(0.01 * (attempt + 1))


def _pid_alive(pid: Any) -> bool:
    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
        return False
    if os.name == "nt":
        # os.kill(pid, 0) is not a non-mutating existence probe on Windows.
        # Query the process handle instead and never send a signal here.
        process_query_limited_information = 0x1000
        still_active = 259
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.argtypes = [ctypes.c_ulong, ctypes.c_int, ctypes.c_ulong]
        kernel32.OpenProcess.restype = ctypes.c_void_p
        kernel32.GetExitCodeProcess.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_ulong)]
        kernel32.GetExitCodeProcess.restype = ctypes.c_int
        kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
        kernel32.CloseHandle.restype = ctypes.c_int
        handle = kernel32.OpenProcess(process_query_limited_information, False, pid)
        if not handle:
            return False
        try:
            exit_code = ctypes.c_ulong()
            return bool(kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code))) and exit_code.value == still_active
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except (OSError, ValueError):
        return False
    return True


def _validate_arguments(arguments: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(arguments, dict):
        raise ValueError("arguments must be an object")
    if any(not isinstance(key, str) or key.startswith("_") for key in arguments):
        raise ValueError("argument names must be public string fields")
    try:
        encoded = json.dumps(arguments, ensure_ascii=False, allow_nan=False).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ValueError("arguments must contain JSON-compatible finite values") from exc
    if len(encoded) > MAX_ARGUMENT_BYTES:
        raise ValueError("arguments exceed the 256 KiB limit")
    return json.loads(encoded.decode("utf-8"))


def _job_path(root: Path, job_id: str) -> Path:
    directory = root / "jobs" / _job_id(job_id)
    jobs = root / "jobs"
    if (directory.is_symlink() or directory.parent.resolve() != jobs.resolve() or
            (directory.exists() and directory.resolve().parent != jobs.resolve())):
        raise ValueError("job directory must not be a symbolic link or junction")
    return directory / "job.json"


def _job_files(root: Path) -> Iterator[Path]:
    jobs = root / "jobs"
    if not jobs.is_dir() or jobs.is_symlink():
        return
    for directory in jobs.iterdir():
        if (directory.is_symlink() or not directory.is_dir() or
                directory.resolve().parent != jobs.resolve()):
            continue
        try:
            _job_id(directory.name)
        except ValueError:
            continue
        path = directory / "job.json"
        if path.is_file() and not path.is_symlink():
            yield path


def _write_job(root: Path, job: dict[str, Any]) -> None:
    path = _job_path(root, job["id"])
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.parent.is_symlink():
        raise ValueError("job directory must not be a symbolic link")
    _write_json(path, job)


def _assert_lock_safe(target: Path) -> None:
    lock = target.with_name(target.name + ".lock")
    if (lock.is_symlink() or
            (lock.exists() and lock.resolve().parent != target.parent.resolve())):
        raise ValueError("assistant lock files must not be symbolic links or junctions")


def _mark_abandoned(root: Path, active_token: str | None) -> None:
    """Interrupt jobs not owned by the currently locked worker.

    Comparing worker tokens as well as PIDs prevents PID reuse and a newly
    written starting state from hiding work abandoned by an older process.
    """
    for path in _job_files(root):
        try:
            _assert_lock_safe(path)
            with _locked(path):
                job = _read_json(path)
                if job and job.get("status") == "running" and job.get("worker_token") != active_token:
                    job.update(status="interrupted", updated_at=utc_now(), error={"type": "WorkerInterrupted"})
                    job.pop("worker_token", None)
                    _write_json(path, job)
        except RuntimeError:
            # The worker is transitioning this receipt; a later status read can
            # reconcile it if the owning worker disappears.
            continue


def _render_status(state: dict[str, Any] | None, *, worker_locked: bool,
                   start_in_progress: bool = False) -> dict[str, Any]:
    if not state:
        return {"status": "starting" if start_in_progress else "stopped",
                "pid": None, "current_job_id": None}
    status = state.get("status")
    active = worker_locked and (status == "starting" or _pid_alive(state.get("pid")))
    if start_in_progress and not active:
        status, active = "starting", True
    if not active:
        status = "stopped"
    if status not in {"starting", "idle", "running", "stopping", "stopped"}:
        status = "stopped"
        active = False
    return {
        "status": status,
        "pid": state.get("pid") if active else None,
        "current_job_id": state.get("current_job_id") if active else None,
        "updated_at": state.get("updated_at"),
    }


def _status_with_start_lock(workspace: Path) -> dict[str, Any]:
    """Inspect worker ownership while the caller prevents a new launch."""
    target = workspace / "runtime" / "worker"
    _assert_lock_safe(target)
    try:
        with _locked(target):
            # Holding both start and worker locks proves that no worker can
            # claim a job until stale receipts are reconciled.
            state = _read_json(workspace / "runtime" / "state.json")
            _mark_abandoned(workspace, None)
            return _render_status(state, worker_locked=False)
    except RuntimeError:
        # Do not probe by briefly acquiring and releasing the worker lock: that
        # can make a concurrently starting worker's non-blocking lock fail.
        state = _read_json(workspace / "runtime" / "state.json")
        return _render_status(state, worker_locked=True)


def runtime_status(root: str | Path) -> dict[str, Any]:
    workspace = _root(root)
    start = workspace / "runtime" / "start"
    _assert_lock_safe(start)
    try:
        with _locked(start):
            return _status_with_start_lock(workspace)
    except RuntimeError:
        # A launcher owns the start lock. Trust only its transitional state and
        # never reconcile jobs until either it finishes or a worker owns them.
        state = _read_json(workspace / "runtime" / "state.json")
        return _render_status(state, worker_locked=False, start_in_progress=True)


def submit_job(root: str | Path, operation: str, arguments: dict[str, Any]) -> dict[str, Any]:
    workspace = _root(root)
    if operation not in OPERATIONS:
        raise ValueError("operation is not supported")
    safe_arguments = _validate_arguments(arguments)
    job_id = uuid.uuid4().hex
    now = utc_now()
    job = {
        "schema": "source-ledger/assistant-job/v1", "id": job_id, "operation": operation,
        "arguments": safe_arguments, "status": "queued", "attempt": 0,
        "created_at": now, "updated_at": now,
    }
    _write_job(workspace, job)
    return dict(job)


def get_job(root: str | Path, job_id: str) -> dict[str, Any]:
    workspace = _root(root)
    runtime_status(workspace)
    job = _read_json(_job_path(workspace, job_id))
    if job is None or job.get("id") != job_id or job.get("status") not in JOB_STATES:
        raise FileNotFoundError(f"Unknown job: {job_id}")
    result = _read_json(_job_path(workspace, job_id).with_name("result.json"))
    return {**job, **({"result": result} if result is not None else {})}


def list_jobs(root: str | Path, limit: int = 20) -> list[dict[str, Any]]:
    workspace = _root(root)
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= MAX_JOBS_LIMIT:
        raise ValueError(f"limit must be an integer from 1 to {MAX_JOBS_LIMIT}")
    runtime_status(workspace)
    jobs = []
    for path in _job_files(workspace):
        value = _read_json(path)
        if value and value.get("status") in JOB_STATES:
            jobs.append(value)
    jobs.sort(key=lambda item: (str(item.get("created_at", "")), str(item.get("id", ""))), reverse=True)
    return jobs[:limit]


def resume_job(root: str | Path, job_id: str) -> dict[str, Any]:
    workspace = _root(root)
    path = _job_path(workspace, job_id)
    if not path.is_file():
        raise FileNotFoundError(f"Unknown job: {job_id}")
    _assert_lock_safe(path)
    with _locked(path):
        job = _read_json(path)
        if job is None:
            raise FileNotFoundError(f"Unknown job: {job_id}")
        result_path = path.with_name("result.json")
        result = _read_json(result_path)
        paused_agent = (job.get("status") == "succeeded" and job.get("operation") == "agent" and
                        isinstance(result, dict) and isinstance(result.get("result"), dict) and
                        result["result"].get("status") == "paused")
        if job.get("status") != "interrupted" and not paused_agent:
            raise ValueError("only interrupted or paused agent jobs can be resumed")
        job.update(status="queued", updated_at=utc_now(), error=None)
        job.pop("worker_token", None)
        if result_path.exists():
            result_path.unlink()
        _write_json(path, job)
    return job


def start_worker(root: str | Path) -> dict[str, Any]:
    workspace = _root(root)
    runtime = workspace / "runtime"
    runtime.mkdir(parents=True, exist_ok=True)
    for path in (runtime / "start.lock", runtime / "worker.lock", runtime / "worker.log"):
        if path.is_symlink() or (path.exists() and path.resolve().parent != runtime.resolve()):
            raise ValueError("assistant runtime files must not be symbolic links or junctions")
    with _locked(runtime / "start"):
        status = _status_with_start_lock(workspace)
        if status["status"] != "stopped":
            return status
        token = uuid.uuid4().hex
        # A prior timed-out launcher may have left a request for its own token.
        # It cannot apply to this new worker and should not accumulate forever.
        (runtime / "stop.json").unlink(missing_ok=True)
        _write_json(runtime / "state.json", {
            "status": "starting", "pid": None, "token": token, "current_job_id": None, "updated_at": utc_now(),
        })
        log = (runtime / "worker.log").open("ab", buffering=0)
        # Keep the venv entry point itself. Resolving a POSIX venv symlink can
        # silently select the global interpreter and lose installed packages.
        interpreter = os.path.abspath(os.path.expanduser(sys.executable))
        command = [interpreter, "-m", "su_crawler.assistant_runtime", "worker", "--root", str(workspace), "--token", token]
        options: dict[str, Any] = {"stdin": subprocess.DEVNULL, "stdout": log, "stderr": subprocess.STDOUT, "cwd": str(workspace)}
        if os.name == "nt":
            options["creationflags"] = (getattr(subprocess, "CREATE_NO_WINDOW", 0) |
                                        getattr(subprocess, "DETACHED_PROCESS", 0) |
                                        getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0))
        else:
            options["start_new_session"] = True
        try:
            process = subprocess.Popen(command, **options)
        finally:
            log.close()
        deadline = time.monotonic() + STARTUP_SECONDS
        while time.monotonic() < deadline:
            state = _read_json(runtime / "state.json")
            # Some Windows venv launchers hand off to a child interpreter, so
            # the durable worker PID can legitimately differ from Popen.pid.
            if state and state.get("token") == token and state.get("pid") and state.get("status") != "starting":
                return _render_status(state, worker_locked=True)
            if process.poll() is not None:
                break
            time.sleep(0.05)
        if process.poll() is not None:
            _write_json(runtime / "state.json", {
                "status": "stopped", "pid": None, "token": token, "current_job_id": None,
                "updated_at": utc_now(), "error": {"type": "WorkerStartFailed"},
            })
            raise RuntimeError(f"Assistant worker exited before it became ready; inspect {runtime / 'worker.log'}")
        # Do not signal or kill the detached process. A token-scoped stop makes
        # it exit if it later acquires the worker lock; if a new launch replaces
        # state first, its expected-token check rejects this stale child.
        _write_json(runtime / "stop.json", {"token": token, "requested_at": utc_now()})
        raise RuntimeError(f"Assistant worker did not become ready within {STARTUP_SECONDS:g} seconds; inspect {runtime / 'worker.log'}")


def stop_worker(root: str | Path) -> dict[str, Any]:
    workspace = _root(root)
    runtime = workspace / "runtime"
    start = runtime / "start"
    _assert_lock_safe(start)
    try:
        with _locked(start):
            status = _status_with_start_lock(workspace)
            state = _read_json(runtime / "state.json")
            if status["status"] == "stopped" or not state or not isinstance(state.get("token"), str):
                return status
            _write_json(runtime / "stop.json", {"token": state["token"], "requested_at": utc_now()})
            return {**status, "stop_requested": True}
    except RuntimeError:
        # A launcher owns the start lock. Once its new token is visible, a
        # token-scoped stop request is safe and the child will honor it without
        # receiving a process signal.
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline:
            state = _read_json(runtime / "state.json")
            token = state.get("token") if state else None
            if (state and state.get("status") == "starting" and isinstance(token, str) and
                    len(token) == 32 and all(char in "0123456789abcdef" for char in token)):
                _write_json(runtime / "stop.json", {"token": token, "requested_at": utc_now()})
                return {**_render_status(state, worker_locked=False, start_in_progress=True),
                        "stop_requested": True}
            time.sleep(0.05)
        return runtime_status(workspace)


@contextmanager
def _worker_lock(path: Path) -> Iterator[None]:
    _assert_lock_safe(path)
    try:
        with _locked(path):
            yield
    except RuntimeError as exc:
        raise RuntimeError("Assistant worker is already running") from exc


def _next_queued(root: Path) -> dict[str, Any] | None:
    # Scan all job receipts. list_jobs intentionally limits user-facing output,
    # but applying that limit here can starve old queued work indefinitely.
    queued = []
    for path in _job_files(root):
        job = _read_json(path)
        if job and job.get("status") == "queued":
            queued.append(job)
    return min(queued, key=lambda item: (item["created_at"], item["id"])) if queued else None


def _claim_job(root: Path, job_id: str, token: str) -> dict[str, Any] | None:
    path = _job_path(root, job_id)
    try:
        _assert_lock_safe(path)
        with _locked(path):
            job = _read_json(path)
            if not job or job.get("status") != "queued":
                return None
            job.update(status="running", attempt=int(job.get("attempt", 0)) + 1,
                       worker_token=token, updated_at=utc_now(), error=None)
            _write_json(path, job)
            return job
    except RuntimeError:
        return None


def _finish_job(root: Path, job: dict[str, Any], token: str, *, result: dict[str, Any] | None,
                error: BaseException | None) -> None:
    path = _job_path(root, job["id"])
    _assert_lock_safe(path)
    with _locked(path):
        current = _read_json(path)
        if not current or current.get("status") != "running" or current.get("worker_token") != token:
            raise RuntimeError("job ownership changed while it was running")
        if error is None:
            if result is None:
                raise TypeError("execute_job must return an object")
            _write_json(path.with_name("result.json"), result)
            current.update(status="succeeded", updated_at=utc_now())
            current.pop("error", None)
        else:
            details = {"type": type(error).__name__}
            if isinstance(error, (ValueError, FileNotFoundError)):
                details["message"] = ("Required workspace input was not found."
                                      if isinstance(error, FileNotFoundError) else
                                      "Job arguments or workspace inputs are invalid; review the job configuration.")
            elif isinstance(error, RuntimeError):
                details["message"] = "The workspace is busy or the operation could not continue; review inputs and retry."
            current.update(status="failed", updated_at=utc_now(), error=details)
        current.pop("worker_token", None)
        _write_json(path, current)


def _run_worker(root: Path, token: str, idle_timeout: float) -> int:
    runtime = root / "runtime"
    runtime.mkdir(parents=True, exist_ok=True)
    state_path = runtime / "state.json"
    with _worker_lock(runtime / "worker"):
        expected = _read_json(state_path)
        if not expected or expected.get("token") != token:
            return 2
        # This process now owns the lock. Any running receipt bearing another
        # token was abandoned, including receipts hidden by a replacement state.
        _mark_abandoned(root, token)
        state = {"status": "idle", "pid": os.getpid(), "token": token, "current_job_id": None, "updated_at": utc_now()}
        _write_json(state_path, state)
        last_work = time.monotonic()
        last_heartbeat = last_work
        while True:
            stop = _read_json(runtime / "stop.json")
            if stop and stop.get("token") == token:
                break
            job = _next_queued(root)
            if job is None:
                now = time.monotonic()
                if now - last_heartbeat >= HEARTBEAT_SECONDS:
                    state.update(status="idle", current_job_id=None, updated_at=utc_now())
                    _write_json(state_path, state)
                    last_heartbeat = now
                if idle_timeout > 0 and now - last_work >= idle_timeout:
                    break
                time.sleep(POLL_SECONDS)
                continue
            job = _claim_job(root, job["id"], token)
            if job is None:
                continue
            last_work = time.monotonic()
            state.update(status="running", current_job_id=job["id"], updated_at=utc_now())
            _write_json(state_path, state)
            result = None
            error = None
            try:
                from .assistant_workspace import execute_job
                execution_arguments = dict(job["arguments"])
                execution_arguments["_attempt"] = job["attempt"]
                if job["attempt"] > 1:
                    execution_arguments["_resume"] = True
                result = execute_job(root, job["operation"], execution_arguments, job_id=job["id"])
                if not isinstance(result, dict):
                    raise TypeError("execute_job must return an object")
            except Exception as exc:
                error = exc
            _finish_job(root, job, token, result=result, error=error)
            state.update(status="idle", current_job_id=None, updated_at=utc_now())
            _write_json(state_path, state)
            last_heartbeat = time.monotonic()
        state.update(status="stopped", current_job_id=None, updated_at=utc_now())
        _write_json(state_path, state)
        stop = _read_json(runtime / "stop.json")
        if stop and stop.get("token") == token:
            (runtime / "stop.json").unlink(missing_ok=True)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="source-ledger-assistant-runtime")
    subparsers = parser.add_subparsers(dest="command", required=True)
    worker = subparsers.add_parser("worker")
    worker.add_argument("--root", required=True)
    worker.add_argument("--token", required=True)
    worker.add_argument("--idle-timeout", type=float, default=0)
    args = parser.parse_args(argv)
    if args.command == "worker":
        if args.idle_timeout < 0 or args.idle_timeout > 86400:
            parser.error("--idle-timeout must be from 0 to 86400 seconds")
        return _run_worker(_root(args.root), _job_id(args.token), args.idle_timeout)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
