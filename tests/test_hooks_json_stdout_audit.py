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
import sqlite3
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

PRETOOL_ACTIVE_FIXTURES = {
    "engram-bash-pipe-exit-warn.py",
    "engram-lesson-tripwire-hook.py",
    "engram-toolcall-repair.py",
}


def _seed_lesson_tripwire_db(engram_home: Path) -> None:
    """Create the minimal active-lesson DB state needed to exercise the hook."""
    db = engram_home / "knowledge.db"
    conn = sqlite3.connect(str(db))
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS nodes (
            id TEXT PRIMARY KEY,
            type TEXT NOT NULL,
            is_current INTEGER NOT NULL DEFAULT 1,
            memory_status TEXT NOT NULL DEFAULT 'active',
            metadata TEXT NOT NULL DEFAULT '{}',
            claim TEXT NOT NULL DEFAULT ''
        )
        """
    )
    conn.execute(
        "INSERT INTO nodes (id, type, is_current, memory_status, metadata, claim) "
        "VALUES (?, 'lesson', 1, 'active', ?, ?)",
        (
            "ls_0001",
            json.dumps({
                "situation_pattern": r"forum reply",
                "scaffolding_nudge": "Read body from stdin.",
            }),
            "forum reply lesson",
        ),
    )
    conn.commit()
    conn.close()


def _payload_for(event_name: str, script_name: str, transcript_path: Path) -> dict:
    base = {
        "session_id": "codex-json-sweep-test",
        "transcript_path": str(transcript_path),
    }
    if event_name == "UserPromptSubmit":
        return {**base, "prompt": "hello world"}
    if event_name == "PreToolUse":
        if script_name == "engram-bash-pipe-exit-warn.py":
            return {
                **base,
                "tool_name": "Bash",
                "tool_input": {"command": "pytest tests/ | tail -20 && git commit -m 'wip'"},
                "input": {"command": "pytest tests/ | tail -20 && git commit -m 'wip'"},
            }
        if script_name == "engram-lesson-tripwire-hook.py":
            return {
                **base,
                "tool_name": "Bash",
                "tool_input": {"command": "forum reply 42"},
                "input": {"command": "forum reply 42"},
            }
        if script_name == "engram-toolcall-repair.py":
            return {
                **base,
                "tool_name": "mcp__engram__engram_add_observation",
                "tool_input": {
                    "quoted_text": "Q",
                    "interpretation": 'I</interpretation><parameter name="claim">C</parameter>',
                    "quote_type": "personal_communication",
                    "url": "https://example.com",
                    "title": "repair test",
                },
            }
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
    if script_name == "engram-lesson-tripwire-hook.py":
        _seed_lesson_tripwire_db(engram_home)

    result = subprocess.run(
        [sys.executable, str(hook_path)],
        input=json.dumps(_payload_for(event_name, script_name, transcript_path)),
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
        assert script_name not in PRETOOL_ACTIVE_FIXTURES, (
            f"{script_name} fixture must exercise its non-empty PreToolUse "
            "output path"
        )
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

    if event_name == "PreToolUse":
        permission = hook_specific.get("permissionDecision")
        has_updated_input = "updatedInput" in hook_specific
        assert not (permission == "allow" and not has_updated_input), (
            f"{script_name} ({event_name}) emitted permissionDecision='allow' "
            "without updatedInput; context-only PreToolUse output must omit "
            "permissionDecision"
        )
        assert not (has_updated_input and permission != "allow"), (
            f"{script_name} ({event_name}) emitted updatedInput without "
            "permissionDecision='allow'"
        )
        if script_name == "engram-toolcall-repair.py":
            assert hook_specific["updatedInput"].get("claim") == "C", (
                "toolcall-repair fixture must prove the rewrite path restored claim"
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
