#!/usr/bin/env bash
# ENGRAM Alpha Install — Gemini CLI surface
#
# Installs ENGRAM as a Gemini CLI extension with its own isolated data
# directory (default: ~/.engram-gemini/). The Gemini agent gets its own
# knowledge graph, warm briefing, and identity — independent from any
# Claude Code install on the same machine.
#
# Usage:
#   ./install-gemini.sh                    # install (default ENGRAM_HOME=~/.engram-gemini)
#   ./install-gemini.sh --force            # archive existing install and start fresh
#                                          #   (knowledge graph + history are moved to a
#                                          #   timestamped backup dir; NOT preserved)
#   ENGRAM_HOME=~/.engram-other ./install-gemini.sh   # custom data dir
#   ./install-gemini.sh --help
#
# To UPGRADE an existing install while preserving the knowledge graph,
# history, diary, and sessions, use:  tools/deploy.sh
# (install-gemini.sh --force is for fresh-start, not upgrade.)

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ALPHA_DIR="$SCRIPT_DIR"

ENGRAM_HOME="${ENGRAM_HOME:-$HOME/.engram-gemini}"
GEMINI_HOME="${GEMINI_HOME:-$HOME/.gemini}"
GEMINI_EXT_DIR="${GEMINI_EXT_DIR:-$GEMINI_HOME/extensions/engram}"

FORCE=0
NO_CRON=0
for arg in "$@"; do
  case "$arg" in
    --force) FORCE=1 ;;
    --no-cron) NO_CRON=1 ;;
    --help|-h)
      echo "Usage: ./install-gemini.sh [--force] [--no-cron]"
      echo ""
      echo "Environment overrides:"
      echo "  ENGRAM_HOME        target for ENGRAM data (default: \$HOME/.engram-gemini)"
      echo "  GEMINI_HOME        Gemini CLI config root (default: \$HOME/.gemini)"
      echo "  GEMINI_EXT_DIR     extension directory (default: \$GEMINI_HOME/extensions/engram)"
      echo "  AGENT_NAME         cron identity for the installed agent (default: derived from username)"
      echo "  COUNTERPART_NAME   peer agent name for inter-agent dispatcher (optional)"
      echo ""
      echo "  --force: archive existing installation and start fresh."
      echo "           Moves \$ENGRAM_HOME (graph, history, diary, sessions, logs)"
      echo "           to a timestamped backup directory. The new install starts"
      echo "           with an empty knowledge graph — old data is NOT migrated in."
      echo "           To UPGRADE an existing install while preserving data, use:"
      echo "             tools/deploy.sh"
      echo "  --no-cron: skip Step 9 (cron schedule install). Useful for headless"
      echo "             servers, CI, or users who manage their crontab separately."
      exit 0
      ;;
    *) echo "Unknown arg: $arg (use --help)"; exit 1 ;;
  esac
done

echo "== ENGRAM Alpha Install — Gemini CLI =="
echo "  Repo:       $ALPHA_DIR"
echo "  ENGRAM:     $ENGRAM_HOME"
echo "  Extension:  $GEMINI_EXT_DIR"
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

PYTHON_BIN="$(python3 -c 'import sys; print(sys.executable)')"

command -v gemini >/dev/null || { echo "ERROR: 'gemini' CLI not found on PATH. Install Gemini CLI first (npm install -g @google/gemini-cli)."; exit 1; }

echo "  python3 ok ($PYTHON_BIN)"
echo "  git ok"
echo "  gemini ok"
echo "  Python deps ok (fastmcp, sentence-transformers)"
echo ""

# -------- Step 2: Idempotency --------

echo "== Step 2: Existing-install check =="

if [ -f "$ENGRAM_HOME/knowledge.db" ] && [ "$FORCE" -eq 0 ]; then
  echo "ERROR: existing install detected ($ENGRAM_HOME/knowledge.db)."
  echo ""
  echo "To UPGRADE this install (preserve graph + history + sessions, update code only):"
  echo "    tools/deploy.sh"
  echo ""
  echo "To reinstall fresh (archive existing data and start over):"
  echo "    ./install-gemini.sh --force"
  echo "  (Existing \$ENGRAM_HOME is moved to a timestamped backup; old graph is NOT"
  echo "   migrated into the new install.)"
  exit 1
fi

if [ "$FORCE" -eq 1 ] && [ -d "$ENGRAM_HOME" ]; then
  BACKUP="$ENGRAM_HOME.backup.$(date +%Y%m%d_%H%M%S)"
  echo "  --force: moving $ENGRAM_HOME → $BACKUP"
  mv "$ENGRAM_HOME" "$BACKUP"
