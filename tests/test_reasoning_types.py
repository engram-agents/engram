"""
Tests for the reasoning types system in ENGRAM v2.

Tests cover:
1. All 13 reasoning types produce correct confidence values
2. Legacy derivation_mode fallback still works
3. Structural validation (premise counts, multi-source)
4. Abductive confidence caps
5. Corroborative reasoning (generalization, consensus)
6. engram_resolve with reasoning_type
7. engram_stats reasoning type breakdown
8. Evidence root tracing for multi-source validation
"""

import atexit
import json
import os
import shutil
import sys
import tempfile

# Ensure server.py is importable
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

TEST_DIR = tempfile.mkdtemp(prefix="engram_test_reasoning_types_")
atexit.register(lambda: shutil.rmtree(TEST_DIR, ignore_errors=True))


def fresh_client():
    """Create a fresh EngramClient with an empty database."""
    if os.path.exists(TEST_DIR):
        shutil.rmtree(TEST_DIR)
    # Force reimport so server module picks up new DATA_DIR
    for key in list(sys.modules):
        if key == "server" or key.startswith("engram_"):
            del sys.modules[key]
    from engram_client import EngramClient
    return EngramClient(db_dir=TEST_DIR)


def setup_base_graph(client):
    """Create a base graph with two evidence nodes and four observations."""
    # Evidence 1: Reuters
    r1 = client.call("engram_add_observation", {
        "url": "https://reuters.com/article1",
        "title": "Gold drops sharply",
        "quoted_text": "Gold fell 9.6% this week, its worst weekly loss since 2013.",
        "interpretation": "Significant gold price decline recorded",
        "claim": "Gold fell 9.6% this week",
        "quote_type": "hard_data",
        "source_date": "2026-03-20",
    })
    assert r1.get("status") == "created", f"Failed to create ob1: {r1}"
    ob1_id = r1["observation_id"]
    ev1_id = r1["evidence_id"]

    r2 = client.call("engram_add_observation", {
        "evidence_id": ev1_id,
        "quoted_text": "Analysts attributed the decline to rising real interest rates.",
        "interpretation": "Causal attribution for gold decline",
        "claim": "Rising real interest rates caused gold's decline",
        "quote_type": "attributed_analysis",
    })
    assert r2.get("status") == "created", f"Failed to create ob2: {r2}"
    ob2_id = r2["observation_id"]

    # Evidence 2: FT (independent source)
    r3 = client.call("engram_add_observation", {
        "url": "https://ft.com/article2",
        "title": "Gold faces worst week in decade",
        "quoted_text": "The precious metal lost approximately 10% of its value.",
        "interpretation": "Independent confirmation of gold price drop",
        "claim": "Gold lost approximately 10% this week",
        "quote_type": "hard_data",
        "source_date": "2026-03-20",
    })
    assert r3.get("status") == "created", f"Failed to create ob3: {r3}"
    ob3_id = r3["observation_id"]
    ev2_id = r3["evidence_id"]

    r4 = client.call("engram_add_observation", {
        "evidence_id": ev2_id,
        "quoted_text": "The Federal Reserve held rates at 4.25-4.50% on March 19.",
        "interpretation": "Fed rate decision as hard data point",
        "claim": "The Fed held rates at 4.25-4.50% on March 19, 2026",
        "quote_type": "hard_data",
    })
    assert r4.get("status") == "created", f"Failed to create ob4: {r4}"
    ob4_id = r4["observation_id"]

    return {
        "ev1": ev1_id, "ev2": ev2_id,
        "ob1": ob1_id, "ob2": ob2_id, "ob3": ob3_id, "ob4": ob4_id,
    }


# ---------------------------------------------------------------------------
# Test: All 13 reasoning types accepted
# ---------------------------------------------------------------------------

