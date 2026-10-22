"""Tests for engram-alpha#2575 PR C0: ``health.model.model_defs_jsonable``,
``health.model.model_from_defs``, and ``health.engine.score_signals``.

Design of record: ``docs/internal/health-series-design.md`` D4.1 ("Versions must be
executable, not merely recorded"). Before this PR, a stored snapshot's
SIGNALS could be re-derived as of the snapshot's own version (via the
already-shipped ``signal_defs``/``curve_defs`` blobs), but its SCORES could
not — nothing in the snapshot declared which curve/aspect/importance each
signal is bound to, or each aspect's points. ``model_defs`` closes that
gap, and ``score_signals`` is the one shared function both the live engine
(``build_snapshot``) and a future re-scorer (PR C1's series deriver) call,
so "the same functions the live engine uses" (design doc §2) is true by
construction.

Fixture strategy: reuses ``tests/test_health_engine.py``'s own
``_NODES_DDL``/``_insert``/``_insert_edge`` fixture shape verbatim (same
precedent ``tests/test_health_series.py`` already follows for its own v2
snapshot fixture) — a populated conn ensures at least one admitted metric
per relevant aspect reads a REAL (non-UNAVAILABLE) score, so the round-trip
mutation tests below actually exercise curve/importance-driven arithmetic
rather than only the "propagate UNAVAILABLE" path an empty conn would give.
"""

from __future__ import annotations

import copy
import json
import os
import sqlite3
import sys
from unittest import mock

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from health import engine as health_engine  # noqa: E402
from health import measurements as health_measurements  # noqa: E402
from health import signals as health_signals  # noqa: E402
from health.model import model_defs_jsonable, model_from_defs  # noqa: E402


# ---------------------------------------------------------------------------
# Fixture (verbatim shape of tests/test_health_engine.py's own fixture --
# see that file's _NODES_DDL/_insert/_insert_edge for the precedent)
# ---------------------------------------------------------------------------

_NODES_DDL = """
CREATE TABLE nodes (
    id              TEXT PRIMARY KEY,
    type            TEXT NOT NULL,
    created_at      TEXT NOT NULL,
    evidence_id     TEXT,
    status          TEXT DEFAULT 'active',
    is_current      INTEGER DEFAULT 1,
    metadata        TEXT DEFAULT '{}',
    recall_summary  TEXT,
    recall_keywords TEXT,
    embedding       TEXT
);

CREATE TABLE edges (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id   TEXT NOT NULL,
    target_id   TEXT NOT NULL,
    relation    TEXT NOT NULL,
    created_at  TEXT NOT NULL
);

CREATE TABLE edit_history (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp   TEXT NOT NULL,
    turn        INTEGER NOT NULL DEFAULT 0,
    action      TEXT NOT NULL,
    node_id     TEXT NOT NULL,
    node_type   TEXT NOT NULL,
    details     TEXT DEFAULT '{}'
);

CREATE TABLE cascade_markers (
    id                      INTEGER PRIMARY KEY AUTOINCREMENT,
    node_id                 TEXT NOT NULL REFERENCES nodes(id),
    cascade_event           TEXT NOT NULL,
    root_node               TEXT NOT NULL,
    root_op                 TEXT NOT NULL CHECK (root_op IN ('retract', 'supersede')),
    status                  TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'resolved')),
    resolution_kind         TEXT,
    resolution_replacement_id TEXT,
    created_at              TEXT NOT NULL,
    resolved_at             TEXT,
    backfill_provenance     TEXT,
    backfill_anomaly        INTEGER NOT NULL DEFAULT 0,
    UNIQUE(node_id, cascade_event)
);
"""


def _make_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(_NODES_DDL)
    return conn


