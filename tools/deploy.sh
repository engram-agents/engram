#!/bin/bash
# tools/deploy.sh — Deploy alpha code into the running ENGRAM home.
#
# This is the deliberate version-upgrade action. Copies code files from
# the alpha working tree into $ENGRAM_HOME, leaving data files
# (knowledge.db, history/, diary/, logs/, etc.) untouched.
#
# The runtime tree ($ENGRAM_HOME) holds running code as a *managed copy*
# of alpha — it is NOT a development surface. Edits to running code
# happen in alpha and propagate via this script. (Lei + Borges, 2026-05-04.)
#
# Usage:
#   tools/deploy.sh [--yes|-y] [--dry-run|-n] [--delete] [--help|-h]
#
# Environment:
#   ENGRAM_HOME — where to deploy to (default: $HOME/.engram)
#
# Flags:
#   --dry-run   Show planned changes; don't apply.
#   --yes       Skip the confirm prompt.
#   --delete    For directories in the manifest, remove files in target
#               that don't exist in source. Off by default — safer, but
#               leaves stale code behind when alpha removes a file.
#
# What gets deployed (manifest at the bottom of this script):
#   - Runtime Python: server.py, engram_client.py, engram_confidence.py,
#     engram_filter.py, engram_idf.py, engram_ids.py, viz_server.py
#   - Hooks (hooks/), tools (tools/)
#   - Protocol doc (SKILL.md)
#
# What does NOT get deployed:
#   - Data files: knowledge.db, graph_snapshot.md, session_log.md,
#     warm-briefing.md, config.json, recall_window.json
#   - Per-agent state: diary/, sessions/ (per-session markers),
#     history/, logs/, feeling-nudge-active.json, etc.
#   - Install-time entry points: install.sh, install-gemini.sh,
#     bootstrap.py, settings.hooks.json.template, README.md, LICENSE,
#     USER_GUIDE.md (these belong to fresh installs, not running upgrades)
#   - Install-time scaffolding: templates/, integrations/, inter-agent/
#     (deployed at install time only, not part of the running tree)
#   - Skills (skills/) and agents (agents/claude/): destinations are outside
#     $ENGRAM_HOME, so deploy.sh cannot reach them; updates require re-running
#     install.sh / install-gemini.sh. deploy.sh prints a nudge if either
#     changed since the last deploy.
#
# After deploy: restart your MCP client to reload server.py.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ALPHA_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
ENGRAM_HOME="${ENGRAM_HOME:-$HOME/.engram}"
YES=0
DRY_RUN=0

# Detect target model platform if not set
if [[ -z "${TARGET:-}" ]]; then
    if [[ "$ENGRAM_HOME" == *"-gemini"* ]]; then
        TARGET="gemini"
    else
        TARGET="claude"
    fi
fi
DELETE=0

usage() {
    sed -n '/^# Usage:/,/^$/p' "$0" | sed 's/^# //; s/^#$//'
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --yes|-y)     YES=1; shift ;;
        --dry-run|-n) DRY_RUN=1; shift ;;
        --delete)     DELETE=1; shift ;;
        -h|--help)    usage; exit 0 ;;
        *) echo "Unknown option: $1" >&2; usage; exit 2 ;;
    esac
done

# Sanity checks
if [[ ! -d "$ALPHA_DIR" ]]; then
    echo "ALPHA_DIR not found: $ALPHA_DIR" >&2
    exit 1
fi
if [[ ! -d "$ENGRAM_HOME" ]]; then
    echo "ENGRAM_HOME not found: $ENGRAM_HOME" >&2
    echo "(set ENGRAM_HOME=/path/to/runtime if it lives elsewhere)" >&2
    exit 1
fi
if [[ "$ALPHA_DIR" -ef "$ENGRAM_HOME" ]]; then
    echo "ALPHA_DIR and ENGRAM_HOME are the same directory — refusing to deploy" >&2
    echo "(this would defeat the dev/runtime separation; see header)" >&2
    exit 1