def test_all_reasoning_types_accepted():
    """Every valid reasoning type should be accepted by engram_derive."""
    client = fresh_client()
    ids = setup_base_graph(client)

    all_types = [
        "deductive_modus_ponens", "deductive_modus_tollens",
        "deductive_hypothetical_syllogism", "deductive_disjunctive",
        "deductive_reductio",
        "inductive_generalization", "inductive_statistical",
        "inductive_analogy", "inductive_causal",
        "abductive_best_explanation", "abductive_elimination",
        "authority_expert", "authority_consensus",
    ]

    for rtype in all_types:
        result = client.call("engram_derive", {
            "claim": f"Test claim for {rtype}",
            "supporting_ids": f"{ids['ob1']},{ids['ob2']}",
            "logical_chain": f"Testing {rtype} reasoning",
            "reasoning_type": rtype,
        })
        assert result.get("status") == "created", f"{rtype} failed: {result}"
        assert result["reasoning_type"] == rtype, f"Expected {rtype}, got {result.get('reasoning_type')}"
        assert result["confidence"] > 0, f"{rtype} produced zero confidence"

    print(f"  PASS: All {len(all_types)} reasoning types accepted")


# ---------------------------------------------------------------------------
# Test: Invalid reasoning type rejected
# ---------------------------------------------------------------------------

def test_invalid_reasoning_type_rejected():
    """Invalid reasoning types should be rejected with helpful error."""
    client = fresh_client()
    ids = setup_base_graph(client)

    result = client.call("engram_derive", {
        "claim": "Bad type test",
        "supporting_ids": f"{ids['ob1']},{ids['ob2']}",
        "logical_chain": "Testing invalid type",
        "reasoning_type": "invalid_type_xyz",
    })
    assert "error" in result, f"Expected error for invalid type: {result}"
    assert "valid_types" in result, "Should return list of valid types"
    print("  PASS: Invalid reasoning type rejected with valid_types list")


# ---------------------------------------------------------------------------
# Test: Deductive confidence = min(premises) × 0.98
# ---------------------------------------------------------------------------

def test_deductive_confidence():
    """Deductive types use weakest-link with 0.98 discount."""
    client = fresh_client()
    ids = setup_base_graph(client)

    # ob1 is hard_data (0.95), ob2 is attributed_analysis (0.70)
    result = client.call("engram_derive", {
        "claim": "Deductive test: min(0.95, 0.70) × 0.98",
        "supporting_ids": f"{ids['ob1']},{ids['ob2']}",
        "logical_chain": "If gold fell AND rates caused it, then rates drive gold",
        "reasoning_type": "deductive_modus_ponens",
    })
    assert result.get("status") == "created"
    expected = round(0.70 * 0.98, 3)
    assert result["confidence"] == expected, f"Expected {expected}, got {result['confidence']}"
    assert result["reasoning_class"] == "deductive"
    print(f"  PASS: Deductive confidence = {expected} (min(0.95,0.70) × 0.98)")


# ---------------------------------------------------------------------------
# Test: Inductive corroboration = (1 - ∏(1-cᵢ)) × 0.95
# ---------------------------------------------------------------------------

def test_inductive_generalization_confidence():
    """Inductive generalization uses corroborative formula."""
    client = fresh_client()
    ids = setup_base_graph(client)

    # ob1 (0.95) and ob3 (0.95) from different sources
    result = client.call("engram_derive", {
        "claim": "Gold fell ~10% this week (confirmed by Reuters and FT)",
        "supporting_ids": f"{ids['ob1']},{ids['ob3']}",
        "logical_chain": "Reuters says 9.6%, FT says ~10% — independent confirmation",
        "reasoning_type": "inductive_generalization",
    })
    assert result.get("status") == "created"
    # (1 - (1-0.95)(1-0.95)) × 0.95 = (1 - 0.0025) × 0.95 = 0.9975 × 0.95
    expected = round((1 - (0.05 * 0.05)) * 0.95, 3)
    assert result["confidence"] == expected, f"Expected {expected}, got {result['confidence']}"
    assert result["reasoning_class"] == "inductive_corroboration"
    print(f"  PASS: Inductive generalization = {expected}")


# ---------------------------------------------------------------------------
# Test: Inductive analogy = min × 0.70 (weakest legitimate form)
# ---------------------------------------------------------------------------