def _insert(conn, id, type, created_at="t0", evidence_id=None, status="active",
            is_current=1, metadata=None, recall_summary=None,
            recall_keywords=None, embedding=None):
    conn.execute(
        "INSERT INTO nodes (id, type, created_at, evidence_id, status, is_current,"
        " metadata, recall_summary, recall_keywords, embedding)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (id, type, created_at, evidence_id, status, is_current,
         json.dumps(metadata) if metadata is not None else "{}",
         recall_summary,
         json.dumps(recall_keywords) if recall_keywords is not None else None,
         embedding),
    )
    conn.commit()


def _insert_edge(conn, source_id, target_id, relation, created_at="t0"):
    conn.execute(
        "INSERT INTO edges (source_id, target_id, relation, created_at) VALUES (?, ?, ?, ?)",
        (source_id, target_id, relation, created_at),
    )
    conn.commit()


def _insert_marker(conn, node_id, root_op="supersede", status="pending",
                    cascade_event="cev_1"):
    """Mirrors tests/test_health_engine.py's own ``_insert_marker`` helper
    (simplified: this file has no age-based diagnostics test to drive an
    ``age_days`` parameter)."""
    conn.execute(
        "INSERT INTO cascade_markers"
        " (node_id, cascade_event, root_node, root_op, status, created_at)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        (node_id, cascade_event, "root_placeholder", root_op, status, "t0"),
    )
    conn.commit()


def _populated_conn() -> sqlite3.Connection:
    """A conn where every admitted metric in epistemic_integrity and
    retrievability reads a REAL (non-UNAVAILABLE) score -- base scenario
    from tests/test_health_engine.py's own
    ``test_populated_graph_produces_real_aspect_scores``, EXTENDED with a
    pending supersede marker on the derivation node so ``pending_premise_
    rate`` (engram-alpha#2905 PR-B; raw 0.5 on this fixture -- dv_1, one of
    two claim-bearing nodes, carries the pending marker) reads a REAL,
    non-trivial score. NOTE (PR-B): epistemic_integrity now has exactly ONE
    admitted metric (pending_premise_rate -- open_contradiction_rate is a
    0.0-importance hold-out, per PR-A) -- the round-trip importance-
    mutation test below drops that SOLE admitted metric, making the whole
    aspect go UNAVAILABLE rather than merely shifting its score between two
    metrics (the pre-PR-B framing, when taint_rate/stale_derivation_rate
    were two separate admitted metrics)."""
    conn = _make_conn()
    _insert(conn, "dv_1", "derivation", metadata={},
            recall_summary="s1", recall_keywords=["k1"],
            embedding=json.dumps([0.1]))
    _insert(conn, "ob_1", "observation_factual", evidence_id="ev_1",
            recall_summary="s2", recall_keywords=["k2"],
            embedding=json.dumps([0.2]))
    _insert(conn, "ev_1", "evidence")
    _insert_edge(conn, "dv_1", "ob_1", "derives_from")
    _insert_marker(conn, "dv_1", root_op="supersede", status="pending")
    return conn


def _build_snapshot_with_embeddings(conn) -> dict:
    # embedding_coverage gates on embedder availability -- the real suite
    # runs with ENGRAM_NO_EMBEDDINGS=1, so without this patch the metric
    # reads UNAVAILABLE regardless of the populated embedding column above
    # (same patch tests/test_health_engine.py's own fixture applies).
    with mock.patch("engram_core._embedder.is_available", return_value=True):
        return health_engine.build_snapshot(conn, "nap")


# ---------------------------------------------------------------------------
# model_defs_jsonable shape
# ---------------------------------------------------------------------------


def test_model_defs_jsonable_shape():
    defs = model_defs_jsonable(health_engine.MODEL)
    assert set(defs.keys()) == {"aspects", "metrics"}
    assert isinstance(defs["aspects"], list)
    assert isinstance(defs["metrics"], list)

    aspect_names = {a["name"] for a in defs["aspects"]}
    for a in defs["aspects"]:
        assert set(a.keys()) == {"name", "points", "reference_metric"}

    for m in defs["metrics"]:
        assert set(m.keys()) == {
            "signal", "aspect", "curve", "importance",
            "detector", "additional_detector", "repair", "dive_target",
        }
        assert m["signal"] in health_signals.SIGNALS
        assert m["curve"] in health_engine.MODEL.curves
        assert m["aspect"] in aspect_names
        if m["detector"] is not None:
            assert set(m["detector"].keys()) == {"name", "population"}
        if m["additional_detector"] is not None:
            assert set(m["additional_detector"].keys()) == {"name", "population"}

    assert sum(a["points"] for a in defs["aspects"]) == 100


