#!/usr/bin/env bash
# Container entry-point for the agentctl-spawn-flow test harness.
#
# Phases:
#   1. agentctl spawn → creates agent-newborn-<timestamp>; assert substrate
#   2. install.sh as newborn → materialize ENGRAM; assert post-install state
#   3. agentctl finalize-name → rename newborn to a chosen name; assert rename
#
# Exit codes:
#   0 — all phases passed
#   1 — agentctl spawn or its bootstrap failed
#   2 — newborn substrate verification failed
#   3 — newborn install.sh failed
#   4 — install.sh succeeded but verification found missing artifacts
#   5 — finalize-name failed or post-rename state is wrong

set -euo pipefail

ALPHA_DIR="${ALPHA_DIR:-/alpha}"

if [ ! -d "$ALPHA_DIR" ]; then
    echo "ERROR: $ALPHA_DIR not found. Mount the alpha tree with -v \"\$(pwd):/alpha:ro\"." >&2
    exit 2
fi

echo "========================================================="
echo "  ENGRAM agentctl spawn-flow — clean-Ubuntu Docker test"
echo "========================================================="
echo "  Alpha tree:  $ALPHA_DIR"
echo "  Container:   running as $(id -un) (uid $(id -u))"
echo

# Copy alpha to a writable location — agent-bootstrap renders install.sh
# wrapper paths relative to it; in the spawn flow the wrapper points at
# wherever --alpha-source was set.
cp -r "$ALPHA_DIR" /tmp/alpha
ALPHA_SOURCE=/tmp/alpha

# ---------------------------------------------------------------------
# Phase 1: agentctl spawn
# ---------------------------------------------------------------------
echo "========================================================="
echo "  Phase 1: agentctl spawn --runtime claude"
echo "========================================================="

SPAWN_OUT=$(mktemp)
if ! /usr/local/bin/agentctl spawn --runtime claude --alpha-source "$ALPHA_SOURCE" 2>&1 | tee "$SPAWN_OUT"; then
    echo
    echo "FAIL: agentctl spawn exited non-zero." >&2
    exit 1
fi

# Parse the newborn-tag from agentctl's output line: "  Newborn name:   agent-newborn-YYYYMMDDhhmmss"
NEWBORN_USER=$(grep -oE 'agent-newborn-[0-9]{14}' "$SPAWN_OUT" | head -1)
NEWBORN_TAG="${NEWBORN_USER#agent-}"
if [[ -z "$NEWBORN_TAG" ]]; then
    echo "FAIL: could not parse newborn-tag from agentctl spawn output." >&2
    cat "$SPAWN_OUT" >&2
    exit 1
fi

NEWBORN_HOME="/home/$NEWBORN_USER"
echo
echo "Captured newborn:"
echo "  user:    $NEWBORN_USER"
echo "  tag:     $NEWBORN_TAG"
echo "  home:    $NEWBORN_HOME"
echo

assert_file() {
    local path="$1"
    if [ ! -f "$path" ]; then
        echo "FAIL: expected file not found: $path" >&2
        exit 2
    fi
    echo "  ✓ $path"
}

assert_dir() {
    local path="$1"
    if [ ! -d "$path" ]; then
        echo "FAIL: expected directory not found: $path" >&2
        exit 2
    fi
    echo "  ✓ $path/"
}

assert_user_exists() {
    local user="$1"
    if ! id "$user" >/dev/null 2>&1; then
        echo "FAIL: user $user does not exist" >&2
        exit 2
    fi
    echo "  ✓ user $user (uid $(id -u "$user"), groups: $(id -Gn "$user"))"
}

echo "Phase 1 verification — substrate state:"
assert_user_exists "$NEWBORN_USER"
assert_dir "$NEWBORN_HOME"
assert_file "$NEWBORN_HOME/.bashrc"
assert_dir "$NEWBORN_HOME/.runtime"
assert_file "$NEWBORN_HOME/.runtime/start.sh"
assert_file "$NEWBORN_HOME/.runtime/install.sh"
echo "  ✓ newborn is in 'agents' group: $(id -nG "$NEWBORN_USER" | grep -qw agents && echo yes || echo NO)"

# ---------------------------------------------------------------------
# Phase 2: install.sh as the newborn
# ---------------------------------------------------------------------
echo
echo "========================================================="
echo "  Phase 2: install.sh as $NEWBORN_USER"
echo "========================================================="

# The newborn's ~/.runtime/install.sh wraps alpha's install.sh. Run it
# under the newborn's uid via sudo (sudo present in the image; spt's
# sudoers config doesn't apply here since we're running in a container).
if ! sudo -u "$NEWBORN_USER" -H bash -lc 'bash ~/.runtime/install.sh --no-cron'; then
    echo
    echo "FAIL: newborn's install.sh exited non-zero." >&2
    exit 3
