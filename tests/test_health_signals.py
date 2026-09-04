"""Tests for health/signals.py — L1, signals as arithmetic formulas over L0
(engram-alpha#2537, slice 2 of the health-v2 SSoT refactor).

Deliberately DB-free: every function in health/signals.py takes a plain
``dict`` (the L0 measurements shape), never a ``sqlite3.Connection`` — this
is the architectural point of L1 existing as its own layer, and these tests
exercise exactly that: no fixture DB, no schema, just literal input dicts.

Cross-checked against a live graph in
tests/test_health_engine.py::TestBuildSnapshot's build_snapshot tests (which
now route through this module) and against
tests/test_health_measurements.py's own acceptance criterion (which this
module's arithmetic was ported from, verbatim, per this module's own
docstring) — this file's job is the arithmetic in isolation, not
re-litigating that cross-check.
"""

from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src", "engram"))

from health import signals  # noqa: E402
from health.config import UNAVAILABLE  # noqa: E402


class TestRateHelper:
    def test_nonzero_denominator_divides(self):
        assert signals._rate(1, 4) == 0.25

    def test_zero_denominator_is_unavailable(self):
        assert signals._rate(0, 0) is UNAVAILABLE

    def test_zero_numerator_nonzero_denominator_is_zero_not_unavailable(self):
        assert signals._rate(0, 5) == 0.0


class TestSimpleRateSignals:
    """The five signals with no coverage-polarity flip and no environment
    gate: numerator/denominator, straight through."""

    def test_stale_derivation_rate(self):
        m = {"derivations_pending_supersede_marker": 1, "derivations_current": 4}
        assert signals.stale_derivation_rate(m) == 0.25

    def test_stale_derivation_rate_empty_population(self):
        m = {"derivations_pending_supersede_marker": 0, "derivations_current": 0}
        assert signals.stale_derivation_rate(m) is UNAVAILABLE

    def test_taint_rate(self):
        m = {"claim_bearing_pending_retract_marker": 2, "claim_bearing_current": 8}
        assert signals.taint_rate(m) == 0.25

    def test_open_contradiction_rate(self):
        m = {"contradictions_open": 1, "contradictions_current": 2}
        assert signals.open_contradiction_rate(m) == 0.5

    def test_open_contradiction_rate_empty_is_unavailable_not_clean(self):
        """engram-alpha#2520 round 2's flip: zero contradictions is
        UNAVAILABLE, never the old "clean" 0.0 — the last holdout among the
        nine signals to make this flip, per this module's own docstring."""
        m = {"contradictions_open": 0, "contradictions_current": 0}
        assert signals.open_contradiction_rate(m) is UNAVAILABLE

    def test_uncited_observation_rate(self):
        m = {"observations_without_evidence_id": 3, "observations_current": 6}
        assert signals.uncited_observation_rate(m) == 0.5

    def test_dangling_evidence_rate(self):
        m = {"evidence_uncited": 1, "evidence_current": 5}
        assert signals.dangling_evidence_rate(m) == 0.2


class TestCoverageSignalsLiteralPolarity:
    """The four coverage signals (engram-alpha#2518 polarity flip): 1.0 =
    fully covered, computed as ``1 - without/total``, not the raw
    "without" rate."""

    def test_recall_summary_coverage_fully_covered(self):
        m = {"claim_bearing_without_recall_summary": 0, "claim_bearing_current": 4}
        assert signals.recall_summary_coverage(m) == 1.0

    def test_recall_summary_coverage_partial(self):
        m = {"claim_bearing_without_recall_summary": 1, "claim_bearing_current": 4}
        assert signals.recall_summary_coverage(m) == 0.75

    def test_recall_summary_coverage_empty_population_stays_unavailable(self):
        """UNAVAILABLE must propagate THROUGH the ``1 - `` flip, never
        becoming ``1 - UNAVAILABLE`` (which would TypeError via
        _Unavailable.__bool__/arithmetic) or silently becoming 1.0."""
        m = {"claim_bearing_without_recall_summary": 0, "claim_bearing_current": 0}
        assert signals.recall_summary_coverage(m) is UNAVAILABLE

    def test_recall_keywords_coverage(self):
        m = {"claim_bearing_without_recall_keywords": 1, "claim_bearing_current": 2}
        assert signals.recall_keywords_coverage(m) == 0.5

    def test_recall_keywords_coverage_empty_population_stays_unavailable(self):
        m = {"claim_bearing_without_recall_keywords": 0, "claim_bearing_current": 0}
        assert signals.recall_keywords_coverage(m) is UNAVAILABLE


