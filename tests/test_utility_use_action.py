"""Tests for the USE-action utility scoring model (Lei 2026-05-19 design call).

Replaces the old registration-then-confirmation two-step (recall_window.json +
_recall_window_check) with direct action-keyed USE bumps. Every node USE
action bumps utility_score immediately, α determined by engagement tier.

Coverage:
  1. _utility_reward alpha table: derive (0.15), inspect/subgraph/mention (0.10)
  2. _utility_reward: missing ID is silently skipped, returns 0
  3. _utility_reward: unknown action falls back to USE_ALPHA_DEFAULT
  4. engram_surface does NOT bump utility
  5. engram_query does NOT bump utility
  6. engram_inspect bumps the inspected node (integration)
  7. engram_get_subgraph bumps only the seed, not returned neighbors (integration)
  8. engram_derive bumps all supporting_ids with alpha=0.15 (integration)
"""

import json
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


TEST_DIR = os.path.join(tempfile.gettempdir(), "engram_test_utility_use_action")


def fresh_server():
    if os.path.exists(TEST_DIR):
        shutil.rmtree(TEST_DIR)
    if "server" in sys.modules:
        del sys.modules["server"]
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
            "claim": f"Axiom {i} for utility test.",
            "basis": f"utility test fixture axiom {i}.",
        })))
        ids.append(r["axiom_id"])
    return ids


def _make_derive(server, premise_ids):
    """Derive from premise_ids; return the derivation result dict."""
    return json.loads(server.engram_derive(payload_json=json.dumps({
        "claim": "Derived claim for utility test.",
        "supporting_ids": ",".join(premise_ids),
        "logical_chain": "Test derivation for utility scoring.",
        "reasoning_type": "deductive_modus_ponens",
    })))


# ---------------------------------------------------------------------------
# Unit tests: _utility_reward directly
# ---------------------------------------------------------------------------


def test_utility_reward_derive_action_uses_alpha_0_15():
    """derive action → α=0.15; Q_new = 0 + 0.15*(1-0) = 0.15."""
    server = fresh_server()
    (ax_id,) = _seed_axioms(server, n=1)
    before = _get_utility(server, ax_id)
    assert before == 0.0 or before is None, f"Expected fresh node to have 0.0 utility, got {before}"

    conn = server._get_db()
    try:
        server._utility_reward(conn, [ax_id], action="derive")
        conn.commit()
    finally:
        conn.close()

    after = _get_utility(server, ax_id)
    assert abs(after - 0.15) < 1e-9, f"derive α=0.15: expected 0.15, got {after}"


def test_utility_reward_inspect_action_uses_alpha_0_10():
    """inspect action → α=0.10; Q_new = 0 + 0.10*(1-0) = 0.10."""
    server = fresh_server()
    (ax_id,) = _seed_axioms(server, n=1)

    conn = server._get_db()
    try:
        server._utility_reward(conn, [ax_id], action="inspect")
        conn.commit()
    finally:
        conn.close()

    after = _get_utility(server, ax_id)
    assert abs(after - 0.10) < 1e-9, f"inspect α=0.10: expected 0.10, got {after}"


def test_utility_reward_subgraph_action_uses_alpha_0_10():
    """subgraph action → α=0.10; Q_new = 0 + 0.10*(1-0) = 0.10."""
    server = fresh_server()
    (ax_id,) = _seed_axioms(server, n=1)

    conn = server._get_db()
    try:
        server._utility_reward(conn, [ax_id], action="subgraph")
        conn.commit()
    finally:
        conn.close()

    after = _get_utility(server, ax_id)
    assert abs(after - 0.10) < 1e-9, f"subgraph α=0.10: expected 0.10, got {after}"


