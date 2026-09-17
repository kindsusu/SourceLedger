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
        return False, "Python 패키지가 설치되지 않았습니다"
    try:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as pw:
            executable = Path(pw.chromium.executable_path)
            if executable.is_file():
                return True, f"Chromium 실행 파일 확인: {executable.name}"
            system_browser = _system_chrome()
            if system_browser:
                return True, f"시스템 브라우저 실행 파일 확인: {system_browser.name}"
            return False, "패키지는 설치됐지만 Chromium 실행 파일이 없습니다"
    except Exception as exc:
        return False, f"패키지는 설치됐지만 실행 상태를 확인하지 못했습니다: {exc}"


def doctor() -> list[dict]:
    """Report installed and runnable states without changing system state."""
    browser_ready, browser_reason = _playwright_browser_ready()
    crawl4ai_installed = importlib.util.find_spec("crawl4ai") is not None
    httpx_installed = importlib.util.find_spec("httpx") is not None
    agent_reach_command = shutil.which("agent-reach") or shutil.which("agent_reach")
    return [
        {"backend": "file", "status": "available", "installed": True, "connected": True, "reason": "Python 표준 파일 읽기"},
        {
            "backend": "http",
            "status": "available" if httpx_installed else "unavailable",
            "installed": httpx_installed,
            "connected": httpx_installed,
            "reason": "httpx 로컬 실행" if httpx_installed else "필수 httpx 패키지가 설치되지 않았습니다",
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
            "reason": "선택 패키지는 설치됐지만 브라우저 런타임 연결은 실제 실행 시 확인합니다" if crawl4ai_installed else "선택 패키지가 설치되지 않았습니다",
        },
        {
            "backend": "agent_reach",
            "status": "available" if agent_reach_command else "unavailable",
            "installed": bool(agent_reach_command),
            "connected": False,
            "reason": "CLI는 확인했지만 외부 reader 연결은 자동 사용하지 않습니다" if agent_reach_command else "선택 CLI가 설치되지 않았으며 자동 설치하지 않습니다",
        },
        {
            "backend": "ego_lite",
            "status": "unsupported",
            "installed": False,
            "connected": False,
            "reason": "ego-lite는 macOS 앱 종속이며 현재 Windows 실행 환경을 지원하지 않습니다",
        },
    ]
