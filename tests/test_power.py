"""Unattended collection opts out of Windows background power throttling."""
import sys

from su_crawler.power import keep_full_speed


def test_keep_full_speed_opts_out_of_windows_power_throttling():
    assert keep_full_speed() is (sys.platform == "win32")


def test_collect_plan_and_worker_request_full_speed(monkeypatch):
    from su_crawler import assistant_runtime, cli, power
    calls = []
    monkeypatch.setattr(power, "keep_full_speed", lambda: calls.append("speed") or True)
    monkeypatch.setattr(assistant_runtime, "_run_worker", lambda *args: 0)
    monkeypatch.setattr(assistant_runtime, "_root", lambda root: root)
    monkeypatch.setattr(assistant_runtime, "_job_id", lambda token: token)
    assert assistant_runtime.main(["worker", "--root", "r", "--token", "t"]) == 0
    try:
        cli.main(["collect-plan", "--urls-file", "missing.txt", "--industry", "i", "--product", "p", "--market", "m"])  # refused without --yes
    except (SystemExit, ValueError):
        pass
    assert calls == ["speed", "speed"]
