#!/usr/bin/env bash
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_PY="$HOME/.engram/venv/bin/python3"
if [ ! -x "$VENV_PY" ]; then
  echo "ENGRAM: venv python not found at $VENV_PY" >&2
  echo "  Run Phase 0 setup:" >&2
  echo "    python3 -m venv ~/.engram/venv" >&2
  echo "    source ~/.engram/venv/bin/activate" >&2
  echo "    pip install -r requirements/requirements.txt" >&2
  exit 1
fi
exec "$VENV_PY" "$SCRIPT_DIR/server.py" "$@"
