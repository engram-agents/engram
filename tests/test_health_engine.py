"""Tests for health_engine.py — the real production CONFIG, top-level
composition, and the snapshot writer (engram-alpha#1991, PR2 of 4).

Covers:
  1. CONFIG validates (weights sum to 100 across A/C/B-stub) and its aspect
     shape matches the design doc (weight 40/25/35).
  2. compose_aspect_scores — the top-level weighted geometric mean, floor at
     1, UNAVAILABLE propagation.
  3. build_snapshot's full dict shape (schema_version, engine_version,
     config, definitions, signals, scores, red_flags, diagnostics, anchor,
     turn, checkpoint_mode) against a real (throwaway) SQLite conn.
  4. Contract item 3 — UNAVAILABLE serializes as the JSON string literal
     "UNAVAILABLE", never coerced to a number (no -1/null sentinel).
  5. write_snapshot's on-disk persistence + best-effort failure handling.

Reuses tests/test_health_engine_metrics.py's fixture DB shape (a subset of
engram_core.py's real `nodes` table columns).
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import health_engine  # noqa: E402
import health_engine_metrics as hem  # noqa: E402
from health_engine_config import UNAVAILABLE, AspectDef, EngineConfig, MetricDef  # noqa: E402


_NODES_DDL = """
CREATE TABLE nodes (
    id          TEXT PRIMARY KEY,
    type        TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    evidence_id TEXT,
    status      TEXT DEFAULT 'active',
    is_current  INTEGER DEFAULT 1,
    metadata    TEXT DEFAULT '{}'
);

