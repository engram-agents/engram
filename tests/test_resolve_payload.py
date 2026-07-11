"""Tests for engram_resolve pure-wire signature (issue #229, 2026-05-20).

The 2026-05-20 redesign drops the combo create-and-wire engram_resolve in
favor of a pure-relational tool: caller composes the resolving derivation
via engram_derive first, then calls engram_resolve(target, resolving_node)
to wire the resolves edge + flip status from the resolving node's confidence.

Parallels how engram_supersede is pure-relational (per ob_1260).

Coverage:
  1. Happy path — two-step workflow creates resolution via derivation_id.
  2. Schema enforcement — invalid JSON, non-object payload, unknown fields.
  3. Required-field validation — missing target_id / resolving_node_id.
  4. Type guards — non-claim-bearing resolving_node rejected; non-current rejected.
  5. Idempotency — duplicate (target, resolving_node, resolves) edge returns no_op.
  6. Max-of-resolves — weaker later resolver cannot downgrade target status.
  7. ct_0006-shape regression — high-conf existing derivation resolves contradiction
     in a single call without chain-dilution.
  8. Internal helper — _resolve_impl works with named kwargs.
"""

import atexit
import json
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


TEST_DIR = tempfile.mkdtemp(prefix="engram_test_payload_resolve_")
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


def _seed_strong_resolution(server):
    """Create observation, question, and a strong resolving derivation.

    Returns (question_id, observation_id, derivation_id).
    The derivation confidence is well above the 0.7 threshold:
    hard_data observation (0.95) × authority_expert discount (0.95) = 0.9025.
    """
    obs_payload = {
        "url": "https://example.com/a",
        "title": "A",
        "quoted_text": "X says Y is the case.",
        "interpretation": "X claims Y holds.",
        "claim": "Y is the case according to X.",
        "quote_type": "hard_data",
    }
    ob = json.loads(server.engram_add_observation(payload_json=json.dumps(obs_payload)))
    ob_id = ob["observation_id"]

    q = json.loads(server.engram_ask(payload_json=json.dumps({"question": "Is Y the case?"})))
    q_id = q["question_id"]

    dv = json.loads(server.engram_derive(payload_json=json.dumps({
        "claim": "Yes, Y is the case based on X's account.",
        "supporting_ids": ob_id,
        "logical_chain": "X attests Y → therefore Y holds.",
        "reasoning_type": "authority_expert",
    })))
    return q_id, ob_id, dv["derivation_id"]


# ── 1. Happy path ──────────────────────────────────────────────────────────


def test_two_step_workflow_resolves_question():
    """Caller composes derivation via engram_derive, then wires via engram_resolve."""
    server = fresh_server()
    q_id, _, dv_id = _seed_strong_resolution(server)
    r = json.loads(server.engram_resolve(payload_json=json.dumps({
        "target_id": q_id,
        "resolving_node_id": dv_id,
    })))
    assert r.get("status") == "resolution_created", r
    assert r["target_id"] == q_id
    assert r["resolving_node_id"] == dv_id
    assert r["resolving_node_type"] == "derivation"
    assert "confidence" in r
    assert r["resolution_status"] == "resolved", r


def test_resolve_flips_target_status_to_resolved():
    server = fresh_server()
    q_id, _, dv_id = _seed_strong_resolution(server)
    json.loads(server.engram_resolve(payload_json=json.dumps({
        "target_id": q_id,
        "resolving_node_id": dv_id,
    })))
    q_inspect = json.loads(server.engram_inspect(payload_json=json.dumps({"node_id": q_id, "dream_mode": True})))
    assert q_inspect["node"]["status"] == "resolved", q_inspect["node"]