def test_model_defs_jsonable_matches_build_snapshot():
    snapshot = _build_snapshot_with_embeddings(_populated_conn())
    assert snapshot["model_defs"] == model_defs_jsonable(health_engine.MODEL)


def test_model_defs_jsonable_json_serializable():
    # Contract item 3 sibling: no callable ever leaks into model_defs.
    json.dumps(model_defs_jsonable(health_engine.MODEL))


# ---------------------------------------------------------------------------
# Round-trip: model_from_defs + score_signals reproduce the live scores
# ---------------------------------------------------------------------------


def _live_l1(conn) -> dict:
    l0 = health_measurements.compute_all_measurements(conn)
    return health_signals.compute_all_signals(l0)


def test_round_trip_reconstructed_model_scores_identically():
    conn = _populated_conn()
    with mock.patch("engram_core._embedder.is_available", return_value=True):
        snapshot = health_engine.build_snapshot(conn, "nap")
        l1 = _live_l1(conn)
        m2 = model_from_defs(snapshot["model_defs"], snapshot["curve_defs"])
        result_reconstructed = health_engine.score_signals(l1, m2)
        result_live = health_engine.score_signals(l1, health_engine.MODEL)
    assert result_reconstructed == result_live


def test_round_trip_knot_mutation_changes_that_signals_score():
    """Named mutation 1: change one knot's y in a COPY of curve_defs before
    reconstructing -> scores["metrics"][<that signal>] differs. Proves the
    reconstructed model is what actually scores -- not a hidden fallback to
    MODEL."""
    conn = _populated_conn()
    with mock.patch("engram_core._embedder.is_available", return_value=True):
        snapshot = health_engine.build_snapshot(conn, "nap")
        l1 = _live_l1(conn)

        target_signal = "pending_premise_rate"
        assert snapshot["signals"][target_signal] != "UNAVAILABLE"
        curve_name = next(
            m["curve"] for m in snapshot["model_defs"]["metrics"]
            if m["signal"] == target_signal
        )

        mutated_curve_defs = copy.deepcopy(snapshot["curve_defs"])
        knots = mutated_curve_defs[curve_name]["knots"]
        # Mutate the knot at x=0.5's y -- pending_premise_rate's raw signal
        # is 0.5 in this fixture (1 of 2 claim-bearing nodes carries the
        # pending marker), which is exactly badness_pending_premise's knot
        # at x=0.5; mutating a knot the raw value does not land on (e.g.
        # the last knot) would silently pass without exercising anything.
        target_x = 0.5
        knot_idx = next(i for i, (x, _y) in enumerate(knots) if x == target_x)
        first_y = knots[knot_idx][1]
        knots[knot_idx][1] = first_y - 0.1 if first_y >= 0.5 else first_y + 0.1

        m2 = model_from_defs(snapshot["model_defs"], mutated_curve_defs)
        _, scores_mutated = health_engine.score_signals(l1, m2)
        _, scores_live = health_engine.score_signals(l1, health_engine.MODEL)

    assert scores_mutated["metrics"][target_signal] != scores_live["metrics"][target_signal]


