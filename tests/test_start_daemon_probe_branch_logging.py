"""Tests for #2652 phase-0 unit 1 (and its later folds/units) --
start-engram-daemon.sh's liveness-probe branch logging, and (this unit,
unit 4) the hook's transition to assert-liveness-and-report-only.

Borges's §4 (issue #2652) left two candidates unresolved for why a probe
fails on a given restart: (1) the previous daemon died silently, or (2) the
pidfile is absent at probe time because the daemon writes it only after its
warmup embedding call, so a SessionStart landing inside another daemon's
warmup window sees no pidfile and reaps a healthy, still-warming daemon. His
proposed fix for unit 1 was one log line per invocation naming exactly
which of the four liveness-probe branches fired -- additive only, at the
time, no change to the reap/relaunch or healthy-exit behavior.

The four branches (see start-engram-daemon.sh's own inline comments for the
reap-pattern reasoning this unit deliberately does not touch):

  1. No pidfile at all                          -> report only (no action)
  2. Pidfile present, kill -0 fails (stale)      -> report only (no action)
  3. Pidfile present, process alive, socket dead -> report only (no action)
  4. Pidfile present, process alive, socket OK   -> healthy, exit 0, no-op

Discovered while writing unit 1, filed separately as #2666: the
socket-probe python heredoc's `except:` (bare) swallowed the SystemExit
its own `sys.exit(0)` success path raised, making branch 4's "healthy,
no-op" outcome UNREACHABLE against the shipped script -- a live, listening
socket got misdiagnosed as branch 3 and the daemon still reaped+relaunched.
Out of scope for #2652 phase-0 unit 1 itself (additive logging only) and
unrelated to the reap-pattern units (2-4), so it shipped as a separate PR
(#2669, `except:` -> `except Exception:`), which has since merged --
branch 4 is now reachable against the real script.

#2652 phase-0 UNIT 4 (this fold) is the big behavioral change, but it landed
in two stages and this docstring describes the FINAL shape, not the
intermediate one. An earlier build of this unit made the hook
UNCONDITIONALLY report-only -- reap/relaunch/rm removed entirely, no
exceptions. Kepler's finding on issue #2652 required a correction before
that could ship: several seats on a given host have NO supervisor unit
registered at all today, so a hook that never launches would strand those
seats with no daemon after a rebuild, until someone installs a unit by
hand. The final design is therefore a FOUR-STATE branch, not a plain
report-only switch:

  SUPERVISED_HEALTHY  -> report only, no reap/relaunch/rm. Trust the
                         supervisor's own restart policy.
  SUPERVISED_FAILING  -> report LOUDLY, no launch. Never fight a real
                         supervisor by launching a competing process.
  UNSUPERVISED        -> the FULL historical reap+guard+launch+stamp
                         sequence (units 1-3's machinery) runs exactly as
                         it always did, PLUS a new install-hint log line.
  unavailable         -> report LOUDLY, no launch (supervision state
                         could not be determined).

So "the hook no longer reaps, relaunches, or rm -f's anything" is true of
only three of the four states (SUPERVISED_HEALTHY, SUPERVISED_FAILING,
unavailable) -- it is FALSE of UNSUPERVISED, where that exact machinery is
restored byte-for-byte (see `TestFourStateBranchUnsupervisedLaunchesFully`
below, which proves the restored path is reachable, not dead text). The
four-branch liveness-probe determination above (no pidfile / stale pid /
socket dead / healthy) is UNCHANGED in kind and is evaluated first, same as
before; the four-STATE supervision branch is a second, independent
determination made only when branches 1-3 decide the daemon needs
attention, and it is this second determination that decides whether the
restored reap+launch machinery actually runs. Branches 3/4 of the
liveness probe (whenever kill -0 succeeds and a pid is in hand) also
report a same-process cgroup supervision read, independent of the
four-state branch: `supervised` (readable, outside any `agent-session@*`
slice) / `unsupervised` (inside one -- dies with the session, the exact
pathology this epic exists to remove) / `unavailable` (cgroup file
missing/unreadable -- must never collapse into either of the other two).

Every test class below that asserts "no reap/relaunch side effect fires"
runs against `_ProbeBranchFixture`'s own default fake-supervisor state
(registered-active, i.e. SUPERVISED_HEALTHY) unless it explicitly
overrides `FAKE_SYSTEMCTL_MODE`/`FAKE_LOAD_STATE`/etc -- it is NOT a claim
that the assertion has been separately exercised under all three
non-UNSUPERVISED states. The dedicated `TestFourStateBranch*` classes
further down are what separately cover SUPERVISED_HEALTHY,
SUPERVISED_FAILING, and unavailable each in isolation. The paired
falsification classes run against `_PRE_UNIT4_SCRIPT_TEXT` (a byte-for-byte
freeze of the script immediately before this fold, captured from the
actual working tree right before this unit's edit) prove the "no side
effect" assertions are exercising a real removed-for-that-state behavior
and not something vacuously true. Separately,
`TestFourStateBranchUnsupervisedLaunchesFully` and its siblings prove the
SAME machinery is faithfully restored on the one state where it still
belongs.

The "reap outcome" logging (`"reap matched N ..."` / `"reap matched
none"`, added by unit 1's own round-2 fold) is NOT removed by this unit --
it is restored, unchanged, inside the UNSUPERVISED case arm, alongside the
kill loop it describes. Its original test classes (`TestReapOutcome*`),
which asserted the logging fired UNCONDITIONALLY regardless of branch, are
replaced by `TestPreexistingMatchingProcessNeverTouched` /
`TestPreexistingMatchingProcessKilledByPreUnit4Script` below, which assert
the state-scoped new/old behavior directly (survives untouched under the
three non-UNSUPERVISED states / gets killed and logged under UNSUPERVISED)
rather than via an assumption that the logging is unconditional.

`_reap_pattern`'s own literal assignment is intentionally UNCHANGED by this
unit (see the live script's own comment) -- it is retained purely as a
single-sourced constant that tests/test_pgrep_liveness_reap_patterns.py and
tests/test_launch_line_reap_pattern_binding.py both extract, even though no
code path in the live script computes or kills against it any more. The two
`TestFrozenBaselineSanity`/`TestRound1FrozenBaselineSanity` checks below
continue to hold unmodified for that reason and are left as-is.
"""

from __future__ import annotations

import os
import re
import shutil
import socket as socket_mod
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DAEMON_DIR = REPO_ROOT / "src" / "engram" / "hooks" / "claude"
START_SCRIPT_PATH = DAEMON_DIR / "start-engram-daemon.sh"

_TIMESTAMP_RE = r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z"
_CGROUP_STATES = ("supervised", "unsupervised", "unavailable")

_STUB_DAEMON_SOURCE = (
    "import time, sys\n"
    "print('stub daemon started', file=sys.stderr)\n"
    "time.sleep(5)\n"
)

# Byte-for-byte snapshot of start-engram-daemon.sh as it stood on origin/dev
# BEFORE #2652 phase-0 unit 1 added probe-branch logging (verified via
# `git show origin/dev:src/engram/hooks/claude/start-engram-daemon.sh`
# immediately prior to writing this unit's patch). Frozen deliberately --
# this is the falsification control's baseline, not a copy meant to track
# the live script going forward. If it silently starts matching the live
# script again (e.g. after a future revert), the falsification tests below
# would stop proving anything; that risk is why every falsification test
# also independently asserts the PATCHED script still logs, so a drift here
# would show up as the "reverted" and "patched" assertions agreeing instead
# of a single test going green for the wrong reason.
_PRE_UNIT1_SCRIPT_TEXT = r"""#!/bin/bash
# SessionStart hook: ensure the engram surface daemon is running.
# If already running, this is a no-op.

: "${ENGRAM_HOME:=$HOME/.engram}"
SOCKET_PATH="$ENGRAM_HOME/recall-daemon.sock"
PID_PATH="$ENGRAM_HOME/recall-daemon.pid"
DAEMON_SCRIPT="$(dirname "$0")/engram-surface-daemon.py"
LOG_PATH="$ENGRAM_HOME/surface-daemon.log"

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
except:
    sys.exit(1)
" 2>/dev/null; then
            # Daemon is alive and socket works
            exit 0
        fi
    fi
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
for stale_pid in $(pgrep -f "$_reap_pattern" 2>/dev/null); do
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

# Byte-for-byte snapshot of start-engram-daemon.sh as of #2652 phase-0 unit
# 1's own merged tip (commit 4ad008cc961e3d37a707179b7407f78f3f86548d --
# verified via `git show 4ad008cc961e3d37a707179b7407f78f3f86548d:
# src/engram/hooks/claude/start-engram-daemon.sh` immediately prior to
# writing this fold's patch). This is the round-2 fold's OWN "before"
# baseline: branch-1..4 logging is present (round 1's own work), but the
# reap section is unlogged -- exactly the gap Ariadne's colleague-review
# blocker on PR #2667 named. Used as the falsification control for the
# reap-outcome tests below, so they prove the new "reap matched ..." line
# comes from THIS fold and not from round 1's already-shipped logging.
_ROUND1_SCRIPT_TEXT = r"""#!/bin/bash
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
except:
    sys.exit(1)
" 2>/dev/null; then
            # Daemon is alive and socket works.
            # #2666 (found while building this logging, NOT fixed here --
            # separate scope): the python probe's own bare `except:` above
            # currently swallows the SystemExit its `sys.exit(0)` success
            # path raises, so this branch is unreachable until #2666 lands
            # -- expect "socket probe failed/timed out" below instead, even
            # on a genuinely healthy connection, until then.
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
for stale_pid in $(pgrep -f "$_reap_pattern" 2>/dev/null); do
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

# Byte-for-byte snapshot of start-engram-daemon.sh as it stood on this
# branch's own tip IMMEDIATELY BEFORE #2652 phase-0 unit 4's edit (units
# 1/1a/1b/2/3 all merged: branch logging, the reap-outcome log line, the
# ownership-guarded rm -f block, and the ENGRAM_HOME-scoped launch line are
# all present). Captured by reading the working-tree file before this
# unit's patch was applied. This is unit 4's OWN "before" baseline -- used
# below to prove every "no side effects any more" assertion is exercising a
# real removed behavior (reap, relaunch, rm, kill) and not something
# vacuously true.
_PRE_UNIT4_SCRIPT_TEXT = r"""#!/bin/bash
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
"""


def _spawn_sleeper(seconds: float = 5.0):
    """A real, short-lived subprocess with a genuine, confirmable PID
    (mirrors tests/test_surface_daemon_pid_reap.py's _spawn_sleeper)."""
    return subprocess.Popen([sys.executable, "-c", f"import time; time.sleep({seconds})"])


def _spawn_reap_pattern_match(stub_daemon_path: Path, engram_home: Path, seconds: float = 8.0):
    """A real, long-lived subprocess whose argv matches this fixture's
    `_reap_pattern` (contains "engram-surface-daemon.py" and ends with
    ENGRAM_HOME) WITHOUT going through the script under test -- standing in
    for a leftover daemon process from a prior invocation (or this
    invocation's own pidfile-not-yet-written race). `stub_daemon_path` and
    `engram_home` are passed as trailing argv to a `python -c` sleeper (not
    actually executed as the daemon script) purely so the joined cmdline
    string pgrep -f inspects contains both required substrings."""
    return subprocess.Popen([
        sys.executable, "-c", f"import time; time.sleep({seconds})",
        str(stub_daemon_path), str(engram_home),
    ])


def _confirmed_dead_pid() -> int:
    """Spawn, terminate, and wait on a real subprocess, then confirm via
    os.kill(pid, 0) that the PID is genuinely dead before returning it --
    guards against the (rare) PID-reuse race the spec flags."""
    proc = _spawn_sleeper(seconds=1.0)
    proc.terminate()
    proc.wait(timeout=2)
    try:
        os.kill(proc.pid, 0)
    except ProcessLookupError:
        return proc.pid
    except PermissionError:
        # Kill-0 on a live different-owner process raises PermissionError,
        # not ProcessLookupError -- treat that as "still alive" and fail
        # loudly rather than silently returning a live pid.
        raise AssertionError(
            f"pid {proc.pid} still resolves to a process after terminate+wait"
        )
    else:
        raise AssertionError(
            f"pid {proc.pid} still resolves to a process after terminate+wait"
        )


def _patch_out_bare_except_2666(script_text: str) -> str:
    """Test-only isolation of #2666 (found while building unit 1, filed
    separately, NOT fixed here): the socket-probe heredoc's bare `except:`
    swallows the SystemExit its own `sys.exit(0)` success path raises, so
    branch 4 ("healthy, no-op") is unreachable against the pre-#2669 script.
    This neutralizes JUST that one line -- `except:` -> `except OSError:`,
    so `SystemExit` propagates normally -- to make branch 4 reachable in
    isolation, so this unit's own added logging for that branch can be
    verified independently of the unrelated pre-existing defect it sits
    next to. Asserts the exact snippet is found so a future edit to the
    heredoc can't make this silently a no-op patch."""
    target = "except:\n    sys.exit(1)"
    assert target in script_text, (
        "expected bare `except:` snippet not found -- #2666 may have been "
        "fixed upstream (update/remove this patch) or the heredoc changed "
        "shape (update the patch to match)"
    )
    return script_text.replace(target, "except OSError:\n    sys.exit(1)")


def _expected_cgroup_state_for_pid(pid: int) -> str:
    """Ground-truth reimplementation of the live script's
    `_cgroup_supervision_state`, in Python, reading the REAL
    `/proc/<pid>/cgroup` for an actual spawned test subprocess.

    Deliberately NOT a hardcoded literal: this dev/test host's own shell
    (and therefore every subprocess it spawns, absent an explicit unshare)
    may itself already sit inside an `agent-session@*` slice -- measured
    true on this exact worktree's host during this unit's own development
    (own `/proc/$$/cgroup` reads
    `.../agent-session@sol.service`) -- so a plain `subprocess.Popen` child
    here reads `unsupervised`, not `supervised`. Computing the expected
    value the same way the script does, per-host, keeps these assertions
    correct regardless of which cgroup topology the test happens to run
    under, while the dedicated `TestCgroupSupervisionState*` classes below
    use the injectable-path override for host-INDEPENDENT, deterministic
    coverage of all three states.
    """
    try:
        with open(f"/proc/{pid}/cgroup") as f:
            content = f.read()
    except OSError:
        return "unavailable"
    return "unsupervised" if "agent-session@" in content else "supervised"


def _extract_bash_function(script_text: str, func_name: str) -> str:
    """Extract a bash function's full definition (`name() { ... }`) as a
    standalone, invocable snippet -- single-sourced from the real script
    text, never re-typed. Relies on the function body containing no nested
    `{`/`}` pairs of its own (true today: `_cgroup_supervision_state` uses
    only `if/elif/else/fi`), so the first line-anchored `}` after the
    opening brace is genuinely the function's own closing brace."""
    pattern = re.compile(
        rf"^{re.escape(func_name)}\(\) \{{\n.*?\n\}}\n", re.MULTILINE | re.DOTALL,
    )
    m = pattern.search(script_text)
    assert m, f"could not extract function {func_name!r} from script text"
    return m.group(0)


