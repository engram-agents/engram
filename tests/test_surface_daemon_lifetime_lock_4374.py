"""Contract tests for engram-alpha#4374 -- on macOS the surface daemon can never
prove its socket unheld, so after an unclean exit (kill -9, OOM) the stale
socket is refused forever. Design: Borges, #4374 comment 6665434725; security
read: Zephyr, comment 6665696239.

Contract (written before the implementation):
  * D1  main()'s FIRST act is a non-blocking exclusive lock on
        `LOCK_PATH` (= <ENGRAM_HOME>/recall-daemon.lockdir), taken with
        engram_core._acquire_lock_dir, held until the process exits, and the
        lock directory is never deleted.
  * D2  lock held by another process -> a daemon of this version is alive:
        ONE stderr line saying "another process holds the daemon lock", exit 0,
        socket and pidfile untouched. Same when there is no socket file.
  * D3  with our lock held: FREE := recorded pid not alive AND the holder
        state is not HELD. The lock turns UNKNOWN into FREE (so a stale
        socket is unlinked and main() continues); it NEVER overrides HELD.
  * D4  lock unusable (a regular file at the path, a directory that is not
        0700, ...) -> refuse EXIT_SOCKET_UNPROVEN (76), nothing removed. The
        gate never runs unlocked, whatever the holder state says.
  * The lock fd is not inheritable, so a child that outlives the daemon can
        never keep the lock (flock follows the open file description, not the
        process: Zephyr, comment 6665696239 item 1).
  * D3' (Borges, comment 6665815071; Zephyr condition 1, comment 6665678356):
        FREE needs ALL FOUR: our lock held; recorded pid not alive; holder
        state not HELD; and a same-uid process listing (`ps -axo
        pid=,uid=,command=`) that (a) CONTAINS OUR OWN PID and (b) shows no
        other same-uid process whose command carries `engram-surface-daemon.py`
        (a wrapper, a harness or a grep included: a text match is a HOLD,
        exit 75). If our own pid is absent, `ps` fails, times out, or the
        output is empty or unparseable, the answer is UNKNOWN and the gate
        refuses (76): an index read whose failure is distinguishable from
        emptiness. The listing runs on BOTH arms (the unlink decision, lock
        held), not only on the no-/proc arm. Other uids are ignored.
  * Launcher cells: tests/test_socket_ownership_guard.py -k c5.

The macOS arm is simulated on any host: `/proc/net/unix` is reported absent
and `lsof` is faked. T4 and T4b use REAL processes and a real SIGKILL: the
kernel release is the property, not a simulation of it.

Mutants this file must catch (each must turn the named cell red while the
older gate files stay green):
  m1  UNKNOWN treated as FREE without the lock             -> T2
  m2  FREE allowed to override HELD                        -> T3
  m3  lock released after the gate instead of held for life -> T2b
  m4  lock fd marked inheritable                           -> T4b
  m5  an empty / own-pid-less listing accepted as clean    -> T9
"""

from __future__ import annotations

import contextlib
import importlib.util
import io
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
import unittest.mock
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
DAEMON_PY_PATH = (
    REPO_ROOT / "src" / "engram" / "hooks" / "claude" / "engram-surface-daemon.py"
)

# #4029: in-process hook/tools loads run inside this window (sys.path / sys.modules restored).
pytestmark = pytest.mark.usefixtures("isolate_hook_loads")

_REAL_EXISTS = os.path.exists


def _load_daemon_module(name="engram_surface_daemon_lock_4374"):
    spec = importlib.util.spec_from_file_location(name, str(DAEMON_PY_PATH))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _dead_pid() -> int:
    p = subprocess.Popen([sys.executable, "-c", "pass"])
    p.wait()
    return p.pid


# A child that takes the SAME kind of lock the daemon will: a 0700 directory,
# flock(LOCK_EX | LOCK_NB) on its fd, held until killed.
_HOLDER_SRC = (
    "import fcntl,os,sys,time\n"
    "p=sys.argv[1]\n"
    "os.makedirs(p,mode=0o700,exist_ok=True)\n"
    "os.chmod(p,0o700)\n"
    "fd=os.open(p,os.O_RDONLY|os.O_DIRECTORY)\n"
    "fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)\n"
    "print('ready',flush=True)\n"
    "time.sleep(120)\n"
)


