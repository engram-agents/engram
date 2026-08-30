"""Tests for engram-alpha#2252 stage 1.6/1.6a — "drop lesson confidence" end
to end: lesson removed from CLAIM_BEARING_TYPES, _add_lesson_impl stops
computing/writing confidence, and a state-based migration backfills existing
lesson rows to NULL.

(Stage 1.7 — the engram_query.py min_confidence claim-bearing-only-predicate
fix — shipped separately as engram-alpha#2261, after a same-day rung
collision: a sibling build was already in flight when this PR's own 1.7 work
was built, so this PR's 1.7 changes were dropped in favor of #2261 rather
than shipping a duplicate. See the PR description for the reconciliation.)

Coverage:
  1. CLAIM_BEARING_TYPES no longer contains 'lesson'.
  2. Lesson nodes are now rejected wherever the CLAIM_BEARING_TYPES guard
     fires "for free" (#2252 activation, no other code change expected):
     derivation premise (supporting_ids), contradiction sides, resolve
     (resolving_node_id), exemplar source (engram_register_exemplar), and
     instantiates edge source.
  3. _add_lesson_impl no longer computes/writes confidence — new lessons
     have confidence=NULL, confidence_history=NULL, and 'confidence' is
     absent from the tool's result dict.
  4. core._count_live_exemplars is unaffected for lesson targets (the
     maturity signal Q2 says to keep as-is).
  5. The state-based DB migration (inside _run_db_one_time_setup) backfills
     EXISTING lesson rows (seeded directly, bypassing _add_lesson_impl, to
     simulate a pre-#2252 write) to NULL confidence/confidence_history, and
     is idempotent.

Import-pattern contract (#948 wave 1 — R1): imports engram_query,
engram_observation, engram_derive, engram_epistemic, engram_revision,
engram_cornerstone, engram_core directly; no ``import server``. Module
objects are passed to fresh_module() (R4) for per-test reload isolation.
"""

import atexit
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_TESTS_DIR = str(Path(__file__).resolve().parent)
if _TESTS_DIR not in sys.path:
    sys.path.insert(0, _TESTS_DIR)
from conftest import fresh_module  # noqa: E402 — must follow sys.path setup

import engram_cornerstone
import engram_core
import engram_derive
import engram_epistemic
import engram_observation
import engram_query
import engram_revision

TEST_DIR = tempfile.mkdtemp(prefix="engram_test_lesson_confidence_ncb_")
atexit.register(lambda: shutil.rmtree(TEST_DIR, ignore_errors=True))


def _fresh_mods():
    """Reload all family modules, configure a clean test DB."""
    if os.path.exists(TEST_DIR):
        shutil.rmtree(TEST_DIR)
    os.environ["ENGRAM_NO_EMBEDDINGS"] = "1"
    fresh_module(engram_query)
    import importlib
    for name in [
        "engram_observation", "engram_revision", "engram_derive",
        "engram_epistemic", "engram_cornerstone",
    ]:
        importlib.import_module(name)
    core = sys.modules["engram_core"]
    core._configure_paths(TEST_DIR)
    core._ensure_data_dir()
    return core


def _mods():
    qry = sys.modules["engram_query"]
    obs = sys.modules["engram_observation"]
    rev = sys.modules["engram_revision"]
    drv = sys.modules["engram_derive"]
    epi = sys.modules["engram_epistemic"]
    cst = sys.modules["engram_cornerstone"]
    core = sys.modules["engram_core"]
    return qry, obs, rev, drv, epi, cst, core


def _add_observation(obs, claim: str, url: str = "https://example.com/test") -> str:
    r = json.loads(obs._add_observation_impl(
        url=url,
        title="Test source",
        quoted_text=claim,
        interpretation=f"Interpretation of: {claim}",
        claim=claim,
        quote_type="hard_data",
    ))
    assert "error" not in r, r
    return r["observation_id"]


def _add_lesson(epi, obs, claim: str = "Test lesson claim") -> str:
    """Create an incident observation + a lesson citing it. Returns lesson_id."""
    inc_id = _add_observation(obs, "Incident: skipped reading the error output.")
    r = json.loads(epi._add_lesson_impl(
        claim=claim,
        incident_ids=inc_id,
        scaffolding_nudge="Read the error output before hypothesizing.",
        logical_chain="Repeated across incidents: skip-read precedes bad hypothesis.",
    ))
    assert "error" not in r, r
    return r["lesson_id"]


# ---------------------------------------------------------------------------
# 1. CLAIM_BEARING_TYPES
# ---------------------------------------------------------------------------

