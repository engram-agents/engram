"""Tests for hooks/claude/engram-loop-arm-stop-hook.py.

2026-07-31-loop-arm-nudge-SPEC.md, PR-B. Spec verification items 2-6 (item
1 -- "existing engram-stop-hook tests pass unchanged" -- is a PR-A
requirement, verified separately; see PR-A's own test run), plus the
round-3 tool-identity fix's own required tests (a real hole the round-3
review found and reproduced: the gate previously matched on TEXT CONTENT
alone, so any tool call/result pair -- e.g. a Bash echo, or a Read of this
hook's own source file, which unavoidably contains the marker and
pending-job words throughout its docstring -- could silently suppress the
nudge), plus the round-6 pacer-gating fix's required tests (a colleague
review asked what happens on a non-scheduleWakeup-paced loop or a
non-Claude install: the hook previously fired on loop-mode.json's mere
EXISTENCE, never reading its "pacer" field, so a cron-paced loop -- which
has no reason to call ScheduleWakeup on any given turn -- got nagged every
single turn with an instruction to ScheduleWakeup, which is actively wrong
for that pacer), plus supporting regression tests (marker-fallback
consistency, stop_hook_active re-fire guard, delta isolation,
lib-import-failure fallback).

2026-08-12 retarget (#1973, #1987): the gate moved from a `CronList`
tool_result to a `ScheduleWakeup` tool_use (tool_call), because a
tool_result showing a pending job is byte-identical whether the job was
armed an hour ago or this second -- it cannot prove "armed from now", which
is what the sliding-window heartbeat rule needs. Every fixture below that
used to build a (CronList tool_use, tool_result-with-marker) correlated
pair now builds a single ScheduleWakeup tool_use block carrying the marker
in its `input.prompt` -- the call itself is the evidence, no result or
correlation needed.

Invocation pattern mirrors tests/test_stop_hook_prose_gate.py: run the hook
as a real subprocess against a synthetic ENGRAM_HOME + transcript, so the
test exercises the exact stdin/stdout contract Claude Code uses.
"""

from __future__ import annotations

import html
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

HOOKS_DIR = Path(__file__).parent.parent / "src" / "engram" / "hooks" / "claude"
HOOK_PATH = HOOKS_DIR / "engram-loop-arm-stop-hook.py"
PYTHON = sys.executable


def _make_stop_payload(
    transcript_path: str,
    session_id: str = "test-loop-arm",
    stop_hook_active: bool = False,
) -> str:
    return json.dumps({
        "session_id": session_id,
        "transcript_path": transcript_path,
        "last_assistant_message": "",
        "stop_hook_active": stop_hook_active,
    })


def _run_hook(
    stdin_payload: str,
    engram_home: Path,
    *,
    extra_env: dict[str, str] | None = None,
    hook_path: Path = HOOK_PATH,
) -> tuple[str, int]:
    """Run the hook, return (stdout_stripped, returncode)."""
    env = dict(os.environ)
    env["ENGRAM_HOME"] = str(engram_home)
    if extra_env:
        env.update(extra_env)
    result = subprocess.run(
        [PYTHON, str(hook_path)],
        input=stdin_payload,
        capture_output=True,
        text=True,
        env=env,
        timeout=15,
    )
    return result.stdout.strip(), result.returncode


def _copy_hook_into_baked_bundle(tmp_path: Path, platform: str) -> tuple[Path, Path]:
    """Create a flat hook bundle with a baked platform profile."""
    bundle = tmp_path / f"{platform}-bundle"
    hooks_dir = bundle / "hooks"
    hooks_dir.mkdir(parents=True)
    hook_path = hooks_dir / HOOK_PATH.name
    shutil.copy2(HOOK_PATH, hook_path)
    (bundle / "platform.json").write_text(
        json.dumps({"platform": platform}), encoding="utf-8"
    )
    return bundle, hook_path


def _nudge_emitted(stdout: str) -> bool:
    """Return True if the hook emitted the loop-arm nudge on this invocation.

    2026-08-12 correction: the nudge is a blocking decision
    (`{"decision": "block", "reason": ...}`), not `additionalContext` --
    additionalContext is not a documented Stop-hook field and never
    reached the agent; see the hook's design-fact-4 docstring.
    """
    if not stdout:
        return False
    try:
        parsed = json.loads(stdout)
        if parsed.get("decision") != "block":
            return False
        return "Loop Arm Check" in parsed.get("reason", "")
    except (json.JSONDecodeError, AttributeError):
        return False


