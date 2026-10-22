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
#     AND that owner SERVES, decided by the `serving` probe (#3736): the owner's own socket is
#     asked for a `ping` within the helper's named bound. Serving -> launch nothing, say nothing.
#     Alive but no healthy ping answer within that bound -> launch nothing (a second owner is
#     worse than a missing one) and ONE line saying so. The probe could not run (no importable
#     client, or the answer is unrecognized) -> nothing is claimed and nothing is launched.
#   * engram_pidfile cannot be imported                -> launch nothing, say so (ambiguous)
#   * the supervised unit / job for THIS home is registered. The home key <id> is the first 8
#     hex of sha256 over the physical ENGRAM_HOME, derived by tools/engramd-owner-home-key.sh,
#     the one helper tools/setup-surface-daemon-service.sh --owner also uses:
#       Linux:  the systemd --user unit engramd-owner-<id>.service
#       macOS:  the launchd job com.engram.engramd-owner.<id> (`launchctl print`, asked and
#               parsed by the I/O helper under a Python-side timeout)
#                                                        -> launch nothing (running / active: the
#                                                           socket is probed first (#3849), because step 3
#                                                           only probes a LIVE RECORDED owner and an up
#                                                           unit / job may have none (still before its
#                                                           pidfile write, pidfile missing or stale, or
#                                                           not an engramd_owner). Not serving AND a
#                                                           positive `none` on a liveness re-read (alive,
#                                                           notregular:* or an unreadable answer claim
#                                                           nothing) -> ONE line (never "alive": no owner
#                                                           pid is established) carrying the probe's
#                                                           observation, "no second owner was launched",
#                                                           "it may still be starting" (a budgeted claim:
#                                                           a cold start can miss the window) and the
#                                                           restart remedy "if this persists" (Linux
#                                                           `systemctl --user restart <unit>`, macOS
#                                                           `launchctl kickstart -k gui/<uid>/<label>`);
#                                                           on Linux that line replaces the NRestarts one.
#                                                           Serving, ambiguous, or an owner that wrote its
#                                                           pidfile during the probe -> as before:
#                                                           silent -- UNLESS it has restarted >= 3
#                                                           times, when ONE line names the restart
#                                                           count and the exit the record holds. #3831:
#                                                           under the unbounded restart policy a
#                                                           persistent fault reads
#                                                           `activating (auto-restart)` forever --
#                                                           a QUIETER failure than a `failed` unit --
#                                                           so the launcher must surface it. #3835:
#                                                           the line is routed by STATE first, count
#                                                           second -- a `failed` unit always gets the
#                                                           DOWN line (systemd will NOT retry it),
#                                                           an `inactive` one the `start` remedy, and
#                                                           only `activating`/`auto-restart` the loud
#                                                           "systemd keeps retrying" clause. The
#                                                           STATE axis is ActiveState, systemd's own:
#                                                           a stopped unit is `inactive`+`dead`, and
#                                                           `SubState=inactive` is NEVER emitted. macOS
#                                                           reports the last exit status, but has no
#                                                           restart counter to read)
#   * the supervisor answers "no such unit / job"        -> launch the owner detached,
#     (Linux also: `systemctl` is absent, or `systemctl --user` cannot answer -- no user bus,
#     e.g. WSL)                                             ONE line: UNSUPERVISED when it then
#                                                           serves a `ping` (#3736), the same
#                                                           line plus the not-serving state when
#                                                           it does not, its own "exited during
#                                                           the serving probe" line when it died
#                                                           inside the probe (NOT the "exited
#                                                           within a second" one: it outlived
#                                                           the watch), or the "could not be
#                                                           started" line when it died inside
#                                                           the watch
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

