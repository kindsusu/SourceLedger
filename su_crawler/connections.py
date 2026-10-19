"""Generate portable-client connection settings without replacing user settings."""
from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import platform
import re
import stat
import sys
import tomllib
import uuid

from .research import _locked


CLIENTS = ("codex", "claude-code", "claude-desktop")


def server_spec(workspace_root: str | Path, *, python: str | Path | None = None) -> dict:
    root = Path(workspace_root).expanduser().resolve()
    # Preserve the venv executable path: resolving its POSIX symlink would
    # select the global interpreter and lose the installed dependencies.
    executable = Path(python or sys.executable).expanduser().absolute()
    if not executable.is_file():
        raise ValueError("Python executable does not exist")
    return {
        "command": str(executable),
        "args": ["-m", "su_crawler", "serve-assistant", "--workspace-root", str(root)],
        "env": {"PYTHONUTF8": "1"},
    }


def _toml(name: str, spec: dict) -> str:
    # JSON basic strings are compatible with TOML for these string/list values.
    encode = lambda value: json.dumps(value, ensure_ascii=False)
    return (f"[mcp_servers.{name}]\ncommand = {encode(spec['command'])}\n"
            f"args = {encode(spec['args'])}\n"
            f"env = {{ PYTHONUTF8 = \"1\" }}\n")


def _publish(path: Path, content: str) -> None:
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        mode = stat.S_IMODE(path.stat().st_mode) if path.exists() else 0o600
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.chmod(mode)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _default_config(client: str, *, project_dir: Path) -> Path:
    if client == "codex":
        return Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex"))) / "config.toml"
    if client == "claude-code":
        return project_dir / ".mcp.json"
    system = platform.system()
    if system == "Windows":
        appdata = os.environ.get("APPDATA")
        if not appdata:
            raise ValueError("APPDATA is unavailable; use --config-file for Claude Desktop")
        return Path(appdata) / "Claude" / "claude_desktop_config.json"
    if system == "Darwin":
        return Path.home() / "Library/Application Support/Claude/claude_desktop_config.json"
    raise ValueError("Specify --config-file for your Claude Desktop installation on this OS")


def install_config(client: str, path: Path, name: str, spec: dict) -> dict:
    """Preserve unrelated settings; refuse to replace an existing different server."""
    path = path.expanduser().absolute()
    if path.is_symlink():
        raise ValueError("Client configuration must not be a symbolic link")
    if path.with_name(path.name + ".lock").is_symlink():
        raise ValueError("Client configuration lock must not be a symbolic link")
    path.parent.mkdir(parents=True, exist_ok=True)
    with _locked(path):
        original = path.read_bytes() if path.exists() else None
        text = original.decode("utf-8-sig") if original is not None else ""
        document = tomllib.loads(text) if client == "codex" else json.loads(text or "{}")
        if not isinstance(document, dict):
            raise ValueError("Client configuration must be an object")
        key = "mcp_servers" if client == "codex" else "mcpServers"
        servers = document.get(key, {})
        if not isinstance(servers, dict):
            raise ValueError(f"{key} must be an object")
        entry = dict(spec, type="stdio") if client == "claude-code" else spec
        if name in servers:
            if servers[name] == entry:
                return {"status": "already_configured", "config_path": str(path), "backup_path": None}
            raise ValueError(f"Server '{name}' already exists with different settings; choose another --name")
        if client == "codex":
            content = text.rstrip() + ("\n\n" if text.strip() else "") + _toml(name, spec)
            # Inline/sealed TOML tables cannot always be extended by appending.
            # Fail before touching the existing configuration in that case.
            parsed = tomllib.loads(content)
            if parsed[key][name] != spec:
                raise ValueError("Could not safely add the Codex server; use the generated snippet")
        else:
            document[key] = {**servers, name: entry}
            content = json.dumps(document, ensure_ascii=False, indent=2) + "\n"
        backup = None
        if original is not None:
            backup = path.with_name(path.name + f".sourceledger-{uuid.uuid4().hex[:12]}.bak")
            descriptor = os.open(backup, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(original)
        _publish(path, content)
    return {"status": "configured", "config_path": str(path), "backup_path": str(backup) if backup else None}


def connect(client: str, *, workspace_root: str | Path, output_dir: str | Path | None = None,
            name: str = "sourceledger", install: bool = False,
            config_file: str | Path | None = None, project_dir: str | Path | None = None) -> dict:
    if client not in CLIENTS:
        raise ValueError("Unknown client")
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", name):
        raise ValueError("Server name must be 1-64 letters, digits, underscores, or hyphens")
    if config_file and not install:
        raise ValueError("--config-file requires --install")
    if importlib.util.find_spec("mcp") is None:
        raise ValueError("MCP is not installed. Run setup.cmd --mcp or bash setup.sh --mcp first")
    root = Path(workspace_root).expanduser().resolve()
    project = Path(project_dir or Path.cwd()).expanduser().resolve()
    output = Path(output_dir).expanduser().resolve() if output_dir else root / "connections"
    spec = server_spec(root)
    output.mkdir(parents=True, exist_ok=True)
    suffix = "toml" if client == "codex" else "json"
    snippet = output / f"{client}.{name}.{suffix}"
    if snippet.is_symlink():
        raise ValueError("Connection snippet must not be a symbolic link")
    entry = dict(spec, type="stdio") if client == "claude-code" else spec
    content = _toml(name, spec) if client == "codex" else json.dumps({"mcpServers": {name: entry}}, ensure_ascii=False, indent=2) + "\n"
    if snippet.exists() and snippet.read_text(encoding="utf-8") != content:
        raise ValueError("Connection snippet already exists with different settings; choose another output directory or name")
    if not snippet.exists():
        # Creation refuses overwrite even if another process created it meanwhile.
        with snippet.open("x", encoding="utf-8", newline="\n") as stream:
            stream.write(content)
    result = {"status": "generated", "client": client, "name": name,
              "workspace_root": str(root), "snippet_path": str(snippet), "server": spec,
              "next_steps": [
                  "Repeat this connect command with --install, retaining the same workspace root and server name, or merge the snippet into the client's MCP settings.",
                  "Run source-ledger assistant start --workspace-root <workspace_root> in a normal terminal outside the assistant client.",
                  "Restart or refresh the client connection, then ask SourceLedger to initialize your industry, product, and market.",
              ]}
    if install:
        target = Path(config_file) if config_file else _default_config(client, project_dir=project)
        result.update(install_config(client, target, name, spec))
        result["next_steps"].pop(0)
        if client == "claude-code":
            result["next_steps"].insert(0, "Open Claude Code in the project directory and approve its project MCP configuration when prompted.")
    return result