def test_resolve_writes_resolved_by_column():
    """engram_resolve must backfill the resolved_by column (issue #758).

    Previously only the status column was updated; resolved_by was always NULL.
    """
    import sqlite3
    server = fresh_server()
    q_id, _, dv_id = _seed_strong_resolution(server)
    json.loads(server.engram_resolve(payload_json=json.dumps({
        "target_id": q_id,
        "resolving_node_id": dv_id,
    })))
    conn = sqlite3.connect(str(server.core.DB_PATH))
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(
            "SELECT resolved_by FROM nodes WHERE id = ?", (q_id,)
        ).fetchone()
        assert row is not None, f"Node {q_id} not found in DB"
        assert row["resolved_by"] == dv_id, (
            f"resolved_by column not written: expected {dv_id!r}, got {row['resolved_by']!r}"
        )
    finally:
        conn.close()


def test_resolved_by_points_at_strongest_not_latest():
    """resolved_by must track the argmax-confidence resolver, not the latest-wired one.

    Wire the strong resolver first, then a weak one below threshold. resolved_by
    must remain pointed at the strong resolver — latest-wired semantics would
    leave it pointing at the weak one, which didn't determine the status.
    """
    import sqlite3
    server = fresh_server()
    q_id, _, strong_dv_id = _seed_strong_resolution(server)

    json.loads(server.engram_resolve(payload_json=json.dumps({
        "target_id": q_id,
        "resolving_node_id": strong_dv_id,
    })))

    weak_ob = json.loads(server.engram_add_observation(payload_json=json.dumps({
        "url": "https://example.com/weak",
        "title": "Weak",
        "quoted_text": "Weak speculation about Y.",
        "interpretation": "Weakly attributed.",
        "claim": "Y is weakly supported.",
        "quote_type": "attributed_analysis",
    })))
    weak_dv = json.loads(server.engram_derive(payload_json=json.dumps({
        "claim": "Weak argument for Y.",
        "supporting_ids": weak_ob["observation_id"],
        "logical_chain": "Loose analogy.",
        "reasoning_type": "inductive_statistical",
    })))
    json.loads(server.engram_resolve(payload_json=json.dumps({
        "target_id": q_id,
        "resolving_node_id": weak_dv["derivation_id"],
    })))

    conn = sqlite3.connect(str(server.core.DB_PATH))
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(
            "SELECT resolved_by FROM nodes WHERE id = ?", (q_id,)
        ).fetchone()
        assert row is not None
        assert row["resolved_by"] == strong_dv_id, (
            f"resolved_by should point at strongest resolver {strong_dv_id!r}, "
            f"not the latest-wired weak one. Got {row['resolved_by']!r}"
        )
    finally:
        conn.close()


# ── 2. Schema enforcement ──────────────────────────────────────────────────


def test_invalid_json_rejected():
    server = fresh_server()
    r = json.loads(server.engram_resolve(payload_json="{not-json}"))
    assert "error" in r and "Invalid JSON" in r["error"], r


def test_non_object_payload_rejected():
    server = fresh_server()
    r = json.loads(server.engram_resolve(payload_json="[1, 2, 3]"))
    assert "error" in r and "must be a JSON object" in r["error"], r


def test_unknown_fields_rejected():
    server = fresh_server()
    q_id, _, dv_id = _seed_strong_resolution(server)
    r = json.loads(server.engram_resolve(payload_json=json.dumps({
        "target_id": q_id,
        "resolving_node_id": dv_id,
        "bogus_field": "should reject",
    })))
    assert "error" in r and "Unknown fields" in r["error"], r
    assert "bogus_field" in r["error"]


def test_legacy_fields_rejected():
    """Old signature fields (claim, supporting_ids, logical_chain) must be rejected."""
    server = fresh_server()
    q_id, ob_id, _ = _seed_strong_resolution(server)
    r = json.loads(server.engram_resolve(payload_json=json.dumps({
        "target_id": q_id,
        "claim": "legacy field",
        "supporting_ids": ob_id,
        "logical_chain": "legacy",
    })))
    assert "error" in r and "Unknown fields" in r["error"], r


# ── 3. Required-field validation ──────────────────────────────────────────