fi
if ! command -v rsync >/dev/null 2>&1; then
    echo "rsync not found — required for deploy" >&2
    exit 1
fi

# Resolve alpha version metadata
if git -C "$ALPHA_DIR" rev-parse --git-dir >/dev/null 2>&1; then
    ALPHA_SHA="$(git -C "$ALPHA_DIR" rev-parse --short HEAD)"
    ALPHA_BRANCH="$(git -C "$ALPHA_DIR" rev-parse --abbrev-ref HEAD)"
    if [[ -n "$(git -C "$ALPHA_DIR" status --porcelain)" ]]; then
        ALPHA_DIRTY="dirty"
    else
        ALPHA_DIRTY="clean"
    fi
else
    ALPHA_SHA="unknown"
    ALPHA_BRANCH="unknown"
    ALPHA_DIRTY="not-a-git-repo"
fi

# Manifest — entries are paths relative to $ALPHA_DIR.
# Directories deploy recursively (with trailing slash semantics).
# Optional src:dst syntax remaps source path (in alpha) to a different
# target path (in $ENGRAM_HOME). Used for the hooks/claude → hooks
# layered-sidecar split where alpha holds claude-side and gemini-side
# in separate directories but the runtime sees a single flat hooks/.
# See header for what's intentionally excluded (install-time scaffolding,
# data files).
MANIFEST=(
    "hooks/$TARGET:hooks"
    "server.py"
    "engram_client.py"
    "engram_confidence.py"
    "engram_filter.py"
    "engram_idf.py"
    "engram_ids.py"
    "engram_log_emitter.py"
    "viz_server.py"
    "tools"
    "SKILL.md"
)
# Note: skills/ deliberately NOT in the manifest. The live skills surface
# is OUTSIDE $ENGRAM_HOME — Claude reads from $CLAUDE_HOME/skills/, and
# Gemini folds skills into GEMINI.md + the extension's hooks.json at install
# time (no parallel skills/ runtime directory). install.sh and
# install-gemini.sh handle the per-CLI placement; deploy.sh (which only
# writes inside $ENGRAM_HOME) cannot reach the right destination.
# If runtime-deployable skills become a need, deploy.sh needs a manifest
# mechanism for destinations outside $ENGRAM_HOME — separate epic.

# Per-deploy report — check what would change before applying.
echo "=== ENGRAM deploy ==="
echo "  source:  $ALPHA_DIR ($ALPHA_BRANCH @ $ALPHA_SHA, $ALPHA_DIRTY)"
echo "  target:  $ENGRAM_HOME"
echo "  delete:  $([[ $DELETE = 1 ]] && echo yes || echo "no (use --delete to remove stale files)")"
echo "  mode:    $([[ $DRY_RUN = 1 ]] && echo "dry-run (no changes)" || echo "live (will apply)")"
if [[ "$ALPHA_DIRTY" = "dirty" ]]; then
    echo "  warning: alpha working tree has uncommitted changes — deploying them anyway"
fi
echo

# Helper: split entry into (src_rel, dst_rel). If entry contains ':',
# split on the first ':'; otherwise src and dst are the same path.
_split_entry() {
    local entry="$1"
    if [[ "$entry" == *:* ]]; then
        ENTRY_SRC="${entry%%:*}"
        ENTRY_DST="${entry##*:}"
        ENTRY_LABEL="${ENTRY_SRC} → ${ENTRY_DST}"
    else
        ENTRY_SRC="$entry"
        ENTRY_DST="$entry"
        ENTRY_LABEL="$entry"
    fi
}

# Files that may carry user/agent appendices and thus need 3-way conflict
# checking before deploy.sh overwrites them. The check uses the alpha SHA
# recorded in $ENGRAM_HOME/.deployed-version as the baseline (recoverable
# via `git show $sha:<path>` from the alpha repo). See header section
# "What does NOT get deployed" for files this list deliberately excludes
# (CLAUDE.md, warm-briefing.md, settings.json — those are install-once
# files that deploy.sh never touches today). Adding a file to this set
# means deploy.sh refuses to silently overwrite a user-divergent local copy.
PROTECTED_FILES=(
    "SKILL.md"
)

