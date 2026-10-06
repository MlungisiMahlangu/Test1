#!/usr/bin/env bash
set -euo pipefail
cd -- "$(dirname -- "${BASH_SOURCE[0]}")"
command -v git >/dev/null || { printf 'Git is required. Install git and try again.\n' >&2; exit 1; }
PYTHON="${PYTHON:-python3}"
command -v "$PYTHON" >/dev/null || { printf 'Python 3.10+ is required.\n' >&2; exit 1; }
"$PYTHON" -c 'import sys; assert sys.version_info >= (3, 10), "Python 3.10+ is required"'
if [[ ! -x .venv/bin/python ]]; then
  "$PYTHON" -m venv .venv || { printf 'Install Python venv support (Ubuntu: sudo apt install python3-venv).\n' >&2; exit 1; }
fi
if ! .venv/bin/python -c 'from importlib.metadata import version; assert version("Flask") == "3.1.2"' >/dev/null 2>&1; then
  .venv/bin/python -m pip install --disable-pip-version-check -r requirements.txt
fi
printf '\nRAT — Repository intelligence\nOpen http://localhost:%s\nPress Ctrl+C to stop.\n\n' "${PORT:-8000}"
exec .venv/bin/python app.py
