"""Tests for health/config.py — schema, loader, and sanity checks
(engram-alpha#1991 PR1 of 4).

These tests build small FAKE fixture EngineConfigs inline. They never
reference the real health-score aspects/metrics (recall_summary coverage,
DAG violations, stale-derivation rate, etc.) — those don't exist yet; they
land in PR2/PR3. This file only proves the validator + loader themselves.

One test per design-doc §5 contract item (1-6), plus the Borges-regression
test (§5.1) and the multi-violation / loader tests called out in the PR1
spec.

Pattern note: EngineConfig.__post_init__ runs validate_config(self) on
EVERY construction (added after Borges's colleague-review finding on #2187
that constructing EngineConfig directly bypassed validation entirely). So
every negative test below wraps the FIXTURE CONSTRUCTION itself in
pytest.raises, not a separate validate_config(config) call afterward — an
"invalid" EngineConfig can no longer exist as a live object to check later,
only be attempted and rejected. Positive ("_is_ok") tests are unaffected:
successful construction already proves the config is valid.
"""

from __future__ import annotations

import os
import sys
import types

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from health import config as hec  # noqa: E402 — must follow sys.path setup
from health.config import (  # noqa: E402
    UNAVAILABLE,
    AspectDef,
    EngineConfig,
    EngineConfigError,
    MetricDef,
    load_config,
    validate_config,
)


# ---------------------------------------------------------------------------
# Real, importable module-level fixture functions.
#
# These MUST be genuine module-level functions (not lambdas defined inline
# inside a test body) so that fn.__module__ resolves to this test module's
# real name rather than something ambiguous — proving the __module__ check
# in _check_fn accepts a genuinely-imported function the same way it would
# accept a real production signal_fn.
# ---------------------------------------------------------------------------


def _signal_ok(conn) -> float:
    """A trivial, always-succeeding fake signal function."""
    return 0.5


def _scoring_ok(raw: float) -> float:
    """A trivial, always-succeeding fake scoring function."""
    return raw * 100


# ---------------------------------------------------------------------------
# Fixture builders
# ---------------------------------------------------------------------------


def _metric(**overrides) -> MetricDef:
    kwargs = dict(
        name="fake_metric",
        signal_fn=_signal_ok,
        denominator_population="observations",
        empty_denominator="clean",
        scored=True,
        state_valued=True,
    )
    kwargs.update(overrides)
    return MetricDef(**kwargs)


def _aspect(**overrides) -> AspectDef:
    kwargs = dict(
        name="fake_aspect",
        weight=100.0,
        metrics=(_metric(),),
        scoring_fn=_scoring_ok,
    )
    kwargs.update(overrides)
    return AspectDef(**kwargs)


def _config(**overrides) -> EngineConfig:
    kwargs = dict(
        schema_version="test-1",
        engine_version="test-1",
        aspects=(_aspect(),),
    )
    kwargs.update(overrides)
    return EngineConfig(**kwargs)


# ---------------------------------------------------------------------------
# UNAVAILABLE sentinel
# ---------------------------------------------------------------------------


class TestUnavailableSentinel:
    def test_repr(self):
        assert repr(UNAVAILABLE) == "UNAVAILABLE"

    def test_bool_raises(self):
        """The whole point of the sentinel: it must never be usable as a
        falsy/truthy value by accident."""
        with pytest.raises(TypeError):
            bool(UNAVAILABLE)

    def test_bool_raises_in_if_statement(self):
        with pytest.raises(TypeError):
            if UNAVAILABLE:
                pass

    def test_is_not_a_number(self):
        assert UNAVAILABLE != 0
        assert not isinstance(UNAVAILABLE, (int, float))


# ---------------------------------------------------------------------------
# Contract item 4 — non-empty required config sets
# ---------------------------------------------------------------------------


class TestNonEmptyRequiredSets:
    def test_empty_aspects_is_error(self):
        with pytest.raises(EngineConfigError) as exc_info:
            _config(aspects=())
        assert "aspects" in str(exc_info.value)

    def test_aspect_with_empty_metrics_is_error(self):
        with pytest.raises(EngineConfigError) as exc_info:
            _config(aspects=(_aspect(name="hollow_aspect", metrics=()),))
        assert "hollow_aspect" in str(exc_info.value)


