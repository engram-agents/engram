"""Tests for hooks/claude/engram-solo-wakeup-guard-hook.py (#2427).

Invocation pattern mirrors tests/test_loop_arm_stop_hook.py: run the hook
as a real subprocess against a synthetic transcript, exercising the exact
stdin/stdout contract Claude Code uses for PreToolUse.

Fixture note: unlike test_loop_arm_stop_hook.py's tool_use records (which
never needed uuid/parentUuid, since that hook only scans for tool_use
BLOCKS by type), this hook's whole job is chain-walking, so every fixture
record here carries real uuid/parentUuid fields, matching the actual
Claude Code transcript shape (empirically confirmed against a real
session transcript: a "parallel" batch of N tool calls lands as N
consecutive single-tool_use assistant records chained by parentUuid, with
no tool_result record between them -- see the hook's own module
docstring, point 1).
"""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import uuid as _uuid_mod
from pathlib import Path

HOOKS_DIR = Path(__file__).parent.parent / "src" / "engram" / "hooks" / "claude"
HOOK_PATH = HOOKS_DIR / "engram-solo-wakeup-guard-hook.py"
PYTHON = sys.executable

# Direct module load for unit-level tests below (the hyphenated filename
# can't be `import`ed normally) -- same pattern as test_time_bar_hook.py's
# own load of engram-time-bar-hook.py.
_spec = importlib.util.spec_from_file_location("solo_wakeup_guard_hook", HOOK_PATH)
guard = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(guard)


def _uid() -> str:
    return str(_uuid_mod.uuid4())


def _run_hook(payload: dict) -> tuple[str, int]:
    result = subprocess.run(
        [PYTHON, str(HOOK_PATH)],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        env=dict(os.environ),
        timeout=15,
    )
    return result.stdout.strip(), result.returncode


def _is_denied(stdout: str) -> bool:
    if not stdout:
        return False
    try:
        parsed = json.loads(stdout)
    except json.JSONDecodeError:
        return False
    hso = parsed.get("hookSpecificOutput", {})
    return hso.get("permissionDecision") == "deny"


def _write_transcript(tmp_path: Path, *records: dict) -> Path:
    transcript = tmp_path / "transcript.jsonl"
    with open(transcript, "w", encoding="utf-8") as f:
        for rec in records:
            f.write(json.dumps(rec, separators=(",", ":")) + "\n")
    return transcript


def _user_prompt(text: str, uid: str, parent: str | None = None) -> dict:
    """A genuine top-level user prompt record (not a tool_result)."""
    return {
        "type": "user",
        "uuid": uid,
        "parentUuid": parent,
        "message": {"content": [{"type": "text", "text": text}]},
    }


def _assistant_text(text: str, uid: str, parent: str | None = None) -> dict:
    return {
        "type": "assistant",
        "uuid": uid,
        "parentUuid": parent,
        "message": {"content": [{"type": "text", "text": text}]},
    }


def _tool_use(name: str, uid: str, parent: str | None = None, input_: dict | None = None) -> dict:
    return {
        "type": "assistant",
        "uuid": uid,
        "parentUuid": parent,
        "message": {
            "content": [
                {"type": "tool_use", "id": f"toolu_{uid[:8]}", "name": name, "input": input_ or {}}
            ]
        },
    }


def _tool_result(tool_call_uid: str, uid: str, parent: str | None = None, text: str = "ok") -> dict:
    return {
        "type": "user",
        "uuid": uid,
        "parentUuid": parent,
        "message": {
            "content": [
                {"type": "tool_result", "tool_use_id": f"toolu_{tool_call_uid[:8]}", "content": text}
            ]
        },
    }


def _payload(transcript_path: Path, tool_name: str = "ScheduleWakeup") -> dict:
    return {
        "session_id": "test-solo-guard",
        "transcript_path": str(transcript_path),
        "tool_name": tool_name,
        "tool_input": {"prompt": "irrelevant for identification", "delaySeconds": 600},
        "cwd": "/tmp",
        "hook_event_name": "PreToolUse",
    }


# ===========================================================================
# Core deny case: human-triggered turn, solo ScheduleWakeup call.
# ===========================================================================

