#!/bin/bash
# SessionStart hook: report on the engram surface daemon's liveness, and
# (only when NO supervisor unit is registered for it) fall back to this
# hook's own historical reap+relaunch behavior.
#
# #2652 phase-0 unit 4 fold: "report-only" cannot be UNCONDITIONAL yet.
# Kepler's finding on issue #2652: today several seats on a given host have
# NO supervisor unit registered at all -- a hook that never launches would
# leave those seats with no daemon after a rebuild, until someone installs
# a unit by hand. So this hook reads FOUR states before deciding what to do:
#
#   SUPERVISED_HEALTHY  -> report only, no reap/relaunch/rm. Trust the
#                          supervisor's own restart policy.
#   SUPERVISED_FAILING  -> report LOUDLY, no launch. Never fight a real
#                          supervisor by launching a competing process.
#   UNSUPERVISED        -> launch as this hook always has (reap, the
#                          ownership-guarded rm, the launch line, the
#                          launch-attempt stamp), log that it launched
#                          because no unit is registered, and print a
#                          one-line install hint.
#   unavailable         -> (supervision state could not be determined)
#                          report LOUDLY, no launch. A missing daemon is
#                          loud and recoverable; a duplicate is the silent
#                          WAL-loss incident this epic exists to remove.
#
# This four-way branch is evaluated ONLY when the liveness probe below has
# already determined the daemon needs attention (branches 1/2/3 -- no
# pidfile, dead pid, or a live pid whose socket probe failed). Branch 4
# (pidfile present, process alive, socket responsive) is UNCHANGED: it
# logs "healthy" and exits 0 immediately, no supervision check needed
# since nothing is wrong.
#
# Supervisor-registration check: systemd --user on Linux, launchd on Darwin
# (#4261). On Darwin (`uname -s` = Darwin, `launchctl` present) the check runs
# `launchctl print gui/$(id -u)/com.engram.surface-daemon.<key>` and maps it:
#   rc 113 (no such job)                      -> not-registered (launch)
#   rc 125 (no GUI launchd domain for this uid; measured on a real seat 10-30,
#     and the installer treats it the same way)
#                                             -> ask user/$(id -u)/<label> the
#       same way: rc 0 -> the rc-0 rows below (a hand-installed user-domain
#       job is never fought, a foreign one never trusted); rc 113 or 125 ->
#       not-registered (launch); any other rc, or no answer -> unavailable
#   rc 0, job's ENGRAM_HOME is not ours or
#     not stated                              -> unavailable (loud, no launch:
#       a stale job with this label pointing at another home must never be
#       trusted as ours, and never fought)
#   rc 0, our home, top-level `state = running` -> registered-active
#   rc 0, our home, any other top-level state   -> registered-failing
#   any other rc, or no answer within 2 s     -> unavailable
# Only the job's own first-level `state =` line counts; coalition blocks
# deeper in the output carry their own `state = active` lines.
#
# #4305 / #2702: the unit / label is KEYED on ENGRAM_HOME -- engram-surface-daemon-<key>.service on
# Linux, com.engram.surface-daemon.<key> on Darwin -- <key> from the ONE helper
# tools/engramd-owner-home-key.sh (found via $CLAUDE_PLUGIN_ROOT, else a walk-parents search bounded
# at the tree root, never a fixed parents[N]). The keyed name is asked FIRST and its answer stands.
# If it is not found, the old FIXED-name unit / job (the legacy-transition code, _legacy_surface_*)
# counts as registered-for-me ONLY IF its own ENGRAM_HOME is this hook's home (physical-path
# comparison); a legacy job for ANOTHER home reads not-registered (that home has its own keyed name
# now); one whose ENGRAM_HOME cannot be read, or a call that fails, reads unavailable. If the key
# itself cannot be computed the answer is unavailable (never not-registered): no launch.
#
# On a host with neither launchctl (on Darwin) nor `systemctl`,
# `_supervisor_registered_state` below returns "not-registered"
# (reviewer-fairy suggestion 2, PR #2700 round 1) --
# structurally cannot-have-a-unit is a DEFINITE fact, not the same kind
# of uncertainty as a systemd host's bus being wedged or ambiguous, so it
# is treated the same as any Linux seat with no unit installed: launch,
# same as UNSUPERVISED today. This is narrower than "no supervisor
# support" sounds -- it only changes what happens when systemctl is
# entirely absent; a systemd host whose call fails or answers
# ambiguously still reads "unavailable" (report loudly, no launch),
# unchanged.
#
# RECOVERY -- the hook banner says the daemon is offline but a daemon process
# is alive:
#   1. `pgrep -fl engram-surface-daemon.py`, then `lsof -p <pid> | grep unix`:
#      does the live process hold a socket whose path no longer exists?
#   2. `launchctl list | grep engram` and
#      `launchctl print gui/$(id -u)/com.engram.surface-daemon.<key>`; compare
#      ProgramArguments with the real daemon path. A job pointing at a deleted
#      worktree or a temp ENGRAM_HOME is a zombie:
#      `launchctl bootout gui/$(id -u)/com.engram.surface-daemon.<key>`. (On Darwin
#      this script reads the same job through `launchctl print`: a keyed-label job
#      whose ENGRAM_HOME differs from this script's is reported "unavailable" and
#      nothing is launched; an OLD fixed-label job for another home reads
#      not-registered, since that home has its own keyed name -- see the header above.)
#   3. SIGTERM the orphaned holder: `kill <pid>`.
#   4. Run this script.
#   5. Confirm $ENGRAM_HOME/recall-daemon.sock and recall-daemon.pid exist.

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

: "${ENGRAM_HOME:=$HOME/.engram}"

# #2652 phase-0 unit 4 fold (second fold onto this unit, colleague-review
# finding on PR #2697, Ariadne): normalize ENGRAM_HOME to an absolute path
# BEFORE SOCKET_PATH/PID_PATH are built from it. #2697 fixed
# engram-surface-daemon.py's own _resolve_engram_home() to always return an
# absolute path -- but that, by itself, broke a coincidental match this
# hook used to get "for free" whenever ENGRAM_HOME was relative and both
# processes launched from the same cwd (hook side stayed relative, daemon
# side became absolute -- the two would then disagree by construction on
# the very same effective socket path). Normalizing here makes the two
# sides agree by construction instead of by cwd coincidence.
#
# mkdir -p FIRST: `cd` below would fail on a relative ENGRAM_HOME that
# doesn't exist yet (a genuinely fresh install/first run) -- without this,
# the normalization would silently no-op exactly when it matters most.
# This duplicates the later, pre-existing `mkdir -p "$ENGRAM_HOME"` in the
# not-registered/launch arm further down (kept there unchanged, next to
# the launch line it documents) -- both calls are idempotent, and this one
# runs unconditionally (every liveness-probe branch, not just the launch
# path) so PID_PATH/SOCKET_PATH are normalized even on branches that never
# reach the launch arm.
mkdir -p "$ENGRAM_HOME" 2>/dev/null || true
if _abs_engram_home="$(cd "$ENGRAM_HOME" 2>/dev/null && pwd)"; then
    ENGRAM_HOME="$_abs_engram_home"
fi
# If cd/pwd failed even after mkdir -p (e.g. a permissions problem, or
# ENGRAM_HOME naming something that isn't a directory), fall through with
# ENGRAM_HOME UNCHANGED rather than aborting -- this hook must never fail
# loudly enough to break a session start over a home-resolution edge case;
# everything downstream simply operates on whatever ENGRAM_HOME resolves
# to, same as before this fold existed.
unset _abs_engram_home