class _Base(unittest.TestCase):
    def setUp(self):
        # Short path: AF_UNIX paths are limited to ~104 bytes on macOS.
        self.tmp = Path(os.path.realpath(tempfile.mkdtemp(prefix="l4374", dir="/tmp")))
        self.addCleanup(shutil.rmtree, str(self.tmp), True)
        self.mod = _load_daemon_module()
        self._point(self.mod)
        self.procs: list[subprocess.Popen] = []
        self.addCleanup(self._reap)
        env = unittest.mock.patch.dict(
            os.environ, {"ENGRAM_DAEMON_REFUSAL_BACKOFF_S": "0"}
        )
        env.start()
        self.addCleanup(env.stop)

    def _point(self, mod):
        mod.SOCKET_PATH = str(self.tmp / "d.sock")
        mod.PID_PATH = str(self.tmp / "d.pid")
        mod.LOCK_PATH = str(self.tmp / "d.lockdir")

    @property
    def sock(self):
        return str(self.tmp / "d.sock")

    @property
    def pid(self):
        return str(self.tmp / "d.pid")

    @property
    def lock(self):
        return str(self.tmp / "d.lockdir")

    def _reap(self):
        for p in self.procs:
            if p.poll() is None:
                p.kill()
            p.wait()
            if p.stdout:
                p.stdout.close()

    def _stale_state(self) -> bytes:
        """A socket file nobody holds and a pidfile naming a dead pid."""
        Path(self.sock).write_text("")
        Path(self.pid).write_text(str(_dead_pid()))
        return Path(self.pid).read_bytes()

    def _hold_lock_in_child(self) -> subprocess.Popen:
        p = subprocess.Popen(
            [sys.executable, "-c", _HOLDER_SRC, self.lock],
            stdout=subprocess.PIPE,
            text=True,
        )
        self.procs.append(p)
        self.assertEqual(p.stdout.readline().strip(), "ready")
        return p

    def _lock_is_free(self) -> bool:
        """Can a fresh process take the lock right now?"""
        src = (
            "import fcntl,os,sys\n"
            "fd=os.open(sys.argv[1],os.O_RDONLY|os.O_DIRECTORY)\n"
            "try:\n"
            "    fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)\n"
            "except BlockingIOError:\n"
            "    sys.exit(3)\n"
        )
        r = subprocess.run([sys.executable, "-c", src, self.lock])
        return r.returncode == 0

    @contextlib.contextmanager
    def _env(self, darwin=True, lsof_rc=1, lsof_out="", ps=None, real_ps=False, force_proc=False):
        """The gate's inputs. darwin: no /proc/net/unix, lsof answers
        `lsof_rc`. ps: None = a CLEAN listing (this process, no daemon: the
        host's own live daemon must not leak into a cell); a string = that
        stdout (rc 0); an int = that return code with empty stdout; an
        Exception = raised. real_ps=True runs the real `ps`."""

        def fake_exists(p):
            if darwin and str(p) == "/proc/net/unix":
                return False
            if force_proc and str(p) == "/proc/net/unix":
                return True  # a /proc host, whatever this host is
            return _REAL_EXISTS(p)

        cp = subprocess.CompletedProcess(["lsof"], lsof_rc, stdout=lsof_out, stderr="")
        real_run = subprocess.run

        def fake_run(argv, *a, **k):
            if isinstance(argv, (list, tuple)) and argv:
                if darwin and argv[0] == "lsof":
                    return cp
                if argv[0] == "ps" and not real_ps:
                    out = self._listing() if ps is None else ps
                    if isinstance(out, Exception):
                        raise out
                    if isinstance(out, tuple):  # (returncode, stdout): a ps that died part-way
                        return subprocess.CompletedProcess(argv, out[0], stdout=out[1], stderr="")
                    if isinstance(out, int):
                        return subprocess.CompletedProcess(argv, out, stdout="", stderr="")
                    return subprocess.CompletedProcess(argv, 0, stdout=out, stderr="")
            return real_run(argv, *a, **k)

        with unittest.mock.patch("os.path.exists", side_effect=fake_exists):
            with unittest.mock.patch("subprocess.run", side_effect=fake_run):
                yield

    def _listing(self, *extra, own=True):
        """A well-formed `ps -axo pid=,uid=,command=` stdout: init, optionally
        THIS process (the one running main()), then `extra` (pid, uid, cmd)."""
        rows = [(1, 0, "/sbin/launchd")]
        if own:
            rows.append((os.getpid(), os.getuid(), "python -m pytest"))
        rows.extend(extra)
        return "".join(f"{pid:>6} {uid:>5} {cmd}\n" for pid, uid, cmd in rows)

    def _run_gate(self, mod=None, darwin=True, lsof_rc=1, lsof_out="", ps=None, real_ps=False, force_proc=False):
        """Run the REAL main() up to the gate and no further.

        Returns (code, stderr_text): the SystemExit code if the gate exited,
        None if it let main() continue (the stub EngramClient, the first thing
        main() touches after the gate, raises _PastGate)."""
        mod = mod or self.mod

        class _PastGate(Exception):
            pass

        class _FakeClient:
            def __init__(self, *a, **k):
                raise _PastGate()

        fake = type(sys)("engram_client")
        fake.EngramClient = _FakeClient
        err = io.StringIO()
        code = None
        with self._env(darwin, lsof_rc, lsof_out, ps, real_ps, force_proc), unittest.mock.patch.dict(
            sys.modules, {"engram_client": fake}
        ):
            with contextlib.redirect_stderr(err):
                try:
                    mod.main()
                except _PastGate:
                    pass
                except SystemExit as exc:
                    code = exc.code
        return code, err.getvalue()


