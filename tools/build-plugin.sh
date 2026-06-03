#!/usr/bin/env bash
# build-plugin.sh — assembles the ENGRAM Claude Code plugin tree from canonical sources.
#
# OUTPUT: build/plugin/
#   <plugin>/                   plugin root (contents below)
#     server.py                 MCP server + runtime modules
#     engram_*.py               runtime helper modules
#     SKILL.md                  ENGRAM protocol reference
#     bootstrap.py              bootstrap helper (imported by first-session)
#     plugin.json               plugin manifest
#     .mcp.json                 MCP server registration (source: packaging/mcp.json)
#     hooks/
#       hooks.json              plugin hook config
#       *.py *.sh               hook scripts (filtered by tier)
#     skills/                   skill tree (namespaced /engram:<name> by Claude Code)
#     agents/                   agent specs
#     output-styles/            output style markdown files
#     .engram-build-manifest.json   build metadata (tier, multi_agent, shipped paths)
#
# USAGE:
#   bash tools/build-plugin.sh [--tier <essential|convenience|dev>] [--multi-agent] [--help]
#
# The script is idempotent: re-running cleans and re-assembles build/plugin/.
# Run from the repo root (engram-alpha/).
#
# TIER FILTERING: packaging/tiers.json is the single source of truth.
# The predicate is:
#   ships(m) := rank(m.tier) <= rank(chosen_tier)
#               AND (m.multi_agent != true OR --multi-agent given)
# where rank(essential)=0, rank(convenience)=1, rank(dev)=2.
# Default tier: the manifest's default_tier field (currently "convenience").
#
# NOTE: This script does NOT copy knowledge.db or any ~/.engram data. The
# plugin reads/writes ENGRAM_HOME (defaults to ~/.engram) at runtime — that
# data is user-scoped and never part of the plugin tree. See plugin.json.
#
# PYTHON SUBSTITUTION: start-engram-daemon.sh contains {{PYTHON}} template
# placeholders (shared with the legacy install.sh path). The build script
# substitutes {{PYTHON}} → venv python at ~/.engram/venv/ location.

set -euo pipefail

# ── helpers ────────────────────────────────────────────────────────────────────

usage() {
  cat <<'EOF'
Usage: bash tools/build-plugin.sh [--tier <essential|convenience|dev>] [--multi-agent] [--help]

Assembles the ENGRAM Claude Code plugin tree into build/plugin/.

Options:
  --tier <tier>    Depth tier to build: essential, convenience, or dev.
                   Default: the manifest's default_tier (convenience).
                   Cumulative: dev includes convenience includes essential.
  --multi-agent    Include multi-agent-gated mechanisms (baton, letter, forum
                   coordination hooks, inter-agent tools). Off by default.
  --help           Show this help message and exit.

Tier filtering uses packaging/tiers.json as the single source of truth.
The build manifest is written to build/plugin/.engram-build-manifest.json.

The script must be run from the repo root (the directory containing server.py).
Output is written to build/plugin/ — this directory is gitignored.
Re-running is safe: the output tree is cleaned and rebuilt each time.
EOF
  exit 0
}

log() { printf '[build-plugin] %s\n' "$*"; }
die() { printf '[build-plugin] ERROR: %s\n' "$*" >&2; exit 1; }

# ── args ───────────────────────────────────────────────────────────────────────

TIER_ARG=""
MULTI_AGENT=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --help|-h) usage ;;
    --tier)
      [[ $# -ge 2 ]] || die "--tier requires an argument (essential|convenience|dev)"
      TIER_ARG="$2"
      shift 2
      ;;
    --multi-agent) MULTI_AGENT=1; shift ;;
    *) die "Unknown argument: $1. Run with --help for usage." ;;
  esac
done

# ── repo-root guard ────────────────────────────────────────────────────────────

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
if [[ ! -f "$REPO_ROOT/server.py" ]]; then
  die "Must be run from the repo root (expected server.py at $REPO_ROOT/server.py)"
fi

MANIFEST="$REPO_ROOT/packaging/tiers.json"
[[ -f "$MANIFEST" ]] || die "packaging/tiers.json not found at $MANIFEST — is PR-1 merged?"

command -v jq >/dev/null 2>&1 || die "jq is required for manifest parsing but was not found on PATH"

