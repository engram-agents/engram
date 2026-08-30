"""Tests for the `predicted_by` relation (#2252 W6 — split out of `supported_by`).

Coverage:
  1. `predicted_by` is declared in VALID_RELATIONS and EDGE_CLASSIFICATIONS
     with cascade: False, dag_check: True — read from EDGE_CLASSIFICATIONS
     directly, never a hardcoded member-list check.
  2. The uncited-observation exemption still holds for a prediction-paired
     observation, on BOTH consumers: engram_stats (`_diagnose_impl`'s
     epistemic.uncited_observations count) and engram_lifecycle
     (`_reflect_impl`'s uncited_observations report). This is the regression
     test for the silent failure the W6 spec calls out by name: if either
     consumer is missed, a prediction-paired observation silently flips to
     "uncited" — a direction that reads as a real finding rather than a bug.
  3. `predicted_by` carries no cascade weight: it is absent from
     CASCADE_RELATIONS, so the general taint walk structurally cannot reach
     a prediction through it (declaration-level proof; the end-to-end
     behavioral proof — retract a paired observation, confirm the prediction
     is not tainted — lives in test_supported_by.py's
     test_retract_predictive_observation_retracts_prediction_not_taints,
     which now exercises the `predicted_by` edge since minting was renamed).

Companion coverage (not duplicated here):
  - "Minting a predictive observation creates a predicted_by edge and no
    supported_by edge" — tests/test_supported_by.py's
    test_predictive_observation_creates_correct_predicted_by_edge (renamed
    in place for W6).
  - Migration script (dry-run/apply/idempotent) —
    tests/test_migrate_prediction_pairing_edges.py.

Import-pattern contract: mirrors test_tier_renderer_confidence.py's
server-fixture pattern (per-test fresh temp-dir server instance).
"""

import json
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

TEST_DIR = tempfile.mkdtemp(prefix="engram_test_predicted_by_")


def fresh_server():
    if os.path.exists(TEST_DIR):
        shutil.rmtree(TEST_DIR)
    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if sys.path[0] != repo_root:
        sys.path.insert(0, repo_root)
    for key in list(sys.modules):
        if key == "server" or key.startswith("engram_"):
            del sys.modules[key]
    import server
    server._configure_paths(TEST_DIR)
    server._ensure_data_dir()
    os.environ["ENGRAM_NO_EMBEDDINGS"] = "1"
    os.environ["ENGRAM_NO_POLARITY"] = "1"
    return server


def _add_predictive_observation(server, claim, predicted_event, url="https://example.com/pb-test"):
    r = json.loads(server.engram_add_observation(payload_json=json.dumps({
        "url": url,
        "title": "Predictive source",
        "quoted_text": claim[:80],
        "interpretation": "Test interpretation.",
        "claim": claim,
        "quote_type": "attributed_analysis",
        "is_predictive": True,
        "predicted_event": predicted_event,
    })))
    assert "error" not in r, f"add_observation (predictive) failed: {r}"
    assert r.get("prediction_id") is not None, f"expected prediction_id: {r}"
    return r["observation_id"], r["prediction_id"]


def _add_plain_observation(server, claim, url="https://example.com/pb-plain"):
    r = json.loads(server.engram_add_observation(payload_json=json.dumps({
        "url": url,
        "title": "Plain source",
        "quoted_text": claim[:80],
        "interpretation": "Test interpretation.",
        "claim": claim,
        "quote_type": "attributed_analysis",
    })))
    assert "error" not in r, f"add_observation (plain) failed: {r}"
    return r["observation_id"]


def _set_importance(server, node_id, score):
    conn = server._get_db()
    try:
        conn.execute("UPDATE nodes SET importance_score = ? WHERE id = ?", (score, node_id))
        conn.commit()
    finally:
        conn.close()


def _get_entries(result, key):
    """Unwrap _slim_reflect_briefing wrapper if present (mirrors
    test_tier_renderer_confidence.py's helper)."""
    val = result.get(key, [])
    if isinstance(val, dict) and "shown" in val:
        return val["shown"]
    return val if isinstance(val, list) else []


# ── 1. Declaration ───────────────────────────────────────────────────────