def test_missing_target_id_rejected():
    server = fresh_server()
    _, _, dv_id = _seed_strong_resolution(server)
    r = json.loads(server.engram_resolve(payload_json=json.dumps({
        "target_id": "",
        "resolving_node_id": dv_id,
    })))
    assert "error" in r and "target_id" in r["error"], r


def test_missing_resolving_node_id_rejected():
    server = fresh_server()
    q_id, _, _ = _seed_strong_resolution(server)
    r = json.loads(server.engram_resolve(payload_json=json.dumps({
        "target_id": q_id,
        "resolving_node_id": "",
    })))
    assert "error" in r and "resolving_node_id" in r["error"], r


def test_target_and_resolver_same_rejected():
    server = fresh_server()
    q_id, _, _ = _seed_strong_resolution(server)
    r = json.loads(server.engram_resolve(payload_json=json.dumps({
        "target_id": q_id,
        "resolving_node_id": q_id,
    })))
    assert "error" in r and "must differ" in r["error"], r


# ── 4. Type guards ────────────────────────────────────────────────────────


def test_non_claim_bearing_resolver_rejected():
    """Question (non-claim-bearing) cannot be a resolving node."""
    server = fresh_server()
    q_id, _, _ = _seed_strong_resolution(server)
    q2 = json.loads(server.engram_ask(payload_json=json.dumps({"question": "Another question?"})))
    r = json.loads(server.engram_resolve(payload_json=json.dumps({
        "target_id": q_id,
        "resolving_node_id": q2["question_id"],
    })))
    assert "error" in r and "claim-bearing" in r["error"], r
    assert "engram_derive" in r["error"], "Error message should point caller to engram_derive"


def test_non_resolvable_target_rejected():
    """Cannot resolve an observation (only question/contradiction/prediction/conjecture/goal_tension)."""
    server = fresh_server()
    _, ob_id, dv_id = _seed_strong_resolution(server)
    r = json.loads(server.engram_resolve(payload_json=json.dumps({
        "target_id": ob_id,
        "resolving_node_id": dv_id,
    })))
    assert "error" in r and "Protocol violation" in r["error"], r


def test_target_not_found_rejected():
    server = fresh_server()
    _, _, dv_id = _seed_strong_resolution(server)
    r = json.loads(server.engram_resolve(payload_json=json.dumps({
        "target_id": "qu_9999",
        "resolving_node_id": dv_id,
    })))
    assert "error" in r and "not found" in r["error"], r


def test_resolver_not_found_rejected():
    server = fresh_server()
    q_id, _, _ = _seed_strong_resolution(server)
    r = json.loads(server.engram_resolve(payload_json=json.dumps({
        "target_id": q_id,
        "resolving_node_id": "dv_9999",
    })))
    assert "error" in r and "not found" in r["error"], r


# ── 5. Idempotency ────────────────────────────────────────────────────────


def test_duplicate_resolve_returns_no_op():
    server = fresh_server()
    q_id, _, dv_id = _seed_strong_resolution(server)
    r1 = json.loads(server.engram_resolve(payload_json=json.dumps({
        "target_id": q_id,
        "resolving_node_id": dv_id,
    })))
    assert r1.get("status") == "resolution_created"
    r2 = json.loads(server.engram_resolve(payload_json=json.dumps({
        "target_id": q_id,
        "resolving_node_id": dv_id,
    })))
    assert r2.get("status") == "no_op", r2
    assert "already exists" in r2["message"]


# ── 6. Max-of-resolves: regression prevention ─────────────────────────────


