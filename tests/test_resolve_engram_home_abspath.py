"""Tests for gh#2695 -- engram-surface-daemon.py's _resolve_engram_home()
must always return an ABSOLUTE path, or start-engram-daemon.sh's unit-2
socket-ownership guard (#2652 phase-0, PR #2687) fails OPEN on a relative
ENGRAM_HOME.

Background (full detail in gh#2695, a finding from PR #2687's
colleague pass): the guard identifies a live socket holder by matching
/proc/net/unix's path-TEXT column against $SOCKET_PATH exactly as it was
passed to bind(). Pre-fix, `_resolve_engram_home()` returned
`os.environ.get("ENGRAM_HOME") or expanduser(...)` with no abspath/realpath
call. A relative ENGRAM_HOME therefore produced a relative bind string, the
guard's own (absolute) comparison target found zero matching rows, and the
Python snippet backing the guard exited 0 ("no live holder found") --
allowing exactly the delete the guard exists to prevent.

Not reachable through any shipped launch path today (the hook, the
.service template, and the .plist template all resolve/pass an absolute
$HOME/.engram-based path) -- this is a latent correctness gap in an
unenforced invariant, not a live incident. The fix lives entirely in
`_resolve_engram_home()`; this file does not touch, and is not testing,
start-engram-daemon.sh's own guard logic (that is
tests/test_socket_ownership_guard.py's scope).

Four things are verified:

  1. TestResolveEngramHomeDirectReturn -- direct, in-process unit tests of
     _resolve_engram_home() itself: relative input -> absolute output;
     already-absolute input -> unchanged (no regression on the common
     case); unset ENGRAM_HOME -> unchanged default-expansion behavior.
  2. TestRelativeEngramHomeBindRecordsAbsolutePath -- the real end-to-end
     regression (spec item 1): with a RELATIVE ENGRAM_HOME set from a
     specific cwd, load the REAL, shipped module fresh, bind a genuine
     AF_UNIX socket at its resolved SOCKET_PATH, and confirm /proc/net/unix
     records an ABSOLUTE path for that bind -- not just a string check on
     the function's return value in isolation.
  3. TestFalsificationPreFixRecordsRelativePath -- permanent falsification
     control (spec item 2): loads a copy of the daemon source with the
     abspath() wrapping mechanically stripped back to the pre-fix body
     (asserted present first, so a future refactor of this function fails
     this test loudly instead of silently no-opping the control), repeats
     test 2's real bind, and confirms it now records a RELATIVE path --
     i.e., test 2 would have gone red against the pre-fix code.
  4. TestGuardPathTextMatchIntegration -- spec item 3's "Python-level test
     that directly re-implements the guard's path-text-matching logic
     against the bind" (an acceptable substitute, per the issuing spec, for
     wiring up the full shell guard in-process). Re-implements ONLY the
     exact matching line the shell guard's own inline Python snippet uses
     (`parts[-1] == SOCKET_PATH`, skipping /proc/net/unix's header line --
     see start-engram-daemon.sh's `_holder_output=` heredoc) and shows: (a)
     against the pre-fix module's relative bind, the guard's own absolute
     comparison target finds zero rows -- the measured fail-open from the
     issue's own reproduction; (b) against the real, fixed module's
     absolute bind, the same comparison target finds the row -- the guard
     now correctly identifies the live holder.

Since alpha #3863 6b (Borges's ruling) a RELATIVE ENGRAM_HOME is not resolved to
an absolute path any more, it is REFUSED: _resolve_engram_home() goes through the
hook layer's engram_home_or_exit(), which prints one stderr line and exits 0
before anything binds. The invariant gh#2695 protects (never a relative bind)
therefore holds structurally. The pre-fix control still reverts the function to
its original shape (a relative-capable return) to reproduce the bug, and the
real-bind cells now use an ABSOLUTE home spelled from the same cwd.
"""

from __future__ import annotations