# ----------------------------------------------------------------- T0
class TestT0FreshInstall(_Base):
    def test_no_socket_creates_a_0700_lock_dir_and_goes_on(self):
        code, err = self._run_gate()
        self.assertIsNone(code, err)
        st = os.stat(self.lock)
        self.assertTrue(os.path.isdir(self.lock))
        self.assertEqual(st.st_mode & 0o777, 0o700)
        # D1: held for the life of the process, not just for the gate.
        self.assertFalse(self._lock_is_free())


# ----------------------------------------------------------------- T1
class TestT1StaleSocketOnDarwin(_Base):
    def test_stale_socket_dead_pid_lsof_rc1_lock_free_is_free(self):
        """Closing condition 1: red on the code before this fix (UNKNOWN ->
        refuse 76); green once the lifetime lock turns UNKNOWN into FREE."""
        self._stale_state()
        code, err = self._run_gate()
        self.assertIsNone(code, err)
        self.assertFalse(os.path.exists(self.sock), "stale socket must be unlinked")
        self.assertFalse(self._lock_is_free(), "the lock stays held for life (D1)")

    def test_the_lock_dir_is_never_removed(self):
        self._stale_state()
        self._run_gate()
        self.assertTrue(os.path.isdir(self.lock))


# ----------------------------------------------------------------- T2
class TestT2LockHeldElsewhere(_Base):
    # T2 pins TODAY's exit path for a held lock: exit 0. Under a KeepAlive(PathState) unit that exit
    # is the respawn loop described in #4373, and #4373 is the PR that changes this path. If it lands
    # first this assertion moves with it; the two PRs must not pin opposite behaviours (Kepler's rider
    # 5 on #4374).
    def test_lock_held_by_live_child_refuses_and_touches_nothing(self):
        before = self._stale_state()
        self._hold_lock_in_child()
        code, err = self._run_gate()
        self.assertEqual(code, 0, err)
        self.assertTrue(os.path.exists(self.sock), "socket must be untouched")
        self.assertEqual(Path(self.pid).read_bytes(), before)
        self.assertIn("another process holds the daemon lock", err)
        self.assertNotIn("a live daemon already owns", err)

    def test_lock_held_elsewhere_and_no_socket_file_also_exits_0(self):
        self._hold_lock_in_child()
        code, err = self._run_gate()
        self.assertEqual(code, 0, err)
        self.assertIn("another process holds the daemon lock", err)

    def test_t2b_a_running_daemon_keeps_the_lock_after_the_gate(self):
        """m3 detector: a first daemon passes the gate and keeps running; a
        second daemon of this version, started against the same home, must see
        the lock held and exit 0 -- the lock is held for life, not just for
        the gate."""
        code, err = self._run_gate()
        self.assertIsNone(code, err)
        Path(self.sock).write_text("")  # the first daemon's socket
        second = _load_daemon_module("engram_surface_daemon_lock_4374_second")
        self._point(second)
        code2, err2 = self._run_gate(mod=second)
        self.assertEqual(code2, 0, err2)
        self.assertTrue(os.path.exists(self.sock))


