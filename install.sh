#!/usr/bin/env bash
# ENGRAM Alpha Install
#
# Sets up a fresh ENGRAM installation: copies skills and hooks, creates the
# seed graph, resolves install-time template placeholders, and arms the
# first-run marker so the agent's next Claude Code session runs the
# engram-first-session cold-start dialogue.
#
# Usage:
#   ./install.sh            # install
#   ./install.sh --force    # overwrite existing install (backs up first)
#   ./install.sh --help

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$SCRIPT_DIR"
ALPHA_DIR="$SCRIPT_DIR"

ENGRAM_HOME="${ENGRAM_HOME:-$HOME/.engram}"
CLAUDE_HOME="${CLAUDE_HOME:-$HOME/.claude}"

FORCE=0
for arg in "$@"; do
  case "$arg" in
    --force) FORCE=1 ;;
    --help|-h)
      echo "Usage: ./install.sh [--force]"
      echo ""
      echo "Environment overrides:"
      echo "  ENGRAM_HOME   target for ENGRAM data (default: \$HOME/.engram)"
      echo "  CLAUDE_HOME   target for Claude config (default: \$HOME/.claude)"
      echo ""
      echo "  --force: overwrites existing installation after backing it up."
      echo "           Use only for testing. Production users should uninstall first."
      exit 0
      ;;
    *) echo "Unknown arg: $arg (use --help)"; exit 1 ;;
  esac
done

echo "== ENGRAM Alpha Install =="
echo "  Repo:    $REPO_ROOT"
echo "  ENGRAM:  $ENGRAM_HOME"
echo "  Claude:  $CLAUDE_HOME"
echo ""

# -------- Step 1: Preflight checks --------

echo "== Step 1: Dependencies =="

command -v python3 >/dev/null || { echo "ERROR: python3 not found"; exit 1; }
command -v git >/dev/null || { echo "ERROR: git not found"; exit 1; }

