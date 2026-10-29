#!/usr/bin/env bash
# install.sh — the scripted fresh-install entry point (#4097, slice 4).
#
# Does every step of a fresh ENGRAM install that does not need the agent or the human, in
# order, and stops at the first failure with `FAIL: [step N] <name>: <what> — <remedy>` and a
# non-zero exit. Every step prints one line, `[step N] <name> ... ok`; nothing is skipped
# silently. `install-local-marketplace.sh` stays the UPGRADE path and is CALLED by step 5.
#
# USAGE:
#   bash tools/install.sh [--tier essential|convenience|dev]
#                         [--recall-service | --no-recall-service] [--viz-service]
#                         [--forum-url <url>] [--install-claude-code] [--yes]
#                         [--sandbox-untagged] [--help]
#
# KNOWN GAP, step 1 never fetches: its deployable-tag guard reads the origin/* refs the checkout
# already has. A checkout with stale origin/* refs (for example a shallow `git clone --branch vX`)
# is refused at step 1, although step 5 does a bounded fetch and might have passed. Remedy:
# `git fetch --tags origin`, then re-run.
#
# TRYING IT ON A LIVE SEAT: scratch HOME, --no-recall-service (services are per user, not per HOME),
# CLAUDE_HOME and CLAUDE_CONFIG_DIR unset. ENGRAM_HOME alone leaves the venv (~/.engram/venv, by
# design, #3863), ~/.claude/CLAUDE.md and the plugin config live. Untagged tip: --sandbox-untagged.
#
# Bash 3.2 compatible (macOS). No interactive prompt except the one CLAUDE.md question in
# step 6 (a tty, no --yes, an existing ~/.claude/CLAUDE.md and no backup yet).

set -euo pipefail