SOCKET_PATH="$ENGRAM_HOME/recall-daemon.sock"
PID_PATH="$ENGRAM_HOME/recall-daemon.pid"
DAEMON_SCRIPT="$(dirname "$0")/engram-surface-daemon.py"
LOG_PATH="$ENGRAM_HOME/surface-daemon.log"
# #4375: opening a FIFO for writing with no reader blocks forever, so a FIFO
# at the log path would hang every append below (and the detached daemon
# launch redirect). If the path exists but is not a plain regular file (or is
# a symlink), send every log write to /dev/null. A missing log stays as is.
if [ -e "$LOG_PATH" ] || [ -L "$LOG_PATH" ]; then
    if [ -L "$LOG_PATH" ] || [ ! -f "$LOG_PATH" ]; then
        LOG_PATH=/dev/null
    fi
fi

# #2652 phase-0 unit 1: log which liveness-probe branch this invocation
# took, so a restart can later be traced to "silent death" vs. "pidfile-
# not-yet-written race" vs. "live but unresponsive" vs. "healthy, no
# action" without guessing (Borges's §4 unresolved question, issue #2652).
_log_probe() {
    printf '[%s] [start-engram-daemon] probe: %s\n' \
        "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$1" >> "$LOG_PATH" 2>/dev/null || true
}

# #2652 phase-0 unit 4 fold: the four-state supervision determination's own
# log line, kept in its own namespace (mirrors _log_socket_guard's
# "socket-guard:" convention below) so a log scan can distinguish "which
# liveness-probe branch fired" from "what did the supervision check decide
# to do about it".
_log_supervision() {
    printf '[%s] [start-engram-daemon] supervision: %s\n' \
        "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$1" >> "$LOG_PATH" 2>/dev/null || true
}

# #1874: reap EVERY process matching this daemon's script name AND this
# specific ENGRAM_HOME before starting a fresh one -- not just the single
# PID recorded in PID_PATH. The prior kill-recorded-pid-only logic left
# orphaned instances running indefinitely whenever: (a) the daemon forks
# workers the pidfile never recorded (multiprocessing children outlive
# their parent -- #1874's leaked-semaphore log evidence), or (b) the
# pidfile was stale/absent (e.g. across a plugin upgrade) while an
# old-version daemon, launched from a since-replaced script path, was
# still alive and holding the socket.
#
# Scoping on basename ALONE is unsafe (colleague-review finding on this
# PR, empirically reproduced): it also matches a completely unrelated,
# legitimate daemon for a DIFFERENT ENGRAM_HOME under the same OS user --
# a second checkout, a manual debug invocation, even this file's own test
# suite running alongside a live session. ENGRAM_HOME is passed only via
# env var today, which `pgrep -f` cannot see (it matches argv/cmdline,
# not the environment) -- so the daemon is launched below with ENGRAM_HOME
# ALSO as a trailing argv (the script itself ignores it; its only purpose
# is making this scope possible). The reap pattern then matches basename
# AND this specific ENGRAM_HOME, mirroring the full-path-scoping
# discipline _hooklib.py's is_own_server_py_running already established
# for the sibling server.py liveness check.
#
# End-anchored (colleague-review fold, Kepler): ENGRAM_HOME is the LAST
# argv, so an unanchored pattern would also match a DIFFERENT, longer
# ENGRAM_HOME that merely has this one as a string prefix (e.g. this
# agent's real ENGRAM_HOME (default `~/.engram`) vs. some other checkout's `~/.engram-backup`)
# -- the shorter path is a substring of the longer one's command line.
# Named so tests/test_pgrep_liveness_reap_patterns.py can import the exact
# pattern under test instead of re-typing a copy that could drift (#2029).
#
# #2029 inventory answers: could this match the invoking process (this
# SessionStart hook itself)? No -- this script's own argv never mentions
# "engram-surface-daemon.py". Could it match a co-tenant's process on a
# shared host? Not via the pattern alone (no -u scope here), but the
# end-anchored "...${ENGRAM_HOME}\$" requires the co-tenant's process to
# have the EXACT same ENGRAM_HOME string as its last argv token -- two
# different agents' homes (or a boot-charge blob quoting this launch line
# followed by more text) will not satisfy that end anchor in practice. As
# with is_own_server_py_running above, pgrep -f is an unescaped-ERE
# substring match, so a co-tenant ENGRAM_HOME differing only at an ERE
# metachar position could in theory collide; distinct per-agent home paths
# make that realistically implausible, not structurally impossible. See
# tests/test_pgrep_liveness_reap_patterns.py for the synthetic-table proof.
#
# #2652 phase-0 unit 4 fold: this pattern is now acted on again -- but
# ONLY inside the UNSUPERVISED case arm below. Units 1-4's own comment
# ("retained as a single-sourced constant ONLY") no longer holds; this
# fold restores a real caller.
_reap_pattern="engram-surface-daemon\.py.*${ENGRAM_HOME}\$"

# #2652 phase-0 unit 4: same-seat, same-process cgroup read -- report only,
# NEVER launches or kills anything based on the result. Distinguishes a
# daemon living inside THIS session's own systemd slice (agent-session@* --
# session-coupled, dies when the session does, the exact pathology this
# epic exists to remove) from one that is not, from one whose cgroup file
# could not even be read (a dead instrument must never read as a clean
# answer -- Ariadne's pinned criteria on #2652). Scoped to what one hook
# invocation can determine about its own daemon's pid -- no cross-seat
# census, no MCP-server axis (see issue #2652's amendment 3/3a/3b/3c design
# discussion for the fleet-wide per-seat (mcp, daemon) supervision-probe
# concept; that is a separate, larger diagnostic tool, explicitly out of
# scope here). The optional second argument overrides the cgroup file path
# read (defaults to the real /proc/<pid>/cgroup) so tests can inject a
# synthetic file instead of joining a real systemd session slice.
#
# This answers a DIFFERENT question than _supervisor_registered_state
# below: this one reads a KNOWN LIVE pid's own cgroup (only meaningful
# when a live pid is already in hand -- branches 3/4); that one asks
# whether a supervisor UNIT is registered at all, independent of whether
# any process is currently alive (a registered-but-crash-looping
# supervisor has no live process for THIS function to read). Both stay in
# this file; neither replaces the other.
_cgroup_supervision_state() {
    # $1 = pid, $2 = optional cgroup-file-path override (test injection).
    # Prints one of: supervised / unsupervised / unavailable.
    local pid="$1"
    local cgroup_file="${2:-/proc/$pid/cgroup}"
    grep -q 'agent-session@' "$cgroup_file" 2>/dev/null
    local rc=$?
    if [ "$rc" -eq 0 ]; then
        echo "unsupervised"   # inside the session slice -- dies with it
    elif [ "$rc" -eq 1 ]; then
        echo "supervised"     # readable, no session-slice match
    else
        echo "unavailable"    # rc 2: file missing/unreadable -- NEVER
                               # collapse this into either of the above
    fi
}