def _write_loop_marker(
    engram_home: Path,
    pacer: str | None = "scheduleWakeup",
    omit_pacer: bool = False,
) -> None:
    """Write loop-mode.json. Defaults to a scheduleWakeup-paced marker (the
    only pacer this hook's nudge applies to, round-6) so every existing
    call site is unaffected. Pass `pacer=` to test other pacers, or
    `omit_pacer=True` to write a marker with no "pacer" key at all."""
    marker: dict = {"kind": "working"}
    if not omit_pacer:
        marker["pacer"] = pacer
    (engram_home / "loop-mode.json").write_text(
        json.dumps(marker), encoding="utf-8"
    )


def _append_lines(transcript: Path, *lines: str) -> None:
    with open(transcript, "a", encoding="utf-8") as f:
        for line in lines:
            f.write(line + "\n")


def _tool_use_record(tool_id: str, name: str, input_: dict | None = None) -> str:
    """Build a compact-JSON assistant-role record with a single tool_use
    block. `input_` defaults to {} (matches a tool call with no relevant
    arguments -- e.g. a bare CronList probe)."""
    return json.dumps(
        {
            "type": "assistant",
            "message": {
                "content": [
                    {
                        "type": "tool_use",
                        "id": tool_id,
                        "name": name,
                        "input": input_ if input_ is not None else {},
                    }
                ]
            },
        },
        separators=(",", ":"),
    )


def _tool_use_record_with_msg_id(
    tool_id: str, name: str, msg_id: str, input_: dict | None = None
) -> str:
    """Assistant record with a single tool_use block AND a message.id —
    the real Claude Code transcript shape for one call in a parallel block."""
    return json.dumps(
        {
            "type": "assistant",
            "message": {
                "id": msg_id,
                "content": [
                    {
                        "type": "tool_use",
                        "id": tool_id,
                        "name": name,
                        "input": input_ if input_ is not None else {},
                    }
                ],
            },
        },
        separators=(",", ":"),
    )


def _batched_arm_records(
    sw_tool_id: str, cronlist_tool_id: str, prompt: str, msg_id: str
) -> tuple[str, str]:
    """Two records sharing `msg_id` — the real Claude Code shape for a
    parallel ScheduleWakeup + CronList call.  Returns (sw_line, cl_line)
    suitable for passing to _append_lines."""
    sw = _tool_use_record_with_msg_id(
        sw_tool_id, "ScheduleWakeup", msg_id, {"prompt": prompt}
    )
    cl = _tool_use_record_with_msg_id(cronlist_tool_id, "CronList", msg_id, {})
    return sw, cl


def _tool_result_record(tool_id: str, text: str) -> str:
    """Build a compact-JSON user-role record with a single tool_result
    block, correlated to `tool_id` via `tool_use_id` -- used only for the
    decoy/companion-result fixtures below (the gate itself never inspects
    tool_result content since the 2026-08-12 retarget)."""
    return json.dumps(
        {
            "type": "user",
            "message": {
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": tool_id,
                        "content": text,
                    }
                ]
            },
        },
        separators=(",", ":"),
    )


def _schedule_wakeup_call(tool_id: str, prompt: str) -> str:
    """A ScheduleWakeup tool_use record carrying `prompt` in its input --
    the shape the gate now checks directly (no companion tool_result
    needed; the call itself is the evidence)."""
    return _tool_use_record(tool_id, "ScheduleWakeup", {"prompt": prompt})


# A realistic loop-wake prompt carrying the marker (spec fact #3's example,
# lightly paraphrased -- now the CONTENT of a ScheduleWakeup call's
# `input.prompt`, not a CronList result's text).
_REAL_LOOP_PROMPT = (
    "<loop-wake>\n<loop-meta {\"kind\":\"working\"}>\nLoop tick."
)

# A ScheduleWakeup prompt with no loop-wake marker -- correctly a
# ScheduleWakeup call, but NOT arming the loop-wake.
_UNRELATED_PROMPT = "Check the mailbox for new letters at 9:00 AM."


# ===========================================================================
# Spec verification item 2: nudge FIRES with loop marker present and no
# ScheduleWakeup call in the delta.
# ===========================================================================

def test_nudge_fires_with_loop_marker_and_no_schedulewakeup_call(tmp_path):
    engram_home = tmp_path / "engram"
    engram_home.mkdir()
    _write_loop_marker(engram_home)
    transcript = tmp_path / "transcript.jsonl"
    transcript.write_text("", encoding="utf-8")

    # Fire 1: establish state at EOF (empty transcript).
    _run_hook(_make_stop_payload(str(transcript)), engram_home)

    # Delta: some ordinary assistant text, no tool_use at all.
    _append_lines(
        transcript,
        json.dumps({"type": "assistant", "message": {"content": [
            {"type": "text", "text": "Just some prose, no arm check."}
        ]}}, separators=(",", ":")),
    )

    stdout, rc = _run_hook(_make_stop_payload(str(transcript)), engram_home)

    assert rc == 0
    assert _nudge_emitted(stdout), (
        f"Loop marker present + no ScheduleWakeup call in delta must EMIT "
        f"the nudge; stdout={stdout!r}"
    )


