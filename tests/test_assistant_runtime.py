from __future__ import annotations

import sys
import threading
import time
import types

import pytest

from su_crawler import assistant_runtime as runtime
from su_crawler.research import _atomic_write


def wait_for(predicate, timeout=5):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.05)
    raise AssertionError("condition was not reached")


def test_submit_is_offline_and_validates_bounded_input(tmp_path):
    job = runtime.submit_job(tmp_path, "discover", {"query": "local fixture"})
    assert job["status"] == "queued" and len(job["id"]) == 32
    assert runtime.runtime_status(tmp_path)["status"] == "stopped"
    assert runtime.get_job(tmp_path, job["id"])["arguments"] == {"query": "local fixture"}
    with pytest.raises(ValueError, match="operation"):
        runtime.submit_job(tmp_path, "delete_everything", {})
    with pytest.raises(ValueError, match="256 KiB"):
        runtime.submit_job(tmp_path, "discover", {"value": "x" * (runtime.MAX_ARGUMENT_BYTES + 1)})
    with pytest.raises(ValueError, match="public"):
        runtime.submit_job(tmp_path, "discover", {"_resume": True})


def test_worker_process_is_singleton_and_stops_without_pid_signals(tmp_path):
    first = runtime.start_worker(tmp_path)
    assert first["status"] in {"idle", "running"} and first["pid"]
    second = runtime.start_worker(tmp_path)
    assert second["pid"] == first["pid"]
    stopped = runtime.stop_worker(tmp_path)
    assert stopped["stop_requested"] is True
    wait_for(lambda: runtime.runtime_status(tmp_path)["status"] == "stopped")


def test_start_worker_reports_child_startup_failure(tmp_path, monkeypatch):
    class FailedProcess:
        pid = 12345

        @staticmethod
        def poll():
            return 2

    monkeypatch.setattr(runtime.subprocess, "Popen", lambda *args, **kwargs: FailedProcess())
    with pytest.raises(RuntimeError, match="worker.log"):
        runtime.start_worker(tmp_path)
    assert runtime.runtime_status(tmp_path)["status"] == "stopped"


def test_start_timeout_requests_token_scoped_stop(tmp_path, monkeypatch):
    class HangingProcess:
        pid = 12345

        @staticmethod
        def poll():
            return None

    clock = iter([0, 0, runtime.STARTUP_SECONDS + 1])
    monkeypatch.setattr(runtime.subprocess, "Popen", lambda *args, **kwargs: HangingProcess())
    monkeypatch.setattr(runtime.time, "monotonic", lambda: next(clock))
    with pytest.raises(RuntimeError, match="did not become ready"):
        runtime.start_worker(tmp_path)
    state = runtime._read_json(tmp_path / "runtime" / "state.json")
    stop = runtime._read_json(tmp_path / "runtime" / "stop.json")
    assert stop["token"] == state["token"]


def test_worker_executes_one_job_and_persists_result(tmp_path, monkeypatch):
    job = runtime.submit_job(tmp_path, "export", {"format": "json"})
    token = "a" * 32
    (tmp_path / "runtime").mkdir()
    _atomic_write(tmp_path / "runtime" / "state.json", {
        "status": "starting", "pid": None, "token": token, "current_job_id": None,
        "updated_at": runtime.utc_now(),
    })
    module = types.ModuleType("su_crawler.assistant_workspace")
    module.execute_job = lambda root, operation, arguments, *, job_id: {
        "job_id": job_id, "operation": operation, "format": arguments["format"],
        "attempt": arguments["_attempt"], "resume": arguments.get("_resume", False),
    }
    monkeypatch.setitem(sys.modules, "su_crawler.assistant_workspace", module)
    thread = threading.Thread(target=runtime._run_worker, args=(tmp_path, token, 0.3), daemon=True)
    thread.start()
    completed = wait_for(lambda: runtime.get_job(tmp_path, job["id"]) if runtime.get_job(tmp_path, job["id"])["status"] == "succeeded" else None)
    thread.join(timeout=2)
    assert not thread.is_alive()
    assert completed["attempt"] == 1
    assert completed["result"] == {"job_id": job["id"], "operation": "export", "format": "json",
                                   "attempt": 1, "resume": False}