# #4261: one bounded `launchctl print <target>` for _supervisor_registered_state
# below (Darwin only). Top level, not nested inside that function: the hooks
# stdout audit (tests/test_hooks_json_stdout_audit.py) cannot parse nested
# function bodies. Sets lc_rc (the exit status, or "timeout" if there was no
# answer within the bound) and lc_out in the CALLER's scope -- the caller
# declares them `local`, and bash's dynamic scoping makes this assignment land
# on those locals. Writes nothing to stdout (launchctl's output goes to a file).
# Called twice by the caller (gui domain, then user domain on rc 125).
_launchctl_print_bounded() {
    local lc_file lc_pid lc_i
    lc_rc=""
    lc_out=""
    lc_file="$(mktemp 2>/dev/null)" || { lc_rc="timeout"; return; }
    launchctl print "$1" >"$lc_file" 2>&1 </dev/null &
    lc_pid=$!
    lc_i=0
    while kill -0 "$lc_pid" 2>/dev/null && [ "$lc_i" -lt 20 ]; do
        sleep 0.1
        lc_i=$((lc_i + 1))
    done
    if kill -0 "$lc_pid" 2>/dev/null; then
        kill "$lc_pid" 2>/dev/null
        { wait "$lc_pid"; } 2>/dev/null
        rm -f "$lc_file"
        lc_rc="timeout"    # no answer within the bound -- never a guess
        return
    fi
    { wait "$lc_pid"; } 2>/dev/null
    lc_rc=$?
    lc_out="$(cat "$lc_file" 2>/dev/null)"
    rm -f "$lc_file"
}

# #4305: the fixed supervisor names of the pre-keyed install. They exist ONLY in these two
# functions (the legacy-transition code); tests/test_surface_label_keyed_4305.py::test_G1 fails on
# any other literal.
_legacy_surface_unit() {
    printf '%s' 'engram-surface-daemon.service'
}
_legacy_surface_label() {
    printf '%s' 'com.engram.surface-daemon'
}

# Where a sibling runtime file may be looked for (#3983): this script's own directory, then each
# parent up to and INCLUDING its tree root, never above it. The tree root is the nearest directory
# holding engram_core.py (the installed plugin root and src/engram/ both do). Walking on to "/" would
# let a tier-filtered build that omits the file pick up a stale copy from an ancestor (a scatter-era
# ENGRAM home folder is an ancestor of the plugin root). With no engram_core.py above, only the own
# directory and its parent are offered. One directory per line, nearest first. bash 3.2-safe: no
# arrays. The same function start-engramd-owner.sh carries (tests/test_no_unbounded_upward_walks_3983.py
# accepts an upward walk only inside a function of this name).
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

# #4305: print this home's 8-hex key from the ONE helper (tools/engramd-owner-home-key.sh); print
# nothing if it cannot be found or computed. The helper is looked up via $CLAUDE_PLUGIN_ROOT first,
# then in each directory _bounded_dirs offers (hooks/claude/ in source, hooks/ when flattened: never a
# fixed parents[N]), as <dir>/tools/engramd-owner-home-key.sh.
_surface_home_key() {
    local f="tools/engramd-owner-home-key.sh" d found="" key=""
    if [ -n "${CLAUDE_PLUGIN_ROOT:-}" ] && [ -f "$CLAUDE_PLUGIN_ROOT/$f" ]; then
        found="$CLAUDE_PLUGIN_ROOT/$f"
    else
        while IFS= read -r d; do
            if [ -f "$d/$f" ]; then
                found="$d/$f"
                break
            fi
        done <<EOF
$(_bounded_dirs)
EOF
    fi
    [ -n "$found" ] || return 0
    # shellcheck source=/dev/null
    . "$found" 2>/dev/null || return 0
    key="$(engramd_owner_home_key "$ENGRAM_HOME" 2>/dev/null)" || return 0
    printf '%s' "$key"
}

# #4305: 0 if the two paths are the SAME home. PHYSICAL paths are compared (/var vs /private/var, a
# trailing slash and a symlinked home are all the same home); a side that is not an existing
# directory falls back to its raw string, trailing slashes stripped. (The comparison #4261 wrote for
# the Darwin branch, now shared by both platforms and by the legacy checks.)
_same_physical_home() {
    local a_n b_n
    a_n="$(cd "$1" 2>/dev/null && pwd -P)" || a_n=""
    [ -n "$a_n" ] || { a_n="$1"; while [ "${#a_n}" -gt 1 ] && [ "${a_n%/}" != "$a_n" ]; do a_n="${a_n%/}"; done; }
    b_n="$(cd "$2" 2>/dev/null && pwd -P)" || b_n=""
    [ -n "$b_n" ] || { b_n="$2"; while [ "${#b_n}" -gt 1 ] && [ "${b_n%/}" != "$b_n" ]; do b_n="${b_n%/}"; done; }
    [ "$a_n" = "$b_n" ]
}

# #4305: the ENGRAM_HOME inside a `systemctl show -p Environment` value (space-separated
# assignments; a quoted assignment is read to its closing quote). Prints nothing if there is none.
_env_engram_home() {
    local v="$1" before rest
    case "$v" in *ENGRAM_HOME=*) ;; *) return 0 ;; esac
    before="${v%%ENGRAM_HOME=*}"
    rest="${v#*ENGRAM_HOME=}"
    case "$before" in
        *\") printf '%s' "${rest%%\"*}" ;;
        *) printf '%s' "${rest%% *}" ;;
    esac
}

# #4305: one `systemctl --user show` of <unit> (bounded by `timeout 3`, unpiped, rc checked).
# Prints unavailable | not-found | loaded:<ActiveState>:<Environment value, only if $2 = env>.
_linux_unit_probe() {
    local unit="$1" props="LoadState,ActiveState,SubState" out rc load_state active_state env_val=""
    [ "${2:-}" = "env" ] && props="$props,Environment"
    out="$(timeout 3 systemctl --user show "$unit" -p "$props" 2>&1)"
    rc=$?
    if [ "$rc" -ne 0 ]; then
        echo "unavailable"
        return
    fi
    load_state="$(printf '%s\n' "$out" | sed -n 's/^LoadState=//p')"
    if [ -z "$load_state" ]; then
        # rc=0 but no LoadState= line at all -- an ambiguous/unparseable reply shape, NOT the
        # well-formed "not-found" answer. Never collapse this into not-registered.
        echo "unavailable"
        return
    fi
    if [ "$load_state" = "not-found" ]; then
        echo "not-found"
        return
    fi
    active_state="$(printf '%s\n' "$out" | sed -n 's/^ActiveState=//p')"
    [ "${2:-}" = "env" ] && env_val="$(printf '%s\n' "$out" | sed -n 's/^Environment=//p' | head -n 1)"
    echo "loaded:${active_state}:${env_val}"
}

