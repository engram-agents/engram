"""Tests for hooks/claude/engram-loop-arm-stop-hook.py.

2026-07-31-loop-arm-nudge-SPEC.md, PR-B. Spec verification items 2-6 (item
1 -- "existing engram-stop-hook tests pass unchanged" -- is a PR-A
requirement, verified separately; see PR-A's own test run), plus the
round-3 tool-identity-correlation fix's own required tests (a real hole
the round-3 review found and reproduced: the gate previously matched on
TEXT CONTENT alone, so any tool_result -- e.g. a Bash echo, or a Read of
this hook's own source file, which unavoidably contains the marker and
pending-job words throughout its docstring -- could silently suppress the
nudge), plus the round-6 pacer-gating fix's required tests (a colleague
review asked what happens on a non-scheduleWakeup-paced loop or a
non-Claude install: the hook previously fired on loop-mode.json's mere
EXISTENCE, never reading its "pacer" field, so a cron-paced loop -- which
has no reason to run CronList on any given turn -- got nagged every single
turn with an instruction to ScheduleWakeup, which is actively wrong for
that pacer), plus supporting regression tests (marker-fallback
consistency, stop_hook_active re-fire guard, delta isolation,
lib-import-failure fallback).

Invocation pattern mirrors tests/test_stop_hook_prose_gate.py: run the hook
as a real subprocess against a synthetic ENGRAM_HOME + transcript, so the
test exercises the exact stdin/stdout contract Claude Code uses.
"""

from __future__ import annotations

import html
import json
import os
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


def _run_hook(stdin_payload: str, engram_home: Path) -> tuple[str, int]:
    """Run the hook, return (stdout_stripped, returncode)."""
    env = dict(os.environ)
    env["ENGRAM_HOME"] = str(engram_home)
    result = subprocess.run(
        [PYTHON, str(HOOK_PATH)],
        input=stdin_payload,
        capture_output=True,
        text=True,
        env=env,
        timeout=15,
    )
    return result.stdout.strip(), result.returncode


def _nudge_emitted(stdout: str) -> bool:
    """Return True if the hook emitted the loop-arm nudge on this invocation."""
    if not stdout:
        return False
    try:
        parsed = json.loads(stdout)
        ctx = parsed.get("hookSpecificOutput", {}).get("additionalContext", "")
        return "Loop Arm Check" in ctx
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


def _tool_use_record(tool_id: str, name: str) -> str:
    """Build a compact-JSON assistant-role record with a single tool_use block."""
    return json.dumps(
        {
            "type": "assistant",
            "message": {
                "content": [
                    {"type": "tool_use", "id": tool_id, "name": name, "input": {}}
                ]
            },
        },
        separators=(",", ":"),
    )


def _tool_result_record(tool_id: str, text: str) -> str:
    """Build a compact-JSON user-role record with a single tool_result block,
    correlated to `tool_id` via `tool_use_id` -- matches the real
    tool_use/tool_result correlation shape, NOT just text content."""
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


def _correlated_pair(tool_id: str, name: str, text: str) -> tuple[str, str]:
    """A (tool_use, tool_result) record pair sharing `tool_id`, in the order
    they'd actually appear in a transcript (tool_use first)."""
    return _tool_use_record(tool_id, name), _tool_result_record(tool_id, text)


# A realistic CronList rendering carrying the loop-wake marker (spec fact
# #3's example, lightly paraphrased).
_REAL_CRON_LISTING = (
    "60ac4983 — Every day at 6:09 PM (one-shot) [session-only]: <loop-wake>\n"
    "<loop-meta {\"kind\":\"working\"}>\nLoop tick."
)

# A cron listing for an UNRELATED job -- correctly a CronList result, but
# NOT the loop-wake marker.
_UNRELATED_CRON_LISTING = (
    "aa11bb22 — Every day at 9:00 AM (one-shot) [session-only]: "
    "Check the mailbox for new letters."
)


# ===========================================================================
# Spec verification item 2: nudge FIRES with loop marker present and no
# cron result in the delta.
# ===========================================================================

