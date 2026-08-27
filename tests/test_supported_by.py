"""Tests for the supported_by relation rename and direction fix (PR #406).

Coverage:
  1. Taint cascade — retracting the TARGET of a supported_by edge taints the SOURCE.
  2. Taint cascade (inverse not fired) — retracting the SOURCE does not taint the TARGET.
  3. Prediction path post-fix — is_predictive observation creates
     (prediction, observation, 'supported_by'); retracting the observation
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
    """Return True if node_id has tainted_by containing by_id in its metadata."""
    conn = _get_db(core)
    try:
        row = conn.execute("SELECT metadata FROM nodes WHERE id = ?", (node_id,)).fetchone()
        if row is None:
            return False
        meta = json.loads(row["metadata"] or "{}")
        return by_id in meta.get("tainted_by", [])
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
    ))
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
    ))
    assert "error" not in ret, ret

    # The observation (target / premise) must NOT be tainted
    assert not _is_tainted(core, obs_id, cs_id), (
        f"Observation {obs_id} must NOT be tainted by retracting its dependent {cs_id}"
    )


# ── 3. Prediction path post-fix ─────────────────────────────────────────────


def test_predictive_observation_creates_correct_supported_by_edge():
    """An is_predictive observation creates (prediction, observation, 'supported_by').

    The prediction is the dependent (source); the observation is the premise (target).
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

    # Direction must be: prediction (dependent/source) supported_by observation (premise/target)
    assert _edge_exists(core, pred_id, obs_id, "supported_by"), (
        f"Expected (prediction={pred_id}, obs={obs_id}, 'supported_by') edge; "
        f"inverse or 'supports' would be wrong."
    )
    # The old wrong direction must NOT exist
    assert not _edge_exists(core, obs_id, pred_id, "supported_by"), (
        f"Old inverted edge (obs={obs_id}, prediction={pred_id}) must not exist"
    )
    assert not _edge_exists(core, obs_id, pred_id, "supports"), (
        f"Stale 'supports' edge must not exist"
    )


def test_retract_predictive_observation_does_not_taint_prediction():
    """Retracting the paired observation does NOT taint the prediction.

    #2252 Stage 1.3: `supported_by` no longer carries cascade, and
    `prediction` is NCB (Q1 FINAL, Kepler's corrected #2281 ruling,
    2026-08-27T00:46:05Z) — the walk stops at the prediction by partition
    membership before it would even reach this edge. This is a real
    exemption the partition grants, not a gap: the taint-walk's prior
    behavior (asserted by this test's predecessor, pre-1.3) was itself the
    defect Stage 1.3 removes.

    Written in the BARE form deliberately — "the prediction is not
    tainted," not "not tainted via supported_by." The narrower
    via-supported_by wording (from #2283) rested on a since-retracted
    ruling and is withdrawn; do not reintroduce it.

    This is NOT the whole story: a prediction whose paired observation is
    retracted still needs an orphan marker (#2281's proposed fix, not yet
    built) — the taint-walk was quietly doing that job before Stage 1.3 and
    no longer does. This test does not assert that gap is fine; it only
    asserts the mechanism that used to (mis)cover it is gone.
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
    ))
    assert "error" not in ret, ret

    # The prediction must NOT be tainted — bare form (#2252 Stage 1.3).
    assert not _is_tainted(core, pred_id, obs_id), (
        f"Expected prediction {pred_id} to NOT be tainted_by {obs_id} — "
        f"predictions are NCB and supported_by no longer cascades (#2252 Stage 1.3)"
    )


# ── 5. #2252 Stage 1.3 — derived NCB cascade skip ───────────────────────────


def test_cascade_relations_is_derives_from_only():
    """CASCADE_RELATIONS == {'derives_from'} after Stage 1.3.

    Asserted directly so a future flag flip (e.g. re-adding cascade to
    supported_by, or adding a new cascade-bearing relation) is loud rather
    than silently changing cascade-walk behavior with no test to catch it.
    """
    assert engram_core.CASCADE_RELATIONS == frozenset({"derives_from"}), (
        f"Expected CASCADE_RELATIONS == {{'derives_from'}}, got "
        f"{engram_core.CASCADE_RELATIONS}"
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
    ))
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
    ))
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
    ))
    assert "error" not in ret, ret

    assert not _is_tainted(core, pred_id, root_id), (
        f"Expected prediction {pred_id} to NOT be tainted_by {root_id} — "
        f"reached via derives_from (cascade=True), so only the NCB-"
        f"membership skip (not supported_by's cascade flag) can be "
        f"protecting it here (#2252 Stage 1.3 round 2, finding 2)"
    )


