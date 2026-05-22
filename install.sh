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
#   ./install.sh --force    # archive existing install and start fresh
#                           #   (knowledge graph + history are moved to a
#                           #   timestamped backup dir; NOT preserved)
#   ./install.sh --help
#
# To UPGRADE an existing install while preserving the knowledge graph,
# history, diary, and sessions, use:  tools/deploy.sh
# (install.sh --force is for fresh-start, not upgrade.)

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$SCRIPT_DIR"
ALPHA_DIR="$SCRIPT_DIR"

ENGRAM_HOME="${ENGRAM_HOME:-$HOME/.engram}"
CLAUDE_HOME="${CLAUDE_HOME:-$HOME/.claude}"

FORCE=0
WITH_CRON=0
for arg in "$@"; do
  case "$arg" in
    --force) FORCE=1 ;;
    --with-cron) WITH_CRON=1 ;;
    --help|-h)
      echo "Usage: ./install.sh [--force] [--with-cron]"
      echo ""
      echo "Environment overrides:"
      echo "  ENGRAM_HOME   target for ENGRAM data (default: \$HOME/.engram)"
      echo "  CLAUDE_HOME   target for Claude config (default: \$HOME/.claude)"
      echo "  AGENT_NAME    cron identity for the installed agent (default: derived from username)"
      echo "  COUNTERPART_NAME   peer agent name for inter-agent dispatcher (optional)"
      echo ""
      echo "  --force: archive existing installation and start fresh."
      echo "           Moves \$ENGRAM_HOME (graph, history, diary, sessions, logs)"
      echo "           to a timestamped backup directory. The new install starts"
      echo "           with an empty knowledge graph — old data is NOT migrated in."
      echo "           To UPGRADE an existing install while preserving data, use:"
      echo "             tools/deploy.sh"
      echo "  --with-cron: install the ENGRAM cron block (heartbeat at :00/:30,"
      echo "             dispatcher autostart + watchdog). Cron is opt-in by"
      echo "             default — heartbeat mode is not fully tested and shouldn't"
      echo "             mix into a fresh tester's interactive experience. Add"
      echo "             this flag when you specifically want the autonomous"
      echo "             heartbeat + multi-agent dispatchers running. After install,"
      echo "             cron can also be added later via:"
      echo "               AGENT_NAME=<name> \$ENGRAM_HOME/tools/sync-cron.sh --apply"
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

# Python version gate via capability check: `sys.get_int_max_str_digits`
# was a CVE-2020-10735 backport landing in 3.10.7, 3.11.0 final, 3.9.14,
# and 3.8.14. Current `transformers` (5.8.x) needs it at import time.
# Capability check (hasattr) is preferable to a tuple-version check
# because Python's version-info tuple comparison short-circuits on the
# minor field: `(3, 11, 0, 'candidate', 1) >= (3, 10, 7)` evaluates
# `11 > 10 → True` and never inspects releaselevel, so a tuple-based
# gate would let 3.11.0rc1 pass (the specific broken case on Ubuntu 22.04).
# Testing the attribute directly is immune to all such edge cases.
if ! python3 -c "import sys; sys.exit(0 if hasattr(sys, 'get_int_max_str_digits') else 1)"; then
  echo "ERROR: this Python is missing sys.get_int_max_str_digits (you have $(python3 --version 2>&1))."
  echo "Required floor: 3.10.7+ or any 3.11.0 final or later. Earlier 3.10.x and"
  echo "3.11.0rc1 release candidate miss the attribute that current transformers"
  echo "and sentence-transformers need at import."
  echo "Upgrade Python and try again."
  exit 1
fi

# Python deps: validate against the declared ranges in requirements.txt
# rather than just checking that the modules import. This catches the case
# where a fresh `pip install fastmcp sentence-transformers` pulled
# versions outside our supported range — symptoms range from import errors
# to runtime AttributeErrors (e.g., the 2026-05 transformers/sentence-
# transformers + Python 3.11rc release-candidate combination).
REQS_FILE="$REPO_ROOT/requirements.txt"
if [ -f "$REQS_FILE" ]; then
  if ! python3 -c "
