#!/usr/bin/env bash
# start-engramd-owner.sh -- SessionStart fallback that starts the engramd OWNER when
# nothing supervises it (engram-alpha#3663, U5 of #2652; macOS arm: #3709). Linux and macOS.
#
# Gated on the owner flags. If NO `engramd.*_via_owner` flag is on in
# $ENGRAM_HOME/config.json (today's default on every seat) this hook does nothing at
# all: no process, no socket, no pidfile, no output, no log line. The flags are read
# through ONE helper, engramd_owner_flags.py (plugin root), via the plugin venv python.
#
# With at least one flag on:
#   * a live owner for this ENGRAM_HOME (engramd.pid names a positive pid that is alive and
#     whose argv contains `engramd_owner`, decided by engram_pidfile.recorded_live_pid in its
#     STRICT mode, require_marker=True: the marker is required of any live pid whatever its
#     start time, an unreadable argv counts as alive, and an unrelated live process without
#     `engramd_owner` in its argv never counts)
#                                                        -> launch nothing, say nothing
#   * engram_pidfile cannot be imported                -> launch nothing, say so (ambiguous)
#   * the supervised unit / job for THIS home is registered. The home key <id> is the first 8
#     hex of sha256 over the physical ENGRAM_HOME, derived by tools/engramd-owner-home-key.sh,
#     the one helper tools/setup-surface-daemon-service.sh --owner also uses:
#       Linux:  the systemd --user unit engramd-owner-<id>.service
#       macOS:  the launchd job com.engram.engramd-owner.<id> (`launchctl print`, asked and
#               parsed by the I/O helper under a Python-side timeout)
#                                                        -> launch nothing (running / active:
#                                                           silent; registered but not running:
#                                                           one line naming the state, and on
#                                                           macOS the last exit status)
#   * the supervisor answers "no such unit / job"        -> launch the owner detached,
#     (Linux also: `systemctl` is absent, or `systemctl --user` cannot answer -- no user bus,
#     e.g. WSL)                                             ONE line containing UNSUPERVISED
#   * macOS: launchd has NO GUI domain for this uid (`launchctl print gui/<uid>/<label>` exits
#     125 "Domain does not support specified action": no GUI login session, #3780)
#                                                        -> launch the owner detached, UNSUPERVISED,
#                                                           reason "launchd: no GUI domain" (#3823):
#                                                           a flag is on, so its readers call an
#                                                           owner; an absent one blocks them. If that
#                                                           launch fails: ONE line led by `OWNER
#                                                           REFUSED: no launchd GUI domain`, naming
#                                                           the ON flags, the recovery, and (when the
#                                                           stop flag is ON) that the stop hook blocks.
#                                                           Never "not used".
#   * the supervisor's answer is ambiguous (timeout, empty, unparseable, rc other than the
#     measured not-found one; on macOS also `launchctl` missing)
#                                                        -> launch nothing, say so
#                                                           (a missing owner is recoverable;
#                                                           a duplicate is not)
#   * the flags cannot be read at all                    -> launch nothing, say so
#   * the launch fails (cannot spawn, or the owner dies within ~1s)
#                                                        -> one line saying it could not be
#                                                           started, naming the log (on a seat with
#                                                           no GUI domain: the named refusal above)
#   * any other OS                                       -> nothing launched, one line naming the OS
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
# The physical step runs under a function-scoped LC_ALL=C and assigns by NAME: bash 3.2 mangles the
# bytes of a `$(...)` capture (inserting \001 after each byte >= 0x80) when the CAPTURING shell is
# in a multibyte locale, so a pin inside the substitution is not enough, and the installer computes
# this same path the same way (#3780). Only this function's shell is pinned, never the hook's.
# A name that is one of this function's own locals (`_pdi_*`, `LC_ALL`) is refused: return 1 and one stderr
# line (`printf -v` would assign the local and leave the caller's variable unset with rc 0; #3794).
_physical_dir_into() {
    local LC_ALL=C _pdi_dir="${1-}" _pdi_name="${2-}" _pdi_pd
    case "$_pdi_name" in
        _pdi_*|LC_ALL)
            echo "start-engramd-owner: cannot assign to the variable name '$_pdi_name': it is one of _physical_dir_into's own local names" >&2
            return 1
            ;;
    esac
    _pdi_pd="$(cd "$_pdi_dir" 2>/dev/null && pwd -P)" || return 1
    printf -v "$_pdi_name" '%s' "$_pdi_pd"
}
_physical_dir_into "$ENGRAM_HOME" ENGRAM_HOME || exit 0

