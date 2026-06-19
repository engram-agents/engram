#!/usr/bin/env bash
# migrate-to-plugin.sh — scatter→plugin migration orchestrator (part of #581)
#
# Migrates an existing ENGRAM "scatter" install (code+hooks in ~/.engram/,
# hook block in settings.json, MCP in .claude.json) to the Claude Code plugin
# delivery model, while preserving the knowledge graph and all data.
#
# USAGE:
#   tools/migrate-to-plugin.sh [OPTIONS]
#
# OPTIONS:
#   --dry-run               Print every planned mutation without applying.
#   --resume                Skip Steps 0-2; resume from Step 3 (scatter removal).
#                           Use after completing the /plugin install step.
#   --verify                Run Step 5 verification only (post-restart check).
#   --remove-deployed-code  Also remove scatter CODE files from ~/.engram/
#                           (server.py, engram_*.py, SKILL.md, hooks/, tools/).
#                           Default: OFF. See DATA vs CODE section in --help.
#   --help                  Show this help.
#
# MIGRATION STATE:
#   ~/.engram/.migration-state  Written after Step 2 to support --resume.
#   Removed on successful completion.
#
# ENV VARS:
#   ENGRAM_HOME   ENGRAM data/code dir  (default: ~/.engram)
#   CLAUDE_HOME   Claude config dir     (default: ~/.claude)
#
# SAFETY PROPERTIES:
#   - Backup-first: no scatter surface is removed before Step 1 backup succeeds.
#   - Fail-loud: exits non-zero on any mutation failure.
#   - Idempotent: --verify / second full run detect "already migrated" state.
#   - DATA allowlist: knowledge.db, history/, diary/, warm-briefing.md,
#     config.json, sessions/, .deployed-version, cursors/ are NEVER deleted
#     (even with --remove-deployed-code) — see DATA_ALLOWLIST for the source of truth.
#   - set -euo pipefail throughout.
#
# DATA vs CODE (--remove-deployed-code):
#   After migration, the plugin cache provides all CODE surfaces (server.py,
#   hooks, skills, agents). Scatter CODE files in ~/.engram/ become inert
#   because hooks and MCP now point at the plugin. Removing them is tidiness,
#   not correctness. Default is to LEAVE them (safe + reversible).
#
#   With --remove-deployed-code the script removes:
#     ~/.engram/server.py
#     ~/.engram/engram_*.py
#     ~/.engram/SKILL.md
#     ~/.engram/hooks/
#     ~/.engram/tools/
#   It will NEVER remove DATA paths (see DATA allowlist above).
#
# PROCEDURE (steps):
#   0. Pre-flight + idempotency check
#   1. Backup (delegates to tools/migrate-backup.sh)
#   2. Install plugin surfaces (build + marketplace + PAUSE for /plugin install)
#   3. Remove scatter surfaces (hooks, MCP server entry, skills, agents)
#   4. Assert knowledge.db integrity (plugin did NOT clobber it)
#   5. PAUSE for MCP restart + run verify checks
#
# To revert at any point before Step 5:
#   tools/migrate-backup.sh restore <backup-dir>
#   (Also run: /plugin uninstall engram   — the script cannot do that step)

set -euo pipefail

# ---------------------------------------------------------------------------
# Resolve env paths
# ---------------------------------------------------------------------------
ENGRAM_HOME="${ENGRAM_HOME:-$HOME/.engram}"
CLAUDE_HOME="${CLAUDE_HOME:-$HOME/.claude}"

MIGRATION_STATE_FILE="$ENGRAM_HOME/.migration-state"

# ---------------------------------------------------------------------------
# Portability helpers (same style as migrate-backup.sh)
# ---------------------------------------------------------------------------

sha256_file() {
    local f="$1"
    if command -v sha256sum &>/dev/null; then
        sha256sum "$f" | awk '{print $1}'
    elif command -v shasum &>/dev/null; then
        shasum -a 256 "$f" | awk '{print $1}'
    else
        echo "UNAVAILABLE"
    fi
}

file_inode() {
    # Portable: stat -c on Linux, stat -f on macOS.
    local f="$1"
    if stat --version &>/dev/null 2>&1; then
        stat -c '%i' "$f"
    else
        stat -f '%i' "$f"
    fi
}

file_mtime() {
    local f="$1"
    if stat --version &>/dev/null 2>&1; then
        stat -c '%Y' "$f"
    else
        stat -f '%m' "$f"
    fi
}

# ---------------------------------------------------------------------------
# Logging helpers
# ---------------------------------------------------------------------------
info()    { echo "INFO:  $*"; }
ok()      { echo "OK:    $*"; }
warn()    { echo "WARN:  $*" >&2; }
err()     { echo "ERROR: $*" >&2; exit 1; }
dry()     { echo "DRY:   $*"; }
section() { echo ""; echo "=== $* ==="; echo ""; }
pause()   { echo ""; echo ">>> PAUSE: $*"; echo ""; }

# ---------------------------------------------------------------------------
# Arg parsing
# ---------------------------------------------------------------------------
DRY_RUN=0
RESUME=0
VERIFY_ONLY=0
REMOVE_CODE=0

for arg in "$@"; do
    case "$arg" in
        --dry-run)              DRY_RUN=1 ;;
        --resume)               RESUME=1 ;;
        --verify)               VERIFY_ONLY=1 ;;
        --remove-deployed-code) REMOVE_CODE=1 ;;
        --help|-h)
            cat <<'EOF'
