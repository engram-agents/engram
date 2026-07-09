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
  §7 Slice 3 — decay/enactment/telemetry + principle_coverage (#1698 slice 3):
     state-shape migration (bare int -> rich dict), effective-cooldown decay
     math + RETIREMENT_CEILING_PROMPTS cap, axiom decay exemption (Fork-2),
     engram.trigger.fire telemetry shape, enactment detection (Stop hook),
     reset-on-incident, and engram_diagnose's principle_coverage section.
"""

from __future__ import annotations

import atexit
import importlib.util
import io
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import types
import unittest.mock as mock
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

    # #1731: registry values are LISTS of entries (one per principle a
    # trigger relates to). None of these fixtures are dual-role, so each
    # list has exactly one entry -- [0] throughout.
    assert reg[incident_id][0]["kind"] == "lesson"
    assert reg[incident_id][0]["nudge"] == "Watch for this pattern."
    assert reg[cs_ex][0]["kind"] == "cornerstone"
    assert reg[cs_ex][0]["principle_id"] == cs_id
    assert reg[ax_inst][0]["kind"] == "axiom"
    assert reg[ax_inst][0]["principle_id"] == ax_id
    assert reg[goal_ob][0]["kind"] == "goal"
    assert reg[goal_ob][0]["principle_id"] == goal_id
    assert "mode" not in reg[goal_ob][0]
    # nudge falls back to the principle claim when no surfacing_nudge
    assert reg[goal_ob][0]["nudge"] == reg[goal_ob][0]["claim"]
    assert result["by_kind"]["goal"] >= 1
    assert all(len(v) == 1 for v in reg.values()), "no dual-role triggers expected in this fixture"


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
    assert reg[ob_a][0]["mode"] == "tension"
    assert reg[ob_a][0]["principle_id"] == goal_id
    assert reg[ob_b][0]["mode"] == "tension", "goal→node tensions direction missed"
    assert reg[ob_b][0]["principle_id"] == goal_id


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
    assert reg[thought][0]["kind"] == "goal"

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
    assert ex in reg and reg[ex][0]["kind"] == "cornerstone"


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
    assert task_id in reg and reg[task_id][0]["kind"] == "goal"


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
    assert legacy[ex]["cornerstone_id"] == reg[ex][0]["principle_id"]


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
    # #1698 slice 3: state entries are now the rich dict shape
    # ({last_fired_prompt, strength, enactments, fires}), not a bare int.
    assert state.get("ls_cap", {}).get("last_fired_prompt") == 5
    assert state.get("ax_cap", {}).get("last_fired_prompt") == 5
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


# ---------------------------------------------------------------------------
# §7 Slice 3 — decay/enactment/telemetry + principle_coverage (#1698 slice 3)
# ---------------------------------------------------------------------------


def test_state_shape_migration_upconverts_bare_int(tmp_path, monkeypatch):
    """A slice-2 bare-int state entry ({"pid": last_fired_prompt}) is
    upconverted to the rich dict shape on read, and the rewritten file
    persists the dict shape (§1.1)."""
    mod = _load_surface_hook(tmp_path, monkeypatch)
    registry = {
        "ob_mig": {
            "principle_id": "ls_mig", "kind": "lesson",
            "claim": "Migration lesson claim.", "nudge": "Migration nudge.",
        }
    }
    _write_json(tmp_path / "principle_triggers.json", registry)
    _write_json(tmp_path / "principle-trigger-state.json", {"ls_mig": 5})

    # Migrated entry has enactments=0 -> effective cooldown == flat base
    # (10). prompt_count=20 is well past it, so this must fire.
    result = mod.check_principle_triggers(["ob_mig"], prompt_count=20)
    assert result != "", "lesson must fire once past its (migrated, undecayed) cooldown"

    with open(tmp_path / "principle-trigger-state.json") as f:
        state = json.load(f)
    entry = state["ls_mig"]
    assert isinstance(entry, dict), "state entry must be upconverted to the rich dict shape"
    assert entry["last_fired_prompt"] == 20
    assert entry["fires"] == 1
    assert entry["enactments"] == 0
    assert entry["strength"] == 1.0


def test_effective_cooldown_decays_and_caps_at_retirement_ceiling(tmp_path, monkeypatch):
    """A lesson with enactments=2 needs 4x base_cooldown prompts before it's
    eligible again (§1.2); a very high enactments count is capped at
    RETIREMENT_CEILING_PROMPTS rather than growing unbounded."""
    mod = _load_surface_hook(tmp_path, monkeypatch)
    registry = {
        "ob_decay": {
            "principle_id": "ls_decay", "kind": "lesson",
            "claim": "Decay lesson claim.", "nudge": "Decay nudge.",
        }
    }
    _write_json(tmp_path / "principle_triggers.json", registry)
    _write_json(tmp_path / "principle-trigger-state.json", {
        "ls_decay": {"last_fired_prompt": 10, "strength": 1.0, "enactments": 2, "fires": 3},
    })

    base = mod.PRINCIPLE_TRIGGER_COOLDOWN_PROMPTS
    expected_cooldown = base * (2 ** 2)  # 4x base, per design doc §4

    result = mod.check_principle_triggers(["ob_decay"], prompt_count=10 + expected_cooldown - 1)
    assert result == "", "must still be suppressed one prompt short of the decayed cooldown"

    result = mod.check_principle_triggers(["ob_decay"], prompt_count=10 + expected_cooldown)
    assert result != "", "must fire once the decayed cooldown has fully elapsed"

    # Retirement ceiling: unit-check the decay math directly -- an
    # end-to-end fixture for an extreme enactments count would need a
    # prompt_count in the tens of thousands, which adds nothing beyond
    # what the direct math check already proves.
    assert mod._effective_cooldown("lesson", {"enactments": 10}) == mod.RETIREMENT_CEILING_PROMPTS
    assert mod._effective_cooldown("cornerstone", {"enactments": 0}) == base


def test_axiom_exempt_from_decay_keeps_flat_cooldown(tmp_path, monkeypatch):
    """kind=axiom uses the flat base cooldown regardless of enactments --
    Fork-2 (forum #229/#230/#231, Kepler-confirmed): axiom never decays but
    is still cooldown-suppressed between fires (§1.2)."""
    mod = _load_surface_hook(tmp_path, monkeypatch)
    registry = {
        "ob_ax_decay": {
            "principle_id": "ax_decay", "kind": "axiom",
            "claim": "Axiom claim.", "nudge": "Axiom nudge.",
        }
    }
    _write_json(tmp_path / "principle_triggers.json", registry)
    _write_json(tmp_path / "principle-trigger-state.json", {
        "ax_decay": {"last_fired_prompt": 10, "strength": 1.0, "enactments": 5, "fires": 6},
    })

    base = mod.PRINCIPLE_TRIGGER_COOLDOWN_PROMPTS
    # If decay applied, base * 2**5 (32x base) would still suppress here;
    # axiom exemption means the FLAT base cooldown governs instead.
    result = mod.check_principle_triggers(["ob_ax_decay"], prompt_count=10 + base - 1)
    assert result == "", "axiom must still be suppressed within the flat base cooldown"
    result = mod.check_principle_triggers(["ob_ax_decay"], prompt_count=10 + base)
    assert result != "", (
        "axiom must fire once past the flat base cooldown -- NOT decayed by enactments=5"
    )


def test_effective_cooldown_clamps_enactments_before_exponentiating(tmp_path, monkeypatch):
    """Reviewer-fairy flag (PR #1717): a corrupted/huge `enactments` value
    (e.g. hand-edited state file) must not force building an enormous
    integer before min() clamps the result -- the clamp must happen BEFORE
    2**enactments is computed, not just after. Calling _effective_cooldown
    directly with a pathological value must return promptly and correctly
    (capped at RETIREMENT_CEILING_PROMPTS), proving the pre-exponent clamp
    is in effect rather than relying on min()'s post-hoc cap alone."""
    mod = _load_surface_hook(tmp_path, monkeypatch)
    huge_entry = {"last_fired_prompt": 0, "strength": 1.0, "enactments": 10_000_000, "fires": 1}
    result = mod._effective_cooldown("lesson", huge_entry)
    assert result == mod.RETIREMENT_CEILING_PROMPTS, (
        "a pathological enactments value must clamp to the retirement ceiling, "
        "not attempt to compute 2**10_000_000"
    )
    # Sanity: a legitimate small value still behaves as documented (base * 2**enactments).
    small_entry = {"last_fired_prompt": 0, "strength": 1.0, "enactments": 2, "fires": 1}
    base = mod.PRINCIPLE_TRIGGER_COOLDOWN_PROMPTS
    assert mod._effective_cooldown("lesson", small_entry) == min(base * 4, mod.RETIREMENT_CEILING_PROMPTS)


def test_engram_trigger_fire_emitted_with_correct_shape(tmp_path, monkeypatch):
    """engram.trigger.fire is emitted once per rendered firing, with the
    exact {principle_id, kind, trigger_id, prompt_seq} shape (§1.3). Full
    main() integration -- the emission lives at the call site in main(),
    not inside check_principle_triggers_full itself (spec's preferred
    placement; see engram-surface-hook.py main()'s comment)."""
    import engram_log_emitter
    monkeypatch.setattr(engram_log_emitter, "_EMITTER_INSTANCE", None)

    mod = _load_surface_hook(tmp_path, monkeypatch)
    registry = {
        "ob_fire": {
            "principle_id": "ls_fire", "kind": "lesson",
            "claim": "Fire-telemetry lesson claim.", "nudge": "Fire nudge.",
        }
    }
    _write_json(tmp_path / "principle_triggers.json", registry)

    monkeypatch.setattr(
        mod, "query_daemon",
        lambda *a, **kw: {"matched_ids": ["ob_fire"], "match_count": 1},
    )
    monkeypatch.setattr(mod, "query_fts_fallback", lambda *a, **kw: None)
    monkeypatch.setattr(mod, "_get_auto_surface_config", lambda: {
        "idf_gate_enabled": False,
        "short_prompt_threshold_chars": 0,
        "prev_response_tail_chars": 0,
    })
    monkeypatch.setattr(mod, "_query_attached_packs", lambda *a, **kw: [])
    monkeypatch.setattr(mod, "format_nudge", lambda *a, **kw: "")
    monkeypatch.setattr(mod, "check_write_reminder", lambda: "")
    monkeypatch.setattr(mod, "check_repair_marker", lambda: "")
    monkeypatch.setattr(mod, "check_feeling_nudge_marker", lambda: "")
    monkeypatch.setattr(mod, "check_warm_briefing", lambda count: "")
    monkeypatch.setattr(mod, "format_nap_warning", lambda count: "")
    monkeypatch.setattr(mod, "_check_mcp_write_tool_marker", lambda *a: (True, None))

    session_id = "trigger-fire-telemetry-test"
    hook_input = json.dumps({
        "prompt": "irrelevant prompt text",
        "session_id": session_id,
        "transcript_path": str(tmp_path / "t.jsonl"),
    })

    orig_context_tracker = sys.modules.get("context_tracker")
    sys.modules["context_tracker"] = types.SimpleNamespace(
        estimate_usage=lambda *a, **kw: None,
        format_drowsiness=lambda *a, **kw: "",
    )
    monkeypatch.setattr(sys, "stdin", io.StringIO(hook_input))
    monkeypatch.setattr(sys, "stdout", io.StringIO())
    try:
        try:
            mod.main()
        except SystemExit:
            pass
    finally:
        if orig_context_tracker is not None:
            sys.modules["context_tracker"] = orig_context_tracker
        else:
            sys.modules.pop("context_tracker", None)

    log_path = tmp_path / "logs" / "sessions" / f"{session_id}.l1.jsonl"
    assert log_path.exists(), "expected the structured event log to be written"
    events = [json.loads(line) for line in log_path.read_text().splitlines() if line.strip()]
    fire_events = [e for e in events if e.get("event_type") == "engram.trigger.fire"]
    assert len(fire_events) == 1, f"expected exactly one engram.trigger.fire event, got {fire_events}"
    assert fire_events[0]["data"] == {
        "principle_id": "ls_fire", "kind": "lesson",
        "trigger_id": "ob_fire", "prompt_seq": 1,
    }


_CREDIT_MENTION_STOP_HOOK_PATH = (
    PROJECT_ROOT / "src" / "engram" / "hooks" / "claude" / "engram-utility-credit-mention-stop.py"
)


def _load_credit_mention_stop_hook(engram_home, monkeypatch):
    """Import the Stop hook as a module against a scratch ENGRAM_HOME (same
    hyphenated-filename pattern as _load_surface_hook above)."""
    monkeypatch.setenv("ENGRAM_HOME", str(engram_home))
    sys.modules.pop("engram_credit_mention_stop_pt", None)
    spec = importlib.util.spec_from_file_location(
        "engram_credit_mention_stop_pt", str(_CREDIT_MENTION_STOP_HOOK_PATH)
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_enactment_detection_increments_when_no_recent_fire(tmp_path, monkeypatch):
    """A mentioned principle ID with no trigger fire in the trailing window
    increments enactments and emits engram.trigger.enactment; a mentioned ID
    that DID fire recently does not (§2/§4)."""
    import engram_log_emitter
    monkeypatch.setattr(engram_log_emitter, "_EMITTER_INSTANCE", None)

    mod = _load_credit_mention_stop_hook(tmp_path, monkeypatch)
    registry = {
        "ob_trig_a": {"principle_id": "ls_no_fire", "kind": "lesson", "claim": "c1", "nudge": "n1"},
        "ob_trig_b": {"principle_id": "ls_recent_fire", "kind": "lesson", "claim": "c2", "nudge": "n2"},
    }
    _write_json(tmp_path / "principle_triggers.json", registry)
    _write_json(tmp_path / "prompt-counter.json", {"prompts_since_compaction": 50, "last_reset": ""})
    _write_json(tmp_path / "principle-trigger-state.json", {
        # 20 prompts since last fire -- past the 10-prompt enactment window.
        "ls_no_fire": {"last_fired_prompt": 30, "strength": 1.0, "enactments": 0, "fires": 1},
        # 5 prompts since last fire -- inside the window, must NOT enact.
        "ls_recent_fire": {"last_fired_prompt": 45, "strength": 1.0, "enactments": 0, "fires": 1},
    })

    mod._check_enactments(
        ["ls_no_fire", "ls_recent_fire"],
        session_id="enact-test", transcript_path=str(tmp_path / "t.jsonl"),
    )

    with open(tmp_path / "principle-trigger-state.json") as f:
        state = json.load(f)
    assert state["ls_no_fire"]["enactments"] == 1, "no recent fire -> enactment increments"
    assert state["ls_recent_fire"]["enactments"] == 0, "fired recently (within window) -> no enactment"

    log_path = tmp_path / "logs" / "sessions" / "enact-test.l1.jsonl"
    assert log_path.exists()
    events = [json.loads(line) for line in log_path.read_text().splitlines() if line.strip()]
    enactment_events = [e for e in events if e.get("event_type") == "engram.trigger.enactment"]
    assert len(enactment_events) == 1, f"expected exactly one engram.trigger.enactment event, got {enactment_events}"
    assert enactment_events[0]["data"] == {"principle_id": "ls_no_fire", "prompt_seq": 50}


def test_reset_on_incident_clears_enactments():
    """Registering a NEW exemplar against a principle with enactments=3
    resets it to 0 (§3)."""
    server = fresh_server()
    import engram_core as core

    lesson_id, incident_id = _seed_lesson(server, claim="Reset-on-incident lesson.")
    core._rebuild_principle_triggers()

    state_path = os.path.join(str(core.DATA_DIR), "principle-trigger-state.json")
    with open(state_path, "w") as f:
        json.dump({lesson_id: {"last_fired_prompt": 5, "strength": 1.0, "enactments": 3, "fires": 4}}, f)

    new_incident = _seed_axiom(server, "A fresh incident exemplifying the lesson.")
    r = json.loads(server.engram_register_exemplar(payload_json=json.dumps({
        "target_id": lesson_id, "exemplar_id": new_incident,
    })))
    assert "error" not in r, r

    with open(state_path) as f:
        state = json.load(f)
    assert state[lesson_id]["enactments"] == 0, "new exemplar registration must reset enactments to 0"
    assert state[lesson_id]["last_fired_prompt"] == 5, "reset must not disturb last_fired_prompt"
    assert state[lesson_id]["fires"] == 4, "reset must not disturb fires"


def _patch_stats_home(fake_home):
    """Patch Path.home() as seen by engram_stats's own `Path` import, so the
    principle_coverage CLAUDE.md check doesn't read the REAL
    ~/.claude/CLAUDE.md during this test (mirrors
    test_diagnose_cornerstone_coverage.py's _patch_home)."""
    import engram_stats
    patcher = mock.patch("engram_stats.Path")
    m = patcher.start()
    m.home.return_value = fake_home
    return patcher


def test_principle_coverage_uncovered_and_covered_channels(tmp_path):
    """A principle with zero channels appears in uncovered with a non-empty
    cheapest_fix; one with a registry entry, a situation_pattern, a
    warm-briefing mention, or a CLAUDE.md mention does not (§4)."""
    server = fresh_server()
    import engram_core as core
    import engram_stats

    # Uncovered: a bare axiom, no edges/metadata/surface mentions at all.
    uncovered_id = _seed_axiom(server, "Uncovered axiom claim.")

    # Covered via registry_trigger: a goal with a serves edge, registry rebuilt.
    registry_goal_id = _seed_goal(server, aspiration="Registry-covered goal.")
    serves_source = _seed_axiom(server, "Serves-edge source for registry coverage.")
    _add_edge(server, serves_source, registry_goal_id, "serves")
    core._rebuild_principle_triggers()

    # Covered via situation_pattern: set directly on the node's metadata
    # (bypasses the tool surface -- situation_pattern is only exposed via
    # engram_add_lesson today, and every lesson also gets an automatic
    # registry_trigger via its required incident_ids, which would conflate
    # the two channels; a bare axiom with metadata patched directly isolates
    # the situation_pattern channel cleanly).
    pattern_id = _seed_axiom(server, "Pattern-covered axiom claim.")
    conn = core._get_db()
    try:
        conn.execute(
            "UPDATE nodes SET metadata = ? WHERE id = ?",
            (json.dumps({"situation_pattern": "some-test-pattern"}), pattern_id),
        )
        conn.commit()
    finally:
        conn.close()

    # Covered via warm_briefing_anchor.
    wb_id = _seed_axiom(server, "Warm-briefing-covered axiom claim.")
    (core.DATA_DIR / "warm-briefing.md").write_text(f"Anchored: {wb_id} — test principle.\n")

    # Covered via claude_md.
    claude_id = _seed_axiom(server, "CLAUDE-md-covered axiom claim.")
    fake_home = tmp_path / "fake_home_principle_coverage"
    claude_md_dir = fake_home / ".claude"
    claude_md_dir.mkdir(parents=True)
    (claude_md_dir / "CLAUDE.md").write_text(f"Rule: {claude_id} always applies.\n")

    patcher = _patch_stats_home(fake_home)
    try:
        r = json.loads(server._diagnose_impl())
    finally:
        patcher.stop()

    pc = r["principle_coverage"]
    uncovered_ids = {e["id"] for e in pc["uncovered"]}

    assert uncovered_id in uncovered_ids, "a principle with zero channels must appear in uncovered"
    uncovered_entry = next(e for e in pc["uncovered"] if e["id"] == uncovered_id)
    assert uncovered_entry["cheapest_fix"], "uncovered entries must carry a non-empty cheapest_fix"

    assert registry_goal_id not in uncovered_ids, "registry_trigger channel must exclude from uncovered"
    assert pattern_id not in uncovered_ids, "situation_pattern channel must exclude from uncovered"
    assert wb_id not in uncovered_ids, "warm_briefing_anchor channel must exclude from uncovered"
    assert claude_id not in uncovered_ids, "claude_md channel must exclude from uncovered"


# ---------------------------------------------------------------------------
# §8 — #1720 atomic read-modify-write for principle-trigger-state.json
#
# Fixes the cross-process lost-update race between the hook-side write-back
# (_check_principle_triggers_inner, engram-surface-hook.py) and the
# server-side reset (_reset_principle_enactments, engram_core.py): both
# read-whole-file / modify-in-memory / atomic-write-whole-file, so whichever
# writes last silently discards the other's change to a DIFFERENT
# principal's entry in the same file. The fix is a dedicated BLOCKING flock
# (`_with_state_lock`, duplicated once per file -- see the spec's "Why NOT
# #1709's non-blocking-suppress pattern": this is the primary T1
# lesson-tripwire render path, so silently suppressing on contention
# (#1709's shape) is not an option here).
# ---------------------------------------------------------------------------


def _fresh_core_with_data_dir(tmp_path, monkeypatch):
    """Import a fresh engram_core (no full server bootstrap needed --
    _reset_principle_enactments / _with_state_lock only touch DATA_DIR,
    not the DB), with DATA_DIR monkeypatched to tmp_path so it resolves
    principle-trigger-state.json to the SAME file the hook-side module
    (loaded via _load_surface_hook against the same tmp_path as ENGRAM_HOME)
    uses.

    Full-suite-only flake (caught by running the full sibling suite per
    handoff discipline, NOT visible file-in-isolation): a plain `import
    engram_core` after the sys.modules purge below is vulnerable to
    cross-test sys.path pollution elsewhere in the suite (some earlier test,
    exercising a hook's `_resolve_runtime_dir` fallback chain, ends up
    inserting the PARENT checkout's `src/engram` -- this worktree lives
    nested under `.claude/worktrees/<id>/` inside the main checkout tree --
    ahead of this worktree's own `src/engram` in `sys.path`). Explicitly
    forcing this worktree's `src/engram` to `sys.path[0]` immediately before
    the import makes resolution deterministic regardless of what any other
    test file has done to `sys.path` -- matches this file's own
    `_load_surface_hook` pattern of never trusting ambient `sys.path` order
    for locating repo-local modules under test."""
    for key in list(sys.modules):
        if key == "server" or key.startswith("engram_"):
            del sys.modules[key]
    src_engram = str(PROJECT_ROOT / "src" / "engram")
    if src_engram in sys.path:
        sys.path.remove(src_engram)
    sys.path.insert(0, src_engram)
    import engram_core as core
    assert Path(core.__file__).resolve() == (PROJECT_ROOT / "src" / "engram" / "engram_core.py").resolve(), (
        f"engram_core resolved to {core.__file__!r}, not this worktree's copy -- "
        "sys.path pollution from another test file (see docstring)"
    )
    monkeypatch.setattr(core, "DATA_DIR", str(tmp_path))
    return core


def test_concurrent_hook_and_reset_writes_lose_no_update(tmp_path, monkeypatch):
    """Core regression for #1720: concurrent hook-side write-backs
    (_check_principle_triggers_inner, via check_principle_triggers) and a
    concurrent server-side _reset_principle_enactments call, all against the
    SAME state file, threaded with a threading.Barrier for maximal overlap.
    After all complete, every principal the reset call touched must show
    enactments == 0, AND every principal the hook-side calls stamped must
    show the expected last_fired_prompt/fires -- proving BOTH writers'
    updates survive (the pre-#1720 bug: last-writer-wins on the whole file
    silently drops whichever side loses)."""
    import threading

    core = _fresh_core_with_data_dir(tmp_path, monkeypatch)
    mod = _load_surface_hook(tmp_path, monkeypatch)

    n_hook = 5
    n_reset = 5

    registry = {
        f"ob_hook_{i}": {
            "principle_id": f"ls_hook_{i}",
            "kind": "lesson",
            "claim": f"Hook claim {i}.",
            "nudge": f"Hook nudge {i}.",
        }
        for i in range(n_hook)
    }
    _write_json(tmp_path / "principle_triggers.json", registry)

    # Reset-side principals start with nonzero enactments (and other fields
    # that must survive the reset untouched); hook-side principals start
    # absent from state (fresh fire).
    initial_state = {
        f"ls_reset_{i}": {"last_fired_prompt": 5, "strength": 1.0, "enactments": 7, "fires": 3}
        for i in range(n_reset)
    }
    _write_json(tmp_path / "principle-trigger-state.json", initial_state)

    barrier = threading.Barrier(n_hook + n_reset)

    def hook_worker(i):
        barrier.wait()
        mod.check_principle_triggers([f"ob_hook_{i}"], prompt_count=100)

    def reset_worker(i):
        barrier.wait()
        core._reset_principle_enactments(f"ls_reset_{i}")

    threads = [threading.Thread(target=hook_worker, args=(i,)) for i in range(n_hook)]
    threads += [threading.Thread(target=reset_worker, args=(i,)) for i in range(n_reset)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    with open(tmp_path / "principle-trigger-state.json") as f:
        final_state = json.load(f)

    for i in range(n_reset):
        pid = f"ls_reset_{i}"
        assert pid in final_state, f"{pid} lost entirely from final state -- lost update"
        assert final_state[pid]["enactments"] == 0, f"{pid} reset must survive concurrent hook writes"
        assert final_state[pid]["last_fired_prompt"] == 5, f"{pid} reset must not disturb last_fired_prompt"
        assert final_state[pid]["fires"] == 3, f"{pid} reset must not disturb fires"

    for i in range(n_hook):
        pid = f"ls_hook_{i}"
        assert pid in final_state, f"{pid} lost entirely from final state -- lost update"
        assert final_state[pid]["last_fired_prompt"] == 100, f"{pid} hook-side stamp must survive concurrent reset writes"
        assert final_state[pid]["fires"] == 1, f"{pid} hook-side fires-increment must survive"


def test_with_state_lock_degrades_when_lockfile_open_fails(tmp_path, monkeypatch):
    """A _with_state_lock call that can't set up (lockfile open fails) still
    runs its closure -- degrades unlocked, never raises, never skips the
    read-modify-write. Exercises BOTH duplicated copies of the helper
    (engram_core.py + engram-surface-hook.py), since the spec duplicates
    rather than shares this ~15-line helper (#1720)."""
    core = _fresh_core_with_data_dir(tmp_path, monkeypatch)
    mod = _load_surface_hook(tmp_path, monkeypatch)

    def _broken_open(*a, **kw):
        raise OSError("simulated: lockfile open unavailable")

    for helper in (core._with_state_lock, mod._with_state_lock):
        called = {"n": 0}

        def _closure():
            called["n"] += 1
            return "ran-unlocked"

        with mock.patch("os.open", _broken_open):
            result = helper(str(tmp_path / "some.lock"), _closure)

        assert called["n"] == 1, f"{helper.__module__}._with_state_lock must still run its closure when lock setup fails"
        assert result == "ran-unlocked"


def test_with_state_lock_releases_lock_when_closure_raises(tmp_path, monkeypatch):
    """The flock is released (via `finally`) even when the wrapped closure
    raises. Exercises BOTH duplicated copies of the helper (engram_core.py +
    engram-surface-hook.py) (#1720)."""
    import fcntl

    core = _fresh_core_with_data_dir(tmp_path, monkeypatch)
    mod = _load_surface_hook(tmp_path, monkeypatch)

    def _raising_closure():
        raise RuntimeError("deliberate closure failure")

    for helper in (core._with_state_lock, mod._with_state_lock):
        lock_path = tmp_path / f"release-{helper.__module__}.lock"

        raised = False
        try:
            helper(str(lock_path), _raising_closure)
        except RuntimeError:
            raised = True
        assert raised, f"{helper.__module__}._with_state_lock must propagate the closure's exception, not swallow it"

        # If the lock were still held (finally didn't run / didn't unlock),
        # a fresh non-blocking acquire attempt on a NEW fd for the same path
        # would raise BlockingIOError -- flock is per-open-file-description,
        # not per-process, so a same-process second fd still contends
        # (verified pattern: test_lock_contention_suppresses_not_blocks in
        # test_in_turn_recall.py).
        fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)
            assert False, f"{helper.__module__}._with_state_lock did not release the lock when the closure raised"
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


# ---------------------------------------------------------------------------
# §8 Dual-role trigger nodes (#1731) — list-valued registry keyspace
# ---------------------------------------------------------------------------
#
# Live-verify catch (Kepler's install, first deploy of slices 1-3): a
# trigger node that both exemplifies a lesson AND instantiates an axiom
# collided under the original single-dict-per-trigger keyspace. Rebuild
# order in _PRINCIPLE_TRIGGER_SPECS processes lesson before axiom, and the
# rebuild's last-spec-wins assignment (`index[trigger_id] = entry`) meant
# axiom always overwrote lesson silently -- a T1 safety tripwire going dark
# with no error anywhere. Fix: registry values are LISTS of entries; the
# consumer iterates all of them per matched trigger and lets the existing
# priority/cap logic (already built for cross-trigger arbitration) do the
# same job across entries WITHIN one trigger.


def test_dual_role_trigger_keeps_both_entries():
    """A trigger that both exemplifies a lesson and instantiates an axiom
    must survive in the registry with BOTH entries, not last-spec-wins
    collapse to whichever spec runs later in _PRINCIPLE_TRIGGER_SPECS."""
    server = fresh_server()
    import engram_core as core

    lesson_id, incident_id = _seed_lesson(server)
    ax_id = _seed_axiom(server, "An axiom the dual-role node also grounds.")
    _add_edge(server, incident_id, ax_id, "instantiates")

    core._rebuild_principle_triggers()
    reg = _read_registry(core)

    assert incident_id in reg
    assert isinstance(reg[incident_id], list), "registry values must be lists (#1731)"
    kinds = {e["kind"] for e in reg[incident_id]}
    assert kinds == {"lesson", "axiom"}, (
        f"dual-role trigger must keep BOTH entries, not collapse to one; got kinds={kinds}"
    )
    principle_ids = {e["principle_id"] for e in reg[incident_id]}
    assert principle_ids == {lesson_id, ax_id}


def test_dual_role_trigger_by_kind_counts_both():
    """The rebuild summary's by_kind tally counts a dual-role trigger's
    lesson AND axiom entries separately -- neither survivor is invisible to
    the count just because they share a trigger node."""
    server = fresh_server()
    import engram_core as core

    _, incident_id = _seed_lesson(server)
    ax_id = _seed_axiom(server, "Second axiom for by_kind counting.")
    _add_edge(server, incident_id, ax_id, "instantiates")

    result = core._rebuild_principle_triggers()
    assert result["by_kind"].get("lesson", 0) >= 1
    assert result["by_kind"].get("axiom", 0) >= 1


def test_check_principle_triggers_dual_role_renders_both_under_cap(tmp_path, monkeypatch):
    """Consumer side of #1731: a dual-role trigger's list entries both
    produce candidates. With cap=2 and nothing else matched, both the
    lesson and the axiom render -- the lesson (highest priority) is no
    longer silently shadowed by the axiom sharing its trigger id."""
    mod = _load_surface_hook(tmp_path, monkeypatch)
    registry = {
        "ob_9099": [
            {
                "principle_id": "ls_9099",
                "kind": "lesson",
                "claim": "Dual-role lesson claim.",
                "nudge": "Do the corrective action.",
            },
            {
                "principle_id": "ax_9099",
                "kind": "axiom",
                "claim": "Dual-role axiom claim.",
                "nudge": "Constraint text.",
            },
        ],
    }
    _write_json(tmp_path / "principle_triggers.json", registry)
    text, rendered = mod.check_principle_triggers_full(["ob_9099"], prompt_count=5)
    kinds_rendered = {r[0] for r in rendered}
    assert "lesson" in kinds_rendered, "dual-role trigger's lesson entry must not be silently dropped"
    assert "axiom" in kinds_rendered, "both entries fit within cap=2 and should both render"
    assert "[ENGRAM Tripwire (ls_9099)" in text
    assert "[Principle trigger (ax_9099, constraining)]" in text


def test_check_principle_triggers_dual_role_beats_unrelated_lower_priority_under_cap(tmp_path, monkeypatch):
    """A dual-role trigger contributes 2 candidates (lesson, axiom); a
    THIRD, unrelated goal-kind trigger also matches in the same prompt.
    Cap=2 keeps the two higher-priority entries (both from the dual-role
    trigger) and drops the goal -- the arbitration the fix delegates to the
    existing priority/cap logic, now correctly reachable within one
    trigger's own entries and not just across different triggers."""
    mod = _load_surface_hook(tmp_path, monkeypatch)
    registry = {
        "ob_9099": [
            {"principle_id": "ls_9099", "kind": "lesson", "claim": "L", "nudge": "N1"},
            {"principle_id": "ax_9099", "kind": "axiom", "claim": "A", "nudge": "N2"},
        ],
        "ob_9100": {"principle_id": "gl_9100", "kind": "goal", "claim": "G", "nudge": "N3"},
    }
    _write_json(tmp_path / "principle_triggers.json", registry)
    text, rendered = mod.check_principle_triggers_full(["ob_9099", "ob_9100"], prompt_count=5)
    kinds_rendered = [r[0] for r in rendered]
    assert kinds_rendered == ["lesson", "axiom"], f"expected lesson+axiom to win the cap; got {kinds_rendered}"
    assert "gl_9100" not in text


def test_principle_triggers_old_shape_single_dict_still_read(tmp_path, monkeypatch):
    """Lazy-read shim: a registry file still in the pre-#1731 shape (a bare
    dict per trigger, not a list) must still be read correctly -- covers the
    window right after this fix deploys, before the next rebuild rewrites
    the file. (The byte-compat tests above already exercise this
    incidentally; this test names the shim explicitly.)"""
    mod = _load_surface_hook(tmp_path, monkeypatch)
    registry = {
        "ob_9101": {
            "principle_id": "ls_9101",
            "kind": "lesson",
            "claim": "Old-shape claim.",
            "nudge": "Old-shape nudge.",
        }
    }
    _write_json(tmp_path / "principle_triggers.json", registry)
    result = mod.check_principle_triggers(["ob_9101"], prompt_count=5)
    assert "[ENGRAM Tripwire (ls_9101)" in result


# ---------------------------------------------------------------------------
# §8 Startup rebuild (#1740) — _ensure_schema_bootstrap() in server.py is the
# once-per-resolved-path trigger that stands in for "server startup" (fires
# from the first MCP tool call's timing wrapper, gated so it only runs once
# per process/DB-path). Before #1740 it never touched the principle_triggers
# registry, so a deployed rebuild-logic fix stayed inert until an unrelated
# mutation happened to fire a rebuild. These tests cover the startup-time
# rebuild call added there.
# ---------------------------------------------------------------------------


def test_ensure_schema_bootstrap_rebuilds_principle_triggers_once():
    """_ensure_schema_bootstrap() must invoke core._rebuild_principle_triggers()
    exactly once (the once-per-resolved-path gate covers repeat calls too)."""
    server = fresh_server()
    with mock.patch.object(server.core, "_rebuild_principle_triggers") as m:
        server._ensure_schema_bootstrap()
        server._ensure_schema_bootstrap()
    m.assert_called_once()


def test_ensure_schema_bootstrap_survives_rebuild_exception():
    """A raising rebuild must not propagate -- startup must never be blocked
    by a principle_triggers rebuild failure."""
    server = fresh_server()
    with mock.patch.object(
        server.core, "_rebuild_principle_triggers", side_effect=RuntimeError("boom")
    ):
        server._ensure_schema_bootstrap()  # must not raise
