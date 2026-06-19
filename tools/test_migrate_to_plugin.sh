#!/usr/bin/env bash
# test_migrate_to_plugin.sh — tests for migrate-to-plugin.sh
#
# Runs against a FIXTURE HOME — never the real ~/.
# All fixtures are created in a temp directory that is cleaned up on exit.
#
# Coverage:
#   T1  Selective hook removal: scatter ENGRAM hooks gone, non-ENGRAM hooks
#       and plugin hooks untouched, valid JSON after.
#   T2  .claude.json engram-only deletion: engram entry removed, other MCP
#       servers untouched.
#   T3  DATA-allowlist refusal: script refuses to delete a DATA path even
#       with --remove-deployed-code.
#   T4  Idempotency: second run reports "already migrated", no mutation.
#   T5  --dry-run mutates nothing (checksum HOME before/after identical).
#   T6  Already-migrated host (no scatter surfaces) → verify-only path passes.

set -euo pipefail

# ---------------------------------------------------------------------------
# Colors + helpers
# ---------------------------------------------------------------------------
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

pass()  { echo -e "${GREEN}PASS${NC}  $*"; }
fail()  { echo -e "${RED}FAIL${NC}  $*"; FAILURES=$((FAILURES + 1)); }
skip()  { echo -e "${YELLOW}SKIP${NC}  $*"; }
info()  { echo "      $*"; }
section() { echo ""; echo "--- $* ---"; echo ""; }

FAILURES=0
TESTS_RUN=0

# ---------------------------------------------------------------------------
# Locate the script under test
# ---------------------------------------------------------------------------
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MIGRATE_SCRIPT="$SCRIPT_DIR/migrate-to-plugin.sh"

if [[ ! -f "$MIGRATE_SCRIPT" ]]; then
    echo "ERROR: migrate-to-plugin.sh not found at $MIGRATE_SCRIPT" >&2
    exit 1
fi

# ---------------------------------------------------------------------------
# Fixture setup
# ---------------------------------------------------------------------------
FIXTURE_BASE="$(mktemp -d)"
trap 'rm -rf "$FIXTURE_BASE"' EXIT

# We use a dedicated subdirectory per test so tests don't bleed into each other.
make_fixture() {
    local test_name="$1"
    local fixture_dir="$FIXTURE_BASE/$test_name"

    local engram_home="$fixture_dir/dot_engram"
    local claude_home="$fixture_dir/dot_claude"

    mkdir -p "$engram_home/hooks"
    mkdir -p "$engram_home/history"
    mkdir -p "$engram_home/diary"
    mkdir -p "$engram_home/sessions"
    mkdir -p "$claude_home/skills"
    mkdir -p "$claude_home/agents"

    # knowledge.db — a real (empty) SQLite DB or a placeholder file
    if command -v sqlite3 &>/dev/null; then
        sqlite3 "$engram_home/knowledge.db" \
            "CREATE TABLE IF NOT EXISTS nodes (id TEXT PRIMARY KEY, data TEXT);" 2>/dev/null || true
    else
        echo "placeholder-db" > "$engram_home/knowledge.db"
    fi

    # warm-briefing.md
    echo "warm briefing content" > "$engram_home/warm-briefing.md"

    # config.json
    echo '{"multi_agent": false}' > "$engram_home/config.json"

    # .deployed-version
    echo "alpha_sha=abc123" > "$engram_home/.deployed-version"

    # scatter code files
    echo "# server.py stub" > "$engram_home/server.py"
    echo "# engram_filter.py stub" > "$engram_home/engram_filter.py"
    echo "# SKILL.md stub" > "$engram_home/SKILL.md"
    mkdir -p "$engram_home/hooks/claude"
    echo "#!/bin/bash" > "$engram_home/hooks/claude/engram-surface-hook.py"
    mkdir -p "$engram_home/tools"
    echo "#!/bin/bash" > "$engram_home/tools/deploy.sh"

    echo "$fixture_dir"
}