migrate-to-plugin.sh — ENGRAM scatter→plugin migration orchestrator (#581)

USAGE:
  tools/migrate-to-plugin.sh [OPTIONS]

OPTIONS:
  --dry-run               Print every planned mutation without applying.
  --resume                Skip Steps 0-2; resume from Step 3 (scatter removal).
                          Use after completing the /plugin install step inside Claude Code.
  --verify                Run Step 5 verification only (post-restart check).
  --remove-deployed-code  Also remove scatter CODE files from ~/.engram/
                          (server.py, engram_*.py, SKILL.md, hooks/, tools/).
                          Default: OFF. Leaving stale code is safe — once hooks
                          and MCP point at the plugin, the scatter code is inert.
  --help                  Show this help and exit.

ENV VARS:
  ENGRAM_HOME   ENGRAM data/code dir  (default: ~/.engram)
  CLAUDE_HOME   Claude config dir     (default: ~/.claude)

PROCEDURE:
  Step 0   Pre-flight: verify scatter install exists; check already-migrated.
  Step 1   Backup: delegate to tools/migrate-backup.sh (hard gate — no removal
           before backup succeeds).
  Step 2   Install plugin: build + marketplace + PAUSE for /plugin install.
           *** Re-invoke with --resume after completing the /plugin install. ***
  Step 3   Remove scatter: settings.json hook block, .claude.json engram entry,
           ~/.claude/skills/engram-*, ~/.claude/agents/engram-*
           [+ ~/.engram code if --remove-deployed-code].
  Step 4   Assert knowledge.db not clobbered by plugin install.
  Step 5   PAUSE for MCP restart, then run --verify to confirm clean migration.

DATA allowlist (NEVER deleted, even with --remove-deployed-code):
  knowledge.db   history/   diary/   warm-briefing.md
  config.json    sessions/  .deployed-version   cursors/

To revert at any point:
  tools/migrate-backup.sh restore <backup-dir>
  (Also run: /plugin uninstall engram  — separate operator step)

Part of issue #581.
EOF
            exit 0
            ;;
        *) err "Unknown argument: $arg — run with --help for usage." ;;
    esac
done

# ---------------------------------------------------------------------------
# Resolve repo root (for composing existing tools)
# ---------------------------------------------------------------------------
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

if [[ ! -f "$REPO_ROOT/tools/migrate-backup.sh" ]]; then
    err "Could not locate tools/migrate-backup.sh at $REPO_ROOT/tools/migrate-backup.sh. Run from the engram-alpha repo root."
fi

# ---------------------------------------------------------------------------
# DATA allowlist — paths under ENGRAM_HOME that are NEVER deleted.
# This list is a delete-refusing guard: any path matching it is skipped
# (and an error is raised if the --remove-deployed-code path tries to delete it).
# ---------------------------------------------------------------------------
DATA_ALLOWLIST=(
    "knowledge.db"
    "history"
    "diary"
    "warm-briefing.md"
    "config.json"
    "sessions"
    ".deployed-version"
    "cursors"
)

is_data_path() {
    # Returns 0 (true) if the basename matches the allowlist.
    local candidate
    candidate="$(basename "$1")"
    for item in "${DATA_ALLOWLIST[@]}"; do
        if [[ "$candidate" == "$item" ]]; then
            return 0
        fi
    done
    return 1
}

# ---------------------------------------------------------------------------
# Scatter hooks path matcher
# We match on command-PATH substring: hooks under ~/.engram/hooks/ (or
# $ENGRAM_HOME/hooks/). Plugin hooks use ${CLAUDE_PLUGIN_ROOT}/hooks — a
# completely different path — so path-matching is safe and correct.
# ---------------------------------------------------------------------------
SCATTER_HOOKS_DIR="$ENGRAM_HOME/hooks"

# ---------------------------------------------------------------------------
# Migration state helpers
# ---------------------------------------------------------------------------
read_migration_state() {
    if [[ -f "$MIGRATION_STATE_FILE" ]]; then
        cat "$MIGRATION_STATE_FILE"
    else
        echo ""
    fi
}

write_migration_state() {
    local state="$1"
    if [[ $DRY_RUN -eq 0 ]]; then
        echo "$state" > "$MIGRATION_STATE_FILE"
    else
        dry "Would write migration state: $state → $MIGRATION_STATE_FILE"
    fi
}

clear_migration_state() {
    if [[ -f "$MIGRATION_STATE_FILE" ]] && [[ $DRY_RUN -eq 0 ]]; then
        rm -f "$MIGRATION_STATE_FILE"
    fi
}

# ---------------------------------------------------------------------------
# jq check (required for settings.json surgery)
# ---------------------------------------------------------------------------
require_jq() {
    if ! command -v jq &>/dev/null; then
        err "jq is required for settings.json surgery but is not installed. Install jq and retry."
    fi
}

