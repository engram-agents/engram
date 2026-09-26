"""Tests for the supported_by relation rename and direction fix (PR #406).

Coverage:
  1. Taint cascade — retracting the TARGET of a supported_by edge taints the SOURCE.
  2. Taint cascade (inverse not fired) — retracting the SOURCE does not taint the TARGET.
  3. Prediction path post-fix — is_predictive observation creates
     (prediction, observation, 'predicted_by') [#2252 W6: renamed from
     'supported_by', direction unchanged]; retracting the observation
     taints the prediction, not the reverse.
  4. Migration — fixture with both cornerstone-style and prediction-style
     'supports' edges migrated correctly; idempotent on second run.

Import-pattern contract (#948 wave B — R1):
    This file imports engram_revision, engram_cornerstone, engram_observation,
    and engram_core directly and contains no ``import server``. The module objects
    are passed to fresh_module() (R4) for per-test reload isolation.
"""

import atexit
import importlib
import json
import os
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Add tests/ dir so conftest.fresh_module is importable both under pytest and
# when the file is run as a standalone script.
_TESTS_DIR = str(Path(__file__).resolve().parent)
if _TESTS_DIR not in sys.path:
    sys.path.insert(0, _TESTS_DIR)
from conftest import fresh_module  # noqa: E402 — must follow sys.path setup

import engram_core
import engram_cornerstone
import engram_observation
import engram_revision

TEST_DIR = tempfile.mkdtemp(prefix="engram_test_supported_by_")
atexit.register(lambda: shutil.rmtree(TEST_DIR, ignore_errors=True))


def _make_fresh_mods():
    """Reload all family modules, configure a clean test DB, and return modules.

    Uses conftest.fresh_module(engram_revision) per R4: the module object is
    passed (not a string), so the ``import engram_revision`` at the top of
    this file is the AST-visible dependency edge for run_touched_tests.py.

    Returns (rev, obs, cs, core) — freshly reloaded module objects with
    engram_core pointing to TEST_DIR.
    """
    if os.path.exists(TEST_DIR):
        shutil.rmtree(TEST_DIR)
    os.environ["ENGRAM_NO_EMBEDDINGS"] = "1"
    rev = fresh_module(engram_revision)
    obs = importlib.import_module("engram_observation")
    cs = importlib.import_module("engram_cornerstone")
    core = sys.modules["engram_core"]
    core._configure_paths(TEST_DIR)
    core._ensure_data_dir()
    return rev, obs, cs, core


def _seed_observation(obs, url="https://example.com/a", claim="Test claim.", quote="Test quote."):
    """Create a basic factual observation and return its ID."""
    r = json.loads(obs._add_observation_impl(
        url=url,
        title="Source",
        quoted_text=quote,
        interpretation="Test interp.",
        claim=claim,
        quote_type="attributed_analysis",
    ))
    assert "error" not in r, r
    return r["observation_id"]


def _get_db(core):
    return core._get_db()


def _edge_exists(core, source_id, target_id, relation):
    conn = _get_db(core)
    try:
        row = conn.execute(
            "SELECT 1 FROM edges WHERE source_id = ? AND target_id = ? AND relation = ?",
            (source_id, target_id, relation),
        ).fetchone()
        return row is not None
    finally:
        conn.close()


def _is_tainted(core, node_id, by_id):
    """Return True if node_id carries a pending retract-severity cascade
    marker rooted at by_id. Migrated off metadata.tainted_by to
    cascade_markers (#2013/#2009 slice 1) — same meaning, new storage.
    """
    conn = _get_db(core)
    try:
        row = conn.execute(
            "SELECT 1 FROM cascade_markers WHERE node_id = ? AND root_node = ? "
            "AND root_op = 'retract' AND status = 'pending'",
            (node_id, by_id),
        ).fetchone()
        return row is not None
    finally:
        conn.close()


def _node_status(core, node_id):
    conn = _get_db(core)
    try:
        row = conn.execute("SELECT status FROM nodes WHERE id = ?", (node_id,)).fetchone()
        return row["status"] if row else None
    finally:
        conn.close()


# ── 1. Taint cascade: retracting target taints source ──────────────────────


