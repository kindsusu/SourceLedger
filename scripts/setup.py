#!/usr/bin/env python3
"""Create SourceLedger's repository-local environment and install requested features."""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path
from typing import Callable, Sequence


MIN_PYTHON = (3, 11)
Runner = Callable[..., subprocess.CompletedProcess[str]]


def repo_root() -> Path:
    return Path(__file__).resolve().parent.parent


def venv_python(environment: Path) -> Path:
    return environment / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")


def _run(command: Sequence[str], *, cwd: Path, capture_output: bool = False) -> subprocess.CompletedProcess[str]:
    environment = os.environ.copy()
    environment["PYTHONUTF8"] = "1"
    return subprocess.run(
        list(command), cwd=cwd, check=True, text=True,
        capture_output=capture_output, encoding="utf-8", errors="replace", env=environment,
    )


def validate_interpreter(python: Path, *, root: Path, runner: Runner = _run) -> None:
    if not python.is_file():
        raise RuntimeError(f"The environment exists but its Python executable is missing: {python}")
    probe = runner(
        [str(python), "-c", "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')"],
        cwd=root, capture_output=True,
    )
    value = (probe.stdout or "").strip()
    try:
        parts = value.split(".")
        if len(parts) < 2:
            raise ValueError
        version = tuple(int(part) for part in parts[:2])
    except ValueError as exc:
        raise RuntimeError(f"Could not read the environment's Python version: {value!r}") from exc
    if version < MIN_PYTHON:
        raise RuntimeError(
            f"The existing environment uses Python {value}; SourceLedger requires Python 3.11 or newer."
        )


def install(
    *, root: Path, browser: bool = False, mcp: bool = False, dev: bool = False,
    runner: Runner = _run,
) -> Path:
    environment = root / ".venv"
    python = venv_python(environment)
    if environment.exists():
        validate_interpreter(python, root=root, runner=runner)
    else:
        runner([sys.executable, "-m", "venv", str(environment)], cwd=root)
        validate_interpreter(python, root=root, runner=runner)

    extras = []
    if browser:
        extras.append("browser")
    if mcp:
        extras.append("mcp")
    if dev:
        extras.append("test")
    target = str(root) + (f"[{','.join(extras)}]" if extras else "")
    command = [str(python), "-m", "pip", "install"]
    constraints = root / "requirements" / "constraints.txt"
    if constraints.is_file():
        command.extend(["--constraint", str(constraints)])
    if dev:
        command.append("--editable")
    command.append(target)
    runner(command, cwd=root)
    if browser:
        runner([str(python), "-m", "playwright", "install", "chromium"], cwd=root)
    return python


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(
        description="Install SourceLedger into a repository-local .venv (Python 3.11+)."
    )
    result.add_argument("--browser", action="store_true", help="Install Playwright and its managed Chromium browser.")
    result.add_argument("--mcp", action="store_true", help="Install the optional MCP server dependency.")
    result.add_argument("--dev", action="store_true", help="Install test/development dependencies.")
    return result


def main(argv: Sequence[str] | None = None) -> int:
    args = parser().parse_args(argv)
    root = repo_root()
    if sys.version_info < MIN_PYTHON:
        print(
            f"Error: Python 3.11 or newer is required; this is {sys.version.split()[0]}.",
            file=sys.stderr,
        )
        return 2
    try:
        python = install(root=root, browser=args.browser, mcp=args.mcp, dev=args.dev)
    except (OSError, RuntimeError, subprocess.CalledProcessError) as exc:
        print(f"Setup failed: {exc}", file=sys.stderr)
        print(
            "The existing .venv was preserved. Fix the reported problem and run this setup command again. "
            "If the environment itself is invalid, rename it and retry.",
            file=sys.stderr,
        )
        return 1
    print(f"SourceLedger is ready: {python}")
    print("Run source-ledger.cmd on Windows or bash source-ledger.sh on macOS/Linux.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
