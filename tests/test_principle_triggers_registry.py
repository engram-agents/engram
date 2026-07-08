"""Tests for the unified principle_triggers.json registry (#1698 slice 1)
and its unified-read-path consumers (#1698 slice 2).

Coverage:
  §1 Rebuild — all four kinds derive from their edges (lesson/exemplifies,
     cornerstone/exemplifies, axiom/instantiates, goal/serves+tensions),
     nudge fallback to claim, tension mode tagged, bidirectional tensions,
     idempotency, superseded principles excluded.
  §2 Call-site wiring — register_exemplar, generic add_edge/remove_edge
     (serves from an observation — the Lei liberalization), and
     engram_add_task's serves edge all refresh the registry.
  §3 Migration shim — legacy caches still written alongside.
  §4 Trigger-side is_current fix (slice 2, #1698) — a retracted trigger
     node must be absent from the rebuilt registry, forward and
     bidirectional-reverse cases (Luria's catch).
  §5 Unified check_principle_triggers (slice 2, engram-surface-hook.py) —
     byte-compatible lesson/cornerstone renderings, new axiom/goal formats,
     cross-kind cooldown, cap + cap-loser cooldown non-stamping, missing-
     registry graceful "".
  §6 PreToolUse _QUERY broadening (slice 2, engram-lesson-tripwire-hook.py) —
     axiom/goal rows now returned; claim fallback when no nudge field set.
"""

from __future__ import annotations

import atexit
import importlib.util
import json
import os
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

TEST_DIR = tempfile.mkdtemp(prefix="engram_test_principle_triggers_")
atexit.register(lambda: shutil.rmtree(TEST_DIR, ignore_errors=True))


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


def _seed_axiom(server, claim, basis="test fixture basis"):
    r = json.loads(server.engram_add_axiom(payload_json=json.dumps({
        "claim": claim, "basis": basis,
    })))
    assert "error" not in r, r
    return r["axiom_id"]


def _seed_cs(server, supporting_ids, tag="cs-tag"):
    if isinstance(supporting_ids, list):
        supporting_ids = ",".join(supporting_ids)
    r = json.loads(server.engram_add_cornerstone(payload_json=json.dumps({
        "tag": tag, "title": f"Cornerstone: {tag}",
        "new_frame": f"Principle for {tag}.",
        "supporting_ids": supporting_ids,
    })))
    assert "error" not in r, r
    return r["cornerstone_id"]


def _seed_goal(server, aspiration="Test goal aspiration."):
    r = json.loads(server.engram_add_goal(payload_json=json.dumps({
        "claim": aspiration,
        "motivation": "test fixture motivation",
    })))
    assert "error" not in r, r
    return r["goal_id"]


def _seed_lesson(server, claim="Lesson pattern.", incident_id=None):
    if incident_id is None:
        incident_id = _seed_axiom(server, f"Placeholder incident for: {claim}")
    r = json.loads(server.engram_add_lesson(payload_json=json.dumps({
        "claim": claim,
        "incident_ids": incident_id,
        "logical_chain": f"Pattern derived from incidents: {claim}",
        "scaffolding_nudge": "Watch for this pattern.",
    })))
    assert "error" not in r, r
    return r["lesson_id"], incident_id


def _read_registry(core):
    with open(core.PRINCIPLE_TRIGGERS_PATH) as f:
        return json.load(f)


def _add_edge(server, src, dst, relation):
    r = json.loads(server.engram_add_edge(payload_json=json.dumps({
        "source_id": src, "target_id": dst, "relation": relation,
    })))
    assert r.get("status") == "created", r
    return r


# ---------------------------------------------------------------------------
# §1 Rebuild
# ---------------------------------------------------------------------------


