#!/usr/bin/env bash
# start-engramd-owner.sh -- SessionStart fallback that starts the engramd OWNER when
# nothing supervises it (engram-alpha#3663, U5 of #2652). Linux only.
#
# Gated on the owner flags. If NO `engramd.*_via_owner` flag is on in
# $ENGRAM_HOME/config.json (today's default on every seat) this hook does nothing at
# all: no process, no socket, no pidfile, no output, no log line. The flags are read
# through ONE helper, engramd_owner_flags.py (plugin root), via the plugin venv python.
#
# With at least one flag on:
#   * a live owner for this ENGRAM_HOME (engramd.pid names a positive pid that is alive,
#     and /proc/<pid>/cmdline contains `engramd_owner`; the same FAMILY of rule as
#     engram_pidfile.recorded_pid_is_alive but not identical: no pidfile-mtime vs
#     process-start-time check here, and stricter on the marker -- an unrelated live
#     process without `engramd_owner` in its argv never counts)
#                                                        -> launch nothing, say nothing
#   * the supervised systemd --user unit for THIS home (engramd-owner-<id>.service,
#     <id> = first 8 hex of sha256 over the physical ENGRAM_HOME -- the same key
#     tools/setup-surface-daemon-service.sh --owner uses) is registered
#                                                        -> launch nothing (active: silent;
#                                                           registered but not active: one line)
#   * systemd answers "no such unit", `systemctl` is absent, or `systemctl --user`
#     cannot answer (no user bus, e.g. WSL)              -> launch the owner detached,
#                                                           ONE line containing UNSUPERVISED
#   * systemd's answer is ambiguous (timeout, empty)     -> launch nothing, say so
#                                                           (a missing owner is recoverable;
#                                                           a duplicate is not)
#   * the flags cannot be read at all                    -> launch nothing, say so
#   * the launch fails (cannot spawn, or the owner dies within ~1s)
#                                                        -> one line saying it could not be
#                                                           started, naming the log
#   * macOS                                              -> nothing launched, one line
#
# Always exits 0 (never breaks a session start). stdout is at most ONE bounded
# SessionStart additionalContext JSON object (the stdout audit requires JSON-or-nothing);
# the same line is appended to $ENGRAM_HOME/engramd-owner.log, which also receives the
# owner's own stdout/stderr.
#
# State-file I/O never happens in bash: every open of a file under $ENGRAM_HOME (the pidfile
# read, the log append, the launch's output redirect) goes through _engramd_owner_hook_io.py,
# which uses _hooklib's #3628 non-blocking regular-file helpers. A planted FIFO/socket/
# directory therefore cannot block this hook; a non-regular engramd.pid launches nothing
# (one loud line), a non-regular log is skipped (the launch proceeds, output discarded).
# Every report is composed through _hooklib.emit_context (trailer first, harness budget), with
# the DIAGNOSIS words before any path so a clip lands on paths; the log keeps the full line.
#
# Sibling paths resolve via $CLAUDE_PLUGIN_ROOT first, then a walk-parents search for the
# target file -- never a fixed parents[N] (hooks/claude/ in source, hooks/ when flattened).

trap 'exit 0' EXIT

: "${ENGRAM_HOME:=$HOME/.engram}"
case "$ENGRAM_HOME" in
    "~") ENGRAM_HOME="$HOME" ;;
    "~/"*) ENGRAM_HOME="$HOME/${ENGRAM_HOME#"~/"}" ;;
esac
# No home directory yet -> no config -> no flags: nothing to do, and nothing to create.
[ -d "$ENGRAM_HOME" ] || exit 0
ENGRAM_HOME="$(cd "$ENGRAM_HOME" 2>/dev/null && pwd -P)" || exit 0

LOG_PATH="$ENGRAM_HOME/engramd-owner.log"  # used only to NAME the log in messages; never opened here

# One report = one bounded JSON object on stdout + the full line appended to the log, both done
# by the Python I/O helper. Without it (or python) the line goes to stderr only: stdout stays
# JSON-or-nothing.
_report() {
    if [ -n "$PYTHON_BIN" ] && [ -n "$IO_PY" ] && [ -f "$IO_PY" ]; then
        "$PYTHON_BIN" "$IO_PY" report "$ENGRAM_HOME" "$1" 2>/dev/null
    else
        printf 'start-engramd-owner: %s\n' "$1" >&2
    fi
}

# The I/O helper is a sibling of this script (hooks/ when flattened, hooks/claude/ in source).
_find_io_helper() {
    local d f="_engramd_owner_hook_io.py"
    if [ -n "${CLAUDE_PLUGIN_ROOT:-}" ] && [ -f "$CLAUDE_PLUGIN_ROOT/hooks/$f" ]; then
        printf '%s' "$CLAUDE_PLUGIN_ROOT/hooks/$f"
        return 0
    fi
    d="$(dirname "$(readlink -f "${BASH_SOURCE[0]}" 2>/dev/null || printf '%s' "${BASH_SOURCE[0]}")")"
    while [ -n "$d" ] && [ "$d" != "/" ] && [ "$d" != "." ]; do
        if [ -f "$d/$f" ]; then
            printf '%s' "$d/$f"
            return 0
        fi
        if [ -f "$d/hooks/$f" ]; then
            printf '%s' "$d/hooks/$f"
            return 0
        fi
        d="$(dirname "$d")"
    done
    return 1
}