# #4305: one launchd job, by label. Prints not-found | unavailable | foreign | registered-active |
# registered-failing. `foreign` = a job with this label whose ENGRAM_HOME is another home. Same
# reading as #4261: gui domain, then the user domain on rc 125 (no GUI launchd domain for this uid).
# Sets lc_rc / lc_out through _launchctl_print_bounded's dynamic scope.
_darwin_job_state() {
    local label="$1" lc_rc lc_out job_home job_state uid_n
    uid_n="$(id -u)"
    _launchctl_print_bounded "gui/${uid_n}/${label}"
    if [ "$lc_rc" = "125" ]; then
        # rc 125 = this uid has NO GUI launchd domain (measured on a real seat 10-30, and the
        # installer treats rc 125 the same way). No GUI job can exist: ask the user domain.
        _launchctl_print_bounded "user/${uid_n}/${label}"
        if [ "$lc_rc" = "113" ] || [ "$lc_rc" = "125" ]; then
            echo "not-found"      # no job in either domain
            return
        fi
    elif [ "$lc_rc" = "113" ]; then
        echo "not-found"          # measured on macOS: print of a missing label -> rc 113
        return
    fi
    if [ "$lc_rc" != "0" ]; then
        echo "unavailable"        # any other rc, or no answer in time
        return
    fi
    # No stated home at all is untrustworthy: never trust it, never fight it.
    job_home="$(printf '%s\n' "$lc_out" | sed -n 's/^[[:space:]]*ENGRAM_HOME => //p' | head -n 1)"
    if [ -z "$job_home" ]; then
        echo "unavailable"
        return
    fi
    if ! _same_physical_home "$job_home" "$ENGRAM_HOME"; then
        echo "foreign"
        return
    fi
    # Only the job's own first-level line (exactly one leading tab):
    # nested coalition blocks have their own `state = active`.
    job_state="$(printf '%s\n' "$lc_out" | sed -n "s/^$(printf '\t')state = //p" | head -n 1)"
    if [ "$job_state" = "running" ]; then
        echo "registered-active"
    else
        echo "registered-failing"
    fi
}

# #2652 phase-0 unit 4 fold (Kepler's required addition, forum message
# "Unit 4 scoping: accepted + one required addition (four-state branch;
# UNSUPERVISED still launches)"): is a supervisor UNIT REGISTERED for this
# daemon at all -- independent of whether a process happens to be alive
# right now. Answers Aleph's crash-looping case: a registered-but-failing
# supervisor has NO process, so _cgroup_supervision_state above (which
# needs a live pid) cannot see it; this reads the supervisor's OWN state
# instead. systemd --user on Linux; launchd on Darwin (#4261) -- see this
# file's header comment.
_supervisor_registered_state() {
    # Prints one of: registered-active / registered-failing /
    # not-registered / unavailable. Reads LoadState/ActiveState/SubState
    # together, unpiped where the exit status is checked (per Kepler's
    # acceptance-addendum, #2652, 21:15Z), and treats a nonzero exit or
    # empty/ambiguous output as "unavailable", NEVER as "not-registered"
    # -- a dead bus and an absent unit must not read the same.
    #
    # Coder-fairy note (fold, #2652 phase-0 unit 4): the spec's own
    # illustrative snippet merged `load_state = "not-found"` and
    # `load_state` empty into a single "not-registered" branch, which
    # contradicts this same acceptance-addendum's explicit "empty/
    # ambiguous output -> unavailable, NEVER not-registered" sentence --
    # "not-found" is systemd's own well-formed, successful (rc=0) answer
    # for "no unit by this name" (the real, expected not-registered
    # signal), whereas a genuinely EMPTY LoadState (rc=0 but no
    # LoadState= line came back at all) is the ambiguous/unparseable case
    # the prose is about. Split the two below instead of merging them, so
    # "not-found" -> not-registered and empty -> unavailable, matching the
    # prose's explicit safety requirement rather than the snippet's
    # literal (and, on this reading, inconsistent) merge.
    #
    # Reviewer-fairy suggestion 2 (round 1, PR #2700): "no systemctl
    # binary at all" is a DEFINITE, unambiguous fact about this host (it
    # structurally cannot have a systemd unit), not the same kind of
    # uncertainty as a wedged/dead bus on a systemd host -- collapsing
    # both into "unavailable" (never launch) reproduces, for every
    # current or future Darwin/non-systemd seat, exactly the stranding
    # scenario the four-state design exists to prevent. Map this specific
    # sub-case to not-registered (-> UNSUPERVISED, launch, same as any
    # Linux seat with no unit installed) instead. This is narrowly scoped
    # to "the command does not exist at all" -- it does NOT touch any
    # case where systemctl exists but the call fails or answers
    # ambiguously (those remain unavailable, below, unchanged).
    # #4261: Darwin. A Mac has no systemctl, so the "no systemctl ->
    # not-registered" answer below would launch a competing daemon next to a
    # launchd job (or next to a stale job we must not trust). Ask launchd.
    # rc 125 from the gui domain = no GUI launchd domain for this uid (measured
    # on a real seat 10-30; matches the installer's own rc-125 handling), so the
    # user domain is consulted too -- refusing to launch there would mean the
    # daemon never comes back on such a seat.
    # Output and rc are captured UNPIPED (the exit status is checked, as for
    # systemctl below). macOS has no `timeout`, so the call is bounded by
    # running it in the background, polling, and killing it after 2 s; its
    # output goes to a file, so a killed or still-running launchctl cannot
    # hold this function's stdout open.
    if [ "$(uname -s 2>/dev/null)" = "Darwin" ] && command -v launchctl >/dev/null 2>&1; then
        local keyed_state legacy_state
        # #4305: no key, no answer -- unavailable, never not-registered (no launch).
        if [ -z "${_surface_key:-}" ]; then
            echo "unavailable"
            return
        fi
        keyed_state="$(_darwin_job_state "com.engram.surface-daemon.${_surface_key}")"
        case "$keyed_state" in
            not-found) ;;                              # fall through to the legacy fixed label
            foreign) echo "unavailable"; return ;;     # a keyed label with another home's job: never trust, never fight
            *) echo "$keyed_state"; return ;;          # the keyed answer stands
        esac
        legacy_state="$(_darwin_job_state "$(_legacy_surface_label)")"
        case "$legacy_state" in
            not-found|foreign) echo "not-registered" ;;   # nothing of mine (a foreign legacy job concerns only that home)
            *) echo "$legacy_state" ;;                    # mine (active/failing), or unavailable
        esac
        return
    fi
    if ! command -v systemctl >/dev/null 2>&1; then
        echo "not-registered"
        return
    fi
    local out rc load_state active_state
    # Merge-seat HOLD, Kepler (PR #2700, measured on his own seat and
    # independently reproduced on this repo's dev container): the
    # SessionStart hook's actual runtime environment has NO
    # `XDG_RUNTIME_DIR` (the session runs under a system-scope unit, not a
    # login/logind session that would export it) -- so `systemctl --user`
    # cannot find this uid's bus socket and fails "Failed to connect to
    # bus: No medium found", which the rc!=0 branch below correctly (per
    # its own logic) reads as `unavailable` -> never launch. That fires on
    # EVERY Linux seat's hook invocation, regardless of whether a
    # supervisor unit is actually registered -- reproducing, universally,
    # the exact stranding scenario the four-state design exists to
    # prevent. Measured fix (Kepler): with `XDG_RUNTIME_DIR=/run/user/$(id
    # -u)` set, the identical call answers `LoadState=not-found` (or
    # whatever the unit's real state is) instead of failing outright.
    #
    # Guarded the same way this file already guard-defaults ENGRAM_HOME
    # (`: "${ENGRAM_HOME:=...}"` at the top of this script), with one
    # addition Kepler's fold explicitly asked for: only default it when
    # the candidate directory actually exists. `[ -d
    # "/run/user/$(id -u)" ]` is checked against the DEFAULT candidate
    # path specifically (not against whatever XDG_RUNTIME_DIR might
    # already hold) -- so an already-set value is never second-guessed
    # against this existence check, and a host where this uid has no
    # `/run/user/<uid>` at all (no logind-managed runtime dir -- e.g. a
    # non-systemd host, or a systemd host where this user was never
    # logged in via logind) leaves XDG_RUNTIME_DIR untouched, falling
    # through to the same `unavailable` this whole path already produced
    # before this fix. `export` (not the bare `:=` ENGRAM_HOME uses) is
    # required here: unlike ENGRAM_HOME, this variable is read by the
    # `systemctl` child process below via its inherited environment, not
    # passed explicitly -- an unexported shell-local assignment would be
    # invisible to it.
    #
    # `id -u` is a POSIX-mandated builtin with no external dependencies
    # beyond a `/etc/passwd`-style lookup -- effectively never fails or
    # measurably slow. The one theoretical edge case considered: if `id
    # -u` somehow produced empty output, `/run/user/$(id -u)` collapses to
    # `/run/user/` (trailing slash), which -- unlike a per-uid subdirectory
    # -- commonly exists as a real, root-owned directory on any host that
    # has `/run/user/` at all (verified on this host), so the `[ -d ... ]`
    # guard would pass on a value that structurally cannot be this user's
    # actual runtime dir. Not specifically guarded against: `id -u`
    # failing outright is not something any real host in this fleet has
    # ever been observed to do, and the resulting `XDG_RUNTIME_DIR=/run/user/`
    # would itself just make the `systemctl --user` call fail a different
    # way (no bus socket at that path), landing back in the same
    # `unavailable` branch below -- not a silent wrong-answer.
    if [ -d "/run/user/$(id -u)" ]; then
        export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"
    fi
    # Reviewer-fairy suggestion 1 (round 1, PR #2700): unlike the AF_UNIX
    # connect probe above (explicit settimeout(2)), this systemctl call
    # previously had NO timeout -- a wedged/slow-to-respond --user dbus
    # (boot-time race, degraded host) would hang this ENTIRE SessionStart
    # hook rather than degrading to unavailable, a fifth, unhandled
    # failure mode. `timeout` is GNU coreutils, same tier as the
    # `stat -c '%W'` birth-time read this file's socket-guard already
    # assumes GNU-specific -- and this whole code path is already gated
    # behind `command -v systemctl` succeeding, i.e. a systemd/Linux host,
    # which ships coreutils' `timeout` as standard (present since
    # coreutils 7.0, 2009). A timeout exit (rc=124) is not special-cased
    # below: it falls into the same nonzero-exit "unavailable" branch as
    # any other failure -- it's just another way this check couldn't get
    # a clean answer, never a fifth state.
    # #4305: the KEYED unit first; its answer stands. (rc != 0, or empty / ambiguous output, is
    # "unavailable" -- NEVER "not-registered": a dead bus and an absent unit must not read the same.)
    local keyed legacy legacy_home
    if [ -z "${_surface_key:-}" ]; then
        echo "unavailable"
        return
    fi
    keyed="$(_linux_unit_probe "engram-surface-daemon-${_surface_key}.service")"
    case "$keyed" in
        unavailable) echo "unavailable"; return ;;
        not-found) ;;
        loaded:active:*) echo "registered-active"; return ;;
        loaded:*) echo "registered-failing"; return ;;
        *) echo "unavailable"; return ;;
    esac
    # Not found: the old fixed-name unit counts ONLY if its own Environment= ENGRAM_HOME is this home.
    legacy="$(_linux_unit_probe "$(_legacy_surface_unit)" env)"
    case "$legacy" in
        unavailable) echo "unavailable"; return ;;
        not-found) echo "not-registered"; return ;;
        loaded:*) ;;
        *) echo "unavailable"; return ;;
    esac
    legacy_home="$(_env_engram_home "${legacy#loaded:*:}")"
    if [ -z "$legacy_home" ]; then
        echo "unavailable"          # a legacy unit whose home cannot be read: never not-registered
        return
    fi
    if ! _same_physical_home "$legacy_home" "$ENGRAM_HOME"; then
        echo "not-registered"      # another home's legacy unit concerns only that home
        return
    fi
    case "$legacy" in
        loaded:active:*) echo "registered-active" ;;
        *) echo "registered-failing" ;;
    esac
}

