#!/usr/bin/env bash
# Thin wrapper around launcher.py for macOS / Linux.
# Every environment step lives in launcher.py, the single entry point.
set -u

cd "$(dirname "$0")" || exit 1

PYTHON_BIN=""
for candidate in python3.13 python3.12 python3.11 python3.10 python3 python; do
    if command -v "$candidate" >/dev/null 2>&1; then
        if "$candidate" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)' >/dev/null 2>&1; then
            PYTHON_BIN="$candidate"
            break
        fi
    fi
done

if [ -z "$PYTHON_BIN" ]; then
    echo "[ERROR] Python 3.10+ not found. Please install it first."
    echo "        macOS:  brew install python@3.12"
    echo "        Ubuntu: sudo apt install python3 python3-venv python3-pip"
    exit 1
fi

exec "$PYTHON_BIN" launcher.py
