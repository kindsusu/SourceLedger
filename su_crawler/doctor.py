"""Read-only backend diagnostics.  This module never installs or connects tools."""
from __future__ import annotations

import importlib.util
import shutil

from .browser_runtime import browser_headless, select_browser_runtime

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
    except Exception:
        return False, "Package is installed but its runtime could not be checked"


def _probe_browser_launch(*, headless: bool, timeout_ms: int = 5000) -> tuple[bool, str]:
    """Optional bounded smoke check; never include local exception details."""
    try:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as pw:
            runtime = select_browser_runtime(pw.chromium)
            if runtime is None:
                return False, "No Chromium executable was found"
            browser = pw.chromium.launch(headless=headless, timeout=timeout_ms, **runtime.launch_options())
            browser.close()
            return True, "Chromium launched successfully"
    except Exception:
        return False, "Chromium executable was found but could not be launched in the selected display mode"


def doctor(*, verify_browser_launch: bool = False, browser_display_mode: str | None = None) -> list[dict]:
    """Report installed state; launch is verified only when explicitly requested."""
    if browser_display_mode not in {None, "visible", "headless"}:
        raise ValueError("browser_display_mode must be visible or headless")
    browser_ready, browser_reason = _playwright_browser_ready()
    launch_verified: bool | None = None
    if browser_ready and verify_browser_launch:
        headless = browser_headless() if browser_display_mode is None else browser_display_mode == "headless"
        launch_verified, browser_reason = _probe_browser_launch(headless=headless)
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
            "status": "available" if browser_ready and launch_verified is not False else "unavailable",
            "installed": importlib.util.find_spec("playwright") is not None,
            "connected": launch_verified is True,
            "executable_found": browser_ready,
            "launch_verified": launch_verified,
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