import contextlib
import importlib.util
import io
import os
import socket as socket_mod
import sys
import tempfile
import unittest
from pathlib import Path

import pytest

# #4093: each hook load runs the hook's module-level sys.path.insert; the shared window
# (#4029) puts sys.path and the src/engram modules back after each test.
pytestmark = pytest.mark.usefixtures("isolate_hook_loads")

REPO_ROOT = Path(__file__).resolve().parent.parent
DAEMON_DIR = REPO_ROOT / "src" / "engram" / "hooks" / "claude"
DAEMON_PY_PATH = DAEMON_DIR / "engram-surface-daemon.py"

# The exact post-fix body of _resolve_engram_home()'s return statement.
# Asserted present (via .index()/assert, below) before any test relies on
# stripping it back to the pre-fix shape -- if this function's shape ever
# changes, the falsification control fails loudly instead of silently
# testing nothing.
_POST_FIX_RETURN = "    return str(engram_home_or_exit())\n"

# The pre-fix body (byte-for-byte what shipped before gh#2695's fix --
# verified against `git show origin/dev~1:.../engram-surface-daemon.py`
# immediately prior to writing this file's patch, i.e. the tip PR #2687
# merged, before this PR's own commit).
_PRE_FIX_RETURN = (
    "    return (\n"
    "        os.environ.get(\"ENGRAM_HOME\")\n"
    "        or os.path.expanduser(\"~/.engram\")\n"
    "    )\n"
)


def _make_pre_fix_source_text() -> str:
    """The real, shipped daemon source with _resolve_engram_home()'s
    abspath() wrapping mechanically reverted to the pre-fix shape -- mirrors
    tests/test_socket_ownership_guard.py's _patch_in_race_hook technique
    (a targeted, asserted textual patch of a COPY of the script, never the
    shipped file, so a future refactor that moves this code fails this
    test's own assert rather than silently no-opping the falsification)."""
    source_text = DAEMON_PY_PATH.read_text()
    assert _POST_FIX_RETURN in source_text, (
        "expected post-fix `return str(engram_home_or_exit())` body not found in "
        "engram-surface-daemon.py -- _resolve_engram_home()'s shape changed "
        "(update this test's _POST_FIX_RETURN to match)"
    )
    return source_text.replace(_POST_FIX_RETURN, _PRE_FIX_RETURN, 1)


def _load_module_from_text(source_text: str, dest_path: Path, module_name: str):
    """Write source_text to dest_path and load it as a fresh module instance.
    A real file on disk (not exec() in a synthetic namespace) so that
    engram-surface-daemon.py's own `_this_dir = os.path.dirname(...)` /
    sys.path-insertion / `from _hooklib import resolve_runtime_dir` lines
    resolve exactly as they do for the shipped file -- dest_path is placed
    inside DAEMON_DIR's own directory tree indirectly via a symlink-free
    copy of _hooklib.py alongside it (see setUp below), not by importing
    the real hooks/claude directory, so this never touches the shipped
    module's own sys.modules entry."""
    dest_path.write_text(source_text)
    spec = importlib.util.spec_from_file_location(module_name, str(dest_path))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _load_prefix_control(copy_dir: Path, module_name: str):
    """Load the pre-fix copy of the daemon (the refusal reverted) from copy_dir.

    The copy imports _hooklib, which refuses a relative ENGRAM_HOME at import
    (exit 0). So _hooklib is loaded FIRST, with an absolute ENGRAM_HOME, and
    cached in sys.modules; the env is then restored (relative) for the exec of
    the daemon copy itself, which is the thing under test."""
    (copy_dir / "_hooklib.py").write_text((DAEMON_DIR / "_hooklib.py").read_text())
    saved_env = os.environ.get("ENGRAM_HOME")
    saved_mod = sys.modules.get("_hooklib")
    os.environ["ENGRAM_HOME"] = str(copy_dir)
    try:
        spec = importlib.util.spec_from_file_location("_hooklib", str(copy_dir / "_hooklib.py"))
        lib = importlib.util.module_from_spec(spec)
        sys.modules["_hooklib"] = lib
        spec.loader.exec_module(lib)
    finally:
        if saved_env is None:
            os.environ.pop("ENGRAM_HOME", None)
        else:
            os.environ["ENGRAM_HOME"] = saved_env
    try:
        return _load_module_from_text(
            _make_pre_fix_source_text(), copy_dir / "engram-surface-daemon.py", module_name
        )
    finally:
        if saved_mod is None:
            sys.modules.pop("_hooklib", None)
        else:
            sys.modules["_hooklib"] = saved_mod