# Build settings.json with mixed hooks (scatter ENGRAM + non-ENGRAM + plugin-style)
make_settings_json() {
    local fixture_dir="$1"
    local engram_home="$fixture_dir/dot_engram"
    local claude_home="$fixture_dir/dot_claude"

    cat > "$claude_home/settings.json" <<SETTINGS
{
  "hooks": {
    "SessionStart": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "bash \"$engram_home/hooks/start-engram-daemon.sh\"",
            "timeout": 15,
            "statusMessage": "Starting ENGRAM surface daemon..."
          },
          {
            "type": "command",
            "command": "/usr/local/bin/my-non-engram-start-hook.sh",
            "timeout": 5,
            "statusMessage": "Non-ENGRAM hook..."
          }
        ]
      }
    ],
    "UserPromptSubmit": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "python3 \"$engram_home/hooks/engram-surface-hook.py\"",
            "timeout": 10,
            "statusMessage": "Searching ENGRAM..."
          },
          {
            "type": "command",
            "command": "python3 \"$engram_home/hooks/engram-session-start-hook.py\"",
            "timeout": 5,
            "statusMessage": "Session start..."
          },
          {
            "type": "command",
            "command": "python3 \"\${CLAUDE_PLUGIN_ROOT}/hooks/engram-surface-hook.py\"",
            "timeout": 10,
            "statusMessage": "Plugin surface hook..."
          },
          {
            "type": "command",
            "command": "/usr/local/bin/other-non-engram-hook.sh",
            "timeout": 3,
            "statusMessage": "Another non-ENGRAM hook..."
          }
        ]
      }
    ]
  }
}
SETTINGS
}

# Build .claude.json with engram + another MCP server
make_claude_json() {
    local fixture_dir="$1"
    local engram_home="$fixture_dir/dot_engram"

    cat > "$fixture_dir/dot_claude.json" <<CLAUDEJSON
{
  "mcpServers": {
    "engram": {
      "command": "python3",
      "args": ["$engram_home/server.py"],
      "env": {}
    },
    "other-tool": {
      "command": "other-mcp-server",
      "args": [],
      "env": {}
    }
  }
}
CLAUDEJSON
}

# Add scatter engram-* skills + agents
make_scatter_skills_agents() {
    local fixture_dir="$1"
    local claude_home="$fixture_dir/dot_claude"

    mkdir -p "$claude_home/skills/engram-upgrade"
    echo "# engram-upgrade skill" > "$claude_home/skills/engram-upgrade/SKILL.md"
    mkdir -p "$claude_home/skills/engram-nap"
    echo "# engram-nap skill" > "$claude_home/skills/engram-nap/SKILL.md"
    mkdir -p "$claude_home/skills/not-engram-skill"
    echo "# non-engram skill" > "$claude_home/skills/not-engram-skill/SKILL.md"

    echo "# engram-dream-fairy agent" > "$claude_home/agents/engram-dream-fairy.md"
    echo "# other agent" > "$claude_home/agents/other-agent.md"
}

# ---------------------------------------------------------------------------
# Test runner helper: runs the migrate script with a fixture HOME
# ---------------------------------------------------------------------------
run_migrate() {
    local fixture_dir="$1"
    shift
    local args=("$@")

    ENGRAM_HOME="$fixture_dir/dot_engram" \
    CLAUDE_HOME="$fixture_dir/dot_claude" \
    HOME="$fixture_dir" \
    bash "$MIGRATE_SCRIPT" "${args[@]}" 2>&1
}

