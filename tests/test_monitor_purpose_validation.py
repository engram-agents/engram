"""Tests for --purpose key validation in monitor shell scripts (#1307).

Verifies:
  1. forum-mention-monitor rejects purpose key with metacharacter (.) → exit 1
  2. forum-mention-monitor rejects purpose key with spaces → exit 1
  3. forum-mention-monitor accepts alphanumeric+hyphen+underscore key → not a validation exit

The "accepts valid key" test confirms the exit is NOT about --purpose validation
(the script proceeds past that gate before failing for an unrelated reason like
a missing config.json or no forum server — a different error, a different message).

(The collab-letter-monitor.sh coverage that used to live here was removed in
#2857 step 2, alongside the script itself — dead code, retired by the
2026-06-28 UCS cutover to forum DMs.)
"""

import os
import subprocess
import sys
import tempfile
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
_FORUM_MONITOR = _REPO_ROOT / "src" / "engram" / "tools" / "forum-mention-monitor.sh"


def _run(script: Path, args: list[str], env: dict | None = None, timeout: float = 5.0):
    """Run a monitor script with given args; return (returncode, stderr_text).

    Returns (-1, stderr_so_far) on timeout — used by valid-key tests where the
    monitor enters its retry loop instead of exiting.
    """
    merged_env = os.environ.copy()
    if env:
        merged_env.update(env)
    # Prevent the reap step from touching any live monitors during tests.
    merged_env["COLLAB_MONITOR_NO_REAP"] = "1"
    try:
        proc = subprocess.run(
            ["bash", str(script)] + args,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=merged_env,
        )
        return proc.returncode, proc.stderr
    except subprocess.TimeoutExpired as exc:
        stderr = (exc.stderr or b"").decode(errors="replace") if isinstance(exc.stderr, bytes) else (exc.stderr or "")
        return -1, stderr


# ---------------------------------------------------------------------------
# forum-mention-monitor.sh
# ---------------------------------------------------------------------------

def test_forum_monitor_rejects_metachar_in_purpose():
    """forum-mention-monitor: '.' in purpose key → exit 1, validation message."""
    rc, stderr = _run(_FORUM_MONITOR, ["--purpose", "bad.key"])
    assert rc == 1, f"Expected exit 1, got {rc}. stderr: {stderr!r}"
    assert "[a-zA-Z0-9_-]" in stderr or "alphanumeric" in stderr or "must contain" in stderr, (
        f"Expected validation error in stderr. Got: {stderr!r}"
    )


def test_forum_monitor_rejects_space_in_purpose():
    """forum-mention-monitor: space in purpose key → exit 1, validation message."""
    rc, stderr = _run(_FORUM_MONITOR, ["--purpose", "key with spaces"])
    assert rc == 1, f"Expected exit 1, got {rc}. stderr: {stderr!r}"
    assert "[a-zA-Z0-9_-]" in stderr or "alphanumeric" in stderr or "must contain" in stderr, (
        f"Expected validation error in stderr. Got: {stderr!r}"
    )


def test_forum_monitor_accepts_valid_purpose_key(tmp_path):
    """forum-mention-monitor: alphanumeric+hyphen+underscore purpose key passes validation.

    The forum-monitor enters a curl-retry loop when the forum is unreachable,
    so it never exits cleanly — the test times out intentionally (rc == -1).
    A timeout proves the script passed validation and reached the monitoring loop.
    An immediate exit-1 with the validation message would be a regression.
    """
    rc, stderr = _run(
        _FORUM_MONITOR,
        ["--purpose", "valid-key_123"],
        timeout=2.0,
    )
    # rc == -1 (timeout) is the expected success: script is alive in the retry loop.
    # rc == 1 with a validation message would be a bug.
    assert "[a-zA-Z0-9_-]" not in stderr and "must contain" not in stderr, (
        f"Got purpose-validation error for a valid key. stderr: {stderr!r}"
    )
