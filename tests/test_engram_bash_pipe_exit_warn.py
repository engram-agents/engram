"""Tests for the pipe-exit-warn PreToolUse hook.

Issue #1085: agents write `consequential_cmd | tail` which masks the upstream
command's exit status. This hook warns (never blocks) on the high-value shapes:
  1. consequential_cmd | tail && actionable_cmd
  2. consequential_cmd | tail; echo 'success word'
"""

from __future__ import annotations

import importlib.util
import io
import json
import sys
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))


# ---------------------------------------------------------------------------
# Helper: load the hook module (hyphenated filename)
# ---------------------------------------------------------------------------


def _load_hook_module():
    """Import the pipe-exit-warn hook as a module despite the hyphenated filename."""
    hook_path = (
        PROJECT_ROOT
        / "src"
        / "engram"
        / "hooks"
        / "claude"
        / "engram-bash-pipe-exit-warn.py"
    )
    spec = importlib.util.spec_from_file_location("engram_bash_pipe_exit_warn", hook_path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _make_hook_input(command: str) -> str:
    """Build a PreToolUse hook-input payload for sys.stdin."""
    return json.dumps({"toolName": "Bash", "input": {"command": command}})


def _run_hook(command: str) -> str:
    """Run the hook's main() with the given command; return captured stdout text."""
    hook = _load_hook_module()
    output_buf = io.StringIO()
    with (
        unittest.mock.patch("sys.stdin", io.StringIO(_make_hook_input(command))),
        unittest.mock.patch("sys.stdout", output_buf),
    ):
        try:
            hook.main()
        except SystemExit:
            pass
    return output_buf.getvalue()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


import unittest.mock  # noqa: E402 — needed for _run_hook above


class TestPipeExitWarn(unittest.TestCase):

    def test_warns_on_pytest_pipe_then_commit(self):
        """pytest | tail && git commit — warns via context-only PreToolUse output."""
        out = _run_hook("pytest tests/ | tail -20 && git commit -m 'wip'")
        self.assertTrue(out.strip(), "Expected JSON output but got empty stdout")
        response = json.loads(out.strip())
        ctx = response["hookSpecificOutput"]["additionalContext"]
        self.assertIn("pipe-exit-warn", ctx)
        self.assertNotIn(
            "permissionDecision",
            response["hookSpecificOutput"],
            "Context-only PreToolUse output must omit permissionDecision",
        )

    def test_warns_on_gh_pipe_then_push(self):
        """gh pr create | tail && git push — warns."""
        out = _run_hook("gh pr create --title 'x' | tail && git push origin dev")
        self.assertTrue(out.strip(), "Expected JSON output")
        response = json.loads(out.strip())
        ctx = response["hookSpecificOutput"]["additionalContext"]
        self.assertIn("pipe-exit-warn", ctx)

    def test_warns_on_pipe_then_success_echo(self):
        """forum reply | tail; echo '✓ done' — warns."""
        out = _run_hook("forum reply 42 | tail -1; echo '✓ done'")
        self.assertTrue(out.strip(), "Expected JSON output")
        response = json.loads(out.strip())
        ctx = response["hookSpecificOutput"]["additionalContext"]
        self.assertIn("pipe-exit-warn", ctx)

    def test_no_warn_on_pure_read(self):
        """git log | tail — read-only pipeline, no dangerous context, no warning."""
        out = _run_hook("git log --oneline | tail -20")
        self.assertEqual(
            out.strip(),
            "",
            "Pure read pipeline must produce no output (silent exit 0)",
        )

    def test_no_warn_with_pipefail(self):
        """set -o pipefail; pytest | tail && git commit — pipefail guard present."""
        out = _run_hook("set -o pipefail; pytest | tail && git commit")
        self.assertEqual(
            out.strip(),
            "",
            "pipefail guard present — must not warn",
        )

    def test_no_warn_with_PIPESTATUS(self):
        """pytest | tail; ec=$?; [[ ${PIPESTATUS[0]} -eq 0 ]] && git commit — PIPESTATUS guard."""
        out = _run_hook(
            "pytest | tail; ec=$?; [[ ${PIPESTATUS[0]} -eq 0 ]] && git commit"
        )
        self.assertEqual(
            out.strip(),
            "",
            "PIPESTATUS guard present — must not warn",
        )

    def test_no_warn_grep_pipe(self):
        """git log | grep 'fix' | tail — grep excluded from truncating readers."""
        out = _run_hook("git log | grep 'fix' | tail -5 && echo 'found'")
        self.assertEqual(
            out.strip(),
            "",
            "grep is excluded from truncating readers — must not warn",
        )

    def test_no_crash_on_malformed_input(self):
        """Malformed JSON input — hook handles gracefully, no exception propagated."""
        hook = _load_hook_module()
        output_buf = io.StringIO()
        with (
            unittest.mock.patch("sys.stdin", io.StringIO("not valid json")),
            unittest.mock.patch("sys.stdout", output_buf),
        ):
            try:
                hook.main()
            except SystemExit:
                pass
        self.assertEqual(
            output_buf.getvalue().strip(),
            "",
            "Malformed input must produce no output (silent exit 0)",
        )


if __name__ == "__main__":
    unittest.main()