# ----------------------------------------------------------------- T3
class TestT3LockNeverOverridesHeld(_Base):
    def test_lock_free_but_lsof_names_a_live_holder_refuses_75(self):
        before = self._stale_state()
        code, err = self._run_gate(lsof_rc=0, lsof_out="12345\n")
        self.assertEqual(code, self.mod.EXIT_SOCKET_HELD, err)
        self.assertTrue(os.path.exists(self.sock))
        self.assertEqual(Path(self.pid).read_bytes(), before)

    def test_lock_free_and_helper_says_held_refuses_75(self):
        before = self._stale_state()
        with unittest.mock.patch.object(
            self.mod, "_socket_holder_state", return_value=("HELD", [999999])
        ):
            code, err = self._run_gate(darwin=False)
        self.assertEqual(code, self.mod.EXIT_SOCKET_HELD, err)
        self.assertTrue(os.path.exists(self.sock))
        self.assertEqual(Path(self.pid).read_bytes(), before)


# ----------------------------------------------------------------- T4
class TestT4RealKill(_Base):
    def test_kill_9_of_a_lock_holding_child_frees_the_gate(self):
        """The kernel drops the lock on SIGKILL: no simulation."""
        child = self._hold_lock_in_child()
        self._stale_state()
        self.assertFalse(self._lock_is_free())
        child.kill()
        child.wait()
        code, err = self._run_gate()
        self.assertIsNone(code, err)
        self.assertFalse(os.path.exists(self.sock))

    def test_t4b_a_child_that_outlives_the_daemon_cannot_keep_the_lock(self):
        """m4 detector (Zephyr, 6665696239 item 2): flock follows the open file
        description, so an inheritable lock fd that an exec'd child carries
        away would read as 'a live daemon' with nobody serving. The daemon runs
        its gate in a real process, then forks a long-lived exec'd child and
        dies; the next gate must still get the lock."""
        driver = (
            "import contextlib,importlib.util,io,os,sys,unittest.mock\n"
            "spec=importlib.util.spec_from_file_location('d',sys.argv[1])\n"
            "m=importlib.util.module_from_spec(spec); spec.loader.exec_module(m)\n"
            "m.SOCKET_PATH=sys.argv[2]; m.PID_PATH=sys.argv[3]; m.LOCK_PATH=sys.argv[4]\n"
            "import subprocess\n"
            "_r=subprocess.run\n"
            "def _run(a,*k,**kw):\n"
            "    if a and a[0]=='ps':\n"
            "        return subprocess.CompletedProcess(a,0,stdout='    1     0 /sbin/launchd\\n%6d %5d python\\n'%(os.getpid(),os.getuid()),stderr='')\n"
            "    return _r(a,*k,**kw)\n"
            "subprocess.run=_run\n"
            "class G(Exception): pass\n"
            "class C:\n"
            "    def __init__(s,*a,**k): raise G()\n"
            "f=type(sys)('engram_client'); f.EngramClient=C\n"
            "with unittest.mock.patch.dict(sys.modules,{'engram_client':f}):\n"
            "    try: m.main()\n"
            "    except G: pass\n"
            "pid=os.fork()\n"
            "if pid==0:\n"
            "    dn=os.open(os.devnull,os.O_RDWR)\n"
            "    [os.dup2(dn,i) for i in (0,1,2)]\n"
            "    os.execvp('sleep',['sleep','120'])\n"
            "print(pid,flush=True)\n"
            "os._exit(0)\n"
        )
        env = dict(os.environ, ENGRAM_DAEMON_REFUSAL_BACKOFF_S="0")
        r = subprocess.run(
            [sys.executable, "-c", driver, str(DAEMON_PY_PATH), self.sock, self.pid, self.lock],
            capture_output=True,
            text=True,
            env=env,
            timeout=60,
        )
        self.assertEqual(r.returncode, 0, r.stderr)
        sleeper = int(r.stdout.strip().splitlines()[-1])
        self.addCleanup(self._kill_quietly, sleeper)
        # The daemon is gone, its exec'd child is alive.
        os.kill(sleeper, 0)
        self.assertTrue(
            self._lock_is_free(),
            "an inheritable lock fd let a child that outlived the daemon keep the lock",
        )

    @staticmethod
    def _kill_quietly(pid):
        try:
            os.kill(pid, 9)
        except OSError:
            pass