import importlib.metadata as md
import re
import sys
errors = []
for line in open('$REQS_FILE'):
    line = line.strip()
    if not line or line.startswith('#'):
        continue
    # Skip recursive-include directives — these are valid in requirements.txt
    # (e.g. '-r requirements-base.txt') but aren't package specs to validate.
    if line.startswith('-r ') or line.startswith('--requirement ') or line.startswith('-c ') or line.startswith('--constraint '):
        continue
    m = re.match(r'([A-Za-z0-9_.-]+)\s*(.*)', line)
    if not m:
        continue
    name, spec = m.group(1), m.group(2).strip()
    try:
        installed = md.version(name)
    except md.PackageNotFoundError:
        errors.append(f'{name}: not installed (need {spec or \"any\"})')
        continue
    if spec:
        try:
            from packaging.requirements import Requirement
            from packaging.version import Version
            req = Requirement(f'{name}{spec}')
            if Version(installed) not in req.specifier:
                errors.append(f'{name}: have {installed}, need {spec}')
        except ImportError:
            # packaging not available — best-effort skip range check
            pass
if errors:
    print('\n'.join(errors))
    sys.exit(1)
" 2>&1; then
    echo ""
    echo "ERROR: one or more Python dependencies are missing or out of range."
    echo "Install or upgrade with:"
    echo "    pip install -r $REQS_FILE"
    exit 1
  fi
else
  # Fallback path for forks/branches that don't have requirements.txt yet.
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
fi

# Resolve an absolute path to the interpreter that just passed preflight.
# Baked into the MCP registration, settings.json hooks, and
# start-engram-daemon.sh so venv installs work without needing the venv
# activated every time Claude Code launches.
PYTHON_BIN="$(python3 -c 'import sys; print(sys.executable)')"

command -v claude >/dev/null || { echo "ERROR: 'claude' CLI not found on PATH. Install Claude Code first."; exit 1; }

echo "  python3 ok ($PYTHON_BIN)"
echo "  git ok"
echo "  claude ok"
echo "  Python deps ok (validated against $REQS_FILE ranges)"
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
  echo "    ./install.sh --force"
  echo "  (Existing \$ENGRAM_HOME is moved to a timestamped backup; old graph is NOT"
  echo "   migrated into the new install.)"
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
mkdir -p "$ENGRAM_HOME/tools"
mkdir -p "$ENGRAM_HOME/diary"
mkdir -p "$ENGRAM_HOME/sessions"
mkdir -p "$ENGRAM_HOME/logs"
mkdir -p "$ENGRAM_HOME/secrets"
mkdir -p "$ENGRAM_HOME/heartbeat-queue/to-heartbeat"
mkdir -p "$ENGRAM_HOME/heartbeat-queue/from-heartbeat"
# history/ — awake-state milestone log (PRs shipped, decisions, features); one
# file per day at YYYY-MM-DD.md, written by engram-bedtime Step 4.
# history/dream/ — sleep-cycle consolidation reports (turn N→N+1, resolutions,
# supersedes, fairy contribution); one file per day at YYYY-MM-DD.md, written
# by engram-sleep Step 6. Separation per dv_0636 (three-routine architecture)
# keeps awake-state milestones distinct from dream-state consolidation output.
mkdir -p "$ENGRAM_HOME/history/dream"
# Initial empty append-files referenced by HEARTBEAT.md steps 2 + 4 + 6.
# Touching them now means a fresh-install heartbeat firing won't see "file
# not found" errors when it reads/appends. They're append-only markdown
# logs — empty initial state is the correct starting point.
[ -f "$ENGRAM_HOME/heartbeat-queue/to-heartbeat/notes.md" ] || \
  echo "<!-- Foreground-to-heartbeat directives. One bullet per line. Heartbeat consumes + deletes after handling. -->" \
  > "$ENGRAM_HOME/heartbeat-queue/to-heartbeat/notes.md"
[ -f "$ENGRAM_HOME/heartbeat-queue/from-heartbeat/inbox.md" ] || \
  echo "<!-- Heartbeat-to-foreground digests. Append-only. Foreground reads on next session. -->" \
  > "$ENGRAM_HOME/heartbeat-queue/from-heartbeat/inbox.md"
mkdir -p "$CLAUDE_HOME/skills"
mkdir -p "$CLAUDE_HOME/agents"
chmod 0700 "$ENGRAM_HOME/secrets"
chmod 0700 "$ENGRAM_HOME/diary"

# Open the top-level $ENGRAM_HOME dir to peer agents (group=agents) so the
# multi-agent viz_server can read this agent's knowledge.db. Subdirs that
# hold private content (secrets/, diary/) keep mode 700 above and stay
# invisible. The DB itself gets group-read in the bootstrap step below
# (after server.py creates knowledge.db). If the `agents` group doesn't
# exist (single-tenant install with no shared infra), this is a no-op
# silently — peer access wasn't requested.
if getent group agents >/dev/null 2>&1; then
  chgrp agents "$ENGRAM_HOME" 2>/dev/null || true
  chmod g+rx "$ENGRAM_HOME" 2>/dev/null || true