def test_rebuild_all_four_kinds():
    server = fresh_server()
    import engram_core as core

    lesson_id, incident_id = _seed_lesson(server)
    sup = _seed_axiom(server, "Support for the registry cornerstone.")
    cs_id = _seed_cs(server, sup, tag="registry-cs")
    cs_ex = _seed_axiom(server, "Concrete cornerstone exemplar situation.")
    json.loads(server.engram_register_exemplar(payload_json=json.dumps({
        "target_id": cs_id, "exemplar_id": cs_ex,
    })))

    ax_id = _seed_axiom(server, "Axiom under trigger test.")
    ax_inst = _seed_axiom(server, "A grounding instance of the axiom.")
    _add_edge(server, ax_inst, ax_id, "instantiates")

    goal_id = _seed_goal(server)
    goal_ob = _seed_axiom(server, "A thought that relates to the goal.")
    _add_edge(server, goal_ob, goal_id, "serves")

    result = core._rebuild_principle_triggers()
    assert result["status"] == "rebuilt"
    reg = _read_registry(core)

    assert reg[incident_id]["kind"] == "lesson"
    assert reg[incident_id]["nudge"] == "Watch for this pattern."
    assert reg[cs_ex]["kind"] == "cornerstone"
    assert reg[cs_ex]["principle_id"] == cs_id
    assert reg[ax_inst]["kind"] == "axiom"
    assert reg[ax_inst]["principle_id"] == ax_id
    assert reg[goal_ob]["kind"] == "goal"
    assert reg[goal_ob]["principle_id"] == goal_id
    assert "mode" not in reg[goal_ob]
    # nudge falls back to the principle claim when no surfacing_nudge
    assert reg[goal_ob]["nudge"] == reg[goal_ob]["claim"]
    assert result["by_kind"]["goal"] >= 1


def test_tensions_edge_tags_mode_and_is_bidirectional():
    server = fresh_server()
    import engram_core as core

    goal_id = _seed_goal(server, aspiration="Goal with tension exemplars.")
    ob_a = _seed_axiom(server, "A situation that strained the goal (source side).")
    ob_b = _seed_axiom(server, "Another strained situation (target side).")
    _add_edge(server, ob_a, goal_id, "tensions")   # node → goal
    _add_edge(server, goal_id, ob_b, "tensions")   # goal → node

    core._rebuild_principle_triggers()
    reg = _read_registry(core)
    assert reg[ob_a]["mode"] == "tension"
    assert reg[ob_a]["principle_id"] == goal_id
    assert reg[ob_b]["mode"] == "tension", "goal→node tensions direction missed"
    assert reg[ob_b]["principle_id"] == goal_id


def test_rebuild_idempotent_and_excludes_superseded():
    server = fresh_server()
    import engram_core as core

    goal_id = _seed_goal(server)
    ob = _seed_axiom(server, "Serves exemplar for idempotency test.")
    _add_edge(server, ob, goal_id, "serves")

    r1 = core._rebuild_principle_triggers()
    reg1 = _read_registry(core)
    r2 = core._rebuild_principle_triggers()
    reg2 = _read_registry(core)
    assert reg1 == reg2
    assert r1["trigger_count"] == r2["trigger_count"]


# ---------------------------------------------------------------------------
# §2 Call-site wiring
# ---------------------------------------------------------------------------


def test_generic_serves_edge_from_observation_refreshes_registry():
    """The Lei liberalization (design doc §7): a serves edge from any
    claim-bearing node — created via the GENERIC edge tool, no task — lands
    in the registry with kind=goal, with no manual rebuild call."""
    server = fresh_server()
    import engram_core as core

    goal_id = _seed_goal(server)
    thought = _seed_axiom(server, "This thought relates to my goal.")
    _add_edge(server, thought, goal_id, "serves")

    reg = _read_registry(core)
    assert thought in reg, "add_edge(serves) did not refresh the registry"
    assert reg[thought]["kind"] == "goal"

    # remove_edge resyncs too
    r = json.loads(server.engram_remove_edge(payload_json=json.dumps({
        "source_id": thought, "target_id": goal_id, "relation": "serves",
    })))
    assert r.get("status") == "removed", r
    reg = _read_registry(core)
    assert thought not in reg, "remove_edge(serves) left a stale trigger"


def test_register_exemplar_refreshes_registry():
    server = fresh_server()
    import engram_core as core

    sup = _seed_axiom(server, "Support for wiring cornerstone.")
    cs_id = _seed_cs(server, sup, tag="wiring-cs")
    ex = _seed_axiom(server, "Wiring exemplar.")
    json.loads(server.engram_register_exemplar(payload_json=json.dumps({
        "target_id": cs_id, "exemplar_id": ex,
    })))
    reg = _read_registry(core)
    assert ex in reg and reg[ex]["kind"] == "cornerstone"