# ── resolve tier ──────────────────────────────────────────────────────────────

MANIFEST_DEFAULT_TIER="$(jq -r '.default_tier' "$MANIFEST")"
CHOSEN_TIER="${TIER_ARG:-$MANIFEST_DEFAULT_TIER}"

# Validate against manifest's declared depth_tiers
VALID_TIERS="$(jq -r '.depth_tiers[]' "$MANIFEST" | tr '\n' '|' | sed 's/|$//')"
if ! jq -e --arg t "$CHOSEN_TIER" '.depth_tiers | map(. == $t) | any' "$MANIFEST" >/dev/null 2>&1; then
  die "Invalid tier '$CHOSEN_TIER'. Valid tiers: $VALID_TIERS"
fi

# Rank mapping: essential=0, convenience=1, dev=2
tier_rank() {
  case "$1" in
    essential)   echo 0 ;;
    convenience) echo 1 ;;
    dev)         echo 2 ;;
    *)           die "Unknown tier for ranking: $1" ;;
  esac
}

CHOSEN_RANK=$(tier_rank "$CHOSEN_TIER")

log "Tier: $CHOSEN_TIER (rank=$CHOSEN_RANK), multi-agent: $( [[ $MULTI_AGENT -eq 1 ]] && echo yes || echo no )"

# ── build the shipping set from the manifest ──────────────────────────────────
#
# ships(m) := rank(m.tier) <= rank(chosen_tier)
#             AND (m.multi_agent != true OR multi_agent_chosen)
#
# Emit a newline-delimited list of manifest paths that satisfy the predicate.

SHIPPED_PATHS="$(jq -r \
  --argjson chosen_rank "$CHOSEN_RANK" \
  --argjson multi_agent "$MULTI_AGENT" \
  '
  def tier_rank:
    if . == "essential"   then 0
    elif . == "convenience" then 1
    elif . == "dev"         then 2
    else error("unknown tier: " + .)
    end;

  .mechanisms[]
  | select(
      (.tier | tier_rank) <= $chosen_rank
      and (
        (has("multi_agent") | not)
        or (.multi_agent != true)
        or ($multi_agent == 1)
      )
    )
  | .path
  ' "$MANIFEST")"

# Put shipped paths in a set for O(1) membership testing (bash associative array)
declare -A SHIP_SET
while IFS= read -r p; do
  [[ -n "$p" ]] && SHIP_SET["$p"]=1
done <<< "$SHIPPED_PATHS"

ships() { [[ "${SHIP_SET[$1]+_}" ]]; }

# ── build target ──────────────────────────────────────────────────────────────

PLUGIN_ROOT="$REPO_ROOT/build/plugin"

log "Cleaning $PLUGIN_ROOT"
rm -rf "$PLUGIN_ROOT"
mkdir -p "$PLUGIN_ROOT"

# Track actually-copied paths for the build manifest
COPIED_PATHS=()

# ── 1. MCP server + runtime modules (always essential → always ship) ──────────

log "Copying runtime modules → plugin root"
for p in server.py bootstrap.py SKILL.md launch-engram-server.sh; do
  ships "$p" || { log "  (tier-filtered) $p"; continue; }
  cp "$REPO_ROOT/$p" "$PLUGIN_ROOT/$p"
  COPIED_PATHS+=("$p")
done
# Ensure launch-engram-server.sh is executable after copy
[[ -f "$PLUGIN_ROOT/launch-engram-server.sh" ]] && chmod +x "$PLUGIN_ROOT/launch-engram-server.sh"

# engram_*.py — all are essential per manifest; copy if they ship
for f in "$REPO_ROOT"/engram_*.py; do
  [[ -f "$f" ]] || die "No engram_*.py files found at $REPO_ROOT"
  bname="$(basename "$f")"
  ships "$bname" || { log "  (tier-filtered) $bname"; continue; }
  cp "$f" "$PLUGIN_ROOT/$bname"
  COPIED_PATHS+=("$bname")
done

# ── 1b. tools/ — manifest-driven filtering (replaces RUNTIME_TOOLS array) ────
#
# The manifest is the single source of truth for which tools/ paths ship.
# Tools tagged essential always ship; convenience/dev only at matching tier.
# Multi-agent tools (ia.py, baton.py, etc.) additionally require --multi-agent.
#
# Guard: every `from tools.X` / `import tools.X` in server.py must be
# tagged essential so it ships at every tier (checked in tests).