# A RELATIVE ENGRAM_HOME is refused (alpha #3863): it would resolve against the calling process's cwd, a
# different home for every process. Unset or empty means unset; a leading ~ is expanded against $HOME and
# re-exported (~user is not expanded: it is not absolute, so it is refused). Checked before anything else
# runs. A SessionStart hook fails open: one stderr line, nothing written, exit 0.
if [ -n "${ENGRAM_HOME-}" ]; then
    _eh_raw="$ENGRAM_HOME"
    case "$ENGRAM_HOME" in
        "~") ENGRAM_HOME="${HOME-}"; export ENGRAM_HOME ;;
        "~/"*) ENGRAM_HOME="${HOME-}/${ENGRAM_HOME#"~/"}"; export ENGRAM_HOME ;;
    esac
    case "$ENGRAM_HOME" in
        /*) ;;
        *)
            printf "engram hook: ENGRAM_HOME must be an absolute path, got '%s'; doing nothing\n" "$_eh_raw" >&2
            unset _eh_raw
            return 0 2>/dev/null || exit 0 ;;
    esac
    unset _eh_raw
fi

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

# Where a sibling runtime file may be looked for (#3983): this script's own directory, then each
# parent up to and INCLUDING its tree root, never above it. The tree root is the nearest directory
# holding engram_core.py (the installed plugin root and src/engram/ both do). Walking on to "/" would
# let a tier-filtered build that omits the file pick up a stale copy from an ancestor (a scatter-era
# ENGRAM home folder (ENGRAM_HOME) is an ancestor of the plugin root). With no engram_core.py above, only the own directory
# and its parent are offered. One directory per line, nearest first. bash 3.2-safe: no arrays, no bash 4.
_bounded_dirs() {
    local start d
    start="$(dirname "$(readlink -f "${BASH_SOURCE[0]}" 2>/dev/null || printf '%s' "${BASH_SOURCE[0]}")")"
    d="$start"
    while [ -n "$d" ] && [ "$d" != "." ]; do
        if [ -f "$d/engram_core.py" ]; then
            d="$start"
            while :; do
                printf '%s\n' "$d"
                if [ -f "$d/engram_core.py" ]; then
                    return 0
                fi
                d="$(dirname "$d")"
            done
        fi
        if [ "$d" = "/" ]; then
            break
        fi
        d="$(dirname "$d")"
    done
    printf '%s\n' "$start"
    d="$(dirname "$start")"
    if [ "$d" != "$start" ]; then
        printf '%s\n' "$d"
    fi
    return 0
}

# The I/O helper is a sibling of this script (hooks/ when flattened, hooks/claude/ in source).
_find_io_helper() {
    local d f="_engramd_owner_hook_io.py"
    if [ -n "${CLAUDE_PLUGIN_ROOT:-}" ] && [ -f "$CLAUDE_PLUGIN_ROOT/hooks/$f" ]; then
        printf '%s' "$CLAUDE_PLUGIN_ROOT/hooks/$f"
        return 0
    fi
    while IFS= read -r d; do
        if [ -f "$d/$f" ]; then
            printf '%s' "$d/$f"
            return 0
        fi
        if [ -f "$d/hooks/$f" ]; then
            printf '%s' "$d/hooks/$f"
            return 0
        fi
    done <<EOF
$(_bounded_dirs)
EOF
    return 1
}

_find_plugin_root() {
    local d
    if [ -n "${CLAUDE_PLUGIN_ROOT:-}" ] && [ -f "$CLAUDE_PLUGIN_ROOT/engramd_owner_flags.py" ]; then
        printf '%s' "$CLAUDE_PLUGIN_ROOT"
        return 0
    fi
    while IFS= read -r d; do
        if [ -f "$d/engramd_owner_flags.py" ]; then
            printf '%s' "$d"
            return 0
        fi
    done <<EOF
$(_bounded_dirs)
EOF
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
# #3736: a live pid is not a serving owner. The helper owns the bound (stock macOS has no GNU
# `timeout`): it asks the owner's own socket for a `ping` within its named window and prints
# serving | notserving:<what was observed within that window> | ambiguous:<why>. It launches
# nothing. `notserving:` is a BUDGETED claim, never "the owner does not serve".
_probe_serving() {
    "$PYTHON_BIN" "$IO_PY" serving "$ENGRAM_HOME" 2>/dev/null
}
_verdict="$(_live_verdict)"
case "$_verdict" in
    alive)
        # The pid exists, but only the socket says whether the owner SERVES (#3736). Report the
        # not-serving state ONCE and launch nothing: a second owner against a live pid is the
        # hazard this hook exists to avoid. When the probe cannot run (no importable client) it
        # answers `ambiguous:` and nothing is claimed -- today's behaviour, a live pid is left
        # alone. Liveness is re-read before reporting: a process that died while the probe ran is
        # the exited state, not the not-serving one, and must not be misdiagnosed as wedged.
        _serving="$(_probe_serving)"
        if [ "${_serving%%:*}" = "notserving" ] && [ "$(_live_verdict)" = "alive" ]; then
            _report "ENGRAM owner is alive but NOT SERVING: engramd.pid names a live engramd_owner, with ${_serving#notserving:}; no second owner was launched. ENGRAM_HOME=$ENGRAM_HOME"
        fi
        exit 0
        ;;
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

# ---- 3b. the store-maintenance lock directory is usable (#3882) -----------------------
# The owner exits 4 on a planted symlink / non-directory / foreign-owned `knowledge.db.maint.lockdir`
# and systemd does not restart 4, but launchd restarts every non-zero exit: only this refusal, before
# anything is launched, stops that loop. One lstat in the helper; it never creates the directory (the
# owner does). `ambiguous:` claims nothing and proceeds: the owner refuses by itself, closed.
_maintlock="$("$PYTHON_BIN" "$IO_PY" maintlock "$ENGRAM_HOME" 2>/dev/null)"
case "$_maintlock" in
    bad:*)
        _report "ENGRAM owner not launched: the store-maintenance lock directory $ENGRAM_HOME/knowledge.db.maint.lockdir is not a real directory owned by this user (${_maintlock#bad:}); the owner would refuse it (exit 4). Remove or fix that path, then start a session again. Launching nothing. ENGRAM_HOME=$ENGRAM_HOME"
        exit 0
        ;;
esac

# #3849: step 3 probes a LIVE RECORDED owner; the supervised arms below are reached only when no
# live owner is recorded (the owner is still before its engramd.pid write, the pidfile is missing or
# stale, or the unit's process is not an engramd_owner). When the unit / job is UP, ask the same
# probe. Returns 0 only for `notserving:` AND a POSITIVE `none` on the liveness re-read: any other
# answer (`alive` -- an owner that wrote its pidfile during the probe is step 3's, on the next fire --
# or `notregular:*`, or an error / empty read, which step 3 treats as its own ambiguous line) claims
# nothing. `serving` and `ambiguous:` return 1 too. The observation is left in _serving for the
# report line.
_supervised_not_serving() {
    _serving="$(_probe_serving)"
    [ "${_serving%%:*}" = "notserving" ] || return 1
    [ "$(_live_verdict)" = "none" ] || return 1
    return 0
}

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
        running)
            # #3849: up is not serving. Probe; report ONE line only for `notserving:` with no live
            # owner recorded. Serving / ambiguous / an owner that just wrote its pidfile: silent.
            if _supervised_not_serving; then
                _report "ENGRAM owner job $LABEL is running but no live owner is recorded in engramd.pid, and its socket did not serve within the probe window: ${_serving#notserving:}; no second owner was launched. It may still be starting; if this persists across session starts, run: launchctl kickstart -k gui/$(id -u)/$LABEL. ENGRAM_HOME=$ENGRAM_HOME"
            fi
            exit 0
            ;;
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

    # ONE `systemctl --user show` spawn per fire serves BOTH the unit's state and its restart
    # detail, so the two can never describe different moments (#3835 finding 2: round 1 asked
    # twice, and a unit that changed between the two answers was reported inconsistently).  It
    # assigns in the CALLER's shell -- never inside a subshell -- so the consumers below share
    # the one call:
    #   _show_kind = nosystemctl | timeout | nobus | ok
    #   _show_out  = the raw output (only when _show_kind = ok)
    # ExecMainCode joins the SAME list (#3835 round 5) -- one spawn, never a second (C7f).
    _SHOW_PROPS="LoadState,ActiveState,SubState,NRestarts,ExecMainStatus,ExecMainCode"
    _unit_show() {
        _show_kind=""
        _show_out=""
        _show_rc=0
        if ! command -v systemctl >/dev/null 2>&1; then
            _show_kind="nosystemctl"
            return 0
        fi
        if [ -z "${XDG_RUNTIME_DIR:-}" ] && [ -d "/run/user/$(id -u)" ]; then
            export XDG_RUNTIME_DIR="/run/user/$(id -u)"
        fi
        if command -v timeout >/dev/null 2>&1; then
            _show_out="$(timeout 3 systemctl --user show "$UNIT" -p "$_SHOW_PROPS" 2>&1)"
        else
            _show_out="$(systemctl --user show "$UNIT" -p "$_SHOW_PROPS" 2>&1)"
        fi
        _show_rc=$?
        case "$_show_rc" in
            124) _show_kind="timeout"; _show_out="" ;;
            0) _show_kind="ok" ;;
            *) _show_kind="nobus"; _show_out="" ;;
        esac
        return 0
    }

    # The record's field layout, in ONE place (#3835 finding 5): pull one `Key=value` line out of
    # the single show output. Nothing re-derives the record with `${1#* }` / `${2##* }` surgery.
    _show_field() {
        printf '%s\n' "$_show_out" | sed -n "s/^$1=//p"
    }

    # Prints: none | nobus | notfound | active | registered:<ActiveState> | ambiguous:<why>
    # A PURE parse of the one spawn: it launches nothing and does not change the return vocabulary.
    _unit_state() {
        local load_state active_state
        case "$_show_kind" in
            nosystemctl) printf 'none'; return 0 ;;
            timeout) printf 'ambiguous:systemctl timed out'; return 0 ;;
            nobus) printf 'nobus'; return 0 ;;
        esac
        load_state="$(_show_field LoadState)"
        if [ -z "$load_state" ]; then
            printf 'ambiguous:systemctl answered with no LoadState'
            return 0
        fi
        if [ "$load_state" = "not-found" ]; then
            printf 'notfound'
            return 0
        fi
        active_state="$(_show_field ActiveState)"
        if [ "$active_state" = "active" ]; then
            printf 'active'
        else
            printf 'registered:%s' "${active_state:-unknown}"
        fi
    }

    # _unit_detail: NRestarts / ActiveState / SubState / ExecMainStatus / ExecMainCode for $UNIT as
    # "<nr> <act> <subst> <main> <code>", or NOTHING on ANY failure (no systemctl, timeout,
    # non-zero rc, no user bus, a missing or unparsable key). BOTH state fields are carried (#3835
    # rounds 3-4): the launcher ROUTES on ActiveState, and -- within `activating` -- on SubState
    # too, which the loud line also names. ExecMainCode is carried for the down arms' exit-clause
    # gate (#3835 round 5, see _owner_down_line); it is NOT one of the fields whose absence empties
    # the record -- a systemctl that did not answer the key leaves it empty, and an empty code
    # reads as 0 ("no process exit recorded") below, never as a parse failure. The SAME spawn as
    # _unit_state, so the two can never disagree. It launches nothing. An empty detail degrades
    # every caller to today's message EXACTLY: a broken detail read must never be louder or more
    # confident than the read it replaced (#3831).
    _unit_detail() {
        local nr act subst main code
        [ "$_show_kind" = "ok" ] || return 0
        nr="$(_show_field NRestarts)"
        act="$(_show_field ActiveState)"
        subst="$(_show_field SubState)"
        main="$(_show_field ExecMainStatus)"
        code="$(_show_field ExecMainCode)"
        case "$nr" in '' | *[!0-9]*) return 0 ;; esac
        [ -n "$act" ] || return 0
        [ -n "$subst" ] || return 0
        case "$main" in '' | *[!0-9]*) return 0 ;; esac
        # '' (key absent) and any non-numeric value both mean the same as systemd's own 0 = NOT SET:
        # no process exit was recorded. Never a parse failure, so the state's remedy survives.
        case "$code" in '' | *[!0-9]*) code=0 ;; esac
        printf '%s %s %s %s %s' "$nr" "$act" "$subst" "$main" "$code"
    }

    # _owner_verdict <detail>: the routing decision, STATE FIRST and count second (#3835 findings
    # 1 and 4; re-keyed to ActiveState in round 3, EXTENDED to both fields in round 4). The STATE
    # axis is ActiveState -- the field systemd's own state machine is keyed on -- NOT SubState: a
    # STOPPED unit is `ActiveState=inactive` + `SubState=dead`, and `SubState=inactive` is NOT a
    # value systemd ever emits (measured on systemd 255 with a planted `Type=oneshot` unit:
    # `systemctl --user show <u> -p ActiveState -p SubState` reads `inactive`/`dead` both before it
    # has ever been started and after a clean start; only a failure reads `failed`/`failed`).
    # Round 2 keyed this on SubState, so its `inactive` arm was UNREACHABLE and a stopped unit fell
    # through to today's message, which names NO remedy -- finding 3 not actually landing. The
    # round-1 predicate was count-first -- `auto-restart OR NRestarts>=3` -- paired with a CONSTANT
    # retry clause; for a FAILED unit at NRestarts>=3 that clause denied the very failure it was
    # reporting, and it shadowed the correctly-worded DOWN line.
    #
    # Round 4: the LOUD verdict needs evidence the unit has ACTUALLY RESTARTED, so `activating` is
    # split by SubState. `activating` carries `start`, `auto-restart`, `reload`, `stop` and
    # `final-sigterm`; only `auto-restart` means systemd is WAITING to retry. Every other substate
    # is an attempt in progress, and the loud line's "has restarted" / "systemd keeps retrying" are
    # true there only once the unit has restarted (NRestarts >= 1). At `activating` + any other
    # substate + NRestarts=0 the unit has NEVER restarted and is merely starting, so the loud claims
    # would be FALSE -- "has restarted: NRestarts=0" is self-contradictory. That state degrades to
    # today's claim-nothing message: the same defect class as round 1 (a clause true of one state
    # asserted of another), recurring one level down. With this verdict the loud line's retry arm
    # is reachable only when NRestarts >= 1, so its "has restarted" is true by construction.
    # The count now decides only WITHIN `active`, and (as below) within a non-`auto-restart`
    # `activating`. Prints one of:
    #   down-failed | down-stopped | loud | silent | unknown
    # `maintenance` (a seventh ActiveState on systemd 255: `systemctl --state=help` lists active,
    # reloading, inactive, failed, activating, deactivating, maintenance) and any state a future
    # systemd adds fall to the `*)` arm below: `unknown` -> today's claim-nothing message, which
    # names no remedy. That is DELIBERATE, not an omission (#3835 round 5, S3): this launcher names
    # a remedy only for the states it has measured, and a guessed remedy is worse than none.
    _owner_verdict() {
        local nr act subst _main _code
        read -r nr act subst _main _code <<<"$1"
        case "$act" in
            failed)     printf 'down-failed' ;;
            inactive)   printf 'down-stopped' ;;
            activating)
                # `auto-restart` IS the retry wait: loud whatever the count. Any other substate is
                # loud only once the unit has restarted at least once; at 0 it never has, so the
                # loud line would lie -- degrade to today's message instead.
                if [ "$subst" = "auto-restart" ] || [ "$nr" -ge 1 ]; then
                    printf 'loud'
                else
                    printf 'unknown'
                fi
                ;;
            active)     if [ "$nr" -ge 3 ]; then printf 'loud'; else printf 'silent'; fi ;;
            *)          printf 'unknown' ;;
        esac
    }

    # The meaning of an owner exit status, from the template's own comment table. An
    # unrecognized code is named as a number and claims nothing (never guess a meaning).
    _owner_exit_meaning() {
        case "$1" in
            0) printf '%s' "clean stop" ;;
            1) printf '%s' "another owner holds this seat" ;;
            3) printf '%s' "the owner shut down on a displaced substrate" ;;
            4) printf '%s' "a planted non-regular path, or an unusable store-maintenance lock directory (fix the path, then reset-failed)" ;;
            5) printf '%s' "the store is behind this code's schema (the first migration has not run)" ;;
            6) printf '%s' "a store-maintenance tool (or another holder) holds the store; the owner retries once it has exited" ;;
            *) printf '%s' "unrecognized exit status" ;;
        esac
    }

    # The ONE loud line for a unit that has restarted: names the restart count, the SubState, the
    # last exit status and its MEANING. The retry clause is chosen by STATE, never a constant
    # (#3835 finding 1; re-keyed to ActiveState in round 3): an `activating`/`auto-restart` unit IS
    # being retried, so the clause is true there; a `running` unit restarted and came back, so it is
    # NOT, and naming a remedy would be wrong. Round 4 leaves the two-arm selection BELOW exactly as
    # it is and moves the guarantee to `_owner_verdict` instead: this function is now reached in the
    # `*)` arm only for `activating`/`auto-restart` (genuinely being retried) or for a
    # non-`auto-restart` `activating` at NRestarts >= 1 (so "has restarted" is true by construction),
    # and in the `active` arm only at NRestarts >= 3. It must not be restructured further.
    # Round 5 (#3835) left this line's exit clause UNGATED, on the reasoning that every state
    # reaching it implies an exit by construction. That is true of the HISTORY (NRestarts>=1) and
    # false of the FIELD: systemd zeroes ExecMainCode/ExecMainStatus when it spawns a new main
    # process, so a `running` unit that exited 1 three times and came back reads NRestarts=3 with
    # ExecMainCode=0 and ExecMainStatus=0 (measured on systemd 255.4; 18 long-running services on
    # this host read a large NRestarts with 0/0). The ungated clause then printed `last exit 0:
    # clean stop` -- a clean bill of health for a crash-looping owner. Round 6 (#3835, B1) gates the
    # clause on ExecMainCode, the SAME gate the DOWN arms apply, and makes it three-way on that
    # field (the siginfo `si_code`: 1 = CLD_EXITED, 2 = CLD_KILLED, 3 = CLD_DUMPED): 2 and 3 carry a
    # SIGNAL number in ExecMainStatus, not an exit status, so they name the signal and no meaning,
    # and no exit-status-specific remedy. Round 6 (B2) also stops the leading clause from naming a
    # restart count at NRestarts=0, where the FIRST failure -- `activating`/`auto-restart` waiting
    # for its first retry -- read "has restarted: NRestarts=0".
    _owner_loud_line() {
        local nr act subst main code retry remedy lead exit_clause
        read -r nr act subst main code <<<"$1"
        case "${code:-0}" in
            1)     exit_clause="last exit $main: $(_owner_exit_meaning "$main")" ;;
            2 | 3) exit_clause="killed by signal $main" ;;
            *)     exit_clause="no exit status recorded" ;;
        esac
        if [ "$nr" -eq 0 ]; then
            lead="has failed and is waiting for its first restart: NRestarts=$nr"
        else
            lead="has restarted: NRestarts=$nr"
        fi
        case "$act" in
            active)
                retry="it has restarted $nr times and is now running, so systemd is not retrying it"
                remedy=""
                ;;
            *)
                retry="under the unbounded restart policy a restartable exit never goes permanently failed, so systemd keeps retrying"
                remedy=" Remedy: fix the fault, then run: systemctl --user reset-failed $UNIT."
                ;;
        esac
        printf 'ENGRAM owner unit %s %s, SubState=%s, %s; %s.%s ENGRAM_HOME=%s' \
            "$UNIT" "$lead" "$subst" "$exit_clause" "$retry" "$remedy" "$ENGRAM_HOME"
    }

    # The line for a registered unit that is DOWN. ActiveState decides the words AND the remedy
    # (#3835; re-keyed from SubState in round 3 for the reason given at _owner_verdict): `failed` is
    # a unit systemd will NOT retry (exit 4 is in RestartPreventExitStatus; any other exit reached
    # the failed state some other way, and the remedy is to clear it and start); `inactive` is NOT a
    # failure -- it may be a manual stop OR a unit that has never been started -- so nothing needs
    # clearing and the remedy is `start` only (`reset-failed` must not be named: it is a no-op
    # there). Round 4 (nit) words the `inactive` clause so it covers BOTH: the old "it was stopped
    # rather than failed" over-stated the history for a never-started unit.
    #
    # Round 5 (#3835): the exit clause is asserted ONLY where the record HOLDS an exit. The two
    # states this function serves are exactly the two a code-0 record can reach, and each was
    # measured: 70 of 402 units read `inactive`/success with ExecMainCode=0 and ExecMainStatus=0 --
    # never started, since a real stop after a clean exit also reads success/0 (16 units) and only
    # the code separates them; and `auditd.service` reads `failed`/`exit-code` with code 0, so the
    # `*)` arm below would print `last exit 0: clean stop` for a unit systemd calls FAILED. Both
    # arms therefore print "no exit status recorded" at code 0 -- naming no status and no meaning.
    # ExecMainCode is the siginfo `si_code` of the main
    # process: 0 = not set (no process exit was recorded), 1 = CLD_EXITED, 2 = CLD_KILLED,
    # 3 = CLD_DUMPED. An empty 5th field reads as 0 (see _unit_detail), never as a parse failure.
    # Round 6 (#3835, B3): the clause is THREE-WAY on that field, not merely `!= 0`. At code 2 or 3
    # ExecMainStatus is a SIGNAL number, so reading it as an exit status printed a wrong status AND a
    # wrong remedy: SIGILL reads code 3 status 4, and the exit-4 arm below fired on signal 4 with
    # "fix the path". A wrong remedy is worse than no remedy, so 2/3 name the signal, give it no
    # meaning, and never take an exit-status-specific remedy. The loud line applies the same gate
    # (#3835 round 6, B1: see _owner_loud_line).
    _owner_down_line() {
        local act="$1" detail="$2" _nr _act _subst main code exit_clause remedy
        read -r _nr _act _subst main code <<<"$detail"
        case "${code:-0}" in
            1)     exit_clause="(last exit $main: $(_owner_exit_meaning "$main"))" ;;
            2 | 3) exit_clause="(killed by signal $main)" ;;
            *)     exit_clause="(no exit status recorded)" ;;
        esac
        case "$act" in
            inactive)
                printf 'ENGRAM owner unit %s is registered but %s %s; it is not running (stopped, or never started), so there is nothing to clear and no second owner was launched. Remedy: run: systemctl --user start %s. ENGRAM_HOME=%s' \
                    "$UNIT" "$act" "$exit_clause" "$UNIT" "$ENGRAM_HOME"
                ;;
            *)
                remedy="run: systemctl --user reset-failed $UNIT, then systemctl --user start $UNIT"
                if [ "${code:-0}" = "1" ] && [ "$main" = "4" ]; then
                    remedy="fix the path, then run: systemctl --user reset-failed $UNIT, then systemctl --user start $UNIT"
                fi
                printf 'ENGRAM owner unit %s is registered but %s %s; systemd will not retry it, so no second owner was launched. Remedy: %s. ENGRAM_HOME=%s' \
                    "$UNIT" "$act" "$exit_clause" "$remedy" "$ENGRAM_HOME"
                ;;
        esac
    }

    # Today's message, used when the detail could not be read (empty) or the state is one this
    # launcher does not recognize: it claims nothing it did not establish.
    _owner_registered_plain() {
        printf 'ENGRAM owner unit %s is registered but %s; leaving it to systemd, no second owner launched. ENGRAM_HOME=%s' \
            "$UNIT" "$1" "$ENGRAM_HOME"
    }

    _unit_show
    _state="$(_unit_state)"
    _sup="systemd: $_state"
    case "$_state" in
        active)
            # #3849: an active unit is not thereby serving. Probe first; when it does not serve and
            # no live owner is recorded, ONE line (the not-serving one, alone: it also stands in for
            # the NRestarts line) and nothing launched. Otherwise (serving, ambiguous, or an owner
            # that just wrote its pidfile) today's behaviour: silent -- UNLESS it has restarted
            # repeatedly (NRestarts >= 3), the one loud signal a healthy-looking unit gives
            # (#3831). Its line says it is running and NOT being retried (#3835). An empty detail
            # (any read failure) keeps today's silence.
            if _supervised_not_serving; then
                _report "ENGRAM owner unit $UNIT is active but no live owner is recorded in engramd.pid, and its socket did not serve within the probe window: ${_serving#notserving:}; no second owner was launched. It may still be starting; if this persists across session starts, run: systemctl --user restart $UNIT. ENGRAM_HOME=$ENGRAM_HOME"
                exit 0
            fi
            _detail="$(_unit_detail)"
            if [ -n "$_detail" ] && [ "$(_owner_verdict "$_detail")" = "loud" ]; then
                _report "$(_owner_loud_line "$_detail")"
            fi
            exit 0
            ;;
        registered:*)
            _act="${_state#registered:}"
            _detail="$(_unit_detail)"
            if [ -z "$_detail" ]; then
                # The detail could not be read: today's message EXACTLY (see _unit_detail).
                _report "$(_owner_registered_plain "$_act")"
            else
                # STATE first, count second (#3835 finding 1): a `failed` unit is a DOWN unit
                # whatever its restart count, and the loud line is reserved for a unit systemd is
                # actually retrying or one that restarted and came back.
                #
                # THE EXIT-CLAUSE CLASS (#3835 round 5), at the routing point: the DOWN arms --
                # `down-failed` and `down-stopped`, i.e. `_owner_down_line` -- are the ONLY lines
                # reachable for a unit whose record holds no process exit (measured: an `inactive`
                # unit with ExecMainCode=0 is one of 70 that never ran, and `auditd.service` is
                # `failed` with code 0), so both of those arms gate their exit clause on
                # ExecMainCode. The LOUD arm does NOT, and need not: every state that reaches it
                # implies an exit by construction (`_owner_loud_line` states the reasoning).
                case "$(_owner_verdict "$_detail")" in
                    down-failed | down-stopped) _report "$(_owner_down_line "$_act" "$_detail")" ;;
                    loud)                       _report "$(_owner_loud_line "$_detail")" ;;
                    silent) ;;
                    # `unknown`: includes `maintenance` and any state a future systemd adds --
                    # claim nothing (see _owner_verdict).
                    *)                          _report "$(_owner_registered_plain "$_act")" ;;
                esac
            fi
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
            # #3736: the winner's live pid is not a serving owner either -- the same rule as step 3,
            # and this was the last path that claimed liveness without asking the socket. A BUDGETED
            # claim ("did not serve within the probe window"), never "does not serve". Liveness is
            # re-read before reporting (a process that died during the probe is the exited state,
            # not the not-serving one), and nothing is launched: a second owner is the hazard.
            _serving="$(_probe_serving)"
            if [ "${_serving%%:*}" = "notserving" ] && [ "$(_live_verdict)" = "alive" ]; then
                _report "ENGRAM owner is alive but NOT SERVING: another launch for engramd.pid (most likely another session's hook; this launcher's own launch died inside the watch), with ${_serving#notserving:}; it did not serve within the probe window and may still be starting, and no second owner was launched. ENGRAM_HOME=$ENGRAM_HOME"
            fi
            exit 0
        fi
        sleep 0.2
    done
fi

if [ "$_ok" -eq 1 ]; then
    # The process survived the watch -- but alive is still not serving (#3736). Ask the socket,
    # then decide. Liveness is re-read before the REPORT (never before the probe): a process that
    # died while the probe ran is its own state, not this one -- a dead owner and a wedged one need
    # different remedies, so the three states must never collapse into one line.
    _serving="$(_probe_serving)"
    if ! _alive "$_pid"; then
        # Died DURING the probe: it was launched, it was alive at the end of the launch watch, and
        # it is gone at report time. Its OWN line, because "exited within a second" is false for a
        # process that outlived the ~1.2 s watch (#3736 round-2 F1) -- and because sharing the
        # exited line made the two paths indistinguishable, which is exactly what let a process
        # that died inside the WATCH (probe never reached) pass as this state.
        case "$_serving" in
            notserving:*) _probe_note="; the serving probe observed ${_serving#notserving:}" ;;
            *) _probe_note="" ;;
        esac
        _report "ENGRAM owner exited during the serving probe: engramd_owner (pid $_pid) was alive at the end of the launch watch and gone at report time${_probe_note}; no supervisor reachable ($_sup). ENGRAM_HOME=$ENGRAM_HOME. $_log_note"
    elif [ "${_serving%%:*}" = "notserving" ]; then
        _report "UNSUPERVISED: engramd_owner launched (pid $_pid) but it is NOT SERVING: ${_serving#notserving:}; no supervisor reachable ($_sup). ENGRAM_HOME=$ENGRAM_HOME. $_log_note"
    else
        _report "UNSUPERVISED: engramd_owner launched (pid $_pid); no supervisor reachable ($_sup). ENGRAM_HOME=$ENGRAM_HOME. $_log_note"
    fi
else
    _launch_failed "the process exited within a second" "$_log_note"
fi
exit 0