fi

echo "  $ENGRAM_HOME"
echo "  $CLAUDE_HOME"
echo "  heartbeat-queue/{to,from}-heartbeat/ (scaffolded with notes.md + inbox.md)"
echo "  heartbeat-queue/README.md (scaffolded by bootstrap from template)"
echo "  history/dream/ (per-sleep-cycle records; engram-sleep Step 6)"
echo ""

# -------- Step 4: Skills, hooks, MCP config --------

echo "== Step 4: Copying files =="

if [ -d "$ALPHA_DIR/skills/claude" ] && [ -n "$(ls -A "$ALPHA_DIR/skills/claude" 2>/dev/null | grep -v '^\.gitkeep$' || true)" ]; then
  cp -r "$ALPHA_DIR/skills/claude/"* "$CLAUDE_HOME/skills/"
  echo "  skills → $CLAUDE_HOME/skills/ (from skills/claude/)"
fi

# Sub-agents. Used by sleep-routine (engram-dream-fairy) and PR workflow
# (engram-pr-reviewer). `cp -rL` follows symlinks at the source — the
# alpha repo keeps `.claude/agents/*.md` symlinked to `agents/claude/*.md`
# so the alpha-dev working copies and the shipped copies are guaranteed
# byte-identical.
if [ -d "$ALPHA_DIR/agents/claude" ] && [ -n "$(ls -A "$ALPHA_DIR/agents/claude" 2>/dev/null | grep -v '^\.gitkeep$' || true)" ]; then
  cp -rL "$ALPHA_DIR/agents/claude/"* "$CLAUDE_HOME/agents/"
  echo "  agents → $CLAUDE_HOME/agents/ (from agents/claude/)"
fi

if [ -d "$ALPHA_DIR/hooks/claude" ] && [ -n "$(ls -A "$ALPHA_DIR/hooks/claude" 2>/dev/null | grep -v '^\.gitkeep$' || true)" ]; then
  cp -r "$ALPHA_DIR/hooks/claude/"* "$ENGRAM_HOME/hooks/"
  chmod +x "$ENGRAM_HOME/hooks/"*.py 2>/dev/null || true
  chmod +x "$ENGRAM_HOME/hooks/"*.sh 2>/dev/null || true
  # Bake the resolved interpreter path into the daemon-launch script so it
  # doesn't depend on `python3` being on PATH at SessionStart-hook time.
  # `-i.bak` is the BSD/GNU-portable form (works on both macOS and Linux);
  # plain `-i` without an extension argument breaks on macOS BSD sed.
  if [ -f "$ENGRAM_HOME/hooks/start-engram-daemon.sh" ]; then
    sed -i.bak "s|{{PYTHON}}|$PYTHON_BIN|g" "$ENGRAM_HOME/hooks/start-engram-daemon.sh"
    rm -f "$ENGRAM_HOME/hooks/start-engram-daemon.sh.bak"
  fi
  echo "  hooks → $ENGRAM_HOME/hooks/ (from hooks/claude/)"
fi

# Tools: heartbeat, sleep, dispatcher launchers, telegram bot, channel
# helpers, sync-cron + render_cron, migrate_config_v2. Copied per-install
# (not symlinked) so each agent's tools operate on their own $ENGRAM_HOME
# by env-var contract. Cron registration happens in Step 8 below only
# when --with-cron is passed (opt-in default; heartbeat mode is not yet
# stable enough to ship into a fresh tester's environment by default).
if [ -d "$ALPHA_DIR/tools" ] && [ -n "$(ls -A "$ALPHA_DIR/tools" 2>/dev/null || true)" ]; then
  cp -r "$ALPHA_DIR/tools/"* "$ENGRAM_HOME/tools/"
  chmod +x "$ENGRAM_HOME/tools/"*.sh 2>/dev/null || true
  chmod +x "$ENGRAM_HOME/tools/"*.py 2>/dev/null || true
  echo "  tools → $ENGRAM_HOME/tools/"
fi

# Register the ENGRAM MCP server via the Claude Code CLI. This writes into
# ~/.claude.json (the real MCP config) in user scope, so the server is
# available in every project for this user. Writing directly to
# ~/.claude/.mcp.json does NOT work — that path is not a recognized MCP
# config location and Claude Code silently ignores it.
claude mcp remove engram -s user >/dev/null 2>&1 || true
claude mcp remove engram -s local >/dev/null 2>&1 || true
if claude mcp add engram -s user -- "$PYTHON_BIN" "$ENGRAM_HOME/server.py"; then
  echo "  MCP registered (user scope): engram → $PYTHON_BIN $ENGRAM_HOME/server.py"