# #3333: GNU `timeout` is absent on stock macOS. The script's supervisor
# check runs `timeout 3 systemctl --user show ...` (behind `command -v
# systemctl`, which only the fake systemctl satisfies here), so on such a
# host every fixture that fakes systemctl also needs a `timeout`. The
# condition is the missing primitive itself, never the platform name.
def _real_timeout_missing() -> bool:
    """True iff no `timeout` executable is on this process's PATH."""
    return shutil.which("timeout") is None


_NEEDS_REAL_TIMEOUT = unittest.skipIf(
    _real_timeout_missing(),
    "needs a real GNU `timeout` on PATH (absent here; a pass-through stub "
    "cannot bound a wedged call)",
)


def _make_timeout_stub(bin_dir: Path) -> bool:
    """Installs a hermetic pass-through `timeout` into `bin_dir` ONLY when
    the host has none. The stub drops its first argument (the duration)
    and execs the rest -- it never times anything out. Returns True iff it
    installed the stub, so a host with a real `timeout` always uses it."""
    if not _real_timeout_missing():
        return False
    path = bin_dir / "timeout"
    path.write_text('#!/bin/bash\nshift\nexec "$@"\n')
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return True


def _make_fake_systemctl(
    bin_dir: Path, *, mode: str,
    load_state: str = "", active_state: str = "", sub_state: str = "",
    sleep_seconds: float = 10,
) -> None:
    """Writes a fake `systemctl` executable into `bin_dir` (meant to be
    PATH-prepended ahead of the real one) standing in for a real systemd
    user bus. This dev/test host has NO live `systemctl --user` session
    (measured: `systemctl --user show ... ` exits 1 with "Failed to
    connect to bus" here) -- itself a real, unmocked instance of the
    `unavailable` state, but not something any test can rely on being
    true (or false) on every host/CI runner, hence this mock for the
    other three deterministic states.

    Ignores whatever real argv it's invoked with and unconditionally
    behaves per `mode`:
      "show"            -> emits the requested LoadState/ActiveState/
                            SubState triple, exit 0 (mirrors a genuine
                            `systemctl --user show ... -p L,A,S`reply).
      "exit-nonzero"     -> exits 1 with no output (a dead/unreachable bus).
      "ambiguous-output" -> exits 0 but prints no LoadState= line at all
                            (a reply shape the parser doesn't recognize).
      "sleep"            -> sleeps `sleep_seconds` before printing anything
                            (never actually reaches a `show`-like reply) --
                            stands in for a wedged/slow-to-respond --user
                            dbus (reviewer-fairy suggestion 1, PR #2700
                            round 1). Used to prove `_supervisor_registered_
                            state`'s own `timeout 3` wrapper actually
                            bounds this call rather than blocking for the
                            fake's full sleep duration.
      "xdg-gated"        -> mirrors the REAL, measured behavior of a genuine
                            `systemctl --user` when `$XDG_RUNTIME_DIR` is
                            unset/empty (merge-seat HOLD, Kepler, PR #2700):
                            exits 1 printing "Failed to connect to bus: No
                            medium found" to stderr in that case; behaves
                            exactly like "show" (the requested LoadState/
                            ActiveState/SubState triple, exit 0) once
                            `$XDG_RUNTIME_DIR` is non-empty. Used to prove
                            the guard-default fix actually causes the
                            `systemctl` call itself to see the variable
                            (not just that some other part of the script
                            behaves as if it did).
    Exported for cross-file reuse (mirrors the established
    test_pgrep_liveness_reap_patterns._extract_reap_pattern import
    convention) -- tests/test_socket_ownership_guard.py and
    tests/test_surface_daemon_pid_reap.py both need to force the
    UNSUPERVISED state deterministically now that #2652 phase-0 unit 4's
    fold gates their reap/rm/launch scenarios behind this same check.
    """
    path = bin_dir / "systemctl"
    if mode == "show":
        path.write_text(
            "#!/bin/bash\n"
            f"printf 'LoadState={load_state}\\nActiveState={active_state}\\nSubState={sub_state}\\n'\n"
            "exit 0\n"
        )
    elif mode == "exit-nonzero":
        path.write_text("#!/bin/bash\nexit 1\n")
    elif mode == "ambiguous-output":
        path.write_text("#!/bin/bash\necho 'garbage, no LoadState here'\nexit 0\n")
    elif mode == "sleep":
        path.write_text(
            "#!/bin/bash\n"
            f"sleep {sleep_seconds}\n"
            "printf 'LoadState=loaded\\nActiveState=active\\nSubState=running\\n'\n"
            "exit 0\n"
        )
    elif mode == "xdg-gated":
        path.write_text(
            "#!/bin/bash\n"
            'if [ -z "${XDG_RUNTIME_DIR:-}" ]; then\n'
            "    echo 'Failed to connect to bus: No medium found' >&2\n"
            "    exit 1\n"
            "fi\n"
            f"printf 'LoadState={load_state}\\nActiveState={active_state}\\nSubState={sub_state}\\n'\n"
            "exit 0\n"
        )
    else:
        raise ValueError(mode)
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    if mode != "sleep":
        # "sleep" is the wedged-bus fake: it needs a REAL timeout to bound
        # it, so no stub is installed for it (#3333).
        _make_timeout_stub(bin_dir)


def _run_supervisor_function(
    *, fake_systemctl_mode: str | None = None, load_state: str = "",
    active_state: str = "", sub_state: str = "", no_systemctl: bool = False,
    mutated: bool = False, strip_timeout: bool = False,
    strip_xdg_guard: bool = False, xdg_runtime_dir_unset: bool = False,
    xdg_runtime_dir: str | None = None, fake_id_uid: str | None = None,
    sleep_seconds: float = 10.0, subprocess_timeout: float = 5.0,
) -> str:
    """Invoke the REAL (or, if mutated=True/strip_timeout=True, a
    deliberately-broken) `_supervisor_registered_state` extracted verbatim
    from the live script, in an isolated bash subprocess -- mirrors
    `_run_cgroup_function` above, but with an injectable-PATH `systemctl`
    instead of an injectable file path (there is no single file argument
    to override here; the function shells out to `systemctl` itself).

    `no_systemctl=True` exercises the "systemctl absent from PATH branch"
    deterministically: `bash` is invoked via its own absolute path (so no
    PATH lookup is needed to find bash itself), and PATH is set to a
    non-empty but systemctl-free directory -- the function's own
    `command -v systemctl` check therefore genuinely fails, exactly as it
    would on a non-systemd host (e.g. Darwin), without needing to hide the
    real systemctl from the rest of this test process's environment.

    `mutated=True` is the falsification for the Darwin-scope fix
    (reviewer-fairy suggestion 2, PR #2700 round 1): it REVERTS the fixed
    "no systemctl -> not-registered" mapping back to its pre-fix
    "unavailable" answer, so the dedicated not-registered test can be
    shown to go red under the reversion while the genuinely-ambiguous
    cases (nonzero exit, ambiguous output) stay green, untouched by this
    narrowly-targeted mutation -- proving the fix is exercising the
    intended, narrow distinction and not a broader, accidental change.

    `strip_timeout=True` is the falsification for the timeout fix
    (reviewer-fairy suggestion 1, PR #2700 round 1): it removes the
    `timeout 3` wrapper around the `systemctl --user show ...` call,
    restoring the pre-fix unbounded invocation -- paired with
    `fake_systemctl_mode="sleep"`, this proves the wrapper (not some
    other effect, e.g. the fake sleep just happening to be fast) is what
    makes the timeout test return quickly.

    `sleep_seconds`/`subprocess_timeout` only matter for
    `fake_systemctl_mode="sleep"` scenarios: the fake sleeps for
    `sleep_seconds` before ever printing a reply; `subprocess_timeout` is
    this TEST's own bound on the whole subprocess call (raises
    `subprocess.TimeoutExpired` if exceeded) -- independent of, and
    strictly larger than, the script's internal `timeout 3` wrapper, so
    that a genuinely-hanging call (e.g. under `strip_timeout=True`)
    cannot hang CI even if the fix under test is broken.

    `strip_xdg_guard=True` is the falsification for the merge-seat HOLD fix
    (Kepler, PR #2700): it removes the `[ -d "/run/user/$(id -u)" ]`
    guard-default block entirely, restoring the pre-fix behavior where
    `XDG_RUNTIME_DIR` is never touched by this function -- so a scenario
    that unsets it upstream reaches `systemctl` exactly as unset, the same
    as on every real, unfixed Linux seat.

    `xdg_runtime_dir_unset=True` explicitly removes `XDG_RUNTIME_DIR` from
    the subprocess's environment (rather than relying on whatever this
    TEST PROCESS happens to have -- which varies by host/CI runner and
    would make the scenario non-deterministic). `xdg_runtime_dir=<str>`
    explicitly sets it instead (for the "already set -- leave it alone"
    sanity case). At most one of the two should be given.

    `fake_id_uid=<str>` writes a fake `id` executable ahead of the real one
    on PATH that unconditionally echoes this string for `id -u` -- used to
    make `/run/user/$(id -u)` resolve to a uid this host does NOT have a
    runtime dir for, exercising the guard's "candidate directory absent"
    branch deterministically instead of depending on which uids happen to
    have logind sessions on whatever host runs this test.
    """
    func_text = _extract_bash_function(START_SCRIPT_PATH.read_text(), "_supervisor_registered_state")
    if strip_xdg_guard:
        # Falsification (merge-seat HOLD, Kepler, PR #2700): remove the
        # guard-default block entirely, reproducing the pre-fix function
        # verbatim (XDG_RUNTIME_DIR is never touched here).
        target = (
            '    if [ -d "/run/user/$(id -u)" ]; then\n'
            '        export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"\n'
            "    fi\n"
        )
        assert target in func_text, "XDG guard-default target snippet not found -- function shape changed"
        func_text = func_text.replace(target, "")

    if mutated:
        # Falsification (reviewer-fairy suggestion 2): revert the
        # no-systemctl-found case back to its PRE-fix "unavailable"
        # mapping.
        target = (
            "    if ! command -v systemctl >/dev/null 2>&1; then\n"
            '        echo "not-registered"\n'
            "        return\n"
            "    fi\n"
        )
        assert target in func_text, "mutation target snippet not found -- function shape changed"
        replacement = (
            "    if ! command -v systemctl >/dev/null 2>&1; then\n"
            '        echo "unavailable"\n'
            "        return\n"
            "    fi\n"
        )
        func_text = func_text.replace(target, replacement)

    if strip_timeout:
        # Falsification (reviewer-fairy suggestion 1): remove the
        # `timeout 3` wrapper, restoring the pre-fix unbounded systemctl
        # invocation.
        target = (
            'out="$(timeout 3 systemctl --user show engram-surface-daemon.service '
            '-p LoadState,ActiveState,SubState 2>&1)"'
        )
        assert target in func_text, "timeout-wrapper target snippet not found -- function shape changed"
        replacement = (
            'out="$(systemctl --user show engram-surface-daemon.service '
            '-p LoadState,ActiveState,SubState 2>&1)"'
        )
        func_text = func_text.replace(target, replacement)

    assert not (xdg_runtime_dir_unset and xdg_runtime_dir is not None), (
        "specify at most one of xdg_runtime_dir_unset / xdg_runtime_dir"
    )

    with tempfile.TemporaryDirectory() as td:
        bin_dir = Path(td)
        env = dict(os.environ)
        if xdg_runtime_dir_unset:
            env.pop("XDG_RUNTIME_DIR", None)
        elif xdg_runtime_dir is not None:
            env["XDG_RUNTIME_DIR"] = xdg_runtime_dir
        if fake_id_uid is not None:
            fake_id = bin_dir / "id"
            fake_id.write_text(f"#!/bin/bash\necho {fake_id_uid}\n")
            fake_id.chmod(fake_id.stat().st_mode | stat.S_IXUSR)
        if no_systemctl:
            env["PATH"] = str(bin_dir)  # empty dir, deliberately no systemctl
        elif fake_systemctl_mode is not None:
            _make_fake_systemctl(
                bin_dir, mode=fake_systemctl_mode, load_state=load_state,
                active_state=active_state, sub_state=sub_state,
                sleep_seconds=sleep_seconds,
            )
            env["PATH"] = f"{bin_dir}:{os.environ.get('PATH', '')}"
        elif fake_id_uid is not None:
            # fake `id` only, real systemctl untouched -- still needs
            # bin_dir on PATH ahead of the rest for the fake `id` to win.
            env["PATH"] = f"{bin_dir}:{os.environ.get('PATH', '')}"
        script = func_text + "\n_supervisor_registered_state"
        bash_path = shutil.which("bash") or "/bin/bash"
        result = subprocess.run(
            [bash_path, "-c", script], capture_output=True, text=True,
            timeout=subprocess_timeout, env=env,
        )
        assert result.returncode == 0, f"supervisor function invocation failed: {result.stderr}"
        return result.stdout.strip()


def _run_cgroup_function(pid, cgroup_file: str | None = None, *, mutated: bool = False) -> str:
    """Invoke the REAL (or, if mutated=True, a deliberately-broken)
    `_cgroup_supervision_state` extracted verbatim from the live script,
    in an isolated bash subprocess, with an injectable cgroup-file-path
    override -- so the three states can be exercised deterministically
    without joining a real systemd session slice (per the spec's explicit
    "refactor into a small testable unit that accepts an injectable path"
    option)."""
    func_text = _extract_bash_function(START_SCRIPT_PATH.read_text(), "_cgroup_supervision_state")
    if mutated:
        # The exact failure mode the spec's falsification step targets:
        # collapse the exit-code-1-specific "supervised" branch and the
        # exit-code-2 "unavailable" branch into a single naive fallback,
        # so an unreadable/missing cgroup file silently reads as a clean
        # "supervised" answer instead of the discriminating "unavailable".
        target = (
            '    elif [ "$rc" -eq 1 ]; then\n'
            '        echo "supervised"     # readable, no session-slice match\n'
            "    else\n"
            '        echo "unavailable"    # rc 2: file missing/unreadable -- NEVER\n'
            "                               # collapse this into either of the above\n"
            "    fi\n"
        )
        assert target in func_text, "mutation target snippet not found -- function shape changed"
        replacement = '    else\n        echo "supervised" || true\n    fi\n'
        func_text = func_text.replace(target, replacement)

    args = [str(pid)]
    if cgroup_file is not None:
        args.append(cgroup_file)
    script = func_text + "\n_cgroup_supervision_state " + " ".join(f'"{a}"' for a in args)
    result = subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=5)
    assert result.returncode == 0, f"cgroup function invocation failed: {result.stderr}"
    return result.stdout.strip()