# ----------------------------------------------------------------- T5
class TestT5LockUnusable(_Base):
    def _refused(self):
        before = self._stale_state()
        code, err = self._run_gate()
        self.assertEqual(code, self.mod.EXIT_SOCKET_UNPROVEN, err)
        self.assertTrue(os.path.exists(self.sock), "nothing may be removed")
        self.assertEqual(Path(self.pid).read_bytes(), before)

    def test_regular_file_at_the_lock_path(self):
        Path(self.lock).write_text("not a directory")
        self._refused()

    def test_lock_dir_with_the_wrong_mode(self):
        os.mkdir(self.lock, 0o755)
        os.chmod(self.lock, 0o755)
        self._refused()

    def test_unusable_lock_refuses_even_when_the_holder_state_reads_free(self):
        """The gate never runs unlocked (D4)."""
        Path(self.lock).write_text("not a directory")
        before = self._stale_state()
        with unittest.mock.patch.object(
            self.mod, "_socket_holder_state", return_value=("FREE", [])
        ):
            code, err = self._run_gate(darwin=False)
        self.assertEqual(code, self.mod.EXIT_SOCKET_UNPROVEN, err)
        self.assertTrue(os.path.exists(self.sock))
        self.assertEqual(Path(self.pid).read_bytes(), before)


# ----------------------------------------------------------------- T9-T12  (D3')
class TestT9ListingMustContainOurOwnPid(_Base):
    """An index read whose failure is distinguishable from emptiness. m5
    (accept an empty / own-pid-less listing as clean) turns these red."""

    def _refused_76(self, ps, darwin=True):
        before = self._stale_state()
        code, err = self._run_gate(darwin=darwin, ps=ps)
        self.assertEqual(code, self.mod.EXIT_SOCKET_UNPROVEN, err)
        self.assertTrue(os.path.exists(self.sock), "socket must be untouched")
        self.assertEqual(Path(self.pid).read_bytes(), before)
        return err

    def test_listing_without_our_own_pid_refuses_76(self):
        self._refused_76(self._listing(own=False))

    def test_empty_listing_refuses_76(self):
        self._refused_76("")

    def test_ps_nonzero_exit_refuses_76(self):
        self._refused_76(2)

    def test_ps_nonzero_exit_with_a_plausible_listing_still_refuses_76(self):
        """Clio's survivor: dropping the nonzero-exit check left every cell green,
        because the nonzero cell above prints nothing and the empty listing refuses
        anyway. A ps that dies part-way and prints a prefix of the table that still
        contains our pid must not be taken as a full listing: a missing peer row
        would read as 'no peer', the dangerous direction."""
        self._refused_76((1, self._listing()))

    def test_ps_missing_refuses_76(self):
        self._refused_76(FileNotFoundError("ps"))

    def test_ps_timeout_refuses_76(self):
        self._refused_76(subprocess.TimeoutExpired("ps", 5))

    def test_unparseable_listing_refuses_76(self):
        self._refused_76("this is not a process table\n" + self._listing())

    def test_the_same_refusal_on_a_host_with_proc(self):
        """Both arms: the helper says FREE (a stub), the listing lacks our pid."""
        before = self._stale_state()
        with unittest.mock.patch.object(
            self.mod, "_socket_holder_state", return_value=("FREE", [])
        ):
            code, err = self._run_gate(darwin=False, ps=self._listing(own=False), force_proc=True)
        self.assertEqual(code, self.mod.EXIT_SOCKET_UNPROVEN, err)
        self.assertTrue(os.path.exists(self.sock))
        self.assertEqual(Path(self.pid).read_bytes(), before)

    def test_control_a_good_listing_with_no_other_daemon_goes_through(self):
        self._stale_state()
        code, err = self._run_gate(ps=self._listing())
        self.assertIsNone(code, err)
        self.assertFalse(os.path.exists(self.sock))