def test_claim_bearing_types_excludes_lesson():
    core = _fresh_mods()
    assert "lesson" not in core.CLAIM_BEARING_TYPES
    # Sanity: the other claim-bearing types are untouched.
    for t in ("observation_factual", "observation_predictive", "derivation",
              "theory", "axiom", "conjecture"):
        assert t in core.CLAIM_BEARING_TYPES


# ---------------------------------------------------------------------------
# 2. Activation of the CLAIM_BEARING_TYPES guard at call sites (#2252 §1)
# ---------------------------------------------------------------------------

def test_lesson_rejected_as_derivation_premise():
    core = _fresh_mods()
    qry, obs, rev, drv, epi, cst, core = _mods()
    lesson_id = _add_lesson(epi, obs)
    r = json.loads(drv._derive_impl(
        claim="Derived from a lesson (should be rejected)",
        supporting_ids=lesson_id,
        logical_chain="lesson implies derived claim",
        reasoning_type="deductive_modus_ponens",
    ))
    assert "error" in r, r
    assert lesson_id in r["error"]


def test_lesson_rejected_as_contradiction_side():
    core = _fresh_mods()
    qry, obs, rev, drv, epi, cst, core = _mods()
    lesson_id = _add_lesson(epi, obs)
    obs_id = _add_observation(obs, "Some claim that conflicts with the lesson.")
    r = json.loads(drv._contradict_impl(
        node_id_a=lesson_id,
        node_id_b=obs_id,
        description="Lesson vs observation (should be rejected)",
    ))
    assert "error" in r, r
    assert lesson_id in r["error"]


def test_lesson_rejected_as_resolver():
    core = _fresh_mods()
    qry, obs, rev, drv, epi, cst, core = _mods()
    lesson_id = _add_lesson(epi, obs)
    r = json.loads(drv._ask_impl(question="What causes the incident pattern?"))
    assert "error" not in r, r
    question_id = r["question_id"]

    r = json.loads(rev._resolve_impl(
        target_id=question_id,
        resolving_node_id=lesson_id,
    ))
    assert "error" in r, r
    assert lesson_id in r["error"]


def test_lesson_rejected_as_exemplar_source():
    core = _fresh_mods()
    qry, obs, rev, drv, epi, cst, core = _mods()
    target_lesson_id = _add_lesson(epi, obs, claim="Target lesson")
    exemplar_lesson_id = _add_lesson(epi, obs, claim="Would-be exemplar lesson")

    r = json.loads(epi._register_exemplar_impl(
        target_id=target_lesson_id,
        exemplar_id=exemplar_lesson_id,
    ))
    assert "error" in r, r
    assert exemplar_lesson_id in r["error"]


def test_lesson_rejected_as_instantiates_source():
    core = _fresh_mods()
    qry, obs, rev, drv, epi, cst, core = _mods()
    lesson_id = _add_lesson(epi, obs)
    r = json.loads(epi._add_axiom_impl(
        claim="An axiom target for instantiates",
        basis="Test fixture basis.",
    ))
    assert "error" not in r, r
    axiom_id = r["axiom_id"]

    r = json.loads(cst._add_edge_impl(
        source_id=lesson_id,
        target_id=axiom_id,
        relation="instantiates",
    ))
    assert "error" in r, r
    assert lesson_id in r["error"]
    assert "claim-bearing" in r["error"]


# ---------------------------------------------------------------------------
# 3. _add_lesson_impl no longer computes/writes confidence
# ---------------------------------------------------------------------------

def test_add_lesson_stores_null_confidence():
    core = _fresh_mods()
    qry, obs, rev, drv, epi, cst, core = _mods()
    lesson_id = _add_lesson(epi, obs)

    conn = core._get_db()
    try:
        row = conn.execute(
            "SELECT confidence, confidence_history FROM nodes WHERE id = ?",
            (lesson_id,),
        ).fetchone()
    finally:
        conn.close()
    assert row["confidence"] is None
    assert row["confidence_history"] in (None, "[]")


def test_add_lesson_result_omits_confidence_key():
    core = _fresh_mods()
    qry, obs, rev, drv, epi, cst, core = _mods()
    inc_id = _add_observation(obs, "Incident: skipped reading the error output.")
    r = json.loads(epi._add_lesson_impl(
        claim="Test lesson claim",
        incident_ids=inc_id,
        scaffolding_nudge="Read the error output before hypothesizing.",
        logical_chain="Repeated across incidents: skip-read precedes bad hypothesis.",
    ))
    assert "error" not in r, r
    assert "confidence" not in r, r


