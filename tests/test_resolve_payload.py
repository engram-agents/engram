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

import json
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


TEST_DIR = os.path.join(tempfile.gettempdir(), "engram_test_payload_resolve")


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
    q_inspect = json.loads(server.engram_inspect(node_id=q_id, dream_mode=True))
    assert q_inspect["node"]["status"] == "resolved", q_inspect["node"]


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
      - Weak resolve: attributed_analysis (0.70) × inductive_analogy (0.70) = 0.49 → below threshold
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
        "reasoning_type": "inductive_analogy",
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
    q_inspect = json.loads(server.engram_inspect(node_id=q_id, dream_mode=True))
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
