"""alpha #3919 M2 -- quiet-wake counter + Stop-hook nudge.

Contract (spec: epic #3919 item M2, commander's rulings 2026-10-19):

A QUIET WAKE is one turn (the transcript delta the loop-arm Stop hook already
computes) such that ALL hold:
  1. it was started by a wake-class trigger (first user-role TEXT record is a
     `<loop-wake>` prompt or contains `<task-notification>`);
  2. every tool_use is in the observe-or-arm ALLOWLIST, and every Bash
     tool_use passes `_bash_is_read_only`;
  3. the delta parses (unparseable / unreadable / empty -> NOT quiet).

The hook counts consecutive quiet wakes in its OWN state file
(`$ENGRAM_HOME/.quiet-wake-counter.json`), and after QUIET_TICKS_THRESHOLD
(2) emits ONE block decision, then resets. The counter is additive: it never
changes the arm-check nudge, never raises, never blocks on failure.

The hook is hyphen-named, so unit cells load it via importlib; the counter /
interaction cells run the real hook as a subprocess against a temp
ENGRAM_HOME (same fixtures as tests/test_loop_arm_stop_hook.py).
"""

from __future__ import annotations

import datetime
import importlib.util
import json
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))
from test_loop_arm_stop_hook import (  # noqa: E402
    HOOK_PATH,
    _REAL_LOOP_PROMPT,
    _make_stop_payload,
    _run_hook,
    _schedule_wakeup_call,
    _tool_use_record,
    _tool_use_record_with_msg_id,
    _batched_arm_records,
    _write_loop_marker,
)

STATE_NAME = ".quiet-wake-counter.json"

# #4029: in-process hook/tools loads run inside this window (sys.path / sys.modules restored);
# module scope: the load happens in module-scope fixtures or at import, which a function-scope window does not cover.
pytestmark = pytest.mark.usefixtures("isolate_hook_loads_module")


# ---------------------------------------------------------------------------
# module loader (hyphenated hook name)
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def hook(tmp_path_factory):
    home = tmp_path_factory.mktemp("quiet_home")
    old = os.environ.get("ENGRAM_HOME")
    os.environ["ENGRAM_HOME"] = str(home)
    try:
        spec = importlib.util.spec_from_file_location("loop_arm_hook_3919", HOOK_PATH)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
    finally:
        if old is None:
            os.environ.pop("ENGRAM_HOME", None)
        else:
            os.environ["ENGRAM_HOME"] = old
    return mod


# ---------------------------------------------------------------------------
# transcript builders
# ---------------------------------------------------------------------------

def _user_text(text: str) -> str:
    return json.dumps({"type": "user", "message": {"role": "user", "content": text}})


def _user_blocks(text: str) -> str:
    return json.dumps(
        {"type": "user", "message": {"role": "user", "content": [{"type": "text", "text": text}]}}
    )


WAKE = _user_text(_REAL_LOOP_PROMPT)
NOTIF = _user_text("<task-notification>\n<task-id>abc</task-id>\nMonitor expired\n</task-notification>")
HUMAN = _user_text("hey, can you look at the failing test?")
REARM = _schedule_wakeup_call("t_rearm", _REAL_LOOP_PROMPT)


def _tu(name: str, input_: dict | None = None, tid: str = "t1") -> str:
    return _tool_use_record(tid, name, input_ or {})


def _bash(cmd: str) -> str:
    return _tu("Bash", {"command": cmd})


def _delta(*lines: str) -> bytes:
    return ("\n".join(lines) + "\n").encode("utf-8")


# ---------------------------------------------------------------------------
# constants pinned
# ---------------------------------------------------------------------------

EXPECTED_TOOLS = frozenset({
    "ScheduleWakeup", "CronList", "Monitor", "TaskStop", "ToolSearch",
    "Read", "Glob", "Grep",
    "whats_new", "hear", "look_around", "look_at", "look_in",
    "crossings_here", "crossings_for_agent", "search_crossings",
    "thread", "map", "where_can_i_go", "inventory",
})


def test_observe_or_arm_membership_pinned(hook):
    assert hook.QUIET_WAKE_OBSERVE_OR_ARM_TOOLS == EXPECTED_TOOLS
    assert isinstance(hook.QUIET_WAKE_OBSERVE_OR_ARM_TOOLS, frozenset)
    for banned in ("Write", "Edit", "Bash", "Agent", "send_crossing", "checklist", "open"):
        assert banned not in hook.QUIET_WAKE_OBSERVE_OR_ARM_TOOLS


def test_threshold_pinned(hook):
    assert hook.QUIET_TICKS_THRESHOLD == 2


def test_nudge_text_contract(hook):
    t = hook._QUIET_NUDGE_TEXT
    assert len(t) < 600
    assert "study_queue" in t and "For the awake seat" in t
    import re
    assert not re.search(r"\b[a-z]{2}_\d{4}\b", t)  # no real node ids


# ---------------------------------------------------------------------------
# classification: quiet / not quiet
# ---------------------------------------------------------------------------