# ---------------------------------------------------------------------------
# Step 0 — Pre-flight + idempotency check
# ---------------------------------------------------------------------------
step0_preflight() {
    section "Step 0: Pre-flight"

    # jq is required up-front. Step 2's plugin build reads multi_agent/install_tier
    # from config.json via jq to pass --multi-agent/--tier (#704); without jq the
    # config read is skipped and the build silently falls back to single-agent —
    # the exact regression #704 prevents. Fail loud here, before any build, rather
    # than at Step 3 (which still calls require_jq for the --resume path that skips
    # Step 0). require_jq is idempotent, so the double-call on the full path is safe.
    require_jq

    # Guard: scatter install must exist.
    if [[ ! -f "$ENGRAM_HOME/knowledge.db" ]]; then
        err "No scatter install found — $ENGRAM_HOME/knowledge.db does not exist.
  Nothing to migrate. For a fresh install, use tools/install-local-marketplace.sh
  followed by /plugin install engram inside Claude Code."
    fi
    ok "Scatter install confirmed: $ENGRAM_HOME/knowledge.db exists."

    # Idempotency check: are scatter surfaces already absent?
    local hooks_count=0
    local has_engram_mcp=0

    if [[ -f "$CLAUDE_HOME/settings.json" ]] && command -v jq &>/dev/null; then
        hooks_count="$(jq --arg hooks_dir "$SCATTER_HOOKS_DIR" \
            '[.. | objects | select(has("command")) | .command
              | select(contains($hooks_dir))] | length' \
            "$CLAUDE_HOME/settings.json" 2>/dev/null || echo 0)"
    fi

    # Check .claude.json for engram MCP entry
    local claude_json_path=""
    if [[ -f "$CLAUDE_HOME/.claude.json" ]]; then
        claude_json_path="$CLAUDE_HOME/.claude.json"
    elif [[ -f "$HOME/.claude.json" ]]; then
        claude_json_path="$HOME/.claude.json"
    fi

    if [[ -n "$claude_json_path" ]] && command -v jq &>/dev/null; then
        has_engram_mcp="$(jq 'if .mcpServers | has("engram") then 1 else 0 end' \
            "$claude_json_path" 2>/dev/null || echo 0)"
    fi

    if [[ "$hooks_count" -eq 0 && "$has_engram_mcp" -eq 0 ]]; then
        info "Scatter surfaces already absent (0 scatter hook commands, no engram mcpServer entry)."
        info "Already migrated — jumping to Step 5 verify."
        step5_verify
        exit 0
    fi

    info "Scatter surfaces present: $hooks_count scatter hook command(s), engram mcpServer: $has_engram_mcp."

    if [[ $DRY_RUN -eq 1 ]]; then
        dry "Pre-flight passed."
    else
        ok "Pre-flight passed."
    fi
}

# ---------------------------------------------------------------------------
# Step 1 — Backup (delegate to migrate-backup.sh)
# ---------------------------------------------------------------------------
step1_backup() {
    section "Step 1: Backup (delegate to migrate-backup.sh)"

    if [[ $DRY_RUN -eq 1 ]]; then
        dry "Would run: bash $REPO_ROOT/tools/migrate-backup.sh backup"
        dry "Hard gate: no scatter surface is removed before this succeeds."
        BACKUP_DIR="<backup-dir-would-be-created>"
        return
    fi

    info "Running migrate-backup.sh backup..."
    local backup_output
    backup_output="$(bash "$REPO_ROOT/tools/migrate-backup.sh" backup 2>&1)"
    local backup_exit=$?

    echo "$backup_output"

    if [[ $backup_exit -ne 0 ]]; then
        err "Backup FAILED (exit $backup_exit). Aborting before any removal.
No scatter surface has been touched. Investigate the backup failure above and retry."
    fi

    # Extract backup dir from the output.
    # migrate-backup.sh prints: "INFO: Backup dir: <path>" (using info() helper)
    BACKUP_DIR="$(echo "$backup_output" | grep 'Backup dir:' | awk '{print $NF}' || true)"

    if [[ -z "$BACKUP_DIR" ]] || [[ ! -d "$BACKUP_DIR" ]]; then
        warn "Could not parse backup dir from migrate-backup.sh output. Backup completed; locate it at \$HOME/.engram-migration-backups/."
        BACKUP_DIR="$HOME/.engram-migration-backups (most recent subdirectory)"
    fi

    ok "Backup complete: $BACKUP_DIR"
    echo ""
    echo "  To revert at any time:  bash $REPO_ROOT/tools/migrate-backup.sh restore $BACKUP_DIR"
    echo "  (Plus: /plugin uninstall engram  — separate operator step)"
}

# Global to carry backup dir through steps.
BACKUP_DIR=""

