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
        # Daemon is running, verify socket is responsive. #2676: capture the
        # heredoc's stderr (the caught exception's class name) via command
        # substitution instead of discarding it to /dev/null, so branch 3's
        # log line can name WHICH of three distinct populations fired --
        # FileNotFoundError (healthy daemon still in its warmup window,
        # before bind()/listen() -- see engram-surface-daemon.py's main():
        # pidfile write, then server.bind(), then server.listen()),
        # ConnectionRefusedError (the orphan: a dead/unlinked socket -- the
        # actual pathology #2652 exists to fix), or BlockingIOError (a live,
        # listening daemon whose accept backlog is full). socket.timeout is
        # measured unreachable on an AF_UNIX connect() -- every failure
        # returns immediately -- so no refused-vs-timed-out split is built;
        # the settimeout(2) call stays as defensive code regardless.
        # Restructured from a direct `if <cmd>; then` into a
        # capture-then-check-$? form so the same exit-code semantics still
        # gate the branch.
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
            # Daemon is alive and socket works.
            _log_probe "healthy (pid=${pid}), no action"
            exit 0
        else
            _log_probe "pidfile present (pid=${pid}), process alive, socket probe failed: ${_probe_output:-unknown}"
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

# #2652 phase-0 unit 2: never `rm -f` a socket this run has not established
# it owns (issue #2652, Borges's "Unit 2" design intent + Sol's own
# correction comment on the same issue -- the discriminator below is the
# CORRECTED mechanism; an earlier literal recipe that cross-referenced
# stat()'s filesystem-dentry inode directly against /proc/net/unix's Inode
# column was measured to never match -- those are disjoint kernel inode
# namespaces (filesystem dentry vs. sockfs) that structurally never
# coincide). The live incident this stops: a reap pattern scoped to THIS
# hook's own launch argv (see _reap_pattern above) cannot match a daemon
# started by a different launch surface (e.g. a systemd/launchd
# supervisor with its own argv) -- so `_stale_pids` reads empty even
# though the daemon at $SOCKET_PATH is alive, correctly functioning, and
# unrelated to anything this run just reaped. Unconditionally deleting
# $SOCKET_PATH in that case clobbers a live process's socket out from
# under it. The guard below splits into two independently-answerable
# questions, run in order:
#
#   (A) RACE DETECTION -- has the on-disk special file's own identity
#       (device+inode+birth-time, pure stat(), no /proc/net/unix
#       involved) changed between a snapshot taken now (before the kill
#       loop) and a second snapshot taken immediately before the delete
#       would happen? A mismatch means some other process (re)bound a
#       fresh socket at this path in the window -- refuse unconditionally,
#       do not proceed to (B).
#   (B) LIVE-HOLDER IDENTIFICATION -- once (A) confirms no race, correlate
#       /proc/net/unix's PATH-TEXT column (never its Inode column, which
#       is a sockfs number -- see above) to a pid via /proc/<pid>/fd/*,
#       and check whether every such pid is one this run's kill loop just
#       killed (or the socket is unheld by anyone -- a plain orphan). Any
#       live holder NOT in this run's killed-pid set is UNKNOWN OWNER.
#
# On UNKNOWN OWNER (either question), skip the socket delete and write one
# report line to $LOG_PATH -- never abort the script; the launch attempt
# below still proceeds unchanged, so a genuinely-occupied socket makes the
# new daemon's own bind() fail loudly, which is the correct, visible
# failure mode.
#
# $PID_PATH IS now gated by this SAME guard verdict (fold onto this unit,
# reviewer-fairy finding on PR #2687): engram-surface-daemon.py's own
# second-layer defense (main()'s _recorded_pid_is_alive() check, added in
# #1874) reads $PID_PATH at the next daemon's startup to decide whether to
# back off from $SOCKET_PATH (a confirmably-live prior instance owns it) or
# steal it (the prior owner reads as dead). A stale-but-present pidfile
# pointing at a dead pid is harmless -- _recorded_pid_is_alive() correctly
# reads it as dead via os.kill(pid, 0) -> ProcessLookupError -- but a
# MISSING pidfile is read as "not confirmably ours" and causes a steal.
# Unconditionally removing $PID_PATH here, regardless of the guard's own
# verdict on $SOCKET_PATH, would silently defeat this guard on its most
# realistic trigger path: the very next process this same invocation
# launches would see no pidfile, conclude the live, unmatched holder is
# dead, and unlink $SOCKET_PATH itself -- reproducing the exact incident
# this guard exists to prevent, one function call later. Removing
# $PID_PATH only when $_allow_socket_rm == 1 (an orphan, or a pid this
# run's own kill loop just killed) keeps both files' lifecycles coupled to
# the same ownership determination.
_stat_identity() {
    # dev:inode:birthtime, or empty (nonzero exit) if the path doesn't
    # exist. Birth-time (%W) is GNU-coreutils-specific (statx-backed);
    # reads as "0" when the filesystem doesn't expose it, which is
    # harmless here -- a constant field can never itself manufacture a
    # mismatch between two calls, it just adds no discriminating value
    # when unsupported. Confirmed on this dev container's filesystem that
    # %W IS supported (nonzero epoch seconds returned for a freshly bound
    # socket) -- see this unit's PR description for the measurement.
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
    : # nothing on disk any more -- rm -f would be a no-op either way,
      # nothing here for the guard to protect.
elif [ "$_socket_pre_stat" != "$_socket_post_stat" ]; then
    # (A) race: the file's own identity changed since the first
    # observation. Sufficient on its own to refuse -- do not proceed to
    # (B); a race with no live holder yet (the new socket hasn't started
    # listening) is still correctly "don't delete".
    _allow_socket_rm=0
    _log_socket_guard "refusing to remove ${SOCKET_PATH} -- on-disk identity changed since first observation (race: pre=[${_socket_pre_stat:-none}] post=[${_socket_post_stat}]), another process may have just (re)bound this path"
else
    # (B) live-holder identification. Path-TEXT match against
    # /proc/net/unix (never its Inode column against stat() -- see above),
    # then /proc/<pid>/fd/* to resolve which pid (if any) holds it, and
    # whether that pid is one this run's kill loop just killed.
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
    : # UNKNOWN OWNER: leave both files untouched. Removing PID_PATH here
      # would defeat engram-surface-daemon.py's own second-layer defense
      # (_recorded_pid_is_alive()), which reads PID_PATH to decide whether
      # to back off from or steal SOCKET_PATH -- exactly the incident this
      # guard exists to prevent, one layer down. See PR #2687's reviewer
      # findings for the full reproduction.
fi

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