class TestT10TextMatchIsAHold(_Base):
    SCRIPT = "engram-surface-daemon.py"

    def test_a_real_same_uid_process_carrying_the_script_name_holds(self):
        """Lock free, pid dead, lsof rc 1, a REAL process of our uid whose
        argv carries the script name (a wrapper, a harness or a grep would do
        the same): refuse 75, socket untouched. Real `ps`, no fake."""
        before = self._stale_state()
        p = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(60)", self.SCRIPT]
        )
        self.procs.append(p)
        time.sleep(0.2)
        code, err = self._run_gate(real_ps=True)
        self.assertEqual(code, self.mod.EXIT_SOCKET_HELD, err)
        self.assertIn(str(p.pid), err)
        self.assertTrue(os.path.exists(self.sock))
        self.assertEqual(Path(self.pid).read_bytes(), before)

    def test_a_listed_wrapper_line_holds_even_when_it_is_not_python(self):
        before = self._stale_state()
        ps = self._listing((424242, os.getuid(), f"grep -r {self.SCRIPT} /tmp"))
        code, err = self._run_gate(ps=ps)
        self.assertEqual(code, self.mod.EXIT_SOCKET_HELD, err)
        self.assertTrue(os.path.exists(self.sock))
        self.assertEqual(Path(self.pid).read_bytes(), before)

    def test_another_uid_carrying_the_script_name_is_ignored(self):
        self._stale_state()
        ps = self._listing((424242, os.getuid() + 1, f"python {self.SCRIPT}"))
        code, err = self._run_gate(ps=ps)
        self.assertIsNone(code, err)
        self.assertFalse(os.path.exists(self.sock))

    def test_our_own_line_carrying_the_script_name_is_not_a_hold(self):
        """The running daemon's own command carries the script name."""
        self._stale_state()
        ps = self._listing()
        ps = ps.replace("python -m pytest", f"python {self.SCRIPT} /home")
        code, err = self._run_gate(ps=ps)
        self.assertIsNone(code, err)

    def test_the_hold_applies_on_a_host_with_proc_too(self):
        before = self._stale_state()
        ps = self._listing((424242, os.getuid(), f"python {self.SCRIPT}"))
        with unittest.mock.patch.object(
            self.mod, "_socket_holder_state", return_value=("FREE", [])
        ):
            code, err = self._run_gate(darwin=False, ps=ps, force_proc=True)
        self.assertEqual(code, self.mod.EXIT_SOCKET_HELD, err)
        self.assertTrue(os.path.exists(self.sock))
        self.assertEqual(Path(self.pid).read_bytes(), before)

    def test_no_socket_file_means_no_listing_is_needed(self):
        """The listing guards the unlink; with nothing to unlink it is not read."""
        code, err = self._run_gate(ps=self._listing(own=False))
        self.assertIsNone(code, err)


class TestT11DocstringOrdering(unittest.TestCase):
    """Zephyr condition 2: the contract says what FREE means and in what order
    the lock, the veto and the refusal act. Cheap to pin, easy to lose."""

    def test_the_holder_state_contract_names_participating_holders_and_the_order(self):
        mod = _load_daemon_module("engram_surface_daemon_lock_4374_doc")
        doc = mod._socket_holder_state.__doc__
        self.assertIn("no participating holder", doc)
        self.assertIn("lock authorises", doc)
        self.assertIn("veto", doc)
        self.assertIn("unusable lock refuses", doc)


# ----------------------------------------------------------------- T12  (Clio, round-1 survivors)
class TestT12LockIsNonBlockingAndNamed(_Base):
    def test_a_held_lock_is_the_answer_not_a_wait(self):
        """Clio's survivor (a): acquire_daemon_lock with wait_s=5 passes every
        other cell, because a held lock would just wait 5 s and then refuse.
        D1' (maint policy): ONE attempt plus the inode recheck. A held lock must
        make main() exit 0 at once."""
        self._hold_lock_in_child()
        t0 = time.monotonic()
        code, err = self._run_gate()
        elapsed = time.monotonic() - t0
        self.assertEqual(code, 0, err)
        self.assertLess(elapsed, 2.0, f"a held lock must not be waited on (took {elapsed:.2f}s)")

    def test_the_default_lock_path_is_recall_daemon_lockdir_under_the_home(self):
        """Clio's survivor (b): nothing pinned the name. Other tooling, the
        README and Zephyr's read all refer to <ENGRAM_HOME>/recall-daemon.lockdir."""
        mod = _load_daemon_module("engram_surface_daemon_lock_4374_default")
        self.assertEqual(os.path.basename(mod.LOCK_PATH), "recall-daemon.lockdir")
        self.assertEqual(os.path.dirname(mod.LOCK_PATH), mod.ENGRAM_HOME)
        self.assertEqual(os.path.dirname(mod.SOCKET_PATH), os.path.dirname(mod.LOCK_PATH))