fi

echo
echo "Phase 2 verification — post-install state under $NEWBORN_HOME:"
assert_file "$NEWBORN_HOME/.engram/knowledge.db"
assert_file "$NEWBORN_HOME/.engram/seed-manifest.json"
assert_dir  "$NEWBORN_HOME/.engram/hooks"
assert_dir  "$NEWBORN_HOME/.engram/tools"
assert_file "$NEWBORN_HOME/.engram/first-run-pending"
assert_file "$NEWBORN_HOME/.claude/CLAUDE.md"
assert_file "$NEWBORN_HOME/.engram/warm-briefing.md"

# Templates must still carry the placeholders — first-session resolves them.
if ! grep -q '{{AGENT_NAME}}' "$NEWBORN_HOME/.claude/CLAUDE.md"; then
    echo "FAIL: $NEWBORN_HOME/.claude/CLAUDE.md is missing the {{AGENT_NAME}} placeholder" >&2
    exit 4
fi
echo "  ✓ {{AGENT_NAME}} placeholder retained in CLAUDE.md (first-session contract)"

# Seed graph integrity (matches tests/install/'s assertion shape).
NODE_COUNT=$(sudo -u "$NEWBORN_USER" sqlite3 "$NEWBORN_HOME/.engram/knowledge.db" \
    "SELECT COUNT(*) FROM nodes WHERE is_current=1;" 2>/dev/null || echo 0)
if [ "$NODE_COUNT" -lt 5 ]; then
    echo "FAIL: knowledge.db has $NODE_COUNT current nodes, expected >= 5" >&2
    exit 4
fi
echo "  ✓ knowledge.db has $NODE_COUNT current nodes"

# Explicit .git + commit assertion — the install.sh git-identity fix
# exercises this path implicitly (a missing identity makes Step 7's
# `git commit` fail and propagates as exit 3). The implicit coverage is
# real, but if a future change weakens `set -euo pipefail` or wraps the
# git block in `|| true`, the silent-failure path opens. Asserting the
# .git dir + at least one commit closes that gap.
assert_dir "$NEWBORN_HOME/.engram/.git"
if ! sudo -u "$NEWBORN_USER" git -C "$NEWBORN_HOME/.engram" log --oneline -1 >/dev/null 2>&1; then
    echo "FAIL: $NEWBORN_HOME/.engram/.git has no commits" >&2
    exit 4
fi
echo "  ✓ .engram/.git initialized with at least one commit"

# ---------------------------------------------------------------------
# Phase 3: finalize-name rename
# ---------------------------------------------------------------------
echo
echo "========================================================="
echo "  Phase 3: agentctl finalize-name → 'testbot'"
echo "========================================================="

CHOSEN_NAME="testbot"
CHOSEN_USER="agent-$CHOSEN_NAME"
CHOSEN_HOME="/home/$CHOSEN_USER"

if ! /usr/local/bin/agentctl finalize-name "$NEWBORN_TAG" "$CHOSEN_NAME"; then
    echo
    echo "FAIL: agentctl finalize-name exited non-zero." >&2
    exit 5
fi

echo
echo "Phase 3 verification — post-rename state:"
assert_user_exists "$CHOSEN_USER"
assert_dir "$CHOSEN_HOME"
assert_dir "$CHOSEN_HOME/.engram"

# The original newborn-named user/home should be gone.
if id "$NEWBORN_USER" >/dev/null 2>&1; then
    echo "FAIL: pre-rename user $NEWBORN_USER still exists after finalize-name" >&2
    exit 5
fi
echo "  ✓ pre-rename user $NEWBORN_USER no longer exists"

if [ -d "$NEWBORN_HOME" ]; then
    echo "FAIL: pre-rename home $NEWBORN_HOME still exists after finalize-name" >&2
    exit 5
fi
echo "  ✓ pre-rename home $NEWBORN_HOME no longer exists"

# finalize-name leaves a marker + baseline snapshot.
assert_file "$CHOSEN_HOME/.engram/finalize-name-complete.json"
if [ -d "$CHOSEN_HOME/.engram.first-session-baseline.bak" ]; then
    echo "  ✓ baseline snapshot at .engram.first-session-baseline.bak/"
else
    echo "  (no baseline snapshot — Phase 2 ran with --no-cron so install was minimal; non-fatal)"
fi

echo
echo "========================================================="
echo "  PASS: spawn → install → finalize-name flow validated"
echo "========================================================="
