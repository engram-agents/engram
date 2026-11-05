#!/usr/bin/env bash
# tools/setup-surface-daemon-service.sh — Install the ENGRAM surface daemon as a
# persistent OS service (systemd user service on Linux, LaunchAgent on macOS).
#
# The daemon will start at login and restart automatically on exit. The
# ENGRAM_DAEMON_IDLE_TIMEOUT=0 environment variable is baked into the service
# unit so the daemon never self-exits on idle (persistent-service mode).
#
# Usage:
#   tools/setup-surface-daemon-service.sh
#   tools/setup-surface-daemon-service.sh --owner     # the engramd OWNER (#3663 Linux, #3709 macOS)
#   tools/setup-surface-daemon-service.sh --help      # print the usage, change nothing
# Options: --owner, --allow-path (below), -h/--help. Any OTHER argument exits 2 naming it, before
# anything is written or any systemctl/launchctl call (#3939; it used to be ignored, so `--help`
# ran a real install).
#
# --owner installs the engramd owner (engramd_owner.py) instead. One supervisor entry per
# ENGRAM_HOME, not per user, named with <id> = first 8 hex chars of sha256 over the
# NORMALIZED (absolute, physical, no trailing slash) ENGRAM_HOME, derived by
# tools/engramd-owner-home-key.sh -- the one helper hooks/start-engramd-owner.sh also uses:
#   Linux  → the systemd USER unit engramd-owner-<id>.service rendered from
#            engramd-owner.service.template (restarts are unbounded; the retry RATE is
#            bounded by exponential backoff, systemd >= 254).
#   Darwin → the LaunchAgent com.engram.engramd-owner.<id> rendered from
#            engramd-owner.plist.template to ~/Library/LaunchAgents/<label>.plist, then
#            `launchctl bootout gui/<uid>/<label>` (failure ignored) and
#            `launchctl bootstrap gui/<uid> <plist>` (#3709). KeepAlive is
#            {SuccessfulExit: false} with ThrottleInterval 60: exit 0 is not restarted, a
#            non-zero exit retries once a minute with NO start limit (the template's header
#            says why that is acceptable).
#   Darwin, before anything is written: ENGRAM_HOME is run through the owner's own
#            `engramd_owner.py --preflight` (platform support, socket-path length, the
#            ~/Documents|Desktop|Downloads TCC home check: the functions start() runs, not
#            copies) first on the tilde-expanded absolute path, BEFORE the home is created
#            (a mkdir under ~/Documents is itself a TCC access), and again on the physical
#            path (it can be longer). A refusal exits non-zero with the owner's reason and
#            leaves no home dir, no LaunchAgents dir, no plist and no launchctl call: a
#            refusal under KeepAlive is a respawn loop by construction.
# The ephemeral-path refusal below applies to engramd_owner.py's physical path in this
# mode. Without --owner the behaviour is exactly what it always was.
#
# macOS bootout/bootstrap race (#3939): `launchctl bootout` returns before launchd has finished
# removing the service, so an immediate `bootstrap` can fail "5: Input/output error". After the
# bootout this script polls `launchctl print gui/<uid>/<label>` until it fails (the service is gone),
# bounded; at the deadline it still tries bootstrap. A bootstrap failure other than 125 is retried
# (5 attempts in all); after the last one the script exits with launchctl's last rc and one stderr
# message naming the label, the attempts, launchctl's text and the manual `launchctl bootstrap` command.
# Timing overrides (seconds, a plain decimal; anything else falls back to the default; tests set tiny values):
#   ENGRAM_SETUP_LAUNCHD_WAIT_S  total time to wait for the service to go away   (default 10)
#   ENGRAM_SETUP_LAUNCHD_POLL_S  pause between `launchctl print` polls           (default 0.2)
#   ENGRAM_SETUP_LAUNCHD_RETRY_S pause between bootstrap attempts                (default 1)
#
# Exit codes: 0 done; 1 an error (missing tool or template, a preflight refusal); 2 a refused
# value (unsupported character, `..`, ephemeral path) or an unknown argument; 3 (#3780, Darwin) launchd has no GUI
# domain for this uid (`launchctl bootstrap gui/<uid>` exits 125: no GUI login session). The
# plist WAS written but is not active now; whether launchd loads it at this account's next GUI
# login has not been measured. Any other launchctl bootstrap failure (after the retries above) exits with launchctl's own last rc.
#
# --owner refuses any ENGRAM_HOME with a `..` path component (exit 2, before anything is written):
# mkdir and cd/pwd -P read `..` after a symlink differently. Names like `a..b` are fine.
#
# The script detects the OS via `uname -s`:
#   Linux  → renders engram-surface-daemon.service.template and installs it as a
#             systemd user service (~/.config/systemd/user/).
#   Darwin → renders engram-surface-daemon.plist.template and installs it as a
#             LaunchAgent (~/Library/LaunchAgents/).
#
# Surface-daemon supervisor names are KEYED on ENGRAM_HOME (#4305, #2702), with the same <id> the
# owner uses (tools/engramd-owner-home-key.sh, the ONE derivation):
#   Linux  → engram-surface-daemon-<id>.service      Darwin → com.engram.surface-daemon.<id>
# so two ENGRAM_HOMEs under one OS user no longer share (and replace) one job. ENGRAM_HOME is
# created if missing (the key needs an existing directory).
# Upgrade from the old FIXED name (the legacy-transition code, _legacy_surface_*): the old unit /
# job is stopped and removed BEFORE the keyed one is started, and ONLY if its own ENGRAM_HOME is
# this home (physical-path comparison); another home's old job is left alone and named on one
# line; an old job whose ENGRAM_HOME cannot be read is left alone and named on one line.
# Stop condition for a deleted home: the unit carries ExecCondition=/usr/bin/test -d <home>
# in [Service] (re-run before every restart; ConditionPathExists is not; systemd >= 243); the plist's
# KeepAlive is {PathState: {<home>: true}} (measured once on a Mac, best-effort; see the template).
#
# Idempotent: re-running re-renders the unit/plist and re-enables the service.
# Fails loudly if systemctl (Linux) or launchctl (macOS) is absent.
#
# Placeholder substitutions (matching both templates):
#   {{PYTHON}}           — canonical Python interpreter (prefers ~/.engram/venv)
#   {{ENGRAM_HOOKS_DIR}} — directory containing engram-surface-daemon.py
#   {{ENGRAM_HOME}}      — ENGRAM data directory (~/.engram if unset)
#
# Mirror of conventions from tools/operator-setup-viz.sh.
#
# Override / testing:
#   ENGRAM_HOOKS_DIR — if set, skips auto-resolution and uses this directory
#                      directly as HOOKS_DIR (also used by layer-b testing against
#                      the real deployed bundle).
#
# Ephemeral-path refusal (#3519 item 1):
#   The script REFUSES (exit 2, before writing any plist/unit and before calling
#   launchctl/systemctl) when the physical, symlink-resolved path of
#   engram-surface-daemon.py lies under a temp root (/tmp, /private/tmp,
#   /var/tmp, /private/var/tmp, /var/folders, /private/var/folders, or $TMPDIR
#   when set and non-empty) or contains a /.claude/worktrees/ component pair
#   (agent/fairy worktrees). A service pointing at such a path outlives its
#   directory and respawns forever (2026-10-03: 27,605 launchd respawns).
#   Override, when you really mean it: pass --allow-path, or set
#   ENGRAM_SETUP_ALLOW_PATH=1 (exactly 1). The script then prints one WARN line.

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