def test_utility_reward_mention_action_uses_alpha_0_10():
    """mention action → α=0.10; Q_new = 0 + 0.10*(1-0) = 0.10.

    Pins the `mention` key in USE_ALPHA. The action will be wired by a
    follow-up PR that simplifies engram-utility-credit-mention-stop.py
    to call _utility_reward directly. This test anchors the registration
    so the follow-up doesn't have to add the key on its own.
    """
    server = fresh_server()
    (ax_id,) = _seed_axioms(server, n=1)

    conn = server._get_db()
    try:
        server._utility_reward(conn, [ax_id], action="mention")
        conn.commit()
    finally:
        conn.close()

    after = _get_utility(server, ax_id)
    assert abs(after - 0.10) < 1e-9, f"mention α=0.10: expected 0.10, got {after}"


def test_utility_reward_missing_id_silently_skipped():
    """A node_id not in the DB is silently skipped; count=0, no exception."""
    server = fresh_server()

    conn = server._get_db()
    try:
        count = server._utility_reward(conn, ["ob_9999"], action="derive")
        conn.commit()
    finally:
        conn.close()

    assert count == 0, f"Expected 0 updates for missing ID, got {count}"


def test_utility_reward_dedups_duplicate_ids_in_one_call():
    """Same ID passed twice in one call → ONE bump, not two.

    The old recall_window-based model was immune to duplicates via dict-key
    semantics. The action-keyed model enforces this at the bump boundary
    so future callers (existing or new) inherit the invariant. Guards
    against e.g. supporting_ids="ob_1234,ob_1234" producing two sequential
    Q-updates.
    """
    server = fresh_server()
    (ax_id,) = _seed_axioms(server, n=1)

    conn = server._get_db()
    try:
        count = server._utility_reward(conn, [ax_id, ax_id, ax_id], action="derive")
        conn.commit()
    finally:
        conn.close()

    after = _get_utility(server, ax_id)
    # One bump → 0 + 0.15*(1-0) = 0.15 (NOT 0.15 + 0.15*0.85 + ... compound)
    assert abs(after - 0.15) < 1e-9, (
        f"Expected single-bump α=0.15 after dedup, got {after} "
        f"(double-bump would be ~0.2775, triple ~0.385875)"
    )
    assert count == 1, f"Expected updated=1 (deduped), got {count}"


def test_utility_reward_dedups_preserves_first_occurrence_order():
    """Dedup is order-preserving (first-occurrence wins), matching engram_ids.find_node_ids."""
    server = fresh_server()
    a_id, b_id = _seed_axioms(server, n=2)

    conn = server._get_db()
    try:
        # Order matters for cohort-consistency with other dedup helpers.
        count = server._utility_reward(conn, [a_id, b_id, a_id, b_id], action="derive")
        conn.commit()
    finally:
        conn.close()

    after_a = _get_utility(server, a_id)
    after_b = _get_utility(server, b_id)
    assert abs(after_a - 0.15) < 1e-9
    assert abs(after_b - 0.15) < 1e-9
    assert count == 2, f"Expected updated=2 (two unique IDs), got {count}"


def test_utility_reward_default_alpha_on_unknown_action():
    """Unknown action label falls back to USE_ALPHA_DEFAULT (0.10)."""
    server = fresh_server()
    (ax_id,) = _seed_axioms(server, n=1)

    conn = server._get_db()
    try:
        server._utility_reward(conn, [ax_id], action="totally_unknown_action")
        conn.commit()
    finally:
        conn.close()

    after = _get_utility(server, ax_id)
    expected = server.USE_ALPHA_DEFAULT
    assert abs(after - expected) < 1e-9, (
        f"Unknown action should use default α={expected}, got {after}"
    )


# ---------------------------------------------------------------------------
# Integration tests: tool-level call-site verification
# ---------------------------------------------------------------------------


def test_engram_surface_does_not_bump_utility():
    """Calling engram_surface leaves utility_score unchanged.

    surface is a pure-return operation — the agent's later USE of
    returned IDs will trigger the bump, not the surface call itself.
    """
    server = fresh_server()
    (ax_id,) = _seed_axioms(server, n=1)
    before = _get_utility(server, ax_id)

    server.engram_surface(query="Axiom 0 for utility test")

    after = _get_utility(server, ax_id)
    assert after == before, (
        f"engram_surface must not bump utility; before={before}, after={after}"
    )


