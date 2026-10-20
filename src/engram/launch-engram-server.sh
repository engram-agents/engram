#!/usr/bin/env bash
# A RELATIVE ENGRAM_HOME is refused (alpha #3863): it would resolve against the calling process's cwd, a
# different home for every process. Unset or empty means unset; a leading ~ is expanded against $HOME and
# re-exported (~user is not expanded: it is not absolute, so it is refused). Checked before anything else
# runs; `return` covers a script that is sourced, `exit` one that is executed.
if [ -n "${ENGRAM_HOME-}" ]; then
    _eh_raw="$ENGRAM_HOME"
    case "$ENGRAM_HOME" in
        "~") ENGRAM_HOME="${HOME-}"; export ENGRAM_HOME ;;
        "~/"*) ENGRAM_HOME="${HOME-}/${ENGRAM_HOME#"~/"}"; export ENGRAM_HOME ;;
    esac
    case "$ENGRAM_HOME" in
        /*) ;;
        *)
            printf "ENGRAM_HOME must be an absolute path, got '%s'\n" "$_eh_raw" >&2
            unset _eh_raw
            return 2 2>/dev/null || exit 2 ;;
    esac
    unset _eh_raw
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# The venv is a plugin install artifact at ~/.engram/venv; ENGRAM_HOME does not move it (README-AGENT.md:187).
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