def test_retract_target_does_NOT_taint_cornerstone_source():
    """Retracting the TARGET of a supported_by edge from a CORNERSTONE does NOT taint it.

    Cascade skip rule (§2.1): cornerstones accumulate instance-votes. Retracting
    one supporting observation does NOT mark the cornerstone tainted — the pattern
    may still hold via remaining supporters.

    Setup: cornerstone (source) --supported_by--> observation (target/premise).
    Retracting the observation must NOT taint the cornerstone.
    """
    rev, obs, cs, core = _make_fresh_mods()

    # Create the observation (premise). Use the default evidence URL to avoid
    # multiple DNS lookups in the test environment when tests run in sequence.
    obs_id = _seed_observation(obs, claim="Premise observation.")

    # Create a cornerstone and link it to the observation via supported_by
    cs_r = json.loads(cs._add_cornerstone_impl(
        tag="test-cornerstone",
        title="Test cornerstone principle",
        new_frame="Operate according to the test principle.",
        supporting_ids=obs_id,
    ))
    assert "error" not in cs_r, cs_r
    cs_id = cs_r["cornerstone_id"]

    # Verify the edge was created with the correct direction and relation
    assert _edge_exists(core, cs_id, obs_id, "supported_by"), (
        f"Expected (cornerstone={cs_id}, obs={obs_id}, 'supported_by') edge"
    )

    # Retract the observation (premise / target of the edge)
    ret = json.loads(rev._retract_impl(
        node_id=obs_id,
        error_type="hallucinated_claim",
        reason="The premise observation was fabricated.",
     replacement_absent_because="NONE_BECAUSE_UNKNOWN", replacement_absent_note="test fixture -- replacement decision not under test",))
    assert "error" not in ret, ret

    # The cornerstone (source) must NOT be tainted — cascade skip rule applies.
    assert not _is_tainted(core, cs_id, obs_id), (
        f"Cornerstone {cs_id} must NOT be tainted_by {obs_id}: "
        f"cascade skip rule applies to cornerstones (instance-vote semantics)."
    )

    # The cornerstone should appear in support_lost (only one supporter existed).
    assert ret.get("support_lost_count", 0) == 1, (
        f"Expected support_lost_count=1, got: {ret}"
    )
    support_lost_ids = [n["id"] for n in ret.get("support_lost", [])]
    assert cs_id in support_lost_ids, (
        f"Expected {cs_id} in support_lost list, got: {support_lost_ids}"
    )


# ── 2. Inverse NOT tainted: retracting source does not taint target ─────────


def test_retract_source_does_not_taint_target():
    """Retracting the SOURCE of a supported_by edge must NOT taint the TARGET.

    The target is the premise — it's upstream, not downstream. The taint cascade
    only flows source→ (from dependents down to their dependents), never ←target.
    """
    rev, obs, cs, core = _make_fresh_mods()

    obs_id = _seed_observation(obs, url="https://example.com/premise2",
                               claim="Second premise observation.")

    cs_r = json.loads(cs._add_cornerstone_impl(
        tag="test-cornerstone-2",
        title="Second cornerstone",
        new_frame="Second principle.",
        supporting_ids=obs_id,
    ))
    assert "error" not in cs_r, cs_r
    cs_id = cs_r["cornerstone_id"]

    assert _edge_exists(core, cs_id, obs_id, "supported_by")

    # Retract the CORNERSTONE (source / dependent side)
    # Cornerstones don't have a valid retract error_type path in the typical
    # workflow, but the internal _retract_impl is type-agnostic.
    ret = json.loads(rev._retract_impl(
        node_id=cs_id,
        error_type="other",
        reason="Retracting the cornerstone itself to test inverse cascade.",
     replacement_absent_because="NONE_BECAUSE_UNKNOWN", replacement_absent_note="test fixture -- replacement decision not under test",))
    assert "error" not in ret, ret

    # The observation (target / premise) must NOT be tainted
    assert not _is_tainted(core, obs_id, cs_id), (
        f"Observation {obs_id} must NOT be tainted by retracting its dependent {cs_id}"
    )


# ── 3. Prediction path post-fix ─────────────────────────────────────────────


