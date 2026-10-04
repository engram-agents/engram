"""Tests for #2652 phase-0 unit 2 -- start-engram-daemon.sh never `rm -f`s a
socket this run has not established it owns.

Sibling file to tests/test_start_daemon_probe_branch_logging.py (unit 1's
probe-branch-logging tests) rather than an extension of it: unit 1's file is
about which LIVENESS-PROBE BRANCH fired (branches 1-4, the healthy-exit
early-return, and the reap-outcome count); this unit is about a materially
different concern -- what happens to $SOCKET_PATH once the script has
already fallen through past the healthy branch and reached the
reap-then-delete section. The two concerns share a script but not a test
subject, so this file is kept separate. Per the dispatching agent's own
addendum: this unit does NOT touch
tests/test_start_daemon_probe_branch_logging.py, so PR #2682's suggested
fourth permanent falsification class (TestBranch3BlockingIOAgainstPreUnit1Script)
is explicitly NOT folded in here -- it remains for whichever later #2652 unit
next touches that file.

The incident this unit exists to stop (see issue #2652, Borges's "Unit 2"
design intent comment, and Sol's own correction comment on the same issue):
today's code does

    for stale_pid in $(pgrep -f "$_reap_pattern"); do kill "$stale_pid"; done
    rm -f "$PID_PATH" "$SOCKET_PATH"

unconditionally, regardless of whether the reap loop matched anything. A
daemon launched by a DIFFERENT launch surface (e.g. a systemd/launchd
supervisor with its own argv) is invisible to `_reap_pattern` (which is
scoped to this hook's OWN launch line), so `_stale_pids` reads empty even
though the daemon at $SOCKET_PATH is alive and correctly functioning -- and
the unconditional `rm -f` clobbers its socket file out from under it. This
is Zephyr's live incident, described in the #2652 issue thread.

The guard's discriminator (the CORRECTED version -- an earlier literal
recipe that cross-referenced stat()'s filesystem-dentry inode directly
against /proc/net/unix's Inode column was measured to never match, since
those are disjoint kernel inode namespaces) splits into two questions:

  (A) RACE DETECTION -- pure stat() identity (device+inode+birth-time) of
      $SOCKET_PATH, compared between a snapshot taken before the kill loop
      and a second snapshot taken immediately before the delete would
      happen. A mismatch means some other process (re)bound a fresh socket
      at this path in the window -- refuse, no /proc/net/unix needed.
  (B) LIVE-HOLDER IDENTIFICATION -- once (A) confirms no race, correlate
      /proc/net/unix's PATH-TEXT column (never its Inode column) to a pid
      via /proc/<pid>/fd/*, and check whether every such pid is one this
      run's kill loop just killed.

Six scenarios below (spec's numbering): no socket; orphaned socket; socket
held by a killed pid; socket held by an unmatched pid (the Zephyr case);
the race case; and a falsification control proving scenario 4 would have
failed against the OLD unconditional `rm -f` line. Scenario 7 (unit 1's
existing branch tests still pass unmodified) is verified by directly
running tests/test_start_daemon_probe_branch_logging.py -- pasted in the
PR description, not re-implemented here since it is unit 1's own file
running unit 1's own assertions unchanged.

Fold onto this unit (reviewer-fairy REQUEST_CHANGES on PR #2687, tip
319ae6ae): the ORIGINAL version of this unit left $PID_PATH unconditionally
removed regardless of the guard's own $SOCKET_PATH verdict, on the stated
(and falsified) rationale that $PID_PATH is "not a live resource in
itself". This is wrong: engram-surface-daemon.py's own second-layer
defense (main()'s _recorded_pid_is_alive(), added in #1874) reads
$PID_PATH at the NEXT daemon's startup to decide whether to back off from
$SOCKET_PATH or steal it -- so unconditionally clearing $PID_PATH whenever
this guard refuses defeats the guard one function call later, on its most
realistic trigger path. The fold gates $PID_PATH's removal by the SAME
$_allow_socket_rm verdict as $SOCKET_PATH. Scenario 4's test class
(TestUnknownOwnerNotMatchedByReap) now also seeds and asserts on
$PID_PATH directly, and TestPidPathRemovalDefeatsOwnershipCheck below is
the permanent falsification control proving the pre-fold behavior really
did defeat engram-surface-daemon.py's own ownership check.
"""

from __future__ import annotations

import importlib.util
import os
import re
import shutil
import socket as socket_mod
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

import pytest

# Cross-file reuse (mirrors the established
# test_pgrep_liveness_reap_patterns._extract_reap_pattern import
# convention used by tests/test_launch_line_reap_pattern_binding.py):
# #2652 phase-0 unit 4's fold gates every scenario in this file's
# reap/rm site behind the new supervisor-registration check, so the
# scenarios below need a deterministic way to force the UNSUPERVISED
# state regardless of this host's own ambient systemctl behavior.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from test_start_daemon_probe_branch_logging import _make_fake_systemctl  # noqa: E402



def _missing_guard_primitives() -> list[str]:
    """Linux primitives the script's socket-ownership guard reads
    (/proc/net/unix, /proc/<pid>/fd, GNU `stat -c`); names the ones this
    host lacks. The condition is the primitive itself, never the platform
    name (#3333)."""
    missing = []
    if not Path("/proc/net/unix").exists():
        missing.append("/proc/net/unix")
    if not os.path.isdir("/proc/self/fd"):
        missing.append("/proc/<pid>/fd")
    try:
        gnu_stat = subprocess.run(
            ["stat", "-c", "%d:%i:%W", "/"], capture_output=True, timeout=5,
        ).returncode == 0
    except (OSError, subprocess.SubprocessError):
        gnu_stat = False
    if not gnu_stat:
        missing.append("GNU `stat -c`")
    return missing


_MISSING_GUARD_PRIMITIVES = _missing_guard_primitives()
_NEEDS_GUARD_PRIMITIVES = pytest.mark.skipif(
    bool(_MISSING_GUARD_PRIMITIVES),
    reason="needs " + ", ".join(_MISSING_GUARD_PRIMITIVES) + " (socket-ownership guard)",
)

def _missing_stat_primitive() -> list[str]:
    """Only the GNU `stat -c` part of the guard primitives, for cells that
    shell out to `stat` but touch neither /proc/net/unix nor /proc/<pid>/fd
    (a host with GNU stat and no procfs must still run them) (#3333)."""
    return [m for m in _missing_guard_primitives() if m == "GNU `stat -c`"]


_MISSING_STAT_PRIMITIVE = _missing_stat_primitive()
_NEEDS_GNU_STAT = pytest.mark.skipif(
    bool(_MISSING_STAT_PRIMITIVE),
    reason="needs GNU `stat -c` (absent on this host)",
)

def _missing_live_guard_primitives() -> list[str]:
    """What the LIVE script's guard needs on THIS host (#3333 follow-up):
    an identity snapshot method (GNU `stat -c` or BSD `stat -f`) and a
    live-holder method (/proc/net/unix + /proc/<pid>/fd, or `lsof`). Unlike
    _missing_guard_primitives (which names the GNU-only primitives the
    frozen pre-fold script copies still need), this is what the shipped
    script itself needs, so cells exercising the live script run on Darwin
    as well as Linux."""
    missing = []
    stat_ok = False
    for argv in (["stat", "-c", "%d:%i:%W", "/"], ["stat", "-f", "%d:%i", "/"]):
        try:
            if subprocess.run(argv, capture_output=True, timeout=5).returncode == 0:
                stat_ok = True
                break
        except (OSError, subprocess.SubprocessError):
            pass
    if not stat_ok:
        missing.append("a usable `stat` (GNU -c or BSD -f)")
    has_proc = Path("/proc/net/unix").exists() and os.path.isdir("/proc/self/fd")
    if not has_proc and shutil.which("lsof") is None:
        missing.append("/proc/net/unix or `lsof`")
    return missing


_MISSING_LIVE_GUARD_PRIMITIVES = _missing_live_guard_primitives()
_NEEDS_LIVE_GUARD_PRIMITIVES = pytest.mark.skipif(
    bool(_MISSING_LIVE_GUARD_PRIMITIVES),
    reason="needs " + ", ".join(_MISSING_LIVE_GUARD_PRIMITIVES) + " (live socket-ownership guard)",
)

REPO_ROOT = Path(__file__).resolve().parent.parent
DAEMON_DIR = REPO_ROOT / "src" / "engram" / "hooks" / "claude"
START_SCRIPT_PATH = DAEMON_DIR / "start-engram-daemon.sh"
DAEMON_PY_PATH = DAEMON_DIR / "engram-surface-daemon.py"


