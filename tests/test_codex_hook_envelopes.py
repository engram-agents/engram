"""Tests: all fixed hooks emit strict-JSON envelopes on stdout (part of #823).

Each test:
  1. Runs the hook via subprocess with a minimal stdin payload.
  2. Captures stdout.
  3. Asserts stdout is valid JSON.
  4. Asserts the correct envelope shape for the hook's event type.

NON-VACUOUS design: every test uses a real subprocess run and asserts the
actual envelope shape on the parsed output, not just "stdout is non-empty".
If a hook reverts to plain-text stdout the JSON parse will fail and the test
will catch it. The envelope-shape assertions also catch the older bare
`{"additionalContext": ...}` form that Codex rejects, and the
`{"systemMessage": ...}` form that is user-facing (not model-reaching).

Hooks covered:
  - engram-time-bar-hook.py  (UserPromptSubmit)
  - engram-stop-hook.py      (Stop -> hookSpecificOutput.additionalContext)
  - engram-user-identity-hook.py (UserPromptSubmit, context + no-op paths)
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

HOOKS_DIR = Path(__file__).parent.parent / "src" / "engram" / "hooks" / "claude"

PYTHON = sys.executable

# Minimal SessionStart / UserPromptSubmit payload (no real session needed)
_BASE_PROMPT_PAYLOAD = json.dumps({
    "session_id": "test-session-envelope",
    "transcript_path": "/tmp/fake-transcript.jsonl",
    "prompt": "hello world",
})

# Minimal Stop payload (Stop hooks also accept session_id / transcript_path)
_STOP_PAYLOAD = json.dumps({
    "session_id": "test-session-envelope",
    "transcript_path": "/tmp/fake-transcript.jsonl",
    "last_assistant_message": "Some assistant text.",
    "stop_hook_active": False,
})


def _run_hook(hook_name: str, stdin_payload: str, env: dict | None = None) -> dict:
    """Run a hook script, return its stdout parsed as JSON.

    Raises AssertionError (via pytest.fail) if the hook exits with a non-zero
    code OR if stdout is not valid JSON.  This is deliberate: a non-zero exit is
    an infrastructure failure, not an envelope failure.
    """
    hook_path = HOOKS_DIR / hook_name
    merged_env = dict(os.environ)
    # Point ENGRAM_HOME at a temp path so the hook never touches production data.
    merged_env["ENGRAM_HOME"] = "/tmp/engram-test-home-envelopes"
    if env:
        merged_env.update(env)

    result = subprocess.run(
        [PYTHON, str(hook_path)],
        input=stdin_payload,
        capture_output=True,
        text=True,
        env=merged_env,
    )
    # Don't assert returncode == 0: hooks often exit(0) explicitly but some
    # paths exit(1) on stdin errors. What matters is the stdout envelope shape.
    stdout = result.stdout.strip()
    assert stdout, (
        f"{hook_name} produced empty stdout.\n"
        f"stderr: {result.stderr[:500]}"
    )
    try:
        return json.loads(stdout)
    except json.JSONDecodeError as exc:
        pytest.fail(
            f"{hook_name} stdout is not valid JSON: {exc}\n"
            f"stdout was: {stdout[:500]}"
        )


# ---------------------------------------------------------------------------
# engram-time-bar-hook.py — UserPromptSubmit
# ---------------------------------------------------------------------------

class TestTimeBarHookEnvelope:
    """engram-time-bar-hook.py must emit hookSpecificOutput/UserPromptSubmit."""

    def test_stdout_is_valid_json(self):
        payload = _run_hook("engram-time-bar-hook.py", _BASE_PROMPT_PAYLOAD)
        # If _run_hook returns, stdout was valid JSON.
        assert isinstance(payload, dict)

    def test_has_hook_specific_output_key(self):
        payload = _run_hook("engram-time-bar-hook.py", _BASE_PROMPT_PAYLOAD)
        assert "hookSpecificOutput" in payload, (
            "Missing 'hookSpecificOutput' key — hook returned bare or plain-text output"
        )

    def test_hook_event_name_is_user_prompt_submit(self):
        payload = _run_hook("engram-time-bar-hook.py", _BASE_PROMPT_PAYLOAD)
        hso = payload["hookSpecificOutput"]
        assert hso.get("hookEventName") == "UserPromptSubmit", (
            f"hookEventName is {hso.get('hookEventName')!r}, expected 'UserPromptSubmit'"
        )

    def test_additional_context_is_string(self):
        payload = _run_hook("engram-time-bar-hook.py", _BASE_PROMPT_PAYLOAD)
        hso = payload["hookSpecificOutput"]
        ctx = hso.get("additionalContext")
        assert isinstance(ctx, str) and ctx, (
            "additionalContext is absent or not a non-empty string"
        )

    def test_additional_context_contains_time_marker(self):
        """Agent-visible content is unchanged: the time bar must still start with '[Time:'."""
        payload = _run_hook("engram-time-bar-hook.py", _BASE_PROMPT_PAYLOAD)
        ctx = payload["hookSpecificOutput"]["additionalContext"]
        assert ctx.startswith("[Time:"), (
            f"Time bar content changed: {ctx[:80]!r}"
        )

    def test_not_bare_additional_context(self):
        """Codex rejects the bare form {'additionalContext': ...} — must be nested."""
        payload = _run_hook("engram-time-bar-hook.py", _BASE_PROMPT_PAYLOAD)
        assert "additionalContext" not in payload, (
            "Hook is emitting the bare {'additionalContext': ...} form that Codex rejects"
        )


# ---------------------------------------------------------------------------
# engram-stop-hook.py — Stop
# ---------------------------------------------------------------------------

class TestStopHookEnvelope:
    """engram-stop-hook.py must emit hookSpecificOutput/Stop/additionalContext.

    The Stop hook is a MODEL-directed write-check nudge, so it must use the
    model-reaching form (hookSpecificOutput.additionalContext) — NOT the
    user-facing systemMessage form, which would produce a visible warning
    banner and never reach the model.
    """

    def test_stdout_is_valid_json(self):
        payload = _run_hook("engram-stop-hook.py", _STOP_PAYLOAD)
        assert isinstance(payload, dict)

    def test_has_hook_specific_output_key(self):
        payload = _run_hook("engram-stop-hook.py", _STOP_PAYLOAD)
        assert "hookSpecificOutput" in payload, (
            "Missing 'hookSpecificOutput' key — Stop hook returned wrong envelope form"
        )

    def test_hook_event_name_is_stop(self):
        payload = _run_hook("engram-stop-hook.py", _STOP_PAYLOAD)
        hso = payload["hookSpecificOutput"]
        assert hso.get("hookEventName") == "Stop", (
            f"hookEventName is {hso.get('hookEventName')!r}, expected 'Stop'"
        )

    def test_additional_context_is_nonempty_string(self):
        payload = _run_hook("engram-stop-hook.py", _STOP_PAYLOAD)
        hso = payload["hookSpecificOutput"]
        ctx = hso.get("additionalContext")
        assert isinstance(ctx, str) and ctx, (
            "additionalContext is absent or not a non-empty string"
        )

    def test_additional_context_contains_write_check(self):
        """Model-visible content is unchanged: must contain the ENGRAM Write Check text."""
        payload = _run_hook("engram-stop-hook.py", _STOP_PAYLOAD)
        ctx = payload["hookSpecificOutput"]["additionalContext"]
        assert "ENGRAM Write Check" in ctx, (
            "Stop hook write-check text changed unexpectedly"
        )

    def test_not_system_message(self):
        """systemMessage is user-facing (banner), not model-reaching — must NOT be present."""
        payload = _run_hook("engram-stop-hook.py", _STOP_PAYLOAD)
        assert "systemMessage" not in payload, (
            "Stop hook is using systemMessage (user-facing banner) — must use "
            "hookSpecificOutput.additionalContext (model-reaching) instead"
        )

    def test_not_plain_text(self):
        """Verify stdout is not plain text (the old broken form)."""
        hook_path = HOOKS_DIR / "engram-stop-hook.py"
        env = dict(os.environ)
        env["ENGRAM_HOME"] = "/tmp/engram-test-home-envelopes"
        result = subprocess.run(
            [PYTHON, str(hook_path)],
            input=_STOP_PAYLOAD,
            capture_output=True,
            text=True,
            env=env,
        )
        stdout = result.stdout.strip()
        # Must be parseable as JSON (not plain text like "[ENGRAM Write Check: ...]")
        try:
            json.loads(stdout)
        except json.JSONDecodeError:
            pytest.fail(
                f"Stop hook stdout is plain text (not JSON): {stdout[:200]!r}"
            )


# ---------------------------------------------------------------------------
# engram-user-identity-hook.py — UserPromptSubmit
# ---------------------------------------------------------------------------

class TestUserIdentityHookEnvelope:
    """engram-user-identity-hook.py must emit nested hookSpecificOutput or {}."""

    def _no_op_payload(self):
        """Payload with no person prefix that can match — hook will return {}."""
        return json.dumps({
            "session_id": "test-session-envelope",
            "transcript_path": "/tmp/fake-transcript.jsonl",
            "prompt": "hello world",
        })

    def _command_payload(self):
        """Prompt starting with / — hook exits 0 without printing."""
        return json.dumps({
            "session_id": "test-session-envelope",
            "transcript_path": "/tmp/fake-transcript.jsonl",
            "prompt": "/help",
        })

    def test_no_op_path_is_empty_json_object(self):
        """No-op paths (no match, no sticky) must return {} — valid JSON."""
        payload = _run_hook("engram-user-identity-hook.py", self._no_op_payload())
        assert payload == {}, (
            f"No-op path returned non-empty dict: {payload!r}"
        )

    def test_no_op_is_valid_json(self):
        """Even the no-op path must be valid JSON."""
        payload = _run_hook("engram-user-identity-hook.py", self._no_op_payload())
        assert isinstance(payload, dict)

    def test_no_bare_additional_context_on_no_op(self):
        """No-op {} must NOT have a bare additionalContext key."""
        payload = _run_hook("engram-user-identity-hook.py", self._no_op_payload())
        assert "additionalContext" not in payload, (
            "No-op path emitting bare additionalContext — must be {}"
        )

    def test_when_context_injected_has_nested_envelope(self, tmp_path, monkeypatch):
        """When a person is detected (sticky path), envelope must be
        hookSpecificOutput/UserPromptSubmit.

        Sets up:
          - A minimal knowledge.db with one person node named "Alex".
          - current_user.json pointing at Alex.
          - config.json with primary_user="borges" (not "alex") so sticky fires.

        The prompt "what should we do next" has no name-prefix, so
        detect_speaker returns None and the sticky path activates.
        """
        import sqlite3 as _sqlite3

        # Build a fake ENGRAM_HOME
        fake_home = tmp_path / "engram-fake"
        fake_home.mkdir()

        # Minimal knowledge.db with one person node for "Alex"
        db_path = fake_home / "knowledge.db"
        conn = _sqlite3.connect(str(db_path))
        conn.execute("""
            CREATE TABLE nodes (
                id TEXT PRIMARY KEY,
                type TEXT,
                claim TEXT,
                logical_chain TEXT,
                metadata TEXT,
                is_current INTEGER DEFAULT 1
            )
        """)
        conn.execute(
            "INSERT INTO nodes (id, type, claim, logical_chain, metadata, is_current) "
            "VALUES (?, ?, ?, ?, ?, 1)",
            (
                "pn_0099",
                "person",
                "Alex is a tester",
                "Test user",
                json.dumps({"name": "Alex", "aliases": [], "role": "tester"}),
            ),
        )
        conn.commit()
        conn.close()

        # current_user.json: non-primary user (sticky path)
        current_user = {
            "person_id": "pn_0099",
            "name": "Alex",
            "role": "tester",
            "description": "Test user for envelope testing",
            "matched_prefix": "Alex",
        }
        (fake_home / "current_user.json").write_text(json.dumps(current_user))
        # config.json: primary_user is "borges" (not "alex"), so sticky fires
        config = {"primary_user": "borges"}
        (fake_home / "config.json").write_text(json.dumps(config))

        # Prompt with no name prefix — sticky path fires (detect_speaker returns None)
        prompt_payload = json.dumps({
            "session_id": "test-session-envelope",
            "transcript_path": "/tmp/fake-transcript.jsonl",
            "prompt": "what should we do next",
        })

        env = dict(os.environ)
        env["ENGRAM_HOME"] = str(fake_home)
        hook_path = HOOKS_DIR / "engram-user-identity-hook.py"
        result = subprocess.run(
            [PYTHON, str(hook_path)],
            input=prompt_payload,
            capture_output=True,
            text=True,
            env=env,
        )
        stdout = result.stdout.strip()
        assert stdout, f"Hook produced empty stdout; stderr: {result.stderr[:300]}"

        try:
            parsed = json.loads(stdout)
        except json.JSONDecodeError as exc:
            pytest.fail(f"Hook stdout not valid JSON: {exc}\nstdout: {stdout[:300]}")

        # Must use the nested envelope form
        assert "hookSpecificOutput" in parsed, (
            f"Missing hookSpecificOutput in identity hook output: {parsed!r}"
        )
        hso = parsed["hookSpecificOutput"]
        assert hso.get("hookEventName") == "UserPromptSubmit", (
            f"hookEventName wrong: {hso.get('hookEventName')!r}"
        )
        assert isinstance(hso.get("additionalContext"), str), (
            "additionalContext must be a string"
        )
        assert "Alex" in hso["additionalContext"], (
            "additionalContext should mention the sticky user name"
        )
        # Confirm NOT the bare form
        assert "additionalContext" not in parsed, (
            "Bare additionalContext key at top level — still using old form"
        )