def test_predicted_by_declared_with_cascade_false_and_dag_check_true():
    """predicted_by is in VALID_RELATIONS and EDGE_CLASSIFICATIONS, cascade
    False, temporal_causal/dag both True — read directly from the
    declaration, never a hardcoded member-list.

    #2357 renamed dag_check -> temporal_causal + added the new dag flag;
    predicted_by's value is unchanged and identical on both (no relation
    existing before #2357 needed re-judging — see EDGE_CLASSIFICATIONS'
    header comment), so this test checks both fields.
    """
    import engram_core as core

    assert "predicted_by" in core.VALID_RELATIONS, (
        "predicted_by must be a VALID_RELATIONS member (#2252 W6)"
    )
    assert "predicted_by" in core.EDGE_CLASSIFICATIONS, (
        "predicted_by must have an EDGE_CLASSIFICATIONS entry (#2252 W6)"
    )
    classification = core.EDGE_CLASSIFICATIONS["predicted_by"]
    assert classification["cascade"] is False, (
        f"predicted_by cascade must be False (Q1 FINAL — predictions are "
        f"non-claim-bearing; settled, not a judgement call), got: {classification}"
    )
    assert classification["temporal_causal"] is True, (
        f"predicted_by temporal_causal must be True (same provenance-chain "
        f"shape as supported_by, which it is split out of), got: {classification}"
    )
    assert classification["dag"] is True, (
        f"predicted_by dag must be True (real acyclicity matters for a "
        f"dependency-tier provenance edge), got: {classification}"
    )
    assert classification["addable_after_creation"] is False, (
        "predicted_by must not be hand-addable — minted once at node-add "
        "time only, matching supported_by's own treatment"
    )
    assert "predicted_by" not in core._ADDABLE_AFTER_CREATION_RELATIONS, (
        "predicted_by leaked into _ADDABLE_AFTER_CREATION_RELATIONS"
    )


def test_predicted_by_not_in_cascade_relations():
    """predicted_by carries no cascade weight — the general taint walk
    structurally cannot reach a prediction through it (CASCADE_RELATIONS is
    the walked-relation set; see engram_core._trace_evidence_roots's sibling
    walker in engram_revision.py)."""
    import engram_core as core

    assert "predicted_by" not in core.CASCADE_RELATIONS, (
        f"predicted_by must not be in CASCADE_RELATIONS, got: {core.CASCADE_RELATIONS}"
    )


# ── 2. Uncited-observation exemption regression test ────────────────────


def test_prediction_paired_observation_not_counted_uncited_by_stats():
    """engram_stats's uncited_observations count (via _diagnose_impl) must
    NOT count a prediction-paired observation as uncited.

    Regression test named explicitly by the W6 spec: if
    engram_stats.py's prediction-pairing exemption clause is broken (e.g. by
    a missed rename from supported_by to predicted_by), this count silently
    increases by one for every prediction-paired observation — a direction
    that reads as a real finding rather than a bug.
    """
    server = fresh_server()

    before = json.loads(server.engram_diagnose())
    before_count = before["epistemic"]["uncited_observations"]

    # A genuinely uncited plain observation DOES increase the count (sanity
    # check that the metric is live and this test isn't vacuous).
    _add_plain_observation(server, "A plain, uncited observation.")
    after_plain = json.loads(server.engram_diagnose())
    assert after_plain["epistemic"]["uncited_observations"] == before_count + 1, (
        f"expected the plain uncited observation to increase the count; "
        f"before={before_count}, after={after_plain['epistemic']['uncited_observations']}"
    )

    # A prediction-paired observation must NOT further increase the count.
    _add_predictive_observation(
        server, "X predicts Y will happen.", "Y occurs within 30 days."
    )
    after_paired = json.loads(server.engram_diagnose())
    assert after_paired["epistemic"]["uncited_observations"] == before_count + 1, (
        f"prediction-paired observation must be exempt from uncited count; "
        f"before_pairing={before_count + 1}, "
        f"after_pairing={after_paired['epistemic']['uncited_observations']}"
    )