def test_weaker_later_resolver_cannot_downgrade_status():
    """A low-confidence later resolver MUST NOT flip resolved → partially_resolved.

    Setup:
      - Strong resolve: hard_data (0.95) × authority_expert (0.95) = 0.9025 → resolved
      - Weak resolve: attributed_analysis (0.70) × inductive_statistical (0.90) = 0.63 → below threshold
    After both wired, status MUST stay 'resolved' because max-of-resolves picks 0.9025.
    """
    server = fresh_server()
    q_id, _, strong_dv_id = _seed_strong_resolution(server)

    json.loads(server.engram_resolve(payload_json=json.dumps({
        "target_id": q_id,
        "resolving_node_id": strong_dv_id,
    })))

    # Build a weak observation + derivation (clearly below threshold)
    weak_ob = json.loads(server.engram_add_observation(payload_json=json.dumps({
        "url": "https://example.com/weak",
        "title": "Weak source",
        "quoted_text": "An analyst speculates Y is loosely plausible.",
        "interpretation": "Weakly attributed speculation about Y.",
        "claim": "Y is loosely plausible (speculative).",
        "quote_type": "attributed_analysis",
    })))
    weak_dv = json.loads(server.engram_derive(payload_json=json.dumps({
        "claim": "Weak analogical argument for Y.",
        "supporting_ids": weak_ob["observation_id"],
        "logical_chain": "Loose analogy between Y and similar cases.",
        "reasoning_type": "inductive_statistical",
    })))

    r = json.loads(server.engram_resolve(payload_json=json.dumps({
        "target_id": q_id,
        "resolving_node_id": weak_dv["derivation_id"],
    })))
    assert r.get("status") == "resolution_created", r
    assert r["resolution_status"] == "resolved", (
        f"max-of-resolves must protect from regression: weak resolver should not "
        f"downgrade status. Got {r}"
    )

    # And target status field stays resolved
    q_inspect = json.loads(server.engram_inspect(payload_json=json.dumps({"node_id": q_id, "dream_mode": True})))
    assert q_inspect["node"]["status"] == "resolved", q_inspect["node"]


# ── 7. ct_0006-shape regression test ──────────────────────────────────────


def test_existing_canonical_node_resolves_contradiction_in_one_call():
    """The ct_0006 saga: an existing high-conf derivation can resolve a
    contradiction in a single call without chain-dilution.
    """
    server = fresh_server()

    ob_a = json.loads(server.engram_add_observation(payload_json=json.dumps({
        "url": "https://example.com/source-a",
        "title": "Source A",
        "quoted_text": "Author claims P is true.",
        "interpretation": "Source A holds P.",
        "claim": "P is the case (per source A).",
        "quote_type": "hard_data",
    })))["observation_id"]
    ob_b = json.loads(server.engram_add_observation(payload_json=json.dumps({
        "url": "https://example.com/source-b",
        "title": "Source B",
        "quoted_text": "Author claims P is false.",
        "interpretation": "Source B holds not P.",
        "claim": "P is NOT the case (per source B).",
        "quote_type": "hard_data",
    })))["observation_id"]

    ct = json.loads(server.engram_contradict(payload_json=json.dumps({
        "node_id_a": ob_a,
        "node_id_b": ob_b,
        "description": "Source A says P, source B says not P.",
    })))
    ct_id = ct["contradiction_id"]

    synth_dv = json.loads(server.engram_derive(payload_json=json.dumps({
        "claim": "Source B's stricter reading wins; P is not the case under the proper test.",
        "supporting_ids": ob_b,
        "logical_chain": "Source B applies the operational criterion correctly.",
        "reasoning_type": "authority_expert",
        # ob_b sits on the ct_id contradiction filed above, still open at
        # this point — composing the resolving derivation itself is exactly
        # the #1654 resolution-pressure-valve use case for use_contested.
        "use_contested": True,
    })))
    assert synth_dv["confidence"] >= 0.7, (
        f"Test sanity: synth_dv needs conf >= 0.7 to test the regression; "
        f"got {synth_dv['confidence']}"
    )

    r = json.loads(server.engram_resolve(payload_json=json.dumps({
        "target_id": ct_id,
        "resolving_node_id": synth_dv["derivation_id"],
    })))
    assert r.get("status") == "resolution_created", r
    assert r["resolution_status"] == "resolved", r
    assert r["confidence"] == synth_dv["confidence"], (
        f"Confidence must be taken from resolving node directly (no recomputation): "
        f"expected {synth_dv['confidence']}, got {r['confidence']}"
    )


# ── 8. Internal helper ────────────────────────────────────────────────────