# Read the last-deployed alpha SHA from $ENGRAM_HOME/.deployed-version.
# Returns "" if the file is missing, malformed, or the SHA isn't resolvable
# in the alpha repo (orphan SHA, branch deleted). Callers treat empty as
# "no-baseline" and skip the 3-way check (with a warning).
_last_deployed_sha() {
    local marker="$ENGRAM_HOME/.deployed-version"
    [[ -f "$marker" ]] || { echo ""; return; }
    local sha
    sha=$(grep -E "^alpha_sha=" "$marker" 2>/dev/null | head -1 | cut -d= -f2 | tr -d ' \r\n')
    [[ -n "$sha" ]] || { echo ""; return; }
    if ! git -C "$ALPHA_DIR" rev-parse --verify "$sha^{commit}" >/dev/null 2>&1; then
        echo ""
        return
    fi
    echo "$sha"
}

# 3-way comparison for a single file. Echoes one of:
#   no-baseline  — no recoverable baseline; caller falls back to default behavior
#   identical    — alpha-NEW == user-LOCAL == baseline (or no change to apply)
#   safe-update  — user-LOCAL == baseline; safe to overwrite with alpha-NEW
#   conflict     — both alpha-NEW and user-LOCAL diverged from baseline
#   new-file     — file did not exist at baseline (first time being deployed)
# All comparisons are content-hash based (git hash-object) — robust against
# whitespace-only differences in the runtime tree (e.g. line-ending munging).
_check_3way() {
    local rel="$1"  # path relative to alpha root, also relative to $ENGRAM_HOME for protected files
    local alpha_path="$ALPHA_DIR/$rel"
    local local_path="$ENGRAM_HOME/$rel"
    local sha; sha="$(_last_deployed_sha)"

    if [[ -z "$sha" ]]; then
        echo "no-baseline"
        return
    fi

    local alpha_hash local_hash baseline_hash
    # Guard alpha-side existence: hash-object on a missing file emits empty;
    # if local is ALSO missing then both empties would falsely classify as
    # "identical". Manifest expects alpha files to exist — a missing alpha
    # means manifest drift, not a deploy outcome.
    if [[ ! -f "$alpha_path" ]]; then
        echo "alpha-missing"
        return
    fi
    alpha_hash="$(git -C "$ALPHA_DIR" hash-object "$alpha_path" 2>/dev/null || echo "")"
    [[ -f "$local_path" ]] && local_hash="$(git -C "$ALPHA_DIR" hash-object "$local_path" 2>/dev/null || echo "")" || local_hash=""
    baseline_hash="$(git -C "$ALPHA_DIR" rev-parse "$sha:$rel" 2>/dev/null || echo "")"

    if [[ -z "$baseline_hash" ]]; then
        # File didn't exist at the baseline SHA — first time being deployed.
        echo "new-file"
        return
    fi
    if [[ "$alpha_hash" == "$local_hash" && "$alpha_hash" == "$baseline_hash" ]]; then
        echo "identical"; return
    fi
    if [[ "$local_hash" == "$baseline_hash" ]]; then
        echo "safe-update"; return
    fi
    if [[ "$alpha_hash" == "$baseline_hash" ]]; then
        # User has diverged but alpha hasn't changed since baseline —
        # nothing for us to push, leave the user's version alone.
        echo "identical"; return
    fi
    echo "conflict"
}