def test_inductive_analogy_confidence():
    """Inductive analogy has the lowest discount (0.70)."""
    client = fresh_client()
    ids = setup_base_graph(client)

    result = client.call("engram_derive", {
        "claim": "Similar to 2013 taper tantrum pattern",
        "supporting_ids": f"{ids['ob1']},{ids['ob2']}",
        "logical_chain": "2013 analogy: rates rose, gold fell similarly",
        "reasoning_type": "inductive_analogy",
    })
    assert result.get("status") == "created"
    expected = round(0.70 * 0.70, 3)  # min(0.95, 0.70) × 0.70
    assert result["confidence"] == expected, f"Expected {expected}, got {result['confidence']}"
    print(f"  PASS: Inductive analogy = {expected} (weakest form)")


# ---------------------------------------------------------------------------
# Test: Abductive best_explanation capped at 0.80
# ---------------------------------------------------------------------------

def test_abductive_cap():
    """Abductive best_explanation is capped at 0.80 even with strong premises."""
    client = fresh_client()
    ids = setup_base_graph(client)

    # Both ob1 and ob3 are hard_data (0.95)
    result = client.call("engram_derive", {
        "claim": "Best explanation: macro regime shift",
        "supporting_ids": f"{ids['ob1']},{ids['ob3']}",
        "logical_chain": "The best explanation for both data points is a regime shift",
        "reasoning_type": "abductive_best_explanation",
    })
    assert result.get("status") == "created"
    # min(0.95, 0.95) × 0.80 = 0.76, but cap is 0.80, so min(0.76, 0.80) = 0.76
    # Actually: min(0.95 * 0.80, 0.80) = min(0.76, 0.80) = 0.76
    expected = round(min(0.95 * 0.80, 0.80), 3)
    assert result["confidence"] == expected, f"Expected {expected}, got {result['confidence']}"
    print(f"  PASS: Abductive best_explanation = {expected} (capped at 0.80)")


def test_abductive_elimination_cap():
    """Abductive elimination capped at 0.90."""
    client = fresh_client()
    ids = setup_base_graph(client)

    result = client.call("engram_derive", {
        "claim": "By elimination: must be rate-driven",
        "supporting_ids": f"{ids['ob1']},{ids['ob3']}",
        "logical_chain": "Ruled out supply shock and geopolitics, leaving rates",
        "reasoning_type": "abductive_elimination",
    })
    assert result.get("status") == "created"
    # min(0.95 * 0.88, 0.90) = min(0.836, 0.90) = 0.836
    expected = round(min(0.95 * 0.88, 0.90), 3)
    assert result["confidence"] == expected, f"Expected {expected}, got {result['confidence']}"
    print(f"  PASS: Abductive elimination = {expected} (capped at 0.90)")


# ---------------------------------------------------------------------------
# Test: Authority consensus (corroborative)
# ---------------------------------------------------------------------------

def test_authority_consensus_confidence():
    """Authority consensus uses corroborative formula like generalization."""
    client = fresh_client()
    ids = setup_base_graph(client)

    result = client.call("engram_derive", {
        "claim": "Expert consensus: gold decline is rate-driven",
        "supporting_ids": f"{ids['ob1']},{ids['ob3']}",
        "logical_chain": "Multiple independent sources agree",
        "reasoning_type": "authority_consensus",
    })
    assert result.get("status") == "created"
    expected = round((1 - (0.05 * 0.05)) * 0.98, 3)
    assert result["confidence"] == expected, f"Expected {expected}, got {result['confidence']}"
    assert result["reasoning_class"] == "inductive_corroboration"
    print(f"  PASS: Authority consensus = {expected} (corroborative)")


# ---------------------------------------------------------------------------
# Test: Legacy derivation_mode fallback
# ---------------------------------------------------------------------------