# A RELATIVE ENGRAM_HOME is refused (alpha #3863): it would resolve against the calling process's cwd, a
# different home for every process. Unset or empty means unset; a leading ~ is expanded against $HOME and
# re-exported (~user is not expanded: it is not absolute, so it is refused). Checked before anything else
# runs; `return` covers a script that is sourced, `exit` one that is executed.
if [ -n "${ENGRAM_HOME-}" ]; then
    _eh_raw="$ENGRAM_HOME"
    case "$ENGRAM_HOME" in
        "~") ENGRAM_HOME="${HOME-}"; export ENGRAM_HOME ;;
        "~/"*) ENGRAM_HOME="${HOME-}/${ENGRAM_HOME#"~/"}"; export ENGRAM_HOME ;;
    esac
    case "$ENGRAM_HOME" in
        /*) ;;
        *)
            printf "ENGRAM_HOME must be an absolute path, got '%s'\n" "$_eh_raw" >&2
            unset _eh_raw
            return 2 2>/dev/null || exit 2 ;;
    esac
    unset _eh_raw
fi

# ── Ruled defaults (Lei, 2026-10-23, in session: "yes to all three install questions") ───────
# One named constant each. A flag changes a default for one run; these are the defaults.
# 0 = the Claude Code native installer runs only behind --install-claude-code. It runs remote
#     code, so that flag is the operator's yes and --yes NEVER implies it.
DEFAULT_INSTALL_CLAUDE_CODE=0
# 1 = an existing ~/.claude/CLAUDE.md is copied ONCE to CLAUDE.md.pre-engram (never
#     overwritten) and then replaced. It answers the step-6 question when nobody can be asked
#     (--yes, or no tty). 0 = stop instead of replacing.
DEFAULT_BACKUP_AND_REPLACE_CLAUDE_MD=1
# 1 = the persistent recall service is installed unless --no-recall-service.
DEFAULT_RECALL_SERVICE=1

DEFAULT_TIER="convenience"
CLAUDE_INSTALLER_URL="https://claude.ai/install.sh"
CLAUDE_INSTALLER_COMMAND="curl -fsSL $CLAUDE_INSTALLER_URL | bash"
PLUGIN_REF="engram@engram-local"
MIN_PYTHON="3.10.7"

usage() {
  cat <<'EOF'
Usage: bash tools/install.sh [options]

Fresh ENGRAM install: preflight, Claude Code (optional), config, venv, plugin build and
marketplace, bootstrap, plugin install + enable, services, self-check. Stops at the first
failure. To UPGRADE an installed seat use tools/install-local-marketplace.sh instead.

Options:
  --tier T              essential | convenience | dev (default convenience on a fresh config;
                        an existing config.json's install_tier is KEPT when this is absent)
  --recall-service      install the persistent recall service (default)
  --no-recall-service   do not install it
  --viz-service         install the viz dashboard service (Linux only; a no-op line on macOS)
  --forum-url URL       write forum.url into config.json
  --install-claude-code run Claude Code's native installer if `claude` is missing
                        (off by default; --yes never implies it)
  --yes                 non-interactive: answer the CLAUDE.md question "back up and replace"
  --sandbox-untagged    SANDBOX RIGS ONLY, not a release: build from an untagged commit or a
                        branch (passes --allow-untagged and --allow-branch to step 5) and print
                        "SANDBOX BUILD (not a release): <HEAD sha>" in step 1 and at the end.
                        Never implied by --yes or any other flag or variable
  --help               show this help and exit
EOF
}

# ── Arguments ────────────────────────────────────────────────────────────────────────────────
# Absent means "keep what config.json holds, else the default" (step 3 resolves both). The
# effective value is TIER after step 3.
TIER_ARG=""
TIER="$DEFAULT_TIER"
RECALL_SERVICE="$DEFAULT_RECALL_SERVICE"
VIZ_SERVICE=0
FORUM_URL=""
INSTALL_CLAUDE_CODE="$DEFAULT_INSTALL_CLAUDE_CODE"
ASSUME_YES=0
SANDBOX_UNTAGGED=0

_arg_error() { printf 'install.sh: %s (see --help)\n' "$*" >&2; exit 2; }

# Retired flags (#4255): accepted and ignored; one stderr line per distinct flag given.
_RETIRED_SEEN=" "
_retired_flag() {
  case "$_RETIRED_SEEN" in
    *" $1 "*) ;;
    *) printf '%s is retired and has no effect (#4255)\n' "$1" >&2; _RETIRED_SEEN="$_RETIRED_SEEN$1 " ;;
  esac
}

while [ $# -gt 0 ]; do
  case "$1" in
    --help|-h) usage; exit 0 ;;
    --tier)
      [ $# -ge 2 ] || _arg_error "--tier requires a value: essential, convenience or dev"
      TIER_ARG="$2"; shift 2 ;;
    --tier=*) TIER_ARG="${1#--tier=}"; shift ;;
    --multi-agent|--single-agent) _retired_flag "$1"; shift ;;
    --recall-service) RECALL_SERVICE=1; shift ;;
    --no-recall-service) RECALL_SERVICE=0; shift ;;
    --viz-service) VIZ_SERVICE=1; shift ;;
    --forum-url)
      [ $# -ge 2 ] || _arg_error "--forum-url requires a URL"
      FORUM_URL="$2"; shift 2 ;;
    --forum-url=*) FORUM_URL="${1#--forum-url=}"; shift ;;
    --install-claude-code) INSTALL_CLAUDE_CODE=1; shift ;;
    --yes|-y) ASSUME_YES=1; shift ;;
    --sandbox-untagged) SANDBOX_UNTAGGED=1; shift ;;
    *) _arg_error "unknown argument: $1" ;;
  esac
done
case "$TIER_ARG" in
  ""|essential|convenience|dev) ;;
  *) _arg_error "invalid --tier '$TIER_ARG' (expected essential, convenience or dev)" ;;
esac

# ── Step reporting ───────────────────────────────────────────────────────────────────────────
CUR_STEP=0
CUR_NAME="startup"
FAIL_PRINTED=0

begin_step() { CUR_STEP="$1"; CUR_NAME="$2"; }
step_ok() {
  if [ $# -ge 1 ] && [ -n "$1" ]; then
    printf '[step %s] %s ... ok (%s)\n' "$CUR_STEP" "$CUR_NAME" "$1"
  else
    printf '[step %s] %s ... ok\n' "$CUR_STEP" "$CUR_NAME"
  fi
}
# fail "<what>" "<remedy>": the one place a step fails.
fail() {
  FAIL_PRINTED=1
  printf 'FAIL: [step %s] %s: %s — %s\n' "$CUR_STEP" "$CUR_NAME" "$1" "$2" >&2
  exit 1
}
warn() { printf 'WARN: [step %s] %s\n' "$CUR_STEP" "$*"; }

# A `set -e` exit that no step turned into a FAIL line must still name its step (never silent).
_on_exit() {
  local rc=$?
  if [ "$rc" -ne 0 ] && [ "$FAIL_PRINTED" -eq 0 ]; then
    printf 'FAIL: [step %s] %s: unexpected error (exit %s) — read the output above; fix it and re-run this installer\n' \
      "$CUR_STEP" "$CUR_NAME" "$rc" >&2
  fi
  # The step logs live in the scratch dir; on a failure copy them to the path the FAIL lines
  # name (LOG_SAVE_DIR, under $ENGRAM_HOME) before the scratch dir is removed.
  if [ -n "${WORK_DIR-}" ] && [ -d "${WORK_DIR-}" ]; then
    if [ "$rc" -ne 0 ] && [ -n "${LOG_SAVE_DIR-}" ]; then
      if mkdir -p "$LOG_SAVE_DIR" 2>/dev/null && cp -R "$WORK_DIR/." "$LOG_SAVE_DIR/" 2>/dev/null; then
        printf 'install logs kept in %s\n' "$LOG_SAVE_DIR" >&2
      fi
    fi
    rm -rf "$WORK_DIR"
  fi
}
trap _on_exit EXIT

# Print the last lines of a log FILE (no pipe: the exit status of the command that wrote it
# was already checked). The header names the copy that survives the failure, not the scratch one.
show_log_tail() {
  if [ -s "$1" ]; then
    printf -- '--- last lines of %s ---\n' "${LOG_SAVE_DIR:-$(dirname "$1")}/$(basename "$1")" >&2
    tail -n 25 "$1" >&2 || true
    printf -- '--- end ---\n' >&2
  fi
}

# ── Locations ────────────────────────────────────────────────────────────────────────────────
# Resolve this script through symlinks without `readlink -f` (absent on older macOS).
_src="${BASH_SOURCE[0]}"
while [ -L "$_src" ]; do
  _dir="$(cd -P "$(dirname "$_src")" && pwd)"
  _src="$(readlink "$_src")"
  case "$_src" in /*) ;; *) _src="$_dir/$_src" ;; esac
done
TOOLS_DIR="$(cd -P "$(dirname "$_src")" && pwd)"

ENGRAM_HOME="${ENGRAM_HOME:-$HOME/.engram}"
CLAUDE_HOME="${CLAUDE_HOME:-$HOME/.claude}"
export ENGRAM_HOME CLAUDE_HOME
VENV_DIR="$HOME/.engram/venv"
VENV_PY="$VENV_DIR/bin/python"
PLUGIN_ROOT="$ENGRAM_HOME/marketplace/plugins/engram"
DB_PATH="$ENGRAM_HOME/knowledge.db"
CONFIG_PATH="$ENGRAM_HOME/config.json"
CLAUDE_MD="$CLAUDE_HOME/CLAUDE.md"
CLAUDE_MD_BACKUP="$CLAUDE_HOME/CLAUDE.md.pre-engram"
WORK_DIR=""
LOG_SAVE_DIR=""

# ── Python helpers (kept in variables, run with `python3 -c`: no heredoc inside $(...), which
# bash 3.2 mis-parses) ────────────────────────────────────────────────────────────────────────
read -r -d '' PY_CONFIG <<'PY' || true
import json, os, sys
path, tier_arg, default_tier = sys.argv[1], sys.argv[2], sys.argv[3]
TIERS = ("essential", "convenience", "dev")
if os.path.lexists(path):
    try:
        with open(path, encoding="utf-8") as f:
            d = json.load(f)
    except (OSError, ValueError) as exc:
        print("ERR could not read %s: %s" % (path, exc))
        sys.exit(1)
    if not isinstance(d, dict):
        print("ERR %s is not a JSON object" % path)
        sys.exit(1)
    existed = True
else:
    d, existed = {}, False
# An absent flag keeps the stored value (when it is a valid one), else the default applies.
if tier_arg:
    tier = tier_arg
elif isinstance(d.get("install_tier"), str) and d["install_tier"] in TIERS:
    tier = d["install_tier"]
else:
    tier = default_tier
def show(v):
    return v if isinstance(v, str) else json.dumps(v)
notes = []
if "install_tier" in d and d["install_tier"] != tier:
    notes.append("config: install_tier %s -> %s" % (show(d["install_tier"]), tier))
changed = d.get("install_tier") != tier
# First output line: "<status> <tier>"; any further lines are change notes.
def out(status):
    print("%s %s" % (status, tier))
    for n in notes:
        print(n)
if existed and not changed:
    out("unchanged")
    sys.exit(0)
d["install_tier"] = tier
# Written in place (not temp file + rename): that keeps the file's mode, owner and ACL, which a
# rename resets. The file is a few hundred bytes, written from a dict already held in memory.
# Other keys keep their values and their order; the formatting is normalized (indent=2).
try:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(d, f, indent=2, ensure_ascii=False)
        f.write("\n")
except OSError as exc:
    print("ERR could not write %s: %s" % (path, exc))
    sys.exit(1)
out("updated" if existed else "created")
PY

read -r -d '' PY_NODE_COUNT <<'PY' || true
import sqlite3, sys
from pathlib import Path
db = sys.argv[1]
try:
    con = sqlite3.connect(Path(db).absolute().as_uri() + "?mode=ro", uri=True, timeout=5)
    try:
        try:
            n = con.execute("SELECT COUNT(*) FROM nodes").fetchone()[0]
        except sqlite3.OperationalError as exc:
            if "no such table" not in str(exc).lower():
                raise
            n = 0
    finally:
        con.close()
except Exception as exc:
    print("ERR %s: %s" % (db, exc))
    sys.exit(1)
print(n)
PY

# Reads `claude plugin list --json` from a file; exit 0 only if engram@engram-local is listed
# with enabled == true. The JSON shape is not documented, so any object that carries an
# `enabled` key and names the plugin (the id string, or name + marketplace) counts.
read -r -d '' PY_PLUGIN_ENABLED <<'PY' || true
import json, sys
ref = sys.argv[2]
name, _, market = ref.partition("@")
with open(sys.argv[1]) as f:
    data = json.load(f)
found = []
def names_it(d):
    vals = [v for v in d.values() if isinstance(v, str)]
    if ref in vals:
        return True
    return d.get("name") == name and any(market in v for v in vals)
def walk(x):
    if isinstance(x, dict):
        if "enabled" in x and names_it(x):
            found.append(x["enabled"])
        for v in x.values():
            walk(v)
    elif isinstance(x, list):
        for v in x:
            walk(v)
walk(data)
sys.exit(0 if found and all(v is True for v in found) else 1)
PY

# ── Step 1: preflight ────────────────────────────────────────────────────────────────────────
begin_step 1 "preflight"

command -v python3 >/dev/null 2>&1 \
  || fail "python3 not found on PATH" "install Python $MIN_PYTHON or newer (with the venv module) and re-run"
python3 -c 'import sys; sys.exit(0 if sys.version_info[:3] >= (3, 10, 7) else 1)' \
  || fail "python3 is older than $MIN_PYTHON ($(python3 --version 2>&1))" "install Python $MIN_PYTHON or newer and re-run (3.10.0-3.10.6 are not supported)$(
    [ "$(uname -s 2>/dev/null)" != "Darwin" ] || printf '; on macOS: brew install python@3.12')"
python3 -c 'import venv, ensurepip' >/dev/null 2>&1 \
  || fail "python3 has no venv/ensurepip module" "install it (Debian/Ubuntu: apt install python3-venv) and re-run"
command -v git >/dev/null 2>&1 \
  || fail "git not found on PATH" "install git and re-run"

# The #4099 case: git exiting non-zero on a checkout owned by another user. Test the
# assignment in an `if` so `set -e` cannot kill the script before the message.
_git_err=""
if ! REPO_ROOT="$(git -C "$TOOLS_DIR" rev-parse --show-toplevel 2>/dev/null)"; then
  REPO_ROOT=""
  _git_err="$(git -C "$TOOLS_DIR" rev-parse --show-toplevel 2>&1 || true)"
fi
if [ -z "$REPO_ROOT" ]; then
  [ -z "$_git_err" ] || printf '%s\n' "$_git_err" >&2
  case "$_git_err" in
    *"dubious ownership"*)
      _unsafe="$(printf '%s\n' "$_git_err" | sed -n "s/.*repository at '\([^']*\)'.*/\1/p" | sed -n '1p')"
      [ -n "$_unsafe" ] || _unsafe="$TOOLS_DIR"
      fail "git cannot read this checkout (owned by another user)" "to trust it, run: git config --global --add safe.directory '$_unsafe'" ;;
  esac
  fail "git cannot resolve the repo root of $TOOLS_DIR" "run this script from a git clone of engram (git clone https://github.com/engram-agents/engram)"