def _load_real_daemon_module():
    """Load the REAL, shipped engram-surface-daemon.py as a fresh module
    instance (mirrors tests/test_surface_daemon_pid_reap.py's
    _load_daemon_module)."""
    spec = importlib.util.spec_from_file_location(
        "engram_surface_daemon_resolve_home_test", str(DAEMON_PY_PATH)
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _proc_net_unix_bind_texts() -> list[str]:
    """Every path-text /proc/net/unix currently records (its last
    whitespace-separated column, skipping the header line) -- the exact
    column the shell guard's own inline Python snippet reads (see
    start-engram-daemon.sh's `_holder_output=` heredoc,
    `parts[-1] == SOCKET_PATH`)."""
    with open("/proc/net/unix") as f:
        lines = f.readlines()
    out = []
    for line in lines[1:]:
        parts = line.split()
        if len(parts) >= 8:
            out.append(parts[-1])
    return out


def _guard_matches(bind_text_target: str) -> bool:
    """Re-implements ONLY the shell guard's own matching predicate --
    `parts[-1] == SOCKET_PATH` over /proc/net/unix's non-header lines --
    against a candidate comparison target. Deliberately does not
    re-implement the guard's inode->pid resolution (that part is
    tests/test_socket_ownership_guard.py's scope, exercising the real
    shell script); this function isolates exactly the path-TEXT matching
    step gh#2695 is about."""
    return bind_text_target in _proc_net_unix_bind_texts()


class TestResolveEngramHomeDirectReturn(unittest.TestCase):
    """Direct, in-process checks of _resolve_engram_home() itself --
    _resolve_engram_home() reads os.environ fresh on every call, so no
    module reload is needed for these (only the end-to-end bind tests
    below, which also check the module-level ENGRAM_HOME/SOCKET_PATH
    globals, need a fresh module load)."""

    def setUp(self):
        self.mod = _load_real_daemon_module()
        self._orig_engram_home = os.environ.get("ENGRAM_HOME")
        self.addCleanup(self._restore_env)

    def _restore_env(self):
        if self._orig_engram_home is None:
            os.environ.pop("ENGRAM_HOME", None)
        else:
            os.environ["ENGRAM_HOME"] = self._orig_engram_home

    def test_relative_engram_home_is_refused_not_resolved(self):
        """#3863 6b: a relative ENGRAM_HOME is refused (one stderr line naming it,
        exit 0), never turned into a path against this process's cwd."""
        os.environ["ENGRAM_HOME"] = "some/relative/home"
        err = io.StringIO()
        with contextlib.redirect_stderr(err), self.assertRaises(SystemExit) as cm:
            self.mod._resolve_engram_home()
        self.assertEqual(cm.exception.code, 0)
        lines = [ln for ln in err.getvalue().splitlines() if ln.strip()]
        self.assertEqual(len(lines), 1, err.getvalue())
        self.assertIn("some/relative/home", lines[0])
        self.assertIn("must be an absolute path", lines[0])

    def test_already_absolute_engram_home_unchanged(self):
        """Spec item 4: the common (and only currently-shipped) case --
        an already-absolute ENGRAM_HOME resolves to the SAME value before
        and after the fix (abspath() is a no-op on an already-absolute,
        already-normalized path)."""
        abs_home = os.path.join(tempfile.gettempdir(), "engram-home-test-abs")
        os.environ["ENGRAM_HOME"] = abs_home
        result = self.mod._resolve_engram_home()
        self.assertEqual(result, abs_home)

    def test_unset_engram_home_default_unchanged(self):
        os.environ.pop("ENGRAM_HOME", None)
        result = self.mod._resolve_engram_home()
        self.assertEqual(result, os.path.abspath(os.path.expanduser("~/.engram")))
        # os.path.expanduser("~/.engram") is already absolute in every
        # realistic environment, so this also confirms abspath() is a
        # genuine no-op on the default path -- no regression there either.
        self.assertEqual(result, os.path.expanduser("~/.engram"))


# The three real-bind classes below read /proc/net/unix; the condition is
# that primitive's absence, never the platform name (#3333). Subclasses
# inherit the skip from this base.
def _has_proc_net_unix() -> bool:
    return Path("/proc/net/unix").exists()


_NEEDS_PROC_NET_UNIX = unittest.skipUnless(
    _has_proc_net_unix(),
    "needs /proc/net/unix (absent on this host)",
)


@_NEEDS_PROC_NET_UNIX
class _RealBindFixture(unittest.TestCase):
    """Shared setup for the two end-to-end bind tests: a short-path temp
    directory (AF_UNIX's sun_path has a ~108-byte kernel limit, so this
    stays under tempfile's own /tmp-rooted default), a RELATIVE ENGRAM_HOME
    value, and a chdir into the temp dir so "relative" has a concrete,
    known cwd to resolve against."""

    RELATIVE_HOME_NAME = "relhome"

    def setUp(self):
        self._tmp_dir_obj = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp_dir_obj.cleanup)
        self.tmp = Path(self._tmp_dir_obj.name).resolve()

        self._orig_cwd = os.getcwd()
        os.chdir(self.tmp)
        self.addCleanup(os.chdir, self._orig_cwd)

        self._orig_engram_home = os.environ.get("ENGRAM_HOME")
        os.environ["ENGRAM_HOME"] = self.RELATIVE_HOME_NAME
        self.addCleanup(self._restore_env)

        self._sockets_to_close = []
        self.addCleanup(self._close_sockets)

    def _restore_env(self):
        if self._orig_engram_home is None:
            os.environ.pop("ENGRAM_HOME", None)
        else:
            os.environ["ENGRAM_HOME"] = self._orig_engram_home

    def _close_sockets(self):
        for s in self._sockets_to_close:
            try:
                s.close()
            except OSError:
                pass

    def _bind(self, socket_path: str) -> socket_mod.socket:
        os.makedirs(os.path.dirname(socket_path), exist_ok=True)
        srv = socket_mod.socket(socket_mod.AF_UNIX, socket_mod.SOCK_STREAM)
        srv.bind(socket_path)
        srv.listen(1)
        self._sockets_to_close.append(srv)
        return srv