def test_legacy_chain_fallback():
    """Legacy chain mode maps to deductive_hypothetical_syllogism."""
    client = fresh_client()
    ids = setup_base_graph(client)

    result = client.call("engram_derive", {
        "claim": "Legacy chain test",
        "supporting_ids": f"{ids['ob1']},{ids['ob2']}",
        "logical_chain": "Testing legacy fallback",
        "derivation_mode": "chain",
    })
    assert result.get("status") == "created"
    assert result["reasoning_type"] == "deductive_hypothetical_syllogism"
    expected = round(0.70 * 0.98, 3)
    assert result["confidence"] == expected, f"Expected {expected}, got {result['confidence']}"
    print(f"  PASS: Legacy chain → deductive_hypothetical_syllogism ({expected})")


def test_legacy_corroboration_fallback():
    """Legacy corroboration mode maps to inductive_generalization."""
    client = fresh_client()
    ids = setup_base_graph(client)

    result = client.call("engram_derive", {
        "claim": "Legacy corroboration test",
        "supporting_ids": f"{ids['ob1']},{ids['ob3']}",
        "logical_chain": "Testing legacy fallback",
        "derivation_mode": "corroboration",
    })
    assert result.get("status") == "created"
    assert result["reasoning_type"] == "inductive_generalization"
    print(f"  PASS: Legacy corroboration → inductive_generalization")


# ---------------------------------------------------------------------------
# Test: Structural validation — premise count
# ---------------------------------------------------------------------------

def test_structural_warning_too_few_premises():
    """Modus ponens with 1 premise should produce a warning."""
    client = fresh_client()
    ids = setup_base_graph(client)

    result = client.call("engram_derive", {
        "claim": "Under-premised deduction",
        "supporting_ids": ids["ob1"],
        "logical_chain": "Only one premise",
        "reasoning_type": "deductive_modus_ponens",
    })
    assert result.get("status") == "created"  # Warnings don't block creation
    assert "structure_warnings" in result, "Expected structural warning"
    assert any("at least 2" in w for w in result["structure_warnings"])
    print(f"  PASS: Warning for too few premises (modus_ponens with 1)")


def test_structural_warning_too_many_premises():
    """Modus ponens with 3 premises should produce a warning."""
    client = fresh_client()
    ids = setup_base_graph(client)

    result = client.call("engram_derive", {
        "claim": "Over-premised deduction",
        "supporting_ids": f"{ids['ob1']},{ids['ob2']},{ids['ob3']}",
        "logical_chain": "Three premises for modus ponens",
        "reasoning_type": "deductive_modus_ponens",
    })
    assert result.get("status") == "created"
    assert "structure_warnings" in result, "Expected structural warning"
    assert any("at most 2" in w for w in result["structure_warnings"])
    print(f"  PASS: Warning for too many premises (modus_ponens with 3)")


# ---------------------------------------------------------------------------
# Test: Structural validation — multi-source
# ---------------------------------------------------------------------------

def test_structural_warning_single_source_generalization():
    """Inductive generalization with all premises from same source should warn."""
    client = fresh_client()
    ids = setup_base_graph(client)

    # ob1 and ob2 both come from ev1 (reuters)
    result = client.call("engram_derive", {
        "claim": "Generalization from single source",
        "supporting_ids": f"{ids['ob1']},{ids['ob2']}",
        "logical_chain": "Both from same Reuters article",
        "reasoning_type": "inductive_generalization",
    })
    assert result.get("status") == "created"
    assert "structure_warnings" in result, "Expected multi-source warning"
    assert any("independent sources" in w for w in result["structure_warnings"])
    print(f"  PASS: Warning for single-source generalization")