class TestEmbeddingCoverageEnvironmentGate:
    """The one signal with a SEPARATE, EARLIER UNAVAILABLE gate — the
    embedder environment facts, checked before the denominator question
    even applies (design charge: "not derivable from any count")."""

    def test_both_environment_facts_true_computes_normally(self):
        m = {
            "embedder_installed_available": True,
            "embedder_config_enabled": True,
            "claim_bearing_null_embedding": 1,
            "claim_bearing_current": 4,
        }
        assert signals.embedding_coverage(m) == 0.75

    def test_embedder_not_installed_is_unavailable_regardless_of_counts(self):
        m = {
            "embedder_installed_available": False,
            "embedder_config_enabled": True,
            "claim_bearing_null_embedding": 0,  # would compute 1.0 if not gated
            "claim_bearing_current": 4,
        }
        assert signals.embedding_coverage(m) is UNAVAILABLE

    def test_embedder_config_disabled_is_unavailable_regardless_of_counts(self):
        m = {
            "embedder_installed_available": True,
            "embedder_config_enabled": False,
            "claim_bearing_null_embedding": 0,
            "claim_bearing_current": 4,
        }
        assert signals.embedding_coverage(m) is UNAVAILABLE

    def test_environment_gate_checked_before_empty_denominator(self):
        """Both gates absent AND an empty population — must still resolve
        to UNAVAILABLE (either reason alone already forces it; this checks
        the environment gate is not skipped just because the denominator
        question would ALSO have produced UNAVAILABLE)."""
        m = {
            "embedder_installed_available": False,
            "embedder_config_enabled": False,
            "claim_bearing_null_embedding": 0,
            "claim_bearing_current": 0,
        }
        assert signals.embedding_coverage(m) is UNAVAILABLE


class TestGiantComponentFraction:
    """The one signal with NO polarity flip at all — the literal connected
    fraction, no ``1 - ``."""

    def test_literal_fraction_no_complement(self):
        m = {"claim_bearing_giant_component_size": 3, "claim_bearing_current": 4}
        assert signals.giant_component_fraction(m) == 0.75

    def test_empty_population_is_unavailable(self):
        m = {"claim_bearing_giant_component_size": 0, "claim_bearing_current": 0}
        assert signals.giant_component_fraction(m) is UNAVAILABLE


class TestComputeAllSignals:
    def test_covers_every_registered_signal(self):
        m = {
            "derivations_pending_supersede_marker": 1, "derivations_current": 4,
            "claim_bearing_pending_retract_marker": 1, "claim_bearing_current": 4,
            "contradictions_open": 1, "contradictions_current": 4,
            "observations_without_evidence_id": 1, "observations_current": 4,
            "evidence_uncited": 1, "evidence_current": 4,
            "claim_bearing_without_recall_summary": 1,
            "claim_bearing_without_recall_keywords": 1,
            "embedder_installed_available": True, "embedder_config_enabled": True,
            "claim_bearing_null_embedding": 1,
            "claim_bearing_giant_component_size": 1,
        }
        result = signals.compute_all_signals(m)
        assert set(result.keys()) == set(signals.SIGNALS.keys())
        assert len(result) == 9

    def test_perturbing_a_nonzero_input_changes_the_output(self):
        """THE REQUIRED CONTROL, applied to this module directly rather
        than only via test_health_measurements.py's cross-check — a test
        that has only ever been seen to pass has not been shown capable of
        failing."""
        m = {"claim_bearing_pending_retract_marker": 1, "claim_bearing_current": 4}
        before = signals.taint_rate(m)
        perturbed = dict(m)
        perturbed["claim_bearing_pending_retract_marker"] = 2
        after = signals.taint_rate(perturbed)
        assert before != after, (
            "control failed to detect the perturbation: "
            f"before={before!r} after={after!r}"
        )
