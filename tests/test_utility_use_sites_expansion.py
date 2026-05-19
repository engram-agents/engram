"""Tests for the expanded USE-action utility wiring (Lei 2026-05-19 tier table).

Covers all new call sites added in the use-sites-expansion PR:

  Tier 1 (α=0.15):
    - engram_supersede  → bumps new_node_id only (old is retired)
    - engram_contradict → bumps both node_id_a and node_id_b
    - engram_resolve    → bumps both target_id and the new derivation_id
    - engram_lesson_register_incident → bumps lesson_id

  Tier 2 (α=0.10):
    - engram_focus      → bumps each focused node
    - engram_focus_swap → bumps both outgoing and incoming sets
    - engram_history    → bumps node_id when provided; no bump when empty
    - engram_add_observation   → no context_ids field on this tool; skip (flagged)
    - engram_add_observation_batch → same; skip (flagged)
    - engram_add_cornerstone → bumps supporting_ids, NOT the new cornerstone itself
    - engram_ask        → bumps context_ids

  Tier 3 (α=0.05):
    - engram_unfocus     → bumps released node
    - engram_focus_load  → bumps each loaded set member

  NO-BUMP:
    - engram_retract            → target retains pre-retract utility
    - engram_outgrow_cornerstone → target retains current utility
"""

import json
import os
import shutil
import sys
import tempfile

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


TEST_DIR = os.path.join(tempfile.gettempdir(), "engram_test_utility_use_sites_expansion")


def fresh_server():
    if os.path.exists(TEST_DIR):
        shutil.rmtree(TEST_DIR)
    for mod in list(sys.modules.keys()):
        if mod == "server" or mod.startswith("server."):
            del sys.modules[mod]
    import server
    server._configure_paths(TEST_DIR)
    server._ensure_data_dir()
    os.environ["ENGRAM_NO_EMBEDDINGS"] = "1"
    return server


def _get_utility(server, node_id: str) -> float:
    """Read utility_score directly from DB for a given node_id."""
    conn = server._get_db()
    try:
        row = conn.execute(
            "SELECT utility_score FROM nodes WHERE id = ?", (node_id,)
        ).fetchone()
        if row is None:
            return None
        val = row["utility_score"]
        return float(val) if val is not None else 0.0
    finally:
        conn.close()


def _seed_axioms(server, n=2):
    """Create n axioms and return their IDs."""
    ids = []
    for i in range(n):
        r = json.loads(server.engram_add_axiom(payload_json=json.dumps({
            "claim": f"Axiom {i} for use-sites expansion test.",
            "basis": f"Use-sites expansion test fixture axiom {i}.",
        })))
        ids.append(r["axiom_id"])
    return ids


def _make_derive(server, premise_ids):
    """Derive from premise_ids; return the derivation result dict."""
    return json.loads(server.engram_derive(payload_json=json.dumps({
        "claim": "Derived claim for use-sites expansion test.",
        "supporting_ids": ",".join(premise_ids),
        "logical_chain": "Test derivation for use-sites expansion.",
        "reasoning_type": "deductive_modus_ponens",
    })))


# ---------------------------------------------------------------------------
# Tier 1 — deep cognitive load (α=0.15)
# ---------------------------------------------------------------------------


def test_engram_supersede_bumps_new_node_only():
    """engram_supersede bumps new_node_id only; old_node_id is NOT bumped.

    Spec: retired nodes shouldn't bump (Lei 2026-05-19 design call).
    """
    server = fresh_server()
    ax_old, ax_new = _seed_axioms(server, n=2)

    before_old = _get_utility(server, ax_old)
    before_new = _get_utility(server, ax_new)

    result = json.loads(server.engram_supersede(payload_json=json.dumps({
        "old_node_id": ax_old,
        "new_node_id": ax_new,
        "supersede_reason": "test: expansion supersede bump",
    })))
    assert "error" not in result, f"engram_supersede failed: {result}"
    assert result["status"] == "superseded"

    after_old = _get_utility(server, ax_old)
    after_new = _get_utility(server, ax_new)

    # old_node_id must NOT be bumped (retired)
    assert after_old == before_old, (
        f"old_node_id must NOT be bumped; before={before_old}, after={after_old}"
    )

    # new_node_id must be bumped with α=0.15
    expected_new = round(before_new + 0.15 * (1.0 - before_new), 6)
    assert abs(after_new - expected_new) < 1e-9, (
        f"new_node_id should be bumped with α=0.15; expected {expected_new}, got {after_new}"
    )


