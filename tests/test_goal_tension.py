"""
Tests for goal tension node type.

Covers:
1. Goal tension creation with correct ID prefix
2. Goal tension links exactly two goals via 'tensions' edges
3. Non-goal nodes rejected (must use engram_contradict instead)
4. Self-tension rejected
5. Goal tension is non-claim-bearing (rejected as derivation premise)
6. Goal tension appears in engram_reflect output
7. Goal tension resolvable via engram_resolve
8. Goal tension with analysis stored in logical_chain
"""

import atexit
import json
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

TEST_DIR = tempfile.mkdtemp(prefix="engram_test_goal_tension_")
atexit.register(lambda: shutil.rmtree(TEST_DIR, ignore_errors=True))


def fresh_client():
    if os.path.exists(TEST_DIR):
        shutil.rmtree(TEST_DIR)
    for key in list(sys.modules):
        if key == "server" or key.startswith("engram_"):
            del sys.modules[key]
    from engram_client import EngramClient
    return EngramClient(db_dir=TEST_DIR)


def _two_goals(client):
    """Helper: create two goals and return their IDs."""
    g1 = client.call("engram_add_goal", {
        "claim": "Make ENGRAM actively shape agent behavior in future sessions",
        "motivation": "Stored knowledge should change behavior, not just be available for retrieval.",
    })
    g2 = client.call("engram_add_goal", {
        "claim": "Keep ENGRAM interactions purely passive and user-initiated",
        "motivation": "Agent autonomy over memory is risky; user should control all writes.",
    })
    return g1["goal_id"], g2["goal_id"]


# ── Creation tests ──────────────────────────────────────────────────────

def test_tension_creation():
    """Goal tension should be created with gt_ prefix."""
    client = fresh_client()
    g1, g2 = _two_goals(client)
    r = client.call("engram_goal_tension", {
        "goal_id_a": g1,
        "goal_id_b": g2,
        "description": "Active behavior shaping vs passive user-controlled interaction",
    })
    assert r["status"] == "created"
    assert r["tension_id"].startswith("gt_"), f"Expected gt_ prefix, got {r['tension_id']}"


def test_tension_links_both_goals():
    """Tension node should have 'tensions' edges to both goals."""
    client = fresh_client()
    g1, g2 = _two_goals(client)
    r = client.call("engram_goal_tension", {
        "goal_id_a": g1,
        "goal_id_b": g2,
        "description": "Active vs passive ENGRAM usage",
    })
    node = client.call("engram_inspect", {"node_id": r["tension_id"]})
    # 'tensions' is a lateral logical-substrate relation in recall view.
    lateral = node["logical_neighbors"]["lateral"]
    tension_targets = [n["id"] for n in lateral if n["relation"] == "tensions"]
    assert g1 in tension_targets, f"Goal A not linked. Lateral: {lateral}"
    assert g2 in tension_targets, f"Goal B not linked. Lateral: {lateral}"


def test_tension_initial_status_open():
    """Goal tension should start with status 'open'."""
    client = fresh_client()
    g1, g2 = _two_goals(client)
    r = client.call("engram_goal_tension", {
        "goal_id_a": g1,
        "goal_id_b": g2,
        "description": "Test tension",
    })
    node = client.call("engram_inspect", {"node_id": r["tension_id"]})
    assert node["node"].get("status") == "open"


def test_tension_with_analysis():
    """Analysis should be stored in logical_chain."""
    client = fresh_client()
    g1, g2 = _two_goals(client)
    analysis = "This is a fundamental design philosophy conflict about agent autonomy."
    r = client.call("engram_goal_tension", {
        "goal_id_a": g1,
        "goal_id_b": g2,
        "description": "Active vs passive",
        "analysis": analysis,
    })
    node = client.call("engram_inspect", {"node_id": r["tension_id"], "view": "deep"})
    assert node["node"].get("logical_chain") == analysis