def test_engram_query_does_not_bump_utility():
    """Calling engram_query leaves utility_score unchanged.

    query is also a pure-return operation for utility purposes.
    """
    server = fresh_server()
    (ax_id,) = _seed_axioms(server, n=1)
    before = _get_utility(server, ax_id)

    server.engram_query(payload_json=json.dumps({"query": "Axiom 0 for utility test"}))

    after = _get_utility(server, ax_id)
    assert after == before, (
        f"engram_query must not bump utility; before={before}, after={after}"
    )


def test_engram_inspect_bumps_target():
    """engram_inspect bumps the inspected node's utility_score.

    inspect is a deliberate probe — the agent is actively engaging with
    a specific node. α=0.10 (tier-2).
    """
    server = fresh_server()
    (ax_id,) = _seed_axioms(server, n=1)
    before = _get_utility(server, ax_id)

    result = json.loads(server.engram_inspect(node_id=ax_id))
    assert "error" not in result, f"engram_inspect failed: {result}"

    after = _get_utility(server, ax_id)
    expected = round(before + 0.10 * (1.0 - before), 6)
    assert abs(after - expected) < 1e-9, (
        f"engram_inspect should bump with α=0.10; expected {expected}, got {after}"
    )


def test_engram_get_subgraph_bumps_seed_only():
    """engram_get_subgraph bumps the seed node; returned neighbors are NOT bumped.

    The agent's USE target is the seed — what they're asking to explore.
    Neighbors are shown as context, not actively used.
    """
    server = fresh_server()
    ax_seed, ax_neighbor = _seed_axioms(server, n=2)

    # Create a derivation rooted at ax_seed that references ax_neighbor,
    # so ax_neighbor appears in the subgraph as a neighbor.
    derive_result = _make_derive(server, [ax_seed, ax_neighbor])
    assert "error" not in derive_result, f"derive failed: {derive_result}"
    dv_id = derive_result["derivation_id"]

    # Reset utility of ax_neighbor by reading the current value before subgraph call.
    before_seed = _get_utility(server, ax_seed)
    before_neighbor = _get_utility(server, ax_neighbor)
    # Note: derive already bumped both ax_seed and ax_neighbor (both are supporting_ids).
    # The test checks the *incremental* change from subgraph, not absolute values.

    result = json.loads(server.engram_get_subgraph(node_id=ax_seed))
    assert "error" not in result, f"engram_get_subgraph failed: {result}"
    # Verify ax_neighbor appears in topology (i.e., it IS a neighbor)
    assert ax_neighbor in result["topology"] or dv_id in result["topology"], (
        "Expected neighbor nodes in subgraph topology"
    )

    after_seed = _get_utility(server, ax_seed)
    after_neighbor = _get_utility(server, ax_neighbor)

    # Seed should have been bumped (increment > 0 from the subgraph call)
    expected_seed_bump = round(before_seed + 0.10 * (1.0 - before_seed), 6)
    assert abs(after_seed - expected_seed_bump) < 1e-9, (
        f"seed should be bumped with α=0.10; expected {expected_seed_bump}, got {after_seed}"
    )

    # Neighbor should NOT have been bumped by the subgraph call
    assert after_neighbor == before_neighbor, (
        f"neighbor must not be bumped by subgraph; before={before_neighbor}, after={after_neighbor}"
    )


def test_engram_derive_bumps_supporting_ids():
    """engram_derive bumps all supporting_ids with action='derive' (α=0.15).

    Premise citation is tier-1 (deepest engagement) — the agent used
    these nodes as the epistemic foundation for a new claim.
    """
    server = fresh_server()
    ax_a, ax_b = _seed_axioms(server, n=2)
    before_a = _get_utility(server, ax_a)
    before_b = _get_utility(server, ax_b)

    result = _make_derive(server, [ax_a, ax_b])
    assert "error" not in result, f"engram_derive failed: {result}"

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