def test_denies_solo_schedulewakeup_on_a_human_triggered_turn(tmp_path):
    u_prompt = _uid()
    u_call = _uid()
    transcript = _write_transcript(
        tmp_path,
        _user_prompt("Please check in on the deploy in ten minutes.", u_prompt),
        _tool_use("ScheduleWakeup", u_call, parent=u_prompt),
    )
    stdout, rc = _run_hook(_payload(transcript))
    assert _is_denied(stdout), f"expected deny, got: {stdout!r}"
    assert rc == 0


# ===========================================================================
# Batched (not solo) — sibling BEFORE this call in the chain.
# ===========================================================================

def test_allows_when_a_sibling_tool_use_precedes_in_the_same_batch(tmp_path):
    u_prompt = _uid()
    u_sibling = _uid()
    u_call = _uid()
    transcript = _write_transcript(
        tmp_path,
        _user_prompt("Please check in on the deploy in ten minutes.", u_prompt),
        _tool_use("CronList", u_sibling, parent=u_prompt),
        _tool_use("ScheduleWakeup", u_call, parent=u_sibling),
    )
    stdout, rc = _run_hook(_payload(transcript))
    assert not _is_denied(stdout), f"expected allow, got: {stdout!r}"
    assert rc == 0


# ===========================================================================
# Batched (not solo) — sibling AFTER this call in the chain (chained off
# this call's own uuid, no tool_result in between).
# ===========================================================================

def test_allows_when_a_sibling_tool_use_follows_in_the_same_batch(tmp_path):
    u_prompt = _uid()
    u_call = _uid()
    u_sibling = _uid()
    transcript = _write_transcript(
        tmp_path,
        _user_prompt("Please check in on the deploy in ten minutes.", u_prompt),
        _tool_use("ScheduleWakeup", u_call, parent=u_prompt),
        _tool_use("CronList", u_sibling, parent=u_call),
    )
    stdout, rc = _run_hook(_payload(transcript))
    assert not _is_denied(stdout), f"expected allow, got: {stdout!r}"
    assert rc == 0


# ===========================================================================
# Not human-triggered: loop-wake origin, solo call — must ALLOW (this is
# the ordinary, expected shape of every autonomous heartbeat tick).
# ===========================================================================

def test_allows_solo_schedulewakeup_on_a_loop_wake_triggered_turn(tmp_path):
    u_prompt = _uid()
    u_call = _uid()
    transcript = _write_transcript(
        tmp_path,
        _user_prompt("<loop-wake>\n<loop-meta {\"kind\":\"working\"}>\nLoop tick.", u_prompt),
        _tool_use("ScheduleWakeup", u_call, parent=u_prompt),
    )
    stdout, rc = _run_hook(_payload(transcript))
    assert not _is_denied(stdout), f"expected allow, got: {stdout!r}"
    assert rc == 0


def test_allows_solo_schedulewakeup_when_loop_wake_marker_is_html_escaped(tmp_path):
    """Mirrors #1649: an escaped &lt;loop-wake&gt; must still classify as
    non-human, not silently fall through to a human deny."""
    u_prompt = _uid()
    u_call = _uid()
    transcript = _write_transcript(
        tmp_path,
        _user_prompt("&lt;loop-wake&gt;\nLoop tick.", u_prompt),
        _tool_use("ScheduleWakeup", u_call, parent=u_prompt),
    )
    stdout, rc = _run_hook(_payload(transcript))
    assert not _is_denied(stdout), f"expected allow, got: {stdout!r}"
    assert rc == 0


# ===========================================================================
# Not human-triggered: task-notification origin, solo call — must ALLOW.
# ===========================================================================

def test_allows_solo_schedulewakeup_on_a_task_notification_triggered_turn(tmp_path):
    u_prompt = _uid()
    u_call = _uid()
    transcript = _write_transcript(
        tmp_path,
        _user_prompt(
            "<task-notification>\n<task-id>abc123</task-id>\n"
            "Agent finished.\n</task-notification>",
            u_prompt,
        ),
        _tool_use("ScheduleWakeup", u_call, parent=u_prompt),
    )
    stdout, rc = _run_hook(_payload(transcript))
    assert not _is_denied(stdout), f"expected allow, got: {stdout!r}"
    assert rc == 0