fi
[ -f "$REPO_ROOT/requirements/requirements.txt" ] \
  || fail "requirements/requirements.txt missing under $REPO_ROOT" "run this script from a complete engram checkout"

# L1: refuse here what install-local-marketplace.sh's step-5 guard would refuse, before the venv
# and pip are spent. Same rule, same code: src/build/engine/deploy_tag.py deployable_tag_at
# (a v*/deploy/* tag on HEAD, HEAD an ancestor of origin/dev or origin/main). It never fetches,
# so it reads the refs this checkout already has. --sandbox-untagged skips it.
if [ "$SANDBOX_UNTAGGED" != "1" ]; then
  _rc=0
  _deploy_tag="$(PYTHONPATH="$REPO_ROOT" python3 -c 'import sys
try:
    from src.build.engine.deploy_tag import deployable_tag_at as f
except ImportError:
    sys.exit(3)
print(f(sys.argv[1]) or "")' "$REPO_ROOT" 2>/dev/null)" || _rc=$?
  [ "$_rc" -ne 3 ] \
    || fail "src/build/engine/deploy_tag.py is missing under $REPO_ROOT, so the deployable-tag rule cannot be checked" "run this script from a complete engram checkout"
  if [ "$_rc" -ne 0 ] || [ -z "$_deploy_tag" ]; then
    _head_short="$(git -C "$REPO_ROOT" rev-parse --short HEAD 2>/dev/null || echo unknown)"
    _branch="$(git -C "$REPO_ROOT" rev-parse --abbrev-ref HEAD 2>/dev/null || echo unknown)"
    fail "HEAD $_head_short (branch $_branch) is not a deployable tag merged into origin/dev or origin/main; step 5 would refuse it" "check out a deployable tag (v* or deploy/*; git fetch --tags origin first if your tags are stale) and re-run; --sandbox-untagged is for a test rig only, not a release"
  fi
