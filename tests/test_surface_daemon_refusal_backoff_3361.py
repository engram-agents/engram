"""Contract tests for engram-alpha#3361 -- a socket-holder refusal in the
surface daemon waits a fixed backoff BEFORE exiting, so under Restart=always
(systemd) / KeepAlive true (launchd) the refusal loop logs about once a minute
and still self-heals the moment the holder goes away.

Why a daemon-side wait and not a unit/plist change: RestartPreventExitStatus
leaves the unit `failed`, and start-engram-daemon.sh treats any ActiveState
other than `active` as registered-failing and refuses to relaunch, so a
refusal would stick forever even after the holder died.

Contract (written before the implementation), in engram-surface-daemon.py:
  B0 `REFUSAL_BACKOFF_S = 60`; env ENGRAM_DAEMON_REFUSAL_BACKOFF_S (float
     seconds) overrides it. Missing / empty / non-numeric / negative /
     non-finite -> 60 silently. 0 -> no wait. Capped at 3600. Read by
     `_refusal_backoff_s()`; the wait itself is `_wait_refusal_backoff(s)`.
  B1 Both refusal branches (EXIT_SOCKET_HELD 75, EXIT_SOCKET_UNPROVEN 76):
     print the existing ONE stderr line (flushed), THEN wait, THEN exit with
     the same code.
  B2 The wait is interruptible by SIGTERM/SIGINT (prompt exit, well < 3 s).
  B3 No wait on the FREE path, the recorded-pid-alive exit-0 path, or the
     no-socket-file path.
  B4 Exit codes unchanged (75 / 76).

Cells K1-K3 are in-process (wait helper stubbed); K5-K6 run the REAL daemon
script as a subprocess in a temp ENGRAM_HOME against a real holder.
"""

from __future__ import annotations

import contextlib
import importlib.util
import io
import os
import resource
import selectors
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest
import unittest.mock
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DAEMON_PY_PATH = (
    REPO_ROOT / "src" / "engram" / "hooks" / "claude" / "engram-surface-daemon.py"
)

HAS_PROC_NET_UNIX = os.path.exists("/proc/net/unix")
HAS_LSOF = shutil.which("lsof") is not None
ENV_VAR = "ENGRAM_DAEMON_REFUSAL_BACKOFF_S"

import pytest  # noqa: E402
# #4029: in-process hook/tools loads run inside this window (sys.path / sys.modules restored).
pytestmark = pytest.mark.usefixtures("isolate_hook_loads")