def test_allows_solo_schedulewakeup_on_a_stop_hook_feedback_triggered_turn(tmp_path):
    """Borges's colleague-review finding on PR#2433: a blocking Stop-hook
    re-invocation (e.g. engram-loop-arm-stop-hook.py's own nudge) renders
    with promptSource=None and no other recognized non-human prefix,
    classifying as HUMAN under the base tuple -- which would have this
    guard deny the exact solo ScheduleWakeup arm that Stop hook is
    DEMANDING, two of this codebase's own hooks in direct opposition on
    one turn. Exact leading text confirmed against a real occurrence in
    a live session transcript."""
    u_prompt = _uid()
    u_call = _uid()
    transcript = _write_transcript(
        tmp_path,
        _user_prompt(
            "Stop hook feedback:\n[Loop Arm Check: a loop marker is active "
            "and this turn shows no ScheduleWakeup call. Arm now.]",
            u_prompt,
        ),
        _tool_use("ScheduleWakeup", u_call, parent=u_prompt),
    )
    stdout, rc = _run_hook(_payload(transcript))
    assert not _is_denied(stdout), f"expected allow, got: {stdout!r}"
    assert rc == 0


# ===========================================================================
# Tool-result records between the origin prompt and this call must be
# walked THROUGH (skipped), not mistaken for the origin themselves.
# ===========================================================================

def test_walks_through_prior_tool_results_to_find_the_real_origin(tmp_path):
    u_prompt = _uid()
    u_call1 = _uid()
    u_result1 = _uid()
    u_text = _uid()
    u_call2 = _uid()
    transcript = _write_transcript(
        tmp_path,
        _user_prompt("Please check in on the deploy in ten minutes.", u_prompt),
        _tool_use("Bash", u_call1, parent=u_prompt),
        _tool_result(u_call1, u_result1, parent=u_call1, text="deploy status: ok"),
        _assistant_text("Deploy looks healthy.", u_text, parent=u_result1),
        _tool_use("ScheduleWakeup", u_call2, parent=u_text),
    )
    stdout, rc = _run_hook(_payload(transcript))
    assert _is_denied(stdout), f"expected deny (origin is still the human prompt), got: {stdout!r}"
    assert rc == 0


# ===========================================================================
# Fail-toward-allow: this call's own record can't be identified.
# ===========================================================================

def test_allows_when_last_tool_use_in_transcript_is_not_schedulewakeup(tmp_path):
    """Simulates flush-lag / identification failure: the most recent
    tool_use in the transcript names a different tool. Per the module
    docstring's fail-direction, this must ALLOW, not guess."""
    u_prompt = _uid()
    u_call = _uid()
    transcript = _write_transcript(
        tmp_path,
        _user_prompt("Please check in on the deploy in ten minutes.", u_prompt),
        _tool_use("Bash", u_call, parent=u_prompt),
    )
    stdout, rc = _run_hook(_payload(transcript))
    assert not _is_denied(stdout), f"expected allow, got: {stdout!r}"
    assert rc == 0


def test_allows_on_empty_transcript(tmp_path):
    transcript = _write_transcript(tmp_path)
    stdout, rc = _run_hook(_payload(transcript))
    assert not _is_denied(stdout)
    assert rc == 0


def test_allows_on_missing_transcript_file(tmp_path):
    missing = tmp_path / "does-not-exist.jsonl"
    stdout, rc = _run_hook(_payload(missing))
    assert not _is_denied(stdout)
    assert rc == 0


def test_allows_when_origin_chain_is_broken(tmp_path):
    """A ScheduleWakeup call whose parentUuid points nowhere resolvable --
    the origin walk can't find a real prompt, so this must fail toward
    ALLOW (module docstring's fail-direction), not deny on absent
    evidence."""
    u_call = _uid()
    transcript = _write_transcript(
        tmp_path,
        _tool_use("ScheduleWakeup", u_call, parent="orphan-parent-does-not-exist"),
    )
    stdout, rc = _run_hook(_payload(transcript))
    assert not _is_denied(stdout), f"expected allow, got: {stdout!r}"
    assert rc == 0


# ===========================================================================
# Defensive: wrong tool_name (matcher should already scope this, but check
# the hook's own belt-and-suspenders identity gate).
# ===========================================================================

