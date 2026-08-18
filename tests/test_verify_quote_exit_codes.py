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
    2 — could not check at all (bad usage, file does not exist).
"""

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