_find_plugin_root() {
    local d
    if [ -n "${CLAUDE_PLUGIN_ROOT:-}" ] && [ -f "$CLAUDE_PLUGIN_ROOT/engramd_owner_flags.py" ]; then
        printf '%s' "$CLAUDE_PLUGIN_ROOT"
        return 0
    fi
    d="$(dirname "$(readlink -f "${BASH_SOURCE[0]}" 2>/dev/null || printf '%s' "${BASH_SOURCE[0]}")")"
    while [ -n "$d" ] && [ "$d" != "/" ] && [ "$d" != "." ]; do
        if [ -f "$d/engramd_owner_flags.py" ]; then
            printf '%s' "$d"
            return 0
        fi
        d="$(dirname "$d")"
    done
    return 1
}

PYTHON_BIN="$HOME/.engram/venv/bin/python3"
if [ ! -x "$PYTHON_BIN" ]; then
    PYTHON_BIN="$(command -v python3 2>/dev/null)"
fi

# ---- 1. do any owner flags want an owner? -----------------------------------------
PLUGIN_ROOT="$(_find_plugin_root)"
IO_PY="$(_find_io_helper)"
if [ -z "$PLUGIN_ROOT" ] || [ -z "$PYTHON_BIN" ]; then
    _report "ENGRAM owner not launched (could not read the owner flags: engramd_owner_flags.py or python not found; ambiguous). ENGRAM_HOME=$ENGRAM_HOME"
    exit 0
fi
_flags="$("$PYTHON_BIN" "$PLUGIN_ROOT/engramd_owner_flags.py" "$ENGRAM_HOME" 2>/dev/null)"
case "$_flags" in
    off) exit 0 ;;
    on) ;;
    *)
        _report "ENGRAM owner not launched (could not read the owner flags: helper failed; ambiguous). ENGRAM_HOME=$ENGRAM_HOME"
        exit 0
        ;;
esac

# ---- 2. a flag is on: the I/O helper, then the platform ----------------------------------
if [ -z "$IO_PY" ] || [ ! -f "$IO_PY" ]; then
    printf 'start-engramd-owner: _engramd_owner_hook_io.py not found; owner not launched\n' >&2
    exit 0
fi
if [ "$(uname -s 2>/dev/null)" != "Linux" ]; then
    _report "ENGRAM owner not launched: macOS is not supported yet (permission layer, #3194); an owner flag is on. ENGRAM_HOME=$ENGRAM_HOME"
    exit 0
fi

# ---- 3. a live owner already holds this home -------------------------------------
# Prints alive | none | notregular:<kind> (the helper reads engramd.pid non-blockingly).
_live_verdict() {
    "$PYTHON_BIN" "$IO_PY" live "$ENGRAM_HOME" 2>/dev/null
}
_verdict="$(_live_verdict)"
case "$_verdict" in
    alive) exit 0 ;;
    none) ;;
    notregular:*)
        _report "ENGRAM owner not launched: engramd.pid is not a regular file (${_verdict#notregular:}); a planted special file is abnormal, launching nothing. ENGRAM_HOME=$ENGRAM_HOME"
        exit 0
        ;;
    *)
        _report "ENGRAM owner not launched (could not check engramd.pid; ambiguous). ENGRAM_HOME=$ENGRAM_HOME"
        exit 0
        ;;
esac

# ---- 4. is the supervised unit for THIS home registered? ---------------------------
if command -v sha256sum >/dev/null 2>&1; then
    _home_id="$(printf '%s' "$ENGRAM_HOME" | sha256sum | cut -c1-8)"
elif command -v shasum >/dev/null 2>&1; then
    _home_id="$(printf '%s' "$ENGRAM_HOME" | shasum -a 256 | cut -c1-8)"
else
    _report "ENGRAM owner not launched (cannot compute the unit name: no sha256sum; ambiguous). ENGRAM_HOME=$ENGRAM_HOME"
    exit 0
fi
UNIT="engramd-owner-$_home_id.service"