def test_engram_contradict_bumps_both_nodes():
    """engram_contradict bumps both node_id_a and node_id_b with α=0.15.

    Both conflicting nodes are deeply engaged — the agent had to think with
    both nodes to identify the conflict.
    """
    server = fresh_server()
    ax_a, ax_b = _seed_axioms(server, n=2)

    before_a = _get_utility(server, ax_a)
    before_b = _get_utility(server, ax_b)

    result = json.loads(server.engram_contradict(payload_json=json.dumps({
        "node_id_a": ax_a,
        "node_id_b": ax_b,
        "description": "Test contradiction for use-sites expansion.",
    })))
    assert "error" not in result, f"engram_contradict failed: {result}"
    assert result["status"] == "created"

    after_a = _get_utility(server, ax_a)
    after_b = _get_utility(server, ax_b)

    expected_a = round(before_a + 0.15 * (1.0 - before_a), 6)
    expected_b = round(before_b + 0.15 * (1.0 - before_b), 6)

    assert abs(after_a - expected_a) < 1e-9, (
        f"ax_a should be bumped with α=0.15; expected {expected_a}, got {after_a}"
    )
    assert abs(after_b - expected_b) < 1e-9, (
        f"ax_b should be bumped with α=0.15; expected {expected_b}, got {after_b}"
    )


def test_engram_resolve_bumps_both_target_and_derivation():
    """engram_resolve bumps both the question target AND the new derivation with α=0.15.

    The question is the cognitive target; the derivation is the answer
    constructed through it. Both receive tier-1 treatment.
    """
    server = fresh_server()
    ax_a, ax_b = _seed_axioms(server, n=2)

    # Create a question to resolve
    q_result = json.loads(server.engram_ask(payload_json=json.dumps({
        "question": "Test question for resolve bump test.",
        "category": "research",
    })))
    assert "error" not in q_result, f"engram_ask failed: {q_result}"
    qu_id = q_result["question_id"]

    before_qu = _get_utility(server, qu_id)

    resolve_result = json.loads(server.engram_resolve(payload_json=json.dumps({
        "target_id": qu_id,
        "claim": "Resolution claim for use-sites expansion test.",
        "supporting_ids": f"{ax_a},{ax_b}",
        "logical_chain": "Test resolution derivation for use-sites expansion.",
        "reasoning_type": "deductive_modus_ponens",
    })))
    assert "error" not in resolve_result, f"engram_resolve failed: {resolve_result}"
    assert resolve_result["status"] == "resolution_created"

    dv_id = resolve_result["derivation_id"]
    after_qu = _get_utility(server, qu_id)
    after_dv = _get_utility(server, dv_id)

    # Question must be bumped with α=0.15
    expected_qu = round(before_qu + 0.15 * (1.0 - before_qu), 6)
    assert abs(after_qu - expected_qu) < 1e-9, (
        f"question target should be bumped with α=0.15; expected {expected_qu}, got {after_qu}"
    )

    # Derivation must be bumped with α=0.15
    # Note: derivation was just born (utility_score starts at 0.0)
    expected_dv = round(0.0 + 0.15 * (1.0 - 0.0), 6)
    assert abs(after_dv - expected_dv) < 1e-9, (
        f"derivation should be bumped with α=0.15; expected {expected_dv}, got {after_dv}"
    )


