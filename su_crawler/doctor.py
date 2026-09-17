"""Read-only backend diagnostics.  This module never installs or connects tools."""
from __future__ import annotations

import importlib.util
import shutil
import os
from pathlib import Path


def _system_chrome() -> Path | None:
    candidates = [shutil.which("chrome"), shutil.which("msedge")]
    for root_name in ("PROGRAMFILES", "PROGRAMFILES(X86)", "LOCALAPPDATA"):
        root = os.environ.get(root_name)
        if root:
            candidates.extend(
                [
                    str(Path(root) / "Google" / "Chrome" / "Application" / "chrome.exe"),
                    str(Path(root) / "Microsoft" / "Edge" / "Application" / "msedge.exe"),
                ]
            )
    for candidate in candidates:
        if candidate and Path(candidate).is_file():
            return Path(candidate)
    return None


def _playwright_browser_ready() -> tuple[bool, str]:
    if importlib.util.find_spec("playwright") is None:
        return False, "Python package is not installed"
    try:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as pw:
            executable = Path(pw.chromium.executable_path)
            if executable.is_file():
                return True, f"Chromium executable found: {executable.name}"
            system_browser = _system_chrome()
            if system_browser:
                return True, f"System browser executable found: {system_browser.name}"
            return False, "Package is installed but no Chromium executable was found"
    except Exception as exc:
        return False, f"Package is installed but its runtime could not be checked: {exc}"


def doctor() -> list[dict]:
    """Report installed and runnable states without changing system state."""
    browser_ready, browser_reason = _playwright_browser_ready()
    crawl4ai_installed = importlib.util.find_spec("crawl4ai") is not None
    httpx_installed = importlib.util.find_spec("httpx") is not None
    agent_reach_command = shutil.which("agent-reach") or shutil.which("agent_reach")
    return [
        {"backend": "file", "status": "available", "installed": True, "connected": True, "reason": "Python standard file reading"},
        {
            "backend": "http",
            "status": "available" if httpx_installed else "unavailable",
            "installed": httpx_installed,
            "connected": httpx_installed,
            "reason": "httpx available locally" if httpx_installed else "Required httpx package is not installed",
        },
        {
            "backend": "playwright",
            "status": "available" if browser_ready else "unavailable",
            "installed": importlib.util.find_spec("playwright") is not None,
            "connected": browser_ready,
            "reason": browser_reason,
        },
        {
            "backend": "crawl4ai",
            "status": "installed" if crawl4ai_installed else "unavailable",
            "installed": crawl4ai_installed,
            "connected": False,
            "reason": "Optional package is installed; browser runtime connectivity is checked at collection time" if crawl4ai_installed else "Optional package is not installed",
        },
        {
            "backend": "agent_reach",
            "status": "available" if agent_reach_command else "unavailable",
            "installed": bool(agent_reach_command),
            "connected": False,
            "reason": "CLI found, but external reader connections are not used automatically" if agent_reach_command else "Optional CLI is not installed and will not be installed automatically",
        },
        {
            "backend": "ego_lite",
            "status": "unsupported",
            "installed": False,
            "connected": False,
            "reason": "ego-lite depends on a macOS app path and is not supported in this Windows runtime",
        },
    ]