def test_round_trip_importance_mutation_drops_metric_from_play():
    """Named mutation 2: set one admitted metric's importance to 0.0 in a
    copy of model_defs -> that aspect's score changes and
    aspect_coverage[aspect]["metrics_in_play"] drops the signal.

    NOTE (engram-alpha#2905 PR-B): ``pending_premise_rate`` is now
    epistemic_integrity's SOLE admitted metric (open_contradiction_rate is
    a 0.0-importance hold-out, per PR-A) -- dropping its importance to 0.0
    makes the WHOLE ASPECT go UNAVAILABLE (zero admitted metrics, held out
    and renormalized per the design ruling), a stronger demonstration than
    the pre-PR-B two-admitted-metrics framing (where the aspect score
    merely shifted). The ``!=`` assertion below still holds (a real float
    vs "UNAVAILABLE" are never equal).

    FIX (round 2, engram-alpha#2905 PR-B): this docstring note already
    correctly anticipated the WHOLE-ASPECT-held-out consequence, but the
    original ``target_signal not in scores_mutated["aspect_coverage"]
    [target_aspect]["metrics_in_play"]`` assertion still assumed
    ``aspect_coverage[target_aspect]`` EXISTS in the mutated result -- it
    does not: a held-out aspect has NO entry in ``aspect_coverage`` at all
    (per ``build_snapshot``'s own scoping -- see
    ``TestRealModelCompositionConsequence`` in test_health_engine.py,
    "provenance_integrity has NO entry in aspect_coverage"), so the
    original assertion raised ``KeyError`` rather than testing anything.
    Never actually run before this round -- the import chain round 1's own
    commit message flagged as blocked (health.engine raising at import
    time) prevented this test from executing at all until this round's
    engine.py/model.py fix landed."""
    conn = _populated_conn()
    with mock.patch("engram_core._embedder.is_available", return_value=True):
        snapshot = health_engine.build_snapshot(conn, "nap")
        l1 = _live_l1(conn)

        target_signal = "pending_premise_rate"
        assert snapshot["signals"][target_signal] != "UNAVAILABLE"
        target_aspect = next(
            m["aspect"] for m in snapshot["model_defs"]["metrics"]
            if m["signal"] == target_signal
        )

        mutated_model_defs = copy.deepcopy(snapshot["model_defs"])
        found = False
        for m in mutated_model_defs["metrics"]:
            if m["signal"] == target_signal:
                assert m["importance"] > 0.0
                m["importance"] = 0.0
                found = True
        assert found

        m2 = model_from_defs(mutated_model_defs, snapshot["curve_defs"])
        _, scores_mutated = health_engine.score_signals(l1, m2)
        _, scores_live = health_engine.score_signals(l1, health_engine.MODEL)

    assert scores_mutated["aspects"][target_aspect] == "UNAVAILABLE"
    assert scores_mutated["aspects"][target_aspect] != scores_live["aspects"][target_aspect]
    assert target_signal in scores_live["aspect_coverage"][target_aspect]["metrics_in_play"]
    assert target_aspect not in scores_mutated["aspect_coverage"]


# ---------------------------------------------------------------------------
# Refactor identity: build_snapshot's signals/scores == score_signals(...)
# ---------------------------------------------------------------------------


def test_build_snapshot_signals_scores_equal_score_signals_direct_call():
    conn = _populated_conn()
    with mock.patch("engram_core._embedder.is_available", return_value=True):
        snapshot = health_engine.build_snapshot(conn, "nap")
        l0 = health_measurements.compute_all_measurements(conn)
        l1 = health_signals.compute_all_signals(l0)
        signals, scores = health_engine.score_signals(l1, health_engine.MODEL)
    assert snapshot["signals"] == signals
    assert snapshot["scores"] == scores


# ---------------------------------------------------------------------------
# Serialization
# ---------------------------------------------------------------------------


def test_snapshot_model_defs_serializes():
    snapshot = _build_snapshot_with_embeddings(_populated_conn())
    json.dumps(snapshot["model_defs"])


