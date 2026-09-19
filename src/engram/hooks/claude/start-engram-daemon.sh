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
# Supervisor-registration check is Linux-only (systemd --user) for this
# fold. A Darwin equivalent (`launchctl print gui/$(id -u)/...`) is a
# named follow-up, not implemented here. On a host with no `systemctl` at
# all (including Darwin), `_supervisor_registered_state` below returns
# "not-registered" (reviewer-fairy suggestion 2, PR #2700 round 1) --
# structurally cannot-have-a-unit is a DEFINITE fact, not the same kind
# of uncertainty as a systemd host's bus being wedged or ambiguous, so it
# is treated the same as any Linux seat with no unit installed: launch,
# same as UNSUPERVISED today. This is narrower than "no launchctl
# support" sounds -- it only changes what happens when systemctl is
# entirely absent; a systemd host whose call fails or answers
# ambiguously still reads "unavailable" (report loudly, no launch),
# unchanged.

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
# agent's real `~/.engram` vs. some other checkout's `~/.engram-backup`)
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

# #2652 phase-0 unit 4 fold (Kepler's required addition, forum message
# "Unit 4 scoping: accepted + one required addition (four-state branch;
# UNSUPERVISED still launches)"): is a supervisor UNIT REGISTERED for this
# daemon at all -- independent of whether a process happens to be alive
# right now. Answers Aleph's crash-looping case: a registered-but-failing
# supervisor has NO process, so _cgroup_supervision_state above (which
# needs a live pid) cannot see it; this reads the supervisor's OWN state
# instead. Linux-only (systemd --user) -- see this file's header comment
# for the Darwin scope note.
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
    out="$(timeout 3 systemctl --user show engram-surface-daemon.service -p LoadState,ActiveState,SubState 2>&1)"
    rc=$?
    if [ "$rc" -ne 0 ]; then
        echo "unavailable"
        return
    fi
    load_state="$(printf '%s\n' "$out" | sed -n 's/^LoadState=//p')"
    if [ -z "$load_state" ]; then
        # rc=0 but no LoadState= line at all -- an ambiguous/unparseable
        # reply shape, NOT the well-formed "not-found" answer. Never
        # collapse this into not-registered.
        echo "unavailable"
        return
    fi
    if [ "$load_state" = "not-found" ]; then
        echo "not-registered"
        return
    fi
    active_state="$(printf '%s\n' "$out" | sed -n 's/^ActiveState=//p')"
    if [ "$active_state" = "active" ]; then
        echo "registered-active"
    else
        echo "registered-failing"
    fi
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
        _log_supervision "SUPERVISED_HEALTHY -- engram-surface-daemon.service is registered and active; trusting the supervisor's own restart policy, no reap/relaunch/rm"
        ;;
    registered-failing)
        _log_supervision "SUPERVISED_FAILING -- engram-surface-daemon.service is registered but NOT active; refusing to launch a competing process (never fight a real supervisor). Investigate: systemctl --user status engram-surface-daemon.service"
        ;;
    not-registered)
        if [ "$_systemctl_present" -eq 1 ]; then
            _log_supervision "UNSUPERVISED -- no supervisor unit is registered for engram-surface-daemon.service; running the reap+launch sequence directly, as before. Install a supervisor unit: tools/setup-surface-daemon-service.sh"
        else
            _log_supervision "UNSUPERVISED (no systemctl on this host -- treating as unsupervised, launching) -- this host cannot have a systemd unit at all (Darwin, or any non-systemd host); running the reap+launch sequence directly, as before. A Darwin launchctl-based supervision check is a named follow-up, not this fold."
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
        # On UNKNOWN OWNER (either question), skip the socket delete and
        # write one report line to $LOG_PATH -- never abort the script; the
        # launch attempt below still proceeds unchanged, so a
        # genuinely-occupied socket makes the new daemon's own bind() fail
        # loudly, which is the correct, visible failure mode.
        #
        # $PID_PATH IS also gated by this SAME guard verdict (fold onto
        # this unit, reviewer-fairy finding on PR #2687):
        # engram-surface-daemon.py's own second-layer defense (main()'s
        # _recorded_pid_is_alive() check, added in #1874) reads $PID_PATH
        # at the next daemon's startup to decide whether to back off from
        # $SOCKET_PATH (a confirmably-live prior instance owns it) or steal
        # it (the prior owner reads as dead). A stale-but-present pidfile
        # pointing at a dead pid is harmless -- _recorded_pid_is_alive()
        # correctly reads it as dead via os.kill(pid, 0) ->
        # ProcessLookupError -- but a MISSING pidfile is read as "not
        # confirmably ours" and causes a steal. Unconditionally removing
        # $PID_PATH here, regardless of the guard's own verdict on
        # $SOCKET_PATH, would silently defeat this guard on its most
        # realistic trigger path: the very next process this same
        # invocation launches would see no pidfile, conclude the live,
        # unmatched holder is dead, and unlink $SOCKET_PATH itself --
        # reproducing the exact incident this guard exists to prevent, one
        # function call later. Removing $PID_PATH only when
        # $_allow_socket_rm == 1 (an orphan, or a pid this run's own kill
        # loop just killed) keeps both files' lifecycles coupled to the
        # same ownership determination.
        _stat_identity() {
            # dev:inode:birthtime, or empty (nonzero exit) if the path
            # doesn't exist. Birth-time (%W) is GNU-coreutils-specific
            # (statx-backed); reads as "0" when the filesystem doesn't
            # expose it, which is harmless here -- a constant field can
            # never itself manufacture a mismatch between two calls, it
            # just adds no discriminating value when unsupported.
            stat -c '%d:%i:%W' "$1" 2>/dev/null
        }
        _log_socket_guard() {
            printf '[%s] [start-engram-daemon] socket-guard: %s\n' \
                "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$1" >> "$LOG_PATH" 2>/dev/null || true
        }
        _socket_pre_stat="$(_stat_identity "$SOCKET_PATH")"

        for stale_pid in $_stale_pids; do
            kill "$stale_pid" 2>/dev/null || true
        done

        _socket_post_stat="$(_stat_identity "$SOCKET_PATH")"
        _allow_socket_rm=1
        if [ -z "$_socket_post_stat" ]; then
            : # nothing on disk any more -- rm -f would be a no-op either
              # way, nothing here for the guard to protect.
        elif [ "$_socket_pre_stat" != "$_socket_post_stat" ]; then
            # (A) race: the file's own identity changed since the first
            # observation. Sufficient on its own to refuse -- do not
            # proceed to (B); a race with no live holder yet (the new
            # socket hasn't started listening) is still correctly "don't
            # delete".
            _allow_socket_rm=0
            _log_socket_guard "refusing to remove ${SOCKET_PATH} -- on-disk identity changed since first observation (race: pre=[${_socket_pre_stat:-none}] post=[${_socket_post_stat}]), another process may have just (re)bound this path"
        else
            # (B) live-holder identification. Path-TEXT match against
            # /proc/net/unix (never its Inode column against stat() -- see
            # above), then /proc/<pid>/fd/* to resolve which pid (if any)
            # holds it, and whether that pid is one this run's kill loop
            # just killed.
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