def _load_daemon_module():
    spec = importlib.util.spec_from_file_location(
        "engram_surface_daemon_refusal_backoff_3361", str(DAEMON_PY_PATH)
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _dead_pid() -> int:
    p = subprocess.Popen([sys.executable, "-c", "pass"])
    p.wait()
    return p.pid


class _Base:
    def setUp(self):
        # Short path: AF_UNIX paths are limited to ~104 bytes on macOS.
        self.tmp = Path(os.path.realpath(tempfile.mkdtemp(prefix="b3361", dir="/tmp")))
        self.addCleanup(shutil.rmtree, str(self.tmp), True)
        self.mod = _load_daemon_module()
        # #4374 D3': keep the gate cell independent of the host's processes (a live
        # same-uid daemon on the seat would read as a HOLD).
        self.mod._ps_listing_text = lambda: (
            "    1     0 /sbin/launchd\n%6d %5d python\n" % (os.getpid(), os.getuid())
        )
        self.sock = str(self.tmp / "recall-daemon.sock")
        self.pid = str(self.tmp / "recall-daemon.pid")
        self.mod.SOCKET_PATH = self.sock
        self.mod.LOCK_PATH = str(self.tmp / "d.lockdir")  # #4374: never the real home's lock
        self.mod.PID_PATH = self.pid
        self.procs: list[subprocess.Popen] = []
        self.addCleanup(self._reap)
        # Every cell controls the env var itself; never inherit one.
        env = unittest.mock.patch.dict(os.environ)
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop(ENV_VAR, None)

    def _reap(self):
        for p in self.procs:
            if p.poll() is None:
                p.kill()
            p.wait()
            for s in (p.stdout, p.stderr):
                if s is not None:
                    s.close()

    def _write_dead_pidfile(self) -> bytes:
        Path(self.pid).write_text(str(_dead_pid()))
        return Path(self.pid).read_bytes()

    def _touch_socket_file(self):
        Path(self.sock).write_text("")

    def _spawn_holder(self) -> subprocess.Popen:
        src = (
            "import socket,time\n"
            "s=socket.socket(socket.AF_UNIX)\n"
            f"s.bind({self.sock!r})\n"
            "s.listen(4)\n"
            "print('ready',flush=True)\n"
            "time.sleep(120)\n"
        )
        p = subprocess.Popen(
            [sys.executable, "-c", src], stdout=subprocess.PIPE, text=True
        )
        self.procs.append(p)
        self.assertEqual(p.stdout.readline().strip(), "ready")
        return p


class _InProcess(_Base):
    def _run_gate(self, state, wait_stub):
        """Run the REAL main() up to the gate, holder state stubbed.

        Returns (code, stderr_text). code None = the gate let main() go on.
        The wait helper is replaced by `wait_stub`; `self.err` is the live
        stderr buffer so a stub can inspect it at call time.
        """

        class _PastGate(Exception):
            pass

        class _FakeClient:
            def __init__(self, *a, **k):
                raise _PastGate()

        fake = type(sys)("engram_client")
        fake.EngramClient = _FakeClient
        self.err = io.StringIO()
        code = None
        helper = unittest.mock.Mock(return_value=state)
        # #4374: a cell may drive main() more than once in this one process; a
        # real second daemon is a second process, so drop the lock the previous
        # run took (else the second run exits 0 on "another process holds the
        # daemon lock" before it reaches the gate).
        self._drop_lock()
        self.addCleanup(self._drop_lock)
        with contextlib.ExitStack() as stack:
            stack.enter_context(unittest.mock.patch.dict(sys.modules, {"engram_client": fake}))
            stack.enter_context(contextlib.redirect_stderr(self.err))
            # #4374: UNKNOWN is refused only on a host WITH /proc (a no-/proc host
            # resolves it to FREE via the lifetime lock; that arm is pinned in
            # test_surface_daemon_holder_gate_3361.py::test_h4_unknown_refuses).
            # These cells pin the refusal contract (exit 76, line, backoff), so
            # steer main()'s own arm check to the /proc arm on every host.
            _real_exists = os.path.exists
            stack.enter_context(
                unittest.mock.patch(
                    "os.path.exists",
                    lambda p: True if p == "/proc/net/unix" else _real_exists(p),
                )
            )
            stack.enter_context(
                unittest.mock.patch.object(self.mod, "_socket_holder_state", helper, create=True)
            )
            stack.enter_context(
                unittest.mock.patch.object(self.mod, "_wait_refusal_backoff", wait_stub, create=True)
            )
            try:
                self.mod.main()
            except _PastGate:
                pass
            except SystemExit as exc:
                code = exc.code
        return code, self.err.getvalue()

    def _drop_lock(self):
        fd = getattr(self.mod, "_lock_fd", None)
        if fd is not None:
            os.close(fd)
            self.mod._lock_fd = None

    def _refuse(self, state, env_value=None):
        """Drive a refusal; return (code, err, wait_calls, err_at_each_call)."""
        self._touch_socket_file()
        self._write_dead_pidfile()
        if env_value is not None:
            os.environ[ENV_VAR] = env_value
        seen = []
        calls = []

        def stub(seconds):
            calls.append(seconds)
            seen.append(self.err.getvalue())

        code, err = self._run_gate(state, stub)
        return code, err, calls, seen


HELD = ("HELD", [os.getpid() + 1])
UNKNOWN = ("UNKNOWN", [])


# ------------------------------------------------------------------ K1
class TestK1DefaultBackoff(_InProcess, unittest.TestCase):
    def _check(self, state, expect_code):
        code, err, calls, seen = self._refuse(state)
        self.assertEqual(code, expect_code, err)
        self.assertEqual(calls, [60], "the wait helper must be asked for exactly 60 s")
        # ORDER: the single stderr line was already written when the wait began.
        lines_at_call = [l for l in seen[0].splitlines() if l.strip()]
        self.assertEqual(len(lines_at_call), 1, seen[0])
        self.assertTrue(lines_at_call[0].startswith("[engram-daemon]"))
        # Still exactly one line afterwards (no second line).
        lines = [l for l in err.splitlines() if l.strip()]
        self.assertEqual(lines, lines_at_call)
        self.assertTrue(os.path.exists(self.sock), "socket file must survive")

    def test_k1_held_75_waits_60_after_the_line(self):
        self._check(HELD, 75)

    def test_k1_unproven_76_waits_60_after_the_line(self):
        self._check(UNKNOWN, 76)

    def test_k1_constant_is_60(self):
        self.assertEqual(self.mod.REFUSAL_BACKOFF_S, 60)

    def test_k1_exit_codes_unchanged(self):
        self.assertEqual(self.mod.EXIT_SOCKET_HELD, 75)
        self.assertEqual(self.mod.EXIT_SOCKET_UNPROVEN, 76)

    def test_k1_refusal_line_text_unchanged(self):
        _, err, _, _ = self._refuse(HELD)
        self.assertIn("is held by live process(es)", err)
        self.assertIn("refusing to steal it (exit 75)", err)
        _, err, _, _ = self._refuse(UNKNOWN)
        self.assertIn("could not prove socket", err)
        self.assertIn("(exit 76)", err)


# ------------------------------------------------------------------ K2
class TestK2EnvOverride(_InProcess, unittest.TestCase):
    def _asked(self, env_value, state=HELD):
        code, err, calls, _ = self._refuse(state, env_value)
        self.assertEqual(len(calls), 1, (env_value, calls))
        lines = [l for l in err.splitlines() if l.strip()]
        self.assertEqual(len(lines), 1, (env_value, err))
        self.assertIn(code, (75, 76))
        return calls[0]

    def test_k2_override_small_float(self):
        self.assertEqual(self._asked("0.05"), 0.05)
        self.assertEqual(self._asked("0.05", UNKNOWN), 0.05)

    def test_k2_zero_means_no_wait(self):
        self.assertEqual(self._asked("0"), 0)

    def test_k2_bad_values_fall_back_to_60_silently(self):
        for bad in ("", "abc", "-3", "nan", "inf", "-inf", "  "):
            with self.subTest(value=bad):
                self.assertEqual(self._asked(bad), 60)

    def test_k2_capped_at_3600(self):
        self.assertEqual(self._asked("99999"), 3600)

    def test_k2_unset_is_60(self):
        self.assertEqual(self._asked(None), 60)


# ------------------------------------------------------------------ K3
class TestK3NoWaitElsewhere(_InProcess, unittest.TestCase):
    @staticmethod
    def _boom(seconds):
        raise AssertionError(f"wait helper called ({seconds}) on a non-refusal path")

    def test_k3_free_path_continues_without_wait(self):
        self._touch_socket_file()
        self._write_dead_pidfile()
        code, err = self._run_gate(("FREE", []), self._boom)
        self.assertIsNone(code, err)
        self.assertFalse(os.path.exists(self.sock))
        self.assertEqual(err, "")

    def test_k3_recorded_pid_alive_exit_0_without_wait(self):
        self._touch_socket_file()
        Path(self.pid).write_text("1")
        with unittest.mock.patch.object(self.mod, "_recorded_pid_is_alive", return_value=True):
            code, err = self._run_gate(HELD, self._boom)
        self.assertEqual(code, 0)
        self.assertIn("a live daemon already owns", err)

    def test_k3_no_socket_file_without_wait(self):
        self._write_dead_pidfile()
        code, err = self._run_gate(HELD, self._boom)
        self.assertIsNone(code)
        self.assertEqual(err, "")


# ------------------------------------------------------- K5 / K6 (real)
@unittest.skipUnless(
    HAS_PROC_NET_UNIX or HAS_LSOF,
    "neither /proc/net/unix nor lsof on this host: no holder check possible",
)
class TestRealSubprocess(_Base, unittest.TestCase):
    def _spawn_daemon(self, backoff: str) -> subprocess.Popen:
        env = dict(os.environ)
        env["ENGRAM_HOME"] = str(self.tmp)
        env[ENV_VAR] = backoff
        p = subprocess.Popen(
            [sys.executable, str(DAEMON_PY_PATH)],
            env=env,
            cwd=str(self.tmp),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        self.procs.append(p)
        return p

    def _read_line(self, p, timeout=20.0) -> tuple[str, float]:
        """Block (selectors) until a full stderr line arrives; (line, t_seen)."""
        buf = b""
        end = time.monotonic() + timeout
        fd = p.stderr.fileno()
        # DefaultSelector (kqueue/epoll), not select.select: select() is
        # capped at fd 1023 and fails under a full-suite run (#3361).
        sel = selectors.DefaultSelector()
        try:
            sel.register(fd, selectors.EVENT_READ)
            while b"\n" not in buf:
                left = end - time.monotonic()
                if left <= 0:
                    self.fail(f"no stderr line within {timeout}s; got {buf!r}")
                r = sel.select(timeout=left)
                if r:
                    chunk = os.read(fd, 4096)
                    if not chunk:
                        break
                    buf += chunk
        finally:
            sel.close()
        return buf.decode(errors="replace"), time.monotonic()

    def _setup(self):
        holder = self._spawn_holder()
        before = self._write_dead_pidfile()
        return holder, before

    def _assert_untouched(self, holder, before):
        self.assertIsNone(holder.poll(), "holder must still be alive")
        self.assertTrue(os.path.exists(self.sock), "socket file must survive")
        self.assertEqual(Path(self.pid).read_bytes(), before)

    def test_k5_real_refusal_waits_then_exits_75(self):
        holder, before = self._setup()
        p = self._spawn_daemon("0.5")
        line, t_line = self._read_line(p)
        rc = p.wait(timeout=10)
        t_exit = time.monotonic()
        rest = p.stderr.read()
        self.assertEqual(rc, 75, (line, rest))
        self.assertEqual(rest, b"", "exactly one stderr line expected")
        self.assertIn("refusing to steal it (exit 75)", line)
        waited = t_exit - t_line
        self.assertGreaterEqual(waited, 0.4, f"no wait observed: {waited:.3f}s")
        self.assertLessEqual(waited, 5.0)
        self._assert_untouched(holder, before)

    def _term_during_long_wait(self, sig, quiet_exit_75=False):
        holder, before = self._setup()
        p = self._spawn_daemon("30")
        line, _ = self._read_line(p)
        self.assertIn("refusing to steal it", line)
        self.assertIsNone(p.poll(), "daemon must be inside its wait")
        time.sleep(0.3)
        self.assertIsNone(p.poll(), "daemon must still be waiting")
        t0 = time.monotonic()
        p.send_signal(sig)
        try:
            rc = p.wait(timeout=3.0)
        except subprocess.TimeoutExpired:
            self.fail(f"{sig.name} did not end the wait within 3 s")
        elapsed = time.monotonic() - t0
        self.__class__.measured = getattr(self.__class__, "measured", {})
        self.__class__.measured[sig.name] = (rc, elapsed)
        print(f"\n[K6 measured] {sig.name}: returncode={rc} elapsed={elapsed:.3f}s", file=sys.stderr)
        self.assertLess(elapsed, 3.0)
        if quiet_exit_75:
            # Contract: SIGINT ends the wait quietly -> held exit 75, and
            # nothing on stderr after the one refusal line (no traceback).
            rest = p.stderr.read().decode(errors="replace")
            lines = [ln for ln in (line + rest).splitlines() if ln.strip()]
            self.assertEqual(rc, 75, (line, rest))
            self.assertEqual(
                len(lines), 1,
                f"expected only the refusal line on stderr, got {lines!r}",
            )
            self.assertIn("refusing to steal it", lines[0])
        self._assert_untouched(holder, before)

    def test_k6_sigterm_during_long_wait_exits_promptly(self):
        self._term_during_long_wait(signal.SIGTERM)

    def test_k6_sigint_during_long_wait_exits_promptly(self):
        self._term_during_long_wait(signal.SIGINT, quiet_exit_75=True)


# ----------------------------------------- _read_line above the select() cap
class TestReadLineFdLimit(unittest.TestCase):
    """Needs neither /proc/net/unix nor lsof, so it is NOT behind
    TestRealSubprocess's skip: the fd-limit regression stays guarded on every
    seat. Borrows the helper under test from TestRealSubprocess."""

    _read_line = TestRealSubprocess._read_line

    def test_wait_works_with_fd_above_select_limit(self):
        """_read_line must work when the pipe fd is >= 1024 (select() limit)."""
        soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
        want = 1200
        if soft != resource.RLIM_INFINITY and soft < want:
            target = want if hard == resource.RLIM_INFINITY else min(want, hard)
            if target < want:
                self.skipTest(f"cannot raise RLIMIT_NOFILE to {want} (hard={hard})")
            try:
                resource.setrlimit(resource.RLIMIT_NOFILE, (target, hard))
            except (ValueError, OSError) as e:
                self.skipTest(f"cannot raise RLIMIT_NOFILE: {e}")
            self.addCleanup(resource.setrlimit, resource.RLIMIT_NOFILE, (soft, hard))
        r, w = os.pipe()
        fillers = [r, w]  # closed on any failure path

        def _cleanup():
            for f in fillers:
                with contextlib.suppress(OSError):
                    os.close(f)

        self.addCleanup(_cleanup)
        # First dup BEFORE the loop: if the pipe's own fd is already above
        # 1100 (process holding many fds) `high` must still not be `r`, or
        # os.close(r) below closes the fd we are about to wrap.
        high = os.dup(r)
        fillers.append(high)
        while high <= 1100:
            high = os.dup(r)
            fillers.append(high)
        os.close(r)
        fillers.remove(r)
        fillers.remove(w)
        wf = os.fdopen(w, "wb")
        self.addCleanup(lambda: wf.closed or wf.close())
        rf = os.fdopen(high, "rb", buffering=0)
        fillers.remove(high)
        self.addCleanup(rf.close)
        self.assertGreater(rf.fileno(), 1023)
        wf.write(b"hello refusal line\n")
        wf.flush()
        fake = unittest.mock.Mock(stderr=rf)
        line, t_seen = self._read_line(fake, timeout=5.0)
        self.assertEqual(line, "hello refusal line\n")
        self.assertIsInstance(t_seen, float)


if __name__ == "__main__":
    unittest.main()
