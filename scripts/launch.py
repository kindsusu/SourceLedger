#!/usr/bin/env python3
"""Launch SourceLedger from its repository-local environment."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from typing import Sequence


def main(argv: Sequence[str] | None = None) -> int:
    root = Path(__file__).resolve().parent.parent
    os.chdir(root)
    arguments = list(sys.argv[1:] if argv is None else argv)
    if not arguments:
        arguments = ["ui"]
    return subprocess.call([sys.executable, "-m", "su_crawler.cli", *arguments], cwd=root)


if __name__ == "__main__":
    raise SystemExit(main())