else
  echo "ERROR: 'claude mcp add' failed; ENGRAM MCP server not registered."
  exit 1
fi

# Runtime snapshot: all top-level engram_*.py + SKILL.md copied into
# $ENGRAM_HOME so the install is self-contained and version-locked against
# ongoing main-branch development. Globs *.py so new top-level modules added
# to alpha root are auto-shipped (no enum to maintain — historical drift
# between this list and deploy.sh's manifest caused fresh-install crashes
# when engram_log_emitter.py and friends were missed).
for runtime_path in "$ALPHA_DIR"/*.py "$ALPHA_DIR/SKILL.md"; do
  runtime_file="$(basename "$runtime_path")"
  # Install-time scaffolding / dev tools — at alpha root but NOT runtime
  # modules. Mirror the MANIFEST_ORPHAN_ALLOWLIST in
  # tests/test_deploy_manifest_sync.py; deploy.sh's MANIFEST excludes
  # these too (kept symmetric so upgrades and fresh installs ship the
  # same set).
  case "$runtime_file" in
    bootstrap.py|selftest.py|engram_log_indexer.py) continue ;;
  esac
  if [ -f "$runtime_path" ]; then
    cp "$runtime_path" "$ENGRAM_HOME/$runtime_file"
    case "$runtime_file" in
      *.py) chmod +x "$ENGRAM_HOME/$runtime_file" ;;
    esac
    echo "  $runtime_file → $ENGRAM_HOME/$runtime_file"
  fi
done

# Write the install-time version marker so future deploy.sh runs have a
# baseline for 3-way comparisons + install-time template-changes warnings.
# Match deploy.sh's format exactly (tools/deploy.sh:562-572) so they're
# interoperable.
if git -C "$ALPHA_DIR" rev-parse --git-dir >/dev/null 2>&1; then
  ALPHA_SHA="$(git -C "$ALPHA_DIR" rev-parse --short HEAD)"
  ALPHA_BRANCH="$(git -C "$ALPHA_DIR" rev-parse --abbrev-ref HEAD)"
  if [ -n "$(git -C "$ALPHA_DIR" status --porcelain)" ]; then
    ALPHA_DIRTY="dirty"
  else
    ALPHA_DIRTY="clean"
  fi
else
  ALPHA_SHA="unknown"
  ALPHA_BRANCH="unknown"
  ALPHA_DIRTY="not-a-git-repo"
fi
INSTALL_TS=$(date -u +"%Y-%m-%dT%H:%M:%SZ")
cat > "$ENGRAM_HOME/.deployed-version" <<EOF
alpha_sha=$ALPHA_SHA
alpha_branch=$ALPHA_BRANCH
alpha_dirty=$ALPHA_DIRTY
deployed_at=$INSTALL_TS
deployed_from=$ALPHA_DIR
EOF
echo "  .deployed-version → $ENGRAM_HOME/.deployed-version (baseline for future deploy.sh runs)"

# Crontab template: staged into $ENGRAM_HOME so tools/sync-cron.sh can
# read it post-install without reaching back into the alpha source tree.
if [ -f "$ALPHA_DIR/templates/crontab.template" ]; then
  cp "$ALPHA_DIR/templates/crontab.template" "$ENGRAM_HOME/crontab.template"
  echo "  crontab.template → $ENGRAM_HOME/crontab.template"
fi

# Pre-warm the sentence-transformers embedding model so the first MCP launch
# in the user's next Claude Code session doesn't trip the 30s stdio handshake
# timeout while downloading + loading the model on a cold HuggingFace cache
# (alpha #144). On a warm cache this is a <1s no-op; on a cold cache it
# downloads ~80MB. Non-fatal — if it fails (no network, HF rate limit) the
# user can still recover via the /mcp slash command after the first timeout.
echo "  Pre-warming sentence-transformers cache (one-time, may take a few minutes on slow connections)..."
if "$PYTHON_BIN" -c "from sentence_transformers import SentenceTransformer; SentenceTransformer('all-MiniLM-L6-v2')" >/dev/null 2>&1; then
  echo "  pre-warm ok (embedding model cached for fast MCP startup)"
else
  echo "  WARN: pre-warm failed; first MCP launch may exceed Claude Code's 30s timeout."
  echo "        Recovery if first session reports 'MCP engram unavailable': run /mcp inside"
  echo "        Claude Code to reconnect once the embedder finishes loading in the background."
fi
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

# Open the just-created knowledge.db (and WAL friends) to peer agents
# (group=agents) so the multi-agent viz_server can read it. Pairs with
# the $ENGRAM_HOME group-traversal opening in Step 3.
if getent group agents >/dev/null 2>&1 && [ -f "$ENGRAM_HOME/knowledge.db" ]; then
  chgrp agents "$ENGRAM_HOME"/knowledge.db* 2>/dev/null || true
  chmod g+r "$ENGRAM_HOME"/knowledge.db* 2>/dev/null || true
fi
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
  (
    cd "$ENGRAM_HOME"
    git init -q
    # Ensure a local git identity exists so the install commit can succeed
    # even when no global identity is configured (fresh newborn agents on
    # a host where git was never set up for the new uid). Local config
    # only — no global pollution.
    if ! git config user.email >/dev/null 2>&1; then
      git config user.email "${USER:-engram}@$(hostname)"
    fi
    if ! git config user.name >/dev/null 2>&1; then
      git config user.name "${USER:-engram} (ENGRAM agent)"
    fi
    git add -A && git commit -q -m "ENGRAM alpha install"
  )
  echo "  initialized $ENGRAM_HOME/.git"
else
  echo "  .git exists, skipping init"
fi
echo ""

# -------- Step 8: Cron schedule (opt-in) --------
#
# Renders + installs the user's crontab from config.json's cadence block via
# tools/sync-cron.sh. Defaults match the production cadence: heartbeat at
# :00/:30, dispatcher autostart + watchdog.
#
# Cron is OPT-IN as of 2026-05-15: heartbeat mode (autonomous cron-fired
# sessions doing inter-agent coordination) is not fully tested and a fresh
# tester's first-day experience is better focused on interactive
# ENGRAM-with-Claude-Code without surprise background sessions. Single-
# agent installs (no COUNTERPART_NAME set) don't benefit from the
# dispatchers either. Pass --with-cron to enable, or run sync-cron.sh
# manually later.
#
# Sleep is NOT cron-scheduled as of 2026-05-14 (alpha #133 PR C). Sleep is
# interactive-first: the agent runs `engram-bedtime` then `engram-sleep` at
# end-of-day in the user's active session. `cadence.sleep_hour` /
# `cadence.sleep_minute` in config.json continue to define the heartbeat
# protected window (preferred quiet slot for interactive bedtime + sleep).
# Manual catch-up via `tools/sleep.sh` remains available if a day's sleep
# was missed.

if [ "$WITH_CRON" -eq 0 ]; then
  echo "== Step 8: Cron (skipped — default; pass --with-cron to enable) =="
  echo "  Add later via: AGENT_NAME=<name> $ENGRAM_HOME/tools/sync-cron.sh --apply"
  echo ""
else
  echo "== Step 8: Cron schedule (--with-cron) =="

  # AGENT_NAME defaults to the current username (stripped of any 'agent-'
  # prefix common in multi-agent uid setups).
  CRON_AGENT_NAME="${AGENT_NAME:-$(id -un | sed 's/^agent-//')}"
  CRON_COUNTERPART="${COUNTERPART_NAME:-}"

  if [ -x "$ENGRAM_HOME/tools/sync-cron.sh" ]; then
    AGENT_NAME="$CRON_AGENT_NAME" \
    COUNTERPART_NAME="$CRON_COUNTERPART" \
    AGENT_CLI="claude" \
    ENGRAM_HOME="$ENGRAM_HOME" \
      "$ENGRAM_HOME/tools/sync-cron.sh" --apply \
        || {
          echo ""
          echo "  WARN: cron install failed (see message above). Common causes:"
          echo "    - Existing un-bracketed ENGRAM lines in your crontab. Edit"
          echo "      crontab manually to remove them, then re-run:"
          echo "        AGENT_NAME=$CRON_AGENT_NAME $ENGRAM_HOME/tools/sync-cron.sh --apply"
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
echo "  1. Start a new Claude Code session"
echo "  2. The agent will detect $ENGRAM_HOME/first-run-pending"
echo "     and run the engram-first-session cold-start dialogue"
echo "  3. After dialogue completes, {{AGENT_NAME}} and {{USER_NAME}} will"
echo "     be resolved in $CLAUDE_HOME/CLAUDE.md and $ENGRAM_HOME/warm-briefing.md"
echo "  4. (Optional) Enable the autonomous heartbeat: re-run with --with-cron, or"
echo "     run: AGENT_NAME=<name> $ENGRAM_HOME/tools/sync-cron.sh --apply"
echo ""