def test_stop_waits_for_current_job_then_worker_exits(tmp_path, monkeypatch):
    job = runtime.submit_job(tmp_path, "verify", {})
    token = "b" * 32
    (tmp_path / "runtime").mkdir()
    _atomic_write(tmp_path / "runtime" / "state.json", {
        "status": "starting", "pid": None, "token": token, "current_job_id": None,
        "updated_at": runtime.utc_now(),
    })
    entered, release = threading.Event(), threading.Event()
    module = types.ModuleType("su_crawler.assistant_workspace")

    def execute(*args, **kwargs):
        entered.set()
        assert release.wait(2)
        return {"verified": True}

    module.execute_job = execute
    monkeypatch.setitem(sys.modules, "su_crawler.assistant_workspace", module)
    thread = threading.Thread(target=runtime._run_worker, args=(tmp_path, token, 0), daemon=True)
    thread.start()
    assert entered.wait(2)
    response = runtime.stop_worker(tmp_path)
    assert response["stop_requested"] is True
    assert runtime.get_job(tmp_path, job["id"])["status"] == "running"
    release.set()
    thread.join(timeout=3)
    assert runtime.get_job(tmp_path, job["id"])["status"] == "succeeded"
    assert runtime.runtime_status(tmp_path)["status"] == "stopped"


def test_dead_worker_interrupts_running_job_and_requires_explicit_resume(tmp_path):
    job = runtime.submit_job(tmp_path, "agent", {"max_steps": 1})
    token = "c" * 32
    job.update(status="running", worker_token=token)
    runtime._write_job(tmp_path, job)
    (tmp_path / "runtime").mkdir()
    _atomic_write(tmp_path / "runtime" / "state.json", {
        "status": "running", "pid": 999_999_999, "token": token,
        "current_job_id": job["id"], "updated_at": runtime.utc_now(),
    })
    assert runtime.runtime_status(tmp_path)["status"] == "stopped"
    interrupted = runtime.get_job(tmp_path, job["id"])
    assert interrupted["status"] == "interrupted"
    assert interrupted["error"] == {"type": "WorkerInterrupted"}
    resumed = runtime.resume_job(tmp_path, job["id"])
    assert resumed["status"] == "queued" and resumed["attempt"] == 0


def test_replacement_state_does_not_hide_job_abandoned_by_an_old_token(tmp_path):
    job = runtime.submit_job(tmp_path, "verify", {"config_path": "config.json"})
    job.update(status="running", worker_token="old-token")
    runtime._write_job(tmp_path, job)
    (tmp_path / "runtime").mkdir()
    _atomic_write(tmp_path / "runtime" / "state.json", {
        "status": "starting", "pid": None, "token": "new-token",
        "current_job_id": None, "updated_at": runtime.utc_now(),
    })
    runtime.runtime_status(tmp_path)
    assert runtime.get_job(tmp_path, job["id"])["status"] == "interrupted"


def test_status_does_not_interrupt_job_while_worker_start_is_locked(tmp_path):
    job = runtime.submit_job(tmp_path, "verify", {"config_path": "config.json"})
    token = "f" * 32
    job.update(status="running", worker_token=token)
    runtime._write_job(tmp_path, job)
    (tmp_path / "runtime").mkdir(exist_ok=True)
    _atomic_write(tmp_path / "runtime" / "state.json", {
        "status": "starting", "pid": None, "token": token,
        "current_job_id": job["id"], "updated_at": runtime.utc_now(),
    })
    with runtime._locked(tmp_path / "runtime" / "start"):
        assert runtime.runtime_status(tmp_path)["status"] == "starting"
        assert runtime.get_job(tmp_path, job["id"])["status"] == "running"


def test_stop_during_start_writes_token_scoped_request(tmp_path):
    token = "1" * 32
    (tmp_path / "runtime").mkdir()
    _atomic_write(tmp_path / "runtime" / "state.json", {
        "status": "starting", "pid": None, "token": token,
        "current_job_id": None, "updated_at": runtime.utc_now(),
    })
    with runtime._locked(tmp_path / "runtime" / "start"):
        stopped = runtime.stop_worker(tmp_path)
    assert stopped["stop_requested"] is True
    assert runtime._read_json(tmp_path / "runtime" / "stop.json")["token"] == token