# Check daemon liveness and report. Branch 4 (healthy) remains a true
# no-op, unchanged by this fold: no supervision check needed since
# nothing here found anything wrong.
if [ -f "$PID_PATH" ]; then
    pid=$(cat "$PID_PATH")
    if kill -0 "$pid" 2>/dev/null; then
        # Daemon is running. #2652 phase-0 unit 4: read this pid's cgroup
        # supervision state whenever a live pid is in hand -- regardless of
        # what the socket probe below finds.
        _cgroup_state="$(_cgroup_supervision_state "$pid")"

        # Verify socket is responsive. #2676: capture the heredoc's stderr
        # (the caught exception's class name) via command substitution
        # instead of discarding it to /dev/null, so the failure log line can
        # name WHICH of three distinct populations fired -- FileNotFoundError
        # (healthy daemon still in its warmup window, before bind()/listen()
        # -- see engram-surface-daemon.py's main(): pidfile write, then
        # server.bind(), then server.listen()), ConnectionRefusedError (the
        # orphan: a dead/unlinked socket -- the actual pathology #2652 exists
        # to fix), or BlockingIOError (a live, listening daemon whose accept
        # backlog is full). socket.timeout is measured unreachable on an
        # AF_UNIX connect() -- every failure returns immediately -- so no
        # refused-vs-timed-out split is built; the settimeout(2) call stays
        # as defensive code regardless. Restructured from a direct
        # `if <cmd>; then` into a capture-then-check-$? form so the same
        # exit-code semantics still gate the branch.
        _probe_output="$({{PYTHON}} -c "
import socket, sys
s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
s.settimeout(2)
try:
    s.connect('$SOCKET_PATH')
    s.close()
    sys.exit(0)
except Exception as e:
    print(type(e).__name__, file=sys.stderr)
    sys.exit(1)
" 2>&1)"
        _probe_rc=$?
        if [ "$_probe_rc" -eq 0 ]; then
            # Daemon is alive and socket works. Unit-4-fold: this remains
            # a true early exit -- no supervision check, no further action.
            _log_probe "healthy (pid=${pid}), no action, cgroup: ${_cgroup_state}"
            exit 0
        else
            _log_probe "pidfile present (pid=${pid}), process alive, socket probe failed: ${_probe_output:-unknown}, cgroup: ${_cgroup_state}"
        fi
    else
        _log_probe "pidfile present (pid=${pid}) but process not running"
    fi
else
    _log_probe "no pidfile at ${PID_PATH}"
fi

# Reaching here means branches 1/2/3 fired -- the liveness probe decided
# this daemon needs attention. #2652 phase-0 unit 4 fold: before doing
# anything about that, determine whether a supervisor unit is registered
# for it, and branch four ways.
# #4305: the supervisor name is KEYED on this home. _supervisor_registered_state reads _surface_key
# (empty = the key could not be computed -> unavailable); the log lines name the keyed unit / label.
_surface_key="$(_surface_home_key)"
if [ "$(uname -s 2>/dev/null)" = "Darwin" ]; then
    _surface_sup_name="com.engram.surface-daemon.${_surface_key:-<no-key>}"
else
    _surface_sup_name="engram-surface-daemon-${_surface_key:-<no-key>}.service"
fi
_supervisor_state="$(_supervisor_registered_state)"

