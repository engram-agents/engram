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

import dataclasses
import os
import sqlite3
import sys
from unittest import mock

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
# #2565: append (not insert(0)) -- putting src/engram at position 0
# outranks the repo-root tools/ package for the rest of the process,
# a live instance of the tools/-package-name-clash class found while
# building #2565's durable guard (tests/conftest.py).
sys.path.append(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src", "engram"))

from health import engine as health_engine  # noqa: E402
from health import measurements as health_measurements  # noqa: E402
from health import signals  # noqa: E402
from health.config import UNAVAILABLE  # noqa: E402
from health.signals import SIGNAL_DEFS, _build, compute_all_signals  # noqa: E402

from test_health_measurements import _build_populated_fixture  # noqa: E402


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
    """The three coverage signals (engram-alpha#2518 polarity flip): 1.0 =
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


# ---------------------------------------------------------------------------
# SignalDef -- declarative L1 arithmetic (engram-alpha#2518)
# ---------------------------------------------------------------------------


class TestSignalDefsRegisteredAgainstL0:
    """Every SIGNAL_DEFS numerator/denominator/gate key must be a real,
    registered L0 measurement name -- so a typo'd key fails HERE, at test
    time, rather than surfacing later as a plausible-looking wrong number
    (or a KeyError deep in build_snapshot)."""

    def test_every_key_is_a_registered_measurement(self):
        registered = set(health_measurements.MEASUREMENTS.keys())
        for name, d in signals.SIGNAL_DEFS.items():
            assert d.numerator in registered, (
                f"{name}: numerator {d.numerator!r} not in MEASUREMENTS"
            )
            assert d.denominator in registered, (
                f"{name}: denominator {d.denominator!r} not in MEASUREMENTS"
            )
            for gate in d.gates:
                assert gate in registered, (
                    f"{name}: gate {gate!r} not in MEASUREMENTS"
                )


class TestSignalsBuiltFromSignalDefs:
    def test_signals_keys_match_signal_defs_keys(self):
        assert set(signals.SIGNALS.keys()) == set(signals.SIGNAL_DEFS.keys())

    def test_each_alias_is_its_signals_entry(self):
        for name in signals.SIGNAL_DEFS:
            alias = getattr(signals, name)
            assert alias is signals.SIGNALS[name], (
                f"module-level alias {name!r} is not SIGNALS[{name!r}]"
            )


class TestSignalDefsShape:
    def test_exactly_three_coverage_signals_are_one_minus_rate(self):
        """NOTE (flagged in the coder-fairy handoff for #2518): the task
        brief's test list says "exactly the four coverage signals are
        one_minus_rate", but the actual, verified-against-code arithmetic
        (matching the brief's own per-signal enumeration in its step 2,
        which lists giant_component_fraction's form as "rate") has only
        THREE one_minus_rate signals -- recall_summary_coverage,
        recall_keywords_coverage, embedding_coverage.
        giant_component_fraction is the literal connected fraction with NO
        complement, per its own preserved docstring above. This test
        asserts the real, code-verified set; see the handoff for the
        "four" vs "three" discrepancy."""
        one_minus = {
            name for name, d in signals.SIGNAL_DEFS.items()
            if d.form == "one_minus_rate"
        }
        assert one_minus == {
            "recall_summary_coverage",
            "recall_keywords_coverage",
            "embedding_coverage",
        }
        assert signals.SIGNAL_DEFS["giant_component_fraction"].form == "rate"

    def test_exactly_embedding_coverage_has_gates(self):
        gated = {
            name for name, d in signals.SIGNAL_DEFS.items() if d.gates
        }
        assert gated == {"embedding_coverage"}
        assert signals.SIGNAL_DEFS["embedding_coverage"].gates == (
            "embedder_installed_available",
            "embedder_config_enabled",
        )


class TestFormulaText:
    def test_rate_formula_text(self):
        d = signals.SIGNAL_DEFS["taint_rate"]
        assert d.formula_text == (
            "claim_bearing_pending_retract_marker / claim_bearing_current"
        )

    def test_one_minus_rate_formula_text(self):
        d = signals.SIGNAL_DEFS["recall_summary_coverage"]
        assert d.formula_text == (
            "1 - claim_bearing_without_recall_summary / claim_bearing_current"
        )

    def test_gated_formula_text_appends_gates_in_words(self):
        d = signals.SIGNAL_DEFS["embedding_coverage"]
        assert d.formula_text == (
            "1 - claim_bearing_null_embedding / claim_bearing_current "
            "(requires embedder_installed_available and "
            "embedder_config_enabled)"
        )


class TestSignalDefsJsonable:
    def test_shape(self):
        rendered = signals.signal_defs_jsonable()
        assert set(rendered.keys()) == set(signals.SIGNAL_DEFS.keys())
        for name, entry in rendered.items():
            d = signals.SIGNAL_DEFS[name]
            assert entry["numerator"] == d.numerator
            assert entry["denominator"] == d.denominator
            assert entry["form"] == d.form
            assert entry["gates"] == list(d.gates)
            assert entry["formula"] == d.formula_text
            assert entry["doc"] == d.doc

    def test_doc_nonempty_for_all_nine_signals(self):
        """engram-alpha#2580: signal_defs_jsonable() carries each signal's
        own hand-written doc, non-empty for all nine registered signals."""
        rendered = signals.signal_defs_jsonable()
        assert len(rendered) == 9
        for name, entry in rendered.items():
            assert isinstance(entry["doc"], str) and entry["doc"].strip(), (
                f"{name} has an empty/missing doc"
            )


# ---------------------------------------------------------------------------
# build_snapshot integration -- signal_defs block (minimal throwaway
# fixture DB, mirroring tests/test_health_engine_metrics.py's own DDL
# subset; this file stays otherwise DB-free per its own module docstring).
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


class TestBuildSnapshotSignalDefs:
    def test_signal_defs_present_with_nine_entries_matching_signals(self):
        conn = _make_conn()
        snap = health_engine.build_snapshot(conn, "nap")
        assert "signal_defs" in snap
        assert len(snap["signal_defs"]) == 9
        assert set(snap["signal_defs"].keys()) == set(snap["signals"].keys())


class TestEqualityControlFailsOnPerturbedDefs:
    """Borges's review ask (engram-alpha#2563): a control that has never
    been seen to fail is not a control. ``_verify_l1_against_live_signal_fns``
    (health/engine.py) is the SSoT equality control that compares L1 values
    (computed from stored L0) against the live ``health.metrics`` SQL
    functions on the SAME connection and raises ``HealthEngineSSoTError`` on
    any disagreement. This class shows it actually catches a corrupted
    ``SignalDef`` — reusing the same richly-populated fixture
    ``tests/test_health_measurements.py``'s acceptance test already builds
    (``_build_populated_fixture``), not a synthetic all-nonzero fixture built
    fresh here.

    Two independent perturbations, each shown to change the value on the
    fixture (never vacuous), plus one positive control confirming the
    UNPERTURBED path still passes on the same connection.

    Sibling: `tests/test_health_engine.py::TestBuildSnapshot::test_ssot_mismatch_between_l1_and_live_signal_fn_raises_loudly`
    perturbs the LIVE-SQL arm (the oracle) of the same control by patching
    `_LIVE_SIGNAL_FNS_FOR_VERIFICATION`; this class perturbs the DECLARED
    arm. Together they show the control fails whichever side moves.
    """

    def _embedder_patches(self):
        """Same two-patch embedder-available context
        test_health_measurements.py's own acceptance test uses -- must wrap
        BOTH the L0 measurement read AND the verifier call, since the
        verifier's live path (``health.metrics.embedding_coverage``) checks
        the same environment gate independently; wrapping only the
        measurement read leaves the verifier reading the real (test-suite
        default, order-dependent) embedder-availability state and produces a
        spurious mismatch unrelated to any perturbation."""
        return (
            mock.patch("engram_core._embedder.is_available", return_value=True),
            mock.patch("engram_core._get_embedding_config", return_value={"enabled": True}),
        )

    def test_positive_control_unperturbed_passes(self):
        """The unperturbed path must NOT raise on the same connection —
        the control that shows the verifier isn't just always-failing."""
        conn = _build_populated_fixture()
        patch_a, patch_b = self._embedder_patches()
        with patch_a, patch_b:
            measurements = health_measurements.compute_all_measurements(conn)
            l1 = compute_all_signals(measurements)
            health_engine._verify_l1_against_live_signal_fns(conn, l1)  # must not raise

    def test_recall_summary_coverage_polarity_flip_is_caught(self):
        """Perturbation (a): flip ``recall_summary_coverage``'s ``form``
        from ``one_minus_rate`` to ``rate`` -- inverts its polarity.
        Fixture: claim_bearing_without_recall_summary=1,
        claim_bearing_current=7 -> one_minus_rate=6/7≈0.857, rate=1/7≈0.143
        -- genuinely different, not vacuous."""
        conn = _build_populated_fixture()
        patch_a, patch_b = self._embedder_patches()
        with patch_a, patch_b:
            measurements = health_measurements.compute_all_measurements(conn)
            l1 = compute_all_signals(measurements)

            orig_def = SIGNAL_DEFS["recall_summary_coverage"]
            perturbed_def = dataclasses.replace(orig_def, form="rate")
            perturbed_fn = _build(perturbed_def)
            perturbed_value = perturbed_fn(measurements)

            assert perturbed_value != l1["recall_summary_coverage"], (
                "control setup failed: the polarity flip did not change "
                "the value on this fixture -- the perturbation would be "
                "vacuous"
            )

            perturbed_l1 = dict(l1)
            perturbed_l1["recall_summary_coverage"] = perturbed_value

            with pytest.raises(health_engine.HealthEngineSSoTError):
                health_engine._verify_l1_against_live_signal_fns(conn, perturbed_l1)

    def test_taint_rate_numerator_swap_is_caught(self):
        """Perturbation (b): point ``taint_rate``'s numerator at a
        DIFFERENT registered L0 key.

        DECISION (flagged per the brief's own escape hatch): the brief's own
        example key, ``derivations_pending_supersede_marker``, is vacuous on
        this fixture -- its value is 1, identical to the real numerator
        ``claim_bearing_pending_retract_marker`` (also 1), and both share the
        same denominator (``claim_bearing_current``=7), so the swap produces
        the SAME 1/7 value as the live signal and the verifier would not
        raise. Using ``claim_bearing_without_recall_keywords`` (fixture
        value 2) instead: 2/7≈0.286 vs the live taint_rate's 1/7≈0.143 --
        genuinely different, not vacuous."""
        conn = _build_populated_fixture()
        patch_a, patch_b = self._embedder_patches()
        with patch_a, patch_b:
            measurements = health_measurements.compute_all_measurements(conn)
            l1 = compute_all_signals(measurements)

            orig_def = SIGNAL_DEFS["taint_rate"]
            perturbed_def = dataclasses.replace(
                orig_def, numerator="claim_bearing_without_recall_keywords"
            )
            perturbed_fn = _build(perturbed_def)
            perturbed_value = perturbed_fn(measurements)

            assert perturbed_value != l1["taint_rate"], (
                "control setup failed: the numerator swap did not change "
                "the value on this fixture -- the perturbation would be "
                "vacuous"
            )

            perturbed_l1 = dict(l1)
            perturbed_l1["taint_rate"] = perturbed_value

            with pytest.raises(health_engine.HealthEngineSSoTError):
                health_engine._verify_l1_against_live_signal_fns(conn, perturbed_l1)
