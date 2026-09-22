import json
import os
from pathlib import Path
import tomllib

import pytest

pytest.importorskip("mcp")

from su_crawler.connections import connect, install_config, server_spec


@pytest.mark.parametrize("client", ["codex", "claude-code", "claude-desktop"])
def test_generate_round_trip_without_installing(tmp_path, client):
    root = tmp_path / '한글 space! "quote"' if os.name != 'nt' else tmp_path / '한글 space!'
    result = connect(client, workspace_root=root)
    text = Path(result["snippet_path"]).read_text(encoding="utf-8")
    document = tomllib.loads(text) if client == "codex" else json.loads(text)
    server = document["mcp_servers" if client == "codex" else "mcpServers"]["sourceledger"]
    assert server["args"][-1] == str(root.resolve())
    assert server["command"] == result["server"]["command"]
    assert result["status"] == "generated"
    assert not (tmp_path / ".mcp.json").exists()


@pytest.mark.parametrize("client", ["codex", "claude-code", "claude-desktop"])
def test_install_preserves_existing_settings_and_backup(tmp_path, client):
    config = tmp_path / ("config.toml" if client == "codex" else "config.json")
    old = ('# keep comments\nmodel = "existing"\n[mcp_servers.other]\ncommand = "other"\n'
           if client == "codex" else '{"theme":"dark","mcpServers":{"other":{"command":"other"}}}')
    config.write_text(old, encoding="utf-8")
    if os.name != "nt":
        config.chmod(0o600)
    original = config.read_bytes()
    result = connect(client, workspace_root=tmp_path / "work", install=True, config_file=config)
    assert Path(result["backup_path"]).read_bytes() == original
    if os.name != "nt":
        assert config.stat().st_mode & 0o777 == 0o600
        assert Path(result["backup_path"]).stat().st_mode & 0o777 == 0o600
    content = config.read_text(encoding="utf-8")
    data = tomllib.loads(content) if client == "codex" else json.loads(content)
    assert "other" in data["mcp_servers" if client == "codex" else "mcpServers"]
    if client == "codex":
        assert content.startswith("# keep comments")
    else:
        assert data["theme"] == "dark"
    assert connect(client, workspace_root=tmp_path / "work", install=True, config_file=config)["status"] == "already_configured"
    updated = config.read_bytes()
    with pytest.raises(ValueError, match="different settings"):
        connect(client, workspace_root=tmp_path / "another", install=True, config_file=config)
    assert config.read_bytes() == updated


def test_invalid_configuration_is_not_overwritten(tmp_path):
    config = tmp_path / "bad.json"
    config.write_text("{broken", encoding="utf-8")
    with pytest.raises(ValueError):
        connect("claude-desktop", workspace_root=tmp_path / "work", install=True, config_file=config)
    assert config.read_text(encoding="utf-8") == "{broken"


def test_inline_codex_table_is_not_silently_rewritten(tmp_path):
    config = tmp_path / "config.toml"
    config.write_text('mcp_servers = { other = { command = "other" } }\n', encoding="utf-8")
    original = config.read_bytes()
    with pytest.raises(ValueError):
        install_config("codex", config, "sourceledger", server_spec(tmp_path))
    assert config.read_bytes() == original


def test_project_install_does_not_touch_user_configuration(tmp_path):
    result = connect("claude-code", workspace_root=tmp_path / "data", project_dir=tmp_path, install=True)
    assert result["config_path"] == str(tmp_path / ".mcp.json")
    assert json.loads((tmp_path / ".mcp.json").read_text(encoding="utf-8"))["mcpServers"]["sourceledger"]["type"] == "stdio"


def test_reject_invalid_server_name(tmp_path):
    with pytest.raises(ValueError, match="Server name"):
        connect("codex", workspace_root=tmp_path, name='x]\nmalicious=1')


def test_preserves_virtual_environment_python_symlink(tmp_path):
    if os.name == "nt":
        pytest.skip("Windows virtual environments use a regular executable")
    target = tmp_path / "global-python"
    target.touch()
    venv_python = tmp_path / "venv" / "bin" / "python"
    venv_python.parent.mkdir(parents=True)
    venv_python.symlink_to(target)
    assert server_spec(tmp_path, python=venv_python)["command"] == str(venv_python)


def test_connection_cli_generates_a_parseable_local_server(tmp_path, capsys):
    from su_crawler.cli import main

    assert main(["connect", "--client", "codex", "--workspace-root", str(tmp_path)]) == 0
    result = json.loads(capsys.readouterr().out)
    spec = tomllib.loads(Path(result["snippet_path"]).read_text(encoding="utf-8"))["mcp_servers"]["sourceledger"]
    assert spec["args"] == ["-m", "su_crawler", "serve-assistant", "--workspace-root", str(tmp_path)]
    assert main(["connect", "--client", "codex", "--workspace-root", str(tmp_path),
                 "--config-file", str(tmp_path / "unused.toml")]) == 2
    assert "requires --install" in capsys.readouterr().err


def test_assistant_cli_inspects_queue_without_starting_worker(tmp_path, capsys):
    from su_crawler.cli import main
    from su_crawler.assistant_runtime import submit_job

    workspace = ["--workspace-root", str(tmp_path)]
    assert main(["assistant", "status", *workspace]) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "stopped"
    job = submit_job(tmp_path, "discover", {"source_id": "fixture"})
    assert main(["assistant", "jobs", *workspace, "--limit", "1"]) == 0
    assert json.loads(capsys.readouterr().out)["jobs"][0]["id"] == job["id"]
    assert main(["assistant", "job", *workspace, "--job-id", job["id"]]) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "queued"
    assert main(["assistant", "resume", *workspace, "--job-id", job["id"]]) == 2
    capsys.readouterr()
    assert main(["assistant", "job", *workspace]) == 2
    assert "--job-id is required" in capsys.readouterr().err
    assert main(["assistant", "status", *workspace, "--job-id", job["id"]]) == 2
    assert "only available" in capsys.readouterr().err


def test_configuration_lock_symlink_is_rejected_before_touching_target(tmp_path):
    config = tmp_path / "config.json"
    target = tmp_path / "untouched.txt"
    target.write_bytes(b"original")
    try:
        config.with_name("config.json.lock").symlink_to(target)
    except OSError:
        pytest.skip("file symbolic links unavailable")
    with pytest.raises(ValueError, match="lock"):
        install_config("claude-desktop", config, "sourceledger", server_spec(tmp_path))
    assert target.read_bytes() == b"original"
    assert not config.exists()
