"""The owner and a non-regular pidfile -- issue #3768 (follow-up to the read-side fix).

Contract (the ruling on #3768): the real owner REFUSES TO START, by name and
with its own exit code, when the pidfile path exists and is not a regular file;
its write never follows a symlink (on a write the follow IS the truncation);
its cleanup read never blocks and never unlinks what it does not own.

Cells (all through the real `EngramdOwner` / `main()`; anything that can block
runs in a CHILD process under a kill timeout, so a regression is a red cell and
not a hung run):

  1. FIFO at the pidfile path before start: `OwnerPidfileNotRegular`, no
     socket, via `main()` exit code `EXIT_PATH_NOT_REGULAR` and stderr
     naming the path and "FIFO".
  2. FIFO swapped in AFTER the pre-bind check: same refusal, the owner's own
     socket removed, both store connections closed, no hang.
  3. Symlink to a sentinel: (a) pre-bind, (b) swapped in after the pre-bind
     check -- refusal both ways, the sentinel's bytes unchanged, no socket.
  4. A directory at the path: (a) pre-bind, (b) swapped in after the check.
  5. FIFO planted WHILE the owner runs: stop completes under a timeout, the
     FIFO is still there, the socket is removed.
  6. Controls: a stale regular pidfile (dead pid / garbage) is overwritten and
     start succeeds; no pidfile -> created regular, holding the pid, with the
     mode `write_text` would have produced; any OTHER OSError (EACCES)
     propagates as itself, not as the named refusal.
  7. A regular pidfile naming this pid is still removed on stop.

Every cell's docstring names the mutation that must redden it.
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import socket
import stat
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
_SRC_ENGRAM = str(REPO_ROOT / "src" / "engram")
if _SRC_ENGRAM not in sys.path:
    sys.path.insert(0, _SRC_ENGRAM)

from _owner_platform import requires_supported_owner  # noqa: E402
from _owner_store import seed_current_store  # noqa: E402
import engramd_owner as eo  # noqa: E402

pytestmark = requires_supported_owner

CHILD_TIMEOUT = 20.0

_CHILD = r"""
import json, os, socket, sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
import engramd_owner as eo

scenario = sys.argv[2]
home = Path(sys.argv[3])
sentinel = home / "sentinel.txt"


def plant(kind, path):
    try:
        path.unlink()
    except FileNotFoundError:
        pass
    if kind == "fifo":
        os.mkfifo(path)
    elif kind == "symlink":
        os.symlink(sentinel, path)
    elif kind == "dir":
        os.mkdir(path)
    elif kind == "sock":
        sk = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sk.bind(str(path))
        sk.close()
    elif kind == "hardlink":
        os.link(sentinel, path)
    elif kind == "fifo_reader":
        os.mkfifo(path)
        HELD.append(os.open(path, os.O_RDONLY | os.O_NONBLOCK))


HELD = []
owner = eo.EngramdOwner(home, request_deadline=0.6)
if scenario.startswith("inject_"):
    kind = scenario[len("inject_"):]
    real = eo.EngramdOwner._check_stale_socket_or_raise

    def patched(self):
        real(self)
        plant(kind, self.pid_path)

    eo.EngramdOwner._check_stale_socket_or_raise = patched

named = getattr(eo, "OwnerPidfileNotRegular", None)
exc_name = None
exc_text = None
is_named = False
try:
    owner.start(install_signal_handlers=False)
except BaseException as exc:
    exc_name = type(exc).__name__
    exc_text = str(exc)
    is_named = named is not None and isinstance(exc, named)
else:
    owner.stop()