def _load_daemon_module():
    """Load engram-surface-daemon.py as a fresh module instance -- mirrors
    tests/test_surface_daemon_pid_reap.py's _load_daemon_module (same
    pattern, duplicated here rather than imported since that file is a
    sibling this fold's scope bounds say not to touch). A fresh instance
    per call avoids two tests' PID_PATH/SOCKET_PATH monkey-patches leaking
    into each other via a cached sys.modules entry."""
    spec = importlib.util.spec_from_file_location(
        "engram_surface_daemon_ownership_check", str(DAEMON_PY_PATH)
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _simulate_daemon_ownership_gate(mod, pid_path: Path, socket_path: Path) -> str:
    """Mirrors engram-surface-daemon.py's main() steal-or-back-off block
    (the `if os.path.exists(SOCKET_PATH): if _recorded_pid_is_alive(): ...
    else: os.unlink(SOCKET_PATH)` shape at the top of main(), verbatim in
    control flow) WITHOUT calling the rest of main() (which would import
    engram_client and load a real embedding model). Calls the REAL,
    unmodified mod._recorded_pid_is_alive() to make the decision -- this
    test file does not re-implement that function's logic, so a change to
    the shipped function is exactly what this helper would catch.

    Returns "no-socket" (main() never reaches the check), "backed-off"
    (main()'s own sys.exit(0) path -- SOCKET_PATH is left untouched, same
    as the real function), or "stole" (main() unlinks SOCKET_PATH -- and
    this helper performs that same unlink for real, so callers can observe
    the actual on-disk effect)."""
    mod.PID_PATH = str(pid_path)
    mod.SOCKET_PATH = str(socket_path)
    if not os.path.exists(mod.SOCKET_PATH):
        return "no-socket"
    if mod._recorded_pid_is_alive():
        return "backed-off"
    try:
        os.unlink(mod.SOCKET_PATH)
    except OSError:
        pass
    return "stole"

_STUB_DAEMON_SOURCE = (
    "import time, sys\n"
    "print('stub daemon started', file=sys.stderr)\n"
    "time.sleep(5)\n"
)

# Byte-for-byte snapshot of start-engram-daemon.sh as it stood on origin/dev
# BEFORE #2652 phase-0 unit 2 added the socket ownership guard (verified via
# `git show origin/dev:src/engram/hooks/claude/start-engram-daemon.sh`
# immediately prior to writing this unit's patch -- origin/dev at that point
# already carried units 1, 1a, and 1b). Frozen deliberately as the
# falsification control's baseline (scenario 6) -- proves the Zephyr
# scenario (TestUnknownOwnerNotMatchedByReap) would have FAILED (socket
# deleted, live holder orphaned) against the unconditional
# `rm -f "$PID_PATH" "$SOCKET_PATH"` this unit replaces.
_PRE_UNIT2_SCRIPT_TEXT = r"""#!/bin/bash
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
"""


# Frozen snapshot of start-engram-daemon.sh as it stood at PR #2687's tip
# 319ae6ae -- i.e. AFTER #2652 phase-0 unit 2's own guard landed, but
# BEFORE this fold gated $PID_PATH's removal by the same $_allow_socket_rm
# verdict as $SOCKET_PATH (verified by diffing against the live script
# immediately prior to writing this fold's patch). Differs from the live
# script ONLY in the final if/else block: this baseline still runs
# `rm -f "$PID_PATH"` unconditionally in the else (UNKNOWN OWNER) branch,
# reproducing the reviewer-fairy's REQUEST_CHANGES finding on PR #2687 --
# see TestPidPathRemovalDefeatsOwnershipCheck below, the permanent
# falsification control proving that finding was real and is closed by
# this fold.
_PRE_FOLD_SCRIPT_TEXT = r"""#!/bin/bash
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
# $PID_PATH is deliberately NOT gated by this guard and is always removed
# below: it is this hook's own bookkeeping text file, not a live resource
# in itself -- removing a stale/wrong pidfile can't touch a live process
# or its socket, so there is nothing here for the ownership guard to
# protect. Only $SOCKET_PATH (a real on-disk special file a live process
# may hold open) needs the guard.
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
    rm -f "$PID_PATH"
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
"""


_HAS_PROC = Path("/proc/net/unix").exists()


def _spawn_sleeper(seconds: float = 8.0):
    """A real, short-lived subprocess with a genuine, confirmable PID
    (mirrors tests/test_surface_daemon_pid_reap.py's and
    tests/test_start_daemon_probe_branch_logging.py's _spawn_sleeper)."""
    return subprocess.Popen([sys.executable, "-c", f"import time; time.sleep({seconds})"])


def _patch_in_race_hook(script_text: str) -> str:
    """Test-only injection point for scenario 5 (the race case): finds the
    exact `_socket_post_stat=...` line this unit's guard added and prepends
    a conditional call to $_UNIT2_TEST_RACE_HOOK (a no-op unless the test
    sets that env var), run immediately before the post-kill-loop stat.
    This is the same technique
    tests/test_start_daemon_probe_branch_logging.py's own
    _patch_out_bare_except_2666 uses -- a targeted, asserted textual patch
    of a copy of the script, never touching the shipped file, so a future
    refactor that moves this line fails this test loudly (via the assert)
    rather than silently no-opping the race simulation. Reliably racing the
    real, sub-millisecond window between the two stat() calls from an
    external process is not practical; this hook makes the race
    deterministic instead of statistical."""
    target = '_socket_post_stat="$(_stat_identity "$SOCKET_PATH")"'
    assert target in script_text, (
        "expected `_socket_post_stat=...` line not found -- unit 2's guard "
        "shape changed (update this patch to match the new location)"
    )
    hook_call = (
        'if [ -n "$_UNIT2_TEST_RACE_HOOK" ]; then "$_UNIT2_TEST_RACE_HOOK"; fi\n'
        + target
    )
    return script_text.replace(target, hook_call)


class _ForceUnsupervisedMixin:
    """#2652 phase-0 unit 4 fold: forces the four-state supervisor-
    registration branch into its UNSUPERVISED (not-registered) state for
    every `_run()` call in this mixed-in class, so the unit-2-era guard
    scenarios below (written before the fold's supervisor check existed)
    continue to exercise the actual reap/rm/launch site instead of
    silently short-circuiting on this host's own real `systemctl`
    (measured "Failed to connect to bus", exit 1 -- reads `unavailable`
    here, which would make the guarded rm site unreachable for a reason
    entirely unrelated to what each scenario is actually testing).

    Applies ONLY to the classes below that run the LIVE script -- classes
    using a frozen pre-fold SCRIPT_TEXT (e.g. _PRE_UNIT2_SCRIPT_TEXT,
    _PRE_FOLD_SCRIPT_TEXT) never call systemctl at all, so mixing this in
    there would be harmless but pointless; they are left unmixed."""

    def setUp(self):
        super().setUp()
        self._fake_systemctl_dir = self.tmp / "fakebin-systemctl"
        self._fake_systemctl_dir.mkdir()
        _make_fake_systemctl(
            self._fake_systemctl_dir, mode="show",
            load_state="not-found", active_state="inactive", sub_state="dead",
        )

    def _run(self, timeout: float = 10.0, extra_env: dict | None = None) -> subprocess.CompletedProcess:
        merged_env = {"PATH": f"{self._fake_systemctl_dir}:{os.environ.get('PATH', '')}"}
        if extra_env:
            merged_env.update(extra_env)
        return super()._run(timeout=timeout, extra_env=merged_env)


def _shell_stat_identity(path) -> str:
    """Python-side mirror of the guard's own `_stat_identity()` shell
    function (`stat -c '%d:%i:%W' <path>`) -- shells out to the real `stat`
    binary for byte-for-byte parity with what the shipped script actually
    runs, since Python's os.stat() does not expose the Linux statx
    birth-time field. Returns "" if the path does not exist (mirrors the
    shell function's empty-string-on-nonzero-exit behavior). Used by
    TestRaceHookIdentityChangeIsGuaranteed (#2703) to test the
    scenario-5 race-hook mechanism directly, independent of the full
    script-under-test."""
    try:
        return subprocess.check_output(
            ["stat", "-c", "%d:%i:%W", str(path)], stderr=subprocess.DEVNULL, text=True,
        ).strip()
    except subprocess.CalledProcessError:
        return ""


class _SocketGuardFixture(unittest.TestCase):
    """Shared setup: a real, executable copy of start-engram-daemon.sh (or
    the frozen pre-unit-2 variant) with {{PYTHON}} substituted, next to a
    stub engram-surface-daemon.py so the reap/relaunch path never touches
    the real daemon (which would load a real embedding model)."""

    SCRIPT_TEXT: str | None = None  # None -> use the live source file
    RACE_HOOK: bool = False  # patch in the scenario-5 race-simulation hook

    def setUp(self):
        self._tmp_dir_obj = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp_dir_obj.cleanup)
        self.tmp = Path(self._tmp_dir_obj.name)

        hooks_dir = self.tmp / "hooks"
        hooks_dir.mkdir()
        self.stub_daemon = hooks_dir / "engram-surface-daemon.py"
        self.stub_daemon.write_text(_STUB_DAEMON_SOURCE)

        source_text = self.SCRIPT_TEXT if self.SCRIPT_TEXT is not None else START_SCRIPT_PATH.read_text()
        script_text = source_text.replace("{{PYTHON}}", sys.executable)
        if self.RACE_HOOK:
            script_text = _patch_in_race_hook(script_text)
        self.script = hooks_dir / "start-engram-daemon.sh"
        self.script.write_text(script_text)
        self.script.chmod(self.script.stat().st_mode | stat.S_IXUSR)

        self.engram_home = self.tmp / "engram_home"
        self.engram_home.mkdir()
        self.pid_path = self.engram_home / "recall-daemon.pid"
        self.socket_path = self.engram_home / "recall-daemon.sock"
        self.log_path = self.engram_home / "surface-daemon.log"

        self._extra_cleanups = []
        self.addCleanup(self._run_extra_cleanups)

    def _run_extra_cleanups(self):
        for fn in self._extra_cleanups:
            try:
                fn()
            except Exception:
                pass

    def _run(self, timeout: float = 10.0, extra_env: dict | None = None) -> subprocess.CompletedProcess:
        env = dict(os.environ)
        env["ENGRAM_HOME"] = str(self.engram_home)
        if extra_env:
            env.update(extra_env)
        return subprocess.run(
            ["bash", str(self.script)], env=env, capture_output=True,
            text=True, timeout=timeout,
        )

    def _log_lines(self) -> list[str]:
        if not self.log_path.exists():
            return []
        return self.log_path.read_text().splitlines()

    def _relaunch_happened(self) -> bool:
        return any("stub daemon started" in line for line in self._log_lines())

    def _wait_for_relaunch_marker(self, timeout: float = 5.0) -> bool:
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self._relaunch_happened():
                return True
            time.sleep(0.1)
        return False

    def _bind_listening_socket(self) -> socket_mod.socket:
        """A real, bound+listening AF_UNIX socket at self.socket_path,
        standing in for a live daemon's own listener. Caller is responsible
        for closing it (via addCleanup)."""
        srv = socket_mod.socket(socket_mod.AF_UNIX, socket_mod.SOCK_STREAM)
        srv.bind(str(self.socket_path))
        srv.listen(1)
        return srv

    def _spawn_holder_process(self, *, match_reap_pattern: bool, ignore_sigterm: bool = False, pad_argv: str = "") -> subprocess.Popen:
        """A real subprocess that binds+listens on self.socket_path itself
        (not this test process, so the socket's owning pid is genuinely a
        different, killable/unkillable process). If match_reap_pattern,
        its argv is engineered to satisfy `_reap_pattern`
        ("engram-surface-daemon\\.py" substring, ends with ENGRAM_HOME) so
        this run's reap loop targets and kills it; otherwise its argv
        deliberately does NOT match (standing in for the Zephyr case: a
        daemon launched by a different surface, invisible to this hook's
        own reap pattern)."""
        holder_src = (
            "import signal, socket, sys, time\n"
            + ("signal.signal(signal.SIGTERM, signal.SIG_IGN)\n" if ignore_sigterm else "")
            + "s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)\n"
            f"s.bind({str(self.socket_path)!r})\n"
            "s.listen(1)\n"
            "time.sleep(20)\n"
        )
        argv = [sys.executable, "-c", holder_src]
        if pad_argv:
            argv.append(pad_argv)  # inert extra argv (sys.argv[1]) to lengthen the cmdline
        if match_reap_pattern:
            argv += ["engram-surface-daemon.py", str(self.engram_home)]
        proc = subprocess.Popen(argv)
        self._extra_cleanups.append(lambda: self._kill_proc(proc))
        # Confirm the socket is actually bound before the script under test
        # runs -- a setup race here would make later assertions meaningless.
        deadline = time.time() + 5.0
        while time.time() < deadline and not self.socket_path.exists():
            time.sleep(0.05)
        self.assertTrue(self.socket_path.exists(), "test setup: holder did not bind in time")
        return proc

    @staticmethod
    def _kill_proc(proc: subprocess.Popen):
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=3)

    def _socket_still_functional(self) -> bool:
        """Attempts a fresh connect() to self.socket_path -- true evidence
        the on-disk file is still the live holder's own listening socket,
        not merely "a file happens to exist at this path"."""
        try:
            c = socket_mod.socket(socket_mod.AF_UNIX, socket_mod.SOCK_STREAM)
            c.settimeout(2)
            c.connect(str(self.socket_path))
            c.close()
            return True
        except OSError:
            return False

    def _dead_pid(self) -> int:
        """A pid guaranteed to no longer be alive: spawn a real, short-lived
        subprocess, terminate it, wait for exit, then return its (now
        unassigned) pid -- mirrors
        tests/test_surface_daemon_pid_reap.py's test_dead_pid_returns_false
        pattern. Used to seed $PID_PATH with an on-disk file BEFORE the
        script runs, WITHOUT tripping the script's own branch-1 "healthy,
        no action" early exit (which requires `kill -0 $pid` to succeed) --
        a live pid there would make branch 1 immediately probe-and-exit
        before ever reaching the reap+guard section this unit's fold is
        about, since a genuinely-functional $SOCKET_PATH always makes that
        probe succeed regardless of which alive pid is recorded."""
        proc = _spawn_sleeper(seconds=8.0)
        proc.terminate()
        proc.wait(timeout=3)
        return proc.pid