def test_wake_with_whats_new_and_schedulewakeup_is_quiet(hook):
    assert hook._delta_is_quiet_wake(_delta(WAKE, _tu("mcp__umwelt__whats_new"), REARM))


def test_task_notification_with_cronlist_is_quiet(hook):
    assert hook._delta_is_quiet_wake(_delta(NOTIF, _tu("CronList")))


def test_wake_with_zero_tools_is_quiet(hook):
    assert hook._delta_is_quiet_wake(_delta(WAKE))


def test_wake_text_in_block_form_is_quiet(hook):
    assert hook._delta_is_quiet_wake(_delta(_user_blocks(_REAL_LOOP_PROMPT)))


def test_html_escaped_wake_marker_is_quiet(hook):
    assert hook._delta_is_quiet_wake(_delta(_user_text("&lt;loop-wake&gt; tick")))


def test_human_started_turn_not_quiet_even_with_zero_tools(hook):
    assert not hook._delta_is_quiet_wake(_delta(HUMAN))
    assert not hook._delta_is_quiet_wake(_delta(HUMAN, _tu("CronList")))


def test_first_user_text_record_decides_not_a_later_one(hook):
    # human first, wake-looking text later -> not quiet
    assert not hook._delta_is_quiet_wake(_delta(HUMAN, WAKE))


def test_tool_result_user_record_before_wake_is_skipped(hook):
    tr = json.dumps({"type": "user", "message": {"content": [
        {"type": "tool_result", "tool_use_id": "x", "content": "ok"}]}})
    assert hook._delta_is_quiet_wake(_delta(tr, WAKE))


@pytest.mark.parametrize("bad_line", [
    _tu("mcp__umwelt__send_crossing", {"to": "x"}),
    _tu("Write", {"file_path": "/x", "content": "y"}),
    _tu("Edit", {"file_path": "/x"}),
    _tu("mcp__plugin_engram_engram__engram_add_observation", {}),
    _tu("engram_add_observation", {}),
    _tu("Agent", {"prompt": "go"}),
    _tu("mcp__umwelt__checklist"),
    _tu("mcp__umwelt__open"),
    _bash("git commit -m x"),
    _bash("rm x"),
    _bash("echo hi > f"),
    _bash("gh api -X POST repos/a/b/issues"),
])
def test_wake_with_mutating_tool_not_quiet(hook, bad_line):
    assert not hook._delta_is_quiet_wake(_delta(WAKE, bad_line))


def test_one_bad_tool_among_good_ones_not_quiet(hook):
    assert not hook._delta_is_quiet_wake(
        _delta(WAKE, _tu("CronList"), _tu("Write", {"file_path": "/x"}), REARM))


def test_bash_gh_pr_view_piped_to_head_is_quiet(hook):
    assert hook._delta_is_quiet_wake(_delta(WAKE, _bash("gh pr view 5 | head")))


def test_bash_without_command_string_not_quiet(hook):
    assert not hook._delta_is_quiet_wake(_delta(WAKE, _tu("Bash", {})))
    assert not hook._delta_is_quiet_wake(_delta(WAKE, _tu("Bash", {"command": 5})))


def test_umwelt_verb_requires_umwelt_server_prefix(hook):
    # a verb name on some OTHER mcp server must not be assumed read-only
    assert not hook._delta_is_quiet_wake(_delta(WAKE, _tu("mcp__other__whats_new")))


def test_unparseable_delta_not_quiet(hook):
    assert not hook._delta_is_quiet_wake(b"this is not json\n{broken\n")
    assert not hook._delta_is_quiet_wake(b"\xff\xfe\x00garbage")


def test_partially_unparseable_delta_not_quiet(hook):
    assert not hook._delta_is_quiet_wake(WAKE.encode() + b"\n{broken\n")


def test_empty_delta_not_quiet(hook):
    assert not hook._delta_is_quiet_wake(b"")
    assert not hook._delta_is_quiet_wake(b"\n\n")


def test_no_user_text_record_not_quiet(hook):
    assert not hook._delta_is_quiet_wake(_delta(_tu("CronList")))


# ---------------------------------------------------------------------------
# _bash_is_read_only table
# ---------------------------------------------------------------------------

ALLOWED = [
    "baton mine",
    "baton status",
    "baton show PR-5",
    "gh pr view 5",
    "gh pr list --state open",
    "gh pr checks 5",
    "gh issue view 3919 -R owner/repo",
    "gh run list --branch x",
    "gh pr status",
    "gh api repos/a/b/pulls/5",
    "git status",
    "git log --oneline -5",
    "git diff HEAD",
    "git show abc123",
    "git fetch origin",
    "git branch -a",
    "git rev-parse HEAD",
    "git worktree list",
    "git tag --points-at HEAD",
    "pgrep -f foo",
    "ps aux",
    "ls -la /tmp",
    "cat /tmp/x",
    "head -n 5 /tmp/x",
    "tail -n 5 /tmp/x",
    "wc -l /tmp/x",
    "grep -rn foo src",
    "date",
    "pwd",
    "cd x && git status",
    "gh pr view 5 | head",
    "ps aux | grep foo",
    "git log --oneline | wc -l",
    "cat f | sort",
]

