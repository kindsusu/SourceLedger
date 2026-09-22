"""Read-only backend diagnostics.  This module never installs or connects tools."""
from __future__ import annotations

import importlib.util
import shutil

from .browser_runtime import select_browser_runtime

def _playwright_browser_ready() -> tuple[bool, str]:
    if importlib.util.find_spec("playwright") is None:
        return False, "Python package is not installed"
    try:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as pw:
            runtime = select_browser_runtime(pw.chromium)
            if runtime:
                return True, f"{runtime.name} executable found: {runtime.executable.name}"
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
            "reason": "ego-lite is a design reference; no runtime adapter is integrated",
        },
    ]