class TestRelativeEngramHomeBindRecordsAbsolutePath(_RealBindFixture):
    """Spec item 1 -- the regression this fix closes, end-to-end via a real
    bind (not just a string-equality check on the function's return
    value)."""

    def test_bind_through_relative_home_records_absolute_path(self):
        # #3863 6b: the relative spelling is refused now (see
        # TestRelativeHomeIsRefusedBeforeAnyBind); the bind cell uses the ABSOLUTE
        # form of the same directory, spelled from the same cwd.
        os.environ["ENGRAM_HOME"] = str(self.tmp / self.RELATIVE_HOME_NAME)
        mod = _load_real_daemon_module()

        # Module-level ENGRAM_HOME/SOCKET_PATH were computed once, at
        # import time, from the RELATIVE env var + this test's cwd -- both
        # must already be absolute.
        self.assertTrue(
            os.path.isabs(mod.ENGRAM_HOME),
            f"mod.ENGRAM_HOME must be absolute, got: {mod.ENGRAM_HOME!r}",
        )
        self.assertTrue(
            os.path.isabs(mod.SOCKET_PATH),
            f"mod.SOCKET_PATH must be absolute, got: {mod.SOCKET_PATH!r}",
        )
        expected_home = os.path.abspath(str(self.tmp / self.RELATIVE_HOME_NAME))
        self.assertEqual(mod.ENGRAM_HOME, expected_home)

        self._bind(mod.SOCKET_PATH)

        recorded = _proc_net_unix_bind_texts()
        self.assertIn(
            mod.SOCKET_PATH, recorded,
            "expected /proc/net/unix to record the absolute bind path "
            f"{mod.SOCKET_PATH!r}; recorded texts: {recorded!r}",
        )
        # The relative literal (what the pre-fix code would have bound)
        # must NOT be what got recorded.
        relative_literal = os.path.join(self.RELATIVE_HOME_NAME, "recall-daemon.sock")
        self.assertNotIn(relative_literal, recorded)