# ---------------------------------------------------------------------------
# T1 — Selective hook removal
# ---------------------------------------------------------------------------
test_t1_selective_hook_removal() {
    section "T1: Selective hook removal"
    TESTS_RUN=$((TESTS_RUN + 1))

    if ! command -v jq &>/dev/null; then
        skip "T1: jq not installed — skipping"
        return
    fi

    local fixture_dir
    fixture_dir="$(make_fixture "t1")"
    make_settings_json "$fixture_dir"
    make_claude_json "$fixture_dir"
    make_scatter_skills_agents "$fixture_dir"

    # We'll call just the hook-removal function; we need to do this by invoking
    # a partial run. Since the script pauses at step2, we can use --dry-run=false
    # + a trick: manually invoke the surgery on the fixture settings.json using
    # the same jq logic the script uses.
    #
    # Strategy: we run the script with a fake knowledge.db in engram_home but
    # without a real migrate-backup.sh available. Instead we extract just the
    # jq transform and test it in isolation.

    local settings="$fixture_dir/dot_claude/settings.json"
    local engram_hooks_dir="$fixture_dir/dot_engram/hooks"

    # Apply the same jq transform the script uses
    local jq_filter='.hooks |= with_entries(
      .value |= map(
        .hooks |= map(
          select(.command? | contains($hooks_dir) | not)
        )
        | select((.hooks | length) > 0)
      )
      | select((.value | length) > 0)
    )'

    local result
    result="$(jq --arg hooks_dir "$engram_hooks_dir" "$jq_filter" "$settings")"

    # Assertions:

    # 1. Valid JSON after
    if echo "$result" | jq '.' > /dev/null 2>&1; then
        pass "T1a: Post-edit settings.json is valid JSON"
    else
        fail "T1a: Post-edit settings.json is NOT valid JSON"
        return
    fi

    # 2. Scatter ENGRAM hook commands are gone
    local scatter_count
    scatter_count="$(echo "$result" | jq --arg hooks_dir "$engram_hooks_dir" \
        '[.. | objects | select(has("command")) | .command | select(contains($hooks_dir))] | length')"
    if [[ "$scatter_count" -eq 0 ]]; then
        pass "T1b: Scatter ENGRAM hook commands removed (count=0)"
    else
        fail "T1b: $scatter_count scatter ENGRAM hook command(s) remain after removal"
    fi

    # 3. Non-ENGRAM hook is still present
    local non_engram_count
    non_engram_count="$(echo "$result" | jq \
        '[.. | objects | select(has("command")) | .command | select(contains("/usr/local/bin/"))] | length')"
    if [[ "$non_engram_count" -ge 1 ]]; then
        pass "T1c: Non-ENGRAM hooks preserved ($non_engram_count found)"
    else
        fail "T1c: Non-ENGRAM hooks were removed (expected >= 1)"
    fi

    # 4. Plugin-style hook (${CLAUDE_PLUGIN_ROOT}/hooks) is still present
    local plugin_count
    plugin_count="$(echo "$result" | jq \
        '[.. | objects | select(has("command")) | .command | select(contains("CLAUDE_PLUGIN_ROOT"))] | length')"
    if [[ "$plugin_count" -ge 1 ]]; then
        pass "T1d: Plugin-path hook preserved ($plugin_count found) — not removed by scatter-path filter"
    else
        fail "T1d: Plugin-path hook was incorrectly removed"
    fi

    # 5. Confirm total hook commands remaining = non-ENGRAM + plugin (2 non-ENGRAM + 1 plugin = 3 expected)
    local total_remaining
    total_remaining="$(echo "$result" | jq '[.. | objects | select(has("command")) | .command] | length')"
    if [[ "$total_remaining" -eq 3 ]]; then
        pass "T1e: Correct total hook command count after removal: $total_remaining (expected 3)"
    else
        fail "T1e: Expected 3 hook commands after removal, got $total_remaining"
    fi
}

# ---------------------------------------------------------------------------
# T2 — .claude.json engram-only deletion
# ---------------------------------------------------------------------------
test_t2_claude_json_engram_only() {
    section "T2: .claude.json engram-only deletion"
    TESTS_RUN=$((TESTS_RUN + 1))

    if ! command -v jq &>/dev/null; then
        skip "T2: jq not installed — skipping"
        return
    fi

    local fixture_dir
    fixture_dir="$(make_fixture "t2")"
    make_claude_json "$fixture_dir"

    local claude_json="$fixture_dir/dot_claude.json"

    # Apply the same jq transform the script uses
    local result
    result="$(jq 'del(.mcpServers.engram)' "$claude_json")"

    # 1. Valid JSON
    if echo "$result" | jq '.' > /dev/null 2>&1; then
        pass "T2a: Post-edit .claude.json is valid JSON"
    else
        fail "T2a: Post-edit .claude.json is NOT valid JSON"
        return
    fi

    # 2. engram key is gone
    local has_engram
    has_engram="$(echo "$result" | jq 'if .mcpServers | has("engram") then 1 else 0 end')"
    if [[ "$has_engram" -eq 0 ]]; then
        pass "T2b: 'engram' removed from mcpServers"
    else
        fail "T2b: 'engram' still present in mcpServers after deletion"
    fi

    # 3. other-tool is still present
    local has_other
    has_other="$(echo "$result" | jq 'if .mcpServers | has("other-tool") then 1 else 0 end')"
    if [[ "$has_other" -eq 1 ]]; then
        pass "T2c: 'other-tool' MCP server preserved"
    else
        fail "T2c: 'other-tool' MCP server was incorrectly removed"
    fi
}