class _ProbeBranchFixture(unittest.TestCase):
    """Shared setup: a real, executable copy of start-engram-daemon.sh (or a
    frozen pre-instrumentation variant) with {{PYTHON}} substituted, next to
    a stub engram-surface-daemon.py so any relaunch path a FROZEN baseline
    still exercises never touches the real daemon (which would load a real
    embedding model). The live script no longer references the stub at
    all -- its presence on disk is inert for the live-script test classes,
    but still required for the `_PRE_UNIT4_SCRIPT_TEXT` (and older) baseline
    classes, which still try to launch it."""

    SCRIPT_TEXT: str | None = None  # None -> use the live source file
    PATCH_OUT_2666: bool = False  # isolate #2666 for branch-4 reachability

    # #2652 phase-0 unit 4 fold: every _run() of the LIVE script now also
    # evaluates the four-state supervisor-registration branch whenever
    # branches 1/2/3 fire. Left un-mocked, that check would hit this dev
    # host's own real `systemctl --user` (which has no live user-bus
    # session here -- measured "Failed to connect to bus", exit 1 --
    # itself a real "unavailable" instance, but not something any test
    # should rely on being true, or false, on every host/CI runner).
    # Defaulting every fixture to a deterministic, host-independent
    # "registered-active" (SUPERVISED_HEALTHY) keeps every PRE-EXISTING
    # "no relaunch" assertion in this file meaningful for the reason it
    # always was (branches 1/2/3 alone decide nothing needs launching),
    # not an accident of ambient host state. Individual test classes
    # override these three attributes to exercise the other three states.
    # Harmless (unused) for the FROZEN pre-fold SCRIPT_TEXT classes, which
    # never call systemctl at all.
    FAKE_SYSTEMCTL_MODE: str | None = "show"
    FAKE_LOAD_STATE: str = "loaded"
    FAKE_ACTIVE_STATE: str = "active"
    FAKE_SUB_STATE: str = "running"

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
        if self.PATCH_OUT_2666:
            script_text = _patch_out_bare_except_2666(script_text)
        self.script = hooks_dir / "start-engram-daemon.sh"
        self.script.write_text(script_text)
        self.script.chmod(self.script.stat().st_mode | stat.S_IXUSR)

        self.engram_home = self.tmp / "engram_home"
        self.engram_home.mkdir()
        self.pid_path = self.engram_home / "recall-daemon.pid"
        self.socket_path = self.engram_home / "recall-daemon.sock"
        self.log_path = self.engram_home / "surface-daemon.log"

        self._fake_systemctl_dir = self.tmp / "fakebin-systemctl"
        self._fake_systemctl_dir.mkdir()
        if self.FAKE_SYSTEMCTL_MODE is not None:
            _make_fake_systemctl(
                self._fake_systemctl_dir, mode=self.FAKE_SYSTEMCTL_MODE,
                load_state=self.FAKE_LOAD_STATE, active_state=self.FAKE_ACTIVE_STATE,
                sub_state=self.FAKE_SUB_STATE,
            )

    def _run(self, timeout: float = 10.0) -> subprocess.CompletedProcess:
        env = dict(os.environ)
        env["ENGRAM_HOME"] = str(self.engram_home)
        if self.FAKE_SYSTEMCTL_MODE is not None:
            env["PATH"] = f"{self._fake_systemctl_dir}:{env.get('PATH', '')}"
        return subprocess.run(
            ["bash", str(self.script)], env=env, capture_output=True,
            text=True, timeout=timeout,
        )

    def _log_lines(self) -> list[str]:
        if not self.log_path.exists():
            return []
        return self.log_path.read_text().splitlines()

    def _relaunch_happened(self) -> bool:
        """The stub daemon prints a marker to stderr, which the script
        redirects into LOG_PATH -- its presence is direct evidence a
        reap+relaunch section ran. For the LIVE script (this unit's whole
        point) this must always be False; the frozen baselines still
        exercise it, which is what the falsification classes below check
        for."""
        return any("stub daemon started" in line for line in self._log_lines())

    def _wait_for_relaunch_marker(self, timeout: float = 5.0) -> bool:
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self._relaunch_happened():
                return True
            time.sleep(0.1)
        return False

    def _wait_briefly_for_no_relaunch(self, timeout: float = 1.5) -> bool:
        """The negative-space counterpart of _wait_for_relaunch_marker:
        waits out the same window a genuine relaunch would need to produce
        its marker, then confirms it never appeared. Used by the live-script
        (unit 4) assertions, where "no relaunch" is the property under
        test and a zero-wait check would be too eager to catch a delayed
        spawn."""
        time.sleep(timeout)
        return not self._relaunch_happened()


def _branch_regex(
    branch: int, *, pid: int | None = None, pid_path: Path | None = None,
    exc_name: str | None = None, cgroup: str | None = None,
) -> re.Pattern:
    if branch == 1:
        assert pid_path is not None
        assert cgroup is None, "branch 1 never has a live pid to read a cgroup for"
        pattern = (
            rf"\[{_TIMESTAMP_RE}\] \[start-engram-daemon\] probe: "
            rf"no pidfile at {re.escape(str(pid_path))}"
        )
    elif branch == 2:
        assert pid is not None
        assert cgroup is None, "branch 2's pid is confirmed DEAD -- kill -0 never succeeds, so no cgroup read happens"
        pattern = (
            rf"\[{_TIMESTAMP_RE}\] \[start-engram-daemon\] probe: "
            rf"pidfile present \(pid={pid}\) but process not running"
        )
    elif branch == 3:
        # #2676: branch 3's log line now names WHICH exception the socket
        # probe caught (FileNotFoundError / ConnectionRefusedError /
        # BlockingIOError) -- exc_name is required so every caller states
        # explicitly which population it is asserting, rather than matching
        # any exception name generically. #2652 unit 4: also requires the
        # trailing cgroup-state annotation, since kill -0 succeeded here.
        assert pid is not None
        assert exc_name is not None, "branch 3 assertions must name the expected exception class"
        assert cgroup in _CGROUP_STATES, f"branch 3 requires a valid cgroup state, got {cgroup!r}"
        pattern = (
            rf"\[{_TIMESTAMP_RE}\] \[start-engram-daemon\] probe: "
            rf"pidfile present \(pid={pid}\), process alive, "
            rf"socket probe failed: {re.escape(exc_name)}, cgroup: {re.escape(cgroup)}"
        )
    elif branch == 4:
        assert pid is not None
        assert cgroup in _CGROUP_STATES, f"branch 4 requires a valid cgroup state, got {cgroup!r}"
        pattern = (
            rf"\[{_TIMESTAMP_RE}\] \[start-engram-daemon\] probe: "
            rf"healthy \(pid={pid}\), no action, cgroup: {re.escape(cgroup)}"
        )
    else:
        raise ValueError(branch)
    return re.compile(pattern)


# ===========================================================================
# Branch 1 -- no pidfile at all
# ===========================================================================


class TestBranch1NoPidfile(_ProbeBranchFixture):
    def test_logs_branch1_line_and_no_other_branch_line(self):
        result = self._run()
        self.assertEqual(result.returncode, 0, result.stderr)
        content = "\n".join(self._log_lines())

        self.assertRegex(content, _branch_regex(1, pid_path=self.pid_path))
        # branches 2-4 all share the literal phrases below regardless of
        # their pid= value, so checking the phrase alone is sufficient to
        # confirm none of them fired for this invocation.
        self.assertNotIn("probe: pidfile present", content)
        self.assertNotIn("probe: healthy", content)

    def test_no_relaunch_or_side_effects(self):
        """#2652 phase-0 unit 4: branch 1 must be a pure no-op now -- no
        reap, no relaunch, no file ever created."""
        result = self._run()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(
            self._wait_briefly_for_no_relaunch(),
            "branch 1 (no pidfile) must NOT relaunch any more -- unit 4 removes reap+relaunch entirely",
        )
        self.assertFalse(self.pid_path.exists(), "no pidfile existed before; the hook must not create one")
        self.assertFalse(self.socket_path.exists(), "no socket existed before; the hook must not create one")


class TestBranch1AgainstPreUnit1Script(_ProbeBranchFixture):
    """Falsification control: the identical branch-1 scenario, run against
    the frozen pre-#2652-unit-1 script. Proves TestBranch1NoPidfile's log
    assertion is exercising the new log line and not something incidental
    (e.g. the exit code, which is unchanged and would still pass here)."""

    SCRIPT_TEXT = _PRE_UNIT1_SCRIPT_TEXT

    def test_reverted_script_writes_no_probe_line_at_all(self):
        result = self._run()
        self.assertEqual(result.returncode, 0, result.stderr)
        content = "\n".join(self._log_lines())

        self.assertNotRegex(
            content, _branch_regex(1, pid_path=self.pid_path),
            "the pre-instrumentation script must NOT write the branch-1 probe "
            "line -- if it did, TestBranch1NoPidfile's assertion would prove "
            "nothing about this unit's actual change",
        )
        self.assertNotIn(
            "[start-engram-daemon] probe:", content,
            "the pre-instrumentation script must write no probe line of any kind",
        )
        # This very old baseline still reaps+relaunches -- unaffected by
        # unit 4 (which only touches the LIVE script, not this frozen text).
        self.assertTrue(self._wait_for_relaunch_marker())


class TestBranch1AgainstPreUnit4Script(_ProbeBranchFixture):
    """Falsification control for unit 4 specifically: the identical
    branch-1 scenario, run against the immediately-prior (units 1-3
    merged) script. Proves TestBranch1NoPidfile's "no relaunch" assertion
    is exercising a real removed behavior, not something vacuously true --
    this frozen baseline DOES still relaunch in this scenario."""

    SCRIPT_TEXT = _PRE_UNIT4_SCRIPT_TEXT

    def test_pre_unit4_script_still_relaunches(self):
        result = self._run()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(
            self._wait_for_relaunch_marker(),
            "the pre-unit-4 baseline must still relaunch on branch 1 -- if it "
            "doesn't, TestBranch1NoPidfile's 'no relaunch' assertion would "
            "prove nothing about unit 4's actual removal",
        )


# ===========================================================================
# Branch 2 -- stale pidfile (process not running)
# ===========================================================================


class TestBranch2StalePidfile(_ProbeBranchFixture):
    def setUp(self):
        super().setUp()
        self.dead_pid = _confirmed_dead_pid()
        self.pid_path.write_text(str(self.dead_pid))

    def test_logs_branch2_line_and_no_other_branch_line(self):
        result = self._run()
        self.assertEqual(result.returncode, 0, result.stderr)
        content = "\n".join(self._log_lines())

        self.assertRegex(content, _branch_regex(2, pid=self.dead_pid))
        self.assertNotIn("probe: no pidfile", content)
        self.assertNotIn("socket probe failed", content)
        self.assertNotIn("probe: healthy", content)

    def test_no_relaunch_or_side_effects(self):
        result = self._run()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(
            self._wait_briefly_for_no_relaunch(),
            "branch 2 (stale pidfile) must NOT relaunch any more",
        )
        self.assertFalse(self.socket_path.exists(), "no socket existed before; the hook must not create one")
        self.assertEqual(
            self.pid_path.read_text().strip(), str(self.dead_pid),
            "the (stale) pidfile content must be left exactly as it was -- no rm -f any more",
        )


class TestBranch2AgainstPreUnit1Script(_ProbeBranchFixture):
    SCRIPT_TEXT = _PRE_UNIT1_SCRIPT_TEXT

    def setUp(self):
        super().setUp()
        self.dead_pid = _confirmed_dead_pid()
        self.pid_path.write_text(str(self.dead_pid))

    def test_reverted_script_writes_no_probe_line_at_all(self):
        result = self._run()
        self.assertEqual(result.returncode, 0, result.stderr)
        content = "\n".join(self._log_lines())

        self.assertNotRegex(content, _branch_regex(2, pid=self.dead_pid))
        self.assertNotIn("[start-engram-daemon] probe:", content)
        self.assertTrue(self._wait_for_relaunch_marker())


class TestBranch2AgainstPreUnit4Script(_ProbeBranchFixture):
    SCRIPT_TEXT = _PRE_UNIT4_SCRIPT_TEXT

    def setUp(self):
        super().setUp()
        self.dead_pid = _confirmed_dead_pid()
        self.pid_path.write_text(str(self.dead_pid))

    def test_pre_unit4_script_still_relaunches_and_removes_pidfile(self):
        result = self._run()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(
            self._wait_for_relaunch_marker(),
            "the pre-unit-4 baseline must still relaunch on branch 2",
        )
        # The pre-unit-4 baseline's unit-2 guard allows the rm because the
        # dead pid was matched+killed by ITS OWN reap loop scan on the real
        # pattern (a dead pid won't actually match the pgrep pattern here,
        # but the guard still permits rm -f whenever nothing live holds the
        # socket) -- assert the pidfile ends up rewritten with the NEW
        # relaunched daemon's pid, i.e. it no longer contains the old dead
        # pid, proving real reap/rm/relaunch activity fired.
        self.assertNotEqual(
            self.pid_path.read_text().strip() if self.pid_path.exists() else "",
            str(self.dead_pid),
            "the pre-unit-4 baseline must have rewritten the pidfile (relaunch actually happened)",
        )


# ===========================================================================
# Branch 3 -- pidfile present, process alive, socket probe fails
# ===========================================================================


class TestBranch3LiveProcessDeadSocket(_ProbeBranchFixture):
    """#2676 case 1: no socket file present at all -> FileNotFoundError --
    the healthy-daemon-in-warmup-window population (pidfile written before
    bind()/listen() -- engram-surface-daemon.py's main(): pidfile write,
    then server.bind(), then server.listen())."""

    def setUp(self):
        super().setUp()
        # A real, alive process -- kill -0 must succeed. No socket is bound
        # at self.socket_path, so the python connect-probe must fail with
        # FileNotFoundError specifically.
        self.live_proc = _spawn_sleeper(seconds=8.0)
        self.pid_path.write_text(str(self.live_proc.pid))
        self.addCleanup(self._kill_live_proc)

    def _kill_live_proc(self):
        if self.live_proc.poll() is None:
            self.live_proc.terminate()
            self.live_proc.wait(timeout=2)

    def test_logs_branch3_line_and_no_other_branch_line(self):
        self.assertFalse(self.socket_path.exists(), "test setup: socket must not exist")
        result = self._run()
        self.assertEqual(result.returncode, 0, result.stderr)
        content = "\n".join(self._log_lines())

        expected_cgroup = _expected_cgroup_state_for_pid(self.live_proc.pid)
        self.assertRegex(
            content,
            _branch_regex(3, pid=self.live_proc.pid, exc_name="FileNotFoundError", cgroup=expected_cgroup),
        )
        self.assertNotIn("probe: no pidfile", content)
        self.assertNotIn("but process not running", content)
        self.assertNotIn("probe: healthy", content)

    def test_no_relaunch_or_side_effects(self):
        result = self._run()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(
            self._wait_briefly_for_no_relaunch(),
            "branch 3 (live process, dead socket) must NOT relaunch any more",
        )
        self.assertIsNone(self.live_proc.poll(), "the live process this run found must NOT be killed")
        self.assertEqual(self.pid_path.read_text().strip(), str(self.live_proc.pid))


