"""Contract tests for engram-alpha#3361 -- the surface daemon's startup
socket gate must not unlink a socket path a DIFFERENT live process holds.

The defect: main() did `if exists(SOCKET_PATH): if _recorded_pid_is_alive():
exit 0; os.unlink(SOCKET_PATH)`. With a dead/missing/wrong pidfile and a live
unmatched holder, that unlink stole the path. The shell guard in
start-engram-daemon.sh (#3359) cannot protect a holder from the systemd or
direct-launch paths, so the daemon-side gate must refuse itself.

Contract (written before the implementation):
  * `_socket_holder_state(socket_path, proc_net_unix=..., proc_root=...)`
    -> (state, pids), state in HELD / FREE / UNKNOWN. Positive identification
    only; fail CLOSED. connect() is never proof of absence.
  * The gate is the block at the top of main() (exercised through the real
    main(), with a stub EngramClient that marks "went past the gate").
    FREE -> unlink and continue; HELD (other pid, or pids unknown) or
    UNKNOWN -> no unlink, no pidfile touch, ONE stderr line, non-zero exit.
    Recorded pid alive -> exit 0, helper not called. No socket file ->
    helper not called.

Cells H1-H9 map to the issue's contract text. H5/H6/no-socket are controls
that are green on the pre-fix code by design.
"""

from __future__ import annotations

import contextlib
import importlib.util
import io
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import unittest
import unittest.mock
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DAEMON_PY_PATH = (
    REPO_ROOT / "src" / "engram" / "hooks" / "claude" / "engram-surface-daemon.py"
)

HAS_PROC_NET_UNIX = os.path.exists("/proc/net/unix")
HAS_LSOF = shutil.which("lsof") is not None

import pytest  # noqa: E402
# #4029: in-process hook/tools loads run inside this window (sys.path / sys.modules restored).
pytestmark = pytest.mark.usefixtures("isolate_hook_loads")


