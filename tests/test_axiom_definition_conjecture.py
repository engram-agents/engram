"""
Tests for axiom, definition, and conjecture node types.

Covers:
1. Axiom creation, fixed confidence 1.0, importance-anchored
2. Definition creation, no confidence, term in metadata
3. Conjecture creation, configurable confidence, capped range
4. Axiom cited by derivation (claim-bearing)
5. Definition cited via context_ids in kg_derive (not claim-bearing)
6. Conjecture resolved via kg_resolve
7. Conjecture in kg_reflect output
8. Definition NOT accepted as supporting_id in kg_derive
"""

import json
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

TEST_DIR = os.path.join(tempfile.gettempdir(), "engram_test_adc")


def fresh_client():
    if os.path.exists(TEST_DIR):
        shutil.rmtree(TEST_DIR)
    if "server" in sys.modules:
        del sys.modules["server"]
    from engram_client import EngramClient
    return EngramClient(db_dir=TEST_DIR)


# ── Axiom tests ──────────────────────────────────────────────────────────

def test_axiom_creation():
    """Axiom should be created with confidence 1.0."""
    client = fresh_client()
    r = client.call("engram_add_axiom", {
        "claim": "Honesty is non-negotiable in graph construction",
        "basis": "Foundational principle for epistemic integrity.",
    })
    assert r["status"] == "created"
    assert r["confidence"] == 1.0
    assert r["axiom_id"].startswith("ax_")


def test_axiom_exempt_from_forgetting():
    """Axiom importance_base should be 2.0 (exempt from decay)."""
    client = fresh_client()
    r = client.call("engram_add_axiom", {
        "claim": "All observations must trace to evidence",
        "basis": "Core provenance requirement.",
    })
    insp = client.call("engram_inspect", {"node_id": r["axiom_id"], "view": "deep"})
    node = insp["node"]
    assert node["importance_base"] == 2.0


def test_axiom_is_claim_bearing():
    """Axiom should be usable as a premise in kg_derive."""
    client = fresh_client()
    ax = client.call("engram_add_axiom", {
        "claim": "Shortcuts undermine provenance integrity",
        "basis": "Learned from the fake URL incident.",
    })
    # Create an observation to combine with
    ob = client.call("engram_add_observation", {
        "url": "https://example.com/test",
        "title": "Test",
        "quoted_text": "Some evidence.",
        "interpretation": "Test.",
        "claim": "A specific shortcut was taken",
        "quote_type": "hard_data",
    })
    dv = client.call("engram_derive", {
        "claim": "This shortcut violated the integrity principle",
        "supporting_ids": f"{ax['axiom_id']},{ob['observation_id']}",
        "logical_chain": "Axiom states shortcuts undermine integrity. Observation shows a shortcut. Therefore: violation.",
        "reasoning_type": "deductive_modus_ponens",
    })
    assert dv["status"] == "created"
    assert ax["axiom_id"] in dv["supporting_nodes"]


def test_axiom_with_context():
    """Axiom with context_ids should create cites edges."""
    client = fresh_client()
    ob = client.call("engram_add_observation", {
        "url": "https://example.com/test",
        "title": "Test",
        "quoted_text": "Context.",
        "interpretation": "Background.",
        "claim": "Some background context",
        "quote_type": "hard_data",
    })
    ax = client.call("engram_add_axiom", {
        "claim": "Test axiom with context",
        "basis": "Testing context_ids.",
        "context_ids": ob["observation_id"],
    })
    assert ob["observation_id"] in ax["context_nodes"]
    sub = client.call("engram_get_subgraph", {"node_id": ax["axiom_id"]})
    # topology: look for outgoing cites edges from the axiom
    topo_entries = sub["topology"].get(ax["axiom_id"], [])
    cites = [e for e in topo_entries if e["relation"] == "cites" and e["direction"] == "outgoing"]
    assert len(cites) == 1


# ── Definition tests ──────────────────────────────────────────────────────

def test_definition_creation():
    """Definition should be created with term in metadata."""
    client = fresh_client()
    r = client.call("engram_add_definition", {
        "term": "recession",
        "definition": "Two consecutive quarters of negative GDP growth",
    })
    assert r["status"] == "created"
    assert r["definition_id"].startswith("df_")
    assert r["term"] == "recession"


def test_definition_no_confidence():
    """Definition nodes should have no confidence (NULL in DB, omitted from dict)."""
    client = fresh_client()
    r = client.call("engram_add_definition", {
        "term": "bull market",
        "definition": "A market condition where prices are rising or expected to rise",
    })
    insp = client.call("engram_inspect", {"node_id": r["definition_id"], "view": "deep"})
    node = insp["node"]
    # confidence is NULL in DB, so _node_to_dict omits it
    assert "confidence" not in node or node["confidence"] is None