-- health_engine_metrics.stale_derivation_rate/taint_rate query this table
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
            is_current=1, metadata=None):
    conn.execute(
        "INSERT INTO nodes (id, type, created_at, evidence_id, status, is_current, metadata)"
        " VALUES (?, ?, ?, ?, ?, ?, ?)",
        (id, type, created_at, evidence_id, status, is_current,
         json.dumps(metadata) if metadata is not None else "{}"),
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

    def test_retrievability_is_a_single_stub_metric(self):
        """Aspect B is explicitly NOT this PR (PR3's job) — this PR's CONFIG
        ships an honest stub, not real coverage/embedding metrics."""
        aspect = next(a for a in health_engine.CONFIG.aspects if a.name == "retrievability")
        assert len(aspect.metrics) == 1
        assert aspect.metrics[0].name == "retrievability_not_yet_available"

    def test_all_metrics_are_scored_and_state_valued(self):
        for aspect in health_engine.CONFIG.aspects:
            for metric in aspect.metrics:
                assert metric.scored is True
                assert metric.state_valued is True


# ---------------------------------------------------------------------------
# 2. compose_aspect_scores — top-level composition (design doc §3)
# ---------------------------------------------------------------------------


def _fake_config(weight_a=60.0, weight_b=40.0):
    def _sig(conn):
        return 0.0

    def _score(raw):
        return 100.0

    metric_a = MetricDef(
        name="ma", signal_fn=_sig, denominator_population="edges",
        empty_denominator="clean", scored=True, state_valued=True,
    )
    metric_b = MetricDef(
        name="mb", signal_fn=_sig, denominator_population="edges",
        empty_denominator="clean", scored=True, state_valued=True,
    )
    aspect_a = AspectDef(name="a", weight=weight_a, metrics=(metric_a,), scoring_fn=_score)
    aspect_b = AspectDef(name="b", weight=weight_b, metrics=(metric_b,), scoring_fn=_score)
    return EngineConfig(schema_version="t", engine_version="t", aspects=(aspect_a, aspect_b))


class TestComposeAspectScores:
    def test_all_aspects_perfect_composes_to_100(self):
        config = _fake_config()
        composed = health_engine.compose_aspect_scores(config, {"a": 100.0, "b": 100.0})
        assert composed == pytest.approx(100.0)

    def test_any_unavailable_aspect_propagates(self):
        config = _fake_config()
        composed = health_engine.compose_aspect_scores(config, {"a": 100.0, "b": UNAVAILABLE})
        assert composed is UNAVAILABLE

    def test_floors_at_one_not_zero(self):
        """A fully-collapsed aspect (score 0) is floored to 1 before
        composing — never a literal 0 (design doc §3's floor)."""
        config = _fake_config(weight_a=50.0, weight_b=50.0)
        composed_at_zero = health_engine.compose_aspect_scores(config, {"a": 0.0, "b": 100.0})
        composed_at_one = health_engine.compose_aspect_scores(config, {"a": 1.0, "b": 100.0})
        assert composed_at_zero == pytest.approx(composed_at_one)

    def test_weighted_toward_the_higher_weight_aspect(self):
        """A collapsed HIGH-weight aspect drags the composed score down more
        than a collapsed LOW-weight aspect would (design doc §3: 'dead
        retrievability with perfect integrity reads in the 30s, not 65')."""
        config = _fake_config(weight_a=80.0, weight_b=20.0)
        collapsed_high_weight = health_engine.compose_aspect_scores(
            config, {"a": 1.0, "b": 100.0}
        )
        config2 = _fake_config(weight_a=20.0, weight_b=80.0)
        collapsed_low_weight = health_engine.compose_aspect_scores(
            config2, {"a": 1.0, "b": 100.0}
        )
        assert collapsed_high_weight < collapsed_low_weight


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
        conn = _make_conn()
        snap = health_engine.build_snapshot(conn, "nap")
        assert set(snap.keys()) == {
            "schema_version", "engine_version", "config", "definitions",
            "signals", "scores", "red_flags", "diagnostics", "anchor",
            "turn", "checkpoint_mode",
        }

    def test_red_flags_and_diagnostics_empty_this_pr(self):
        conn = _make_conn()
        snap = health_engine.build_snapshot(conn, "nap")
        assert snap["red_flags"] == []
        assert snap["diagnostics"] == {}

    def test_config_is_inlined_full_declaration(self):
        conn = _make_conn()
        snap = health_engine.build_snapshot(conn, "nap")
        assert snap["config"]["schema_version"] == health_engine.CONFIG.schema_version
        aspect_names = {a["name"] for a in snap["config"]["aspects"]}
        assert aspect_names == {"epistemic_integrity", "provenance_integrity", "retrievability"}

    def test_empty_graph_all_signals_unavailable_except_clean_contradiction(self):
        conn = _make_conn()
        snap = health_engine.build_snapshot(conn, "nap")
        assert snap["signals"]["stale_derivation_rate"] == "UNAVAILABLE"
        assert snap["signals"]["taint_rate"] == "UNAVAILABLE"
        assert snap["signals"]["uncited_observation_rate"] == "UNAVAILABLE"
        assert snap["signals"]["dangling_evidence_rate"] == "UNAVAILABLE"
        # clean case: zero contradictions -> 0.0, not UNAVAILABLE
        assert snap["signals"]["open_contradiction_rate"] == 0.0
        assert snap["scores"]["composed"] == "UNAVAILABLE"

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

    def test_populated_graph_produces_real_composed_score(self):
        conn = _make_conn()
        # Give every real metric a non-empty denominator with a clean signal.
        _insert(conn, "dv_1", "derivation", metadata={})
        _insert(conn, "ob_1", "observation_factual", evidence_id="ev_1")
        _insert(conn, "ev_1", "evidence")
        # aspect B stays UNAVAILABLE (stub) -> composed stays UNAVAILABLE,
        # but the two real aspects must compute real numeric scores.
        snap = health_engine.build_snapshot(conn, "nap")
        assert isinstance(snap["scores"]["aspects"]["epistemic_integrity"], float)
        assert isinstance(snap["scores"]["aspects"]["provenance_integrity"], float)
        assert snap["scores"]["aspects"]["retrievability"] == "UNAVAILABLE"
        assert snap["scores"]["composed"] == "UNAVAILABLE"

    def test_per_metric_display_score_matches_the_shared_goodness_transform(self):
        """Regression guard for the reviewer-fairy round-1 finding on PR2:
        build_snapshot's per-metric display score (snap["scores"]["metrics"])
        must be derived from the SAME metrics.metric_goodness helper that
        aspect_geometric_mean_scoring uses internally to build the aspect
        score those metrics feed into — not an independently re-implemented
        copy of the same formula that could silently diverge."""
        conn = _make_conn()
        _insert(conn, "ct_1", "contradiction", status="active")
        _insert(conn, "ct_2", "contradiction", status="resolved")
        snap = health_engine.build_snapshot(conn, "nap")
        raw = snap["signals"]["open_contradiction_rate"]
        assert raw == 0.5
        assert snap["scores"]["metrics"]["open_contradiction_rate"] == hem.metric_goodness(raw)


# ---------------------------------------------------------------------------
# 5. write_snapshot — persistence + best-effort failure handling
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