def test_predictive_observation_creates_correct_predicted_by_edge():
    """An is_predictive observation creates (prediction, observation, 'predicted_by').

    The prediction is the dependent (source); the observation is the premise (target).

    #2252 W6: the pairing edge was split out of `supported_by` into its own
    `predicted_by` relation (direction unchanged — prediction stays the
    source). This test asserts the NEW relation, and that no `supported_by`
    edge (either direction) is minted for the pairing — the regression test
    for a missed rename at the single creation site (engram_observation.py).
    """
    _rev, obs, _cs, core = _make_fresh_mods()

    r = json.loads(obs._add_observation_impl(
        url="https://example.com/predictive",
        title="Predictive source",
        quoted_text="Based on X, Y will follow.",
        interpretation="X predicts Y.",
        claim="Y will follow from X.",
        quote_type="attributed_analysis",
        is_predictive=True,
        predicted_event="Y occurs within 30 days.",
        resolution_timeframe="2026-06-30",
    ))
    assert "error" not in r, r

    obs_id = r["observation_id"]
    pred_id = r.get("prediction_id")
    assert pred_id is not None, f"Expected prediction_id in response, got: {r}"

    # Direction must be: prediction (dependent/source) predicted_by observation (premise/target)
    assert _edge_exists(core, pred_id, obs_id, "predicted_by"), (
        f"Expected (prediction={pred_id}, obs={obs_id}, 'predicted_by') edge; "
        f"inverse or 'supports' would be wrong."
    )
    # The old wrong direction must NOT exist
    assert not _edge_exists(core, obs_id, pred_id, "predicted_by"), (
        f"Old inverted edge (obs={obs_id}, prediction={pred_id}) must not exist"
    )
    assert not _edge_exists(core, obs_id, pred_id, "supports"), (
        f"Stale 'supports' edge must not exist"
    )
    # #2252 W6: no supported_by edge (either direction) should be minted for
    # the pairing anymore — it must be predicted_by only.
    assert not _edge_exists(core, pred_id, obs_id, "supported_by"), (
        f"Pairing edge must be 'predicted_by', not 'supported_by' "
        f"(prediction={pred_id}, obs={obs_id})"
    )
    assert not _edge_exists(core, obs_id, pred_id, "supported_by"), (
        f"Pairing edge must be 'predicted_by', not 'supported_by' (inverse direction)"
    )


def test_retract_predictive_observation_retracts_prediction_not_taints():
    """Retracting the paired observation RETRACTS the prediction — not taint.

    #2252 Stage 1.3: `supported_by` no longer carries cascade, and
    `prediction` is NCB (Q1 FINAL, Kepler's corrected #2281 ruling,
    2026-08-27T00:46:05Z) — the general taint-walk stops at the prediction
    by partition membership before it would even reach this edge, so it is
    NOT the mechanism that gives a voided prediction its signal (asserted
    below, bare form: "not tainted," not "not tainted via supported_by" —
    the narrower via-supported_by wording from #2283 rested on a
    since-retracted ruling and is withdrawn).

    The signal is issue #2281's dedicated basis-retraction branch in
    `_retract_impl`, added alongside this test: a prediction whose sole
    live evidential basis (its paired observation_predictive) is retracted
    was never validly grounded — void ab initio — and gets RETRACTED, the
    correct terminal state per the retract/supersede split (never valid,
    not valid-then-updated). This closes the orphan-marker gap this test's
    predecessor docstring flagged as not-yet-built.
    """
    rev, obs, _cs, core = _make_fresh_mods()

    r = json.loads(obs._add_observation_impl(
        url="https://example.com/predictive2",
        title="Predictive source 2",
        quoted_text="Model signals an upcoming event.",
        interpretation="Signal predicts event.",
        claim="Event will occur.",
        quote_type="attributed_analysis",
        is_predictive=True,
        predicted_event="Event occurs within 60 days.",
    ))
    assert "error" not in r, r

    obs_id = r["observation_id"]
    pred_id = r["prediction_id"]

    # Retract the observation
    ret = json.loads(rev._retract_impl(
        node_id=obs_id,
        error_type="fabricated_quote",
        reason="Quote was not in the source document.",
     replacement_absent_because="NONE_BECAUSE_UNKNOWN", replacement_absent_note="test fixture -- replacement decision not under test",))
    assert "error" not in ret, ret
    assert ret.get("retracted_predictions_count") == 1, ret
    assert ret["retracted_predictions"][0]["id"] == pred_id, ret

    # The prediction must NOT be tainted — the general cascade never
    # reaches it (bare form, #2252 Stage 1.3).
    assert not _is_tainted(core, pred_id, obs_id), (
        f"Expected prediction {pred_id} to NOT be tainted_by {obs_id} — "
        f"predictions are NCB and supported_by no longer cascades (#2252 Stage 1.3)"
    )

    # The prediction must be RETRACTED (#2281's dedicated branch, not the
    # general cascade).
    conn = _get_db(core)
    try:
        row = conn.execute(
            "SELECT status, is_current, metadata FROM nodes WHERE id = ?",
            (pred_id,),
        ).fetchone()
    finally:
        conn.close()
    assert row is not None, f"prediction {pred_id} not found"
    assert row["status"] == "retracted", (
        f"Expected prediction {pred_id} status='retracted', got {row['status']!r}"
    )
    assert row["is_current"] == 0, (
        f"Expected prediction {pred_id} is_current=0, got {row['is_current']!r}"
    )
    meta = json.loads(row["metadata"] or "{}")
    assert meta.get("error_type") == "fabricated_quote", meta
    assert meta.get("retracted_via_basis_retraction") == obs_id, meta


