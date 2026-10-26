"""alpha #4193 -- loop-arm Stop hook: the reply gate consults the Stop payload's
`last_assistant_message` so transcript flush lag cannot false-block a rendered reply.

Contract (stated in the hook's module docstring; the field's semantics are UNVERIFIED, so
every cell runs under BOTH readings: R-flat = all text blocks of the final assistant
message concatenated, R-final = the final text block only):

* the override is consulted only when the transcript path already says
  `has_human and not reply_rendered`, and can only turn a block into no-block;
* payload present and NOT found in the delta's assistant text -> flush lag -> no block;
* payload present and found there, with a tool_use after the text -> still blocks
  (a swallowed reply must not be silenced by a non-empty payload);
* payload absent / non-str / empty / whitespace -> exactly the transcript-only path;
* every block / override writes ONE line to $ENGRAM_HOME/loop-arm-reply-gate.log (no
  payload text, bounded, fail-silent).

Real subprocess against a synthetic ENGRAM_HOME + transcript, helpers from #4104's tests.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))
from test_loop_arm_reply_gate_4104 import (  # noqa: E402
    ENV,
    HUMAN,
    STATE_NAME,
    WAKE_PROMPT,
    _SW_BLOCK,
    _blocks,
    arm_pair,
    kind,
    text,
    tool,
    tool_result,
    user,
)
from test_loop_arm_stop_hook import _run_hook, _write_loop_marker  # noqa: E402

pytestmark = pytest.mark.usefixtures("isolate_hook_loads_module")

LOG_NAME = "loop-arm-reply-gate.log"
_ABSENT = object()
READINGS = ["flat", "final"]


def _tb(body: str) -> dict:
    return {"type": "text", "text": body}


def run(tmp_path, delta, payload_msg=_ABSENT, *, active=False, prepare=None):
    tmp_path.mkdir(exist_ok=True)
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    _write_loop_marker(home, pacer="scheduleWakeup")
    transcript = tmp_path / "t.jsonl"
    transcript.write_bytes("".join(ln + "\n" for ln in delta).encode())
    (home / STATE_NAME).write_text(json.dumps({"transcript_path": str(transcript), "byte_offset": 0}))
    if prepare:
        prepare(home)
    payload = {"session_id": "sid-4193", "transcript_path": str(transcript),
               "stop_hook_active": active}
    if payload_msg is not _ABSENT:
        payload["last_assistant_message"] = payload_msg
    out, rc = _run_hook(json.dumps(payload), home, extra_env=ENV)
    assert rc == 0
    return out, home


def _log(home) -> list[str]:
    p = home / LOG_NAME
    return p.read_text().splitlines() if p.is_file() else []


def _pay(reading: str, *blocks: str) -> str:
    """The payload under each reading of the field for a message with these text blocks."""
    return " ".join(blocks) if reading == "flat" else blocks[-1]


SWALLOWED = [user(HUMAN), _blocks("m1", _tb("the reply"), _SW_BLOCK), tool_result()]
LAGGED = [user(HUMAN), text("m1", "old"), tool("m2", "Read"), tool_result()]


# ---- rule 1: payload not in the delta -> flush lag -> no block ----

@pytest.mark.parametrize("reading", READINGS)
def test_flush_lag_payload_not_in_delta_no_block(tmp_path, reading):
    out, _ = run(tmp_path, LAGGED, _pay(reading, "fresh first part", "fresh final part"))
    assert kind(out) != "reply"


def test_flush_lag_single_record_text_then_tool_no_block(tmp_path):
    out, _ = run(tmp_path, [user(HUMAN), _blocks("m1", _tb("old"), _SW_BLOCK), tool_result()],
                 "something else entirely")
    assert kind(out) != "reply"


def test_flush_lag_no_assistant_record_at_all_no_block(tmp_path):
    out, _ = run(tmp_path, [user(HUMAN)], "the reply")
    assert kind(out) != "reply"


# ---- rule 2: payload already in the delta, tool_use after it -> still blocks ----

@pytest.mark.parametrize("reading", READINGS)
def test_swallowed_single_record_still_blocks(tmp_path, reading):
    out, _ = run(tmp_path, SWALLOWED, _pay(reading, "the reply"))
    assert kind(out) == "reply"


@pytest.mark.parametrize("reading", READINGS)
def test_swallowed_two_text_blocks_still_blocks(tmp_path, reading):
    out, _ = run(tmp_path, [user(HUMAN), _blocks("m1", _tb("A"), _tb("B"), _SW_BLOCK), tool_result()],
                 _pay(reading, "A", "B"))
    assert kind(out) == "reply"


@pytest.mark.parametrize("reading", READINGS)
def test_swallowed_split_records_still_blocks(tmp_path, reading):
    # real transcripts write one record per content block: the reply text sits in an
    # earlier record than the trailing tool_use
    out, _ = run(tmp_path, [user(HUMAN), text("m1", "A"), text("m1", "B"),
                            *arm_pair("m1"), tool_result()],
                 _pay(reading, "A", "B"))
    assert kind(out) == "reply"


def test_swallowed_whitespace_differences_are_normalised(tmp_path):
    out, _ = run(tmp_path, [user(HUMAN), _blocks("m1", _tb("the  reply\n here"), _SW_BLOCK),
                            tool_result()], "the reply   here ")
    assert kind(out) == "reply"


# ---- rule 3: unusable payload -> exactly the transcript path ----

BAD = [_ABSENT, "", "   \n\t", 5, None, ["x"], {"a": 1}]


@pytest.mark.parametrize("bad", BAD)
def test_unusable_payload_swallowed_blocks(tmp_path, bad):
    out, _ = run(tmp_path, SWALLOWED, bad)
    assert kind(out) == "reply"


@pytest.mark.parametrize("bad", BAD)
def test_unusable_payload_rendered_no_block(tmp_path, bad):
    out, _ = run(tmp_path, [user(HUMAN), *arm_pair("m1"), tool_result(), text("m2")], bad)
    assert out == ""


@pytest.mark.parametrize("bad", BAD)
def test_unusable_payload_never_overrides_flush_lag_shape(tmp_path, bad):
    out, _ = run(tmp_path, LAGGED, bad)
    assert kind(out) == "reply"


# ---- unchanged paths ----

@pytest.mark.parametrize("payload_msg", ["the reply", "totally different text"])
def test_delta_already_ends_on_text_no_block(tmp_path, payload_msg):
    out, _ = run(tmp_path, [user(HUMAN), *arm_pair("m1"), tool_result(), text("m2", "the reply")],
                 payload_msg)
    assert out == ""


@pytest.mark.parametrize("payload_msg", [_ABSENT, "x", "the reply"])
def test_non_human_turn_unchanged(tmp_path, payload_msg):
    out, home = run(tmp_path, [user(WAKE_PROMPT), *arm_pair("m1"), tool_result()], payload_msg)
    assert kind(out) == "none"
    assert _log(home) == []


def test_stop_hook_active_still_returns_early(tmp_path):
    out, home = run(tmp_path, LAGGED, "fresh", active=True)
    assert out == ""
    assert _log(home) == []


def test_override_flows_into_rendered_branch(tmp_path):
    # flush lag, no arm in the delta: the arm check nudges exactly as for a rendered reply
    out, _ = run(tmp_path, LAGGED, "fresh")
    assert kind(out) == "nudge"


# ---- diagnostic line ----

def test_log_line_on_block(tmp_path):
    out, home = run(tmp_path, SWALLOWED, "the reply")
    assert kind(out) == "reply"
    lines = _log(home)
    assert len(lines) == 1
    line = lines[0]
    assert "sid-4193" in line and "payload_len=9" in line and "found=y" in line
    assert "ends_tool_use=y" in line and "decision=block" in line
    assert "the reply" not in line
    assert line.split()[0].endswith("Z") and line[:4].isdigit()


def test_log_line_on_override(tmp_path):
    out, home = run(tmp_path, LAGGED, "fresh secret text")
    assert kind(out) != "reply"
    lines = _log(home)
    assert len(lines) == 1
    line = lines[0]
    assert "payload_len=17" in line and "found=n" in line and "ends_tool_use=y" in line
    assert "decision=override-rendered" in line
    assert "secret" not in line and "fresh" not in line


def test_log_line_payload_absent(tmp_path):
    _, home = run(tmp_path, [user(HUMAN), tool("m1", "Read"), tool_result()])
    (line,) = _log(home)
    assert "payload_len=absent" in line and "decision=block" in line


def test_no_log_when_gate_does_not_fire(tmp_path):
    _, home = run(tmp_path, [user(HUMAN), text("m1")], "anything")
    assert _log(home) == []


def _blockdir(home):
    (home / LOG_NAME).mkdir()  # a directory where the log file should be: unwritable


def test_unwritable_log_does_not_change_block(tmp_path):
    out, _ = run(tmp_path, SWALLOWED, "the reply", prepare=_blockdir)
    assert kind(out) == "reply"


def test_unwritable_log_does_not_change_override(tmp_path):
    out, _ = run(tmp_path, LAGGED, "fresh", prepare=_blockdir)
    assert kind(out) == "nudge"


def test_log_is_bounded(tmp_path):
    def fill(home):
        (home / LOG_NAME).write_text("".join("old line %05d %s\n" % (i, "x" * 80) for i in range(1500)))
    _, home = run(tmp_path, [user(HUMAN), tool("m1", "Read"), tool_result()], "p", prepare=fill)
    lines = _log(home)
    assert len(lines) <= 201
    assert "decision=" in lines[-1]
    assert lines[0].startswith("old line")


# ---- round 2: false-block via substring matching; current-turn scoping ----
# "found" looks only at the FINAL MESSAGE (see round 3 below); text of an earlier message
# (a different message.id) is not the final message. The reverse clause is a SUFFIX test,
# not a substring test.

def test_short_preamble_same_message_as_tool_use_is_not_a_suffix_of_payload(tmp_path):
    # the suffix-vs-substring pin: the flushed message m1 = [text "Ok", tool_use] (same id) is
    # an EARLIER message; the real final reply (payload) is not flushed. "Ok" is a prefix of
    # the payload, not its tail -> not found -> flush lag, no block. A substring rule would
    # call it found and false-block.
    out, _ = run(tmp_path, [user(HUMAN), text("m1", "Ok"), tool("m1", "Read"), tool_result()],
                 "Ok, here is the full answer.")
    assert kind(out) != "reply"


def test_short_earlier_preamble_does_not_defeat_override(tmp_path):
    # probe (a), different ids (preamble message m1, flushed tool_use message m2): the final
    # message has no text, so this pins the id scoping, not the suffix rule (see the cell above)
    out, _ = run(tmp_path, [user(HUMAN), text("m1", "Ok"), tool("m2", "Read"), tool_result()],
                 "Ok, here is the full answer.")
    assert kind(out) != "reply"


def test_payload_equal_to_last_block_stays_blocked_ambiguous(tmp_path):
    # probe (b): text "Done." then a tool call, payload "Done.": indistinguishable from a
    # swallowed reply (same message m1), so the fix keeps the OLD behaviour (block). Pinned
    # deliberately.
    out, _ = run(tmp_path, [user(HUMAN), text("m1", "Done."), tool("m1", "Read"), tool_result()],
                 "Done.")
    assert kind(out) == "reply"


def test_payload_is_earlier_text_in_this_turn_still_blocks(tmp_path):
    # the text belongs to message m1, the final message is m3 (different ids) and carries no
    # text. A payload equal to text ALREADY flushed in this turn cannot be an unflushed final
    # record: indistinguishable from a swallowed reply (a "last text produced in the turn"
    # reading of the field), so the block stays (same trade as the "Done." cell).
    out, _ = run(tmp_path, [user(HUMAN), text("m1", "A"), tool("m2", "Read"), tool_result("r1"),
                            tool("m3", "Read"), tool_result("r2")], "A")
    assert kind(out) == "reply"


def test_third_reading_last_text_of_turn_with_tool_only_final_message_blocks(tmp_path):
    # human; msgA[text]; msgA[tool_use]; tool_result; msgB[tool_use]. P = msgA's text.
    out, _ = run(tmp_path, [user(HUMAN), text("mA", "Here is the full answer."),
                            tool("mA", "Read"), tool_result("r1"), tool("mB", "Read")],
                 "Here is the full answer.")
    assert kind(out) == "reply"


def test_third_reading_payload_is_a_middle_part_of_earlier_text_blocks(tmp_path):
    # P is a substring of the turn's flushed text but neither its tail nor the final message's
    out, _ = run(tmp_path, [user(HUMAN), text("mA", "Here is the answer. Anything else?"),
                            text("mA", "Second block."), tool("mA", "Read"), tool_result("r1"),
                            tool("mB", "Read")], "Here is the answer.")
    assert kind(out) == "reply"


def test_flush_lag_with_intervening_tool_result_and_different_narration_overrides(tmp_path):
    # P appears nowhere in any assistant record of the turn -> still the flush-lag override
    out, _ = run(tmp_path, [user(HUMAN), text("mA", "Let me look."), tool("mA", "Read"),
                            tool_result("r1"), tool("mB", "Read"), tool_result("r2")],
                 "The real final reply, not flushed yet.")
    assert kind(out) != "reply"


def test_all_records_clause_is_scoped_after_the_last_human_prompt(tmp_path):
    # P equals text of the PREVIOUS human turn only; the current turn is lagged -> no block
    out, _ = run(tmp_path, [user("first question"), text("m1", "first answer"), user(HUMAN),
                            text("m2", "Looking."), tool("m2", "Read"), tool_result("r1")],
                 "first answer")
    assert kind(out) != "reply"


@pytest.mark.parametrize("reading", READINGS)
def test_swallowed_with_parallel_tool_results_still_blocks(tmp_path, reading):
    out, _ = run(tmp_path, [user(HUMAN), text("m1", "A"), text("m1", "B"),
                            tool("m1", "ScheduleWakeup", {"delaySeconds": 1800, "prompt": WAKE_PROMPT}),
                            tool("m1", "CronList"), tool_result("a"), tool_result("b")],
                 _pay(reading, "A", "B"))
    assert kind(out) == "reply"


def test_current_turn_scoping_earlier_turn_text_not_found(tmp_path):
    # turn 1 ended on text "first answer"; turn 2 is lagged (last flushed record is a tool_use)
    # and the payload equals turn 1's text: that text is not in turn 2 -> no block
    out, _ = run(tmp_path, [user("first question"), text("m1", "first answer"), user(HUMAN),
                            tool("m2", "Read"), tool_result()], "first answer")
    assert kind(out) != "reply"


# ---- round 3: the final message is identified by message.id, not by adjacency ----
# Measured in a real transcript: with parallel tool calls one API message m1 = [text,
# tool_use A, tool_use B] is written as record(text), record(A), user(result A),
# record(B), user(result B): non-assistant records sit BETWEEN records of one message.
# The final message = every assistant record after the last human prompt whose message.id
# equals the last assistant record's id. (Round-2 fixtures that gave the text and the
# tool_use different ids modelled a shape the harness does not write; they now share m1.)

def test_current_turn_scoping_holds_even_with_a_colliding_message_id(tmp_path):
    # defensive: ids are unique per API message in practice, but the after-prompt scope must
    # not depend on that. Turn 1's text shares the id of turn 2's last record.
    out, _ = run(tmp_path, [user("first question"), text("m1", "first answer"), user(HUMAN),
                            tool("m1", "Read"), tool_result()], "first answer")
    assert kind(out) != "reply"


def _wake(delay=1800):
    return {"delaySeconds": delay, "prompt": WAKE_PROMPT}


@pytest.mark.parametrize("reading", READINGS)
def test_swallowed_interleaved_results_still_blocks(tmp_path, reading):
    out, _ = run(tmp_path, [user(HUMAN), text("m1", "the reply"),
                            tool("m1", "ScheduleWakeup", _wake()), tool_result("a"),
                            tool("m1", "CronList"), tool_result("b")],
                 _pay(reading, "the reply"))
    assert kind(out) == "reply"


def test_swallowed_single_arm_interleaved_still_blocks(tmp_path):
    out, _ = run(tmp_path, [user(HUMAN), text("m1", "the reply"),
                            tool("m1", "Bash", {"command": "echo"}), tool_result("a"),
                            tool("m1", "ScheduleWakeup", _wake(900)), tool_result("b")],
                 "the reply")
    assert kind(out) == "reply"


@pytest.mark.parametrize("reading", READINGS)
def test_swallowed_text_blocks_split_across_the_interleave(tmp_path, reading):
    out, _ = run(tmp_path, [user(HUMAN), text("m1", "A"),
                            tool("m1", "Read"), tool_result("a"),
                            text("m1", "B"),
                            tool("m1", "ScheduleWakeup", _wake()), tool_result("b")],
                 _pay(reading, "A", "B"))
    assert kind(out) == "reply"


def _noid(blocks: list) -> str:
    return json.dumps({"type": "assistant", "message": {"content": blocks}}, separators=(",", ":"))


_NOID_TOOL = {"type": "tool_use", "id": "x", "name": "Read", "input": {}}


def test_missing_message_id_falls_back_to_adjacent_run_swallowed(tmp_path):
    out, _ = run(tmp_path, [user(HUMAN), _noid([_tb("the reply")]), _noid([_NOID_TOOL]),
                            tool_result()], "the reply")
    assert kind(out) == "reply"


def test_missing_message_id_payload_is_earlier_text_in_this_turn_still_blocks(tmp_path):
    # no ids: the text sits before a tool_result, so it is not in the adjacent run, but a
    # payload equal to already-flushed text of this turn is indistinguishable from a
    # swallowed reply: the block stays
    out, _ = run(tmp_path, [user(HUMAN), _noid([_tb("A")]), _noid([_NOID_TOOL]), tool_result("r1"),
                            _noid([_NOID_TOOL]), tool_result("r2")], "A")
    assert kind(out) == "reply"


def test_missing_message_id_flush_lag_overrides_when_payload_is_nowhere_in_the_turn(tmp_path):
    out, _ = run(tmp_path, [user(HUMAN), _noid([_tb("A")]), _noid([_NOID_TOOL]), tool_result("r1"),
                            _noid([_NOID_TOOL]), tool_result("r2")], "B, the unflushed reply")
    assert kind(out) != "reply"