# ===========================================================================
# Scenario 1 -- no socket file exists: guard is a no-op
# ===========================================================================


class TestNoSocketFile(_ForceUnsupervisedMixin, _SocketGuardFixture):
    """#2652 phase-0 unit 4 FOLD: un-skipped. Unit 4's own build removed
    this guard's site entirely; the fold restores it on the
    UNSUPERVISED/not-registered supervisor-branch path -- forced
    deterministically here via _ForceUnsupervisedMixin (this host's own
    real systemctl reads `unavailable`, which would otherwise make this
    site unreachable for a reason unrelated to what this scenario tests).
    See TestUnknownOwnerAgainstPreUnit2Script below (frozen baseline,
    unaffected either way) for the historical incident record."""
    def test_no_op_no_report_line_script_proceeds(self):
        self.assertFalse(self.socket_path.exists(), "test setup: socket must not exist")
        result = self._run()
        self.assertEqual(result.returncode, 0, result.stderr)
        content = "\n".join(self._log_lines())

        self.assertNotIn("socket-guard", content)
        self.assertTrue(
            self._wait_for_relaunch_marker(),
            "script must still fall through to reap+relaunch, unchanged",
        )


# ===========================================================================
# Scenario 2 -- socket exists, orphaned (no process holds it)
# ===========================================================================


class TestOrphanedSocketAllowsDelete(_ForceUnsupervisedMixin, _SocketGuardFixture):
    """#2652 phase-0 unit 4 FOLD: un-skipped -- see TestNoSocketFile's
    class docstring above for the full explanation."""
    def setUp(self):
        super().setUp()
        # bind+listen+close leaves the on-disk inode behind with nothing
        # holding it -- the plain-orphan case.
        srv = self._bind_listening_socket()
        srv.close()

    def test_guard_allows_delete_no_report_line(self):
        self.assertTrue(self.socket_path.exists(), "test setup: orphaned socket file must exist")
        result = self._run()
        self.assertEqual(result.returncode, 0, result.stderr)
        content = "\n".join(self._log_lines())

        if not _HAS_PROC:
            # No /proc: the guard is refuse-only (lsof rc 1 is UNKNOWN).
            self.assertTrue(self.socket_path.exists())
            self.assertIn("socket-guard: refusing", content)
            return
        self.assertNotIn("socket-guard", content)
        self.assertTrue(self._wait_for_relaunch_marker())
        # The stub daemon never re-binds a socket, so the orphan's removal
        # is directly observable: nothing should exist at this path once
        # the guard has allowed the `rm -f` and the (non-binding) stub has
        # relaunched.
        self.assertFalse(
            self.socket_path.exists(),
            "guard allowed the delete, but the orphaned socket file is still present",
        )


# ===========================================================================
# Scenario 3 -- socket held by a pid the reap loop DID match and kill
# ===========================================================================


class TestHeldByKilledPidAllowsDelete(_ForceUnsupervisedMixin, _SocketGuardFixture):
    """#2652 phase-0 unit 4 FOLD: un-skipped -- see TestNoSocketFile's
    class docstring above for the full explanation. (Unit 4's own build
    had removed this behavior entirely; the fold restores it on the
    UNSUPERVISED path, forced deterministically here.)"""
    def setUp(self):
        super().setUp()
        self.holder = self._spawn_holder_process(match_reap_pattern=True)

    def test_guard_allows_delete_after_kill_no_report_line(self):
        result = self._run()
        self.assertEqual(result.returncode, 0, result.stderr)
        content = "\n".join(self._log_lines())

        self.assertRegex(content, re.compile(r"reap matched 1 stale process\(es\):"))
        if not _HAS_PROC:
            # No /proc: real lsof reports rc 1 after the holder exits, which
            # is UNKNOWN -> refuse-only (the exempt-and-removed path is
            # covered with a shim in the C6 cells).
            self.holder.wait(timeout=3)
            self.assertTrue(self.socket_path.exists())
            self.assertIn("socket-guard: refusing", content)
            return
        self.assertNotIn("socket-guard", content)
        self.assertTrue(self._wait_for_relaunch_marker())

        # The reap loop's kill must have actually landed (unchanged
        # behavior) -- a guard that merely logs "matched" without the kill
        # loop's own effect firing would be testing nothing new.
        self.holder.wait(timeout=3)
        with self.assertRaises(OSError):
            os.kill(self.holder.pid, 0)
        self.assertFalse(
            self.socket_path.exists(),
            "guard allowed the delete for a killed-pid holder, but the socket file is still present",
        )


# ===========================================================================
# Scenario 4 -- socket held by a pid the reap loop did NOT match (Zephyr)
# ===========================================================================


class TestUnknownOwnerNotMatchedByReap(_ForceUnsupervisedMixin, _SocketGuardFixture):
    """#2652 phase-0 unit 4 FOLD: un-skipped -- see TestNoSocketFile's
    class docstring above for the full explanation. (Unit 4's own build
    had removed this scenario's site entirely; the fold restores it on
    the UNSUPERVISED path, forced deterministically here.)

    The live incident #2652 unit 2 exists to stop: a daemon launched by
    a different surface (different argv) is invisible to `_reap_pattern`,
    so the reap loop matches zero processes -- and the guard must still
    refuse to delete a socket that a real, live, unrelated process holds.

    Fold onto this unit (reviewer-fairy REQUEST_CHANGES on PR #2687):
    self.pid_path is now seeded with a DEAD pid before the script runs, so
    a real $PID_PATH file exists on disk when the reap+guard section
    below runs (every test in this file before this fold left
    self.pid_path unwritten, which is why the original guard's
    unconditional `rm -f "$PID_PATH"` in the UNKNOWN-OWNER branch was
    never exercised against a real pidfile). A DEAD pid, not the live
    holder's own pid, is used deliberately: seeding the holder's own
    (alive) pid would make the script's branch-1 `kill -0` check succeed,
    and since the holder's socket genuinely IS connectable, the
    healthy-socket probe would ALSO succeed -- the script would exit 0
    immediately at "healthy, no action" and never reach the reap+guard
    section this fold is about. A dead pid reaches "pidfile present but
    process not running", which falls through exactly like "no pidfile"
    does, but (unlike "no pidfile") leaves a real file on disk for the
    guard to preserve-or-remove."""

    def setUp(self):
        super().setUp()
        self.holder = self._spawn_holder_process(match_reap_pattern=False)
        self._seeded_dead_pid = self._dead_pid()
        self.pid_path.write_text(str(self._seeded_dead_pid))

    @_NEEDS_LIVE_GUARD_PRIMITIVES
    def test_socket_preserved_holder_alive_and_functional_reported(self):
        result = self._run()
        self.assertEqual(result.returncode, 0, result.stderr)
        content = "\n".join(self._log_lines())

        # The reap loop itself is genuinely unchanged -- it must report
        # (and have) matched nothing, since the holder's argv does not
        # satisfy `_reap_pattern`.
        self.assertRegex(content, re.compile(r"reap matched none"))

        # (a) the socket file must NOT be removed
        self.assertTrue(
            self.socket_path.exists(),
            "the guard must refuse to remove a socket held by an unmatched live process",
        )
        # (a2) fold: $PID_PATH must ALSO survive the refusal, with its
        # content untouched -- not just $SOCKET_PATH. This is the actual
        # assertion this fold adds: the reviewer-fairy found that no test
        # in this file ever wrote to self.pid_path, so the pre-fold
        # unconditional `rm -f "$PID_PATH"` in the UNKNOWN-OWNER branch was
        # never exercised. The shell guard treats $PID_PATH's content
        # opaquely (it decides keep-or-remove without ever reading it), so
        # this assertion is valid regardless of the seeded pid's liveness.
        self.assertTrue(
            self.pid_path.exists(),
            "the guard must leave PID_PATH in place when it refuses the socket "
            "delete -- removing it defeats engram-surface-daemon.py's own "
            "second-layer ownership check on the very next launch",
        )
        self.assertEqual(
            self.pid_path.read_text().strip(),
            str(self._seeded_dead_pid),
            "PID_PATH's content must be untouched by the guard",
        )
        # (b) the holder must still be alive AND its bind must still be
        # genuinely functional -- not just present-but-broken.
        self.assertIsNone(self.holder.poll(), "the unmatched live holder must be left running")
        self.assertTrue(
            self._socket_still_functional(),
            "the preserved socket file must still be the holder's own live, connectable listener",
        )
        # (c) the report line must name the correct pid.
        self.assertIn("socket-guard", content)
        self.assertIn(f"held by pid {self.holder.pid}", content)
        self.assertIn("not matched by the reap loop for this invocation", content)

    @_NEEDS_LIVE_GUARD_PRIMITIVES
    def test_next_launch_backs_off_instead_of_stealing(self):
        """The actual regression test for the reviewer-fairy's finding on
        PR #2687: after the shell guard refuses (PID_PATH and SOCKET_PATH
        both preserved), invoke engram-surface-daemon.py's own REAL
        _recorded_pid_is_alive() / steal-or-back-off logic (mirrored via
        _simulate_daemon_ownership_gate, which calls the actual shipped
        function rather than re-implementing it) against the resulting
        on-disk state, and confirm it correctly backs off rather than
        stealing the socket out from under the still-alive holder.

        The shell guard preserves $PID_PATH's content OPAQUELY -- it never
        inspects or rewrites what's in the file, only decides whether to
        keep or remove it (confirmed by the previous test). This test's
        setUp seeded a DEAD placeholder pid rather than the holder's own
        (see that class docstring for why), so before checking the
        downstream interaction contract this test overwrites $PID_PATH's
        CONTENT to the live holder's real pid -- standing in for what a
        genuine branch-3 invocation (pidfile present, `kill -0` succeeds,
        but the socket probe itself fails -- e.g. BlockingIOError on a
        full accept backlog) would have already had recorded there BEFORE
        the guard ever ran, since branch 1 sources `$pid` from this exact
        file. Deterministically forcing that specific probe failure at the
        shell level requires OS-level accept-backlog congestion, which is
        unit 1's probe-branch concern (its own test file), not this
        fold's; this substitution isolates the downstream contract this
        fold is actually about: DOES a guard-preserved, correctly-named
        $PID_PATH cause the next launch to back off? Before this fold,
        the guard did not preserve $PID_PATH at all, so this question was
        moot -- see TestPidPathRemovalDefeatsOwnershipCheck's permanent
        falsification control, which proves the pre-fold answer was
        "stole" without needing this substitution (there, $PID_PATH is
        gone outright, not merely differently-seeded)."""
        # C8 note: the hook now refuses to LAUNCH at all after a refusal
        # (see TestSocketGuardPortableContract C8 cells), so this "next
        # launch" is hypothetical for the hook; the cell still pins the
        # daemon-side gate's behaviour on the preserved on-disk state.
        result = self._run()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(self.pid_path.exists(), "guard must have preserved PID_PATH first")

        self.pid_path.write_text(str(self.holder.pid))

        mod = _load_daemon_module()
        outcome = _simulate_daemon_ownership_gate(mod, self.pid_path, self.socket_path)
        self.assertEqual(
            outcome, "backed-off",
            "engram-surface-daemon.py's own ownership check must back off, not "
            "steal, when PID_PATH is preserved and correctly names the live "
            "unmatched holder",
        )
        # Confirm the back-off was genuine: the holder is still alive and
        # its socket is still the SAME live, connectable listener -- not
        # merely "a file happens to exist at this path".
        self.assertIsNone(self.holder.poll())
        self.assertTrue(self._socket_still_functional())