# ---------------------------------------------------------------------------
# Reduced model (PR C0 round 2, ruling 2) -- score_signals iterates the
# MODEL, not config.aspects: a model missing an aspect HEAD's CONFIG still
# declares must score cleanly over what it DOES declare, never KeyError or
# silently pull in HEAD's aspect. This is the test that fails under round
# 1's config-driven iteration (a bare KeyError on the first metric of the
# removed aspect, since config.aspects still enumerates
# provenance_integrity's metrics and looks up their MetricBinding in the
# reduced model's metric_by_signal, where it no longer exists) -- verified
# by running this test against round 1's code (618b5af5) in a separate
# worktree before this round's fix: it failed with
# ``KeyError: 'uncited_observation_rate'`` from inside score_signals's
# ``binding = metric_by_signal[metric_name]`` lookup.
# ---------------------------------------------------------------------------


def test_reduced_model_scores_only_remaining_aspects_and_metrics():
    conn = _populated_conn()
    with mock.patch("engram_core._embedder.is_available", return_value=True):
        snapshot = health_engine.build_snapshot(conn, "nap")
        l1 = _live_l1(conn)

        # Drop provenance_integrity (and both its metrics) from a COPY of
        # model_defs -- the example the spec names for "one aspect and one
        # metric removed".
        reduced_model_defs = copy.deepcopy(snapshot["model_defs"])
        reduced_model_defs["aspects"] = [
            a for a in reduced_model_defs["aspects"] if a["name"] != "provenance_integrity"
        ]
        reduced_model_defs["metrics"] = [
            m for m in reduced_model_defs["metrics"] if m["aspect"] != "provenance_integrity"
        ]
        m2 = model_from_defs(reduced_model_defs, snapshot["curve_defs"])

        reduced_signals, reduced_scores = health_engine.score_signals(l1, m2)

    assert set(reduced_scores["aspects"].keys()) == {"epistemic_integrity", "retrievability"}
    assert set(reduced_signals.keys()) == {
        m["signal"] for m in reduced_model_defs["metrics"]
    }
    assert "provenance_integrity" not in reduced_scores["aspects"]
    assert "uncited_observation_rate" not in reduced_signals
    assert "dangling_evidence_rate" not in reduced_signals
    # epistemic_integrity (40) + retrievability (35) -- both remain scored
    # (the populated fixture guarantees real, available scores in both) --
    # points_in_play reflects only the REMAINING declared points.
    assert reduced_scores["points_in_play"] == 75.0
    assert reduced_scores["composed"] != "UNAVAILABLE"


# ---------------------------------------------------------------------------
# engram-alpha#2905 PR-A -- v1.2 curve rebindings (direct assertions).
# health-v1.2 (the 2026-09-13 Lei+Kepler curves session): two new curves
# (badness_pending_premise, goodness_coverage) and six metric-binding
# changes in health.model.MODEL_SPEC. Reads straight off health_engine.MODEL
# (this file's own established convention -- see every test above, which
# reads the real production model via health_engine.MODEL, never
# health.model.MODEL_SPEC directly).
# ---------------------------------------------------------------------------


def _metric_by_signal() -> dict:
    return {m.signal: m for m in health_engine.MODEL.metrics}