# ---------------------------------------------------------------------------
# Arguments -- --allow-path, --owner, -h/--help. Anything else exits 2 naming it, before anything is
# written or any service-manager call (#3939; it used to be ignored, which made `--help` run a real
# install). `--help` anywhere prints the usage and exits 0 (also before an unknown argument is judged).
# ---------------------------------------------------------------------------
_usage() {
    cat <<'USAGE_EOF'
Usage: setup-surface-daemon-service.sh [--owner] [--allow-path]
       setup-surface-daemon-service.sh -h | --help

Installs the ENGRAM surface daemon as a persistent OS service (systemd user service on Linux,
LaunchAgent on macOS).

  --owner       install the engramd OWNER service (one per ENGRAM_HOME) instead
  --allow-path  proceed although the daemon path is under a temp root or an agent worktree
                (same as ENGRAM_SETUP_ALLOW_PATH=1)
  -h, --help    print this usage and exit 0, changing nothing

Any other argument exits 2 before anything is written.
USAGE_EOF
}
ALLOW_PATH=0
OWNER_MODE=0
for _arg in "$@"; do
    if [[ "$_arg" == "-h" || "$_arg" == "--help" ]]; then
        _usage
        exit 0
    fi
done
for _arg in "$@"; do
    if [[ "$_arg" == "--allow-path" ]]; then
        ALLOW_PATH=1
    elif [[ "$_arg" == "--owner" ]]; then
        OWNER_MODE=1
    else
        echo "ERROR: unknown argument: $_arg (see --help). Nothing was written." >&2
        exit 2
    fi
done
if [[ "${ENGRAM_SETUP_ALLOW_PATH:-}" == "1" ]]; then
    ALLOW_PATH=1
fi

# --owner supports Linux (systemd --user) and macOS (launchd) only: refuse anything else
# BEFORE touching anything.
if [[ "$OWNER_MODE" == "1" ]]; then
    _owner_os="$(uname -s)"
    if [[ "$_owner_os" != "Linux" && "$_owner_os" != "Darwin" ]]; then
        echo "ERROR: --owner supports Linux (systemd --user) and macOS (launchd) only; this is $_owner_os. Nothing was written." >&2
        exit 1
    fi
fi

# ---------------------------------------------------------------------------
# _pinned_into <varname> <cmd...>: run <cmd...> and assign its stdout (trailing newlines stripped, as
# `$(...)` does) to the variable NAMED by $1; the return status is the command's. Every PATH value this
# script captures goes through here (#3794): bash 3.2 mangles the bytes of a `$(...)` capture (inserting
# \001 after each byte >= 0x80) when the CAPTURING shell is in a multibyte locale, and a pin written
# inside the substitution does not help. The capture runs INSIDE this function under its own
# `local LC_ALL=C`; only this function's shell is pinned, never the script's (#3780).
# A name that is one of this function's own locals (`_pin_*`, `LC_ALL`) is refused: `printf -v` would
# assign the local and leave the caller's variable unset with rc 0. Exit 1, one stderr line.
# ---------------------------------------------------------------------------
_pinned_into() {
    local LC_ALL=C _pin_name="${1-}" _pin_v _pin_rc=0
    case "$_pin_name" in
        _pin_*|LC_ALL)
            echo "ERROR: cannot assign to the variable name '$_pin_name': it is one of _pinned_into's own local names" >&2
            return 1
            ;;
    esac
    shift
    _pin_v="$("$@")" || _pin_rc=$?
    printf -v "$_pin_name" '%s' "$_pin_v"
    return "$_pin_rc"
}
_resolve_self() { readlink -f "$1" 2>/dev/null || echo "$1"; }
_cd_pwd() { cd "$1" && pwd; }
_sys_python() { python3 -c 'import sys; print(sys.executable)' 2>/dev/null || echo ""; }

# The fixed supervisor names of the pre-#4305 install. They exist ONLY here (the legacy-transition
# code); tests/test_surface_label_keyed_4305.py::test_G1 fails on any other literal.
_legacy_surface_unit() {
    printf '%s' 'engram-surface-daemon.service'
}
_legacy_surface_label() {
    printf '%s' 'com.engram.surface-daemon'
}

# _home_norm_into <path> <varname>: the physical path of <path> if it is a directory, else the
# raw string with trailing slashes stripped. Assigned to the variable NAMED by $2 (the capture runs
# under this function's own LC_ALL=C: bash 3.2 mangles multibyte captures otherwise, #3794).
_home_norm_into() {
    local LC_ALL=C _hn_in="${1-}" _hn_name="${2-}" _hn_out
    _hn_out="$(cd "$_hn_in" 2>/dev/null && pwd -P)" || {
        _hn_out="$_hn_in"
        while [[ "${#_hn_out}" -gt 1 && "$_hn_out" == */ ]]; do _hn_out="${_hn_out%/}"; done
    }
    printf -v "$_hn_name" '%s' "$_hn_out"
}

# ---------------------------------------------------------------------------
# Resolve script location and repo root (template source)
# ---------------------------------------------------------------------------
_pinned_into _self _resolve_self "${BASH_SOURCE[0]}"
_pinned_into _script_dir dirname "$_self"