# ---------------------------------------------------------------------------
# Step 2 — Install plugin surfaces
# ---------------------------------------------------------------------------
step2_install_plugin() {
    section "Step 2: Install plugin surfaces"

    # Build the plugin with THIS install's feature-set, read from config.json.
    # Without this, build-plugin.sh defaults to single-agent (MULTI_AGENT=0) and
    # reads only its --multi-agent/--tier flags (never config.json), so migrating
    # a multi-agent install would SILENTLY DROP its multi-agent surfaces
    # (baton / inter-agent letters / forum / inter-agent prompt-hook). #704.
    # NOTE: necessary, not sufficient — other break surfaces (surface-drift hooks,
    # RUNTIME_TOOLS, the engram_client fallback) are separate; see the audit on #704.
    # install.sh persists both fields; install_tier is null on controlled
    # multi-agent installs, so pass --tier ONLY when set (else build-plugin uses
    # its manifest default_tier).
    local build_flags=()
    local _cfg="$ENGRAM_HOME/config.json"
    if [[ -f "$_cfg" ]] && command -v jq &>/dev/null; then
        local _ma _tier
        _ma="$(jq -r '.multi_agent // false' "$_cfg")"
        _tier="$(jq -r '.install_tier // empty' "$_cfg")"
        [[ "$_ma" == "true" ]] && build_flags+=("--multi-agent")
        [[ -n "$_tier" ]] && build_flags+=("--tier" "$_tier")
    fi

    if [[ $DRY_RUN -eq 1 ]]; then
        dry "Would run: bash $REPO_ROOT/tools/build-plugin.sh ${build_flags[*]:-}"
        dry "Would run: bash $REPO_ROOT/tools/install-local-marketplace.sh --skip-build"
        dry "Would create venv + pip install -r requirements.txt (if $ENGRAM_HOME/venv absent or fastmcp not importable); upgrade deps in-place if sqlite_vec absent"
        dry "Would write migration state 'post-step2' to $MIGRATION_STATE_FILE"
        dry "Would pause and wait for user /plugin install + --resume invocation."
        return
    fi

    info "Building plugin tree (flags: ${build_flags[*]:-none})..."
    bash "$REPO_ROOT/tools/build-plugin.sh" ${build_flags[@]+"${build_flags[@]}"}

    info "Installing local marketplace (--skip-build: reusing the build above)..."
    # Pass --skip-build so install-local-marketplace.sh does NOT re-invoke
    # build-plugin.sh without flags — that flagless rebuild would clobber the
    # tier/multi-agent-correct build we just ran above. (#713)
    bash "$REPO_ROOT/tools/install-local-marketplace.sh" --skip-build

    # Ensure the plugin venv exists and has dependencies installed. (#713)
    # The plugin launcher (launch-engram-server.sh) hard-requires
    # $ENGRAM_HOME/venv/bin/python3 and exits if absent. Scatter installs running
    # on system python have no such venv, so migration must create it.
    #
    # Python3 selection: prefer the python3 on PATH (same interpreter the scatter
    # install used at runtime), which by definition has the venv module available
    # on any modern distro. If for any reason it lacks venv, the command will fail
    # loud via set -euo pipefail — do not try to paper over it silently.
    _venv_dir="$ENGRAM_HOME/venv"
    _venv_python="$_venv_dir/bin/python3"
    _venv_pip="$_venv_dir/bin/pip"

    _venv_needs_create=0
    if [[ ! -f "$_venv_python" ]]; then
        _venv_needs_create=1
    elif ! "$_venv_python" -c "import fastmcp" &>/dev/null 2>&1; then
        _venv_needs_create=1
    fi

    if [[ $_venv_needs_create -eq 1 ]]; then
        info "Creating plugin venv at $_venv_dir ..."
        info "NOTE: this is a one-time HEAVY install (sentence-transformers pulls"
        info "      torch, ~minutes depending on bandwidth). Please be patient."
        python3 -m venv "$_venv_dir"
        "$_venv_pip" install --quiet -r "$REPO_ROOT/requirements.txt"
        info "Venv created and dependencies installed."
    else
        info "Plugin venv already present with fastmcp importable — skipping venv create."
        # Secondary upgrade check: venvs created before sqlite_vec was added to
        # requirements.txt (#729 / PR #730) are missing sqlite_vec, causing
        # semantic search to fall back to Python-cosine and backup dumps to crash
        # on DBs with vec_nodes tables. If sqlite_vec is absent, upgrade in-place
        # (pip install -r) without destroying existing venv state.
        if ! "$_venv_python" -c "import sqlite_vec" &>/dev/null 2>&1; then
            info "Existing venv is missing sqlite_vec — upgrading dependencies in-place..."
            "$_venv_pip" install --quiet -r "$REPO_ROOT/requirements.txt"
            info "Venv dependencies upgraded (sqlite_vec now present)."
        fi
    fi

    write_migration_state "post-step2"

    pause "PLUGIN INSTALL REQUIRED — complete the following before continuing:

  1. Inside a Claude Code session, run:
       /plugin install engram
       /plugin enable engram

  2. (Keep Claude Code open — do NOT restart yet.)

  3. Re-invoke this script with --resume to continue the scatter removal:
       bash $REPO_ROOT/tools/migrate-to-plugin.sh --resume

  The plugin must be installed BEFORE scatter surfaces are removed so there
  is never a window with zero ENGRAM surfaces.

  Backup dir (if you need to revert):
    $BACKUP_DIR
    bash $REPO_ROOT/tools/migrate-backup.sh restore $BACKUP_DIR"

    exit 0
}

# ---------------------------------------------------------------------------
# Step 3 — Remove scatter surfaces
# ---------------------------------------------------------------------------
step3_remove_scatter() {
    section "Step 3: Remove scatter surfaces"

    require_jq

    # 3a. settings.json — remove hooks whose command references scatter hooks dir
    step3a_remove_hook_entries

    # 3b. .claude.json — remove mcpServers.engram
    step3b_remove_mcp_entry

    # 3c. ~/.claude/skills/engram-*
    step3c_remove_skills

    # 3d. ~/.claude/agents/engram-*
    step3d_remove_agents

    # 3e. ~/.engram deployed code (optional, gated by --remove-deployed-code)
    if [[ $REMOVE_CODE -eq 1 ]]; then
        step3e_remove_code
    else
        info "Skipping ~/.engram code removal (--remove-deployed-code not set)."
        info "Scatter code files are inert once hooks and MCP point at the plugin."
        info "Re-run with --remove-deployed-code to remove them later."
    fi
}