# Reviewer-fairy suggestion 2 (round 1, PR #2700): _supervisor_registered_state
# now maps TWO distinct causes onto the same "not-registered" return value --
# a genuinely-reachable systemctl reporting LoadState=not-found, and
# systemctl not existing on this host at all (Darwin, or any non-systemd
# host). Both correctly launch (see the function's own comment for why),
# but a future reader tracing a log should be able to tell them apart
# without re-deriving it from the daemon's absence -- so the log line
# below names which one fired, via this same cheap, deterministic check
# (not a second call to _supervisor_registered_state itself, whose return
# value already decided the case branch).
if command -v systemctl >/dev/null 2>&1; then
    _systemctl_present=1
else
    _systemctl_present=0
fi

case "$_supervisor_state" in
    registered-active)
        _log_supervision "SUPERVISED_HEALTHY -- ${_surface_sup_name} (or the old fixed-name job for this home) is registered and active; trusting the supervisor's own restart policy, no reap/relaunch/rm"
        ;;
    registered-failing)
        _log_supervision "SUPERVISED_FAILING -- ${_surface_sup_name} (or the old fixed-name job for this home) is registered but NOT active; refusing to launch a competing process (never fight a real supervisor). Investigate: systemctl --user status ${_surface_sup_name} (Linux) / launchctl print gui/$(id -u)/${_surface_sup_name} (macOS)"
        ;;
    not-registered)
        if [ "$_systemctl_present" -eq 1 ]; then
            _log_supervision "UNSUPERVISED -- no supervisor unit is registered for ${_surface_sup_name} (nor an old fixed-name job for this home); running the reap+launch sequence directly, as before. Install a supervisor unit: tools/setup-surface-daemon-service.sh"
        else
            _log_supervision "UNSUPERVISED (no systemctl on this host -- treating as unsupervised, launching) -- this host cannot have a systemd unit at all (a non-systemd host, or a Darwin host where launchctl found no job); running the reap+launch sequence directly, as before."
        fi

        # #2652 phase-0 unit 1 (fold onto unit 1, colleague-review finding,
        # Ariadne): branch 1's "no pidfile" line above collapses three
        # distinct cases -- a genuine cold start, the pidfile-not-yet-written
        # warmup race, and a start that lands after THIS OR A PRIOR
        # invocation already reaped a daemon and removed the pidfile.
        # Recording whether this invocation's reap loop actually
        # matched/killed anything doesn't fully disambiguate all three (the
        # race and a genuine cold start both reap zero), but it separates
        # "something was killed just now" from "nothing was killed" -- the
        # discriminating datum this unit exists to produce. Capture the
        # pgrep output ONCE into a variable (rather than calling pgrep
        # twice) so the log line and the kill loop can never disagree about
        # what was matched, and so a live race between two separate pgrep
        # calls can't produce a different process set for each.
        _stale_pids="$(pgrep -f "$_reap_pattern" 2>/dev/null)"
        if [ -n "$_stale_pids" ]; then
            _stale_count="$(printf '%s\n' "$_stale_pids" | grep -c .)"
            _log_probe "reap matched ${_stale_count} stale process(es): $(printf '%s' "$_stale_pids" | tr '\n' ' ')"
        else
            _log_probe "reap matched none"
        fi

        # #2652 phase-0 unit 2: never `rm -f` a socket this run has not
        # established it owns (issue #2652, Borges's "Unit 2" design intent
        # + Sol's own correction comment on the same issue -- the
        # discriminator below is the CORRECTED mechanism; an earlier literal
        # recipe that cross-referenced stat()'s filesystem-dentry inode
        # directly against /proc/net/unix's Inode column was measured to
        # never match -- those are disjoint kernel inode namespaces
        # (filesystem dentry vs. sockfs) that structurally never coincide).
        # The live incident this stops: a reap pattern scoped to THIS
        # hook's own launch argv (see _reap_pattern above) cannot match a
        # daemon started by a different launch surface (e.g. a
        # systemd/launchd supervisor with its own argv) -- so `_stale_pids`
        # reads empty even though the daemon at $SOCKET_PATH is alive,
        # correctly functioning, and unrelated to anything this run just
        # reaped. Unconditionally deleting $SOCKET_PATH in that case
        # clobbers a live process's socket out from under it. The guard
        # below splits into two independently-answerable questions, run in
        # order:
        #
        #   (A) RACE DETECTION -- has the on-disk special file's own
        #       identity (device+inode+birth-time, pure stat(), no
        #       /proc/net/unix involved) changed between a snapshot taken
        #       now (before the kill loop) and a second snapshot taken
        #       immediately before the delete would happen? A mismatch
        #       means some other process (re)bound a fresh socket at this
        #       path in the window -- refuse unconditionally, do not
        #       proceed to (B).
        #   (B) LIVE-HOLDER IDENTIFICATION -- once (A) confirms no race,
        #       correlate /proc/net/unix's PATH-TEXT column (never its
        #       Inode column, which is a sockfs number -- see above) to a
        #       pid via /proc/<pid>/fd/*, and check whether every such pid
        #       is one this run's kill loop just killed (or the socket is
        #       unheld by anyone -- a plain orphan). Any live holder NOT in
        #       this run's killed-pid set is UNKNOWN OWNER.
        #
        # On UNKNOWN OWNER (either question), the hook keeps the socket AND
        # the pidfile, does NOT launch a daemon, does NOT stamp
        # daemon-launch-attempt, logs one loud `socket-guard:` line to
        # $LOG_PATH, and exits 0 (C8, below). It does not launch because the
        # daemon's own startup gate (engram-surface-daemon.py: socket
        # exists and the recorded pid is not alive -> unlink) would steal
        # the socket from the live holder this guard just declined to
        # touch (tracked as #3361). A missing daemon is loud and
        # recoverable; a duplicate risks silent WAL loss.
        #
        # $PID_PATH IS also gated by this SAME guard verdict (fold onto
        # this unit, reviewer-fairy finding on PR #2687): removing it would
        # leave the live, unmatched holder with no recorded owner. It is
        # removed only when $_allow_socket_rm == 1 (an orphan, or a pid this
        # run's own kill loop just killed), which keeps both files'
        # lifecycles coupled to the same ownership determination. A
        # stale-but-present pidfile pointing at a dead pid is NOT harmless
        # on UNKNOWN OWNER: that daemon startup gate reads the dead pid as
        # "not alive" and unlinks the socket, so on UNKNOWN OWNER no daemon
        # is launched at all (C8) rather than relying on that gate to back
        # off.
        _stat_identity() {
            # dev:inode:birthtime, or empty (nonzero exit) if the path
            # doesn't exist. Birth-time (%W) is GNU-coreutils-specific
            # (statx-backed); reads as "0" when the filesystem doesn't
            # expose it, which is harmless here -- a constant field can
            # never itself manufacture a mismatch between two calls, it
            # just adds no discriminating value when unsupported.
            #
            # Portable (#3333 follow-up): GNU `stat -c` first; where that
            # yields nothing (BSD/macOS stat has no -c: "illegal option",
            # rc 1) fall back to BSD `stat -f '%d:%i'` (dev:inode; no birth
            # time, still a fine race discriminator -- a rebound socket gets
            # a new inode). The fallback is tried only after -c has already
            # produced nothing, and a path that does not exist fails both,
            # so GNU hosts never see a -f (filesystem-status) reading. If
            # BOTH yield nothing the output is empty -- which the caller must
            # NOT conflate with "nothing on disk"; it re-checks the path.
            _si_out="$(stat -c '%d:%i:%W' "$1" 2>/dev/null)"
            if [ -z "$_si_out" ]; then
                _si_out="$(stat -f '%d:%i' "$1" 2>/dev/null)"
                # GNU `stat -f '%d:%i'` is a filesystem-status query that
                # prints multi-line info (rc 0): accept the fallback only
                # if it is exactly dev:inode, else treat it as unreadable
                # (the caller's C1 check then refuses).
                case "$_si_out" in
                    *[!0-9:]*|*:*:*|:*|*:|"") _si_out="" ;;
                    *:*) ;;
                    *) _si_out="" ;;
                esac
            fi
            printf '%s' "$_si_out"
        }
        _log_socket_guard() {
            printf '[%s] [start-engram-daemon] socket-guard: %s\n' \
                "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$(printf '%s' "$1" | tr '\r\n' '  ')" >> "$LOG_PATH" 2>/dev/null || true
        }
        # Holder text (a process cmdline: may be multi-line, may be huge) is
        # normalized to single-spaced text and hard-capped BEFORE it is
        # composed into the sentence, so the template's restore instruction
        # is never cut. (The embedded python already caps the cmdline itself
        # at 300 chars; this 1000-char cap is the backstop for the rest of
        # its output.) The line-flatten above is the chokepoint for every
        # reason. bash 3.2-safe.
        _flatten_holder_text() {
            _fh="$(printf '%s' "$1" | tr '[:cntrl:]' ' ' | tr -s ' ')"
            if [ "${#_fh}" -gt 1000 ]; then
                _fh="${_fh:0:1000}..."
            fi
            printf '%s' "$_fh"
        }
        _socket_pre_stat="$(_stat_identity "$SOCKET_PATH")"

        for stale_pid in $_stale_pids; do
            kill "$stale_pid" 2>/dev/null || true
        done

        _socket_post_stat="$(_stat_identity "$SOCKET_PATH")"
        _allow_socket_rm=1
        _launch_unknown=0
        _refusal_reason=""
        if [ -z "$_socket_post_stat" ] && { [ -e "$SOCKET_PATH" ] || [ -L "$SOCKET_PATH" ]; }; then
            # (C1) the path IS on disk but no stat dialect could read its
            # identity. An empty snapshot must never be read as "nothing on
            # disk" (that is how this guard failed OPEN on macOS, where
            # `stat -c` does not exist) -- fail closed: keep both files.
            _allow_socket_rm=0
            _refusal_reason="it exists but its on-disk identity could not be read (no usable stat), so ownership cannot be established"
        elif [ -z "$_socket_post_stat" ]; then
            : # nothing on disk any more -- rm -f would be a no-op either
              # way, nothing here for the guard to protect.
        elif [ "$_socket_pre_stat" != "$_socket_post_stat" ]; then
            # (A) race: the file's own identity changed since the first
            # observation. Sufficient on its own to refuse -- do not
            # proceed to (B); a race with no live holder yet (the new
            # socket hasn't started listening) is still correctly "don't
            # delete".
            _allow_socket_rm=0
            _refusal_reason="on-disk identity changed since first observation (race: pre=[${_socket_pre_stat:-none}] post=[${_socket_post_stat}]), another process may have just (re)bound this path"
        else
            # (B) live-holder identification. Path-TEXT match against
            # /proc/net/unix (never its Inode column against stat() -- see
            # above), then /proc/<pid>/fd/* to resolve EVERY pid that holds
            # it (not just the first). Removal is authorized only if each
            # holder is a pid this run's kill loop just killed AND has
            # exited; any other holder refuses.
            _holder_output="$({{PYTHON}} -c "