def test_impl_callable_with_kwargs():
    server = fresh_server()
    q_id, _, dv_id = _seed_strong_resolution(server)
    r = json.loads(server._resolve_impl(
        target_id=q_id,
        resolving_node_id=dv_id,
    ))
    assert r.get("status") == "resolution_created", r


# ── 9. discipline_hint on threshold-gate partial resolution (issue #386) ──


def _seed_weak_resolution(server):
    """Create observation, question, and a below-threshold resolving derivation.

    Returns (question_id, derivation_id).
    The derivation confidence is well below the 0.7 threshold:
    attributed_analysis observation (0.70) × inductive_analogy discount (0.70)
    = 0.49 — below the 0.70 resolution threshold.
    """
    obs_payload = {
        "url": "https://example.com/weak-source",
        "title": "Weak Source",
        "quoted_text": "An analyst speculates Z might be loosely plausible.",
        "interpretation": "Weakly attributed speculation about Z.",
        "claim": "Z is loosely plausible (speculative).",
        "quote_type": "attributed_analysis",
    }
    ob = json.loads(server.engram_add_observation(payload_json=json.dumps(obs_payload)))
    ob_id = ob["observation_id"]

    q = json.loads(server.engram_ask(payload_json=json.dumps({"question": "Is Z the case?"})))
    q_id = q["question_id"]

    dv = json.loads(server.engram_derive(payload_json=json.dumps({
        "claim": "Z might hold based on loose analogy.",
        "supporting_ids": ob_id,
        "logical_chain": "Loose analogy between Z and similar weak cases.",
        "reasoning_type": "inductive_statistical",
    })))
    return q_id, dv["derivation_id"]


def test_threshold_gate_partial_resolution_includes_discipline_hint():
    """When threshold gate fires (no resolver meets 0.70), discipline_hint must be present."""
    server = fresh_server()
    q_id, weak_dv_id = _seed_weak_resolution(server)

    r = json.loads(server.engram_resolve(payload_json=json.dumps({
        "target_id": q_id,
        "resolving_node_id": weak_dv_id,
    })))

    assert r.get("status") == "resolution_created", r
    assert r["resolution_status"] == "partially_resolved", (
        f"Expected partially_resolved for below-threshold resolver; got {r}"
    )
    assert "discipline_hint" in r, (
        f"discipline_hint must be present on threshold-gate partial_resolution; got {r}"
    )
    assert r["discipline_hint"] == server._THRESHOLD_GATE_DISCIPLINE_HINT, (
        f"discipline_hint must match the module-level constant; got {r['discipline_hint']!r}"
    )
    # Spot-check key phrases from the verbatim spec text
    assert "chain dilution" in r["discipline_hint"], r["discipline_hint"]
    assert "inspect-first before composing resolution" in r["discipline_hint"], r["discipline_hint"]


def test_strong_resolution_does_not_include_discipline_hint():
    """When resolution_status is 'resolved' (above threshold), discipline_hint must be absent."""
    server = fresh_server()
    q_id, _, strong_dv_id = _seed_strong_resolution(server)

    r = json.loads(server.engram_resolve(payload_json=json.dumps({
        "target_id": q_id,
        "resolving_node_id": strong_dv_id,
    })))

    assert r.get("status") == "resolution_created", r
    assert r["resolution_status"] == "resolved", (
        f"Expected resolved for above-threshold resolver; got {r}"
    )
    assert "discipline_hint" not in r, (
        f"discipline_hint must NOT be present on a full resolution; got {r}"
    )


