"""The storage command must be explicit about its output and preview mode."""

import json
from pathlib import Path

from su_crawler.cli import main


def test_storage_cli_previews_by_default_and_applies_only_on_request(tmp_path, monkeypatch, capsys):
    calls = []

    def maintain(output, *, apply, grace_days):
        calls.append((output, apply, grace_days))
        return {"mode": "apply" if apply else "dry_run", "removed_files": int(apply)}

    monkeypatch.setattr("su_crawler.retention.maintain_storage", maintain)
    directory = str(tmp_path / "output")
    assert main(["storage", "--output-dir", directory]) == 0
    assert json.loads(capsys.readouterr().out)["mode"] == "dry_run"
    assert main(["storage", "--output-dir", directory, "--apply", "--grace-days", "14"]) == 0
    assert json.loads(capsys.readouterr().out)["removed_files"] == 1
    assert calls == [(Path(directory), False, 7), (Path(directory), True, 14)]


def test_storage_cli_rejects_invalid_grace_period(tmp_path, capsys):
    directory = tmp_path / "output"
    directory.mkdir()
    assert main(["storage", "--output-dir", str(directory), "--grace-days", "0"]) == 2
    assert "grace_days must be 1 to 365" in capsys.readouterr().err