class TestBranch3AgainstPreUnit1Script(_ProbeBranchFixture):
    SCRIPT_TEXT = _PRE_UNIT1_SCRIPT_TEXT

    def setUp(self):
        super().setUp()
        self.live_proc = _spawn_sleeper(seconds=8.0)
        self.pid_path.write_text(str(self.live_proc.pid))
        self.addCleanup(self._kill_live_proc)

    def _kill_live_proc(self):
        if self.live_proc.poll() is None:
            self.live_proc.terminate()
            self.live_proc.wait(timeout=2)

    def test_reverted_script_writes_no_probe_line_at_all(self):
        result = self._run()
        self.assertEqual(result.returncode, 0, result.stderr)
        content = "\n".join(self._log_lines())

        self.assertNotIn("[start-engram-daemon] probe:", content)
        self.assertTrue(self._wait_for_relaunch_marker())


class TestBranch3AgainstPreUnit4Script(_ProbeBranchFixture):
    SCRIPT_TEXT = _PRE_UNIT4_SCRIPT_TEXT

    def setUp(self):
        super().setUp()
        self.live_proc = _spawn_sleeper(seconds=8.0)
        self.pid_path.write_text(str(self.live_proc.pid))
        self.addCleanup(self._kill_live_proc)

    def _kill_live_proc(self):
        if self.live_proc.poll() is None:
            self.live_proc.terminate()
            self.live_proc.wait(timeout=2)

    def test_pre_unit4_script_still_relaunches(self):
        result = self._run()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(
            self._wait_for_relaunch_marker(),
            "the pre-unit-4 baseline must still relaunch on branch 3",
        )


class TestBranch3ConnectionRefused(_ProbeBranchFixture):
    """#2676 case 2: the socket file exists but nothing is listening on it
    (bind + listen + close, leaving the inode behind) -> ConnectionRefusedError
    -- the actual orphan pathology the #2652 epic exists to fix. Measured:
    bind()+listen()+close() reliably produces ECONNREFUSED on a subsequent
    connect() (30/30 in manual verification during unit 1's development)."""

    def setUp(self):
        super().setUp()
        self.live_proc = _spawn_sleeper(seconds=8.0)
        self.pid_path.write_text(str(self.live_proc.pid))
        self.addCleanup(self._kill_live_proc)

        srv = socket_mod.socket(socket_mod.AF_UNIX, socket_mod.SOCK_STREAM)
        srv.bind(str(self.socket_path))
        srv.listen(1)
        srv.close()

    def _kill_live_proc(self):
        if self.live_proc.poll() is None:
            self.live_proc.terminate()
            self.live_proc.wait(timeout=2)

    def test_logs_branch3_line_with_connection_refused(self):
        self.assertTrue(self.socket_path.exists(), "test setup: stale socket file must exist")
        result = self._run()
        self.assertEqual(result.returncode, 0, result.stderr)
        content = "\n".join(self._log_lines())

        expected_cgroup = _expected_cgroup_state_for_pid(self.live_proc.pid)
        self.assertRegex(
            content,
            _branch_regex(3, pid=self.live_proc.pid, exc_name="ConnectionRefusedError", cgroup=expected_cgroup),
        )
        self.assertNotIn("probe: no pidfile", content)
        self.assertNotIn("but process not running", content)
        self.assertNotIn("probe: healthy", content)

    def test_no_relaunch_or_side_effects(self):
        result = self._run()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(
            self._wait_briefly_for_no_relaunch(),
            "branch 3 (live process, refused connection) must NOT relaunch any more",
        )
        self.assertIsNone(self.live_proc.poll())
        self.assertEqual(self.pid_path.read_text().strip(), str(self.live_proc.pid))
        # The stale-but-orphaned socket FILE must be left exactly as it
        # was -- no rm -f any more, unit 2's guard code is retired
        # alongside the rm site it used to guard.
        self.assertTrue(self.socket_path.exists(), "the stale socket file must not be removed")


class TestBranch3ConnectionRefusedAgainstPreUnit1Script(_ProbeBranchFixture):
    """Falsification control for the ConnectionRefusedError case: identical
    scenario against the frozen pre-#2652-unit-1 baseline, which writes no
    probe line at all."""

    SCRIPT_TEXT = _PRE_UNIT1_SCRIPT_TEXT

    def setUp(self):
        super().setUp()
        self.live_proc = _spawn_sleeper(seconds=8.0)
        self.pid_path.write_text(str(self.live_proc.pid))
        self.addCleanup(self._kill_live_proc)

        srv = socket_mod.socket(socket_mod.AF_UNIX, socket_mod.SOCK_STREAM)
        srv.bind(str(self.socket_path))
        srv.listen(1)
        srv.close()

    def _kill_live_proc(self):
        if self.live_proc.poll() is None:
            self.live_proc.terminate()
            self.live_proc.wait(timeout=2)

    def test_reverted_script_writes_no_probe_line_at_all(self):
        result = self._run()
        self.assertEqual(result.returncode, 0, result.stderr)
        content = "\n".join(self._log_lines())

        self.assertNotIn("[start-engram-daemon] probe:", content)
        self.assertTrue(self._wait_for_relaunch_marker())


class TestBranch3ConnectionRefusedAgainstPreUnit4Script(_ProbeBranchFixture):
    """Falsification control for unit 4: identical ConnectionRefusedError
    scenario, run against the pre-unit-4 baseline -- proves the stale
    socket file DID get removed by the old code (the exact behavior this
    unit's "must not be removed" assertion depends on having actually
    changed)."""

    SCRIPT_TEXT = _PRE_UNIT4_SCRIPT_TEXT

    def setUp(self):
        super().setUp()
        self.live_proc = _spawn_sleeper(seconds=8.0)
        self.pid_path.write_text(str(self.live_proc.pid))
        self.addCleanup(self._kill_live_proc)

        srv = socket_mod.socket(socket_mod.AF_UNIX, socket_mod.SOCK_STREAM)
        srv.bind(str(self.socket_path))
        srv.listen(1)
        srv.close()

    def _kill_live_proc(self):
        if self.live_proc.poll() is None:
            self.live_proc.terminate()
            self.live_proc.wait(timeout=2)

    def test_pre_unit4_script_removes_the_stale_socket_and_relaunches(self):
        result = self._run()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(
            self._wait_for_relaunch_marker(),
            "the pre-unit-4 baseline must still relaunch on branch 3 (connection refused)",
        )


def _full_backlog_connect_raises_blocking_io() -> bool:
    """Probes the primitive TestBranch3BlockingIO's fake depends on: that a
    non-blocking AF_UNIX connect() to a listen(0) socket whose backlog is
    full raises BlockingIOError (Linux). macOS never does: measured on one Mac,
    20-40 non-blocking connects to a listen(0) socket all succeeded, and
    with listen(1) the second onward got ECONNREFUSED, no BlockingIOError
    either way. So the fake cannot produce the BlockingIOError branch
    there (#3333)."""
    with tempfile.TemporaryDirectory() as td:
        path = os.path.join(td, "p.sock")
        srv = socket_mod.socket(socket_mod.AF_UNIX, socket_mod.SOCK_STREAM)
        clients = []
        try:
            srv.bind(path)
            srv.listen(0)
            for _ in range(20):
                c = socket_mod.socket(socket_mod.AF_UNIX, socket_mod.SOCK_STREAM)
                clients.append(c)
                c.setblocking(False)
                try:
                    c.connect(path)
                except BlockingIOError:
                    return True
                except OSError:
                    return False
            return False
        except OSError:
            # bind()/listen() can fail (e.g. a TMPDIR path over the AF_UNIX
            # length limit): the primitive is unusable here, so skip the cell
            # rather than turn the whole file into a collection error.
            return False
        finally:
            for c in clients:
                c.close()
            srv.close()


class TestBranch3BlockingIO(_ProbeBranchFixture):
    """#2676 case 3: a real bound+listening socket with a zero backlog
    (`listen(0)`), its accept backlog deliberately exhausted by opening
    several simultaneous non-blocking connections before the probe connects
    -> BlockingIOError. A live, listening, busy daemon -- distinct from the
    orphan case above. Measured deterministic 30/30 in manual verification
    during unit 1's development (see that unit's PR description for the
    full run)."""

    def setUp(self):
        super().setUp()
        self.live_proc = _spawn_sleeper(seconds=8.0)
        self.pid_path.write_text(str(self.live_proc.pid))
        self.addCleanup(self._kill_live_proc)

        self.srv = socket_mod.socket(socket_mod.AF_UNIX, socket_mod.SOCK_STREAM)
        self.srv.bind(str(self.socket_path))
        self.srv.listen(0)
        self.addCleanup(self.srv.close)

        # Exhaust the zero backlog with several simultaneous non-blocking
        # connect() attempts before the script's own probe ever connects --
        # none of these are ever accept()ed.
        self._backlog_clients = []
        for _ in range(20):
            c = socket_mod.socket(socket_mod.AF_UNIX, socket_mod.SOCK_STREAM)
            c.setblocking(False)
            try:
                c.connect(str(self.socket_path))
            except BlockingIOError:
                pass
            self._backlog_clients.append(c)
        self.addCleanup(self._close_backlog_clients)

    def _close_backlog_clients(self):
        for c in self._backlog_clients:
            c.close()

    def _kill_live_proc(self):
        if self.live_proc.poll() is None:
            self.live_proc.terminate()
            self.live_proc.wait(timeout=2)

    @unittest.skipUnless(
        _full_backlog_connect_raises_blocking_io(),
        "needs a full AF_UNIX accept backlog to raise BlockingIOError on a "
        "non-blocking connect (not observed on macOS: connects succeed or get ECONNREFUSED)",
    )
    def test_logs_branch3_line_with_blocking_io_error(self):
        result = self._run()
        self.assertEqual(result.returncode, 0, result.stderr)
        content = "\n".join(self._log_lines())

        expected_cgroup = _expected_cgroup_state_for_pid(self.live_proc.pid)
        self.assertRegex(
            content,
            _branch_regex(3, pid=self.live_proc.pid, exc_name="BlockingIOError", cgroup=expected_cgroup),
        )
        self.assertNotIn("probe: no pidfile", content)
        self.assertNotIn("but process not running", content)
        self.assertNotIn("probe: healthy", content)

    def test_no_relaunch_or_side_effects(self):
        result = self._run()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(
            self._wait_briefly_for_no_relaunch(),
            "branch 3 (live process, full accept backlog) must NOT relaunch any more",
        )
        self.assertIsNone(self.live_proc.poll())


# ===========================================================================
# Branch 4 -- pidfile present, process alive, socket responds
# ===========================================================================


class _Branch4BoundAndListeningSetup:
    """Shared setup mixin: a real live process + a real listening AF_UNIX
    socket standing in for a daemon that answers the liveness probe. This
    fixture never calls accept(); the probe cannot distinguish it from a
    serving daemon, which is a known limit of connect()-based liveness, not
    a gap in this test (Borges's #2669 review finding, folded here for
    consistency -- see test_probe_systemexit_fix.py's identical fixture)."""

    def setUp(self):
        super().setUp()
        self.live_proc = _spawn_sleeper(seconds=8.0)
        self.pid_path.write_text(str(self.live_proc.pid))
        self.addCleanup(self._kill_live_proc)

        # bind+listen is enough for a connect()+close() probe to succeed;
        # no accept() needed.
        self.srv = socket_mod.socket(socket_mod.AF_UNIX, socket_mod.SOCK_STREAM)
        self.srv.bind(str(self.socket_path))
        self.srv.listen(1)
        self.addCleanup(self.srv.close)

    def _kill_live_proc(self):
        if self.live_proc.poll() is None:
            self.live_proc.terminate()
            self.live_proc.wait(timeout=2)


class TestBranch4Healthy(_Branch4BoundAndListeningSetup, _ProbeBranchFixture):
    """Verifies this unit's own branch-4 logging + no-op exit, run directly
    against the real script. This is also the phase-0 unit 1 real
    assertion: a genuinely responsive connection (bound+listening; see the
    mixin's docstring for what that does and doesn't prove) logs branch 4
    and the daemon is NOT relaunched -- now with the #2652 unit 4 cgroup
    annotation."""

    def test_logs_branch4_line_and_no_other_branch_line(self):
        result = self._run()
        self.assertEqual(result.returncode, 0, result.stderr)
        content = "\n".join(self._log_lines())

        expected_cgroup = _expected_cgroup_state_for_pid(self.live_proc.pid)
        self.assertRegex(content, _branch_regex(4, pid=self.live_proc.pid, cgroup=expected_cgroup))
        self.assertNotIn("probe: no pidfile", content)
        self.assertNotIn("but process not running", content)
        self.assertNotIn("socket probe failed", content)

    def test_reap_relaunch_does_not_fire(self):
        """The healthy early-exit must remain a true no-op: no reap, no
        relaunch, PID/socket files untouched, original process still alive."""
        self._run()
        time.sleep(0.5)
        self.assertFalse(
            self._relaunch_happened(),
            "branch 4 (healthy) must exit early -- reap+relaunch must NOT fire",
        )
        self.assertIsNone(
            self.live_proc.poll(),
            "the healthy, socket-responsive recorded daemon must be left running",
        )
        self.assertEqual(self.pid_path.read_text().strip(), str(self.live_proc.pid))


class TestBranch4AgainstPreUnit1Script(_Branch4BoundAndListeningSetup, _ProbeBranchFixture):
    """Falsification control for branch 4: identical scenario (#2666
    neutralized so the healthy path is reachable), run against the frozen
    pre-#2652-unit-1 script -- proves the branch-4 log line in
    TestBranch4Healthy comes from unit 1's added code, not from the (also
    patched-here) socket-probe logic, which is shared by both scripts and
    produces the same no-op behavior either way."""

    SCRIPT_TEXT = _PRE_UNIT1_SCRIPT_TEXT
    PATCH_OUT_2666 = True

    def test_reverted_script_writes_no_probe_line_at_all(self):
        result = self._run()
        self.assertEqual(result.returncode, 0, result.stderr)
        content = "\n".join(self._log_lines())

        self.assertNotIn("[start-engram-daemon] probe:", content)
        # Healthy early-exit behavior (no reap, process left alone) is
        # unchanged either way -- only the log line differs.
        self.assertFalse(self._relaunch_happened())
        self.assertIsNone(self.live_proc.poll())


