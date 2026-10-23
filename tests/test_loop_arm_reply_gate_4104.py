"""alpha #4104 -- loop-arm Stop hook: reply-delivery gate.

Replies were swallowed: with a loop marker live the hook forced an arm, the
harness told the agent to end the turn on the arm result, and the human's
answer never rendered (only a turn's FINAL assistant text renders). Contract
(#4104), decided from the transcript only:

* reply gate: a human prompt in the delta whose turn does not END on assistant
  text (a tool_use after the last text, or no text) -> one block with
  `[Reply Delivery Check:`; takes precedence over the arm check, and also
  fires on a confirmed-arm turn;
* a human-answering turn with its reply rendered and a wake still pending
  (latest loop-wake ScheduleWakeup due > 90 s after `now`, not yet consumed by
  a fired wake) needs no forced arm;
* every non-human turn (`<loop-wake>`, `<task-notification>`), every
  unparseable delta, and every other pacer/marker state behaves as before.

`now` is injectable through ENGRAM_LOOP_ARM_NOW_ISO (test seam).

Real subprocess against a synthetic ENGRAM_HOME + transcript, helper style of
tests/test_loop_arm_stop_hook.py.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))
from test_loop_arm_stop_hook import (  # noqa: E402
    HOOK_PATH,
    _make_stop_payload,
    _run_hook,
    _write_loop_marker,
)

# #4029: module-scope window (the sys.path insert above runs at import).
pytestmark = pytest.mark.usefixtures("isolate_hook_loads_module")

NOW = datetime(2026, 10, 23, 14, 0, 0, tzinfo=timezone.utc)
NOW_ISO = NOW.strftime("%Y-%m-%dT%H:%M:%S.000Z")
WAKE_PROMPT = "<loop-wake> heartbeat"
ENV = {"ENGRAM_LOOP_ARM_NOW_ISO": NOW_ISO}
STATE_NAME = ".loop-arm-nudge-last-fire.json"
HUMAN = "please summarise the thing"


def _ts(delta_minutes: float) -> str:
    return (NOW + timedelta(minutes=delta_minutes)).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def _j(obj: dict) -> str:
    return json.dumps(obj, separators=(",", ":"))


def user(body: str) -> str:
    return _j({"type": "user", "message": {"role": "user", "content": body}})


def tool_result(tool_id: str = "t") -> str:
    return _j({"type": "user", "message": {"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": tool_id, "content": "ok"}]}})


def text(msg_id: str, body: str = "here is the answer") -> str:
    return _j({"type": "assistant", "message": {"id": msg_id, "content": [
        {"type": "text", "text": body}]}})


def tool(msg_id: str, name: str, input_: dict | None = None, ts: str | None = None) -> str:
    rec = {"type": "assistant", "message": {"id": msg_id, "content": [
        {"type": "tool_use", "id": f"{msg_id}-{name}", "name": name, "input": input_ or {}}]}}
    if ts:
        rec["timestamp"] = ts
    return _j(rec)


def arm_pair(msg_id: str) -> list[str]:
    return [
        tool(msg_id, "ScheduleWakeup", {"delaySeconds": 1800, "prompt": WAKE_PROMPT}),
        tool(msg_id, "CronList"),
    ]


def run(tmp_path, prior, delta, *, marker=True, pacer="scheduleWakeup", active=False):
    """Write prior + delta, put the offset boundary between them, run the hook."""
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    if marker:
        _write_loop_marker(home, pacer=pacer)
    transcript = tmp_path / "t.jsonl"
    prior_bytes = "".join(ln + "\n" for ln in prior).encode()
    transcript.write_bytes(prior_bytes)
    (home / STATE_NAME).write_text(json.dumps(
        {"transcript_path": str(transcript), "byte_offset": len(prior_bytes)}))
    state_before = (home / STATE_NAME).read_text()
    with open(transcript, "a", encoding="utf-8") as f:
        for ln in delta:
            f.write(ln + "\n")
    out, rc = _run_hook(_make_stop_payload(str(transcript), stop_hook_active=active), home,
                        extra_env=ENV)
    assert rc == 0
    return out, home, state_before


def kind(out: str) -> str:
    if not out:
        return "none"
    reason = json.loads(out)["reason"]
    if reason.startswith("[Reply Delivery Check:"):
        return "reply"
    if "Loop Arm Check" in reason:
        return "nudge"
    return "other"


def _prior_arm(ts, delay=1800):
    inp = {"prompt": WAKE_PROMPT}
    if delay is not None:
        inp["delaySeconds"] = delay
    return tool("p1", "ScheduleWakeup", inp, ts=ts)


def test_a_text_then_parallel_arm_swallows_reply(tmp_path):
    out, _, _ = run(tmp_path, [], [user(HUMAN), text("m1"), *arm_pair("m2"), tool_result()])
    assert kind(out) == "reply"
    assert json.loads(out)["decision"] == "block"


def test_b_arm_then_final_text_ok(tmp_path):
    out, _, _ = run(tmp_path, [], [user(HUMAN), *arm_pair("m1"), tool_result(), text("m2")])
    assert kind(out) == "none"


def test_c_loop_wake_prompt_arm_no_text(tmp_path):
    out, _, _ = run(tmp_path, [], [user(WAKE_PROMPT), *arm_pair("m1"), tool_result()])
    assert kind(out) == "none"


def test_c2_task_notification_no_reply_gate(tmp_path):
    out, _, _ = run(tmp_path, [], [user("<task-notification>done</task-notification>"),
                                   tool("m1", "Read")])
    assert kind(out) != "reply"


def test_d_pending_wake_no_forced_arm(tmp_path):
    out, _, _ = run(tmp_path, [_prior_arm(_ts(-5))], [user(HUMAN), text("m1")])
    assert out == ""


def test_d_pending_wake_writes_offset(tmp_path):
    _, home, before = run(tmp_path, [_prior_arm(_ts(-5))], [user(HUMAN), text("m1")])
    after = json.loads((home / STATE_NAME).read_text())
    assert after["byte_offset"] == (tmp_path / "t.jsonl").stat().st_size
    assert json.loads(before)["byte_offset"] < after["byte_offset"]


def test_d2_wake_due_in_past_nudges(tmp_path):
    out, _, _ = run(tmp_path, [_prior_arm(_ts(-31))], [user(HUMAN), text("m1")])
    assert kind(out) == "nudge"


def test_d2b_wake_due_within_90s_nudges(tmp_path):
    # armed 29 min ago, 30 min delay: due in 60 s, not "more than 90 s" away
    out, _, _ = run(tmp_path, [_prior_arm(_ts(-29))], [user(HUMAN), text("m1")])
    assert kind(out) == "nudge"


def test_d3_fired_wake_consumes_pending(tmp_path):
    out, _, _ = run(tmp_path, [_prior_arm(_ts(-5)), user(WAKE_PROMPT)],
                    [user(HUMAN), text("m1")])
    assert kind(out) == "nudge"


def test_d4_arm_without_timestamp_nudges(tmp_path):
    out, _, _ = run(tmp_path, [_prior_arm(None)], [user(HUMAN), text("m1")])
    assert kind(out) == "nudge"


def test_d5_arm_without_delay_nudges(tmp_path):
    out, _, _ = run(tmp_path, [_prior_arm(_ts(-5), delay=None)], [user(HUMAN), text("m1")])
    assert kind(out) == "nudge"


def test_e_loop_wake_no_arm_nudges_even_with_pending(tmp_path):
    out, _, _ = run(tmp_path, [_prior_arm(_ts(-5))], [user(WAKE_PROMPT), text("m1")])
    assert kind(out) == "nudge"


def test_e2_loop_wake_no_arm_nudges_without_pending(tmp_path):
    out, _, _ = run(tmp_path, [], [user(WAKE_PROMPT), text("m1")])
    assert kind(out) == "nudge"


def test_f_confirmed_arm_with_final_text_no_block(tmp_path):
    out, _, _ = run(tmp_path, [], [user(HUMAN), *arm_pair("m1"), tool_result(), text("m2")])
    assert out == ""


def test_g_only_tool_use_reply_gate_wins(tmp_path):
    out, _, _ = run(tmp_path, [], [user(HUMAN), tool("m1", "Read"), tool_result()])
    assert kind(out) == "reply"
    assert "Loop Arm Check" not in json.loads(out)["reason"]


def test_h_unparseable_line_no_reply_gate(tmp_path):
    out, _, _ = run(tmp_path, [], [user(HUMAN), "{not json", tool("m1", "Read")])
    assert kind(out) != "reply"


def test_i_stop_hook_active_no_output(tmp_path):
    out, _, _ = run(tmp_path, [], [user(HUMAN), text("m1"), *arm_pair("m2"), tool_result()],
                    active=True)
    assert out == ""


def test_j_non_loop_session_untouched(tmp_path):
    out, home, before = run(tmp_path, [], [user(HUMAN), text("m1"), *arm_pair("m2"), tool_result()],
                            marker=False)
    assert out == ""
    assert (home / STATE_NAME).read_text() == before


def test_k_cron_pacer_no_output(tmp_path):
    out, _, _ = run(tmp_path, [], [user(HUMAN), text("m1"), *arm_pair("m2"), tool_result()],
                    pacer="cron")
    assert out == ""


def test_l_message_pins():
    spec = importlib.util.spec_from_file_location("loop_arm_hook_4104", HOOK_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    nudge = mod._NUDGE_TEXT
    assert "cadence_seconds" in nudge
    for retired in ("PRESERVES", "preserve", "next_due_at", "bare full cadence"):
        assert retired not in nudge
    reply = mod._REPLY_NOT_RENDERED_TEXT
    assert reply.startswith("[Reply Delivery Check:")
    assert "arm now" not in reply.lower()
    assert len(reply) < 800


def test_m_stop_hook_feedback_is_not_a_human_prompt(tmp_path):
    fb = user("Stop hook feedback: [Loop Arm Check: ... <loop-wake>]")
    out, _, _ = run(tmp_path, [], [user(HUMAN), text("m1"), *arm_pair("m2"), tool_result(),
                                   fb, text("m3")])
    assert kind(out) == "none"


def test_n_last_human_prompt_governs(tmp_path):
    out, _, _ = run(tmp_path, [], [user("first question"), text("m1"),
                                   user("second question"), tool("m2", "Read"), tool_result()])
    assert kind(out) == "reply"


# ---- real-transcript fields (measured on a live 2.1.308 transcript) ----

def user_x(body: str, **fields) -> str:
    rec = {"type": "user", "message": {"role": "user", "content": body}}
    rec.update(fields)
    return _j(rec)


SKILL_TEXT = "Base directory for this skill: /x/skills/engram-loop\n\n# engram-loop ..."


def test_o_skill_expansion_after_wake_is_not_human(tmp_path):
    out, _, _ = run(tmp_path, [], [user(WAKE_PROMPT),
                                   user_x(SKILL_TEXT, isMeta=True, turnCompanion=True),
                                   *arm_pair("m1"), tool_result()])
    assert kind(out) != "reply"


def test_p_unflagged_plain_text_is_human_fallback(tmp_path):
    # No provenance fields (old/synthetic transcript): the text rules alone decide.
    # (Unflagged "Base directory for this skill:" text is excluded by the text rule since
    # the x5b fold, so this fallback cell uses ordinary text.)
    out, _, _ = run(tmp_path, [], [user(WAKE_PROMPT), user_x("please continue with the review"),
                                   tool("m1", "Read"), tool_result()])
    assert kind(out) == "reply"


def test_q_typed_human_record_blocks(tmp_path):
    out, _, _ = run(tmp_path, [], [user_x(HUMAN, origin={"kind": "human"},
                                          promptSource="typed", turnOrigin="human"),
                                   text("m1"), *arm_pair("m2"), tool_result()])
    assert kind(out) == "reply"


def test_r_task_notification_origin_without_tag_is_not_human(tmp_path):
    out, _, _ = run(tmp_path, [], [user_x("background job finished",
                                          origin={"kind": "task-notification"}),
                                   tool("m1", "Read"), tool_result()])
    assert kind(out) != "reply"


def test_s_scheduled_turn_origin_is_not_human(tmp_path):
    out, _, _ = run(tmp_path, [], [user_x("time to check in", turnOrigin="scheduled"),
                                   tool("m1", "Read"), tool_result()])
    assert kind(out) != "reply"


def test_t_system_prompt_source_is_not_human(tmp_path):
    out, _, _ = run(tmp_path, [], [user_x("injected", promptSource="system"),
                                   tool("m1", "Read"), tool_result()])
    assert kind(out) != "reply"


# ---- 4 MiB tail bound on the wake-pending scan ----

def _padding():
    return [text("pad", "x" * 100_000) for _ in range(45)]  # ~4.5 MB


def test_u_big_transcript_arm_inside_window_is_pending(tmp_path):
    out, _, _ = run(tmp_path, [*_padding(), _prior_arm(_ts(-5))], [user(HUMAN), text("m1")])
    assert out == ""


def test_v_arm_before_4mib_window_not_pending(tmp_path):
    out, _, _ = run(tmp_path, [_prior_arm(_ts(-5)), *_padding()], [user(HUMAN), text("m1")])
    assert kind(out) == "nudge"


# ---- single-record (multi-block) shape and last-human selection ----

def _blocks(msg_id: str, *blocks: dict) -> str:
    return _j({"type": "assistant", "message": {"id": msg_id, "content": list(blocks)}})


_TEXT_BLOCK = {"type": "text", "text": "here is the answer"}
_SW_BLOCK = {"type": "tool_use", "id": "sw1", "name": "ScheduleWakeup",
             "input": {"delaySeconds": 1800, "prompt": WAKE_PROMPT}}


def test_w_text_then_tool_use_in_one_record_blocks(tmp_path):
    out, _, _ = run(tmp_path, [], [user(HUMAN), _blocks("m1", _TEXT_BLOCK, _SW_BLOCK),
                                   tool_result()])
    assert kind(out) == "reply"


def test_w2_tool_use_then_text_in_one_record_not_blocked(tmp_path):
    out, _, _ = run(tmp_path, [], [user(HUMAN), _blocks("m1", _SW_BLOCK, _TEXT_BLOCK)])
    assert kind(out) != "reply"


def test_x_wake_after_human_turn_resets_human(tmp_path):
    out, _, _ = run(tmp_path, [], [user(HUMAN), text("m1"), user(WAKE_PROMPT),
                                   *arm_pair("m2"), tool_result()])
    assert out == ""


MARKER_SKILL_TEXT = "Base directory for this skill: /x/skills/engram-loop\n\n" + WAKE_PROMPT + " ..."


def test_x5_skill_expansion_quoting_marker_does_not_clear_human(tmp_path):
    out, _, _ = run(tmp_path, [], [user(HUMAN),
                                   user_x(MARKER_SKILL_TEXT, isMeta=True, turnCompanion=True),
                                   tool("m1", "Read"), tool_result()])
    assert kind(out) == "reply"


def test_x5b_skill_text_without_flags_does_not_clear_human(tmp_path):
    out, _, _ = run(tmp_path, [], [user(HUMAN), user_x(MARKER_SKILL_TEXT),
                                   tool("m1", "Read"), tool_result()])
    assert kind(out) == "reply"


POST_COMPACTION_TEXT = (
    "# Post-compaction context (assembled by compact-request at 2026-10-23T15:00:00Z, "
    "session abc)\n\n## Where things stand\n...")


def test_c1_post_compaction_context_is_not_human(tmp_path):
    out, _, _ = run(tmp_path, [], [user(POST_COMPACTION_TEXT), *arm_pair("m1"), tool_result()])
    assert kind(out) != "reply"


def test_c2_post_compaction_context_does_not_clear_human(tmp_path):
    out, _, _ = run(tmp_path, [], [user(HUMAN), user(POST_COMPACTION_TEXT),
                                   tool("m1", "Read"), tool_result()])
    assert kind(out) == "reply"


def test_pend1_arm_due_120s_out_is_pending(tmp_path):
    # armed 28 min ago with a 30 min delay: due in ~120 s (> 90 s)
    out, _, _ = run(tmp_path, [_prior_arm(_ts(-28))], [user(HUMAN), text("m1")])
    assert out == ""


def test_pend2_arm_due_60s_out_is_not_pending(tmp_path):
    out, _, _ = run(tmp_path, [_prior_arm(_ts(-29))], [user(HUMAN), text("m1")])
    assert kind(out) == "nudge"


def test_p1_stop_feedback_quoting_marker_never_clears_human(tmp_path):
    fb = user("Stop hook feedback: [Loop Arm Check: ... " + WAKE_PROMPT + "]")
    out, _, _ = run(tmp_path, [], [user(HUMAN), tool("m1", "Read"), fb,
                                   tool("m2", "Read"), tool_result()])
    assert kind(out) == "reply"


def test_p2_compaction_summary_quoting_marker_never_clears_human(tmp_path):
    summ = user("This session is being continued from a previous conversation ... "
                + WAKE_PROMPT + " ...")
    out, _, _ = run(tmp_path, [], [user(HUMAN), tool("m1", "Read"), summ,
                                   tool("m2", "Read"), tool_result()])
    assert kind(out) == "reply"


def test_x2b_task_notification_after_human_turn_ending_on_tool(tmp_path):
    out, _, _ = run(tmp_path, [], [user(HUMAN), text("m1"),
                                   user("<task-notification>x</task-notification>"),
                                   tool("m2", "Read"), tool_result()])
    assert kind(out) != "reply"


def test_x3_skill_expansion_does_not_reset_human(tmp_path):
    out, _, _ = run(tmp_path, [], [user(HUMAN), user_x(SKILL_TEXT, isMeta=True),
                                   tool("m1", "ScheduleWakeup",
                                        {"delaySeconds": 1800, "prompt": WAKE_PROMPT}),
                                   tool_result()])
    assert kind(out) == "reply"


def test_x4_real_shaped_meta_wake_record_resets_human(tmp_path):
    wake = user_x(WAKE_PROMPT, isMeta=True, promptSource="system", turnOrigin="scheduled")
    out, _, _ = run(tmp_path, [], [user(HUMAN), text("m1"), wake, *arm_pair("m2"), tool_result()])
    assert out == ""


def test_y_reply_block_writes_offset(tmp_path):
    out, home, _ = run(tmp_path, [], [user(HUMAN), text("m1"), *arm_pair("m2"), tool_result()])
    assert kind(out) == "reply"
    state = json.loads((home / STATE_NAME).read_text())
    assert state["byte_offset"] == (tmp_path / "t.jsonl").stat().st_size


def test_z_pending_wake_does_not_suppress_non_human_turn(tmp_path):
    out, _, _ = run(tmp_path, [_prior_arm(_ts(-5))],
                    [user("<task-notification>x</task-notification>"), text("m1")])
    assert kind(out) == "nudge"


def test_z2_reply_block_resets_quiet_counter(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    counter = home / ".quiet-wake-counter.json"
    counter.write_text(json.dumps({"transcript_path": str(tmp_path / "t.jsonl"),
                                   "quiet_ticks": 1}))
    out, _, _ = run(tmp_path, [], [user(HUMAN), text("m1"), *arm_pair("m2"), tool_result()])
    assert kind(out) == "reply"
    assert json.loads(counter.read_text())["quiet_ticks"] == 0


def test_n2_unanswered_last_human_prompt_blocks(tmp_path):
    out, _, _ = run(tmp_path, [], [user("question A"), text("m1"), user("question B")])
    assert kind(out) == "reply"
