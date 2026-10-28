"""engram-alpha#4009 -- `--real-store` classification is interpreter-independent.

On Python 3.12 `Path.is_file()` RAISES PermissionError for a path whose parent directory cannot be searched;
on 3.14 it returns False.  A precheck that reads "did `is_file()` raise?" therefore answers differently on the
two interpreters: 3.12 refused with the classifier's EACCES line, 3.14 fell through to the "is not a file"
line.  `tools/engramd_a6_latency._classify_real_store` classifies with `os.stat` (which follows symlinks, as
`is_file()` did) and consults the EXCEPTION TYPE only, so the answer is identical on every interpreter.

These cells unit-test that helper in-process: `tests/test_store_path_precheck_3962c.py` drives the CLI as a
subprocess, so it measures whatever interpreter runs it and CANNOT pin the 3.14 answer on a 3.12 seat.
`Path.is_file` is pinned to the 3.14 answer (False) here, so a classification that still consults it fails
HERE on 3.12 too.

The contract (#4009):

  os.stat raises an errno `is_file()` swallowed
    (ENOENT, ENOTDIR, EBADF, ELOOP -- missing,
    non-directory parent, dangling link, loop)    the existing "is not a file ... Name one with --real-store PATH" line
  os.stat raises any other OSError (EACCES from
    an unsearchable parent, ENAMETOOLONG, EIO)    classifier line: `engramd_a6_latency: error -- <msg naming it>`
  os.stat ok, not S_ISREG (FIFO, directory, ...)  the same "is not a file" line
  os.stat ok, S_ISREG                             None -- the run proceeds (no refusal)

The errno split is `is_file()`'s own boundary, not "any OSError": it swallowed exactly four errnos and raised
the rest, so matching it keeps the pre-#4009 message for EVERY errno and only changes the 3.14 answer.
"""

from __future__ import annotations

import os
import pathlib
import sys
from pathlib import Path
from unittest import mock

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.append(str(REPO_ROOT))

from tools import engramd_a6_latency as a6  # noqa: E402  (REPO_ROOT must be on sys.path first)

PREFIX = "engramd_a6_latency: error -- "
NOT_A_FILE = "is not a file;"
NAME_ONE = "Name one with --real-store PATH"


def classify_with_is_file_false(path: Path):
    """The helper's answer with `Path.is_file` pinned to the Python 3.14 answer (False)."""
    with mock.patch.object(Path, "is_file", return_value=False):
        return a6._classify_real_store(path)


def make_shape(tmp_path: Path, shape: str) -> Path:
    if shape == "notdir":  # a parent COMPONENT is a regular file -> os.stat raises ENOTDIR
        parent = tmp_path / "afile"
        parent.write_text("not a directory", encoding="utf-8")
        return parent / "knowledge.db"
    slot = tmp_path / "knowledge.db"
    if shape == "missing":
        return slot
    if shape == "dangling":
        os.symlink("no-such-target.db", slot)
    elif shape == "loop":
        os.symlink(slot.name, slot)
    elif shape == "dir":
        slot.mkdir()
    elif shape == "fifo":
        os.mkfifo(slot)
    else:  # pragma: no cover
        raise AssertionError(shape)
    return slot


def test_unsearchable_parent_still_names_eacces(tmp_path):
    """The 3.14 failure, pinned: `is_file()` answers False, yet the parent cannot be searched.

    The pre-fix logic read that False as "not a file" and printed that line; the contract requires the
    classifier message, so this cell goes RED against the old logic on ANY interpreter."""
    if os.geteuid() == 0:
        pytest.skip("root bypasses the directory permission")
    locked = tmp_path / "locked"
    locked.mkdir()
    slot = locked / "knowledge.db"
    slot.write_text("a store behind an unsearchable directory", encoding="utf-8")
    locked.chmod(0)
    try:
        line = classify_with_is_file_false(slot)
    finally:
        locked.chmod(0o755)
    assert line is not None
    assert line.startswith(PREFIX), line
    assert "\n" not in line, line
    assert str(slot) in line, line
    assert "EACCES" in line and "Permission denied" in line, line
    assert NOT_A_FILE not in line, line


def test_regular_file_is_accepted_even_when_is_file_answers_false(tmp_path):
    """`is_file()` must not be consulted at all: a regular file is a usable source whatever it answers."""
    slot = tmp_path / "knowledge.db"
    slot.write_text("x", encoding="utf-8")
    assert classify_with_is_file_false(slot) is None


def test_a_too_long_path_keeps_the_classifier_line(tmp_path):
    """ENAMETOOLONG is NOT one of the errnos `is_file()` swallowed -- it raised -- so the pre-#4009 message
    was the classifier line, on every interpreter. It must stay that line: the boundary is that errno set,
    not "any OSError". Reviewer-found on #4131; a plain `except OSError -> not_a_file` would regress it."""
    slot = tmp_path / ("x" * 5000)
    line = classify_with_is_file_false(slot)
    assert line is not None
    assert line.startswith(PREFIX), line
    assert "ENAMETOOLONG" in line, line
    assert NOT_A_FILE not in line, line
    assert NAME_ONE not in line, line


def test_the_errno_boundary_is_pathlibs_own():
    """The set is only correct while it equals what `is_file()` actually swallows; pin it to pathlib rather
    than to a copied comment, so a CPython change reddens this instead of silently altering the message."""
    ignored = getattr(pathlib, "_IGNORED_ERRNOS", None)
    if ignored is None:
        pytest.skip("this pathlib no longer exposes _IGNORED_ERRNOS")
    assert set(a6._IS_FILE_IGNORED_ERRNOS) == {int(e) for e in ignored}


@pytest.mark.parametrize("shape", ["missing", "notdir", "dangling", "loop", "dir", "fifo"])
def test_non_regular_shapes_keep_the_not_a_file_line(tmp_path, shape):
    """Every other class keeps the existing line -- the classifier line is reserved for EACCES."""
    slot = make_shape(tmp_path, shape)
    line = classify_with_is_file_false(slot)
    assert line is not None
    assert line.startswith(PREFIX), line
    assert "\n" not in line, line
    assert str(slot) in line, line
    assert NOT_A_FILE in line and NAME_ONE in line, line
    assert "EACCES" not in line, line