import os, sys

SOCKET_PATH = '$SOCKET_PATH'
killed_pids = set(sys.argv[1:])

def pid_holds_inode(pid, inode):
    fd_dir = '/proc/' + pid + '/fd'
    target = 'socket:[' + inode + ']'
    try:
        entries = os.listdir(fd_dir)
    except OSError:
        return False
    for fd in entries:
        try:
            link = os.readlink(fd_dir + '/' + fd)
        except OSError:
            continue
        if link == target:
            return True
    return False

def clean_cmd(text):
    # Single-spaced, capped at 300 chars: a cmdline can be multi-line or huge,
    # and the message that embeds it must stay one bounded log line.
    text = ' '.join(text.split())
    return text[:300] + '...' if len(text) > 300 else text

def exited_within(pid, seconds=3.0):
    # Signaled is not killed: a holder may ignore SIGTERM. True only once the
    # pid has actually exited (poll up to ~3s).
    import time
    for _ in range(int(seconds * 10)):
        try:
            os.kill(int(pid), 0)
        except ProcessLookupError:
            return True
        except OSError:
            pass  # e.g. EPERM: it exists
        time.sleep(0.1)
    return False

def read_cmdline(pid):
    try:
        with open('/proc/' + pid + '/cmdline', 'rb') as f:
            raw = f.read()
    except OSError:
        return '(cmdline unavailable)'
    parts = [p.decode(errors='replace') for p in raw.split(b'\0') if p]
    return clean_cmd(' '.join(parts)) if parts else '(empty cmdline)'

def wait_all_exited(pids, seconds=3.0):
    # One shared budget (not per pid) for every reap-killed pid to exit.
    # Returns the set of pids still answering kill(pid, 0) when it runs out.
    import time
    deadline = time.time() + seconds
    alive = set(pids)
    while True:
        for pid in list(alive):
            try:
                os.kill(int(pid), 0)
            except ProcessLookupError:
                alive.discard(pid)
            except (OSError, ValueError):
                pass  # e.g. EPERM: it exists
        if not alive or time.time() >= deadline:
            return alive
        time.sleep(0.1)

def find_holders(inode):
    # EVERY pid holding the inode, not the first one found: a listening
    # socket fd can be inherited by an exec'd child whose argv the reap
    # pattern does not match, so proving one holder exited proves nothing
    # about the others. Returns None when /proc cannot be listed.
    try:
        all_pids = [p for p in os.listdir('/proc') if p.isdigit()]
    except OSError:
        return None
    return [pid for pid in sorted(all_pids, key=int) if pid_holds_inode(pid, inode)]