def test_definition_in_metadata():
    """Term and definition should be stored separately in metadata."""
    client = fresh_client()
    r = client.call("engram_add_definition", {
        "term": "epistemic",
        "definition": "Relating to knowledge or the conditions for acquiring it",
    })
    insp = client.call("engram_inspect", {"node_id": r["definition_id"], "view": "deep"})
    node = insp["node"]
    meta = json.loads(node["metadata"]) if isinstance(node["metadata"], str) else node["metadata"]
    assert meta["term"] == "epistemic"
    assert meta["definition"] == "Relating to knowledge or the conditions for acquiring it"


def test_definition_exempt_from_forgetting():
    """Definition importance_base should be 2.0."""
    client = fresh_client()
    r = client.call("engram_add_definition", {
        "term": "provenance",
        "definition": "The origin and chain of custody of a piece of information",
    })
    insp = client.call("engram_inspect", {"node_id": r["definition_id"], "view": "deep"})
    node = insp["node"]
    assert node["importance_base"] == 2.0


def test_definition_not_claim_bearing():
    """Definition should NOT be accepted as supporting_id in kg_derive."""
    client = fresh_client()
    df = client.call("engram_add_definition", {
        "term": "test_term",
        "definition": "A term used for testing.",
    })
    ob = client.call("engram_add_observation", {
        "url": "https://example.com/test",
        "title": "Test",
        "quoted_text": "Data.",
        "interpretation": "Test.",
        "claim": "Some claim",
        "quote_type": "hard_data",
    })
    # Try to derive using definition as supporting_id — should fail
    dv = client.call("engram_derive", {
        "claim": "Derived using definition as premise",
        "supporting_ids": f"{df['definition_id']},{ob['observation_id']}",
        "logical_chain": "This should fail.",
        "reasoning_type": "deductive_modus_ponens",
    })
    assert "error" in dv, f"Expected error when using definition as supporting_id, got {dv}"


def test_definition_via_context_ids():
    """Definition should be referenceable via context_ids in kg_derive."""
    client = fresh_client()
    df = client.call("engram_add_definition", {
        "term": "claim-bearing",
        "definition": "A node type that asserts something about the world",
    })
    ob = client.call("engram_add_observation", {
        "url": "https://example.com/test",
        "title": "Test",
        "quoted_text": "Data.",
        "interpretation": "Test.",
        "claim": "Observations are claim-bearing",
        "quote_type": "hard_data",
    })
    dv = client.call("engram_derive", {
        "claim": "Observations assert claims per the definition of claim-bearing",
        "supporting_ids": ob["observation_id"],
        "logical_chain": "Observation states observations are claim-bearing. Definition clarifies the term.",
        "reasoning_type": "deductive_modus_ponens",
        "context_ids": df["definition_id"],
    })
    assert dv["status"] == "created"
    assert df["definition_id"] in dv["context_nodes"]


# ── Conjecture tests ─────────────────────────────────────────────────────

def test_conjecture_creation():
    """Conjecture should be created with default confidence 0.40."""
    client = fresh_client()
    r = client.call("engram_add_conjecture", {
        "claim": "Structured memory substitutes for model scale",
        "basis": "Small model + KG outperformed large model vanilla in eval.",
    })
    assert r["status"] == "created"
    assert r["conjecture_id"].startswith("cj_")
    assert r["confidence"] == 0.40


def test_conjecture_custom_confidence():
    """Conjecture should accept custom confidence within range."""
    client = fresh_client()
    r = client.call("engram_add_conjecture", {
        "claim": "Test conjecture",
        "basis": "Testing.",
        "initial_confidence": 0.55,
    })
    assert r["confidence"] == 0.55


def test_conjecture_confidence_capped():
    """Conjecture confidence should be capped at [0.10, 0.60]."""
    client = fresh_client()
    # Too high
    r1 = client.call("engram_add_conjecture", {
        "claim": "Overconfident conjecture",
        "basis": "Testing cap.",
        "initial_confidence": 0.95,
    })
    assert r1["confidence"] == 0.60

    # Too low
    r2 = client.call("engram_add_conjecture", {
        "claim": "Underconfident conjecture",
        "basis": "Testing floor.",
        "initial_confidence": 0.01,
    })
    assert r2["confidence"] == 0.10


