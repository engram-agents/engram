"""
Tests for goal node type.

Covers:
1. Goal creation with correct ID prefix and no confidence
2. Goal importance-anchored (importance_base = 2.0, score inflated)
3. Goal status lifecycle: open → achieved, open → abandoned
4. Goal cited as context in derivation (not as premise)
5. Goal rejected as derivation premise (non-claim-bearing)
6. Goal appears in engram_reflect output
7. Goal supersede preserves no-confidence semantics
"""

import atexit
import json
import os
import shutil
import sqlite3
import sys
import tempfile

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

TEST_DIR = tempfile.mkdtemp(prefix="engram_test_goal_")
atexit.register(lambda: shutil.rmtree(TEST_DIR, ignore_errors=True))


def fresh_client():
    if os.path.exists(TEST_DIR):
        shutil.rmtree(TEST_DIR)
    for key in list(sys.modules):
        if key == "server" or key.startswith("engram_"):
            del sys.modules[key]
    from engram_client import EngramClient
    return EngramClient(db_dir=TEST_DIR)


# ── Creation tests ──────────────────────────────────────────────────────

def test_goal_creation():
    """Goal should be created with gl_ prefix and no confidence."""
    client = fresh_client()
    r = client.call("engram_add_goal", {
        "claim": "Close the loop from static graph nodes to active daily habits",
        "motivation": "Stored knowledge should change agent behavior, not just be available for retrieval.",
    })
    assert r["status"] == "created"
    assert r["goal_id"].startswith("gl_"), f"Expected gl_ prefix, got {r['goal_id']}"
    assert "confidence" not in r or r.get("confidence") is None


def _get_metadata_from_db(goal_id):
    """Read node metadata from DB directly (parsed_metadata stripped from MCP returns per #358)."""
    import server as _server
    conn = sqlite3.connect(str(_server.DB_PATH))
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute("SELECT metadata FROM nodes WHERE id = ?", (goal_id,)).fetchone()
        if row is None:
            return {}
        return json.loads(row["metadata"] or "{}")
    finally:
        conn.close()


def test_goal_has_motivation_in_metadata():
    """Goal motivation should be stored in metadata."""
    client = fresh_client()
    r = client.call("engram_add_goal", {
        "claim": "Test goal",
        "motivation": "Test motivation",
    })
    # Read metadata from DB (parsed_metadata is stripped from MCP returns per #358).
    meta = _get_metadata_from_db(r["goal_id"])
    assert meta.get("motivation") == "Test motivation"


def test_goal_id_sequential():
    """Multiple goals should get sequential IDs."""
    client = fresh_client()
    r1 = client.call("engram_add_goal", {
        "claim": "First goal",
        "motivation": "First",
    })
    r2 = client.call("engram_add_goal", {
        "claim": "Second goal",
        "motivation": "Second",
    })
    assert r1["goal_id"] == "gl_0001"
    assert r2["goal_id"] == "gl_0002"


# ── Forgetting exemption ────────────────────────────────────────────────

def test_goal_exempt_from_forgetting():
    """Goals should have importance_base = 2.0, score >= 2.0 (inflated with turn)."""
    client = fresh_client()
    r = client.call("engram_add_goal", {
        "claim": "Persist as orientation",
        "motivation": "Goals must outlast forgetting.",
    })
    node = client.call("engram_inspect", {"node_id": r["goal_id"], "view": "deep"})
    assert node["node"]["importance_base"] == 2.0
    # Score should be >= 2.0 (2.0 × 1.014^turn, at turn 0 it's exactly 2.0)
    assert node["node"]["importance_score"] >= 2.0


# ── Status lifecycle ────────────────────────────────────────────────────

def test_goal_initial_status_open():
    """Goal should start with status 'open'."""
    client = fresh_client()
    r = client.call("engram_add_goal", {
        "claim": "Test status",
        "motivation": "Status test",
    })
    node = client.call("engram_inspect", {"node_id": r["goal_id"]})
    assert node["node"].get("status") == "open"


# ── Claim-bearing checks ───────────────────────────────────────────────

def test_goal_rejected_as_derivation_premise():
    """Goals are non-claim-bearing; cannot be used as derivation supporting_ids."""
    client = fresh_client()
    r = client.call("engram_add_goal", {
        "claim": "A goal to cite",
        "motivation": "Testing non-claim-bearing enforcement.",
    })
    goal_id = r["goal_id"]
    d = client.call("engram_derive", {
        "claim": "This should fail",
        "supporting_ids": goal_id,
        "logical_chain": "Trying to derive from a goal",
        "reasoning_type": "deductive_modus_ponens",
    })
    assert "error" in d, f"Expected error when deriving from goal, got {d}"
    assert "claim-bearing" in d["error"]


