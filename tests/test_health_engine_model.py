"""Tests for health/model.py — the scoring-model file (engram-alpha
#1991, PR5 of 5, "the dream-side contract"; round 2 = engram-alpha#2520;
round 3 = engram-alpha#1991/#2520, Kepler's ruling, 2026-09-03).

Pattern note, UPDATED FOR ROUND 3: round 2's docstring here said
``ScoringModel.__post_init__`` ran ``validate_model(self)`` on EVERY
construction, so every negative test wrapped the FIXTURE CONSTRUCTION
itself in ``pytest.raises``. Round 3 REMOVED that ``__post_init__`` call
(see ``ScoringModel``'s own docstring for why: ``validate_model`` now needs
a real ``EngineConfig`` to cross-check a detector's population, which does
not exist at bare ``ScoringModel`` construction time). Every negative test
below now constructs the (unchecked) ``ScoringModel`` fixture UNGUARDED,
then wraps the explicit ``load(model, config)`` call in ``pytest.raises`` —
an "invalid" ``ScoringModel`` CAN now exist as a live, unvalidated object
(that is the whole point of the ``MODEL_SPEC``/``load()`` split), it is only
``load()`` that rejects it. Positive tests likewise now call ``load()``
explicitly (not merely constructing the model) to actually exercise
validation succeeding, mirroring test_health_engine_config.py's own
"positive tests are unaffected: successful construction already proves the
config is valid" — that sentence is no longer true HERE post round-3, so
positive tests changed to make the equivalent claim via an explicit
``load()`` call instead.

Covers:
  1. The real production model SPEC (``health.model.MODEL_SPEC``) —
     shape (3 aspects, 9 metrics), points sum to 100, every admitted metric
     has a registered detector + repair, [engram-alpha#2518] exactly the
     two named v1 curves are used (badness_default for badness-rate
     metrics, goodness_default for the four retrievability coverage-style
     metrics), the load-bearing provenance_integrity "zero admitted
     metrics" fact (documented, not silently asserted only in a
     docstring), AND [round 3] that it cross-validates cleanly against the
     REAL ``health.engine.CONFIG`` via ``load()``.
  2. ``validate_model``/``load`` — one test per contract clause, built from
     small FAKE fixture ``ScoringModel``s + a matching fake ``EngineConfig``
     (never referencing the real 9 metrics), mirroring
     test_health_engine_config.py's fixture style. A multi-violation test
     proves ALL violations are collected in one ``ModelError``, not just the
     first.
  3. [round 2, engram-alpha#2520] The zero-admitted-aspect WARNING (design
     ruling item 3) — fires, does not raise, names the aspect(s).
  4. [round 3, replaces round 2's row-local check] A detector's declared
     population must match the metric's REAL ``denominator_population``, as
     declared on the real ``EngineConfig`` passed into ``load()``/
     ``validate_model`` — round 2's version of this check compared a
     detector's population against a SECOND, independently-declared
     ``population`` field on the same ``MetricBinding`` row (removed this
     round; see ``MetricBinding``'s own docstring for why that check could
     not catch a metric-vs-real-denominator drift).
"""

from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from health import model as hem  # noqa: E402
from health.config import AspectDef, EngineConfig, MetricDef  # noqa: E402
from health.curves import CurveSpec, GOODNESS_DEFAULT_CURVE, piecewise_linear  # noqa: E402
from health.model import (  # noqa: E402
    MODEL_SPEC,
    AspectSpec,
    DetectorRef,
    MetricBinding,
    ModelError,
    ScoringModel,
    load,
    validate_model,
)


# ---------------------------------------------------------------------------
# Small fake fixtures — a trivial curve + trivial signal, real module-level
# functions (not lambdas) so any __module__-style check elsewhere would see
# a genuine importable name, mirroring test_health_engine_config.py's own
# reasoning for using real functions over inline lambdas.
# ---------------------------------------------------------------------------


def _fake_signal_ok(conn):
    return 0.5


_FAKE_CURVE = CurveSpec(
    name="fake_curve",
    # curve_fn is now DERIVED from knots (engram-alpha#2576); these two
    # knots reproduce the old `lambda x: 1.0 - x` EXACTLY over [0.0, 1.0].
    knots=((0.0, 1.0), (1.0, 0.0)),
    input_domain=(0.0, 1.0),
    monotone_direction="decreasing",
)