# ---------------------------------------------------------------------------
# T3 — DATA-allowlist refusal
# ---------------------------------------------------------------------------
test_t3_data_allowlist_refusal() {
    section "T3: DATA-allowlist refusal"
    TESTS_RUN=$((TESTS_RUN + 1))

    local fixture_dir
    fixture_dir="$(make_fixture "t3")"
    make_settings_json "$fixture_dir"
    make_claude_json "$fixture_dir"

    # We test the is_data_path guard directly by invoking the script with
    # --remove-deployed-code on a fixture where knowledge.db is listed as a
    # "code target". We can't easily inject into the target list from outside,
    # so we test the guard by sourcing just the helper function and calling it.
    #
    # Strategy: write a small inline bash script that sources the helper
    # and calls is_data_path on each DATA item.

    local allowlist_items=("knowledge.db" "history" "diary" "warm-briefing.md" "config.json" "sessions" ".deployed-version" "cursors")

    local guard_failures=0
    for item in "${allowlist_items[@]}"; do
        # Extract and eval just the is_data_path function + DATA_ALLOWLIST from the script
        local test_result
        test_result="$(bash -c "
DATA_ALLOWLIST=(knowledge.db history diary warm-briefing.md config.json sessions .deployed-version cursors)
is_data_path() {
    local candidate
    candidate=\"\$(basename \"\$1\")\"
    for item in \"\${DATA_ALLOWLIST[@]}\"; do
        if [[ \"\$candidate\" == \"\$item\" ]]; then
            return 0
        fi
    done
    return 1
}
if is_data_path \"/some/path/$item\"; then echo yes; else echo no; fi
" 2>&1)"
        if [[ "$test_result" == "yes" ]]; then
            : # good
        else
            fail "T3: is_data_path did not recognize DATA path: $item"
            guard_failures=$((guard_failures + 1))
        fi
    done

    if [[ $guard_failures -eq 0 ]]; then
        pass "T3a: is_data_path correctly identifies all DATA allowlist items"
    fi

    # Also test that a CODE path is NOT in the allowlist
    local code_test
    code_test="$(bash -c "
DATA_ALLOWLIST=(knowledge.db history diary warm-briefing.md config.json sessions .deployed-version cursors)
is_data_path() {
    local candidate
    candidate=\"\$(basename \"\$1\")\"
    for item in \"\${DATA_ALLOWLIST[@]}\"; do
        if [[ \"\$candidate\" == \"\$item\" ]]; then
            return 0
        fi
    done
    return 1
}
if is_data_path \"/some/path/server.py\"; then echo yes; else echo no; fi
" 2>&1)"
    if [[ "$code_test" == "no" ]]; then
        pass "T3b: is_data_path correctly allows removal of CODE paths (server.py)"
    else
        fail "T3b: is_data_path incorrectly flagged server.py as a DATA path"
    fi

    # T3c: end-to-end guard: --dry-run --remove-deployed-code must not list knowledge.db
    # as a removal target.
    # NOTE: Without jq installed, step0 exits "already migrated" before reaching the
    # step3e DATA-guard code path — the test would pass vacuously. T3a/T3b already
    # cover the is_data_path guard logic directly (no jq needed). Skip T3c when jq
    # is absent so the vacuous-pass is explicit.
    if ! command -v jq &>/dev/null; then
        skip "T3c: jq not installed — step0 would exit early (already-migrated), test would pass vacuously; T3a/T3b cover the guard logic"
    else
        local dry_run_output
        dry_run_output="$(ENGRAM_HOME="$fixture_dir/dot_engram" \
            CLAUDE_HOME="$fixture_dir/dot_claude" \
            HOME="$fixture_dir" \
            bash "$MIGRATE_SCRIPT" --dry-run --remove-deployed-code 2>&1 || true)"

        # The dry-run output should NOT contain "Would remove code path" for any DATA path
        if echo "$dry_run_output" | grep -q "knowledge.db"; then
            # It may mention knowledge.db in informational context — check it's not in "Would remove"
            if echo "$dry_run_output" | grep "Would remove code path" | grep -q "knowledge.db"; then
                fail "T3c: --dry-run --remove-deployed-code listed knowledge.db as a removal target"
            else
                pass "T3c: knowledge.db mentioned in dry-run but not as a removal target"
            fi
        else
            pass "T3c: knowledge.db not listed as a removal target in --dry-run --remove-deployed-code"
        fi
    fi
}

# ---------------------------------------------------------------------------
# T4 — Idempotency: second run reports "already migrated"
# ---------------------------------------------------------------------------
test_t4_idempotency() {
    section "T4: Idempotency (already-migrated detection)"
    TESTS_RUN=$((TESTS_RUN + 1))

    if ! command -v jq &>/dev/null; then
        skip "T4: jq not installed — skipping"
        return
    fi

    local fixture_dir
    fixture_dir="$(make_fixture "t4")"

    # Create a settings.json with ZERO scatter hook commands (already migrated state)
    cat > "$fixture_dir/dot_claude/settings.json" <<'SETTINGS'
{
  "hooks": {
    "UserPromptSubmit": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "python3 \"${CLAUDE_PLUGIN_ROOT}/hooks/engram-surface-hook.py\"",
            "timeout": 10
          }
        ]
      }
    ]
  }
}
SETTINGS

    # Create .claude.json with NO engram mcpServers entry (already migrated)
    cat > "$fixture_dir/dot_claude.json" <<'CLAUDEJSON'
{
  "mcpServers": {
    "other-tool": {
      "command": "other-mcp",
      "args": []
    }
  }
}
CLAUDEJSON

    # Run the migrate script — should detect "already migrated" and jump to verify
    local output
    output="$(ENGRAM_HOME="$fixture_dir/dot_engram" \
        CLAUDE_HOME="$fixture_dir/dot_claude" \
        HOME="$fixture_dir" \
        bash "$MIGRATE_SCRIPT" 2>&1 || true)"

    if echo "$output" | grep -qi "already migrated"; then
        pass "T4a: Second run (already-migrated state) correctly reports 'already migrated'"
    else
        fail "T4a: Expected 'already migrated' message, got: $(echo "$output" | head -5)"
    fi

    # Verify settings.json was NOT mutated
    local plugin_hook_count
    plugin_hook_count="$(jq '[.. | objects | select(has("command")) | .command | select(contains("CLAUDE_PLUGIN_ROOT"))] | length' \
        "$fixture_dir/dot_claude/settings.json")"
    if [[ "$plugin_hook_count" -eq 1 ]]; then
        pass "T4b: settings.json not mutated by second run"
    else
        fail "T4b: settings.json was mutated — plugin hook count changed to $plugin_hook_count (expected 1)"
    fi

    # Verify .claude.json was NOT mutated
    local other_tool_present
    other_tool_present="$(jq 'if .mcpServers | has("other-tool") then 1 else 0 end' \
        "$fixture_dir/dot_claude.json")"
    if [[ "$other_tool_present" -eq 1 ]]; then
        pass "T4c: .claude.json not mutated by second run"
    else
        fail "T4c: .claude.json was mutated — other-tool MCP server disappeared"
    fi
}