DISALLOWED = [
    "",
    "   ",
    "git status; rm x",
    "git status && rm x",
    "git status || rm x",
    "cat a > b",
    "cat a >> b",
    "cat < a",
    "echo $(rm x)",
    "echo `rm x`",
    "ls $(rm x)",
    "rm x",
    "echo hi",
    "git commit -m x",
    "git push",
    "git checkout main",
    "git reset --hard",
    "git branch -D foo",
    "git branch newname",
    "git diff --output=/tmp/x",
    "git tag v1",
    "git worktree add /tmp/x",
    "gh pr create",
    "gh pr merge 5",
    "gh issue comment 5 -b hi",
    "gh api -X POST repos/a/b/issues",
    "gh api --method DELETE repos/a/b",
    "gh api repos/a/b -f title=x",
    "gh api repos/a/b -F x=y",
    "gh api repos/a/b --field x=y",
    "baton flip PR-5 someone",
    "baton claim pool-x",
    "python3 tools/baton.py mine",
    "FOO=bar ls",
    "ls | rm",
    "ls | head | tail | wc | cut -c1-3",
    "cat f | sort -o out",
    "ls & rm x",
    "date -s 2020-01-01",
    "cat 'unterminated",
    "ls\nrm x",
    "cd x && cd y && git status",
    "cd x; git status",
]


@pytest.mark.parametrize("cmd", ALLOWED)
def test_bash_read_only_allowed(hook, cmd):
    assert hook._bash_is_read_only(cmd) is True, cmd


@pytest.mark.parametrize("cmd", DISALLOWED)
def test_bash_read_only_disallowed(hook, cmd):
    assert hook._bash_is_read_only(cmd) is False, cmd


def test_bash_table_sizes():
    assert len(ALLOWED) >= 12 and len(DISALLOWED) >= 15


def test_bash_non_string_false(hook):
    assert hook._bash_is_read_only(None) is False  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# counter: subprocess cells
# ---------------------------------------------------------------------------

class Seat:
    """A temp ENGRAM_HOME + transcript driving the real hook."""

    def __init__(self, tmp_path: Path, marker: bool = True):
        self.home = tmp_path / "home"
        self.home.mkdir()
        self.transcript = tmp_path / "t.jsonl"
        self.transcript.write_text("", encoding="utf-8")
        if marker:
            _write_loop_marker(self.home)

    @property
    def state_path(self) -> Path:
        return self.home / STATE_NAME

    def ticks(self):
        return json.loads(self.state_path.read_text())["quiet_ticks"]

    def fire(self, *lines: str, transcript: Path | None = None, active: bool = False):
        tp = transcript or self.transcript
        with open(tp, "a", encoding="utf-8") as f:
            for ln in lines:
                f.write(ln + "\n")
        out, rc = _run_hook(_make_stop_payload(str(tp), stop_hook_active=active), self.home)
        assert rc == 0
        return out

    def quiet(self, *, rearm=True):
        lines = [WAKE, _tu("mcp__umwelt__whats_new")]
        if rearm:
            sw, cl = _batched_arm_records("t_sw", "t_cl", _REAL_LOOP_PROMPT, "msg_quiet_rearm")
            lines.extend([sw, cl])
        return self.fire(*lines)

    def busy(self):
        sw, cl = _batched_arm_records("t_sw", "t_cl", _REAL_LOOP_PROMPT, "msg_busy_rearm")
        return self.fire(WAKE, _tu("mcp__umwelt__send_crossing", {"to": "x"}), sw, cl)


def _decision(out: str):
    return json.loads(out) if out else None


def _is_quiet_nudge(out: str) -> bool:
    d = _decision(out)
    return bool(d) and d.get("decision") == "block" and "study_queue" in d.get("reason", "")


def _is_arm_nudge(out: str) -> bool:
    d = _decision(out)
    return bool(d) and d.get("decision") == "block" and "Loop Arm Check" in d.get("reason", "")


def test_two_quiet_then_nudge_on_second(tmp_path):
    s = Seat(tmp_path)
    assert s.quiet() == ""
    out = s.quiet()
    assert _is_quiet_nudge(out)
    assert "For the awake seat" in _decision(out)["reason"]
    assert out.count('"decision"') == 1  # exactly one block decision


def test_quiet_nonquiet_quiet_no_nudge(tmp_path):
    s = Seat(tmp_path)
    assert s.quiet() == ""
    assert s.busy() == ""
    assert s.ticks() == 0
    assert s.quiet() == ""
    assert s.ticks() == 1


def test_count_restarts_after_nudge(tmp_path):
    s = Seat(tmp_path)
    s.quiet()
    assert _is_quiet_nudge(s.quiet())
    assert s.ticks() == 0
    assert s.quiet() == ""
    assert _is_quiet_nudge(s.quiet())
    assert s.quiet() == ""