def _fake_curves():
    return {"fake_curve": _FAKE_CURVE}


def _fake_signals(*names):
    return {name: _fake_signal_ok for name in names}


# Generic fake population, shared by a MetricBinding's detector(s) and the
# matching fake EngineConfig fixture below, in fixtures that aren't
# specifically testing the population-mismatch check below — keeps every
# OTHER test focused on the ONE violation it names, per this file's own
# "one test per contract clause" precedent. [round 3] MUST be a genuine
# member of health.config.DECLARED_POPULATIONS (unlike round 2's
# arbitrary "pop" string) because it is now used to build a real
# EngineConfig fixture, and EngineConfig's OWN validate_config rejects an
# undeclared population at construction time.
_FAKE_POP = "observations"


def _fake_config_def(pop_by_signal: dict[str, str]) -> EngineConfig:
    """[round 3] Build a minimal, REAL ``EngineConfig`` declaring one
    ``MetricDef`` per (signal-name -> denominator_population) pair — the
    real cross-module source of truth ``validate_model`` (round 3) now
    checks a detector's declared population against. Mirrors
    test_health_engine_config.py's own ``_metric``/``_aspect``/``_config``
    fixture-builder style.
    """
    def _sig(conn):
        return 0.5

    def _scoring(raw):
        return raw

    metric_defs = tuple(
        MetricDef(
            name=name,
            signal_fn=_sig,
            denominator_population=pop,
            empty_denominator="clean",
            scored=True,
            state_valued=True,
        )
        for name, pop in pop_by_signal.items()
    )
    aspect = AspectDef(
        name="fake_aspect", weight=100.0, metrics=metric_defs, scoring_fn=_scoring
    )
    return EngineConfig(
        schema_version="test-1", engine_version="test-1", aspects=(aspect,)
    )


def _config_matching(model: ScoringModel) -> EngineConfig:
    """[round 3] Derive a fake ``EngineConfig`` that AGREES with every
    metric binding's own declared detector population (falling back to
    ``_FAKE_POP`` for a metric with no detector at all, where the value is
    irrelevant since no population check fires for it) — makes the
    round-3 cross-config check a no-op for any test that isn't specifically
    exercising IT, so every other negative test still isolates the ONE
    violation it names, per this file's "one test per contract clause"
    precedent.
    """
    pop_by_signal: dict[str, str] = {}
    for m in model.metrics:
        pop = m.detector.population if m.detector is not None else _FAKE_POP
        pop_by_signal.setdefault(m.signal, pop)
    return _fake_config_def(pop_by_signal)


# ---------------------------------------------------------------------------
# 1. The real production MODEL
# ---------------------------------------------------------------------------