print(json.dumps({
    "exc": exc_name,
    "text": exc_text,
    "is_named": is_named,
    "socket_exists": os.path.lexists(owner.socket_path),
    "conns_closed": owner._owner_conn is None and owner._client_read_conn is None,
}))
"""


@pytest.fixture
def home():
    d = tempfile.mkdtemp(prefix="p68-")  # short: AF_UNIX path limits
    # #3753: the schema gate runs BEFORE the pidfile check, so a cell expecting a pidfile
    # refusal (or a successful start) needs a current-schema store. Explicit, not the
    # conftest default: most cells run the owner in a CHILD process the default cannot reach.
    seed_current_store(d)
    try:
        yield Path(d)
    finally:
        shutil.rmtree(d, ignore_errors=True)


SENTINEL_BYTES = b"S" * 135


def _sentinel(home: Path) -> Path:
    s = home / "sentinel.txt"
    s.write_bytes(SENTINEL_BYTES)
    return s


def _run_child(home: Path, scenario: str) -> dict:
    """Run the scenario in a child under a kill timeout. A hang is a red cell."""
    proc = subprocess.Popen(
        [sys.executable, "-c", _CHILD, _SRC_ENGRAM, scenario, str(home)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        out, err = proc.communicate(timeout=CHILD_TIMEOUT)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.communicate()
        pytest.fail(f"scenario {scenario!r} HUNG past {CHILD_TIMEOUT}s (killed)")
    assert proc.returncode == 0, f"child crashed: rc={proc.returncode} stderr={err!r}"
    return json.loads(out.strip().splitlines()[-1])


def _run_main(home: Path) -> subprocess.CompletedProcess:
    env = dict(os.environ, ENGRAM_HOME=str(home), PYTHONDONTWRITEBYTECODE="1")
    code = f"import sys; sys.path.insert(0, {_SRC_ENGRAM!r}); import engramd_owner as eo; sys.exit(eo.main())"
    proc = subprocess.Popen(
        [sys.executable, "-c", code], env=env,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    try:
        out, err = proc.communicate(timeout=CHILD_TIMEOUT)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.communicate()
        pytest.fail(f"main() HUNG past {CHILD_TIMEOUT}s (killed)")
    return subprocess.CompletedProcess(proc.args, proc.returncode, out, err)


def _pidfile(home: Path) -> Path:
    return home / eo.DEFAULT_PID_NAME


def _socket(home: Path) -> Path:
    return home / "run" / "engramd.sock"


# ---------------------------------------------------------------------------
# Cell 1
# ---------------------------------------------------------------------------


def test_fifo_before_start_is_refused_by_name_and_binds_nothing(home):
    """Mutation: drop the pre-bind check -> the refusal comes late (the socket
    was bound then cleaned up) or not at all; here `socket_exists` stays False
    only on the pre-bind path, but the real discriminator is the main() cell
    below, which needs the named exit code."""
    os.mkfifo(_pidfile(home))
    res = _run_child(home, "plain")
    assert res["exc"] == "OwnerPidfileNotRegular" and res["is_named"], res
    assert not res["socket_exists"], res
    assert not os.path.lexists(_socket(home))
    assert stat.S_ISFIFO(os.lstat(_pidfile(home)).st_mode), "the FIFO must be left alone"
    assert "FIFO" in res["text"] and str(_pidfile(home)) in res["text"]
    assert "nothing was bound" in res["text"]


def test_fifo_before_start_via_main_exit_code_and_stderr(home):
    """Mutation: drop the pre-bind check, or the main() catch -> wrong exit code."""
    os.mkfifo(_pidfile(home))
    proc = _run_main(home)
    assert proc.returncode == eo.EXIT_PATH_NOT_REGULAR == 4, (proc.returncode, proc.stderr)
    assert str(_pidfile(home)) in proc.stderr and "FIFO" in proc.stderr
    assert not os.path.lexists(_socket(home))


def test_pre_bind_check_runs_before_anything_is_bound(home):
    """Mutation: drop the pre-bind check -> the write-time mapping still
    refuses, but only AFTER the socket was bound. Observable: the stale-socket
    check (which precedes the bind) is never reached on a non-regular pidfile."""
    os.mkfifo(_pidfile(home))
    owner = eo.EngramdOwner(home, request_deadline=0.6)
    reached = []
    real = eo.EngramdOwner._check_stale_socket_or_raise
    eo.EngramdOwner._check_stale_socket_or_raise = lambda self: reached.append(1) or real(self)
    try:
        with pytest.raises(eo.OwnerPidfileNotRegular):
            owner.start(install_signal_handlers=False)
    finally:
        eo.EngramdOwner._check_stale_socket_or_raise = real
    assert not reached, "refusal must precede _check_stale_socket_or_raise"


# ---------------------------------------------------------------------------
# Cell 2
# ---------------------------------------------------------------------------


def test_fifo_swapped_in_after_the_check_is_refused_and_cleaned_up(home):
    """Mutations: drop O_NONBLOCK (hang -> timeout); do not call _cleanup() on
    the mapped failure (socket left behind / connections open)."""
    res = _run_child(home, "inject_fifo")
    assert res["exc"] == "OwnerPidfileNotRegular" and res["is_named"], res
    assert not res["socket_exists"], "the owner's own socket must be removed"
    assert res["conns_closed"], "both store connections must be closed"
    assert stat.S_ISFIFO(os.lstat(_pidfile(home)).st_mode)


# ---------------------------------------------------------------------------
# Cell 3
# ---------------------------------------------------------------------------


def test_symlink_pre_bind_is_refused_and_sentinel_untouched(home):
    """Mutation: drop the pre-bind check (the O_NOFOLLOW write still refuses,
    so this cell alone stays green; see the mutation table)."""
    sentinel = _sentinel(home)
    os.symlink(sentinel, _pidfile(home))
    res = _run_child(home, "plain")
    assert res["exc"] == "OwnerPidfileNotRegular" and res["is_named"], res
    assert not res["socket_exists"]
    assert sentinel.read_bytes() == SENTINEL_BYTES
    assert "symlink" in res["text"]


def test_symlink_swapped_in_after_the_check_is_refused_by_o_nofollow(home):
    """Mutation: drop O_NOFOLLOW from the write -> the sentinel is truncated
    and rewritten with the pid; this cell reddens."""
    sentinel = _sentinel(home)
    res = _run_child(home, "inject_symlink")
    assert sentinel.read_bytes() == SENTINEL_BYTES, "the sentinel must not be written through"
    assert res["exc"] == "OwnerPidfileNotRegular" and res["is_named"], res
    assert not res["socket_exists"]
    assert os.path.islink(_pidfile(home)), "the planted symlink must be left alone"


# ---------------------------------------------------------------------------
# Cell 4
# ---------------------------------------------------------------------------


def test_directory_pre_bind_is_refused_and_binds_nothing(home):
    """Mutation: drop the pre-bind check -> a directory pidfile is refused only
    at write time (after the bind); covered by the swap variant below."""
    _pidfile(home).mkdir()
    res = _run_child(home, "plain")
    assert res["exc"] == "OwnerPidfileNotRegular" and res["is_named"], res
    assert not res["socket_exists"]
    assert "directory" in res["text"]
    assert _pidfile(home).is_dir()


def test_directory_swapped_in_after_the_check_is_refused_at_write(home):
    """Mutation: map EISDIR to nothing (let it propagate) -> IsADirectoryError."""
    res = _run_child(home, "inject_dir")
    assert res["exc"] == "OwnerPidfileNotRegular" and res["is_named"], res
    assert not res["socket_exists"] and res["conns_closed"]
    assert _pidfile(home).is_dir()


# ---------------------------------------------------------------------------
# Cell 5
# ---------------------------------------------------------------------------


def test_fifo_planted_while_running_stop_neither_hangs_nor_unlinks_it(home):
    """Mutations: cleanup read back to a plain open -> stop hangs (timeout);
    cleanup treats a non-regular pidfile as still_ours -> the FIFO is unlinked."""
    env = dict(os.environ, ENGRAM_HOME=str(home), PYTHONDONTWRITEBYTECODE="1")
    code = f"import sys; sys.path.insert(0, {_SRC_ENGRAM!r}); import engramd_owner as eo; sys.exit(eo.main())"
    proc = subprocess.Popen(
        [sys.executable, "-c", code], env=env,
        stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True,
    )
    try:
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            if _pidfile(home).exists() and os.path.lexists(_socket(home)):
                break
            assert proc.poll() is None, f"owner exited early: {proc.stderr.read()!r}"
            time.sleep(0.05)
        else:
            pytest.fail("owner never came up")
        _pidfile(home).unlink()
        os.mkfifo(_pidfile(home))
        proc.send_signal(signal.SIGTERM)
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            pytest.fail("stop() HUNG on a FIFO pidfile (killed)")
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()
    assert proc.returncode == 0
    assert os.path.lexists(_pidfile(home)) and stat.S_ISFIFO(os.lstat(_pidfile(home)).st_mode), (
        "the FIFO is not ours to unlink"
    )
    assert not os.path.lexists(_socket(home)), "the socket is ours and must be removed"


# ---------------------------------------------------------------------------
# Cell 6 / 7 -- controls (behaviour unchanged)
# ---------------------------------------------------------------------------


def _start(home: Path) -> "eo.EngramdOwner":
    owner = eo.EngramdOwner(home, request_deadline=0.6)
    owner.start(install_signal_handlers=False)
    return owner


@pytest.mark.parametrize("content", ["999999999", "not a pid at all", ""])
def test_stale_regular_pidfile_is_overwritten_and_start_succeeds(home, content):
    """Control: the happy path with a stale/garbage regular pidfile is unchanged."""
    _pidfile(home).write_text(content)
    owner = _start(home)
    try:
        assert _pidfile(home).read_text() == str(os.getpid())
        assert stat.S_ISREG(os.lstat(_pidfile(home)).st_mode)
    finally:
        owner.stop()


def test_no_pidfile_is_created_regular_with_the_pid_and_write_text_mode(home):
    """Control: fresh start; the mode equals what Path.write_text yields under
    this process's umask."""
    ref = home / "reference.txt"
    ref.write_text("x")
    expected_mode = stat.S_IMODE(os.lstat(ref).st_mode)
    owner = _start(home)
    try:
        st = os.lstat(_pidfile(home))
        assert stat.S_ISREG(st.st_mode)
        assert stat.S_IMODE(st.st_mode) == expected_mode
        assert _pidfile(home).read_text() == str(os.getpid())
    finally:
        owner.stop()


