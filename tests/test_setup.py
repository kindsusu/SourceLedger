from __future__ import annotations

import importlib.util
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]


def load_script(name: str):
    path = ROOT / "scripts" / name
    spec = importlib.util.spec_from_file_location("sourceledger_" + path.stem, path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


setup_script = load_script("setup.py")
launch_script = load_script("launch.py")


def make_existing_environment(root: Path) -> Path:
    python = setup_script.venv_python(root / ".venv")
    python.parent.mkdir(parents=True)
    python.touch()
    return python


def test_minimal_install_reuses_environment_and_applies_constraints(tmp_path):
    root = tmp_path / "소스 ledger!"
    root.mkdir()
    python = make_existing_environment(root)
    constraints = root / "requirements" / "constraints.txt"
    constraints.parent.mkdir()
    constraints.write_text("httpx==0.28.1\n", encoding="utf-8")
    calls = []

    def run(command, **kwargs):
        calls.append((command, kwargs))
        if command[1:3] == ["-c", "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')"]:
            return subprocess.CompletedProcess(command, 0, stdout="3.11\n", stderr="")
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    assert setup_script.install(root=root, runner=run) == python
    assert not any("venv" in command for command, _ in calls)
    assert calls[-1][0] == [
        str(python), "-m", "pip", "install", "--constraint", str(constraints), str(root)
    ]


def test_optional_install_creates_venv_and_installs_browser_last(tmp_path, monkeypatch):
    python = setup_script.venv_python(tmp_path / ".venv")
    calls = []

    def run(command, **kwargs):
        calls.append(command)
        if command[1:3] == ["-m", "venv"]:
            python.parent.mkdir(parents=True)
            python.touch()
        if command[1:2] == ["-c"]:
            return subprocess.CompletedProcess(command, 0, stdout="3.12\n", stderr="")
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    monkeypatch.setattr(setup_script.sys, "executable", "host-python")
    setup_script.install(root=tmp_path, browser=True, mcp=True, dev=True, runner=run)
    assert calls[0] == ["host-python", "-m", "venv", str(tmp_path / ".venv")]
    assert calls[-2] == [
        str(python), "-m", "pip", "install", "--editable", f"{tmp_path}[browser,mcp,test]"
    ]
    assert calls[-1] == [str(python), "-m", "playwright", "install", "chromium"]


def test_invalid_existing_environment_is_preserved(tmp_path):
    python = make_existing_environment(tmp_path)

    def run(command, **kwargs):
        return subprocess.CompletedProcess(command, 0, stdout="3.10\n", stderr="")

    with pytest.raises(RuntimeError, match="requires Python 3.11"):
        setup_script.install(root=tmp_path, runner=run)
    assert python.exists()


def test_malformed_environment_version_is_reported(tmp_path):
    make_existing_environment(tmp_path)

    def run(command, **kwargs):
        return subprocess.CompletedProcess(command, 0, stdout="3\n", stderr="")

    with pytest.raises(RuntimeError, match="Could not read"):
        setup_script.install(root=tmp_path, runner=run)


def test_subprocess_runner_forces_python_utf8(tmp_path, monkeypatch):
    observed = {}

    def run(command, **kwargs):
        observed.update(kwargs)
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    monkeypatch.setattr(setup_script.subprocess, "run", run)
    setup_script._run(["python", "--version"], cwd=tmp_path)
    assert observed["env"]["PYTHONUTF8"] == "1"


def test_install_failure_propagates_and_browser_is_not_attempted(tmp_path):
    make_existing_environment(tmp_path)
    calls = []

    def run(command, **kwargs):
        calls.append(command)
        if command[1:2] == ["-c"]:
            return subprocess.CompletedProcess(command, 0, stdout="3.11\n", stderr="")
        raise subprocess.CalledProcessError(9, command)

    with pytest.raises(subprocess.CalledProcessError):
        setup_script.install(root=tmp_path, browser=True, runner=run)
    assert not any(command[1:4] == ["-m", "playwright", "install"] for command in calls)


@pytest.mark.parametrize(
    ("initialized", "expected"),
    [(False, "ui"), (True, "ui")],
)
def test_launcher_default_opens_browser_workspace(tmp_path, monkeypatch, initialized, expected):
    fake_script = tmp_path / "scripts" / "launch.py"
    fake_script.parent.mkdir()
    fake_script.touch()
    if initialized:
        workspace = tmp_path / ".sourceledger" / "research.json"
        workspace.parent.mkdir()
        workspace.touch()
    observed = {}
    monkeypatch.setattr(launch_script, "__file__", str(fake_script))
    monkeypatch.setattr(launch_script.os, "chdir", lambda path: observed.setdefault("chdir", path))
    monkeypatch.setattr(
        launch_script.subprocess, "call",
        lambda command, cwd: observed.update(command=command, cwd=cwd) or 7,
    )
    assert launch_script.main([]) == 7
    assert observed["command"][-1] == expected
    assert observed["cwd"] == tmp_path


def test_launcher_forwards_explicit_arguments_unchanged(tmp_path, monkeypatch):
    fake_script = tmp_path / "scripts" / "launch.py"
    fake_script.parent.mkdir()
    fake_script.touch()
    observed = {}
    monkeypatch.setattr(launch_script, "__file__", str(fake_script))
    monkeypatch.setattr(launch_script.os, "chdir", lambda path: None)
    monkeypatch.setattr(
        launch_script.subprocess, "call",
        lambda command, cwd: observed.update(command=command) or 0,
    )
    arguments = ["init", "--lang", "ko", "--product", "정밀 펌프"]
    assert launch_script.main(arguments) == 0
    assert observed["command"][3:] == arguments
