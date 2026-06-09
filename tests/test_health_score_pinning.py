"""Health-score pinning test — dual-direction probe discipline (D3 / dv_0843).

Dual-probe requirement (charter spec D3): every classifier the self-improvement
cycle relies on must be tested in BOTH directions before it is trusted:

  1. Known-good control probe — clean graph gives expected high score.
     Guards against a classifier that always outputs "broken."
  2. Known-bad sabotage probe — introducing deliberate degradation causes
     health_score to drop by the formula-specified amount.
     Guards against a classifier that always outputs "correct."

The frozen stability metric reads health_score.  Without test coverage either
probe direction is unsatisfied — the metric could be reporting a constant, and
any regression introduced by the cycle would pass silently.

Coverage:
  1. Known-good control probe — baseline clean graph ≥ 80.
  2. Tainted-node sabotage — retract a premise; downstream derivation gets
     tainted_by; health_score drops by the formula penalty (-3 per tainted node).
  3. Orphan-node sabotage — inject a node with no edges; health_score drops by
     the formula penalty (-1 per orphan node).
  4. Magnitude scaling — 5 tainted nodes hit the -15 cap, not -3×5 = -15 + epsilon.
"""

from __future__ import annotations

import atexit
import json
import os
import shutil
import sqlite3
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

TEST_DIR = tempfile.mkdtemp(prefix="engram_test_health_score_")
atexit.register(lambda: shutil.rmtree(TEST_DIR, ignore_errors=True))


def fresh_server():
    """Return a freshly initialised server module with an empty test DB."""
    td = tempfile.mkdtemp(dir=TEST_DIR)
    for key in list(sys.modules):
        if key == "server" or key.startswith("engram_"):
            del sys.modules[key]
    import server
    server._configure_paths(td)
    server._ensure_data_dir()
    os.environ["ENGRAM_NO_EMBEDDINGS"] = "1"
    return server


def _get_db_path(server) -> str:
    import engram_core
    return str(engram_core.DB_PATH)


def _health_score(server) -> float:
    r = json.loads(server.engram_stats(json.dumps({"sections": ["health_score"]})))
    return r["health_score"]


def _seed_observation(server, claim: str, quote_type: str = "hard_data") -> str:
    r = json.loads(server.engram_add_observation(payload_json=json.dumps({
        "url": "https://example.com/test",
        "title": "Test Article",
        "quoted_text": f"Supporting quote for: {claim}",
        "interpretation": f"Interpretation of: {claim}",
        "claim": claim,
        "quote_type": quote_type,
        "source_class": "external",
    })))
    assert r.get("status") == "created", f"obs creation failed: {r}"
    return r["observation_id"]


def _seed_derivation(server, claim: str, supporting_ids: list[str]) -> str:
    r = json.loads(server.engram_derive(payload_json=json.dumps({
        "claim": claim,
        "supporting_ids": ",".join(supporting_ids),
        "logical_chain": f"Chain for: {claim}",
        "reasoning_type": "deductive_modus_ponens",
    })))
    assert "derivation_id" in r, f"derivation creation failed: {r}"
    return r["derivation_id"]


def _retract(server, node_id: str) -> None:
    r = json.loads(server.engram_retract(payload_json=json.dumps({
        "node_id": node_id,
        "error_type": "hallucinated_claim",
        "reason": "Sabotage probe: deliberate retraction for health-score test",
    })))
    assert r.get("status") == "retracted", f"retract failed: {r}"


# ── 1. Known-good control probe ───────────────────────────────────────────────


def test_health_score_known_good_probe():
    """A minimal clean graph must produce a health_score ≥ 80.

    D3 known-good direction: the classifier must return "healthy" on healthy input.
    A score < 80 on an empty/minimal graph means the formula is miscalibrated or
    the classifier is permanently in the "broken" position.
    """
    server = fresh_server()
    ob1 = _seed_observation(server, "Known-good observation A")
    ob2 = _seed_observation(server, "Known-good observation B")
    _seed_derivation(server, "Known-good derivation from A and B", [ob1, ob2])

    score = _health_score(server)
    assert score >= 80, (
        f"Known-good control probe FAILED: clean graph score {score} < 80. "
        "The health_score classifier is not returning 'healthy' on healthy input — "
        "either the formula is miscalibrated or the probe always returns 'broken'."
    )


# ── 2. Tainted-node sabotage probe ───────────────────────────────────────────


def test_health_score_tainted_node_sabotage():
    """Retracting a premise taints its derivation; health_score drops by 3.

    D3 known-bad sabotage direction (dv_0843): the classifier must return 'degraded'
    on degraded input.  Formula: -3 per tainted node.

    If this test fails, health_score is NOT detecting tainted nodes — the stability
    metric would pass a cycle that introduced retraction cascades silently.
    """
    server = fresh_server()
    ob_premise = _seed_observation(server, "Sabotage premise observation")
    ob_other = _seed_observation(server, "Unaffected observation")
    dv = _seed_derivation(
        server,
        "Derivation that will become tainted",
        [ob_premise, ob_other],
    )

    baseline = _health_score(server)

    _retract(server, ob_premise)

    after = _health_score(server)
    drop = round(baseline - after, 1)

    # dv is now tainted_by ob_premise → penalty is -3
    assert drop >= 3.0, (
        f"Tainted-node sabotage probe FAILED: expected health_score to drop by ≥3, "
        f"got {drop} (baseline={baseline}, after={after}). "
        "health_score is not detecting tainted nodes — the stability metric is blind "
        "to retraction cascades."
    )