def test_nudge_fires_with_loop_marker_and_no_cron_result(tmp_path):
    engram_home = tmp_path / "engram"
    engram_home.mkdir()
    _write_loop_marker(engram_home)
    transcript = tmp_path / "transcript.jsonl"
    transcript.write_text("", encoding="utf-8")

    # Fire 1: establish state at EOF (empty transcript).
    _run_hook(_make_stop_payload(str(transcript)), engram_home)

    # Delta: some ordinary assistant text, no tool_result at all.
    _append_lines(
        transcript,
        json.dumps({"type": "assistant", "message": {"content": [
            {"type": "text", "text": "Just some prose, no cron check."}
        ]}}, separators=(",", ":")),
    )

    stdout, rc = _run_hook(_make_stop_payload(str(transcript)), engram_home)

    assert rc == 0
    assert _nudge_emitted(stdout), (
        f"Loop marker present + no cron result in delta must EMIT the nudge; "
        f"stdout={stdout!r}"
    )


# ===========================================================================
# Spec verification item 3: SUPPRESSES given a tool_result containing a
# "[session-only] ... <loop-wake>" line -- CORRECTLY CORRELATED to a
# CronList tool_use (this is also the round-3 fix's core positive case).
# ===========================================================================

def test_suppresses_given_correlated_cronlist_result_with_marker(tmp_path):
    engram_home = tmp_path / "engram"
    engram_home.mkdir()
    _write_loop_marker(engram_home)
    transcript = tmp_path / "transcript.jsonl"
    transcript.write_text("", encoding="utf-8")

    _run_hook(_make_stop_payload(str(transcript)), engram_home)

    tool_use, tool_result = _correlated_pair(
        "toolu_test_cronlist", "CronList", _REAL_CRON_LISTING
    )
    _append_lines(transcript, tool_use, tool_result)

    stdout, rc = _run_hook(_make_stop_payload(str(transcript)), engram_home)

    assert rc == 0
    assert stdout == "", (
        f"A CronList tool_result (correctly correlated via tool_use_id) "
        f"carrying <loop-wake> must SUPPRESS; got={stdout!r}"
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

    escaped_listing = (
        "60ac4983 — Every day at 6:09 PM (one-shot) [session-only]: "
        + html.escape("<loop-wake>")
        + "\nLoop tick."
    )
    assert "&lt;loop-wake&gt;" in escaped_listing  # sanity on the fixture itself

    tool_use, tool_result = _correlated_pair(
        "toolu_test_escaped", "CronList", escaped_listing
    )
    _append_lines(transcript, tool_use, tool_result)

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

    # Delta with prose and no cron result -- would EMIT if loop-mode were active.
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
# ("Re-arm now — ScheduleWakeup...") is scheduleWakeup-specific, so firing
# it at a loop this hook can't positively identify as scheduleWakeup-paced
# risks telling the agent to take an actively wrong action (mixing pacers
# is a real, previously-hit double-fire bug). The gate must fire ONLY when
# `pacer` is (case/whitespace-normalized) "scheduleWakeup"; anything else
# is a silent no-op, even with no CronList confirmation in the delta at all.
# ===========================================================================

def test_schedulewakeup_pacer_still_fires_with_no_cron_result(tmp_path):
    """Existing behaviour preserved: an explicit pacer="scheduleWakeup"
    marker with no CronList confirmation in the delta must still fire."""
    engram_home = tmp_path / "engram"
    engram_home.mkdir()
    _write_loop_marker(engram_home, pacer="scheduleWakeup")
    transcript = tmp_path / "transcript.jsonl"
    transcript.write_text("", encoding="utf-8")

    _run_hook(_make_stop_payload(str(transcript)), engram_home)

    _append_lines(
        transcript,
        json.dumps({"type": "assistant", "message": {"content": [
            {"type": "text", "text": "Just some prose, no cron check."}
        ]}}, separators=(",", ":")),
    )

    stdout, rc = _run_hook(_make_stop_payload(str(transcript)), engram_home)

    assert rc == 0
    assert _nudge_emitted(stdout), (
        f"pacer='scheduleWakeup' with no cron result must still EMIT; "
        f"stdout={stdout!r}"
    )


def test_cron_paced_marker_does_not_fire_even_with_no_cronlist(tmp_path):
    """A cron/other-paced marker must NOT fire, even though the delta has
    no CronList confirmation at all -- the OLD (pre-round-6) behaviour
    would have nagged every turn on a cron-paced loop; this is the bug
    being fixed."""
    engram_home = tmp_path / "engram"
    engram_home.mkdir()
    _write_loop_marker(engram_home, pacer="cron")
    transcript = tmp_path / "transcript.jsonl"
    transcript.write_text("", encoding="utf-8")

    _run_hook(_make_stop_payload(str(transcript)), engram_home)

    _append_lines(
        transcript,
        json.dumps({"type": "assistant", "message": {"content": [
            {"type": "text", "text": "Just some prose, no cron check."}
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
            {"type": "text", "text": "Just some prose, no cron check."}
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
            {"type": "text", "text": "Just some prose, no cron check."}
        ]}}, separators=(",", ":")),
    )

    stdout, rc = _run_hook(_make_stop_payload(str(transcript)), engram_home)

    assert rc == 0
    assert _nudge_emitted(stdout), (
        f"pacer='ScheduleWakeup' (different case) must still fire; "
        f"stdout={stdout!r}"
    )


# ===========================================================================
# Spec verification item 6: an unrelated cron entry (no <loop-wake>) does
# NOT suppress, even though it IS a correctly-correlated CronList result.
# ===========================================================================

def test_unrelated_cron_entry_does_not_suppress(tmp_path):
    engram_home = tmp_path / "engram"
    engram_home.mkdir()
    _write_loop_marker(engram_home)
    transcript = tmp_path / "transcript.jsonl"
    transcript.write_text("", encoding="utf-8")

    _run_hook(_make_stop_payload(str(transcript)), engram_home)

    tool_use, tool_result = _correlated_pair(
        "toolu_test_unrelated", "CronList", _UNRELATED_CRON_LISTING
    )
    _append_lines(transcript, tool_use, tool_result)

    stdout, rc = _run_hook(_make_stop_payload(str(transcript)), engram_home)

    assert rc == 0
    assert _nudge_emitted(stdout), (
        f"A correctly-correlated CronList result with no <loop-wake> marker "
        f"must NOT suppress -- the nudge must still fire; stdout={stdout!r}"
    )


# ===========================================================================
# Round-3 review fix -- tool-identity correlation. The gate previously
# matched on tool_result TEXT alone; these are the four required tests from
# the round-3 blocker: the two payloads that falsely suppressed before the
# fix (now must fire), a correctly-correlated result (must suppress,
# covered above), an unknown tool name (must fire), and an unresolvable
# tool_use_id (must fire).
# ===========================================================================

def test_bash_echo_carrying_marker_and_pending_word_does_not_suppress(tmp_path):
    """Round-3 finding, payload 2: a Bash tool_result whose echoed text
    happens to contain both the marker and a pending-job word must NOT
    suppress -- it is not a CronList result, regardless of its text."""
    engram_home = tmp_path / "engram"
    engram_home.mkdir()
    _write_loop_marker(engram_home)
    transcript = tmp_path / "transcript.jsonl"
    transcript.write_text("", encoding="utf-8")

    _run_hook(_make_stop_payload(str(transcript)), engram_home)

    bash_text = "<loop-wake> this text is scheduled and came from an echo, not cron"
    tool_use, tool_result = _correlated_pair("toolu_test_bash", "Bash", bash_text)
    _append_lines(transcript, tool_use, tool_result)

    stdout, rc = _run_hook(_make_stop_payload(str(transcript)), engram_home)

    assert rc == 0
    assert _nudge_emitted(stdout), (
        f"A Bash tool_result carrying the marker in its echoed text must "
        f"NOT suppress -- Bash is not the wake-listing tool; "
        f"stdout={stdout!r}"
    )


def test_read_of_own_source_file_does_not_suppress(tmp_path):
    """Round-3 finding, payload 3 (the one that matters): a Read tool_result
    whose content is (a chunk of) THIS HOOK'S OWN SOURCE FILE -- which
    unavoidably contains '<loop-wake>' and words like 'pending' throughout
    its docstring -- must NOT suppress. No adversarial intent required: an
    agent doing ordinary maintenance reading on this file must not silently
    disable the safety net for that turn."""
    engram_home = tmp_path / "engram"
    engram_home.mkdir()
    _write_loop_marker(engram_home)
    transcript = tmp_path / "transcript.jsonl"
    transcript.write_text("", encoding="utf-8")

    _run_hook(_make_stop_payload(str(transcript)), engram_home)

    own_source = HOOK_PATH.read_text(encoding="utf-8")[:3000]
    assert "<loop-wake>" in own_source  # sanity: this IS the adversarial-by-accident case
    tool_use, tool_result = _correlated_pair("toolu_test_read", "Read", own_source)
    _append_lines(transcript, tool_use, tool_result)

    stdout, rc = _run_hook(_make_stop_payload(str(transcript)), engram_home)

    assert rc == 0
    assert _nudge_emitted(stdout), (
        f"A Read of this hook's own source file must NOT suppress, even "
        f"though its docstring contains the marker and pending-job words "
        f"throughout; stdout={stdout!r}"
    )


def test_unknown_tool_name_with_valid_looking_listing_does_not_suppress(tmp_path):
    """A tool_use_id that DOES resolve, but to a tool name that is not in
    _WAKE_LISTING_TOOL_NAMES, must NOT suppress -- even though the
    tool_result's text looks exactly like a valid CronList listing."""
    engram_home = tmp_path / "engram"
    engram_home.mkdir()
    _write_loop_marker(engram_home)
    transcript = tmp_path / "transcript.jsonl"
    transcript.write_text("", encoding="utf-8")

    _run_hook(_make_stop_payload(str(transcript)), engram_home)

    tool_use, tool_result = _correlated_pair(
        "toolu_test_unknown_tool", "SomeOtherTool", _REAL_CRON_LISTING
    )
    _append_lines(transcript, tool_use, tool_result)

    stdout, rc = _run_hook(_make_stop_payload(str(transcript)), engram_home)

    assert rc == 0
    assert _nudge_emitted(stdout), (
        f"An unknown tool name carrying a valid-looking listing must NOT "
        f"suppress -- tool identity is required, text alone is not enough; "
        f"stdout={stdout!r}"
    )


def test_unresolvable_tool_use_id_does_not_suppress(tmp_path):
    """A tool_result whose tool_use_id has no corresponding tool_use block
    anywhere in this delta must NOT suppress -- an uncorrelatable result
    fails toward emitting, never toward trusting it."""
    engram_home = tmp_path / "engram"
    engram_home.mkdir()
    _write_loop_marker(engram_home)
    transcript = tmp_path / "transcript.jsonl"
    transcript.write_text("", encoding="utf-8")

    _run_hook(_make_stop_payload(str(transcript)), engram_home)

    # tool_result only -- no matching tool_use in this delta at all.
    orphan_result = _tool_result_record("toolu_never_seen", _REAL_CRON_LISTING)
    _append_lines(transcript, orphan_result)

    stdout, rc = _run_hook(_make_stop_payload(str(transcript)), engram_home)

    assert rc == 0
    assert _nudge_emitted(stdout), (
        f"A tool_result with no resolvable tool_use_id must NOT suppress; "
        f"stdout={stdout!r}"
    )


def test_tool_use_in_earlier_delta_does_not_correlate(tmp_path):
    """A tool_use seen on an EARLIER fire's delta must not correlate with a
    tool_result appearing in a LATER delta -- correlation is scoped to
    the SAME delta, matching invariant 2 (an out-of-delta tool_use is
    exactly as unresolvable as a missing one)."""
    engram_home = tmp_path / "engram"
    engram_home.mkdir()
    _write_loop_marker(engram_home)
    transcript = tmp_path / "transcript.jsonl"
    transcript.write_text("", encoding="utf-8")

    _run_hook(_make_stop_payload(str(transcript)), engram_home)

    # Turn 1's delta: only the tool_use (no result yet).
    _append_lines(transcript, _tool_use_record("toolu_split", "CronList"))
    stdout1, rc1 = _run_hook(_make_stop_payload(str(transcript)), engram_home)
    assert rc1 == 0
    assert _nudge_emitted(stdout1), "Turn 1 (tool_use only, no result) must EMIT"

    # Turn 2's delta: the tool_result for that SAME id, but the tool_use
    # itself is no longer in THIS delta (it was consumed by turn 1's read).
    _append_lines(transcript, _tool_result_record("toolu_split", _REAL_CRON_LISTING))
    stdout2, rc2 = _run_hook(_make_stop_payload(str(transcript)), engram_home)
    assert rc2 == 0
    assert _nudge_emitted(stdout2), (
        f"A tool_result whose matching tool_use fell in an earlier delta "
        f"must NOT suppress this later turn; stdout={stdout2!r}"
    )


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
    contains a verified, correlated CronList result (e.g. it happened
    earlier in the same brand-new transcript) must suppress -- the delta
    starts from byte 0 on first fire, not 'unknown -> always emit'."""
    engram_home = tmp_path / "engram"
    engram_home.mkdir()
    _write_loop_marker(engram_home)
    transcript = tmp_path / "transcript.jsonl"

    tool_use, tool_result = _correlated_pair(
        "toolu_test_firstfire", "CronList", _REAL_CRON_LISTING
    )
    transcript.write_text(tool_use + "\n" + tool_result + "\n", encoding="utf-8")

    stdout, rc = _run_hook(_make_stop_payload(str(transcript)), engram_home)

    assert rc == 0
    assert stdout == "", (
        f"First fire on a transcript that already contains a verified, "
        f"correlated CronList result must SUPPRESS; got={stdout!r}"
    )


def test_delta_isolation_prior_confirmation_does_not_bleed_into_later_turn(tmp_path):
    """A CronList confirmation seen on turn 1 must not satisfy turn 2's
    check if turn 2 produced no new confirmation -- byte-offset delta
    scoping must prevent an old confirmation from silently covering a
    later turn that never re-armed."""
    engram_home = tmp_path / "engram"
    engram_home.mkdir()
    _write_loop_marker(engram_home)
    transcript = tmp_path / "transcript.jsonl"
    transcript.write_text("", encoding="utf-8")

    # Fire 1: establish state.
    _run_hook(_make_stop_payload(str(transcript)), engram_home)

    # Turn 1: confirmed, correlated cron result in the delta.
    tool_use, tool_result = _correlated_pair(
        "toolu_test_turn1", "CronList", _REAL_CRON_LISTING
    )
    _append_lines(transcript, tool_use, tool_result)
    stdout1, rc1 = _run_hook(_make_stop_payload(str(transcript)), engram_home)
    assert rc1 == 0
    assert stdout1 == "", f"Turn 1 with confirmation must suppress; got={stdout1!r}"

    # Turn 2: no new confirmation, just prose.
    _append_lines(
        transcript,
        json.dumps({"type": "assistant", "message": {"content": [
            {"type": "text", "text": "Doing more work, no cron check this turn."}
        ]}}, separators=(",", ":")),
    )
    stdout2, rc2 = _run_hook(_make_stop_payload(str(transcript)), engram_home)
    assert rc2 == 0
    assert _nudge_emitted(stdout2), (
        f"Turn 2 with no new confirmation must EMIT -- the turn-1 "
        f"confirmation must not bleed into turn 2's delta; stdout={stdout2!r}"
    )