def test_prediction_partial_resolution_does_not_include_discipline_hint():
    """Prediction path cannot produce discipline_hint — two safety layers:
    (1) VALID_PREDICTION_OUTCOMES excludes 'partially_resolved', so passing it
        is rejected before reaching the threshold gate;
    (2) valid outcomes (confirmed/partially_confirmed/refuted/partially_refuted)
        set resolution_status directly from prediction_outcome, which is never
        'partially_resolved', so the threshold-gate hint block is unreachable.
    This test confirms layer (1): passing 'partially_resolved' as prediction_outcome
    is an error, never a discipline_hint surface.
    """
    server = fresh_server()

    # Create a predictive observation to seed a prediction node
    obs = json.loads(server.engram_add_observation(payload_json=json.dumps({
        "url": "https://example.com/pred-test",
        "title": "Prediction test",
        "quoted_text": "Event P will occur by Q3.",
        "interpretation": "Prediction: event P will occur.",
        "claim": "Event P will occur by Q3.",
        "quote_type": "editorial",
        "is_predictive": True,
        "predicted_event": "Event P occurs by Q3",
    })))
    assert "prediction_id" in obs, obs
    pred_id = obs["prediction_id"]

    # Create a resolving derivation (weak, below threshold — makes no difference
    # for predictions since they bypass the threshold gate entirely)
    ob_id = obs["observation_id"]
    dv = json.loads(server.engram_derive(payload_json=json.dumps({
        "claim": "Event P did not occur by Q3 based on Q3 results.",
        "supporting_ids": ob_id,
        "logical_chain": "Q3 passed without P occurring.",
        "reasoning_type": "inductive_statistical",
    })))
    dv_id = dv["derivation_id"]

    # Attempt to pass 'partially_resolved' as prediction_outcome — must be rejected
    r = json.loads(server.engram_resolve(payload_json=json.dumps({
        "target_id": pred_id,
        "resolving_node_id": dv_id,
        "prediction_outcome": "partially_resolved",
    })))
    assert "error" in r, (
        f"'partially_resolved' is not a valid prediction_outcome and must be rejected; got {r}"
    )
    assert "discipline_hint" not in r, (
        f"discipline_hint must NOT appear on a prediction resolution error; got {r}"
    )

    # Also confirm a valid prediction outcome does not produce discipline_hint
    r2 = json.loads(server.engram_resolve(payload_json=json.dumps({
        "target_id": pred_id,
        "resolving_node_id": dv_id,
        "prediction_outcome": "refuted",
    })))
    assert r2.get("status") == "resolution_created", r2
    assert r2["resolution_status"] == "refuted", r2
    assert "discipline_hint" not in r2, (
        f"discipline_hint must NOT be present on a prediction resolution; got {r2}"
    )


def test_retract_last_resolver_clears_resolved_by():
    """Retracting the last resolver must reopen the target and clear resolved_by to NULL."""
    server = fresh_server()
    q_id, _, dv_id = _seed_strong_resolution(server)

    # Resolve the question.
    r = json.loads(server.engram_resolve(payload_json=json.dumps({
        "target_id": q_id,
        "resolving_node_id": dv_id,
    })))
    assert r.get("status") == "resolution_created", r

    # Verify resolved_by is populated.
    import sqlite3
    db_path = os.path.join(TEST_DIR, "knowledge.db")
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT status, resolved_by FROM nodes WHERE id = ?", (q_id,)).fetchone()
    assert row["status"] in ("resolved", "partially_resolved"), row["status"]
    assert row["resolved_by"] == dv_id, f"resolved_by should be {dv_id!r}, got {row['resolved_by']!r}"
    conn.close()

    # Retract the resolver.
    ret = json.loads(server.engram_retract(payload_json=json.dumps({
        "node_id": dv_id,
        "error_type": "other",
        "reason": "Retracting for test.",
    })))
    assert ret.get("status") in ("retracted", "retraction_recorded"), ret

    # The target should be reopened and resolved_by cleared.
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT status, resolved_by FROM nodes WHERE id = ?", (q_id,)).fetchone()
    assert row["status"] == "open", f"target should be reopened, got {row['status']!r}"
    assert row["resolved_by"] is None, f"resolved_by must be NULL after last resolver retracted, got {row['resolved_by']!r}"
    conn.close()


