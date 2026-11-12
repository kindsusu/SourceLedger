from pathlib import Path

import pytest

from su_crawler.browser_runtime import (
    BROWSERS_ENV, browser_headless, select_browser_runtime, use_environment_browsers,
)


class Chromium:
    def __init__(self, executable: Path):
        self.executable_path = str(executable)


def test_managed_chromium_is_preferred_on_every_platform(tmp_path, monkeypatch):
    managed = tmp_path / "managed-chromium"
    managed.write_bytes(b"browser")
    monkeypatch.setattr("su_crawler.browser_runtime.shutil.which", lambda _: str(tmp_path / "system-browser"))
    runtime = select_browser_runtime(Chromium(managed), system="Linux", environ={})
    assert runtime is not None
    assert runtime.kind == "managed" and runtime.executable == managed
    assert runtime.launch_options() == {}


@pytest.mark.parametrize("system,command", [
    ("Linux", "google-chrome-stable"),
    ("Darwin", "microsoft-edge"),
    ("Windows", "msedge"),
])
def test_system_browser_fallback_uses_the_detected_executable(tmp_path, monkeypatch, system, command):
    missing = tmp_path / "missing-managed"
    executable = tmp_path / command
    executable.write_bytes(b"browser")
    monkeypatch.setattr(
        "su_crawler.browser_runtime.shutil.which",
        lambda name: str(executable) if name == command else None,
    )
    runtime = select_browser_runtime(Chromium(missing), system=system, environ={})
    assert runtime is not None and runtime.kind == "system"
    assert runtime.launch_options() == {"executable_path": str(executable.resolve())}


def test_missing_managed_and_system_browser_is_unavailable(tmp_path, monkeypatch):
    monkeypatch.setattr("su_crawler.browser_runtime.shutil.which", lambda _: None)
    assert select_browser_runtime(Chromium(tmp_path / "missing"), system="Linux", environ={}) is None


@pytest.mark.parametrize("value,expected", [
    (None, False), ("0", False), ("false", False), ("1", True), ("true", True),
])
def test_headless_environment_contract(value, expected):
    environment = {} if value is None else {"SOURCELEDGER_HEADLESS": value}
    assert browser_headless(environment) is expected


def test_invalid_headless_environment_is_rejected():
    with pytest.raises(ValueError, match="must be 1 or 0"):
        browser_headless({"SOURCELEDGER_HEADLESS": "sometimes"})


def test_environment_browsers_are_used_when_setup_installed_them(tmp_path):
    (tmp_path / "ms-playwright").mkdir()
    environ = {}
    assert use_environment_browsers(environ, prefix=tmp_path) == tmp_path / "ms-playwright"
    assert environ == {BROWSERS_ENV: str(tmp_path / "ms-playwright")}


@pytest.mark.parametrize("value", ["D:/browsers", ""])
def test_an_explicit_browsers_path_is_left_alone(tmp_path, value):
    (tmp_path / "ms-playwright").mkdir()
    environ = {BROWSERS_ENV: value}
    assert use_environment_browsers(environ, prefix=tmp_path) is None
    assert environ == {BROWSERS_ENV: value}


def test_older_installs_keep_playwrights_default_folder(tmp_path):
    environ = {}
    assert use_environment_browsers(environ, prefix=tmp_path) is None
    assert environ == {}