# ---------------------------------------------------------------------------
# Contract item 2 — weight sums
# ---------------------------------------------------------------------------


class TestWeightSums:
    def test_aspect_weights_summing_to_90_is_error(self):
        with pytest.raises(EngineConfigError) as exc_info:
            _config(
                aspects=(
                    _aspect(name="a", weight=50.0),
                    _aspect(name="b", weight=40.0),
                )
            )
        assert "weight" in str(exc_info.value)

    def test_aspect_weights_summing_to_100_within_tolerance_is_ok(self):
        _config(
            aspects=(
                _aspect(name="a", weight=33.34),
                _aspect(name="b", weight=33.33),
                _aspect(name="c", weight=33.33),
            )
        )  # must not raise

    def test_zero_weight_is_error(self):
        with pytest.raises(EngineConfigError) as exc_info:
            _config(aspects=(_aspect(name="a", weight=0.0),))
        assert "a" in str(exc_info.value)
        assert "weight" in str(exc_info.value)


# ---------------------------------------------------------------------------
# Contract item 2/6 — every declared fn exists and is callable
# ---------------------------------------------------------------------------


class TestFunctionsMustBeCallable:
    def test_none_signal_fn_is_error(self):
        with pytest.raises(EngineConfigError) as exc_info:
            _config(aspects=(_aspect(metrics=(_metric(signal_fn=None),)),))
        assert "signal_fn" in str(exc_info.value)

    def test_string_signal_fn_is_error(self):
        with pytest.raises(EngineConfigError) as exc_info:
            _config(
                aspects=(
                    _aspect(metrics=(_metric(signal_fn="not_a_function"),)),
                )
            )
        assert "signal_fn" in str(exc_info.value)

    def test_none_scoring_fn_is_error(self):
        with pytest.raises(EngineConfigError) as exc_info:
            _config(aspects=(_aspect(scoring_fn=None),))
        assert "scoring_fn" in str(exc_info.value)

    def test_valid_module_level_fns_are_ok(self):
        _config()  # uses _signal_ok / _scoring_ok, both module-level; must not raise


# ---------------------------------------------------------------------------
# Contract item 2/4 — every denominator is a declared population
# ---------------------------------------------------------------------------


class TestDenominatorMustBeDeclared:
    def test_unregistered_population_is_error(self):
        with pytest.raises(EngineConfigError) as exc_info:
            _config(
                aspects=(
                    _aspect(
                        metrics=(
                            _metric(denominator_population="not_a_real_population"),
                        )
                    ),
                )
            )
        assert "not_a_real_population" in str(exc_info.value)

    def test_declared_population_is_ok(self):
        _config(
            aspects=(
                _aspect(metrics=(_metric(denominator_population="edges"),)),
            )
        )  # must not raise


# ---------------------------------------------------------------------------
# Contract item 5 — empty_denominator must be a VALID declared policy value,
# not just present (reviewer-fairy round 1 finding: the "no default" dataclass
# field only proves the field is declared, not that its value is one of the
# two real policies — a typo'd/bogus value sailed through undetected before
# this check existed).
# ---------------------------------------------------------------------------


class TestEmptyDenominatorMustBeValidPolicy:
    def test_bogus_policy_value_is_error(self):
        with pytest.raises(EngineConfigError) as exc_info:
            _config(
                aspects=(
                    _aspect(
                        metrics=(
                            _metric(empty_denominator="BOGUS_NOT_A_REAL_POLICY"),
                        )
                    ),
                )
            )
        assert "BOGUS_NOT_A_REAL_POLICY" in str(exc_info.value)

    def test_clean_is_ok(self):
        _config(
            aspects=(_aspect(metrics=(_metric(empty_denominator="clean"),)),)
        )  # must not raise

    def test_unavailable_is_ok(self):
        _config(
            aspects=(
                _aspect(metrics=(_metric(empty_denominator="unavailable"),)),
            )
        )  # must not raise


# ---------------------------------------------------------------------------
# Contract item 2 (FOLD) — scored ⇒ state_valued
# ---------------------------------------------------------------------------