def test_engram_lesson_register_incident_bumps_lesson():
    """engram_lesson_register_incident bumps the lesson_id with α=0.15.

    Recognizing a fresh error as an instance of a known lesson is a
    distinct cognitive act (ob_1235) — tier-1 engagement.
    """
    server = fresh_server()
    (ax_id,) = _seed_axioms(server, n=1)

    # Create a lesson node
    lesson_result = json.loads(server.engram_add_lesson(payload_json=json.dumps({
        "claim": "Test lesson pattern for incident bump test.",
        "incident_ids": ax_id,
        "scaffolding_nudge": "When this fires, check the test pattern.",
        "logical_chain": "The incident exemplifies the lesson pattern.",
    })))
    assert "error" not in lesson_result, f"engram_add_lesson failed: {lesson_result}"
    ls_id = lesson_result["lesson_id"]

    # Create a second axiom to serve as the new incident
    (ax_incident,) = _seed_axioms(server, n=1)

    before_ls = _get_utility(server, ls_id)

    incident_result = json.loads(server.engram_lesson_register_incident(payload_json=json.dumps({
        "lesson_id": ls_id,
        "incident_id": ax_incident,
        "note": "Second incident for bump test.",
    })))
    assert "error" not in incident_result, f"engram_lesson_register_incident failed: {incident_result}"
    assert incident_result["status"] == "created"

    after_ls = _get_utility(server, ls_id)

    expected_ls = round(before_ls + 0.15 * (1.0 - before_ls), 6)
    assert abs(after_ls - expected_ls) < 1e-9, (
        f"lesson should be bumped with α=0.15; expected {expected_ls}, got {after_ls}"
    )


# ---------------------------------------------------------------------------
# Tier 2 — moderate engagement (α=0.10)
# ---------------------------------------------------------------------------


def test_engram_focus_bumps_target():
    """engram_focus bumps each focused node with α=0.10."""
    server = fresh_server()
    (ax_id,) = _seed_axioms(server, n=1)

    before = _get_utility(server, ax_id)

    result = json.loads(server.engram_focus(
        node_ids=ax_id,
        reason="test: focus bump",
    ))
    assert "error" not in result, f"engram_focus failed: {result}"
    assert result["status"] == "ok"

    after = _get_utility(server, ax_id)
    expected = round(before + 0.10 * (1.0 - before), 6)
    assert abs(after - expected) < 1e-9, (
        f"focused node should be bumped with α=0.10; expected {expected}, got {after}"
    )


def test_engram_focus_swap_bumps_both():
    """engram_focus_swap bumps both outgoing (saved) and incoming (loaded) node sets with α=0.10."""
    server = fresh_server()
    ax_out, ax_in = _seed_axioms(server, n=2)

    # Focus ax_out first (becomes the active list to save)
    focus_result = json.loads(server.engram_focus(
        node_ids=ax_out,
        reason="test: focus swap out",
    ))
    assert "error" not in focus_result, f"engram_focus failed: {focus_result}"

    # Save ax_in to a named set so it can be loaded
    # We do this by focusing ax_in briefly under a name, then restoring ax_out
    focus_result2 = json.loads(server.engram_focus(
        node_ids=ax_in,
        reason="test: set to save for load",
    ))
    # Both are now focused; save them as "load_set" then clear ax_in
    save_result = json.loads(server.engram_focus_save(
        name="load_set",
        description="test set for swap",
    ))
    assert "error" not in save_result, f"engram_focus_save failed: {save_result}"

    # Unfocus ax_in so the active list is just ax_out
    server.engram_unfocus(node_ids=ax_in)

    # Now perform the swap: save ax_out as "out_set", load "load_set"
    before_out = _get_utility(server, ax_out)
    before_in = _get_utility(server, ax_in)

    swap_result = json.loads(server.engram_focus_swap(
        save_as="out_set",
        load="load_set",
    ))
    assert "error" not in swap_result, f"engram_focus_swap failed: {swap_result}"
    assert swap_result["status"] == "ok"

    after_out = _get_utility(server, ax_out)
    after_in = _get_utility(server, ax_in)

    expected_out = round(before_out + 0.10 * (1.0 - before_out), 6)
    expected_in = round(before_in + 0.10 * (1.0 - before_in), 6)

    assert abs(after_out - expected_out) < 1e-9, (
        f"outgoing node should be bumped with α=0.10; expected {expected_out}, got {after_out}"
    )
    assert abs(after_in - expected_in) < 1e-9, (
        f"incoming node should be bumped with α=0.10; expected {expected_in}, got {after_in}"
    )


def test_engram_history_with_node_id_bumps_target():
    """engram_history bumps the specified node_id with α=0.10 when node_id is provided."""
    server = fresh_server()
    (ax_id,) = _seed_axioms(server, n=1)

    before = _get_utility(server, ax_id)

    result = json.loads(server.engram_history(
        mode="edits",
        node_id=ax_id,
    ))
    assert "error" not in result, f"engram_history failed: {result}"
    assert result["mode"] == "edits"

    after = _get_utility(server, ax_id)
    expected = round(before + 0.10 * (1.0 - before), 6)
    assert abs(after - expected) < 1e-9, (
        f"engram_history with node_id should bump with α=0.10; expected {expected}, got {after}"
    )


