"""Exit-code contract for tools/verify_quote.py (#1990).

The tool used to exit 0 on a quote MISS — the NOT-FOUND branch printed
diagnostics and fell off the end of main() with no sys.exit() call, so the
natural shell idiom `verify_quote.py f q && next_step` treated every miss
as a pass. This test pins the fixed contract directly against the real
entrypoint via subprocess, not against imported internals, so a regression
back to the buggy fall-through is caught.

Exit codes under test:
    0 — quote FOUND (verbatim substring match).
    1 — quote NOT FOUND (including an empty/unflushed file).
    2 — could not check at all (bad usage, file does not exist, path is not a
        regular file — a directory, FIFO or device).
"""

import os
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).parent.parent
VERIFY_QUOTE = REPO_ROOT / "src" / "engram" / "tools" / "verify_quote.py"


def _run(args):
    return subprocess.run(
        [sys.executable, str(VERIFY_QUOTE), *args],
        capture_output=True,
        text=True,
    )


@pytest.fixture()
def jsonl_fixture(tmp_path):
    """A temp JSONL fixture — never a real transcript, never ~/.engram."""
    fp = tmp_path / "session.jsonl"
    fp.write_text(
        '{"type": "user", "message": {"content": "the quick brown fox jumps"}}\n'
        '{"type": "assistant", "message": {"content": "acknowledged"}}\n'
        * 6,
        encoding="utf-8",
    )
    return fp


def test_hit_exits_zero(jsonl_fixture):
    result = _run([str(jsonl_fixture), "quick brown fox"])
    assert result.returncode == 0
    assert "FOUND at position" in result.stdout
    assert "NOT FOUND" not in result.stdout


def test_miss_exits_one(jsonl_fixture):
    result = _run([str(jsonl_fixture), "this phrase is not in the file"])
    assert result.returncode == 1
    assert "NOT FOUND" in result.stdout


def test_empty_file_exits_one(tmp_path):
    fp = tmp_path / "empty.jsonl"
    fp.write_text("", encoding="utf-8")
    result = _run([str(fp), "anything"])
    assert result.returncode == 1
    assert "FILE IS EMPTY" in result.stdout


def test_nonexistent_file_exits_two(tmp_path):
    missing = tmp_path / "does-not-exist.jsonl"
    result = _run([str(missing), "anything"])
    assert result.returncode == 2
    assert "FILE NOT FOUND" in result.stdout


def test_bad_usage_exits_two():
    result = _run(["only-one-arg"])
    assert result.returncode == 2
    assert "Usage:" in result.stdout


def test_and_idiom_short_circuits_on_miss(jsonl_fixture):
    """The natural shell idiom must actually gate on a miss."""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".sh", delete=False) as f:
        f.write(
            f'#!/bin/sh\n'
            f'"{sys.executable}" "{VERIFY_QUOTE}" "{jsonl_fixture}" "not present" '
            f'&& echo SHOULD_NOT_PRINT\n'
        )
        script = f.name
    try:
        result = subprocess.run(["/bin/sh", script], capture_output=True, text=True)
        assert "SHOULD_NOT_PRINT" not in result.stdout
    finally:
        Path(script).unlink()


# --- #3400: a non-regular file must be rejected before open() -------------
# Contract: a path that exists but is not a regular file (directory, FIFO,
# device) prints a line starting `Evidence must be a file: '<path>' is not a
# regular file` and exits 2 ("could not check at all", NOT 1 = "quote absent").
# No traceback, and open() is never called on it (a FIFO would hang forever).

_NOT_FILE_LEAD = "Evidence must be a file: '{p}' is not a regular file"


def _run_bounded(args, timeout=5):
    """Like _run, but a hang FAILS the test instead of hanging the suite."""
    try:
        return subprocess.run(
            [sys.executable, str(VERIFY_QUOTE), *args],
            capture_output=True,
            text=True,
            timeout=timeout,
            stdin=subprocess.DEVNULL,
        )
    except subprocess.TimeoutExpired:
        pytest.fail(f"verify_quote.py hung >{timeout}s on {args[0]!r} (open() on a non-file?)")


def _assert_rejected_as_non_file(result, path):
    assert result.returncode == 2, (result.returncode, result.stdout, result.stderr)
    assert result.stdout.startswith(_NOT_FILE_LEAD.format(p=path)), result.stdout
    assert "Traceback" not in result.stdout + result.stderr
    assert "IsADirectoryError" not in result.stdout + result.stderr


def test_directory_exits_two_not_a_regular_file(tmp_path):
    d = tmp_path / "adir"
    d.mkdir()
    _assert_rejected_as_non_file(_run_bounded([str(d), "anything"]), d)


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="os.mkfifo unavailable")
def test_fifo_exits_two_and_does_not_hang(tmp_path):
    fifo = tmp_path / "afifo"
    os.mkfifo(fifo)
    _assert_rejected_as_non_file(_run_bounded([str(fifo), "anything"]), fifo)


def test_device_exits_two_not_a_regular_file():
    dev = "/dev/null"
    if not os.path.exists(dev) or os.path.isfile(dev):
        pytest.skip("/dev/null is absent or a regular file on this platform")
    _assert_rejected_as_non_file(_run_bounded([dev, "anything"]), dev)


def test_symlink_to_directory_rejected(tmp_path):
    d = tmp_path / "adir"
    d.mkdir()
    link = tmp_path / "dirlink"
    link.symlink_to(d)
    _assert_rejected_as_non_file(_run_bounded([str(link), "anything"]), link)


def test_symlink_to_regular_file_accepted(jsonl_fixture, tmp_path):
    link = tmp_path / "filelink.jsonl"
    link.symlink_to(jsonl_fixture)
    result = _run_bounded([str(link), "quick brown fox"])
    assert result.returncode == 0
    assert "FOUND at position" in result.stdout


def test_dangling_symlink_is_file_not_found(tmp_path):
    link = tmp_path / "dangling"
    link.symlink_to(tmp_path / "nowhere")
    result = _run_bounded([str(link), "anything"])
    assert result.returncode == 2
    assert "FILE NOT FOUND" in result.stdout