def test_retract_one_of_two_resolvers_updates_resolved_by():
    """Retracting one of two resolvers must update resolved_by to the remaining stronger one."""
    server = fresh_server()

    # Seed an observation and a question.
    ob = json.loads(server.engram_add_observation(payload_json=json.dumps({
        "url": "https://example.com/multi-resolver",
        "title": "Multi-resolver test",
        "quoted_text": "Evidence for multi-resolver scenario.",
        "interpretation": "This supports both resolvers.",
        "claim": "Multi-resolver evidence.",
        "quote_type": "official_statement",
    })))
    ob_id = ob["observation_id"]

    q = json.loads(server.engram_ask(payload_json=json.dumps({
        "question": "Is multi-resolver correctly handled?",
    })))
    q_id = q["question_id"]

    # Create a strong resolver (inductive_generalization → higher computed confidence).
    dv_strong = json.loads(server.engram_derive(payload_json=json.dumps({
        "claim": "Multi-resolver correctly handled — strong evidence.",
        "supporting_ids": ob_id,
        "logical_chain": "Strong evidence confirms it.",
        "reasoning_type": "inductive_generalization",
    })))
    assert "derivation_id" in dv_strong, dv_strong
    dv_strong_id = dv_strong["derivation_id"]

    # Create a weaker resolver (inductive_analogy → lower computed confidence).
    dv_weak = json.loads(server.engram_derive(payload_json=json.dumps({
        "claim": "Multi-resolver correctly handled — weak evidence.",
        "supporting_ids": ob_id,
        "logical_chain": "Weak evidence suggests it.",
        "reasoning_type": "inductive_analogy",
    })))
    assert "derivation_id" in dv_weak, dv_weak
    dv_weak_id = dv_weak["derivation_id"]

    # Wire weak FIRST so resolved_by = dv_weak_id, then wire strong SECOND
    # so resolved_by = dv_strong_id before retraction.  After retracting strong,
    # the else-branch must update resolved_by back to dv_weak_id.  If wired in
    # the opposite order, resolved_by is already dv_weak_id before retraction
    # and the else-branch is a no-op — the test passes even without the fix.
    r1 = json.loads(server.engram_resolve(payload_json=json.dumps({
        "target_id": q_id,
        "resolving_node_id": dv_weak_id,
    })))
    assert r1.get("status") == "resolution_created", r1

    r2 = json.loads(server.engram_resolve(payload_json=json.dumps({
        "target_id": q_id,
        "resolving_node_id": dv_strong_id,
    })))
    assert r2.get("status") in ("resolution_created", "no_op"), r2

    # Verify resolved_by is dv_strong_id before retraction (pre-condition).
    import sqlite3
    db_path = os.path.join(TEST_DIR, "knowledge.db")
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT resolved_by FROM nodes WHERE id = ?", (q_id,)).fetchone()
    assert row["resolved_by"] == dv_strong_id, (
        f"pre-retraction: resolved_by must be the most-recently-wired resolver "
        f"{dv_strong_id!r}, got {row['resolved_by']!r}"
    )
    conn.close()

    # Retract the strong resolver.
    ret = json.loads(server.engram_retract(payload_json=json.dumps({
        "node_id": dv_strong_id,
        "error_type": "other",
        "reason": "Retracting strong resolver for test.",
    })))
    assert ret.get("status") in ("retracted", "retraction_recorded"), ret

    # Target should downgrade to partially_resolved (weak resolver's confidence ~0.45
    # is below the 0.7 threshold), and resolved_by must point at the remaining weak resolver.
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT status, resolved_by FROM nodes WHERE id = ?", (q_id,)).fetchone()
    assert row["status"] == "partially_resolved", (
        f"target must downgrade to partially_resolved when strongest resolver is retracted "
        f"and only a below-threshold resolver remains; got {row['status']!r}"
    )
    assert row["resolved_by"] == dv_weak_id, (
        f"resolved_by must be updated to remaining resolver {dv_weak_id!r}, got {row['resolved_by']!r}"
    )
    conn.close()


# ── #919: target is_current guard ──────────────────────────────────────────


