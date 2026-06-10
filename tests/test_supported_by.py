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

# Add tools/migration/ to sys.path so we can import the migration script.
TOOLS_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tools")
MIGRATION_DIR = os.path.join(TOOLS_DIR, "migration")
sys.path.insert(0, MIGRATION_DIR)

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


def test_retract_predictive_observation_taints_prediction():
    """Retracting the observation taints the prediction (not the reverse).

    After the direction fix: prediction --supported_by--> observation.
    Taint cascade: retract observation (target) → taint prediction (source).
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

    # The prediction must be tainted
    assert _is_tainted(core, pred_id, obs_id), (
        f"Expected prediction {pred_id} to be tainted_by {obs_id} after "
        f"retracting its supporting observation"
    )


# ── 4. Migration tests ───────────────────────────────────────────────────────


def _count_edges(core, relation):
    conn = _get_db(core)
    try:
        return conn.execute(
            "SELECT COUNT(*) FROM edges WHERE relation = ?", (relation,)
        ).fetchone()[0]
    finally:
        conn.close()


def _seed_migration_fixture():
    """Set up a fresh DB with two stale 'supports' edges of different kinds.

    Returns (rev, obs, cs, core, cs_id, obs_id, pred_id, obs_predictive_id)
    where:
      - (cs_id, obs_id, 'supported_by') is the CORRECT-direction cornerstone edge
        (but seeded as 'supports' to simulate pre-migration state)
      - (obs_predictive_id, pred_id, 'supports') is the WRONG-direction prediction edge

    We inject these by going through the modules normally (which now creates
    'supported_by') and then raw-SQL downgrade them to 'supports' to simulate
    a pre-migration database.
    """
    rev, obs, cs, core = _make_fresh_mods()

    # 1. Create an observation and cornerstone (writes 'supported_by')
    obs_id = _seed_observation(obs, url="https://example.com/cs-support",
                               claim="Cornerstone premise.")
    cs_r = json.loads(cs._add_cornerstone_impl(
        tag="migration-test",
        title="Migration test cornerstone",
        new_frame="Principle.",
        supporting_ids=obs_id,
    ))
    cs_id = cs_r["cornerstone_id"]

    # 2. Create a predictive observation (writes (pred, obs, 'supported_by'))
    pred_r = json.loads(obs._add_observation_impl(
        url="https://example.com/predictive-mig",
        title="Predictive",
        quoted_text="Signal suggests outcome.",
        interpretation="Signal predicts outcome.",
        claim="Outcome will follow.",
        quote_type="attributed_analysis",
        is_predictive=True,
        predicted_event="Outcome event.",
    ))
    obs_predictive_id = pred_r["observation_id"]
    pred_id = pred_r["prediction_id"]

    # 3. Downgrade both edges back to 'supports' (simulating pre-migration DB)
    db_path = os.path.join(TEST_DIR, "knowledge.db")
    conn = sqlite3.connect(db_path)
    try:
        # Cornerstone edge: currently (cs_id, obs_id, 'supported_by')
        # → downgrade to (cs_id, obs_id, 'supports')
        conn.execute(
            "UPDATE edges SET relation = 'supports' WHERE source_id = ? AND target_id = ? AND relation = 'supported_by'",
            (cs_id, obs_id),
        )
        # Prediction edge: currently (pred_id, obs_predictive_id, 'supported_by')
        # → downgrade to (obs_predictive_id, pred_id, 'supports')  [wrong direction]
        conn.execute(
            "DELETE FROM edges WHERE source_id = ? AND target_id = ? AND relation = 'supported_by'",
            (pred_id, obs_predictive_id),
        )
        conn.execute(
            "INSERT INTO edges (source_id, target_id, relation, created_at) "
            "VALUES (?, ?, 'supports', datetime('now'))",
            (obs_predictive_id, pred_id),
        )
        conn.commit()
    finally:
        conn.close()

    return rev, obs, cs, core, cs_id, obs_id, pred_id, obs_predictive_id


def test_migration_renames_and_flips_correctly():
    """Migration converts both edge types correctly."""
    import migrate_supports_to_supported_by as mig

    rev, obs, cs, core, cs_id, obs_id, pred_id, obs_predictive_id = _seed_migration_fixture()

    db_path = os.path.join(TEST_DIR, "knowledge.db")
    conn = mig._get_db(db_path)
    try:
        # Verify fixture is in pre-migration state
        assert _count_edges(core, "supports") == 2, (
            f"Expected 2 'supports' edges in fixture, got {_count_edges(core, 'supports')}"
        )
        assert _count_edges(core, "supported_by") == 0

        p = mig.plan(conn)
        assert len(p["rename_in_place"]) == 1, f"Expected 1 rename-in-place, got {p['rename_in_place']}"
        assert len(p["flip_and_rename"]) == 1, f"Expected 1 flip-and-rename, got {p['flip_and_rename']}"

        result = mig.apply_migration(conn, p)
        conn.commit()
    finally:
        conn.close()

    # After migration: no 'supports' edges
    assert _count_edges(core, "supports") == 0, (
        f"Expected 0 'supports' edges after migration, got {_count_edges(core, 'supports')}"
    )

    # Cornerstone edge: renamed in place (cs_id, obs_id, 'supported_by')
    assert _edge_exists(core, cs_id, obs_id, "supported_by"), (
        f"Expected (cornerstone={cs_id}, obs={obs_id}, 'supported_by') after migration"
    )

    # Prediction edge: flipped (pred_id, obs_predictive_id, 'supported_by')
    assert _edge_exists(core, pred_id, obs_predictive_id, "supported_by"), (
        f"Expected (pred={pred_id}, obs={obs_predictive_id}, 'supported_by') after migration"
    )
    # Old wrong-direction edge must be gone
    assert not _edge_exists(core, obs_predictive_id, pred_id, "supported_by"), (
        f"Inverted edge (obs, pred) must not exist after migration"
    )

    assert result["renamed_in_place"] == 1
    assert result["flipped"] == 1
    assert result["skipped_collision"] == []


def test_migration_is_idempotent():
    """Running migration twice is a no-op on the second run."""
    import migrate_supports_to_supported_by as mig

    rev, obs, cs, core, cs_id, obs_id, pred_id, obs_predictive_id = _seed_migration_fixture()

    db_path = os.path.join(TEST_DIR, "knowledge.db")

    # First run
    conn = mig._get_db(db_path)
    try:
        p = mig.plan(conn)
        mig.apply_migration(conn, p)
        conn.commit()
    finally:
        conn.close()

    # Second run — should find no 'supports' edges
    conn = mig._get_db(db_path)
    try:
        p2 = mig.plan(conn)
        assert p2["total"] == 0, (
            f"Second migration run found {p2['total']} 'supports' edges — expected 0 (idempotent)"
        )
        result2 = mig.apply_migration(conn, p2)
        conn.commit()
    finally:
        conn.close()

    assert result2["renamed_in_place"] == 0
    assert result2["flipped"] == 0
    assert result2["total_processed"] == 0


def test_migration_does_not_flip_cornerstone_supported_by_prediction():
    """A (cornerstone, prediction, 'supports') edge is renamed in place, not flipped.

    The discriminator keys on target.type='prediction' AND source.type=
    'observation_factual' — precise to the inverted prediction code path. A
    cornerstone that happens to be supported_by a prediction (source=cornerstone)
    is already correct-direction and must only be renamed, never flipped.
    """
    import migrate_supports_to_supported_by as mig

    rev, obs, cs, core = _make_fresh_mods()

    # A prediction node only exists via an is_predictive observation.
    pred_r = json.loads(obs._add_observation_impl(
        url="https://example.com/cs-pred",
        title="Predictive",
        quoted_text="A signal.",
        interpretation="Signal.",
        claim="Outcome.",
        quote_type="attributed_analysis",
        is_predictive=True,
        predicted_event="Outcome event.",
    ))
    assert "error" not in pred_r and pred_r.get("prediction_id"), f"pred creation failed: {pred_r}"
    pred_id = pred_r["prediction_id"]

    # Cornerstone supported_by the prediction (unusual but possible), then
    # downgrade to 'supports' to simulate a pre-migration DB.
    cs_r = json.loads(cs._add_cornerstone_impl(
        tag="cs-pred-test",
        title="Cornerstone grounded in a prediction",
        new_frame="Principle.",
        supporting_ids=pred_id,
    ))
    cs_id = cs_r["cornerstone_id"]

    db_path = os.path.join(TEST_DIR, "knowledge.db")
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "UPDATE edges SET relation = 'supports' WHERE source_id = ? AND target_id = ? AND relation = 'supported_by'",
            (cs_id, pred_id),
        )
        conn.commit()
    finally:
        conn.close()

    conn = mig._get_db(db_path)
    try:
        p = mig.plan(conn)
        # Classified as rename-in-place, NOT flip, despite target.type='prediction'.
        assert (cs_id, pred_id) in p["rename_in_place"], (
            f"(cornerstone, prediction) edge must rename-in-place, got plan={p}"
        )
        assert len(p["flip_and_rename"]) == 0, (
            f"No flip expected for a cornerstone-source edge, got {p['flip_and_rename']}"
        )
        mig.apply_migration(conn, p)
        conn.commit()
    finally:
        conn.close()

    # Edge preserved in correct direction, only renamed.
    assert _edge_exists(core, cs_id, pred_id, "supported_by")
    assert not _edge_exists(core, pred_id, cs_id, "supported_by"), (
        "cornerstone→prediction edge must NOT be flipped"
    )