class TestScoredImpliesStateValued:
    def test_scored_true_state_valued_false_is_error(self):
        with pytest.raises(EngineConfigError) as exc_info:
            _config(
                aspects=(
                    _aspect(
                        metrics=(_metric(scored=True, state_valued=False),)
                    ),
                )
            )
        msg = str(exc_info.value)
        assert "scored" in msg
        assert "state_valued" in msg

    def test_unscored_diagnostic_with_state_valued_false_is_ok(self):
        """A turn-delta / recency surface: scored=False, state_valued=False
        is the legitimate unscored-diagnostic carve-out (design doc §5.2)."""
        _config(
            aspects=(
                _aspect(
                    metrics=(_metric(scored=False, state_valued=False),)
                ),
            )
        )  # must not raise

    def test_scored_true_state_valued_true_is_ok(self):
        _config(
            aspects=(
                _aspect(metrics=(_metric(scored=True, state_valued=True),)),
            )
        )  # must not raise


# ---------------------------------------------------------------------------
# dive_target — optional, but if present must be a non-empty string
# ---------------------------------------------------------------------------


class TestDiveTarget:
    def test_none_dive_target_is_ok(self):
        _config(
            aspects=(_aspect(metrics=(_metric(dive_target=None),)),)
        )  # must not raise

    def test_non_empty_string_dive_target_is_ok(self):
        _config(
            aspects=(
                _aspect(metrics=(_metric(dive_target="some_fairy_hint"),)),
            )
        )  # must not raise

    def test_empty_string_dive_target_is_error(self):
        with pytest.raises(EngineConfigError) as exc_info:
            _config(aspects=(_aspect(metrics=(_metric(dive_target=""),)),))
        assert "dive_target" in str(exc_info.value)


# ---------------------------------------------------------------------------
# Contract item 5 — empty_denominator has no implicit default
# ---------------------------------------------------------------------------


class TestEmptyDenominatorHasNoDefault:
    def test_metric_def_requires_empty_denominator_positionally_or_by_kwarg(self):
        """empty_denominator has no default value in MetricDef's own
        definition (contract item 5) — omitting it entirely is a TypeError
        from dataclass construction itself, not something validate_config
        needs to check. This test proves the field is actually required."""
        with pytest.raises(TypeError):
            MetricDef(
                name="fake_metric",
                signal_fn=_signal_ok,
                denominator_population="observations",
                # empty_denominator omitted deliberately
                scored=True,
                state_valued=True,
            )


# ---------------------------------------------------------------------------
# The regression test that matters most — Borges's own bug shape
# ---------------------------------------------------------------------------


