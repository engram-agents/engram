"""Contract tests for engram-alpha#3768 -- a FIFO at the pidfile path must not
hang `engram_pidfile.recorded_live_pid`.

Written BEFORE the fix (contract-first). A regression HANGS, so every call on a
non-regular path runs in a child process with a timeout: a failure is a red
cell, not a hung test run.

Contract:
 1. FIFO at the path: recorded_live_pid -> None, recorded_pid_is_alive -> False,
    each within 3 s.
 2. Symlink to a FIFO: same. Directory: None. Symlink to a regular pidfile
    naming a live pid: still that pid (no O_NOFOLLOW).
 3. Regular pidfile naming a live process: unchanged, with and without
    program_marker.
 4. The FIFO case logs a record containing "not a regular file" and the path;
    a missing and a garbage pidfile log no such record.
 5. No platform skip: mkfifo is POSIX.
 6. One real caller (engramd_client's call site is inside a daemon-bound path;
    the surface daemon's `_owner_alive`-style wrapper is exercised below via
    recorded_pid_is_alive, which is what all three callers delegate to).
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
_SRC_ENGRAM = str(REPO_ROOT / "src" / "engram")
if _SRC_ENGRAM not in sys.path:
    sys.path.insert(0, _SRC_ENGRAM)

import engram_pidfile as ep  # noqa: E402

_CHILD = (
    "import sys; sys.path.insert(0, {src!r}); import engram_pidfile as ep; "
    "p = sys.argv[1]; "
    "print(repr(ep.recorded_live_pid(p, None)), "
    "repr(ep.recorded_pid_is_alive(p, None)))"
)


def _run_child(path):
    """(live_pid_repr, alive_repr) from a child, or fail on timeout."""
    proc = subprocess.Popen(
        [sys.executable, "-c", _CHILD.format(src=_SRC_ENGRAM), str(path)],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    try:
        out, err = proc.communicate(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.communicate()
        pytest.fail(f"recorded_live_pid hung on {path} (child killed at 5 s)")
    assert proc.returncode == 0, err
    return tuple(out.split())


def _fifo(tmp_path, name="engramd.pid"):
    p = tmp_path / name
    os.mkfifo(p)
    return p


def test_fifo_reads_as_no_live_pid_without_hanging(tmp_path):
    assert _run_child(_fifo(tmp_path)) == ("None", "False")


def test_symlink_to_fifo_reads_as_no_live_pid(tmp_path):
    target = _fifo(tmp_path, "real.fifo")
    link = tmp_path / "engramd.pid"
    link.symlink_to(target)
    assert _run_child(link) == ("None", "False")


def test_directory_reads_as_no_live_pid(tmp_path):
    d = tmp_path / "engramd.pid"
    d.mkdir()
    assert ep.recorded_live_pid(d, None) is None


def test_symlink_to_regular_pidfile_still_follows(tmp_path):
    real = tmp_path / "real.pid"
    real.write_text(str(os.getpid()))
    link = tmp_path / "engramd.pid"
    link.symlink_to(real)
    assert ep.recorded_live_pid(link, None) == os.getpid()


@pytest.mark.parametrize("marker", [None, "pytest", "python"])
def test_regular_pidfile_live_pid_unchanged(tmp_path, marker):
    p = tmp_path / "engramd.pid"
    p.write_text(str(os.getpid()))
    assert ep.recorded_live_pid(p, marker) == os.getpid()
    assert ep.recorded_pid_is_alive(p, marker) is True


def test_fifo_warning_names_reason_and_path(tmp_path, caplog):
    p = _fifo(tmp_path)
    # Opening a FIFO O_NONBLOCK|O_RDONLY returns at once, so this is safe
    # in-process only after the fix; guard with the child first.
    _run_child(p)
    with caplog.at_level(logging.WARNING, logger="engram_pidfile"):
        assert ep.recorded_live_pid(p, None) is None
    msgs = [r.getMessage() for r in caplog.records]
    assert any("not a regular file" in m and str(p) in m for m in msgs), msgs


def test_directory_warning(tmp_path, caplog):
    d = tmp_path / "engramd.pid"
    d.mkdir()
    with caplog.at_level(logging.WARNING, logger="engram_pidfile"):
        ep.recorded_live_pid(d, None)
    assert any("not a regular file" in r.getMessage() for r in caplog.records)


def test_missing_pidfile_is_silent(tmp_path, caplog):
    with caplog.at_level(logging.WARNING, logger="engram_pidfile"):
        assert ep.recorded_live_pid(tmp_path / "nope.pid", None) is None
    assert not any("not a regular file" in r.getMessage() for r in caplog.records)


def test_garbage_pidfile_is_silent(tmp_path, caplog):
    p = tmp_path / "engramd.pid"
    p.write_text("not-a-number")
    with caplog.at_level(logging.WARNING, logger="engram_pidfile"):
        assert ep.recorded_live_pid(p, None) is None
    assert not any("not a regular file" in r.getMessage() for r in caplog.records)
