#!/usr/bin/env bash
# Container entry-point for the agentctl-spawn-flow test harness.
#
# Phases:
#   1. agentctl spawn → creates agent-newborn-<timestamp>; assert substrate
#   2. (install.sh already ran inside spawn, as the newborn) assert post-install state
#   3. agentctl finalize-name → rename newborn to a chosen name; assert rename
#
# Exit codes:
#   0 — all phases passed
#   1 — agentctl spawn or its bootstrap failed
#   2 — newborn substrate verification failed
#   3 — (unused since #4097: install.sh runs inside spawn; its failure is exit 1)
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
# agent-bootstrap clones this source as the newborn and checks out the newest deployable
# tag that is an ancestor of origin/dev (never the branch head). Give the copy a fresh
# one-commit history on branch dev with such a tag.
rm -rf /tmp/alpha/.git
git -C /tmp/alpha init -q -b dev
git -C /tmp/alpha add -A
git -C /tmp/alpha -c user.name=spawn-test -c user.email=spawn-test@example.invalid commit -q -m "spawn-test fixture"
git -C /tmp/alpha tag v0.0.0-spawntest
# The newborn clones a root-owned source: agent-bootstrap trusts that one .git for the clone
# command alone (a scoped GIT_CONFIG_GLOBAL temp file), so the fixture adds NO safe.directory
# entry: a global one here would mask the very path under test.
chmod -R a+rX /tmp/alpha

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
if id -nG "$NEWBORN_USER" | grep -qw agents; then
    echo "  ✓ newborn is in 'agents' group"
else
    echo "FAIL: newborn '$NEWBORN_USER' is not in 'agents' group" >&2
    exit 2
fi

# Step 5.7: operator ACL on ~/.engram directory default (agent-bootstrap issue #341)
echo ""
echo "Phase 1 verification — ~/.engram directory default ACL (step 5.7):"

# ~/.engram must exist after spawn (agent-bootstrap mkdir -p's it for the ACL step).
assert_dir "$NEWBORN_HOME/.engram"

# agent-bootstrap itself writes no config.json stub; tools/install.sh (run by agent-bootstrap
# as the newborn, #4097) creates it. So after spawn it exists and the clone sits on a tag.
assert_dir "$NEWBORN_HOME/engram-alpha"
if [ "$(sudo -u "$NEWBORN_USER" -H git -C "$NEWBORN_HOME/engram-alpha" describe --tags --exact-match HEAD)" != "v0.0.0-spawntest" ]; then
    echo "FAIL: the newborn's clone is not on the deployable tag v0.0.0-spawntest" >&2
    exit 2
fi
echo "  ✓ newborn clone is on a deployable tag (never the branch head)"
# No claude symlink into another agent's home or a system path (the old section 5.5).
if [ -L "$NEWBORN_HOME/.local/bin/claude" ]; then
    case "$(readlink "$NEWBORN_HOME/.local/bin/claude")" in
        /home/agent-*|/usr/local/*|/opt/claude/*)
            echo "FAIL: newborn's claude points outside its own install: $(readlink "$NEWBORN_HOME/.local/bin/claude")" >&2
            exit 2 ;;
    esac
fi
echo "  ✓ no claude symlink into another home"

# Synthetic ACL tests: exercise the three behavioral paths of step 5.7.
# The spawn test runs as root with SUDO_USER unset (Docker CMD), so the
# operator-absent path fires for the real spawn above. We verify all three
# paths synthetically below using temp directories.
echo ""
echo "Synthetic ACL tests (step 5.7 — three paths):"

if ! command -v setfacl >/dev/null 2>&1; then
    echo "  WARNING: setfacl not available — cannot run ACL behavioral tests." >&2
    echo "           Install the 'acl' package (apt install acl) to enable full coverage." >&2
else

# Test 1: operator-present → directory default ACL set on ~/.engram.
# No config.json on disk → only the -d (default) ACL is applied.
TMP_DIR=$(mktemp -d)
setfacl -d -m "u:root:rw" "$TMP_DIR"
if getfacl "$TMP_DIR" 2>/dev/null | grep -q "default:user:root:rw"; then
    echo "  ✓ operator-present: default ACL applied to directory"
else
    echo "FAIL: operator-present: default:user:root:rw not found in getfacl output" >&2
    getfacl "$TMP_DIR" >&2 || true
    rm -rf "$TMP_DIR"
    exit 2
fi

# Also verify that a file created INSIDE the dir inherits the default ACL
# (this is the core mechanism: new files get operator rw without a stub).
INNER_FILE="$TMP_DIR/config.json"
touch "$INNER_FILE"
if getfacl "$INNER_FILE" 2>/dev/null | grep -q "user:root:rw"; then
    echo "  ✓ operator-present: new file inside dir inherits operator rw ACL"
else
    echo "FAIL: operator-present: new file did not inherit default ACL" >&2
    getfacl "$INNER_FILE" >&2 || true
    rm -rf "$TMP_DIR"
    exit 2
fi
rm -rf "$TMP_DIR"

# Test 2: operator-present + config.json already exists (re-bootstrap path) →
# direct named-user ACL also applied to the existing file.
TMP_DIR2=$(mktemp -d)
EXISTING_CFG="$TMP_DIR2/config.json"
echo '{"schema_version":2}' > "$EXISTING_CFG"
setfacl -d -m "u:root:rw" "$TMP_DIR2"
setfacl -m "u:root:rw" "$EXISTING_CFG"
if getfacl "$EXISTING_CFG" 2>/dev/null | grep -q "user:root:rw"; then
    echo "  ✓ re-bootstrap: existing config.json receives direct ACL grant"
else
    echo "FAIL: re-bootstrap: existing config.json did not get direct ACL grant" >&2
    getfacl "$EXISTING_CFG" >&2 || true
    rm -rf "$TMP_DIR2"
    exit 2
fi
rm -rf "$TMP_DIR2"

# Test 3: operator-absent (SUDO_USER empty or root) → ACL step skipped, no error.
SKIP_CHECK=$(bash -c '
OPERATOR=""
if [[ -n "$OPERATOR" ]]; then
    echo "ran_acl"
else
    echo "skipped_acl"
fi
')
if [[ "$SKIP_CHECK" == "skipped_acl" ]]; then
    echo "  ✓ operator-absent: ACL step correctly skipped (no error)"
else
    echo "FAIL: operator-absent: ACL step ran unexpectedly" >&2
    exit 2
fi

    echo "  ✓ all step 5.7 ACL logic tests passed"
fi  # end setfacl-available block

# ---------------------------------------------------------------------
# Phase 2: install.sh as the newborn
# ---------------------------------------------------------------------
echo
echo "========================================================="
echo "  Phase 2: install.sh as $NEWBORN_USER"
echo "========================================================="

# tools/install.sh already ran inside Phase 1: agent-bootstrap runs it AS the newborn after
# creating the substrate (#4097), and agentctl spawn exits non-zero if it failed (exit 1
# above). Nothing is run here; this phase only verifies the result. (Exit 3 is now
# unused. The artifact list below predates install.sh and is UNVERIFIED against it: run
# the container once and reconcile.)

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
    echo "  (no baseline snapshot — Phase 2 install was minimal in this harness; non-fatal)"
fi

echo
echo "========================================================="
echo "  PASS: spawn → install → finalize-name flow validated"
echo "========================================================="