def test_add_task_serves_refreshes_registry():
    server = fresh_server()
    import engram_core as core

    goal_id = _seed_goal(server)
    r = json.loads(server.engram_add_task(payload_json=json.dumps({
        "description": "Task serving the goal (wired-through-add-task test)",
        "goal_id": goal_id,
    })))
    assert "error" not in r, r
    task_id = r["task_id"]
    reg = _read_registry(core)
    assert task_id in reg and reg[task_id]["kind"] == "goal"


# ---------------------------------------------------------------------------
# §3 Migration shim
# ---------------------------------------------------------------------------


def test_legacy_caches_still_written():
    server = fresh_server()
    import engram_core as core

    _seed_lesson(server)
    sup = _seed_axiom(server, "Support for shim cornerstone.")
    cs_id = _seed_cs(server, sup, tag="shim-cs")
    ex = _seed_axiom(server, "Shim exemplar.")
    json.loads(server.engram_register_exemplar(payload_json=json.dumps({
        "target_id": cs_id, "exemplar_id": ex,
    })))

    assert os.path.exists(core.ERROR_INCIDENTS_PATH)
    assert os.path.exists(core.CORNERSTONE_ANCHORS_PATH)
    assert os.path.exists(core.PRINCIPLE_TRIGGERS_PATH)
    # And the unified registry agrees with the legacy cornerstone cache
    with open(core.CORNERSTONE_ANCHORS_PATH) as f:
        legacy = json.load(f)
    reg = _read_registry(core)
    assert ex in legacy and ex in reg
    assert legacy[ex]["cornerstone_id"] == reg[ex]["principle_id"]


# ---------------------------------------------------------------------------
# §4 Trigger-side is_current fix (slice 2, #1698 -- Luria's catch)
# ---------------------------------------------------------------------------


def _retract(server, node_id, reason="test retraction"):
    r = json.loads(server.engram_retract(payload_json=json.dumps({
        "node_id": node_id, "error_type": "other", "reason": reason,
    })))
    assert "error" not in r, r
    return r


def test_forward_trigger_is_current_excludes_retracted():
    """A retracted trigger node (source side of the forward exemplifies
    query) must be absent from the rebuilt registry, not just the
    principle side."""
    server = fresh_server()
    import engram_core as core

    lesson_id, incident_id = _seed_lesson(server, claim="Retracted-trigger lesson.")
    core._rebuild_principle_triggers()
    reg = _read_registry(core)
    assert incident_id in reg, "sanity: trigger present before retraction"

    _retract(server, incident_id)
    core._rebuild_principle_triggers()
    reg = _read_registry(core)
    assert incident_id not in reg, (
        "retracted trigger node must be absent from the rebuilt registry "
        "(forward query trigger-side is_current fix)"
    )


def test_bidirectional_reverse_trigger_is_current_excludes_retracted():
    """A retracted trigger node on the bidirectional-reverse (goal→node
    tensions) side must be absent from the rebuilt registry."""
    server = fresh_server()
    import engram_core as core

    goal_id = _seed_goal(server, aspiration="Goal with tension exemplar to retract.")
    ob_b = _seed_axiom(server, "Strained situation on the goal-to-node tensions side.")
    _add_edge(server, goal_id, ob_b, "tensions")  # goal -> node (reverse-query direction)

    core._rebuild_principle_triggers()
    reg = _read_registry(core)
    assert ob_b in reg, "sanity: trigger present before retraction"

    _retract(server, ob_b)
    core._rebuild_principle_triggers()
    reg = _read_registry(core)
    assert ob_b not in reg, (
        "retracted trigger node (bidirectional-reverse side) must be absent "
        "from the rebuilt registry"
    )


# ---------------------------------------------------------------------------
# §5 Unified check_principle_triggers (slice 2, engram-surface-hook.py)
# ---------------------------------------------------------------------------

_SURFACE_HOOK_PATH = PROJECT_ROOT / "src" / "engram" / "hooks" / "claude" / "engram-surface-hook.py"