def read_cmdline(pid):
    try:
        with open('/proc/' + pid + '/cmdline', 'rb') as f:
            raw = f.read()
    except OSError:
        return '(cmdline unavailable)'
    parts = [p.decode(errors='replace') for p in raw.split(b'\0') if p]
    return ' '.join(parts) if parts else '(empty cmdline)'

def find_holder(inode):
    for pid in killed_pids:
        if pid_holds_inode(pid, inode):
            return pid, True
    try:
        all_pids = [p for p in os.listdir('/proc') if p.isdigit()]
    except OSError:
        return None, False
    for pid in all_pids:
        if pid_holds_inode(pid, inode):
            return pid, False
    return None, False

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
    holder_pid, was_killed = find_holder(inode)
    if holder_pid is None:
        print('held by an unresolved process (sockfs inode=' + inode + '; holder pid not determinable -- permission boundary or process already exited)')
        sys.exit(1)
    if not was_killed:
        cmdline = read_cmdline(holder_pid)
        print('(inode=' + inode + ') held by pid ' + holder_pid + ' (' + cmdline + '), not matched by the reap loop for this invocation')
        sys.exit(1)

sys.exit(0)
" $_stale_pids)"
            _holder_rc=$?
            if [ "$_holder_rc" -ne 0 ]; then
                _allow_socket_rm=0
                _log_socket_guard "refusing to remove ${SOCKET_PATH} -- ${_holder_output}"
            fi
        fi

        if [ "$_allow_socket_rm" -eq 1 ]; then
            rm -f "$PID_PATH" "$SOCKET_PATH"
        else
            : # UNKNOWN OWNER: leave both files untouched. Removing
              # PID_PATH here would defeat engram-surface-daemon.py's own
              # second-layer defense (_recorded_pid_is_alive()), which
              # reads PID_PATH to decide whether to back off from or steal
              # SOCKET_PATH -- exactly the incident this guard exists to
              # prevent, one layer down. See PR #2687's reviewer findings
              # for the full reproduction.
        fi

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
        date +%s > "$ENGRAM_HOME/daemon-launch-attempt" 2>/dev/null || true
        ;;
    *)
        # unavailable (or any other value this function should never
        # produce): could not determine supervisor registration state.
        # Per Kepler: "a missing daemon is loud and recoverable, a
        # duplicate was the silent WAL loss" -- when in doubt, do not risk
        # creating a second holder.
        _log_supervision "unavailable -- could not determine whether a supervisor unit is registered for engram-surface-daemon.service; refusing to launch (a missing daemon is loud and recoverable; a duplicate risks silent WAL loss)"
        ;;
esac

exit 0