fi

mkdir -p "$CLAUDE_HOME" 2>/dev/null && [ -w "$CLAUDE_HOME" ] \
  || fail "$CLAUDE_HOME is not writable" "fix its ownership or permissions (chown/chmod) and re-run"
# A dangling ~/.claude/CLAUDE.md link would fail the step-6 backup after real work was done.
if [ -L "$CLAUDE_MD" ] && [ ! -e "$CLAUDE_MD" ]; then
  fail "$CLAUDE_MD is a symlink to a path that does not exist ($(readlink "$CLAUDE_MD"))" "remove the link (rm '$CLAUDE_MD') or repair it so it points at a real file, then re-run"
fi
mkdir -p "$ENGRAM_HOME" 2>/dev/null && [ -w "$ENGRAM_HOME" ] \
  || fail "$ENGRAM_HOME is not writable" "fix its ownership or permissions (chown/chmod) and re-run"
_venv_parent="$(dirname "$VENV_DIR")"
mkdir -p "$_venv_parent" 2>/dev/null && [ -w "$_venv_parent" ] \
  || fail "$_venv_parent is not writable (the venv lives there)" "fix its ownership or permissions and re-run"

if command -v claude >/dev/null 2>&1; then
  claude --version >/dev/null 2>&1 \
    || fail "claude is on PATH ($(command -v claude)) but does not run" "remove the broken launcher (rm \$(command -v claude)) and re-run with --install-claude-code, or reinstall Claude Code: $CLAUDE_INSTALLER_COMMAND"