def test_counter_persists_across_invocations_via_state_file(tmp_path):
    s = Seat(tmp_path)
    s.quiet()
    data = json.loads(s.state_path.read_text())
    assert data == {"transcript_path": str(s.transcript), "quiet_ticks": 1}


def test_different_transcript_path_resets(tmp_path):
    s = Seat(tmp_path)
    s.quiet()
    other = tmp_path / "other.jsonl"
    other.write_text("")
    sw, cl = _batched_arm_records("t_sw", "t_cl", _REAL_LOOP_PROMPT, "msg_other_rearm")
    assert s.fire(WAKE, _tu("mcp__umwelt__whats_new"), sw, cl, transcript=other) == ""
    assert s.ticks() == 1  # restarted from 0 on the new transcript, not 2
    assert json.loads(s.state_path.read_text())["transcript_path"] == str(other)


@pytest.mark.parametrize("junk", ["not json{", "[]", '{"transcript_path": 5}', '{"quiet_ticks": "x"}', ""])
def test_corrupt_state_file_counts_as_zero(tmp_path, junk):
    s = Seat(tmp_path)
    s.state_path.write_text(junk)
    assert s.quiet() == ""  # 0 -> 1, no nudge, no exception
    assert s.ticks() == 1


def test_corrupt_state_with_matching_path_but_bad_ticks_counts_as_zero(tmp_path):
    s = Seat(tmp_path)
    s.state_path.write_text(json.dumps({"transcript_path": str(s.transcript), "quiet_ticks": -7}))
    assert s.quiet() == ""
    assert s.ticks() == 1


def test_quiet_turn_that_rearmed_still_counts_and_nudges(tmp_path):
    s = Seat(tmp_path)
    assert s.quiet(rearm=True) == ""
    assert _is_quiet_nudge(s.quiet(rearm=True))


def test_quiet_turn_without_rearm_emits_arm_nudge_only_and_counts(tmp_path):
    s = Seat(tmp_path)
    assert s.quiet(rearm=True) == ""
    out = s.quiet(rearm=False)
    assert _is_arm_nudge(out) and not _is_quiet_nudge(out)
    assert out.count('"decision"') == 1  # no double block
    assert s.ticks() == 2  # still incremented; not reset because no quiet nudge was emitted


def test_arm_nudge_text_unchanged_when_counter_runs(tmp_path, hook):
    s = Seat(tmp_path)
    out = s.quiet(rearm=False)
    assert _decision(out)["reason"] == hook._NUDGE_TEXT


def test_stop_hook_active_leaves_counter_untouched(tmp_path):
    s = Seat(tmp_path)
    s.quiet()
    before = s.state_path.read_text()
    out = s.fire(WAKE, _tu("CronList"), REARM, active=True)
    assert out == ""
    assert s.state_path.read_text() == before


def test_no_loop_marker_silent_and_no_state_file(tmp_path):
    s = Seat(tmp_path, marker=False)
    assert s.fire(WAKE, _tu("CronList")) == ""
    assert not s.state_path.exists()


def test_non_schedulewakeup_pacer_no_state_file(tmp_path):
    s = Seat(tmp_path, marker=False)
    _write_loop_marker(s.home, pacer="cron")
    assert s.fire(WAKE, _tu("CronList")) == ""
    assert not s.state_path.exists()


def test_unwritable_state_path_never_breaks_hook(tmp_path):
    s = Seat(tmp_path)
    s.state_path.mkdir()  # a directory where the file should be -> read & write both fail
    out = s.quiet(rearm=False)
    assert _is_arm_nudge(out)  # existing behaviour intact
    assert s.quiet(rearm=True) == ""  # confirmed turn: silent, exit 0


def test_human_turn_resets_via_subprocess(tmp_path):
    s = Seat(tmp_path)
    s.quiet()
    sw, cl = _batched_arm_records("t_sw", "t_cl", _REAL_LOOP_PROMPT, "msg_human_rearm")
    # #4104: a human turn must END on assistant text (a trailing arm swallows the reply and the
    # reply gate blocks it), so the reply follows the arm as the turn's final text.
    reply = json.dumps({"type": "assistant", "message": {"id": "msg_reply", "content": [
        {"type": "text", "text": "the test fails on the stale fixture"}]}})
    assert s.fire(HUMAN, sw, cl, reply) == ""
    assert s.ticks() == 0


# ---------------------------------------------------------------------------
# review round 1 (found by replaying the REAL transcript, not fixtures): a Stop-hook block
# arrives as a user-role record that quotes the loop-wake marker; it must not be read as the
# turn's trigger.
# ---------------------------------------------------------------------------

FEEDBACK = _user_text(
    "Stop hook feedback:\n[Loop Arm Check: a loop marker is active and this turn shows no "
    "ScheduleWakeup call. Arm now with <loop-wake> ...]"
)


