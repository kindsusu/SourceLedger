#!/usr/bin/env sh
set -u
export PYTHONUTF8=1

ROOT=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd) || exit 1
PYTHON="$ROOT/.venv/bin/python"
if [ ! -x "$PYTHON" ]; then
    echo "Error: SourceLedger is not installed. Run bash setup.sh first." >&2
    exit 2
fi
exec "$PYTHON" "$ROOT/scripts/launch.py" "$@"