fi

if [ -d "$GEMINI_EXT_DIR" ] && [ "$FORCE" -eq 0 ]; then
  echo "ERROR: existing extension dir detected ($GEMINI_EXT_DIR)."
  echo ""
  echo "To UPGRADE this install (preserve graph + history + sessions, update code only):"
  echo "    tools/deploy.sh"
  echo ""
  echo "To reinstall fresh (archive existing extension dir and start over):"
  echo "    ./install-gemini.sh --force"
  echo "  (Existing \$GEMINI_EXT_DIR is moved to a timestamped backup; old extension"
  echo "   is NOT migrated into the new install.)"
  exit 1
fi

if [ "$FORCE" -eq 1 ] && [ -d "$GEMINI_EXT_DIR" ]; then
  EXT_BACKUP="$GEMINI_EXT_DIR.backup.$(date +%Y%m%d_%H%M%S)"
  echo "  --force: moving $GEMINI_EXT_DIR → $EXT_BACKUP"
  mv "$GEMINI_EXT_DIR" "$EXT_BACKUP"
fi
echo "  ok"
echo ""

# -------- Step 3: Directories --------

echo "== Step 3: Creating directories =="

mkdir -p "$ENGRAM_HOME/hooks"
mkdir -p "$ENGRAM_HOME/tools"
mkdir -p "$ENGRAM_HOME/diary"
mkdir -p "$ENGRAM_HOME/sessions"
mkdir -p "$ENGRAM_HOME/logs"
mkdir -p "$ENGRAM_HOME/secrets"
mkdir -p "$ENGRAM_HOME/heartbeat-queue/to-heartbeat"
mkdir -p "$ENGRAM_HOME/heartbeat-queue/from-heartbeat"
# Initial empty append-files referenced by HEARTBEAT.md steps 2 + 4 + 6.
# Touching them now means a fresh-install heartbeat firing won't see "file
# not found" errors when it reads/appends. They're append-only markdown
# logs — empty initial state is the correct starting point. Mirrored from
# install.sh so Gemini-target installs get the same heartbeat scaffold.
[ -f "$ENGRAM_HOME/heartbeat-queue/to-heartbeat/notes.md" ] || \
  echo "<!-- Foreground-to-heartbeat directives. One bullet per line. Heartbeat consumes + deletes after handling. -->" \
  > "$ENGRAM_HOME/heartbeat-queue/to-heartbeat/notes.md"
[ -f "$ENGRAM_HOME/heartbeat-queue/from-heartbeat/inbox.md" ] || \
  echo "<!-- Heartbeat-to-foreground digests. Append-only. Foreground reads on next session. -->" \
  > "$ENGRAM_HOME/heartbeat-queue/from-heartbeat/inbox.md"
mkdir -p "$GEMINI_EXT_DIR/hooks"
chmod 0700 "$ENGRAM_HOME/secrets"
chmod 0700 "$ENGRAM_HOME/diary"

# Open the top-level $ENGRAM_HOME dir to peer agents (group=agents) so
# the multi-agent viz_server can read this agent's knowledge.db. Subdirs
# with private content (secrets/, diary/) keep mode 700 above. The DB
# itself gets group-read in the bootstrap step below. No-op silently if
# the `agents` group doesn't exist (single-tenant install).
if getent group agents >/dev/null 2>&1; then
  chgrp agents "$ENGRAM_HOME" 2>/dev/null || true
  chmod g+rx "$ENGRAM_HOME" 2>/dev/null || true
fi

echo "  $ENGRAM_HOME"
echo "  $GEMINI_EXT_DIR"
echo "  heartbeat-queue/{to,from}-heartbeat/ (scaffolded with notes.md + inbox.md)"
echo "  heartbeat-queue/README.md (scaffolded by bootstrap from template)"
echo ""

# -------- Step 4: Hooks + runtime snapshot --------

echo "== Step 4: Copying files =="

# Hook scripts: each install gets its own copy (so uninstalling one doesn't
# break the other), and the scripts are path-aware via $ENGRAM_HOME so they
# operate on this install's data dir.
# Hooks: deploy from hooks/gemini/ if populated; fall back to hooks/claude/
# while Mneme hasn't yet mirrored gemini-side sidecar versions. Per ob_1354,
# the hook surface JSON shape is mostly compatible between the two CLIs, so
# the claude hooks are usable as-is on gemini until divergence is forced.
GEMINI_HOOKS_SRC="$ALPHA_DIR/hooks/gemini"
if [ ! -d "$GEMINI_HOOKS_SRC" ] || [ -z "$(ls -A "$GEMINI_HOOKS_SRC" 2>/dev/null | grep -v '^\.gitkeep$' || true)" ]; then
  GEMINI_HOOKS_SRC="$ALPHA_DIR/hooks/claude"
  echo "  (gemini sidecar empty; bootstrapping hooks from claude/)"
