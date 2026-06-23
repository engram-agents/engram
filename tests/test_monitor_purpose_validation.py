"""Tests for --purpose key validation in monitor shell scripts (#1307).

Verifies:
  1. collab-letter-monitor rejects purpose key with metacharacter (.) → exit 1
  2. collab-letter-monitor rejects purpose key with spaces → exit 1
  3. collab-letter-monitor accepts alphanumeric+hyphen+underscore key → not a validation exit
  4. forum-mention-monitor rejects purpose key with metacharacter (.) → exit 1
  5. forum-mention-monitor rejects purpose key with spaces → exit 1
  6. forum-mention-monitor accepts alphanumeric+hyphen+underscore key → not a validation exit

The "accepts valid key" tests confirm the exit is NOT about --purpose validation
(the script proceeds past that gate before failing for an unrelated reason like
a missing config.json or no forum server — a different error, a different message).
"""

import os
import subprocess
import sys
import tempfile
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
_COLLAB_MONITOR = _REPO_ROOT / "src" / "engram" / "tools" / "collab-letter-monitor.sh"
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
# collab-letter-monitor.sh
# ---------------------------------------------------------------------------

def test_collab_monitor_rejects_metachar_in_purpose():
    """collab-letter-monitor: '.' in purpose key → exit 1, validation message."""
    rc, stderr = _run(_COLLAB_MONITOR, ["--purpose", "bad.key"])
    assert rc == 1, f"Expected exit 1, got {rc}. stderr: {stderr!r}"
    assert "[a-zA-Z0-9_-]" in stderr or "alphanumeric" in stderr or "must contain" in stderr, (
        f"Expected validation error in stderr. Got: {stderr!r}"
    )


def test_collab_monitor_rejects_space_in_purpose():
    """collab-letter-monitor: space in purpose key → exit 1, validation message."""
    rc, stderr = _run(_COLLAB_MONITOR, ["--purpose", "key with spaces"])
    assert rc == 1, f"Expected exit 1, got {rc}. stderr: {stderr!r}"
    assert "[a-zA-Z0-9_-]" in stderr or "alphanumeric" in stderr or "must contain" in stderr, (
        f"Expected validation error in stderr. Got: {stderr!r}"
    )


def test_collab_monitor_accepts_valid_purpose_key(tmp_path):
    """collab-letter-monitor: alphanumeric+hyphen+underscore purpose key passes validation.

    The script fails for a different reason (missing agent_name in config.json),
    but NOT with the purpose-validation message — confirming the key was accepted.
    """
    config = tmp_path / "config.json"
    config.write_text("{}")  # missing agent_name → triggers a *different* exit
    rc, stderr = _run(
        _COLLAB_MONITOR,
        ["--purpose", "valid-key_123"],
        env={"HOME": str(tmp_path)},
    )
    assert rc == 1, f"Expected exit 1 (agent_name absent), got {rc}. stderr: {stderr!r}"
    # Must NOT be the purpose-validation message.
    assert "[a-zA-Z0-9_-]" not in stderr and "must contain" not in stderr, (
        f"Got purpose-validation error for a valid key. stderr: {stderr!r}"
    )
    # Should be the agent_name-missing error instead.
    assert "agent_name" in stderr.lower() or "refusing to arm" in stderr.lower(), (
        f"Expected agent_name error, got: {stderr!r}"
    )


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