# Show the relevant diffs and prompt the user when a conflict is detected.
# Returns 0 if user chose accept-alpha (caller proceeds with rsync),
#         1 if user chose keep-local (caller saves alpha-NEW as .alpha-pending and skips),
#         2 if user chose abort.
_resolve_conflict() {
    local rel="$1"
    local alpha_path="$ALPHA_DIR/$rel"
    local local_path="$ENGRAM_HOME/$rel"
    local sha; sha="$(_last_deployed_sha)"

    echo
    echo "=== CONFLICT on $rel ==="
    echo "Both alpha and your local copy have diverged from the last-deployed baseline ($sha)."
    echo
    echo "--- baseline → alpha-NEW (alpha changes since last deploy) ---"
    git -C "$ALPHA_DIR" --no-pager diff --no-color "$sha" -- "$rel" | sed 's/^/    /' | head -40
    echo
    echo "--- baseline → user-LOCAL (your changes since last deploy) ---"
    diff -u <(git -C "$ALPHA_DIR" show "$sha:$rel" 2>/dev/null) "$local_path" | sed 's/^/    /' | head -40
    echo

    if [[ $YES = 1 ]]; then
        # In --yes mode we never auto-clobber a divergent local copy.
        echo "  --yes mode: refusing to auto-overwrite divergent local. Saving alpha-NEW to ${rel}.alpha-pending and skipping."
        cp "$alpha_path" "$local_path.alpha-pending"
        return 1
    fi
    if [[ $DRY_RUN = 1 ]]; then
        echo "  dry-run mode: conflict would block update. Re-run without --dry-run to choose."
        return 2  # treat dry-run conflicts as block — surfaces them in the report
    fi

    local choice=""
    while [[ ! "$choice" =~ ^[abc]$ ]]; do
        read -r -p "Choose: (a) accept alpha (overwrite local), (b) keep local (skip + save .alpha-pending), (c) abort deploy [a/b/c]: " choice
    done
    case "$choice" in
        a) return 0 ;;
        b)
            cp "$alpha_path" "$local_path.alpha-pending"
            echo "  saved alpha-NEW to ${rel}.alpha-pending — merge manually when ready."
            return 1
            ;;
        c) return 2 ;;
    esac
    # Defensive fallthrough — should be unreachable since the read-loop
    # only exits on [abc]. Treat any other value as abort to avoid a
    # silent function-success return that would let caller rsync over a
    # conflict.
    return 2
}

CHANGES=0
PLAN_DETAILS=""
for entry in "${MANIFEST[@]}"; do
    _split_entry "$entry"
    src="$ALPHA_DIR/$ENTRY_SRC"
    dst="$ENGRAM_HOME/$ENTRY_DST"
    if [[ ! -e "$src" ]]; then
        printf '  skip:      %s (not in alpha)\n' "$ENTRY_LABEL"
        continue
    fi
    if [[ ! -e "$dst" ]]; then
        printf '  NEW:       %s\n' "$ENTRY_LABEL"
        CHANGES=1
        continue
    fi
    # Compute diff via rsync --dry-run --itemize-changes
    rsync_args=(-ac --dry-run --itemize-changes)
    if [[ -d "$src" ]]; then
        [[ $DELETE = 1 ]] && rsync_args+=(--delete)
        diff_output="$(rsync "${rsync_args[@]}" "$src/" "$dst/" 2>&1 | grep -v '^$' | grep -vE '^sending incremental|^sent |^total size|^$' || true)"
    else
        diff_output="$(rsync "${rsync_args[@]}" "$src" "$dst" 2>&1 | grep -v '^$' | grep -vE '^sending incremental|^sent |^total size|^$' || true)"
    fi
    if [[ -z "$diff_output" ]]; then
        printf '  unchanged: %s\n' "$ENTRY_LABEL"
    else
        printf '  CHANGED:   %s\n' "$ENTRY_LABEL"
        echo "$diff_output" | sed 's/^/      /' | head -8
        CHANGES=1
    fi
done
echo