def test_engram_history_without_node_id_does_not_bump():
    """engram_history does NOT bump when node_id is empty (global edits browse)."""
    server = fresh_server()
    (ax_id,) = _seed_axioms(server, n=1)

    before = _get_utility(server, ax_id)

    result = json.loads(server.engram_history(
        mode="edits",
        node_id="",
    ))
    assert "error" not in result, f"engram_history failed: {result}"

    after = _get_utility(server, ax_id)
    assert after == before, (
        f"engram_history without node_id must not bump; before={before}, after={after}"
    )


@pytest.mark.skip(
    reason=(
        "engram_add_observation has no context_ids parameter — the tool's schema "
        "does not include a context citation field. Wiring not possible without a "
        "schema addition. Flagged for parent review (see handoff)."
    )
)
def test_engram_add_observation_bumps_context_ids():
    """engram_add_observation bumps each cited context_id with α=0.10 (citation).

    SKIPPED: engram_add_observation does not have a context_ids field in its
    schema (_ADD_OBSERVATION_FIELDS). The tool only records quoted_text,
    interpretation, claim, quote_type, source fields, and predictive fields.
    Wiring cannot be done without a schema change. Flagged for parent.
    """
    pass


@pytest.mark.skip(
    reason=(
        "engram_add_observation_batch has no context_ids parameter — same as "
        "engram_add_observation. Wiring not possible without a schema addition. "
        "Flagged for parent review (see handoff)."
    )
)
def test_engram_add_observation_batch_bumps_context_ids_per_record():
    """engram_add_observation_batch bumps each context_id per record with α=0.10.

    SKIPPED: Neither engram_add_observation_batch nor its per-observation records
    have a context_ids field. Flagged for parent.
    """
    pass


def test_engram_add_cornerstone_bumps_supporting_ids_not_new_cornerstone():
    """engram_add_cornerstone bumps supporting_ids with α=0.10; NOT the new cornerstone.

    The new cornerstone is just born — its supporting nodes are the USE targets.
    """
    server = fresh_server()
    ax_a, ax_b = _seed_axioms(server, n=2)

    before_a = _get_utility(server, ax_a)
    before_b = _get_utility(server, ax_b)

    result = json.loads(server.engram_add_cornerstone(payload_json=json.dumps({
        "tag": "test-expansion",
        "title": "Test cornerstone for use-sites expansion.",
        "new_frame": "The new frame for this cornerstone.",
        "supporting_ids": f"{ax_a},{ax_b}",
    })))
    assert "error" not in result, f"engram_add_cornerstone failed: {result}"
    cs_id = result["cornerstone_id"]

    after_a = _get_utility(server, ax_a)
    after_b = _get_utility(server, ax_b)
    after_cs = _get_utility(server, cs_id)

    # Supporting nodes should be bumped with α=0.10 (citation)
    expected_a = round(before_a + 0.10 * (1.0 - before_a), 6)
    expected_b = round(before_b + 0.10 * (1.0 - before_b), 6)
    assert abs(after_a - expected_a) < 1e-9, (
        f"supporting ax_a should be bumped with α=0.10; expected {expected_a}, got {after_a}"
    )
    assert abs(after_b - expected_b) < 1e-9, (
        f"supporting ax_b should be bumped with α=0.10; expected {expected_b}, got {after_b}"
    )

    # The new cornerstone itself must NOT be bumped (just born)
    assert after_cs == 0.0 or after_cs is None, (
        f"new cornerstone must not be bumped; got {after_cs}"
    )


