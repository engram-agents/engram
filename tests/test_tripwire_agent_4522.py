"""Contract tests for alpha #4522: the lesson tripwire fires on Agent-tool dispatches.

Advisory only. Lei's standing ruling of 2026-10-18 limits blocking to ENGRAM and Umwelt
calls; a fairy dispatch is outside that set, so the prevention path never reaches an Agent
call, even for a lesson with prevention_type "blocking".

Contract:
  A1  must-fire: a pattern inside the first 500 chars of the prompt hits
  A2  must-not-fire: no pattern present -> no hit, no output
  A3  subagent_type is in the target; a target-anchored pattern fires on Agent, not on Bash
  A4  head boundary: a pattern only after char 500 of the prompt does not fire; ending at 500 does
  A5  match_scope command: sees subagent_type + description, NOT the prompt
  A6  advisory only: a blocking lesson matching an Agent payload -> advisory, no deny, no ledger row
  A7  control: the same lesson on an mcp__ call still denies
  A8  malformed tool_input / fields: no crash, exit 0, no deny
  A9  Bash and mcp__ targets and views are byte-identical to before
  A10 hooks.json registers an Agent matcher routing the tripwire hook
"""

from __future__ import annotations

import importlib.util
import io
import json
import os
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
_HOOK_PATH = PROJECT_ROOT / "src" / "engram" / "hooks" / "claude" / "engram-lesson-tripwire-hook.py"
_HOOKS_JSON = PROJECT_ROOT / "src" / "engram" / "hooks" / "hooks.json"

pytestmark = pytest.mark.usefixtures("isolate_hook_loads")

SUBAGENT = "engram:engram-pr-reviewer"


def _load_hook(home: str):
    old = os.environ.get("ENGRAM_HOME")
    os.environ["ENGRAM_HOME"] = home
    try:
        spec = importlib.util.spec_from_file_location(f"tripwire_hook_4522_{abs(hash(home))}", _HOOK_PATH)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod
    finally:
        if old is None:
            os.environ.pop("ENGRAM_HOME", None)
        else:
            os.environ["ENGRAM_HOME"] = old


@pytest.fixture
def home(tmp_path):
    h = tmp_path / "home"
    h.mkdir()
    return h


@pytest.fixture
def hook(home):
    return _load_hook(str(home))


def _agent(prompt="do the review", description="review PR", subagent_type=SUBAGENT):
    return {"tool_name": "Agent",
            "tool_input": {"description": description, "prompt": prompt, "subagent_type": subagent_type}}


def _bash(cmd):
    return {"tool_name": "Bash", "tool_input": {"command": cmd}}


def _mcp(tool, **args):
    return {"tool_name": tool, "tool_input": args}


def _hits(hook, payload, tripwires):
    full, command = hook.build_match_views(payload)
    return hook.check_command(full, tripwires, command_target=command)


# ------------------------------------------------------------------ A1 / A2
def test_a1_pattern_in_prompt_head_fires(hook):
    hits = _hits(hook, _agent(prompt="please run the frobnicate step first"), [("frobnicate", "n", "ls_a1")])
    assert [h["node_id"] for h in hits] == ["ls_a1"]


def test_a2_no_pattern_no_hit(hook):
    assert _hits(hook, _agent(prompt="nothing relevant"), [("frobnicate", "n", "ls_a2")]) == []


def test_a2_no_pattern_no_output(hook, home):
    out, code, rows = _run(hook, home, _agent(prompt="nothing relevant"), prevention=[], tripwires=[TW])
    assert code == 0 and out == "" and rows == []


# ------------------------------------------------------------------ A3
def test_a3_subagent_type_anchored_pattern_fires_on_agent_not_bash(hook):
    tw = [(r"^Agent engram:engram-pr-reviewer\b", "n", "ls_a3")]
    assert [h["node_id"] for h in _hits(hook, _agent(), tw)] == ["ls_a3"]
    assert _hits(hook, _bash("echo engram:engram-pr-reviewer"), tw) == []


# ------------------------------------------------------------------ A4
def test_a4_head_boundary(hook):
    needle = "ZQXJ"
    past = _agent(prompt="a" * 500 + needle)
    assert _hits(hook, past, [(needle, "n", "ls_a4")]) == []
    at = _agent(prompt="a" * (500 - len(needle)) + needle)
    assert len(at["tool_input"]["prompt"]) == 500
    assert [h["node_id"] for h in _hits(hook, at, [(needle, "n", "ls_a4")])] == ["ls_a4"]


def test_a4_head_constant(hook):
    assert hook.AGENT_PROMPT_HEAD_CHARS == 500


# ------------------------------------------------------------------ A5
def test_a5_command_scope_sees_subagent_type_not_prompt(hook):
    tw = [("engram-pr-reviewer", "n", "ls_a5", "normal", "command")]
    assert [h["node_id"] for h in _hits(hook, _agent(prompt="plain"), tw)] == ["ls_a5"]
    tw2 = [("promptonlytext", "n", "ls_a5b", "normal", "command")]
    assert _hits(hook, _agent(prompt="has promptonlytext inside"), tw2) == []
    # the same text on the default (full) scope does fire
    assert len(_hits(hook, _agent(prompt="has promptonlytext inside"), [("promptonlytext", "n", "x")])) == 1