# ===========================================================================
# Scenario 5 -- the race case: file identity changes between the two stats
# ===========================================================================


class TestRaceDetectionBlocksEvenWithoutALiveHolder(_ForceUnsupervisedMixin, _SocketGuardFixture):
    """#2652 phase-0 unit 4 FOLD: un-skipped -- see TestNoSocketFile's
    class docstring above for the full explanation. (Unit 4's own build
    had removed this guard's pre/post-stat race detection along with the
    `rm -f` site it protected; the fold restores both on the
    UNSUPERVISED path, forced deterministically here.)

    Simulates the socket file being replaced (unlinked + freshly bound)
    in the window between the guard's pre-kill-loop stat and its
    immediately-pre-delete stat, via the test-only race hook (see
    _patch_in_race_hook). Deliberately does NOT bind a new listener at the
    replacement path -- proving the mismatch alone (pure stat() identity)
    is sufficient to refuse, with no live holder found at all.

    #2703: the race-hook mechanism itself must GUARANTEE the on-disk
    identity actually changes -- an unlink() immediately followed by a
    bind() at the SAME path does not reliably guarantee this. Diagnosed via
    `bash -x` tracing (repro script, not committed): on this host's ext4
    filesystem, a just-freed inode is frequently the very NEXT inode number
    handed out for a new file created in the same directory -- measured
    16/20 unlink-then-rebind trials producing a byte-identical
    `dev:inode:%W` triplet pre/post (`%W`, the GNU-coreutils statx-backed
    birth time, has only 1-second resolution, and the whole hook completes
    in well under 100ms, so even a genuinely fresh allocation can't be
    relied on to bump it). When that collision happens, the guard's own
    stat-identity race check (A) finds no disagreement and falls through to
    live-holder identification (B), which finds no holder (the hook's
    socket is closed immediately) and ALLOWS the delete -- reproducing
    exactly the observed symptom (no "socket-guard" line in the log). This
    explains the order-dependence reported in #2703: this test failed
    ~90% of standalone runs (low inode churn nearby) but passed reliably
    as part of the full file (busy nearby inode traffic from sibling
    tests' own socket/subprocess churn perturbs which inode the allocator
    hands back) -- host-filesystem behavior, not Python-side state leakage
    and not a bug in the shipped script.

    Fix: bind the REPLACEMENT socket at a SCRATCH path FIRST, while the
    original path's inode is still allocated (not yet unlinked), then
    `os.rename()` the scratch socket over the original path. Since the
    scratch bind()'s inode allocation happens strictly before the
    original's inode is freed, the allocator structurally cannot hand back
    the same number -- this guarantees a byte-different identity
    deterministically, with no dependence on timing, sleep, or host speed.
    Measured 20/20 with this approach (vs. 16/20 FAILURES for the old
    unlink-then-rebind approach, same host, same loop) -- see
    TestRaceHookIdentityChangeIsGuaranteed below, the permanent regression
    control for this specific mechanism."""

    RACE_HOOK = True

    def setUp(self):
        super().setUp()
        # An initial socket file exists before the run (any file will do
        # for the pre-stat to have something to disagree with -- using a
        # real bind+listen+close, i.e. what would otherwise be the
        # orphan-allow case, makes the point sharpest: this scenario must
        # STILL refuse even though the ORIGINAL file was a plain orphan).
        srv = self._bind_listening_socket()
        srv.close()
        self.assertTrue(self.socket_path.exists())

        # The race-hook script (#2703 fix): bind the FRESH replacement
        # socket at a SCRATCH path first -- the original file at
        # self.socket_path is still on disk/allocated at this point, so the
        # scratch bind() cannot possibly be handed the original's inode --
        # then atomically os.rename() it over the original path, and close
        # it immediately, deliberately leaving no live holder. This
        # guarantees the post-hook on-disk identity (dev:inode:%W) differs
        # from the pre-hook one, so a pass here can only be explained by
        # the identity-mismatch check itself, not by any live-holder
        # signal, AND not by luck of which inode the allocator happened to
        # reuse (see class docstring).
        hook_path = self.tmp / "race_hook.sh"
        scratch_path = f"{self.socket_path}.new"
        hook_src = (
            "#!/bin/bash\n"
            f"{sys.executable} -c \"import socket, os; "
            "s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM); "
            f"s.bind('{scratch_path}'); s.listen(1); "
            f"os.rename('{scratch_path}', '{self.socket_path}'); "
            "s.close()\"\n"
        )
        hook_path.write_text(hook_src)
        hook_path.chmod(hook_path.stat().st_mode | stat.S_IXUSR)
        self.hook_path = hook_path

    @_NEEDS_LIVE_GUARD_PRIMITIVES
    def test_race_blocks_delete_and_reports_even_with_no_live_holder(self):
        result = self._run(extra_env={"_UNIT2_TEST_RACE_HOOK": str(self.hook_path)})
        self.assertEqual(result.returncode, 0, result.stderr)
        content = "\n".join(self._log_lines())

        self.assertIn("socket-guard", content)
        self.assertIn("race", content)
        self.assertTrue(
            self.socket_path.exists(),
            "the guard must refuse to remove a socket whose identity changed mid-run, "
            "even though the replacement has no live holder",
        )

    def test_control_hook_disarmed_allows_normal_orphan_delete(self):
        """Sanity control: the SAME race-hook-patched script, with the hook
        env var unset (a no-op), must behave exactly like the ordinary
        orphan-delete case -- proving the patched script's behavior differs
        from the unpatched script ONLY when the hook actually fires, not
        because patching in the conditional call changed anything else."""
        result = self._run()  # no _UNIT2_TEST_RACE_HOOK env var set
        self.assertEqual(result.returncode, 0, result.stderr)
        content = "\n".join(self._log_lines())

        if not _HAS_PROC:
            # No /proc: refuse-only guard keeps the orphan.
            self.assertTrue(self.socket_path.exists())
            self.assertIn("socket-guard: refusing", content)
            return
        self.assertNotIn("socket-guard", content)
        self.assertFalse(self.socket_path.exists())


# ===========================================================================
# #2703 regression control: the scenario-5 race-hook mechanism must ALWAYS
# produce a changed on-disk stat() identity -- isolates the root cause of the
# test-order-dependent failure diagnosed on issue #2703, independent of the
# full script-under-test.
# ===========================================================================