def test_prediction_paired_observation_not_counted_uncited_by_lifecycle_reflect():
    """engram_lifecycle's weak-grounding sweep (via engram_reflect) must NOT
    list a prediction-paired observation in uncited_observations.

    Companion to the stats regression test above — same exemption clause,
    duplicated (pre-W6) across engram_stats.py and engram_lifecycle.py, now
    unified via core.prediction_pairing_exemption_sql.
    """
    server = fresh_server()

    obs_id, pred_id = _add_predictive_observation(
        server, "A predicts B will occur.", "B occurs within 45 days."
    )
    _set_importance(server, obs_id, 1.5)  # ensure tier-1 visibility in the report

    result = json.loads(server.engram_reflect(payload_json=json.dumps({"summary_top_k": 5})))
    assert "error" not in result, f"unexpected error: {result}"

    entries = _get_entries(result, "uncited_observations")
    ids = [e.get("id") for e in entries]
    assert obs_id not in ids, (
        f"prediction-paired observation {obs_id} must be exempt from "
        f"uncited_observations, got entries: {ids}"
    )


# ── 4. Site 5: _generate_snapshot's "Depended on by:" query ─────────────
# Found post-hoc by the parent's colleague review on the W6 spec (#2252):
# a fifth consumer of the pairing edge, byte-for-byte the same hardcoded-
# 'supported_by' shape as the four originally enumerated, at
# engram_core.py's _generate_snapshot (the git-diff text snapshot). Because
# the migration is ADDITIVE (old supported_by edges are never deleted), a
# missed rename here would not go uniformly blank — pre-W6 predictions would
# keep showing "Depended on by:" via their preserved old edge while post-W6
# predictions silently would not, an inconsistent-not-blank failure that is
# harder to notice than a clean regression.


def test_generate_snapshot_shows_depended_on_by_for_predicted_by_edge():
    """_generate_snapshot's observation section shows "Depended on by:
    <prediction_id>" for a prediction paired via the (post-W6) `predicted_by`
    edge — the regression test for the fifth consumer site, which the
    original W6 spec's four-site enumeration missed."""
    import engram_core as core

    server = fresh_server()

    obs_id, pred_id = _add_predictive_observation(
        server, "C predicts D will occur.", "D occurs within 20 days."
    )

    conn = server._get_db()
    try:
        snapshot = core._generate_snapshot(conn)
    finally:
        conn.close()

    assert f"Depended on by: {pred_id}" in snapshot, (
        f"expected 'Depended on by: {pred_id}' in the snapshot text for "
        f"observation {obs_id}, got snapshot:\n{snapshot}"
    )


# ── 5. prediction_pairing_exemption_sql's target_col identifier guard ────
# Colleague review (#2252 W6): target_col is interpolated into SQL via
# f-string (a column reference cannot be a bound parameter). Both current
# callers pass a constant, so there is no live exposure — but the API
# invites a future caller to pass something dynamic that would look
# ordinary at the call site. This guard closes that door.


def test_prediction_pairing_exemption_sql_rejects_non_identifier_target_col():
    """target_col must be a bare identifier or table.column — anything else
    raises ValueError rather than being interpolated into the SQL fragment."""
    import pytest
    import engram_core as core

    with pytest.raises(ValueError):
        core.prediction_pairing_exemption_sql("n.id; DROP TABLE nodes")


def test_prediction_pairing_exemption_sql_accepts_bare_identifier_and_table_column():
    """Sanity check the guard doesn't false-positive on the two accepted shapes."""
    import engram_core as core

    sql, params = core.prediction_pairing_exemption_sql("n.id")
    assert "n.id" in sql
    sql2, params2 = core.prediction_pairing_exemption_sql("node_id")
    assert "node_id" in sql2


# ── 6. engram_inspect upstream/downstream classification (BLOCKER 1) ─────
# Reviewer-fairy round 1 on #2296 (semantic-hunt brief) found the sixth
# consumer site: engram_query.py's _UPSTREAM_OUTGOING (which drives
# engram_inspect's upstream/downstream split) still enumerated only
# {derives_from, supported_by, supersedes, resolves, subtask_of} —
# predicted_by fell through the `else: downstream` branch, so an
# engram_inspect on a prediction reported its own evidential basis under
# "downstream" — the exact inversion of the direction this PR's own
# earlier commit asserts (prediction is the SOURCE / upstream side).


