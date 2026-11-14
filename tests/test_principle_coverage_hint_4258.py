"""#4258: principle_coverage's `cheapest_fix` names only actions that create a
channel the check actually counts, and depends on the node type.

The four counted channels (engram_stats, principle_coverage block):
registry_trigger (principle_triggers.json entry for the id), situation_pattern
(in the node's metadata), warm_briefing_anchor, claude_md.

Which tool writes which counted channel for which type (read from code, not
from the issue text; see the hand-off):
  lesson      engram_register_exemplar  -> exemplifies edge -> registry rebuild
              engram_update_lesson      -> metadata.situation_pattern (lessons only)
  cornerstone engram_register_exemplar  -> exemplifies edge -> registry rebuild
  axiom       engram_add_edge relation=instantiates -> registry rebuild
              (engram_register_exemplar refuses axiom targets)
  goal        engram_add_edge relation=serves       -> registry rebuild
              (engram_register_exemplar refuses goal targets)
  every type  an anchor line in warm-briefing.md or ~/.claude/CLAUDE.md.

Contract: A. hint depends on type and names only verified tools for that type.
B. every hint carries the always-works anchor action; axiom/goal hints never
name engram_register_exemplar / engram_lesson_register_incident.
C. following the hint's action moves the node out of `uncovered`.
D. output shape unchanged: a `cheapest_fix` string on each uncovered entry.
"""

from __future__ import annotations

import atexit
import json
import os
import re
import shutil
import sys
import tempfile
import unittest.mock as mock
from pathlib import Path

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

TEST_DIR = tempfile.mkdtemp(prefix="engram_test_pc_hint_4258_")
atexit.register(lambda: shutil.rmtree(TEST_DIR, ignore_errors=True))

TYPES = ("lesson", "cornerstone", "axiom", "goal")

# Tools whose code was verified to write a counted channel, per type.
VERIFIED_TOOLS = {
    "lesson": {"engram_register_exemplar", "engram_update_lesson"},
    "cornerstone": {"engram_register_exemplar"},
    "axiom": {"engram_add_edge"},
    "goal": {"engram_add_edge"},
}
FORBIDDEN_FOR_AXIOM_GOAL = ("engram_register_exemplar", "engram_lesson_register_incident")


def fresh_server():
    if os.path.exists(TEST_DIR):
        shutil.rmtree(TEST_DIR)
    for key in list(sys.modules):
        if key == "server" or key.startswith("engram_"):
            del sys.modules[key]
    import server
    server._configure_paths(TEST_DIR)
    server._ensure_data_dir()
    os.environ["ENGRAM_NO_EMBEDDINGS"] = "1"
    return server


def _call(fn, **payload):
    return json.loads(fn(payload_json=json.dumps(payload)))


def _seed_axiom(server, claim):
    r = _call(server.engram_add_axiom, claim=claim, basis="fixture basis")
    assert "error" not in r, r
    return r["axiom_id"]


def _seed_goal(server):
    r = _call(server.engram_add_goal, claim="Fixture goal aspiration.",
              motivation="fixture motivation")
    assert "error" not in r, r
    return r["goal_id"]


def _seed_cs(server, support):
    r = _call(server.engram_add_cornerstone, tag="hint-cs", title="Cornerstone: hint",
              new_frame="Principle for hint.", supporting_ids=support)
    assert "error" not in r, r
    return r["cornerstone_id"]


def _seed_lesson(server):
    inc = _seed_axiom(server, "Placeholder incident for the lesson.")
    r = _call(server.engram_add_lesson, claim="Fixture lesson pattern.",
              incident_ids=inc, logical_chain="Pattern derived from the incident.",
              scaffolding_nudge="Watch for this pattern.")
    assert "error" not in r, r
    return r["lesson_id"], inc