log "Copying tools/ → plugin tools/ (manifest-filtered)"
mkdir -p "$PLUGIN_ROOT/tools"

while IFS= read -r p; do
  [[ -z "$p" ]] && continue
  src="$REPO_ROOT/$p"
  if [[ ! -e "$src" ]]; then
    die "Manifest entry $p does not exist at $src"
  fi
  dest="$PLUGIN_ROOT/$p"
  mkdir -p "$(dirname "$dest")"
  if [[ -d "$src" ]]; then
    cp -r "$src/." "$dest/"
  else
    cp "$src" "$dest"
  fi
  COPIED_PATHS+=("$p")
  log "  $p"
done <<< "$(echo "$SHIPPED_PATHS" | grep '^tools/')"

# ── 1c. templates/ — always ship entirely (all tagged essential/convenience) ──
#
# bootstrap.py reads $ALPHA_TEMPLATES_DIR and renders warm-briefing, CLAUDE.md,
# HEARTBEAT.md, etc. Without templates/ the bootstrap step fails.
# Ship the subset that passes the tier filter.

log "Copying templates → plugin templates/"
mkdir -p "$PLUGIN_ROOT/templates"
while IFS= read -r p; do
  [[ -z "$p" ]] && continue
  src="$REPO_ROOT/$p"
  if [[ ! -e "$src" ]]; then
    die "Manifest entry $p does not exist at $src"
  fi
  dest="$PLUGIN_ROOT/$p"
  mkdir -p "$(dirname "$dest")"
  if [[ -d "$src" ]]; then
    cp -a "$src/." "$dest/"
  else
    cp "$src" "$dest"
  fi
  COPIED_PATHS+=("$p")
  log "  $p"
done <<< "$(echo "$SHIPPED_PATHS" | grep '^templates/')"

# ── 2. Manifests ──────────────────────────────────────────────────────────────

log "Copying manifests"
cp "$REPO_ROOT/plugin.json"  "$PLUGIN_ROOT/plugin.json"
COPIED_PATHS+=("plugin.json")
# Source lives at packaging/mcp.json, NOT repo-root .mcp.json — a file literally
# named `.mcp.json` at the repo root is auto-loaded by Claude Code as a
# project-scope MCP server for every dev-mode clone, conflicting with the
# working user-scope engram and knocking it offline on restart (#618). The repo
# is never *recognized* as a plugin; build-plugin.sh reconstructs the format, so
# the source path is arbitrary. It is renamed to `.mcp.json` here at the plugin
# root, where the packaged-plugin format expects it.
cp "$REPO_ROOT/packaging/mcp.json" "$PLUGIN_ROOT/.mcp.json"
COPIED_PATHS+=("packaging/mcp.json")

# ── 3. Hooks — manifest-filtered (replaces HOOKS_EXCLUDE array) ───────────────

log "Copying hooks → plugin hooks/ (manifest-filtered)"
mkdir -p "$PLUGIN_ROOT/hooks"

# Filter hooks.json to include ONLY entries whose hook file ships at this
# tier/multi-agent config.  The source hooks/hooks.json references every hook
# across all tiers; shipping it wholesale would register hooks whose files are
# absent, producing a UserPromptSubmit FileNotFoundError on every prompt for
# non-MA / lower-tier installs (#713 root cause).
#
# Algorithm:
#   1. Read hooks/hooks.json.
#   2. For each hook entry whose command references a hooks/<file>, drop the
#      entry if <file> is NOT in the shipping set.
#   3. After dropping entries, prune event-arrays / group-arrays that became
#      empty, so the output JSON stays valid and minimal.
#   4. Write the filtered result to $PLUGIN_ROOT/hooks/hooks.json.
#
# Uses python3 (already a hard build-time dependency; the nested JSON structure
# is awkward to manipulate correctly with jq alone).
#
# The shipped hook basenames are derived directly from SHIPPED_PATHS (computed
# above) by filtering for hooks/claude/* entries.  The filter runs BEFORE the
# hook-file copy loop so the built hooks.json is in place before hook scripts
# are written alongside it.