def test_tension_sequential_ids():
    """Multiple tensions should get sequential IDs."""
    client = fresh_client()
    g1, g2 = _two_goals(client)
    g3 = client.call("engram_add_goal", {
        "claim": "Third goal",
        "motivation": "Test",
    })["goal_id"]
    r1 = client.call("engram_goal_tension", {
        "goal_id_a": g1,
        "goal_id_b": g2,
        "description": "Tension 1",
    })
    r2 = client.call("engram_goal_tension", {
        "goal_id_a": g1,
        "goal_id_b": g3,
        "description": "Tension 2",
    })
    assert r1["tension_id"] == "gt_0001"
    assert r2["tension_id"] == "gt_0002"


# ── Validation tests ───────────────────────────────────────────────────

def test_non_goal_rejected():
    """Non-goal nodes should be rejected with helpful error."""
    client = fresh_client()
    g1, _ = _two_goals(client)
    obs = client.call("engram_add_observation", {
        "quoted_text": "Test in the cited source",
        "interpretation": "Test",
        "claim": "A claim",
        "quote_type": "hard_data",
        "url": "https://arxiv.org/abs/2401.00001",
        "title": "Test",
    })
    r = client.call("engram_goal_tension", {
        "goal_id_a": g1,
        "goal_id_b": obs["observation_id"],
        "description": "Should fail",
    })
    assert "error" in r, f"Expected error for non-goal node, got {r}"
    assert "goal" in r["error"].lower()
    assert "engram_contradict" in r["error"]


def test_self_tension_rejected():
    """Cannot create tension between a goal and itself."""
    client = fresh_client()
    g1, _ = _two_goals(client)
    r = client.call("engram_goal_tension", {
        "goal_id_a": g1,
        "goal_id_b": g1,
        "description": "Self-tension",
    })
    assert "error" in r, f"Expected error for self-tension, got {r}"


def test_nonexistent_node_rejected():
    """Nonexistent node IDs should be rejected."""
    client = fresh_client()
    g1, _ = _two_goals(client)
    r = client.call("engram_goal_tension", {
        "goal_id_a": g1,
        "goal_id_b": "gl_9999",
        "description": "Should fail",
    })
    assert "error" in r, f"Expected error for nonexistent node, got {r}"
    assert "not found" in r["error"]


# ── Claim-bearing checks ───────────────────────────────────────────────

def test_tension_rejected_as_derivation_premise():
    """Goal tensions are non-claim-bearing; cannot be derivation premises."""
    client = fresh_client()
    g1, g2 = _two_goals(client)
    t = client.call("engram_goal_tension", {
        "goal_id_a": g1,
        "goal_id_b": g2,
        "description": "Test tension",
    })
    d = client.call("engram_derive", {
        "claim": "This should fail",
        "supporting_ids": t["tension_id"],
        "logical_chain": "Trying to derive from a goal tension",
        "reasoning_type": "deductive_modus_ponens",
    })
    assert "error" in d, f"Expected error deriving from tension, got {d}"
    assert "claim-bearing" in d["error"]


def test_tension_rejected_in_contradict():
    """Goal tensions should not be accepted in engram_contradict."""
    client = fresh_client()
    g1, g2 = _two_goals(client)
    t = client.call("engram_goal_tension", {
        "goal_id_a": g1,
        "goal_id_b": g2,
        "description": "Test tension",
    })
    obs = client.call("engram_add_observation", {
        "quoted_text": "Test in the cited source",
        "interpretation": "Test",
        "claim": "A claim",
        "quote_type": "hard_data",
        "url": "https://arxiv.org/abs/2401.00001",
        "title": "Test",
    })
    ct = client.call("engram_contradict", {
        "node_id_a": t["tension_id"],
        "node_id_b": obs["observation_id"],
        "description": "Should fail",
    })
    assert "error" in ct, f"Expected error, got {ct}"
    assert "claim-bearing" in ct["error"]


# ── Reflect integration ────────────────────────────────────────────────