# _pinned_into <varname> <cmd...>: run <cmd...> and assign its stdout (trailing newlines stripped, as `$(...)`
# does) to the variable NAMED by $1; the return status is the command's. The plugin root, the I/O helper and
# the python path are PATH values, so their captures go through here (#3794), for the reason given above:
# the capture runs INSIDE this function under its own `local LC_ALL=C`, never in the hook's own shell.
# A name that is one of this function's own locals (`_pin_*`, `LC_ALL`) is refused: return 1, one stderr line.
_pinned_into() {
    local LC_ALL=C _pin_name="${1-}" _pin_v _pin_rc=0
    case "$_pin_name" in
        _pin_*|LC_ALL)
            echo "start-engramd-owner: cannot assign to the variable name '$_pin_name': it is one of _pinned_into's own local names" >&2
            return 1
            ;;
    esac
    shift
    _pin_v="$("$@")" || _pin_rc=$?
    printf -v "$_pin_name" '%s' "$_pin_v"
    return "$_pin_rc"
}

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
    _pinned_into PYTHON_BIN command -v python3 2>/dev/null
fi

# ---- 1. do any owner flags want an owner? -----------------------------------------
_pinned_into PLUGIN_ROOT _find_plugin_root
_pinned_into IO_PY _find_io_helper
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
_os="$(uname -s 2>/dev/null)"
if [ "$_os" != "Linux" ] && [ "$_os" != "Darwin" ]; then
    _report "ENGRAM owner not launched: ${_os:-this OS} is not supported by the owner launcher; an owner flag is on. ENGRAM_HOME=$ENGRAM_HOME"
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

# ---- 4. is the supervised unit / job for THIS home registered? -----------------------
# The home key (first 8 hex of sha256 over the physical home) comes from the ONE shared helper,
# the same one tools/setup-surface-daemon-service.sh --owner names the unit / label with.
HOME_KEY_SH="$PLUGIN_ROOT/tools/engramd-owner-home-key.sh"
_home_id=""
if [ -f "$HOME_KEY_SH" ]; then
    # shellcheck source=/dev/null
    . "$HOME_KEY_SH" 2>/dev/null && _home_id="$(engramd_owner_home_key "$ENGRAM_HOME" 2>/dev/null)"
fi
if [ -z "$_home_id" ]; then
    _report "ENGRAM owner not launched (cannot compute the home key: $HOME_KEY_SH missing or failed; ambiguous). ENGRAM_HOME=$ENGRAM_HOME"
    exit 0
fi

if [ "$_os" = "Darwin" ]; then
    # ---- 4a. macOS: the launchd job com.engram.engramd-owner.<key> ----------------------
    # `launchctl print gui/<uid>/<label>` is asked and parsed by the Python I/O helper (it owns
    # the timeout: stock macOS has no GNU `timeout`). It prints one of
    #   running | registered:<state>:<last exit or none> | notfound | no_gui_domain | ambiguous:<why>
    LABEL="com.engram.engramd-owner.$_home_id"
    _state="$("$PYTHON_BIN" "$IO_PY" launchd_state "$LABEL" 2>/dev/null)"
    _sup="launchd: ${_state:-no answer}"
    case "$_state" in
        running) exit 0 ;;
        registered:*)
            _rest="${_state#registered:}"
            _report "ENGRAM owner job $LABEL is registered but ${_rest%:*} (last exit ${_rest##*:}); leaving it to launchd, no second owner launched. ENGRAM_HOME=$ENGRAM_HOME"
            exit 0
            ;;
        no_gui_domain)
            # rc 125 from the domain probe: the domain is absent (no GUI login session), so there is
            # no launchd to supervise the owner and nothing to retry (a bootstrap into an absent domain
            # cannot work). A flag is ON, so its readers WILL call an owner: the same answer as the
            # Linux no-bus / WSL path -- fall through to the unsupervised launch (#3823). If that launch
            # fails, _launch_failed prints the named refusal instead of "not used".
            _nogui=1
            _sup="launchd: no GUI domain"
            ;;
        ambiguous:*)
            _report "ENGRAM owner not launched (launchd state is ambiguous: ${_state#ambiguous:}); a duplicate is worse than a missing owner. ENGRAM_HOME=$ENGRAM_HOME"
            exit 0
            ;;
        notfound) ;;
        *)
            _report "ENGRAM owner not launched (launchd state is ambiguous: unrecognized answer). ENGRAM_HOME=$ENGRAM_HOME"
            exit 0
            ;;
    esac