def test_count_live_exemplars_unaffected_for_lesson():
    core = _fresh_mods()
    qry, obs, rev, drv, epi, cst, core = _mods()
    lesson_id = _add_lesson(epi, obs)

    conn = core._get_db()
    try:
        n = core._count_live_exemplars(conn, lesson_id, "lesson")
    finally:
        conn.close()
    assert n == 1  # one incident cited at creation


# ---------------------------------------------------------------------------
# 4. State-based migration backfill (#2252 §3)
# ---------------------------------------------------------------------------

def test_migration_backfills_existing_lesson_rows_to_null():
    """Seed a lesson row with non-NULL confidence DIRECTLY (bypassing
    _add_lesson_impl) to simulate a row written before #2252, then run the
    migration and assert it actually touches that pre-existing row — not
    just newly-created ones (the vacuously-green failure mode #2252 warns
    against)."""
    core = _fresh_mods()
    conn = core._get_db()
    try:
        node_id = core._next_id(conn, "lesson")
        now = core._now()
        conn.execute(
            """INSERT INTO nodes (id, type, claim, created_at, confidence, confidence_history)
               VALUES (?, 'lesson', ?, ?, ?, ?)""",
            (node_id, "Pre-#2252 lesson with a stale confidence", now, 0.73,
             json.dumps([{"timestamp": now, "value": 0.73, "reason": "pre-2252 write"}])),
        )
        conn.commit()

        # Sanity: the seeded row really does have a non-NULL confidence
        # before the migration runs.
        pre = conn.execute(
            "SELECT confidence, confidence_history FROM nodes WHERE id = ?", (node_id,)
        ).fetchone()
        assert pre["confidence"] == 0.73
        assert pre["confidence_history"] is not None

        # Run the state-based migration directly.
        core._run_db_one_time_setup(conn)
        conn.commit()

        post = conn.execute(
            "SELECT confidence, confidence_history FROM nodes WHERE id = ?", (node_id,)
        ).fetchone()
        assert post["confidence"] is None, (
            "migration did not backfill an EXISTING lesson row's confidence to NULL"
        )
        assert post["confidence_history"] is None, (
            "migration did not backfill an EXISTING lesson row's confidence_history to NULL"
        )
    finally:
        conn.close()


def test_migration_is_idempotent():
    core = _fresh_mods()
    conn = core._get_db()
    try:
        node_id = core._next_id(conn, "lesson")
        now = core._now()
        conn.execute(
            """INSERT INTO nodes (id, type, claim, created_at, confidence, confidence_history)
               VALUES (?, 'lesson', ?, ?, ?, ?)""",
            (node_id, "Pre-#2252 lesson", now, 0.5, json.dumps([])),
        )
        conn.commit()

        core._run_db_one_time_setup(conn)
        conn.commit()
        core._run_db_one_time_setup(conn)  # second run must be a no-op, not an error
        conn.commit()

        post = conn.execute(
            "SELECT confidence, confidence_history FROM nodes WHERE id = ?", (node_id,)
        ).fetchone()
        assert post["confidence"] is None
        assert post["confidence_history"] is None
    finally:
        conn.close()


def test_migration_does_not_touch_other_types_confidence():
    """The backfill is scoped to type='lesson' — a claim-bearing node's
    confidence must survive the migration untouched."""
    core = _fresh_mods()
    qry, obs, rev, drv, epi, cst, core = _mods()
    obs_id = _add_observation(obs, "An observation with real confidence.")

    conn = core._get_db()
    try:
        before = conn.execute(
            "SELECT confidence FROM nodes WHERE id = ?", (obs_id,)
        ).fetchone()["confidence"]
        assert before is not None

        core._run_db_one_time_setup(conn)
        conn.commit()

        after = conn.execute(
            "SELECT confidence FROM nodes WHERE id = ?", (obs_id,)
        ).fetchone()["confidence"]
        assert after == before
    finally:
        conn.close()


