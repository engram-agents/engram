"""Tests for the repair-pass provenance record schema + validator
(engram-alpha#2519, the health-v2 epic's follow-up to #1991/#2520's PR5).

Scope: ``health.engine.REPAIR_RECORD_TOP_LEVEL_FIELDS`` /
``REPAIR_RECORD_METRIC_FIELDS`` / ``REPAIR_RECORD_STATUS_VALUES`` /
``REPAIR_RECORD_RUN_STATUS_VALUES`` (the schema, declared once so the
dream-master's write path and a future #2518 panel's read path check
against the SAME shape), ``health.engine.validate_repair_record`` (the
schema-conformance check, mirroring ``health.config.validate_config``
/ ``health.model.validate_model``'s "collect every violation into one
pass, don't just raise on the first" contract), and
``health.engine.repair_record_missing_metric_names`` (round 2's completeness
helper — the "every metric gets an entry, always" side of the schema).

File-organization note: this is a NEW, narrowly-scoped test file rather than
an addition to ``test_health_engine.py`` — mirrors ``test_health_engine_
model.py`` being its own file rather than folded into ``test_health_engine_
config.py`` even though both files test closely-related schemas in the same
epic; a genuinely new schema + validator pair gets its own test file in this
codebase's established convention, one file per schema module/section.

Covers:
  1. The schema constants have the expected members (a renamed/typo'd
     member would otherwise only surface as a mysterious validator failure
     elsewhere).
  2. ``validate_repair_record`` accepts a well-formed record (empty
     violation list) — including, [round 2] a record with an
     "unavailable"-status entry carrying a non-null ``reason``, and a
     skip-night record (top-level ``status: "skipped"`` + matching
     per-metric entries).
  3. It rejects: a record missing a top-level field, a record with an
     extra/misspelled metric-level field, a record with an invalid
     per-metric ``status`` value, [round 2] a record with an invalid
     TOP-LEVEL ``status`` value — each violation names the offending
     key/value in the returned string(s).
  4. A record combining TWO simultaneous violations returns BOTH in one
     call (the "collect all, don't short-circuit" contract this codebase
     already tests for its sibling validators — see
     ``TestMultiViolationCollection`` in test_health_engine_config.py and
     the equivalent in test_health_engine_model.py).
  5. [round 2] ``repair_record_missing_metric_names`` reports the sorted set
     of metric names declared in ``MODEL.metrics`` but absent from a given
     record's own ``metrics`` dict — empty on a complete record, every
     declared name on a malformed/empty one.
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from health import engine as he  # noqa: E402


# ---------------------------------------------------------------------------
# Fixture builder — a well-formed record, one metric entry.
# ---------------------------------------------------------------------------


def _metric_entry(**overrides) -> dict:
    entry = dict(
        detector="scan:cascade-frontier",
        population="claim_bearing_nodes",
        findings_count=3,
        repairs_dispatched=2,
        delta_since_last=-0.05,
        status="done",
        reason=None,
    )
    entry.update(overrides)
    return entry


def _record(**overrides) -> dict:
    rec = dict(
        turn=42,
        written_at="2026-09-03T00:00:00Z",
        status="ran",
        reason=None,
        metrics={"taint_rate": _metric_entry()},
    )
    rec.update(overrides)
    return rec


# ---------------------------------------------------------------------------
# 1. Schema constants
# ---------------------------------------------------------------------------


class TestSchemaConstants:
    def test_top_level_fields(self):
        assert set(he.REPAIR_RECORD_TOP_LEVEL_FIELDS) == {
            "turn", "written_at", "status", "reason", "metrics",
        }

    def test_metric_fields(self):
        assert set(he.REPAIR_RECORD_METRIC_FIELDS) == {
            "detector",
            "population",
            "findings_count",
            "repairs_dispatched",
            "delta_since_last",
            "status",
            "reason",
        }

    def test_status_values(self):
        assert he.REPAIR_RECORD_STATUS_VALUES == {"done", "carried", "unavailable", "skipped"}

    def test_status_values_is_frozenset(self):
        assert isinstance(he.REPAIR_RECORD_STATUS_VALUES, frozenset)

    def test_run_status_values(self):
        """[round 2, engram-alpha#2519] The WHOLE-PASS-level enum — distinct
        from the per-metric REPAIR_RECORD_STATUS_VALUES above."""
        assert he.REPAIR_RECORD_RUN_STATUS_VALUES == {"ran", "skipped"}

    def test_run_status_values_is_frozenset(self):
        assert isinstance(he.REPAIR_RECORD_RUN_STATUS_VALUES, frozenset)


# ---------------------------------------------------------------------------
# 2. Well-formed record accepted
# ---------------------------------------------------------------------------


class TestValidRecordAccepted:
    def test_well_formed_record_has_no_violations(self):
        assert he.validate_repair_record(_record()) == []

    def test_well_formed_record_multiple_metrics(self):
        rec = _record(
            metrics={
                "taint_rate": _metric_entry(status="done"),
                "stale_derivation_rate": _metric_entry(status="carried"),
                # [round 3, engram-alpha#2519, Kepler's ruling] zero null
                # embeddings found is a "done" outcome (the metric WAS fully
                # examined tonight), not "skipped" — "skipped" is now
                # reserved to mean ONLY "not examined tonight at all" (the
                # whole-pass skip-night case, see test_skip_night_record_is_
                # valid below).
                "embedding_coverage": _metric_entry(
                    detector="scan:null-embeddings",
                    status="done",
                    findings_count=0,
                    repairs_dispatched=0,
                ),
                "uncited_observation_rate": _metric_entry(
                    detector="reflect",
                    population="observations",
                    findings_count=None,
                    repairs_dispatched=None,
                    delta_since_last=None,
                    status="unavailable",
                    reason="no_repair_binding",
                ),
                "open_contradiction_rate": _metric_entry(
                    detector="tool:resolve",
                    population="contradictions",
                    findings_count=None,
                    repairs_dispatched=None,
                    delta_since_last=None,
                    status="unavailable",
                    reason="signal_unavailable_this_run",
                ),
            }
        )
        assert he.validate_repair_record(rec) == []

    def test_empty_metrics_dict_is_structurally_valid(self):
        """An empty ``metrics`` dict is still schema-valid shape-wise —
        [round 2, engram-alpha#2519] the dream-master itself no longer
        produces this on a skip night (a skip night now writes a full entry
        per metric, see the corrected precondition rule), but the generic
        validator does not require a non-empty metrics dict, so this stays
        a valid (if not dream-master-realistic) shape."""
        assert he.validate_repair_record(_record(metrics={})) == []

    def test_unavailable_status_entry_with_non_null_reason_is_valid(self):
        """The test Kepler's ruling names explicitly (engram-alpha#2519
        review): a well-formed record with an "unavailable"-status entry
        for a metric whose signal returns UNAVAILABLE tonight, carrying a
        non-null reason, passes validation — the schema-level proof that
        "every metric gets an entry, always" is representable."""
        rec = _record(
            metrics={
                "open_contradiction_rate": _metric_entry(
                    detector="tool:resolve",
                    population="contradictions",
                    findings_count=None,
                    repairs_dispatched=None,
                    delta_since_last=None,
                    status="unavailable",
                    reason="signal_unavailable_this_run",
                ),
            }
        )
        violations = he.validate_repair_record(rec)
        assert violations == []

    def test_embedding_coverage_zero_findings_is_done_not_skipped(self):
        """[round 3, engram-alpha#2519, Kepler's ruling: "so `skipped`
        means exactly one thing"] The scenario that motivated the
        narrowing: a night where the repair pass's ``_count_null_
        embeddings`` scanner finds ZERO null embeddings for ``embedding_
        coverage``. The repair tool is never dispatched (nothing to
        backfill), but the metric WAS fully examined this run — that's a
        ``"done"`` outcome, with ``findings_count: 0`` / ``repairs_
        dispatched: 0``, NOT ``"skipped"`` (round 2's wording for this same
        case, now replaced). ``"skipped"`` is reserved for the whole-pass
        skip-night case exercised by ``test_skip_night_record_is_valid``
        below, where a metric was NOT examined at all — a genuinely
        different situation that used to share this same status value."""
        rec = _record(
            metrics={
                "embedding_coverage": _metric_entry(
                    detector="scan:null-embeddings",
                    population="claim_bearing_nodes",
                    findings_count=0,
                    repairs_dispatched=0,
                    delta_since_last=0.0,
                    status="done",
                    reason=None,
                ),
            }
        )
        violations = he.validate_repair_record(rec)
        assert violations == []
        assert rec["metrics"]["embedding_coverage"]["status"] == "done"
        assert rec["metrics"]["embedding_coverage"]["status"] != "skipped"

    def test_skip_night_record_is_valid(self):
        """[round 2, engram-alpha#2519] A skip-night record: top-level
        status "skipped" with a reason, every metric entry ALSO "skipped"
        with the mirrored reason — the corrected precondition rule's own
        shape."""
        rec = _record(
            status="skipped",
            reason="projection_stale",
            metrics={
                "taint_rate": _metric_entry(
                    status="skipped",
                    findings_count=None,
                    repairs_dispatched=None,
                    delta_since_last=None,
                    reason="projection_stale",
                ),
            },
        )
        assert he.validate_repair_record(rec) == []


# ---------------------------------------------------------------------------
# 3. Individual violations, each named in the returned string(s)
# ---------------------------------------------------------------------------


class TestMissingTopLevelField:
    def test_missing_turn_is_named(self):
        rec = _record()
        del rec["turn"]
        violations = he.validate_repair_record(rec)
        assert len(violations) == 1
        assert "turn" in violations[0]

    def test_missing_written_at_is_named(self):
        rec = _record()
        del rec["written_at"]
        violations = he.validate_repair_record(rec)
        assert len(violations) == 1
        assert "written_at" in violations[0]

    def test_extra_top_level_field_is_named(self):
        rec = _record(unexpected_field="surprise")
        violations = he.validate_repair_record(rec)
        assert len(violations) == 1
        assert "unexpected_field" in violations[0]


class TestExtraOrMisspelledMetricLevelKey:
    def test_extra_metric_key_is_named(self):
        rec = _record(metrics={"taint_rate": _metric_entry(extra_field="surprise")})
        violations = he.validate_repair_record(rec)
        assert len(violations) == 1
        assert "extra_field" in violations[0]
        assert "taint_rate" in violations[0]

    def test_misspelled_metric_key_is_named(self):
        """A misspelled field (e.g. 'repair_dispatched' instead of
        'repairs_dispatched') must surface as BOTH an unexpected field
        (the misspelling) AND a missing field (the real name never
        arrived) — a bare "has unexpected field" check alone would miss
        that the correctly-named field is now absent."""
        entry = _metric_entry()
        entry["repair_dispatched"] = entry.pop("repairs_dispatched")
        rec = _record(metrics={"taint_rate": entry})
        violations = he.validate_repair_record(rec)
        assert len(violations) == 2
        missing_msg = next(v for v in violations if "missing" in v)
        extra_msg = next(v for v in violations if "unexpected" in v)
        assert "repairs_dispatched" in missing_msg
        assert "repair_dispatched" in extra_msg

    def test_missing_metric_key_is_named(self):
        entry = _metric_entry()
        del entry["status"]
        rec = _record(metrics={"taint_rate": entry})
        violations = he.validate_repair_record(rec)
        assert len(violations) == 1
        assert "status" in violations[0]
        assert "taint_rate" in violations[0]


class TestInvalidStatusValue:
    def test_invalid_status_is_named(self):
        rec = _record(metrics={"taint_rate": _metric_entry(status="in_progress")})
        violations = he.validate_repair_record(rec)
        assert len(violations) == 1
        assert "in_progress" in violations[0]
        assert "taint_rate" in violations[0]


class TestInvalidTopLevelStatusValue:
    """[round 2, engram-alpha#2519] The WHOLE-PASS-level status field has
    its own (smaller) enum, checked separately from the per-metric one
    above."""

    def test_invalid_top_level_status_is_named(self):
        rec = _record(status="in_progress")
        violations = he.validate_repair_record(rec)
        assert len(violations) == 1
        assert "in_progress" in violations[0]

    def test_valid_top_level_statuses_accepted(self):
        assert he.validate_repair_record(_record(status="ran", reason=None)) == []
        assert he.validate_repair_record(
            _record(status="skipped", reason="projection_missing", metrics={})
        ) == []


# ---------------------------------------------------------------------------
# 4. Multi-violation collection — both appear in one call
# ---------------------------------------------------------------------------


class TestMultiViolationCollection:
    def test_two_simultaneous_violations_both_returned(self):
        rec = _record(metrics={"taint_rate": _metric_entry(status="bogus")})
        del rec["written_at"]
        violations = he.validate_repair_record(rec)
        assert len(violations) == 2
        joined = " | ".join(violations)
        assert "written_at" in joined
        assert "bogus" in joined

    def test_missing_top_level_and_extra_metric_key_both_returned(self):
        rec = _record(metrics={"taint_rate": _metric_entry(extra_field="x")})
        del rec["turn"]
        violations = he.validate_repair_record(rec)
        assert len(violations) == 2
        joined = " | ".join(violations)
        assert "turn" in joined
        assert "extra_field" in joined


# ---------------------------------------------------------------------------
# Malformed shapes beyond simple key mismatches
# ---------------------------------------------------------------------------


class TestMalformedShapes:
    def test_record_not_a_dict_returns_single_violation(self):
        violations = he.validate_repair_record(["not", "a", "dict"])
        assert len(violations) == 1
        assert "dict" in violations[0]

    def test_metrics_not_a_dict_is_named(self):
        rec = _record(metrics=["not", "a", "dict"])
        violations = he.validate_repair_record(rec)
        assert len(violations) == 1
        assert "metrics" in violations[0]

    def test_metric_entry_not_a_dict_is_named(self):
        rec = _record(metrics={"taint_rate": "not-a-dict"})
        violations = he.validate_repair_record(rec)
        assert len(violations) == 1
        assert "taint_rate" in violations[0]


# ---------------------------------------------------------------------------
# 5. repair_record_missing_metric_names — the completeness helper
# (JUDGMENT CALL, engram-alpha#2519 round 2: added rather than judged out of
# scope — see health/engine.py's own docstring on the function).
# ---------------------------------------------------------------------------


class TestRepairRecordMissingMetricNames:
    def test_complete_record_reports_nothing_missing(self):
        rec = _record(
            metrics={m.signal: _metric_entry() for m in he.MODEL.metrics}
        )
        assert he.repair_record_missing_metric_names(rec) == []

    def test_empty_metrics_dict_reports_every_declared_metric_missing(self):
        rec = _record(metrics={})
        expected = sorted(m.signal for m in he.MODEL.metrics)
        assert he.repair_record_missing_metric_names(rec) == expected
        assert len(expected) > 0  # sanity: MODEL actually declares metrics

    def test_one_missing_metric_is_named(self):
        all_but_one = {
            m.signal: _metric_entry()
            for m in he.MODEL.metrics
            if m.signal != "taint_rate"
        }
        rec = _record(metrics=all_but_one)
        assert he.repair_record_missing_metric_names(rec) == ["taint_rate"]

    def test_malformed_record_reports_every_declared_metric_missing(self):
        expected = sorted(m.signal for m in he.MODEL.metrics)
        assert he.repair_record_missing_metric_names(["not", "a", "dict"]) == expected
        assert he.repair_record_missing_metric_names({"metrics": "not-a-dict"}) == expected


# ---------------------------------------------------------------------------
# 6. dream_projection_error_sentinel_is_stale — the repair-pass precondition's
# freshness check on the `.error` sentinel (engram-alpha#2525): a sentinel
# whose own `turn` doesn't match tonight's `current_turn` is a leftover from
# an earlier cycle's failed unlink, not tonight's own failure, and must not
# be treated as a hard-skip condition.
# ---------------------------------------------------------------------------


class TestDreamProjectionErrorSentinelStaleness:
    def test_matching_turn_is_not_stale(self):
        sentinel = {"turn": 42, "written_at": "2026-09-04T00:00:00Z", "error": "boom"}
        assert he.dream_projection_error_sentinel_is_stale(sentinel, 42) is False

    def test_mismatched_turn_is_stale(self):
        sentinel = {"turn": 41, "written_at": "2026-09-03T00:00:00Z", "error": "boom"}
        assert he.dream_projection_error_sentinel_is_stale(sentinel, 42) is True

    def test_missing_turn_key_is_treated_as_stale(self):
        # Never assume shape: a sentinel missing its own `turn` field entirely
        # (e.g. a hand-edited or corrupted file) must not be trusted as
        # "tonight's failure" — `.get("turn")` returns None, and None != 42,
        # so this falls out of the same comparison without special-casing.
        sentinel = {"written_at": "2026-09-04T00:00:00Z", "error": "boom"}
        assert he.dream_projection_error_sentinel_is_stale(sentinel, 42) is True
