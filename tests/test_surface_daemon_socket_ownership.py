"""Tests for engram-alpha#2713 -- surface-daemon cleanup() unlinking the
shared socket path on PIDFILE IDENTITY rather than SOCKET OWNERSHIP.

Daemon-side sibling of #2687 (which guarded the *hook's* `rm -f` on socket
ownership) -- this covers the daemon's own atexit/cleanup() path, which
#2687 did not touch.

The bug: cleanup() decided whether to unlink SOCKET_PATH using PID_PATH
identity (or the pidfile's absence) as a proxy for "did this process bind
the socket". On a two-daemon seat, the hook-launched instance is the one
that writes the pidfile, but a DIFFERENT, supervisor-launched instance may
be the one actually holding SOCKET_PATH -- so the hook-launched instance's
own clean exit deleted the socket the OTHER, live daemon was serving.

The fix: track ownership at BIND TIME (`_bound_socket`, set only after a
successful server.bind()) and gate the SOCKET_PATH unlink on that flag,
independent of PID_PATH identity. PID_PATH's own unlink keeps its
pre-existing identity check unchanged -- two different resources, two
different predicates (see cleanup()'s own docstring in
engram-surface-daemon.py).

Required tests (per issue #2713):
  1. A non-binding instance's cleanup() must NOT delete a live socket.
  2. A binding instance's cleanup() DOES delete the socket it bound.
  3. PID_PATH being absent/unreadable is no longer, on its own,
     sufficient to unlink the socket (pre-fix, the "nothing to protect,
     proceed" fallback applied to BOTH resources; it must now apply to
     PID_PATH only).
"""

from __future__ import annotations

import importlib.util
import os
import sys
import tempfile
import unittest
import unittest.mock
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DAEMON_DIR = REPO_ROOT / "src" / "engram" / "hooks" / "claude"
DAEMON_PY_PATH = DAEMON_DIR / "engram-surface-daemon.py"

import pytest  # noqa: E402
# #4029: in-process hook/tools loads run inside this window (sys.path / sys.modules restored).
pytestmark = pytest.mark.usefixtures("isolate_hook_loads")


def _load_daemon_module():
    """Load engram-surface-daemon as a fresh module instance (mirrors
    test_surface_daemon_idle_timeout.py / test_surface_daemon_pid_reap.py's
    loading pattern)."""
    spec = importlib.util.spec_from_file_location(
        "engram_surface_daemon_socket_ownership", str(DAEMON_PY_PATH)
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class TestSocketUnlinkGatedOnBindOwnership(unittest.TestCase):
    """Direct cleanup() unit tests -- mirrors the established
    TestCleanupOwnershipGuard pattern in test_surface_daemon_pid_reap.py,
    but manipulates `_bound_socket` (the NEW predicate) rather than only
    PID_PATH identity (the OLD, now-insufficient-alone predicate)."""

    def setUp(self):
        self._tmp_dir_obj = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp_dir_obj.cleanup)
        self.tmp = Path(self._tmp_dir_obj.name)
        self.mod = _load_daemon_module()
        self.mod.PID_PATH = str(self.tmp / "recall-daemon.pid")
        self.mod.SOCKET_PATH = str(self.tmp / "recall-daemon.sock")
        # A fresh module load already defaults _bound_socket to False, but
        # state it explicitly -- every assertion below depends on it.
        self.mod._bound_socket = False

    # -- test 1: a non-binder must not delete a live socket ---------------

    def test_non_binder_survives_pidfile_naming_it(self):
        """THE REGRESSION (#2713's core bug). This process wrote PID_PATH
        (still_ours=True -- exactly the hook-launched-instance shape) but
        NEVER bound the socket (_bound_socket=False -- exactly the
        two-daemon shape where a DIFFERENT, supervisor-launched instance
        is the one actually serving SOCKET_PATH). cleanup() must not
        delete the socket just because the pidfile happens to name this
        process."""
        Path(self.mod.SOCKET_PATH).write_text("live daemon's socket marker")
        Path(self.mod.PID_PATH).write_text(str(os.getpid()))
        self.mod._bound_socket = False

        with self.assertRaises(SystemExit):
            self.mod.cleanup()

        self.assertTrue(
            Path(self.mod.SOCKET_PATH).exists(),
            "cleanup() deleted a socket this process never bound, purely "
            "on pidfile identity -- the exact #2713 bug",
        )
        # PID_PATH unlink is UNCHANGED behavior -- still_ours=True still
        # removes it; only the socket's predicate changed.
        self.assertFalse(Path(self.mod.PID_PATH).exists())

    def test_non_binder_survives_pidfile_absent(self):
        """Test 3 (issue's own numbering): PID_PATH absent/unreadable used
        to be read as "nothing to protect, proceed" for BOTH resources
        (the pre-fix fallback). It must no longer be sufficient, ON ITS
        OWN, to unlink the socket -- only `_bound_socket` decides that
        now."""
        Path(self.mod.SOCKET_PATH).write_text("live daemon's socket marker")
        # No PID_PATH file at all.
        self.mod._bound_socket = False

        with self.assertRaises(SystemExit):
            self.mod.cleanup()

        self.assertTrue(
            Path(self.mod.SOCKET_PATH).exists(),
            "pidfile-absence alone unlinked the socket -- it must no "
            "longer be sufficient on its own (issue #2713 test 3)",
        )

    # -- test 2: the binder's exit DOES remove the socket ------------------

    def test_binder_removes_its_own_socket(self):
        """This process DID bind the socket (_bound_socket=True) -- its
        own clean exit must still remove it, regardless of what PID_PATH
        says. Confirms the fix isn't 'never unlink anything'."""
        Path(self.mod.SOCKET_PATH).write_text("this process's own socket")
        Path(self.mod.PID_PATH).write_text(str(os.getpid()))
        self.mod._bound_socket = True

        with self.assertRaises(SystemExit):
            self.mod.cleanup()

        self.assertFalse(
            Path(self.mod.SOCKET_PATH).exists(),
            "the binder's own cleanup() must remove the socket it bound",
        )
        self.assertFalse(Path(self.mod.PID_PATH).exists())

    def test_binder_removes_its_own_socket_even_if_pidfile_absent(self):
        """_bound_socket is independent of PID_PATH -- a binder whose
        pidfile is (for whatever reason) missing must still remove the
        socket it itself bound."""
        Path(self.mod.SOCKET_PATH).write_text("this process's own socket")
        self.mod._bound_socket = True

        with self.assertRaises(SystemExit):
            self.mod.cleanup()

        self.assertFalse(Path(self.mod.SOCKET_PATH).exists())