@pytest.mark.skipif(os.getuid() == 0, reason="root ignores file modes: no EACCES to provoke")
def test_unwritable_pidfile_is_the_named_refusal_not_a_bare_permission_error(home):
    """#4068 point 4: replaces the old "EACCES propagates as PermissionError"
    cell, which pinned the traceback-and-respawn shape #3709 names. Pins the
    end-to-end refusal through start(): an unwritable pidfile surfaces as the
    named OwnerPidfileNotWritable, never a bare PermissionError. start()'s own
    preflight (check 4) refuses first here, so this cell does NOT pin the
    `_write_pidfile` PermissionError mapping; the race cell
    `test_cell3c_race_pidfile_unwritable_after_the_preflight_still_exits_4`
    in test_engramd_owner_pidfile_identity_4068.py does."""
    _pidfile(home).write_text("1")
    os.chmod(_pidfile(home), 0o444)
    owner = eo.EngramdOwner(home, request_deadline=0.6)
    try:
        with pytest.raises(eo.OwnerPidfileNotWritable) as ei:
            owner.start(install_signal_handlers=False)
        assert str(_pidfile(home)) in str(ei.value)
    finally:
        os.chmod(_pidfile(home), 0o644)
        owner.stop()


def test_regular_pidfile_naming_this_pid_is_removed_on_stop(home):
    """Control: ordinary cleanup still removes our own regular pidfile."""
    owner = _start(home)
    assert _pidfile(home).exists()
    owner.stop()
    assert not os.path.lexists(_pidfile(home))
    assert not os.path.lexists(_socket(home))