def test_stop_hook_feedback_is_not_a_trigger_human_turn_after_it_is_not_quiet(hook):
    """feedback + continuation (re-arm), then a HUMAN message answered with no tools: the
    delta of the next fire starts at the feedback record. The human turn must NOT be quiet."""
    assert not hook._delta_is_quiet_wake(_delta(FEEDBACK, REARM, HUMAN))


def test_stop_hook_feedback_then_a_real_wake_is_quiet(hook):
    assert hook._delta_is_quiet_wake(_delta(FEEDBACK, REARM, WAKE, _tu("mcp__umwelt__whats_new")))


def test_stop_hook_feedback_alone_has_no_trigger_so_is_not_quiet(hook):
    assert not hook._delta_is_quiet_wake(_delta(FEEDBACK, REARM))


def test_control_without_the_feedback_prefix_a_marker_quoting_record_still_triggers(hook):
    quoting = _user_text("a note quoting <loop-wake> in passing")
    assert hook._delta_is_quiet_wake(_delta(quoting, _tu("CronList")))


SUMMARY = _user_text(
    "This session is being continued from a previous conversation that ran out of context. "
    "The summary below mentions the <loop-wake> marker and <task-notification> events."
)


def test_compaction_summary_is_not_a_trigger(hook):
    assert not hook._delta_is_quiet_wake(_delta(SUMMARY, _tu("CronList")))


def test_compaction_summary_then_a_real_wake_is_quiet(hook):
    assert hook._delta_is_quiet_wake(_delta(SUMMARY, WAKE, _tu("mcp__umwelt__whats_new")))


# ---------------------------------------------------------------------------
# review round 1 (PR 3947): clustered short flags, refspec fetch, quote-aware pipes, -C/-R
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("cmd", [
    "git fetch origin main:refs/heads/foo",     # writes a ref
    "git fetch --prune",                        # deletes refs
    "git fetch origin main",                    # more than one remote-ish argument
    "date -us x",                               # clustered -s sets the clock
    "sort -uo f",                               # clustered -o writes a file
    "cat a | sort -ro f",
    "cat a | sort --compress-program=sh",       # runs a program
    "gh api -iXPOST repos/x/y",                 # clustered method
    "gh api -ifa=b repos/x/y",                  # clustered field
    "git diff --ext-diff",                      # runs a configured program
    "git -C /tmp/x diff --output=f",
    "ps aux | grep x | rm",                     # a non-sink third stage
    "ps aux | grep a | grep b | grep c",        # more than three stages
])
def test_round1_mutating_or_unbounded_commands_are_not_read_only(hook, cmd):
    assert not hook._bash_is_read_only(cmd), cmd


@pytest.mark.parametrize("cmd", [
    "git fetch",
    "git fetch origin",
    "git -C /tmp/x log --oneline -5",           # the commander's own example
    "git -C /tmp/x status",
    "gh -R owner/repo pr list",
    "gh pr list -R owner/repo",
    "ps aux | grep foo | grep -v grep",         # the common idiom
    "grep -E 'a|b' file",                       # a quoted pipe is not a pipe
    "pgrep -fl x | head -5",
])
def test_round1_common_read_only_commands_are_accepted(hook, cmd):
    assert hook._bash_is_read_only(cmd), cmd


# ---------------------------------------------------------------------------
# review round 2 (PR 3947, Luria): the re-arm helper the loop skill and the nudge prescribe is an
# ARM action (a heartbeat turn that calls it is quiet); a Monitor is an arm only for the standard
# supervisor/bell command; three fringe read-only passes closed.
# ---------------------------------------------------------------------------

HELPER_CALLS = [
    # the REAL forms from a live seat's transcript (a quoted path is what the loop skill's snippet produces)
    'python3 "/Users/engram-sonnet/.engram/marketplace/plugins/engram/tools/loop_prompt.py" --rearm-delay --wake-kind other',
    "python3 /Users/x/.engram/marketplace/plugins/engram/tools/loop_prompt.py --rearm-delay --wake-kind heartbeat",
    "python3 loop_prompt.py --rearm-delay --wake-kind other",
    "delay=$(python3 ~/.engram/marketplace/plugins/engram/tools/loop_prompt.py --rearm-delay --wake-kind other)",
    "python3 tools/loop_prompt.py --rearm-delay --marker /tmp/m.json",
    "cd /x && python3 loop_prompt.py --rearm-delay",
]


@pytest.mark.parametrize("cmd", HELPER_CALLS)
def test_round2_the_prescribed_rearm_helper_is_read_only_enough_to_be_quiet(hook, cmd):
    assert hook._bash_is_read_only(cmd), cmd


@pytest.mark.parametrize("cmd", [
    "python3 loop_prompt.py --rearm-delay; rm x",
    "python3 loop_prompt.py --rearm-delay && rm x",
    "python3 loop_prompt.py --rearm-delay | sh",
    "python3 evil_loop_prompt.py --rearm-delay",
    "python3 loop_prompt.py --rearm-delay --wake-kind bogus",
    "python3 loop_prompt.py --kind working",
    "delay=$(python3 loop_prompt.py --rearm-delay; rm x)",
    "python3 loop_prompt.py --rearm-delay)",
    "x=$(python3 loop_prompt.py --rearm-delay",
])
def test_round2_the_helper_rule_is_whole_command_only(hook, cmd):
    assert not hook._bash_is_read_only(cmd), cmd