step3a_remove_hook_entries() {
    info "Step 3a: settings.json hook block removal"
    local settings="$CLAUDE_HOME/settings.json"

    if [[ ! -f "$settings" ]]; then
        warn "settings.json not found at $settings — nothing to remove."
        return
    fi

    # Count how many ENGRAM scatter hook commands are present.
    local before_count
    before_count="$(jq --arg hooks_dir "$SCATTER_HOOKS_DIR" \
        '[.. | objects | select(has("command")) | .command
          | select(contains($hooks_dir))] | length' \
        "$settings" 2>/dev/null || echo 0)"

    if [[ "$before_count" -eq 0 ]]; then
        ok "No scatter ENGRAM hook entries found in settings.json — already clean."
        return
    fi

    info "Found $before_count scatter ENGRAM hook command(s) in $settings."

    # Build the jq filter.
    # Structure: .hooks is an object (event-name → array of hook-group objects).
    # Each hook-group object has a "hooks" array of individual command entries.
    # We use with_entries to iterate over event names generically (no hardcoded
    # event names), then map over hook-groups, then filter individual commands
    # by path substring. Empty hook-group objects and empty event arrays are
    # pruned so the JSON stays clean.
    # Note: walk()-based approach was tested and does NOT work here because
    # walk() processes bottom-up and the $hooks_dir variable does not propagate
    # correctly through the recursive descent — direct path traversal is correct.
    local jq_filter
    jq_filter='.hooks |= with_entries(
      .value |= map(
        .hooks |= map(
          select(if has("command") then (.command | contains($hooks_dir) | not) else true end)
        )
        | select((.hooks | length) > 0)
      )
      | select((.value | length) > 0)
    )'

    if [[ $DRY_RUN -eq 1 ]]; then
        dry "Would remove $before_count scatter ENGRAM hook entries from $settings"
        dry "jq filter removes entries whose .command contains: $SCATTER_HOOKS_DIR"
        echo "  Commands that would be removed:"
        jq --arg hooks_dir "$SCATTER_HOOKS_DIR" \
            '[.. | objects | select(has("command")) | .command | select(contains($hooks_dir))]' \
            "$settings"
        return
    fi

    # Apply the filter (write to temp file first, then atomically replace)
    local tmp_settings
    tmp_settings="$(mktemp)"

    jq --arg hooks_dir "$SCATTER_HOOKS_DIR" "$jq_filter" "$settings" > "$tmp_settings"

    # Validate the result is still valid JSON
    if ! jq '.' "$tmp_settings" > /dev/null 2>&1; then
        rm -f "$tmp_settings"
        err "Post-edit settings.json validation FAILED — jq output is not valid JSON. Original file untouched."
    fi

    # Verify ENGRAM scatter hook count is now 0
    local after_count
    after_count="$(jq --arg hooks_dir "$SCATTER_HOOKS_DIR" \
        '[.. | objects | select(has("command")) | .command
          | select(contains($hooks_dir))] | length' \
        "$tmp_settings" 2>/dev/null || echo -1)"

    if [[ "$after_count" -ne 0 ]]; then
        rm -f "$tmp_settings"
        err "Post-edit verify FAILED: $after_count scatter ENGRAM hook commands remain (expected 0). Original file untouched."
    fi

    cp "$settings" "${settings}.migrate-bak"  # belt-and-suspenders local backup
    mv "$tmp_settings" "$settings"

    ok "settings.json: removed $before_count scatter ENGRAM hook entries. Post-edit count: 0. Valid JSON confirmed."
}

step3b_remove_mcp_entry() {
    info "Step 3b: .claude.json engram mcpServers entry removal"

    local claude_json_path=""
    if [[ -f "$CLAUDE_HOME/.claude.json" ]]; then
        claude_json_path="$CLAUDE_HOME/.claude.json"
    elif [[ -f "$HOME/.claude.json" ]]; then
        claude_json_path="$HOME/.claude.json"
    fi

    if [[ -z "$claude_json_path" ]]; then
        warn ".claude.json not found at $CLAUDE_HOME/.claude.json or $HOME/.claude.json — nothing to remove."
        return
    fi

    local has_engram
    has_engram="$(jq 'if .mcpServers | has("engram") then 1 else 0 end' \
        "$claude_json_path" 2>/dev/null || echo 0)"

    if [[ "$has_engram" -eq 0 ]]; then
        ok "No 'engram' entry in mcpServers of $claude_json_path — already clean."
        return
    fi

    if [[ $DRY_RUN -eq 1 ]]; then
        dry "Would remove .mcpServers.engram from $claude_json_path"
        echo "  Current engram entry:"
        jq '.mcpServers.engram' "$claude_json_path"
        return
    fi

    local tmp_json
    tmp_json="$(mktemp)"
    jq 'del(.mcpServers.engram)' "$claude_json_path" > "$tmp_json"

    # Validate
    if ! jq '.' "$tmp_json" > /dev/null 2>&1; then
        rm -f "$tmp_json"
        err "Post-edit .claude.json validation FAILED — jq output is not valid JSON. Original file untouched."
    fi

    # Verify engram key is gone
    local still_has
    still_has="$(jq 'if .mcpServers | has("engram") then 1 else 0 end' "$tmp_json" 2>/dev/null || echo 1)"
    if [[ "$still_has" -ne 0 ]]; then
        rm -f "$tmp_json"
        err "Post-edit verify FAILED: 'engram' key still present in mcpServers. Original file untouched."
    fi

    cp "$claude_json_path" "${claude_json_path}.migrate-bak"
    mv "$tmp_json" "$claude_json_path"

    ok ".claude.json: removed mcpServers.engram. No 'engram' key under mcpServers confirmed."
}

