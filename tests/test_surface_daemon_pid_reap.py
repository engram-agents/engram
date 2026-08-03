"""Tests for engram-alpha#1874 — surface-daemon process-proliferation fix.

Covers two independent layers of the same root defect (a daemon instance
could keep running, unreachable, after a fresh one silently took over its
socket path):

  (a) engram-surface-daemon.py's _recorded_pid_is_alive() helper — the new
      liveness check that gates whether main() may unlink an existing
      SOCKET_PATH.
  (b) start-engram-daemon.sh's reap-before-start loop — kills every process
      matching the daemon's script basename (not just the one PID recorded
      in PID_PATH) before launching a fresh instance.
"""

from __future__ import annotations

import importlib.util
import os
import stat
import subprocess
import sys
import time
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DAEMON_DIR = REPO_ROOT / "src" / "engram" / "hooks" / "claude"
DAEMON_PY_PATH = DAEMON_DIR / "engram-surface-daemon.py"
START_SCRIPT_PATH = DAEMON_DIR / "start-engram-daemon.sh"


def _load_daemon_module():
    """Load engram-surface-daemon as a fresh module instance (mirrors
    test_surface_daemon_idle_timeout.py's loading pattern)."""
    spec = importlib.util.spec_from_file_location(
        "engram_surface_daemon", str(DAEMON_PY_PATH)
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _spawn_sleeper():
    """A real, short-lived subprocess with a genuine, confirmable PID."""
    return subprocess.Popen([sys.executable, "-c", "import time; time.sleep(5)"])


# ---------------------------------------------------------------------------
# (a) _recorded_pid_is_alive()
# ---------------------------------------------------------------------------


class TestRecordedPidIsAlive(unittest.TestCase):
    def setUp(self):
        self.mod = _load_daemon_module()

    def test_absent_pid_path_returns_false(self, tmp_path=None):
        self.mod.PID_PATH = str(Path(self._tmp()) / "recall-daemon.pid")
        self.assertFalse(self.mod._recorded_pid_is_alive())

    def test_garbage_content_returns_false(self):
        pid_path = Path(self._tmp()) / "recall-daemon.pid"
        pid_path.write_text("not-a-pid\n")
        self.mod.PID_PATH = str(pid_path)
        self.assertFalse(self.mod._recorded_pid_is_alive())

    def test_dead_pid_returns_false(self):
        proc = _spawn_sleeper()
        proc.terminate()
        proc.wait(timeout=2)
        pid_path = Path(self._tmp()) / "recall-daemon.pid"
        pid_path.write_text(str(proc.pid))
        self.mod.PID_PATH = str(pid_path)
        self.assertFalse(
            self.mod._recorded_pid_is_alive(),
            "a confirmed-dead PID must not read as alive",
        )

    def test_live_pid_returns_true(self):
        proc = _spawn_sleeper()
        try:
            pid_path = Path(self._tmp()) / "recall-daemon.pid"
            pid_path.write_text(str(proc.pid))
            self.mod.PID_PATH = str(pid_path)
            self.assertTrue(
                self.mod._recorded_pid_is_alive(),
                "a genuinely running process's PID must read as alive "
                "-- confirms the fix narrows unlink-guarding, it doesn't "
                "just always return False",
            )
        finally:
            proc.terminate()
            proc.wait(timeout=2)

    # -- helper -----------------------------------------------------------
    def _tmp(self):
        if not hasattr(self, "_tmp_dir"):
            import tempfile
            self._tmp_dir_obj = tempfile.TemporaryDirectory()
            self._tmp_dir = self._tmp_dir_obj.name
            self.addCleanup(self._tmp_dir_obj.cleanup)
        return self._tmp_dir


# ---------------------------------------------------------------------------
# (b) start-engram-daemon.sh — syntax + reap-before-start integration
# ---------------------------------------------------------------------------


class TestStartScriptSyntax(unittest.TestCase):
    def test_bash_syntax_check(self):
        result = subprocess.run(
            ["bash", "-n", str(START_SCRIPT_PATH)], capture_output=True, text=True
        )
        self.assertEqual(
            result.returncode, 0,
            f"bash -n failed on start-engram-daemon.sh:\n{result.stderr}",
        )


class TestReapBeforeStart(unittest.TestCase):
    """Runs the real script (with {{PYTHON}} substituted and a stub daemon
    in place of the real engram-surface-daemon.py, which would otherwise
    load a real embedding model) against real decoy processes."""

    def setUp(self):
        import tempfile
        self._tmp_dir_obj = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp_dir_obj.cleanup)
        self.tmp = Path(self._tmp_dir_obj.name)

        # A stub standing in for engram-surface-daemon.py, named exactly
        # that (the reap loop matches on this basename) so the pgrep -f
        # pattern under test finds it -- but it just sleeps, no real
        # daemon behaviour, no embedding-model load.
        hooks_dir = self.tmp / "hooks"
        hooks_dir.mkdir()
        self.stub_daemon = hooks_dir / "engram-surface-daemon.py"
        self.stub_daemon.write_text(
            "import time, sys\n"
            "print('stub daemon started', file=sys.stderr)\n"
            "time.sleep(5)\n"
        )

        script_text = START_SCRIPT_PATH.read_text().replace("{{PYTHON}}", sys.executable)
        self.script = hooks_dir / "start-engram-daemon.sh"
        self.script.write_text(script_text)
        self.script.chmod(self.script.stat().st_mode | stat.S_IXUSR)

        self.engram_home = self.tmp / "engram_home"
        self.engram_home.mkdir()

    def _spawn_decoy_daemon(self, engram_home=None):
        """A real process whose argv contains the daemon script's own
        basename literally -- exactly the shape a stray prior instance
        (from any script-path variant) would have. `engram_home` (if given)
        is appended as a trailing argv, mirroring how the real script
        launches the daemon -- so a decoy can be scoped to a specific
        ENGRAM_HOME the same way the reap pattern under test expects."""
        argv = [sys.executable, str(self.stub_daemon)]
        if engram_home is not None:
            argv.append(str(engram_home))
        proc = subprocess.Popen(argv)
        time.sleep(0.3)  # give argv time to become visible to pgrep
        return proc

    def test_reaps_stray_decoy_before_starting(self):
        """A decoy process matching the daemon's basename AND this specific
        ENGRAM_HOME, with NO corresponding PID_PATH entry at all (the exact
        #1874 shape: an orphan the pidfile never knew about), must be
        killed by the script's reap loop."""
        decoy = self._spawn_decoy_daemon(engram_home=self.engram_home)
        try:
            self.assertIsNone(decoy.poll(), "decoy failed to start -- test setup broken")

            env = dict(os.environ)
            env["ENGRAM_HOME"] = str(self.engram_home)
            result = subprocess.run(
                ["bash", str(self.script)], env=env, capture_output=True,
                text=True, timeout=10,
            )
            self.assertEqual(result.returncode, 0, result.stderr)

            # The decoy must be dead -- reaped by the scoped pgrep loop,
            # not left orphaned the way #1874 found 9 of them.
            for _ in range(20):
                if decoy.poll() is not None:
                    break
                time.sleep(0.1)
            self.assertIsNotNone(
                decoy.poll(),
                "stray decoy process was not reaped before a fresh daemon started",
            )
        finally:
            if decoy.poll() is None:
                decoy.terminate()
                decoy.wait(timeout=2)

    def test_does_not_kill_a_decoy_for_a_different_engram_home(self):
        """Colleague-review finding on this PR (empirically reproduced by
        the reviewer, then confirmed here as a regression test): a decoy
        matching the daemon's basename but serving a DIFFERENT ENGRAM_HOME
        -- a second checkout, a manual debug invocation, or literally the
        SAME test class's own prior fixture -- must survive. Basename-only
        scoping killed this as collateral damage; basename+ENGRAM_HOME
        scoping must not."""
        unrelated_home = self.tmp / "unrelated_engram_home"
        unrelated_home.mkdir()
        unrelated_decoy = self._spawn_decoy_daemon(engram_home=unrelated_home)
        try:
            self.assertIsNone(
                unrelated_decoy.poll(), "decoy failed to start -- test setup broken"
            )

            env = dict(os.environ)
            env["ENGRAM_HOME"] = str(self.engram_home)  # a DIFFERENT home than the decoy's
            result = subprocess.run(
                ["bash", str(self.script)], env=env, capture_output=True,
                text=True, timeout=10,
            )
            self.assertEqual(result.returncode, 0, result.stderr)

            time.sleep(0.5)
            self.assertIsNone(
                unrelated_decoy.poll(),
                "a decoy serving a DIFFERENT ENGRAM_HOME was killed as "
                "collateral damage -- the reap must scope on ENGRAM_HOME, "
                "not just the script's basename",
            )
        finally:
            if unrelated_decoy.poll() is None:
                unrelated_decoy.terminate()
                unrelated_decoy.wait(timeout=2)

    def test_does_not_kill_a_decoy_whose_home_has_this_home_as_a_prefix(self):
        """Colleague-review fold (Kepler): ENGRAM_HOME is the trailing argv,
        so an unanchored pgrep pattern would also match a DIFFERENT, longer
        ENGRAM_HOME that merely has this run's target as a string prefix
        (e.g. `~/.engram` vs. some other checkout's `~/.engram-backup`).
        End-anchoring the pattern must prevent this."""
        longer_home = Path(str(self.engram_home) + "-backup")
        longer_home.mkdir()
        decoy = self._spawn_decoy_daemon(engram_home=longer_home)
        try:
            self.assertIsNone(decoy.poll(), "decoy failed to start -- test setup broken")

            env = dict(os.environ)
            env["ENGRAM_HOME"] = str(self.engram_home)  # a PREFIX of the decoy's home
            result = subprocess.run(
                ["bash", str(self.script)], env=env, capture_output=True,
                text=True, timeout=10,
            )
            self.assertEqual(result.returncode, 0, result.stderr)

            time.sleep(0.5)
            self.assertIsNone(
                decoy.poll(),
                "a decoy whose ENGRAM_HOME merely has this run's ENGRAM_HOME "
                "as a string PREFIX was killed -- the reap pattern must be "
                "end-anchored so a shorter home can't substring-match a "
                "longer one",
            )
        finally:
            if decoy.poll() is None:
                decoy.terminate()
                decoy.wait(timeout=2)

    def test_healthy_recorded_daemon_is_left_alone(self):
        """The pre-existing fast path — a genuinely healthy, socket-responsive
        daemon recorded in PID_PATH — must still exit 0 as a no-op, unchanged
        by the reap addition (the reap only runs on the restart branches)."""
        # A live process that will stand in for "the recorded, healthy daemon".
        decoy = _spawn_sleeper()
        try:
            pid_path = self.engram_home / "recall-daemon.pid"
            pid_path.write_text(str(decoy.pid))
            socket_path = self.engram_home / "recall-daemon.sock"

            # Make the socket check pass: bind a real listening socket at
            # SOCKET_PATH so the script's own connect-probe succeeds.
            import socket as socket_mod
            srv = socket_mod.socket(socket_mod.AF_UNIX, socket_mod.SOCK_STREAM)
            srv.bind(str(socket_path))
            srv.listen(1)
            try:
                env = dict(os.environ)
                env["ENGRAM_HOME"] = str(self.engram_home)
                result = subprocess.run(
                    ["bash", str(self.script)], env=env, capture_output=True,
                    text=True, timeout=10,
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                # Still alive -- the fast/no-op path must not have touched it.
                self.assertIsNone(
                    decoy.poll(),
                    "a healthy, socket-responsive recorded daemon must be "
                    "left running, not killed by the new reap logic",
                )
            finally:
                srv.close()
        finally:
            decoy.terminate()
            decoy.wait(timeout=2)


# ---------------------------------------------------------------------------
# (c) cleanup()'s PID-ownership guard -- colleague-review suggestion
# ---------------------------------------------------------------------------


class TestCleanupOwnershipGuard(unittest.TestCase):
    """A delayed cleanup() from a superseded instance must not delete a
    newer instance's fresh PID/socket files."""

    def setUp(self):
        import tempfile
        self._tmp_dir_obj = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp_dir_obj.cleanup)
        self.tmp = Path(self._tmp_dir_obj.name)
        self.mod = _load_daemon_module()
        self.mod.PID_PATH = str(self.tmp / "recall-daemon.pid")
        self.mod.SOCKET_PATH = str(self.tmp / "recall-daemon.sock")

    def test_does_not_unlink_when_pid_path_names_a_different_process(self):
        """PID_PATH already holds a DIFFERENT (newer) pid than ours -- the
        exact shape of a delayed cleanup() firing after a superseded
        instance's SIGTERM was slow to land. Both files must survive."""
        Path(self.mod.SOCKET_PATH).write_text("not a real socket, just a marker")
        Path(self.mod.PID_PATH).write_text(str(os.getpid() + 1))  # "someone else's" pid

        with self.assertRaises(SystemExit):
            self.mod.cleanup()

        self.assertTrue(
            Path(self.mod.SOCKET_PATH).exists(),
            "cleanup() deleted a socket owned by a DIFFERENT (newer) instance",
        )
        self.assertTrue(
            Path(self.mod.PID_PATH).exists(),
            "cleanup() deleted a pidfile owned by a DIFFERENT (newer) instance",
        )

    def test_unlinks_when_pid_path_names_this_process(self):
        """The normal case -- PID_PATH genuinely names this process -- must
        still clean up exactly as before the guard was added."""
        Path(self.mod.SOCKET_PATH).write_text("marker")
        Path(self.mod.PID_PATH).write_text(str(os.getpid()))

        with self.assertRaises(SystemExit):
            self.mod.cleanup()

        self.assertFalse(Path(self.mod.SOCKET_PATH).exists())
        self.assertFalse(Path(self.mod.PID_PATH).exists())

    def test_unlinks_when_pid_path_absent(self):
        """No pidfile at all -- nothing to protect against, proceed as
        before (matches the pre-guard unconditional behavior for this case)."""
        Path(self.mod.SOCKET_PATH).write_text("marker")

        with self.assertRaises(SystemExit):
            self.mod.cleanup()

        self.assertFalse(Path(self.mod.SOCKET_PATH).exists())


if __name__ == "__main__":
    unittest.main()