def test_retract_predictive_observation_does_not_retract_prediction_with_other_basis():
    """A prediction with a second live observation_predictive basis survives
    the retraction of one of its supporting observations — its evidential
    basis is not entirely gone, so #2281's branch must not retract it.
    """
    rev, obs, _cs, core = _make_fresh_mods()

    r1 = json.loads(obs._add_observation_impl(
        url="https://example.com/predictive-multi-1",
        title="Predictive source A",
        quoted_text="Signal A predicts the event.",
        interpretation="Signal A predicts event.",
        claim="Event will occur (signal A).",
        quote_type="attributed_analysis",
        is_predictive=True,
        predicted_event="Shared event occurs within 90 days.",
    ))
    assert "error" not in r1, r1
    obs_a_id = r1["observation_id"]
    pred_id = r1["prediction_id"]

    r2 = json.loads(obs._add_observation_impl(
        url="https://example.com/predictive-multi-2",
        title="Predictive source B",
        quoted_text="Signal B also predicts the event.",
        interpretation="Signal B predicts event.",
        claim="Event will occur (signal B).",
        quote_type="attributed_analysis",
        is_predictive=True,
        predicted_event="Shared event occurs within 90 days.",
    ))
    assert "error" not in r2, r2
    obs_b_id = r2["observation_id"]
    # Same predicted_event text dedups onto the same prediction node.
    assert r2["prediction_id"] == pred_id, r2

    ret = json.loads(rev._retract_impl(
        node_id=obs_a_id,
        error_type="fabricated_quote",
        reason="Signal A's quote was not in the source document.",
     replacement_absent_because="NONE_BECAUSE_UNKNOWN", replacement_absent_note="test fixture -- replacement decision not under test",))
    assert "error" not in ret, ret
    # The implementation only ever adds the key when the list is non-empty
    # (engram_revision.py's retracted_predictions block), so its absence
    # already says "zero" — no need for the wordier disjunction.
    assert "retracted_predictions" not in ret, ret

    conn = _get_db(core)
    try:
        row = conn.execute(
            "SELECT status, is_current FROM nodes WHERE id = ?",
            (pred_id,),
        ).fetchone()
    finally:
        conn.close()
    assert row["status"] != "retracted", (
        f"prediction {pred_id} was retracted even though {obs_b_id} still "
        f"supports it — a live second basis must protect it"
    )
    assert row["is_current"] == 1, row