def test_engram_inspect_predicted_by_edge_lands_in_upstream():
    """Inspecting a prediction shows its paired (outgoing) predicted_by edge
    to the observation under logical_neighbors.upstream, not downstream."""
    server = fresh_server()

    obs_id, pred_id = _add_predictive_observation(
        server, "E predicts F will occur.", "F occurs within 15 days."
    )

    result = json.loads(server.engram_inspect(payload_json=json.dumps({"node_id": pred_id})))
    assert "error" not in result, f"unexpected error: {result}"

    ln = result["logical_neighbors"]
    upstream_ids = [nb["id"] for nb in ln["upstream"]]
    downstream_ids = [nb["id"] for nb in ln["downstream"]]
    assert obs_id in upstream_ids, (
        f"expected paired observation {obs_id} in upstream for prediction "
        f"{pred_id}, got upstream={upstream_ids}, downstream={downstream_ids}"
    )
    assert obs_id not in downstream_ids, (
        f"paired observation {obs_id} must NOT appear in downstream for "
        f"prediction {pred_id} — that is the inverted reading blocker 1 caught"
    )


# ── 7. Provenance-bearing relation list — derived, not hand-listed ──────
# Colleague review, round 5 (#2252 W6): the round-3/4 "relabel" replaced a
# false "Cascade-bearing" heading with a hand-listed "Provenance/dependency-
# bearing (derives_from, supported_by, predicted_by)" bucket that excluded
# `cites` even though `cites` is IDENTICAL to the other three on every
# EDGE_CLASSIFICATIONS field (cascade=False, removable=False,
# addable_after_creation=False, provenance=True) — no field separates them,
# so the split encoded nothing. Fix: build the user-facing list at RUNTIME
# from core.PROVENANCE_RELATIONS (EDGE_CLASSIFICATIONS' `provenance` flag),
# never hand-list membership. These two sites (engram_remove_edge,
# engram_add_edge error messages) previously had ZERO test coverage — the
# third instance of the same untested-site-is-the-invisible-site
# correlation on this branch.


def test_remove_edge_blocked_message_lists_derived_provenance_relations():
    """engram_remove_edge's error message for a blocked relation must list
    exactly the provenance-bearing relations, derived from
    EDGE_CLASSIFICATIONS' `provenance` flag at call time — not a hand-typed
    member list. Computed independently here (not by importing
    core.PROVENANCE_RELATIONS) so this test cannot pass by re-reading the
    same possibly-stale constant the production code reads."""
    import json
    import engram_core as core

    server = fresh_server()

    expected = sorted(r for r, v in core.EDGE_CLASSIFICATIONS.items() if v["provenance"])
    expected_str = ", ".join(expected)

    result = json.loads(server._remove_edge_impl(
        source_id="a", target_id="b", relation="supported_by"
    ))
    assert "error" in result, result
    assert f"Provenance-bearing edges ({expected_str})" in result["error"], (
        f"expected 'Provenance-bearing edges ({expected_str})' in the "
        f"error message, got: {result['error']!r}"
    )
    # cites must be present — the whole point of this round's fix.
    assert "cites" in result["error"], (
        f"'cites' must be folded into the provenance-bearing bucket, "
        f"got: {result['error']!r}"
    )


def test_add_edge_blocked_message_lists_derived_provenance_relations():
    """Same binding for engram_add_edge's error message."""
    import json
    import engram_core as core

    server = fresh_server()

    expected = sorted(r for r, v in core.EDGE_CLASSIFICATIONS.items() if v["provenance"])
    expected_str = ", ".join(expected)

    result = json.loads(server._add_edge_impl(
        source_id="a", target_id="b", relation="cites"
    ))
    assert "error" in result, result
    assert f"Provenance-bearing edges ({expected_str})" in result["error"], (
        f"expected 'Provenance-bearing edges ({expected_str})' in the "
        f"error message, got: {result['error']!r}"
    )
    assert "predicted_by" in result["error"], (
        f"'predicted_by' must be in the provenance-bearing bucket, "
        f"got: {result['error']!r}"
    )