class TestRealModelShape:
    def test_model_is_a_scoring_model(self):
        assert isinstance(MODEL_SPEC, ScoringModel)

    def test_three_aspects_with_draft2_points(self):
        points = {a.name: a.points for a in MODEL_SPEC.aspects}
        assert points == {
            "epistemic_integrity": 40.0,
            "provenance_integrity": 25.0,
            "retrievability": 35.0,
        }

    def test_nine_metric_bindings_unmerged(self):
        """Clarification 5: taint_rate / stale_derivation_rate stay two
        separate metrics — the pending_marker_ratio merge is NOT this PR's
        job."""
        assert len(MODEL_SPEC.metrics) == 9
        assert {m.signal for m in MODEL_SPEC.metrics} == {
            "stale_derivation_rate",
            "taint_rate",
            "open_contradiction_rate",
            "uncited_observation_rate",
            "dangling_evidence_rate",
            "recall_summary_coverage",
            "recall_keywords_coverage",
            "embedding_coverage",
            "giant_component_fraction",
        }

    def test_every_metric_uses_one_of_the_four_v1_2_curves(self):
        """Clarification 3 (originally: exactly ONE named curve for v1,
        shared by every metric) — AMENDED by engram-alpha#2518: raw signal
        = literal meaning of its own name; direction/polarity moved into
        the curve. AMENDED AGAIN by engram-alpha#2905 PR-A (the 2026-09-13
        Lei+Kepler curves session): two more named curves joined the
        registry (``badness_pending_premise``, ``goodness_coverage``), and
        five of the nine real metrics rebound to them — see
        tests/test_health_model_defs.py's ``TestV1_2CurveRebindings`` for
        the direct per-metric assertions. Still a small,
        named-curve-per-family v1 simplification, not per-metric
        differentiation — now four named curves rather than two."""
        assert {m.curve for m in MODEL_SPEC.metrics} == {
            "badness_default",
            "goodness_default",
            "badness_pending_premise",
            "goodness_coverage",
        }
        assert set(MODEL_SPEC.curves.keys()) == {
            "badness_default",
            "goodness_default",
            "badness_pending_premise",
            "goodness_coverage",
        }

    def test_retrievability_metrics_curve_bindings(self):
        """engram-alpha#2518 ESTABLISHED "goodness_default" for all four
        retrievability metrics; engram-alpha#2905 PR-A (2026-09-13) moved
        three of those four (embedding_coverage, recall_summary_coverage,
        recall_keywords_coverage) onto the new "goodness_coverage" curve,
        leaving giant_component_fraction alone on "goodness_default". On
        the badness side, PR-A also moved taint_rate/stale_derivation_rate
        off "badness_default" onto the new "badness_pending_premise" curve,
        leaving open_contradiction_rate/uncited_observation_rate/
        dangling_evidence_rate as the only metrics still on
        "badness_default" — see tests/test_health_model_defs.py's
        ``TestV1_2CurveRebindings`` for the direct per-metric assertions
        this test summarizes at the curve-membership level."""
        goodness_coverage_bound = {
            m.signal for m in MODEL_SPEC.metrics if m.curve == "goodness_coverage"
        }
        assert goodness_coverage_bound == {
            "embedding_coverage",
            "recall_summary_coverage",
            "recall_keywords_coverage",
        }
        goodness_default_bound = {
            m.signal for m in MODEL_SPEC.metrics if m.curve == "goodness_default"
        }
        assert goodness_default_bound == {"giant_component_fraction"}
        badness_pending_premise_bound = {
            m.signal for m in MODEL_SPEC.metrics if m.curve == "badness_pending_premise"
        }
        assert badness_pending_premise_bound == {
            "taint_rate",
            "stale_derivation_rate",
        }
        badness_default_bound = {
            m.signal for m in MODEL_SPEC.metrics if m.curve == "badness_default"
        }
        assert badness_default_bound == {
            "open_contradiction_rate",
            "uncited_observation_rate",
            "dangling_evidence_rate",
        }

    def test_goodness_default_curve_resolves_in_curves_registry(self):
        """engram-alpha#2518: CURVES["goodness_default"] resolves to the
        real shipped GOODNESS_DEFAULT_CURVE object, same pattern as the
        existing badness_default resolution."""
        assert MODEL_SPEC.curves["goodness_default"] is GOODNESS_DEFAULT_CURVE
        assert MODEL_SPEC.curves["goodness_default"].monotone_direction == "increasing"

    def test_five_admitted_four_diagnostic(self):
        """engram-alpha#2905 PR-A (2026-09-13, Lei's ruling): open_
        contradiction_rate moved from admitted (importance 1.0) to
        diagnostic HOLD-OUT (importance 0.0) -- six-admitted/three-
        diagnostic (this test's pre-PR-A name and shape) is now
        five-admitted/four-diagnostic."""
        admitted = [m for m in MODEL_SPEC.metrics if m.importance > 0]
        diagnostic = [m for m in MODEL_SPEC.metrics if m.importance <= 0]
        assert {m.signal for m in admitted} == {
            "taint_rate",
            "stale_derivation_rate",
            "embedding_coverage",
            "recall_summary_coverage",
            "recall_keywords_coverage",
        }
        assert {m.signal for m in diagnostic} == {
            "open_contradiction_rate",
            "uncited_observation_rate",
            "dangling_evidence_rate",
            "giant_component_fraction",
        }

    def test_every_admitted_metric_has_registered_detector_and_repair(self):
        for m in MODEL_SPEC.metrics:
            if m.importance > 0:
                assert m.detector is not None
                assert m.detector.name in hem.REGISTERED_DETECTORS
                assert m.repair is not None
                assert m.repair in hem.REGISTERED_REPAIRS

    def test_every_detector_population_matches_the_real_config_denominator(self):
        """[round 3, replaces round 2's row-local version of this test]
        Kepler's ruling invariant, re-confirmed directly against the real
        model spec and the real ``health.engine.CONFIG`` (not just
        exercised indirectly via ``health/engine.py`` importing cleanly) —
        every detector / additional_detector's declared population equals
        the metric's REAL ``denominator_population``, read from the real
        ``EngineConfig`` via ``config.metric()`` — not a second,
        independently-declared field on the same ``MetricBinding`` row
        (round 2's version, removed this round)."""
        from health.engine import CONFIG

        for m in MODEL_SPEC.metrics:
            config_metric = CONFIG.metric(m.signal)
            assert config_metric is not None, (
                f"{m.signal!r} has no matching MetricDef in the real CONFIG"
            )
            if m.detector is not None:
                assert m.detector.population == config_metric.denominator_population
            if m.additional_detector is not None:
                assert (
                    m.additional_detector.population
                    == config_metric.denominator_population
                )

    def test_stale_derivation_rate_has_no_additional_detector(self):
        """Fold #1 (Kepler via Borges, addendum 2 on #1991, 2026-09-03):
        the 'lottery:stale-load-bearing' additional route is DROPPED —
        'category 3 in the lottery is a different notion of stale than
        this metric measures'. scan:cascade-frontier remains its sole
        (primary) detector."""
        binding = hem.METRIC_BY_SIGNAL["stale_derivation_rate"]
        assert binding.detector == DetectorRef(
            name="scan:cascade-frontier", population="derivations"
        )
        assert binding.additional_detector is None

    def test_retrievability_reference_metric_is_embedding_coverage(self):
        aspect = next(a for a in MODEL_SPEC.aspects if a.name == "retrievability")
        assert aspect.reference_metric == "embedding_coverage"
        binding = next(
            m for m in MODEL_SPEC.metrics
            if m.aspect == "retrievability" and m.signal == "embedding_coverage"
        )
        assert binding.importance == 1.0

    def test_epistemic_and_provenance_have_no_reference_metric(self):
        """JUDGMENT CALL (flagged in the PR5 report): reference_metric is
        OPTIONAL and only set where draft-2's own table names one."""
        for name in ("epistemic_integrity", "provenance_integrity"):
            aspect = next(a for a in MODEL_SPEC.aspects if a.name == name)
            assert aspect.reference_metric is None

    def test_provenance_integrity_has_zero_admitted_metrics(self):
        """LOAD-BEARING DISCOVERED CONSEQUENCE (see health/engine.py's
        module docstring): under the v1 metric table, provenance_integrity's
        both metrics ship at importance 0 — this aspect has NO admitted
        metric today. This is a documentation-as-test guard, mirroring this
        codebase's own "make discovered behavior visible via a test, not
        just a comment" convention (see e.g. tests/
        test_health_engine_checkpoint_wiring.py's pre_sleep-absence test):
        if a future PR gives this aspect an admitted metric, this test
        should be updated deliberately, not left silently stale."""
        admitted = [
            m for m in MODEL_SPEC.metrics
            if m.aspect == "provenance_integrity" and m.importance > 0
        ]
        assert admitted == []

    def test_model_load_against_real_config_is_the_validation_gate(self):
        """[round 3] Mirrors health.config.EngineConfig's own
        discipline, adapted to this module's new construct/validate split
        (see ScoringModel's own docstring for why the split exists):
        ``health/engine.py`` importing cleanly already proves the happy
        path (MODEL_SPEC loads against the real CONFIG without raising);
        this asserts the mechanism directly, re-running ``load()`` (which
        re-runs ``validate_model``) explicitly against the already-valid
        MODEL_SPEC + the real CONFIG — must not raise (defense-in-depth
        check, same posture as health.config.load_config's own
        redundant validate_config call)."""
        from health.engine import CONFIG

        load(MODEL_SPEC, CONFIG)