MISSING=$(python3 -c '
import importlib.util as u
want = ["fastmcp", "sentence_transformers"]
missing = [m for m in want if u.find_spec(m) is None]
print(",".join(missing))
')
if [ -n "$MISSING" ]; then
  echo "ERROR: missing Python packages: $MISSING"
  echo "Install with: pip install fastmcp sentence-transformers"
  exit 1
fi

# Resolve an absolute path to the interpreter that just passed preflight.
# Baked into generated configs ($CLAUDE_HOME/.mcp.json, settings.json hooks,
# start-engram-daemon.sh) so venv installs work without needing the venv
# activated every time Claude Code launches.
PYTHON_BIN="$(python3 -c 'import sys; print(sys.executable)')"

echo "  python3 ok ($PYTHON_BIN)"
echo "  git ok"
echo "  Python deps ok (fastmcp, sentence-transformers)"
echo ""

# -------- Step 2: Idempotency --------

echo "== Step 2: Existing-install check =="

if [ -f "$ENGRAM_HOME/knowledge.db" ] && [ "$FORCE" -eq 0 ]; then
  echo "ERROR: existing install detected ($ENGRAM_HOME/knowledge.db)."
  echo "Refuse to overwrite. Use --force to reinstall (existing graph will be backed up)."
  exit 1
fi

if [ "$FORCE" -eq 1 ] && [ -d "$ENGRAM_HOME" ]; then
  BACKUP="$ENGRAM_HOME.backup.$(date +%Y%m%d_%H%M%S)"
  echo "  --force: moving $ENGRAM_HOME → $BACKUP"
  mv "$ENGRAM_HOME" "$BACKUP"
fi
echo "  ok"
echo ""

# -------- Step 3: Directories --------

echo "== Step 3: Creating directories =="

mkdir -p "$ENGRAM_HOME/hooks"
mkdir -p "$ENGRAM_HOME/diary"
mkdir -p "$CLAUDE_HOME/skills"
echo "  $ENGRAM_HOME"
echo "  $CLAUDE_HOME"
echo ""

# -------- Step 4: Skills, hooks, MCP config --------

echo "== Step 4: Copying files =="

if [ -d "$ALPHA_DIR/skills" ] && [ -n "$(ls -A "$ALPHA_DIR/skills" 2>/dev/null || true)" ]; then
  cp -r "$ALPHA_DIR/skills/"* "$CLAUDE_HOME/skills/"
  echo "  skills → $CLAUDE_HOME/skills/"
fi

if [ -d "$ALPHA_DIR/hooks" ] && [ -n "$(ls -A "$ALPHA_DIR/hooks" 2>/dev/null || true)" ]; then
  cp -r "$ALPHA_DIR/hooks/"* "$ENGRAM_HOME/hooks/"
  chmod +x "$ENGRAM_HOME/hooks/"*.py 2>/dev/null || true
  chmod +x "$ENGRAM_HOME/hooks/"*.sh 2>/dev/null || true
  # Bake the resolved interpreter path into the daemon-launch script so it
  # doesn't depend on `python3` being on PATH at SessionStart-hook time.
  if [ -f "$ENGRAM_HOME/hooks/start-engram-daemon.sh" ]; then
    sed -i "s|{{PYTHON}}|$PYTHON_BIN|g" "$ENGRAM_HOME/hooks/start-engram-daemon.sh"
  fi
  echo "  hooks → $ENGRAM_HOME/hooks/"
fi

if [ -f "$ALPHA_DIR/mcp.json.template" ]; then
  if [ -f "$CLAUDE_HOME/.mcp.json" ] && [ "$FORCE" -eq 0 ]; then
    echo "  WARN: $CLAUDE_HOME/.mcp.json exists; not overwriting (preserve user config)"
  else
    sed -e "s|{{ENGRAM_HOME}}|$ENGRAM_HOME|g" \
        -e "s|{{PYTHON}}|$PYTHON_BIN|g" \
        "$ALPHA_DIR/mcp.json.template" > "$CLAUDE_HOME/.mcp.json"
    echo "  .mcp.json → $CLAUDE_HOME/.mcp.json"
  fi
fi

# Runtime snapshot: server + confidence module + protocol doc copied into
# $ENGRAM_HOME so the install is self-contained and version-locked against
# ongoing main-branch development.
for runtime_file in server.py engram_confidence.py engram_client.py SKILL.md viz_server.py; do
  if [ -f "$ALPHA_DIR/$runtime_file" ]; then
    cp "$ALPHA_DIR/$runtime_file" "$ENGRAM_HOME/$runtime_file"
    case "$runtime_file" in
      *.py) chmod +x "$ENGRAM_HOME/$runtime_file" ;;
    esac
    echo "  $runtime_file → $ENGRAM_HOME/$runtime_file"
  fi
done
echo ""

# -------- Step 5: Seed graph + template substitution --------

echo "== Step 5: Seed graph + template resolution =="

ALPHA_DIR="$ALPHA_DIR" \
ALPHA_TEMPLATES_DIR="$ALPHA_DIR/templates" \
ENGRAM_DATA_DIR="$ENGRAM_HOME" \
CLAUDE_HOME="$CLAUDE_HOME" \
PYTHON_BIN="$PYTHON_BIN" \
FORCE="$FORCE" \
  "$PYTHON_BIN" "$ALPHA_DIR/bootstrap.py"
echo ""

# -------- Step 6: First-run marker --------

echo "== Step 6: Arming first-session marker =="

cat > "$ENGRAM_HOME/first-run-pending" <<EOF
{
  "created": "$(date +%Y-%m-%dT%H:%M:%S%z)",
  "install_path": "$REPO_ROOT"
}
EOF
echo "  $ENGRAM_HOME/first-run-pending"
echo ""

# -------- Step 7: Git init --------

echo "== Step 7: Git =="

if [ ! -d "$ENGRAM_HOME/.git" ]; then
  (cd "$ENGRAM_HOME" && git init -q && git add -A && git commit -q -m "ENGRAM alpha install")
  echo "  initialized $ENGRAM_HOME/.git"
else
  echo "  .git exists, skipping init"
fi
echo ""

# -------- Done --------

echo "== Install complete =="
echo ""
echo "Next steps:"
echo "  1. Start a new Claude Code session"
echo "  2. The agent will detect $ENGRAM_HOME/first-run-pending"
echo "     and run the engram-first-session cold-start dialogue"
echo "  3. After dialogue completes, {{AGENT_NAME}} and {{USER_NAME}} will"
echo "     be resolved in $CLAUDE_HOME/CLAUDE.md and $ENGRAM_HOME/warm-briefing.md"
echo ""