def test_engram_ask_bumps_context_ids():
    """engram_ask bumps each context_id with α=0.10 (citation)."""
    server = fresh_server()
    ax_a, ax_b = _seed_axioms(server, n=2)

    before_a = _get_utility(server, ax_a)
    before_b = _get_utility(server, ax_b)

    result = json.loads(server.engram_ask(payload_json=json.dumps({
        "question": "Test question for ask citation bump.",
        "context_ids": f"{ax_a},{ax_b}",
        "category": "research",
    })))
    assert "error" not in result, f"engram_ask failed: {result}"
    qu_id = result["question_id"]
    after_qu = _get_utility(server, qu_id)

    after_a = _get_utility(server, ax_a)
    after_b = _get_utility(server, ax_b)

    expected_a = round(before_a + 0.10 * (1.0 - before_a), 6)
    expected_b = round(before_b + 0.10 * (1.0 - before_b), 6)

    assert abs(after_a - expected_a) < 1e-9, (
        f"context ax_a should be bumped with α=0.10; expected {expected_a}, got {after_a}"
    )
    assert abs(after_b - expected_b) < 1e-9, (
        f"context ax_b should be bumped with α=0.10; expected {expected_b}, got {after_b}"
    )

    # The new question itself must NOT be bumped (just born)
    assert after_qu == 0.0 or after_qu is None, (
        f"new question must not be bumped; got {after_qu}"
    )


# ---------------------------------------------------------------------------
# Tier 3 — loose touch (α=0.05)
# ---------------------------------------------------------------------------


def test_engram_unfocus_bumps_target():
    """engram_unfocus bumps each released node with α=0.05."""
    server = fresh_server()
    (ax_id,) = _seed_axioms(server, n=1)

    # Focus first
    focus_result = json.loads(server.engram_focus(
        node_ids=ax_id,
        reason="test: unfocus bump",
    ))
    assert "error" not in focus_result, f"engram_focus failed: {focus_result}"

    before = _get_utility(server, ax_id)

    result = json.loads(server.engram_unfocus(node_ids=ax_id))
    assert "error" not in result, f"engram_unfocus failed: {result}"
    assert ax_id in result["released"]

    after = _get_utility(server, ax_id)
    expected = round(before + 0.05 * (1.0 - before), 6)
    assert abs(after - expected) < 1e-9, (
        f"unfocused node should be bumped with α=0.05; expected {expected}, got {after}"
    )


def test_engram_focus_load_bumps_set_members():
    """engram_focus_load bumps each loaded set member with α=0.05."""
    server = fresh_server()
    ax_a, ax_b = _seed_axioms(server, n=2)

    # Focus ax_a and ax_b, then save as a set
    focus_result = json.loads(server.engram_focus(
        node_ids=f"{ax_a},{ax_b}",
        reason="test: focus load bump",
    ))
    assert "error" not in focus_result, f"engram_focus failed: {focus_result}"

    save_result = json.loads(server.engram_focus_save(
        name="test_load_set",
        description="test set for focus_load bump test",
    ))
    assert "error" not in save_result, f"engram_focus_save failed: {save_result}"

    # Unfocus so we can load
    server.engram_unfocus(node_ids=f"{ax_a},{ax_b}")

    before_a = _get_utility(server, ax_a)
    before_b = _get_utility(server, ax_b)

    load_result = json.loads(server.engram_focus_load(name="test_load_set"))
    assert "error" not in load_result, f"engram_focus_load failed: {load_result}"
    assert load_result["status"] == "ok"
    assert ax_a in load_result["loaded"]
    assert ax_b in load_result["loaded"]

    after_a = _get_utility(server, ax_a)
    after_b = _get_utility(server, ax_b)

    expected_a = round(before_a + 0.05 * (1.0 - before_a), 6)
    expected_b = round(before_b + 0.05 * (1.0 - before_b), 6)

    assert abs(after_a - expected_a) < 1e-9, (
        f"loaded ax_a should be bumped with α=0.05; expected {expected_a}, got {after_a}"
    )
    assert abs(after_b - expected_b) < 1e-9, (
        f"loaded ax_b should be bumped with α=0.05; expected {expected_b}, got {after_b}"
    )


# ---------------------------------------------------------------------------
# NO-BUMP checks
# ---------------------------------------------------------------------------


def test_engram_retract_does_not_bump():
    """engram_retract does NOT bump the target node's utility.

    Retracting a wrong node shouldn't reward it.
    """
    server = fresh_server()
    (ax_id,) = _seed_axioms(server, n=1)

    before = _get_utility(server, ax_id)

    result = json.loads(server.engram_retract(payload_json=json.dumps({
        "node_id": ax_id,
        "error_type": "other",
        "reason": "test: retract no-bump check",
    })))
    assert "error" not in result, f"engram_retract failed: {result}"

    after = _get_utility(server, ax_id)
    assert after == before, (
        f"engram_retract must not bump; before={before}, after={after}"
    )


