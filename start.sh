#!/usr/bin/env bash
# ============================================================
# SnapStudy launcher for macOS / Linux.
# Prepares the whole environment, then starts the service:
#   1) locate Python  2) create .venv  3) install deps
#   4) create config.yaml from template  5) start service
# Safe to run repeatedly: finished steps are skipped.
# ============================================================
set -u

cd "$(dirname "$0")" || exit 1
PROJECT_DIR="$(pwd)"

echo "============================================"
echo "     SnapStudy v1.0 - Setup & Launch"
echo "============================================"
echo

# ------------------------------------------------------------
# 1/5  Locate a usable Python interpreter (3.10+)
# ------------------------------------------------------------
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

echo "[1/5] Interpreter: $PYTHON_BIN ($($PYTHON_BIN --version 2>&1))"

# ------------------------------------------------------------
# 2/5  Prepare a project-local virtual environment
# ------------------------------------------------------------
VENV_DIR="$PROJECT_DIR/.venv"
VENV_PY="$VENV_DIR/bin/python"

if [ -x "$VENV_PY" ]; then
    echo "[2/5] Using existing virtual environment: $VENV_DIR"
else
    echo "[2/5] Creating virtual environment (first run only)..."
    if "$PYTHON_BIN" -m venv "$VENV_DIR" >/dev/null 2>&1 && [ -x "$VENV_PY" ]; then
        echo "[2/5] Virtual environment ready: $VENV_DIR"
    else
        echo "[2/5] [WARN] Could not create a virtual environment."
        echo "      Falling back to the system interpreter."
        VENV_PY=""
    fi
fi

if [ -n "$VENV_PY" ]; then
    PY="$VENV_PY"
else
    PY="$PYTHON_BIN"
fi

# ------------------------------------------------------------
# 3/5  Install / verify dependencies
# ------------------------------------------------------------
if "$PY" -c "import fastapi, uvicorn, mss, PIL, httpx, yaml, websockets" >/dev/null 2>&1; then
    echo "[3/5] Dependencies OK."
else
    echo "[3/5] Installing dependencies (first run only, may take a while)..."
    "$PY" -m pip install --upgrade pip >/dev/null 2>&1

    if ! "$PY" -m pip install -r "$PROJECT_DIR/requirements.txt" \
            -i https://pypi.tuna.tsinghua.edu.cn/simple; then
        echo "[3/5] Mirror failed, retrying with the default index..."
        if ! "$PY" -m pip install -r "$PROJECT_DIR/requirements.txt"; then
            echo "[3/5] [ERROR] Failed to install dependencies, please check your network."
            exit 1
        fi
    fi
    echo "[3/5] Dependencies installed."
fi

# ------------------------------------------------------------
# 4/5  Create config.yaml from the template when missing
# ------------------------------------------------------------
if [ ! -f "$PROJECT_DIR/config.yaml" ]; then
    echo "[4/5] config.yaml not found - creating it from the template..."
    cp "$PROJECT_DIR/config.example.yaml" "$PROJECT_DIR/config.yaml"
fi

if grep -q "your-api-key-here" "$PROJECT_DIR/config.yaml" 2>/dev/null; then
    echo "[4/5] [ACTION REQUIRED] config.yaml still has the placeholder api_key."
    echo "      Open config.yaml and fill in your own key, or set it later"
    echo "      from the web UI on your phone."
fi

# ------------------------------------------------------------
# 5/5  Launch the service
# ------------------------------------------------------------
echo "[5/5] Starting service..."
echo
echo "Open the URL printed below in your phone browser:"
echo

exec "$PY" "$PROJECT_DIR/main.py"
