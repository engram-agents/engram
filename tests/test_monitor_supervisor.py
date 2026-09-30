"""Tests for monitor-supervisor.py (engram-alpha#2982 step 1).

Contract-first: written before monitor-supervisor.py existed (see the PR's
own handoff for the "observed failing before implementation" record). Each
test below names, in its own docstring/comment, the exact mutation that
should turn it red -- these mirror the spec's own "Tests" section verbatim.

REAL vs FAKE children -- and why (required by the spec to be stated here):

  - `test_tagging_with_real_children` and `test_teardown_kills_real_children`
    run the supervisor against the THREE REAL scripts
    (forum-updates-monitor.sh, forum-mention-monitor.sh, umwelt-bell.py),
    pointed at an unreachable loopback address (http://127.0.0.1:1,
    guaranteed-refused -- nothing listens there) under a scratch $HOME. All
    three scripts are DESIGNED to run forever retrying against a down
    forum/world, printing one informative line on the first failed seed
    attempt (verified live against each script before writing these tests --
    see the PR handoff) -- so this exercises the real, unmodified production
    invocations end-to-end without needing a live forum or umwelt world
    server, and without ever becoming a real production instance (unique
    --purpose / cursor-file / seen-file paths scoped under a fresh tmp_path
    per test, per the isolation convention in
    test_forum_monitor_duplicate_warn.py's own module docstring).
  - The other seven tests use a small CONTROLLABLE fake child (written to a
    temp file per-test by `_write_fake_child`) instead of the real scripts,
    because they each need to force a precise, fast, deterministic condition
    a real child would not reliably or safely produce within a unit-test
    budget: an exact kill-and-restart moment (test 4), a guaranteed-quiet
    channel for a timed idle window (test 8), a guaranteed backlog burst of
    an exact known size (test 9), concurrent long-line writers with no
    network dependency (test 3), a controlled stdout/stderr pair (test 2),
    and two supervisor lifecycle tests that only care about process-group
    membership, not child content (tests 6, 7). None of these touch
    forum-updates-monitor.sh / forum-mention-monitor.sh / umwelt-bell.py --
    those three files are out of this change's scope and are referenced only
    (never edited) by the two real-children tests above.

Round 2 (#2982 step 1 review) adds two more tests, both process-lifecycle
(no fake-child script needed):

  - `test_reap_refuses_to_kill_unrelated_process_on_pid_reuse` -- spawns a
    REAL, plain OS process (`sleep`, its own process-group leader, exactly
    like the shells/tmux-panes/other-supervisors the round-1 review named as
    "routine" pid-reuse candidates) and writes ITS pid into the pidfile,
    standing in for the OS having recycled a pid the pidfile still names
    since it was last written. A genuine monitor-supervisor.py must refuse
    to kill it and say so loudly, never SIGKILL it on a bare "something is
    alive at this pid" check.
  - `test_graceful_shutdown_exits_cleanly_and_removes_pidfile` -- confirms
    Suggestion 1 (teardown()'s final unconditional kill only fires if
    children are still alive after the grace period).

Round 3 (post-approval coverage gap, PR #3177) adds two more tests, closing
the two branches the reviewer-fairy confirmed were untested (each verified
by the reviewer to stay green with its own branch body replaced by a no-op):

  - `test_reap_refuses_when_identity_cannot_be_verified` -- forces
    `_pid_cmdline_status()` into its "unknown" branch (not the "ok"/mismatch
    branch test 10 already covers) by launching the supervisor with a `PATH`
    stripped down to an executable-free scratch directory, so its internal
    `subprocess.run(["ps", ...])` probe itself raises `FileNotFoundError`.
    A real, plain OS process's pid (same `sleep`-standin as test 10) is
    recorded in the pidfile; the supervisor must refuse to reap it and warn
    loudly, exactly like the confirmed-mismatch case, rather than treating
    "could not tell" as "safe to kill".
  - `test_forced_sigkill_after_grace_period_fires` -- gives one fake child
    the new "ignore-sigterm" mode (installs `SIG_IGN` for `SIGTERM`) so the
    ordinary graceful-shutdown path (test 11) cannot apply; `teardown()`'s
    final `os.killpg(pgid, SIGKILL)` fallback must still fire once the ~2.0s
    grace period elapses, taking the whole group (including the supervisor
    itself, since it shares its children's pgid) down with it.

Isolation (same discipline as test_forum_monitor_duplicate_warn.py's own
docstring, which this suite is modeled on): every test uses a unique,
per-test UUID fragment baked into every fake child's own argv (so
`pgrep -f <uuid>` can never match a real production monitor-supervisor
instance or another test's children), and every file-based path (pidfile,
status file, cursor file, seen file) lives under pytest's per-test
`tmp_path`, never a real shared production path.
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[1]
_SUPERVISOR_SCRIPT = _REPO_ROOT / "src" / "engram" / "tools" / "monitor-supervisor.py"
_UPDATES_SCRIPT = _REPO_ROOT / "src" / "engram" / "tools" / "forum-updates-monitor.sh"
_MENTION_SCRIPT = _REPO_ROOT / "src" / "engram" / "tools" / "forum-mention-monitor.sh"
_BELL_SCRIPT = Path.home() / ".umwelt" / "marketplace" / "plugins" / "umwelt" / "tools" / "umwelt-bell.py"

_UNREACHABLE_URL = "http://127.0.0.1:1"  # nothing listens here -> instant ECONNREFUSED


# ---------------------------------------------------------------------------
# Fake controllable child (tests 2, 3, 4, 6, 7, 8, 9 -- see module docstring)
# ---------------------------------------------------------------------------
_FAKE_CHILD_SRC = '''
import signal
import sys
import time


def main():
    tag = sys.argv[1]  # unused except to make this process pgrep -f -able
    mode = sys.argv[2]
    if mode == "quiet":
        while True:
            time.sleep(3600)
    elif mode == "ignore-sigterm":
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        while True:
            time.sleep(3600)
    elif mode == "hello-sleep":
        print("hello", flush=True)
        while True:
            time.sleep(3600)
    elif mode == "burst":
        n = int(sys.argv[3])
        for i in range(n):
            print(f"line-{i}", flush=True)
        while True:
            time.sleep(3600)
    elif mode == "stdout-stderr":
        i = 0
        while True:
            print(f"OUT-{i}", flush=True)
            print(f"ERR-{i}", file=sys.stderr, flush=True)
            i += 1
            time.sleep(0.05)
    elif mode == "long-lines":
        marker = sys.argv[3]
        n = int(sys.argv[4]) if len(sys.argv) > 4 else 300
        text = marker * 2000
        for _ in range(n):
            print(text, flush=True)
        while True:
            time.sleep(3600)
    else:
        raise SystemExit(f"fake_child: unknown mode {mode!r}")


if __name__ == "__main__":
    main()
'''


def _write_fake_child(tmp_path: Path) -> Path:
    p = tmp_path / "fake_child.py"
    p.write_text(_FAKE_CHILD_SRC)
    return p


def _fake_cmd(fake_script: Path, tag: str, mode: str, *extra) -> str:
    argv = [sys.executable, str(fake_script), tag, mode, *[str(e) for e in extra]]
    return json.dumps(argv)


def _uid() -> str:
    return uuid.uuid4().hex[:10]


def _count_matching(marker: str, exclude_pids: frozenset[int] = frozenset()) -> int:
    r = subprocess.run(["pgrep", "-f", marker], capture_output=True, text=True)
    pids = [int(p) for p in r.stdout.split()]
    return len([p for p in pids if p not in exclude_pids])


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _wait_for_status_children(status_file: Path, n: int, timeout: float = 10.0) -> dict:
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        try:
            data = json.loads(status_file.read_text())
            children = data.get("children", {})
            if len(children) == n and all(c.get("pid") for c in children.values()):
                return data
            last = data
        except Exception:
            pass
        time.sleep(0.1)
    raise AssertionError(f"status file never reached {n} children in time; last seen: {last}")


class _SupervisorHarness:
    """Launches monitor-supervisor.py as a real subprocess and continuously
    pumps its stdout/stderr into thread-safe line buffers, so tests can poll
    for a specific line without racing a one-shot `communicate()` (these
    processes are persistent Monitors by design and never exit on their
    own -- see the module docstring)."""

    def __init__(self, args: list[str], env: dict | None = None,
                 cmd_prefix: list[str] | None = None):
        cmd = [*(cmd_prefix or []), sys.executable, str(_SUPERVISOR_SCRIPT), *args]
        self.proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, env=env,
        )
        self._lock = threading.Lock()
        self._stdout_lines: list[str] = []
        self._stderr_lines: list[str] = []
        self._t_out = threading.Thread(target=self._pump, args=(self.proc.stdout, self._stdout_lines), daemon=True)
        self._t_err = threading.Thread(target=self._pump, args=(self.proc.stderr, self._stderr_lines), daemon=True)
        self._t_out.start()
        self._t_err.start()

    def _pump(self, pipe, sink):
        for line in iter(pipe.readline, ""):
            with self._lock:
                sink.append(line.rstrip("\n"))
        try:
            pipe.close()
        except Exception:
            pass

    def snapshot(self) -> tuple[list[str], list[str]]:
        with self._lock:
            return list(self._stdout_lines), list(self._stderr_lines)

    def wait_for_stdout(self, predicate, timeout: float = 10.0):
        return self._wait_for(lambda: self.snapshot()[0], predicate, timeout)

    def wait_for_stderr(self, predicate, timeout: float = 10.0):
        return self._wait_for(lambda: self.snapshot()[1], predicate, timeout)

    @staticmethod
    def _wait_for(getter, predicate, timeout):
        deadline = time.time() + timeout
        while time.time() < deadline:
            for line in getter():
                if predicate(line):
                    return line
            time.sleep(0.05)
        return None

    def terminate(self, timeout: float = 5.0):
        try:
            self.proc.terminate()
            self.proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            self.proc.wait(timeout=timeout)
        except ProcessLookupError:
            pass


def _base_fake_args(tmp_path: Path, uid: str, fake: Path, *, status_interval: str = "5") -> list[str]:
    """Three quiet fake children + scratch pidfile/status-file -- the common
    starting point every fake-child test overrides one or more legs of."""
    return [
        "--bell-cmd-json", _fake_cmd(fake, f"{uid}-bell", "quiet"),
        "--updates-cmd-json", _fake_cmd(fake, f"{uid}-updates", "quiet"),
        "--mention-cmd-json", _fake_cmd(fake, f"{uid}-mention", "quiet"),
        "--pidfile", str(tmp_path / "sup.pid"),
        "--status-file", str(tmp_path / "status.json"),
        "--status-interval", status_interval,
    ]


def _real_children_env(tmp_path: Path, uid: str) -> dict:
    scratch_home = tmp_path / "home"
    (scratch_home / ".engram").mkdir(parents=True)
    (scratch_home / ".engram" / "config.json").write_text(
        json.dumps({"agent_name": f"test-{uid}"})
    )
    env = os.environ.copy()
    env["HOME"] = str(scratch_home)
    env["FORUM_URL"] = _UNREACHABLE_URL
    env["FORUM_UPDATES_NO_REAP"] = "1"
    env["FORUM_MENTION_NO_REAP"] = "1"
    env["FORUM_UPDATES_POLL_SECONDS"] = "1"
    env["FORUM_MENTION_POLL_SECONDS"] = "1"
    env["FORUM_MENTION_MAX_TIME_SECONDS"] = "2"
    env["FORUM_UPDATES_CURSOR_FILE"] = str(tmp_path / "cursor-updates.json")
    env["FORUM_MENTION_SEEN_FILE"] = str(tmp_path / "seen-mention.json")
    return env


def _real_children_args(tmp_path: Path, uid: str) -> list[str]:
    return [
        "--purpose", f"supervisortest-{uid}",
        "--umwelt-agent", f"test-bell-{uid}",
        "--umwelt-world-url", _UNREACHABLE_URL,
        "--umwelt-cursor-file", str(tmp_path / "bell-cursors.json"),
        "--umwelt-interval", "1",
        # The child env's $HOME is overridden to a scratch dir (see
        # _real_children_env) so the forum scripts never touch a real
        # ~/.engram/config.json -- but that means the supervisor's own
        # DEFAULT_UMWELT_BELL_SCRIPT (computed from Path.home() at import
        # time, inside that same scratch-$HOME child process) would resolve
        # under the scratch home too, where no umwelt install exists. Point
        # explicitly at the REAL deployed umwelt-bell.py so this stays a
        # genuine real-child test rather than accidentally depending on a
        # fake stand-in.
        "--bell-script", str(_BELL_SCRIPT),
        "--pidfile", str(tmp_path / "sup.pid"),
        "--status-file", str(tmp_path / "status.json"),
        "--status-interval", "0.5",
    ]


@pytest.fixture(autouse=True)
def _require_bell_script():
    if not _BELL_SCRIPT.exists():
        pytest.skip(f"umwelt-bell.py not found at {_BELL_SCRIPT} -- skipping "
                    "real-children tests that need it")


# ---------------------------------------------------------------------------
# 1. Tagging -- REAL children
# Mutation: drop the tag prefix in Supervisor._reader -> test must go red.
# ---------------------------------------------------------------------------
def test_tagging_with_real_children(tmp_path):
    uid = _uid()
    env = _real_children_env(tmp_path, uid)
    args = _real_children_args(tmp_path, uid)
    h = _SupervisorHarness(args, env=env)
    try:
        got_bell = h.wait_for_stdout(lambda l: l.startswith("[bell] "), timeout=15)
        got_updates = h.wait_for_stdout(lambda l: l.startswith("[updates] "), timeout=15)
        got_mention = h.wait_for_stdout(lambda l: l.startswith("[mention] "), timeout=15)
        assert got_bell, "no [bell]-tagged stdout line seen from the real umwelt-bell.py"
        assert got_updates, "no [updates]-tagged stdout line seen from the real forum-updates-monitor.sh"
        assert got_mention, "no [mention]-tagged stdout line seen from the real forum-mention-monitor.sh"
    finally:
        h.terminate()


# ---------------------------------------------------------------------------
# 2. Stream split -- FAKE child (mode "stdout-stderr")
# Mutation: merge with 2>&1 (route the stderr reader to sys.stdout) -> red.
# ---------------------------------------------------------------------------
def test_stream_split_fake_child(tmp_path):
    uid = _uid()
    fake = _write_fake_child(tmp_path)
    args = list(_base_fake_args(tmp_path, uid, fake))
    # override just the "updates" leg with the stdout/stderr-emitting fake
    idx = args.index("--updates-cmd-json") + 1
    args[idx] = _fake_cmd(fake, f"{uid}-updates", "stdout-stderr")

    h = _SupervisorHarness(args)
    try:
        out_line = h.wait_for_stdout(lambda l: "[updates] OUT-" in l, timeout=5)
        err_line = h.wait_for_stderr(lambda l: "[updates] ERR-" in l, timeout=5)
        assert out_line, "expected an OUT- line forwarded to supervisor stdout"
        assert err_line, "expected an ERR- line forwarded to supervisor stderr"
        time.sleep(0.3)
        out_snap, err_snap = h.snapshot()
        assert not any("ERR-" in l for l in out_snap), \
            f"a stderr line leaked onto stdout: {[l for l in out_snap if 'ERR-' in l]}"
        assert not any("OUT-" in l for l in err_snap), \
            f"a stdout line leaked onto stderr: {[l for l in err_snap if 'OUT-' in l]}"
    finally:
        h.terminate()


# ---------------------------------------------------------------------------
# 3. Line atomicity -- FAKE children (mode "long-lines", two concurrent writers)
# Mutation: forward by arbitrary chunk instead of by line -> red.
# ---------------------------------------------------------------------------
def test_line_atomicity_fake_children(tmp_path):
    uid = _uid()
    fake = _write_fake_child(tmp_path)
    args = [
        "--bell-cmd-json", _fake_cmd(fake, f"{uid}-bell", "quiet"),
        "--updates-cmd-json", _fake_cmd(fake, f"{uid}-updates", "long-lines", "A", 300),
        "--mention-cmd-json", _fake_cmd(fake, f"{uid}-mention", "long-lines", "B", 300),
        "--pidfile", str(tmp_path / "sup.pid"),
        "--status-file", str(tmp_path / "status.json"),
        "--status-interval", "5",
    ]
    h = _SupervisorHarness(args)
    try:
        time.sleep(2.0)  # let both children spew concurrently
        out, _ = h.snapshot()
        payload_lines = [
            l.split(" ", 1)[1]
            for l in out
            if l.startswith("[updates] ") or l.startswith("[mention] ")
        ]
        assert len(payload_lines) > 20, \
            f"not enough lines captured to meaningfully test atomicity: {len(payload_lines)}"
        for content in payload_lines:
            chars = set(content)
            assert chars <= {"A"} or chars <= {"B"}, \
                f"spliced/interleaved line detected (mixes A and B): {content[:120]!r}..."
    finally:
        h.terminate()


# ---------------------------------------------------------------------------
# 4. Loud death + restart -- FAKE child (mode "hello-sleep")
# Mutation: suppress the EXITED announcement -> red.
# ---------------------------------------------------------------------------
def test_loud_death_and_restart_fake_child(tmp_path):
    uid = _uid()
    fake = _write_fake_child(tmp_path)
    status_file = tmp_path / "status.json"
    args = _base_fake_args(tmp_path, uid, fake, status_interval="0.2")
    idx = args.index("--bell-cmd-json") + 1
    args[idx] = _fake_cmd(fake, f"{uid}-bell", "hello-sleep")

    h = _SupervisorHarness(args)
    try:
        assert h.wait_for_stdout(lambda l: l == "[bell] hello", timeout=5), \
            "fake bell child's initial line never arrived"
        data = _wait_for_status_children(status_file, 3, timeout=5)
        old_pid = data["children"]["bell"]["pid"]
        assert old_pid

        os.kill(old_pid, signal.SIGKILL)

        exited_line = h.wait_for_stdout(
            lambda l: l == f"[supervisor] CHILD bell EXITED code=-9", timeout=5,
        )
        assert exited_line, "no loud EXITED announcement seen on supervisor stdout"
        restart_line = h.wait_for_stdout(
            lambda l: l.startswith("[supervisor] restarting bell in"), timeout=5,
        )
        assert restart_line, "no restart-attempt announcement seen on supervisor stdout"

        deadline = time.time() + 10
        new_pid = None
        while time.time() < deadline:
            try:
                st = json.loads(status_file.read_text())
            except Exception:
                st = {}
            p = st.get("children", {}).get("bell", {}).get("pid")
            if p and p != old_pid:
                new_pid = p
                break
            time.sleep(0.1)
        assert new_pid, "bell child was never restarted with a new pid"
        assert _pid_alive(new_pid), "restarted bell child's pid is not alive"
    finally:
        h.terminate()


# ---------------------------------------------------------------------------
# 5. Teardown -- REAL children (process-group kill is child-content-agnostic;
#    using the real scripts here demonstrates the supervisor's actual
#    production invocations tear down cleanly, not just a fake stand-in).
# Mutation: skip the process-group kill in Supervisor.teardown() -> red
# (an orphan survives).
# ---------------------------------------------------------------------------
def test_teardown_kills_real_children(tmp_path):
    uid = _uid()
    env = _real_children_env(tmp_path, uid)
    args = _real_children_args(tmp_path, uid)
    purpose_marker = f"supervisortest-{uid}"
    bell_marker = str(tmp_path / "bell-cursors.json")

    h = _SupervisorHarness(args, env=env)
    try:
        assert h.wait_for_stdout(lambda l: l.startswith("[bell] "), timeout=15)
        assert h.wait_for_stdout(lambda l: l.startswith("[updates] "), timeout=15)
        assert h.wait_for_stdout(lambda l: l.startswith("[mention] "), timeout=15)
        sup_pid = h.proc.pid
        # sanity: children are genuinely alive before teardown
        assert _count_matching(purpose_marker, exclude_pids=frozenset({sup_pid})) == 2
        assert _count_matching(bell_marker, exclude_pids=frozenset({sup_pid})) == 1

        h.proc.send_signal(signal.SIGTERM)
        h.proc.wait(timeout=5)
    finally:
        # Safety net: if any assertion ABOVE fails before the explicit
        # SIGTERM is sent, the supervisor (and its real children) would
        # otherwise leak as an orphaned, PPID=1 process on this shared host
        # forever -- terminate() is a no-op if it's already exited (Popen
        # tolerates terminate()/wait() on an already-dead process), so this
        # is safe to call unconditionally.
        h.terminate()

    time.sleep(0.5)
    assert _count_matching(purpose_marker) == 0, \
        "a forum monitor child survived supervisor teardown"
    assert _count_matching(bell_marker) == 0, \
        "the umwelt-bell child survived supervisor teardown"


# ---------------------------------------------------------------------------
# 6. SIGKILL recovery -- FAKE children (process-lifecycle test; content of
#    the children is irrelevant, so a fast fake keeps this deterministic).
# Mutation: skip the start-time pidfile reap (comment out
# reap_stale_supervisor() in main()) -> red.
# ---------------------------------------------------------------------------
def test_sigkill_recovery_single_set(tmp_path):
    uid = _uid()
    fake = _write_fake_child(tmp_path)
    status_file = tmp_path / "status.json"
    args = _base_fake_args(tmp_path, uid, fake, status_interval="0.2")

    h1 = _SupervisorHarness(args)
    h2 = None
    try:
        _wait_for_status_children(status_file, 3, timeout=5)
        assert _count_matching(uid, exclude_pids=frozenset({h1.proc.pid})) == 3

        os.kill(h1.proc.pid, signal.SIGKILL)
        try:
            h1.proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pass

        time.sleep(0.3)
        assert _count_matching(uid) == 3, \
            "expected the 3 orphaned children to still be alive right after SIGKILLing the supervisor"

        h2 = _SupervisorHarness(args)
        _wait_for_status_children(status_file, 3, timeout=10)
        time.sleep(0.5)
        assert _count_matching(uid, exclude_pids=frozenset({h2.proc.pid})) == 3, \
            "expected exactly one set of 3 children after SIGKILL recovery, not two"
    finally:
        # h1 is SIGKILLed above already in the success path (a harmless
        # no-op for .terminate() at that point) -- but if an EARLIER
        # assertion in this try block fails, h1 (and its 3 real children)
        # would otherwise leak on this shared host forever. Same for h2.
        if h2 is not None:
            h2.terminate()
        h1.terminate()

    time.sleep(0.3)
    assert _count_matching(uid) == 0


# ---------------------------------------------------------------------------
# 7. Single instance -- FAKE children. No separately-named mutation in the
#    spec (it explicitly says R7 "falls out of" R6's mechanism) -- verified
#    with the SAME mutation as test 6 (skip reap_stale_supervisor()).
# ---------------------------------------------------------------------------
def test_single_instance_two_starts(tmp_path):
    uid = _uid()
    fake = _write_fake_child(tmp_path)
    status_file = tmp_path / "status.json"
    args = _base_fake_args(tmp_path, uid, fake, status_interval="0.2")

    h1 = _SupervisorHarness(args)
    h2 = None
    try:
        _wait_for_status_children(status_file, 3, timeout=5)
        assert _count_matching(uid, exclude_pids=frozenset({h1.proc.pid})) == 3

        h2 = _SupervisorHarness(args)
        _wait_for_status_children(status_file, 3, timeout=10)
        time.sleep(0.5)
        # h1 is OUR OWN direct child (spawned via subprocess.Popen), so a
        # reaped-by-h2-but-not-yet-wait()ed h1 is a ZOMBIE -- os.kill(pid, 0)
        # (what _pid_alive uses) reports zombies as "alive" too, which would
        # false-negative this check. proc.poll() is the correct liveness
        # check for a process WE are the direct parent of: it performs the
        # reap (WNOHANG waitpid) as a side effect and reports the real exit
        # status once the kernel has actually delivered it.
        deadline = time.time() + 5.0
        while h1.proc.poll() is None and time.time() < deadline:
            time.sleep(0.05)
        assert h1.proc.poll() is not None, \
            "the first supervisor instance was not reaped by the second"
        assert _count_matching(uid, exclude_pids=frozenset({h2.proc.pid})) == 3, \
            "expected exactly 3 children after the second start, not 6"
    finally:
        # If an assertion above fails before h2 reaps h1 (e.g. under the
        # skip-the-reap mutation), h1 -- and its 3 real children -- would
        # otherwise leak on this shared host forever. Same for h2.
        if h2 is not None:
            h2.terminate()
        h1.terminate()

    time.sleep(0.3)
    assert _count_matching(uid) == 0


# ---------------------------------------------------------------------------
# 8. Quiet when idle -- FAKE children, all "quiet" (no traffic at all).
# Mutation: add a periodic heartbeat print (in Supervisor._status_loop) -> red.
# ---------------------------------------------------------------------------
def test_quiet_when_idle_fake_children(tmp_path):
    uid = _uid()
    fake = _write_fake_child(tmp_path)
    args = _base_fake_args(tmp_path, uid, fake, status_interval="0.3")

    h = _SupervisorHarness(args)
    try:
        time.sleep(1.0)  # settle past any arm-time burst (there is none here)
        baseline, _ = h.snapshot()
        time.sleep(3.0)  # many multiples of the 0.3s status-write interval
        after, _ = h.snapshot()
        assert len(after) == len(baseline), \
            f"unexpected stdout during an idle window: {after[len(baseline):]}"
    finally:
        h.terminate()


# ---------------------------------------------------------------------------
# 9. Arm-time burst -- FAKE child (mode "burst"), backlog pending at start.
# Mutation: forward only the first line per read call (Supervisor._reader
# does a single readline() instead of looping) -> red.
# ---------------------------------------------------------------------------
def test_arm_time_burst_forwarded_fully(tmp_path):
    uid = _uid()
    fake = _write_fake_child(tmp_path)
    n = 30
    args = _base_fake_args(tmp_path, uid, fake)
    idx = args.index("--updates-cmd-json") + 1
    args[idx] = _fake_cmd(fake, f"{uid}-updates", "burst", n)

    h = _SupervisorHarness(args)
    try:
        deadline = time.time() + 5.0
        got: list[str] = []
        while time.time() < deadline:
            out, _ = h.snapshot()
            got = [l for l in out if l.startswith("[updates] line-")]
            if len(got) >= n:
                break
            time.sleep(0.05)
        assert len(got) == n, f"expected {n} burst lines forwarded, got {len(got)}: {got}"
        nums = sorted(int(l.split("line-")[1]) for l in got)
        assert nums == list(range(n)), "burst lines forwarded out of order or with gaps"
    finally:
        h.terminate()


# ---------------------------------------------------------------------------
# 10. Round-2 blocker fix -- reap_stale_supervisor() must REFUSE to kill a
# pid whose identity it cannot confirm as a prior monitor-supervisor, rather
# than warning-then-killing-anyway. Ported from umwelt-bell.py's
# _is_bell_process()/reap_prior() three-way branch (umwelt#205).
#
# Mutation: revert reap_stale_supervisor() to the pre-fix behaviour (identity
# check informational-only, os.killpg(old_pid, signal.SIGKILL) unconditional)
# -> red: the unrelated process gets killed and/or no refusal warning is
# printed.
# ---------------------------------------------------------------------------
def test_reap_refuses_to_kill_unrelated_process_on_pid_reuse(tmp_path):
    uid = _uid()
    fake = _write_fake_child(tmp_path)
    pidfile = tmp_path / "sup.pid"
    status_file = tmp_path / "status.json"

    # A plain, innocuous, long-running OS process that is ALSO its own
    # process-group leader (true of shells, tmux panes, other supervisors,
    # another agent's session wrapper -- the round-1 review's own list of
    # "extremely common" pid-reuse candidates) standing in for the OS having
    # recycled a pid the pidfile still names.
    unrelated = subprocess.Popen(["sleep", "300"], preexec_fn=os.setpgrp)
    try:
        pidfile.write_text(str(unrelated.pid))

        args = _base_fake_args(tmp_path, uid, fake, status_interval="0.2")
        h = _SupervisorHarness(args)
        try:
            _wait_for_status_children(status_file, 3, timeout=5)
            warn_line = h.wait_for_stderr(
                lambda l: str(unrelated.pid) in l and "not reaping" in l.lower(),
                timeout=5,
            )
            assert warn_line, (
                "expected a loud non-reap warning naming the unverified pid "
                f"{unrelated.pid} on supervisor stderr"
            )
            # `unrelated` is OUR OWN direct child (spawned via
            # subprocess.Popen above), so a SIGKILLed-but-not-yet-wait()ed
            # `unrelated` is a ZOMBIE -- os.kill(pid, 0) (what a bare
            # _pid_alive-style probe uses) reports zombies as "alive" too,
            # which would false-negative this check (see test 7's own
            # docstring for the same caveat). poll() is the correct
            # liveness check for a process we are the direct parent of: it
            # performs the reap as a side effect and reports the real exit
            # status once the kernel has delivered it.
            assert unrelated.poll() is None, (
                "the unrelated process-group leader was killed (exit code "
                f"{unrelated.returncode}) despite its cmdline not matching "
                "monitor-supervisor.py -- pid-reuse hazard"
            )
        finally:
            h.terminate()
    finally:
        try:
            os.killpg(unrelated.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        try:
            unrelated.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pass


# ---------------------------------------------------------------------------
# 11. Round-2 suggestion 1 -- teardown()'s final unconditional
# os.killpg(pgid, SIGKILL) must only fire if the SIGTERM-and-wait loop
# actually exhausted its deadline with children still alive. The supervisor
# is a member of its own process group, so an unconditional final SIGKILL
# also self-SIGKILLs the supervisor, making the os.remove(pidfile) right
# after it unreachable on the ordinary graceful-shutdown path.
#
# Mutation: drop the `if not graceful:` guard (restore the unconditional
# final os.killpg(pgid, SIGKILL)) -> red: returncode becomes a negative
# signal-shaped code (-9) and/or the pidfile survives.
# ---------------------------------------------------------------------------
def test_graceful_shutdown_exits_cleanly_and_removes_pidfile(tmp_path):
    uid = _uid()
    fake = _write_fake_child(tmp_path)
    pidfile = tmp_path / "sup.pid"
    status_file = tmp_path / "status.json"
    args = _base_fake_args(tmp_path, uid, fake, status_interval="0.2")

    h = _SupervisorHarness(args)
    try:
        _wait_for_status_children(status_file, 3, timeout=5)
        assert pidfile.exists()

        h.proc.send_signal(signal.SIGTERM)
        h.proc.wait(timeout=5)

        assert h.proc.returncode == 0, (
            f"expected a clean exit(0) on graceful shutdown, got "
            f"{h.proc.returncode} (a negative signal-shaped code means the "
            f"supervisor SIGKILLed its own process group instead of exiting "
            f"normally)"
        )
        assert not pidfile.exists(), (
            "pidfile was not removed -- unreachable os.remove() on the "
            "graceful-shutdown path"
        )
    finally:
        h.terminate()

    time.sleep(0.3)
    assert _count_matching(uid) == 0


# ---------------------------------------------------------------------------
# 12. Round-3 coverage gap (PR #3177 reviewer-fairy finding) -- the R6
# identity-UNKNOWN branch (`_pid_cmdline_status()` returns "unknown" because
# the `ps` probe itself raised) is distinct from test 10's confirmed-MISMATCH
# branch and was previously exercised by no test at all. `reap_stale_
# supervisor()` must refuse to reap and warn loudly, exactly as it does for a
# confirmed mismatch -- "could not tell" must never collapse into "safe to
# kill".
#
# Forcing the branch: `_pid_cmdline_status()` calls
# `subprocess.run(["ps", ...])` with no explicit env override, so it inherits
# whatever PATH the supervisor PROCESS itself was launched with. Launching
# the supervisor with PATH pointed at an empty, executable-free scratch
# directory makes that internal call raise a real (non-mocked)
# FileNotFoundError, which `_pid_cmdline_status`'s `except Exception` catches
# as ("unknown", "") -- a genuine "ps unavailable" reproduction, not a
# monkeypatch. `sys.executable` (used to launch both the supervisor itself
# and every fake child) is always an absolute path, so none of that needs
# PATH resolution and still launches fine under the stripped PATH.
#
# Mutation: revert reap_stale_supervisor() to fall through to the reap on
# status == "unknown" (treat "unknown" the same as a confirmed match) -> red:
# the unrelated process gets killed and/or no refusal warning is printed.
# ---------------------------------------------------------------------------
def test_reap_refuses_when_identity_cannot_be_verified(tmp_path):
    uid = _uid()
    fake = _write_fake_child(tmp_path)
    pidfile = tmp_path / "sup.pid"
    status_file = tmp_path / "status.json"

    # Same real, plain, own-process-group-leader stand-in as test 10 --
    # only the FORCING MECHANISM differs (PATH-stripped supervisor env
    # instead of a genuine cmdline mismatch).
    unrelated = subprocess.Popen(["sleep", "300"], preexec_fn=os.setpgrp)
    try:
        pidfile.write_text(str(unrelated.pid))

        no_ps_dir = tmp_path / "no-ps-path"
        no_ps_dir.mkdir()
        env = dict(os.environ)
        env["PATH"] = str(no_ps_dir)

        args = _base_fake_args(tmp_path, uid, fake, status_interval="0.2")
        h = _SupervisorHarness(args, env=env)
        try:
            _wait_for_status_children(status_file, 3, timeout=5)
            warn_line = h.wait_for_stderr(
                lambda l: str(unrelated.pid) in l and "not reaping" in l.lower(),
                timeout=5,
            )
            assert warn_line, (
                "expected a loud non-reap warning naming the unverifiable "
                f"pid {unrelated.pid} on supervisor stderr (ps unavailable)"
            )
            assert unrelated.poll() is None, (
                "the unrelated process-group leader was killed (exit code "
                f"{unrelated.returncode}) despite its identity being "
                "UNVERIFIABLE (ps unavailable) -- 'could not tell' was "
                "treated as 'safe to kill'"
            )
        finally:
            h.terminate()
    finally:
        try:
            os.killpg(unrelated.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        try:
            unrelated.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pass


# ---------------------------------------------------------------------------
# 13. Round-3 coverage gap (PR #3177 reviewer-fairy finding) -- the forced-
# SIGKILL-after-deadline fallback in Supervisor.teardown() (the `if not
# graceful:` branch) was previously exercised by no test: test 11 only ever
# takes the graceful path, because its fake children die promptly on
# SIGTERM's default disposition.
#
# One fake child runs in "ignore-sigterm" mode (installs SIG_IGN for
# SIGTERM), so it cannot die on the ordinary SIGTERM the supervisor sends
# first -- teardown()'s ~2.0s grace-period wait must exhaust, and the final
# os.killpg(pgid, SIGKILL) must fire to force it down. Because the
# supervisor shares its own children's process group, that final SIGKILL
# also kills the supervisor itself -- so, unlike test 11, this test does NOT
# assert on the pidfile (os.remove(self.pidfile) right after the final
# SIGKILL is never reached on this path; that is the known, deliberate
# limitation of this last-resort branch, not a bug). What IS asserted:
# every process matching this test's unique uid marker (the stuck child, and
# by extension the whole group) is genuinely gone once the deadline has
# passed.
#
# Mutation: make the forced-kill branch a no-op (comment out / `pass` the
# os.killpg(pgid, signal.SIGKILL) call inside the `if not graceful:` block)
# -> red: the SIGTERM-ignoring child (and the supervisor) survive past the
# timeout, so _count_matching(uid) == 0 fails (or the harness's own
# terminate()/wait() times out and has to fall back to a real SIGKILL of its
# own -- either way, the intended forced-kill never fired on its own).
# ---------------------------------------------------------------------------
def test_forced_sigkill_after_grace_period_fires(tmp_path):
    uid = _uid()
    fake = _write_fake_child(tmp_path)
    status_file = tmp_path / "status.json"
    args = _base_fake_args(tmp_path, uid, fake, status_interval="0.2")
    idx = args.index("--bell-cmd-json") + 1
    args[idx] = _fake_cmd(fake, f"{uid}-bell", "ignore-sigterm")

    h = _SupervisorHarness(args)
    try:
        _wait_for_status_children(status_file, 3, timeout=5)

        h.proc.send_signal(signal.SIGTERM)
        # ~2.0s grace period in Supervisor.teardown() + margin for the final
        # SIGKILL to actually land and the kernel to reap everything.
        h.proc.wait(timeout=8)
    finally:
        h.terminate()

    time.sleep(0.3)
    assert _count_matching(uid) == 0, (
        "a child (or the supervisor itself) matching this test's uid marker "
        "survived past the grace period + forced-SIGKILL deadline -- the "
        "forced-kill fallback did not fire"
    )


# ---------------------------------------------------------------------------
# 13. Session-leader startup (PR #3177 colleague-review blocker, Ariadne) --
# the natural single-command arm shape (e.g.
# `Monitor(command: "python3 monitor-supervisor.py --purpose collab-loop")`)
# makes the supervisor its own SESSION LEADER: a bare top-level command
# exec'd this way ends up with pid == pgid by construction of how
# setsid(2)/session leadership works. POSIX documents setpgid()/setpgrp() as
# unconditionally raising EPERM when a session leader calls it on itself --
# regardless of whether the call would actually change anything -- so an
# unconditional `os.setpgrp()` crashed the supervisor at startup in exactly
# this shape, before spawning a single child (reproduced live by Ariadne;
# see this PR's review thread).
#
# Launched here via `setsid -w <python> monitor-supervisor.py ...`
# (`_SupervisorHarness`'s new `cmd_prefix` parameter): this is the same POSIX
# mechanism the natural arm path hits (a bare exec'd command becomes its own
# session leader), so this genuinely exercises the regression rather than
# merely looking plausible. `-w`/`--wait` matters: without it, `setsid` forks
# and returns immediately with exit 0 regardless of what the launched program
# does, which would make `h.proc` track the wrong process and hide the very
# crash this test exists to catch.
#
# Mutation: revert the guard to a bare, unconditional `os.setpgrp()` -> red
# (the supervisor crashes with PermissionError before spawning any children;
# see this PR's own handoff for the confirmed mutation-control result).
# ---------------------------------------------------------------------------
def test_starts_and_tears_down_cleanly_as_session_leader(tmp_path):
    if shutil.which("setsid") is None:
        pytest.skip("setsid not available on this host -- cannot reproduce "
                    "the session-leader shape this test requires")

    uid = _uid()
    fake = _write_fake_child(tmp_path)
    pidfile = tmp_path / "sup.pid"
    status_file = tmp_path / "status.json"
    args = _base_fake_args(tmp_path, uid, fake, status_interval="0.2")

    h = _SupervisorHarness(args, cmd_prefix=["setsid", "-w"])
    try:
        # Under the reverted-to-unconditional mutation, the supervisor
        # crashes with PermissionError almost immediately: it exits nonzero
        # before ever writing a status file, so this either times out or
        # raises AssertionError -- either way, red.
        _wait_for_status_children(status_file, 3, timeout=5)
        assert h.proc.poll() is None, (
            f"supervisor exited early (code={h.proc.returncode}) instead of "
            f"staying up as a session leader -- stderr: {h.snapshot()[1]}"
        )
        assert pidfile.exists()

        # Clean-teardown check, end to end in this same session-leader shape
        # (not just "didn't crash at startup"): SIGTERM -> clean exit(0) ->
        # pidfile removed -> all three children gone.
        h.proc.send_signal(signal.SIGTERM)
        h.proc.wait(timeout=5)

        assert h.proc.returncode == 0, (
            f"expected a clean exit(0) on graceful shutdown as a session "
            f"leader, got {h.proc.returncode}"
        )
        assert not pidfile.exists(), (
            "pidfile was not removed on teardown in the session-leader shape"
        )
    finally:
        h.terminate()

    time.sleep(0.3)
    assert _count_matching(uid) == 0, (
        "a fake child survived teardown in the session-leader shape"
    )
