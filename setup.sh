#!/usr/bin/env sh
set -u
export PYTHONUTF8=1

ROOT=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd) || exit 1

for candidate in python3 python; do
    if command -v "$candidate" >/dev/null 2>&1 &&
       "$candidate" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)' >/dev/null 2>&1; then
        exec "$candidate" "$ROOT/scripts/setup.py" "$@"
    fi
done

echo "Error: Python 3.11 or newer was not found. Install it, then run bash setup.sh again." >&2
exit 2