def test_resolve_superseded_target_rejected_names_successor():
    """#919: resolving a superseded target is rejected, and the error walks
    the supersede chain to name the current successor."""
    server = fresh_server()
    q_id, ob_id, dv_id = _seed_strong_resolution(server)

    cj1 = json.loads(server.engram_add_conjecture(payload_json=json.dumps({
        "claim": "Y might generalize beyond X's account.",
        "basis": "Single attestation so far.",
    })))
    cj1_id = cj1["conjecture_id"]
    cj2 = json.loads(server.engram_add_conjecture(payload_json=json.dumps({
        "claim": "Y might generalize beyond X's account, within domain D only.",
        "basis": "Scoped refinement of the earlier conjecture.",
    })))
    cj2_id = cj2["conjecture_id"]
    sup = json.loads(server.engram_supersede(payload_json=json.dumps({
        "old_node_id": cj1_id,
        "new_node_id": cj2_id,
        "supersede_reason": "Scope refinement (test).",
    })))
    assert sup.get("status") == "superseded", sup

    res = json.loads(server.engram_resolve(payload_json=json.dumps({
        "target_id": cj1_id,
        "resolving_node_id": dv_id,
        "prediction_outcome": "supported",
    })))
    assert "error" in res, f"resolving a superseded target must be rejected, got: {res}"
    assert "not current" in res["error"], res["error"]
    assert cj2_id in res["error"], (
        f"error must name the current successor {cj2_id}, got: {res['error']}"
    )


def test_resolve_retracted_target_rejected_no_successor():
    """#919: resolving a retracted target is rejected with the
    no-current-successor hint (retracted nodes have no replacement)."""
    server = fresh_server()
    q_id, ob_id, dv_id = _seed_strong_resolution(server)

    q2 = json.loads(server.engram_ask(payload_json=json.dumps({
        "question": "Transient question to retract (test #919)?",
    })))
    q2_id = q2["question_id"]
    ret = json.loads(server.engram_retract(payload_json=json.dumps({
        "node_id": q2_id,
        "error_type": "other",
        "reason": "Retracting target for #919 guard test.",
    })))
    assert ret.get("status") in ("retracted", "retraction_recorded"), ret

    res = json.loads(server.engram_resolve(payload_json=json.dumps({
        "target_id": q2_id,
        "resolving_node_id": dv_id,
    })))
    assert "error" in res, f"resolving a retracted target must be rejected, got: {res}"
    assert "not current" in res["error"], res["error"]
    assert "no current successor" in res["error"], (
        f"error must carry the no-successor hint, got: {res['error']}"
    )


def test_resolve_chain_walk_names_final_successor():
    """#919: a two-hop supersede chain (A → B → C) resolves to the FINAL
    current successor C, not the intermediate B."""
    server = fresh_server()
    q_id, ob_id, dv_id = _seed_strong_resolution(server)

    ids = []
    for label in ("v1", "v2", "v3"):
        cj = json.loads(server.engram_add_conjecture(payload_json=json.dumps({
            "claim": f"Chain conjecture {label} about Y's scope.",
            "basis": f"Iteration {label} (test #919 chain).",
        })))
        ids.append(cj["conjecture_id"])
    for old, new in ((ids[0], ids[1]), (ids[1], ids[2])):
        sup = json.loads(server.engram_supersede(payload_json=json.dumps({
            "old_node_id": old,
            "new_node_id": new,
            "supersede_reason": "Chain step (test).",
        })))
        assert sup.get("status") == "superseded", sup

    res = json.loads(server.engram_resolve(payload_json=json.dumps({
        "target_id": ids[0],
        "resolving_node_id": dv_id,
        "prediction_outcome": "supported",
    })))
    assert "error" in res, f"resolving chain-root must be rejected, got: {res}"
    assert ids[2] in res["error"], (
        f"error must name the final successor {ids[2]} (not the intermediate "
        f"{ids[1]}), got: {res['error']}"
    )
    assert ids[1] not in res["error"], (
        f"intermediate successor must not be presented as the resolution "
        f"target, got: {res['error']}"
    )