def test_migration_does_not_re_touch_freshly_created_lesson_rows():
    """Reviewer-fairy finding on PR #2263: the migration must gate on
    `confidence IS NOT NULL` alone, not `confidence OR confidence_history`.

    _add_lesson_impl's INSERT omits confidence_history, so a freshly-created
    lesson row has confidence=NULL (correct, per this PR) but
    confidence_history='[]' (the schema DEFAULT, not NULL). Gating the
    migration on confidence_history too would match that row on every
    startup and flip '[]' to NULL for a row that was never dirty — making
    the migration's own 'idempotent, re-running is a no-op' claim false for
    the post-migration steady state, even though nothing downstream reads
    the difference between NULL and '[]' (functionally harmless, but the
    migration's docstring claim should be true, not just harmless-if-wrong).
    """
    core = _fresh_mods()
    qry, obs, rev, drv, epi, cst, core = _mods()
    incident_id = _add_observation(obs, "Incident for the fresh-row regression fixture.")
    lesson_result = json.loads(epi._add_lesson_impl(
        claim="Fresh-row regression fixture lesson.",
        incident_ids=incident_id,
        scaffolding_nudge="n/a — regression fixture only.",
        logical_chain="n/a — regression fixture only.",
    ))
    lesson_id = lesson_result["lesson_id"]

    conn = core._get_db()
    try:
        before = conn.execute(
            "SELECT confidence, confidence_history FROM nodes WHERE id = ?", (lesson_id,)
        ).fetchone()
        assert before["confidence"] is None
        assert before["confidence_history"] == "[]", (
            "sanity: a freshly-created lesson row's confidence_history is "
            "the schema default '[]', not NULL — this is the state the "
            "migration must not treat as 'needs backfill'"
        )

        core._run_db_one_time_setup(conn)
        conn.commit()

        after = conn.execute(
            "SELECT confidence, confidence_history FROM nodes WHERE id = ?", (lesson_id,)
        ).fetchone()
        assert after["confidence"] is None
        assert after["confidence_history"] == "[]", (
            "migration re-touched a freshly-created lesson row's "
            "confidence_history ('[]' -> NULL) that was never dirty"
        )
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 6. #2252 Stage 1.3 round 2 — `_refuse_ncb_confidence` wiring, demonstrated
#    as a fireable mechanism (not just a fireable function)
#
# Round-1 verification (PR #2289 body) called the guard's *logic* correct
# via a handful of spot checks, but the guard itself had zero test coverage
# — the guard was demonstrated as callable and correct on the cases spot
# checked, never as fired-by-iterating-every-type. Ariadne's colleague
# review (finding 1) named the gap: 14 of 18 real call sites pass a literal
# `None` and can never raise regardless of `_refuse_ncb_confidence`'s own
# correctness, so nothing in the round-1 suite could have caught a broken
# guard. These two tests close that: they call the guard directly across
# the FULL derived membership set (never a hand-listed one), both
# directions.
# ---------------------------------------------------------------------------

def _ncb_types(core):
    """The full non-claim-bearing type set: everything in VALID_NODE_TYPES
    that is not in CLAIM_BEARING_TYPES, plus `trust_signal` — which is
    absent from VALID_NODE_TYPES entirely (a separate tracked gap, #2203)
    but is a real NCB write-path guarded by `_refuse_ncb_confidence`
    (engram_trust.py). Derived, never hand-listed — the whole point of
    this rung is that the test tracks the classification, not a copy of it.
    """
    return (core.VALID_NODE_TYPES - core.CLAIM_BEARING_TYPES) | {"trust_signal"}


def test_refuse_ncb_confidence_rejects_every_ncb_type():
    """Every NCB type raises ValueError when passed a non-NULL confidence.

    Iterates the derived NCB set (see _ncb_types) rather than the 13
    NCB literals inline — re-listing them here would reproduce, in the
    guard's own test, exactly the re-enumeration defect #2252 exists to
    remove.
    """
    core = _fresh_mods()
    ncb_types = _ncb_types(core)
    assert len(ncb_types) >= 12, (
        f"sanity: expected a double-digit NCB set, got {sorted(ncb_types)} "
        f"— VALID_NODE_TYPES or CLAIM_BEARING_TYPES may have changed shape"
    )
    for node_type in sorted(ncb_types):
        try:
            core._refuse_ncb_confidence(node_type, 0.5)
        except ValueError:
            continue
        else:
            raise AssertionError(
                f"_refuse_ncb_confidence('{node_type}', 0.5) did not raise — "
                f"'{node_type}' is NCB and must reject a non-NULL confidence"
            )


def test_refuse_ncb_confidence_accepts_every_cb_type():
    """Every claim-bearing type is ACCEPTED with a valid confidence.

    The mirror direction of the test above. A guard test never seen to
    pass-and-fail on both sides is not a control (round-2 spec, citing
    Ariadne's finding 1) — this side confirms the guard doesn't
    over-fire and start rejecting the types it must let through.
    """
    core = _fresh_mods()
    for node_type in sorted(core.CLAIM_BEARING_TYPES):
        core._refuse_ncb_confidence(node_type, 0.5)  # must not raise
        core._refuse_ncb_confidence(node_type, None)  # NULL is always fine too
