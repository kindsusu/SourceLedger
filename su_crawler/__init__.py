"""Evidence-first price collection."""

from .browser_runtime import use_environment_browsers

__version__ = "1.0.1"

# Every entry point (CLI, UI, worker, MCP server) imports this package before Playwright starts.
use_environment_browsers()