# ===========================================================================
# Spec verification item 3: SUPPRESSES given a ScheduleWakeup tool_use
# call whose `input.prompt` carries the `<loop-wake>` marker -- this is
# also the round-3 fix's core positive case, ported to the tool_use shape.
# ===========================================================================

def test_suppresses_given_schedulewakeup_call_with_marker(tmp_path):
    engram_home = tmp_path / "engram"
    engram_home.mkdir()
    _write_loop_marker(engram_home)
    transcript = tmp_path / "transcript.jsonl"
    transcript.write_text("", encoding="utf-8")

    _run_hook(_make_stop_payload(str(transcript)), engram_home)

    sw, cl = _batched_arm_records(
        "toolu_test_swu", "toolu_test_swu_cl", _REAL_LOOP_PROMPT, "msg_test_arm_1"
    )
    _append_lines(transcript, sw, cl)

    stdout, rc = _run_hook(_make_stop_payload(str(transcript)), engram_home)

    assert rc == 0
    assert stdout == "", (
        f"A ScheduleWakeup tool_use carrying <loop-wake> in its prompt "
        f"input must SUPPRESS; got={stdout!r}"
    )


# ===========================================================================
# Spec verification item 4: SUPPRESSES given the HTML-escaped marker form.
# ===========================================================================

def test_suppresses_given_html_escaped_marker(tmp_path):
    engram_home = tmp_path / "engram"
    engram_home.mkdir()
    _write_loop_marker(engram_home)
    transcript = tmp_path / "transcript.jsonl"
    transcript.write_text("", encoding="utf-8")

    _run_hook(_make_stop_payload(str(transcript)), engram_home)

    escaped_prompt = html.escape("<loop-wake>") + "\nLoop tick."
    assert "&lt;loop-wake&gt;" in escaped_prompt  # sanity on the fixture itself

    sw, cl = _batched_arm_records(
        "toolu_test_escaped", "toolu_test_escaped_cl", escaped_prompt, "msg_test_arm_2"
    )
    _append_lines(transcript, sw, cl)

    stdout, rc = _run_hook(_make_stop_payload(str(transcript)), engram_home)

    assert rc == 0
    assert stdout == "", (
        f"An HTML-escaped &lt;loop-wake&gt; marker must satisfy the gate "
        f"identically to the literal form (must SUPPRESS); got={stdout!r}"
    )


# ===========================================================================
# Spec verification item 5: does NOT fire with no loop-mode.json.
# ===========================================================================

def test_does_not_fire_without_loop_mode_marker(tmp_path):
    engram_home = tmp_path / "engram"
    engram_home.mkdir()
    # Deliberately do NOT write loop-mode.json.
    transcript = tmp_path / "transcript.jsonl"
    transcript.write_text("", encoding="utf-8")

    _run_hook(_make_stop_payload(str(transcript)), engram_home)

    # Delta with prose and no ScheduleWakeup call -- would EMIT if
    # loop-mode were active.
    _append_lines(
        transcript,
        json.dumps({"type": "assistant", "message": {"content": [
            {"type": "text", "text": "Some prose."}
        ]}}, separators=(",", ":")),
    )

    stdout, rc = _run_hook(_make_stop_payload(str(transcript)), engram_home)

    assert rc == 0
    assert stdout == "", (
        f"No loop-mode.json must never nag (empty stdout); got={stdout!r}"
    )


# ===========================================================================
# Round-6 review fix -- pacer gating. The nudge's own instruction text
# ("Arm now — ScheduleWakeup...") is scheduleWakeup-specific, so firing it
# at a loop this hook can't positively identify as scheduleWakeup-paced
# risks telling the agent to take an actively wrong action (mixing pacers
# is a real, previously-hit double-fire bug). The gate must fire ONLY when
# `pacer` is (case/whitespace-normalized) "scheduleWakeup"; anything else
# is a silent no-op, even with no ScheduleWakeup call in the delta at all.
# ===========================================================================