def test_round2_heartbeat_turn_with_the_helper_and_the_arm_is_quiet(hook):
    helper = _bash(HELPER_CALLS[2])
    assert hook._delta_is_quiet_wake(_delta(WAKE, helper, REARM))
    assert hook._delta_is_quiet_wake(_delta(WAKE, _tu("CronList"), helper, REARM))


def test_round2_a_helper_turn_that_also_wrote_something_is_not_quiet(hook):
    assert not hook._delta_is_quiet_wake(_delta(WAKE, _bash(HELPER_CALLS[0]), _tu("Write", {"file_path": "/x"}), REARM))


@pytest.mark.parametrize("cmd,ok", [
    ("UMWELT_BELL_QUIET_ARM=1 python3 /Users/x/.engram/marketplace/plugins/engram/tools/monitor-supervisor.py", True),
    ("python3 /x/umwelt-bell.py clio http://h ~/.umwelt/bell-cursors.json 5", True),
    # REAL Monitor commands from a live seat's transcript
    ("UMWELT_BELL_QUIET_ARM=1 python3 $HOME/.engram/marketplace/plugins/engram/tools/monitor-supervisor.py", True),
    ("UMWELT_BELL_QUIET_ARM=1 python3 $HOME/.engram/marketplace/plugins/engram/tools/monitor-supervisor.py --purpose clio-morning", True),
    ("python3 $HOME/.engram/marketplace/plugins/engram/tools/monitor-supervisor.py", True),
    ("bash /Users/x/.engram/marketplace/plugins/engram/tools/forum-updates-monitor.sh --kinds dm,baton", True),
    ("bash /Users/x/.engram/marketplace/plugins/engram/tools/forum-mention-monitor.sh", True),
    ("bash /Users/x/evil.sh", False),
    ("UMWELT_BELL_QUIET_ARM=1 python3 $HOME/monitor-supervisor.py $(rm x)", False),
    ("git status | head", True),                     # read-only watch
    ("rm -rf /tmp/x", False),
    ("python3 evil.py", False),
    ("UMWELT_BELL_QUIET_ARM=1 python3 monitor-supervisor.py; rm x", False),
    ("tail -f x > y", False),
])
def test_round2_monitor_is_an_arm_only_for_standard_commands(hook, cmd, ok):
    got = hook._delta_is_quiet_wake(_delta(NOTIF, _tu("Monitor", {"command": cmd, "timeout_ms": 1800000}), REARM))
    assert got is ok, cmd


def test_round2_monitor_websocket_form_has_no_command_and_is_an_arm(hook):
    assert hook._delta_is_quiet_wake(_delta(NOTIF, _tu("Monitor", {"ws": {"url": "wss://h/x"}}), REARM))


@pytest.mark.parametrize("cmd", [
    "date 101914302026",             # a bare positional sets the clock on BSD
    "git diff --textconv",           # runs a configured program
    "gh pr view 5 --web",            # opens a browser
    "gh issue list -w",
])
def test_round2_fringe_read_only_passes_are_closed(hook, cmd):
    assert not hook._bash_is_read_only(cmd), cmd


@pytest.mark.parametrize("cmd", ["date", "date +%s", "date -u +%Y-%m-%dT%H:%M:%SZ", "gh pr view 5", "git diff HEAD~1"])
def test_round2_control_ordinary_forms_stay_accepted(hook, cmd):
    assert hook._bash_is_read_only(cmd), cmd


# ---------------------------------------------------------------------------
# helpers for waiting_on tests
# ---------------------------------------------------------------------------

def _marker_with_waiting_on(tmp_path: Path, description: str, return_by: str) -> str:
    """Write a scheduleWakeup loop-mode.json with a waiting_on declaration."""
    p = tmp_path / "loop-mode-wo.json"
    p.write_text(json.dumps({
        "kind": "heartbeat",
        "pacer": "scheduleWakeup",
        "cadence_seconds": 1800,
        "waiting_on": {"description": description, "return_by": return_by},
    }), encoding="utf-8")
    return str(p)


def _future_ts() -> str:
    # Clearly within N (_WAITING_ON_NEAR_ZONE_MINUTES, ~10 min) — advisory zone.
    return (
        datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(minutes=5)
    ).strftime("%Y-%m-%dT%H:%M:%SZ")


def _near_zone_ts() -> str:
    # Just within N (9 min) — pins _WAITING_ON_NEAR_ZONE_MINUTES ≤ 9 would break this.
    return (
        datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(minutes=9)
    ).strftime("%Y-%m-%dT%H:%M:%SZ")


