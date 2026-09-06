#!/bin/bash
# SessionStart hook: ensure the engram surface daemon is running.
# If already running, this is a no-op.

: "${ENGRAM_HOME:=$HOME/.engram}"
SOCKET_PATH="$ENGRAM_HOME/recall-daemon.sock"
PID_PATH="$ENGRAM_HOME/recall-daemon.pid"
DAEMON_SCRIPT="$(dirname "$0")/engram-surface-daemon.py"
LOG_PATH="$ENGRAM_HOME/surface-daemon.log"

# #2652 phase-0 unit 1: log which liveness-probe branch this invocation
# took, so a restart can later be traced to "silent death" vs. "pidfile-
# not-yet-written race" vs. "live but unresponsive" vs. "healthy, no
# action" without guessing (Borges's §4 unresolved question, issue #2652).
# Additive only -- does not change the reap/relaunch or healthy-exit
# behavior below.
_log_probe() {
    printf '[%s] [start-engram-daemon] probe: %s\n' \
        "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$1" >> "$LOG_PATH" 2>/dev/null || true
}

# Check if daemon is already running
if [ -f "$PID_PATH" ]; then
    pid=$(cat "$PID_PATH")
    if kill -0 "$pid" 2>/dev/null; then
        # Daemon is running, verify socket is responsive
        if {{PYTHON}} -c "
import socket, sys
s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
s.settimeout(2)
try:
    s.connect('$SOCKET_PATH')
    s.close()
    sys.exit(0)
except Exception:
    sys.exit(1)
" 2>/dev/null; then
            # Daemon is alive and socket works.
            _log_probe "healthy (pid=${pid}), no action"
            exit 0
        else
            _log_probe "pidfile present (pid=${pid}), process alive, socket probe failed/timed out"
        fi
    else
        _log_probe "pidfile present (pid=${pid}) but process not running"
    fi
else
    _log_probe "no pidfile at ${PID_PATH}"
fi

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
_reap_pattern="engram-surface-daemon\.py.*${ENGRAM_HOME}\$"

# Fold onto #2652 phase-0 unit 1 (colleague-review finding, Ariadne): branch
# 1's "no pidfile" line above collapses three distinct cases -- a genuine
# cold start, the pidfile-not-yet-written warmup race, and a start that
# lands after THIS OR A PRIOR invocation already reaped a daemon and removed
# the pidfile. Recording whether this invocation's reap loop actually
# matched/killed anything doesn't fully disambiguate all three (the race and
# a genuine cold start both reap zero), but it separates "something was
# killed just now" from "nothing was killed" -- the discriminating datum
# this unit exists to produce. Capture the pgrep output ONCE into a variable
# (rather than calling pgrep twice) so the log line and the kill loop can
# never disagree about what was matched, and so a live race between two
# separate pgrep calls can't produce a different process set for each.
_stale_pids="$(pgrep -f "$_reap_pattern" 2>/dev/null)"
if [ -n "$_stale_pids" ]; then
    _stale_count="$(printf '%s\n' "$_stale_pids" | grep -c .)"
    _log_probe "reap matched ${_stale_count} stale process(es): $(printf '%s' "$_stale_pids" | tr '\n' ' ')"
else
    _log_probe "reap matched none"
fi
for stale_pid in $_stale_pids; do
    kill "$stale_pid" 2>/dev/null || true
done
rm -f "$PID_PATH" "$SOCKET_PATH"

# Start daemon in background. The trailing $ENGRAM_HOME argv is unused by
# the script (ENGRAM_HOME is still resolved from the env var as before) --
# it exists solely so pgrep -f above can scope on it.
mkdir -p "$ENGRAM_HOME"
env ENGRAM_HOME="$ENGRAM_HOME" {{PYTHON}} "$DAEMON_SCRIPT" "$ENGRAM_HOME" >> "$LOG_PATH" 2>&1 < /dev/null &

# Fire-and-forget: the daemon warms asynchronously (cold model-load
# routinely exceeds any fixed wait); the per-turn UserPromptSubmit
# surface hook is the liveness check and emits a CRITICAL warning only
# on a genuine daemon-down. No blocking wait here -> no cold-start-race
# false alarm.

# Stamp the launch attempt so the per-turn surface hook can tell a
# cold-start warmup (recent stamp → SOFT "warming up") from a genuinely
# down daemon (stale/absent stamp → CRITICAL).
date +%s > "$ENGRAM_HOME/daemon-launch-attempt" 2>/dev/null || true

exit 0