elif [ "$INSTALL_CLAUDE_CODE" -eq 1 ]; then
  command -v curl >/dev/null 2>&1 \
    || fail "curl not found on PATH (needed to fetch the Claude Code installer)" "install curl and re-run"
else
  fail "claude is not on PATH" "install Claude Code with: $CLAUDE_INSTALLER_COMMAND  — or re-run this installer with --install-claude-code to run it for you"
fi

WORK_DIR="$(mktemp -d "${TMPDIR:-/tmp}/engram-install.XXXXXX")" \
  || fail "could not create a scratch directory under ${TMPDIR:-/tmp}" "set TMPDIR to a writable directory and re-run"
# Where the step logs are copied if this run fails (named in the FAIL lines; see _on_exit).
LOG_SAVE_DIR="$ENGRAM_HOME/install-logs/$(date -u +%Y%m%dT%H%M%SZ)"
# Only the explicit flag sets this; no variable or config does.
SANDBOX_BANNER=""
if [ "$SANDBOX_UNTAGGED" = "1" ]; then
  SANDBOX_BANNER="SANDBOX BUILD (not a release): $(git -C "$REPO_ROOT" rev-parse HEAD 2>/dev/null || echo 'unknown (no commit)')"
  printf '%s\n' "$SANDBOX_BANNER"
fi
step_ok

# ── Step 2: Claude Code ──────────────────────────────────────────────────────────────────────
begin_step 2 "Claude Code"
if command -v claude >/dev/null 2>&1; then
  if [ "$INSTALL_CLAUDE_CODE" -eq 1 ]; then
    step_ok "already installed: $(claude --version 2>&1 | sed -n '1p'); installer not run"
  else
    step_ok "already on PATH: $(command -v claude); --install-claude-code not given"
  fi
else
  _dl="$WORK_DIR/claude-install.sh"
  _inst_log="$WORK_DIR/claude-install.log"
  curl -fsSL "$CLAUDE_INSTALLER_URL" -o "$_dl" \
    || fail "could not download the Claude Code installer from $CLAUDE_INSTALLER_URL" "check the network and re-run, or install by hand: $CLAUDE_INSTALLER_COMMAND"
  if ! bash "$_dl" >"$_inst_log" 2>&1; then
    show_log_tail "$_inst_log"
    fail "the Claude Code installer exited non-zero" "read its output above; fix it or install Claude Code by hand, then re-run without --install-claude-code"
  fi
  PATH="$HOME/.local/bin:$PATH"
  export PATH
  command -v claude >/dev/null 2>&1 \
    || fail "the Claude Code installer finished but claude is not on PATH" "add \$HOME/.local/bin to PATH (open a new shell) and re-run"
  claude --version >/dev/null 2>&1 \
    || fail "claude was installed but 'claude --version' fails" "run 'claude --version' yourself to see why, then re-run"
  step_ok "installed: $(claude --version 2>&1 | sed -n '1p')"
fi

# ── Step 3: config.json ──────────────────────────────────────────────────────────────────────
begin_step 3 "config.json"
if ! _cfg_out="$(python3 -c "$PY_CONFIG" "$CONFIG_PATH" "$TIER_ARG" "$DEFAULT_TIER")"; then
  fail "${_cfg_out#ERR }" "fix or remove $CONFIG_PATH by hand and re-run (this installer never overwrites a config it cannot parse)"
fi
# Line 1 is "<status> <tier>" (the EFFECTIVE value: kept, changed or default);
# later lines are "config: <key> <old> -> <new>" for every stored value this run changes.
_cfg_head="$(printf '%s\n' "$_cfg_out" | sed -n '1p')"
read -r _cfg_status TIER <<EOF
$_cfg_head
EOF
printf '%s\n' "$_cfg_out" | sed '1d'
step_ok "$CONFIG_PATH $_cfg_status: install_tier=$TIER; other keys keep their values and order"

# ── Step 4: venv ─────────────────────────────────────────────────────────────────────────────
begin_step 4 "venv"
_pip_log="$WORK_DIR/pip.log"
printf '[step 4] installing requirements into %s (this can take several minutes; if it fails the pip output is kept at %s/pip.log)\n' "$VENV_DIR" "$LOG_SAVE_DIR"
if [ ! -x "$VENV_PY" ]; then
  python3 -m venv "$VENV_DIR" >"$_pip_log" 2>&1 \
    || { show_log_tail "$_pip_log"; fail "could not create the venv at $VENV_DIR" "read the output above; remove $VENV_DIR if it is half-made, and re-run"; }