def test_ignores_calls_to_other_tools(tmp_path):
    u_prompt = _uid()
    u_call = _uid()
    transcript = _write_transcript(
        tmp_path,
        _user_prompt("Please check in on the deploy in ten minutes.", u_prompt),
        _tool_use("Bash", u_call, parent=u_prompt),
    )
    stdout, rc = _run_hook(_payload(transcript, tool_name="Bash"))
    assert not _is_denied(stdout)
    assert rc == 0


def test_never_crashes_on_malformed_stdin():
    result = subprocess.run(
        [PYTHON, str(HOOK_PATH)],
        input="not valid json{{{",
        capture_output=True,
        text=True,
        env=dict(os.environ),
        timeout=15,
    )
    assert result.returncode == 0
    assert not _is_denied(result.stdout.strip())


def test_never_crashes_on_empty_stdin():
    result = subprocess.run(
        [PYTHON, str(HOOK_PATH)],
        input="",
        capture_output=True,
        text=True,
        env=dict(os.environ),
        timeout=15,
    )
    assert result.returncode == 0
    assert not _is_denied(result.stdout.strip())


# ===========================================================================
# A decoy Bash call whose OWN input happens to contain human-shaped text
# must not confuse identification -- tool identity is required (mirrors
# the sibling Stop hook's own round-3 discipline), and here it's the LAST
# tool_use record's NAME that must be ScheduleWakeup, not any text content.
# ===========================================================================

def test_a_later_non_schedulewakeup_call_is_not_mistaken_for_the_guarded_call(tmp_path):
    u_prompt = _uid()
    u_call1 = _uid()
    transcript = _write_transcript(
        tmp_path,
        _user_prompt("Please check in on the deploy in ten minutes.", u_prompt),
        _tool_use(
            "Bash", u_call1, parent=u_prompt,
            input_={"command": "echo ScheduleWakeup would go here"},
        ),
    )
    # The hook is invoked as if ScheduleWakeup were the pending call, but
    # the transcript's actual last tool_use is a Bash call -- identification
    # must fail (not misread the Bash decoy as confirming anything), and
    # fail toward allow per the module's own documented direction.
    stdout, rc = _run_hook(_payload(transcript))
    assert not _is_denied(stdout), f"expected allow, got: {stdout!r}"
    assert rc == 0


# ===========================================================================
# Direct unit-level pin on _find_this_call, per reviewer-fairy suggestion:
# the subprocess allow/deny contract alone doesn't distinguish "correctly
# found the ScheduleWakeup record" from "gave up and fell through to allow
# for an unrelated reason" -- a mutation test confirmed a prior version of
# _find_this_call (which gave up at the first non-matching tool_use record
# instead of skipping past it) passed all 15 subprocess-level tests
# unchanged, because both versions happen to converge on ALLOW for the
# "sibling follows" scenario (for different reasons: the fixed version
# correctly finds ScheduleWakeup and correctly detects the sibling; the
# buggy version fails to find ScheduleWakeup at all). This test asserts
# WHICH record comes back, closing that blind spot.
# ===========================================================================

def test_find_this_call_returns_the_schedulewakeup_record_even_with_a_later_sibling(tmp_path):
    u_prompt = _uid()
    u_call = _uid()
    u_sibling = _uid()
    transcript = _write_transcript(
        tmp_path,
        _user_prompt("Please check in on the deploy in ten minutes.", u_prompt),
        _tool_use("ScheduleWakeup", u_call, parent=u_prompt, input_={"prompt": "wake"}),
        _tool_use("CronList", u_sibling, parent=u_call),
    )
    records, _ = guard._parse_jsonl_records(transcript.read_bytes())
    found = guard._find_this_call(records)
    assert found is not None
    assert found.get("uuid") == u_call, (
        "must identify the ScheduleWakeup record specifically, not the "
        "later CronList sibling -- this is the exact bug a mutation test "
        "caught mid-development (see module docstring, _find_this_call)"
    )


def test_find_this_call_returns_none_with_no_schedulewakeup_anywhere(tmp_path):
    u_prompt = _uid()
    u_call = _uid()
    transcript = _write_transcript(
        tmp_path,
        _user_prompt("Please check in on the deploy in ten minutes.", u_prompt),
        _tool_use("Bash", u_call, parent=u_prompt),
    )
    records, _ = guard._parse_jsonl_records(transcript.read_bytes())
    assert guard._find_this_call(records) is None