def seed_uncovered(server, core, ptype):
    """Return the id of one principle of `ptype` that no channel covers."""
    if ptype == "axiom":
        return _seed_axiom(server, "Uncovered axiom claim.")
    if ptype == "goal":
        return _seed_goal(server)
    if ptype == "cornerstone":
        return _seed_cs(server, _seed_axiom(server, "Support for the cornerstone."))
    lid, _inc = _seed_lesson(server)
    # A lesson gets a registry trigger from its incident_ids; drop that edge
    # so the lesson is genuinely uncovered.
    conn = core._get_db()
    try:
        conn.execute("DELETE FROM edges WHERE target_id = ? AND relation = 'exemplifies'", (lid,))
        conn.commit()
    finally:
        conn.close()
    core._rebuild_principle_triggers()
    return lid


_CALL_RE = re.compile(r"(engram_\w+)\(payload fields: (.*?)\)(?=;| writes|$)")


def _split_top(text):
    """Split on ',' outside {} and <>."""
    parts, depth, cur = [], 0, ""
    for ch in text:
        if ch in "{<":
            depth += 1
        elif ch in "}>":
            depth -= 1
        if ch == "," and depth == 0:
            parts.append(cur.strip())
            cur = ""
        else:
            cur += ch
    if cur.strip():
        parts.append(cur.strip())
    return parts


def parse_tool_calls(hint):
    """Parse every `tool(payload fields: a=b, c=d)` call the hint prints into
    [(tool, {name: raw_value})], in printed order."""
    calls = []
    for m in _CALL_RE.finditer(hint):
        args = {}
        for part in _split_top(m.group(2)):
            name, _, val = part.partition("=")
            args[name.strip()] = val.strip()
        calls.append((m.group(1), args))
    return calls


def dispatch_hint_call(server, tool, args, placeholder_id):
    """Run a parsed hint call: `<...>` placeholders become `placeholder_id`
    (updates is a JSON object whose placeholder becomes a real regex)."""
    payload = {}
    for name, raw in args.items():
        if name == "updates":
            payload[name] = json.loads(re.sub(r"<[^>]*>", "npm publish --tag next", raw))
        elif raw.startswith("<") and raw.endswith(">"):
            payload[name] = placeholder_id
        else:
            payload[name] = raw
    return _call(getattr(server, tool), **payload)


def run_coverage(fake_home):
    import engram_stats
    with mock.patch("engram_stats.Path") as m:
        m.home.return_value = fake_home
        r = json.loads(engram_stats._diagnose_impl())
    return r["principle_coverage"]


def uncovered_entry(pc, nid):
    return next((e for e in pc["uncovered"] if e["id"] == nid), None)


@pytest.fixture
def env(tmp_path):
    server = fresh_server()
    import engram_core as core
    return server, core, tmp_path / "no_home"


# ── A, B, D: hint text per type ──────────────────────────────────────────────

@pytest.mark.parametrize("ptype", TYPES)
def test_hint_shape_and_anchor_action(env, ptype):
    server, core, home = env
    nid = seed_uncovered(server, core, ptype)
    entry = uncovered_entry(run_coverage(home), nid)
    assert entry is not None, f"{ptype} {nid} should be uncovered"
    assert entry["type"] == ptype
    assert isinstance(entry["cheapest_fix"], str) and entry["cheapest_fix"]  # D
    assert f"add a one-line anchor naming {nid} to warm-briefing.md or ~/.claude/CLAUDE.md" \
        in entry["cheapest_fix"]  # B


@pytest.mark.parametrize("ptype", TYPES)
def test_hint_names_only_verified_tools(env, ptype):
    server, core, home = env
    nid = seed_uncovered(server, core, ptype)
    hint = uncovered_entry(run_coverage(home), nid)["cheapest_fix"]
    named = set(re.findall(r"engram_\w+", hint))
    assert named <= VERIFIED_TOOLS[ptype], (ptype, named - VERIFIED_TOOLS[ptype])


@pytest.mark.parametrize("ptype", ("axiom", "goal"))
def test_axiom_goal_hint_has_no_exemplar_tools(env, ptype):
    server, core, home = env
    nid = seed_uncovered(server, core, ptype)
    hint = uncovered_entry(run_coverage(home), nid)["cheapest_fix"]
    for name in FORBIDDEN_FOR_AXIOM_GOAL:
        assert name not in hint
    assert "exemplar" not in hint