fi
# The CPU torch wheel is the right one for ENGRAM; pip's default on Linux x86_64 is the CUDA
# build (~2 GB). Other platforms have no such index entry, so they get no extra index.
if [ "$(uname -s)" = "Linux" ] && [ "$(uname -m)" = "x86_64" ]; then
  "$VENV_PY" -m pip install --extra-index-url https://download.pytorch.org/whl/cpu \
    -r "$REPO_ROOT/requirements/requirements.txt" >"$_pip_log" 2>&1 \
    || { show_log_tail "$_pip_log"; fail "pip install of requirements/requirements.txt failed" "read the output above (network? disk space?) and re-run; the install is safe to repeat; full pip output: $LOG_SAVE_DIR/pip.log"; }
else
  "$VENV_PY" -m pip install -r "$REPO_ROOT/requirements/requirements.txt" >"$_pip_log" 2>&1 \
    || { show_log_tail "$_pip_log"; fail "pip install of requirements/requirements.txt failed" "read the output above (network? disk space?) and re-run; the install is safe to repeat; full pip output: $LOG_SAVE_DIR/pip.log"; }
fi
if [ "$TIER" = "dev" ]; then
  [ -f "$REPO_ROOT/requirements/requirements-dev.txt" ] \
    || fail "requirements/requirements-dev.txt missing under $REPO_ROOT" "run this script from a complete engram checkout, or use --tier convenience"
  "$VENV_PY" -m pip install -r "$REPO_ROOT/requirements/requirements-dev.txt" >"$_pip_log" 2>&1 \
    || { show_log_tail "$_pip_log"; fail "pip install of requirements/requirements-dev.txt failed" "read the output above and re-run, or use --tier convenience"; }
fi
step_ok "$VENV_DIR, requirements installed (tier $TIER)"

# ── Step 5: build, assemble, register the marketplace ────────────────────────────────────────
# install-local-marketplace.sh reads install_tier from config.json (step 3),
# builds the plugin, assembles the marketplace, pre-warms the embedding model after pip, and
# registers the marketplace. Its non-zero exit is this step's FAIL. Its output is not filtered.
begin_step 5 "build, assemble and register the marketplace"
[ -f "$TOOLS_DIR/install-local-marketplace.sh" ] \
  || fail "install-local-marketplace.sh not found beside this script ($TOOLS_DIR)" "run this script from a complete engram checkout"
printf '[step 5] running install-local-marketplace.sh (its output follows)\n'
_market_flags=""
[ "$SANDBOX_UNTAGGED" != "1" ] || _market_flags="--allow-untagged --allow-branch"
# shellcheck disable=SC2086  # intentional word splitting of the two fixed flags
bash "$TOOLS_DIR/install-local-marketplace.sh" $_market_flags \
  || fail "install-local-marketplace.sh exited non-zero" "read its output above (a tag or branch guard, the model pre-warm and pip are the usual causes) and re-run"
[ -f "$PLUGIN_ROOT/bootstrap.py" ] \
  || fail "the marketplace was assembled but $PLUGIN_ROOT/bootstrap.py is missing" "re-run the build; if it persists the plugin build is broken"
step_ok

# ── Step 6: bootstrap ────────────────────────────────────────────────────────────────────────
# The installer decides the knowledge.db state itself: bootstrap.py only handles "absent".
#   absent                  -> run bootstrap.py
#   present, 0 nodes        -> run it with FORCE_RESEED_EMPTY=1 (an aborted run, or the empty
#                              file a daemon created) and say so
#   present, non-empty      -> skip it and print the node count (the upgrade case)
begin_step 6 "bootstrap"
BOOTSTRAP_RAN=0
_run_bootstrap=0
_force_empty=0
_state_note=""
if [ ! -e "$DB_PATH" ]; then
  _run_bootstrap=1
  _state_note="knowledge.db absent: seeding"
else
  if ! _nodes="$(python3 -c "$PY_NODE_COUNT" "$DB_PATH")"; then
    fail "${_nodes#ERR }" "release any lock on the database, or restore it from backup, and re-run"
  fi
  if [ "$_nodes" = "0" ]; then
    _run_bootstrap=1
    _force_empty=1
    _state_note="knowledge.db present with 0 nodes: re-seeding with FORCE_RESEED_EMPTY=1"
  else
    _state_note="knowledge.db present with $_nodes nodes: bootstrap skipped"
  fi
fi