def test_engram_outgrow_cornerstone_does_not_bump():
    """engram_outgrow_cornerstone does NOT bump the old cornerstone.

    Demoting a cornerstone is analogous to retraction — the target retains
    its current utility score.
    """
    server = fresh_server()
    (ax_id,) = _seed_axioms(server, n=1)

    # Create a cornerstone to outgrow
    cs_result = json.loads(server.engram_add_cornerstone(payload_json=json.dumps({
        "tag": "test-outgrow",
        "title": "Original cornerstone for outgrow test.",
        "new_frame": "Original new_frame.",
    })))
    assert "error" not in cs_result, f"engram_add_cornerstone failed: {cs_result}"
    cs_old_id = cs_result["cornerstone_id"]

    before_cs = _get_utility(server, cs_old_id)

    outgrow_result = json.loads(server.engram_outgrow_cornerstone(payload_json=json.dumps({
        "old_cornerstone_id": cs_old_id,
        "new_new_frame": "Outgrown new_frame — the frame has shifted.",
        "new_triggering_experience": "Test outgrow trigger for no-bump check.",
    })))
    assert "error" not in outgrow_result, f"engram_outgrow_cornerstone failed: {outgrow_result}"

    after_cs = _get_utility(server, cs_old_id)
    assert after_cs == before_cs, (
        f"engram_outgrow_cornerstone must not bump old cornerstone; "
        f"before={before_cs}, after={after_cs}"
    )


def test_engram_add_task_bumps_goal_id_and_implements_ids_as_citation():
    """engram_add_task with goal_id + implements_ids → each bumped α=0.10 (Lei 2026-05-19 Q2 greenlight).

    Folded into the multiplicative-composite PR as the Q2 follow-up; previously
    skipped because the spec said engram_add_task uses context_ids — it actually
    uses goal_id (creates 'serves' edge) + implements_ids (creates 'cites' edges).
    Both are deliberate references to existing nodes and qualify as tier-2
    citation USEs.
    """
    server = fresh_server()

    # Seed a goal and a conjecture as citation targets
    g = json.loads(server.engram_add_goal(payload_json=json.dumps({
        "claim": "test goal for task citation",
        "motivation": "test fixture goal",
    })))
    assert "goal_id" in g, g
    goal_id = g["goal_id"]

    c = json.loads(server.engram_add_conjecture(payload_json=json.dumps({
        "claim": "test conjecture for task citation",
        "basis": "test fixture conjecture basis",
    })))
    assert "conjecture_id" in c, c
    cj_id = c["conjecture_id"]

    # Snapshot pre-bump utility for both targets
    conn = server._get_db()
    goal_pre = conn.execute("SELECT utility_score FROM nodes WHERE id=?", (goal_id,)).fetchone()[0] or 0.0
    cj_pre = conn.execute("SELECT utility_score FROM nodes WHERE id=?", (cj_id,)).fetchone()[0] or 0.0
    conn.close()

    # Create task citing both
    t = json.loads(server.engram_add_task(payload_json=json.dumps({
        "description": "test task citing goal + conjecture",
        "goal_id": goal_id,
        "implements_ids": cj_id,
    })))
    assert "task_id" in t, t

    # Both targets should have received tier-2 citation bump: Q_new = Q_old + 0.10×(1-Q_old)
    conn = server._get_db()
    goal_post = conn.execute("SELECT utility_score FROM nodes WHERE id=?", (goal_id,)).fetchone()[0] or 0.0
    cj_post = conn.execute("SELECT utility_score FROM nodes WHERE id=?", (cj_id,)).fetchone()[0] or 0.0
    conn.close()

    expected_goal = round(goal_pre + 0.10 * (1.0 - goal_pre), 6)
    expected_cj = round(cj_pre + 0.10 * (1.0 - cj_pre), 6)
    assert abs(goal_post - expected_goal) < 1e-9, (
        f"goal_id should bump α=0.10 citation: expected {expected_goal}, got {goal_post}"
    )
    assert abs(cj_post - expected_cj) < 1e-9, (
        f"implements_ids should bump α=0.10 citation: expected {expected_cj}, got {cj_post}"
    )
