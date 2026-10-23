"""#4068 point 4 -- the owner's pidfile cleanup unlinks only on confirmed
identity (content == this pid), and an own pidfile the owner cannot write is
a named refusal (exit 4), in preflight and in start alike.

Cell numbers follow the design ruling: 1 garbage, 2 empty (the mid-write
window), 3 unreadable own pidfile, 3b preflight, 3c start, 4 control, 5 ENOENT,
6 start after a left-behind garbage pidfile. Every cell's docstring names the
mutation that must redden it.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
_SRC_ENGRAM = str(REPO_ROOT / "src" / "engram")
if _SRC_ENGRAM not in sys.path:
    sys.path.insert(0, _SRC_ENGRAM)

from _owner_platform import requires_supported_owner  # noqa: E402
from _owner_store import seed_current_store, stamp_current_schema  # noqa: E402
import engram_core  # noqa: E402
import engramd_owner as eo  # noqa: E402

pytestmark = requires_supported_owner

CHILD_TIMEOUT = 20.0
_NOT_ROOT = pytest.mark.skipif(os.getuid() == 0, reason="root ignores file modes")


@pytest.fixture
def home():
    d = tempfile.mkdtemp(prefix="p68-")  # short: AF_UNIX path limits
    seed_current_store(d)
    try:
        yield Path(d)
    finally:
        shutil.rmtree(d, ignore_errors=True)


def _pidfile(home: Path) -> Path:
    return home / eo.DEFAULT_PID_NAME


def _restore_mode(home: Path) -> None:
    try:
        os.chmod(_pidfile(home), 0o644)
    except FileNotFoundError:
        pass  # an implementation that wrongly unlinked it fails its own assert


def _cleanup_owner(home: Path, content, caplog):
    """Write `content` (None = no file), run `_cleanup()` on a never-started
    owner, return the owner logger's warnings."""
    if content is not None:
        _pidfile(home).write_text(content)
    owner = eo.EngramdOwner(home, request_deadline=0.6)
    with caplog.at_level(logging.WARNING, logger="engramd_owner"):
        owner._cleanup()
    return [r.getMessage() for r in caplog.records if r.name == "engramd_owner"]


def test_cell1_garbage_content_is_not_unlinked_and_warns_once(home, caplog):
    """Mutation: restore the unparseable-content fallback (`still_ours = True`)."""
    warns = _cleanup_owner(home, "abc", caplog)
    assert _pidfile(home).read_text() == "abc"
    assert len(warns) == 1 and str(_pidfile(home)) in warns[0], warns


def test_cell2_empty_content_the_mid_write_window_is_not_unlinked(home, caplog):
    """Mutation: same as cell 1."""
    warns = _cleanup_owner(home, "", caplog)
    assert os.path.lexists(_pidfile(home))
    assert len(warns) == 1 and str(_pidfile(home)) in warns[0], warns


@_NOT_ROOT
def test_cell3_unreadable_own_pidfile_is_not_unlinked(home, caplog):
    """Mutation: restore the open-failure fallback (`still_ours = True`)."""
    _pidfile(home).write_text(str(os.getpid()))
    os.chmod(_pidfile(home), 0)
    try:
        warns = _cleanup_owner(home, None, caplog)
        assert os.path.lexists(_pidfile(home))
        assert len(warns) == 1 and str(_pidfile(home)) in warns[0], warns
    finally:
        _restore_mode(home)


def test_cell4_own_pid_is_unlinked_control(home, caplog):
    """Control. The 'any parseable int is ours' mutant leaves this green; the
    next cell is its discriminator."""
    warns = _cleanup_owner(home, str(os.getpid()), caplog)
    assert not os.path.lexists(_pidfile(home))
    assert warns == []


def test_cell4b_another_live_pid_is_not_unlinked(home, caplog):
    """The delayed-signal case: a newer instance's pid (the parent's) is in the
    file. Mutation: identity = any parseable int."""
    _cleanup_owner(home, str(os.getppid()), caplog)
    assert _pidfile(home).read_text() == str(os.getppid())


def test_cell5_absent_pidfile_is_silent(home, caplog):
    assert _cleanup_owner(home, None, caplog) == []


def test_cell6_start_after_a_left_behind_garbage_pidfile_holds_the_new_pid(home):
    _pidfile(home).write_text("abc")
    owner = eo.EngramdOwner(home, request_deadline=0.6)
    owner.start(install_signal_handlers=False)
    try:
        assert _pidfile(home).read_text() == str(os.getpid())
    finally:
        owner.stop()


