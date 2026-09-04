"""Tests for health/engine.py — the real production CONFIG, the [PR5]
dream-side scoring model wiring, composition, and the snapshot writer
(engram-alpha#1991, PR2+PR3+PR5 of 5).

Covers:
  1. CONFIG validates (weights sum to 100 across A/B/C, all real as of PR3)
     and its aspect shape matches the design doc (weight 40/25/35).
  2. [PR5, rewritten] compose_aspect_scores / _score_aspect — the NEW
     weighted-arithmetic-sum formula (Kepler's draft-2 clarification 1),
     UNAVAILABLE propagation (including the zero-admitted-metrics case),
     and the load-bearing discovered consequence for provenance_integrity
     under the real MODEL (see health/engine.py's module docstring).
  3. build_snapshot's full dict shape (schema_version, engine_version,
     config, definitions, signals, scores, red_flags, diagnostics, anchor,
     turn, checkpoint_mode) against a real (throwaway) SQLite conn.
  4. Contract item 3 — UNAVAILABLE serializes as the JSON string literal
     "UNAVAILABLE", never coerced to a number (no -1/null sentinel).
  5. write_snapshot's on-disk persistence + best-effort failure handling,
     [PR5] plus the dream-side projection's own persistence + P2
     aspect-blindness (negative-space assertions).
  6. PR3: the diagnostics block (activity metrics + cascade-marker age) —
     never wired into scoring, populated for real now.

Reuses tests/test_health_engine_metrics.py's fixture DB shape (a subset of
engram_core.py's real `nodes`/`edges`/`edit_history` table columns).
"""

from __future__ import annotations

import importlib
import json
import os
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from unittest import mock

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import engram_core  # noqa: E402
from health import engine as health_engine  # noqa: E402
from health import measurements as health_measurements  # noqa: E402
from health import metrics as hem  # noqa: E402
from health import signals as health_signals  # noqa: E402
from health import version as health_version  # noqa: E402
from health.config import UNAVAILABLE, AspectDef, EngineConfig, MetricDef  # noqa: E402
from health.curves import CurveSpec  # noqa: E402
from health.model import (  # noqa: E402
    AspectSpec,
    DetectorRef,
    MetricBinding,
    ScoringModel,
)


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