def _just_beyond_zone_ts() -> str:
    # Just beyond N (11 min) — pins _WAITING_ON_NEAR_ZONE_MINUTES ≥ 11 would break this.
    return (
        datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(minutes=11)
    ).strftime("%Y-%m-%dT%H:%M:%SZ")


def _far_future_ts() -> str:
    # Clearly beyond N — study-unit block zone on first trigger.
    return (
        datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=1)
    ).strftime("%Y-%m-%dT%H:%M:%SZ")


def _past_ts() -> str:
    return (
        datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=1)
    ).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------------------
# _read_waiting_on contract
# ---------------------------------------------------------------------------

def test_read_waiting_on_future_returns_pair(hook, tmp_path):
    p = _marker_with_waiting_on(tmp_path, "coder-fairy #4044", _future_ts())
    result = hook._read_waiting_on(p)
    assert result is not None
    desc, rb = result
    assert desc == "coder-fairy #4044"
    assert "T" in rb  # ISO form preserved

def test_read_waiting_on_past_returns_none(hook, tmp_path):
    p = _marker_with_waiting_on(tmp_path, "expired wait", _past_ts())
    assert hook._read_waiting_on(p) is None

def test_read_waiting_on_absent_returns_none(hook, tmp_path):
    p = tmp_path / "marker.json"
    p.write_text('{"kind": "heartbeat", "pacer": "scheduleWakeup"}', encoding="utf-8")
    assert hook._read_waiting_on(str(p)) is None

def test_read_waiting_on_not_dict_returns_none(hook, tmp_path):
    p = tmp_path / "marker.json"
    p.write_text('{"waiting_on": "not-a-dict"}', encoding="utf-8")
    assert hook._read_waiting_on(str(p)) is None

def test_read_waiting_on_missing_description_returns_none(hook, tmp_path):
    p = tmp_path / "marker.json"
    p.write_text(
        json.dumps({"waiting_on": {"return_by": _future_ts()}}), encoding="utf-8"
    )
    assert hook._read_waiting_on(str(p)) is None

def test_read_waiting_on_bad_timestamp_returns_none(hook, tmp_path):
    p = tmp_path / "marker.json"
    p.write_text(
        '{"waiting_on": {"description": "x", "return_by": "not-a-date"}}',
        encoding="utf-8",
    )
    assert hook._read_waiting_on(str(p)) is None

def test_read_waiting_on_missing_file_returns_none(hook, tmp_path):
    assert hook._read_waiting_on(str(tmp_path / "nonexistent.json")) is None


# ---------------------------------------------------------------------------
# advisory text contract
# ---------------------------------------------------------------------------

def test_advisory_text_shape(hook):
    t = hook._QUIET_ADVISORY_TEXT
    assert len(t) < 600
    assert "{n}" in t and "{desc}" in t and "{rb_str}" in t
    # Must NOT contain demand keywords — that would make it a demand, not advisory
    assert "study_queue" not in t
    assert "owner" not in t.lower()
    # Must reference the tick rule (case-insensitive)
    assert "tick" in t.lower()
    # Must be distinct from the demand text and study-unit text
    assert t != hook._QUIET_NUDGE_TEXT
    assert t != hook._QUIET_STUDY_UNIT_TEXT
    # Must not contain real node IDs
    import re
    assert not re.search(r"\b[a-z]{2}_\d{4}\b", t)


def test_study_unit_text_shape(hook):
    t = hook._QUIET_STUDY_UNIT_TEXT
    assert len(t) < 600
    assert "{desc}" in t and "{rb_str}" in t
    # Must reference "study unit" so _is_study_unit_nudge can detect it
    assert "study unit" in t.lower()
    # Must reference the tick rule
    assert "tick" in t.lower()
    # Must not contain the demand's marker keyword
    assert "study_queue" not in t
    # Must be distinct from both advisory and demand
    assert t != hook._QUIET_ADVISORY_TEXT
    assert t != hook._QUIET_NUDGE_TEXT
    # Must not contain real node IDs
    import re
    assert not re.search(r"\b[a-z]{2}_\d{4}\b", t)


def test_read_waiting_on_far_future_returns_pair(hook, tmp_path):
    """Far-future return_by is now active (no horizon filter in _read_waiting_on)."""
    p = _marker_with_waiting_on(tmp_path, "long-running CI", _far_future_ts())
    result = hook._read_waiting_on(p)
    assert result is not None
    desc, rb = result
    assert desc == "long-running CI"


# ---------------------------------------------------------------------------
# waiting_on interaction: Seat with waiting_on in marker
# ---------------------------------------------------------------------------

class SeatWithWaiting(Seat):
    """Seat whose loop-mode.json carries a future waiting_on declaration."""

    def __init__(self, tmp_path: Path, return_by: str | None = None):
        super().__init__(tmp_path)
        rb = return_by or _future_ts()
        marker_path = self.home / "loop-mode.json"
        data = json.loads(marker_path.read_text(encoding="utf-8"))
        data["waiting_on"] = {"description": "test-fairy", "return_by": rb}
        marker_path.write_text(json.dumps(data), encoding="utf-8")