# ── 3. Orphan-node sabotage probe ────────────────────────────────────────────


def test_health_score_orphan_node_sabotage():
    """An orphan node (no edges) incurs a -1 health_score penalty.

    D3 known-bad sabotage direction: the classifier must detect structural gaps.
    Orphan nodes represent claims that float disconnected from the graph — exactly
    the kind of epistemic gap the health metric exists to surface.

    If this test fails, health_score is NOT detecting orphan nodes — isolated claims
    introduced by the cycle would go unnoticed.
    """
    server = fresh_server()
    ob1 = _seed_observation(server, "Connected observation A")
    ob2 = _seed_observation(server, "Connected observation B")
    _seed_derivation(server, "Derivation from A and B", [ob1, ob2])

    baseline = _health_score(server)

    # Inject an orphan node directly (no edges created).
    # Cannot use the MCP API because every observation creates an `about` edge to
    # its evidence node.  Direct DB insert is the only way to create a true orphan.
    db_path = _get_db_path(server)
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """INSERT INTO nodes
                 (id, type, claim, confidence, is_current, status, metadata, created_at)
               VALUES (?, ?, ?, ?, 1, 'current', '{}',
                       strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))""",
            ("ob_orphan_test", "observation_factual", "Orphan node with no edges", 0.7),
        )
        conn.commit()

    after = _health_score(server)
    drop = round(baseline - after, 1)

    assert drop >= 1.0, (
        f"Orphan-node sabotage probe FAILED: expected health_score to drop by ≥1, "
        f"got {drop} (baseline={baseline}, after={after}). "
        "health_score is not detecting orphan nodes — the stability metric is blind "
        "to disconnected claims."
    )


# ── 4. Magnitude scaling — cap at -15 for tainted ───────────────────────────


def test_health_score_tainted_node_cap():
    """5 tainted nodes hit the -15 cap, not a runaway penalty.

    Formula: min(tainted_count * 3, 15). With 5 tainted nodes: 5*3=15, which equals
    the cap exactly.  This tests that the formula applies correctly at the boundary
    and the score doesn't underflow past the cap.

    Graph is padded to 60+ nodes so the 5 retractions stay below the 10% retraction-rate
    threshold — otherwise the retraction-rate penalty fires on top of the taint penalty
    and the combined drop is 30, masking whether the taint cap is working.
    """
    server = fresh_server()

    # Pad the graph: 55 background observations so total nodes >> 5 retractions.
    # 5 retracted / ~70 total ≈ 7% < 10% threshold → retraction-rate penalty stays silent.
    for i in range(55):
        _seed_observation(server, f"Background observation {i}")

    # Build 5 independent premise → derivation chains.
    # Retracting each premise taints its derivation → 5 tainted nodes.
    premises = []
    for i in range(5):
        ob_p = _seed_observation(server, f"Sabotage premise {i}")
        ob_s = _seed_observation(server, f"Supporting observation {i}")
        _seed_derivation(server, f"Derivation {i} (will be tainted)", [ob_p, ob_s])
        premises.append(ob_p)

    baseline = _health_score(server)

    for ob_p in premises:
        _retract(server, ob_p)

    after = _health_score(server)
    drop = round(baseline - after, 1)

    # 5 tainted nodes → penalty = min(5*3, 15) = 15
    assert drop >= 15.0, (
        f"Cap-scaling probe FAILED: 5 tainted nodes should drop score by ≥15, "
        f"got {drop} (baseline={baseline}, after={after})."
    )
    # The taint cap is -15; with retraction rate below 10% on the padded graph,
    # total drop should be close to 15 (allow +5 slack for minor compound penalties).
    assert drop <= 20.0, (
        f"Cap-scaling probe UNEXPECTED: drop {drop} exceeds expected taint cap of 15. "
        "Either the cap isn't applied, or another penalty is stacking unexpectedly. "
        f"baseline={baseline}, after={after}."
    )


# ── Parity pin: stats and diagnose report the SAME score (charter §6 fold) ──


def test_stats_and_diagnose_health_score_parity():
    """The single-source property itself, pinned.

    Both surfaces must report the identical health_score on the same graph
    because both consume _compute_health_score. Mutation note: re-introducing
    a second formula copy that drifts by any coefficient would fail this test
    on any graph where the drifted penalty term is non-zero — the sabotaged
    graph below exercises the tainted-node penalty (retraction cascade), so
    a drift in that coefficient (or in the base/bonus/clamp envelope) shows
    as a stats↔diagnose mismatch.
    """
    server = fresh_server()
    ob_premise = _seed_observation(server, "Parity sabotage premise")
    ob_other = _seed_observation(server, "Parity unaffected observation")
    _seed_derivation(server, "Derivation to taint for parity", [ob_premise, ob_other])
    _retract(server, ob_premise)  # taints the derivation → non-zero penalty term

    stats_score = _health_score(server)

    diag = json.loads(server.engram_diagnose())
    # diagnose nests the score under metrics in the current response shape;
    # accept either placement so the parity property, not the envelope, is pinned.
    diag_score = (
        diag.get("health_score")
        if "health_score" in diag
        else diag["metrics"]["health_score"]
    )
    assert stats_score == diag_score, (
        f"stats ({stats_score}) and diagnose ({diag_score}) must report the "
        f"same health_score — single source of truth violated"
    )