def test_retract_predictive_observation_preserves_resolved_status_before_overwrite():
    """A prediction that has already RESOLVED (status is a RESOLVED_STATUSES
    value — confirmed/refuted/etc, engram_core.py) before its basis is
    retracted must not silently lose that completed falsification
    measurement from the live surface: retraction still overwrites
    status='retracted' (the outcome IS displaced, not preserved as the
    live status), but metadata.status_before_retraction records it so it's
    recoverable from the node itself, not only by digging into edit_history.

    Caught by Borges's colleague review on PR #2292 (2026-08-27): he first
    suspected the resolved status was fully lost, then verified it survives
    in edit_history and the resolves edge stays intact (displaced, not
    destroyed) — but recovering it required knowing to dig into the audit
    log. This test covers the one-line fix that makes it visible on the
    node directly.
    """
    rev, obs, _cs, core = _make_fresh_mods()

    r = json.loads(obs._add_observation_impl(
        url="https://example.com/predictive-resolved",
        title="Predictive source, later resolved",
        quoted_text="Signal E predicts the event.",
        interpretation="Signal E predicts event.",
        claim="Event will occur (signal E).",
        quote_type="attributed_analysis",
        is_predictive=True,
        predicted_event="Resolved-then-retracted event occurs within 30 days.",
    ))
    assert "error" not in r, r
    obs_id = r["observation_id"]
    pred_id = r["prediction_id"]

    resolver_id = _seed_observation(
        obs, url="https://example.com/resolver",
        claim="The event did occur, confirming the prediction.",
    )
    res = json.loads(rev._resolve_impl(
        target_id=pred_id,
        resolving_node_id=resolver_id,
        prediction_outcome="confirmed",
    ))
    assert "error" not in res, res

    conn = _get_db(core)
    try:
        pre = conn.execute(
            "SELECT status FROM nodes WHERE id = ?", (pred_id,)
        ).fetchone()
    finally:
        conn.close()
    assert pre["status"] == "confirmed", pre

    ret = json.loads(rev._retract_impl(
        node_id=obs_id,
        error_type="fabricated_quote",
        reason="Signal E's quote was not in the source document.",
     replacement_absent_because="NONE_BECAUSE_UNKNOWN", replacement_absent_note="test fixture -- replacement decision not under test",))
    assert "error" not in ret, ret
    assert ret.get("retracted_predictions_count") == 1, ret

    conn = _get_db(core)
    try:
        row = conn.execute(
            "SELECT status, is_current, metadata FROM nodes WHERE id = ?",
            (pred_id,),
        ).fetchone()
    finally:
        conn.close()
    assert row["status"] == "retracted", row
    assert row["is_current"] == 0, row
    meta = json.loads(row["metadata"] or "{}")
    assert meta.get("status_before_retraction") == "confirmed", meta


def test_retract_predictive_observation_with_no_paired_prediction_noops():
    """Retracting an observation_predictive that has no paired prediction at
    all (is_predictive=True but predicted_event omitted — legal today,
    nothing enforces the pairing) must no-op cleanly: no crash, no
    retracted_predictions key in the result.

    Caught as a gap by round-1 reviewer-fairy on PR #2292 (2026-08-27):
    verified correct by the fairy's own probe script but unasserted by any
    shipped test until now.
    """
    rev, obs, _cs, core = _make_fresh_mods()

    r = json.loads(obs._add_observation_impl(
        url="https://example.com/predictive-unpaired",
        title="Predictive source, no predicted_event",
        quoted_text="This reads as predictive in tone but names no event.",
        interpretation="Predictive framing without a decomposable prediction.",
        claim="Something predictive-shaped, unpaired.",
        quote_type="attributed_analysis",
        is_predictive=True,
        # predicted_event deliberately omitted.
    ))
    assert "error" not in r, r
    assert r.get("prediction_id") is None, r
    obs_id = r["observation_id"]

    ret = json.loads(rev._retract_impl(
        node_id=obs_id,
        error_type="fabricated_quote",
        reason="No paired prediction exists to react to.",
     replacement_absent_because="NONE_BECAUSE_UNKNOWN", replacement_absent_note="test fixture -- replacement decision not under test",))
    assert "error" not in ret, ret
    assert "retracted_predictions" not in ret, ret


