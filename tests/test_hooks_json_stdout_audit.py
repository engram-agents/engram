"""Audit every hook shipped in hooks/hooks.json for Codex-safe stdout JSON.

PR #824 fixed the known laggards (time-bar, stop, user-identity).  This
regression test keeps the rest of the shipped hook set from drifting back to
plain-text or bare-Codex-incompatible stdout: if a referenced hook writes
anything to stdout, that stdout must be a JSON object.  Event hooks that surface
model context must use the event-specific envelope the hook protocol requires.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).parent.parent
HOOKS_JSON = REPO_ROOT / "src" / "engram" / "hooks" / "hooks.json"
HOOKS_DIR = REPO_ROOT / "src" / "engram" / "hooks" / "claude"


def _referenced_hooks() -> list[tuple[str, str, str]]:
    config = json.loads(HOOKS_JSON.read_text())
    refs: list[tuple[str, str, str]] = []
    for event_name, groups in config["hooks"].items():
        for group in groups:
            for hook in group["hooks"]:
                command = hook["command"]
                match = re.search(r"/hooks/([^\"\s]+)", command)
                assert match, f"Could not identify hook script in command: {command!r}"
                refs.append((event_name, match.group(1), command))
    return refs


REFERENCED_HOOKS = _referenced_hooks()


def test_every_hooks_json_reference_has_a_supported_script_type():
    """Future hook script types must be added to this audit deliberately."""
    unsupported = [
        (event_name, script_name, command)
        for event_name, script_name, command in REFERENCED_HOOKS
        if not (script_name.endswith(".py") or script_name.endswith(".sh"))
    ]
    assert unsupported == []


PY_HOOK_REFS = [
    (event_name, script_name)
    for event_name, script_name, _command in REFERENCED_HOOKS
    if script_name.endswith(".py")
]

SHELL_HOOK_REFS = [
    script_name
    for _event_name, script_name, _command in REFERENCED_HOOKS
    if script_name.endswith(".sh")
]


def _payload_for(event_name: str, transcript_path: Path) -> dict:
    base = {
        "session_id": "codex-json-sweep-test",
        "transcript_path": str(transcript_path),
    }
    if event_name == "UserPromptSubmit":
        return {**base, "prompt": "hello world"}
    if event_name == "PreToolUse":
        return {
            **base,
            "tool_name": "mcp__engram__engram_query",
            "tool_input": {"payload_json": "{}"},
        }
    if event_name == "Stop":
        return {
            **base,
            "last_assistant_message": "No ENGRAM node ids here.",
            "stop_hook_active": False,
        }
    if event_name == "SessionStart":
        return {**base, "source": "startup"}
    return base


@pytest.mark.parametrize(("event_name", "script_name"), PY_HOOK_REFS)
def test_referenced_python_hook_stdout_is_empty_or_json_object(
    event_name: str,
    script_name: str,
    tmp_path: Path,
):
    """Every Python hook in hooks/hooks.json must avoid invalid stdout.

    Empty stdout is allowed for no-op hooks. Non-empty stdout is parsed exactly
    the way Codex parses hook output: it must be a JSON object, never plain text.
    """
    hook_path = HOOKS_DIR / script_name
    assert hook_path.exists(), f"hooks.json references missing hook: {script_name}"

    engram_home = tmp_path / "engram-home"
    engram_home.mkdir()
    transcript_path = tmp_path / "transcript.jsonl"
    transcript_path.write_text("", encoding="utf-8")

    env = dict(os.environ)
    env.update(
        {
            "ENGRAM_HOME": str(engram_home),
            "HOME": str(tmp_path),
            "BATON_PROJECTS_DIR": str(tmp_path / "baton-projects"),
            "INTER_AGENT_DIR": str(tmp_path / "inter-agent"),
            "FORUM_URL": "",
            # Keep runtime imports pointed at this checkout, not a user's
            # production ENGRAM_HOME scatter copy.
            "ENGRAM_RUNTIME_DIR": str(REPO_ROOT),
        }
    )
    result = subprocess.run(
        [sys.executable, str(hook_path)],
        input=json.dumps(_payload_for(event_name, transcript_path)),
        capture_output=True,
        text=True,
        env=env,
        timeout=15,
    )

    assert result.returncode == 0, (
        f"{script_name} ({event_name}) exited non-zero: {result.returncode}\n"
        f"stdout: {result.stdout[:500]!r}\n"
        f"stderr: {result.stderr[:500]!r}"
    )

    stdout = result.stdout.strip()
    if not stdout:
        return

    try:
        payload = json.loads(stdout)
    except json.JSONDecodeError as exc:
        pytest.fail(
            f"{script_name} ({event_name}) emitted invalid JSON stdout: {exc}\n"
            f"stdout: {stdout[:500]!r}\n"
            f"stderr: {result.stderr[:500]!r}"
        )

    assert isinstance(payload, dict), (
        f"{script_name} ({event_name}) stdout JSON must be an object, "
        f"got {type(payload).__name__}: {payload!r}"
    )
    assert "additionalContext" not in payload, (
        f"{script_name} ({event_name}) emitted bare additionalContext; "
        "Codex requires event-specific envelopes, never bare additionalContext"
    )

    if event_name == "Stop":
        assert "hookSpecificOutput" not in payload, (
            f"{script_name} ({event_name}) must not use hookSpecificOutput; "
            "Stop hooks continue the turn with decision/reason"
        )
        assert payload.get("decision") == "block", (
            f"{script_name} ({event_name}) decision must be 'block'; got {payload.get('decision')!r}"
        )
        reason = payload.get("reason")
        assert isinstance(reason, str) and reason, (
            f"{script_name} ({event_name}) reason must be a non-empty string"
        )
        return

    hook_specific = payload.get("hookSpecificOutput")
    if hook_specific is None:
        # {} is the canonical no-op JSON object.
        assert payload == {}, (
            f"{script_name} ({event_name}) emitted a non-empty JSON object "
            f"without hookSpecificOutput: {payload!r}"
        )
        return

    assert isinstance(hook_specific, dict), (
        f"{script_name} ({event_name}) hookSpecificOutput must be an object"
    )
    assert hook_specific.get("hookEventName") == event_name, (
        f"{script_name} emitted hookEventName={hook_specific.get('hookEventName')!r}, "
        f"expected {event_name!r}"
    )
    if "additionalContext" in hook_specific:
        assert isinstance(hook_specific["additionalContext"], str), (
            f"{script_name} ({event_name}) additionalContext must be a string"
        )


def test_referenced_shell_hooks_do_not_write_plaintext_stdout():
    """Shell hooks in hooks.json must not use stdout for human/plain text."""
    for script_name in SHELL_HOOK_REFS:
        script_path = HOOKS_DIR / script_name
        assert script_path.exists(), f"hooks.json references missing hook: {script_name}"
        for line_no, line in enumerate(script_path.read_text().splitlines(), start=1):
            stripped = line.strip()
            if not stripped or stripped.startswith("#") or "echo" not in stripped:
                continue
            assert ">&2" in stripped or "1>&2" in stripped, (
                f"{script_name}:{line_no} writes with echo but does not redirect "
                f"to stderr: {line!r}"
            )