class TestBackOffPathNeverBindsSocket(unittest.TestCase):
    """Integration-shaped check on the real two-instance scenario named in
    the issue: main()'s own pre-bind back-off branch (a live daemon
    already owns SOCKET_PATH -- back off rather than steal it) must exit
    WITHOUT ever setting `_bound_socket`, so nothing this instance does
    afterward could wrongly claim ownership of a socket it never bound."""

    def setUp(self):
        self._tmp_dir_obj = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp_dir_obj.cleanup)
        self.tmp = Path(self._tmp_dir_obj.name)
        self.mod = _load_daemon_module()
        self.mod.PID_PATH = str(self.tmp / "recall-daemon.pid")
        self.mod.SOCKET_PATH = str(self.tmp / "recall-daemon.sock")

    def test_back_off_branch_never_sets_bound_socket(self):
        # A confirmably-alive "prior" instance -- use this very test
        # process's own pid, which is trivially alive.
        Path(self.mod.PID_PATH).write_text(str(os.getpid()))
        Path(self.mod.SOCKET_PATH).write_text("a live daemon's socket")

        with self.assertRaises(SystemExit) as ctx:
            self.mod.main()

        self.assertEqual(ctx.exception.code, 0)
        self.assertFalse(
            self.mod._bound_socket,
            "the back-off branch must exit before ever binding -- "
            "_bound_socket must stay False",
        )
        self.assertTrue(
            Path(self.mod.SOCKET_PATH).exists(),
            "the back-off branch must not touch the live daemon's socket",
        )


class TestBindFailureDoesNotClaimOwnership(unittest.TestCase):
    """Mutation-control-shaped test: exercises the REAL main() bind() call
    site (unlike the direct-`_bound_socket`-manipulation tests above),
    so it is sensitive to the ordering of the `_bound_socket = True`
    assignment relative to `server.bind(SOCKET_PATH)`.

    If `_bound_socket` were set BEFORE calling bind() (a plausible-wrong
    variant of the fix) rather than after, a bind() that raises would
    still leave the flag True -- falsely claiming ownership of a socket
    this process never actually bound. The correct fix only reaches the
    assignment line if bind() returns without raising."""

    def setUp(self):
        self._tmp_dir_obj = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp_dir_obj.cleanup)
        self.tmp = Path(self._tmp_dir_obj.name)
        self.mod = _load_daemon_module()
        self.mod.PID_PATH = str(self.tmp / "recall-daemon.pid")
        self.mod.SOCKET_PATH = str(self.tmp / "recall-daemon.sock")

        # Stub out engram_client so main() doesn't try to load a real
        # embedding model. Registered in sys.modules BEFORE main() runs
        # its `from engram_client import EngramClient`, which resolves
        # against the module cache rather than re-executing a file.
        import types
        stub_module = types.ModuleType("engram_client")

        class _StubClient:
            def call(self, *a, **kw):
                return {}

        stub_module.EngramClient = _StubClient
        self._engram_client_patch = unittest.mock.patch.dict(
            sys.modules, {"engram_client": stub_module}
        )
        self._engram_client_patch.start()
        self.addCleanup(self._engram_client_patch.stop)

    def test_bound_socket_stays_false_if_bind_raises(self):
        import socket as socket_mod

        bind_error = OSError("simulated bind failure")

        with unittest.mock.patch.object(
            socket_mod.socket, "bind", side_effect=bind_error
        ):
            with self.assertRaises(OSError):
                self.mod.main()

        self.assertFalse(
            self.mod._bound_socket,
            "_bound_socket read True after a bind() that raised -- the "
            "assignment must happen strictly AFTER a successful bind(), "
            "never before it",
        )


if __name__ == "__main__":
    unittest.main()
