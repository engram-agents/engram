"""Tests for tools/set-context-mode.py (issue #72 — agent-writes-marker)."""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "tools" / "set-context-mode.py"


def _run(args, env_overrides=None):
    """Run the script in a clean subprocess; return (returncode, stdout, stderr)."""
    env = os.environ.copy()
    # Strip session-id from inherited env so test cases that omit it actually do.
    env.pop("CLAUDE_CODE_SESSION_ID", None)
    env.pop("ENGRAM_HOME", None)
    if env_overrides:
        env.update(env_overrides)
    result = subprocess.run(
        [sys.executable, str(SCRIPT)] + args,
        capture_output=True,
        text=True,
        env=env,
    )
    return result.returncode, result.stdout, result.stderr


class TestSetContextMode(unittest.TestCase):

    def test_missing_arg_returns_exit_2(self):
        rc, _, err = _run([])
        self.assertEqual(rc, 2)
        self.assertIn("usage", err)

    def test_invalid_mode_returns_exit_2(self):
        rc, _, err = _run(["999k"])
        self.assertEqual(rc, 2)
        self.assertIn("mode must be one of", err)

    def test_no_session_id_silent_noop(self):
        """No CLAUDE_CODE_SESSION_ID env → exit 0 silently."""
        rc, out, err = _run(["1m"])
        self.assertEqual(rc, 0)
        self.assertEqual(out, "")
        self.assertEqual(err, "")

    def test_missing_marker_silent_noop(self):
        """Marker file doesn't exist → exit 0 silently."""
        with tempfile.TemporaryDirectory() as tmp:
            engram_home = Path(tmp) / "engram_home"
            (engram_home / "sessions").mkdir(parents=True)
            rc, out, err = _run(
                ["1m"],
                env_overrides={
                    "CLAUDE_CODE_SESSION_ID": "nonexistent-session",
                    "ENGRAM_HOME": str(engram_home),
                },
            )
            self.assertEqual(rc, 0)
            self.assertEqual(out, "")
            self.assertEqual(err, "")

    def test_writes_mode_to_empty_marker(self):
        """Marker exists without context_mode → writes it."""
        with tempfile.TemporaryDirectory() as tmp:
            engram_home = Path(tmp) / "engram_home"
            (engram_home / "sessions").mkdir(parents=True)
            sid = "aaaa1111-bbbb-cccc-dddd-eeeeffffaaaa"
            marker = engram_home / "sessions" / f"{sid}.json"
            marker.write_text(json.dumps({"session_id": sid, "source": "startup"}))

            rc, out, err = _run(
                ["1m"],
                env_overrides={
                    "CLAUDE_CODE_SESSION_ID": sid,
                    "ENGRAM_HOME": str(engram_home),
                },
            )
            self.assertEqual(rc, 0)
            self.assertIn("context_mode set to '1m'", out)

            with marker.open() as f:
                data = json.load(f)
            self.assertEqual(data["context_mode"], "1m")
            # Other fields preserved
            self.assertEqual(data["session_id"], sid)
            self.assertEqual(data["source"], "startup")

    def test_idempotent_when_already_populated(self):
        """Marker has context_mode already → silent no-op, doesn't overwrite."""
        with tempfile.TemporaryDirectory() as tmp:
            engram_home = Path(tmp) / "engram_home"
            (engram_home / "sessions").mkdir(parents=True)
            sid = "bbbb2222-cccc-dddd-eeee-ffffaaaabbbb"
            marker = engram_home / "sessions" / f"{sid}.json"
            marker.write_text(json.dumps({"session_id": sid, "context_mode": "200k"}))

            rc, out, err = _run(
                ["1m"],
                env_overrides={
                    "CLAUDE_CODE_SESSION_ID": sid,
                    "ENGRAM_HOME": str(engram_home),
                },
            )
            self.assertEqual(rc, 0)
            self.assertEqual(out, "")
            self.assertEqual(err, "")

            # Existing value preserved, not overwritten
            with marker.open() as f:
                data = json.load(f)
            self.assertEqual(data["context_mode"], "200k")

    def test_writes_200k_mode(self):
        """Symmetric path for 200K mode."""
        with tempfile.TemporaryDirectory() as tmp:
            engram_home = Path(tmp) / "engram_home"
            (engram_home / "sessions").mkdir(parents=True)
            sid = "cccc3333-dddd-eeee-ffff-aaaabbbbcccc"
            marker = engram_home / "sessions" / f"{sid}.json"
            marker.write_text(json.dumps({"session_id": sid}))

            rc, _, _ = _run(
                ["200k"],
                env_overrides={
                    "CLAUDE_CODE_SESSION_ID": sid,
                    "ENGRAM_HOME": str(engram_home),
                },
            )
            self.assertEqual(rc, 0)
            with marker.open() as f:
                self.assertEqual(json.load(f)["context_mode"], "200k")

    def test_malformed_marker_returns_exit_1(self):
        """Marker exists but isn't valid JSON → exit 1 with stderr message."""
        with tempfile.TemporaryDirectory() as tmp:
            engram_home = Path(tmp) / "engram_home"
            (engram_home / "sessions").mkdir(parents=True)
            sid = "dddd4444-eeee-ffff-aaaa-bbbbccccdddd"
            marker = engram_home / "sessions" / f"{sid}.json"
            marker.write_text("not valid json {")

            rc, _, err = _run(
                ["1m"],
                env_overrides={
                    "CLAUDE_CODE_SESSION_ID": sid,
                    "ENGRAM_HOME": str(engram_home),
                },
            )
            self.assertEqual(rc, 1)
            self.assertIn("could not read marker", err)

    def test_write_error_returns_exit_1(self):
        """Marker readable but unwritable (read-only) → exit 1 with stderr."""
        with tempfile.TemporaryDirectory() as tmp:
            engram_home = Path(tmp) / "engram_home"
            (engram_home / "sessions").mkdir(parents=True)
            sid = "eeee5555-ffff-aaaa-bbbb-ccccddddeeee"
            marker = engram_home / "sessions" / f"{sid}.json"
            marker.write_text(json.dumps({"session_id": sid}))
            # Make marker read-only so the write path fails.
            marker.chmod(0o444)
            try:
                rc, _, err = _run(
                    ["1m"],
                    env_overrides={
                        "CLAUDE_CODE_SESSION_ID": sid,
                        "ENGRAM_HOME": str(engram_home),
                    },
                )
                self.assertEqual(rc, 1)
                self.assertIn("could not write marker", err)
            finally:
                # Restore permission so tempdir cleanup works.
                marker.chmod(0o644)


if __name__ == "__main__":
    unittest.main()