def _load_daemon_module():
    spec = importlib.util.spec_from_file_location(
        "engram_surface_daemon_holder_gate_3361", str(DAEMON_PY_PATH)
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _dead_pid() -> int:
    p = subprocess.Popen([sys.executable, "-c", "pass"])
    p.wait()
    return p.pid


_HEADER = "Num       RefCount Protocol Flags    Type St Inode Path\n"


def _row(path: str | None, inode: str = "424242") -> str:
    base = f"0000000000000000: 00000002 00000000 00010000 0001 01 {inode}"
    return base + (f" {path}\n" if path is not None else "\n")


class _TmpMixin:
    def setUp(self):
        # Short path: AF_UNIX paths are limited to ~104 bytes on macOS.
        self.tmp = Path(os.path.realpath(tempfile.mkdtemp(prefix="g3361", dir="/tmp")))
        self.addCleanup(shutil.rmtree, str(self.tmp), True)
        self.mod = _load_daemon_module()
        # #4374 D3': keep the gate cell independent of the host's processes (a live
        # same-uid daemon on the seat would read as a HOLD).
        self.mod._ps_listing_text = lambda: (
            "    1     0 /sbin/launchd\n%6d %5d python\n" % (os.getpid(), os.getuid())
        )
        self.sock = str(self.tmp / "d.sock")
        self.pid = str(self.tmp / "d.pid")
        self.mod.SOCKET_PATH = self.sock
        self.mod.PID_PATH = self.pid
        self.mod.LOCK_PATH = str(self.tmp / "d.lockdir")  # #4374: never the real home's lock
        self.procs: list[subprocess.Popen] = []
        self.addCleanup(self._reap)
        # A refusal waits a backoff before exiting (default 60 s). These cells
        # run refusals through the real main(); keep them fast. The backoff
        # itself is pinned in test_surface_daemon_refusal_backoff_3361.py.
        env = unittest.mock.patch.dict(
            os.environ, {"ENGRAM_DAEMON_REFUSAL_BACKOFF_S": "0"}
        )
        env.start()
        self.addCleanup(env.stop)

    def _reap(self):
        for p in self.procs:
            if p.poll() is None:
                p.kill()
            p.wait()

    def _write_dead_pidfile(self) -> bytes:
        Path(self.pid).write_text(str(_dead_pid()))
        return Path(self.pid).read_bytes()

    def _touch_socket_file(self):
        Path(self.sock).write_text("")

    def _run_gate(self):
        """Run the REAL main() up to the gate and no further.

        Returns (code, stderr_text): code is the SystemExit code if the gate
        exited, or None if the gate let main() continue (detected by the
        stub EngramClient, the first thing main() touches after the gate,
        raising _PastGate). No daemon model is ever loaded.
        """
        class _PastGate(Exception):
            pass

        class _FakeClient:
            def __init__(self, *a, **k):
                raise _PastGate()

        fake = type(sys)("engram_client")
        fake.EngramClient = _FakeClient
        err = io.StringIO()
        code = None
        with unittest.mock.patch.dict(sys.modules, {"engram_client": fake}):
            with contextlib.redirect_stderr(err):
                try:
                    self.mod.main()
                except _PastGate:
                    pass
                except SystemExit as exc:
                    code = exc.code
        return code, err.getvalue()

    def _spawn_holder(self, listen: bool) -> subprocess.Popen:
        src = (
            "import socket,sys,time\n"
            "s=socket.socket(socket.AF_UNIX)\n"
            f"s.bind({self.sock!r})\n"
            + ("s.listen(4)\n" if listen else "")
            + "print('ready',flush=True)\n"
            "time.sleep(60)\n"
        )
        p = subprocess.Popen(
            [sys.executable, "-c", src], stdout=subprocess.PIPE, text=True
        )
        self.procs.append(p)
        self.assertEqual(p.stdout.readline().strip(), "ready")
        return p


# ---------------------------------------------------------------- H1
class TestH1ProcParser(_TmpMixin, unittest.TestCase):
    def _state(self, text: str):
        fx = self.tmp / "net_unix"
        fx.write_text(text)
        # proc_root points at an empty dir: no pid can be resolved.
        empty = self.tmp / "proc"
        empty.mkdir(exist_ok=True)
        return self.mod._socket_holder_state(
            self.sock, proc_net_unix=str(fx), proc_root=str(empty)
        )

    def test_exact_path_text_row_is_held_even_if_pid_unresolvable(self):
        state, pids = self._state(_HEADER + _row(self.sock))
        self.assertEqual(state, "HELD")
        self.assertEqual(list(pids), [])

    def test_non_matching_rows_are_free(self):
        for text in (
            _row("/some/other/path.sock"),
            _row(self.sock + ".bak"),
            _row(self.sock + "2"),
            _row("other.sock"),  # relative text, different basename
            _row(None),  # unnamed socket
            _row("@" + self.sock),  # abstract-namespace text
        ):
            with self.subTest(text=text):
                state, _ = self._state(_HEADER + text)
                self.assertEqual(state, "FREE")

    def _linked(self):
        real = self.tmp / "real"
        real.mkdir()
        link = self.tmp / "link"
        os.symlink(str(real), str(link))
        return str(real / "d.sock"), str(link / "d.sock")

    def test_symlink_given_holder_bound_via_realpath_is_held(self):
        real_sock, link_sock = self._linked()
        fx = self.tmp / "net_unix"
        fx.write_text(_HEADER + _row(real_sock))
        empty = self.tmp / "proc"
        empty.mkdir()
        state, _ = self.mod._socket_holder_state(
            link_sock, proc_net_unix=str(fx), proc_root=str(empty)
        )
        self.assertEqual(state, "HELD")

    def test_unrelated_longer_path_still_free_with_symlinked_home(self):
        real_sock, link_sock = self._linked()
        fx = self.tmp / "net_unix"
        fx.write_text(_HEADER + _row(real_sock + ".bak") + _row(real_sock + "2"))
        empty = self.tmp / "proc"
        empty.mkdir()
        state, _ = self.mod._socket_holder_state(
            link_sock, proc_net_unix=str(fx), proc_root=str(empty)
        )
        self.assertEqual(state, "FREE")

    def test_reverse_alias_row_for_symlink_spelling_given_realpath_is_held(self):
        # Replaces the earlier "known limit" cell that pinned FREE for this
        # row (#3490 security review): a row that can refer to our file must
        # never read FREE. See TestH1bRowClassification for the full matrix.
        real_sock, link_sock = self._linked()
        fx = self.tmp / "net_unix"
        fx.write_text(_HEADER + _row(link_sock))
        empty = self.tmp / "proc"
        empty.mkdir()
        state, _ = self.mod._socket_holder_state(
            real_sock, proc_net_unix=str(fx), proc_root=str(empty)
        )
        self.assertEqual(state, "HELD")

    def test_path_prefix_row_is_not_a_match(self):
        # a parent-dir row / shorter prefix of our path
        state, _ = self._state(_HEADER + _row(str(self.tmp)))
        self.assertEqual(state, "FREE")

    def test_header_only_and_malformed_rows_never_crash(self):
        for text in ("", _HEADER, _HEADER + "garbage\n", _HEADER + "\n\n",
                     _HEADER + "a b c\n", _HEADER + "\xff\xfe weird \x00\n"):
            with self.subTest(text=text):
                state, _ = self._state(text)
                self.assertEqual(state, "FREE")

    def test_match_found_among_other_rows(self):
        text = _HEADER + _row("/x") + "junk\n" + _row(self.sock, "7") + _row("/y")
        state, _ = self._state(text)
        self.assertEqual(state, "HELD")

    def test_holder_pid_resolved_through_proc_fd_links(self):
        proc = self.tmp / "proc"
        fd = proc / "4321" / "fd"
        fd.mkdir(parents=True)
        os.symlink("socket:[777]", str(fd / "3"))
        (proc / "999" / "fd").mkdir(parents=True)
        os.symlink("socket:[778]", str(proc / "999" / "fd" / "3"))
        fx = self.tmp / "net_unix"
        fx.write_text(_HEADER + _row(self.sock, "777"))
        state, pids = self.mod._socket_holder_state(
            self.sock, proc_net_unix=str(fx), proc_root=str(proc)
        )
        self.assertEqual(state, "HELD")
        self.assertEqual([int(p) for p in pids], [4321])


# ------------------------------------------- H1b row classification
class TestH1bRowClassification(_TmpMixin, unittest.TestCase):
    """The /proc rows name the BIND-TIME spelling, which may differ from our
    absolute `socket_path` (symlinked parent, relative bind). Each row is
    classified: refers-to-our-file -> HELD; provably another file -> ignore;
    cannot tell -> UNKNOWN. Fixture text plus a fake proc_root (fd links and
    per-pid cwd links); everything here is MODELLED, not a real /proc.
    """

    def setUp(self):
        super().setUp()
        self.home = self.tmp / "home"
        self.home.mkdir()
        self.sock = str(self.home / "d.sock")
        self.mod.SOCKET_PATH = self.sock
        self.proc = self.tmp / "proc"
        self.proc.mkdir()
        self.fx = self.tmp / "net_unix"

    def _proc_pid(self, pid, inode, cwd="UNSET"):
        d = self.proc / str(pid)
        (d / "fd").mkdir(parents=True)
        os.symlink(f"socket:[{inode}]", str(d / "fd" / "3"))
        if cwd != "UNSET" and cwd is not None:
            os.symlink(str(cwd), str(d / "cwd"))

    def _state(self, *rows, sock=None):
        self.fx.write_text(_HEADER + "".join(rows))
        return self.mod._socket_holder_state(
            sock or self.sock, proc_net_unix=str(self.fx), proc_root=str(self.proc)
        )

    # C1
    def test_c1_symlinked_directory_bind_given_real_path_is_held(self):
        link = self.tmp / "lnk"
        os.symlink(str(self.home), str(link))
        self._proc_pid(4321, 11)
        state, pids = self._state(_row(str(link / "d.sock"), "11"))
        self.assertEqual(state, "HELD")
        self.assertEqual([int(p) for p in pids], [4321])

    # C2
    def test_c2_reverse_real_row_given_symlink_spelling_is_held(self):
        link = self.tmp / "lnk"
        os.symlink(str(self.home), str(link))
        state, _ = self._state(_row(self.sock, "12"), sock=str(link / "d.sock"))
        self.assertEqual(state, "HELD")

    # C3
    def test_c3_relative_bind_with_holder_cwd_in_socket_dir_is_held(self):
        self._proc_pid(4322, 13, cwd=self.home)
        state, pids = self._state(_row("d.sock", "13"))
        self.assertEqual(state, "HELD")
        self.assertEqual([int(p) for p in pids], [4322])

    def test_c3b_relative_bind_cwd_reached_through_symlink_is_held(self):
        link = self.tmp / "lnk"
        os.symlink(str(self.home), str(link))
        self._proc_pid(4322, 13, cwd=link)
        state, _ = self._state(_row("d.sock", "13"))
        self.assertEqual(state, "HELD")

    # C4
    def test_c4_relative_bind_with_cwd_elsewhere_is_ignored(self):
        other = self.tmp / "elsewhere"
        other.mkdir()
        self._proc_pid(4323, 14, cwd=other)
        state, _ = self._state(_row("d.sock", "14"))
        self.assertEqual(state, "FREE")

    # C5
    def test_c5_relative_bind_cwd_unreadable_is_unknown(self):
        self._proc_pid(4324, 15, cwd=None)  # no cwd link
        state, _ = self._state(_row("d.sock", "15"))
        self.assertEqual(state, "UNKNOWN")

    def test_c5b_relative_bind_no_resolvable_pid_is_unknown(self):
        state, _ = self._state(_row("d.sock", "16"))
        self.assertEqual(state, "UNKNOWN")

    def test_relative_row_with_different_basename_is_ignored(self):
        state, _ = self._state(_row("other.sock", "17"))
        self.assertEqual(state, "FREE")

    # C6
    def test_c6_same_basename_decoy_in_other_existing_dir_is_free(self):
        other = self.tmp / "elsewhere"
        other.mkdir()
        state, _ = self._state(_row(str(other / "d.sock"), "18"))
        self.assertEqual(state, "FREE")

    # C7
    def test_c7_same_basename_in_nonexistent_dir_is_unknown(self):
        state, _ = self._state(_row(str(self.tmp / "gone" / "d.sock"), "19"))
        self.assertEqual(state, "UNKNOWN")

    def test_c7b_different_basename_in_nonexistent_dir_is_ignored(self):
        state, _ = self._state(_row(str(self.tmp / "gone" / "x.sock"), "19"))
        self.assertEqual(state, "FREE")

    # C8
    def test_c8_dangling_symlinked_directory_same_basename_is_unknown(self):
        dangling = self.tmp / "dangling"
        os.symlink(str(self.tmp / "nowhere"), str(dangling))
        state, _ = self._state(_row(str(dangling / "d.sock"), "20"))
        self.assertEqual(state, "UNKNOWN")

    # C9
    def test_c9_aggregate_decoy_plus_held_is_held(self):
        other = self.tmp / "elsewhere"
        other.mkdir()
        state, _ = self._state(
            _row(str(other / "d.sock"), "21"), _row(self.sock, "22")
        )
        self.assertEqual(state, "HELD")

    def test_c9_aggregate_decoy_plus_unknown_is_unknown(self):
        other = self.tmp / "elsewhere"
        other.mkdir()
        state, _ = self._state(
            _row(str(other / "d.sock"), "21"),
            _row(str(self.tmp / "gone" / "d.sock"), "23"),
        )
        self.assertEqual(state, "UNKNOWN")

    def test_c9_aggregate_held_beats_unknown(self):
        state, _ = self._state(
            _row(str(self.tmp / "gone" / "d.sock"), "23"), _row(self.sock, "22")
        )
        self.assertEqual(state, "HELD")

    # C11 -- gate level, driven by the fake proc files (helper not stubbed)
    def _gate_over_fake_proc(self, allow_unlink=False):
        real = self.mod._socket_holder_state
        wrapped = lambda p: real(  # noqa: E731
            p, proc_net_unix=str(self.fx), proc_root=str(self.proc)
        )
        Path(self.sock).write_text("")
        before = self._write_dead_pidfile()
        if allow_unlink:
            unlink = unittest.mock.Mock(wraps=os.unlink)
        else:
            unlink = unittest.mock.Mock(side_effect=AssertionError("unlink called"))
        with unittest.mock.patch.object(self.mod, "_socket_holder_state", wrapped):
            with unittest.mock.patch("os.unlink", unlink):
                code, err = self._run_gate()
        return code, err, before

    def test_c11_gate_refuses_symlinked_dir_bind_modelled(self):
        link = self.tmp / "lnk"
        os.symlink(str(self.home), str(link))
        self._proc_pid(4321, 11)
        self.fx.write_text(_HEADER + _row(str(link / "d.sock"), "11"))
        code, err, before = self._gate_over_fake_proc()
        self.assertEqual(code, self.mod.EXIT_SOCKET_HELD, err)
        self.assertTrue(os.path.exists(self.sock))
        self.assertEqual(Path(self.pid).read_bytes(), before)
        self.assertIn("4321", err)

    def test_c11_gate_refuses_relative_bind_modelled(self):
        self._proc_pid(4322, 13, cwd=self.home)
        self.fx.write_text(_HEADER + _row("d.sock", "13"))
        code, err, before = self._gate_over_fake_proc()
        self.assertEqual(code, self.mod.EXIT_SOCKET_HELD, err)
        self.assertTrue(os.path.exists(self.sock))
        self.assertEqual(Path(self.pid).read_bytes(), before)

    def test_c11_gate_refuses_unresolvable_relative_bind_unproven(self):
        self.fx.write_text(_HEADER + _row("d.sock", "16"))
        # The arm is decided by main()'s own /proc/net/unix test (#4374), not by
        # the fake proc fixture: the helper answers UNKNOWN either way.
        if os.path.exists("/proc/net/unix"):
            code, err, before = self._gate_over_fake_proc()
            self.assertEqual(code, self.mod.EXIT_SOCKET_UNPROVEN, err)
            self.assertTrue(os.path.exists(self.sock))
        else:
            # #4374: no /proc host, UNKNOWN is resolved by the lifetime lock:
            # gate passes, stale socket unlinked, lock held. (Bound: a daemon of
            # this version is excluded by the lock; any other holder of this
            # path is out of model -- Borges R1/R2.)
            code, err, before = self._gate_over_fake_proc(allow_unlink=True)
            self.assertIsNone(code, err)
            self.assertFalse(os.path.exists(self.sock))
            self.assertIsNotNone(self.mod._lock_fd, "the lifetime lock is held")
        self.assertEqual(Path(self.pid).read_bytes(), before)

    def test_c11_gate_same_spelling_control_is_held(self):
        self._proc_pid(4325, 24)
        self.fx.write_text(_HEADER + _row(self.sock, "24"))
        code, err, before = self._gate_over_fake_proc()
        self.assertEqual(code, self.mod.EXIT_SOCKET_HELD, err)
        self.assertTrue(os.path.exists(self.sock))
        self.assertEqual(Path(self.pid).read_bytes(), before)


# ---------------------------------------------------------------- H2
class TestH2LsofBranch(_TmpMixin, unittest.TestCase):
    def _state(self, run_side_effect=None, run_return=None):
        no_proc = str(self.tmp / "no-such-net-unix")
        with unittest.mock.patch(
            "subprocess.run", side_effect=run_side_effect, return_value=run_return
        ) as m:
            out = self.mod._socket_holder_state(self.sock, proc_net_unix=no_proc)
        self.last_call = m
        return out

    @staticmethod
    def _cp(rc, out=""):
        return subprocess.CompletedProcess(["lsof"], rc, stdout=out, stderr="")

    def test_rc0_with_pids_is_held(self):
        state, pids = self._state(run_return=self._cp(0, "123\n456\n"))
        self.assertEqual(state, "HELD")
        self.assertEqual(sorted(int(p) for p in pids), [123, 456])

    def test_lsof_is_not_asked_with_dash_U(self):
        # `lsof -U -- <path>` ORs the two selections and lists EVERY unix
        # socket holder on the host (measured on macOS): the pid list would
        # be false evidence. The path must be selected on its own.
        self._state(run_return=self._cp(0, "1\n"))
        argv = self.last_call.call_args[0][0]
        self.assertEqual(argv[0], "lsof")
        self.assertNotIn("-U", argv)
        self.assertEqual(argv[-1], self.sock)
        self.assertIn("timeout", self.last_call.call_args[1])

    def test_every_non_rc0_outcome_is_unknown(self):
        cases = {
            "rc1": dict(run_return=self._cp(1, "")),
            "rc1-with-output": dict(run_return=self._cp(1, "77\n")),
            "rc2": dict(run_return=self._cp(2, "")),
            "rc2-with-output": dict(run_return=self._cp(2, "77\n")),
            "timeout": dict(run_side_effect=subprocess.TimeoutExpired("lsof", 5)),
            "no-lsof": dict(run_side_effect=FileNotFoundError("lsof")),
            "oserror": dict(run_side_effect=PermissionError("x")),
            "rc0-empty": dict(run_return=self._cp(0, "")),
            "rc0-garbage": dict(run_return=self._cp(0, "lsof: WARNING blah\n")),
            "rc0-blank-lines": dict(run_return=self._cp(0, "\n\n")),
        }
        for name, kw in cases.items():
            with self.subTest(case=name):
                state, _ = self._state(**kw)
                self.assertEqual(state, "UNKNOWN")


# ------------------------------------------------- H3-H6 stubbed gate
class TestGateStubbed(_TmpMixin, unittest.TestCase):
    def _gate_with_helper(self, result):
        helper = unittest.mock.Mock(return_value=result)
        unlink = unittest.mock.Mock(side_effect=AssertionError("unlink called"))
        with unittest.mock.patch.object(self.mod, "_socket_holder_state", helper, create=True):
            if result[0] == "FREE" or (
                result[0] == "HELD" and list(result[1]) == [os.getpid()]
            ) or (
                # #4374: with no /proc main() resolves UNKNOWN to FREE (lock held)
                result[0] == "UNKNOWN" and not os.path.exists("/proc/net/unix")
            ):
                with unittest.mock.patch("os.unlink", wraps=os.unlink) as ul:
                    code, err = self._run_gate()
                    self.unlink_calls = ul.call_args_list
            else:
                with unittest.mock.patch("os.unlink", unlink):
                    code, err = self._run_gate()
                self.unlink_calls = unlink.call_args_list
        self.helper = helper
        return code, err

    def _assert_refusal(self, code, err, before_pid):
        self.assertIsNotNone(code, "gate must exit, not continue")
        self.assertNotEqual(code, 0)
        self.assertIsInstance(code, int)
        self.assertTrue(os.path.exists(self.sock), "socket file must survive")
        self.assertEqual(Path(self.pid).read_bytes(), before_pid)
        self.assertEqual(self.unlink_calls, [])
        lines = [l for l in err.splitlines() if l.strip()]
        self.assertEqual(len(lines), 1, err)
        self.assertTrue(lines[0].startswith("[engram-daemon]"), lines[0])
        self.assertIn(self.sock, lines[0])

    def test_h3_held_by_other_pid_refuses(self):
        self._touch_socket_file()
        before = self._write_dead_pidfile()
        other = os.getpid() + 1
        code, err = self._gate_with_helper(("HELD", [other]))
        self._assert_refusal(code, err, before)
        self.assertIn(str(other), err)
        self.assertIn("steal", err)
        self.assertEqual(code, self.mod.EXIT_SOCKET_HELD)
        self.assertNotIn(code, (0, 1))

    def test_h3_held_with_unknown_pids_refuses(self):
        self._touch_socket_file()
        before = self._write_dead_pidfile()
        code, err = self._gate_with_helper(("HELD", []))
        self._assert_refusal(code, err, before)
        self.assertIn("unknown", err)
        self.assertEqual(code, self.mod.EXIT_SOCKET_HELD)

    def test_h4_unknown_refuses(self):
        self._touch_socket_file()
        before = self._write_dead_pidfile()
        code, err = self._gate_with_helper(("UNKNOWN", []))
        # The arm is main()'s own os.path.exists("/proc/net/unix"), not the stub.
        if os.path.exists("/proc/net/unix"):
            self._assert_refusal(code, err, before)
            self.assertIn("could not prove", err)
            self.assertIn("by hand", err)
            self.assertEqual(code, self.mod.EXIT_SOCKET_UNPROVEN)
            self.assertNotIn(code, (0, 1))
        else:
            # #4374: no /proc, lifetime lock held: UNKNOWN := FREE. The gate
            # passes silently, unlinks the stale socket, keeps the pidfile.
            self.assertIsNone(code, err)
            self.assertEqual(err, "")
            self.assertFalse(os.path.exists(self.sock))
            self.assertEqual(Path(self.pid).read_bytes(), before)
            self.assertIsNotNone(self.mod._lock_fd, "the lifetime lock is held")

    def test_self_pid_only_holder_does_not_refuse(self):
        # Defensive filter: HELD with only our own pid falls through to the
        # unlink and the gate continues (unreachable in practice: we have
        # not bound yet at gate time).
        self._touch_socket_file()
        before = self._write_dead_pidfile()
        code, err = self._gate_with_helper(("HELD", [os.getpid()]))
        self.assertIsNone(code, err)
        self.assertFalse(os.path.exists(self.sock))
        self.assertEqual(Path(self.pid).read_bytes(), before)
        self.assertEqual(err, "")

    def test_self_pid_plus_other_pid_refuses_naming_only_the_other(self):
        self._touch_socket_file()
        before = self._write_dead_pidfile()
        other = os.getpid() + 1
        code, err = self._gate_with_helper(("HELD", [os.getpid(), other]))
        self._assert_refusal(code, err, before)
        self.assertEqual(code, self.mod.EXIT_SOCKET_HELD)
        self.assertIn(f"pid={other} ", err)
        # Parse only the `pid=<ids> --` segment: the line also carries the
        # random tmp socket path, which could contain a pid-like string.
        m = re.search(r"pid=([0-9,]+|unknown) --", err)
        self.assertIsNotNone(m, err)
        self.assertEqual(set(m.group(1).split(",")), {str(other)})
        self.assertNotIn(f"pid={os.getpid()} ", err)
        self.assertNotIn(f"{os.getpid()},", m.group(1) + ",")
        self.assertNotIn(f",{os.getpid()}", m.group(1))

    def test_h5_free_unlinks_and_continues(self):
        self._touch_socket_file()
        before = self._write_dead_pidfile()
        code, err = self._gate_with_helper(("FREE", []))
        self.assertIsNone(code)
        self.assertFalse(os.path.exists(self.sock))
        self.assertEqual(Path(self.pid).read_bytes(), before)
        self.assertEqual(err, "")

    def test_h5b_free_path_consults_the_helper_once(self):
        self._touch_socket_file()
        self._write_dead_pidfile()
        self._gate_with_helper(("FREE", []))
        self.helper.assert_called_once()

    def test_h6_recorded_pid_alive_exits_0_and_helper_not_called(self):
        self._touch_socket_file()
        Path(self.pid).write_text("1")
        helper = unittest.mock.Mock(side_effect=AssertionError("helper called"))
        with unittest.mock.patch.object(
            self.mod, "_recorded_pid_is_alive", return_value=True
        ), unittest.mock.patch.object(self.mod, "_socket_holder_state", helper, create=True):
            code, err = self._run_gate()
        self.assertEqual(code, 0)
        self.assertIn("a live daemon already owns", err)
        self.assertTrue(os.path.exists(self.sock))
        helper.assert_not_called()

    def test_no_socket_file_helper_not_called(self):
        self._write_dead_pidfile()
        helper = unittest.mock.Mock(side_effect=AssertionError("helper called"))
        with unittest.mock.patch.object(self.mod, "_socket_holder_state", helper, create=True):
            code, err = self._run_gate()
        self.assertIsNone(code)
        self.assertEqual(err, "")
        helper.assert_not_called()


# --------------------------------------------- H7-H9 real, unstubbed
@unittest.skipUnless(
    HAS_PROC_NET_UNIX or HAS_LSOF,
    "neither /proc/net/unix nor lsof on this host: no holder check possible",
)
class TestRealHolders(_TmpMixin, unittest.TestCase):
    def _assert_real_refusal(self, holder, before):
        code, err = self._run_gate()
        self.assertEqual(code, self.mod.EXIT_SOCKET_HELD, err)
        self.assertTrue(os.path.exists(self.sock))
        self.assertEqual(Path(self.pid).read_bytes(), before)
        self.assertIsNone(holder.poll(), "holder must still be alive")
        self.assertIn(str(holder.pid), err)
        lines = [l for l in err.splitlines() if l.strip()]
        self.assertEqual(len(lines), 1, err)

    def test_h7_real_listening_holder_is_not_stolen(self):
        holder = self._spawn_holder(listen=True)
        before = self._write_dead_pidfile()
        self._assert_real_refusal(holder, before)
        c = socket.socket(socket.AF_UNIX)
        self.addCleanup(c.close)
        c.settimeout(5)
        c.connect(self.sock)  # still accepts

    def test_h8_real_bound_not_listening_holder_is_not_stolen(self):
        holder = self._spawn_holder(listen=False)
        c = socket.socket(socket.AF_UNIX)
        self.addCleanup(c.close)
        with self.assertRaises(ConnectionRefusedError):
            c.connect(self.sock)  # connect() refusal is NOT absence
        before = self._write_dead_pidfile()
        self._assert_real_refusal(holder, before)

    def test_real_holder_bound_via_realpath_gate_given_symlink_path(self):
        """Holder binds through the real directory; the gate is handed the
        symlinked spelling (symlinked ENGRAM_HOME). On /proc hosts the
        realpath alternative makes this HELD. On the lsof host (macOS) lsof
        does not resolve the symlink: UNKNOWN, still a refusal. This cell
        pins whichever platform path runs, and claims no more than that."""
        real = self.tmp / "real"
        real.mkdir()
        link = self.tmp / "link"
        os.symlink(str(real), str(link))
        real_sock = str(real / "d.sock")
        self.mod.SOCKET_PATH = str(link / "d.sock")
        src = (
            "import socket,time\n"
            "s=socket.socket(socket.AF_UNIX)\n"
            f"s.bind({real_sock!r})\n"
            "s.listen(4)\n"
            "print('ready',flush=True)\n"
            "time.sleep(60)\n"
        )
        holder = subprocess.Popen(
            [sys.executable, "-c", src], stdout=subprocess.PIPE, text=True
        )
        self.procs.append(holder)
        self.assertEqual(holder.stdout.readline().strip(), "ready")
        before = self._write_dead_pidfile()
        code, err = self._run_gate()
        if HAS_PROC_NET_UNIX:
            self.assertEqual(code, self.mod.EXIT_SOCKET_HELD, err)
            self.assertTrue(os.path.exists(real_sock))
        else:
            # Measured on macOS: lsof does NOT resolve a symlinked directory
            # component (rc 1 for the symlink spelling), so the helper says
            # UNKNOWN. #4374 contract on a no-/proc host: the lifetime lock
            # resolves UNKNOWN to FREE, so the gate passes and unlinks the
            # path. Bound: lsof-by-path cannot see a live holder bound through
            # a symlinked parent or relative path; a daemon of this version is
            # excluded by the lock, any other holder is out of model (Borges
            # R1/R2, #4374). So the foreign holder's socket IS unlinked here.
            self.assertIsNone(code, err)
            self.assertFalse(os.path.exists(real_sock))
            self.assertIsNotNone(self.mod._lock_fd, "the lifetime lock is held")
        self.assertEqual(Path(self.pid).read_bytes(), before)
        self.assertIsNone(holder.poll())

    def _mira_case(self, bind_spelling, unproven_passes=False):
        """Mira's #3490 probe shape: holder is a real separate process,
        already listening; the gate is given the REAL pathname. bind_spelling
        is 'same' | 'alias' (bound through a symlinked parent) | 'relative'
        (bound as the bare basename with cwd = the socket directory)."""
        real = self.tmp / "real"
        real.mkdir()
        alias = self.tmp / "alias"
        os.symlink(str(real), str(alias))
        gatepath = str(real / "d.sock")
        bindpath = {
            "same": gatepath,
            "alias": str(alias / "d.sock"),
            "relative": "d.sock",
        }[bind_spelling]
        self.mod.SOCKET_PATH = gatepath
        src = (
            "import socket,time\n"
            "s=socket.socket(socket.AF_UNIX)\n"
            f"s.bind({bindpath!r})\n"
            "s.listen(2)\n"
            "print('ready',flush=True)\n"
            "time.sleep(60)\n"
        )
        holder = subprocess.Popen(
            [sys.executable, "-c", src],
            cwd=str(real),
            stdout=subprocess.PIPE,
            text=True,
        )
        self.procs.append(holder)
        self.assertEqual(holder.stdout.readline().strip(), "ready")
        before = self._write_dead_pidfile()
        code, err = self._run_gate()
        if unproven_passes:
            # #4374, no-/proc host, alias/relative bind: the helper says UNKNOWN
            # and the lifetime lock resolves it to FREE: gate passes, the
            # socket path is unlinked, lock held. Bound: lsof-by-path cannot
            # see a live holder bound through a symlinked parent or relative
            # path; a daemon of this version is excluded by the lock, any other
            # holder is out of model (Borges R1/R2, #4374).
            self.assertIsNone(code, err)
            self.assertFalse(os.path.exists(gatepath))
            self.assertIsNotNone(self.mod._lock_fd, "the lifetime lock is held")
            self.assertEqual(Path(self.pid).read_bytes(), before)
            self.assertIsNone(holder.poll())
            return code, err
        # Never past the gate, socket and pidfile untouched, holder alive.
        self.assertIsNotNone(code, "gate must not continue: " + err)
        self.assertTrue(os.path.exists(gatepath))
        self.assertEqual(Path(self.pid).read_bytes(), before)
        self.assertIsNone(holder.poll())
        return code, err

    def test_mira_same_spelling_control_is_held(self):
        code, _ = self._mira_case("same")
        self.assertEqual(code, self.mod.EXIT_SOCKET_HELD)

    def test_mira_reverse_alias_real_holder_is_refused(self):
        if HAS_PROC_NET_UNIX:
            code, _ = self._mira_case("alias")
            self.assertEqual(code, self.mod.EXIT_SOCKET_HELD)
        else:
            # #4374 contract (see _mira_case): lsof is blind to the alias.
            code, _ = self._mira_case("alias", unproven_passes=True)
            self.assertIsNone(code)

    def test_mira_relative_bind_real_holder_is_refused(self):
        if HAS_PROC_NET_UNIX:
            code, _ = self._mira_case("relative")
            self.assertEqual(code, self.mod.EXIT_SOCKET_HELD)
        else:
            # #4374 contract (see _mira_case): lsof is blind to the relative bind.
            code, _ = self._mira_case("relative", unproven_passes=True)
            self.assertIsNone(code)

    def test_h9_real_stale_orphan_platform_correct_outcome(self):
        """A socket file left by a process that exited (nobody bound).

        /proc host (Linux): the kernel lists nothing bound at the path ->
        FREE -> the gate unlinks and continues (no SystemExit).
        No-/proc host (macOS): lsof rc 1 is UNKNOWN, never FREE -> the gate
        refuses with EXIT_SOCKET_UNPROVEN and leaves the file. Both outcomes
        are pinned; neither is silent."""
        src = (
            "import socket\n"
            "s=socket.socket(socket.AF_UNIX)\n"
            f"s.bind({self.sock!r})\n"
        )
        subprocess.run([sys.executable, "-c", src], check=True)
        self.assertTrue(os.path.exists(self.sock), "orphan file must remain")
        before = self._write_dead_pidfile()
        code, err = self._run_gate()
        if HAS_PROC_NET_UNIX:
            self.assertIsNone(code, err)
            self.assertFalse(os.path.exists(self.sock))
        else:
            # #4374: lsof rc 1 is still UNKNOWN from the helper, but main()'s
            # lifetime lock now resolves it: lock held + pid dead + not HELD
            # is FREE, so the gate passes and the stale socket is unlinked.
            self.assertIsNone(code, err)
            self.assertFalse(os.path.exists(self.sock))
            self.assertIsNotNone(self.mod._lock_fd, "the lifetime lock is held")
        self.assertEqual(Path(self.pid).read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