def test_conjecture_is_claim_bearing():
    """Conjecture should be usable as a premise in kg_derive."""
    client = fresh_client()
    cj = client.call("engram_add_conjecture", {
        "claim": "KG enables smaller models to match larger ones",
        "basis": "Eval results suggest this.",
    })
    ob = client.call("engram_add_observation", {
        "url": "https://example.com/eval",
        "title": "Eval results",
        "quoted_text": "Flash Lite + KG scored higher.",
        "interpretation": "Smaller model outperformed.",
        "claim": "Flash Lite with KG outperformed Flash without KG",
        "quote_type": "hard_data",
    })
    dv = client.call("engram_derive", {
        "claim": "Evidence supports the scale-substitution conjecture",
        "supporting_ids": f"{cj['conjecture_id']},{ob['observation_id']}",
        "logical_chain": "Conjecture states KG substitutes for scale. Eval shows small+KG > large. Supports conjecture.",
        "reasoning_type": "inductive_generalization",
    })
    assert dv["status"] == "created"


def test_conjecture_resolved():
    """Conjecture should be resolvable via kg_resolve."""
    client = fresh_client()
    cj = client.call("engram_add_conjecture", {
        "claim": "Rate cuts will happen in Q3",
        "basis": "Based on market signals.",
    })
    ob = client.call("engram_add_observation", {
        "url": "https://example.com/fed",
        "title": "Fed decision",
        "quoted_text": "Rates held steady through Q3.",
        "interpretation": "No rate cuts happened.",
        "claim": "Fed held rates steady through Q3 2026",
        "quote_type": "hard_data",
    })
    # Two-step (issue #229): compose resolving derivation, then wire it.
    dv = client.call("engram_derive", {
        "claim": "Rate cuts did not happen in Q3",
        "supporting_ids": ob["observation_id"],
        "logical_chain": "Fed held rates steady — conjecture refuted.",
        "reasoning_type": "deductive_modus_tollens",
    })
    res = client.call("engram_resolve", {
        "target_id": cj["conjecture_id"],
        "resolving_node_id": dv["derivation_id"],
        "prediction_outcome": "refuted",
    })
    assert res["status"] == "resolution_created"

    # Verify conjecture status is updated (use inspect for full node content)
    insp = client.call("engram_inspect", {"node_id": cj["conjecture_id"]})
    node = insp["node"]
    assert node["status"] == "refuted"


def test_conjecture_in_reflect():
    """Open conjectures should appear in kg_reflect output."""
    client = fresh_client()
    cj = client.call("engram_add_conjecture", {
        "claim": "Axioms improve graph navigability",
        "basis": "Hypothesis from design session.",
    })
    reflect = client.call("engram_reflect", {})
    assert reflect["summary"]["open_conjectures"] >= 1
    cj_ids = [c["id"] for c in reflect["open_conjectures"]]
    assert cj["conjecture_id"] in cj_ids


def test_conjecture_invalid_outcome():
    """Conjecture resolve with invalid outcome should fail."""
    client = fresh_client()
    cj = client.call("engram_add_conjecture", {
        "claim": "Test conjecture for invalid outcome",
        "basis": "Testing.",
    })
    ob = client.call("engram_add_observation", {
        "url": "https://example.com/test",
        "title": "Test",
        "quoted_text": "Data.",
        "interpretation": "Test.",
        "claim": "Supporting claim",
        "quote_type": "hard_data",
    })
    dv = client.call("engram_derive", {
        "claim": "Attempt with bad outcome",
        "supporting_ids": ob["observation_id"],
        "logical_chain": "Testing.",
        "reasoning_type": "deductive_modus_ponens",
    })
    res = client.call("engram_resolve", {
        "target_id": cj["conjecture_id"],
        "resolving_node_id": dv["derivation_id"],
        "prediction_outcome": "confirmed",  # not valid for conjectures
    })
    assert "error" in res


# ═════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    tests = [
        # Axiom
        test_axiom_creation,
        test_axiom_exempt_from_forgetting,
        test_axiom_is_claim_bearing,
        test_axiom_with_context,
        # Definition
        test_definition_creation,
        test_definition_no_confidence,
        test_definition_in_metadata,
        test_definition_exempt_from_forgetting,
        test_definition_not_claim_bearing,
        test_definition_via_context_ids,
        # Conjecture
        test_conjecture_creation,
        test_conjecture_custom_confidence,
        test_conjecture_confidence_capped,
        test_conjecture_is_claim_bearing,
        test_conjecture_resolved,
        test_conjecture_in_reflect,
        test_conjecture_invalid_outcome,
    ]

    passed = 0
    failed = 0
    errors = []
    for test in tests:
        try:
            test()
            print(f"  PASS: {test.__name__}")
            passed += 1
        except Exception as e:
            print(f"  FAIL: {test.__name__}: {e}")
            failed += 1
            errors.append((test.__name__, str(e)))

    print(f"\n{'='*60}")
    print(f"{passed}/{passed + failed} tests passed")
    if errors:
        print(f"\nFailed tests:")
        for name, err in errors:
            print(f"  - {name}: {err}")
        sys.exit(1)