log "Filtering hooks.json for tier=$CHOSEN_TIER multi-agent=$( [[ $MULTI_AGENT -eq 1 ]] && echo yes || echo no )"
python3 - "$REPO_ROOT" "$PLUGIN_ROOT" "$SHIPPED_PATHS" <<'PYEOF'
import json
import os
import re
import sys

repo_root   = sys.argv[1]
plugin_root = sys.argv[2]
shipped_paths_raw = sys.argv[3] if len(sys.argv) > 3 else ""

# Build set of shipped hook basenames from the shipped paths list.
shipped_hook_names: set[str] = set()
for line in shipped_paths_raw.splitlines():
    line = line.strip()
    if line.startswith("hooks/claude/"):
        shipped_hook_names.add(os.path.basename(line))

HOOK_REF_RE = re.compile(r'\bhooks/([A-Za-z0-9_\-]+\.(?:py|sh))\b')

src_path = os.path.join(repo_root, "hooks", "hooks.json")
with open(src_path, encoding="utf-8") as f:
    data = json.load(f)

def hook_ships(entry: dict) -> bool:
    """Return True iff this hook entry should be kept at the current build config.

    An entry is dropped if its command references a hooks/<file> that is NOT in
    the shipped set.  Entries with no hooks/<file> reference are always kept.
    """
    cmd = entry.get("command", "")
    m = HOOK_REF_RE.search(cmd)
    if m is None:
        return True  # no hook-file reference → keep unconditionally
    return m.group(1) in shipped_hook_names

filtered_hooks: dict = {}
for event_name, groups in data.get("hooks", {}).items():
    filtered_groups = []
    for group in groups:
        raw_entries = group.get("hooks", [])
        kept_entries = [e for e in raw_entries if hook_ships(e)]
        if not kept_entries:
            # All entries in this group were dropped → prune the group.
            continue
        filtered_group = {k: v for k, v in group.items() if k != "hooks"}
        filtered_group["hooks"] = kept_entries
        filtered_groups.append(filtered_group)
    if not filtered_groups:
        # All groups for this event were pruned → omit the event key.
        continue
    filtered_hooks[event_name] = filtered_groups

out = {"hooks": filtered_hooks}
dest_path = os.path.join(plugin_root, "hooks", "hooks.json")
with open(dest_path, "w", encoding="utf-8") as f:
    json.dump(out, f, indent=2)
    f.write("\n")

dropped = sum(
    1 for groups in data.get("hooks", {}).values()
    for group in groups
    for entry in group.get("hooks", [])
    if not hook_ships(entry)
)
kept = sum(
    len(group.get("hooks", []))
    for groups in filtered_hooks.values()
    for group in groups
)
print(f"[build-plugin] hooks.json filtered: {kept} entries kept, {dropped} dropped (tier/MA excluded)")
PYEOF

COPIED_PATHS+=("hooks/hooks.json")

# Copy hook scripts filtered through the manifest predicate.
# Previously build-plugin.sh maintained a HOOKS_EXCLUDE array of paths to skip;
# that array is now deleted — the manifest tier tags are the sole authority.
HOOKS_DIR="$REPO_ROOT/hooks/claude"
while IFS= read -r src; do
  fname="$(basename "$src")"
  manifest_key="hooks/claude/$fname"
  if ships "$manifest_key"; then
    cp "$src" "$PLUGIN_ROOT/hooks/$fname"
    COPIED_PATHS+=("$manifest_key")
    log "  $manifest_key"
  else
    log "  (tier-filtered) $manifest_key"
  fi
done < <(find "$HOOKS_DIR" -maxdepth 1 \( -name "*.py" -o -name "*.sh" \) | sort)

# Substitute {{PYTHON}} → venv python at the canonical ~/.engram/venv/ location.
# The literal $HOME expands at script-run time (not build time), so the
# plugin works under any user's home. Earlier substitution to bare
# 'python3' broke the surface daemon because fastmcp + sentence-transformers
# only live in the venv, not system python (Lei Chromebook trial 2026-05-31).
if [[ -f "$PLUGIN_ROOT/hooks/start-engram-daemon.sh" ]]; then
  sed -i 's|{{PYTHON}}|$HOME/.engram/venv/bin/python3|g' "$PLUGIN_ROOT/hooks/start-engram-daemon.sh"
  log "  Substituted {{PYTHON}} → \$HOME/.engram/venv/bin/python3 in start-engram-daemon.sh"
