#!/usr/bin/env bash
# nuke-vcd.sh — wrapper around nuke_vcd_tenant.py.
# Picks up nuke_vcd.conf next to the script, makes a venv if the system Python
# doesn't have requests/urllib3, then runs Python with whatever args you passed.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

PY_SCRIPT="${SCRIPT_DIR}/nuke_vcd_tenant.py"
VENV_DIR="${SCRIPT_DIR}/.venv"
CONFIG_FILE="${SCRIPT_DIR}/nuke_vcd.conf"
CONFIG_EXAMPLE="${SCRIPT_DIR}/nuke_vcd.conf.example"

# Basic sanity.
if [ ! -f "$PY_SCRIPT" ]; then
    echo "[X] $PY_SCRIPT not found" >&2
    exit 1
fi

if [ ! -f "$CONFIG_FILE" ]; then
    if [ -f "$CONFIG_EXAMPLE" ]; then
        echo "[!] $CONFIG_FILE is missing — copy the template and fill it in:" >&2
        echo "    cp '$CONFIG_EXAMPLE' '$CONFIG_FILE' && chmod 600 '$CONFIG_FILE'" >&2
        echo "    \$EDITOR '$CONFIG_FILE'" >&2
    else
        echo "[X] No $CONFIG_FILE and no .example template either" >&2
    fi
    exit 2
fi

# The config has a password. Yell if the perms are wider than they should be.
PERM=$(stat -f '%Lp' "$CONFIG_FILE" 2>/dev/null || stat -c '%a' "$CONFIG_FILE" 2>/dev/null || echo '')
if [ "$PERM" != "600" ] && [ -n "$PERM" ]; then
    echo "[!] $CONFIG_FILE perms are $PERM. 600 is what you want." >&2
fi

PYTHON_BIN="${PYTHON_BIN:-python3}"
if ! command -v "$PYTHON_BIN" >/dev/null 2>&1; then
    echo "[X] $PYTHON_BIN not in PATH" >&2
    exit 3
fi

# Use a venv only if the system Python is missing requests/urllib3. Saves time on
# machines where they're already installed (most macOS dev boxes, fwiw).
USE_VENV="${USE_VENV:-auto}"
if [ "$USE_VENV" = "auto" ]; then
    if "$PYTHON_BIN" -c 'import requests, urllib3' 2>/dev/null; then
        USE_VENV=0
    else
        USE_VENV=1
    fi
fi

if [ "$USE_VENV" = "1" ]; then
    if [ ! -d "$VENV_DIR" ]; then
        echo "[*] Creating venv in $VENV_DIR..."
        "$PYTHON_BIN" -m venv "$VENV_DIR"
        "$VENV_DIR/bin/pip" install --quiet --upgrade pip
        "$VENV_DIR/bin/pip" install --quiet requests urllib3
    fi
    PYTHON_BIN="$VENV_DIR/bin/python3"
fi

export VCD_CONFIG="$CONFIG_FILE"
exec "$PYTHON_BIN" "$PY_SCRIPT" "$@"