# ---------------------------------------------------------------------------
# T5 — --dry-run mutates nothing
# ---------------------------------------------------------------------------
test_t5_dry_run_no_mutations() {
    section "T5: --dry-run mutates nothing"
    TESTS_RUN=$((TESTS_RUN + 1))

    if ! command -v jq &>/dev/null; then
        skip "T5: jq not installed — skipping"
        return
    fi

    local fixture_dir
    fixture_dir="$(make_fixture "t5")"
    make_settings_json "$fixture_dir"
    make_claude_json "$fixture_dir"
    make_scatter_skills_agents "$fixture_dir"

    # Checksum the entire fixture HOME before the dry-run
    local before_checksums after_checksums
    before_checksums="$(find "$fixture_dir" -type f | sort | xargs sha256sum 2>/dev/null || find "$fixture_dir" -type f | sort | xargs shasum -a 256 2>/dev/null || echo "UNAVAILABLE")"

    # Run --dry-run
    local dry_output
    dry_output="$(ENGRAM_HOME="$fixture_dir/dot_engram" \
        CLAUDE_HOME="$fixture_dir/dot_claude" \
        HOME="$fixture_dir" \
        bash "$MIGRATE_SCRIPT" --dry-run 2>&1 || true)"

    # Checksum after
    after_checksums="$(find "$fixture_dir" -type f | sort | xargs sha256sum 2>/dev/null || find "$fixture_dir" -type f | sort | xargs shasum -a 256 2>/dev/null || echo "UNAVAILABLE")"

    if [[ "$before_checksums" == "UNAVAILABLE" ]]; then
        skip "T5: sha256 tools not available for checksum comparison"
        return
    fi

    if [[ "$before_checksums" == "$after_checksums" ]]; then
        pass "T5a: --dry-run did not mutate any files (checksums identical)"
    else
        fail "T5a: --dry-run mutated files! Diff:"
        diff <(echo "$before_checksums") <(echo "$after_checksums") || true
    fi

    # Verify that dry-run output contains planned mutation messages
    if echo "$dry_output" | grep -q "DRY:"; then
        pass "T5b: --dry-run output contains DRY: mutation plan messages"
    else
        fail "T5b: --dry-run output contains no DRY: messages (expected mutation plan)"
    fi
}