def test_retract_predictive_observation_retracts_prediction_only_after_last_basis_gone():
    """Sequential retraction of BOTH bases of a shared prediction: the first
    retraction (while the other basis is still live) must leave the
    prediction alone; only the second retraction (removing the last live
    basis) retracts it.

    Companion to test_retract_predictive_observation_does_not_retract_prediction_with_other_basis
    above, which only covers the "one basis retracted, one still live" half.
    This is the terminal case that half deliberately doesn't reach — caught
    as a gap by round-1 reviewer-fairy on PR #2292 (2026-08-27), verified
    correct by the fairy's own probe script but unasserted by any shipped
    test until now.
    """
    rev, obs, _cs, core = _make_fresh_mods()

    r1 = json.loads(obs._add_observation_impl(
        url="https://example.com/predictive-sequential-1",
        title="Predictive source C",
        quoted_text="Signal C predicts the shared event.",
        interpretation="Signal C predicts event.",
        claim="Event will occur (signal C).",
        quote_type="attributed_analysis",
        is_predictive=True,
        predicted_event="Sequential shared event occurs within 45 days.",
    ))
    assert "error" not in r1, r1
    obs_c_id = r1["observation_id"]
    pred_id = r1["prediction_id"]

    r2 = json.loads(obs._add_observation_impl(
        url="https://example.com/predictive-sequential-2",
        title="Predictive source D",
        quoted_text="Signal D also predicts the shared event.",
        interpretation="Signal D predicts event.",
        claim="Event will occur (signal D).",
        quote_type="attributed_analysis",
        is_predictive=True,
        predicted_event="Sequential shared event occurs within 45 days.",
    ))
    assert "error" not in r2, r2
    obs_d_id = r2["observation_id"]
    assert r2["prediction_id"] == pred_id, r2

    # First retraction: obs_d is still live, so the prediction must survive.
    ret1 = json.loads(rev._retract_impl(
        node_id=obs_c_id,
        error_type="fabricated_quote",
        reason="Signal C's quote was not in the source document.",
     replacement_absent_because="NONE_BECAUSE_UNKNOWN", replacement_absent_note="test fixture -- replacement decision not under test",))
    assert "error" not in ret1, ret1
    assert "retracted_predictions" not in ret1, ret1

    conn = _get_db(core)
    try:
        row = conn.execute(
            "SELECT status, is_current FROM nodes WHERE id = ?",
            (pred_id,),
        ).fetchone()
    finally:
        conn.close()
    assert row["status"] != "retracted", row
    assert row["is_current"] == 1, row

    # Second retraction: obs_c is already is_current=0 (retracted above), so
    # it no longer counts as "another basis" — this is the last live basis
    # gone, and the prediction must now be retracted.
    ret2 = json.loads(rev._retract_impl(
        node_id=obs_d_id,
        error_type="fabricated_quote",
        reason="Signal D's quote was not in the source document either.",
     replacement_absent_because="NONE_BECAUSE_UNKNOWN", replacement_absent_note="test fixture -- replacement decision not under test",))
    assert "error" not in ret2, ret2
    assert ret2.get("retracted_predictions_count") == 1, ret2
    assert ret2["retracted_predictions"][0]["id"] == pred_id, ret2

    conn = _get_db(core)
    try:
        row = conn.execute(
            "SELECT status, is_current, metadata FROM nodes WHERE id = ?",
            (pred_id,),
        ).fetchone()
    finally:
        conn.close()
    assert row["status"] == "retracted", row
    assert row["is_current"] == 0, row
    meta = json.loads(row["metadata"] or "{}")
    assert meta.get("retracted_via_basis_retraction") == obs_d_id, meta


# ── 5. #2252 Stage 1.3 — derived NCB cascade skip ───────────────────────────


def test_cascade_relations_membership_is_pinned():
    """CASCADE_RELATIONS == {'derives_from', 'acknowledged_derives_from'}
    after Stage 1.3 + #2357.

    Asserted directly so a future flag flip (e.g. re-adding cascade to
    supported_by, or adding a new cascade-bearing relation) is loud rather
    than silently changing cascade-walk behavior with no test to catch it.
    #2357 (2026-08-29) added `acknowledged_derives_from` as a second cascade
    relation — the cascade-frontier acknowledgement edge, which must
    cascade (that's the entire reason it exists).

    RENAMED from `test_cascade_relations_is_derives_from_only`, and the
    rename is the point rather than tidying: the old name asserted a
    membership claim that THIS PR makes false, while the body and the
    assertion were already correct. The test did its job — it fired on the
    addition, exactly as its own docstring said it was for — and only its
    NAME had become the false half. A test named for an invariant it does
    not test gets trusted by its name later.

    Restored twice. Both rebases resolved this hunk toward `dev`, which
    carries the old name, under a rule aimed at a DIFFERENT hunk — the
    classification entry citing the falsified §12.4 proof. That rule was
    right there and wrong here: this rename is an independent correction
    `dev` had not seen, not a claim resting on the retracted proof. Found
    both times by a colleague reading the delta, not by the rebase.
    """
    assert engram_core.CASCADE_RELATIONS == frozenset(
        {"derives_from", "acknowledged_derives_from"}
    ), (
        f"Expected CASCADE_RELATIONS == {{'derives_from', "
        f"'acknowledged_derives_from'}}, got {engram_core.CASCADE_RELATIONS}"
    )