# Agents/skills nudge. agents/claude/ and skills/ ship to $CLAUDE_HOME via
# install.sh at FRESH-INSTALL time — destinations outside $ENGRAM_HOME, so
# deploy.sh cannot reach them, AND install.sh refuses to run on existing
# installs (knowledge.db guard). Until the manifest-external-destinations
# epic lands, the remedy for changed agents/skills on an existing install
# is manual cp. Fire an alert with the actual command so users can copy-
# paste — that's the honest remedy today.
# Fires in both live and dry-run modes (informational either way).
# TARGET-aware: install-gemini.sh does NOT install agents/claude/ (verified
# by reading the script end-to-end). For TARGET=gemini we scope the diff
# to skills/gemini/ only so Gemini users don't see irrelevant agents/
# changes that wouldn't apply to them anyway.
# Note: templates/ drift (CLAUDE.md.template, warm-briefing.md.template,
# HEARTBEAT.md.template, GEMINI.md.template, settings.hooks.json.template)
# is surfaced by the TEMPLATE_FILES warning block below — also fires in
# both live and dry-run modes. This nudge covers only the agents/skills
# install-time files that the existing warning doesn't catch — separate
# drift category, separate remedy (cp instead of inverse-merge).
# Single capture of the previous deploy's SHA, reused by both the agents/
# skills nudge below and the TEMPLATE_FILES warning further down. Both
# blocks need the same baseline; one capture keeps maintenance surface
# minimal.
BASELINE_SHA="$(_last_deployed_sha)"
if [[ -n "$BASELINE_SHA" ]]; then
    if [[ "$TARGET" = "claude" ]]; then
        nudge_paths=(agents/claude/ skills/claude/ skills/shared/)
    else
        nudge_paths=(skills/gemini/ skills/shared/)
    fi
    nudge_changed=$(git -C "$ALPHA_DIR" diff --name-only "$BASELINE_SHA..HEAD" -- "${nudge_paths[@]}" 2>/dev/null || true)
    if [[ -n "$nudge_changed" ]]; then
        # Split changed paths by top-level dir so we surface the right cp command(s).
        nudge_agents_changed=$(echo "$nudge_changed" | grep -E '^agents/' || true)
        nudge_skills_changed=$(echo "$nudge_changed" | grep -E '^skills/' || true)

        echo "⚠ agents/skills changed since last deploy ($BASELINE_SHA → $ALPHA_SHA):"
        echo
        while IFS= read -r f; do
            echo "    $f"
        done <<< "$nudge_changed"
        echo
        echo "  These ship at FRESH-INSTALL time only — deploy.sh cannot reach their"
        echo "  destinations (outside \$ENGRAM_HOME), and install.sh refuses to run on"
        echo "  existing installs. Manual remedy until the manifest-external-destinations"
        echo "  epic lands (issue #166):"
        echo
        if [[ -n "$nudge_agents_changed" && "$TARGET" = "claude" ]]; then
            echo "    cp -rL $ALPHA_DIR/agents/claude/* \$CLAUDE_HOME/agents/"
        fi
        if [[ -n "$nudge_skills_changed" ]]; then
            if [[ "$TARGET" = "claude" ]]; then
                echo "    cp -rL $ALPHA_DIR/skills/claude/* \$CLAUDE_HOME/skills/"
            else
                # Gemini folds skills into GEMINI.md at install time — no parallel
                # runtime skills dir. The manual remedy is to re-run install-gemini.sh
                # --force (archives existing install). Surface this rather than a
                # cp command that wouldn't reach the right destination.
                echo "    (Gemini folds skills into GEMINI.md at install time. The only path"
                echo "     is to back up data + re-run install-gemini.sh --force.)"
            fi
        fi
        echo
        echo "  (templates/ drift — CLAUDE.md / warm-briefing / HEARTBEAT — is covered"
        echo "  separately by the TEMPLATE_FILES warning below; fires in both modes.)"
        echo
    fi
fi