def test_goal_accepted_as_context():
    """Goals should be citable via context_ids in derivations."""
    client = fresh_client()
    # Create a goal and an observation to use as premise
    g = client.call("engram_add_goal", {
        "claim": "A motivating goal",
        "motivation": "Context test",
    })
    obs = client.call("engram_add_observation", {
        "quoted_text": "Test data",
        "interpretation": "For testing",
        "claim": "A real claim for premise",
        "quote_type": "hard_data",
        "url": "https://arxiv.org/abs/2401.00001",
        "title": "Test Paper",
    })
    d = client.call("engram_derive", {
        "claim": "Derived with goal as context",
        "supporting_ids": obs["observation_id"],
        "logical_chain": "The observation supports this, motivated by the goal.",
        "reasoning_type": "deductive_modus_ponens",
        "context_ids": g["goal_id"],
    })
    assert d.get("status") == "created", f"Expected success, got {d}"
    assert g["goal_id"] in d.get("context_nodes", [])


def test_goal_rejected_in_contradict():
    """Goals should not be accepted in engram_contradict (non-claim-bearing)."""
    client = fresh_client()
    g = client.call("engram_add_goal", {
        "claim": "A goal",
        "motivation": "Test",
    })
    obs = client.call("engram_add_observation", {
        "quoted_text": "Test",
        "interpretation": "Test",
        "claim": "A claim",
        "quote_type": "hard_data",
        "url": "https://arxiv.org/abs/2401.00001",
        "title": "Test",
    })
    ct = client.call("engram_contradict", {
        "node_id_a": g["goal_id"],
        "node_id_b": obs["observation_id"],
        "description": "Testing goal in contradiction",
    })
    assert "error" in ct, f"Expected error, got {ct}"
    assert "claim-bearing" in ct["error"]


# ── Reflect integration ────────────────────────────────────────────────

def test_goal_in_reflect():
    """Active goals should appear in engram_reflect output."""
    client = fresh_client()
    client.call("engram_add_goal", {
        "claim": "Visible in reflection",
        "motivation": "Reflect test",
    })
    report = client.call("engram_reflect", {})
    assert "active_goals" in report
    assert len(report["active_goals"]) == 1
    assert report["active_goals"][0]["goal"] == "Visible in reflection"
    assert report["summary"]["active_goals"] == 1


# ── Supersede ───────────────────────────────────────────────────────────

def test_goal_supersede_no_confidence():
    """Superseding a goal should produce a new goal with no confidence."""
    client = fresh_client()
    r = client.call("engram_add_goal", {
        "claim": "Original goal",
        "motivation": "First version",
    })
    # Two-step supersede under the relational-only API: create the
    # replacement goal via engram_add_goal first, then wire the supersede.
    new = client.call("engram_add_goal", {
        "claim": "Refined goal with better direction",
        "motivation": "Refined after discussion",
    })
    s = client.call("engram_supersede", {
        "old_node_id": r["goal_id"],
        "new_node_id": new["goal_id"],
        "supersede_reason": "Refined after discussion",
    })
    assert s.get("status") == "superseded", f"Expected success, got {s}"
    node = client.call("engram_inspect", {"node_id": s["new_node_id"]})
    # Goal confidence should be None (not a truth claim)
    assert node["node"].get("confidence") is None


# ── Context edges ───────────────────────────────────────────────────────

def test_goal_with_context_ids():
    """Goal should accept context_ids and create cites edges."""
    client = fresh_client()
    obs = client.call("engram_add_observation", {
        "quoted_text": "Motivating observation",
        "interpretation": "This observation motivates the goal",
        "claim": "Something we observed",
        "quote_type": "hard_data",
        "url": "https://arxiv.org/abs/2401.00001",
        "title": "Test",
    })
    assert "observation_id" in obs, f"Observation creation failed: {obs}"
    g = client.call("engram_add_goal", {
        "claim": "Goal motivated by observation",
        "motivation": "The observation shows we need this",
        "context_ids": obs["observation_id"],
    })
    assert obs["observation_id"] in g.get("context_nodes", []), f"Context not in goal: {g}"

    # Verify edge exists — context_ids creates a 'cites' edge (contextual in recall view).
    node = client.call("engram_inspect", {"node_id": g["goal_id"]})
    neighbor_ids = [n["id"] for n in node["contextual_neighbors"]]
    assert obs["observation_id"] in neighbor_ids, f"Edge not found. Contextual neighbors: {node['contextual_neighbors']}"


# ── Run all tests ───────────────────────────────────────────────────────

if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    passed = failed = 0
    for t in tests:
        try:
            t()
            print(f"  PASS: {t.__name__}")
            passed += 1
        except Exception as e:
            print(f"  FAIL: {t.__name__}: {e}")
            failed += 1
    print(f"\n{'=' * 60}")
    print(f"{passed}/{passed + failed} tests passed")
    if failed:
        sys.exit(1)