def test_schedulewakeup_pacer_still_fires_with_no_call(tmp_path):
    """Existing behaviour preserved: an explicit pacer="scheduleWakeup"
    marker with no ScheduleWakeup call in the delta must still fire."""
    engram_home = tmp_path / "engram"
    engram_home.mkdir()
    _write_loop_marker(engram_home, pacer="scheduleWakeup")
    transcript = tmp_path / "transcript.jsonl"
    transcript.write_text("", encoding="utf-8")

    _run_hook(_make_stop_payload(str(transcript)), engram_home)

    _append_lines(
        transcript,
        json.dumps({"type": "assistant", "message": {"content": [
            {"type": "text", "text": "Just some prose, no arm check."}
        ]}}, separators=(",", ":")),
    )

    stdout, rc = _run_hook(_make_stop_payload(str(transcript)), engram_home)

    assert rc == 0
    assert _nudge_emitted(stdout), (
        f"pacer='scheduleWakeup' with no ScheduleWakeup call must still "
        f"EMIT; stdout={stdout!r}"
    )


def test_cron_paced_marker_does_not_fire_even_with_no_schedulewakeup_call(tmp_path):
    """A cron/other-paced marker must NOT fire, even though the delta has
    no ScheduleWakeup call at all -- the OLD (pre-round-6) behaviour would
    have nagged every turn on a cron-paced loop; this is the bug being
    fixed."""
    engram_home = tmp_path / "engram"
    engram_home.mkdir()
    _write_loop_marker(engram_home, pacer="cron")
    transcript = tmp_path / "transcript.jsonl"
    transcript.write_text("", encoding="utf-8")

    _run_hook(_make_stop_payload(str(transcript)), engram_home)

    _append_lines(
        transcript,
        json.dumps({"type": "assistant", "message": {"content": [
            {"type": "text", "text": "Just some prose, no arm check."}
        ]}}, separators=(",", ":")),
    )

    stdout, rc = _run_hook(_make_stop_payload(str(transcript)), engram_home)

    assert rc == 0
    assert stdout == "", (
        f"A cron-paced marker must NOT fire, regardless of delta content; "
        f"got={stdout!r}"
    )


def test_marker_with_no_pacer_key_does_not_fire(tmp_path):
    """A marker with no "pacer" key at all (can't positively identify the
    pacer) must NOT fire -- missing pacer fails toward silence here, the
    one deliberate exception to this hook's fail-toward-emitting policy."""
    engram_home = tmp_path / "engram"
    engram_home.mkdir()
    _write_loop_marker(engram_home, omit_pacer=True)
    transcript = tmp_path / "transcript.jsonl"
    transcript.write_text("", encoding="utf-8")

    _run_hook(_make_stop_payload(str(transcript)), engram_home)

    _append_lines(
        transcript,
        json.dumps({"type": "assistant", "message": {"content": [
            {"type": "text", "text": "Just some prose, no arm check."}
        ]}}, separators=(",", ":")),
    )

    stdout, rc = _run_hook(_make_stop_payload(str(transcript)), engram_home)

    assert rc == 0
    assert stdout == "", (
        f"A marker with no pacer key must NOT fire; got={stdout!r}"
    )


def test_pacer_comparison_is_case_insensitive(tmp_path):
    """pacer="ScheduleWakeup" (different case than the canonical
    "scheduleWakeup") must still fire -- the comparison is
    case-insensitive and whitespace-tolerant."""
    engram_home = tmp_path / "engram"
    engram_home.mkdir()
    _write_loop_marker(engram_home, pacer="ScheduleWakeup")
    transcript = tmp_path / "transcript.jsonl"
    transcript.write_text("", encoding="utf-8")

    _run_hook(_make_stop_payload(str(transcript)), engram_home)

    _append_lines(
        transcript,
        json.dumps({"type": "assistant", "message": {"content": [
            {"type": "text", "text": "Just some prose, no arm check."}
        ]}}, separators=(",", ":")),
    )

    stdout, rc = _run_hook(_make_stop_payload(str(transcript)), engram_home)

    assert rc == 0
    assert _nudge_emitted(stdout), (
        f"pacer='ScheduleWakeup' (different case) must still fire; "
        f"stdout={stdout!r}"
    )


# ===========================================================================
# Spec verification item 6: a ScheduleWakeup call with no <loop-wake>
# marker does NOT suppress, even though it IS a real ScheduleWakeup call.
# ===========================================================================

def test_unrelated_schedulewakeup_call_does_not_suppress(tmp_path):
    engram_home = tmp_path / "engram"
    engram_home.mkdir()
    _write_loop_marker(engram_home)
    transcript = tmp_path / "transcript.jsonl"
    transcript.write_text("", encoding="utf-8")

    _run_hook(_make_stop_payload(str(transcript)), engram_home)

    call = _schedule_wakeup_call("toolu_test_unrelated", _UNRELATED_PROMPT)
    _append_lines(transcript, call)

    stdout, rc = _run_hook(_make_stop_payload(str(transcript)), engram_home)

    assert rc == 0
    assert _nudge_emitted(stdout), (
        f"A ScheduleWakeup call with no <loop-wake> marker in its prompt "
        f"must NOT suppress -- the nudge must still fire; stdout={stdout!r}"
    )