# Template-drift warning. deploy.sh deliberately doesn't touch install-time
# template-rendered files (CLAUDE.md, GEMINI.md, warm-briefing.md, settings.json)
# — those are written ONCE at install time from templates/* and never overwritten.
# But when the ALPHA template files have been updated since the agent's
# last deploy, the agent's live working copy may be missing alpha-material
# changes. This warning surfaces the drift for the agent's intentional
# merge/no-merge judgment (the inverse-merge workflow — manual port from
# live working copy, deliberate PR back to alpha when alpha-material).
#
# settings.hooks.json.template included because hook wiring drift has high
# operational consequence: an upstream hook addition or invocation-path
# change would silently no-op for an agent on a stale rendered settings.json.
#
# Fires in both live and dry-run modes (informational either way — dry-run
# is exactly when users want to see what's about to change).
# BASELINE_SHA was captured earlier (before the agents/skills nudge block);
# reuse it here.
TEMPLATE_FILES=(
    "templates/CLAUDE.md.template"
    "integrations/gemini-cli/GEMINI.md.template"
    "templates/warm-briefing.md.template"
    "templates/HEARTBEAT.md.template"
    "settings.hooks.json.template"
)
if [[ -n "$BASELINE_SHA" ]]; then
    drifted=()
    for tpl in "${TEMPLATE_FILES[@]}"; do
        # git log returns lines for each commit that touched the file in
        # the range. Empty output = no changes since baseline. Use xargs
        # to strip whitespace robustly (tr -d ' ' only handles spaces;
        # some wc implementations emit tabs).
        n_commits=$(git -C "$ALPHA_DIR" log --oneline "$BASELINE_SHA..$ALPHA_SHA" -- "$tpl" 2>/dev/null | wc -l | xargs)
        if [[ "$n_commits" -gt 0 ]]; then
            drifted+=("$tpl ($n_commits commit$( [[ "$n_commits" -eq 1 ]] || echo s ))")
        fi
    done
    if [[ ${#drifted[@]} -gt 0 ]]; then
        echo
        echo "⚠ Install-time template changes since your last deploy ($BASELINE_SHA → $ALPHA_SHA):"
        for d in "${drifted[@]}"; do
            echo "    $d"
        done
        echo
        echo "  These templates render install-time files (CLAUDE.md, GEMINI.md, warm-briefing.md,"
        echo "  settings.json) that deploy.sh deliberately does NOT touch. Your live working copy"
        echo "  may be missing alpha-material changes. Review with:"
        echo "    git -C \"$ALPHA_DIR\" log $BASELINE_SHA..$ALPHA_SHA -- <template-path>"
        echo "  and intentionally port universal-applicable parts into your live config (or skip"
        echo "  if the changes are not alpha-material for your install). Inverse-merge is judgment"
        echo "  work, not automation — both directions can drift."
    fi
fi

if [[ $CHANGES -eq 0 ]]; then
    echo "No changes needed. ENGRAM_HOME is up to date with alpha @ $ALPHA_SHA."
    exit 0
fi

if [[ $DRY_RUN = 1 ]]; then
    echo "Dry-run complete. Re-run without --dry-run to apply."
    exit 0
fi

if [[ $YES = 0 ]]; then
    read -r -p "Apply these changes? [y/N] " confirm
    if [[ ! "$confirm" =~ ^[Yy]$ ]]; then
        echo "Aborted."
        exit 1
    fi
fi

# Apply
SKIP_DST_PATHS=()  # protected files the user chose to keep local (skip rsync)
ABORT_DEPLOY=0
for entry in "${MANIFEST[@]}"; do
    _split_entry "$entry"
    src="$ALPHA_DIR/$ENTRY_SRC"
    dst="$ENGRAM_HOME/$ENTRY_DST"
    [[ ! -e "$src" ]] && continue

    # 3-way conflict check for protected files (SKILL.md etc.). Only applies
    # to single-file manifest entries — directories are out of scope for
    # this layer (handled by per-file rules at install/edit time).
    if [[ ! -d "$src" ]] && [[ " ${PROTECTED_FILES[*]} " == *" $ENTRY_SRC "* ]]; then
        status="$(_check_3way "$ENTRY_SRC")"
        case "$status" in
            no-baseline)
                echo "  warning: no recoverable baseline for $ENTRY_SRC — overwriting (first deploy or orphaned baseline SHA)"
                ;;
            alpha-missing)
                # Defense-in-depth: the early `[[ ! -e "$src" ]] && continue`
                # at the top of this loop already skips manifest entries
                # missing on the alpha side. This branch is reached only if
                # something has gone subtly wrong (race, manifest drift) —
                # warn loudly and skip rather than risk a "false identical"
                # that lets rsync overwrite the local file with nothing.
                echo "  warning: alpha-side file missing for $ENTRY_SRC — skipping (manifest may be drifted)"
                continue
                ;;
            identical|safe-update|new-file)
                : # proceed with normal rsync
                ;;
            conflict)
                if _resolve_conflict "$ENTRY_SRC"; then
                    : # user chose accept-alpha — proceed with rsync
                else
                    rc=$?
                    if [[ $rc -eq 1 ]]; then
                        SKIP_DST_PATHS+=("$dst")
                        continue
                    fi
                    if [[ $rc -eq 2 ]]; then
                        ABORT_DEPLOY=1
                        break
                    fi
                fi
                ;;
        esac
    fi

    if [[ -d "$src" ]]; then
        mkdir -p "$dst"
        rsync_args=(-ac)
        [[ $DELETE = 1 ]] && rsync_args+=(--delete)
        rsync "${rsync_args[@]}" "$src/" "$dst/"
    else
        rsync -ac "$src" "$dst"
    fi