# ---------------------------------------------------------------------------
# 1b. [round 2, engram-alpha#2520] Zero-admitted-aspect WARNING — fires,
# never raises, names the aspect(s).
# ---------------------------------------------------------------------------


class TestZeroAdmittedAspectWarning:
    def test_real_model_warns_naming_provenance_integrity(self, capsys):
        from health.engine import CONFIG

        load(MODEL_SPEC, CONFIG)  # must not raise
        captured = capsys.readouterr()
        assert "provenance_integrity" in captured.err

    def test_fake_model_with_zero_admitted_aspect_warns_naming_it(self, capsys):
        model = ScoringModel(
            curves=_fake_curves(),
            signals=_fake_signals("s1"),
            aspects=(AspectSpec(name="unscored_aspect", points=100.0),),
            metrics=(
                MetricBinding(
                    signal="s1", aspect="unscored_aspect", curve="fake_curve",
                    importance=0.0,
                ),
            ),
        )
        load(model, _config_matching(model))
        captured = capsys.readouterr()
        assert "unscored_aspect" in captured.err

    def test_fully_admitted_model_does_not_warn(self, capsys):
        model = ScoringModel(
            curves=_fake_curves(),
            signals=_fake_signals("s1"),
            aspects=(AspectSpec(name="a", points=100.0),),
            metrics=(
                MetricBinding(
                    signal="s1", aspect="a", curve="fake_curve",
                    importance=1.0,
                    detector=DetectorRef(name="reflect", population=_FAKE_POP),
                    repair="tool:resolve",
                ),
            ),
        )
        load(model, _config_matching(model))
        captured = capsys.readouterr()
        assert captured.err == ""

    def test_multi_aspect_warning_names_every_zero_admitted_aspect(self, capsys):
        model = ScoringModel(
            curves=_fake_curves(),
            signals=_fake_signals("s1", "s2"),
            aspects=(
                AspectSpec(name="empty_a", points=50.0),
                AspectSpec(name="empty_b", points=50.0),
            ),
            metrics=(
                MetricBinding(signal="s1", aspect="empty_a", curve="fake_curve",
                              importance=0.0),
                MetricBinding(signal="s2", aspect="empty_b", curve="fake_curve",
                              importance=0.0),
            ),
        )
        load(model, _config_matching(model))
        captured = capsys.readouterr()
        assert "empty_a" in captured.err
        assert "empty_b" in captured.err