class TestBorgesRegression:
    """Design doc §5.1: Borges's first prototype read DAG_EXEMPT_RELATIONS
    (a derived frozenset) via regex against its str() form instead of a real
    `in` test against the live object. An empty regex match silently
    produced a wrong score with zero exemptions applied — no exception, no
    validator caught it.

    validate_config must never be implementable as a string/regex proxy over
    DECLARED_POPULATIONS. We prove this three ways:

    1. Confirming the module never imports Python's `re` module at all — an
       AST check, not a substring check (reviewer-fairy round 1 finding: a
       substring check for e.g. "re.search" is evadable by re.fullmatch, an
       aliased import, or a compiled-pattern object's .search() called
       without the "re." prefix; a module with no `re` import whatsoever
       cannot use any of those either).
    2. Directly reading the validate_config source and confirming the
       membership check is a real `in` test against the DECLARED_POPULATIONS
       object.
    3. Monkeypatching a NEW member into DECLARED_POPULATIONS at test time and
       confirming the validator picks it up immediately — which is only
       possible if the check reads the live object, not a stale parsed copy
       (a regex-over-a-string proxy computed once at import time could not
       react to a runtime mutation like this). This is the load-bearing test
       of the three: it proves the actual mechanism of the original Borges
       bug (a value captured once, unable to react to the frozenset
       changing) cannot recur, regardless of how the check is implemented.
    """

    def test_module_never_imports_re_at_all(self):
        import ast
        import inspect

        tree = ast.parse(inspect.getsource(hec))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                assert not any(alias.name == "re" for alias in node.names), (
                    "health/config.py imports the 're' module — this "
                    "reopens exactly the regex/string-proxy failure mode the "
                    "Borges bug (§5.1) came from; use a real 'in' test instead"
                )
            if isinstance(node, ast.ImportFrom):
                assert node.module != "re", (
                    "health/config.py imports from 're' — same "
                    "concern as a bare 'import re'"
                )

    def test_validate_config_checks_denominator_with_real_in_test(self):
        import inspect

        source = inspect.getsource(hec.validate_config)
        assert "DECLARED_POPULATIONS" in source
        assert " in DECLARED_POPULATIONS" in source or "in DECLARED_POPULATIONS)" in source

    def test_membership_check_reads_the_live_object_not_a_stale_copy(
        self, monkeypatch
    ):
        # Baseline: a population not yet declared is rejected — now at
        # CONSTRUCTION time (EngineConfig.__post_init__ validates on every
        # build, per Borges's colleague-review finding on #2187), which
        # means an "invalid" EngineConfig can never exist as a live object
        # to re-check later — only re-attempted. So this test proves live-
        # object reactivity by attempting the SAME construction twice
        # (identical kwargs, fresh objects each time) rather than by
        # holding one object across the monkeypatch — the epistemic content
        # is the same: the same denominator string resolves differently
        # before vs after DECLARED_POPULATIONS changes underneath it.
        def _attempt():
            return _config(
                aspects=(
                    _aspect(
                        metrics=(
                            _metric(denominator_population="brand_new_population"),
                        )
                    ),
                )
            )

        with pytest.raises(EngineConfigError):
            _attempt()

        # Mutate the live DECLARED_POPULATIONS object (simulating a PR2/PR3
        # extension) and confirm the SAME construction now succeeds cleanly,
        # proving the check consults the live object on every call rather
        # than a value captured/parsed once at import time.
        monkeypatch.setattr(
            hec,
            "DECLARED_POPULATIONS",
            hec.DECLARED_POPULATIONS | {"brand_new_population"},
        )
        _attempt()  # must not raise now


# ---------------------------------------------------------------------------
# Multi-violation collection
# ---------------------------------------------------------------------------


class TestMultiViolationCollection:
    def test_two_simultaneous_violations_both_appear_in_one_error(self):
        with pytest.raises(EngineConfigError) as exc_info:
            _config(
                aspects=(
                    _aspect(
                        name="broken_aspect",
                        weight=50.0,  # will fail the weight-sum check (only aspect)
                        metrics=(
                            _metric(
                                denominator_population="not_a_real_population"
                            ),
                        ),
                    ),
                )
            )
        msg = str(exc_info.value)
        assert "weight" in msg
        assert "not_a_real_population" in msg


# ---------------------------------------------------------------------------
# load_config
# ---------------------------------------------------------------------------


class TestLoadConfig:
    def test_module_missing_config_raises_engine_config_error(self):
        fake_module = types.ModuleType("fake_module_without_config")
        with pytest.raises(EngineConfigError):
            load_config(fake_module)

    def test_module_with_non_engine_config_config_raises_engine_config_error(self):
        fake_module = types.ModuleType("fake_module_with_wrong_config")
        fake_module.CONFIG = {"not": "an EngineConfig"}
        with pytest.raises(EngineConfigError):
            load_config(fake_module)

    def test_valid_module_returns_engine_config(self):
        fake_module = types.ModuleType("fake_module_with_valid_config")
        fake_module.CONFIG = _config()
        result = load_config(fake_module)
        assert isinstance(result, EngineConfig)
        assert result is fake_module.CONFIG

    def test_invalid_config_can_never_reach_a_module_at_all(self):
        """This scenario used to be 'a module holds an already-invalid
        EngineConfig; load_config catches it' — now, per
        EngineConfig.__post_init__ (Borges's colleague-review finding on
        #2187), an invalid EngineConfig can never be constructed in the
        first place, so it can never reach `fake_module.CONFIG = ...` to
        begin with. That's the stronger guarantee the fix was for: proving
        it explicitly here, at the exact point the old test assumed the
        invalid object would already exist, rather than silently dropping
        the coverage."""
        fake_module = types.ModuleType("fake_module_with_invalid_config")
        with pytest.raises(EngineConfigError):
            fake_module.CONFIG = _config(aspects=())
        assert not hasattr(fake_module, "CONFIG")