done

if [[ $ABORT_DEPLOY = 1 ]]; then
    echo
    echo "✗ Deploy aborted at user request. Some files may have been updated before the abort."
    echo "  Run again to continue, or resolve the conflicts manually."
    exit 1
fi
if [[ ${#SKIP_DST_PATHS[@]} -gt 0 ]]; then
    echo
    echo "  Files kept-local (alpha-NEW saved as .alpha-pending alongside each):"
    for p in "${SKIP_DST_PATHS[@]}"; do echo "    - $p"; done
fi

# Apply install-time template substitutions that install.sh would have done.
# These need to run after every deploy because rsync overwrites the substituted
# version with the literal-template version from alpha. Without this step, the
# daemon launcher silently fails (literal `{{PYTHON}}` is not a valid command).
# Source of truth: install.sh's sed lines.
PYTHON_BIN="$(python3 -c 'import sys; print(sys.executable)' 2>/dev/null || echo "")"
if [[ -n "$PYTHON_BIN" && -f "$ENGRAM_HOME/hooks/start-engram-daemon.sh" ]]; then
    # `-i.bak` is the BSD/GNU-portable form (works on both macOS and Linux);
    # plain `-i` without an extension argument breaks on macOS BSD sed.
    sed -i.bak "s|{{PYTHON}}|$PYTHON_BIN|g" "$ENGRAM_HOME/hooks/start-engram-daemon.sh"
    rm -f "$ENGRAM_HOME/hooks/start-engram-daemon.sh.bak"
    echo "  ✓ Substituted {{PYTHON}} → $PYTHON_BIN in hooks/start-engram-daemon.sh"
fi

# Stamp version marker for forensics + future "what version is running?" queries
TS="$(date -u +'%Y-%m-%dT%H:%M:%SZ')"
cat > "$ENGRAM_HOME/.deployed-version" <<EOF
alpha_sha=$ALPHA_SHA
alpha_branch=$ALPHA_BRANCH
alpha_dirty=$ALPHA_DIRTY
deployed_at=$TS
deployed_from=$ALPHA_DIR
EOF

echo
echo "✓ Deployed alpha @ $ALPHA_SHA → $ENGRAM_HOME"
echo "  Version marker: $ENGRAM_HOME/.deployed-version"

echo
echo "Next step: restart the MCP server so it loads the new server.py."
echo "  - In Claude Code: run /mcp to reconnect the engram server (or restart Claude Code)."
echo "  - Hooks (context_tracker.py, etc.) reload per-fire automatically."