step3c_remove_skills() {
    info "Step 3c: ~/.claude/skills/engram-* removal"
    local skills_dir="$CLAUDE_HOME/skills"

    if [[ ! -d "$skills_dir" ]]; then
        info "No $skills_dir — nothing to remove."
        return
    fi

    local removed_count=0
    while IFS= read -r -d '' skill_dir; do
        local skill_name
        skill_name="$(basename "$skill_dir")"
        if [[ $DRY_RUN -eq 1 ]]; then
            dry "Would remove skill dir: $skill_dir"
        else
            rm -rf "$skill_dir"
            info "Removed skill: $skill_name"
        fi
        removed_count=$((removed_count + 1))
    done < <(find "$skills_dir" -maxdepth 1 -type d -name 'engram-*' -print0 2>/dev/null || true)

    if [[ $DRY_RUN -eq 1 ]]; then
        [[ $removed_count -gt 0 ]] || dry "No engram-* skill dirs found in $skills_dir."
    else
        if [[ $removed_count -gt 0 ]]; then
            ok "Removed $removed_count engram-* skill dir(s) from $skills_dir."
        else
            info "No engram-* skill dirs found in $skills_dir."
        fi
    fi
}

step3d_remove_agents() {
    info "Step 3d: ~/.claude/agents/engram-* removal"
    local agents_dir="$CLAUDE_HOME/agents"

    if [[ ! -d "$agents_dir" ]]; then
        info "No $agents_dir — nothing to remove."
        return
    fi

    local removed_count=0
    while IFS= read -r -d '' agent_path; do
        local agent_name
        agent_name="$(basename "$agent_path")"
        if [[ $DRY_RUN -eq 1 ]]; then
            dry "Would remove agent: $agent_path"
        else
            rm -rf "$agent_path"
            info "Removed agent: $agent_name"
        fi
        removed_count=$((removed_count + 1))
    done < <(find "$agents_dir" -maxdepth 1 \( -type f -o -type d \) -name 'engram-*' -print0 2>/dev/null || true)

    if [[ $DRY_RUN -eq 1 ]]; then
        [[ $removed_count -gt 0 ]] || dry "No engram-* agent files/dirs found in $agents_dir."
    else
        if [[ $removed_count -gt 0 ]]; then
            ok "Removed $removed_count engram-* agent(s) from $agents_dir."
        else
            info "No engram-* agent files/dirs found in $agents_dir."
        fi
    fi
}

step3e_remove_code() {
    info "Step 3e: ~/.engram scatter CODE removal (--remove-deployed-code)"

    # CODE targets — an EXPLICIT fixed list of scatter code surfaces (NOT a
    # dynamic "everything not in the DATA allowlist"). A fixed list is the safe
    # design: a new file in ENGRAM_HOME is never deleted by default. The DATA
    # allowlist is a secondary delete-refusing guard, not the selection mechanism.
    local code_targets=(
        "server.py"
        "SKILL.md"
        "hooks"
        "tools"
    )

    # Also engram_*.py files (discovered dynamically)
    local py_files=()
    while IFS= read -r -d '' pyf; do
        py_files+=("$(basename "$pyf")")
    done < <(find "$ENGRAM_HOME" -maxdepth 1 -type f -name 'engram_*.py' -print0 2>/dev/null || true)

    local all_targets=("${code_targets[@]}" "${py_files[@]}")

    for target in "${all_targets[@]}"; do
        local full_path="$ENGRAM_HOME/$target"

        # Refuse to delete DATA paths — belt-and-suspenders guard
        if is_data_path "$full_path"; then
            err "DATA ALLOWLIST VIOLATION: attempted to delete DATA path: $full_path — refusing. This is a bug; please report."
        fi

        if [[ ! -e "$full_path" ]]; then
            info "Not present (already removed or never deployed): $full_path"
            continue
        fi

        if [[ $DRY_RUN -eq 1 ]]; then
            dry "Would remove code path: $full_path"
        else
            rm -rf "$full_path"
            info "Removed: $full_path"
        fi
    done

    # Post-removal: always assert knowledge.db is still present (core guard)
    if [[ $DRY_RUN -eq 0 ]]; then
        if [[ ! -f "$ENGRAM_HOME/knowledge.db" ]]; then
            err "DATA INTEGRITY FAILURE: knowledge.db is missing after code removal! Restore immediately:
  bash $REPO_ROOT/tools/migrate-backup.sh restore $BACKUP_DIR"
        fi
        ok "DATA allowlist: knowledge.db present and intact after code removal."
    fi
}

# ---------------------------------------------------------------------------
# Step 4 — Assert knowledge.db not clobbered
# ---------------------------------------------------------------------------
step4_assert_db_intact() {
    section "Step 4: Assert knowledge.db integrity (plugin did not clobber it)"

    if [[ ! -f "$ENGRAM_HOME/knowledge.db" ]]; then
        err "CRITICAL: knowledge.db is missing at $ENGRAM_HOME/knowledge.db!
The plugin install may have clobbered the existing database.
Restore immediately:
  bash $REPO_ROOT/tools/migrate-backup.sh restore $BACKUP_DIR"
    fi

    if [[ $DRY_RUN -eq 1 ]]; then
        dry "Would check: knowledge.db checksum matches pre-migration backup."
        return
    fi

    # If we have a backup dir with a backed-up db, compare checksums.
    if [[ -n "$BACKUP_DIR" ]] && [[ -d "$BACKUP_DIR" ]] && [[ -f "$BACKUP_DIR/engram/knowledge.db" ]]; then
        local backup_sha live_sha
        backup_sha="$(sha256_file "$BACKUP_DIR/engram/knowledge.db")"
        live_sha="$(sha256_file "$ENGRAM_HOME/knowledge.db")"

        if [[ "$backup_sha" != "UNAVAILABLE" && "$live_sha" != "UNAVAILABLE" ]]; then
            if [[ "$live_sha" != "$backup_sha" ]]; then
                err "CRITICAL: knowledge.db checksum changed since backup!
  Backup SHA:  $backup_sha
  Live SHA:    $live_sha
The plugin may have replaced the database. Restore immediately:
  bash $REPO_ROOT/tools/migrate-backup.sh restore $BACKUP_DIR"
            fi
            ok "knowledge.db checksum matches backup: $live_sha"
        else
            # sha256 unavailable — fall back to mtime
            local live_mtime backup_mtime
            live_mtime="$(file_mtime "$ENGRAM_HOME/knowledge.db")"
            backup_mtime="$(file_mtime "$BACKUP_DIR/engram/knowledge.db")"
            if [[ "$live_mtime" -lt "$backup_mtime" ]]; then
                warn "knowledge.db mtime is older than backup copy (unexpected). sha256 unavailable. Inspect manually."
            else
                ok "knowledge.db present; mtime check passed (sha256 unavailable)."
            fi
        fi
    else
        ok "knowledge.db present at $ENGRAM_HOME/knowledge.db (no backup dir available for checksum comparison)."
    fi
}