class TestRelativeHomeIsRefusedBeforeAnyBind(unittest.TestCase):
    """#3863 6b: with a RELATIVE ENGRAM_HOME the shipped daemon exits 0 at load,
    before it computes SOCKET_PATH or binds anything. Runs on every platform
    (no /proc needed)."""

    def test_loading_the_daemon_under_a_relative_home_exits_zero_and_binds_nothing(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td).resolve()
            orig_cwd = os.getcwd()
            orig_env = os.environ.get("ENGRAM_HOME")
            os.chdir(tmp)
            os.environ["ENGRAM_HOME"] = "relhome"
            err = io.StringIO()
            try:
                with contextlib.redirect_stderr(err), self.assertRaises(SystemExit) as cm:
                    _load_real_daemon_module()
            finally:
                os.chdir(orig_cwd)
                if orig_env is None:
                    os.environ.pop("ENGRAM_HOME", None)
                else:
                    os.environ["ENGRAM_HOME"] = orig_env
            self.assertEqual(cm.exception.code, 0)
            self.assertIn("relhome", err.getvalue())
            self.assertEqual(sorted(p.name for p in tmp.iterdir()), [], "nothing may be created")


class TestFalsificationPreFixStillComputesARelativePath(unittest.TestCase):
    """Permanent falsification control, runs everywhere: with the refusal
    mechanically reverted (see _make_pre_fix_source_text) the same relative
    ENGRAM_HOME yields a RELATIVE ENGRAM_HOME and SOCKET_PATH - the root of
    gh#2695 - so the refusal cell above would have gone red against it."""

    def test_prefix_copy_has_a_relative_socket_path(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td).resolve()
            copy_dir = tmp / "prefix_copy_text"
            copy_dir.mkdir()
            orig_cwd = os.getcwd()
            orig_env = os.environ.get("ENGRAM_HOME")
            os.chdir(tmp)
            os.environ["ENGRAM_HOME"] = "relhome"
            try:
                mod = _load_prefix_control(copy_dir, "engram_surface_daemon_prefix_text_control")
            finally:
                os.chdir(orig_cwd)
                if orig_env is None:
                    os.environ.pop("ENGRAM_HOME", None)
                else:
                    os.environ["ENGRAM_HOME"] = orig_env
            self.assertFalse(os.path.isabs(mod.ENGRAM_HOME), mod.ENGRAM_HOME)
            self.assertEqual(mod.ENGRAM_HOME, "relhome")
            self.assertFalse(os.path.isabs(mod.SOCKET_PATH), mod.SOCKET_PATH)