fi
if [ -d "$GEMINI_HOOKS_SRC" ] && [ -n "$(ls -A "$GEMINI_HOOKS_SRC" 2>/dev/null | grep -v '^\.gitkeep$' || true)" ]; then
  cp -r "$GEMINI_HOOKS_SRC/"* "$ENGRAM_HOME/hooks/"
  chmod +x "$ENGRAM_HOME/hooks/"*.py 2>/dev/null || true
  chmod +x "$ENGRAM_HOME/hooks/"*.sh 2>/dev/null || true
  if [ -f "$ENGRAM_HOME/hooks/start-engram-daemon.sh" ]; then
    sed -i "s|{{PYTHON}}|$PYTHON_BIN|g" "$ENGRAM_HOME/hooks/start-engram-daemon.sh"
  fi
  echo "  hooks → $ENGRAM_HOME/hooks/ (from $GEMINI_HOOKS_SRC#$ALPHA_DIR/)"
fi

# Tools: heartbeat, sleep, dispatcher launchers, telegram bot, channel
# helpers. Same per-install copy pattern as hooks. Cron registration is
# handled by the runtime installer that wraps install-gemini.sh in the
# multi-agent uid-substrate setup.
if [ -d "$ALPHA_DIR/tools" ] && [ -n "$(ls -A "$ALPHA_DIR/tools" 2>/dev/null || true)" ]; then
  cp -r "$ALPHA_DIR/tools/"* "$ENGRAM_HOME/tools/"
  chmod +x "$ENGRAM_HOME/tools/"*.sh 2>/dev/null || true
  chmod +x "$ENGRAM_HOME/tools/"*.py 2>/dev/null || true
  echo "  tools → $ENGRAM_HOME/tools/"
fi

# Runtime snapshot (server + protocol + viz)
for runtime_file in server.py engram_confidence.py engram_client.py SKILL.md viz_server.py; do
  if [ -f "$ALPHA_DIR/$runtime_file" ]; then
    cp "$ALPHA_DIR/$runtime_file" "$ENGRAM_HOME/$runtime_file"
    case "$runtime_file" in
      *.py) chmod +x "$ENGRAM_HOME/$runtime_file" ;;
    esac
    echo "  $runtime_file → $ENGRAM_HOME/$runtime_file"
  fi
done

# Crontab template: staged into $ENGRAM_HOME so tools/sync-cron.sh can
# read it post-install without reaching back into the alpha source tree.
if [ -f "$ALPHA_DIR/templates/crontab.template" ]; then
  cp "$ALPHA_DIR/templates/crontab.template" "$ENGRAM_HOME/crontab.template"
  echo "  crontab.template → $ENGRAM_HOME/crontab.template"
fi
echo ""

# -------- Step 5: Gemini extension files (manifest + hooks.json + GEMINI.md) --------

echo "== Step 5: Rendering Gemini extension =="

INTEG_DIR="$ALPHA_DIR/integrations/gemini-cli"
ENGRAM_HOOKS_DIR="$ENGRAM_HOME/hooks"

# Manifest: registers MCP server, names GEMINI.md as context file, sets env
sed -e "s|{{PYTHON}}|$PYTHON_BIN|g" \
    -e "s|{{ENGRAM_HOME}}|$ENGRAM_HOME|g" \
    "$INTEG_DIR/gemini-extension.json.template" \
    > "$GEMINI_EXT_DIR/gemini-extension.json"
echo "  gemini-extension.json → $GEMINI_EXT_DIR/gemini-extension.json"

# Hooks config: wires SessionStart/BeforeAgent/AfterAgent → ENGRAM hooks
sed -e "s|{{PYTHON}}|$PYTHON_BIN|g" \
    -e "s|{{ENGRAM_HOOKS_DIR}}|$ENGRAM_HOOKS_DIR|g" \
    -e "s|{{ENGRAM_HOME}}|$ENGRAM_HOME|g" \
    "$INTEG_DIR/hooks/hooks.json.template" \
    > "$GEMINI_EXT_DIR/hooks/hooks.json"
echo "  hooks.json → $GEMINI_EXT_DIR/hooks/hooks.json"