# Templates live alongside this script's parent bundle root.
# In a plugin install the layout is:
#   <plugin_root>/tools/setup-surface-daemon-service.sh  (this file)
#   <plugin_root>/templates/*.template
#   <plugin_root>/hooks/engram-surface-daemon.py         (deployed bundle — flat)
# In a repo-clone install, script_dir is src/engram/tools/ → step up two
# levels to reach src/engram/. Either way, the template dir is:
#   <script_dir>/../templates/
_pinned_into PLUGIN_ROOT _cd_pwd "$_script_dir/.."
TEMPLATE_DIR="$PLUGIN_ROOT/templates"

# ---------------------------------------------------------------------------
# Resolve HOOKS_DIR — where engram-surface-daemon.py lives.
#
# Precedence (first match wins):
#   1. ENGRAM_HOOKS_DIR env var (explicit override; also used for layer-b testing)
#   2. $PLUGIN_ROOT/hooks/engram-surface-daemon.py exists → flat bundle layout
#   3. $PLUGIN_ROOT/hooks/claude/engram-surface-daemon.py exists → source-tree layout
#   4. Neither found → fail loud (surface at setup-time, not as a silent broken service)
# ---------------------------------------------------------------------------
if [[ "$OWNER_MODE" == "1" ]]; then
    HOOKS_DIR=""   # --owner needs no hooks dir; engramd_owner.py sits at $PLUGIN_ROOT
elif [[ -n "${ENGRAM_HOOKS_DIR:-}" ]]; then
    HOOKS_DIR="$ENGRAM_HOOKS_DIR"
elif [[ -f "$PLUGIN_ROOT/hooks/engram-surface-daemon.py" ]]; then
    HOOKS_DIR="$PLUGIN_ROOT/hooks"
elif [[ -f "$PLUGIN_ROOT/hooks/claude/engram-surface-daemon.py" ]]; then
    HOOKS_DIR="$PLUGIN_ROOT/hooks/claude"
else
    echo "ERROR: could not locate engram-surface-daemon.py under $PLUGIN_ROOT/hooks or $PLUGIN_ROOT/hooks/claude" >&2
    exit 1
fi

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
ENGRAM_HOME="${ENGRAM_HOME:-$HOME/.engram}"

# ---------------------------------------------------------------------------
# Python interpreter — prefer the ENGRAM venv, fall back to system python3
# ---------------------------------------------------------------------------
ENGRAM_VENV_PYTHON="$HOME/.engram/venv/bin/python3"
if [[ -x "$ENGRAM_VENV_PYTHON" ]]; then
    PYTHON_BIN="$ENGRAM_VENV_PYTHON"
else
    _pinned_into PYTHON_BIN _sys_python
fi

if [[ -z "$PYTHON_BIN" ]]; then
    echo "ERROR: python3 not found. Install Python 3.10+ or create a venv at ~/.engram/venv." >&2
    exit 1
fi

# ---------------------------------------------------------------------------
# Validate the daemon script exists
# ---------------------------------------------------------------------------
DAEMON_PY="$HOOKS_DIR/engram-surface-daemon.py"
if [[ "$OWNER_MODE" == "1" ]]; then
    # In --owner mode the "daemon script" every check below guards IS the owner.
    # engramd_owner.py sits flat at the plugin root in the bundle AND in src/engram/.
    DAEMON_PY="$PLUGIN_ROOT/engramd_owner.py"
fi
if [[ ! -f "$DAEMON_PY" ]]; then
    echo "ERROR: daemon script not found: $DAEMON_PY" >&2
    echo "       Run this script from within an engram-alpha repo clone or a" >&2
    echo "       plugin bundle where hooks/engram-surface-daemon.py exists." >&2
    exit 1
fi

# ---------------------------------------------------------------------------
# Refuse an ephemeral daemon path (#3519 item 1) — BEFORE any OS branching,
# plist/unit write, or launchctl/systemctl call.
#
# Physical resolution in plain bash (macOS bash 3.2: no readlink -f, no
# realpath): follow a file symlink chain with plain `readlink`, then resolve the
# directory with `cd ... && pwd -P`.
# ---------------------------------------------------------------------------
_physical_path() {
    # LC_ALL=C from the first line: a path VALUE is bytes, and bash 3.2 mangles a multibyte capture otherwise
    # (#3780), the readlink/dirname captures of the loop below included (#3794).
    # Optional 2nd argument: the NAME of a variable to assign the result to (no stdout), because a
    # `$(_physical_path ...)` at the caller's top level is itself an unpinned capture. A name that is one of
    # this function's own locals (`_pp_*`, `LC_ALL`) is refused: exit 1, one stderr line.
    local LC_ALL=C _pp_p="${1-}" _pp_out="${2-}" _pp_target _pp_n=0 _pp_d _pp_b _pp_res
    case "$_pp_out" in
        _pp_*|LC_ALL)
            echo "ERROR: cannot assign to the variable name '$_pp_out': it is one of _physical_path's own local names" >&2
            return 1
            ;;
    esac
    while [[ -L "$_pp_p" && $_pp_n -lt 40 ]]; do
        _pp_target="$(readlink "$_pp_p")"
        if [[ "$_pp_target" == /* ]]; then
            _pp_p="$_pp_target"
        else
            _pp_p="$(dirname "$_pp_p")/$_pp_target"
        fi
        _pp_n=$((_pp_n + 1))
    done
    _pp_d="$(dirname "$_pp_p")"
    _pp_b="${_pp_p##*/}"
    if _pp_d="$(cd "$_pp_d" 2>/dev/null && pwd -P)"; then
        if [[ "$_pp_d" == "/" ]]; then
            _pp_res="/$_pp_b"
        else
            _pp_res="$_pp_d/$_pp_b"
        fi
    else
        _pp_res="$_pp_p"
    fi
    if [[ -n "$_pp_out" ]]; then
        printf -v "$_pp_out" '%s' "$_pp_res"
    else
        echo "$_pp_res"
    fi
}

