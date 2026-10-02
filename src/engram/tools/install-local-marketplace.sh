#!/usr/bin/env bash
# install-local-marketplace.sh — assembles a local ENGRAM plugin marketplace
# from build/plugin/ and optionally registers it with the host CLI.
#
# OUTPUT: $ENGRAM_HOME/marketplace/ (default ~/.engram/marketplace/)
#   .claude-plugin/
#     marketplace.json          marketplace manifest pointing at plugins/engram
#   plugins/
#     engram/                   = contents of build/plugin/ (the assembled plugin tree)
#       .mcp.json
#       plugin.json
#       server.py
#       bootstrap.py
#       launch-engram-server.sh
#       hooks/
#       skills/
#       agents/
#       ...
#
# Then runs `claude plugin marketplace add $ENGRAM_HOME/marketplace` for Claude
# Code targets. Codex targets are installed with `codex plugin add engram@engram-local`.
#
# USAGE:
#   bash tools/install-local-marketplace.sh [--help] [--skip-add] [--skip-build] [--target <claude-code|codex>]
#
# OPTIONS:
#   --help        Show this help and exit.
#   --skip-add    Assemble the marketplace tree but skip the
#                 `claude plugin marketplace add` step.
#   --skip-build  Skip the internal build-plugin.sh invocation entirely.
#                 Use this when the caller has already built build/plugin/ with
#                 the correct flags (e.g., migrate-to-plugin.sh Step 2) and you
#                 do not want this script to clobber that build with a flagless
#                 rebuild.  build/plugin/plugin.json must already exist.
#   --target T    Build target: claude-code (default) or codex.
#
# PRECONDITIONS:
#   - Repo cloned + this script run from the repo root.
#   - build/plugin/ already assembled via `bash tools/build-plugin.sh`. If it
#     doesn't exist, this script invokes build-plugin.sh first.
#   - Phase 0 prereqs done (venv at ~/.engram/venv/ with requirements
#     installed). This script does NOT install Python deps — see README.md.
#
# WHY A LOCAL MARKETPLACE?
#   Claude Code's `/plugin install <path>` slash command does NOT accept
#   raw local paths — it only resolves plugin references via a registered
#   marketplace. A "marketplace" in this context can be a LOCAL DIRECTORY
#   containing .claude-plugin/marketplace.json + plugins/<name>/. This
#   script assembles such a directory at $ENGRAM_HOME/marketplace/ so the
#   user can install via the canonical `/plugin install engram` once the
#   marketplace is registered.
#
# IDEMPOTENT: re-running cleans + reassembles the marketplace tree.

set -euo pipefail

log() { printf '[install-local-marketplace] %s\n' "$*"; }
die() { printf '[install-local-marketplace] ERROR: %s\n' "$*" >&2; exit 1; }

# Venv-provisioning failure flag + EXIT-trap surfacing. The installer has
# several early `exit 0` short-circuits (--skip-add, codex-not-found, the
# codex plugin path) that fire BEFORE the end of the script, so an end-of-
# script check would be unreachable from those paths. A runtime-requirements
# install failure (see the tier-aware venv block) must surface as a non-zero
# exit on EVERY path — a successful exit over a venv that can't start the
# server is the silent-broken-venv failure mode this PR exists to prevent
# (Lei, 2026-08-10). The trap catches all exits; it is a no-op when the
# venv block never flagged (the normal case).
_venv_fail=0
_venv_fail_trap() {
  if [[ "${_venv_fail:-0}" == "1" ]]; then
    log ""
    log "ERROR: venv runtime-requirements provisioning failed."
    log "       The MCP server may not start until the venv is repaired."
    log "       Re-run this installer to retry, or inspect the pip output above."
    exit 1
  fi
}
trap _venv_fail_trap EXIT