# Prints: none | nobus | notfound | active | registered:<ActiveState> | ambiguous:<why>
_unit_state() {
    local out rc load_state active_state
    if ! command -v systemctl >/dev/null 2>&1; then
        printf 'none'
        return
    fi
    if [ -z "${XDG_RUNTIME_DIR:-}" ] && [ -d "/run/user/$(id -u)" ]; then
        export XDG_RUNTIME_DIR="/run/user/$(id -u)"
    fi
    if command -v timeout >/dev/null 2>&1; then
        out="$(timeout 3 systemctl --user show "$UNIT" -p LoadState,ActiveState,SubState 2>&1)"
    else
        out="$(systemctl --user show "$UNIT" -p LoadState,ActiveState,SubState 2>&1)"
    fi
    rc=$?
    if [ "$rc" -eq 124 ]; then
        printf 'ambiguous:systemctl timed out'
        return
    fi
    if [ "$rc" -ne 0 ]; then
        printf 'nobus'
        return
    fi
    load_state="$(printf '%s\n' "$out" | sed -n 's/^LoadState=//p')"
    if [ -z "$load_state" ]; then
        printf 'ambiguous:systemctl answered with no LoadState'
        return
    fi
    if [ "$load_state" = "not-found" ]; then
        printf 'notfound'
        return
    fi
    active_state="$(printf '%s\n' "$out" | sed -n 's/^ActiveState=//p')"
    if [ "$active_state" = "active" ]; then
        printf 'active'
    else
        printf 'registered:%s' "${active_state:-unknown}"
    fi
}

_state="$(_unit_state)"
case "$_state" in
    active) exit 0 ;;
    registered:*)
        _report "ENGRAM owner unit $UNIT is registered but ${_state#registered:}; leaving it to systemd, no second owner launched. ENGRAM_HOME=$ENGRAM_HOME"
        exit 0
        ;;
    ambiguous:*)
        _report "ENGRAM owner not launched (systemd state is ambiguous: ${_state#ambiguous:}); a duplicate is worse than a missing owner. ENGRAM_HOME=$ENGRAM_HOME"
        exit 0
        ;;
    none | nobus | notfound) ;;
    *)
        _report "ENGRAM owner not launched (systemd state is ambiguous: unrecognized answer). ENGRAM_HOME=$ENGRAM_HOME"
        exit 0
        ;;
esac

# ---- 5. unsupervised launch --------------------------------------------------------
OWNER_PY="$PLUGIN_ROOT/engramd_owner.py"

# The `State: Z` guard is UNTESTED, and I could not make a deterministic cell for it. The
# owner is started by the Python helper (the helper exits, so the owner is reparented and its
# exit is reaped by init / a subreaper, not by this shell): a dead owner stops answering
# `kill -0` within moments, so the zombie branch is not reliably observable at the 0.2 s poll.
# It is kept as a belt; do not read it as covered.
_alive() {
    kill -0 "$1" 2>/dev/null && ! grep -q '^State:[[:space:]]*Z' "/proc/$1/status" 2>/dev/null
}

if [ ! -f "$OWNER_PY" ]; then
    _report "ENGRAM owner could not be started: $OWNER_PY not found. ENGRAM_HOME=$ENGRAM_HOME. See $LOG_PATH"
    exit 0
fi

# argv is `<python> <plugin root>/engramd_owner.py`: it carries `engramd_owner`, which
# engram_pidfile's program-identity guard looks for. The helper starts it detached (own
# session, stdin /dev/null); its output is appended to the log under ENGRAM_HOME when that is a
# regular file, else discarded. Prints `<pid> log` | `<pid> nolog` | `fail`.
_launch="$("$PYTHON_BIN" "$IO_PY" launch "$ENGRAM_HOME" "$PYTHON_BIN" "$OWNER_PY" 2>/dev/null)"
case "$_launch" in
    [0-9]*" log") _pid="${_launch%% *}"; _log_note="Log: $LOG_PATH" ;;
    [0-9]*" nolog") _pid="${_launch%% *}"; _log_note="The log is unavailable (not a regular file): owner output discarded" ;;
    *)
        _report "ENGRAM owner could not be started: the launch failed. ENGRAM_HOME=$ENGRAM_HOME. See $LOG_PATH"
        exit 0
        ;;
esac

_ok=1
for _i in 1 2 3 4 5 6; do
    sleep 0.2
    if ! _alive "$_pid"; then
        _ok=0
        break
    fi
done

# Our launch died within the window. If we LOST a race (another session's hook started
# the owner first, and ours exited 1 with OwnerAlreadyRunning), a live owner for this
# home now exists and "could not be started" would be false and teach everyone to ignore
# the one loud line. Re-check with the same rule as step 3, bounded (~1s): the winner
# writes its pidfile a moment after the loser dies.
if [ "$_ok" -eq 0 ]; then
    for _j in 1 2 3 4 5; do
        if [ "$(_live_verdict)" = "alive" ]; then
            exit 0
        fi
        sleep 0.2
    done
fi

if [ "$_ok" -eq 1 ]; then
    _report "UNSUPERVISED: engramd_owner launched (pid $_pid); no supervisor reachable (systemd: $_state). ENGRAM_HOME=$ENGRAM_HOME. $_log_note"
else
    _report "ENGRAM owner could not be started: the process exited within a second. ENGRAM_HOME=$ENGRAM_HOME. $_log_note"
fi
exit 0
