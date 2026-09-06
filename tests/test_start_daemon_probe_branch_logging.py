"""Tests for #2652 phase-0 unit 1 — start-engram-daemon.sh logs which
liveness-probe branch fired on each invocation.

Borges's §4 (issue #2652) left two candidates unresolved for why a probe
fails on a given restart: (1) the previous daemon died silently, or (2) the
pidfile is absent at probe time because the daemon writes it only after its
warmup embedding call, so a SessionStart landing inside another daemon's
warmup window sees no pidfile and reaps a healthy, still-warming daemon. His
proposed fix for this unit is one log line per invocation naming exactly
which of the four liveness-probe branches fired -- additive only, no change
to the reap/relaunch or healthy-exit behavior.

The four branches (see start-engram-daemon.sh's own inline comments for the
reap-pattern reasoning this unit deliberately does not touch):

  1. No pidfile at all                          -> reap+relaunch
  2. Pidfile present, kill -0 fails (stale)      -> reap+relaunch
  3. Pidfile present, process alive, socket dead -> reap+relaunch
  4. Pidfile present, process alive, socket OK   -> healthy, exit 0, no-op

This file invokes the REAL script (via subprocess, {{PYTHON}} substituted)
against a controlled temp ENGRAM_HOME for each of the four branches, and
also runs the same four scenarios against a byte-for-byte frozen copy of
the PRE-#2652-unit-1 script (no logging at all) to prove the assertions are
actually exercising the new lines and not something else that happens to be
true anyway (e.g. exit code, or the reap/relaunch side effects, which are
unchanged and would pass regardless of whether the logging exists).

Discovered while writing this unit, filed separately as #2666: the
socket-probe python heredoc's `except:` (bare) swallowed the SystemExit
its own `sys.exit(0)` success path raised, making branch 4's "healthy,
no-op" outcome UNREACHABLE against the shipped script -- a live, listening
socket got misdiagnosed as branch 3 and the daemon still reaped+relaunched.
Out of scope for #2652 phase-0 unit 1 itself (additive logging only) and
unrelated to the reap-pattern units (2-4), so it shipped as a separate PR
(#2669, `except:` -> `except Exception:`), which has since merged --
branch 4 is now reachable against the real script. Two things are tested
for branch 4 accordingly:

  - TestBranch4Healthy: verifies THIS unit's own branch-4 logging + no-op
    exit directly against the real (now-fixed) script -- also the phase-0
    unit 1 assertion for a genuinely responsive daemon: branch 4 logs and
    the daemon is NOT relaunched.
  - TestBranch4AgainstPreUnit1Script: falsification control against the
    frozen pre-#2652-unit-1 baseline, which predates #2666's fix -- still
    needs the test-only patch (PATCH_OUT_2666) to neutralize that older
    bug and reach the healthy path in isolation, proving the branch-4 log
    line comes from THIS unit's own code and not from the shared
    socket-probe logic.

(The characterization test that used to pin the pre-fix misdiagnosis,
TestBranch4RealScriptCurrentlyMisdiagnosesHealthyAsBranch3, has been
retired per its own docstring's built-in escape valve: "if this test
starts failing because the healthy line now appears, that means #2666 has
been fixed upstream -- update/retire this test rather than treating it as
a regression." #2666 is fixed upstream; the test is retired.)
"""

from __future__ import annotations

import os
import re
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


def _spawn_sleeper(seconds: float = 5.0):
    """A real, short-lived subprocess with a genuine, confirmable PID
    (mirrors tests/test_surface_daemon_pid_reap.py's _spawn_sleeper)."""
    return subprocess.Popen([sys.executable, "-c", f"import time; time.sleep({seconds})"])


def _spawn_reap_pattern_match(stub_daemon_path: Path, engram_home: Path, seconds: float = 8.0):
    """A real, long-lived subprocess whose argv matches this fixture's
    `_reap_pattern` (contains "engram-surface-daemon.py" and ends with
    ENGRAM_HOME) WITHOUT going through the script under test -- standing in
    for a leftover daemon process from a prior invocation (or this
    invocation's own pidfile-not-yet-written race), which the reap section
    is expected to find and kill on THIS run. `stub_daemon_path` and
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
    """Test-only isolation of #2666 (found while building this unit, filed
    separately, NOT fixed here): the socket-probe heredoc's bare `except:`
    swallows the SystemExit its own `sys.exit(0)` success path raises, so
    branch 4 ("healthy, no-op") is unreachable against the real script.
    This neutralizes JUST that one line -- `except:` -> `except OSError:`,
    so `SystemExit` propagates normally -- to make branch 4 reachable in
    isolation, so THIS unit's own added logging for that branch can be
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