# ===========================================================================
# Round-3 review fix -- tool-identity requirement, ported to the tool_use
# shape. The gate previously matched on tool_result TEXT alone; these are
# the required tests from the round-3 blocker, re-derived for tool_use
# detection: a decoy call/result pair carrying the marker in text that
# ISN'T a ScheduleWakeup call (must fire), a correctly-identified call
# (must suppress, covered above), and an unknown tool name whose `input`
# happens to look exactly like a valid arm (must fire).
# ===========================================================================

def test_bash_echo_carrying_marker_and_pending_word_does_not_suppress(tmp_path):
    """Round-3 finding, payload 2, ported: a Bash tool_use/tool_result pair
    whose echoed text happens to contain both the marker and a pending-job
    word must NOT suppress -- it is not a ScheduleWakeup call, regardless
    of its text."""
    engram_home = tmp_path / "engram"
    engram_home.mkdir()
    _write_loop_marker(engram_home)
    transcript = tmp_path / "transcript.jsonl"
    transcript.write_text("", encoding="utf-8")

    _run_hook(_make_stop_payload(str(transcript)), engram_home)

    bash_text = "<loop-wake> this text is scheduled and came from an echo, not arming"
    tool_use = _tool_use_record(
        "toolu_test_bash", "Bash", {"command": f"echo '{bash_text}'"}
    )
    tool_result = _tool_result_record("toolu_test_bash", bash_text)
    _append_lines(transcript, tool_use, tool_result)

    stdout, rc = _run_hook(_make_stop_payload(str(transcript)), engram_home)

    assert rc == 0
    assert _nudge_emitted(stdout), (
        f"A Bash call/result pair carrying the marker in its echoed text "
        f"must NOT suppress -- Bash is not the wake-arming tool; "
        f"stdout={stdout!r}"
    )


def test_read_of_own_source_file_does_not_suppress(tmp_path):
    """Round-3 finding, payload 3 (the one that matters), ported: a Read
    tool_use/tool_result pair whose result is (a chunk of) THIS HOOK'S OWN
    SOURCE FILE -- which unavoidably contains '<loop-wake>' and words like
    'pending' throughout its docstring -- must NOT suppress. No adversarial
    intent required: an agent doing ordinary maintenance reading on this
    file must not silently disable the safety net for that turn."""
    engram_home = tmp_path / "engram"
    engram_home.mkdir()
    _write_loop_marker(engram_home)
    transcript = tmp_path / "transcript.jsonl"
    transcript.write_text("", encoding="utf-8")

    _run_hook(_make_stop_payload(str(transcript)), engram_home)

    own_source = HOOK_PATH.read_text(encoding="utf-8")[:3000]
    assert "<loop-wake>" in own_source  # sanity: this IS the adversarial-by-accident case
    tool_use = _tool_use_record(
        "toolu_test_read", "Read", {"file_path": str(HOOK_PATH)}
    )
    tool_result = _tool_result_record("toolu_test_read", own_source)
    _append_lines(transcript, tool_use, tool_result)

    stdout, rc = _run_hook(_make_stop_payload(str(transcript)), engram_home)

    assert rc == 0
    assert _nudge_emitted(stdout), (
        f"A Read of this hook's own source file must NOT suppress, even "
        f"though its docstring contains the marker and pending-job words "
        f"throughout; stdout={stdout!r}"
    )


def test_unknown_tool_name_with_valid_looking_prompt_does_not_suppress(tmp_path):
    """A tool_use whose `name` is NOT in `_WAKE_ARMING_TOOL_NAMES` must NOT
    suppress -- even though its `input.prompt` looks exactly like a valid
    loop-wake arming prompt. Tool identity is required, an input shape
    alone is not enough."""
    engram_home = tmp_path / "engram"
    engram_home.mkdir()
    _write_loop_marker(engram_home)
    transcript = tmp_path / "transcript.jsonl"
    transcript.write_text("", encoding="utf-8")

    _run_hook(_make_stop_payload(str(transcript)), engram_home)

    call = _tool_use_record(
        "toolu_test_unknown_tool", "SomeOtherTool", {"prompt": _REAL_LOOP_PROMPT}
    )
    _append_lines(transcript, call)

    stdout, rc = _run_hook(_make_stop_payload(str(transcript)), engram_home)

    assert rc == 0
    assert _nudge_emitted(stdout), (
        f"An unknown tool name carrying a valid-looking prompt must NOT "
        f"suppress -- tool identity is required, prompt shape alone is not "
        f"enough; stdout={stdout!r}"
    )