_under_root() {
    # $1 = path, $2 = root. Component-bounded prefix match. An empty root (or
    # "/" once trailing slashes are stripped) matches nothing.
    local path="$1" root="$2"
    while [[ "$root" == */ ]]; do root="${root%/}"; done
    [[ -n "$root" ]] || return 1
    [[ "$path" == "$root" || "$path" == "$root"/* ]]
}

_ephemeral_reason() {
    local path="$1" root LC_ALL=C  # C: the `pwd -P` capture below is a path value (#3780)
    local roots=(/tmp /private/tmp /var/tmp /private/var/tmp /var/folders /private/var/folders)
    if [[ -n "${TMPDIR:-}" ]]; then
        roots+=("$TMPDIR")
        roots+=("$(cd "$TMPDIR" 2>/dev/null && pwd -P || true)")
    fi
    for root in "${roots[@]}"; do
        if _under_root "$path" "$root"; then
            echo "it is under the temp directory $root"
            return 0
        fi
    done
    if [[ "$path/" == */.claude/worktrees/* ]]; then
        echo "it is inside an agent/fairy worktree (.claude/worktrees)"
        return 0
    fi
    return 1
}

_physical_path "$DAEMON_PY" DAEMON_REAL
if _why="$(_ephemeral_reason "$DAEMON_REAL")"; then
    if [[ "$ALLOW_PATH" == "1" ]]; then
        echo "WARN: registering a service for an ephemeral daemon path $DAEMON_REAL ($_why); proceeding because --allow-path / ENGRAM_SETUP_ALLOW_PATH=1 was given." >&2
    else
        echo "ERROR: refusing to register a service for $DAEMON_REAL: ephemeral location ($_why); the service would respawn forever once that directory is deleted. Override with --allow-path or ENGRAM_SETUP_ALLOW_PATH=1." >&2
        exit 2
    fi
fi

# ---------------------------------------------------------------------------
# OS detection
# ---------------------------------------------------------------------------
OS="$(uname -s)"

# --owner: refuse values the unit render cannot carry (I1). The unit is rendered with sed
# (`&` expands to the match, `\` is eaten, `|` aborts the s||| and leaves a truncated
# file) and then read by systemd (whitespace splits Environment=/ExecStart, `%` and `$`
# expand, quotes are interpreted). A wrong-home unit named for home X is silent wrong
# supervision, so refuse BEFORE the destination is opened or any directory is created.
_owner_bad_char() {
    local LC_ALL=C _prefix
    case "$1" in
        *' '*) printf 'space' ;;
        # ONE arm for the CLASS of control bytes (0x01-0x1F and 0x7F), not a list of members:
        # the plist is XML and systemd reads the unit line by line, so whatever a member list does
        # not name (BEL, ESC, ...) would fall through. `[[:cntrl:]]` is locale-dependent, so the
        # match runs under LC_ALL=C (the `local` below is function-scoped). On LINUX this is a
        # small behaviour change: the arms it replaces named only tab, LF and CR, so the other
        # C0 bytes and DEL are now refused there too. NUL cannot occur in a shell string.
        *[[:cntrl:]]*)
            _prefix="${1%%[[:cntrl:]]*}"
            printf 'control byte 0x%02X' "'${1:${#_prefix}:1}"
            ;;
        *'&'*) printf 'ampersand' ;;
        *'\'*) printf 'backslash' ;;
        *'|'*) printf 'pipe' ;;
        *'%'*) printf 'percent' ;;
        *'"'*) printf 'double-quote' ;;
        *"'"*) printf 'single-quote' ;;
        *'$'*) printf 'dollar' ;;
        # sed substitutes the python and owner paths BEFORE ENGRAM_HOME, so a literal
        # {{ENGRAM_HOME}}/{{PYTHON}} inside them would be re-substituted: refuse {{ and }}
        # in every value (one uniform rule).
        *'{{'*) printf 'open-brace-pair' ;;
        *'}}'*) printf 'close-brace-pair' ;;
        # The macOS plist is XML: `<` and `>` would break it. (Linux units carry them fine, and
        # the Linux --owner behaviour is unchanged.)
        *'<'*) [[ "$OS" == "Darwin" ]] && printf 'less-than' || return 1 ;;
        *'>'*) [[ "$OS" == "Darwin" ]] && printf 'greater-than' || return 1 ;;
        *) return 1 ;;
    esac
}
_owner_refuse_if_bad() {
    # $1 = what the value is, $2 = the value
    local _name _what
    if _name="$(_owner_bad_char "$2")"; then
        case "$_name" in
            "control byte "*) _what="$_name" ;;
            a*|e*|i*|o*|u*) _what="an $_name character" ;;
            *) _what="a $_name character" ;;
        esac
        echo "ERROR: unsupported in --owner mode: $1 contains $_what ($(printf '%q' "$2")); the unit would be rendered or read wrongly. No unit was written or loaded." >&2
        exit 2
    fi
}
if [[ "$OWNER_MODE" == "1" ]]; then
    case "$ENGRAM_HOME" in
        "~") _owner_home_raw="$HOME" ;;
        "~/"*) _owner_home_raw="$HOME/${ENGRAM_HOME#"~/"}" ;;
        *) _owner_home_raw="$ENGRAM_HOME" ;;
    esac
    _owner_refuse_if_bad "ENGRAM_HOME" "$_owner_home_raw"
    # A `..` anywhere in the home is refused (both platforms, one rule). `mkdir -p` follows the
    # kernel (a `..` after a symlink goes to the link TARGET's parent) while `cd`/`pwd -P` read it
    # lexically, so the directory created could be a different one than the one planned and checked.
    case "/$_owner_home_raw/" in
        */../*)
            echo "ERROR: the home path must not contain '..' components (ENGRAM_HOME=$(printf '%q' "$_owner_home_raw")); a '..' after a symlink makes mkdir create a different directory than the one checked. Give the path without '..'. No unit was written or loaded." >&2
            exit 2
            ;;
    esac
    _owner_refuse_if_bad "the python path" "$PYTHON_BIN"
    _owner_refuse_if_bad "the owner script path" "$DAEMON_PY"
fi

# macOS --owner: run the OWNER'S OWN start-time refusals (platform support, socket-path length, the
# TCC home check) through `engramd_owner.py --preflight`, before anything is written. The owner's
# reason goes to our stderr; nothing has been created yet when it refuses. ENGRAM_HOME is passed in
# the environment, as launchd will pass it.
_owner_preflight() {
    # $1 = absolute ENGRAM_HOME to check, $2 = which pass (for the message)
    local _out _rc
    _out="$(ENGRAM_HOME="$1" "$PYTHON_BIN" "$DAEMON_PY" --preflight 2>&1)" && _rc=0 || _rc=$?
    if [[ "$_rc" != "0" ]]; then
        echo "ERROR: the owner would refuse to start with ENGRAM_HOME=$1 ($2); registering it would only respawn. Nothing was written or loaded." >&2
        [[ -n "$_out" ]] && echo "$_out" >&2
        exit 1
    fi
}

# _owner_physical_dir_into <dir> <varname>: assign the `cd && pwd -P` physical path of <dir> to the
# variable NAMED by $2 (no stdout; non-zero if <dir> cannot be entered). The capture runs inside this
# function under its own `local LC_ALL=C`: bash 3.2 mangles the bytes of a `$(...)` capture (inserting
# \001 after each byte >= 0x80) when the CAPTURING shell is in a multibyte locale, and a pin written
# inside the substitution does not help. Only this function's shell is pinned, never the script (#3780).
# A name that is one of this function's own locals (`_opdi_*`, `LC_ALL`) is refused: exit 1, one stderr line
# (`printf -v` would assign the local and leave the caller's variable unset with rc 0; #3794).
_owner_physical_dir_into() {
    local LC_ALL=C _opdi_dir="${1-}" _opdi_name="${2-}" _opdi_pd
    case "$_opdi_name" in
        _opdi_*|LC_ALL)
            echo "ERROR: cannot assign to the variable name '$_opdi_name': it is one of _owner_physical_dir_into's own local names" >&2
            return 1
            ;;
    esac
    _opdi_pd="$(cd "$_opdi_dir" && pwd -P)" || return 1
    printf -v "$_opdi_name" '%s' "$_opdi_pd"
}

if [[ "$OWNER_MODE" == "1" ]]; then
    # Normalize ENGRAM_HOME (after the refusals above, so a refusal creates nothing):
    # ~ expanded, created, made absolute and physical, no trailing slash. The unit name /
    # label is keyed on this string (by the shared helper), so every spelling of one home
    # must give the same one.
    case "$ENGRAM_HOME" in
        "~") ENGRAM_HOME="$HOME" ;;
        "~/"*) ENGRAM_HOME="$HOME/${ENGRAM_HOME#"~/"}" ;;
    esac
    # The ONE home-key helper (tools/engramd-owner-home-key.sh): the key derivation AND the
    # physical-path resolution live there, and nowhere else.
    _home_key_sh="$_script_dir/engramd-owner-home-key.sh"
    _owner_source_home_key() {
        if [[ ! -f "$_home_key_sh" ]]; then
            echo "ERROR: home-key helper not found: $_home_key_sh" >&2
            exit 1
        fi
        # shellcheck source=engramd-owner-home-key.sh
        source "$_home_key_sh"
    }
    if [[ "$OS" == "Darwin" ]]; then
        # Absolute (a relative home is relative to this cwd, as the owner reads it).
        [[ "$ENGRAM_HOME" == /* ]] || ENGRAM_HOME="$PWD/$ENGRAM_HOME"
        while [[ "$ENGRAM_HOME" != "/" && "$ENGRAM_HOME" == */ ]]; do ENGRAM_HOME="${ENGRAM_HOME%/}"; done
        # Both preflights run BEFORE any directory is created: a mkdir under ~/Documents would
        # itself be a TCC access, and a refusal must leave no home dir behind. The physical path
        # is computed without creating anything (deepest existing ancestor, resolved, plus the
        # not-yet-existing remainder), so it is checked first too.
        _owner_source_home_key
        _owner_preflight "$ENGRAM_HOME" "as given"
        engramd_owner_physical_path "$ENGRAM_HOME" _owner_physical_planned || {
            echo "ERROR: cannot work out the physical path of ENGRAM_HOME=$ENGRAM_HOME. Nothing was written or loaded." >&2
            exit 1
        }
        _owner_refuse_if_bad "ENGRAM_HOME (physical path)" "$_owner_physical_planned"
        # The physical path can be longer than the spelling that passed above.
        _owner_preflight "$_owner_physical_planned" "physical path"
    fi
    mkdir -p "$ENGRAM_HOME"
    _owner_physical_dir_into "$ENGRAM_HOME" ENGRAM_HOME
    _owner_refuse_if_bad "ENGRAM_HOME (physical path)" "$ENGRAM_HOME"
    while [[ "$ENGRAM_HOME" != "/" && "$ENGRAM_HOME" == */ ]]; do ENGRAM_HOME="${ENGRAM_HOME%/}"; done
    if [[ "$OS" == "Darwin" && "$ENGRAM_HOME" != "$_owner_physical_planned" ]]; then
        # Something moved between the plan and the mkdir (a race, or a case this plan did not
        # foresee). Refuse rather than guess; what mkdir created is left in place.
        echo "ERROR: ENGRAM_HOME resolved to $ENGRAM_HOME after it was created, but $_owner_physical_planned was planned and checked. Refusing to register a different home than the one checked. Nothing was written or loaded (the home dir itself was created and is left in place)." >&2
        exit 1
    fi
    _owner_source_home_key
    _home_id="$(engramd_owner_home_key "$ENGRAM_HOME")" || {
        echo "ERROR: cannot key the unit name / label on ENGRAM_HOME." >&2
        exit 1
    }
    echo "=== ENGRAM owner service setup ==="
else
    echo "=== ENGRAM surface daemon service setup ==="
fi
echo "  OS:            $OS"
echo "  python:        $PYTHON_BIN"
if [[ "$OWNER_MODE" == "1" ]]; then
    echo "  owner script:  $DAEMON_PY"
else
    echo "  hooks dir:     $HOOKS_DIR"
fi
echo "  ENGRAM_HOME:   $ENGRAM_HOME"
echo

# #4336: surface mode renders ENGRAM_HOME with the same sed as --owner but had no refusal. What it
# cannot carry differs by platform (measured on the real templates), so this is NOT _owner_bad_char:
# the unit is read by systemd (whitespace splits, `%` and `$` expand, quotes are interpreted), while
# the plist puts the value in its own XML <string>, where a space, `%`, `$`, quotes and `}}` render
# intact today and refusing them would break a setup that works. Each class is tested on its own
# (never one first-match `case`), so an allowed class cannot mask a refused one later in the value.
# Prints the class name; returns 1 when the value is fine.
_surface_home_bad_char() {
    local LC_ALL=C _prefix
    if [[ "$1" == *[[:cntrl:]]* ]]; then
        _prefix="${1%%[[:cntrl:]]*}"
        printf 'control byte 0x%02X' "'${1:${#_prefix}:1}"
        return 0
    fi
    # both platforms: sed expands `&`, eats `\`, aborts on `|`; a `{{` is re-substituted by a later -e
    if [[ "$1" == *'&'* ]]; then printf 'ampersand'; return 0; fi
    if [[ "$1" == *'\'* ]]; then printf 'backslash'; return 0; fi
    if [[ "$1" == *'|'* ]]; then printf 'pipe'; return 0; fi
    if [[ "$1" == *'{{'* ]]; then printf 'open-brace-pair'; return 0; fi
    if [[ "$OS" == "Darwin" ]]; then
        # the plist is XML: `<` breaks it, `>` is refused as on the --owner path
        if [[ "$1" == *'<'* ]]; then printf 'less-than'; return 0; fi
        if [[ "$1" == *'>'* ]]; then printf 'greater-than'; return 0; fi
    else
        if [[ "$1" == *' '* ]]; then printf 'space'; return 0; fi
        if [[ "$1" == *'%'* ]]; then printf 'percent'; return 0; fi
        if [[ "$1" == *'"'* ]]; then printf 'double-quote'; return 0; fi
        if [[ "$1" == *"'"* ]]; then printf 'single-quote'; return 0; fi
        if [[ "$1" == *'$'* ]]; then printf 'dollar'; return 0; fi
        if [[ "$1" == *'}}'* ]]; then printf 'close-brace-pair'; return 0; fi
    fi
    return 1
}
_surface_refuse_if_bad() {
    # $1 = what the value is (the label in the message), $2 = the value; runs before anything is created
    local _name _what
    if _name="$(_surface_home_bad_char "$2")"; then
        case "$_name" in
            "control byte "*) _what="$_name" ;;
            a*|e*|i*|o*|u*) _what="an $_name character" ;;
            *) _what="a $_name character" ;;
        esac
        echo "ERROR: $1 contains $_what ($(printf '%q' "$2")); the unit or plist would be rendered or read wrongly. No unit or plist was written or loaded and nothing was created." >&2
        exit 2
    fi
}
_surface_refuse_if_bad_home() {
    # $1 = the absolute ENGRAM_HOME
    _surface_refuse_if_bad "ENGRAM_HOME" "$1"
}