def test_no_warning_multi_source_generalization():
    """Inductive generalization with premises from different sources: no
    SOURCE-INDEPENDENCE structural warning.

    (Updated for the #49 standpoint-v3 F-S un-nest: the FALSIFICATION advisory
    is now decoupled from standpoint, so any 2+-premise derivation with a known
    fs_class emits an informational FALSIFICATION ratio line — here "2/2
    re-executable" for the two hard_data premises, which is benign/good F-S.
    The test's real intent is that the multi_source check does NOT flag these as
    same-source — so assert the absence of the source-independence warning, not
    the absence of all warnings.)"""
    client = fresh_client()
    ids = setup_base_graph(client)

    # ob1 (ev1/reuters) and ob3 (ev2/ft) — independent sources
    result = client.call("engram_derive", {
        "claim": "Generalization from independent sources",
        "supporting_ids": f"{ids['ob1']},{ids['ob3']}",
        "logical_chain": "Reuters and FT independently confirm",
        "reasoning_type": "inductive_generalization",
    })
    assert result.get("status") == "created"
    warns = result.get("structure_warnings", [])
    assert not any(
        ("independent sources" in w) or ("same evidence node" in w) for w in warns
    ), f"multi-source generalization must not trip the source-independence warning: {warns}"
    # Any other line present must be a benign advisory: the standpoint-v3
    # informational lines, or the #933 register-disclosure lines (the
    # CHECKED/NOT-CHECKED pair is always present on multi-premise derivations).
    _benign = ("FALSIFICATION:", "STANDPOINT:", "CHECKED (mechanical):",
               "NOT CHECKED (agent judgment):")
    assert all(w.startswith(_benign) for w in warns), (
        f"only advisory/register lines expected, got: {warns}"
    )
    print(f"  PASS: no source-independence warning for multi-source generalization (F-S advisory benign)")


# ---------------------------------------------------------------------------
# Test: Register disclosure (issue #933) — mechanical vs agent-judgment
# ---------------------------------------------------------------------------

def test_register_disclosure_multi_premise():
    """#933: every multi-premise derivation discloses BOTH registers — the
    mechanical check that ran (CHECKED) and the role axis the substrate
    structurally cannot check (NOT CHECKED, a question, never pass/fail)."""
    client = fresh_client()
    ids = setup_base_graph(client)

    result = client.call("engram_derive", {
        "claim": "Register disclosure probe — multi-source generalization",
        "supporting_ids": f"{ids['ob1']},{ids['ob3']}",
        "logical_chain": "Two independent sources; probing #933 output contract",
        "reasoning_type": "inductive_generalization",
    })
    assert result.get("status") == "created"
    warns = result.get("structure_warnings", [])
    assert any(w.startswith("CHECKED (mechanical):") for w in warns), (
        f"#933: multi_source derivation must disclose the mechanical cluster "
        f"check ran, got: {warns}"
    )
    assert any(w.startswith("NOT CHECKED (agent judgment):") and "premise role" in w
               for w in warns), (
        f"#933: multi-premise derivation must surface the unchecked role axis "
        f"as a question, got: {warns}"
    )
    print("  PASS: #933 register disclosure present on multi-premise derivation")


def test_register_disclosure_role_prompt_without_multisource():
    """#933: the role-axis prompt fires on ANY 2+-premise derivation (here
    deductive modus ponens — not a multi_source type, so no CHECKED line for
    the cluster check), because the role axis is unchecked regardless of
    reasoning type."""
    client = fresh_client()
    ids = setup_base_graph(client)

    result = client.call("engram_derive", {
        "claim": "Role prompt probe — deductive with two premises",
        "supporting_ids": f"{ids['ob1']},{ids['ob3']}",
        "logical_chain": "If P then Q; P; therefore Q — probing #933",
        "reasoning_type": "deductive_modus_ponens",
    })
    assert result.get("status") == "created"
    warns = result.get("structure_warnings", [])
    assert any(w.startswith("NOT CHECKED (agent judgment):") for w in warns), (
        f"#933: role prompt must be present on any multi-premise derivation, "
        f"got: {warns}"
    )
    assert not any(w.startswith("CHECKED (mechanical):") for w in warns), (
        f"#933: non-multi_source type must not claim the cluster check ran, "
        f"got: {warns}"
    )
    print("  PASS: #933 role prompt on non-multi_source derivation, no false CHECKED claim")