def test_derives_from_chain_between_cb_nodes_still_cascades():
    """A derives_from chain between two CB nodes still cascades.

    Guards against the derived NCB skip (#2252 Stage 1.3's
    `_walk_cascade_downstream` change) over-firing and exempting
    claim-bearing nodes it should not touch.
    """
    rev, obs, _cs, core = _make_fresh_mods()
    engram_derive = importlib.import_module("engram_derive")

    obs_id = _seed_observation(obs, url="https://example.com/chain-root",
                                claim="Root claim.", quote="Root quote.")

    d1_r = json.loads(engram_derive._derive_impl(
        claim="Derived from root.",
        supporting_ids=obs_id,
        logical_chain="root implies d1",
        reasoning_type="deductive_modus_ponens",
    ))
    assert "error" not in d1_r, d1_r
    d1_id = d1_r["derivation_id"]

    d2_r = json.loads(engram_derive._derive_impl(
        claim="Derived from d1.",
        supporting_ids=d1_id,
        logical_chain="d1 implies d2",
        reasoning_type="deductive_modus_ponens",
    ))
    assert "error" not in d2_r, d2_r
    d2_id = d2_r["derivation_id"]

    ret = json.loads(rev._retract_impl(
        node_id=obs_id,
        error_type="fabricated_quote",
        reason="Root was fabricated.",
     replacement_absent_because="NONE_BECAUSE_UNKNOWN", replacement_absent_note="test fixture -- replacement decision not under test",))
    assert "error" not in ret, ret

    assert _is_tainted(core, d1_id, obs_id), (
        f"Expected d1 {d1_id} to be tainted_by {obs_id} — direct derives_from cascade"
    )
    assert _is_tainted(core, d2_id, obs_id), (
        f"Expected d2 {d2_id} to be tainted_by {obs_id} — derives_from cascade "
        f"must still propagate through a chain of CB nodes"
    )


def test_retract_taints_trust_signal_dependent_via_derives_from():
    """Retracting a CB node whose dependent is a trust_signal (reached via
    derives_from, cascade: True) leaves the trust_signal untainted.

    trust_signal is NCB; the derived skip (core._is_claim_bearing) exempts
    it from the walk the same way it exempts cornerstone/lesson/prediction
    — this is the resolution of the W0 doc's flagged trust_signal cascade
    tension, folded automatically out of the derive-from-partition fix
    rather than requiring its own hand-written exemption.
    """
    rev, obs, _cs, core = _make_fresh_mods()
    engram_trust = importlib.import_module("engram_trust")

    obs_id = _seed_observation(obs, url="https://example.com/trust-source",
                                claim="Trust-relevant claim.", quote="Trust quote.")

    person_r = json.loads(engram_trust._add_person_impl(
        name="Test Person",
    ))
    assert "error" not in person_r, person_r
    pn_id = person_r["person_id"]

    ts_r = json.loads(engram_trust._add_trust_signal_impl(
        subject_pn=pn_id,
        source_obs_id=obs_id,
        claim="Signal claim.",
        kind="consistency",
        polarity=1.0,
        weight=0.5,
    ))
    assert "error" not in ts_r, ts_r
    ts_id = ts_r["trust_signal_id"]

    ret = json.loads(rev._retract_impl(
        node_id=obs_id,
        error_type="fabricated_quote",
        reason="Source was fabricated.",
     replacement_absent_because="NONE_BECAUSE_UNKNOWN", replacement_absent_note="test fixture -- replacement decision not under test",))
    assert "error" not in ret, ret

    assert not _is_tainted(core, ts_id, obs_id), (
        f"Expected trust_signal {ts_id} to NOT be tainted_by {obs_id} — "
        f"trust_signal is NCB and the derived skip must stop the walk there"
    )