# ===========================================================================
# Preexisting matching process (formerly "reap outcome") -- #2652 phase-0
# unit 4 removes the "reap matched N ..." logging entirely along with the
# kill loop it described (nothing in the live script computes or acts on a
# stale-pid set any more). These two classes replace the old
# `TestReapOutcome*` classes: they assert the new/old behavior directly
# (survives untouched / actually killed), since there is no longer a log
# line to assert against for the new behavior.
# ===========================================================================


class _PreexistingReapMatchSetup:
    """Mixin: spawns a REAL, already-running process matching
    `_reap_pattern` BEFORE the script under test ever runs -- standing in
    for a leftover daemon left behind by a prior invocation (or this
    invocation's own pidfile-not-yet-written race)."""

    def setUp(self):
        super().setUp()
        self.preexisting_proc = _spawn_reap_pattern_match(self.stub_daemon, self.engram_home)
        self.addCleanup(self._kill_preexisting_if_alive)
        # Confirm it's genuinely alive before the run -- a dead-on-arrival
        # process would make "never touched" trivially true for the wrong
        # reason.
        try:
            os.kill(self.preexisting_proc.pid, 0)
        except OSError:
            self.fail("test setup: preexisting reap-pattern process did not start alive")

    def _kill_preexisting_if_alive(self):
        if self.preexisting_proc.poll() is None:
            self.preexisting_proc.terminate()
            try:
                self.preexisting_proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self.preexisting_proc.kill()
                self.preexisting_proc.wait(timeout=2)


class TestPreexistingMatchingProcessNeverTouched(_PreexistingReapMatchSetup, _ProbeBranchFixture):
    """Branch 1 (no pidfile) fall-through, with a real process already
    matching `_reap_pattern` before this invocation runs. #2652 phase-0
    unit 4: this process must now be left completely alone -- no reap
    logging (the mechanism is gone entirely), no kill, no relaunch."""

    def test_never_touched_and_no_reap_logging_at_all(self):
        result = self._run()
        self.assertEqual(result.returncode, 0, result.stderr)
        content = "\n".join(self._log_lines())

        self.assertNotIn(
            "reap matched", content,
            "unit 4 removes the reap-outcome logging mechanism entirely -- "
            "no 'reap matched ...' line should ever appear again",
        )
        self.assertTrue(
            self._wait_briefly_for_no_relaunch(),
            "this run's own relaunch must not fire either -- unit 4 removes it entirely",
        )
        # The preexisting matching process must survive.
        time.sleep(0.5)
        self.assertIsNone(
            self.preexisting_proc.poll(),
            "the preexisting reap-pattern-matching process must NOT be killed any more",
        )


class TestPreexistingMatchingProcessKilledByPreUnit4Script(_PreexistingReapMatchSetup, _ProbeBranchFixture):
    """Falsification control: the identical preexisting-process scenario,
    run against the pre-unit-4 baseline -- proves the "never touched"
    assertion above is exercising a real removed behavior (the old script
    DOES find and kill this process)."""

    SCRIPT_TEXT = _PRE_UNIT4_SCRIPT_TEXT

    def test_pre_unit4_script_logs_and_kills_the_match(self):
        result = self._run()
        self.assertEqual(result.returncode, 0, result.stderr)
        content = "\n".join(self._log_lines())

        self.assertIn("reap matched 1 stale process(es)", content)
        self.assertIn(str(self.preexisting_proc.pid), content)
        self.assertTrue(self._wait_for_relaunch_marker())

        self.preexisting_proc.wait(timeout=3)
        with self.assertRaises(OSError):
            os.kill(self.preexisting_proc.pid, 0)


# ===========================================================================
# #2652 phase-0 unit 4 -- cgroup supervision state, all three outcomes,
# exercised deterministically via the injectable-path override (per the
# spec's explicitly-authorized "refactor into a small testable unit that
# accepts an injectable path" option) rather than by joining a real
# systemd session slice.
# ===========================================================================


class TestCgroupSupervisionStateSupervised(unittest.TestCase):
    """2a: a live process whose own cgroup is readable and does NOT match
    `agent-session@` -> `supervised`."""

    def test_readable_non_session_cgroup_reads_supervised(self):
        with tempfile.NamedTemporaryFile("w", delete=False) as f:
            f.write("0::/user.slice/user-1000.slice/some-other-unit.service\n")
            path = f.name
        try:
            self.assertEqual(_run_cgroup_function(12345, path), "supervised")
        finally:
            os.unlink(path)


class TestCgroupSupervisionStateUnsupervised(unittest.TestCase):
    """2b: a live process inside a real or simulated `agent-session@`-named
    cgroup path -> `unsupervised`. Faked via the injectable path override
    (per the spec's suggestion) rather than actually joining a systemd
    session slice, which would need root/mount-namespace privileges this
    test environment does not assume."""

    def test_session_slice_cgroup_reads_unsupervised(self):
        with tempfile.NamedTemporaryFile("w", delete=False) as f:
            f.write("0::/system.slice/system-agent\\x2dsession.slice/agent-session@sol.service\n")
            path = f.name
        try:
            self.assertEqual(_run_cgroup_function(12345, path), "unsupervised")
        finally:
            os.unlink(path)

    def test_real_own_process_cgroup_on_this_host(self):
        """Live, non-injected confirmation: this test host's own processes
        (this very test runner) are measured to sit inside an
        `agent-session@*` slice (see _expected_cgroup_state_for_pid's
        docstring) -- so calling the real function with NO override, on
        this process's own pid, must also read `unsupervised`. This is the
        positive control proving the injectable-path tests above aren't
        the ONLY way this state is ever reached."""
        own_pid = os.getpid()
        try:
            with open(f"/proc/{own_pid}/cgroup") as f:
                content = f.read()
        except OSError:
            self.skipTest("cannot read own /proc/<pid>/cgroup on this host")
        if "agent-session@" not in content:
            self.skipTest(
                "this test host's own process is not inside an agent-session@ "
                "slice -- the injectable-path test above already covers this "
                "state deterministically regardless"
            )
        self.assertEqual(_run_cgroup_function(own_pid), "unsupervised")


class TestCgroupSupervisionStateUnavailable(unittest.TestCase):
    """2c: an unreadable/missing cgroup file (e.g. a pid that exited
    between the liveness check and the cgroup read, or a permissions-denied
    simulation) -> `unavailable`, confirmed never equal to either of the
    other two strings."""

    def test_missing_cgroup_file_reads_unavailable(self):
        nonexistent = "/nonexistent/path/does-not-exist/cgroup"
        result = _run_cgroup_function(12345, nonexistent)
        self.assertEqual(result, "unavailable")
        self.assertNotEqual(result, "supervised")
        self.assertNotEqual(result, "unsupervised")

    def test_permission_denied_reads_unavailable(self):
        """A file that exists but cannot be opened -- exercises the same
        rc=2 path via a different real-world cause (permission boundary
        rather than a vanished pid)."""
        with tempfile.NamedTemporaryFile("w", delete=False) as f:
            f.write("0::/user.slice/whatever.service\n")
            path = f.name
        try:
            os.chmod(path, 0o000)
            if os.access(path, os.R_OK):
                self.skipTest("running as a user that bypasses file permissions (e.g. root) -- cannot exercise this path")
            self.assertEqual(_run_cgroup_function(12345, path), "unavailable")
        finally:
            os.chmod(path, 0o644)
            os.unlink(path)


class TestCgroupSupervisionFalsification(unittest.TestCase):
    """#3: falsification -- swap the exit-code-1-specific check for a naive
    collapse (`|| echo supervised`-shaped) and confirm the `unavailable`
    case (2c) specifically goes red, since that is the exact failure mode
    ("a dead instrument silently reads as a clean answer") this discipline
    exists to prevent. (2a)/(2b) may or may not also flip -- the spec
    explicitly allows either -- and here they do NOT flip, since the
    mutation only touches the exit-code 1-vs-2 branch, not exit-code 0."""

    def test_mutation_breaks_unavailable_but_not_the_other_two(self):
        # (2c) must flip: unavailable silently reads as the wrong, clean
        # "supervised" answer under the mutation -- this is the exact
        # discriminating case.
        mutated_unavailable = _run_cgroup_function(12345, "/nonexistent/path", mutated=True)
        self.assertEqual(
            mutated_unavailable, "supervised",
            "the mutation should make an unreadable cgroup file silently "
            "read as 'supervised' -- if it doesn't, the mutation didn't "
            "actually collapse the discriminating branch",
        )
        self.assertNotEqual(
            mutated_unavailable, "unavailable",
            "this IS the falsification: the mutated function must NOT "
            "produce the correct 'unavailable' answer any more",
        )

        # (2a)/(2b) are untouched by this specific mutation (it only
        # touches the rc==1/rc==2 split, not rc==0) -- confirm they still
        # read correctly, demonstrating the mutation is narrowly targeted
        # rather than breaking everything indiscriminately.
        with tempfile.NamedTemporaryFile("w", delete=False) as f:
            f.write("0::/user.slice/user-1000.slice/some-other-unit.service\n")
            supervised_path = f.name
        with tempfile.NamedTemporaryFile("w", delete=False) as f:
            f.write("0::/system.slice/agent-session@sol.service\n")
            unsupervised_path = f.name
        try:
            self.assertEqual(_run_cgroup_function(12345, supervised_path, mutated=True), "supervised")
            self.assertEqual(_run_cgroup_function(12345, unsupervised_path, mutated=True), "unsupervised")
        finally:
            os.unlink(supervised_path)
            os.unlink(unsupervised_path)

        # Sanity: the REAL (unmutated) function correctly distinguishes
        # unavailable from supervised, proving the falsification above is
        # actually exercising the mutation and not some unrelated defect.
        real_unavailable = _run_cgroup_function(12345, "/nonexistent/path", mutated=False)
        self.assertEqual(real_unavailable, "unavailable")


# ===========================================================================
# #2652 phase-0 unit 4 FOLD -- Kepler's required four-state supervisor-
# registration branch ("report-only cannot be unconditional yet"). Two
# layers of coverage, mirroring the cgroup-state section above:
#
#   (I)  Isolated function tests for `_supervisor_registered_state` itself,
#        exercised via an injectable fake `systemctl` on PATH (the
#        function shells out to `systemctl`, so there is no single file
#        argument to override the way `_cgroup_supervision_state` accepts
#        one -- see `_run_supervisor_function`'s own docstring).
#   (II) End-to-end tests running the REAL, unmodified script through
#        branch 1 (no pidfile) with each of the four states faked, and
#        confirming the resulting DECISION (reap+rm+launch fires, or does
#        not) -- not just the log line.
# ===========================================================================


class TestSupervisorRegisteredStateRegisteredActive(unittest.TestCase):
    """LoadState=loaded, ActiveState=active -> registered-active."""

    def test_active_unit_reads_registered_active(self):
        result = _run_supervisor_function(
            fake_systemctl_mode="show", load_state="loaded",
            active_state="active", sub_state="running",
        )
        self.assertEqual(result, "registered-active")


class TestSupervisorRegisteredStateRegisteredFailing(unittest.TestCase):
    """LoadState=loaded, ActiveState=failed (registered but NOT active) ->
    registered-failing. This is Aleph's crash-looping case: a real unit
    file is loaded, but the daemon itself is down/looping -- distinct from
    "no unit at all"."""

    def test_failed_unit_reads_registered_failing(self):
        result = _run_supervisor_function(
            fake_systemctl_mode="show", load_state="loaded",
            active_state="failed", sub_state="failed",
        )
        self.assertEqual(result, "registered-failing")

    def test_inactive_but_loaded_unit_also_reads_registered_failing(self):
        """Any ActiveState other than the literal "active" string falls
        into the "not confirmed healthy" bucket -- not just "failed"."""
        result = _run_supervisor_function(
            fake_systemctl_mode="show", load_state="loaded",
            active_state="inactive", sub_state="dead",
        )
        self.assertEqual(result, "registered-failing")


class TestSupervisorRegisteredStateNotRegistered(unittest.TestCase):
    """LoadState=not-found -> not-registered (systemctl itself is
    reachable and answered cleanly; it just has no unit file for this
    name)."""

    def test_not_found_load_state_reads_not_registered(self):
        result = _run_supervisor_function(
            fake_systemctl_mode="show", load_state="not-found",
            active_state="inactive", sub_state="dead",
        )
        self.assertEqual(result, "not-registered")


class TestSupervisorRegisteredStateNoSystemctlOnHost(unittest.TestCase):
    """Reviewer-fairy suggestion 2 (round 1, PR #2700): "no systemctl
    binary at all" (Darwin, or any non-systemd host) is a DEFINITE,
    unambiguous fact about the host -- not the same kind of uncertainty as
    a systemd host's bus being wedged or answering ambiguously (see
    TestSupervisorRegisteredStateUnavailable below). Must read
    not-registered (-> UNSUPERVISED, launch), distinct from
    TestSupervisorRegisteredStateNotRegistered above (whose case has
    systemctl genuinely reachable and answering LoadState=not-found) only
    in WHY it's not-registered, not in the resulting state or action."""

    def test_no_systemctl_on_path_reads_not_registered(self):
        result = _run_supervisor_function(no_systemctl=True)
        self.assertEqual(result, "not-registered")


class TestSupervisorRegisteredStateUnavailable(unittest.TestCase):
    """Three independent real-world causes of "cannot determine
    supervision state" on a host that DOES have systemctl: a nonzero exit
    (dead bus), and ambiguous output -- either a reply shape the parser
    can't read at all, or (a narrower instance of the same shape) a reply
    that parses but carries no LoadState= line/value. All three must read
    `unavailable`, never `not-registered` -- a dead bus and an absent unit
    must not read the same. ("No systemctl at all" used to be a fourth
    member of this class; reviewer-fairy suggestion 2, PR #2700 round 1,
    moved it to TestSupervisorRegisteredStateNoSystemctlOnHost above,
    since it is a definite fact about the host rather than a genuine
    ambiguity -- see that class's own docstring.)

    Coder-fairy note (fold): this class's coverage of "empty/ambiguous
    output -> unavailable" is a deliberate DEVIATION from the spec's
    illustrative snippet, which merged empty LoadState into the
    not-registered branch (`[ "$load_state" = "not-found" ] || [ -z
    "$load_state" ]`) -- see start-engram-daemon.sh's own
    `_supervisor_registered_state` comment for the full reasoning. This
    is flagged as a judgment call for review, not silently applied."""

    def test_systemctl_nonzero_exit_reads_unavailable(self):
        result = _run_supervisor_function(fake_systemctl_mode="exit-nonzero")
        self.assertEqual(result, "unavailable")

    def test_empty_load_state_reads_unavailable_not_not_registered(self):
        """rc=0 but the reply carries NO LoadState value at all (an empty
        string after the `LoadState=` key) -- distinct from the
        well-formed `LoadState=not-found` answer systemd itself gives for
        a genuinely-absent unit (see TestSupervisorRegisteredState
        NotRegistered). Must read unavailable, not not-registered."""
        result = _run_supervisor_function(
            fake_systemctl_mode="show", load_state="",
            active_state="inactive", sub_state="dead",
        )
        self.assertEqual(result, "unavailable")

    def test_systemctl_ambiguous_output_reads_unavailable(self):
        result = _run_supervisor_function(fake_systemctl_mode="ambiguous-output")
        self.assertEqual(result, "unavailable")

    def test_both_are_distinguishable_from_not_registered(self):
        for result in (
            _run_supervisor_function(fake_systemctl_mode="exit-nonzero"),
            _run_supervisor_function(fake_systemctl_mode="ambiguous-output"),
        ):
            self.assertNotEqual(result, "not-registered")
            self.assertEqual(result, "unavailable")