usage() {
  cat <<'EOF'
Usage: bash tools/install-local-marketplace.sh [--help] [--skip-add] [--skip-build] [--target <claude-code|codex>] [--allow-branch] [--allow-untagged]

Assembles a local ENGRAM marketplace at $ENGRAM_HOME/marketplace/ from
build/plugin/. With --target claude-code it registers via `claude plugin marketplace add`;
with --target codex it installs via `codex plugin add engram@engram-local`.

Options:
  --help            Show this help and exit.
  --skip-add        Assemble but skip the `claude plugin marketplace add` step.
  --skip-build      Skip the internal build-plugin.sh invocation. Use when the
                    caller has already built build/plugin/ with the correct flags.
                    build/plugin/plugin.json must already exist.
  --target T        Build target to pass through when not using --skip-build:
                    claude-code (default) or codex.
  --allow-branch    Allow building from a PR/feature branch (overrides #794's guard; 'dev' and 'main' always allowed).
                    Use only for deliberate branch builds (e.g. metric-eval checkouts).
  --allow-untagged  Allow building from a commit that carries no deployable tag
                    (overrides the tag guard, #2586). Deployable tags are v* and
                    deploy/* at HEAD — the same set the health-snapshot versioning
                    gate (#2563) treats as the deployable predecessors. Use only for
                    a deliberate untagged deploy (e.g. certifying an unreleased
                    merge commit); .deployed-version records alpha_tag=none.

After running:
  $ENGRAM_HOME/marketplace/.claude-plugin/marketplace.json
  $ENGRAM_HOME/marketplace/plugins/engram/...

Then in a Claude Code session:
  /plugin install engram

Or for Codex:
  bash tools/install-local-marketplace.sh --target codex

To upgrade later:
  cd path/to/engram
  git pull
  bash tools/install-local-marketplace.sh    # builds + re-assembles, idempotent
  # then in Claude Code:
  /plugin marketplace update engram-local
  /plugin -> Installed -> engram plugin (not the MCP under it) -> Update now
  /mcp -> engram -> Reconnect
  # or in Codex:
  bash tools/install-local-marketplace.sh --target codex
EOF
  exit 0
}

SKIP_ADD=0
SKIP_BUILD=0
TARGET="claude-code"
ALLOW_BRANCH=0
ALLOW_UNTAGGED=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    --help|-h) usage ;;
    --skip-add) SKIP_ADD=1; shift ;;
    --skip-build) SKIP_BUILD=1; shift ;;
    --allow-branch) ALLOW_BRANCH=1; shift ;;
    --allow-untagged) ALLOW_UNTAGGED=1; shift ;;
    --target)
      [[ $# -ge 2 ]] || die "--target requires a value: claude-code or codex"
      TARGET="$2"
      shift 2
      ;;
    --target=*) TARGET="${1#--target=}"; shift ;;
    *) die "Unknown argument: $1. Run with --help for usage." ;;
  esac
done
case "$TARGET" in
  claude-code|codex) ;;
  *) die "Invalid --target: $TARGET (expected claude-code or codex)" ;;
esac

_self="$(readlink -f "${BASH_SOURCE[0]}" 2>/dev/null || echo "${BASH_SOURCE[0]}")"
REPO_ROOT="$(git -C "$(dirname "$_self")" rev-parse --show-toplevel 2>/dev/null)"
[ -n "$REPO_ROOT" ] || { echo "install-local-marketplace: ERROR — could not resolve repo root via git" >&2; exit 1; }
if [[ ! -f "$REPO_ROOT/src/engram/server.py" ]]; then
  die "Must be run from the repo root (expected src/engram/server.py at $REPO_ROOT/src/engram/server.py)"
fi

# Raw tags are diagnostics only. Authorization comes solely from the shared
# Python refresh-and-check wrapper below, so the shell and engine cannot drift.
DEPLOY_TAGS="$(git -C "$REPO_ROOT" tag --points-at HEAD 2>/dev/null | grep -E '^(v|deploy/)' || true)"

# Refresh is bounded inside refresh_and_check_deployable_tag_at() by Python's
# private POSIX fetch group, so seats need no timeout/gtimeout binary. A failed
# refresh still uses LOCAL cached origin/dev or origin/main refs; that does not
# prove current remote history (a remote force-rewind is not observable here).
# Parse before invoking the wrapper so malformed config never becomes an
# offline/local-ref authorization fallback.
# `${var:-default}` would silently turn an explicitly empty setting into the
# default.  Keep unset as the default but pass an empty supplied value to the
# validator so "missing number" is a loud configuration error.
if [[ "${ENGRAM_INSTALL_FETCH_TIMEOUT_SECS+x}" == "x" ]]; then
  FETCH_TIMEOUT_INPUT="$ENGRAM_INSTALL_FETCH_TIMEOUT_SECS"
else
  FETCH_TIMEOUT_INPUT="15"
fi
if ! FETCH_TIMEOUT_SECS="$(PYTHONPATH="$REPO_ROOT" python3 -c "
from src.build.engine.deploy_tag import parse_timeout_seconds
import sys
try:
    print(parse_timeout_seconds(sys.argv[1]))
except ValueError:
    sys.exit(2)
" "$FETCH_TIMEOUT_INPUT")"; then
  die "Invalid ENGRAM_INSTALL_FETCH_TIMEOUT_SECS '$FETCH_TIMEOUT_INPUT': expected a positive finite decimal with optional s/m/h/d suffix."
fi
DEPLOYABLE_TAG=""
if [[ -n "$DEPLOY_TAGS" ]] && ! DEPLOYABLE_TAG="$(PYTHONPATH="$REPO_ROOT" python3 -c "
from src.build.engine.deploy_tag import refresh_and_check_deployable_tag_at
import sys
print(refresh_and_check_deployable_tag_at(sys.argv[1], timeout=float(sys.argv[2])) or '')
" "$REPO_ROOT" "$FETCH_TIMEOUT_SECS")"; then
  die "Could not evaluate deployable tag after validating ENGRAM_INSTALL_FETCH_TIMEOUT_SECS."
fi

# #794 branch guard: refuse builds from PR/feature branches unless --allow-branch is set,
# OR HEAD is at a deployable tag that is merged history (the #2586/#2711 exception above).
# Valid production branches: 'dev' (private dev source) and 'main' (public release branch).
# Every other branch — PR branches, feature branches, etc. — requires --allow-branch to override
# (or a deploy-tag checkout, which needs no flag at all).
CURRENT_BRANCH="$(git -C "$REPO_ROOT" rev-parse --abbrev-ref HEAD 2>/dev/null || echo unknown)"
if [[ "$CURRENT_BRANCH" != "dev" && "$CURRENT_BRANCH" != "main" && -z "$DEPLOYABLE_TAG" && $ALLOW_BRANCH -eq 0 ]]; then
  die "Source repo is on branch '$CURRENT_BRANCH', not 'dev' (private dev) or 'main' (public release). Building from a PR/feature branch can deploy unmerged code. Pass --allow-branch to override (e.g. for metric-eval checkouts), or check out a deployable tag (v*/deploy/*) that is merged into origin/dev or origin/main — no flag needed for that. Guard per #794 (tag exception per #2586/#2711)."
fi

# #2586/#3103 tag guard: refuse builds unless the shared wrapper confirms
# HEAD carries a deployable tag merged into locally available origin/dev or
# origin/main history — not merely "does a deployable-pattern tag point at
# HEAD" (DEPLOY_TAGS non-empty). Before
# #3103 this guard decided on DEPLOY_TAGS alone, independent of ancestry —
# so a same-named LOCAL tag on an unmerged/unpushed commit (e.g. a local
# 'dev' checkout with an unpushed child commit tagged v9.9.9) passed this
# guard even though the #794 guard's own shared-wrapper result,
# a few lines above, had already determined HEAD is NOT confirmed-merged —
# the shell and the engine's `cli run upgrade` path (which routes entirely
# through refresh_and_check_deployable_tag_at()) disagreed on exactly this
# case. Three distinct cases now, not two:
#   1. No tag at all (DEPLOY_TAGS empty) — refuse; "not a tagged commit".
#   2. Tag present AND confirmed ancestor (DEPLOYABLE_TAG non-empty) — proceed.
#   3. Tag present but NOT confirmed ancestor (genuinely unmerged/unpushed,
#      OR local tracked refs are absent/do not contain it) — refuse, but say so
#      accurately: the tag exists, it just isn't confirmed merged into
#      origin/dev/origin/main, so the guard doesn't claim "not a tagged
#      commit" when it plainly is.
#
# This sits before the --target dispatch so both Claude and Codex install
# paths get it — the guard lives in the script, not in the engram-upgrade
# skill (see #2586's issue body).
#
# TODO(#2586 follow-up): a future `--verify` mode (compare the deployed tree
# to the recorded tag's source) must apply the build's template substitution
# before diffing, or its first result on every seat is a false positive
# (start-engram-daemon.sh's {{PYTHON}} token is the concrete example Borges
# measured) — and it must name its baseline (the seat's own recorded
# alpha_tag, never the dev tip) in its own output, or a fully-consistent seat
# reads as drifted against the wrong reference.
ALPHA_UNTAGGED_OVERRIDE_FIRED=0
if [[ -z "$DEPLOYABLE_TAG" ]]; then
  SHORT_SHA="$(git -C "$REPO_ROOT" rev-parse --short HEAD 2>/dev/null || echo unknown)"
  NEAREST_TAG="$(git -C "$REPO_ROOT" describe --tags --abbrev=0 2>/dev/null || echo none)"
  if [[ -z "$DEPLOY_TAGS" ]]; then
    TAG_GUARD_MSG="HEAD ($SHORT_SHA) is not a tagged commit (deployable tags = v* and deploy/*; nearest by ancestry: $NEAREST_TAG). The health-snapshot versioning gate (engram-alpha#2563) treats tags as the deployable set; snapshots written from untagged code are invisible to it and can orphan your health history. Rebuild from a tag (git fetch --tags origin first if your tags are stale), or pass --allow-untagged to proceed anyway — the deploy log repeats this warning and .deployed-version records alpha_tag=none. Guard per #2586."
  else
    TAG_GUARD_MSG="HEAD ($SHORT_SHA) carries a deployable-looking tag ($DEPLOY_TAGS) but that tag is NOT confirmed to be merged into locally available origin/dev or origin/main (nearest by ancestry: $NEAREST_TAG) — treating HEAD as unconfirmed merged history, same as the #794 guard above. This can mean the tag is on genuinely unmerged/unpushed work, or that local tracked refs are absent or do not contain it. The health-snapshot versioning gate (engram-alpha#2563) treats CONFIRMED-merged tags as the deployable set; snapshots written from unconfirmed code are invisible to it and can orphan your health history. Push/merge this commit into origin/dev or origin/main and re-fetch, or pass --allow-untagged to proceed anyway — the deploy log repeats this warning and .deployed-version records alpha_tag=none. Guard per #2586/#3103."
  fi
  if [[ $ALLOW_UNTAGGED -eq 0 ]]; then
    die "$TAG_GUARD_MSG"
  else
    log "WARNING: $TAG_GUARD_MSG"
    ALPHA_UNTAGGED_OVERRIDE_FIRED=1
  fi
  ALPHA_TAG="none"
else
  ALPHA_TAG="$DEPLOYABLE_TAG"
  log "Deployable tag(s) at HEAD: $ALPHA_TAG"
fi

ENGRAM_HOME="${ENGRAM_HOME:-$HOME/.engram}"
MARKETPLACE_ROOT="$ENGRAM_HOME/marketplace"
PLUGIN_SRC="${ENGRAM_BUILD_DIR:-$REPO_ROOT/build/plugin}"

# Build the plugin tree unless --skip-build was passed.
#
# When called standalone (no --skip-build), read $ENGRAM_HOME/config.json for
# {multi_agent, install_tier} and pass the matching flags to build-plugin.sh.
# This mirrors the build_flags pattern in migrate-to-plugin.sh (step2_install_plugin)
# and makes this script self-sufficient: any caller gets a config-correct build
# rather than a silent single-agent/convenience default.
#
# When called with --skip-build (e.g., from migrate-to-plugin.sh after it has
# already run build-plugin.sh with the correct flags), the existing build/plugin/
# tree is used directly — no second flagless rebuild that would clobber the first.
if [[ $SKIP_BUILD -eq 1 ]]; then
  log "Skipping build-plugin.sh invocation (--skip-build)"
else
  # Assemble build flags from config.json (same pattern as migrate-to-plugin.sh).
  build_flags=()
  _cfg="$ENGRAM_HOME/config.json"
  if [[ -f "$_cfg" ]]; then
    # Read multi_agent/install_tier via python3, not jq (#2955) — python3 is
    # already a hard requirement of this script (see the config-persist block
    # below), so there is nothing left to require a system jq binary for.
    # One python3 invocation reads both fields from a single parse (cheaper
    # than two separate calls, and keeps the pair atomically consistent) and
    # prints them as two stdout lines. This is a DELIBERATE style choice
    # against the later _eff_tier/_eff_multi_agent block (~line 497 below),
    # which makes two separate python3 -c calls — that shape predates this
    # fix and is unrelated pre-existing code (its own silent-exception-swallow
    # anti-pattern is tracked separately under #2951; not the model to follow
    # here). On ANY parse error this block prints a clear message to stderr
    # and exits non-zero — the same fail-loud contract the old jq path got
    # for free from `set -e`, just with a cleaner message than a raw
    # traceback.
    # PORTABILITY LANDMINE (#2979): bash 3.2 (macOS's system bash) requires an
    # EVEN total count of literal ' characters across this heredoc's body when
    # the heredoc is nested in a $(...) command substitution like this one --
    # an odd count (even from an English apostrophe in a comment!) desyncs
    # bash 3.2's naive quote-balance scan for the substitution's closing paren
    # and produces a spurious syntax error dozens of lines later. Count single
    # quotes before adding/editing any comment or string inside this heredoc.
    if ! _ma_tier_out="$(python3 - "$_cfg" <<'PY'
import json, sys
path = sys.argv[1]
try:
    with open(path) as f:
        d = json.load(f)
    if not isinstance(d, dict):
        raise ValueError(f"expected a JSON object at the top level, got {type(d).__name__}")
    # #2965: the TOP LEVEL was type-checked above; the VALUES were not, so a
    # well-formed config with a wrong-typed value (install_tier: 0, an int
    # whose str() is non-empty) silently passed the `-z` fallback check
    # further down and dropped dev/forum requirements with no signal --
    # exactly the outcome issue #2951 says absence should never
    # produce. Reject a present-but-wrong-typed value the same fail-loud way
    # a malformed file is already rejected, rather than let it degrade to a
    # convenience/single-agent default.
    ma_raw = d.get("multi_agent")
    if ma_raw is not None and not isinstance(ma_raw, bool):
        raise ValueError(f"multi_agent must be a boolean (or null/absent), got {type(ma_raw).__name__}: {ma_raw!r}")
    ma = "true" if (ma_raw is True or d.get("mode") == "multi") else "false"
    # Match jq install_tier // empty semantics: only null/missing falls back
    # to empty -- an already-empty-string value passes through as itself.
    tier = d.get("install_tier")
    if tier is not None and tier is not False and not isinstance(tier, str):
        raise ValueError(f"install_tier must be a string (or null/absent/false), got {type(tier).__name__}: {tier!r}")
    if tier is None or tier is False:
        tier = ""
    # Reviewer catch (#2958, Mira): this block hands ma/tier to bash as two
    # stdout lines, split via `sed -n '1p'`/`sed -n '2p'` (see below) --
    # unlike the old single-jq-argument path, a value containing an embedded
    # newline is silently split across MULTIPLE lines and the sed split then
    # truncates it to just its first line, with everything after the
    # newline dropped from the pipeline with no error. Reject it here
    # instead, matching the fail-loud contract already used above. `ma` is always
    # "true"/"false" (derived above, never copied from user input) so it can
    # never contain a newline -- only `tier`, a raw string straight out of
    # config.json, is at risk.
    if "\n" in tier:
        raise ValueError(
            "install_tier must not contain a newline "
            f"(got {tier!r})"
        )
except Exception as e:
    # Reviewer-fairy round 1 (#2955): the ORIGINAL version of this block only
    # wrapped the open()+json.load() call, so a syntactically-valid-but-wrong-
    # shape config (e.g. a bare JSON array or string at the top level) still
    # aborted the script (d.get() raised AttributeError, uncaught, non-zero
    # exit -- fail-loud held) but with a raw Python traceback instead of this
    # clean one-line message. Widened to cover every failure mode with the
    # SAME message, matching the safety property this whole block exists for.
    print(f"[install-local-marketplace] ERROR: failed to parse {path}: {e}", file=sys.stderr)
    sys.exit(1)
print(ma)
print(tier)
PY
    )"; then
      exit 1
    fi
    # Split the two stdout lines without readarray/mapfile -- unavailable
    # on bash 3.2 (macOS's system /usr/bin/env bash target).
    _ma="$(printf '%s\n' "$_ma_tier_out" | sed -n '1p')"
    _tier="$(printf '%s\n' "$_ma_tier_out" | sed -n '2p')"
    [[ "$_ma" == "true" ]] && build_flags+=("--multi-agent")
    [[ -n "$_tier" ]] && build_flags+=("--tier" "$_tier")
  fi
  build_flags+=("--target" "$TARGET")
  # build-plugin.sh (tools/engine/cli.py) carries its OWN independent #794
  # branch guard, unrelated to the one above — passing our local tag/ancestor
  # exception here too (not just $ALLOW_BRANCH) is what actually makes a
  # deploy-tag checkout build end-to-end without --allow-branch; otherwise
  # this guard would pass while the engine's own guard still refuses one
  # call downstream. #2711.
  [[ $ALLOW_BRANCH -eq 1 || -n "$DEPLOYABLE_TAG" ]] && build_flags+=("--allow-branch")
  # Mirror: forward --allow-untagged too, for the same #2711-class reason —
  # cli.py build's own #3092 tag guard is independent of this script's
  # #2586 guard above, so the override must be forwarded explicitly or an
  # explicit --allow-untagged deploy fails one call downstream.
  [[ $ALLOW_UNTAGGED -eq 1 ]] && build_flags+=("--allow-untagged")
  log "Building plugin tree (target: $TARGET, flags: ${build_flags[*]:-none})"
  bash "$REPO_ROOT/tools/build-plugin.sh" ${build_flags[@]+"${build_flags[@]}"}
fi

if [[ ! -f "$PLUGIN_SRC/plugin.json" ]]; then
  die "$PLUGIN_SRC/plugin.json missing — build-plugin.sh did not assemble correctly"
fi

if [[ $SKIP_BUILD -eq 1 ]]; then
  if [[ ! -f "$PLUGIN_SRC/platform.json" ]]; then
    die "--skip-build cannot verify target: $PLUGIN_SRC/platform.json missing. Rebuild with tools/install-local-marketplace.sh --target $TARGET or pass a build tree with platform.json."
  fi
  BUILT_TARGET="$(python3 -c "import json,sys; print(json.load(open(sys.argv[1])).get('platform',''))" "$PLUGIN_SRC/platform.json")"
  if [[ "$BUILT_TARGET" != "$TARGET" ]]; then
    die "--skip-build target mismatch: $PLUGIN_SRC/platform.json is '$BUILT_TARGET' but --target is '$TARGET'. Rebuild with tools/install-local-marketplace.sh --target $TARGET or pass the matching --target."
  fi
fi

log "Cleaning $MARKETPLACE_ROOT"
rm -rf "$MARKETPLACE_ROOT"
mkdir -p "$MARKETPLACE_ROOT/.claude-plugin"
mkdir -p "$MARKETPLACE_ROOT/plugins/engram"

log "Copying $PLUGIN_SRC/ -> $MARKETPLACE_ROOT/plugins/engram/"
cp -a "$PLUGIN_SRC/." "$MARKETPLACE_ROOT/plugins/engram/"

PLUGIN_VERSION="$(python3 -c "import json,sys; print(json.load(open(sys.argv[1])).get('version','0.1.0'))" "$PLUGIN_SRC/plugin.json")"

log "Writing $MARKETPLACE_ROOT/.claude-plugin/marketplace.json (engram@${PLUGIN_VERSION})"
cat > "$MARKETPLACE_ROOT/.claude-plugin/marketplace.json" <<EOF
{
  "\$schema": "https://json.schemastore.org/claude-code-marketplace.json",
  "name": "engram-local",
  "version": "1.0.0",
  "description": "Local ENGRAM plugin marketplace assembled from build/plugin/ by tools/install-local-marketplace.sh",
  "owner": {
    "name": "engram-agents"
  },
  "plugins": [
    {
      "name": "engram",
      "description": "ENGRAM - Protocol-governed knowledge graph memory for Claude Code agents",
      "source": "./plugins/engram",
      "version": "$PLUGIN_VERSION",
      "category": "development",
      "author": {
        "name": "engram-agents"
      },
      "homepage": "https://github.com/engram-agents/engram",
      "tags": ["memory", "knowledge-graph", "mcp", "agent", "epistemics"]
    }
  ]
}
EOF

if ! python3 -c "import json; json.load(open('$MARKETPLACE_ROOT/.claude-plugin/marketplace.json'))" 2>/dev/null; then
  die "marketplace.json failed JSON parse"
fi

log "Marketplace assembled at $MARKETPLACE_ROOT"

# ── Version marker ─────────────────────────────────────────────────────────────
# Refresh ~/.engram/.deployed-version so the plugin path maintains the same
# forensics anchor the retired scatter upgrader (deploy.sh) kept (which commit
# is my running code from?). Without this the marker goes stale after every
# plugin upgrade — the engram-upgrade skill's change-set review
# (deployed..target) and post-upgrade verification both anchor on it. Same
# format the scatter-era writer used.
# Capture the OLD sha BEFORE overwriting the marker — used below to detect
# whether src/engram/hooks/hooks.json changed in this upgrade (requires a full restart).
OLD_ALPHA_SHA="$(grep '^alpha_sha=' "$ENGRAM_HOME/.deployed-version" 2>/dev/null | cut -d= -f2- || true)"
ALPHA_SHA="$(git -C "$REPO_ROOT" rev-parse --short HEAD 2>/dev/null || echo unknown)"
ALPHA_BRANCH="$(git -C "$REPO_ROOT" rev-parse --abbrev-ref HEAD 2>/dev/null || echo unknown)"
if [[ -n "$(git -C "$REPO_ROOT" status --porcelain --untracked-files=no 2>/dev/null)" ]]; then
  ALPHA_DIRTY="dirty"
else
  ALPHA_DIRTY="clean"
fi
{
  printf 'alpha_sha=%s\n' "$ALPHA_SHA"
  printf 'alpha_branch=%s\n' "$ALPHA_BRANCH"
  printf 'alpha_tag=%s\n' "$ALPHA_TAG"
  if [[ $ALPHA_UNTAGGED_OVERRIDE_FIRED -eq 1 ]]; then
    printf 'alpha_untagged_override=1\n'
  fi
  printf 'alpha_dirty=%s\n' "$ALPHA_DIRTY"
  printf 'deployed_at=%s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  printf 'deployed_from=%s\n' "$REPO_ROOT"
} > "$ENGRAM_HOME/.deployed-version"
log "Version marker refreshed: $ENGRAM_HOME/.deployed-version (alpha_sha=$ALPHA_SHA)"

# ── Persist install choices to config.json (bug 4, #1244) ────────────────────
# Write install_tier and multi_agent back to config.json so the next upgrade
# can read them without the user needing to re-specify explicit flags.
# Only runs in the non-skip-build path: --skip-build callers managed their own
# flags and their choices are not visible here.
if [[ $SKIP_BUILD -eq 0 ]] && [[ -n "${_cfg:-}" ]] && [[ -f "${_cfg}" ]] && command -v python3 &>/dev/null; then
  python3 - "${_cfg}" "${_tier:-}" "${_ma:-false}" <<'PY'
import json, sys
path, tier, ma = sys.argv[1], sys.argv[2], sys.argv[3]
try:
    with open(path) as f:
        d = json.load(f)
except (OSError, json.JSONDecodeError):
    sys.exit(0)
changed = False
if tier and d.get('install_tier') != tier:
    d['install_tier'] = tier
    changed = True
new_ma = ma == 'true'
if d.get('multi_agent') is not new_ma:
    d['multi_agent'] = new_ma
    changed = True
if changed:
    with open(path, 'w') as f:
        json.dump(d, f, indent=2)
        f.write('\n')
    print(f"[install-local-marketplace] config.json updated: install_tier={tier or 'unset'} multi_agent={ma}")
PY
fi

# ── Tier-aware venv provisioning (Lei 2026-08-10, #576/#578) ──────────────────
# Keep the runtime venv's installed packages in sync with the chosen install
# tier on EVERY install/upgrade, so a new release that adds a dependency
# auto-installs it instead of leaving the user's venv broken after upgrade
# (Lei, 2026-08-10: "making the venv updated with the tier config in every
# upgrade is actually the desired behavior").
#
# The venv lives at ~/.engram/venv/ — canonical since #578's plugin port
# colocated all engram state under ENGRAM_HOME=~/.engram (per #576). The
# pre-plugin ~/.engram-venv hyphen path was retired then; agent-bootstrap now
# creates the venv at the same slash path.
#
# All tiers need the runtime requirements (fastmcp etc.) to run the server;
# the dev tier additionally installs requirements-dev.txt (runtime + pytest)
# so the local test runner (tools/run-all-tests.sh --engram-only) works out
# of the box. `pip install -r` is a no-op when the installed versions already
# satisfy the spec, so re-running on every upgrade is cheap and safe. Runs
# for --skip-build callers too: a venv kept in sync is independent of whether
# this run rebuilt the plugin tree.
_ENGRAM_VENV_PY="$ENGRAM_HOME/venv/bin/python"
if [[ -f "$_ENGRAM_VENV_PY" ]] && command -v python3 &>/dev/null; then
  # #2951: these two reads used to swallow ANY parse failure and silently
  # resolve to convenience/not-multi-agent, dropping the dev/forum
  # requirements with no signal. Fail loud instead, matching the
  # _ma_tier_out block's contract above (#2955/#2958) -- absence of the
  # value means "something is wrong", never "single-agent convenience".
  # A well-formed config that simply omits install_tier (or has it as
  # null) still resolves to empty string here, exactly like the sibling
  # block, so the existing `[[ -z ... ]] && _eff_tier="convenience"` line
  # below keeps doing the legitimate-default job -- only a parse/type
  # failure takes the new sys.exit(1) path. Kept as TWO separate python3
  # calls (not merged into one heredoc like the _ma_tier_out block) --
  # deliberate minimal-diff scope: this fix makes the existing two calls
  # fail loud, it does not restructure them.
  if ! _eff_tier="$(python3 -c "import json,sys
path = sys.argv[1]
try:
    with open(path) as f:
        d = json.load(f)
    if not isinstance(d, dict):
        raise ValueError('expected a JSON object at the top level, got ' + type(d).__name__)
    tier = d.get('install_tier')
    # #2965: reject a present-but-wrong-typed value (e.g. an int/list/dict)
    # instead of letting it fall through non-empty and silently defeat the
    # [[ -z ... ]] && _eff_tier=convenience fallback below.
    if tier is not None and tier is not False and not isinstance(tier, str):
        raise ValueError('install_tier must be a string (or null/absent/false), got ' + type(tier).__name__ + ': ' + repr(tier))
    if tier is None or tier is False:
        tier = ''
except Exception as e:
    print('[install-local-marketplace] ERROR: failed to parse ' + path + ': ' + str(e), file=sys.stderr)
    sys.exit(1)
print(tier)
" "$ENGRAM_HOME/config.json")"; then
    exit 1
  fi
  [[ -z "$_eff_tier" ]] && _eff_tier="convenience"
  # Independent of tier (#2944): is this seat multi-agent? Same boolean logic
  # as the earlier jq-based read above (multi_agent==true OR mode=="multi"),
  # reimplemented via python3 here to match this later block's jq-optional
  # style (this block already doesn't assume jq is present). Same #2951
  # fail-loud contract as _eff_tier above -- absence of multi_agent/mode
  # keys is a legitimate "false", only a parse/type failure errors.
  if ! _eff_multi_agent="$(python3 -c "import json,sys
path = sys.argv[1]
try:
    with open(path) as f:
        d = json.load(f)
    if not isinstance(d, dict):
        raise ValueError('expected a JSON object at the top level, got ' + type(d).__name__)
    _ma_raw = d.get('multi_agent')
    # #2965: reject a present-but-wrong-typed value (e.g. the string 'true',
    # or 1) instead of letting it silently resolve to false and drop the
    # forum package requirements with no signal.
    if _ma_raw is not None and not isinstance(_ma_raw, bool):
        raise ValueError('multi_agent must be a boolean (or null/absent), got ' + type(_ma_raw).__name__ + ': ' + repr(_ma_raw))
    print('true' if (_ma_raw is True or d.get('mode') == 'multi') else 'false')
except Exception as e:
    print('[install-local-marketplace] ERROR: failed to parse ' + path + ': ' + str(e), file=sys.stderr)
    sys.exit(1)
" "$ENGRAM_HOME/config.json")"; then
    exit 1
  fi
  [[ -z "$_eff_multi_agent" ]] && _eff_multi_agent="false"
  _runtime_req="$REPO_ROOT/requirements/requirements.txt"
  if [[ -f "$_runtime_req" ]]; then
    log "Provisioning venv at $ENGRAM_HOME/venv (tier: $_eff_tier)"
    # A failed RUNTIME-requirements install leaves the MCP server unable to
    # start — the exact "broken venv after upgrade" this block exists to
    # prevent. We let the rest of the install complete (marketplace + hooks
    # are already assembled) but flag it so the script exits non-zero at the
    # end — unattended/cron upgrades key on exit code and would otherwise
    # report success over a broken server (Lei, 2026-08-10).
    "$_ENGRAM_VENV_PY" -m pip install --quiet -r "$_runtime_req" \
      || { log "WARN: venv runtime-requirements install failed (continuing install; will exit non-zero)"; _venv_fail=1; }
  fi
  if [[ "$_eff_tier" == "dev" ]]; then
    _dev_req="$REPO_ROOT/requirements/requirements-dev.txt"
    if [[ -f "$_dev_req" ]]; then
      log "Dev tier: installing test dependencies into $ENGRAM_HOME/venv"
      # A failed DEV-requirements install (pytest etc.) only breaks the local
      # test runner — the server still runs on the runtime reqs above. Failing
      # the whole install over a missing test dep would block a working
      # server update, so this is WARN-and-continue only (NOT flagged).
      "$_ENGRAM_VENV_PY" -m pip install --quiet -r "$_dev_req" \
        || log "WARN: venv dev-requirements install failed (continuing; server still runs)"
    fi
  fi
  if [[ "$_eff_multi_agent" == "true" ]]; then
    _forum_req="$REPO_ROOT/src/forum/requirements.txt"
    if [[ -f "$_forum_req" ]]; then
      log "Multi-agent: installing forum package dependencies into $ENGRAM_HOME/venv"
      # A failed FORUM-requirements install (Flask, bleach, beautifulsoup4,
      # html5lib, markdown-it-py, sentence-transformers, huggingface_hub,
      # sqlite-vec) only affects local forum-test collection (src/forum/tests/)
      # — the server still runs on the runtime reqs above. Failing the whole
      # install over a missing forum-test dep would block a working server
      # update, so this is WARN-and-continue only (NOT flagged), matching the
      # dev-requirements install above.
      "$_ENGRAM_VENV_PY" -m pip install --quiet -r "$_forum_req" \
        || log "WARN: venv forum-requirements install failed (continuing; server still runs)"
    else
      # #2951 (Kepler, folded into the same rc2 item): a missing forum-
      # requirements file used to silently no-op with zero log output,
      # unlike the install-failed case above which does WARN. A
      # multi-agent seat with no forum requirements file is a real gap
      # (forum tooling silently unavailable), so surface it the same way.
      log "WARN: multi-agent seat but $_forum_req not found -- forum/coordination package dependencies were not installed (continuing; server still runs, forum tooling may be unavailable)"
    fi
  fi
fi

# ── Hook-registration-change detection ────────────────────────────────────────
# Claude Code only reloads the hook set on a FULL restart — /mcp reconnect
# reloads the MCP server but NOT the hook registration.  If
# src/engram/hooks/hooks.json changed in this upgrade, a removed hook will
# throw a dangling-file error and an added hook won't fire until Claude Code
# is restarted.
if [[ -n "$OLD_ALPHA_SHA" ]] && git -C "$REPO_ROOT" cat-file -e "${OLD_ALPHA_SHA}^{commit}" 2>/dev/null && ! git -C "$REPO_ROOT" diff --quiet "$OLD_ALPHA_SHA" HEAD -- src/engram/hooks/hooks.json 2>/dev/null; then
  echo ""
  echo "  ⚠️  HOOK REGISTRATION CHANGED in this upgrade."
  echo "      You MUST fully RESTART Claude Code (not just /mcp) to reload the hook set —"
  echo "      otherwise removed hooks will error and added hooks won't fire."
  echo "      (/mcp reconnect reloads the MCP server, not the hook registration.)"
  echo ""
fi

if [[ $SKIP_ADD -eq 1 ]]; then
  log "Skipping host CLI marketplace registration (--skip-add)"
  if [[ "$TARGET" == "codex" ]]; then
    log "To install in Codex: codex plugin add engram@engram-local"
  else
    log "To register manually: claude plugin marketplace add $MARKETPLACE_ROOT"
    log "Then in a Claude Code session: /plugin install engram"
  fi
  exit 0
fi

if [[ "$TARGET" == "codex" ]]; then
  if ! command -v codex >/dev/null 2>&1; then
    log "WARN: 'codex' not on PATH - cannot install the plugin automatically"
    log "      Run manually in Codex: codex plugin add engram@engram-local"
    exit 0
  fi

  CODEX_HOME="${CODEX_HOME:-$HOME/.codex}"
  # Normalize lexically (rather than resolving symlinks) before trusting the
  # hook-provided cache root.  The compat cleanup below may remove and replace
  # OLD_CODEX_PLUGIN_CACHE_DIR, so it must only ever receive a direct child of
  # this plugin's versioned cache directory.
  # PORTABILITY LANDMINE (#2979): this heredoc is $(...)-nested like the one at
  # line 343 -- keep the total literal ' count across its body EVEN or bash 3.2
  # breaks with a spurious syntax error dozens of lines later. See line 343's
  # full comment for why.
  CODEX_CACHE_PARENT="$(python3 - "$CODEX_HOME/plugins/cache/engram-local/engram" <<'PY'
import os
import sys
print(os.path.abspath(os.path.normpath(sys.argv[1])))
PY
)"

  # Resolve the active cache path BEFORE `codex plugin add`.  Codex may remove
  # that directory while replacing its versioned cache, so discovering only
  # after the add leaves no path to preserve for hooks in this live session.
  # Prefer the hook-provided root when it is a direct child of this plugin's
  # cache; otherwise, fall back to the newest development cache directory.
  # A raw prefix check would accept traversal or nested paths and let the
  # compat cleanup modify paths outside that set.  Plain release caches are
  # not eligible fallbacks because this cleanup replaces its selected path.
  OLD_CODEX_PLUGIN_CACHE_DIR=""
  if [[ -n "${CLAUDE_PLUGIN_ROOT:-}" ]]; then
    # Same $(...)-nested-heredoc landmine as line 714/343 (#2979) -- keep the
    # literal ' count in this body EVEN.
    NORMALIZED_CLAUDE_PLUGIN_ROOT="$(python3 - "$CLAUDE_PLUGIN_ROOT" <<'PY'
import os
import sys
print(os.path.abspath(os.path.normpath(sys.argv[1])))
PY
)"
    if [[ "$(dirname "$NORMALIZED_CLAUDE_PLUGIN_ROOT")" == "$CODEX_CACHE_PARENT" ]] && [[ -d "$NORMALIZED_CLAUDE_PLUGIN_ROOT" ]]; then
      OLD_CODEX_PLUGIN_CACHE_DIR="$NORMALIZED_CLAUDE_PLUGIN_ROOT"
    fi
  fi
  if [[ -z "$OLD_CODEX_PLUGIN_CACHE_DIR" && -d "$CODEX_CACHE_PARENT" ]]; then
    # Same $(...)-nested-heredoc landmine as line 714/343 (#2979) -- keep the
    # literal ' count in this body EVEN.
    OLD_CODEX_PLUGIN_CACHE_DIR="$(python3 - "$CODEX_CACHE_PARENT" <<'PY'
import os
import re
import sys

root = sys.argv[1]

def version_key(name: str) -> tuple[int, ...]:
    parts = re.split(r"[^0-9]+", name)
    return tuple(int(part) for part in parts if part)

candidates = [
    os.path.join(root, name)
    for name in os.listdir(root)
    if re.fullmatch(r".+-dev\..+", name) and os.path.isdir(os.path.join(root, name))
]
if candidates:
    candidates.sort(key=lambda path: (version_key(os.path.basename(path)), os.path.basename(path)))
    print(candidates[-1])
PY
)"
  fi
  if [[ -n "$OLD_CODEX_PLUGIN_CACHE_DIR" ]]; then
    log "Capturing active Codex cache path for in-session compat: $OLD_CODEX_PLUGIN_CACHE_DIR"
  fi

  log "Installing/updating plugin with Codex"
  codex plugin add engram@engram-local

  # Codex 0.137.0 does not expand ${CLAUDE_PLUGIN_ROOT} in plugin .mcp.json
  # command strings at MCP startup.  Patch the installed cache entry to the
  # absolute cache path so the next session can spawn ENGRAM reliably.
  CODEX_PLUGIN_CACHE_DIR=""
  if [[ -d "$CODEX_CACHE_PARENT" ]]; then
    # Same $(...)-nested-heredoc landmine as line 714/343 (#2979) -- keep the
    # literal ' count in this body EVEN.
    CODEX_PLUGIN_CACHE_DIR="$(python3 - "$CODEX_CACHE_PARENT" <<'PY'
import os
import re
import sys

root = sys.argv[1]

def version_key(name: str) -> tuple[int, ...]:
    parts = re.split(r"[^0-9]+", name)
    return tuple(int(part) for part in parts if part)

candidates = [
    os.path.join(root, name)
    for name in os.listdir(root)
    if os.path.isdir(os.path.join(root, name))
]
if candidates:
    candidates.sort(key=lambda path: (version_key(os.path.basename(path)), os.path.basename(path)))
    print(candidates[-1])
PY
)"
  fi
  # Create compatibility symlinks from stale ENGRAM cache dirs to the new one.
  # An in-session Codex upgrade can leave hook registrations pointing at the
  # old path, blocking shell commands until the session restarts.  Symlinking
  # old → new keeps current sessions alive while the new hook code runs.
  if [[ -n "$CODEX_PLUGIN_CACHE_DIR" ]]; then
    CACHE_PARENT="$(dirname "$CODEX_PLUGIN_CACHE_DIR")"
    NEW_BASE="$(basename "$CODEX_PLUGIN_CACHE_DIR")"
    if [[ -z "$NEW_BASE" ]]; then
      log "WARN: could not derive new cache basename — skipping compat symlink step"
    else
      shopt -s nullglob
      # Version-agnostic: matches "<base>-dev.<stamp>.<sha>" for ANY base
      # version (0.1.0, 0.2.0, future bumps), not just 0.1.0. The "-dev."
      # substring keeps this from matching a plain (non-dev) release-tagged
      # cache dir that might otherwise sit alongside these in $CACHE_PARENT.
      for old_dir in "$CACHE_PARENT"/*-dev.*; do
        [[ -e "$old_dir" || -L "$old_dir" ]] || continue
        [[ "$(basename "$old_dir")" == "$NEW_BASE" ]] && continue
        [[ "$old_dir" == "$OLD_CODEX_PLUGIN_CACHE_DIR" ]] && continue
        log "Symlinking stale cache dir for in-session compat: $(basename "$old_dir") -> $NEW_BASE"
        rm -rf "$old_dir"
        ln -s "$CODEX_PLUGIN_CACHE_DIR" "$old_dir"
      done
      shopt -u nullglob

      # The immediately preceding live cache can have been removed by `codex
      # plugin add`, so it will not appear in the stale-dir scan above. Restore
      # that exact path only after the new cache exists, including across
      # consecutive dev upgrades where the prior compatibility link was gone.
      if [[ -n "$OLD_CODEX_PLUGIN_CACHE_DIR" && "$OLD_CODEX_PLUGIN_CACHE_DIR" != "$CODEX_PLUGIN_CACHE_DIR" ]]; then
        log "Symlinking prior live cache for in-session compat: $(basename "$OLD_CODEX_PLUGIN_CACHE_DIR") -> $NEW_BASE"
        rm -rf "$OLD_CODEX_PLUGIN_CACHE_DIR"
        ln -s "$CODEX_PLUGIN_CACHE_DIR" "$OLD_CODEX_PLUGIN_CACHE_DIR"
      fi
    fi
  fi

  if [[ -n "$CODEX_PLUGIN_CACHE_DIR" && -f "$CODEX_PLUGIN_CACHE_DIR/.mcp.json" ]]; then
    log "Patching Codex plugin MCP command -> $CODEX_PLUGIN_CACHE_DIR/launch-engram-server.sh"
    python3 - "$CODEX_PLUGIN_CACHE_DIR/.mcp.json" "$CODEX_PLUGIN_CACHE_DIR/launch-engram-server.sh" <<'PY'
import json
import sys
path, command = sys.argv[1:3]
with open(path, encoding="utf-8") as f:
    data = json.load(f)
data.setdefault("mcpServers", {}).setdefault("engram", {})["command"] = command
with open(path, "w", encoding="utf-8") as f:
    json.dump(data, f, indent=2)
    f.write("\n")
PY
  else
    log "WARN: could not locate installed Codex plugin cache .mcp.json for absolute command patch"
  fi

  CODEX_AGENTS_DIR="$CODEX_HOME/agents"
  if compgen -G "$MARKETPLACE_ROOT/plugins/engram/agents/*.toml" > /dev/null; then
    log "Installing Codex custom-agent TOML files -> $CODEX_AGENTS_DIR"
    mkdir -p "$CODEX_AGENTS_DIR"
    cp -a "$MARKETPLACE_ROOT/plugins/engram/agents/"*.toml "$CODEX_AGENTS_DIR/"

    # Codex custom agents are config layers.  Plugin MCP tool allow/deny policy
    # must target the plugin-provided server namespace, not top-level
    # [mcp_servers.*] transport tables.  Rewrite older built TOMLs defensively.
    python3 - "$CODEX_AGENTS_DIR" <<'PY'
import pathlib
import sys
agents_dir = pathlib.Path(sys.argv[1])
for path in agents_dir.glob("engram-*.toml"):
    text = path.read_text(encoding="utf-8")
    text = text.replace(
        "[mcp_servers.engram]",
        "[plugins.\"engram@engram-local\".mcp_servers.engram]",
    )
    path.write_text(text, encoding="utf-8")
PY
  else
    log "WARN: no Codex custom-agent TOML files found in $MARKETPLACE_ROOT/plugins/engram/agents"
  fi
else
  if ! command -v claude >/dev/null 2>&1; then
    log "WARN: 'claude' not on PATH - cannot register the marketplace automatically"
    log "      Run manually: claude plugin marketplace add $MARKETPLACE_ROOT"
    log "      Then in a Claude Code session: /plugin install engram"
    exit 0
  fi

  log "Registering marketplace with Claude Code"
  claude plugin marketplace add "$MARKETPLACE_ROOT"
fi

log ""
log "═══════════════════════════════════════════════════════════════════"
if [[ "$TARGET" == "codex" ]]; then
  log " Codex marketplace installed/updated. Next steps:"
  log "═══════════════════════════════════════════════════════════════════"
  log ""
  log "  Restart or resume Codex so hooks/MCP reload the upgraded plugin cache."
  log "  If hook trust changed, approve the refreshed hook definitions when prompted."
  log ""
  log " To upgrade later in Codex:"
  log "   cd path/to/engram"
  log "   git pull"
  log "   bash tools/install-local-marketplace.sh --target codex"
else
  log " Marketplace registered. Next steps (in a Claude Code session):"
  log "═══════════════════════════════════════════════════════════════════"
  log ""
  log "  1. /plugin install engram      # install from the local marketplace"
  log "                                 #   If this reports 'Plugin not found',"
  log "                                 #   the plugin is already registered from"
  log "                                 #   a previous install — run step 2 with"
  log "                                 #   /plugin enable engram@engram-local"
  log "  2. /plugin enable  engram      # ENABLE — local marketplace plugins"
  log "                                 #   ship DISABLED by default; this"
  log "                                 #   step is REQUIRED to activate."
  log "  3. Restart Claude Code so MCP picks up the now-enabled plugin."
  log ""
  log " (Empirically verified: 2026-05-31 Chromebook trial. The /plugin"
  log "  install step alone leaves the plugin in disabled state — the"
  log "  user must explicitly /plugin enable engram.)"
  log ""
  log " To upgrade later:"
  log "   cd path/to/engram"
  log "   git pull"
  log "   bash tools/install-local-marketplace.sh"
  log "   # then in Claude Code:"
  log "   1. /plugin marketplace update engram-local"
  log "   2. /plugin -> Installed -> engram plugin (not the MCP entry under it) -> Update now"
  log "   3. /mcp -> engram -> Reconnect"
fi