if [ "$_run_bootstrap" -eq 1 ]; then
  # Back up an existing CLAUDE.md BEFORE bootstrap renders over it, once, never over an
  # existing backup. Only when bootstrap will run: on a seat whose graph is seeded the file
  # is ENGRAM's own rendering, and copying it would put the wrong file in the backup.
  if [ -e "$CLAUDE_MD" ] || [ -L "$CLAUDE_MD" ]; then
    if [ -e "$CLAUDE_MD_BACKUP" ] || [ -L "$CLAUDE_MD_BACKUP" ]; then
      _claude_md_note="existing backup kept untouched at $CLAUDE_MD_BACKUP; $CLAUDE_MD will be replaced"
    else
      _go=$DEFAULT_BACKUP_AND_REPLACE_CLAUDE_MD
      if [ "$ASSUME_YES" -eq 0 ] && [ -t 0 ] && [ -t 1 ]; then
        printf '%s already exists. ENGRAM will replace it. Back it up to %s first and continue? [Y/n] ' "$CLAUDE_MD" "$CLAUDE_MD_BACKUP"
        read -r _answer || _answer="n"
        case "$_answer" in ""|y|Y|yes|YES|Yes) _go=1 ;; *) _go=0 ;; esac
      fi
      [ "$_go" -eq 1 ] \
        || fail "$CLAUDE_MD exists and was not backed up" "move or copy it away yourself, or re-run with --yes to back it up to CLAUDE.md.pre-engram and replace it"
      cp -p "$CLAUDE_MD" "$CLAUDE_MD_BACKUP" \
        || fail "could not copy $CLAUDE_MD to $CLAUDE_MD_BACKUP" "fix the permissions of $CLAUDE_HOME and re-run"
      _claude_md_note="backed up $CLAUDE_MD to $CLAUDE_MD_BACKUP; $CLAUDE_MD will be replaced"
    fi
    printf '[step 6] CLAUDE.md: %s\n' "$_claude_md_note"
  fi

  printf '[step 6] %s\n' "$_state_note"
  if [ "$_force_empty" -eq 1 ]; then
    env -u FORCE FORCE_RESEED_EMPTY=1 \
      ALPHA_DIR="$PLUGIN_ROOT" ALPHA_TEMPLATES_DIR="$PLUGIN_ROOT/templates" \
      ENGRAM_HOME="$ENGRAM_HOME" CLAUDE_HOME="$CLAUDE_HOME" PYTHON_BIN="$VENV_PY" \
      TARGET=claude ENGRAM_INSTALL_MODE=plugin \
      "$VENV_PY" "$PLUGIN_ROOT/bootstrap.py" \
      || fail "bootstrap.py failed" "read its output above and re-run; the 0-node state is retried safely"
  else
    env -u FORCE -u FORCE_RESEED_EMPTY \
      ALPHA_DIR="$PLUGIN_ROOT" ALPHA_TEMPLATES_DIR="$PLUGIN_ROOT/templates" \
      ENGRAM_HOME="$ENGRAM_HOME" CLAUDE_HOME="$CLAUDE_HOME" PYTHON_BIN="$VENV_PY" \
      TARGET=claude ENGRAM_INSTALL_MODE=plugin \
      "$VENV_PY" "$PLUGIN_ROOT/bootstrap.py" \
      || fail "bootstrap.py failed" "read its output above; if knowledge.db was left behind with 0 nodes, re-running this installer reseeds it"
  fi
  BOOTSTRAP_RAN=1
  step_ok "$_state_note"
else
  step_ok "$_state_note"
fi

# ── Step 7: plugin install + enable ──────────────────────────────────────────────────────────
# The state is read back, not assumed: `claude plugin list --json` must show the plugin with
# enabled: true. Install and enable may report non-zero when the plugin is already installed
# or already enabled, so the read-back is the gate; their outputs are shown if it fails.
begin_step 7 "plugin install and enable"
_inst_out="$WORK_DIR/plugin-install.out"
_enab_out="$WORK_DIR/plugin-enable.out"
_list_out="$WORK_DIR/plugin-list.json"
_inst_rc=0; _enab_rc=0
claude plugin install "$PLUGIN_REF" >"$_inst_out" 2>&1 || _inst_rc=$?
claude plugin enable "$PLUGIN_REF" >"$_enab_out" 2>&1 || _enab_rc=$?
_list_rc=0
claude plugin list --json >"$_list_out" 2>&1 || _list_rc=$?
if [ "$_list_rc" -ne 0 ] || ! python3 -c "$PY_PLUGIN_ENABLED" "$_list_out" "$PLUGIN_REF" 2>/dev/null; then
  printf -- '--- claude plugin install %s (exit %s) ---\n' "$PLUGIN_REF" "$_inst_rc" >&2
  show_log_tail "$_inst_out"
  printf -- '--- claude plugin enable %s (exit %s) ---\n' "$PLUGIN_REF" "$_enab_rc" >&2
  show_log_tail "$_enab_out"
  printf -- '--- claude plugin list --json (exit %s) ---\n' "$_list_rc" >&2
  show_log_tail "$_list_out"
  fail "$PLUGIN_REF is not installed and enabled according to 'claude plugin list --json'" "run 'claude plugin install $PLUGIN_REF' and 'claude plugin enable $PLUGIN_REF' by hand and read their output; the marketplace is already registered"