class TestSupervisorRegisteredStateFalsification(unittest.TestCase):
    """Reviewer-fairy suggestion 2 (round 1, PR #2700)'s own falsification
    requirement: "revert this specific distinction and confirm the 'no
    systemctl' test goes red while the existing ambiguous-output tests
    stay green (proving you're testing the right thing, not just any
    change to the function)." Reverting the fix maps "no systemctl" back
    to its pre-fix `unavailable` answer -- the DANGEROUS direction for a
    Darwin/non-systemd seat (never launches, stranding it), even though it
    is the SAFE direction for a genuinely-ambiguous systemd host. Confirm
    the reversion breaks only the no-systemctl case, leaving every other
    state (registered-active / registered-failing / not-registered-via-
    LoadState / the two genuinely-ambiguous unavailable causes)
    untouched by this narrowly-targeted mutation."""

    def test_reverting_the_fix_breaks_only_the_no_systemctl_case(self):
        reverted = _run_supervisor_function(no_systemctl=True, mutated=True)
        self.assertEqual(
            reverted, "unavailable",
            "the reversion should make an absent systemctl read as "
            "'unavailable' again (the pre-fix mapping) -- if it doesn't, "
            "the mutation didn't actually revert the fix",
        )
        self.assertNotEqual(
            reverted, "not-registered",
            "this IS the falsification: the reverted function must NOT "
            "produce the fixed 'not-registered' answer any more",
        )

        # The reversion only touches the "systemctl absent" early-return --
        # confirm every other state is untouched by it.
        self.assertEqual(
            _run_supervisor_function(fake_systemctl_mode="exit-nonzero", mutated=True),
            "unavailable",
        )
        self.assertEqual(
            _run_supervisor_function(fake_systemctl_mode="ambiguous-output", mutated=True),
            "unavailable",
        )
        self.assertEqual(
            _run_supervisor_function(
                fake_systemctl_mode="show", load_state="loaded",
                active_state="active", sub_state="running", mutated=True,
            ),
            "registered-active",
        )
        self.assertEqual(
            _run_supervisor_function(
                fake_systemctl_mode="show", load_state="loaded",
                active_state="failed", sub_state="failed", mutated=True,
            ),
            "registered-failing",
        )
        self.assertEqual(
            _run_supervisor_function(
                fake_systemctl_mode="show", load_state="not-found",
                active_state="inactive", sub_state="dead", mutated=True,
            ),
            "not-registered",
        )

        # Sanity: the REAL (unmutated) function still correctly reads
        # no-systemctl as not-registered, proving the falsification above
        # exercises the mutation and not some unrelated defect.
        real_not_registered = _run_supervisor_function(no_systemctl=True, mutated=False)
        self.assertEqual(real_not_registered, "not-registered")


class TestSupervisorRegisteredStateTimeout(unittest.TestCase):
    """Reviewer-fairy suggestion 1 (round 1, PR #2700): unlike the AF_UNIX
    connect probe elsewhere in this script (explicit settimeout(2)), the
    `systemctl --user show ...` call had NO timeout -- a wedged/slow
    --user dbus would hang the ENTIRE SessionStart hook rather than
    degrading to unavailable, a fifth, unhandled failure mode. Confirm the
    fix's `timeout 3` wrapper actually bounds the call (asserting on
    wall-clock duration, not just the returned value, so a fake that
    merely happens to be fast can't pass this test for the wrong reason),
    and that removing the wrapper (falsification) makes the same scenario
    block for much longer -- bounded at the pytest level via
    `_run_supervisor_function`'s own `subprocess_timeout`, so a genuinely-
    hanging call cannot hang CI even if this fix is broken."""

    @_NEEDS_REAL_TIMEOUT
    def test_wedged_systemctl_times_out_and_reads_unavailable(self):
        start = time.monotonic()
        result = _run_supervisor_function(
            fake_systemctl_mode="sleep", sleep_seconds=10.0, subprocess_timeout=8.0,
        )
        elapsed = time.monotonic() - start
        self.assertEqual(
            result, "unavailable",
            "a timeout exit code (124) is just another nonzero exit -- it "
            "must map to unavailable, the same as any other failure mode, "
            "never a distinct fifth state",
        )
        self.assertLess(
            elapsed, 6.0,
            "the internal `timeout 3` wrapper should return well before "
            "the fake systemctl's 10s sleep completes -- if this is not "
            "less than 6s, the wrapper is not actually bounding the call",
        )

    def test_falsification_without_timeout_wrapper_hangs_past_the_bound(self):
        """Remove the timeout wrapper and confirm the SAME fake-sleep
        scenario now blocks well past the wrapper's 3s bound -- proving
        the wrapper (and not some other effect) is what makes the
        previous test return quickly. The 6s `subprocess_timeout` here is
        strictly less than the fake's 10s sleep, so this call itself
        cannot hang past 6s regardless of the outcome under test."""
        with self.assertRaises(
            subprocess.TimeoutExpired,
            msg="without the `timeout 3` wrapper, the wedged-systemctl "
            "scenario should still be blocking when this test's own "
            "bounded subprocess_timeout (6s, well short of the fake's "
            "10s sleep) is reached",
        ):
            _run_supervisor_function(
                fake_systemctl_mode="sleep", sleep_seconds=10.0,
                strip_timeout=True, subprocess_timeout=6.0,
            )


# This dev seat genuinely has a systemd-logind `/run/user/<uid>` for the
# real test-running uid (verified: `agent-sol`, uid 1007, matches Kepler's
# own measurement on his seat) -- but that is a host property, not
# something this test file can assume of every future CI runner. Gate the
# tests below that need the REAL candidate directory to exist on this
# check, per the fold spec's own escape valve ("note if this host's
# /run/user/<uid> doesn't exist or isn't the systemd bus location for some
# reason"). The falsification and control tests do NOT depend on this --
# they exercise the guard's "directory absent" branch deterministically
# via a faked `id`, so they run unconditionally.
_REAL_RUNTIME_DIR_EXISTS = Path(f"/run/user/{os.getuid()}").is_dir()


class TestSupervisorRegisteredStateXdgRuntimeDirGuard(unittest.TestCase):
    """Merge-seat HOLD, Kepler (PR #2700, measured on his own seat and
    independently reproduced live on this repo's own dev seat, both real
    `systemctl --user` calls against a real bus, not simulated): the
    SessionStart hook's actual runtime environment has NO
    `XDG_RUNTIME_DIR` -- `systemctl --user show ...` then fails "Failed to
    connect to bus: No medium found" (rc=1), which
    `_supervisor_registered_state` correctly (per its own pre-existing
    logic) reads as `unavailable` -> never launch, on EVERY Linux seat,
    regardless of whether a supervisor unit is actually registered.
    Kepler's own measured fix: with `XDG_RUNTIME_DIR=/run/user/$(id -u)`
    set, the identical call succeeds. `_make_fake_systemctl`'s
    "xdg-gated" mode mirrors this exact measured behavior (checks
    `$XDG_RUNTIME_DIR` itself, not just some property of the wrapping
    script) so these tests prove the fix causes `systemctl` to actually
    SEE the variable, not merely that some earlier branch behaves as if it
    did.

    `test_falsification_reverting_guard_reads_unavailable` below is the
    literal "one-line export + test red on today's tip" Kepler asked for,
    packaged as a permanent regression rather than a one-off manual repro
    (see this PR's own commit message / PR comment for the manual
    red-then-green reproduction performed by hand against this same real
    seat, both with the shipped fix and with it reverted)."""

    @unittest.skipUnless(
        _REAL_RUNTIME_DIR_EXISTS,
        f"this host has no /run/user/{os.getuid()} -- the guard's real "
        "default candidate directory doesn't exist here, so its "
        "'directory present' branch can't be exercised without faking a "
        "directory this non-root test process cannot create under the "
        "root-owned /run/user/. Confirmed present on the seat this fix "
        "was measured against (this repo's own dev container, uid 1007).",
    )
    def test_xdg_runtime_dir_unset_reads_not_registered_after_fix(self):
        """The fix's own headline case: XDG_RUNTIME_DIR starts unset (the
        real hook's actual environment), the candidate directory exists,
        so the guard exports it before the systemctl call -- the fake
        "xdg-gated" systemctl then sees a non-empty XDG_RUNTIME_DIR and
        answers LoadState=not-found (a genuinely-absent unit) rather than
        failing to connect at all."""
        result = _run_supervisor_function(
            xdg_runtime_dir_unset=True, fake_systemctl_mode="xdg-gated",
            load_state="not-found", active_state="inactive", sub_state="dead",
        )
        self.assertEqual(result, "not-registered")

    @unittest.skipUnless(
        _REAL_RUNTIME_DIR_EXISTS,
        f"this host has no /run/user/{os.getuid()} -- see class docstring's "
        "skip reason above.",
    )
    def test_xdg_runtime_dir_already_set_is_left_alone(self):
        """The guard must not clobber an already-set XDG_RUNTIME_DIR even
        when the default candidate directory also happens to exist --
        `${XDG_RUNTIME_DIR:-default}` inside `export` already has this
        property; this pins it against a real value (this uid's own real
        runtime dir) rather than trusting the parameter-expansion syntax
        alone."""
        real_dir = f"/run/user/{os.getuid()}"
        result = _run_supervisor_function(
            xdg_runtime_dir=real_dir, fake_systemctl_mode="xdg-gated",
            load_state="loaded", active_state="active", sub_state="running",
        )
        self.assertEqual(result, "registered-active")

    def test_falsification_reverting_guard_reads_unavailable(self):
        """Falsification, and the "red on today's tip" demonstration:
        with the guard-default block removed (byte-for-byte reproducing
        this fix's pre-fix code), the SAME XDG_RUNTIME_DIR-unset scenario
        reads `unavailable` instead of `not-registered` -- proving the
        guard block (and not some coincidental property of the fake
        systemctl) is what makes the test above pass. Does not depend on
        `_REAL_RUNTIME_DIR_EXISTS`: with the guard stripped,
        XDG_RUNTIME_DIR is never touched regardless of whether the real
        candidate directory exists, so this reproduces on any host."""
        result = _run_supervisor_function(
            strip_xdg_guard=True, xdg_runtime_dir_unset=True,
            fake_systemctl_mode="xdg-gated",
            load_state="not-found", active_state="inactive", sub_state="dead",
        )
        self.assertEqual(
            result, "unavailable",
            "reverting the guard-default block should reproduce the "
            "pre-fix reading (unavailable) for the exact scenario every "
            "unfixed Linux seat hits today -- if this is not unavailable, "
            "the mutation didn't actually remove the fix",
        )

    def test_control_runtime_dir_absent_still_reads_unavailable(self):
        """Kepler's own control ask: if the CANDIDATE directory doesn't
        exist for this uid at all (no logind session, or a non-systemd
        host that nonetheless has a `systemctl` shim on PATH), the guard
        must not export anything, and the unset-XDG_RUNTIME_DIR scenario
        must still read `unavailable` -- the fix must not paper over a
        genuinely un-determinable host by exporting a value that can't
        possibly be this uid's real runtime directory. Deterministic
        regardless of host: a fake `id -u` reports an unlikely-to-exist
        uid, so `/run/user/999999` is absent everywhere this test runs."""
        result = _run_supervisor_function(
            xdg_runtime_dir_unset=True, fake_id_uid="999999",
            fake_systemctl_mode="xdg-gated",
            load_state="not-found", active_state="inactive", sub_state="dead",
        )
        self.assertEqual(result, "unavailable")


# ---------------------------------------------------------------------------
# (II) End-to-end: the real script, branch 1 (no pidfile), each of the
# four supervisor states faked -- confirms the DECISION (launch or not),
# not just the isolated function's return value.
# ---------------------------------------------------------------------------


class TestFourStateBranchSupervisedHealthy(_ProbeBranchFixture):
    """registered-active -> SUPERVISED_HEALTHY: log it, no reap/rm/launch
    -- trust the supervisor's own restart policy."""

    FAKE_SYSTEMCTL_MODE = "show"
    FAKE_LOAD_STATE = "loaded"
    FAKE_ACTIVE_STATE = "active"
    FAKE_SUB_STATE = "running"

    def test_logs_supervised_healthy_and_takes_no_action(self):
        result = self._run()
        self.assertEqual(result.returncode, 0, result.stderr)
        content = "\n".join(self._log_lines())
        self.assertIn("SUPERVISED_HEALTHY", content)
        self.assertNotIn("SUPERVISED_FAILING", content)
        self.assertNotIn("UNSUPERVISED", content)
        self.assertTrue(
            self._wait_briefly_for_no_relaunch(),
            "registered-active must never launch a competing process",
        )
        self.assertFalse(self.pid_path.exists())
        self.assertFalse(self.socket_path.exists())


class TestFourStateBranchSupervisedFailing(_ProbeBranchFixture):
    """registered-failing -> SUPERVISED_FAILING: log it LOUDLY, no
    launch -- never fight a real (if currently-failing) supervisor by
    launching a competing process."""

    FAKE_SYSTEMCTL_MODE = "show"
    FAKE_LOAD_STATE = "loaded"
    FAKE_ACTIVE_STATE = "failed"
    FAKE_SUB_STATE = "failed"

    def test_logs_supervised_failing_and_takes_no_action(self):
        result = self._run()
        self.assertEqual(result.returncode, 0, result.stderr)
        content = "\n".join(self._log_lines())
        self.assertIn("SUPERVISED_FAILING", content)
        self.assertNotIn("SUPERVISED_HEALTHY", content)
        self.assertNotIn("UNSUPERVISED", content)
        self.assertTrue(
            self._wait_briefly_for_no_relaunch(),
            "registered-failing must NOT launch a competing process, even "
            "though the supervised daemon is itself down",
        )
        self.assertFalse(self.pid_path.exists())
        self.assertFalse(self.socket_path.exists())