def test_oldest_queued_job_is_selected_beyond_public_list_limit(tmp_path):
    oldest = runtime.submit_job(tmp_path, "discover", {"source_id": "oldest"})
    for index in range(runtime.MAX_JOBS_LIMIT + 5):
        job = runtime.submit_job(tmp_path, "discover", {"source_id": str(index)})
        if index < runtime.MAX_JOBS_LIMIT:
            job["status"] = "succeeded"
            runtime._write_job(tmp_path, job)
    assert runtime._next_queued(tmp_path)["id"] == oldest["id"]


def test_paused_agent_result_can_be_explicitly_resumed(tmp_path):
    job = runtime.submit_job(tmp_path, "agent", {"max_steps": 1})
    job["status"] = "succeeded"
    runtime._write_job(tmp_path, job)
    _atomic_write(tmp_path / "jobs" / job["id"] / "result.json", {
        "operation": "agent", "result": {"status": "paused"}
    })
    resumed = runtime.resume_job(tmp_path, job["id"])
    assert resumed["status"] == "queued"
    assert not (tmp_path / "jobs" / job["id"] / "result.json").exists()


def test_resumed_attempt_passes_internal_resume_marker_only_to_executor(tmp_path, monkeypatch):
    job = runtime.submit_job(tmp_path, "agent", {"max_steps": 1})
    job.update(status="interrupted", attempt=1, error={"type": "WorkerInterrupted"})
    runtime._write_job(tmp_path, job)
    runtime.resume_job(tmp_path, job["id"])
    token = "e" * 32
    (tmp_path / "runtime").mkdir(exist_ok=True)
    _atomic_write(tmp_path / "runtime" / "state.json", {
        "status": "starting", "pid": None, "token": token, "current_job_id": None,
        "updated_at": runtime.utc_now(),
    })
    module = types.ModuleType("su_crawler.assistant_workspace")
    module.execute_job = lambda root, operation, arguments, *, job_id: arguments
    monkeypatch.setitem(sys.modules, "su_crawler.assistant_workspace", module)
    thread = threading.Thread(target=runtime._run_worker, args=(tmp_path, token, 0.2), daemon=True)
    thread.start()
    completed = wait_for(lambda: runtime.get_job(tmp_path, job["id"])
                         if runtime.get_job(tmp_path, job["id"])["status"] == "succeeded" else None)
    thread.join(timeout=2)
    assert completed["attempt"] == 2
    assert completed["result"] == {"max_steps": 1, "_attempt": 2, "_resume": True}
    assert runtime.get_job(tmp_path, job["id"])["arguments"] == {"max_steps": 1}


def test_job_ids_limits_and_symlinked_job_directories_are_rejected(tmp_path):
    with pytest.raises(ValueError, match="job_id"):
        runtime.get_job(tmp_path, "../escape")
    with pytest.raises(ValueError, match="limit"):
        runtime.list_jobs(tmp_path, 0)
    target = tmp_path / "outside"
    target.mkdir()
    jobs = tmp_path / "jobs"
    jobs.mkdir()
    linked = jobs / ("d" * 32)
    try:
        linked.symlink_to(target, target_is_directory=True)
    except OSError:
        pytest.skip("directory symlinks are unavailable")
    with pytest.raises(ValueError, match="symbolic link"):
        runtime.get_job(tmp_path, "d" * 32)


def test_symlinked_job_lock_is_rejected(tmp_path):
    job = runtime.submit_job(tmp_path, "discover", {"source_id": "fixture"})
    path = tmp_path / "jobs" / job["id"] / "job.json"
    lock = path.with_name("job.json.lock")
    target = tmp_path / "outside-lock"
    target.write_text("", encoding="utf-8")
    try:
        lock.symlink_to(target)
    except OSError:
        pytest.skip("file symlinks are unavailable")
    with pytest.raises(ValueError, match="lock files"):
        runtime.resume_job(tmp_path, job["id"])