fi
step_ok "$PLUGIN_REF enabled"

# ── Step 8: services ─────────────────────────────────────────────────────────────────────────
# Tools come from the assembled plugin tree (as the first-session skill resolves them), not the
# checkout: the service units must point at the plugin's files. A missing service manager is a
# WARN (the first-session skill treats it as non-fatal), anything else that fails is a FAIL.
begin_step 8 "services"
_svc_notes=""
_svc_add() { if [ -n "$_svc_notes" ]; then _svc_notes="$_svc_notes; $1"; else _svc_notes="$1"; fi; }
_os="$(uname -s)"
if [ "$_os" = "Darwin" ]; then _mgr="launchctl"; else _mgr="systemctl"; fi

if [ "$RECALL_SERVICE" -eq 1 ]; then
  _recall_script="$PLUGIN_ROOT/tools/setup-surface-daemon-service.sh"
  _recall_why=""
  if ! command -v "$_mgr" >/dev/null 2>&1; then
    _recall_why="$_mgr not found"
  elif [ "$_os" != "Darwin" ] && ! systemctl --user show-environment >/dev/null 2>&1; then
    # systemctl exists but there is no user session bus (WSL without systemd, a container, ssh
    # without lingering): the service script would fail, so say so and carry on. macOS has no
    # probe here: a cheap `launchctl` equivalent has not been measured, so it keeps the old
    # behaviour (the service script runs and a failure is a FAIL).
    _recall_why="$_mgr is present but has no usable user session"
  fi
  if [ -n "$_recall_why" ]; then
    warn "recall service not installed: $_recall_why — install it later with bash $_recall_script (until then the per-session recall daemon is used; recall still works)"
    _svc_add "recall: skipped ($_recall_why)"
  else
    [ -f "$_recall_script" ] \
      || fail "setup-surface-daemon-service.sh missing from $PLUGIN_ROOT/tools" "rebuild the plugin (re-run this installer), or use --no-recall-service"
    bash "$_recall_script" \
      || fail "the recall service setup exited non-zero" "read its output above, then re-run with --no-recall-service to skip the service and set it up later with bash $_recall_script"
    _svc_add "recall: installed"
  fi
else
  _svc_add "recall: not requested"
fi

if [ "$VIZ_SERVICE" -eq 1 ]; then
  if [ "$_os" = "Linux" ]; then
    [ -f "$PLUGIN_ROOT/tools/operator-setup-viz.sh" ] \
      || fail "operator-setup-viz.sh missing from $PLUGIN_ROOT/tools" "rebuild the plugin (re-run this installer), or drop --viz-service"
    bash "$PLUGIN_ROOT/tools/operator-setup-viz.sh" \
      || fail "the viz service setup exited non-zero" "read its output above, or drop --viz-service and run operator-setup-viz.sh later"
    _svc_add "viz: installed"
  else
    printf '[step 8] --viz-service is Linux only; nothing done on %s\n' "$_os"
    _svc_add "viz: no-op on $_os"
  fi
fi

if [ -n "$FORUM_URL" ]; then
  [ -f "$PLUGIN_ROOT/tools/set_forum_url.py" ] \
    || fail "set_forum_url.py missing from $PLUGIN_ROOT/tools" "rebuild at tier convenience or dev, or set forum.url in $CONFIG_PATH by hand"
  "$VENV_PY" "$PLUGIN_ROOT/tools/set_forum_url.py" "$FORUM_URL" --config "$CONFIG_PATH" \
    || fail "set_forum_url.py refused $FORUM_URL" "read its message above; it names what is wrong with the URL or the config"
  _svc_add "forum.url set"
fi
step_ok "$_svc_notes"

# ── Step 9: self-check ───────────────────────────────────────────────────────────────────────
# Right after a bootstrap this run, the seed graph must be exactly the seed: pre-first-session.
# When bootstrap was skipped the graph may already hold a first session's work, which that
# phase would (wrongly) call a failure, so the `installed` phase runs instead.
begin_step 9 "self-check"
if [ "$BOOTSTRAP_RAN" -eq 1 ]; then _phase="pre-first-session"; else _phase="installed"; fi
[ -f "$TOOLS_DIR/install_selfcheck.py" ] \
  || fail "install_selfcheck.py not found beside this script ($TOOLS_DIR)" "run this script from a complete engram checkout"
"$VENV_PY" "$TOOLS_DIR/install_selfcheck.py" --phase "$_phase" \
  || fail "the self-check reported a failure (--phase $_phase)" "each FAIL line above names the item and its remedy; fix those and re-run this installer"
step_ok "--phase $_phase"

# ── Step 10: what happens next ───────────────────────────────────────────────────────────────
begin_step 10 "done"
step_ok
cat <<'EOF'

ENGRAM is installed. What you do next:
  1. Run:  claude
  2. If Claude Code asks you to sign in, run:  /login
  3. The first session starts by itself and walks you through the naming dialogue.
EOF
[ -z "$SANDBOX_BANNER" ] || printf '\n%s\n' "$SANDBOX_BANNER"