def _run_main(home: Path, args, pre="") -> subprocess.CompletedProcess:
    env = dict(os.environ, ENGRAM_HOME=str(home), PYTHONDONTWRITEBYTECODE="1")
    code = (f"import sys; sys.path.insert(0, {_SRC_ENGRAM!r}); import engramd_owner as eo; {pre}"
            "sys.exit(eo.main())")
    proc = subprocess.Popen([sys.executable, "-c", code, *args], env=env,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        out, err = proc.communicate(timeout=CHILD_TIMEOUT)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.communicate()
        pytest.fail("main() HUNG (killed)")
    return subprocess.CompletedProcess(proc.args, proc.returncode, out, err)


@pytest.fixture
def unwritable_home(home):
    _pidfile(home).write_text(str(os.getpid()))
    os.chmod(_pidfile(home), 0)
    try:
        yield home
    finally:
        _restore_mode(home)


@_NOT_ROOT
def test_cell3b_preflight_refuses_a_left_behind_unwritable_pidfile(unwritable_home):
    """Mutation: drop the writability test from preflight check 4. (Letting
    PermissionError escape _write_pidfile leaves this green: separate check.)"""
    home = unwritable_home
    res = _run_main(home, ["--preflight"])
    assert res.returncode == eo.EXIT_PATH_NOT_REGULAR == 4, res.stderr
    assert str(_pidfile(home)) in res.stderr and "Traceback" not in res.stderr
    assert os.lstat(_pidfile(home)).st_mode & 0o777 == 0  # pure: untouched


@_NOT_ROOT
def test_cell3c_start_through_main_exits_4_with_no_traceback(unwritable_home):
    """start() runs the same preflight first, so this cell alone does not pin
    _write_pidfile's mapping; the race cell below does."""
    home = unwritable_home
    res = _run_main(home, [])
    assert res.returncode == 4, (res.returncode, res.stderr)
    assert str(_pidfile(home)) in res.stderr and "Traceback" not in res.stderr


@_NOT_ROOT
def test_cell3c_race_pidfile_unwritable_after_the_preflight_still_exits_4(unwritable_home):
    """The preflight passes (disabled here) and the write meets the mode-0000
    file. Mutation: let PermissionError escape _write_pidfile -> traceback,
    exit 1, while cell 3b stays green."""
    home = unwritable_home
    res = _run_main(home, [], pre="eo.EngramdOwner.check_start_refusals = lambda self: None; ")
    assert res.returncode == 4, (res.returncode, res.stderr)
    assert str(_pidfile(home)) in res.stderr and "Traceback" not in res.stderr


@pytest.fixture
def readonly_home(home):
    """No pidfile, and the home directory itself is mode 0500: creating the
    pidfile would meet EACCES on the directory, not on a file."""
    os.chmod(home, 0o500)
    try:
        yield home
    finally:
        os.chmod(home, 0o700)


def _assert_directory_wording(text: str, home: Path) -> None:
    assert "exists but" not in text, text
    assert "directory" in text and str(home) in text, text
    assert "writable by the user that runs the owner" in text, text


@_NOT_ROOT
def test_cell3e_absent_pidfile_in_unwritable_dir_write_maps_to_the_directory_refusal(readonly_home):
    """The write meets EACCES on the directory (no preflight on this path): the
    same named refusal, directory wording. Mutation: use the exists-case text
    for both -> the wording assert reddens."""
    home = readonly_home
    owner = eo.EngramdOwner(home, request_deadline=0.6)
    with pytest.raises(eo.OwnerPidfileNotWritable) as ei:
        owner._write_pidfile()
    _assert_directory_wording(str(ei.value), home)


def _layout(kind: str) -> Path:
    """A fresh home for one row of the parity matrix. Read-only rows are chmod'ed
    0500 LAST; the caller restores 0700 and removes the tree."""
    d = Path(tempfile.mkdtemp(prefix="p68m-"))
    if kind == "no_store_ro":
        os.chmod(d, 0o500)
        return d
    seed_current_store(d)
    if kind == "store_behind_no_lockdir_ro":
        # Behind schema (user_version below latest), no lock directory, home read-only:
        # start() takes the lock directory first, so BOTH paths must name the lock dir.
        stamp_current_schema(d / "knowledge.db", engram_core.LATEST_SCHEMA_VERSION - 1)
        os.chmod(d, 0o500)
    elif kind == "store_lockdir_run_ro" or kind == "store_no_lockdir_ro":
        if kind == "store_lockdir_run_ro":
            first = eo.EngramdOwner(d, request_deadline=0.6)
            first.start(install_signal_handlers=False)  # creates .maint.lockdir and run/
            first.stop()  # releases them and removes the pidfile
            assert (d / "run").is_dir() and any(p.name.endswith(".maint.lockdir") for p in d.iterdir())
            assert not (d / eo.DEFAULT_PID_NAME).exists()
        os.chmod(d, 0o500)
    elif kind == "unwritable_pidfile":
        (d / eo.DEFAULT_PID_NAME).write_text(str(os.getpid()))
        os.chmod(d / eo.DEFAULT_PID_NAME, 0)
    else:
        assert kind == "writable_fresh"
    return d


def _outcome(call) -> str:
    try:
        call()
    except Exception as exc:  # the CLASS is the contract
        return type(exc).__name__
    return "passes"


_PARITY_ROWS = [
    # (layout, what both paths must do)
    ("no_store_ro", "OwnerStoreBehindSchema"),            # the schema gate keeps its name
    ("store_lockdir_run_ro", "OwnerPidfileNotWritable"),  # the layout the reviewer drove
    ("store_no_lockdir_ro", "OwnerStoreLockDirUnusable"), # the lock directory cannot be made
    # store behind schema AND no lock dir: the lock-dir refusal comes before the schema gate
    ("store_behind_no_lockdir_ro", "OwnerStoreLockDirUnusable"),
    ("unwritable_pidfile", "OwnerPidfileNotWritable"),
    ("writable_fresh", "passes"),
]


@_NOT_ROOT
@pytest.mark.parametrize("kind,expected", _PARITY_ROWS, ids=[r[0] for r in _PARITY_ROWS])
def test_cell3f_preflight_and_start_agree_on_every_layout(kind, expected):
    """Parity matrix: for each layout, check_start_refusals() raises the same
    exception class start() raises, or both pass. Each side gets its own fresh
    copy of the layout. This is what keeps the preflight honest: launchd restarts
    every non-zero exit (including this owner's refusals) every 60 s, so a preflight
    that passes where start() then exits 4 loops forever. Mutations: delete the
    absent-pidfile branch of check_start_refusals -> the store_lockdir_run_ro row
    reddens; make that branch refuse unconditionally -> the no_store_ro row reddens."""
    results = {}
    for side in ("preflight", "start"):
        d = _layout(kind)
        try:
            owner = eo.EngramdOwner(d, request_deadline=0.6)
            if side == "preflight":
                results[side] = _outcome(owner.check_start_refusals)
            else:
                results[side] = _outcome(lambda: owner.start(install_signal_handlers=False))
                assert not owner.socket_path.exists() or expected == "passes"
                if expected == "passes":
                    owner.stop()
        finally:
            os.chmod(d, 0o700)
            if (d / eo.DEFAULT_PID_NAME).exists():
                os.chmod(d / eo.DEFAULT_PID_NAME, 0o644)
            shutil.rmtree(d, ignore_errors=True)
    assert results == {"preflight": expected, "start": expected}, results


@_NOT_ROOT
def test_cell3f_second_layout_message_names_the_directory():
    """The store+lockdir+run read-only layout refuses with the DIRECTORY wording,
    and nothing is bound or written."""
    d = _layout("store_lockdir_run_ro")
    try:
        owner = eo.EngramdOwner(d, request_deadline=0.6)
        with pytest.raises(eo.OwnerPidfileNotWritable) as ei:
            owner.check_start_refusals()
        _assert_directory_wording(str(ei.value), d)
        assert not owner.socket_path.exists() and not (d / eo.DEFAULT_PID_NAME).exists()
    finally:
        os.chmod(d, 0o700)
        shutil.rmtree(d, ignore_errors=True)


def test_cell3g_absent_wording_says_cannot_be_checked_only_when_lstat_failed_otherwise(home, monkeypatch):
    """ENOENT keeps the exact "does not exist" text; any other lstat failure (a
    directory that cannot be searched) says "does not exist or cannot be checked"."""
    owner = eo.EngramdOwner(home, request_deadline=0.6)
    plain = owner._pidfile_not_writable_text()
    assert "does not exist and its directory" in plain and "cannot be checked" not in plain, plain

    def denied(path, *a, **k):
        raise PermissionError(13, "Permission denied", str(path))

    monkeypatch.setattr(eo.os, "lstat", denied)
    unchecked = owner._pidfile_not_writable_text()
    assert "does not exist or cannot be checked and its directory" in unchecked, unchecked