echo ""

# -------- Step 6: Seed graph + GEMINI.md template substitution --------

echo "== Step 6: Seed graph + GEMINI.md template resolution =="

ALPHA_DIR="$ALPHA_DIR" \
ALPHA_TEMPLATES_DIR="$ALPHA_DIR/templates" \
ALPHA_INTEGRATIONS_DIR="$ALPHA_DIR/integrations" \
ENGRAM_DATA_DIR="$ENGRAM_HOME" \
GEMINI_EXT_DIR="$GEMINI_EXT_DIR" \
PYTHON_BIN="$PYTHON_BIN" \
TARGET="gemini" \
FORCE="$FORCE" \
  "$PYTHON_BIN" "$ALPHA_DIR/bootstrap.py"

# Open the just-created knowledge.db (and WAL friends) to peer agents
# (group=agents) so the multi-agent viz_server can read it. Pairs with
# the $ENGRAM_HOME group-traversal opening in Step 3.
if getent group agents >/dev/null 2>&1 && [ -f "$ENGRAM_HOME/knowledge.db" ]; then
  chgrp agents "$ENGRAM_HOME"/knowledge.db* 2>/dev/null || true
  chmod g+r "$ENGRAM_HOME"/knowledge.db* 2>/dev/null || true
fi
echo ""

# -------- Step 7: First-run marker --------

echo "== Step 7: Arming first-session marker =="

cat > "$ENGRAM_HOME/first-run-pending" <<EOF
{
  "created": "$(date +%Y-%m-%dT%H:%M:%S%z)",
  "install_path": "$ALPHA_DIR",
  "surface": "gemini"
}
EOF
echo "  $ENGRAM_HOME/first-run-pending"
echo ""

# -------- Step 8: Git init --------

echo "== Step 8: Git =="

if [ ! -d "$ENGRAM_HOME/.git" ]; then
  (cd "$ENGRAM_HOME" && git init -q && git add -A && git commit -q -m "ENGRAM alpha install (Gemini surface)")
  echo "  initialized $ENGRAM_HOME/.git"
else
  echo "  .git exists, skipping init"
fi
echo ""

# -------- Step 9: Cron schedule (optional) --------

if [ "$NO_CRON" -eq 1 ]; then
  echo "== Step 9: Cron (skipped per --no-cron) =="
  echo "  Run later via: AGENT_NAME=<name> $ENGRAM_HOME/tools/sync-cron.sh --apply --agent-cli gemini"
  echo ""
else
  echo "== Step 9: Cron schedule =="

  CRON_AGENT_NAME="${AGENT_NAME:-$(id -un | sed 's/^agent-//')}"
  CRON_COUNTERPART="${COUNTERPART_NAME:-}"

  if [ -x "$ENGRAM_HOME/tools/sync-cron.sh" ]; then
    AGENT_NAME="$CRON_AGENT_NAME" \
    COUNTERPART_NAME="$CRON_COUNTERPART" \
    AGENT_CLI="gemini" \
    ENGRAM_HOME="$ENGRAM_HOME" \
      "$ENGRAM_HOME/tools/sync-cron.sh" --apply \
        || {
          echo ""
          echo "  WARN: cron install failed (see message above). Common causes:"
          echo "    - Existing un-bracketed ENGRAM lines in your crontab. Edit"
          echo "      crontab manually to remove them, then re-run:"
          echo "        AGENT_NAME=$CRON_AGENT_NAME $ENGRAM_HOME/tools/sync-cron.sh --apply --agent-cli gemini"
          echo "    - config.json missing 'cadence' block. Run:"
          echo "        $ENGRAM_HOME/tools/migrate_config_v2.py"
        }
  else
    echo "  sync-cron.sh not staged (unexpected); skipping cron install"
  fi
  echo ""
fi

# -------- Done --------

echo "== Install complete =="
echo ""
echo "Next steps:"
echo "  1. Start a new Gemini CLI session: gemini"
echo "  2. Verify the extension loaded: gemini mcp list (should show engram)"
echo "  3. The agent will detect $ENGRAM_HOME/first-run-pending and run the"
echo "     first-session cold-start dialogue (it picks its own name —"
echo "     independent from any other ENGRAM-backed agent on this machine)"
echo "  4. After dialogue completes, identity placeholders resolve in"
echo "     $GEMINI_EXT_DIR/GEMINI.md and $ENGRAM_HOME/warm-briefing.md"
echo "  5. Verify cron with 'crontab -l' (or re-run with --no-cron to skip)"
echo ""