def lsof_main():
    # No /proc/net/unix (macOS, BSD): ask lsof who holds the path. rc 0 +
    # pids = holders (each must be a reap-killed pid that has exited); every
    # other outcome, rc 1 included, or no lsof at all, fails CLOSED.
    import shutil, subprocess
    if shutil.which('lsof') is None:
        print('this host has no /proc/net/unix and no lsof, so the live holder of the socket cannot be determined -- refusing to delete without a live-holder check')
        sys.exit(1)
    try:
        r = subprocess.run(['lsof', '-t', '--', SOCKET_PATH], capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.SubprocessError) as exc:
        print('the guard could not run lsof (' + str(exc) + ') -- refusing to delete without a live-holder check')
        sys.exit(1)
    pids = [p for p in r.stdout.split() if p.isdigit()]
    if r.returncode not in (0, 1) or (r.returncode == 1 and r.stdout.strip()):
        print('lsof exited ' + str(r.returncode) + ' -- refusing to delete without a live-holder check')
        sys.exit(1)
    if r.returncode == 0 and not pids:
        print('lsof exited 0 with no holder pid -- refusing to delete without a live-holder check')
        sys.exit(1)
    if r.returncode == 1:
        # rc 1 is UNKNOWN, never "orphan": lsof also exits 1 on a failed or
        # incomplete search (a holder it cannot inspect), -t can hide the
        # warning, and no independent probe can prove absence. So this
        # branch deletes nothing. It exits 2 (#4374), a code distinct from
        # every refusal: UNKNOWN, delete nothing but launch. The shell
        # then launches the daemon, whose lifetime-lock gate decides: a
        # live daemon holds the lock (the new one exits 0), a dead one's
        # lock is gone (the stale socket is proven unheld and removed), and
        # a non-daemon holder lsof could not see is still caught by the
        # daemon's own HELD check where it can be seen (#3361).
        print('lsof exited 1 (no holder reported, but that is not proof of absence: its search may have been incomplete) -- deleting nothing')
        sys.exit(2)
    for pid in pids:
        if pid in killed_pids:
            # Exempt only once it has actually exited.
            if exited_within(pid):
                continue
            print('holder pid ' + pid + ' was signaled but is still alive')
            sys.exit(1)
        try:
            cmd = clean_cmd(subprocess.run(['ps', '-ww', '-o', 'command=', '-p', pid], capture_output=True, text=True, timeout=5).stdout)
        except (OSError, subprocess.SubprocessError):
            cmd = ''
        print('held by pid ' + pid + ' (' + (cmd or '(cmdline unavailable)') + '), not matched by the reap loop for this invocation')
        sys.exit(1)
    sys.exit(0)

if not os.path.exists('/proc/net/unix'):
    lsof_main()

# /proc branch: removal is authorized only if EVERY process holding EVERY
# matching inode is a reap-killed pid that has exited. First give all killed
# pids one shared ~3s budget to exit (signaled is not killed, #3360), THEN
# read /proc/net/unix: whatever still holds the path after that is, by
# definition, either an unmatched process or a killed pid still alive.
still_alive = wait_all_exited(killed_pids)

try:
    with open('/proc/net/unix') as f:
        lines = f.readlines()
except OSError as exc:
    print('the guard could not read /proc/net/unix (' + str(exc) + ') -- refusing to delete without a live-holder check')
    sys.exit(1)

inodes = []
for line in lines[1:]:
    parts = line.split()
    if len(parts) >= 8 and parts[-1] == SOCKET_PATH:
        inodes.append(parts[6])

if not inodes:
    sys.exit(0)

for inode in inodes:
    holders = find_holders(inode)
    if not holders:
        print('held by an unresolved process (sockfs inode=' + inode + '; holder pid not determinable -- permission boundary or process already exited)')
        sys.exit(1)
    # Every holder is judged, none is skipped: an unmatched holder refuses
    # outright; a reap-killed holder must have exited (#3360).
    for holder_pid in holders:
        if holder_pid not in killed_pids:
            cmdline = read_cmdline(holder_pid)
            print('(inode=' + inode + ') held by pid ' + holder_pid + ' (' + cmdline + '), not matched by the reap loop for this invocation')
            sys.exit(1)
    for holder_pid in holders:
        if holder_pid in still_alive or not exited_within(holder_pid, 0.3):
            print('holder pid ' + holder_pid + ' was signaled but is still alive')
            sys.exit(1)

sys.exit(0)
" $_stale_pids)"
            _holder_rc=$?
            if [ "$_holder_rc" -eq 2 ]; then
                # (#4374 D5) lsof rc 1: UNKNOWN. Delete nothing, but launch:
                # the daemon's lifetime-lock gate decides.
                _allow_socket_rm=0
                _launch_unknown=1
                _log_socket_guard "NOT removing ${SOCKET_PATH} (lsof exited 1: no holder reported, which is not proof of absence); launching the daemon so its lifetime-lock gate decides"
            elif [ "$_holder_rc" -ne 0 ]; then
                _allow_socket_rm=0
                _refusal_reason="$(_flatten_holder_text "${_holder_output:-holder check exited rc=${_holder_rc} with no output}")"
            fi
        fi

        if [ "$_allow_socket_rm" -eq 1 ]; then
            rm -f "$PID_PATH" "$SOCKET_PATH"
        else
            : # UNKNOWN OWNER: leave both files untouched (the C8 block
              # below then refuses to launch). Removing PID_PATH would
              # also discard the recorded owner of a live holder. See PR
              # #2687's reviewer findings for the original reproduction.
        fi

        if [ "$_allow_socket_rm" -ne 1 ] && [ "$_launch_unknown" -ne 1 ]; then
            # (C8) UNKNOWN OWNER, any branch: do NOT launch and do NOT
            # stamp a launch attempt. A launched daemon's own startup gate
            # (engram-surface-daemon.py: socket exists and recorded pid not
            # alive -> unlink) would steal the socket from the live holder.
            # A missing daemon is loud and recoverable; a duplicate risks
            # silent WAL loss. Exactly one loud line, naming the holder
            # (when known) and the restore path.
            _log_socket_guard "refusing to remove ${SOCKET_PATH} and NOT launching a daemon -- ${_refusal_reason}. A missing daemon is loud and recoverable; a duplicate risks silent WAL loss. To restore: stop that process, or a human can clear it by hand with clear-stale-engram-daemon.sh --yes (human-invoked only), then re-run the hook"
        else
            # Start daemon in background. The trailing $ENGRAM_HOME argv is
            # unused by the script (ENGRAM_HOME is still resolved from the env
            # var as before) -- it exists solely so pgrep -f above can scope on
            # it.
            mkdir -p "$ENGRAM_HOME"
            env ENGRAM_HOME="$ENGRAM_HOME" {{PYTHON}} "$DAEMON_SCRIPT" "$ENGRAM_HOME" >> "$LOG_PATH" 2>&1 < /dev/null &

            # Fire-and-forget: the daemon warms asynchronously (cold model-load
            # routinely exceeds any fixed wait); the per-turn UserPromptSubmit
            # surface hook is the liveness check and emits a CRITICAL warning
            # only on a genuine daemon-down. No blocking wait here -> no
            # cold-start-race false alarm.

            # Stamp the launch attempt so the per-turn surface hook can tell a
            # cold-start warmup (recent stamp → SOFT "warming up") from a
            # genuinely down daemon (stale/absent stamp → CRITICAL).
            # #4375: write the stamp only to an absent path or a regular
            # file; a FIFO would block forever and a symlink would be written
            # through. Otherwise skip silently.
            if [ ! -L "$ENGRAM_HOME/daemon-launch-attempt" ] && \
               { [ ! -e "$ENGRAM_HOME/daemon-launch-attempt" ] || [ -f "$ENGRAM_HOME/daemon-launch-attempt" ]; }; then
                date +%s > "$ENGRAM_HOME/daemon-launch-attempt" 2>/dev/null || true
            fi
        fi
        ;;
    *)
        # unavailable (or any other value this function should never
        # produce): could not determine supervisor registration state.
        # Per Kepler: "a missing daemon is loud and recoverable, a
        # duplicate was the silent WAL loss" -- when in doubt, do not risk
        # creating a second holder.
        _log_supervision "unavailable -- could not determine whether a supervisor unit is registered for ${_surface_sup_name}; refusing to launch (a missing daemon is loud and recoverable; a duplicate risks silent WAL loss)"
        ;;
esac

exit 0