class TestT13OneBadArgvMustNotBlindTheListing(_Base):
    def test_a_process_with_invalid_utf8_in_its_argv_does_not_make_the_listing_undecodable(self):
        """Reviewer-fairy round 2: one process anywhere on the host whose argv
        is not valid UTF-8 made text=True decoding raise, so every gate refused
        76 while it lived. The enumeration must still parse, and still hold OUR
        own pid and the odd process's row."""
        p = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(30)", b"x\xff"]
        )
        self.procs.append(p)
        time.sleep(0.3)
        rows = self.mod._ps_enumeration()
        pids = {r[0] for r in rows}
        self.assertIn(os.getpid(), pids)
        self.assertIn(p.pid, pids)


# ----------------------------------------------------------------- T14  (Kepler's riders 2, 3 and Borges's cheap cell)
VERB = "clear-stale-engram-daemon.sh"
VERB_PATH = REPO_ROOT / "src" / "engram" / "hooks" / "claude" / VERB


class TestT14TheRemedyIsNamed(_Base):
    """Rider 2: every exit-76 line says what a human does next. Nothing in the
    installed hooks, tools or skills named the verb before this; builder and
    design seat both failed to recall it."""

    def _assert_names_the_verb(self, err):
        self.assertIn(VERB, err)
        self.assertIn("--yes", err)
        self.assertIn("human", err.lower())

    def test_unusable_lock_line_names_the_verb(self):
        Path(self.lock).write_text("not a directory")
        self._stale_state()
        code, err = self._run_gate()
        self.assertEqual(code, self.mod.EXIT_SOCKET_UNPROVEN, err)
        self._assert_names_the_verb(err)

    def test_listing_not_established_line_names_the_verb(self):
        self._stale_state()
        code, err = self._run_gate(ps="")
        self.assertEqual(code, self.mod.EXIT_SOCKET_UNPROVEN, err)
        self._assert_names_the_verb(err)

    def test_unknown_on_a_proc_host_line_names_the_verb(self):
        self._stale_state()
        with unittest.mock.patch.object(
            self.mod, "_socket_holder_state", return_value=("UNKNOWN", [])
        ):
            code, err = self._run_gate(darwin=False, force_proc=True)
        self.assertEqual(code, self.mod.EXIT_SOCKET_UNPROVEN, err)
        self._assert_names_the_verb(err)
        # tests/test_surface_daemon_holder_gate_3361.py::test_h4_unknown_refuses pins "by hand" in this
        # line, and on a /proc host (Linux CI) it is reached; on a Mac that cell takes the no-/proc arm and
        # never sees the line. This forced-/proc cell is the one that fails on a Mac as well.
        self.assertIn("by hand", err)


class TestT14VerbHeaderIsTrue(unittest.TestCase):
    """Rider 3: after the lifetime lock a crash-stale macOS socket IS told
    apart from a live holder whenever the lock is usable. The verb's header
    still said the daemon keeps refusing; a true statement turned false by the
    change beside it."""

    def setUp(self):
        self.header = "".join(VERB_PATH.read_text().splitlines(keepends=True)[:30])

    def test_the_false_sentence_is_gone(self):
        self.assertNotIn("and the daemon keeps refusing", self.header)

    def test_the_header_says_when_the_verb_is_still_needed(self):
        h = self.header.lower()
        self.assertIn("lifetime lock", h)
        self.assertIn("unusable", h)
        self.assertIn("does not take the lock", h)

    def test_the_verb_is_still_human_only(self):
        flat = " ".join(line.lstrip("# ") for line in self.header.splitlines()).lower()
        self.assertIn("never by a hook", flat)