def test_schedulewakeup_call_with_no_prompt_field_does_not_suppress(tmp_path):
    """A ScheduleWakeup call whose `input` is missing the `prompt` field
    entirely (e.g. a bare probe call with only other args) must NOT
    suppress -- there is no text to find the marker in."""
    engram_home = tmp_path / "engram"
    engram_home.mkdir()
    _write_loop_marker(engram_home)
    transcript = tmp_path / "transcript.jsonl"
    transcript.write_text("", encoding="utf-8")

    _run_hook(_make_stop_payload(str(transcript)), engram_home)

    call = _tool_use_record("toolu_test_no_prompt", "ScheduleWakeup", {"seconds": 1800})
    _append_lines(transcript, call)

    stdout, rc = _run_hook(_make_stop_payload(str(transcript)), engram_home)

    assert rc == 0
    assert _nudge_emitted(stdout), (
        f"A ScheduleWakeup call with no prompt field must NOT suppress; "
        f"stdout={stdout!r}"
    )


def test_tool_use_in_earlier_delta_does_not_bleed_into_later_turn(tmp_path):
    """A ScheduleWakeup call seen on an EARLIER fire's delta must not
    satisfy a LATER turn's check -- confirmation is scoped to the SAME
    delta, matching the byte-offset delta-isolation discipline (an
    out-of-delta tool_use is exactly as unconfirming as no call at all)."""
    engram_home = tmp_path / "engram"
    engram_home.mkdir()
    _write_loop_marker(engram_home)
    transcript = tmp_path / "transcript.jsonl"
    transcript.write_text("", encoding="utf-8")

    _run_hook(_make_stop_payload(str(transcript)), engram_home)

    # Turn 1's delta: a confirming batched arm (SW + CronList, same message.id).
    sw, cl = _batched_arm_records(
        "toolu_split_sw", "toolu_split_cl", _REAL_LOOP_PROMPT, "msg_split_arm"
    )
    _append_lines(transcript, sw, cl)
    stdout1, rc1 = _run_hook(_make_stop_payload(str(transcript)), engram_home)
    assert rc1 == 0
    assert stdout1 == "", f"Turn 1 with a confirming batched arm must suppress; got={stdout1!r}"

    # Turn 2's delta: no new ScheduleWakeup call, just prose. Turn 1's
    # call must not bleed forward and suppress this turn too.
    _append_lines(
        transcript,
        json.dumps({"type": "assistant", "message": {"content": [
            {"type": "text", "text": "Doing more work, no arm this turn."}
        ]}}, separators=(",", ":")),
    )
    stdout2, rc2 = _run_hook(_make_stop_payload(str(transcript)), engram_home)
    assert rc2 == 0
    assert _nudge_emitted(stdout2), (
        f"A ScheduleWakeup call from an earlier delta must not suppress "
        f"this later turn; stdout={stdout2!r}"
    )


def test_codex_wrong_schedulewakeup_marker_gets_managed_repair_not_claude_arm(tmp_path):
    """#2508: Codex must not be told to call Claude's wake tool when a stale
    generic boot charge wrote the wrong pacer into loop-mode.json."""
    engram_home = tmp_path / "engram"
    engram_home.mkdir()
    _write_loop_marker(engram_home, pacer="scheduleWakeup")
    transcript = tmp_path / "transcript.jsonl"
    transcript.write_text("", encoding="utf-8")

    stdout, rc = _run_hook(
        _make_stop_payload(str(transcript)),
        engram_home,
        extra_env={"CODEX_PLUGIN_ROOT": str(tmp_path / "codex-plugin")},
    )

    assert rc == 0
    parsed = json.loads(stdout)
    assert parsed["decision"] == "block"
    reason = parsed["reason"]
    assert "managed Codex" in reason
    assert "codex-umwelt-wake.py" in reason
    assert "systemd heartbeat" in reason
    assert "Arm now — ScheduleWakeup" not in reason