def _load_surface_hook(engram_home, monkeypatch):
    """Import the surface hook as a module against a scratch ENGRAM_HOME
    (hyphenated filename, not importable via normal import -- same pattern
    as test_surface_hook_mcp_liveness.py)."""
    monkeypatch.setenv("ENGRAM_HOME", str(engram_home))
    sys.modules.pop("engram_surface_hook_pt", None)
    spec = importlib.util.spec_from_file_location(
        "engram_surface_hook_pt", str(_SURFACE_HOOK_PATH)
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _write_json(path, data):
    with open(path, "w") as f:
        json.dump(data, f)


# Frozen transcriptions of the pre-unification per-entry render logic
# (verbatim from the now-deleted check_incident_tripwire /
# _check_cornerstone_anchor_inner). These are the byte-compat ground truth
# the spec asks for -- NOT re-derived, copied from the code as it stood
# immediately before this slice's edit (see handoff notes: this is a
# judgment call -- a hardcoded frozen transcription rather than dynamically
# re-executing the deleted functions from git history).
def _old_lesson_tripwire_line(lesson_id, lesson_claim, nudge, mid):
    return (
        f"[ENGRAM Tripwire ({lesson_id}): {lesson_claim}]\n"
        f"  Action: {nudge}\n"
        f"  (Triggered by incident match: {mid})"
    )


def _old_cornerstone_anchor_line(cid, anchor):
    return f"[Cornerstone anchor ({cid})]: {anchor}"


def test_check_principle_triggers_lesson_byte_compatible(tmp_path, monkeypatch):
    """A lesson-kind hit renders byte-identical to pre-unification
    check_incident_tripwire output for the same fixture."""
    mod = _load_surface_hook(tmp_path, monkeypatch)
    registry = {
        "ob_9001": {
            "principle_id": "ls_9001",
            "kind": "lesson",
            "claim": "Test lesson claim.",
            "nudge": "Do the corrective action.",
        }
    }
    _write_json(tmp_path / "principle_triggers.json", registry)
    result = mod.check_principle_triggers(["ob_9001"], prompt_count=5)
    expected = _old_lesson_tripwire_line(
        "ls_9001", "Test lesson claim.", "Do the corrective action.", "ob_9001"
    )
    assert result == expected


def test_check_principle_triggers_cornerstone_byte_compatible(tmp_path, monkeypatch):
    """A cornerstone-kind hit renders byte-identical to pre-unification
    check_cornerstone_anchor output for the same fixture."""
    mod = _load_surface_hook(tmp_path, monkeypatch)
    registry = {
        "ob_9002": {
            "principle_id": "cs_9002",
            "kind": "cornerstone",
            "claim": "Cornerstone claim text.",
            "nudge": "Anchor line for the cornerstone.",
        }
    }
    _write_json(tmp_path / "principle_triggers.json", registry)
    result = mod.check_principle_triggers(["ob_9002"], prompt_count=5)
    expected = _old_cornerstone_anchor_line(
        "cs_9002", "Anchor line for the cornerstone."
    )
    assert result == expected


def test_check_principle_triggers_axiom_and_goal_new_format(tmp_path, monkeypatch):
    """axiom-kind and goal-kind hits render the new register-tagged format
    (no prior rendering to preserve for these kinds)."""
    mod = _load_surface_hook(tmp_path, monkeypatch)
    registry = {
        "ob_9003": {
            "principle_id": "ax_9003",
            "kind": "axiom",
            "claim": "Axiom claim.",
            "nudge": "Axiom nudge text.",
        },
        "ob_9004": {
            "principle_id": "gl_9004",
            "kind": "goal",
            "claim": "Goal claim.",
            "nudge": "Goal nudge text.",
        },
    }
    _write_json(tmp_path / "principle_triggers.json", registry)
    result = mod.check_principle_triggers(["ob_9003", "ob_9004"], prompt_count=5)
    lines = result.split("\n")
    assert "[Principle trigger (ax_9003, constraining)]: Axiom claim. → Constraint: Axiom nudge text." in lines
    assert "[Principle trigger (gl_9004, directional)]: Goal claim. → Serves: Goal nudge text." in lines


def test_check_principle_triggers_cooldown_suppresses_regardless_of_kind(tmp_path, monkeypatch):
    """A principle within PRINCIPLE_TRIGGER_COOLDOWN_PROMPTS of its last fire
    is suppressed regardless of kind -- the lesson-cadence behavior change
    (lessons previously had NO cross-prompt cooldown)."""
    mod = _load_surface_hook(tmp_path, monkeypatch)
    registry = {
        "ob_9005": {
            "principle_id": "ls_9005",
            "kind": "lesson",
            "claim": "Cooldown lesson claim.",
            "nudge": "Cooldown lesson nudge.",
        }
    }
    _write_json(tmp_path / "principle_triggers.json", registry)
    _write_json(tmp_path / "principle-trigger-state.json", {"ls_9005": 10})

    # prompt_count=12 is only 2 prompts after last fire (10) -- within the
    # 10-prompt cooldown window -- must be suppressed.
    result = mod.check_principle_triggers(["ob_9005"], prompt_count=12)
    assert result == "", "principle within cooldown window must be suppressed regardless of kind"

    # prompt_count=21 is 11 prompts after last fire -- cooldown expired.
    result = mod.check_principle_triggers(["ob_9005"], prompt_count=21)
    assert result != "", "principle past cooldown window must fire again"


def test_check_principle_triggers_cap_and_cap_loser_not_stamped(tmp_path, monkeypatch):
    """3+ simultaneous candidates across mixed kinds -> exactly 2 render, in
    lesson > axiom > cornerstone > goal priority order; the cap-losing
    candidate's cooldown state is NOT stamped (still eligible next prompt)."""
    mod = _load_surface_hook(tmp_path, monkeypatch)
    registry = {
        "ob_l": {"principle_id": "ls_cap", "kind": "lesson", "claim": "Lesson claim.", "nudge": "Lesson nudge."},
        "ob_a": {"principle_id": "ax_cap", "kind": "axiom", "claim": "Axiom claim.", "nudge": "Axiom nudge."},
        "ob_c": {"principle_id": "cs_cap", "kind": "cornerstone", "claim": "CS claim.", "nudge": "CS nudge."},
        "ob_g": {"principle_id": "gl_cap", "kind": "goal", "claim": "Goal claim.", "nudge": "Goal nudge."},
    }
    _write_json(tmp_path / "principle_triggers.json", registry)

    result = mod.check_principle_triggers(["ob_l", "ob_a", "ob_c", "ob_g"], prompt_count=5)
    lines = result.split("\n\n") if "\n\n" in result else None
    # Rendered lines: lesson is multi-line, axiom/cornerstone/goal are
    # single-line -- just check which principle_ids appear at all.
    assert "ls_cap" in result and "ax_cap" in result, "top-2 priority candidates (lesson, axiom) must render"
    assert "cs_cap" not in result and "gl_cap" not in result, "cap must exclude cornerstone/goal (lower priority)"

    with open(tmp_path / "principle-trigger-state.json") as f:
        state = json.load(f)
    assert state.get("ls_cap") == 5
    assert state.get("ax_cap") == 5
    assert "cs_cap" not in state, "cap-losing candidate must NOT be stamped in cooldown state"
    assert "gl_cap" not in state, "cap-losing candidate must NOT be stamped in cooldown state"


def test_check_principle_triggers_unknown_kind_does_not_consume_cap_slot(tmp_path, monkeypatch):
    """Colleague-review catch (Ariadne, PR #1713, corroborating the
    reviewer-fairy's own flag): an unrecognized `kind` must not consume a cap
    slot or a cooldown stamp just because it sorted into the candidate list --
    it must be filtered out BEFORE the cap slice, not merely skipped in the
    render loop (which runs AFTER the slice and only skips producing a line,
    too late to free the slot for a real candidate)."""
    mod = _load_surface_hook(tmp_path, monkeypatch)
    registry = {
        "ob_l": {"principle_id": "ls_slot", "kind": "lesson", "claim": "Lesson claim.", "nudge": "Lesson nudge."},
        "ob_u": {"principle_id": "zz_slot", "kind": "unknown_future_kind", "claim": "Unknown claim.", "nudge": "Unknown nudge."},
        "ob_g": {"principle_id": "gl_slot", "kind": "goal", "claim": "Goal claim.", "nudge": "Goal nudge."},
    }
    _write_json(tmp_path / "principle_triggers.json", registry)

    result = mod.check_principle_triggers(["ob_l", "ob_u", "ob_g"], prompt_count=5)
    assert "ls_slot" in result, "known-kind candidate must render"
    assert "gl_slot" in result, "goal must still get a cap slot -- the unknown kind must not have consumed it"
    assert "zz_slot" not in result, "unrecognized kind must never render"

    with open(tmp_path / "principle-trigger-state.json") as f:
        state = json.load(f)
    assert "zz_slot" not in state, "unrecognized kind must not be cooldown-stamped either"


def test_check_principle_triggers_missing_registry_returns_empty(tmp_path, monkeypatch):
    """Missing/absent principle_triggers.json -> check_principle_triggers
    returns "" (the §2.2 deviation-from-doc behavior) rather than raising."""
    mod = _load_surface_hook(tmp_path, monkeypatch)
    assert not (tmp_path / "principle_triggers.json").exists()
    result = mod.check_principle_triggers(["ob_anything"], prompt_count=1)
    assert result == ""


# ---------------------------------------------------------------------------
# §6 PreToolUse _QUERY broadening (slice 2, engram-lesson-tripwire-hook.py)
# ---------------------------------------------------------------------------

_LESSON_TRIPWIRE_HOOK_PATH = (
    PROJECT_ROOT / "src" / "engram" / "hooks" / "claude" / "engram-lesson-tripwire-hook.py"
)

_MINIMAL_NODES_SCHEMA = """
CREATE TABLE IF NOT EXISTS nodes (
    id TEXT PRIMARY KEY,
    type TEXT NOT NULL,
    is_current INTEGER NOT NULL DEFAULT 1,
    memory_status TEXT NOT NULL DEFAULT 'active',
    metadata TEXT NOT NULL DEFAULT '{}',
    claim TEXT NOT NULL DEFAULT '',
    confidence REAL NOT NULL DEFAULT 0.5,
    created_at TEXT NOT NULL DEFAULT '',
    logical_chain TEXT NOT NULL DEFAULT '',
    confidence_history TEXT NOT NULL DEFAULT '[]'
)
"""


def _load_lesson_tripwire_hook():
    spec = importlib.util.spec_from_file_location(
        "lesson_tripwire_hook_pt", str(_LESSON_TRIPWIRE_HOOK_PATH)
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _make_minimal_db(tmp_path):
    db = tmp_path / "knowledge.db"
    conn = sqlite3.connect(str(db))
    conn.execute(_MINIMAL_NODES_SCHEMA)
    conn.commit()
    conn.close()
    return db


def _insert_node(db, node_id, node_type, *, pattern="", metadata=None, claim="test claim"):
    meta = dict(metadata or {})
    if pattern:
        meta["situation_pattern"] = pattern
    conn = sqlite3.connect(str(db))
    conn.execute(
        "INSERT INTO nodes (id, type, is_current, memory_status, metadata, claim) "
        "VALUES (?, ?, 1, 'active', ?, ?)",
        (node_id, node_type, json.dumps(meta), claim),
    )
    conn.commit()
    conn.close()


def test_load_tripwires_returns_axiom_and_goal_rows_with_surfacing_nudge(tmp_path):
    """axiom/goal rows with only surfacing_nudge set are now returned by
    load_tripwires() -- previously excluded by the type IN ('lesson',
    'cornerstone') filter."""
    hook = _load_lesson_tripwire_hook()
    db = _make_minimal_db(tmp_path)
    _insert_node(
        db, "ax_1001", "axiom",
        pattern=r"some-axiom-pattern",
        metadata={"surfacing_nudge": "Axiom surfacing nudge."},
        claim="Axiom claim text.",
    )
    _insert_node(
        db, "gl_1002", "goal",
        pattern=r"some-goal-pattern",
        metadata={"surfacing_nudge": "Goal surfacing nudge."},
        claim="Goal claim text.",
    )
    tripwires = hook.load_tripwires(db_path=db)
    nudges = [n for _p, n in tripwires]
    assert "Axiom surfacing nudge." in nudges
    assert "Goal surfacing nudge." in nudges


def test_load_tripwires_falls_back_to_claim_when_no_nudge_field(tmp_path):
    """A row with situation_pattern but no nudge field (no
    scaffolding_nudge/anchor_line/surfacing_nudge) falls back to claim
    instead of being silently dropped by load_tripwires' `if row[0] and
    row[1]` filter."""
    hook = _load_lesson_tripwire_hook()
    db = _make_minimal_db(tmp_path)
    _insert_node(
        db, "cs_1003", "cornerstone",
        pattern=r"some-cornerstone-pattern",
        metadata={},  # no nudge field at all
        claim="Cornerstone claim used as fallback nudge.",
    )
    tripwires = hook.load_tripwires(db_path=db)
    nudges = [n for _p, n in tripwires]
    assert "Cornerstone claim used as fallback nudge." in nudges, (
        "row with situation_pattern but no nudge field must fall back to "
        "claim, not be silently dropped"
    )