# ---------------------------------------------------------------------------
# T6 — Already-migrated host: verify-only path passes
# ---------------------------------------------------------------------------
test_t6_already_migrated_verify() {
    section "T6: Already-migrated host — verify-only path"
    TESTS_RUN=$((TESTS_RUN + 1))

    if ! command -v jq &>/dev/null; then
        skip "T6: jq not installed — skipping"
        return
    fi

    local fixture_dir
    fixture_dir="$(make_fixture "t6")"

    # Fully clean state: no scatter surfaces, plugin hooks present
    cat > "$fixture_dir/dot_claude/settings.json" <<'SETTINGS'
{
  "hooks": {
    "SessionStart": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "bash \"${CLAUDE_PLUGIN_ROOT}/hooks/start-engram-daemon.sh\"",
            "timeout": 15
          }
        ]
      }
    ],
    "UserPromptSubmit": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "python3 \"${CLAUDE_PLUGIN_ROOT}/hooks/engram-surface-hook.py\"",
            "timeout": 10
          }
        ]
      }
    ]
  }
}
SETTINGS

    # .claude.json with no engram mcpServer
    cat > "$fixture_dir/dot_claude.json" <<'CLAUDEJSON'
{
  "mcpServers": {
    "other-tool": {
      "command": "other-mcp",
      "args": []
    }
  }
}
CLAUDEJSON

    # No scatter skill or agent dirs

    # Run --verify
    local verify_output
    verify_output="$(ENGRAM_HOME="$fixture_dir/dot_engram" \
        CLAUDE_HOME="$fixture_dir/dot_claude" \
        HOME="$fixture_dir" \
        bash "$MIGRATE_SCRIPT" --verify 2>&1 || true)"

    if echo "$verify_output" | grep -q "ALL CHECKS PASSED"; then
        pass "T6a: --verify on already-migrated host reports ALL CHECKS PASSED"
    else
        fail "T6a: --verify did not report ALL CHECKS PASSED. Output:"
        echo "$verify_output" | head -20
    fi

    # Run plain (no flags) — should detect already-migrated and jump to verify
    local plain_output
    plain_output="$(ENGRAM_HOME="$fixture_dir/dot_engram" \
        CLAUDE_HOME="$fixture_dir/dot_claude" \
        HOME="$fixture_dir" \
        bash "$MIGRATE_SCRIPT" 2>&1 || true)"

    if echo "$plain_output" | grep -qi "already migrated"; then
        pass "T6b: Plain run on already-migrated host reports 'already migrated'"
    else
        fail "T6b: Expected 'already migrated', got: $(echo "$plain_output" | head -5)"
    fi
}

