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


def _make_hook_input(command: str, run_in_background: bool = False) -> str:
    """Build a PreToolUse hook-input payload for sys.stdin."""
    input_payload = {"command": command}
    if run_in_background:
        input_payload["run_in_background"] = True
    return json.dumps({"toolName": "Bash", "input": input_payload})


def _run_hook(command: str, run_in_background: bool = False) -> str:
    """Run the hook's main() with the given command; return captured stdout text."""
    hook = _load_hook_module()
    output_buf = io.StringIO()
    with (
        unittest.mock.patch(
            "sys.stdin", io.StringIO(_make_hook_input(command, run_in_background))
        ),
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

    # -----------------------------------------------------------------
    # Backgrounded-command shapes (issue #2061)
    # -----------------------------------------------------------------

    def test_warns_on_background_bare_pipe(self):
        """Backgrounded bare `cmd | tail` — no in-command consumer, still warns
        because the harness's out-of-band completion notification is the
        consumer for a backgrounded command."""
        out = _run_hook("pytest tests/ | tail -20", run_in_background=True)
        self.assertTrue(out.strip(), "Expected JSON output but got empty stdout")
        response = json.loads(out.strip())
        ctx = response["hookSpecificOutput"]["additionalContext"]
        self.assertIn("pipe-exit-warn", ctx)
        self.assertIn("Backgrounded", ctx)

    def test_no_warn_on_foreground_bare_pipe(self):
        """Foreground bare `cmd | tail` with no dangerous chain/echo — unchanged,
        must not fire (foreground behavior is unaffected by this fix)."""
        out = _run_hook("pytest tests/ | tail -20", run_in_background=False)
        self.assertEqual(
            out.strip(),
            "",
            "Foreground bare pipe with no dangerous context must not warn",
        )

    def test_no_warn_background_without_truncating_reader(self):
        """Backgrounded command with no truncating reader at all — must not warn."""
        out = _run_hook("pytest tests/ -q", run_in_background=True)
        self.assertEqual(
            out.strip(),
            "",
            "Backgrounded command without a truncating reader must not warn",
        )

    def test_warns_on_background_chain_shape_still_fires(self):
        """The June `&&`-chain shape still fires in the foreground (regression
        guard for the pre-existing dangerous-chain detection)."""
        out = _run_hook(
            "pytest tests/ | tail -20 && git commit -m 'wip'",
            run_in_background=False,
        )
        self.assertTrue(out.strip(), "Expected JSON output")
        response = json.loads(out.strip())
        ctx = response["hookSpecificOutput"]["additionalContext"]
        self.assertIn("pipe-exit-warn", ctx)

    def test_warns_on_measured_miss_backgrounded(self):
        """The issue's exact measured miss: `python3 -m pytest tests/ -q 2>&1 |
        tail -8` run backgrounded — must fire."""
        out = _run_hook(
            "python3 -m pytest tests/ -q 2>&1 | tail -8", run_in_background=True
        )
        self.assertTrue(out.strip(), "Expected JSON output")
        response = json.loads(out.strip())
        ctx = response["hookSpecificOutput"]["additionalContext"]
        self.assertIn("pipe-exit-warn", ctx)

    # -----------------------------------------------------------------
    # Dual-key payload shape (tool_input vs input) — round-1 review item 1
    # -----------------------------------------------------------------

    def test_warns_with_tool_input_key_production_shape(self):
        """Production PreToolUse payloads use 'tool_input', not 'input' — the
        hook must read that key too, not just the test helper's 'input'."""
        hook = _load_hook_module()
        payload = json.dumps(
            {
                "toolName": "Bash",
                "tool_input": {"command": "pytest tests/ | tail -20 && git commit -m 'wip'"},
            }
        )
        output_buf = io.StringIO()
        with (
            unittest.mock.patch("sys.stdin", io.StringIO(payload)),
            unittest.mock.patch("sys.stdout", output_buf),
        ):
            try:
                hook.main()
            except SystemExit:
                pass
        out = output_buf.getvalue()
        self.assertTrue(out.strip(), "Expected JSON output but got empty stdout")
        response = json.loads(out.strip())
        ctx = response["hookSpecificOutput"]["additionalContext"]
        self.assertIn("pipe-exit-warn", ctx)

    def test_tool_input_takes_precedence_over_input(self):
        """When both 'tool_input' and 'input' keys are present, 'tool_input'
        must win. tool_input is benign (no risky pipe); input is risky. If
        the hook fell through to 'input', it would (wrongly) warn."""
        hook = _load_hook_module()
        payload = json.dumps(
            {
                "toolName": "Bash",
                "tool_input": {"command": "echo hello"},
                "input": {
                    "command": "pytest tests/ | tail -20 && git commit -m 'wip'"
                },
            }
        )
        output_buf = io.StringIO()
        with (
            unittest.mock.patch("sys.stdin", io.StringIO(payload)),
            unittest.mock.patch("sys.stdout", output_buf),
        ):
            try:
                hook.main()
            except SystemExit:
                pass
        self.assertEqual(
            output_buf.getvalue().strip(),
            "",
            "tool_input must take precedence over input — benign tool_input "
            "command means no warning even though input is risky",
        )

    # -----------------------------------------------------------------
    # Redirect-then-separate-tail/head shape (issue #2061's 2026-08-03 incident)
    # -----------------------------------------------------------------

    def test_warns_on_background_redirect_then_tail(self):
        """Backgrounded `cmd > log; tail log` — no pipe at all, but the same
        trap: the notification reflects tail's exit status, not the
        redirected command's."""
        out = _run_hook(
            "pytest tests/ -q > log.txt; tail log.txt", run_in_background=True
        )
        self.assertTrue(out.strip(), "Expected JSON output but got empty stdout")
        response = json.loads(out.strip())
        ctx = response["hookSpecificOutput"]["additionalContext"]
        self.assertIn("pipe-exit-warn", ctx)

    def test_warns_on_background_redirect_then_tail_n(self):
        """The exact 2026-08-03 incident shape: backgrounded `pytest ... > log; tail -30
        log` — 58 failures presented as a clean pass."""
        out = _run_hook(
            "pytest tests/ -q > log.txt; tail -30 log.txt", run_in_background=True
        )
        self.assertTrue(out.strip(), "Expected JSON output but got empty stdout")
        response = json.loads(out.strip())
        ctx = response["hookSpecificOutput"]["additionalContext"]
        self.assertIn("pipe-exit-warn", ctx)

    def test_no_warn_on_foreground_redirect_then_tail(self):
        """Foreground behavior is byte-unchanged: the redirect-then-tail
        shape is only checked for backgrounded commands."""
        out = _run_hook(
            "pytest tests/ -q > log.txt; tail -30 log.txt", run_in_background=False
        )
        self.assertEqual(
            out.strip(),
            "",
            "Foreground redirect-then-tail shape must not warn (unchanged)",
        )

    def test_no_warn_on_background_redirect_without_tail(self):
        """Backgrounded `cmd > log` with no later tail/head — must not warn."""
        out = _run_hook("pytest tests/ -q > log.txt", run_in_background=True)
        self.assertEqual(
            out.strip(),
            "",
            "Backgrounded redirect without a tail/head reader must not warn",
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

    # Env-var prefixes must not hide a consequential command (#2112 round 3,
    # colleague probe: this repo's own documented pytest convention was
    # invisible to the detector).
    def test_env_prefix_pytest_background_bare_pipe_fires(self):
        out = _run_hook("ENGRAM_NO_EMBEDDINGS=1 pytest tests/ -q | tail -20",
                        run_in_background=True)
        self.assertTrue(out.strip(), "env-prefixed pytest bare pipe must warn")
        ctx = json.loads(out.strip())["hookSpecificOutput"]["additionalContext"]
        self.assertIn("pipe-exit-warn", ctx)

    def test_env_prefix_pythonpath_background_bare_pipe_fires(self):
        out = _run_hook("PYTHONPATH=src python3 -m pytest tests/ -q 2>&1 | tail -8",
                        run_in_background=True)
        self.assertTrue(out.strip(), "PYTHONPATH-prefixed pytest bare pipe must warn")

    def test_env_prefix_alone_does_not_make_consequential(self):
        out = _run_hook("FOO=bar ls -la | tail -5", run_in_background=True)
        self.assertEqual(out.strip(), "", "prefix alone must not make ls consequential")


if __name__ == "__main__":
    unittest.main()