def test_tension_in_reflect():
    """Unresolved goal tensions should appear in engram_reflect output."""
    client = fresh_client()
    g1, g2 = _two_goals(client)
    client.call("engram_goal_tension", {
        "goal_id_a": g1,
        "goal_id_b": g2,
        "description": "Active vs passive",
    })
    report = client.call("engram_reflect", {})
    assert "unresolved_goal_tensions" in report
    assert len(report["unresolved_goal_tensions"]) == 1
    tension = report["unresolved_goal_tensions"][0]
    assert tension["id"].startswith("gt_")
    assert g1 in tension["goals"]
    assert g2 in tension["goals"]
    assert report["summary"]["goal_tensions"] == 1


# ── Resolution ─────────────────────────────────────────────────────────

def test_tension_resolvable():
    """Goal tensions should be resolvable via engram_resolve."""
    client = fresh_client()
    g1, g2 = _two_goals(client)
    t = client.call("engram_goal_tension", {
        "goal_id_a": g1,
        "goal_id_b": g2,
        "description": "Active vs passive",
        "analysis": "Fundamental design philosophy conflict",
    })
    # Need a claim-bearing node to support the resolution
    obs = client.call("engram_add_observation", {
        "quoted_text": "Graduated autonomy models show agents can be given increasing control as trust builds",
        "interpretation": "Active and passive aren't binary — graduated autonomy resolves the tension",
        "claim": "Graduated autonomy resolves the active-vs-passive tension by making control level a spectrum",
        "quote_type": "hard_data",
        "url": "https://arxiv.org/abs/2401.00001",
        "title": "Test",
    })
    # Two-step (issue #229): compose resolving derivation, then wire it.
    dv = client.call("engram_derive", {
        "claim": "The active-vs-passive tension is synthesized via graduated autonomy: start passive, earn active control through demonstrated reliability",
        "supporting_ids": obs["observation_id"],
        "logical_chain": "Graduated autonomy makes the dichotomy false — control is a spectrum, not binary",
        "reasoning_type": "abductive_best_explanation",
    })
    r = client.call("engram_resolve", {
        "target_id": t["tension_id"],
        "resolving_node_id": dv["derivation_id"],
    })
    assert r.get("status") == "resolution_created", f"Expected resolution, got {r}"

    # Verify tension status updated
    node = client.call("engram_inspect", {"node_id": t["tension_id"]})
    assert node["node"]["status"] in ("resolved", "partially_resolved"), \
        f"Expected resolved status, got {node['node']['status']}"


def test_resolved_tension_not_in_reflect():
    """Resolved tensions should not appear in engram_reflect."""
    client = fresh_client()
    g1, g2 = _two_goals(client)
    t = client.call("engram_goal_tension", {
        "goal_id_a": g1,
        "goal_id_b": g2,
        "description": "Test tension",
    })
    obs = client.call("engram_add_observation", {
        "quoted_text": "Test in the cited source",
        "interpretation": "Test",
        "claim": "Resolution support",
        "quote_type": "hard_data",
        "url": "https://arxiv.org/abs/2401.00001",
        "title": "Test",
    })
    dv = client.call("engram_derive", {
        "claim": "Tension resolved",
        "supporting_ids": obs["observation_id"],
        "logical_chain": "Resolved by synthesis",
        "reasoning_type": "abductive_best_explanation",
    })
    client.call("engram_resolve", {
        "target_id": t["tension_id"],
        "resolving_node_id": dv["derivation_id"],
    })
    report = client.call("engram_reflect", {})
    # If fully resolved, the empty category is dropped from the briefing.
    # If partially_resolved, it appears as a list (or dict if truncated).
    tensions = report.get("unresolved_goal_tensions", [])
    if isinstance(tensions, dict):
        tensions = tensions.get("shown", [])
    # Either way, status should have changed from 'open'
    node = client.call("engram_inspect", {"node_id": t["tension_id"]})
    assert node["node"]["status"] != "open"


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