class _ProbeBranchFixture(unittest.TestCase):
    """Shared setup: a real, executable copy of start-engram-daemon.sh (or a
    frozen pre-instrumentation variant) with {{PYTHON}} substituted, next to
    a stub engram-surface-daemon.py so the reap/relaunch path never touches
    the real daemon (which would load a real embedding model)."""

    SCRIPT_TEXT: str | None = None  # None -> use the live source file
    PATCH_OUT_2666: bool = False  # isolate #2666 for branch-4 reachability

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

    def _run(self, timeout: float = 10.0) -> subprocess.CompletedProcess:
        env = dict(os.environ)
        env["ENGRAM_HOME"] = str(self.engram_home)
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
        redirects into LOG_PATH -- its presence is direct evidence the
        reap+relaunch section ran (unchanged by this unit)."""
        return any("stub daemon started" in line for line in self._log_lines())

    def _wait_for_relaunch_marker(self, timeout: float = 5.0) -> bool:
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self._relaunch_happened():
                return True
            time.sleep(0.1)
        return False


def _branch_regex(branch: int, *, pid: int | None = None, pid_path: Path | None = None) -> re.Pattern:
    if branch == 1:
        assert pid_path is not None
        pattern = (
            rf"\[{_TIMESTAMP_RE}\] \[start-engram-daemon\] probe: "
            rf"no pidfile at {re.escape(str(pid_path))}"
        )
    elif branch == 2:
        assert pid is not None
        pattern = (
            rf"\[{_TIMESTAMP_RE}\] \[start-engram-daemon\] probe: "
            rf"pidfile present \(pid={pid}\) but process not running"
        )
    elif branch == 3:
        assert pid is not None
        pattern = (
            rf"\[{_TIMESTAMP_RE}\] \[start-engram-daemon\] probe: "
            rf"pidfile present \(pid={pid}\), process alive, "
            rf"socket probe failed/timed out"
        )
    elif branch == 4:
        assert pid is not None
        pattern = (
            rf"\[{_TIMESTAMP_RE}\] \[start-engram-daemon\] probe: "
            rf"healthy \(pid={pid}\), no action"
        )
    else:
        raise ValueError(branch)
    return re.compile(pattern)


def _reap_regex(count: int | None, *, pid: int | None = None) -> re.Pattern:
    """count=None -> the explicit "matched none" line this fold requires
    (zero and unknown must not collapse into an absent line); otherwise the
    "matched N stale process(es): ..." line, optionally requiring a
    specific pid appear in the trailing pid list."""
    prefix = rf"\[{_TIMESTAMP_RE}\] \[start-engram-daemon\] probe: reap matched "
    if count is None:
        return re.compile(prefix + r"none\b")
    pattern = prefix + rf"{count} stale process\(es\):"
    if pid is not None:
        pattern += rf".*\b{pid}\b"
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

    def test_reap_relaunch_unchanged(self):
        self._run()
        self.assertTrue(
            self._wait_for_relaunch_marker(),
            "branch 1 (no pidfile) must still fall through to reap+relaunch, unchanged",
        )


class TestBranch1AgainstPreUnit1Script(_ProbeBranchFixture):
    """Falsification control: the identical branch-1 scenario, run against
    the frozen pre-#2652-unit-1 script. Proves TestBranch1NoPidfile's
    assertion is exercising the new log line and not something incidental
    (e.g. the exit code or the reap/relaunch side effect, both of which are
    unchanged and would still pass here)."""

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
        # Same reap/relaunch side effect fires either way -- confirms the
        # two scripts differ ONLY in the logging, not in behavior.
        self.assertTrue(self._wait_for_relaunch_marker())


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

    def test_reap_relaunch_unchanged(self):
        self._run()
        self.assertTrue(
            self._wait_for_relaunch_marker(),
            "branch 2 (stale pidfile) must still fall through to reap+relaunch, unchanged",
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


# ===========================================================================
# Branch 3 -- pidfile present, process alive, socket probe fails
# ===========================================================================


class TestBranch3LiveProcessDeadSocket(_ProbeBranchFixture):
    def setUp(self):
        super().setUp()
        # A real, alive process -- kill -0 must succeed. No socket is bound
        # at self.socket_path, so the python connect-probe must fail.
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

        self.assertRegex(content, _branch_regex(3, pid=self.live_proc.pid))
        self.assertNotIn("probe: no pidfile", content)
        self.assertNotIn("but process not running", content)
        self.assertNotIn("probe: healthy", content)

    def test_reap_relaunch_unchanged(self):
        self._run()
        self.assertTrue(
            self._wait_for_relaunch_marker(),
            "branch 3 (live process, dead socket) must still fall through "
            "to reap+relaunch, unchanged",
        )


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

        self.assertNotRegex(content, _branch_regex(3, pid=self.live_proc.pid))
        self.assertNotIn("[start-engram-daemon] probe:", content)
        self.assertTrue(self._wait_for_relaunch_marker())


# ===========================================================================
# Branch 4 -- pidfile present, process alive, socket responds
#
# #2666 (found while building this unit, filed separately as its own PR
# #2669): the socket-probe heredoc's bare `except:` swallowed the
# SystemExit its own `sys.exit(0)` success path raised, making this branch
# UNREACHABLE against the shipped script -- see the module docstring. #2669
# has since merged (`except:` -> `except Exception:`), so branch 4 is now
# reachable against the real script. Two test classes: one verifies this
# unit's own branch-4 logging directly against the real (fixed) script, one
# is a falsification control against the frozen pre-#2652-unit-1 baseline
# (which predates the #2666 fix and still needs the test-only patch below).
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
    """Verifies THIS unit's own branch-4 logging + no-op exit, run directly
    against the real script -- #2666 is fixed upstream (#2669, merged), so
    no test-only patch is needed to reach this branch any more. This is
    also the phase-0 unit 1 real assertion: a genuinely responsive
    connection (bound+listening; see the mixin's docstring for what that
    does and doesn't prove) logs branch 4 and the daemon is NOT relaunched.
    Supersedes the retired TestBranch4RealScriptCurrentlyMisdiagnosesHealthyAsBranch3
    characterization test -- see the module docstring."""

    def test_logs_branch4_line_and_no_other_branch_line(self):
        result = self._run()
        self.assertEqual(result.returncode, 0, result.stderr)
        content = "\n".join(self._log_lines())

        self.assertRegex(content, _branch_regex(4, pid=self.live_proc.pid))
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
    TestBranch4Healthy comes from THIS unit's added code, not from the
    (also patched-here) socket-probe logic, which is shared by both
    scripts and produces the same no-op behavior either way."""

    SCRIPT_TEXT = _PRE_UNIT1_SCRIPT_TEXT
    PATCH_OUT_2666 = True

    def test_reverted_script_writes_no_probe_line_at_all(self):
        result = self._run()
        self.assertEqual(result.returncode, 0, result.stderr)
        content = "\n".join(self._log_lines())

        self.assertNotRegex(content, _branch_regex(4, pid=self.live_proc.pid))
        self.assertNotIn("[start-engram-daemon] probe:", content)
        # Healthy early-exit behavior (no reap, process left alone) is
        # unchanged either way -- only the log line differs.
        self.assertFalse(self._relaunch_happened())
        self.assertIsNone(self.live_proc.poll())


# ===========================================================================
# #2652 phase-0 unit 1, round-2 fold (PR #2667, Ariadne's colleague-review
# blocker): the reap section itself was unlogged, so branch 1's "no
# pidfile" line could not distinguish a genuine cold start / the
# pidfile-not-yet-written race from a start that lands after a leftover
# daemon just got reaped. This fold adds one `_log_probe` call reporting
# whether the reap loop matched/killed anything -- the discriminating datum
# (>=1 killed means a healthy daemon got reaped; 0 means neither reap
# candidate fired). Additive-only: the kill loop's own behavior (which
# pids get killed, in what order) is unchanged -- see
# TestReapOutcomeMatchesLiveProcess, which asserts the matched process is
# actually killed, not just logged as matched.
# ===========================================================================


class _PreexistingReapMatchSetup:
    """Mixin: spawns a REAL, already-running process matching
    `_reap_pattern` BEFORE the script under test ever runs -- standing in
    for a leftover daemon left behind by a prior invocation (or this
    invocation's own pidfile-not-yet-written race), which this run's reap
    section is expected to find and kill."""

    def setUp(self):
        super().setUp()
        self.preexisting_proc = _spawn_reap_pattern_match(self.stub_daemon, self.engram_home)
        self.addCleanup(self._kill_preexisting_if_alive)
        # Confirm it's genuinely alive before the run -- a dead-on-arrival
        # process would make "matched none" the CORRECT outcome instead of
        # the one under test.
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


class TestReapOutcomeZeroMatches(_ProbeBranchFixture):
    """Branch 1 (no pidfile) fall-through, nothing else running -- the reap
    loop matches zero processes. Per the fold spec's property 4: zero
    matched and "unknown" must not collapse into an absent line -- the log
    must say "matched none" explicitly."""

    def test_logs_reap_matched_none(self):
        result = self._run()
        self.assertEqual(result.returncode, 0, result.stderr)
        content = "\n".join(self._log_lines())

        self.assertRegex(content, _reap_regex(None))
        self.assertNotRegex(content, re.compile(r"reap matched \d"))
        self.assertTrue(
            self._wait_for_relaunch_marker(),
            "reap+relaunch must still fire, unchanged, alongside the new logging",
        )


class TestReapOutcomeZeroMatchesAgainstRound1Script(_ProbeBranchFixture):
    """Falsification control: the identical zero-match scenario, run
    against the frozen round-1 script (branch-line logging present,
    reap-outcome logging absent). Proves TestReapOutcomeZeroMatches's
    assertion is exercising THIS fold's new line, not something round 1
    already produced."""

    SCRIPT_TEXT = _ROUND1_SCRIPT_TEXT

    def test_round1_script_writes_no_reap_outcome_line(self):
        result = self._run()
        self.assertEqual(result.returncode, 0, result.stderr)
        content = "\n".join(self._log_lines())

        self.assertNotIn("reap matched", content)
        # round-1's own branch-1 line must still be present -- confirms
        # this really is round 1's script, not an unrelated break.
        self.assertRegex(content, _branch_regex(1, pid_path=self.pid_path))
        self.assertTrue(self._wait_for_relaunch_marker())


class TestReapOutcomeMatchesLiveProcess(_PreexistingReapMatchSetup, _ProbeBranchFixture):
    """Branch 1 (no pidfile) fall-through, with a real process already
    matching `_reap_pattern` before this invocation runs -- e.g. a leftover
    daemon from a prior invocation. The reap loop must both kill it
    (unchanged behavior) AND this fold's new line must report it was
    matched, turning branch 1's line into a discriminating observation."""

    def test_logs_reap_matched_and_actually_kills_the_matched_process(self):
        result = self._run()
        self.assertEqual(result.returncode, 0, result.stderr)
        content = "\n".join(self._log_lines())

        self.assertRegex(content, _reap_regex(1, pid=self.preexisting_proc.pid))
        self.assertNotRegex(content, _reap_regex(None))

        # Existing reap-verification: this run's OWN fresh relaunch must
        # still fire, unchanged, alongside the new logging.
        self.assertTrue(
            self._wait_for_relaunch_marker(),
            "reap+relaunch must still fire, unchanged, alongside the new logging",
        )
        # The PRE-EXISTING process the reap loop matched must actually be
        # dead afterward -- logging a match is not the same as killing it.
        self.preexisting_proc.wait(timeout=3)
        with self.assertRaises(OSError):
            os.kill(self.preexisting_proc.pid, 0)


class TestReapOutcomeNonzeroMatchesAgainstRound1Script(
    _PreexistingReapMatchSetup, _ProbeBranchFixture
):
    """Falsification control for the nonzero-match case: the identical
    preexisting-process scenario, run against the frozen round-1 script --
    proves the "reap matched N ..." line in
    TestReapOutcomeMatchesLiveProcess comes from THIS fold, not from round
    1's kill loop (which killed the process just as effectively but never
    logged it -- same kill-loop code, unchanged by this fold either way)."""

    SCRIPT_TEXT = _ROUND1_SCRIPT_TEXT

    def test_round1_script_kills_but_does_not_log_the_match(self):
        result = self._run()
        self.assertEqual(result.returncode, 0, result.stderr)
        content = "\n".join(self._log_lines())

        self.assertNotIn("reap matched", content)
        self.assertTrue(self._wait_for_relaunch_marker())
        # Round 1's kill loop is untouched by this fold -- it still kills
        # the preexisting process, just silently.
        self.preexisting_proc.wait(timeout=3)
        with self.assertRaises(OSError):
            os.kill(self.preexisting_proc.pid, 0)


class TestReapOutcomeNotLoggedOnHealthyEarlyExit(_Branch4BoundAndListeningSetup, _ProbeBranchFixture):
    """Branch 4 (healthy) exits before the reap section is ever reached --
    the reap-outcome line must NOT appear. Confirms property 2 from the
    fold spec isn't accidentally violated (the new line firing on branch D
    too, which never reaches the reap section in the first place). Runs
    directly against the real script -- #2666 is fixed upstream (#2669),
    so no test-only patch is needed to reach branch 4 any more."""

    def test_no_reap_outcome_line_on_healthy_early_exit(self):
        result = self._run()
        self.assertEqual(result.returncode, 0, result.stderr)
        content = "\n".join(self._log_lines())

        self.assertRegex(content, _branch_regex(4, pid=self.live_proc.pid))
        self.assertNotIn("reap matched", content)


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
        """The two scripts must differ ONLY in the added logging -- same
        reap pattern -- so any behavior difference the tests above observe
        is attributable to the logging change alone."""
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
        """The two scripts must differ ONLY in the added reap-outcome
        logging -- same reap pattern -- so any behavior difference the
        tests above observe is attributable to the logging change alone."""
        live_text = START_SCRIPT_PATH.read_text()
        reap_re = re.compile(r'_reap_pattern="([^"]*)"')
        live_match = reap_re.search(live_text)
        round1_match = reap_re.search(_ROUND1_SCRIPT_TEXT)
        self.assertIsNotNone(live_match)
        self.assertIsNotNone(round1_match)
        self.assertEqual(live_match.group(1), round1_match.group(1))


if __name__ == "__main__":
    unittest.main()