else
    # ---- 4b. Linux: the systemd --user unit engramd-owner-<key>.service -----------------
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
    _sup="systemd: $_state"
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
fi

# ---- 5. unsupervised launch --------------------------------------------------------
OWNER_PY="$PLUGIN_ROOT/engramd_owner.py"

# The zombie guard is UNTESTED, and I could not make a deterministic cell for it. The
# owner is started by the Python helper (the helper exits, so the owner is reparented and its
# exit is reaped by init / launchd / a subreaper, not by this shell): a dead owner stops
# answering `kill -0` within moments, so the zombie branch is not reliably observable at the
# 0.2 s poll. It is kept as a belt; do not read it as covered. Linux reads /proc/<pid>/status;
# macOS has no /proc, so there `ps -o stat=` is asked (when ps exists; otherwise `kill -0`
# alone decides, as it did when this guard only had the /proc arm).
# When `ps -o stat=` prints NOTHING (the pid vanished between the `kill -0` and the read), the
# `case` below matches no `*Z*` and this returns ALIVE. That is the conservative direction for
# this caller: the pid is gone, so the next poll's `kill -0` says dead and the normal path
# (including the race re-check) runs one poll later, whereas answering DEAD on an empty read
# could condemn a live owner on a transient `ps` failure.
_alive() {
    kill -0 "$1" 2>/dev/null || return 1
    if [ -r "/proc/$1/status" ]; then
        grep -q '^State:[[:space:]]*Z' "/proc/$1/status" 2>/dev/null && return 1
    elif [ "$_os" = "Darwin" ] && command -v ps >/dev/null 2>&1; then
        case "$(ps -o stat= -p "$1" 2>/dev/null)" in
            *Z*) return 1 ;;
        esac
    fi
    return 0
}

# A failed owner start. On a seat with no launchd GUI domain (#3823) a flag is ON and there is no
# supervisor to fall back on, so the generic "could not be started" line is not enough: it must be the
# named refusal -- the ON flags, the recovery, and what the flagged readers will do (the stop hook
# never falls back to the store once its flag reads ON, so an absent owner blocks every stop chain).
# The words "not used" never appear here: the owner IS used, it is just absent.
# ORDER MATTERS: _engramd_owner_hook_io.py report() clips the MIDDLE of any message over 600 chars
# (_HEAD_CLIP), so the parts the contract promises -- flags, recovery, the readers' fate -- come first
# and are short, and the path-bearing detail (the failure reason, ENGRAM_HOME) comes last. No path
# appears twice and no log path is repeated: a long home must not push the promises into the middle.
_launch_failed() {
    local _names _fate
    if [ "${_nogui:-0}" -ne 1 ]; then
        _report "ENGRAM owner could not be started: $1. ENGRAM_HOME=$ENGRAM_HOME. $2"
        return
    fi
    _names="$("$PYTHON_BIN" "$PLUGIN_ROOT/engramd_owner_flags.py" "$ENGRAM_HOME" --names 2>/dev/null)"
    # What the flagged readers do is stated only for the one reader whose behaviour is verified (the stop
    # hook never falls back to the store once its flag reads ON, so it blocks every stop). A seat whose ON
    # flags do not include stop_reads_via_owner must not be told its stop hook blocks.
    case ",$_names," in
        *,stop_reads_via_owner,*) _fate="The stop hook blocks every stop until an owner answers." ;;
        *) _fate="The flagged readers have no owner until one answers." ;;
    esac
    _report "OWNER REFUSED: no launchd GUI domain. Flags ON: ${_names:-unreadable}. Fix: set them false in config.json, or log in to a GUI session. $_fate Owner launch failed: $1. ENGRAM_HOME=$ENGRAM_HOME"
}

if [ ! -f "$OWNER_PY" ]; then
    _launch_failed "$OWNER_PY not found" "See $LOG_PATH"
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
        _launch_failed "the launch failed" "See $LOG_PATH"
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
    _report "UNSUPERVISED: engramd_owner launched (pid $_pid); no supervisor reachable ($_sup). ENGRAM_HOME=$ENGRAM_HOME. $_log_note"
else
    _launch_failed "the process exited within a second" "$_log_note"
fi
exit 0