def test_register_disclosure_absent_single_premise():
    """#933: single-premise derivations get NEITHER register line — the role
    question is only meaningful when there is more than one premise to weigh."""
    client = fresh_client()
    ids = setup_base_graph(client)

    result = client.call("engram_derive", {
        "claim": "Single premise probe — no register lines",
        "supporting_ids": f"{ids['ob1']}",
        "logical_chain": "Restating one source's claim — probing #933",
        "reasoning_type": "authority_expert",
    })
    assert result.get("status") == "created"
    warns = result.get("structure_warnings", [])
    assert not any(w.startswith(("CHECKED (mechanical):",
                                 "NOT CHECKED (agent judgment):"))
                   for w in warns), (
        f"#933: single-premise derivation must carry no register lines, "
        f"got: {warns}"
    )
    print("  PASS: #933 no register lines on single-premise derivation")


def test_register_disclosure_absent_single_premise_multisource_type():
    """#933 (reviewer-fairy round 1): a multi_source TYPE at n=1 exercises the
    `spec.get(\"multi_source\") and n >= 2` guard's n=1 branch directly — the
    cluster check does not run, so the CHECKED line must not claim it did,
    and the role prompt stays absent (nothing to weigh against)."""
    client = fresh_client()
    ids = setup_base_graph(client)

    result = client.call("engram_derive", {
        "claim": "Single premise on a multi_source type — no register lines",
        "supporting_ids": f"{ids['ob1']}",
        "logical_chain": "Generalizing from one case — probing #933 n=1 branch",
        "reasoning_type": "inductive_generalization",
    })
    assert result.get("status") == "created"
    warns = result.get("structure_warnings", [])
    assert not any(w.startswith(("CHECKED (mechanical):",
                                 "NOT CHECKED (agent judgment):"))
                   for w in warns), (
        f"#933: multi_source type at n=1 must carry no register lines, "
        f"got: {warns}"
    )
    # The premise-count warning (min 2) should still fire — unchanged behavior.
    assert any("at least 2" in w for w in warns), (
        f"#933: the pre-existing min-premise warning must be unaffected, "
        f"got: {warns}"
    )
    print("  PASS: #933 no register lines on n=1 multi_source type; count warning intact")


# ---------------------------------------------------------------------------
# Test: engram_resolve with reasoning_type
# ---------------------------------------------------------------------------

def test_resolve_with_reasoning_type():
    """Two-step workflow (issue #229): derivation carries the reasoning_type;
    engram_resolve wires it without recomputing confidence."""
    client = fresh_client()
    ids = setup_base_graph(client)

    # Create a question first
    q = client.call("engram_ask", {"question": "Why did gold fall?"})
    assert q.get("status") == "created"
    q_id = q["question_id"]

    # Step 1: compose resolving derivation with reasoning_type
    dv = client.call("engram_derive", {
        "claim": "Gold fell because of rising real rates",
        "supporting_ids": f"{ids['ob1']},{ids['ob2']}",
        "logical_chain": "Rate data + analyst attribution → causal explanation",
        "reasoning_type": "inductive_causal",
    })
    assert dv.get("status") == "created", f"Failed: {dv}"
    # min(0.95, 0.70) × 0.85 = 0.595
    expected = round(0.70 * 0.85, 3)
    assert dv["confidence"] == expected, f"Expected {expected}, got {dv['confidence']}"

    # Step 2: wire it
    result = client.call("engram_resolve", {
        "target_id": q_id,
        "resolving_node_id": dv["derivation_id"],
    })
    assert result.get("status") == "resolution_created", f"Failed: {result}"
    assert result["confidence"] == expected, f"Expected {expected}, got {result['confidence']}"
    print(f"  PASS: engram_resolve with reasoning_type = {expected}")


# ---------------------------------------------------------------------------
# Test: engram_stats reasoning type breakdown
# ---------------------------------------------------------------------------