# ---------------------------------------------------------------------------
# 2. validate_model — one test per contract clause, small fake fixtures
# ---------------------------------------------------------------------------


class TestValidateModelPositive:
    def test_minimal_valid_model_constructs(self):
        model = ScoringModel(
            curves=_fake_curves(),
            signals=_fake_signals("s1"),
            aspects=(AspectSpec(name="a", points=100.0),),
            metrics=(
                MetricBinding(
                    signal="s1", aspect="a", curve="fake_curve",
                    importance=1.0,
                    detector=DetectorRef(name="reflect", population=_FAKE_POP),
                    repair="tool:resolve",
                ),
            ),
        )
        assert isinstance(model, ScoringModel)
        load(model, _config_matching(model))  # must not raise

    def test_diagnostic_metric_needs_no_detector_or_repair(self):
        model = ScoringModel(
            curves=_fake_curves(),
            signals=_fake_signals("s1"),
            aspects=(AspectSpec(name="a", points=100.0),),
            metrics=(
                MetricBinding(
                    signal="s1", aspect="a", curve="fake_curve",
                    importance=0.0,
                ),
            ),
        )
        assert model.metrics[0].detector is None
        load(model, _config_matching(model))  # must not raise

    def test_reference_metric_none_is_allowed(self):
        model = ScoringModel(
            curves=_fake_curves(),
            signals=_fake_signals("s1"),
            aspects=(AspectSpec(name="a", points=100.0, reference_metric=None),),
            metrics=(
                MetricBinding(signal="s1", aspect="a", curve="fake_curve",
                              importance=0.0),
            ),
        )
        assert model.aspects[0].reference_metric is None
        load(model, _config_matching(model))  # must not raise

    def test_additional_detector_with_matching_population_is_allowed(self):
        model = ScoringModel(
            curves=_fake_curves(),
            signals=_fake_signals("s1"),
            aspects=(AspectSpec(name="a", points=100.0),),
            metrics=(
                MetricBinding(
                    signal="s1", aspect="a", curve="fake_curve",
                    importance=1.0,
                    detector=DetectorRef(name="reflect", population=_FAKE_POP),
                    additional_detector=DetectorRef(
                        name="lottery:tainted-valid", population=_FAKE_POP
                    ),
                    repair="tool:resolve",
                ),
            ),
        )
        assert model.metrics[0].additional_detector.population == _FAKE_POP
        load(model, _config_matching(model))  # must not raise