# ---------------------------------------------------------------------------
# Step 5 — Verify (post-restart checks)
# ---------------------------------------------------------------------------
step5_verify() {
    section "Step 5: Verify migration"

    local all_ok=1

    # 5a. settings.json: zero scatter-path hook commands
    info "Check 5a: settings.json — scatter ENGRAM hook commands count == 0"
    if [[ -f "$CLAUDE_HOME/settings.json" ]] && command -v jq &>/dev/null; then
        local scatter_hook_count
        scatter_hook_count="$(jq --arg hooks_dir "$SCATTER_HOOKS_DIR" \
            '[.. | objects | select(has("command")) | .command
              | select(contains($hooks_dir))] | length' \
            "$CLAUDE_HOME/settings.json" 2>/dev/null || echo -1)"
        if [[ "$scatter_hook_count" -eq 0 ]]; then
            ok "settings.json: 0 scatter ENGRAM hook commands. PASS"
        else
            echo "FAIL   settings.json: $scatter_hook_count scatter ENGRAM hook command(s) remain."
            all_ok=0
        fi
    else
        warn "Cannot verify settings.json (file missing or jq not installed)."
    fi

    # 5b. .claude.json: no engram key under mcpServers
    info "Check 5b: .claude.json — no engram entry in mcpServers"
    local claude_json_path=""
    if [[ -f "$CLAUDE_HOME/.claude.json" ]]; then
        claude_json_path="$CLAUDE_HOME/.claude.json"
    elif [[ -f "$HOME/.claude.json" ]]; then
        claude_json_path="$HOME/.claude.json"
    fi

    if [[ -n "$claude_json_path" ]] && command -v jq &>/dev/null; then
        local has_engram
        has_engram="$(jq 'if .mcpServers | has("engram") then 1 else 0 end' \
            "$claude_json_path" 2>/dev/null || echo -1)"
        if [[ "$has_engram" -eq 0 ]]; then
            ok ".claude.json: no 'engram' in mcpServers. PASS"
        else
            echo "FAIL   .claude.json: 'engram' still present in mcpServers."
            all_ok=0
        fi
    else
        warn "Cannot verify .claude.json (file missing or jq not installed)."
    fi

    # 5c. ~/.claude/skills/engram-* absent
    info "Check 5c: ~/.claude/skills/engram-* — scatter skill dirs absent"
    if [[ -d "$CLAUDE_HOME/skills" ]]; then
        local remaining_skills
        remaining_skills="$(find "$CLAUDE_HOME/skills" -maxdepth 1 -type d -name 'engram-*' 2>/dev/null | wc -l || echo -1)"
        if [[ "$remaining_skills" -eq 0 ]]; then
            ok "~/.claude/skills/: no scatter engram-* dirs. PASS"
        else
            echo "FAIL   ~/.claude/skills/: $remaining_skills scatter engram-* dir(s) remain."
            all_ok=0
        fi
    else
        ok "~/.claude/skills/ does not exist — no scatter skill dirs. PASS"
    fi

    # 5d. ~/.claude/agents/engram-* absent
    info "Check 5d: ~/.claude/agents/engram-* — scatter agent entries absent"
    if [[ -d "$CLAUDE_HOME/agents" ]]; then
        local remaining_agents
        remaining_agents="$(find "$CLAUDE_HOME/agents" -maxdepth 1 \( -type f -o -type d \) -name 'engram-*' 2>/dev/null | wc -l || echo -1)"
        if [[ "$remaining_agents" -eq 0 ]]; then
            ok "~/.claude/agents/: no scatter engram-* entries. PASS"
        else
            echo "FAIL   ~/.claude/agents/: $remaining_agents scatter engram-* entry/entries remain."
            all_ok=0
        fi
    else
        ok "~/.claude/agents/ does not exist — no scatter agent entries. PASS"
    fi

    # 5e. knowledge.db intact
    info "Check 5e: knowledge.db integrity"
    if [[ -f "$ENGRAM_HOME/knowledge.db" ]]; then
        local node_count=""
        if command -v sqlite3 &>/dev/null; then
            node_count="$(sqlite3 "$ENGRAM_HOME/knowledge.db" 'SELECT COUNT(*) FROM nodes;' 2>/dev/null || echo "QUERY_FAILED")"
        fi
        if [[ -n "$node_count" && "$node_count" != "QUERY_FAILED" ]]; then
            # Compare against pre-migration count from backup manifest (spec §5 requirement)
            if [[ -n "$BACKUP_DIR" ]] && [[ -d "$BACKUP_DIR" ]] && [[ -f "$BACKUP_DIR/manifest.txt" ]]; then
                local manifest_count
                manifest_count="$(grep '^state\.knowledge_db_node_count:' "$BACKUP_DIR/manifest.txt" | awk '{print $2}' || echo "")"
                if [[ -z "$manifest_count" || "$manifest_count" == "UNAVAILABLE"* ]]; then
                    ok "knowledge.db present: $node_count node(s). Pre-migration count unavailable in manifest (skipping comparison). PASS"
                elif [[ "$node_count" -eq "$manifest_count" ]]; then
                    ok "knowledge.db present: $node_count node(s) == pre-migration count ($manifest_count). PASS"
                else
                    echo "FAIL   knowledge.db node count mismatch: live=$node_count pre-migration=$manifest_count"
                    all_ok=0
                fi
            elif [[ -z "$BACKUP_DIR" ]]; then
                warn "BACKUP_DIR not set — skipping pre-migration node-count comparison. Live count: $node_count node(s)."
                ok "knowledge.db present: $node_count node(s). PASS"
            else
                warn "Backup manifest not found at $BACKUP_DIR/manifest.txt — skipping node-count comparison. Live count: $node_count node(s)."
                ok "knowledge.db present: $node_count node(s). PASS"
            fi
        else
            ok "knowledge.db present (node count unavailable — sqlite3 not installed or query failed). PASS"
        fi
    else
        echo "FAIL   knowledge.db MISSING at $ENGRAM_HOME/knowledge.db!"
        all_ok=0
    fi

    # 5f. Data surfaces present — check all DATA_ALLOWLIST members
    # knowledge.db absence is a hard FAIL (already checked in 5e above).
    # All other members are warned on absence (some are optional per install).
    info "Check 5f: Data surfaces (full DATA_ALLOWLIST)"
    for data_surface in "${DATA_ALLOWLIST[@]}"; do
        if [[ "$data_surface" == "knowledge.db" ]]; then
            continue  # already checked (hard FAIL) in 5e
        fi
        if [[ -e "$ENGRAM_HOME/$data_surface" ]]; then
            ok "$data_surface present. PASS"
        else
            warn "$data_surface not found at $ENGRAM_HOME/$data_surface (may not exist on all installs)."
        fi
    done

    # Summary
    echo ""
    if [[ $all_ok -eq 1 ]]; then
        echo "======================================"
        echo " Migration verify: ALL CHECKS PASSED "
        echo "======================================"
        echo ""
        echo "The ENGRAM plugin install is clean. Next step:"
        echo "  Restart Claude Code (or run /mcp reconnect) to pick up"
        echo "  the plugin MCP server. Hook firing will use plugin-cache paths."
        echo ""
        echo "  Verify one set fires: open a fresh Claude Code session and"
        echo "  confirm only one SessionStart + one UserPromptSubmit hook fires per event."
    else
        echo "======================================"
        echo " Migration verify: SOME CHECKS FAILED"
        echo "======================================"
        echo ""
        echo "Review the FAIL lines above."
        if [[ -n "$BACKUP_DIR" ]] && [[ -d "$BACKUP_DIR" ]]; then
            echo "To revert:"
            echo "  bash $REPO_ROOT/tools/migrate-backup.sh restore $BACKUP_DIR"
        fi
        return 1
    fi
}

