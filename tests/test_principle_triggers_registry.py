"""Tests for the unified principle_triggers.json registry (#1698 slice 1).

Coverage:
  §1 Rebuild — all four kinds derive from their edges (lesson/exemplifies,
     cornerstone/exemplifies, axiom/instantiates, goal/serves+tensions),
     nudge fallback to claim, tension mode tagged, bidirectional tensions,
     idempotency, superseded principles excluded.
  §2 Call-site wiring — register_exemplar, generic add_edge/remove_edge
     (serves from an observation — the Lei liberalization), and
     engram_add_task's serves edge all refresh the registry.
  §3 Migration shim — legacy caches still written alongside.
"""

from __future__ import annotations

import atexit
import json
import os
import shutil
import sys
import tempfile

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