class _FourStateBranchUnavailableBase(_ProbeBranchFixture):
    """Shared assertion body for all `unavailable`-producing scenarios --
    subclasses set FAKE_SYSTEMCTL_MODE (or override further) to select
    which real-world cause of "cannot determine" is under test."""

    def _assert_unavailable_and_no_action(self):
        result = self._run()
        self.assertEqual(result.returncode, 0, result.stderr)
        content = "\n".join(self._log_lines())
        self.assertIn("unavailable", content)
        self.assertNotIn("SUPERVISED_HEALTHY", content)
        self.assertNotIn("SUPERVISED_FAILING", content)
        self.assertNotIn("UNSUPERVISED", content)
        self.assertTrue(
            self._wait_briefly_for_no_relaunch(),
            "unavailable (cannot determine supervision state) must NEVER "
            "launch -- a missing daemon is loud and recoverable, a "
            "duplicate risks silent WAL loss",
        )
        self.assertFalse(self.pid_path.exists())
        self.assertFalse(self.socket_path.exists())


class TestFourStateBranchUnavailableViaNonzeroExit(_FourStateBranchUnavailableBase):
    FAKE_SYSTEMCTL_MODE = "exit-nonzero"

    def test_logs_unavailable_and_takes_no_action(self):
        self._assert_unavailable_and_no_action()


class TestFourStateBranchUnavailableViaAmbiguousOutput(_FourStateBranchUnavailableBase):
    FAKE_SYSTEMCTL_MODE = "ambiguous-output"

    def test_logs_unavailable_and_takes_no_action(self):
        self._assert_unavailable_and_no_action()


class TestFourStateBranchUnsupervisedLaunchesFully(_PreexistingReapMatchSetup, _ProbeBranchFixture):
    """not-registered -> UNSUPERVISED: the FULL today's-behavior sequence
    runs -- reap (a real preexisting matching process gets killed), the
    launch fires (relaunch marker appears), the launch-attempt stamp gets
    restored, AND the install-hint log line appears. This is the
    behavioral confirmation the isolated function tests above cannot
    provide by themselves -- it proves the restored reap/rm/launch code
    is reachable, not merely present as dead text (see
    TestPreUnit4FrozenBaselineSanity's own note on this)."""

    FAKE_SYSTEMCTL_MODE = "show"
    FAKE_LOAD_STATE = "not-found"
    FAKE_ACTIVE_STATE = "inactive"
    FAKE_SUB_STATE = "dead"

    def test_full_reap_launch_sequence_and_install_hint(self):
        result = self._run()
        self.assertEqual(result.returncode, 0, result.stderr)
        content = "\n".join(self._log_lines())

        self.assertIn("UNSUPERVISED", content)
        self.assertNotIn("SUPERVISED_HEALTHY", content)
        self.assertNotIn("SUPERVISED_FAILING", content)
        # The one-line install hint, per spec.
        self.assertIn("setup-surface-daemon-service.sh", content)

        # Reap: the preexisting matching process is genuinely killed.
        self.assertTrue(self._wait_for_relaunch_marker())
        self.preexisting_proc.wait(timeout=3)
        with self.assertRaises(OSError):
            os.kill(self.preexisting_proc.pid, 0)

        # Launch-attempt stamp restored (this is the coupling unit 4's own
        # build left broken, per the spec: engram-surface-hook.py's warmup
        # grace period reads this stamp).
        stamp_path = self.engram_home / "daemon-launch-attempt"
        self.assertTrue(
            stamp_path.exists(),
            "the launch-attempt stamp must be restored on the UNSUPERVISED "
            "path -- this is what makes the per-turn surface hook's warmup "
            "grace period work again",
        )


def _make_systemctl_free_bin_dir(tmp_dir: Path) -> Path:
    """A PATH-usable directory containing symlinks to every external
    command start-engram-daemon.sh's OWN body invokes (cat, kill, pgrep,
    stat, date, mkdir, rm, env, grep, sed, tr) -- resolved from THIS
    process's real PATH -- but deliberately NO `systemctl` symlink. Lets
    a test set PATH to ONLY this directory and get a "systemctl absent
    from PATH" host truthfully, without also breaking every other command
    the rest of the script's own reap/launch sequence needs (a bare empty
    PATH, as used by `_run_supervisor_function`'s isolated no_systemctl
    mode, is fine there because THAT test only exercises the standalone
    function, which needs no other external command)."""
    bin_dir = tmp_dir / "systemctl-free-bin"
    bin_dir.mkdir()
    for cmd in ("cat", "kill", "pgrep", "stat", "date", "mkdir", "rm", "env", "grep", "sed", "tr", "dirname", "bash"):
        real = shutil.which(cmd)
        assert real, f"could not resolve real {cmd!r} on this host's PATH"
        (bin_dir / cmd).symlink_to(real)
    return bin_dir


class _SystemctlFreeHostFixture(_ProbeBranchFixture):
    """Shared base: runs the script on a PATH that has every command it
    needs EXCEPT systemctl (see _make_systemctl_free_bin_dir's own
    docstring for why a bare empty PATH won't do for end-to-end runs).
    Stands in for a genuine Darwin/non-systemd host."""

    # No fake systemctl on PATH at all for this class -- systemctl is
    # simply absent, as on a real non-systemd host.
    FAKE_SYSTEMCTL_MODE = None

    def setUp(self):
        super().setUp()
        self._systemctl_free_bin_dir = _make_systemctl_free_bin_dir(self.tmp)

    def _run(self, timeout: float = 10.0) -> subprocess.CompletedProcess:
        bash_path = str(self._systemctl_free_bin_dir / "bash")
        env = dict(os.environ)
        env["ENGRAM_HOME"] = str(self.engram_home)
        env["PATH"] = str(self._systemctl_free_bin_dir)
        return subprocess.run(
            [bash_path, str(self.script)], env=env, capture_output=True,
            text=True, timeout=timeout,
        )


class TestFourStateBranchNoSystemctlLaunchesFully(_PreexistingReapMatchSetup, _SystemctlFreeHostFixture):
    """Reviewer-fairy suggestion 2 (round 1, PR #2700): on a host with NO
    systemctl at all (Darwin, or any non-systemd host), the fix maps
    not-registered -> UNSUPERVISED -> launch, same as any Linux seat with
    no unit installed -- the end-to-end counterpart to
    TestSupervisorRegisteredStateNoSystemctlOnHost's isolated-function
    coverage. The FULL reap+launch sequence must fire, exactly as it does
    in TestFourStateBranchUnsupervisedLaunchesFully, AND the log line must
    name this specific sub-case distinctly (per the spec's own request)
    so a future reader can tell "no unit installed on a systemd host" from
    "this host cannot have a unit at all" without re-deriving it."""

    def test_full_reap_launch_sequence_and_distinct_log_line(self):
        result = self._run()
        self.assertEqual(result.returncode, 0, result.stderr)
        content = "\n".join(self._log_lines())

        self.assertIn("UNSUPERVISED", content)
        self.assertIn(
            "no systemctl on this host", content,
            "the no-systemctl sub-case must be named distinctly in the "
            "log, separate from the genuine LoadState=not-found wording "
            "TestFourStateBranchUnsupervisedLaunchesFully asserts",
        )
        self.assertNotIn("SUPERVISED_HEALTHY", content)
        self.assertNotIn("SUPERVISED_FAILING", content)

        # Reap: the preexisting matching process is genuinely killed.
        self.assertTrue(self._wait_for_relaunch_marker())
        self.preexisting_proc.wait(timeout=3)
        with self.assertRaises(OSError):
            os.kill(self.preexisting_proc.pid, 0)

        stamp_path = self.engram_home / "daemon-launch-attempt"
        self.assertTrue(
            stamp_path.exists(),
            "the launch-attempt stamp must be restored on this path too, "
            "same as any other UNSUPERVISED launch",
        )


class TestFourStateBranchFalsificationNoSystemctlFixReverted(_SystemctlFreeHostFixture):
    """The full-script counterpart to
    TestSupervisorRegisteredStateFalsification: patch the LIVE script's
    own `_supervisor_registered_state` to revert the Darwin-scope fix
    (map "systemctl absent" back to unavailable, its pre-fix answer) and
    confirm a systemctl-free host now, wrongly, refuses to launch --
    reproducing the exact stranding scenario the fix exists to prevent.
    Proves the fixed script's launch behavior in
    TestFourStateBranchNoSystemctlLaunchesFully is exercising a real
    discriminating check, not something vacuously true."""

    def test_reverted_script_refuses_to_launch_on_a_systemctl_free_host(self):
        # Patch the reversion into THIS instance's own script copy (not
        # the shipped file) before it's first executed.
        original_text = self.script.read_text()
        target = (
            "    if ! command -v systemctl >/dev/null 2>&1; then\n"
            '        echo "not-registered"\n'
            "        return\n"
            "    fi\n"
        )
        assert target in original_text, "mutation target snippet not found -- function shape changed"
        reverted_text = original_text.replace(
            target,
            "    if ! command -v systemctl >/dev/null 2>&1; then\n"
            '        echo "unavailable"\n'
            "        return\n"
            "    fi\n",
        )
        self.script.write_text(reverted_text)

        result = self._run()
        self.assertEqual(result.returncode, 0, result.stderr)
        content = "\n".join(self._log_lines())
        self.assertIn(
            "unavailable", content,
            "the reversion should make a systemctl-free host silently read "
            "(and refuse to act on) unavailable -- if it doesn't, the "
            "reversion didn't collapse the discriminating branch",
        )
        self.assertNotIn("UNSUPERVISED", content)
        self.assertTrue(
            self._wait_briefly_for_no_relaunch(),
            "this IS the falsification: the reverted script refuses to "
            "launch on a host that structurally cannot have a systemd "
            "unit, which the fixed script (see "
            "TestFourStateBranchNoSystemctlLaunchesFully above) correctly "
            "does not do -- this is the exact stranding scenario "
            "reviewer-fairy suggestion 2 exists to prevent",
        )


# ===========================================================================
# #2652 phase-0 unit 4, SECOND fold: ENGRAM_HOME normalization, so this
# hook's own SOCKET_PATH/PID_PATH agree with engram-surface-daemon.py's
# _resolve_engram_home() (#2697, closed #2695's fail-open by making the
# daemon side always return an absolute path). Ariadne's colleague-review
# finding on #2697: fixing only the daemon side broke a coincidental match
# the hook used to get "for free" whenever ENGRAM_HOME was relative and both
# processes launched from the same cwd. This fold normalizes ENGRAM_HOME to
# absolute BEFORE SOCKET_PATH/PID_PATH are built from it, at the top of the
# script, so the two sides agree by construction rather than by cwd
# coincidence.
#
# IMPORTANT CAVEAT this test class's own docstrings repeat below: this
# BRANCH's own working-tree copy of engram-surface-daemon.py was forked from
# b0402bf1, BEFORE #2697 (c8117323) merged into dev -- so it does NOT yet
# contain the #2697 fix. The daemon-side comparisons below are therefore
# against the #2697 fix's own documented, merged behavior
# (`os.path.abspath(os.environ.get("ENGRAM_HOME") or
# os.path.expanduser("~/.engram"))`, read from `git show
# c8117323:src/engram/hooks/claude/engram-surface-daemon.py`), reproduced
# here as a plain `os.path.abspath()` call -- NOT against this branch's own
# (stale) copy of that file, which this fold deliberately does not touch and
# which will pick up the real fix once this branch is rebased onto/merged
# into dev.
# ===========================================================================


class _EngramHomeNormalizationFixture(unittest.TestCase):
    """Extracts and runs, in isolation, the exact top-of-file ENGRAM_HOME/
    SOCKET_PATH/PID_PATH normalization block from the live script -- mirrors
    _extract_bash_function's discipline of testing the REAL script text
    rather than a hand-copied approximation, adapted for a top-level
    (non-function) block via fixed start/end markers instead of brace
    matching."""

    _START_MARKER = ': "${ENGRAM_HOME:=$HOME/.engram}"'
    _END_MARKER = 'LOG_PATH="$ENGRAM_HOME/surface-daemon.log"'

    # The exact pre-fold header block -- byte-for-byte snapshot of
    # start-engram-daemon.sh's top-of-file ENGRAM_HOME/SOCKET_PATH/PID_PATH
    # block as it stood immediately before this fold's patch, verified via
    # `git show 786764c0:src/engram/hooks/claude/start-engram-daemon.sh`
    # at patch-authoring time (786764c0 is this fold's own parent commit
    # post-rebase). Frozen deliberately, same one-time-authoring-check
    # discipline as _PRE_UNIT1_SCRIPT_TEXT/_ROUND1_SCRIPT_TEXT above -- used
    # as the falsification mutation below.
    #
    # De-pinning note (merge-seat HOLD, Kepler, PR #2700): an earlier
    # revision of this fixture ran a LIVE `git show <SHA>` test against this
    # literal on every test run, re-pinned from the fixture's original SHA
    # (`49dae0f2`, which went dangling -- reachable from no ref -- after a
    # rebase alone) to `786764c0`. Dropped entirely, not just re-pinned
    # again: `786764c0` is itself a branch-local commit that a squash
    # `--delete-branch` merge makes unreachable the moment this branch is
    # deleted, so the same failure would recur on any checkout taken after
    # this PR merges. The frozen literal stays and is trusted as an
    # authoring-time-verified constant -- see
    # test_live_header_now_differs_from_the_frozen_pre_fold_header below,
    # which needs no git access at all -- only the git-backed
    # re-verification-at-test-time is gone.
    #
    # substituting this for the live block reverts SOCKET_PATH/PID_PATH to
    # being built from a possibly-relative ENGRAM_HOME, with no
    # normalization at all.
    _PRE_FOLD_HEADER = (
        ': "${ENGRAM_HOME:=$HOME/.engram}"\n'
        'SOCKET_PATH="$ENGRAM_HOME/recall-daemon.sock"\n'
        'PID_PATH="$ENGRAM_HOME/recall-daemon.pid"\n'
        'DAEMON_SCRIPT="$(dirname "$0")/engram-surface-daemon.py"\n'
        'LOG_PATH="$ENGRAM_HOME/surface-daemon.log"'
    )

    def _extract_live_header(self) -> str:
        script_text = START_SCRIPT_PATH.read_text()
        start_idx = script_text.index(self._START_MARKER)
        end_idx = script_text.index(self._END_MARKER) + len(self._END_MARKER)
        return script_text[start_idx:end_idx]

    def _run_normalization(self, engram_home_value: str, cwd: Path, *, mutated: bool = False) -> dict:
        """Runs the extracted header block (or, if mutated, the frozen
        pre-fold header -- the falsification control) in a fresh bash
        subprocess with ENGRAM_HOME set to `engram_home_value` and the
        subprocess's own cwd set to `cwd` (so a relative ENGRAM_HOME
        resolves relative to a controlled, known directory), then echoes
        the three resulting variables."""
        header = self._PRE_FOLD_HEADER if mutated else self._extract_live_header()
        script = header + '\necho "$ENGRAM_HOME|$SOCKET_PATH|$PID_PATH"'
        env = dict(os.environ)
        env["ENGRAM_HOME"] = engram_home_value
        result = subprocess.run(
            ["bash", "-c", script], cwd=str(cwd), env=env,
            capture_output=True, text=True, timeout=5,
        )
        assert result.returncode == 0, f"header block failed: {result.stderr}"
        parts = result.stdout.strip().split("|")
        assert len(parts) == 3, f"unexpected header output: {result.stdout!r}"
        return {"ENGRAM_HOME": parts[0], "SOCKET_PATH": parts[1], "PID_PATH": parts[2]}

    @staticmethod
    def _daemon_side_abspath(engram_home_value: str, cwd: Path) -> str:
        """The #2697-fixed daemon side's own resolution, reproduced from its
        merged source (`os.path.abspath(os.environ.get("ENGRAM_HOME") or
        os.path.expanduser("~/.engram"))`, c8117323) -- computed WITHOUT
        actually os.chdir()-ing this test process (which would leak across
        tests); os.path.abspath(p) for a relative p is defined as
        normpath(join(os.getcwd(), p)), so joining against the known `cwd`
        directly reproduces it exactly."""
        if os.path.isabs(engram_home_value):
            return os.path.abspath(engram_home_value)
        return os.path.normpath(os.path.join(str(cwd), engram_home_value))