class TestRaceHookIdentityChangeIsGuaranteed(unittest.TestCase):
    """Permanent regression control for issue #2703's diagnosed root cause.

    TestRaceDetectionBlocksEvenWithoutALiveHolder's own race hook (see that
    class's docstring) simulates a mid-run socket replacement by making the
    on-disk special file's stat() identity (dev:inode:%W) differ between the
    guard's pre-kill-loop snapshot and its immediately-pre-delete snapshot.
    The guard's own race-detection branch (A) depends ENTIRELY on that
    difference actually materializing -- if it doesn't, the guard silently
    falls through to live-holder identification (B), finds no holder (the
    hook's own socket is closed immediately), and allows the delete, which
    is exactly the symptom #2703 reports (no "socket-guard" line in the log,
    order-dependent pass/fail).

    Diagnosis: the ORIGINAL hook did `os.unlink(path)` immediately followed
    by a fresh `bind()` at the SAME path. On this host's ext4 filesystem, a
    just-freed inode is frequently the very NEXT inode number the allocator
    hands out for a new file created in the same directory -- measured
    16/20 unlink-then-rebind trials producing a byte-identical
    `dev:inode:%W` triplet pre/post (`%W` has only 1-second resolution, and
    the whole hook completes in well under 100ms, so even a genuinely fresh
    allocation event can't be relied on to bump it within the same wall-clock
    second). This is a HOST-FILESYSTEM-BEHAVIOR race in the test's own
    simulation mechanism, not Python-side state leakage between tests and
    not a bug in the shipped script -- it explains #2703's order-dependence
    directly: low nearby inode-churn (running this single test standalone)
    makes the collision highly likely; busy nearby inode churn (sibling
    tests' own socket/subprocess activity, as when running the full file)
    makes it unlikely.

    Fix (applied to TestRaceDetectionBlocksEvenWithoutALiveHolder's hook):
    bind the REPLACEMENT socket at a SCRATCH path FIRST, while the original
    path's inode is still allocated (not yet unlinked), then `os.rename()`
    the scratch socket over the original path. Since the scratch bind()'s
    inode allocation happens strictly before the original's inode is freed,
    the allocator structurally cannot hand back the same number -- this
    guarantees a byte-different identity deterministically, independent of
    timing, sleep, or host speed.

    This control exercises that exact mechanism directly (not through the
    full script-under-test, so it stays fast and isolates the one property
    that matters) N_TRIALS times and asserts every trial changes identity.
    Verified during diagnosis (not re-asserted here, to avoid a permanently
    flaky CI assertion tied to host filesystem behavior) that the OLD
    unlink-then-rebind mechanism, run through this identical loop on this
    same host, failed 16/20 trials -- confirming this control discriminates
    the diagnosed defect from the fix rather than passing regardless of
    which mechanism is under test."""

    N_TRIALS = 20

    @_NEEDS_GNU_STAT
    def test_bind_then_rename_hook_always_changes_stat_identity(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "probe.sock"
            scratch = Path(d) / "probe.sock.new"
            failures = []
            for i in range(self.N_TRIALS):
                srv = socket_mod.socket(socket_mod.AF_UNIX, socket_mod.SOCK_STREAM)
                srv.bind(str(path))
                srv.listen(1)
                pre = _shell_stat_identity(path)
                srv.close()

                # The fixed hook mechanism: bind the replacement at a
                # scratch path FIRST (the original's inode is still
                # allocated -- not yet unlinked), then atomically rename it
                # into place. See class docstring for why this guarantees a
                # distinct identity where unlink-then-rebind did not.
                new_srv = socket_mod.socket(socket_mod.AF_UNIX, socket_mod.SOCK_STREAM)
                new_srv.bind(str(scratch))
                new_srv.listen(1)
                os.rename(str(scratch), str(path))
                post = _shell_stat_identity(path)
                new_srv.close()
                os.unlink(str(path))

                if not pre or not post or pre == post:
                    failures.append((i, pre, post))

            self.assertEqual(
                failures, [],
                f"{len(failures)}/{self.N_TRIALS} trials failed to produce a "
                "changed (and non-empty) stat() identity via the bind-then-"
                "rename mechanism -- the fix's core guarantee (scratch bind "
                "precedes the original's unlink, so the allocator cannot "
                "reuse the freed inode) has regressed: "
                f"{failures!r}",
            )


# ===========================================================================
# Scenario 6 -- falsification control: proves TestUnknownOwnerNotMatchedByReap
# would have FAILED against the OLD unconditional `rm -f` line.
# ===========================================================================


class TestUnknownOwnerAgainstPreUnit2Script(_SocketGuardFixture):
    """The single most important test in this unit: the IDENTICAL Zephyr
    scenario from TestUnknownOwnerNotMatchedByReap, run against the frozen
    pre-#2652-unit-2 baseline (today's shipped, unconditional `rm -f
    "$PID_PATH" "$SOCKET_PATH"`). Confirms the regression this unit exists
    to guard against is real and was reachable before this unit's patch --
    i.e. that TestUnknownOwnerNotMatchedByReap's assertions actually
    discriminate old behavior from new, rather than passing regardless of
    the guard's presence."""

    SCRIPT_TEXT = _PRE_UNIT2_SCRIPT_TEXT

    def setUp(self):
        super().setUp()
        self.holder = self._spawn_holder_process(match_reap_pattern=False)

    def test_old_script_deletes_the_unmatched_holders_socket(self):
        result = self._run()
        self.assertEqual(result.returncode, 0, result.stderr)
        content = "\n".join(self._log_lines())

        # Same reap-loop behavior either way: matched nothing.
        self.assertRegex(content, re.compile(r"reap matched none"))
        # No guard exists in this baseline -- there is no "socket-guard"
        # line to find, and (this is the point) the socket is GONE.
        self.assertNotIn("socket-guard", content)
        self.assertFalse(
            self.socket_path.exists(),
            "the pre-unit-2 baseline must unconditionally delete the socket here -- "
            "if it did NOT, this falsification control proves nothing about "
            "TestUnknownOwnerNotMatchedByReap's discriminating power",
        )
        # The live holder process itself is untouched (still running) --
        # this is exactly the silent-corruption shape of the incident: the
        # process survives, but its on-disk socket file is gone out from
        # under it, and a fresh connect() to the (stub-relaunch-recreated
        # or now-absent) path no longer reaches it.
        self.assertIsNone(self.holder.poll(), "the old script does not touch the holder process itself")
        self.assertFalse(
            self._socket_still_functional(),
            "the holder's socket file was deleted -- a fresh connect() to this "
            "path must no longer reach it (this IS the incident)",
        )


# ===========================================================================
# Fold falsification control -- proves the reviewer-fairy's REQUEST_CHANGES
# finding on PR #2687 was real, and that this fold closes it. Runs the
# IDENTICAL Zephyr scenario (unmatched live holder, PID_PATH seeded with its
# pid) against the frozen PR #2687-tip baseline, which still runs
# `rm -f "$PID_PATH"` unconditionally in the UNKNOWN-OWNER branch.
# ===========================================================================


class TestPidPathRemovalDefeatsOwnershipCheck(_SocketGuardFixture):
    """The permanent falsification control for this fold. Same fixture as
    TestUnknownOwnerNotMatchedByReap (unmatched live holder, PID_PATH
    seeded with a dead placeholder pid before the script runs -- see that
    class's docstring for why a dead pid, not the holder's own, is used),
    but against _PRE_FOLD_SCRIPT_TEXT -- the script exactly as it stood at
    PR #2687's tip 319ae6ae, i.e. WITH unit 2's own socket-ownership guard,
    but BEFORE this fold gated $PID_PATH's removal by that guard's
    verdict.

    Confirms two things test_socket_preserved_holder_alive_and_functional_reported
    and test_next_launch_backs_off_instead_of_stealing (both on
    TestUnknownOwnerNotMatchedByReap) would have gone RED against this
    baseline -- i.e. that this fold's own added assertions actually
    discriminate the fix from the bug, rather than passing regardless."""

    SCRIPT_TEXT = _PRE_FOLD_SCRIPT_TEXT

    def setUp(self):
        super().setUp()
        self.holder = self._spawn_holder_process(match_reap_pattern=False)
        self.pid_path.write_text(str(self._dead_pid()))

    @_NEEDS_GUARD_PRIMITIVES
    def test_pre_fold_script_removes_pid_path_despite_guard_refusal(self):
        result = self._run()
        self.assertEqual(result.returncode, 0, result.stderr)
        content = "\n".join(self._log_lines())

        # Sanity: this baseline DOES have unit 2's own guard -- it must
        # still correctly refuse the SOCKET_PATH delete (that part of unit
        # 2 is not what this fold is about).
        self.assertRegex(content, re.compile(r"reap matched none"))
        self.assertIn("socket-guard", content)
        self.assertTrue(
            self.socket_path.exists(),
            "sanity: the pre-fold baseline's own guard must still refuse the "
            "SOCKET_PATH delete -- if it did not, this control proves nothing "
            "about the PID_PATH-specific defect this fold closes",
        )

        # The actual defect: PID_PATH is gone despite the guard's refusal --
        # this is what TestUnknownOwnerNotMatchedByReap's
        # test_socket_preserved_holder_alive_and_functional_reported would
        # have caught (its PID_PATH assertions would go red here).
        self.assertFalse(
            self.pid_path.exists(),
            "the pre-fold baseline unconditionally removes PID_PATH even when "
            "the guard refuses the socket delete -- this IS the reviewer-fairy's "
            "REQUEST_CHANGES finding on PR #2687",
        )

    @_NEEDS_GUARD_PRIMITIVES
    def test_pre_fold_script_causes_next_launch_to_steal_the_socket(self):
        """The actual regression: with PID_PATH gone (see previous test),
        engram-surface-daemon.py's own REAL _recorded_pid_is_alive() /
        steal-or-back-off logic reads the still-alive, unmatched holder as
        "not confirmably ours" and steals SOCKET_PATH out from under it --
        reproducing the exact Zephyr incident this whole unit exists to
        stop, one function call later. This is what
        test_next_launch_backs_off_instead_of_stealing would have caught
        (it would read "stole" here instead of "backed-off")."""
        result = self._run()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(self.pid_path.exists(), "sanity: PID_PATH must be gone in this baseline")

        mod = _load_daemon_module()
        outcome = _simulate_daemon_ownership_gate(mod, self.pid_path, self.socket_path)
        self.assertEqual(
            outcome, "stole",
            "with PID_PATH unconditionally removed (the pre-fold behavior), "
            "engram-surface-daemon.py's own ownership check must read the "
            "still-alive, unmatched holder as not-confirmably-alive and steal "
            "the socket -- this is the exact regression this fold closes",
        )
        self.assertFalse(self.socket_path.exists())
        # The holder process itself survives, unreachable via the path --
        # the identical silent-corruption shape as the shell-level incident,
        # just attributed to the daemon process instead of the shell script.
        self.assertIsNone(self.holder.poll())
        self.assertFalse(self._socket_still_functional())


# ===========================================================================
# Sanity: the frozen pre-unit-2 baseline really is the byte-for-byte prior
# script, not a hand-typo'd approximation.
# ===========================================================================


class TestFrozenBaselineSanity(unittest.TestCase):
    def test_frozen_baseline_has_no_socket_guard(self):
        self.assertNotIn("socket-guard", _PRE_UNIT2_SCRIPT_TEXT)
        self.assertNotIn("_stat_identity", _PRE_UNIT2_SCRIPT_TEXT)
        self.assertIn('rm -f "$PID_PATH" "$SOCKET_PATH"', _PRE_UNIT2_SCRIPT_TEXT)

    def test_frozen_baseline_shares_the_same_reap_pattern_as_the_live_script(self):
        """The two scripts must differ ONLY in the added guard -- same reap
        pattern -- so any behavior difference the tests above observe is
        attributable to the guard alone."""
        live_text = START_SCRIPT_PATH.read_text()
        reap_re = re.compile(r'_reap_pattern="([^"]*)"')
        live_match = reap_re.search(live_text)
        frozen_match = reap_re.search(_PRE_UNIT2_SCRIPT_TEXT)
        self.assertIsNotNone(live_match)
        self.assertIsNotNone(frozen_match)
        self.assertEqual(live_match.group(1), frozen_match.group(1))

    def test_pre_fold_baseline_has_the_socket_guard_but_not_the_fold(self):
        """_PRE_FOLD_SCRIPT_TEXT must differ from the live script in
        EXACTLY the fold's own change (PID_PATH's removal gated by
        $_allow_socket_rm) -- it has unit 2's socket-ownership guard
        (unlike _PRE_UNIT2_SCRIPT_TEXT above, which has neither), but still
        carries the pre-fold unconditional `rm -f "$PID_PATH"` in the
        UNKNOWN-OWNER branch."""
        self.assertIn("socket-guard", _PRE_FOLD_SCRIPT_TEXT)
        self.assertIn("_stat_identity", _PRE_FOLD_SCRIPT_TEXT)
        self.assertIn(
            'rm -f "$PID_PATH" "$SOCKET_PATH"', _PRE_FOLD_SCRIPT_TEXT,
            "the pre-fold baseline must still have the allow-case combined delete",
        )
        # The pre-fold else-branch: unconditional PID_PATH removal, NOT
        # gated by $_allow_socket_rm. The live (post-fold) script's else
        # branch instead contains a no-op `:` -- if this baseline's else
        # branch looks the same as the live script's, this control proves
        # nothing.
        else_branch_re = re.compile(
            r'if \[ "\$_allow_socket_rm" -eq 1 \]; then\n\s*rm -f "\$PID_PATH" "\$SOCKET_PATH"\n'
            r'else\n(.*?)\nfi',
            re.DOTALL,
        )
        match = else_branch_re.search(_PRE_FOLD_SCRIPT_TEXT)
        self.assertIsNotNone(match, "could not locate the final if/else block in the pre-fold baseline")
        self.assertIn('rm -f "$PID_PATH"', match.group(1))
        self.assertNotIn(":", match.group(1).split("#")[0])  # no-op `:` must NOT be present pre-fold

    def test_pre_fold_baseline_shares_the_same_reap_pattern_as_the_live_script(self):
        live_text = START_SCRIPT_PATH.read_text()
        reap_re = re.compile(r'_reap_pattern="([^"]*)"')
        live_match = reap_re.search(live_text)
        frozen_match = reap_re.search(_PRE_FOLD_SCRIPT_TEXT)
        self.assertIsNotNone(live_match)
        self.assertIsNotNone(frozen_match)
        self.assertEqual(live_match.group(1), frozen_match.group(1))


# ===========================================================================
# #3333 follow-up -- the guard must not FAIL OPEN where GNU `stat -c` and
# /proc are absent (macOS). Contract (C1-C4), written before the fix:
#   C1  socket on disk but identity snapshot unobtainable -> never "nothing
#       on disk"; snapshot portably or refuse (keep both files, log it).
#   C2  live holder not in the reap-killed set -> keep SOCKET_PATH and
#       PID_PATH, log `socket-guard: refusing ...` -- Linux AND Darwin.
#   C3  orphan socket -> still removed (socket + pidfile). No /proc: holder
#       via `lsof`; neither /proc nor lsof -> refuse (fail closed).
#   C4  path absent -> no-op.
# These cells carry NO GNU/Linux skip: they run on whatever host has a
# usable identity + holder method, i.e. on Darwin too.
# ===========================================================================


class TestSocketGuardPortableContract(_ForceUnsupervisedMixin, _SocketGuardFixture):
    def setUp(self):
        super().setUp()
        self.pid_path.write_text(str(self._dead_pid()))
        self._seeded_pid_text = self.pid_path.read_text()

    def _install_unusable_stat(self):
        """A `stat` earlier on PATH that fails for every flag, standing in
        for a host whose stat speaks neither dialect the guard tries."""
        shim = self._fake_systemctl_dir / "stat"
        shim.write_text("#!/bin/sh\necho 'stat: unusable' >&2\nexit 1\n")
        shim.chmod(shim.stat().st_mode | stat.S_IXUSR)

    def _orphan(self):
        srv = self._bind_listening_socket()
        srv.close()
        self.assertTrue(self.socket_path.exists())

    def _assert_kept(self, content):
        self.assertTrue(self.socket_path.exists(), "socket must be kept\n" + content)
        self.assertTrue(self.pid_path.exists(), "pidfile must be kept\n" + content)
        self.assertEqual(self.pid_path.read_text(), self._seeded_pid_text)

    # -- C1 -----------------------------------------------------------------
    def test_c1_unreadable_identity_orphan_is_kept_not_treated_as_absent(self):
        self._orphan()
        self._install_unusable_stat()
        result = self._run()
        self.assertEqual(result.returncode, 0, result.stderr)
        content = "\n".join(self._log_lines())
        self._assert_kept(content)
        self.assertIn("socket-guard: refusing", content)

    def test_c1_unreadable_identity_live_holder_is_kept(self):
        holder = self._spawn_holder_process(match_reap_pattern=False)
        self._install_unusable_stat()
        result = self._run()
        self.assertEqual(result.returncode, 0, result.stderr)
        content = "\n".join(self._log_lines())
        self._assert_kept(content)
        self.assertIn("socket-guard: refusing", content)
        self.assertIsNone(holder.poll())
        self.assertTrue(self._socket_still_functional())

    # -- C2 -----------------------------------------------------------------
    @_NEEDS_LIVE_GUARD_PRIMITIVES
    def test_c2_live_unmatched_holder_keeps_socket_and_pidfile(self):
        holder = self._spawn_holder_process(match_reap_pattern=False)
        result = self._run()
        self.assertEqual(result.returncode, 0, result.stderr)
        content = "\n".join(self._log_lines())
        self.assertRegex(content, re.compile(r"reap matched none"))
        self._assert_kept(content)
        self.assertIn("socket-guard: refusing", content)
        self.assertIn(f"held by pid {holder.pid}", content)
        self.assertIsNone(holder.poll())
        self.assertTrue(self._socket_still_functional())

    # -- C3 -----------------------------------------------------------------
    @_NEEDS_LIVE_GUARD_PRIMITIVES
    @pytest.mark.skipif(
        not Path("/proc/net/unix").exists(),
        reason="no /proc: the guard is refuse-only (lsof rc 1 is UNKNOWN); see the C5 cells",
    )
    def test_c3_orphan_socket_and_pidfile_are_removed(self):
        self._orphan()
        result = self._run()
        self.assertEqual(result.returncode, 0, result.stderr)
        content = "\n".join(self._log_lines())
        self.assertNotIn("socket-guard", content)
        self.assertTrue(self._wait_for_relaunch_marker())
        self.assertFalse(self.socket_path.exists(), "orphan socket must be removed")
        self.assertFalse(self.pid_path.exists(), "pidfile must be removed with the orphan")

    @pytest.mark.skipif(
        Path("/proc/net/unix").exists(),
        reason="host has /proc; this cell is the no-/proc-no-lsof case",
    )
    def test_c3_no_proc_and_no_lsof_refuses_fail_closed(self):
        self._orphan()
        # PATH without lsof (macOS keeps it in /usr/sbin, not /usr/bin).
        if shutil.which("lsof", path=f"{self._fake_systemctl_dir}:/usr/bin:/bin"):
            self.skipTest("lsof is on the minimal PATH of this host")
        result = self._run(extra_env={"PATH": f"{self._fake_systemctl_dir}:/usr/bin:/bin"})
        self.assertEqual(result.returncode, 0, result.stderr)
        content = "\n".join(self._log_lines())
        self._assert_kept(content)
        self.assertIn("socket-guard: refusing", content)
        self.assertIn("lsof", content)

    # -- C3b: lsof rc 0 with NO pid is not "orphan" -----------------------
    # CONTRACT: only rc 1 + empty stdout means orphan; rc 0 + no pid
    # contradicts itself, so keep socket AND pidfile and log a refusal.
    def _install_lsof_rc0_no_pid(self):
        shim = self._fake_systemctl_dir / "lsof"
        shim.write_text("#!/bin/sh\nexit 0\n")
        shim.chmod(shim.stat().st_mode | stat.S_IXUSR)

    @pytest.mark.skipif(
        Path("/proc/net/unix").exists(),
        reason="host has /proc; the lsof branch is only reached without it",
    )
    def test_c3b_lsof_rc0_no_pid_orphan_socket_is_kept(self):
        self._orphan()
        self._install_lsof_rc0_no_pid()
        result = self._run()
        self.assertEqual(result.returncode, 0, result.stderr)
        content = "\n".join(self._log_lines())
        self._assert_kept(content)
        self.assertIn("socket-guard: refusing", content)

    @pytest.mark.skipif(
        Path("/proc/net/unix").exists(),
        reason="host has /proc; the lsof branch is only reached without it",
    )
    def test_c3b_lsof_rc0_no_pid_live_decoy_holder_is_kept(self):
        holder = self._spawn_holder_process(match_reap_pattern=False)
        self._install_lsof_rc0_no_pid()
        result = self._run()
        self.assertEqual(result.returncode, 0, result.stderr)
        content = "\n".join(self._log_lines())
        self._assert_kept(content)
        self.assertIn("socket-guard: refusing", content)
        self.assertIsNone(holder.poll())
        self.assertTrue(self._socket_still_functional())

    # -- C5: lsof rc 1 + empty stdout is NOT by itself an orphan -----------
    # CONTRACT (revised): on the no-/proc (lsof) branch lsof rc 1 is UNKNOWN,
    # never "orphan" (incomplete searches also exit 1; a connect() probe
    # cannot prove absence). Removal needs rc 0 with every pid a reap-killed
    # pid that has exited. Orphan cleanup is unavailable there: refuse.
    def _install_lsof(self, *, stderr_text="", rc=1, stdout_text=""):
        shim = self._fake_systemctl_dir / "lsof"
        lines = ["#!/bin/sh"]
        if stdout_text:
            lines.append(f"echo {stdout_text}")
        if stderr_text:
            lines.append(f"echo '{stderr_text}' >&2")
        lines.append(f"exit {rc}")
        shim.write_text("\n".join(lines) + "\n")
        shim.chmod(shim.stat().st_mode | stat.S_IXUSR)

    _NO_PROC = pytest.mark.skipif(
        Path("/proc/net/unix").exists(),
        reason="host has /proc; the lsof branch is only reached without it",
    )

    @_NO_PROC
    def test_c5_i_lsof_rc1_empty_stdout_with_stderr_live_holder_is_kept(self):
        holder = self._spawn_holder_process(match_reap_pattern=False)
        self._install_lsof(stderr_text="lsof: WARNING: Permission denied")
        result = self._run()
        self.assertEqual(result.returncode, 0, result.stderr)
        content = "\n".join(self._log_lines())
        self._assert_kept(content)
        self.assertIn("socket-guard: refusing", content)
        self.assertIsNone(holder.poll())
        self.assertTrue(self._socket_still_functional())

    @_NO_PROC
    def test_c5_ii_lsof_blind_rc1_empty_both_live_holder_is_kept(self):
        holder = self._spawn_holder_process(match_reap_pattern=False)
        self._install_lsof()
        result = self._run()
        self.assertEqual(result.returncode, 0, result.stderr)
        content = "\n".join(self._log_lines())
        self._assert_kept(content)
        self.assertIn("socket-guard: refusing", content)
        self.assertIsNone(holder.poll())
        self.assertTrue(self._socket_still_functional())

    @_NO_PROC
    def test_c5_iii_lsof_rc1_empty_both_genuine_orphan_is_kept_with_reason(self):
        # Deliberately retires "orphan removed on no-/proc hosts".
        self._orphan()
        self._install_lsof()
        result = self._run()
        self.assertEqual(result.returncode, 0, result.stderr)
        content = "\n".join(self._log_lines())
        self._assert_kept(content)
        self.assertIn("socket-guard: refusing", content)
        self.assertIn("lsof exited 1", content)

    # -- C6: a signaled pid is exempt only once it has actually exited -----
    def _reap_in_background(self, proc):
        # The holder is this test's child; the script kills it, and a
        # zombie still answers kill(pid, 0). Reap it like a real parent.
        t = threading.Thread(target=proc.wait, daemon=True)
        t.start()

    @_NO_PROC
    def test_c6_iv_killed_pid_that_ignores_sigterm_is_still_a_live_holder(self):
        holder = self._spawn_holder_process(match_reap_pattern=True, ignore_sigterm=True)
        self._install_lsof(stdout_text=str(holder.pid), rc=0)
        result = self._run(timeout=20.0)
        self.assertEqual(result.returncode, 0, result.stderr)
        content = "\n".join(self._log_lines())
        self.assertRegex(content, re.compile(r"reap matched 1 stale process\(es\):"))
        self._assert_kept(content)
        self.assertIn("socket-guard: refusing", content)
        self.assertIn("still alive", content)
        self.assertIsNone(holder.poll(), "the SIGTERM-ignoring holder must still be running")

    @_NO_PROC
    def test_c6_v_killed_pid_that_exits_is_exempt_and_socket_removed(self):
        holder = self._spawn_holder_process(match_reap_pattern=True)
        self._reap_in_background(holder)
        self._install_lsof(stdout_text=str(holder.pid), rc=0)
        result = self._run(timeout=20.0)
        self.assertEqual(result.returncode, 0, result.stderr)
        content = "\n".join(self._log_lines())
        self.assertNotIn("socket-guard", content)
        self.assertTrue(self._wait_for_relaunch_marker())
        self.assertFalse(self.socket_path.exists(), "socket held by an exited killed pid must be removed")
        self.assertFalse(self.pid_path.exists())

    # -- C6 on the /proc branch (#3360): same "signaled is not killed" rule --
    # Linux only (needs /proc/net/unix); UNRUN on Darwin -- needs a Linux run.
    _HAS_PROC = pytest.mark.skipif(
        not Path("/proc/net/unix").exists(),
        reason="the /proc holder branch is only reached on hosts with /proc/net/unix",
    )

    @_HAS_PROC
    def test_c6_proc_iv_killed_pid_that_ignores_sigterm_is_still_a_live_holder(self):
        holder = self._spawn_holder_process(match_reap_pattern=True, ignore_sigterm=True)
        result = self._run(timeout=20.0)
        self.assertEqual(result.returncode, 0, result.stderr)
        content = "\n".join(self._log_lines())
        self.assertRegex(content, re.compile(r"reap matched 1 stale process\(es\):"))
        self._assert_kept(content)
        self.assertIn("still alive", content)
        self._assert_no_launch(content)
        self.assertIsNone(holder.poll(), "the SIGTERM-ignoring holder must still be running")

    @_HAS_PROC
    def test_c6_proc_v_killed_pid_that_exits_is_exempt_and_socket_removed(self):
        holder = self._spawn_holder_process(match_reap_pattern=True)
        self._reap_in_background(holder)
        result = self._run(timeout=20.0)
        self.assertEqual(result.returncode, 0, result.stderr)
        content = "\n".join(self._log_lines())
        self.assertNotIn("socket-guard", content)
        self.assertTrue(self._wait_for_relaunch_marker())
        self.assertFalse(self.socket_path.exists(), "socket held by an exited killed pid must be removed")
        self.assertFalse(self.pid_path.exists())

    # -- C6 shared-FD (Mira): EVERY holder of the inode must be judged -----
    # CONTRACT: on the /proc branch removal is authorized only if every
    # process holding every matching inode is a reap-killed pid that has
    # exited. A listening socket FD inherited by an exec'd child that the
    # reap pattern does NOT match must keep socket + pidfile and block the
    # launch, even though the reap-matched parent holding the same inode
    # exits. UNRUN on Darwin (needs /proc/net/unix): a Linux run is required.
    def _spawn_shared_fd_parent(self, *, child_matches_reap_pattern: bool, barrier: Path | None):
        """A reap-matched parent that binds+listens on the guard's
        SOCKET_PATH, then spawns a child inheriting the listening FD. Returns
        (parent_popen, child_pid). With a barrier path the parent exits only
        after (a) SIGTERM arrived and (b) that file exists, so the test
        controls WHEN the parent dies relative to the guard's holder
        enumeration. Without one it dies on SIGTERM at once (SIGCHLD ignored
        so a dying child never lingers as a zombie)."""
        src = (
            "import os, signal, socket, subprocess, sys, time\n"
            "home = sys.argv[-1]\n"
            f"path = {str(self.socket_path)!r}\n"
            f"barrier = {str(barrier) if barrier else ''!r}\n"
            "signal.signal(signal.SIGCHLD, signal.SIG_IGN)\n"
            "got = []\n"
            "if barrier:\n"
            "    signal.signal(signal.SIGTERM, lambda *a: got.append(1))\n"
            "s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)\n"
            "s.bind(path)\n"
            "s.listen(1)\n"
            "argv = [sys.executable, '-c', 'import time; time.sleep(30)']\n"
            f"if {child_matches_reap_pattern!r}:\n"
            "    argv += ['engram-surface-daemon.py', home]\n"
            "child = subprocess.Popen(argv, pass_fds=[s.fileno()], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL)\n"
            "print(child.pid, flush=True)\n"
            "while True:\n"
            "    if barrier and got and os.path.exists(barrier):\n"
            "        os._exit(0)\n"
            "    time.sleep(0.02)\n"
        )
        parent = subprocess.Popen(
            [sys.executable, "-c", src, "engram-surface-daemon.py", str(self.engram_home)],
            stdout=subprocess.PIPE, text=True,
        )
        self._extra_cleanups.append(lambda: self._kill_proc(parent))
        child_pid = int(parent.stdout.readline().strip())

        def _kill_child():
            try:
                os.kill(child_pid, 9)
            except OSError:
                pass

        self._extra_cleanups.append(_kill_child)
        deadline = time.time() + 5.0
        while time.time() < deadline and not self.socket_path.exists():
            time.sleep(0.05)
        self.assertTrue(self.socket_path.exists(), "test setup: parent did not bind in time")
        self._reap_in_background(parent)
        return parent, child_pid

    @_HAS_PROC
    def test_c6_proc_vi_shared_fd_unmatched_child_keeps_socket_when_parent_exits(self):
        barrier = self.tmp / "holder-selected.barrier"
        # Barrier WITHOUT a hook in the shipped script: patch the TEMP COPY
        # the test runs, right after the guard's holder enumeration/selection
        # (the fixed script's `holders = find_holders(inode)`, or the
        # pre-fix `holder_pid, was_killed = find_holder(inode)`), so the
        # parent exits only AFTER the guard has chosen what to judge. Exactly
        # one match, so the patch cannot silently go stale.
        text = self.script.read_text()
        pattern = re.compile(
            r"^    (?:holders = find_holders|holder_pid, was_killed = find_holder)\(inode\)\n",
            re.MULTILINE,
        )
        self.assertEqual(len(pattern.findall(text)), 1)
        patched, n = pattern.subn(
            lambda m: m.group(0) + f"    open({str(barrier)!r}, 'w').close()\n", text,
        )
        self.assertEqual(n, 1)
        self.script.write_text(patched)

        parent, child_pid = self._spawn_shared_fd_parent(
            child_matches_reap_pattern=False, barrier=barrier,
        )
        result = self._run(timeout=20.0)
        self.assertEqual(result.returncode, 0, result.stderr)
        content = "\n".join(self._log_lines())
        self.assertRegex(content, re.compile(r"reap matched 1 stale process\(es\):"))
        self.assertTrue(barrier.exists(), "the guard never reached holder enumeration\n" + content)
        self.assertTrue(self._wait_for(lambda: parent.poll() is not None), "the reap-matched parent must have exited")
        # The parent (the reap-matched holder) is gone; the unmatched child
        # still holds the inherited listener.
        os.kill(child_pid, 0)  # raises if the child is not alive
        self._assert_kept(content)
        self._assert_no_launch(content)
        self.assertIn(f"held by pid {child_pid}", content)
        self.assertTrue(self._socket_still_functional())

    @_HAS_PROC
    def test_c6_proc_vii_control_two_killed_holders_both_exit_removes_and_launches(self):
        parent, child_pid = self._spawn_shared_fd_parent(
            child_matches_reap_pattern=True, barrier=None,
        )
        result = self._run(timeout=20.0)
        self.assertEqual(result.returncode, 0, result.stderr)
        content = "\n".join(self._log_lines())
        self.assertRegex(content, re.compile(r"reap matched 2 stale process\(es\):"))
        self.assertNotIn("socket-guard", content)
        self.assertTrue(self._wait_for_relaunch_marker())
        self.assertFalse(self.socket_path.exists())
        self.assertFalse(self.pid_path.exists())

    @staticmethod
    def _wait_for(pred, timeout: float = 5.0) -> bool:
        deadline = time.time() + timeout
        while time.time() < deadline:
            if pred():
                return True
            time.sleep(0.05)
        return False

    # -- C7: BSD `stat -f` fallback output must be dev:inode ---------------
    def test_c7_stat_f_multiline_junk_is_not_an_identity(self):
        self._orphan()
        shim = self._fake_systemctl_dir / "stat"
        shim.write_text(
            "#!/bin/sh\n"
            'case "$1" in\n'
            "  -c) echo 'stat: illegal option' >&2; exit 1;;\n"
            "  -f) printf '  File: x\\n    ID: 1 Namelen: 255\\n'; exit 0;;\n"
            "esac\nexit 1\n"
        )
        shim.chmod(shim.stat().st_mode | stat.S_IXUSR)
        result = self._run()
        self.assertEqual(result.returncode, 0, result.stderr)
        content = "\n".join(self._log_lines())
        self._assert_kept(content)
        self.assertIn("socket-guard: refusing", content)
        # Discriminating: junk must be rejected at the identity step (C1
        # reason). If the C7 pattern were removed the junk would be accepted
        # as an identity, fall through to the holder check, and still log
        # "refusing" -- but never this C1-only reason text.
        self.assertIn("on-disk identity could not be read", content)

    # -- C8: UNKNOWN OWNER -> no launch, no launch-attempt stamp, one line --
    # CONTRACT: whenever the guard refuses (any branch), the hook must not
    # start a daemon (its own startup gate would steal the socket from the
    # live holder), must not stamp daemon-launch-attempt, must log exactly
    # ONE `socket-guard:` line naming the holder (when known) and the
    # restore path, and must exit 0.
    def _assert_no_launch(self, content):
        # The launch is backgrounded: give a (wrongly) started stub time to
        # write its marker before asserting its absence.
        time.sleep(1.5)
        self.assertFalse(self._relaunch_happened(), "no daemon may be launched\n" + content)
        self.assertFalse(
            (self.engram_home / "daemon-launch-attempt").exists(),
            "no launch attempt may be stamped",
        )
        lines = [ln for ln in self._log_lines() if "socket-guard:" in ln]
        self.assertEqual(len(lines), 1, lines)
        self.assertIn("NOT launching", lines[0])
        self.assertIn("by hand", lines[0])

    @_NEEDS_LIVE_GUARD_PRIMITIVES
    def test_c8_live_unmatched_holder_dead_pid_no_launch_no_stamp(self):
        holder = self._spawn_holder_process(match_reap_pattern=False)
        result = self._run()
        self.assertEqual(result.returncode, 0, result.stderr)
        content = "\n".join(self._log_lines())
        self._assert_kept(content)
        self._assert_no_launch(content)
        self.assertIn(f"held by pid {holder.pid}", content)
        self.assertIsNone(holder.poll())
        self.assertTrue(self._socket_still_functional())

    @_NEEDS_LIVE_GUARD_PRIMITIVES
    def test_c8_live_unmatched_holder_missing_pidfile_no_launch_no_stamp(self):
        holder = self._spawn_holder_process(match_reap_pattern=False)
        self.pid_path.unlink()
        result = self._run()
        self.assertEqual(result.returncode, 0, result.stderr)
        content = "\n".join(self._log_lines())
        self.assertTrue(self.socket_path.exists())
        self._assert_no_launch(content)
        self.assertIn(f"held by pid {holder.pid}", content)

    def test_c8_unreadable_identity_no_launch_no_stamp(self):
        self._orphan()
        self._install_unusable_stat()
        result = self._run()
        self.assertEqual(result.returncode, 0, result.stderr)
        content = "\n".join(self._log_lines())
        self._assert_kept(content)
        self._assert_no_launch(content)

    def test_c8_holder_check_nonzero_with_no_output_names_the_rc(self):
        # The holder check's interpreter cannot run (rc 127, no stdout): the
        # refusal reason must not be empty -- it must name the exit code.
        self._orphan()
        text = self.script.read_text()
        marker = '_holder_output="$(' + sys.executable + " -c"
        self.assertEqual(text.count(marker), 1)
        self.script.write_text(
            text.replace(marker, '_holder_output="$(/nonexistent/python-for-holder-check -c')
        )
        result = self._run()
        self.assertEqual(result.returncode, 0, result.stderr)
        content = "\n".join(self._log_lines())
        self._assert_kept(content)
        self._assert_no_launch(content)
        self.assertIn("holder check exited rc=", content)

    @pytest.mark.skipif(
        not Path("/proc/net/unix").exists(),
        reason="negative control needs the /proc orphan cleanup (Linux)",
    )
    def test_c8_control_genuine_orphan_is_removed_and_launches(self):
        self._orphan()
        result = self._run()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(self._wait_for_relaunch_marker())
        self.assertFalse(self.socket_path.exists())
        self.assertTrue((self.engram_home / "daemon-launch-attempt").exists())


    # -- C8b: the refusal is ONE line whatever the holder output contains ---
    # CONTRACT: exactly one `socket-guard:` line (no embedded newline/CR/tab),
    # always ending with the restore instruction from the template; the
    # embedded holder text is whitespace-normalized and capped at ~300 chars.
    def _install_ps_shim(self, body: str):
        shim = self._fake_systemctl_dir / "ps"
        shim.write_text("#!/bin/sh\n" + body)
        shim.chmod(shim.stat().st_mode | stat.S_IXUSR)

    def _assert_single_bounded_line(self):
        raw = self.log_path.read_text()
        guard = [ln for ln in raw.splitlines() if "socket-guard:" in ln]
        self.assertEqual(len(guard), 1, raw)
        # no continuation lines: every physical log line is a stamped entry
        # (a split holder cmdline would leave bare, unstamped fragments)
        for ln in raw.splitlines():
            self.assertRegex(ln, r"^\[\d{4}-\d\d-\d\dT[\d:]+Z\] \[start-engram-daemon\] ", raw)
        self.assertNotIn("\r", raw)
        self.assertTrue(
            guard[0].endswith("then re-run the hook"), "restore text must end the line: " + guard[0][-120:]
        )
        return guard[0]

    # cmdline source covered: `ps -o command=` (lsof branch, shim ps + shim lsof)
    @_NO_PROC
    def test_c8b_ps_multiline_holder_command_is_one_log_line(self):
        holder = self._spawn_holder_process(match_reap_pattern=False)
        self._install_lsof(stdout_text=str(holder.pid), rc=0)
        self._install_ps_shim("printf 'python3 -c import os\\n\\tx = 1\\r\\n  y = 2\\n'\n")
        result = self._run()
        self.assertEqual(result.returncode, 0, result.stderr)
        content = "\n".join(self._log_lines())
        self._assert_kept(content)
        self._assert_no_launch(content)
        line = self._assert_single_bounded_line()
        self.assertIn(f"held by pid {holder.pid} (python3 -c import os x = 1 y = 2)", line)

    # cmdline source covered: `ps -o command=` (lsof branch, shim ps + shim lsof)
    @_NO_PROC
    def test_c8b_ps_huge_holder_command_is_capped_and_restore_text_survives(self):
        holder = self._spawn_holder_process(match_reap_pattern=False)
        self._install_lsof(stdout_text=str(holder.pid), rc=0)
        self._install_ps_shim("printf 'python3 -c '; head -c 2000 /dev/zero | tr '\\0' 'A'; printf '\\n'\n")
        result = self._run()
        self.assertEqual(result.returncode, 0, result.stderr)
        content = "\n".join(self._log_lines())
        self._assert_kept(content)
        self._assert_no_launch(content)
        line = self._assert_single_bounded_line()
        self.assertIn("by hand", line)
        self.assertIn("...", line)
        self.assertNotIn("A" * 301, line)
        self.assertLess(len(line), 1000, len(line))

    # cmdline source covered: /proc/<pid>/cmdline on Linux, real `ps` on macOS
    # (the holder's own `python -c "<multi-line script>"` is a real
    # multi-line cmdline; no shim, no override).
    @_NEEDS_LIVE_GUARD_PRIMITIVES
    def test_c8b_real_multiline_holder_cmdline_is_one_log_line(self):
        holder = self._spawn_holder_process(match_reap_pattern=False)
        result = self._run()
        self.assertEqual(result.returncode, 0, result.stderr)
        content = "\n".join(self._log_lines())
        self._assert_kept(content)
        self._assert_no_launch(content)
        self._assert_single_bounded_line()
        self.assertIsNone(holder.poll())

    # cmdline source covered: /proc/<pid>/cmdline on Linux, real `ps` on macOS
    # (a 3000-char extra argv on the real holder).
    @_NEEDS_LIVE_GUARD_PRIMITIVES
    def test_c8b_real_huge_holder_cmdline_is_capped(self):
        holder = self._spawn_holder_process(match_reap_pattern=False, pad_argv="B" * 3000)
        result = self._run()
        self.assertEqual(result.returncode, 0, result.stderr)
        content = "\n".join(self._log_lines())
        self._assert_kept(content)
        self._assert_no_launch(content)
        line = self._assert_single_bounded_line()
        self.assertNotIn("B" * 301, line)
        self.assertLess(len(line), 1000, len(line))
        self.assertIsNone(holder.poll())

    # -- C4 -----------------------------------------------------------------
    def test_c4_absent_path_is_a_noop_even_with_unusable_stat(self):
        self.pid_path.unlink()
        self._install_unusable_stat()
        self.assertFalse(self.socket_path.exists())
        result = self._run()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("socket-guard", "\n".join(self._log_lines()))
        self.assertTrue(self._wait_for_relaunch_marker())


if __name__ == "__main__":
    unittest.main()