class TestT14NoForkWithoutExec(unittest.TestCase):
    """Borges's optional cell: turns Zephyr's read (the daemon forks nothing)
    into a pinned property. flock follows the open file description, so a later
    fork without exec would bring back the silent 'another process holds the
    lock' exit with nobody serving."""

    def test_the_daemon_source_has_no_fork_spawn_or_multiprocessing(self):
        import ast

        tree = ast.parse(DAEMON_PY_PATH.read_text())
        bad = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                bad += [a.name for a in node.names if a.name.split(".")[0] == "multiprocessing"]
            elif isinstance(node, ast.ImportFrom):
                if (node.module or "").split(".")[0] == "multiprocessing":
                    bad.append(node.module)
            elif isinstance(node, ast.Attribute):
                if node.attr in {"fork", "forkpty", "Popen", "posix_spawn", "posix_spawnp"} or node.attr.startswith("spawn"):
                    bad.append(f"{node.attr}@{node.lineno}")
        self.assertEqual(bad, [])


class TestT15PsWidthNeverNarrowsTheListing(_Base):
    """Borges, round 3 on 73ad37b8 (6670763291): on procps under one pytest, `ps -axo` inside
    the cell cut every row to 80 characters, the listing held 0 daemon rows, the gate printed
    nothing and UNLINKED the socket: FREE, the dangerous direction. The design text omitted
    `-ww`. Two pins that hold on any host."""

    def test_the_ps_argv_asks_for_unlimited_width(self):
        seen = []
        real_run = subprocess.run

        def spy(argv, *a, **k):
            seen.append(list(argv))
            return real_run(argv, *a, **k)

        with unittest.mock.patch("subprocess.run", side_effect=spy):
            self.mod._ps_listing_text()
        ps_calls = [a for a in seen if a and a[0] == "ps"]
        self.assertTrue(ps_calls, "the seam must run ps")
        self.assertIn("-ww", ps_calls[0])

    def test_a_narrow_environment_does_not_hide_a_daemon_row(self):
        """COLUMNS=20 plus a REAL same-uid process whose argv carries the script name
        deep in a long command line: the gate must still hold (75) and name its pid."""
        before = self._stale_state()
        p = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(60)", "x" * 200, "engram-surface-daemon.py"]
        )
        self.procs.append(p)
        time.sleep(0.2)
        with unittest.mock.patch.dict(os.environ, {"COLUMNS": "20"}):
            code, err = self._run_gate(real_ps=True)
        self.assertEqual(code, self.mod.EXIT_SOCKET_HELD, err)
        self.assertIn(str(p.pid), err)
        self.assertTrue(os.path.exists(self.sock))
        self.assertEqual(Path(self.pid).read_bytes(), before)

    def test_a_ps_that_truncates_without_ww_cannot_hide_a_daemon_row(self):
        """Kepler's condition on 6670854979: a cell that is RED without -ww on ANY host. The
        BSD ps on a Mac ignores COLUMNS when piped, so COLUMNS alone cannot discriminate there.
        Here a `ps` shim earlier on PATH cuts every row to 80 characters unless -ww is among its
        arguments (what procps did under pytest 9.1.1, measured by Borges), then defers to the
        real ps. A REAL same-uid process whose argv carries the script name beyond column 80 must
        still hold (75) and be named."""
        import shutil as _sh

        real_ps = _sh.which("ps")
        shim_dir = self.tmp / "shim"
        shim_dir.mkdir()
        shim = shim_dir / "ps"
        shim.write_text(
            "#!/bin/sh\n"
            'for a in "$@"; do [ "$a" = "-ww" ] && exec ' + real_ps + ' "$@"; done\n'
            + real_ps + ' "$@" | cut -c1-80\n'
        )
        shim.chmod(0o755)
        before = self._stale_state()
        p = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(60)", "x" * 200, "engram-surface-daemon.py"]
        )
        self.procs.append(p)
        time.sleep(0.2)
        path = str(shim_dir) + os.pathsep + os.environ.get("PATH", "")
        with unittest.mock.patch.dict(os.environ, {"PATH": path}):
            code, err = self._run_gate(real_ps=True)
        self.assertEqual(code, self.mod.EXIT_SOCKET_HELD, err)
        self.assertIn(str(p.pid), err)
        self.assertTrue(os.path.exists(self.sock))
        self.assertEqual(Path(self.pid).read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