# ---------------------------------------------------------------------------
# Round 2 -- a socket-type file at the pidfile path (open() fails with ENXIO on
# Linux, EOPNOTSUPP on macOS: neither is a FIFO/symlink/directory errno)
# ---------------------------------------------------------------------------


def _plant_socket_file(path: Path) -> None:
    sk = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sk.bind(str(path))
    sk.close()


def test_socket_type_pidfile_before_start_is_refused_and_binds_nothing(home):
    """Mutation: drop the pre-bind check (write-time mapping then carries it)."""
    _plant_socket_file(_pidfile(home))
    res = _run_child(home, "plain")
    assert res["exc"] == "OwnerPidfileNotRegular" and res["is_named"], res
    assert not res["socket_exists"]
    assert stat.S_ISSOCK(os.lstat(_pidfile(home)).st_mode)


def test_socket_type_pidfile_swapped_in_after_the_check_is_refused_at_write(home):
    """Mutation: drop EOPNOTSUPP (macOS) / ENXIO (Linux) from the write-time
    map -> the raw OSError leaks, with the socket and connections still held."""
    res = _run_child(home, "inject_sock")
    assert res["exc"] == "OwnerPidfileNotRegular" and res["is_named"], res
    assert not res["socket_exists"], "the owner's own socket must be removed"
    assert res["conns_closed"], "both store connections must be closed"
    assert stat.S_ISSOCK(os.lstat(_pidfile(home)).st_mode)