fi

# ── 3b. settings.hooks.json.template — filter MA hook same as hooks.json ──────
#
# The template lives at the repo root and ships to the plugin so users can
# copy it verbatim when setting up settings.json (scatter-mode or manual
# plugin-mode wiring).  Without filtering, the MA-only hook
# (engram-inter-agent-prompt-hook.py) remains in the template even on
# single-agent builds — the script is absent, so every UserPromptSubmit
# raises FileNotFoundError (#733, same root cause as #713/#714).
#
# Filter: same logic as hooks.json — drop any hook entry whose command
# references a hook file that is not in the shipped set.  The template uses
# {{ENGRAM_HOOKS_DIR}}/hook.py rather than hooks/hook.py, so a separate
# regex is used to extract the filename.

log "Filtering settings.hooks.json.template for tier=$CHOSEN_TIER multi-agent=$( [[ $MULTI_AGENT -eq 1 ]] && echo yes || echo no )"
python3 - "$REPO_ROOT" "$PLUGIN_ROOT" "$SHIPPED_PATHS" <<'PYEOF'
import json
import os
import re
import sys

repo_root             = sys.argv[1]
plugin_root           = sys.argv[2]
shipped_paths_raw     = sys.argv[3] if len(sys.argv) > 3 else ""

shipped_hook_names: set[str] = set()
for line in shipped_paths_raw.splitlines():
    line = line.strip()
    if line.startswith("hooks/claude/"):
        shipped_hook_names.add(os.path.basename(line))

# Template commands use {{ENGRAM_HOOKS_DIR}}/hook.py (not hooks/hook.py).
TMPL_HOOK_REF_RE = re.compile(r'\{\{ENGRAM_HOOKS_DIR\}\}/([A-Za-z0-9_\-]+\.(?:py|sh))\b')

def hook_ships_tmpl(entry: dict) -> bool:
    cmd = entry.get("command", "")
    m = TMPL_HOOK_REF_RE.search(cmd)
    if m is None:
        return True
    return m.group(1) in shipped_hook_names

src_path = os.path.join(repo_root, "settings.hooks.json.template")
if not os.path.exists(src_path):
    print("[build-plugin] settings.hooks.json.template not found in repo root — skipping")
    sys.exit(0)

with open(src_path, encoding="utf-8") as f:
    data = json.load(f)

# Template structure: event → list of groups → hooks entries (flat; no outer
# "hooks" wrapper — hooks.json wraps all events under {"hooks": {...}}).
filtered_hooks: dict = {}
for event_name, groups in data.items():
    filtered_groups = []
    for group in groups:
        raw_entries = group.get("hooks", [])
        kept_entries = [e for e in raw_entries if hook_ships_tmpl(e)]
        if not kept_entries:
            continue
        filtered_group = {k: v for k, v in group.items() if k != "hooks"}
        filtered_group["hooks"] = kept_entries
        filtered_groups.append(filtered_group)
    if not filtered_groups:
        continue
    filtered_hooks[event_name] = filtered_groups

dest_path = os.path.join(plugin_root, "settings.hooks.json.template")
with open(dest_path, "w", encoding="utf-8") as f:
    json.dump(filtered_hooks, f, indent=2)
    f.write("\n")

dropped = sum(
    1 for groups in data.values()
    for group in groups
    for entry in group.get("hooks", [])
    if not hook_ships_tmpl(entry)
)
kept_total = sum(
    len(group.get("hooks", []))
    for groups in filtered_hooks.values()
    for group in groups
)
print(f"[build-plugin] settings.hooks.json.template: {kept_total} entries kept, {dropped} dropped (tier/MA excluded)")
PYEOF

[[ -f "$PLUGIN_ROOT/settings.hooks.json.template" ]] && COPIED_PATHS+=("settings.hooks.json.template")

# ── 4. Skills — manifest-filtered (replaces wholesale cp -r) ──────────────────

log "Copying skills → plugin skills/ (manifest-filtered)"
mkdir -p "$PLUGIN_ROOT/skills"

SKILLS_DIR="$REPO_ROOT/skills/claude"
if [[ ! -d "$SKILLS_DIR" ]]; then
  die "skills/claude/ not found at $SKILLS_DIR"