def _is_advisory_nudge(out: str) -> bool:
    d = _decision(out)
    return (
        bool(d)
        and "systemMessage" in d
        and "decision" not in d
        and "tick" in d.get("systemMessage", "").lower()
    )


def _is_study_unit_nudge(out: str) -> bool:
    d = _decision(out)
    return bool(d) and d.get("decision") == "block" and "study unit" in d.get("reason", "").lower()


def test_waiting_on_active_two_quiet_emits_advisory_not_demand(tmp_path):
    """Within N: nudge fires advisory as systemMessage (no block) after threshold."""
    s = SeatWithWaiting(tmp_path)
    assert s.quiet() == ""            # tick 1, no nudge
    out = s.quiet()                   # tick 2, nudge due
    assert _is_advisory_nudge(out), f"expected advisory systemMessage, got: {out!r}"
    assert not _is_quiet_nudge(out), f"must NOT be a demand nudge, got: {out!r}"
    assert not _is_study_unit_nudge(out), f"must NOT be a study-unit block, got: {out!r}"
    msg = _decision(out)["systemMessage"]
    assert "test-fairy" in msg        # description threaded through


def test_waiting_on_expired_two_quiet_emits_demand(tmp_path):
    """With expired waiting_on (return_by in past), demand fires as normal."""
    s = SeatWithWaiting(tmp_path, return_by=_past_ts())
    assert s.quiet() == ""
    out = s.quiet()
    assert _is_quiet_nudge(out), f"expected demand nudge, got: {out!r}"
    assert not _is_advisory_nudge(out)


def test_waiting_on_counter_still_increments(tmp_path):
    """Counter increments even inside a known-return wait."""
    s = SeatWithWaiting(tmp_path)
    assert s.quiet() == ""
    assert s.ticks() == 1            # incremented despite waiting_on


def test_waiting_on_counter_resets_after_advisory(tmp_path):
    """Counter resets to 0 after an advisory nudge fires."""
    s = SeatWithWaiting(tmp_path)
    s.quiet()
    s.quiet()                        # advisory fires here, resets counter
    assert s.ticks() == 0


def test_waiting_on_beyond_n_first_trigger_emits_study_unit_block(tmp_path):
    """Beyond N: first trigger emits the once-only study-unit block (not demand)."""
    s = SeatWithWaiting(tmp_path, return_by=_far_future_ts())
    assert s.quiet() == ""
    out = s.quiet()
    assert _is_study_unit_nudge(out), f"expected study-unit block, got: {out!r}"
    assert not _is_quiet_nudge(out), f"must NOT be a demand nudge, got: {out!r}"
    assert not _is_advisory_nudge(out), f"must NOT be an advisory systemMessage, got: {out!r}"
    reason = _decision(out)["reason"]
    assert "test-fairy" in reason     # description threaded through


def test_waiting_on_beyond_n_once_only_then_advisory(tmp_path):
    """Beyond N: after the once-only block fires, subsequent triggers give advisory."""
    s = SeatWithWaiting(tmp_path, return_by=_far_future_ts())
    s.quiet()           # tick 1
    out1 = s.quiet()    # tick 2: once-only fires
    assert _is_study_unit_nudge(out1)
    s.quiet()           # tick 1 (counter reset)
    out2 = s.quiet()    # tick 2: advisory (no second block)
    assert _is_advisory_nudge(out2), f"expected advisory after flag set, got: {out2!r}"
    assert not _is_study_unit_nudge(out2), f"block must NOT fire twice, got: {out2!r}"


def test_waiting_on_9min_emits_advisory(tmp_path):
    """return_by at now+9 min is within N (~10) — advisory, never study-unit block.

    Pins _WAITING_ON_NEAR_ZONE_MINUTES: if it were mutated to >= 11 this cell
    stays green (still within zone); mutating it to <= 8 would move 9 min outside
    the zone and turn this red. The 11-min companion below is the load-bearing pin.
    """
    s = SeatWithWaiting(tmp_path, return_by=_near_zone_ts())
    s.quiet()
    out = s.quiet()
    assert _is_advisory_nudge(out), f"9-min wait: expected advisory, got: {out!r}"
    assert not _is_study_unit_nudge(out), f"9-min wait: must NOT be study-unit block"


def test_waiting_on_11min_emits_study_unit_block(tmp_path):
    """return_by at now+11 min is beyond N (~10) — study-unit block on first trigger.

    Pins _WAITING_ON_NEAR_ZONE_MINUTES: mutating it to >= 11 would keep 11 min
    inside the near zone and emit an advisory instead, turning this red.
    """
    s = SeatWithWaiting(tmp_path, return_by=_just_beyond_zone_ts())
    s.quiet()
    out = s.quiet()
    assert _is_study_unit_nudge(out), f"11-min wait: expected study-unit block, got: {out!r}"
    assert not _is_advisory_nudge(out), f"11-min wait: must NOT be advisory"