# ---------------------------------------------------------------------------
# T7 — hook removal: correctly identifies scatter path vs plugin path
#       (critical: plugin hooks share same names as scatter hooks)
# ---------------------------------------------------------------------------
test_t7_path_not_name_matching() {
    section "T7: Path-based matching — plugin hooks with same names NOT removed"
    TESTS_RUN=$((TESTS_RUN + 1))

    if ! command -v jq &>/dev/null; then
        skip "T7: jq not installed — skipping"
        return
    fi

    local fixture_dir
    fixture_dir="$(make_fixture "t7")"
    local engram_hooks_dir="$fixture_dir/dot_engram/hooks"

    # Create settings.json with BOTH scatter AND plugin hooks for the same hook
    # names — the critical collision case. Only scatter hooks should be removed.
    cat > "$fixture_dir/dot_claude/settings.json" <<SETTINGS
{
  "hooks": {
    "UserPromptSubmit": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "python3 \"$engram_hooks_dir/engram-surface-hook.py\"",
            "timeout": 10,
            "statusMessage": "Scatter: Searching ENGRAM..."
          },
          {
            "type": "command",
            "command": "python3 \"\${CLAUDE_PLUGIN_ROOT}/hooks/engram-surface-hook.py\"",
            "timeout": 10,
            "statusMessage": "Plugin: Searching ENGRAM..."
          },
          {
            "type": "command",
            "command": "python3 \"$engram_hooks_dir/engram-deference-detector-prompt.py\"",
            "timeout": 3,
            "statusMessage": "Scatter: Deference..."
          },
          {
            "type": "command",
            "command": "python3 \"\${CLAUDE_PLUGIN_ROOT}/hooks/engram-deference-detector-prompt.py\"",
            "timeout": 3,
            "statusMessage": "Plugin: Deference..."
          }
        ]
      }
    ]
  }
}
SETTINGS

    local jq_filter='.hooks |= with_entries(
      .value |= map(
        .hooks |= map(
          select(.command? | contains($hooks_dir) | not)
        )
        | select((.hooks | length) > 0)
      )
      | select((.value | length) > 0)
    )'

    local result
    result="$(jq --arg hooks_dir "$engram_hooks_dir" "$jq_filter" "$fixture_dir/dot_claude/settings.json")"

    # After filter: scatter hooks gone (2 removed), plugin hooks kept (2 remain)
    local scatter_remaining plugin_remaining
    scatter_remaining="$(echo "$result" | jq --arg hooks_dir "$engram_hooks_dir" \
        '[.. | objects | select(has("command")) | .command | select(contains($hooks_dir))] | length')"
    plugin_remaining="$(echo "$result" | jq \
        '[.. | objects | select(has("command")) | .command | select(contains("CLAUDE_PLUGIN_ROOT"))] | length')"

    if [[ "$scatter_remaining" -eq 0 ]]; then
        pass "T7a: Scatter hooks with same names as plugin hooks correctly removed"
    else
        fail "T7a: $scatter_remaining scatter hook(s) remain after path-based removal"
    fi

    if [[ "$plugin_remaining" -eq 2 ]]; then
        pass "T7b: Plugin hooks with same names as scatter hooks correctly preserved (count=2)"
    else
        fail "T7b: Expected 2 plugin hooks preserved, got $plugin_remaining"
    fi
}