fi
for skill_dir in "$SKILLS_DIR"/*/; do
  skill_name="$(basename "$skill_dir")"
  manifest_key="skills/claude/$skill_name"
  if ships "$manifest_key"; then
    cp -r "$skill_dir" "$PLUGIN_ROOT/skills/$skill_name"
    COPIED_PATHS+=("$manifest_key")
    log "  $manifest_key"
  else
    log "  (tier-filtered) $manifest_key"
  fi
done

# ── 5. Agents — manifest-filtered (replaces wholesale find) ───────────────────

log "Copying agents → plugin agents/ (manifest-filtered)"
mkdir -p "$PLUGIN_ROOT/agents"

AGENTS_DIR="$REPO_ROOT/agents/claude"
if [[ ! -d "$AGENTS_DIR" ]]; then
  die "agents/claude/ not found at $AGENTS_DIR"
fi
while IFS= read -r src; do
  fname="$(basename "$src")"
  manifest_key="agents/claude/$fname"
  if ships "$manifest_key"; then
    cp "$src" "$PLUGIN_ROOT/agents/$fname"
    COPIED_PATHS+=("$manifest_key")
    log "  $manifest_key"
  else
    log "  (tier-filtered) $manifest_key"
  fi
done < <(find "$AGENTS_DIR" -maxdepth 1 -name "*.md" ! -name "README.md" | sort)

# ── 6. Output styles — manifest-filtered (replaces wholesale cp -r) ───────────

log "Copying output-styles → plugin output-styles/ (manifest-filtered)"
mkdir -p "$PLUGIN_ROOT/output-styles"

STYLES_DIR="$REPO_ROOT/output-styles/claude"
if [[ ! -d "$STYLES_DIR" ]]; then
  die "output-styles/claude/ not found at $STYLES_DIR"
fi
for src in "$STYLES_DIR"/*; do
  [[ -f "$src" ]] || continue
  fname="$(basename "$src")"
  manifest_key="output-styles/claude/$fname"
  if ships "$manifest_key"; then
    cp "$src" "$PLUGIN_ROOT/output-styles/$fname"
    COPIED_PATHS+=("$manifest_key")
    log "  $manifest_key"
  else
    log "  (tier-filtered) $manifest_key"
  fi
done

# ── 6b. hooks.json ↔ shipped-files consistency gate (all-or-nothing) ─────────
#
# After filtering hooks.json (step 3) and copying hook scripts (also step 3):
# every file referenced in the BUILT hooks/hooks.json MUST exist on disk.
# No skipping — the filter step already removed entries for non-shipped hooks,
# so any reference that remains in hooks.json IS expected to be present.
# If any referenced file is missing, the build fails (exit≠0).
#
# This is the all-or-nothing version of the round-1 gate.  Round-1 skipped
# references to tier-excluded hooks, which produced a FALSE PASS: a default
# (non-MA) build had engram-inter-agent-prompt-hook.py still in hooks.json
# (because hooks.json was copied wholesale) but absent from the hooks/ dir,
# and the gate silently passed it as "intentionally excluded".  The filtering
# step in section 3 corrects the root cause; this gate enforces the invariant
# that the correction actually worked. (#713 round-2)

log "Checking hooks.json ↔ shipped-files consistency (all-or-nothing)"
python3 - "$PLUGIN_ROOT" <<'PYEOF'
import json
import os
import re
import sys

plugin_root = sys.argv[1]

hooks_json_path = os.path.join(plugin_root, "hooks", "hooks.json")

with open(hooks_json_path, encoding="utf-8") as f:
    data = json.load(f)

# Extract every path of the form hooks/<file> from command strings.
HOOK_REF_RE = re.compile(r'\bhooks/([A-Za-z0-9_\-]+\.(?:py|sh))\b')

referenced: list[str] = []
hooks_root = data.get("hooks", {})
for event_name, groups in hooks_root.items():
    for group in groups:
        for entry in group.get("hooks", []):
            cmd = entry.get("command", "")
            for m in HOOK_REF_RE.finditer(cmd):
                referenced.append(m.group(1))

missing: list[str] = []
for fname in set(referenced):
    fpath = os.path.join(plugin_root, "hooks", fname)
    if not os.path.exists(fpath):
        missing.append(fname)

if missing:
    print("[build-plugin] ERROR: built hooks.json references hook file(s) that are MISSING from the plugin:")
    for f in sorted(missing):
        print(f"[build-plugin]   MISSING: hooks/{f}")
    print("[build-plugin] The hooks.json filter step should have dropped these entries.")
    print("[build-plugin] Check the filtering logic in build-plugin.sh step 3.")
    sys.exit(1)

print(f"[build-plugin] hooks.json ↔ shipped-files: OK ({len(set(referenced))} referenced, all present)")
PYEOF

# ── 6c. settings.hooks.json.template ↔ shipped-files consistency gate ─────────
#
# Mirror of 6b for the template.  Every hook referenced in the BUILT
# settings.hooks.json.template must exist in the shipped hooks/ directory.
# The filter in step 3b should guarantee this; this gate enforces it.

log "Checking settings.hooks.json.template ↔ shipped-files consistency"
python3 - "$PLUGIN_ROOT" <<'PYEOF'
import json
import os
import re
import sys

plugin_root = sys.argv[1]

tmpl_path = os.path.join(plugin_root, "settings.hooks.json.template")
if not os.path.exists(tmpl_path):
    print("[build-plugin] settings.hooks.json.template not in plugin — skipping consistency check")
    sys.exit(0)

with open(tmpl_path, encoding="utf-8") as f:
    data = json.load(f)

TMPL_HOOK_REF_RE = re.compile(r'\{\{ENGRAM_HOOKS_DIR\}\}/([A-Za-z0-9_\-]+\.(?:py|sh))\b')

referenced: list[str] = []
for groups in data.values():
    for group in groups:
        for entry in group.get("hooks", []):
            cmd = entry.get("command", "")
            for m in TMPL_HOOK_REF_RE.finditer(cmd):
                referenced.append(m.group(1))

missing: list[str] = []
for fname in set(referenced):
    fpath = os.path.join(plugin_root, "hooks", fname)
    if not os.path.exists(fpath):
        missing.append(fname)

if missing:
    print("[build-plugin] ERROR: settings.hooks.json.template references hook file(s) MISSING from plugin:")
    for f in sorted(missing):
        print(f"[build-plugin]   MISSING: hooks/{f}")
    print("[build-plugin] Check the filtering logic in build-plugin.sh step 3b.")
    sys.exit(1)

print(f"[build-plugin] settings.hooks.json.template ↔ shipped-files: OK ({len(set(referenced))} referenced, all present)")
PYEOF

# ── 7. Build manifest — record tier choice + shipped paths ────────────────────

BUILD_MANIFEST_PATH="$PLUGIN_ROOT/.engram-build-manifest.json"
log "Writing build manifest → $BUILD_MANIFEST_PATH"

# Build JSON array of copied paths using jq
PATHS_JSON="$(printf '%s\n' "${COPIED_PATHS[@]}" | jq -R . | jq -s .)"
jq -n \
  --arg tier "$CHOSEN_TIER" \
  --argjson multi_agent "$( [[ $MULTI_AGENT -eq 1 ]] && echo true || echo false )" \
  --argjson paths "$PATHS_JSON" \
  '{tier: $tier, multi_agent: $multi_agent, shipped_paths: $paths}' \
  > "$BUILD_MANIFEST_PATH"

# ── done ──────────────────────────────────────────────────────────────────────

log ""
log "Plugin tree assembled at: $PLUGIN_ROOT"
log "  tier=$CHOSEN_TIER  multi_agent=$( [[ $MULTI_AGENT -eq 1 ]] && echo true || echo false )"
log ""
log "Contents:"
find "$PLUGIN_ROOT" -not -path '*/__pycache__/*' -not -name '*.pyc' | sort | sed "s|$PLUGIN_ROOT/||" | sed "s|^|  |"
log ""
log "To verify manifest JSON:"
log "  python3 -c \"import json; json.load(open('$PLUGIN_ROOT/plugin.json'))\" && echo OK"
log "  python3 -c \"import json; json.load(open('$PLUGIN_ROOT/.mcp.json'))\" && echo OK"
log "  python3 -c \"import json; json.load(open('$PLUGIN_ROOT/hooks/hooks.json'))\" && echo OK"
log "  python3 -c \"import json; json.load(open('$PLUGIN_ROOT/.engram-build-manifest.json'))\" && echo OK"