def test_colocated_claude_profile_wins_over_stale_codex_plugin_root(tmp_path):
    """#2508: a stale Codex root must not reclassify a Claude hook bundle."""
    engram_home = tmp_path / "engram"
    engram_home.mkdir()
    _write_loop_marker(engram_home, pacer="scheduleWakeup")
    transcript = tmp_path / "transcript.jsonl"
    transcript.write_text("", encoding="utf-8")
    _claude_bundle, hook_path = _copy_hook_into_baked_bundle(tmp_path, "claude-code")
    codex_bundle, _unused = _copy_hook_into_baked_bundle(tmp_path, "codex")

    stdout, rc = _run_hook(
        _make_stop_payload(str(transcript)),
        engram_home,
        extra_env={"CODEX_PLUGIN_ROOT": str(codex_bundle)},
        hook_path=hook_path,
    )

    assert rc == 0
    parsed = json.loads(stdout)
    assert parsed["decision"] == "block"
    assert "ScheduleWakeup(delay=cadence_seconds)" in parsed["reason"]
    assert "managed Codex" not in parsed["reason"]


def test_colocated_codex_profile_wins_over_stale_claude_plugin_root(tmp_path):
    """#2508: a stale Claude root must not reclassify a Codex hook bundle."""
    engram_home = tmp_path / "engram"
    engram_home.mkdir()
    _write_loop_marker(engram_home, pacer="scheduleWakeup")
    transcript = tmp_path / "transcript.jsonl"
    transcript.write_text("", encoding="utf-8")
    claude_bundle, _unused = _copy_hook_into_baked_bundle(tmp_path, "claude-code")
    _codex_bundle, hook_path = _copy_hook_into_baked_bundle(tmp_path, "codex")

    stdout, rc = _run_hook(
        _make_stop_payload(str(transcript)),
        engram_home,
        extra_env={"CLAUDE_PLUGIN_ROOT": str(claude_bundle)},
        hook_path=hook_path,
    )

    assert rc == 0
    parsed = json.loads(stdout)
    assert parsed["decision"] == "block"
    assert "managed Codex" in parsed["reason"]
    assert "Arm now — ScheduleWakeup" not in parsed["reason"]


# ===========================================================================
# Round-3 review fix -- lib-import-failure fallback (blocker 2). With
# _stophooklib unavailable, the hook must still emit rather than go silent.
# ===========================================================================

def test_emits_via_fallback_when_stophooklib_import_fails(tmp_path):
    """Force _stophooklib to be unimportable and confirm the hook still
    emits the nudge via its inline fallback envelope, rather than silently
    returning nothing -- this is the one failure mode this hook must never
    have, since its entire purpose is making silence loud."""
    engram_home = tmp_path / "engram"
    engram_home.mkdir()
    _write_loop_marker(engram_home)
    transcript = tmp_path / "transcript.jsonl"
    transcript.write_text("", encoding="utf-8")

    # Run in an isolated cwd/PYTHONPATH so `from _stophooklib import ...`
    # fails: copy ONLY the hook (not its sibling _stophooklib.py) into a
    # scratch directory and run it from there.
    scratch = tmp_path / "isolated_hook_dir"
    scratch.mkdir()
    isolated_hook = scratch / "engram-loop-arm-stop-hook.py"
    isolated_hook.write_text(HOOK_PATH.read_text(encoding="utf-8"), encoding="utf-8")

    env = dict(os.environ)
    env["ENGRAM_HOME"] = str(engram_home)
    # Prevent PYTHONPATH from accidentally making the real _stophooklib.py
    # importable in the isolated run.
    env.pop("PYTHONPATH", None)

    import subprocess as _subprocess
    result = _subprocess.run(
        [PYTHON, str(isolated_hook)],
        input=_make_stop_payload(str(transcript)),
        capture_output=True,
        text=True,
        env=env,
        timeout=15,
    )

    assert result.returncode == 0
    assert _nudge_emitted(result.stdout.strip()), (
        f"With _stophooklib unimportable, the hook must still emit via its "
        f"inline fallback envelope; stdout={result.stdout!r} "
        f"stderr={result.stderr!r}"
    )


# ===========================================================================
# Supporting regression tests (not in the spec's 6, added for completeness)
# ===========================================================================


def test_fallback_marker_equals_loop_prompt_marker():
    """SSoT guard (mirrors #1592's guard for engram-time-bar-hook.py): the
    hook's hardcoded fallback marker must equal loop_prompt.LOOP_WAKE_MARKER."""
    import importlib.util

    tools_dir = Path(__file__).parent.parent / "src" / "engram" / "tools"
    lp_spec = importlib.util.spec_from_file_location(
        "loop_prompt_test_instance", tools_dir / "loop_prompt.py"
    )
    lp = importlib.util.module_from_spec(lp_spec)
    lp_spec.loader.exec_module(lp)

    hook_spec = importlib.util.spec_from_file_location(
        "loop_arm_hook_test_instance", HOOK_PATH
    )
    hook_mod = importlib.util.module_from_spec(hook_spec)
    hook_spec.loader.exec_module(hook_mod)

    assert hook_mod._LOOP_WAKE_MARKER_FALLBACK == lp.LOOP_WAKE_MARKER