# ── 6. #2252 Stage 1.3 round 2 — pin the NCB-skip independently of supported_by ──


def test_retract_taints_prediction_dependent_via_derives_from_reaches_ncb_membership_only():
    """Reach a prediction via `derives_from` (still cascade=True) to isolate
    the NCB-membership skip from `supported_by`'s cascade flag.

    Ariadne's colleague review on PR #2289, finding 2: two mechanisms
    independently protect predictions from taint —
    (a) `supported_by` no longer cascades at all (Stage 1.3 §1), and
    (b) the derived skip stops the walk at any NCB node (Stage 1.3 §2),
    which includes `prediction`. `test_retract_predictive_observation_does_
    not_taint_prediction` (test 3 above) asserts only the outcome, and her
    mutation runs showed either change alone leaves it green — so if
    `supported_by`'s cascade were ever restored, that test would stay green
    while protection had silently halved.

    This test isolates (b): the prediction is linked to the retracted root
    NOT via the product `supported_by` edge (prediction --supported_by-->
    paired observation, which no longer cascades and would trivially pass
    regardless of NCB membership), but via a raw `derives_from` edge
    (prediction --derives_from--> root), a relation that still carries
    cascade=True. No product code path creates a `derives_from` edge into a
    prediction node — inserted directly, mirroring the raw-SQL edge
    fixtures already used for cascade-semantics probes elsewhere (e.g.
    tests/test_cascade_semantics.py's round-trip and supported_by-from-cs
    fixtures). If `_walk_cascade_downstream`'s skip ever regresses to the
    old hand-written ("cornerstone", "lesson") tuple, this test goes RED
    even though `supported_by`'s cascade flag is untouched — the same
    single-mechanism-isolation property the trust_signal tests already have
    (M1-alone reddens all three; mirrored here for prediction).
    """
    rev, obs, _cs, core = _make_fresh_mods()

    root_id = _seed_observation(obs, url="https://example.com/pred-derives-root",
                                 claim="Root premise for the prediction derives_from probe.")

    r = json.loads(obs._add_observation_impl(
        url="https://example.com/pred-derives-paired",
        title="Predictive source (derives_from probe)",
        quoted_text="Signal predicts an upcoming event.",
        interpretation="Signal predicts event.",
        claim="Event will occur (derives_from probe).",
        quote_type="attributed_analysis",
        is_predictive=True,
        predicted_event="Derives_from-probe event occurs within 90 days.",
    ))
    assert "error" not in r, r
    pred_id = r["prediction_id"]

    # Wire prediction --derives_from--> root directly. Not addable via the
    # public engram_add_edge API (derives_from is cascade-bearing and
    # excluded from _ADDABLE_AFTER_CREATION_RELATIONS by design), and no
    # product creation path produces this edge shape for a prediction —
    # this is the deliberate probe fixture, not a real product state.
    conn = core._get_db()
    try:
        conn.execute(
            "INSERT INTO edges (source_id, target_id, relation, created_at) "
            "VALUES (?, ?, 'derives_from', ?)",
            (pred_id, root_id, core._now()),
        )
        conn.commit()
    finally:
        conn.close()

    ret = json.loads(rev._retract_impl(
        node_id=root_id,
        error_type="fabricated_quote",
        reason="Root premise fabricated — derives_from-only cascade probe.",
     replacement_absent_because="NONE_BECAUSE_UNKNOWN", replacement_absent_note="test fixture -- replacement decision not under test",))
    assert "error" not in ret, ret

    assert not _is_tainted(core, pred_id, root_id), (
        f"Expected prediction {pred_id} to NOT be tainted_by {root_id} — "
        f"reached via derives_from (cascade=True), so only the NCB-"
        f"membership skip (not supported_by's cascade flag) can be "
        f"protecting it here (#2252 Stage 1.3 round 2, finding 2)"
    )