def test_socket_type_pidfile_planted_while_running_is_not_unlinked_by_stop(home):
    """Mutation: drop the lstat guard before the cleanup unlink -> stop()
    unlinks the socket-type file (it is not ours)."""
    env = dict(os.environ, ENGRAM_HOME=str(home), PYTHONDONTWRITEBYTECODE="1")
    code = f"import sys; sys.path.insert(0, {_SRC_ENGRAM!r}); import engramd_owner as eo; sys.exit(eo.main())"
    proc = subprocess.Popen(
        [sys.executable, "-c", code], env=env,
        stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True,
    )
    try:
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            if _pidfile(home).exists() and os.path.lexists(_socket(home)):
                break
            assert proc.poll() is None, f"owner exited early: {proc.stderr.read()!r}"
            time.sleep(0.05)
        else:
            pytest.fail("owner never came up")
        _pidfile(home).unlink()
        _plant_socket_file(_pidfile(home))
        proc.send_signal(signal.SIGTERM)
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            pytest.fail("stop() HUNG on a socket-type pidfile (killed)")
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()
    assert proc.returncode == 0
    assert os.path.lexists(_pidfile(home)) and stat.S_ISSOCK(os.lstat(_pidfile(home)).st_mode), (
        "the socket-type file is not ours to unlink"
    )
    assert not os.path.lexists(_socket(home))


# ---------------------------------------------------------------------------
# Round 4 -- hard links: O_NOFOLLOW guards symlinks only, and O_TRUNC inside
# open(2) would destroy a hard-linked file before any fd check could run
# ---------------------------------------------------------------------------


def test_hardlink_before_start_is_refused_and_sentinel_untouched(home):
    """Mutation: drop the pre-bind nlink check (the write-time fd check still
    refuses, so this cell alone stays green; see the mutation table)."""
    sentinel = _sentinel(home)
    os.link(sentinel, _pidfile(home))
    res = _run_child(home, "plain")
    assert res["exc"] == "OwnerPidfileNotRegular" and res["is_named"], res
    assert not res["socket_exists"]
    assert "hard links" in res["text"] and str(_pidfile(home)) in res["text"]
    assert sentinel.read_bytes() == SENTINEL_BYTES
    assert os.lstat(_pidfile(home)).st_nlink == 2, "the planted link must be left alone"


def test_hardlink_swapped_in_after_the_check_is_refused_at_the_fd_check(home):
    """Mutations: put O_TRUNC back into the open flags -> the sentinel is
    truncated by open(2) itself; drop the st_nlink == 1 fd check -> the
    sentinel is truncated and rewritten with the pid."""
    sentinel = _sentinel(home)
    res = _run_child(home, "inject_hardlink")
    assert sentinel.read_bytes() == SENTINEL_BYTES, "the sentinel must not be truncated or written"
    assert res["exc"] == "OwnerPidfileNotRegular" and res["is_named"], res
    assert "hard links" in res["text"]
    assert not res["socket_exists"], "the owner's own socket must be removed"
    assert res["conns_closed"], "both store connections must be closed"


def test_cleanup_after_a_hardlink_refusal_leaves_the_path_in_place(home):
    """Mutation: drop the cleanup nlink guard -> _cleanup() (run on the
    refusal) unlinks the planted link."""
    sentinel = _sentinel(home)
    _run_child(home, "inject_hardlink")
    assert os.path.lexists(_pidfile(home)), "the planted link is not ours to unlink"
    assert os.lstat(_pidfile(home)).st_nlink == 2
    assert sentinel.read_bytes() == SENTINEL_BYTES


def test_stale_longer_pidfile_is_overwritten_without_a_leftover_tail(home):
    """Mutation: drop the ftruncate -> the old tail survives the shorter pid."""
    _pidfile(home).write_text("9" * 200)
    owner = _start(home)
    try:
        assert _pidfile(home).read_text() == str(os.getpid())
        assert os.lstat(_pidfile(home)).st_nlink == 1
    finally:
        owner.stop()


def test_fifo_with_a_reader_opens_for_write_and_only_the_fstat_refuses(home):
    """With a reader attached, the write-open of a FIFO SUCCEEDS (no ENXIO), so
    only the post-open fstat S_ISREG check can refuse. The reader is held by
    the child itself for the duration. Mutation: drop the S_ISREG fstat check
    -> the pid is written into the FIFO and the start succeeds."""
    res = _run_child(home, "inject_fifo_reader")
    assert res["exc"] == "OwnerPidfileNotRegular" and res["is_named"], res
    assert not res["socket_exists"] and res["conns_closed"]
