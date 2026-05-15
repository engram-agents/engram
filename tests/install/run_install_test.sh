#!/usr/bin/env bash
# Container entry-point for the install.sh test harness.
#
# Assumes /alpha is the mounted alpha tree (read-only) and runs install.sh
# against $HOME/.engram + $HOME/.claude. Then verifies the expected post-
# install state via assertions on file presence + content.
#
# Exit codes:
#   0 — install ran cleanly AND verification passed
#   1 — install.sh exited non-zero
#   2 — install.sh succeeded but verification found missing/wrong artifacts
#
# Stderr carries the assertion that failed, so CI logs are readable.

set -euo pipefail

ALPHA_DIR="${ALPHA_DIR:-/alpha}"

if [ ! -d "$ALPHA_DIR" ]; then
    echo "ERROR: $ALPHA_DIR not found. Mount the alpha tree with -v \"\$(pwd):/alpha:ro\"." >&2
    exit 2
fi

echo "==================================================="
echo "  ENGRAM install.sh — clean-Ubuntu Docker test"
echo "==================================================="
echo "  Alpha tree:  $ALPHA_DIR"
echo "  Test home:   $HOME"
echo ""

# Copy the alpha tree to a writable location — install.sh creates files
# alongside itself for some operations (e.g., bootstrap.py imports the
# server module from the snapshot). Read-only mount would block that.
# The `rm -rf /tmp/alpha` guards the interactive-debug workflow where the
# README's `docker run -it --entrypoint /bin/bash ... ; bash run_install_test.sh`
# pattern is repeated within a single container instance. Without it,
# `cp -r` would nest the tree as /tmp/alpha/alpha and install.sh would
# operate on stale leftover state from the previous run. Harmless on
# the primary single-shot `docker run --rm` path.
#
# Safety guard: refuse to run if ALPHA_DIR points at /tmp/alpha. Otherwise
# someone overriding the env to test a locally-modified copy placed there
# would have their source destroyed by the rm before cp could read it.
if [ "$ALPHA_DIR" = "/tmp/alpha" ]; then
    echo "ERROR: ALPHA_DIR cannot be /tmp/alpha — the harness uses that path as a writable copy destination and will rm -rf it before cp." >&2
    exit 2
fi
rm -rf /tmp/alpha
cp -r "$ALPHA_DIR" /tmp/alpha
cd /tmp/alpha

echo "==================================================="
echo "  Phase 1: run install.sh (cron opt-in; default skips it)"
echo "==================================================="
if ! bash install.sh; then
    echo ""
    echo "FAIL: install.sh exited non-zero." >&2
    exit 1
fi

echo ""
echo "==================================================="
echo "  Phase 2: verify expected post-install state"
echo "==================================================="

ENGRAM_HOME="$HOME/.engram"
CLAUDE_HOME="$HOME/.claude"

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

assert_contains() {
    local path="$1"
    local needle="$2"
    if ! grep -qF "$needle" "$path"; then
        echo "FAIL: $path missing expected content: '$needle'" >&2
        exit 2
    fi
    echo "  ✓ $path contains: '$needle'"
}

# Core artifacts
echo "Core data + seed graph:"
assert_file "$ENGRAM_HOME/knowledge.db"
assert_file "$ENGRAM_HOME/seed-manifest.json"
assert_dir "$ENGRAM_HOME/hooks"
assert_dir "$ENGRAM_HOME/tools"

# First-run marker
echo ""
echo "First-run marker (cold-start armed):"
assert_file "$ENGRAM_HOME/first-run-pending"

# Heartbeat-queue scaffold. The parent dir always exists (created by
# install.sh since 2026-04-29). The subdirs + initial notes.md / inbox.md
# files only land if PR #48 has merged into the alpha tree being tested
# — guard the deeper checks so this harness works against either dev
# state without false failures.
echo ""
echo "Heartbeat-queue scaffold:"
assert_dir "$ENGRAM_HOME/heartbeat-queue"
if [ -d "$ENGRAM_HOME/heartbeat-queue/to-heartbeat" ]; then
    assert_dir "$ENGRAM_HOME/heartbeat-queue/from-heartbeat"
    assert_file "$ENGRAM_HOME/heartbeat-queue/to-heartbeat/notes.md"
    assert_file "$ENGRAM_HOME/heartbeat-queue/from-heartbeat/inbox.md"
    echo "  ✓ full heartbeat-queue scaffold present (PR #48 has merged)"
else
    echo "  ⚠ heartbeat-queue subdirs not present (pre-PR#48 base state — assertion skipped)"
fi

# Templates rendered (placeholders deferred to first-session, NOT bootstrap)
echo ""
echo "Templates rendered (CLAUDE.md / warm-briefing / HEARTBEAT.md if shipped):"
assert_file "$CLAUDE_HOME/CLAUDE.md"
assert_file "$ENGRAM_HOME/warm-briefing.md"

# Negative-assertion: daily-highlights.md was retired (dv_0478 / migrated
# to history/YYYY-MM-DD.md). If a future change accidentally re-introduces
# the bootstrap call, this catches it cleanly instead of leaving a silent
# regression where the legacy file gets re-created.
if [ -f "$ENGRAM_HOME/daily-highlights.md" ]; then
    echo "FAIL: legacy daily-highlights.md was re-created — was retired; expected per-day files under history/." >&2
    exit 2
fi
echo "  ✓ no legacy daily-highlights.md (retired; superseded by history/YYYY-MM-DD.md)"
# HEARTBEAT.md ships once PR #44 lands. Currently optional — bootstrap.py
# guards with .exists().
if [ -f "$ENGRAM_HOME/HEARTBEAT.md" ]; then
    assert_contains "$ENGRAM_HOME/HEARTBEAT.md" "{{AGENT_NAME}}"  # placeholder still present pre-first-session
    echo "  ✓ HEARTBEAT.md rendered (first-session will resolve placeholders)"
fi

# CLAUDE.md should still have placeholders unresolved (deferred to first-session)
assert_contains "$CLAUDE_HOME/CLAUDE.md" "{{AGENT_NAME}}"
assert_contains "$ENGRAM_HOME/warm-briefing.md" "{{USER_NAME}}"

# Seed graph should have 5 seed nodes resolved (bootstrap.py creates them
# and writes IDs into the manifest).
echo ""
echo "Seed graph integrity:"
node_count=$(sqlite3 "$ENGRAM_HOME/knowledge.db" "SELECT COUNT(*) FROM nodes WHERE is_current=1;")
if [ "$node_count" -lt 5 ]; then
    echo "FAIL: expected ≥5 seed nodes in knowledge.db, got $node_count" >&2
    exit 2
fi
echo "  ✓ knowledge.db has $node_count current nodes (≥5 expected)"

# Schema sanity
table_count=$(sqlite3 "$ENGRAM_HOME/knowledge.db" "SELECT COUNT(*) FROM sqlite_master WHERE type='table';")
if [ "$table_count" -lt 3 ]; then
    echo "FAIL: expected ≥3 tables in knowledge.db, got $table_count" >&2
    exit 2
fi
echo "  ✓ knowledge.db has $table_count tables (schema bootstrapped)"

echo ""
echo "==================================================="
echo "  PASS: install.sh produced the expected post-install state"
echo "==================================================="