class TestV1_2CurveRebindings:
    def test_pending_premise_rate_bound_to_pending_premise_curve(self):
        """engram-alpha#2905 PR-B: unifies taint_rate + stale_derivation_rate
        (PR-A's interim per-metric rebind onto badness_pending_premise) into
        ONE signal, pending_premise_rate, bound to the SAME curve at
        importance 1.0 in epistemic_integrity."""
        bindings = _metric_by_signal()
        binding = bindings["pending_premise_rate"]
        assert binding.curve == "badness_pending_premise"
        assert binding.aspect == "epistemic_integrity"
        assert binding.importance == 1.0

    def test_taint_rate_and_stale_derivation_rate_no_longer_registered(self):
        """engram-alpha#2905 PR-B retires both signals -- neither the
        model's metric bindings nor health.signals' SIGNALS/SIGNAL_DEFS
        registries carry them anymore."""
        bindings = _metric_by_signal()
        assert "taint_rate" not in bindings
        assert "stale_derivation_rate" not in bindings
        assert health_signals.SIGNALS.get("taint_rate") is None
        assert health_signals.SIGNALS.get("stale_derivation_rate") is None
        assert "taint_rate" not in health_signals.SIGNAL_DEFS
        assert "stale_derivation_rate" not in health_signals.SIGNAL_DEFS

    def test_open_contradiction_rate_is_held_out(self):
        """Lei's 2026-09-13 ruling: importance 0.0 (HOLD-OUT, informational)
        -- the curve itself stays "badness_default", deliberately NOT
        rebound (unlike taint_rate/stale_derivation_rate above)."""
        binding = _metric_by_signal()["open_contradiction_rate"]
        assert binding.importance == 0.0
        assert binding.curve == "badness_default"

    def test_three_retrievability_coverage_metrics_rebind_with_new_importances(self):
        bindings = _metric_by_signal()
        expected = {
            "embedding_coverage": 1.0,
            "recall_summary_coverage": 0.5,
            "recall_keywords_coverage": 0.25,
        }
        for signal, importance in expected.items():
            binding = bindings[signal]
            assert binding.curve == "goodness_coverage", signal
            assert binding.importance == importance, signal

    def test_untouched_metrics_are_a_control(self):
        """giant_component_fraction, uncited_observation_rate,
        dangling_evidence_rate are explicitly UNCHANGED by this PR
        (importance 0.0, original curve) -- a control, so a future
        accidental rebind of one of these three is caught here."""
        bindings = _metric_by_signal()
        assert bindings["giant_component_fraction"].importance == 0.0
        assert bindings["giant_component_fraction"].curve == "goodness_default"
        assert bindings["uncited_observation_rate"].importance == 0.0
        assert bindings["uncited_observation_rate"].curve == "badness_default"
        assert bindings["dangling_evidence_rate"].importance == 0.0
        assert bindings["dangling_evidence_rate"].curve == "badness_default"

    def test_new_curves_present_with_exact_knots(self):
        """NOTE: ``CURVES`` (the name->CurveSpec registry) is declared in
        health.model, not health.curves (health.curves only ships the bare
        CurveSpec constants + CURVE_PROVENANCE) -- read via
        health_engine.MODEL.curves, this file's own established convention
        for reaching the real production model."""
        assert health_engine.MODEL.curves["badness_pending_premise"].knots == (
            (0.0, 1.0), (0.01, 0.9), (0.05, 0.8), (0.2, 0.5), (0.5, 0.2), (1.0, 0.0),
        )
        assert health_engine.MODEL.curves["goodness_coverage"].knots == (
            (0.0, 0.0), (0.5, 0.05), (0.75, 0.2), (0.95, 0.6), (0.99, 0.9), (1.0, 1.0),
        )

    def test_new_curve_provenance_loaded(self):
        """Confirms the provenance DATA actually loaded from curves.json --
        not just that the keys exist."""
        from health import curves as health_curves

        assert (
            "ratio of premises not available for derivation"
            in health_curves.CURVE_PROVENANCE["badness_pending_premise"]
        )
        assert (
            "100% is the norm"
            in health_curves.CURVE_PROVENANCE["goodness_coverage"]
        )

    def test_health_version_current_is_v2_0_end_to_end(self):
        """health.version.CURRENT == "health-v2.0", confirmed through the
        full model/engine version-stamping path (build_snapshot's
        health_version key) -- mirrors tests/test_health_engine.py's
        ``test_version_stamps_present_and_match_health_version`` (the
        dynamic version of this check, unaffected by any MINOR bump), pinned
        here against the LITERAL v2.0 string so a regression in either
        health.version.MAJOR/MINOR or the snapshot wiring is caught.
        ``model_version`` (health_version.MINOR) is 0, not 2 -- PR-B's MAJOR
        bump resets MINOR (see health/version.py's own docstring)."""
        from health import version as health_version

        assert health_version.CURRENT == "health-v2.0"
        conn = _make_conn()
        snap = health_engine.build_snapshot(conn, "nap")
        assert snap["health_version"] == "health-v2.0"
        assert snap["model_version"] == 0