# ---------------------------------------------------------------------------
# Main dispatch
# ---------------------------------------------------------------------------
main() {
    if [[ $VERIFY_ONLY -eq 1 ]]; then
        step5_verify
        exit $?
    fi

    if [[ $RESUME -eq 1 ]]; then
        local state
        state="$(read_migration_state)"
        if [[ "$state" != "post-step2" ]]; then
            warn "Migration state is '$state' (expected 'post-step2'). --resume is intended for after /plugin install."
            warn "Proceeding anyway — if this is incorrect, Ctrl-C now and investigate."
            sleep 2
        fi

        section "Resuming from Step 3 (scatter removal)"
        info "Locating most recent backup for integrity checks..."
        BACKUP_DIR="$(ls -1td "$HOME/.engram-migration-backups"/*/ 2>/dev/null | head -1 || echo "")"
        [[ -n "$BACKUP_DIR" ]] && info "Most recent backup: $BACKUP_DIR" \
            || warn "No migration backup dir found at $HOME/.engram-migration-backups/ — checksum comparison will be skipped."

        step3_remove_scatter
        step4_assert_db_intact

        clear_migration_state

        pause "RESTART MCP SERVER — complete before verifying:

  Restart Claude Code or run /mcp reconnect inside a Claude Code session
  so the plugin MCP server is loaded (replacing the scatter server.py).

  Then re-invoke to verify:
    bash $REPO_ROOT/tools/migrate-to-plugin.sh --verify"
        exit 0
    fi

    # Full run
    step0_preflight

    if [[ $DRY_RUN -eq 1 ]]; then
        section "DRY RUN — no mutations will be applied"
        step1_backup
        step2_install_plugin
        step3_remove_scatter
        step4_assert_db_intact
        dry "Would pause for MCP restart, then run --verify."
        echo ""
        echo "Dry run complete. Re-run without --dry-run to apply."
        exit 0
    fi

    step1_backup
    step2_install_plugin
    # Step 2 exits after the PAUSE — user must re-invoke with --resume.
}

main "$@"