def test_stop_hook_active_refire_is_not_renudged(tmp_path):
    """A same-turn Stop-hook re-fire (stop_hook_active=True, as happens when
    another Stop hook in the array returns a blocking decision -- notably
    engram-stop-hook.py's own write-nudge, which is what makes this guard
    NECESSARY rather than merely defensive) must not re-emit the nudge --
    guards against nag-spam within one logical turn."""
    engram_home = tmp_path / "engram"
    engram_home.mkdir()
    _write_loop_marker(engram_home)
    transcript = tmp_path / "transcript.jsonl"
    transcript.write_text("", encoding="utf-8")

    stdout, rc = _run_hook(
        _make_stop_payload(str(transcript), stop_hook_active=True), engram_home
    )

    assert rc == 0
    assert stdout == "", (
        f"stop_hook_active=True re-fire must be a silent no-op; got={stdout!r}"
    )


def test_no_state_touch_when_loop_mode_absent(tmp_path):
    """No loop-mode.json -> the hook's own state file must not be created
    (stateless gate, mirrors engram-stop-hook.py's maturity-gate discipline)."""
    engram_home = tmp_path / "engram"
    engram_home.mkdir()
    transcript = tmp_path / "transcript.jsonl"
    transcript.write_text("", encoding="utf-8")

    _run_hook(_make_stop_payload(str(transcript)), engram_home)

    assert not (engram_home / ".loop-arm-nudge-last-fire.json").exists()


def test_first_fire_on_fresh_transcript_with_confirmation_already_present_suppresses(tmp_path):
    """First-ever fire (no prior state) on a transcript that ALREADY
    contains a confirming ScheduleWakeup call (e.g. it happened earlier in
    the same brand-new transcript) must suppress -- the delta starts from
    byte 0 on first fire, not 'unknown -> always emit'."""
    engram_home = tmp_path / "engram"
    engram_home.mkdir()
    _write_loop_marker(engram_home)
    transcript = tmp_path / "transcript.jsonl"

    sw, cl = _batched_arm_records(
        "toolu_test_firstfire_sw", "toolu_test_firstfire_cl",
        _REAL_LOOP_PROMPT, "msg_firstfire_arm"
    )
    transcript.write_text(sw + "\n" + cl + "\n", encoding="utf-8")

    stdout, rc = _run_hook(_make_stop_payload(str(transcript)), engram_home)

    assert rc == 0
    assert stdout == "", (
        f"First fire on a transcript that already contains a confirming "
        f"ScheduleWakeup call must SUPPRESS; got={stdout!r}"
    )


def test_delta_isolation_prior_confirmation_does_not_bleed_into_later_turn(tmp_path):
    """A ScheduleWakeup confirmation seen on turn 1 must not satisfy turn
    2's check if turn 2 produced no new confirmation -- byte-offset delta
    scoping must prevent an old confirmation from silently covering a
    later turn that never re-armed."""
    engram_home = tmp_path / "engram"
    engram_home.mkdir()
    _write_loop_marker(engram_home)
    transcript = tmp_path / "transcript.jsonl"
    transcript.write_text("", encoding="utf-8")

    # Fire 1: establish state.
    _run_hook(_make_stop_payload(str(transcript)), engram_home)

    # Turn 1: confirmed via batched arm — SW + CronList, same message.id.
    sw, cl = _batched_arm_records(
        "toolu_test_turn1_sw", "toolu_test_turn1_cl",
        _REAL_LOOP_PROMPT, "msg_turn1_arm"
    )
    _append_lines(transcript, sw, cl)
    stdout1, rc1 = _run_hook(_make_stop_payload(str(transcript)), engram_home)
    assert rc1 == 0
    assert stdout1 == "", f"Turn 1 with batched arm must suppress; got={stdout1!r}"

    # Turn 2: no new confirmation, just prose.
    _append_lines(
        transcript,
        json.dumps({"type": "assistant", "message": {"content": [
            {"type": "text", "text": "Doing more work, no arm this turn."}
        ]}}, separators=(",", ":")),
    )
    stdout2, rc2 = _run_hook(_make_stop_payload(str(transcript)), engram_home)
    assert rc2 == 0
    assert _nudge_emitted(stdout2), (
        f"Turn 2 with no new confirmation must EMIT -- the turn-1 "
        f"confirmation must not bleed into turn 2's delta; stdout={stdout2!r}"
    )