# #4347: the same sed renders PYTHON_BIN and HOOKS_DIR, so they get the same per-platform refusal.
# Called from each OS branch after the tool check and before _surface_compute_home_id (python path
# first, then hooks dir, then the home), so nothing is created before the values are judged.
_surface_refuse_if_bad_values() {
    _surface_refuse_if_bad "the python path" "$PYTHON_BIN"
    _surface_refuse_if_bad "the hooks dir" "$HOOKS_DIR"
}

# #4305: surface mode keys its unit / label on the home too. Called from each OS branch AFTER the
# service-manager tool check (so a host without systemctl / launchctl still fails first, and with its
# own message). Absolute first (the helper needs an absolute path), created if missing (the key needs
# an existing directory; the hook does the same). Sets _home_id.
_surface_compute_home_id() {
    [[ "$ENGRAM_HOME" == /* ]] || ENGRAM_HOME="$PWD/$ENGRAM_HOME"
    while [[ "$ENGRAM_HOME" != "/" && "$ENGRAM_HOME" == */ ]]; do ENGRAM_HOME="${ENGRAM_HOME%/}"; done
    _surface_refuse_if_bad_home "$ENGRAM_HOME"
    mkdir -p "$ENGRAM_HOME"
    _home_key_sh="$_script_dir/engramd-owner-home-key.sh"
    if [[ ! -f "$_home_key_sh" ]]; then
        echo "ERROR: home-key helper not found: $_home_key_sh" >&2
        exit 1
    fi
    # shellcheck source=engramd-owner-home-key.sh
    source "$_home_key_sh"
    _home_id="$(engramd_owner_home_key "$ENGRAM_HOME")" || {
        echo "ERROR: cannot key the unit name / label on ENGRAM_HOME." >&2
        exit 1
    }
}