class TestFalsificationPreFixRecordsRelativePath(_RealBindFixture):
    """Spec item 2 -- permanent falsification control: with the abspath()
    wrapping mechanically reverted (see _make_pre_fix_source_text), the
    exact same real-bind exercise now records a RELATIVE path. Proves test
    1 above would have gone red against the pre-fix code, without ever
    modifying the shipped file."""

    def test_prefix_module_binds_relative_path(self):
        copy_dir = self.tmp / "prefix_copy"
        copy_dir.mkdir()
        mod = _load_prefix_control(copy_dir, "engram_surface_daemon_prefix_control")

        self.assertFalse(
            os.path.isabs(mod.ENGRAM_HOME),
            "pre-fix control must reproduce the bug: ENGRAM_HOME should be "
            f"relative here, got: {mod.ENGRAM_HOME!r}",
        )
        self.assertEqual(mod.ENGRAM_HOME, self.RELATIVE_HOME_NAME)

        self._bind(mod.SOCKET_PATH)

        recorded = _proc_net_unix_bind_texts()
        relative_literal = os.path.join(self.RELATIVE_HOME_NAME, "recall-daemon.sock")
        self.assertIn(
            relative_literal, recorded,
            "pre-fix control must record the RELATIVE bind text (the bug "
            f"this fix closes); recorded texts: {recorded!r}",
        )
        # And the absolute form the FIXED code would have produced must be
        # absent -- confirming this really is the un-fixed behavior, not a
        # coincidental match.
        would_be_fixed_path = os.path.abspath(mod.SOCKET_PATH)
        self.assertNotIn(would_be_fixed_path, recorded)


class TestGuardPathTextMatchIntegration(_RealBindFixture):
    """Spec item 3 -- a Python-level re-implementation of the shell guard's
    own path-text-matching predicate (`parts[-1] == SOCKET_PATH`), run
    against both the pre-fix and post-fix bind, to show the guard's actual
    pass/fail verdict flips exactly as gh#2695 describes.

    The guard's own $SOCKET_PATH is built by start-engram-daemon.sh from
    whatever ENGRAM_HOME string reaches IT -- in every currently-shipped
    launch path that string is always absolute (the hook, the .service
    template, and the .plist template all resolve an absolute
    $HOME/.engram-based path per gh#2695's own stated bound), so the
    guard's comparison target here is modeled as the absolute form of the
    same ENGRAM_HOME value -- exactly the `/tmp/.../live.sock` (absolute,
    "match works") side of gh#2695's own measured contrast, as opposed to
    the `rel.sock` (relative, "match FAILS") side."""

    def test_guard_fails_open_on_prefix_relative_bind(self):
        copy_dir = self.tmp / "prefix_copy_guard"
        copy_dir.mkdir()
        mod = _load_prefix_control(copy_dir, "engram_surface_daemon_prefix_guard_control")
        self._bind(mod.SOCKET_PATH)

        # The guard's own comparison target: the ABSOLUTE form of the same
        # ENGRAM_HOME the daemon was given (see class docstring).
        guard_socket_path = os.path.abspath(
            os.path.join(self.RELATIVE_HOME_NAME, "recall-daemon.sock")
        )
        self.assertFalse(
            _guard_matches(guard_socket_path),
            "the guard's path-text match must find ZERO rows against the "
            "pre-fix (relative) bind -- this is the measured fail-open "
            "gh#2695 exists to close",
        )

    def test_guard_identifies_live_holder_on_fixed_absolute_bind(self):
        os.environ["ENGRAM_HOME"] = str(self.tmp / self.RELATIVE_HOME_NAME)  # #3863 6b: absolute form
        mod = _load_real_daemon_module()
        self._bind(mod.SOCKET_PATH)

        guard_socket_path = os.path.abspath(
            os.path.join(self.RELATIVE_HOME_NAME, "recall-daemon.sock")
        )
        # The fixed daemon's own bind path and the guard's independently
        # -constructed comparison target must be the identical absolute
        # string -- the invariant this fix makes true at the source.
        self.assertEqual(mod.SOCKET_PATH, guard_socket_path)
        self.assertTrue(
            _guard_matches(guard_socket_path),
            "the guard's path-text match must find the live holder's row "
            "now that the bind is absolute",
        )


if __name__ == "__main__":
    unittest.main()