-- health.metrics.stale_derivation_rate/taint_rate query this table
-- (cascade machine, #1991 sec1 re-point) -- present here so any test that
-- exercises build_snapshot() over a real derivation/claim-bearing node
-- doesn't hit sqlite3.OperationalError: no such table. Mirrors
-- test_health_engine_metrics.py's own fixture DDL exactly; no test in this
-- file inserts a row into it (none of these tests need a node to actually
-- carry a marker), so no matching _insert_marker() helper is added here --
-- only the table needs to exist for the JOIN to resolve to zero rows.
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


_marker_seq = 0


def _insert_marker(conn, node_id, root_op="supersede", status="pending", age_days=0,
                    cascade_event=None):
    """Insert a cascade_markers row for node_id, created_at set age_days ago
    (age_days=0 -> just now). Mirrors test_health_engine_metrics.py's own
    _insert_marker helper; this file needs an age-controllable created_at
    rather than an explicit ISO string, since _cascade_marker_age_diagnostics
    computes age from wall-clock time (see #1991 sec1 / #2205 review)."""
    global _marker_seq
    _marker_seq += 1
    if cascade_event is None:
        cascade_event = f"cev_{_marker_seq}"
    created_at = (datetime.now(timezone.utc) - timedelta(days=age_days)).isoformat()
    conn.execute(
        "INSERT INTO cascade_markers"
        " (node_id, cascade_event, root_node, root_op, status, created_at)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        (node_id, cascade_event, "root_placeholder", root_op, status, created_at),
    )
    conn.commit()


# ---------------------------------------------------------------------------
# 1. CONFIG shape + validation
# ---------------------------------------------------------------------------


class TestConfigShape:
    def test_config_is_an_engine_config(self):
        assert isinstance(health_engine.CONFIG, EngineConfig)

    def test_three_aspects_with_design_doc_weights(self):
        weights = {a.name: a.weight for a in health_engine.CONFIG.aspects}
        assert weights == {
            "epistemic_integrity": 40.0,
            "provenance_integrity": 25.0,
            "retrievability": 35.0,
        }

    def test_epistemic_integrity_has_three_metrics(self):
        aspect = next(a for a in health_engine.CONFIG.aspects if a.name == "epistemic_integrity")
        assert {m.name for m in aspect.metrics} == {
            "stale_derivation_rate",
            "taint_rate",
            "open_contradiction_rate",
        }

    def test_provenance_integrity_has_two_metrics(self):
        aspect = next(a for a in health_engine.CONFIG.aspects if a.name == "provenance_integrity")
        assert {m.name for m in aspect.metrics} == {
            "uncited_observation_rate",
            "dangling_evidence_rate",
        }

    def test_retrievability_has_four_real_metrics(self):
        """Aspect B is real as of PR3 — the CONFIG ships the 4 real
        coverage/component metrics, not the PR2 stub."""
        aspect = next(a for a in health_engine.CONFIG.aspects if a.name == "retrievability")
        assert {m.name for m in aspect.metrics} == {
            "recall_summary_coverage",
            "recall_keywords_coverage",
            "embedding_coverage",
            "giant_component_fraction",
        }

    def test_all_metrics_are_scored_and_state_valued(self):
        for aspect in health_engine.CONFIG.aspects:
            for metric in aspect.metrics:
                assert metric.scored is True
                assert metric.state_valued is True


# ---------------------------------------------------------------------------
# 2. [PR5] compose_aspect_scores / _score_aspect — the weighted-arithmetic-
# sum formula (Kepler's draft-2 clarification 1). Fake ScoringModel
# fixtures, never the real 9-metric MODEL (mirrors the OLD _fake_config's
# own precedent, updated for the new model shape).
# ---------------------------------------------------------------------------


def _fake_curve(fn=None):
    return CurveSpec(
        name="fake",
        curve_fn=fn or (lambda x: 1.0 - x),
        input_domain=(0.0, 1.0),
        monotone_direction="decreasing",
    )


_FAKE_POP = "pop"


def _fake_model(points_a=60.0, points_b=40.0):
    """A minimal two-aspect, two-metric ScoringModel: one admitted metric
    per aspect, sharing one trivial curve (``curve_fn(x) = 1 - x``)."""
    def _sig(conn):
        return 0.0

    curves = {"fake": _fake_curve()}
    signals = {"sa": _sig, "sb": _sig}
    metric_a = MetricBinding(
        signal="sa", aspect="a", curve="fake", importance=1.0,
        detector=DetectorRef(name="reflect", population=_FAKE_POP), repair="tool:resolve",
    )
    metric_b = MetricBinding(
        signal="sb", aspect="b", curve="fake", importance=1.0,
        detector=DetectorRef(name="reflect", population=_FAKE_POP), repair="tool:resolve",
    )
    aspect_a = AspectSpec(name="a", points=points_a)
    aspect_b = AspectSpec(name="b", points=points_b)
    return ScoringModel(
        curves=curves, signals=signals,
        aspects=(aspect_a, aspect_b), metrics=(metric_a, metric_b),
    )


def _fake_model_three_aspects(points=(50.0, 30.0, 20.0)):
    """A three-aspect, three-metric ScoringModel — one admitted metric per
    aspect — used specifically to make the renormalization arithmetic
    (Σ fractions over SCORED aspects == 1.0, with a held-out aspect's
    points never appearing anywhere in the sum) legible with more than one
    scored aspect remaining."""
    def _sig(conn):
        return 0.0

    curves = {"fake": _fake_curve()}
    signals = {"sa": _sig, "sb": _sig, "sc": _sig}
    names = ("a", "b", "c")
    metrics = tuple(
        MetricBinding(
            signal=f"s{n}", aspect=n, curve="fake",
            importance=1.0, detector=DetectorRef(name="reflect", population=_FAKE_POP),
            repair="tool:resolve",
        )
        for n in names
    )
    aspects = tuple(AspectSpec(name=n, points=p) for n, p in zip(names, points))
    return ScoringModel(curves=curves, signals=signals, aspects=aspects, metrics=metrics)


class TestComposeAspectScores:
    def test_all_aspects_perfect_composes_to_100(self):
        model = _fake_model()
        composed = health_engine.compose_aspect_scores(model, {"a": 1.0, "b": 1.0})
        assert composed == pytest.approx(100.0)

    def test_unavailable_aspect_is_held_out_and_renormalized(self):
        """BEHAVIOR CHANGE (round 2 of engram-alpha#2520, "hold out,
        renormalize, report", generalized by addendum 2 to cover BOTH the
        structural zero-admitted case and this runtime case): an
        UNAVAILABLE aspect score is no longer propagated to the composed
        score — it is HELD OUT, and the remaining aspect's points are
        renormalized to fill the full 100. Here aspect "b" (40 points) is
        held out, so aspect "a" alone (60 points) is renormalized to
        100/100 of the composed sum: composed = (60/60) * 1.0 * 100 =
        100.0 — NOT the old propagate-to-UNAVAILABLE behavior, and NOT the
        naive un-renormalized (60/100)*1.0*100 = 60.0 either."""
        model = _fake_model(points_a=60.0, points_b=40.0)
        composed = health_engine.compose_aspect_scores(model, {"a": 1.0, "b": UNAVAILABLE})
        assert composed == pytest.approx(100.0)

    def test_all_aspects_unavailable_returns_unavailable(self):
        """The one remaining UNAVAILABLE case: EVERY aspect held out ->
        points_in_play == 0 -> nothing anywhere to compose over — direct
        analogy with _score_aspect's own "nothing to sum over is
        UNAVAILABLE" rule, one level up."""
        model = _fake_model()
        composed = health_engine.compose_aspect_scores(
            model, {"a": UNAVAILABLE, "b": UNAVAILABLE}
        )
        assert composed is UNAVAILABLE

    def test_renormalization_arithmetic_excluded_points_never_appear(self):
        """The renormalization arithmetic itself, spec'd explicitly: a
        scored-aspects-only weighted sum where the EXCLUDED aspect's points
        (aspect "c", 20 points, held out) don't appear anywhere in the
        math. Σ renormalized fractions over the two SCORED aspects sums to
        1.0, and composed lands in [0,100]."""
        model = _fake_model_three_aspects(points=(50.0, 30.0, 20.0))
        aspect_scores = {"a": 0.8, "b": 0.4, "c": UNAVAILABLE}
        unscored, points_in_play = health_engine._composition_coverage(model, aspect_scores)
        assert unscored == ["c"]
        assert points_in_play == pytest.approx(80.0)  # 50 + 30, NOT 100

        fractions = [
            aspect.points / points_in_play
            for aspect in model.aspects
            if aspect.name not in unscored
        ]
        assert sum(fractions) == pytest.approx(1.0)

        composed = health_engine.compose_aspect_scores(model, aspect_scores)
        expected = (50.0 / 80.0) * 0.8 * 100.0 + (30.0 / 80.0) * 0.4 * 100.0
        assert composed == pytest.approx(expected)
        assert 0.0 <= composed <= 100.0
        # The naive (un-renormalized, divide-by-100) answer would have been
        # different -- confirms c's 20 points are genuinely held OUT, not
        # silently folded back in as a zero contribution.
        naive = (50.0 / 100.0) * 0.8 * 100.0 + (30.0 / 100.0) * 0.4 * 100.0
        assert composed != pytest.approx(naive)


class TestCompositionCoverage:
    def test_no_unscored_aspects_uses_full_100_points(self):
        model = _fake_model(points_a=60.0, points_b=40.0)
        unscored, points_in_play = health_engine._composition_coverage(
            model, {"a": 1.0, "b": 1.0}
        )
        assert unscored == []
        assert points_in_play == pytest.approx(100.0)

    def test_unscored_list_matches_unavailable_aspect_scores(self):
        model = _fake_model_three_aspects()
        unscored, points_in_play = health_engine._composition_coverage(
            model, {"a": 1.0, "b": UNAVAILABLE, "c": UNAVAILABLE}
        )
        assert unscored == ["b", "c"]
        assert points_in_play == pytest.approx(50.0)  # only "a"'s points

    def test_aspect_declaration_order_preserved(self):
        """JUDGMENT CALL (flagged in the PR5-round-2 report): unscored_
        aspects reports MODEL declaration order, not alphabetical."""
        model = _fake_model_three_aspects()
        unscored, _ = health_engine._composition_coverage(
            model, {"a": UNAVAILABLE, "b": 1.0, "c": UNAVAILABLE}
        )
        assert unscored == ["a", "c"]  # MODEL order (a, b, c), not sorted


class TestComposeAspectScoresArithmetic:
    """Split out from TestComposeAspectScores above only for a clean file
    structure after round 2's insertions — same class of test (compose_
    aspect_scores' own arithmetic), UNCHANGED from PR5 round 1, since
    _fake_model() never produces an unscored aspect in these particular
    fixtures (both aspects always carry a real, non-UNAVAILABLE score)."""

    def test_weighted_sum_arithmetic_shape(self):
        """Direct arithmetic check of clarification 1's formula: composed =
        Σ (points_a/100) × aspect_score_a × 100."""
        model = _fake_model(points_a=40.0, points_b=60.0)
        composed = health_engine.compose_aspect_scores(model, {"a": 0.5, "b": 0.8})
        assert composed == pytest.approx(40.0 * 0.5 + 60.0 * 0.8)

    def test_no_flooring_applied_at_this_level(self):
        """BEHAVIOR CHANGE (flagged in the PR5 report): the OLD
        compose_aspect_scores floored each aspect score at 1 before
        composing (avoiding the zero singularity for its geometric mean).
        The NEW arithmetic-sum formula has no floor at THIS level — where
        flooring happens at all, it now lives inside the CURVE
        (badness_default's own 0.01 floor) that produced the aspect_score
        upstream; compose_aspect_scores itself sums whatever aspect_scores
        it is given, including a literal 0.0, unmodified."""
        model = _fake_model(points_a=50.0, points_b=50.0)
        composed = health_engine.compose_aspect_scores(model, {"a": 0.0, "b": 1.0})
        assert composed == pytest.approx(50.0)

    def test_weighted_toward_the_higher_points_aspect(self):
        """A collapsed HIGH-points aspect drags the composed score down more
        than a collapsed LOW-points aspect would (design doc §3: 'dead
        retrievability with perfect integrity reads in the 30s, not 65') —
        same qualitative property as before, now via points not weight."""
        model = _fake_model(points_a=80.0, points_b=20.0)
        collapsed_high_points = health_engine.compose_aspect_scores(
            model, {"a": 0.0, "b": 1.0}
        )
        model2 = _fake_model(points_a=20.0, points_b=80.0)
        collapsed_low_points = health_engine.compose_aspect_scores(
            model2, {"a": 0.0, "b": 1.0}
        )
        assert collapsed_high_points < collapsed_low_points


class TestScoreAspect:
    def test_weighted_average_of_admitted_metrics(self):
        """Two admitted metrics with DIFFERENT importance, different raw
        signals, verified against the formula by hand."""
        curve = _fake_curve()  # curve_fn(x) = 1 - x
        model = ScoringModel(
            curves={"fake": curve},
            signals={"sa": lambda c: 0.0, "sb": lambda c: 0.0},
            aspects=(AspectSpec(name="a", points=100.0),),
            metrics=(
                MetricBinding(signal="sa", aspect="a", curve="fake",
                              importance=3.0, detector=DetectorRef(name="reflect", population=_FAKE_POP),
                              repair="tool:resolve"),
                MetricBinding(signal="sb", aspect="a", curve="fake",
                              importance=1.0, detector=DetectorRef(name="reflect", population=_FAKE_POP),
                              repair="tool:resolve"),
            ),
        )
        raw = {"sa": 0.2, "sb": 0.8}  # curve(0.2)=0.8, curve(0.8)=0.2
        score = health_engine._score_aspect("a", raw, model)
        # weighted avg = (3*0.8 + 1*0.2) / 4 = 2.6/4 = 0.65
        assert score == pytest.approx(0.65)

    def test_zero_admitted_metrics_is_unavailable(self):
        """LOAD-BEARING CASE (see health/engine.py's module docstring): an
        aspect whose every metric is importance=0 (diagnostic-only) has
        nothing to sum over — UNAVAILABLE, not zero, not skipped."""
        model = ScoringModel(
            curves={"fake": _fake_curve()},
            signals={"sa": lambda c: 0.0},
            aspects=(AspectSpec(name="a", points=100.0),),
            metrics=(
                MetricBinding(signal="sa", aspect="a", curve="fake",
                              importance=0.0),
            ),
        )
        score = health_engine._score_aspect("a", {"sa": 0.0}, model)
        assert score is UNAVAILABLE

    def test_sole_admitted_metric_unavailable_holds_out_the_aspect(self):
        """When an aspect has exactly ONE admitted metric and it is
        UNAVAILABLE this run, zero admitted metrics remain AVAILABLE —
        same "hold out" treatment as the zero-admitted-at-all case (round 2
        of engram-alpha#2520, addendum 2's generalization), not a
        "propagation" distinct from it."""
        model = _fake_model()
        raw = {"sa": UNAVAILABLE, "sb": 0.0}
        score = health_engine._score_aspect("a", raw, model)
        assert score is UNAVAILABLE

    def test_unavailable_non_admitted_metric_does_not_propagate(self):
        """A NON-admitted metric being UNAVAILABLE must NOT drag the aspect
        down — it was never in the sum (clarification 1's propagation rule
        is scoped to ADMITTED metrics only)."""
        model = ScoringModel(
            curves={"fake": _fake_curve()},
            signals={"sa": lambda c: 0.0, "diag": lambda c: 0.0},
            aspects=(AspectSpec(name="a", points=100.0),),
            metrics=(
                MetricBinding(signal="sa", aspect="a", curve="fake",
                              importance=1.0, detector=DetectorRef(name="reflect", population=_FAKE_POP),
                              repair="tool:resolve"),
                MetricBinding(signal="diag", aspect="a", curve="fake",
                              importance=0.0),
            ),
        )
        raw = {"sa": 0.0, "diag": UNAVAILABLE}
        score = health_engine._score_aspect("a", raw, model)
        assert score == pytest.approx(1.0)  # curve(0.0) = 1.0, unaffected by diag

    # -------------------------------------------------------------------
    # [round 2, addendum 2] Metric-level hold-out-and-renormalize — a
    # PARTIAL availability case (some but not all admitted metrics
    # unavailable this run) renormalizes rather than propagating.
    # -------------------------------------------------------------------

    def _two_admitted_metric_model(self):
        curve = _fake_curve()  # curve_fn(x) = 1 - x
        return ScoringModel(
            curves={"fake": curve},
            signals={"sa": lambda c: 0.0, "sb": lambda c: 0.0},
            aspects=(AspectSpec(name="a", points=100.0),),
            metrics=(
                MetricBinding(signal="sa", aspect="a", curve="fake",
                              importance=3.0, detector=DetectorRef(name="reflect", population=_FAKE_POP),
                              repair="tool:resolve"),
                MetricBinding(signal="sb", aspect="a", curve="fake",
                              importance=1.0, detector=DetectorRef(name="reflect", population=_FAKE_POP),
                              repair="tool:resolve"),
            ),
        )

    def test_partial_availability_renormalizes_not_propagates(self):
        """BEHAVIOR CHANGE (round 2, addendum 2): TWO admitted metrics, ONE
        currently unavailable — the aspect score renormalizes over the
        remaining available metric alone, rather than propagating to
        UNAVAILABLE. sa (importance 3) is unavailable; sb (importance 1,
        raw=0.8, curve(0.8)=0.2) is the only one left, so aspect_score ==
        curve(0.8) == 0.2 exactly (not the two-metric weighted average)."""
        model = self._two_admitted_metric_model()
        raw = {"sa": UNAVAILABLE, "sb": 0.8}
        score = health_engine._score_aspect("a", raw, model)
        assert score == pytest.approx(0.2)

    def test_metric_level_extensibility_no_special_casing_by_name(self):
        """Extensibility, proved rather than asserted (per the round-2
        spec's explicit ask, mirrored one level down from the aspect-level
        version in TestZeroAdmittedAspectExtensibility below): the SAME
        two-admitted-metric model, unavailable then available again,
        automatically renormalizes/re-includes with NO special-casing of
        either metric's name anywhere in _score_aspect/_aspect_metric_
        coverage."""
        model = self._two_admitted_metric_model()

        raw_before = {"sa": UNAVAILABLE, "sb": 0.8}
        metrics_in_play_before, coverage_before = health_engine._aspect_metric_coverage(
            "a", raw_before, model
        )
        assert metrics_in_play_before == ["sb"]
        assert coverage_before == pytest.approx(1.0 / 4.0)  # 1 of (3+1) importance

        raw_after = {"sa": 0.2, "sb": 0.8}
        metrics_in_play_after, coverage_after = health_engine._aspect_metric_coverage(
            "a", raw_after, model
        )
        assert metrics_in_play_after == ["sa", "sb"]
        assert coverage_after == pytest.approx(1.0)

        score_after = health_engine._score_aspect("a", raw_after, model)
        assert score_after == pytest.approx(0.65)  # back to the full weighted average


class TestRealModelCompositionConsequence:
    """The real CONFIG/MODEL — RESOLVED (round 2 of engram-alpha#2520,
    "hold out, renormalize, report"): provenance_integrity has ZERO
    admitted metrics under the v1 table (STILL true — see
    tests/test_health_engine_model.py's own model-level version of this
    fact), but this NO LONGER makes the composed score structurally,
    always UNAVAILABLE — its 25 points are held out and composed is
    renormalized over the remaining 75. Supersedes this class's PR5-round-1
    version, which asserted the OPPOSITE (composed IS UNAVAILABLE) — that
    was the discovered consequence THIS round's design ruling resolves;
    see health/engine.py's module docstring "RESOLVED" section."""

    def test_composed_is_a_real_number_renormalized_over_scored_aspects(self):
        conn = _make_conn()
        # Give every OTHER admitted metric (epistemic_integrity,
        # retrievability) a perfect, non-UNAVAILABLE signal, INCLUDING a
        # resolved contradiction so open_contradiction_rate (round 2's
        # fold #2: UNAVAILABLE on an EMPTY population, not just an open
        # one) has a non-empty population to read a real rate from.
        _insert(conn, "dv_1", "derivation", metadata={},
                recall_summary="s1", recall_keywords=["k1"],
                embedding=json.dumps([0.1]))
        _insert(conn, "ob_1", "observation_factual", evidence_id="ev_1",
                recall_summary="s2", recall_keywords=["k2"],
                embedding=json.dumps([0.2]))
        _insert(conn, "ev_1", "evidence")
        _insert_edge(conn, "dv_1", "ob_1", "derives_from")
        _insert(conn, "ct_1", "contradiction", status="resolved")

        with mock.patch("engram_core._embedder.is_available", return_value=True):
            snap = health_engine.build_snapshot(conn, "nap")

        assert isinstance(snap["scores"]["aspects"]["epistemic_integrity"], float)
        assert isinstance(snap["scores"]["aspects"]["retrievability"], float)
        # provenance_integrity's OWN score is still UNAVAILABLE (nothing to
        # measure) -- that determination is unchanged; what changed is
        # whether it PROPAGATES to composed.
        assert snap["scores"]["aspects"]["provenance_integrity"] == "UNAVAILABLE"
        assert isinstance(snap["scores"]["composed"], float)
        assert 0.0 <= snap["scores"]["composed"] <= 100.0
        assert snap["scores"]["composed"] == pytest.approx(100.0)  # every scored metric perfect

        assert snap["scores"]["unscored_aspects"] == ["provenance_integrity"]
        assert snap["scores"]["points_in_play"] == pytest.approx(75.0)
        assert snap["scores"]["composed_coverage"] == pytest.approx(0.75)
        # provenance_integrity has NO entry in aspect_coverage (unscored,
        # not scored -- see build_snapshot's own docstring for the scoping).
        assert "provenance_integrity" not in snap["scores"]["aspect_coverage"]
        assert set(snap["scores"]["aspect_coverage"].keys()) == {
            "epistemic_integrity", "retrievability",
        }
        assert snap["scores"]["aspect_coverage"]["epistemic_integrity"]["coverage"] == pytest.approx(1.0)


class TestZeroAdmittedAspectExtensibility:
    """Extensibility, proved rather than asserted (round-2 spec's explicit
    ask, item 4): the moment provenance_integrity gains an admitted metric
    (importance > 0, valid detector+repair), it automatically re-enters
    composition on the NEXT model construction — no special-casing of
    "provenance_integrity" by name anywhere in health/engine.py's
    composition logic or health/model.py's validator. Builds a
    MODIFIED model the same way tests/test_health_engine_model.py already
    builds adversarial/fake models — via dataclasses.replace off the REAL
    MODEL (round 3: ``health_engine.MODEL``, the validated model — no
    longer importable ready-made from health/model.py, see that
    module's own docstring), never a hand-rewritten copy of the 9-metric
    table (which would risk silently drifting from the real shape)."""

    def _model_with_uncited_observation_rate_admitted(self):
        import dataclasses

        from health.engine import MODEL as real_model

        new_metrics = tuple(
            dataclasses.replace(
                m,
                importance=1.0,
                # detector unchanged — uncited_observation_rate's real
                # binding already carries a valid detector (DetectorRef(
                # name="reflect", population="observations")), so this
                # metric only needs importance/repair flipped to become
                # admitted; round 3 removed MetricBinding.population, which
                # this override used to (redundantly) re-derive.
                repair="tool:resolve",
            )
            if m.signal == "uncited_observation_rate"
            else m
            for m in real_model.metrics
        )
        return dataclasses.replace(real_model, metrics=new_metrics)

    def test_modified_model_no_longer_names_provenance_integrity_unscored(self):
        model = self._model_with_uncited_observation_rate_admitted()
        admitted = [
            m for m in model.metrics
            if m.aspect == "provenance_integrity" and m.importance > 0
        ]
        assert {m.signal for m in admitted} == {"uncited_observation_rate"}

        conn = _make_conn()
        _insert(conn, "ob_1", "observation_factual", evidence_id="ev_1")

        snap = health_engine.build_snapshot(conn, "nap", model=model)
        assert snap["scores"]["unscored_aspects"] == []
        assert snap["scores"]["points_in_play"] == pytest.approx(100.0)
        assert snap["scores"]["composed_coverage"] == pytest.approx(1.0)
        assert isinstance(snap["scores"]["composed"], float)
        assert "provenance_integrity" in snap["scores"]["aspect_coverage"]


# ---------------------------------------------------------------------------
# 3 + 4. build_snapshot shape + UNAVAILABLE serialization contract
# ---------------------------------------------------------------------------


class TestBuildSnapshot:
    def test_rejects_invalid_checkpoint_mode(self):
        conn = _make_conn()
        with pytest.raises(ValueError):
            health_engine.build_snapshot(conn, "not_a_real_mode")

    def test_accepts_all_three_checkpoint_modes(self):
        conn = _make_conn()
        for mode in ("nap", "pre_sleep", "post_dream"):
            snap = health_engine.build_snapshot(conn, mode)
            assert snap["checkpoint_mode"] == mode

    def test_top_level_keys(self):
        """``measurements`` (engram-alpha#2537, PR1 of 3) plus
        ``health_version``/``raw_schema_version``/``model_version``
        (engram-alpha#2537, slice 2 — the two independent version axes,
        health/version.py's own design) are the new keys this epic adds —
        purely additive, see health/measurements.py's and
        health/signals.py's module docstrings. Every other key is
        unchanged from PR5."""
        conn = _make_conn()
        snap = health_engine.build_snapshot(conn, "nap")
        assert set(snap.keys()) == {
            "schema_version", "engine_version", "health_version",
            "raw_schema_version", "model_version", "config", "definitions",
            "measurements", "signals", "scores", "red_flags", "diagnostics",
            "anchor", "turn", "checkpoint_mode",
        }

    def test_measurements_block_delegates_to_compute_all_measurements(self):
        """build_snapshot's ``measurements`` key must be exactly what
        health_measurements.compute_all_measurements(conn) returns on
        the SAME conn — a wiring check, not a re-test of the L0 layer's own
        correctness (see test_health_measurements.py for that)."""
        conn = _make_conn()
        with mock.patch("engram_core._embedder.is_available", return_value=False):
            snap = health_engine.build_snapshot(conn, "nap")
            direct = health_measurements.compute_all_measurements(conn)
        assert snap["measurements"] == direct

    def test_measurements_block_does_not_change_existing_output(self):
        """PURELY ADDITIVE (#2537 slice 1's whole contract): signals/scores/
        aspects/composed must be unchanged from what they were before this
        PR — asserted here against the SAME known-good value the empty-DB
        fixture already produces elsewhere in this file
        (``test_empty_graph_every_signal_and_aspect_and_composed_
        unavailable``'s ``composed == "UNAVAILABLE"``, since ``_make_conn()``
        starts every population at zero), not just that the ``measurements``
        key is present."""
        conn = _make_conn()
        snap = health_engine.build_snapshot(conn, "nap")
        assert "measurements" in snap
        assert isinstance(snap["measurements"], dict)
        assert snap["measurements"]["raw_schema_version"] == (
            health_measurements.RAW_SCHEMA_VERSION
        )
        # signals/scores untouched — still computed independently of
        # "measurements", per the module docstring's "no residue" claim.
        # The actual golden VALUE, not just key-presence: on this empty
        # fixture every population is zero, so every signal is UNAVAILABLE
        # and composed is UNAVAILABLE too — the same value this file's own
        # empty-DB test already establishes.
        assert snap["scores"]["composed"] == "UNAVAILABLE"

    def test_version_stamps_present_and_match_health_version(self):
        """engram-alpha#2537 slice 2: the two independent version axes
        (health/version.py's MAJOR/MINOR) are stamped on every snapshot so
        a stored artifact is self-describing without needing this module's
        current values to interpret it later."""
        conn = _make_conn()
        snap = health_engine.build_snapshot(conn, "nap")
        assert snap["health_version"] == health_version.CURRENT
        assert snap["raw_schema_version"] == health_version.MAJOR
        assert snap["model_version"] == health_version.MINOR

    def test_signals_delegate_to_l1_health_signals(self):
        """engram-alpha#2537 slice 2's actual load-bearing claim: the
        ``signals`` block is now sourced from ``health.signals.
        compute_all_signals`` over the SAME L0 read stored in
        ``measurements`` — not from a second, independent computation.
        Wiring check (not a re-test of L1's own arithmetic correctness —
        see tests/test_health_signals.py for that)."""
        conn = _make_conn()
        snap = health_engine.build_snapshot(conn, "nap")
        l1 = health_signals.compute_all_signals(snap["measurements"])
        for name, l1_value in l1.items():
            assert snap["signals"][name] == health_engine._to_jsonable(l1_value)

    def test_ssot_mismatch_between_l1_and_live_signal_fn_raises_loudly(self):
        """THE REQUIRED FALSIFIER for _verify_l1_against_live_signal_fns —
        a control that has only ever been seen to pass has not been shown
        capable of failing. Monkeypatches ONE entry in the live-verification
        table to return a value that structurally cannot match L1's real
        arithmetic (a constant, independent of the graph), and asserts
        build_snapshot raises HealthEngineSSoTError rather than silently
        accepting the divergence."""
        conn = _make_conn()
        # Populate enough that taint_rate's denominator (claim_bearing_
        # current) is non-zero, so L1's real computed value is a genuine
        # float the patched constant is guaranteed to differ from (0.0
        # could coincidentally match on an all-empty graph — this fixture
        # avoids that).
        _insert(conn, "obs_1", "observation_factual")
        with mock.patch.dict(
            health_engine._LIVE_SIGNAL_FNS_FOR_VERIFICATION,
            {"taint_rate": lambda _conn: 0.987654321},
        ):
            with pytest.raises(health_engine.HealthEngineSSoTError, match="taint_rate"):
                health_engine.build_snapshot(conn, "nap")

    def test_verification_table_is_complete_against_l1_signals(self):
        """Reviewer-fairy finding, PR#2556 round 1: a hand-typed
        _LIVE_SIGNAL_FNS_FOR_VERIFICATION with no enforced relationship to
        health.signals.SIGNALS could silently narrow SSoT coverage if a
        future signal is added to one but not the other — the verification
        loop only iterates over whatever keys are IN the table, so a
        missing entry is invisible, not a red test. This asserts the two
        key-sets are exactly equal today (module import already enforces
        this via a loud completeness check — this test pins the property
        under normal test-suite conditions, in case that import-time check
        is ever weakened)."""
        assert set(health_signals.SIGNALS.keys()) == set(
            health_engine._LIVE_SIGNAL_FNS_FOR_VERIFICATION.keys()
        )

    def test_config_metric_names_match_l1_signals(self):
        """Ariadne's colleague-review suggestion, PR#2556 round 2: the
        direction THIS PR itself newly introduces (CONFIG metric names ->
        health.signals.SIGNALS) was unguarded in round 1's completeness
        check — _run_aspect changed from metric.signal_fn(conn) (always
        resolvable) to l1_signals[metric.name] (a bare dict subscript), so
        a CONFIG metric with no matching SIGNALS entry is now a raw
        KeyError from build_snapshot's hot path. Pins the property under
        normal test-suite conditions (module import already enforces it
        via the same loud three-way completeness check)."""
        config_metric_names = {
            m.name for aspect in health_engine.CONFIG.aspects for m in aspect.metrics
        }
        assert config_metric_names == set(health_signals.SIGNALS.keys())

    def test_verification_table_completeness_check_actually_fires(self):
        """THE FALSIFIER for the completeness check above — and, per the
        delta reviewer-fairy on PR#2556's round-2 commit, a REAL one: the
        original version of this test recomputed the module's own set-
        difference arithmetic inline rather than calling into
        health/engine.py's actual import-time assertion, so it proved the
        arithmetic was right but nothing about whether engine.py's own code
        path would ever run it or run it correctly — a dead or inverted
        check in engine.py would have left this test green.

        Perturbs ``health_signals.SIGNALS`` to drop a name genuinely
        present in ``_LIVE_SIGNAL_FNS_FOR_VERIFICATION``, then reloads
        ``health.engine`` so its module-level completeness assertion
        actually re-runs against the perturbed state, and asserts it
        raises the real exception naming the real victim. Restores state
        and reloads again in ``finally`` regardless of outcome — a
        perturbed ``health_engine`` left in place would corrupt every
        later test in the same process."""
        victim = next(iter(health_signals.SIGNALS.keys()))
        original_entry = health_signals.SIGNALS.pop(victim)
        try:
            try:
                importlib.reload(health_engine)
            except Exception as exc:  # noqa: BLE001 — asserting on the real raise, not narrowing it away
                assert type(exc).__name__ == "HealthEngineSSoTError", (
                    f"expected HealthEngineSSoTError, got {type(exc).__name__}: {exc}"
                )
                assert victim in str(exc), (
                    f"raised, but didn't name the actual missing signal {victim!r}: {exc}"
                )
            else:
                pytest.fail(
                    "reload with a perturbed SIGNALS table did not raise — "
                    "the completeness check silently passed drifted state"
                )
        finally:
            health_signals.SIGNALS[victim] = original_entry
            importlib.reload(health_engine)

    def test_config_to_signals_completeness_check_actually_fires(self):
        """THE FALSIFIER for the NEW direction (round 2) — real, for the
        same reason as its sibling above: calls into engine.py's actual
        import-time check via reload rather than recomputing set
        arithmetic inline.

        Perturbs ``health_signals.SIGNALS`` to drop a name genuinely
        present in CONFIG's own metrics — the exact condition that would
        otherwise surface as a raw, unguarded ``KeyError`` inside
        ``_run_aspect`` — and confirms the reloaded module raises loudly
        instead. Restores + reloads in ``finally`` regardless of outcome,
        same reason as the sibling test."""
        config_metric_names = {
            m.name for aspect in health_engine.CONFIG.aspects for m in aspect.metrics
        }
        victim = next(iter(config_metric_names))
        original_entry = health_signals.SIGNALS.pop(victim)
        try:
            try:
                importlib.reload(health_engine)
            except Exception as exc:  # noqa: BLE001 — asserting on the real raise, not narrowing it away
                assert type(exc).__name__ == "HealthEngineSSoTError", (
                    f"expected HealthEngineSSoTError, got {type(exc).__name__}: {exc}"
                )
                assert victim in str(exc), (
                    f"raised, but didn't name the actual missing signal {victim!r}: {exc}"
                )
            else:
                pytest.fail(
                    "reload with a CONFIG metric missing from SIGNALS did not "
                    "raise — the completeness check silently passed drifted state"
                )
        finally:
            health_signals.SIGNALS[victim] = original_entry
            importlib.reload(health_engine)

    def test_red_flags_still_empty_diagnostics_now_populated(self):
        """red_flags stays [] (DAG-violations-as-a-red-flag is out of scope
        for PR3 too — see health/engine.py's module docstring) — but PR3
        replaces the previously-empty diagnostics block ({}) with a real
        dict (activity metrics + cascade-marker age)."""
        conn = _make_conn()
        snap = health_engine.build_snapshot(conn, "nap")
        assert snap["red_flags"] == []
        assert set(snap["diagnostics"].keys()) == {"activity", "cascade_marker_age"}
        assert set(snap["diagnostics"]["activity"].keys()) == {
            "question_resolution", "conjecture_resolution",
            "prediction_resolution", "contradiction_resolution",
            "nodes_created_last_24h", "supersede_edges_last_24h",
        }
        assert set(snap["diagnostics"]["cascade_marker_age"].keys()) == {
            "count", "max_age_turns", "histogram_turns",
        }

    def test_config_is_inlined_full_declaration(self):
        conn = _make_conn()
        snap = health_engine.build_snapshot(conn, "nap")
        assert snap["config"]["schema_version"] == health_engine.CONFIG.schema_version
        aspect_names = {a["name"] for a in snap["config"]["aspects"]}
        assert aspect_names == {"epistemic_integrity", "provenance_integrity", "retrievability"}

    def test_empty_graph_every_signal_and_aspect_and_composed_unavailable(self):
        """RENAMED (round 2 of engram-alpha#2520, fold #2): open_
        contradiction_rate no longer returns a "clean" 0.0 on zero current
        contradiction nodes — it now declines (UNAVAILABLE) exactly like
        its two epistemic_integrity siblings, so on a truly EMPTY graph
        every one of the nine signals is UNAVAILABLE. With ZERO available
        admitted metrics anywhere, all THREE aspects are held out (round 2,
        addendum 2's generalization) and composed is UNAVAILABLE too — the
        one remaining case where nothing anywhere is scored (see
        compose_aspect_scores's own "all aspects unavailable" branch)."""
        conn = _make_conn()
        snap = health_engine.build_snapshot(conn, "nap")
        assert snap["signals"]["stale_derivation_rate"] == "UNAVAILABLE"
        assert snap["signals"]["taint_rate"] == "UNAVAILABLE"
        assert snap["signals"]["uncited_observation_rate"] == "UNAVAILABLE"
        assert snap["signals"]["dangling_evidence_rate"] == "UNAVAILABLE"
        # aspect B (PR3): empty claim-bearing population -> all four UNAVAILABLE too
        assert snap["signals"]["recall_summary_coverage"] == "UNAVAILABLE"
        assert snap["signals"]["recall_keywords_coverage"] == "UNAVAILABLE"
        assert snap["signals"]["embedding_coverage"] == "UNAVAILABLE"
        assert snap["signals"]["giant_component_fraction"] == "UNAVAILABLE"
        # CHANGED (fold #2): zero contradiction NODES -> UNAVAILABLE, not a
        # fabricated "clean" 0.0 — see health.metrics.
        # open_contradiction_rate's own docstring.
        assert snap["signals"]["open_contradiction_rate"] == "UNAVAILABLE"
        assert snap["scores"]["aspects"]["epistemic_integrity"] == "UNAVAILABLE"
        assert snap["scores"]["aspects"]["retrievability"] == "UNAVAILABLE"
        assert snap["scores"]["aspects"]["provenance_integrity"] == "UNAVAILABLE"
        assert snap["scores"]["composed"] == "UNAVAILABLE"
        assert set(snap["scores"]["unscored_aspects"]) == {
            "epistemic_integrity", "provenance_integrity", "retrievability",
        }
        assert snap["scores"]["points_in_play"] == 0
        assert snap["scores"]["composed_coverage"] == 0.0
        assert snap["scores"]["aspect_coverage"] == {}

    def test_unavailable_serializes_as_the_string_literal_not_a_number(self):
        """Contract item 3: UNAVAILABLE is unrepresentable as a number. This
        PR's serialization decision uses the string literal "UNAVAILABLE" —
        never -1, never null/None."""
        conn = _make_conn()
        snap = health_engine.build_snapshot(conn, "nap")
        raw = json.dumps(snap)
        parsed = json.loads(raw)
        assert parsed["signals"]["stale_derivation_rate"] == "UNAVAILABLE"
        assert parsed["signals"]["stale_derivation_rate"] != -1
        assert parsed["signals"]["stale_derivation_rate"] is not None
        # And the raw JSON text itself must contain the literal string, not
        # a bare `null` or `-1` at that key's position.
        assert '"stale_derivation_rate": "UNAVAILABLE"' in json.dumps(snap, indent=2) or \
            '"stale_derivation_rate":"UNAVAILABLE"' in raw

    def test_populated_graph_produces_real_aspect_scores(self):
        """[PR5, updated; round 2 of engram-alpha#2520 changes composed's
        outcome] A graph that gives every ADMITTED metric in
        epistemic_integrity and retrievability a clean, non-empty
        denominator produces real numeric aspect scores for THOSE two
        aspects. provenance_integrity's OWN score stays UNAVAILABLE (zero
        admitted metrics — unchanged by round 2), but round 2's "hold out,
        renormalize, report" ruling means this NO LONGER propagates to the
        top-level composed score — see TestRealModelCompositionConsequence
        above for the dedicated test of that fact, and health/engine.py's
        module docstring "RESOLVED" section for the full reasoning. This
        fixture deliberately carries NO contradiction node, so open_
        contradiction_rate (round 2's fold #2: UNAVAILABLE on an EMPTY
        population) is one of epistemic_integrity's three admitted metrics
        going unavailable this run — proving the metric-level renormalize
        (addendum 2) lands a REAL epistemic_integrity score anyway, over
        the remaining two available metrics."""
        conn = _make_conn()
        # Give every real metric a non-empty denominator with a clean signal
        # — including aspect B's four: both claim-bearing nodes carry a
        # recall_summary, recall_keywords, and embedding, and are connected
        # by an edge (so giant_component_fraction reads fully connected).
        _insert(conn, "dv_1", "derivation", metadata={},
                recall_summary="s1", recall_keywords=["k1"],
                embedding=json.dumps([0.1]))
        _insert(conn, "ob_1", "observation_factual", evidence_id="ev_1",
                recall_summary="s2", recall_keywords=["k2"],
                embedding=json.dumps([0.2]))
        _insert(conn, "ev_1", "evidence")
        _insert_edge(conn, "dv_1", "ob_1", "derives_from")

        # embedding_coverage now gates on embedder availability (Borges,
        # PR3 colleague review, 2026-08-23, blocker 1) -- the real test
        # suite runs with ENGRAM_NO_EMBEDDINGS=1, so without this patch the
        # metric would read UNAVAILABLE regardless of the populated
        # embedding column above, which is not what this test is checking.
        with mock.patch("engram_core._embedder.is_available", return_value=True):
            snap = health_engine.build_snapshot(conn, "nap")
        assert isinstance(snap["scores"]["aspects"]["epistemic_integrity"], float)
        assert isinstance(snap["scores"]["aspects"]["retrievability"], float)
        assert snap["scores"]["aspects"]["provenance_integrity"] == "UNAVAILABLE"
        # CHANGED (round 2): composed is now a REAL number, renormalized
        # over the two scored aspects (75 of 100 points in play).
        assert isinstance(snap["scores"]["composed"], float)
        assert snap["scores"]["unscored_aspects"] == ["provenance_integrity"]
        assert snap["scores"]["points_in_play"] == pytest.approx(75.0)
        assert snap["scores"]["composed_coverage"] == pytest.approx(0.75)
        # open_contradiction_rate's own UNAVAILABLE (empty population, no
        # contradiction node in this fixture) does NOT drag epistemic_
        # integrity down to UNAVAILABLE -- it renormalizes over the
        # remaining two available admitted metrics (addendum 2).
        assert snap["signals"]["open_contradiction_rate"] == "UNAVAILABLE"
        epistemic_coverage = snap["scores"]["aspect_coverage"]["epistemic_integrity"]
        assert set(epistemic_coverage["metrics_in_play"]) == {
            "taint_rate", "stale_derivation_rate",
        }
        assert epistemic_coverage["coverage"] == pytest.approx(2.0 / 3.0)

        # Fully covered + connected -> all four aspect-B raw signals
        # (literal-coverage convention, engram-alpha#2518 — before that PR
        # these were the badness-rate COMPLEMENT and read 0.0 here instead)
        # read as perfect (1.0), and the aspect score is now on the [0,1]
        # range (PR5 change — see build_snapshot's docstring), not the old
        # [0,100] display scale.
        assert snap["signals"]["recall_summary_coverage"] == 1.0
        assert snap["signals"]["recall_keywords_coverage"] == 1.0
        assert snap["signals"]["embedding_coverage"] == 1.0
        assert snap["signals"]["giant_component_fraction"] == 1.0
        assert snap["scores"]["aspects"]["retrievability"] == pytest.approx(1.0)
        assert snap["scores"]["aspect_coverage"]["retrievability"]["coverage"] == pytest.approx(1.0)

    def test_per_metric_display_score_matches_the_shared_curve_transform(self):
        """[PR5, rewritten] build_snapshot's per-metric display score
        (snap["scores"]["metrics"]) must be derived from the SAME curve the
        real scoring model binds to that metric (``badness_default`` for
        five of the nine real metrics, ``goodness_default`` for the other
        four as of engram-alpha#2518 — see health/model.py's
        ``CURVES``) — not an independently re-implemented copy of the
        transform that could silently diverge. This test exercises
        ``open_contradiction_rate`` specifically, which stays on
        ``badness_default``, unaffected by #2518's flip.
        Supersedes the PR2/PR3-era version of this test, which compared
        against ``hem.metric_goodness`` (now dead code from health/engine.
        py's perspective — see that module's docstring)."""
        from health import model as hem_model

        conn = _make_conn()
        _insert(conn, "ct_1", "contradiction", status="active")
        _insert(conn, "ct_2", "contradiction", status="resolved")
        snap = health_engine.build_snapshot(conn, "nap")
        raw = snap["signals"]["open_contradiction_rate"]
        assert raw == 0.5
        binding = hem_model.METRIC_BY_SIGNAL["open_contradiction_rate"]
        curve = health_engine.MODEL.curves[binding.curve]
        assert snap["scores"]["metrics"]["open_contradiction_rate"] == curve.curve_fn(raw)
        # Numerically, badness_default's shape still matches the OLD
        # metric_goodness transform divided by 100 (by construction — see
        # health/curves.py's BADNESS_DEFAULT_CURVE docstring) — this
        # is the one place the old and new numbers stay directly comparable.
        assert snap["scores"]["metrics"]["open_contradiction_rate"] == pytest.approx(
            hem.metric_goodness(raw) / 100.0
        )


# ---------------------------------------------------------------------------
# 6. Diagnostics (PR3) — activity metrics + cascade-marker age. Never wired
#    into scoring — see health/engine.py's module docstring.
# ---------------------------------------------------------------------------


class TestActivityDiagnostics:
    def test_empty_graph_all_totals_zero(self):
        conn = _make_conn()
        activity = health_engine._activity_diagnostics(conn)
        assert activity["question_resolution"] == {"total": 0, "resolved": 0, "rate": None}
        assert activity["nodes_created_last_24h"] == 0
        assert activity["supersede_edges_last_24h"] == 0

    def test_question_resolution_rate(self):
        conn = _make_conn()
        _insert(conn, "qu_1", "question", status="resolved")
        _insert(conn, "qu_2", "question", status="partially_resolved")
        _insert(conn, "qu_3", "question", status="active")
        activity = health_engine._activity_diagnostics(conn)
        assert activity["question_resolution"] == {"total": 3, "resolved": 2, "rate": round(2 / 3, 3)}

    def test_conjecture_resolution_rate(self):
        conn = _make_conn()
        _insert(conn, "cj_1", "conjecture", status="supported")
        _insert(conn, "cj_2", "conjecture", status="active")
        activity = health_engine._activity_diagnostics(conn)
        assert activity["conjecture_resolution"] == {"total": 2, "resolved": 1, "rate": 0.5}

    def test_prediction_resolution_rate(self):
        conn = _make_conn()
        _insert(conn, "pd_1", "prediction", status="confirmed")
        _insert(conn, "pd_2", "prediction", status="disconfirmed")
        _insert(conn, "pd_3", "prediction", status="pending")
        activity = health_engine._activity_diagnostics(conn)
        assert activity["prediction_resolution"] == {"total": 3, "resolved": 2, "rate": round(2 / 3, 3)}

    def test_contradiction_resolution_rate(self):
        conn = _make_conn()
        _insert(conn, "ct_1", "contradiction", status="resolved")
        _insert(conn, "ct_2", "contradiction", status="active")
        activity = health_engine._activity_diagnostics(conn)
        assert activity["contradiction_resolution"] == {"total": 2, "resolved": 1, "rate": 0.5}

    def test_resolution_rate_not_restricted_to_is_current(self):
        """Matches engram_stats.py's own _resolution_rate scoping exactly —
        a resolved/superseded question is still counted; status, not
        is_current, is what this rate measures."""
        conn = _make_conn()
        _insert(conn, "qu_1", "question", status="resolved", is_current=0)
        activity = health_engine._activity_diagnostics(conn)
        assert activity["question_resolution"] == {"total": 1, "resolved": 1, "rate": 1.0}

    def test_nodes_created_last_24h_excludes_older_nodes(self):
        conn = _make_conn()
        _insert(conn, "dv_1", "derivation", created_at="2020-01-01T00:00:00Z")
        activity = health_engine._activity_diagnostics(conn)
        assert activity["nodes_created_last_24h"] == 0

    def test_nodes_created_last_24h_includes_recent_nodes(self):
        conn = _make_conn()
        recent = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        _insert(conn, "dv_1", "derivation", created_at=recent)
        activity = health_engine._activity_diagnostics(conn)
        assert activity["nodes_created_last_24h"] == 1

    def test_supersede_edges_last_24h(self):
        conn = _make_conn()
        recent = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        _insert(conn, "dv_1", "derivation")
        _insert(conn, "dv_2", "derivation")
        _insert_edge(conn, "dv_2", "dv_1", "supersedes", created_at=recent)
        _insert_edge(conn, "dv_1", "dv_2", "cites", created_at=recent)
        activity = health_engine._activity_diagnostics(conn)
        assert activity["supersede_edges_last_24h"] == 1


class TestCascadeMarkerAgeDiagnostics:
    def test_no_markers_is_empty(self):
        conn = _make_conn()
        diag = health_engine._cascade_marker_age_diagnostics(conn, current_turn=100)
        assert diag == {
            "count": 0, "max_age_turns": 0,
            "histogram_turns": {b[0]: 0 for b in health_engine._CASCADE_MARKER_AGE_BUCKETS_TURNS},
        }

    def test_ages_computed_from_marker_created_at(self):
        """Post-fix (#2205 colleague review): age comes from
        cascade_markers.created_at (wall-clock, converted via the "1 turn ~=
        24h" approximation), not from current_turn - edit_history.turn.
        current_turn is still accepted (signature stability) but unused."""
        conn = _make_conn()
        _insert(conn, "dv_1", "derivation")
        _insert_marker(conn, "dv_1", root_op="retract", age_days=10)
        diag = health_engine._cascade_marker_age_diagnostics(conn, current_turn=100)
        assert diag["count"] == 1
        assert diag["max_age_turns"] == 10
        assert diag["histogram_turns"]["8-14"] == 1

    def test_max_age_is_the_oldest_marker(self):
        conn = _make_conn()
        _insert(conn, "dv_1", "derivation")
        _insert(conn, "dv_2", "derivation")
        _insert_marker(conn, "dv_1", root_op="retract", age_days=100)
        _insert_marker(conn, "dv_2", root_op="supersede", age_days=0)
        diag = health_engine._cascade_marker_age_diagnostics(conn, current_turn=100)
        assert diag["max_age_turns"] == 100
        assert diag["count"] == 2

    def test_resolved_markers_excluded(self):
        """THE bug this function's fix was for: a resolved marker must NOT
        count as live backlog even though its node is still current —
        status is the axis that decides "live", not node-currency alone.
        Pre-fix this function read edit_history with no status filter at
        all and could not distinguish this case (measured overcount: 270
        diagnostics rows vs 60 actually-pending marker rows on one graph,
        #2205 review)."""
        conn = _make_conn()
        _insert(conn, "dv_1", "derivation")
        _insert_marker(conn, "dv_1", root_op="retract", status="resolved", age_days=0)
        diag = health_engine._cascade_marker_age_diagnostics(conn, current_turn=100)
        assert diag["count"] == 0

    def test_both_root_ops_counted(self):
        """cascade_markers.root_op has exactly two values (retract,
        supersede) per the table's own CHECK constraint — no successor to
        the old 4-action-name test exists, since the old edit_history
        action names ('retracted'/'superseded'/'stale_flagged'/
        'support_lost') were a legacy-shaped label set the cascade machine
        collapsed into root_op; both real values are exercised here."""
        conn = _make_conn()
        _insert(conn, "dv_1", "derivation")
        _insert(conn, "dv_2", "derivation")
        _insert_marker(conn, "dv_1", root_op="retract", age_days=0)
        _insert_marker(conn, "dv_2", root_op="supersede", age_days=0)
        diag = health_engine._cascade_marker_age_diagnostics(conn, current_turn=10)
        assert diag["count"] == 2

    def test_marker_on_non_current_node_excluded(self):
        """'Live' cascade markers only — a marker whose node is no longer
        current is no longer part of the live backlog Aspect A's rates
        measure (see _cascade_marker_age_diagnostics's docstring)."""
        conn = _make_conn()
        _insert(conn, "dv_1", "derivation", is_current=0)
        _insert_marker(conn, "dv_1", root_op="retract", age_days=0)
        diag = health_engine._cascade_marker_age_diagnostics(conn, current_turn=100)
        assert diag["count"] == 0


class TestComputeDiagnostics:
    def test_shape(self):
        conn = _make_conn()
        diag = health_engine._compute_diagnostics(conn, current_turn=5)
        assert set(diag.keys()) == {"activity", "cascade_marker_age"}


# ---------------------------------------------------------------------------
# 7. write_snapshot — persistence + best-effort failure handling
# ---------------------------------------------------------------------------


class TestWriteSnapshot:
    def test_writes_a_json_file_under_data_dir_health_snapshots(self, tmp_path, monkeypatch):
        import engram_core as core
        monkeypatch.setattr(core, "DATA_DIR", tmp_path)
        conn = _make_conn()
        snap = health_engine.write_snapshot(conn, "nap")
        assert "_snapshot_write_error" not in snap
        path = snap.get("_snapshot_path")
        assert path, f"no _snapshot_path in {snap}"
        written = json.loads(open(path, encoding="utf-8").read())
        assert written["checkpoint_mode"] == "nap"
        assert (tmp_path / "health-snapshots").is_dir()

    def test_write_failure_is_best_effort_not_raised(self, tmp_path, monkeypatch):
        """A persistence failure (unwritable directory) must not raise —
        mirrors core._commit_snapshot's best-effort posture. The computed
        snapshot is still returned, with the failure surfaced via
        _snapshot_write_error rather than an exception."""
        import engram_core as core
        monkeypatch.setattr(core, "DATA_DIR", tmp_path)

        def _boom(self, *args, **kwargs):
            raise OSError("simulated disk failure")

        monkeypatch.setattr(health_engine.Path, "write_text", _boom)
        conn = _make_conn()
        snap = health_engine.write_snapshot(conn, "nap")  # must not raise
        assert snap.get("_snapshot_write_error")
        assert "checkpoint_mode" in snap  # the computed snapshot content is intact

    def test_filename_includes_turn_and_checkpoint_mode(self, tmp_path, monkeypatch):
        import engram_core as core
        monkeypatch.setattr(core, "DATA_DIR", tmp_path)
        conn = _make_conn()
        snap = health_engine.write_snapshot(conn, "post_dream")
        path = snap["_snapshot_path"]
        filename = os.path.basename(path)
        assert filename.startswith(f"{snap['turn']}-post_dream-")
        assert filename.endswith(".json")

    def test_write_failure_also_surfaces_as_dream_projection_write_error(
        self, tmp_path, monkeypatch
    ):
        """The SAME unwritable-directory failure that hits the main
        snapshot also hits the dream projection's persistence step (both go
        through ``Path.write_text``) — verifies the dream-projection write
        is ALSO best-effort, not just the main snapshot's."""
        import engram_core as core
        monkeypatch.setattr(core, "DATA_DIR", tmp_path)

        def _boom(self, *args, **kwargs):
            raise OSError("simulated disk failure")

        monkeypatch.setattr(health_engine.Path, "write_text", _boom)
        conn = _make_conn()
        snap = health_engine.write_snapshot(conn, "nap")  # must not raise
        assert snap.get("_snapshot_write_error")
        assert snap.get("_dream_projection_write_error")

    def test_dream_projection_failure_writes_error_sentinel(self, tmp_path, monkeypatch):
        """[engram-alpha#2519 Ruling 2] A caught dream-projection exception
        (computation, not just persistence — forcing ``_build_dream_
        projection`` itself to raise, so ``Path.write_text`` stays intact
        and the sentinel write below can actually succeed) writes the
        sibling ``.error`` sentinel with the shape Ruling 2 names:
        ``{"turn": ..., "written_at": ..., "error": ...}``."""
        import engram_core as core
        monkeypatch.setattr(core, "DATA_DIR", tmp_path)

        def _boom(*args, **kwargs):
            raise RuntimeError("simulated dream-projection computation failure")

        monkeypatch.setattr(health_engine, "_build_dream_projection", _boom)
        conn = _make_conn()
        snap = health_engine.write_snapshot(conn, "nap")  # must not raise
        assert snap.get("_dream_projection_write_error")

        error_path = tmp_path / "health-snapshots" / "health-snapshot-dream.error"
        assert error_path.exists()
        payload = json.loads(error_path.read_text(encoding="utf-8"))
        assert payload["turn"] == snap["turn"]
        assert "written_at" in payload
        assert "simulated dream-projection computation failure" in payload["error"]

    def test_dream_projection_write_failure_writes_error_sentinel(self, tmp_path, monkeypatch):
        """[round 3, engram-alpha#2519, reviewer-fairy suggestion] The
        sibling test above (``test_dream_projection_failure_writes_error_
        sentinel``) only exercises the COMPUTATION half of write_snapshot's
        own docstring claim ("on a caught dream-projection exception
        (computation OR write)") — it forces ``_build_dream_projection``
        itself to raise. This test isolates the WRITE half: ``_build_dream_
        projection`` succeeds normally, but the dream-projection file's OWN
        ``write_text`` call fails, while the MAIN snapshot's own SEPARATE
        ``write_text`` call still succeeds — proving the sentinel fires on
        this genuinely different code path within the same ``except``
        block, not merely a re-run of the computation-failure test."""
        import engram_core as core
        monkeypatch.setattr(core, "DATA_DIR", tmp_path)

        real_write_text = health_engine.Path.write_text

        def _write_text_fails_only_for_dream_projection(self, *args, **kwargs):
            if self.name == "health-snapshot-dream.json":
                raise OSError("simulated dream-projection WRITE failure")
            return real_write_text(self, *args, **kwargs)

        monkeypatch.setattr(
            health_engine.Path,
            "write_text",
            _write_text_fails_only_for_dream_projection,
        )
        conn = _make_conn()
        snap = health_engine.write_snapshot(conn, "nap")  # must not raise

        # The MAIN snapshot's own write succeeded — this is a WRITE-path
        # failure isolated to the dream projection, distinct from the
        # computation-failure test (where _build_dream_projection itself
        # never returns, so neither write_text call for the dream
        # projection is even reached).
        assert "_snapshot_write_error" not in snap
        main_path = snap.get("_snapshot_path")
        assert main_path, f"no _snapshot_path in {snap}"
        assert os.path.exists(main_path)

        assert snap.get("_dream_projection_write_error")
        assert (
            "simulated dream-projection WRITE failure"
            in snap["_dream_projection_write_error"]
        )
        # Distinguishable from the computation-failure test's own error text.
        assert "computation failure" not in snap["_dream_projection_write_error"]

        error_path = tmp_path / "health-snapshots" / "health-snapshot-dream.error"
        assert error_path.exists()
        payload = json.loads(error_path.read_text(encoding="utf-8"))
        assert payload["turn"] == snap["turn"]
        assert "written_at" in payload
        assert "simulated dream-projection WRITE failure" in payload["error"]

    def test_successful_write_clears_a_stale_error_sentinel(self, tmp_path, monkeypatch):
        """[engram-alpha#2519 Ruling 2] The OTHER direction: a SUCCESSFUL
        dream-projection write deletes a pre-existing ``.error`` sentinel
        left behind by an earlier failed night — never leaves a stale
        failure sentinel next to a fresh, healthy projection."""
        import engram_core as core
        monkeypatch.setattr(core, "DATA_DIR", tmp_path)

        snapshots_dir = tmp_path / "health-snapshots"
        snapshots_dir.mkdir(parents=True, exist_ok=True)
        error_path = snapshots_dir / "health-snapshot-dream.error"
        error_path.write_text(
            json.dumps({"turn": 1, "written_at": "2026-01-01T00:00:00Z", "error": "stale"}),
            encoding="utf-8",
        )

        conn = _make_conn()
        snap = health_engine.write_snapshot(conn, "nap")  # must not raise
        assert "_dream_projection_write_error" not in snap
        assert not error_path.exists()


# ---------------------------------------------------------------------------
# [PR5] The dream-side projection — draft-2 §2. Fixed filename, aspect-blind
# by construction (P2), delta_since_last read from the previous write.
# ---------------------------------------------------------------------------


class TestDreamProjection:
    _FORBIDDEN_TOP_LEVEL_KEYS = {"aspects", "scores", "composed", "config", "red_flags"}
    _FORBIDDEN_PER_METRIC_KEYS = {"importance", "points", "aspect", "curve"}

    def test_writes_a_fixed_filename_not_timestamped(self, tmp_path, monkeypatch):
        import engram_core as core
        monkeypatch.setattr(core, "DATA_DIR", tmp_path)
        conn = _make_conn()
        snap = health_engine.write_snapshot(conn, "nap")
        assert "_dream_projection_write_error" not in snap
        path = snap.get("_dream_projection_path")
        assert path, f"no _dream_projection_path in {snap}"
        assert os.path.basename(path) == "health-snapshot-dream.json"

    def test_second_checkpoint_overwrites_the_same_fixed_path(self, tmp_path, monkeypatch):
        import engram_core as core
        monkeypatch.setattr(core, "DATA_DIR", tmp_path)
        conn = _make_conn()
        snap1 = health_engine.write_snapshot(conn, "nap")
        snap2 = health_engine.write_snapshot(conn, "nap")
        assert snap1["_dream_projection_path"] == snap2["_dream_projection_path"]
        # Exactly one dream-projection file on disk, regardless of how many
        # timestamped MAIN snapshots accumulated in the same directory.
        dream_files = list((tmp_path / "health-snapshots").glob("health-snapshot-dream.json"))
        assert len(dream_files) == 1

    def test_coverage_fields_present_and_match_the_main_snapshot(
        self, tmp_path, monkeypatch
    ):
        """[round 2, engram-alpha#2520] Kepler's design ruling explicitly
        names the dream projection as a REQUIRED destination for
        unscored_aspects/points_in_play/composed_coverage (module
        docstring's "RESOLVED" section, item 2) — an explicit, narrow
        exception to P2 ("aspect-blind") for these three fields only. Read
        straight off the main snapshot's own ``scores`` block — single
        source of truth, so the two files can never silently disagree."""
        import engram_core as core
        monkeypatch.setattr(core, "DATA_DIR", tmp_path)
        conn = _make_conn()
        _insert(conn, "dv_1", "derivation")
        _insert(conn, "ob_1", "observation_factual")
        snap = health_engine.write_snapshot(conn, "nap")
        projection = json.loads(open(snap["_dream_projection_path"], encoding="utf-8").read())

        assert projection["unscored_aspects"] == snap["scores"]["unscored_aspects"]
        assert projection["points_in_play"] == snap["scores"]["points_in_play"]
        assert projection["composed_coverage"] == snap["scores"]["composed_coverage"]
        assert projection["unscored_aspects"] == ["provenance_integrity"]
        assert projection["points_in_play"] == pytest.approx(75.0)
        assert projection["composed_coverage"] == pytest.approx(0.75)
        # The addendum-2 per-aspect metrics_in_play/coverage structure is
        # DELIBERATELY NOT added here (JUDGMENT CALL — see
        # _build_dream_projection's own docstring): aspect-keyed, a bigger
        # P2 violation than the three small global fields above, and
        # nothing in the addendum asks for it on the dream side.
        assert "aspect_coverage" not in projection
        assert "metrics_in_play" not in projection

    def test_aspect_blind_no_aspects_scores_composed_importances_points(
        self, tmp_path, monkeypatch
    ):
        """P2's mechanical enforcement, tested directly (not just "the
        allowed fields are present" — the NEGATIVE space, per the PR5
        spec's explicit instruction): scores.aspects / scores.composed /
        importances / points must be ABSENT from the projection file, at
        both the top level and inside every per-metric entry."""
        import engram_core as core
        monkeypatch.setattr(core, "DATA_DIR", tmp_path)
        conn = _make_conn()
        _insert(conn, "dv_1", "derivation")
        _insert(conn, "ob_1", "observation_factual")
        snap = health_engine.write_snapshot(conn, "nap")
        projection = json.loads(open(snap["_dream_projection_path"], encoding="utf-8").read())

        assert set(projection.keys()) & self._FORBIDDEN_TOP_LEVEL_KEYS == set()
        assert "metrics" in projection
        assert len(projection["metrics"]) == 9
        for metric_name, entry in projection["metrics"].items():
            assert set(entry.keys()) & self._FORBIDDEN_PER_METRIC_KEYS == set()
            assert set(entry.keys()) == {
                "signal", "score", "admitted", "detector", "repair",
                "delta_since_last",
            }

    def test_admitted_flag_matches_the_scoring_model(self, tmp_path, monkeypatch):
        import engram_core as core
        from health import model as hem_model

        monkeypatch.setattr(core, "DATA_DIR", tmp_path)
        conn = _make_conn()
        snap = health_engine.write_snapshot(conn, "nap")
        projection = json.loads(open(snap["_dream_projection_path"], encoding="utf-8").read())
        for signal, entry in projection["metrics"].items():
            expected_admitted = hem_model.METRIC_BY_SIGNAL[signal].importance > 0
            assert entry["admitted"] == expected_admitted

    def test_delta_since_last_is_none_on_first_write(self, tmp_path, monkeypatch):
        import engram_core as core
        monkeypatch.setattr(core, "DATA_DIR", tmp_path)
        conn = _make_conn()
        snap = health_engine.write_snapshot(conn, "nap")
        projection = json.loads(open(snap["_dream_projection_path"], encoding="utf-8").read())
        for entry in projection["metrics"].values():
            assert entry["delta_since_last"] is None

    def test_delta_since_last_reflects_the_score_change_since_previous_write(
        self, tmp_path, monkeypatch
    ):
        import engram_core as core
        monkeypatch.setattr(core, "DATA_DIR", tmp_path)
        conn = _make_conn()
        _insert(conn, "dv_1", "derivation")
        health_engine.write_snapshot(conn, "nap")  # first write: taint_rate score = 1.0 (clean)

        _insert_marker(conn, "dv_1", root_op="supersede")  # now stale
        snap2 = health_engine.write_snapshot(conn, "nap")
        projection2 = json.loads(open(snap2["_dream_projection_path"], encoding="utf-8").read())
        entry = projection2["metrics"]["stale_derivation_rate"]
        assert entry["signal"] == 1.0
        assert entry["score"] == pytest.approx(0.01)  # badness_default floor
        assert entry["delta_since_last"] == pytest.approx(0.01 - 1.0)

    def test_unavailable_signal_projects_as_the_string_literal(self, tmp_path, monkeypatch):
        """Empty graph -> most metrics UNAVAILABLE — the projection must
        carry the SAME string-literal contract as the main snapshot (never
        coerced to a number)."""
        import engram_core as core
        monkeypatch.setattr(core, "DATA_DIR", tmp_path)
        conn = _make_conn()
        snap = health_engine.write_snapshot(conn, "nap")
        projection = json.loads(open(snap["_dream_projection_path"], encoding="utf-8").read())
        entry = projection["metrics"]["taint_rate"]
        assert entry["signal"] == "UNAVAILABLE"
        assert entry["score"] == "UNAVAILABLE"
        assert entry["delta_since_last"] is None


# ---------------------------------------------------------------------------
# [engram-alpha#2519] _count_null_embeddings — the repair pass's own ONE
# genuinely new read-only scanner (embedding_coverage's detector,
# scan:null-embeddings). Mirrors health.metrics.embedding_coverage's
# own "missing" query shape (same is_current=1 AND type IN (...) AND
# embedding IS NULL predicate) — these tests exist to prove that mirroring
# holds, not to re-test embedding_coverage itself.
# ---------------------------------------------------------------------------


class TestCountNullEmbeddings:
    def test_counts_only_claim_bearing_nodes_with_null_embedding(self):
        conn = _make_conn()
        _insert(conn, "dv_1", "derivation", embedding=None)
        _insert(conn, "dv_2", "derivation", embedding="[0.1, 0.2]")
        _insert(conn, "ob_1", "observation_factual", embedding=None)
        assert health_engine._count_null_embeddings(conn, "claim_bearing_nodes") == 2

    def test_non_claim_bearing_type_never_counted(self):
        """A non-claim-bearing node type with a NULL embedding must not
        inflate the count — this scanner counts the SAME population
        embedding_coverage's own denominator does."""
        conn = _make_conn()
        _insert(conn, "dv_1", "derivation", embedding=None)
        _insert(conn, "gl_1", "goal", embedding=None)
        assert health_engine._count_null_embeddings(conn, "claim_bearing_nodes") == 1

    def test_non_current_node_never_counted(self):
        conn = _make_conn()
        _insert(conn, "dv_1", "derivation", embedding=None, is_current=0)
        assert health_engine._count_null_embeddings(conn, "claim_bearing_nodes") == 0

    def test_zero_when_nothing_missing(self):
        conn = _make_conn()
        _insert(conn, "dv_1", "derivation", embedding="[0.1]")
        assert health_engine._count_null_embeddings(conn, "claim_bearing_nodes") == 0

    def test_unimplemented_population_raises_loud(self):
        """A population value this scanner doesn't implement must fail
        loud, not silently return a wrong count (this codebase's own
        "loud guard, not silent workaround" discipline)."""
        conn = _make_conn()
        with pytest.raises(ValueError, match="observations"):
            health_engine._count_null_embeddings(conn, "observations")