# ---------------------------------------------------------------------------
# Linux: systemd user service
# ---------------------------------------------------------------------------
if [[ "$OS" == "Linux" ]]; then
    if ! command -v systemctl &>/dev/null; then
        echo "ERROR: systemctl not found." >&2
        echo "       This script requires systemd on Linux." >&2
        echo "       On non-systemd Linux, manage the daemon manually:" >&2
        echo "         $PYTHON_BIN $DAEMON_PY" >&2
        exit 1
    fi
    [[ "$OWNER_MODE" == "1" ]] || _surface_refuse_if_bad_values
    [[ "$OWNER_MODE" == "1" ]] || _surface_compute_home_id

    SERVICE_TEMPLATE="$TEMPLATE_DIR/engram-surface-daemon.service.template"
    SERVICE_NAME="engram-surface-daemon-$_home_id.service"
    SERVICE_LOG_NAME="surface-daemon.log"
    if [[ "$OWNER_MODE" == "1" ]]; then
        SERVICE_TEMPLATE="$TEMPLATE_DIR/engramd-owner.service.template"
        SERVICE_NAME="engramd-owner-$_home_id.service"
        SERVICE_LOG_NAME="engramd-owner.log"
    fi
    if [[ ! -f "$SERVICE_TEMPLATE" ]]; then
        echo "ERROR: service template not found: $SERVICE_TEMPLATE" >&2
        exit 1
    fi

    SERVICE_DEST="$HOME/.config/systemd/user/$SERVICE_NAME"
    _pinned_into _service_dir dirname "$SERVICE_DEST"
    mkdir -p "$_service_dir"

    # Legacy transition (#4305): the old fixed-name unit goes BEFORE the keyed one starts, so at no
    # point do two supervised jobs run this home's daemon. Only if it is THIS home's (its own
    # Environment=ENGRAM_HOME= line, compared by physical path); never disable what is not confirmed ours.
    if [[ "$OWNER_MODE" != "1" ]]; then
        _legacy_name="$(_legacy_surface_unit)"
        _legacy_file="$_service_dir/$_legacy_name"
        if [[ -f "$_legacy_file" ]]; then
            _legacy_home="$(sed -n 's/^Environment=ENGRAM_HOME=//p' "$_legacy_file" | head -n 1 || true)"
            if [[ -z "$_legacy_home" ]]; then
                echo "  Left alone: $_legacy_file (old fixed-name unit): could not read its ENGRAM_HOME, so it was not removed."
            else
                _home_norm_into "$_legacy_home" _legacy_home_n
                _home_norm_into "$ENGRAM_HOME" _our_home_n
                if [[ "$_legacy_home_n" == "$_our_home_n" ]]; then
                    echo "  Removing the old fixed-name unit $_legacy_name (ENGRAM_HOME=$_legacy_home is this home) before starting the keyed one"
                    systemctl --user disable --now "$_legacy_name" || {
                        echo "ERROR: could not stop and disable $_legacy_name; not starting $SERVICE_NAME next to it (two daemons for one home)." >&2
                        exit 1
                    }
                    rm -f "$_legacy_file"
                else
                    echo "  Left alone: $_legacy_file (old fixed-name unit) belongs to ENGRAM_HOME=$_legacy_home, not this home."
                fi
            fi
        fi
    fi

    echo "Step 1: Rendering systemd unit → $SERVICE_DEST"
    if [[ "$OWNER_MODE" == "1" ]]; then
        LC_ALL=C sed -e "s|{{PYTHON}}|$PYTHON_BIN|g" \
            -e "s|{{ENGRAM_OWNER_PY}}|$DAEMON_PY|g" \
            -e "s|{{ENGRAM_HOME}}|$ENGRAM_HOME|g" \
            "$SERVICE_TEMPLATE" > "$SERVICE_DEST"
    else
        LC_ALL=C sed -e "s|{{PYTHON}}|$PYTHON_BIN|g" \
            -e "s|{{ENGRAM_HOOKS_DIR}}|$HOOKS_DIR|g" \
            -e "s|{{ENGRAM_HOME}}|$ENGRAM_HOME|g" \
            "$SERVICE_TEMPLATE" > "$SERVICE_DEST"
    fi
    echo "  done."

    echo
    echo "Step 2: Enabling and starting via systemctl --user"
    systemctl --user daemon-reload
    systemctl --user enable --now "$SERVICE_NAME"
    echo "  Status: $(systemctl --user is-active "$SERVICE_NAME" 2>/dev/null || echo unknown)"

    echo
    echo "=== Setup complete ==="
    echo "  Check status:  systemctl --user status $SERVICE_NAME"
    echo "  Follow logs:   journalctl --user -u $SERVICE_NAME -f"
    echo "  Logs file:     $ENGRAM_HOME/$SERVICE_LOG_NAME"
    echo
    echo "  Note: For service persistence across user logout, run:"
    echo "        sudo loginctl enable-linger $USER"