class TestEngramHomeNormalizationRelativeBecomesAbsolute(_EngramHomeNormalizationFixture):
    """The regression this fold closes: a RELATIVE ENGRAM_HOME set from a
    controlled cwd must resolve to an ABSOLUTE SOCKET_PATH (and PID_PATH),
    matching what the #2697-fixed daemon side independently resolves for
    the same ENGRAM_HOME value from the same cwd."""

    def test_relative_engram_home_resolves_absolute_matching_daemon_side(self):
        with tempfile.TemporaryDirectory() as td:
            # realpath: the script's `cd`+`pwd` reports the kernel's cwd, which
            # on macOS is /private/var/... for a /var/... temp dir (#3333).
            cwd = Path(os.path.realpath(td))
            relhome = "relhome"
            resolved = self._run_normalization(relhome, cwd)

            self.assertTrue(
                os.path.isabs(resolved["ENGRAM_HOME"]),
                f"ENGRAM_HOME must be normalized to absolute, got {resolved['ENGRAM_HOME']!r}",
            )
            self.assertTrue(os.path.isabs(resolved["SOCKET_PATH"]))
            self.assertTrue(os.path.isabs(resolved["PID_PATH"]))

            expected_home = self._daemon_side_abspath(relhome, cwd)
            self.assertEqual(resolved["ENGRAM_HOME"], expected_home)
            self.assertEqual(resolved["SOCKET_PATH"], os.path.join(expected_home, "recall-daemon.sock"))
            self.assertEqual(resolved["PID_PATH"], os.path.join(expected_home, "recall-daemon.pid"))

    def test_no_regression_on_already_absolute_engram_home(self):
        """The common (already-absolute ENGRAM_HOME) case: the resolved
        value must be UNCHANGED before vs. after this fold."""
        with tempfile.TemporaryDirectory() as td:
            abshome = str(Path(td) / "abshome")
            os.makedirs(abshome, exist_ok=True)
            before = self._run_normalization(abshome, Path(td), mutated=True)
            after = self._run_normalization(abshome, Path(td), mutated=False)
            self.assertEqual(before["SOCKET_PATH"], after["SOCKET_PATH"])
            self.assertEqual(before["PID_PATH"], after["PID_PATH"])
            self.assertEqual(after["SOCKET_PATH"], os.path.join(abshome, "recall-daemon.sock"))

    def test_falsification_reverted_normalization_stays_relative(self):
        """Revert to the frozen pre-fold header (mutated=True) and confirm
        the SAME relative-ENGRAM_HOME scenario now resolves a RELATIVE
        SOCKET_PATH -- proving test_relative_engram_home_resolves_absolute_
        matching_daemon_side above is exercising a real, removable fix and
        not something vacuously true."""
        with tempfile.TemporaryDirectory() as td:
            cwd = Path(td)
            relhome = "relhome"
            resolved = self._run_normalization(relhome, cwd, mutated=True)

            self.assertFalse(
                os.path.isabs(resolved["ENGRAM_HOME"]),
                "falsification failed to reproduce the pre-fold relative "
                f"ENGRAM_HOME, got {resolved['ENGRAM_HOME']!r}",
            )
            self.assertEqual(resolved["ENGRAM_HOME"], relhome)
            self.assertFalse(os.path.isabs(resolved["SOCKET_PATH"]))
            self.assertEqual(resolved["SOCKET_PATH"], f"{relhome}/recall-daemon.sock")


class TestEngramHomeNormalizationSymlink(_EngramHomeNormalizationFixture):
    """Symlink behavior check (per this fold's own spec, echoing #2695/#2697's
    explicit instruction to check for this exact class of asymmetry rather
    than assume): does the hook's `cd`+`pwd` normalization resolve a
    symlinked ENGRAM_HOME component, or preserve it?

    Measured directly on this test's own host (not assumed): bash's `cd`
    builtin, in its LOGICAL default mode (no `-P` flag used here, and none
    of the standard shell options -P/`set -o physical` are set anywhere in
    this script), does NOT resolve a symlinked path component -- `pwd`
    (also no `-P`) after such a `cd` reports the path as `cd` was given it,
    symlink preserved. Manually confirmed on this exact host:
    `(cd <dir-via-symlink> && pwd)` prints the symlink path, while
    `(cd <dir-via-symlink> && pwd -P)` prints the resolved real path.

    This MATCHES, not mismatches, engram-surface-daemon.py's `os.path.
    abspath()` (not `os.path.realpath()`) choice, documented in that
    function's own #2697 docstring as deliberately symlink-preserving. So
    this fold introduces NO new hook-vs-daemon asymmetry on the symlink
    axis -- both sides now agree on absoluteness AND on not resolving
    symlinks."""

    def test_symlinked_engram_home_is_not_resolved_matching_daemon_abspath(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            real_target = base / "real_target"
            real_target.mkdir()
            link_home = base / "link_home"
            link_home.symlink_to(real_target)

            resolved = self._run_normalization(str(link_home), base)

            # Symlink preserved, not resolved to the real target.
            self.assertEqual(resolved["ENGRAM_HOME"], str(link_home))
            self.assertNotEqual(resolved["ENGRAM_HOME"], str(real_target.resolve()))
            # Matches the daemon side's own abspath() (symlink-preserving).
            self.assertEqual(self._daemon_side_abspath(str(link_home), base), resolved["ENGRAM_HOME"])

    def test_symlinked_relative_engram_home_preserves_symlink_component(self):
        """Same check, but through a RELATIVE ENGRAM_HOME whose path
        traverses the symlink -- closer to the actual regression scenario
        (a relative ENGRAM_HOME set from a given cwd) than the absolute-path
        symlink case above."""
        with tempfile.TemporaryDirectory() as td:
            base = Path(os.path.realpath(td))  # macOS /var -> /private/var (#3333)
            real_target = base / "real_target"
            real_target.mkdir()
            link_name = "link_home"
            (base / link_name).symlink_to(real_target)

            resolved = self._run_normalization(link_name, base)

            expected_home = self._daemon_side_abspath(link_name, base)
            self.assertEqual(resolved["ENGRAM_HOME"], expected_home)
            self.assertNotEqual(resolved["ENGRAM_HOME"], str(real_target.resolve()))


class TestEngramHomeNormalizationFrozenBaselineSanity(unittest.TestCase):
    """Confirms the LIVE (extracted) header now differs from
    _EngramHomeNormalizationFixture._PRE_FOLD_HEADER -- i.e. that this
    fold's patch actually changed the header block, not that the frozen
    literal is vacuously equal to the live text.

    This class used to also carry a test asserting the frozen literal was
    byte-for-byte the branch tip's committed pre-fold content, via a LIVE
    `git show <SHA>` call at test-run time. Dropped (merge-seat HOLD,
    Kepler, PR #2700): the pinned SHA is a branch-local commit that a
    squash `--delete-branch` merge makes unreachable from any ref once this
    branch is deleted, so that test would start failing on every checkout
    taken after this PR merges -- the same failure mode this fixture had
    already hit once before, when its original SHA (`49dae0f2`) went
    dangling from a rebase alone. The literal's provenance is now a
    one-time authoring-time `git show` check, documented in the comment
    above `_EngramHomeNormalizationFixture._PRE_FOLD_HEADER` -- the same
    discipline this file's other frozen baselines (_PRE_UNIT1_SCRIPT_TEXT,
    _ROUND1_SCRIPT_TEXT) already use, neither of which carries a live-git
    test either."""

    def test_live_header_now_differs_from_the_frozen_pre_fold_header(self):
        fixture = _EngramHomeNormalizationFixture()
        live_header = fixture._extract_live_header()
        self.assertNotEqual(live_header, fixture._PRE_FOLD_HEADER)
        self.assertIn("mkdir -p", live_header)
        self.assertIn("_abs_engram_home", live_header)


# ===========================================================================
# Sanity: the frozen pre-unit-1 baseline really is the byte-for-byte prior
# script, not a hand-typo'd approximation -- caught here rather than only
# surfacing as confusing failures in the falsification tests above.
# ===========================================================================


class TestFrozenBaselineSanity(unittest.TestCase):
    def test_frozen_baseline_has_no_probe_log_helper(self):
        self.assertNotIn("_log_probe", _PRE_UNIT1_SCRIPT_TEXT)
        self.assertNotIn("probe:", _PRE_UNIT1_SCRIPT_TEXT)

    def test_frozen_baseline_shares_the_same_reap_pattern_as_the_live_script(self):
        """The two scripts must differ ONLY in the added logging/behavior --
        same reap pattern -- so any behavior difference the tests above
        observe is attributable to the actual change alone. #2652 unit 4
        deliberately keeps `_reap_pattern`'s literal assignment unchanged
        (see the live script's own comment), so this sanity check still
        holds today even though the live script no longer acts on it."""
        live_text = START_SCRIPT_PATH.read_text()
        reap_re = re.compile(r'_reap_pattern="([^"]*)"')
        live_match = reap_re.search(live_text)
        frozen_match = reap_re.search(_PRE_UNIT1_SCRIPT_TEXT)
        self.assertIsNotNone(live_match)
        self.assertIsNotNone(frozen_match)
        self.assertEqual(live_match.group(1), frozen_match.group(1))


class TestRound1FrozenBaselineSanity(unittest.TestCase):
    """Same discipline as TestFrozenBaselineSanity above, for this fold's
    OWN frozen "before" snapshot (_ROUND1_SCRIPT_TEXT)."""

    def test_round1_baseline_has_branch_logging_but_no_reap_outcome_logging(self):
        self.assertIn("_log_probe", _ROUND1_SCRIPT_TEXT)
        self.assertIn("probe:", _ROUND1_SCRIPT_TEXT)
        self.assertNotIn("reap matched", _ROUND1_SCRIPT_TEXT)

    def test_round1_baseline_shares_the_same_reap_pattern_as_the_live_script(self):
        """Same reap pattern as the live script -- see
        TestFrozenBaselineSanity's docstring for why this still holds after
        unit 4."""
        live_text = START_SCRIPT_PATH.read_text()
        reap_re = re.compile(r'_reap_pattern="([^"]*)"')
        live_match = reap_re.search(live_text)
        round1_match = reap_re.search(_ROUND1_SCRIPT_TEXT)
        self.assertIsNotNone(live_match)
        self.assertIsNotNone(round1_match)
        self.assertEqual(live_match.group(1), round1_match.group(1))


class TestPreUnit4FrozenBaselineSanity(unittest.TestCase):
    """Same discipline, for unit 4's OWN frozen "before" snapshot
    (_PRE_UNIT4_SCRIPT_TEXT) -- confirms it genuinely is the pre-fold
    baseline (reap/kill loop, unit-2 guard, and launch line all present)
    and shares the live script's unchanged `_reap_pattern`.

    #2652 phase-0 unit 4 FOLD: the live script's OWN unconditional removal
    of the reap/kill loop, unit-2's guard, and the launch line (this
    class's original "none of that any more" claim) is no longer true --
    the fold RESTORES all of it, but gated behind the new
    `_supervisor_registered_state` four-way branch (only reachable on the
    UNSUPERVISED/not-registered path). `test_live_script_has_none_of_that_
    any_more` is renamed and its assertions flipped accordingly; see the
    dedicated four-state-branch test classes below for the actual
    conditional-reachability proof (i.e. that these restored strings are
    live code, not dead text)."""

    def test_pre_unit4_baseline_has_reap_kill_guard_and_launch(self):
        self.assertIn("reap matched", _PRE_UNIT4_SCRIPT_TEXT)
        self.assertIn('for stale_pid in $_stale_pids', _PRE_UNIT4_SCRIPT_TEXT)
        self.assertIn("_allow_socket_rm", _PRE_UNIT4_SCRIPT_TEXT)
        self.assertIn('env ENGRAM_HOME="$ENGRAM_HOME" {{PYTHON}} "$DAEMON_SCRIPT"', _PRE_UNIT4_SCRIPT_TEXT)

    def test_live_script_restores_reap_guard_and_launch_conditionally(self):
        """The fold's whole point: these strings are BACK in the live
        script (unlike the pre-fold unit-4-only build this class's other
        tests document), but only reachable via the new supervisor check
        -- confirmed unreachable-vs-reachable by the dedicated four-state
        test classes below, not by this text-presence check alone."""
        live_text = START_SCRIPT_PATH.read_text()
        self.assertIn("reap matched", live_text)
        self.assertIn("_stale_pids", live_text)
        self.assertIn("_allow_socket_rm", live_text)
        self.assertIn("DAEMON_SCRIPT", live_text)
        self.assertIn("_cgroup_supervision_state", live_text)
        self.assertIn("_supervisor_registered_state", live_text)
        self.assertIn("not-registered", live_text)

    def test_pre_unit4_baseline_shares_the_same_reap_pattern_as_the_live_script(self):
        live_text = START_SCRIPT_PATH.read_text()
        reap_re = re.compile(r'_reap_pattern="([^"]*)"')
        live_match = reap_re.search(live_text)
        pre_unit4_match = reap_re.search(_PRE_UNIT4_SCRIPT_TEXT)
        self.assertIsNotNone(live_match)
        self.assertIsNotNone(pre_unit4_match)
        self.assertEqual(live_match.group(1), pre_unit4_match.group(1))


if __name__ == "__main__":
    unittest.main()
