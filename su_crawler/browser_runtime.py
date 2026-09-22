"""Shared, read-only Playwright browser runtime selection."""
from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import platform
import shutil
from typing import Any, Mapping


@dataclass(frozen=True)
class BrowserRuntime:
    kind: str
    executable: Path
    name: str

    def launch_options(self) -> dict[str, str]:
        return {} if self.kind == "managed" else {"executable_path": str(self.executable)}


def browser_headless(environ: Mapping[str, str] | None = None) -> bool:
    """Return requested browser display mode; interactive/headful is the default."""
    raw = (os.environ if environ is None else environ).get("SOURCELEDGER_HEADLESS", "0").strip().casefold()
    if raw in {"1", "true", "yes", "on"}:
        return True
    if raw in {"0", "false", "no", "off", ""}:
        return False
    raise ValueError("SOURCELEDGER_HEADLESS must be 1 or 0")


def _system_browser_candidates(system: str | None = None, environ: Mapping[str, str] | None = None) -> list[tuple[str, Path]]:
    system = system or platform.system()
    environment = os.environ if environ is None else environ
    names = (
        "google-chrome", "google-chrome-stable", "chrome", "chromium", "chromium-browser",
        "microsoft-edge", "microsoft-edge-stable", "msedge",
    )
    candidates: list[tuple[str, Path]] = []
    for name in names:
        found = shutil.which(name)
        if found:
            candidates.append((name, Path(found)))
    if system == "Windows":
        for root_name in ("PROGRAMFILES", "PROGRAMFILES(X86)", "LOCALAPPDATA"):
            root = environment.get(root_name)
            if root:
                candidates.extend([
                    ("Google Chrome", Path(root) / "Google" / "Chrome" / "Application" / "chrome.exe"),
                    ("Microsoft Edge", Path(root) / "Microsoft" / "Edge" / "Application" / "msedge.exe"),
                ])
    elif system == "Darwin":
        candidates.extend([
            ("Google Chrome", Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")),
            ("Chromium", Path("/Applications/Chromium.app/Contents/MacOS/Chromium")),
            ("Microsoft Edge", Path("/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge")),
        ])
        home = Path(environment.get("HOME", "~")).expanduser()
        candidates.extend([
            ("Google Chrome", home / "Applications/Google Chrome.app/Contents/MacOS/Google Chrome"),
            ("Microsoft Edge", home / "Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge"),
        ])
    return candidates


def select_browser_runtime(chromium: Any, *, system: str | None = None,
                           environ: Mapping[str, str] | None = None) -> BrowserRuntime | None:
    """Prefer Playwright-managed Chromium, then an executable we can launch directly."""
    managed = Path(chromium.executable_path)
    if managed.is_file():
        return BrowserRuntime("managed", managed, "Playwright Chromium")
    seen: set[Path] = set()
    for name, candidate in _system_browser_candidates(system, environ):
        try:
            resolved = candidate.expanduser().resolve()
        except OSError:
            continue
        if resolved not in seen and resolved.is_file():
            return BrowserRuntime("system", resolved, name)
        seen.add(resolved)
    return None
