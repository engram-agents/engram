"""`engram_pidfile.classify_own_pidfile` -- the ONE identity rule both the
engramd owner's and the surface daemon's cleanup unlink on (#4068 point 4).

One cell per return value, plus the symlink (not followed) and the
non-blocking FIFO (run in a child under a kill timeout)."""

from __future__ import annotations

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

_NOT_ROOT = pytest.mark.skipif(os.getuid() == 0, reason="root ignores file modes")


def test_ours_only_when_content_is_this_pid(tmp_path):
    p = tmp_path / "x.pid"
    p.write_text(str(os.getpid()))
    assert ep.classify_own_pidfile(p)[0] == ep.PIDFILE_OURS


def test_other_pid_is_unknown_not_ours(tmp_path):
    """Mutation: ours = any parseable int -> reddens."""
    p = tmp_path / "x.pid"
    p.write_text(str(os.getppid()))
    status, reason = ep.classify_own_pidfile(p)
    assert status == ep.PIDFILE_UNKNOWN and str(os.getppid()) in reason


@pytest.mark.parametrize("content", ["abc", "", "   \n", "12x"])
def test_garbage_or_empty_is_unknown(tmp_path, content):
    p = tmp_path / "x.pid"
    p.write_text(content)
    assert ep.classify_own_pidfile(p)[0] == ep.PIDFILE_UNKNOWN


def test_absent(tmp_path):
    assert ep.classify_own_pidfile(tmp_path / "nope.pid")[0] == ep.PIDFILE_ABSENT


@_NOT_ROOT
def test_unreadable_is_unknown(tmp_path):
    p = tmp_path / "x.pid"
    p.write_text(str(os.getpid()))
    os.chmod(p, 0)
    try:
        assert ep.classify_own_pidfile(p)[0] == ep.PIDFILE_UNKNOWN
    finally:
        os.chmod(p, 0o644)


def test_hard_linked(tmp_path):
    p = tmp_path / "x.pid"
    p.write_text(str(os.getpid()))
    os.link(p, tmp_path / "other")
    status, reason = ep.classify_own_pidfile(p)
    assert status == ep.PIDFILE_HARD_LINKED and "2 hard links" in reason


def test_symlink_is_not_followed(tmp_path):
    target = tmp_path / "t"
    target.write_text(str(os.getpid()))
    p = tmp_path / "x.pid"
    os.symlink(target, p)
    status, reason = ep.classify_own_pidfile(p)
    assert status == ep.PIDFILE_NOT_REGULAR and "symlink" in reason


def test_directory_is_not_regular(tmp_path):
    assert ep.classify_own_pidfile(tmp_path)[0] == ep.PIDFILE_NOT_REGULAR


def test_fifo_is_not_regular_and_does_not_hang(tmp_path):
    p = tmp_path / "x.pid"
    os.mkfifo(p)
    code = (f"import sys; sys.path.insert(0, {_SRC_ENGRAM!r}); import engram_pidfile as ep; "
            f"print(*ep.classify_own_pidfile({str(p)!r}))")
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=15)
    assert out.stdout.split()[0] == ep.PIDFILE_NOT_REGULAR and "FIFO" in out.stdout, out