def _hint(env, ptype):
    server, core, home = env
    nid = seed_uncovered(server, core, ptype)
    return nid, uncovered_entry(run_coverage(home), nid)["cheapest_fix"]


def test_axiom_hint_names_exact_relation(env):
    _nid, hint = _hint(env, "axiom")
    assert "relation=instantiates" in hint
    assert "relation=serves" not in hint
    assert "engram_add_edge" in hint


def test_goal_hint_names_exact_relation(env):
    _nid, hint = _hint(env, "goal")
    assert "relation=serves" in hint
    assert "relation=instantiates" not in hint
    assert "engram_add_edge" in hint


def test_lesson_and_cornerstone_hints_name_register_exemplar(env):
    server, core, home = env
    ls = seed_uncovered(server, core, "lesson")
    cs = seed_uncovered(server, core, "cornerstone")
    pc = run_coverage(home)
    assert "engram_register_exemplar" in uncovered_entry(pc, ls)["cheapest_fix"]
    assert "engram_register_exemplar" in uncovered_entry(pc, cs)["cheapest_fix"]
    assert "engram_update_lesson" in uncovered_entry(pc, ls)["cheapest_fix"]
    assert "engram_update_lesson" not in uncovered_entry(pc, cs)["cheapest_fix"]


def _field_sets(server):
    """The real payload field names, read from the tool code in server.py."""
    return {
        "engram_register_exemplar": set(server._REGISTER_EXEMPLAR_FIELDS),
        "engram_add_edge": set(server._ADD_EDGE_FIELDS),
        "engram_update_lesson": set(server._UPDATE_LESSON_FIELDS),
    }


@pytest.mark.parametrize("ptype", TYPES)
def test_every_printed_argument_name_is_a_real_payload_field(env, ptype):
    server, _core, _home = env
    _nid, hint = _hint(env, ptype)
    calls = parse_tool_calls(hint)
    assert calls, hint
    fields = _field_sets(server)
    for tool, args in calls:
        assert tool in fields, tool
        assert args, tool
        assert set(args) <= fields[tool], (tool, set(args) - fields[tool])


# ── C: follow the hint ───────────────────────────────────────────────────────

EXPECTED_RELATION = {"axiom": "instantiates", "goal": "serves"}


@pytest.mark.parametrize("ptype", TYPES)
def test_following_printed_first_action_clears_uncovered(env, ptype):
    """Parse the FIRST tool call out of the printed hint and dispatch it."""
    server, core, home = env
    nid, hint = _hint(env, ptype)
    assert uncovered_entry(run_coverage(home), nid) is not None
    tool, args = parse_tool_calls(hint)[0]
    if ptype in EXPECTED_RELATION:
        assert args.get("relation") == EXPECTED_RELATION[ptype], args
    ex = _seed_axiom(server, f"Exemplar/realizer for {nid}.")
    r = dispatch_hint_call(server, tool, args, ex)
    assert r.get("status") == "created", r
    assert uncovered_entry(run_coverage(home), nid) is None


def test_following_printed_update_lesson_action_clears_uncovered(env):
    server, core, home = env
    nid, hint = _hint(env, "lesson")
    calls = [c for c in parse_tool_calls(hint) if c[0] == "engram_update_lesson"]
    assert len(calls) == 1, hint
    r = dispatch_hint_call(server, calls[0][0], calls[0][1], nid)
    assert "error" not in r, r
    assert uncovered_entry(run_coverage(home), nid) is None


@pytest.mark.parametrize("surface", ("warm-briefing", "claude-md"))
@pytest.mark.parametrize("ptype", TYPES)
def test_following_anchor_action_clears_uncovered(env, ptype, surface):
    server, core, home = env
    nid = seed_uncovered(server, core, ptype)
    assert uncovered_entry(run_coverage(home), nid) is not None
    line = f"- {nid}: one-line anchor.\n"
    if surface == "warm-briefing":
        (Path(core.DATA_DIR) / "warm-briefing.md").write_text(line)
    else:
        (home / ".claude").mkdir(parents=True)
        (home / ".claude" / "CLAUDE.md").write_text(line)
    assert uncovered_entry(run_coverage(home), nid) is None