def test_a5_views_shape(hook):
    full, command = hook.build_match_views(_agent(prompt="P", description="D"))
    assert command == f"Agent {SUBAGENT} D"
    assert full == f"Agent {SUBAGENT} D P"


# ------------------------------------------------------------------ end-to-end driver
def _run(hook, home, payload, *, prevention, tripwires=()):
    hook.load_tripwires = lambda *a, **k: list(tripwires)
    hook.load_prevention_lessons = lambda *a, **k: list(prevention)
    hook._query_recall_daemon = lambda q, top_k=5: None
    out = io.StringIO()
    old_in, old_out = sys.stdin, sys.stdout
    sys.stdin, sys.stdout = io.StringIO(json.dumps(payload)), out
    code = 0
    try:
        hook.main()
    except SystemExit as e:
        code = e.code or 0
    finally:
        sys.stdin, sys.stdout = old_in, old_out
    ledger = home / "lesson-prevention-ledger.jsonl"
    rows = [json.loads(line) for line in ledger.read_text().splitlines() if line.strip()] if ledger.exists() else []
    return out.getvalue(), code, rows


BLOCKING = {"id": "ls_block", "pattern": r"frobnicate", "prevention_type": "blocking", "nudge": "do not frobnicate"}
TW = ("frobnicate", "Nudge: frobnicate carefully.", "ls_block")


# ------------------------------------------------------------------ A6 / A7
def test_a6_blocking_lesson_is_advisory_only_on_agent(hook, home):
    out, code, rows = _run(hook, home, _agent(prompt="please frobnicate"), prevention=[BLOCKING], tripwires=[TW])
    assert code == 0
    assert "Nudge: frobnicate carefully." in out
    assert "permissionDecision" not in out and "deny" not in out
    assert rows == []


def test_a7_control_same_lesson_on_mcp_still_denies(hook, home):
    out, code, _ = _run(hook, home, _mcp("mcp__x__tool", body="please frobnicate"),
                        prevention=[BLOCKING], tripwires=[TW])
    assert code == 0
    assert json.loads(out)["hookSpecificOutput"]["permissionDecision"] == "deny"


# ------------------------------------------------------------------ A8
@pytest.mark.parametrize("ti", [
    None, "a string", ["x"], 7, {},
    {"description": 1, "prompt": None, "subagent_type": ["x"]},
    {"prompt": "frobnicate"},
    {"subagent_type": SUBAGENT},
])
def test_a8_malformed_agent_input_is_safe(hook, home, ti):
    payload = {"tool_name": "Agent", "tool_input": ti}
    hook.build_match_views(payload)
    hook.build_match_target(payload)
    out, code, _rows = _run(hook, home, payload, prevention=[BLOCKING], tripwires=[TW])
    assert code == 0
    assert "permissionDecision" not in out


def test_a8_non_dict_tool_input_gives_empty_target(hook):
    for ti in ("s", ["x"], 7):
        p = {"tool_name": "Agent", "tool_input": ti}
        assert hook.build_match_target(p) == ""
        assert hook.build_match_views(p) == ("", "")


def test_a8_missing_and_non_string_fields_count_as_empty(hook):
    assert hook.build_match_target({"tool_name": "Agent", "tool_input": {"prompt": "P"}}) == "Agent   P"
    assert hook.build_match_target(
        {"tool_name": "Agent", "tool_input": {"description": 1, "prompt": None, "subagent_type": []}}) == "Agent   "


# ------------------------------------------------------------------ A9
def test_a9_bash_and_mcp_unchanged(hook):
    assert hook.build_match_target(_bash("sed -n 1p f")) == "sed -n 1p f"
    assert hook.build_match_views(_bash("sed -n 1p f")) == ("sed -n 1p f", "sed -n 1p f")
    heredoc = "cat <<'EOF'\nbody\nEOF"
    assert hook.build_match_views(_bash(heredoc)) == (heredoc, "cat <<'EOF'\n<<HEREDOC>>\nEOF")
    m = _mcp("mcp__x__send", destination="d", body="hello")
    assert hook.build_match_target(m) == 'mcp__x__send {"destination":"d","body":"hello"}'
    assert hook.build_match_views(m) == (
        'mcp__x__send {"destination":"d","body":"hello"}',
        'mcp__x__send {"destination":"d","body":"<<TEXT>>"}')
    assert hook.build_match_target({"tool_name": "Read", "tool_input": {"file_path": "x"}}) == ""


# ------------------------------------------------------------------ A10
def test_a10_agent_matcher_registered():
    cfg = json.loads(_HOOKS_JSON.read_text())
    pre = cfg["hooks"]["PreToolUse"]
    entries = [e for e in pre if e.get("matcher") == "Agent"]
    assert len(entries) == 1
    hooks = entries[0]["hooks"]
    assert len(hooks) == 1 and "engram-lesson-tripwire-hook.py" in hooks[0]["command"]
    mcp = next(e for e in pre if e.get("matcher") == "mcp__.*")
    assert hooks[0]["command"] == mcp["hooks"][0]["command"]
    assert hooks[0]["timeout"] == mcp["hooks"][0]["timeout"]