# ---------------------------------------------------------------------------
# macOS: LaunchAgent
# ---------------------------------------------------------------------------
elif [[ "$OS" == "Darwin" ]]; then
    if ! command -v launchctl &>/dev/null; then
        echo "ERROR: launchctl not found." >&2
        echo "       This script requires launchctl on macOS." >&2
        echo "       Manage the daemon manually:" >&2
        echo "         $PYTHON_BIN $DAEMON_PY" >&2
        exit 1
    fi
    [[ "$OWNER_MODE" == "1" ]] || _surface_refuse_if_bad_values
    [[ "$OWNER_MODE" == "1" ]] || _surface_compute_home_id

    PLIST_TEMPLATE="$TEMPLATE_DIR/engram-surface-daemon.plist.template"
    PLIST_LABEL="com.engram.surface-daemon.$_home_id"
    PLIST_LOG_NAME="surface-daemon.log"
    if [[ "$OWNER_MODE" == "1" ]]; then
        PLIST_TEMPLATE="$TEMPLATE_DIR/engramd-owner.plist.template"
        PLIST_LABEL="com.engram.engramd-owner.$_home_id"
        PLIST_LOG_NAME="engramd-owner.log"
    fi
    if [[ ! -f "$PLIST_TEMPLATE" ]]; then
        echo "ERROR: plist template not found: $PLIST_TEMPLATE" >&2
        exit 1
    fi

    AGENTS_DIR="$HOME/Library/LaunchAgents"
    PLIST_DEST="$AGENTS_DIR/$PLIST_LABEL.plist"
    mkdir -p "$AGENTS_DIR"

    echo "Step 1: Rendering LaunchAgent plist → $PLIST_DEST"
    if [[ "$OWNER_MODE" == "1" ]]; then
        LC_ALL=C sed -e "s|{{PYTHON}}|$PYTHON_BIN|g" \
            -e "s|{{ENGRAM_OWNER_PY}}|$DAEMON_PY|g" \
            -e "s|{{ENGRAM_HOME}}|$ENGRAM_HOME|g" \
            -e "s|{{LABEL}}|$PLIST_LABEL|g" \
            "$PLIST_TEMPLATE" > "$PLIST_DEST"
    else
        LC_ALL=C sed -e "s|{{PYTHON}}|$PYTHON_BIN|g" \
            -e "s|{{ENGRAM_HOOKS_DIR}}|$HOOKS_DIR|g" \
            -e "s|{{ENGRAM_HOME}}|$ENGRAM_HOME|g" \
            -e "s|{{LABEL}}|$PLIST_LABEL|g" \
            "$PLIST_TEMPLATE" > "$PLIST_DEST"
    fi
    echo "  done."

    echo
    echo "Step 2: Loading via launchctl"
    # Modern bootstrap/bootout API (macOS 10.11+); load/unload are Apple-deprecated.
    # bootout targets the service by domain/label; bootstrap takes the plist path.
    _to_ms() {  # <value> <default-ms> -> whole ms on stdout; a non-decimal value gives the default
        local v="$1" ip fp
        if [[ ! "$v" =~ ^[0-9]+(\.[0-9]+)?$ ]]; then echo "$2"; return 0; fi
        ip="${v%%.*}"
        fp=""
        [[ "$v" == *.* ]] && fp="${v#*.}"
        fp="${fp}000"
        echo $(( 10#$ip * 1000 + 10#${fp:0:3} ))
    }
    _wait_ms="$(_to_ms "${ENGRAM_SETUP_LAUNCHD_WAIT_S:-10}" 10000)"
    _poll_ms="$(_to_ms "${ENGRAM_SETUP_LAUNCHD_POLL_S:-0.2}" 200)"
    _retry_s="${ENGRAM_SETUP_LAUNCHD_RETRY_S:-1}"
    [[ "$_retry_s" =~ ^[0-9]+(\.[0-9]+)?$ ]] || _retry_s=1
    _poll_s="${ENGRAM_SETUP_LAUNCHD_POLL_S:-0.2}"
    [[ "$_poll_s" =~ ^[0-9]+(\.[0-9]+)?$ ]] || _poll_s=0.2
    (( _poll_ms >= 1 )) || _poll_ms=1
    _max_polls=$(( _wait_ms / _poll_ms ))
    # Legacy transition (#4305): the old fixed-label job goes BEFORE the keyed one is bootstrapped, so
    # at no point do two supervised jobs run this home's daemon. Only if its ENGRAM_HOME is THIS home
    # (physical-path comparison); never bootout what is not confirmed ours. The home is read from the
    # loaded job (`launchctl print`, the same `ENGRAM_HOME =>` line the hook reads), else from the plist
    # file when no job is loaded.
    if [[ "$OWNER_MODE" != "1" ]]; then
        _legacy_name="$(_legacy_surface_label)"
        _legacy_plist="$AGENTS_DIR/$_legacy_name.plist"
        _legacy_loaded=0
        _legacy_home=""
        if _legacy_out="$(launchctl print "gui/$(id -u)/$_legacy_name" 2>/dev/null)"; then
            _legacy_loaded=1
            _legacy_home="$(printf '%s\n' "$_legacy_out" | sed -n 's/^[[:space:]]*ENGRAM_HOME => //p' | head -n 1 || true)"
        elif [[ -f "$_legacy_plist" ]]; then
            _legacy_home="$(grep -A1 '<key>ENGRAM_HOME</key>' "$_legacy_plist" | sed -n 's|.*<string>\(.*\)</string>.*|\1|p' | head -n 1 || true)"
        fi
        if [[ "$_legacy_loaded" == "1" || -f "$_legacy_plist" ]]; then
            if [[ -z "$_legacy_home" ]]; then
                echo "  Left alone: the old fixed-label job $_legacy_name: could not read its ENGRAM_HOME, so it was not removed."
            else
                _home_norm_into "$_legacy_home" _legacy_home_n
                _home_norm_into "$ENGRAM_HOME" _our_home_n
                if [[ "$_legacy_home_n" == "$_our_home_n" ]]; then
                    echo "  Removing the old fixed-label job $_legacy_name (ENGRAM_HOME=$_legacy_home is this home) before loading the keyed one"
                    launchctl bootout "gui/$(id -u)/$_legacy_name" 2>/dev/null || true
                    _li=0
                    while launchctl print "gui/$(id -u)/$_legacy_name" >/dev/null 2>&1; do
                        if (( _li >= _max_polls )); then
                            echo "ERROR: the old fixed-label job $_legacy_name is still loaded after bootout; not loading $PLIST_LABEL next to it (two daemons for one home)." >&2
                            exit 1
                        fi
                        _li=$(( _li + 1 ))
                        sleep "$_poll_s"
                    done
                    rm -f "$_legacy_plist"
                else
                    echo "  Left alone: the old fixed-label job $_legacy_name belongs to ENGRAM_HOME=$_legacy_home, not this home."
                fi
            fi
        fi
    fi
    launchctl bootout "gui/$(id -u)/$PLIST_LABEL" 2>/dev/null || true
    # bootout returns BEFORE launchd has finished removing the service (#3939): an immediate bootstrap
    # can fail "5: Input/output error". Wait until `print` fails (service gone), bounded; at the
    # deadline still go on to bootstrap. Durations are plain decimals (seconds), held as whole ms.
    _still_loaded=0
    _i=0
    while launchctl print "gui/$(id -u)/$PLIST_LABEL" >/dev/null 2>&1; do
        if (( _i >= _max_polls )); then
            _still_loaded=1
            break
        fi
        _i=$(( _i + 1 ))
        sleep "$_poll_s"
    done
    # rc 125 from bootstrap into gui/<uid> = no launchd GUI domain for this uid (no GUI login
    # session, #3780). Named, with its own exit code (3); any other failure is retried (#3939), then
    # exits with launchctl's last rc.
    # The installer keys on rc 125 alone: bootstrap's stderr for this case was not captured verbatim
    # (unlike `print`, where the parser also requires the "Domain does not support specified action"
    # text). An rc of 125 from `bootstrap gui/<uid>` is treated as no GUI domain.
    # The sentence is ONE constant: Clio's measurement of what launchd does at the next GUI login
    # may change it, and then it is a one-line edit.
    _svc_subject="The surface daemon"
    [[ "$OWNER_MODE" == "1" ]] && _svc_subject="The owner"
    _NO_GUI_DOMAIN_MSG="ERROR: launchd has no GUI domain for uid $(id -u) (no GUI login session). $PLIST_DEST was written but is not active now; whether launchd loads it at this account's next GUI login has not been measured. $_svc_subject is not supervised on this seat."
    _BOOTSTRAP_MAX=5
    _bootstrap_rc=0
    _bootstrap_err=""
    _attempt=0
    exec 4>&1   # launchctl's stdout stays on ours; only its stderr is captured (for the final message)
    while :; do
        _attempt=$(( _attempt + 1 ))
        _bootstrap_rc=0
        _bootstrap_err="$(launchctl bootstrap "gui/$(id -u)" "$PLIST_DEST" 2>&1 1>&4)" || _bootstrap_rc=$?
        if [[ "$_bootstrap_rc" == "0" ]]; then
            break
        fi
        if [[ "$_bootstrap_rc" == "125" ]]; then
            [[ -n "$_bootstrap_err" ]] && printf '%s\n' "$_bootstrap_err" >&2
            echo "$_NO_GUI_DOMAIN_MSG" >&2
            exit 3
        fi
        if (( _attempt >= _BOOTSTRAP_MAX )); then
            [[ -n "$_bootstrap_err" ]] && printf '%s\n' "$_bootstrap_err" >&2
            _loaded_note=""
            [[ "$_still_loaded" == "1" ]] && _loaded_note=" The service was still loaded when the wait for bootout ended, so launchd had not finished removing the old one."
            echo "ERROR: launchctl bootstrap of $PLIST_LABEL failed on all $_BOOTSTRAP_MAX attempts (last rc $_bootstrap_rc; last launchctl message: ${_bootstrap_err:-<none>}).$_loaded_note $PLIST_DEST was written but the service is NOT loaded. Recover by hand with:" >&2
            echo "  launchctl bootstrap gui/$(id -u) $PLIST_DEST" >&2
            exit "$_bootstrap_rc"
        fi
        sleep "$_retry_s"
    done
    exec 4>&-
    if (( _attempt > 1 )); then
        echo "  launchctl bootstrap succeeded on attempt $_attempt of $_BOOTSTRAP_MAX"
    fi
    echo "  Loaded."

    echo
    echo "=== Setup complete ==="
    echo "  Check status:  launchctl list | grep engram"
    echo "  Logs file:     $ENGRAM_HOME/$PLIST_LOG_NAME"

# ---------------------------------------------------------------------------
# Unsupported OS
# ---------------------------------------------------------------------------
else
    echo "ERROR: Unsupported OS: $OS" >&2
    echo "       This script supports Linux (systemd) and macOS (launchd)." >&2
    echo "       On other platforms, manage the daemon manually:" >&2
    echo "         $PYTHON_BIN $DAEMON_PY" >&2
    exit 1
fi