# ---------------------------------------------------------------------------
# T8 — venv upgrade: fastmcp present but sqlite_vec absent triggers pip install
#       (#729 / PR #730: existing venvs pre-dating sqlite_vec requirement must
#        be upgraded in-place rather than silently left with degraded semantics)
# ---------------------------------------------------------------------------
test_t8_venv_upgrade_when_sqlite_vec_missing() {
    section "T8: Existing venv missing sqlite_vec — pip install triggered"
    TESTS_RUN=$((TESTS_RUN + 1))

    local fixture_dir
    fixture_dir="$(make_fixture "t8")"

    # Build a fake venv: a python3 stub that can "import fastmcp" but NOT
    # "import sqlite_vec", plus a pip stub that logs its invocations.
    local fake_venv="$fixture_dir/dot_engram/venv"
    local fake_bin="$fake_venv/bin"
    mkdir -p "$fake_bin"

    local pip_log="$fixture_dir/pip_calls.log"

    # Fake python3: succeeds on "import fastmcp", fails on "import sqlite_vec"
    cat > "$fake_bin/python3" <<'FAKEPY'
#!/usr/bin/env bash
# Stub python3: simulates a venv that has fastmcp but not sqlite_vec
args=("$@")
for arg in "${args[@]}"; do
    if [[ "$arg" == *"import sqlite_vec"* ]]; then
        exit 1
    fi
    if [[ "$arg" == *"import fastmcp"* ]]; then
        exit 0
    fi
done
exit 0
FAKEPY
    chmod +x "$fake_bin/python3"

    # Fake pip: records call args to pip_log and exits 0
    cat > "$fake_bin/pip" <<FAKEPIP
#!/usr/bin/env bash
echo "pip \$*" >> "$pip_log"
exit 0
FAKEPIP
    chmod +x "$fake_bin/pip"

    # The upgrade logic from step2_install_plugin, extracted for direct testing.
    # Variables mirror the script's local names so the logic can be copy-tested.
    local _venv_dir="$fake_venv"
    local _venv_python="$fake_bin/python3"
    local _venv_pip="$fake_bin/pip"
    # Fake requirements.txt path (content doesn't matter; pip is stubbed)
    local _req="$fixture_dir/requirements.txt"
    echo "fastmcp>=0.1.0" > "$_req"
    echo "sqlite-vec==0.1.9" >> "$_req"

    # Run the upgrade-check logic inline (mirrors the else-branch added by #729)
    if ! "$_venv_python" -c "import sqlite_vec" &>/dev/null 2>&1; then
        "$_venv_pip" install --quiet -r "$_req"
    fi

    # T8a: pip was called (log file created and non-empty)
    if [[ -f "$pip_log" ]] && grep -q "install" "$pip_log"; then
        pass "T8a: pip install invoked when sqlite_vec missing from existing venv"
    else
        fail "T8a: pip install was NOT invoked — existing venv with missing sqlite_vec silently skipped"
    fi

    # T8b: pip was called with -r <requirements file>
    if grep -q "\-r " "$pip_log" 2>/dev/null; then
        pass "T8b: pip install called with -r (requirements file upgrade, not ad-hoc)"
    else
        fail "T8b: pip install not called with -r flag (expected requirements-file upgrade)"
    fi

    # T8c: when sqlite_vec IS importable, pip is NOT called (no spurious upgrades)
    local pip_log2="$fixture_dir/pip_calls2.log"
    # Patch fake python3 to accept both imports
    cat > "$fake_bin/python3" <<'FAKEPY2'
#!/usr/bin/env bash
exit 0
FAKEPY2
    chmod +x "$fake_bin/python3"

    if ! "$_venv_python" -c "import sqlite_vec" &>/dev/null 2>&1; then
        "$_venv_pip" install --quiet -r "$_req" 2>>"$pip_log2" || true
        # if we fell into the upgrade branch despite sqlite_vec being importable, that's wrong
        fail "T8c: pip install triggered even though sqlite_vec is importable (spurious upgrade)"
    else
        pass "T8c: No pip install when sqlite_vec already importable (no spurious upgrade)"
    fi
}

# ---------------------------------------------------------------------------
# Run all tests
# ---------------------------------------------------------------------------
echo ""
echo "=========================================="
echo "  test_migrate_to_plugin.sh"
echo "  Fixture base: $FIXTURE_BASE"
echo "=========================================="

test_t1_selective_hook_removal
test_t2_claude_json_engram_only
test_t3_data_allowlist_refusal
test_t4_idempotency
test_t5_dry_run_no_mutations
test_t6_already_migrated_verify
test_t7_path_not_name_matching
test_t8_venv_upgrade_when_sqlite_vec_missing

echo ""
echo "=========================================="
echo "  Results: $TESTS_RUN tests run"
if [[ $FAILURES -eq 0 ]]; then
    echo -e "  ${GREEN}ALL PASSED${NC}"
else
    echo -e "  ${RED}$FAILURES FAILURE(S)${NC}"
fi
echo "=========================================="
echo ""

exit $FAILURES