def test_stats_reasoning_breakdown():
    """engram_stats should include reasoning_type_breakdown."""
    client = fresh_client()
    ids = setup_base_graph(client)

    # Create derivations with different types
    client.call("engram_derive", {
        "claim": "Causal derivation",
        "supporting_ids": f"{ids['ob1']},{ids['ob2']}",
        "logical_chain": "Causal test",
        "reasoning_type": "inductive_causal",
    })
    client.call("engram_derive", {
        "claim": "Deductive derivation",
        "supporting_ids": f"{ids['ob1']},{ids['ob2']}",
        "logical_chain": "Deductive test",
        "reasoning_type": "deductive_modus_ponens",
    })
    client.call("engram_derive", {
        "claim": "Another causal derivation",
        "supporting_ids": f"{ids['ob3']},{ids['ob4']}",
        "logical_chain": "Causal test 2",
        "reasoning_type": "inductive_causal",
    })

    stats = client.call("engram_stats")
    assert "reasoning_type_breakdown" in stats, f"Missing reasoning breakdown: {stats}"
    breakdown = stats["reasoning_type_breakdown"]
    assert breakdown.get("inductive_causal") == 2, f"Expected 2 causal, got {breakdown}"
    assert breakdown.get("deductive_modus_ponens") == 1, f"Expected 1 deductive, got {breakdown}"
    print(f"  PASS: engram_stats reasoning breakdown: {breakdown}")


# ---------------------------------------------------------------------------
# Test: Confidence ordering across types
# ---------------------------------------------------------------------------

def test_confidence_ordering():
    """Verify that reasoning types produce correctly ordered confidence values."""
    client = fresh_client()
    ids = setup_base_graph(client)

    # All using same premises (ob1=0.95, ob2=0.70), so we can compare types
    types_and_expected_order = [
        ("deductive_modus_ponens", "deductive"),     # 0.70 × 0.98 = 0.686
        ("inductive_statistical", "inductive_chain"), # 0.70 × 0.90 = 0.630
        ("inductive_causal", "inductive_chain"),      # 0.70 × 0.85 = 0.595
        ("abductive_best_explanation", "abductive"),   # min(0.70 × 0.80, 0.80) = 0.560
        ("inductive_analogy", "inductive_chain"),      # 0.70 × 0.70 = 0.490
    ]

    prev_conf = 1.0
    for rtype, rclass in types_and_expected_order:
        result = client.call("engram_derive", {
            "claim": f"Ordering test: {rtype}",
            "supporting_ids": f"{ids['ob1']},{ids['ob2']}",
            "logical_chain": f"Ordering test for {rtype}",
            "reasoning_type": rtype,
        })
        assert result.get("status") == "created", f"{rtype} failed: {result}"
        assert result["confidence"] <= prev_conf, (
            f"Ordering violation: {rtype} ({result['confidence']}) > previous ({prev_conf})"
        )
        prev_conf = result["confidence"]

    print(f"  PASS: Confidence ordering correct (deductive > statistical > causal > abductive > analogy)")


# ---------------------------------------------------------------------------
# Run all tests
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    tests = [
        test_all_reasoning_types_accepted,
        test_invalid_reasoning_type_rejected,
        test_deductive_confidence,
        test_inductive_generalization_confidence,
        test_inductive_analogy_confidence,
        test_abductive_cap,
        test_abductive_elimination_cap,
        test_authority_consensus_confidence,
        test_legacy_chain_fallback,
        test_legacy_corroboration_fallback,
        test_structural_warning_too_few_premises,
        test_structural_warning_too_many_premises,
        test_structural_warning_single_source_generalization,
        test_no_warning_multi_source_generalization,
        test_resolve_with_reasoning_type,
        test_stats_reasoning_breakdown,
        test_confidence_ordering,
    ]

    print(f"\nRunning {len(tests)} reasoning type tests...\n")
    passed = 0
    failed = 0
    for test in tests:
        try:
            test()
            passed += 1
        except Exception as e:
            print(f"  FAIL: {test.__name__}: {e}")
            failed += 1

    # Cleanup
    if os.path.exists(TEST_DIR):
        shutil.rmtree(TEST_DIR)

    print(f"\n{'='*50}")
    print(f"Results: {passed} passed, {failed} failed out of {len(tests)}")
    if failed:
        sys.exit(1)
    else:
        print("All tests passed!")