class TestValidateModelNegative:
    def test_empty_aspects_rejected(self):
        model = ScoringModel(
            curves=_fake_curves(), signals=_fake_signals("s1"),
            aspects=(), metrics=(),
        )
        # A trivial, unrelated-to-the-model config — never reached, since
        # the empty-aspects violation fires before the per-metric loop ever
        # calls config.metric() (and _config_matching(model) would itself
        # fail to build here: an EngineConfig with zero-metric aspects is
        # rejected by EngineConfig's OWN validate_config).
        with pytest.raises(ModelError, match="model.aspects is empty"):
            load(model, _fake_config_def({"s1": _FAKE_POP}))

    def test_empty_metrics_rejected(self):
        model = ScoringModel(
            curves=_fake_curves(), signals=_fake_signals("s1"),
            aspects=(AspectSpec(name="a", points=100.0),), metrics=(),
        )
        with pytest.raises(ModelError, match="model.metrics is empty"):
            load(model, _fake_config_def({"s1": _FAKE_POP}))

    def test_points_must_sum_to_100(self):
        model = ScoringModel(
            curves=_fake_curves(),
            signals=_fake_signals("s1", "s2"),
            aspects=(
                AspectSpec(name="a", points=60.0),
                AspectSpec(name="b", points=60.0),
            ),
            metrics=(
                MetricBinding(
                    signal="s1", aspect="a", curve="fake_curve",
                    importance=1.0,
                    detector=DetectorRef(name="reflect", population=_FAKE_POP),
                    repair="tool:resolve",
                ),
                MetricBinding(
                    signal="s2", aspect="b", curve="fake_curve",
                    importance=1.0,
                    detector=DetectorRef(name="reflect", population=_FAKE_POP),
                    repair="tool:resolve",
                ),
            ),
        )
        with pytest.raises(ModelError, match="points"):
            load(model, _config_matching(model))

    def test_undeclared_aspect_rejected(self):
        model = ScoringModel(
            curves=_fake_curves(), signals=_fake_signals("s1"),
            aspects=(AspectSpec(name="a", points=100.0),),
            metrics=(
                MetricBinding(signal="s1", aspect="nope", curve="fake_curve",
                              importance=0.0),
            ),
        )
        with pytest.raises(ModelError, match="undeclared aspect"):
            load(model, _config_matching(model))

    def test_undeclared_curve_rejected(self):
        model = ScoringModel(
            curves=_fake_curves(), signals=_fake_signals("s1"),
            aspects=(AspectSpec(name="a", points=100.0),),
            metrics=(
                MetricBinding(signal="s1", aspect="a", curve="nope",
                              importance=0.0),
            ),
        )
        with pytest.raises(ModelError, match="undeclared curve"):
            load(model, _config_matching(model))

    def test_signal_not_in_registry_rejected(self):
        model = ScoringModel(
            curves=_fake_curves(), signals=_fake_signals("other"),
            aspects=(AspectSpec(name="a", points=100.0),),
            metrics=(
                MetricBinding(signal="s1", aspect="a", curve="fake_curve",
                              importance=0.0),
            ),
        )
        with pytest.raises(ModelError, match="signals registry"):
            load(model, _config_matching(model))

    def test_duplicate_signal_aspect_pair_rejected(self):
        model = ScoringModel(
            curves=_fake_curves(), signals=_fake_signals("s1"),
            aspects=(AspectSpec(name="a", points=100.0),),
            metrics=(
                MetricBinding(signal="s1", aspect="a", curve="fake_curve",
                              importance=0.0),
                MetricBinding(signal="s1", aspect="a", curve="fake_curve",
                              importance=0.0),
            ),
        )
        with pytest.raises(ModelError, match="duplicate"):
            load(model, _config_matching(model))

    def test_admitted_without_detector_rejected(self):
        model = ScoringModel(
            curves=_fake_curves(), signals=_fake_signals("s1"),
            aspects=(AspectSpec(name="a", points=100.0),),
            metrics=(
                MetricBinding(signal="s1", aspect="a", curve="fake_curve",
                              importance=1.0,
                              repair="tool:resolve"),
            ),
        )
        with pytest.raises(ModelError, match="no detector"):
            load(model, _config_matching(model))

    def test_admitted_without_repair_rejected(self):
        model = ScoringModel(
            curves=_fake_curves(), signals=_fake_signals("s1"),
            aspects=(AspectSpec(name="a", points=100.0),),
            metrics=(
                MetricBinding(
                    signal="s1", aspect="a", curve="fake_curve",
                    importance=1.0,
                    detector=DetectorRef(name="reflect", population=_FAKE_POP),
                ),
            ),
        )
        with pytest.raises(ModelError, match="no repair"):
            load(model, _config_matching(model))

    def test_unregistered_detector_name_rejected(self):
        model = ScoringModel(
            curves=_fake_curves(), signals=_fake_signals("s1"),
            aspects=(AspectSpec(name="a", points=100.0),),
            metrics=(
                MetricBinding(
                    signal="s1", aspect="a", curve="fake_curve",
                    importance=1.0,
                    detector=DetectorRef(name="scan:not-a-real-one", population=_FAKE_POP),
                    repair="tool:resolve",
                ),
            ),
        )
        with pytest.raises(ModelError, match="REGISTERED_DETECTORS"):
            load(model, _config_matching(model))

    def test_unregistered_additional_detector_name_rejected(self):
        model = ScoringModel(
            curves=_fake_curves(), signals=_fake_signals("s1"),
            aspects=(AspectSpec(name="a", points=100.0),),
            metrics=(
                MetricBinding(
                    signal="s1", aspect="a", curve="fake_curve",
                    importance=1.0,
                    detector=DetectorRef(name="reflect", population=_FAKE_POP),
                    additional_detector=DetectorRef(
                        name="lottery:not-a-real-one", population=_FAKE_POP
                    ),
                    repair="tool:resolve",
                ),
            ),
        )
        with pytest.raises(ModelError, match="additional_detector"):
            load(model, _config_matching(model))

    def test_unregistered_repair_name_rejected(self):
        model = ScoringModel(
            curves=_fake_curves(), signals=_fake_signals("s1"),
            aspects=(AspectSpec(name="a", points=100.0),),
            metrics=(
                MetricBinding(
                    signal="s1", aspect="a", curve="fake_curve",
                    importance=1.0,
                    detector=DetectorRef(name="reflect", population=_FAKE_POP),
                    repair="tool:not-a-real-one",
                ),
            ),
        )
        with pytest.raises(ModelError, match="REGISTERED_REPAIRS"):
            load(model, _config_matching(model))

    def test_negative_importance_rejected(self):
        model = ScoringModel(
            curves=_fake_curves(), signals=_fake_signals("s1"),
            aspects=(AspectSpec(name="a", points=100.0),),
            metrics=(
                MetricBinding(signal="s1", aspect="a", curve="fake_curve",
                              importance=-1.0),
            ),
        )
        with pytest.raises(ModelError, match="importance must be >= 0"):
            load(model, _config_matching(model))

    def test_reference_metric_not_bound_to_aspect_rejected(self):
        model = ScoringModel(
            curves=_fake_curves(), signals=_fake_signals("s1"),
            aspects=(AspectSpec(name="a", points=100.0, reference_metric="nope"),),
            metrics=(
                MetricBinding(
                    signal="s1", aspect="a", curve="fake_curve",
                    importance=1.0,
                    detector=DetectorRef(name="reflect", population=_FAKE_POP),
                    repair="tool:resolve",
                ),
            ),
        )
        with pytest.raises(ModelError, match="does not name a metric bound"):
            load(model, _config_matching(model))

    def test_reference_metric_not_admitted_rejected(self):
        model = ScoringModel(
            curves=_fake_curves(), signals=_fake_signals("s1"),
            aspects=(AspectSpec(name="a", points=100.0, reference_metric="s1"),),
            metrics=(
                MetricBinding(signal="s1", aspect="a", curve="fake_curve",
                              importance=0.0),
            ),
        )
        with pytest.raises(ModelError, match="is not admitted"):
            load(model, _config_matching(model))

    def test_all_violations_collected_in_one_error(self):
        """Mirrors health/config.py's own "see the whole problem at
        once" contract — a model with TWO simultaneous violations (bad
        points sum + admitted metric missing detector/repair) raises ONE
        ModelError naming both, not two separate failures."""
        model = ScoringModel(
            curves=_fake_curves(),
            signals=_fake_signals("s1"),
            aspects=(AspectSpec(name="a", points=50.0),),
            metrics=(
                MetricBinding(signal="s1", aspect="a", curve="fake_curve",
                              importance=1.0),
            ),
        )
        with pytest.raises(ModelError) as exc_info:
            load(model, _config_matching(model))
        message = str(exc_info.value)
        assert "points" in message
        assert "no detector" in message
        assert "no repair" in message

    # -----------------------------------------------------------------------
    # [round 3, replaces round 2's row-local version] Detector population
    # must match the metric's REAL denominator_population, read from the
    # real EngineConfig — a NEW validator rule (Kepler's ruling on
    # engram-alpha#1991/#2520, 2026-09-03).
    # -----------------------------------------------------------------------

    def test_detector_population_mismatch_rejected(self):
        """[round 3] The cross-module drift check Kepler's ruling asked
        for, replacing round 2's row-local version: a detector's declared
        population must equal the metric's REAL ``denominator_population``,
        as declared on the real ``EngineConfig`` — 'the check is only a
        check if it reads that field'. Here the config declares
        "claim_bearing_nodes" for ``s1`` while the detector claims
        "observations" — a genuine cross-module drift, not merely two
        fields on the same row disagreeing with each other."""
        model = ScoringModel(
            curves=_fake_curves(), signals=_fake_signals("s1"),
            aspects=(AspectSpec(name="a", points=100.0),),
            metrics=(
                MetricBinding(
                    signal="s1", aspect="a", curve="fake_curve",
                    importance=1.0,
                    detector=DetectorRef(name="reflect", population="observations"),
                    repair="tool:resolve",
                ),
            ),
        )
        config = _fake_config_def({"s1": "claim_bearing_nodes"})
        with pytest.raises(
            ModelError, match="does not match the real denominator_population"
        ):
            load(model, config)

    def test_additional_detector_population_mismatch_rejected(self):
        model = ScoringModel(
            curves=_fake_curves(), signals=_fake_signals("s1"),
            aspects=(AspectSpec(name="a", points=100.0),),
            metrics=(
                MetricBinding(
                    signal="s1", aspect="a", curve="fake_curve",
                    importance=1.0,
                    detector=DetectorRef(name="reflect", population="claim_bearing_nodes"),
                    additional_detector=DetectorRef(
                        name="lottery:tainted-valid", population="observations"
                    ),
                    repair="tool:resolve",
                ),
            ),
        )
        # config agrees with the PRIMARY detector's population
        # ("claim_bearing_nodes") — only the additional_detector disagrees.
        config = _fake_config_def({"s1": "claim_bearing_nodes"})
        with pytest.raises(
            ModelError, match="does not match the real denominator_population"
        ):
            load(model, config)

    def test_metric_not_declared_in_config_is_its_own_violation(self):
        """[round 3, new] ``config.metric(m.signal)`` returning ``None`` —
        no matching MetricDef declared anywhere in the real EngineConfig —
        is itself collected as a violation, not silently skipped (which
        would let a detector's population go completely unchecked for a
        metric absent from config)."""
        model = ScoringModel(
            curves=_fake_curves(), signals=_fake_signals("s1"),
            aspects=(AspectSpec(name="a", points=100.0),),
            metrics=(
                MetricBinding(
                    signal="s1", aspect="a", curve="fake_curve",
                    importance=1.0,
                    detector=DetectorRef(name="reflect", population=_FAKE_POP),
                    repair="tool:resolve",
                ),
            ),
        )
        # A config that declares a DIFFERENT metric name entirely ("other"),
        # never "s1" — config.metric("s1") must return None.
        config = _fake_config_def({"other": _FAKE_POP})
        with pytest.raises(ModelError, match="is not declared in the EngineConfig"):
            load(model, config)
