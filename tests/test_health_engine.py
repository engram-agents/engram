"""Tests for health_engine.py — the real production CONFIG, top-level
composition, and the snapshot writer (engram-alpha#1991, PR2+PR3 of 4).

Covers:
  1. CONFIG validates (weights sum to 100 across A/B/C, all real as of PR3)
     and its aspect shape matches the design doc (weight 40/25/35).
  2. compose_aspect_scores — the top-level weighted geometric mean, floor at
     1, UNAVAILABLE propagation.
  3. build_snapshot's full dict shape (schema_version, engine_version,
     config, definitions, signals, scores, red_flags, diagnostics, anchor,
     turn, checkpoint_mode) against a real (throwaway) SQLite conn.
  4. Contract item 3 — UNAVAILABLE serializes as the JSON string literal
     "UNAVAILABLE", never coerced to a number (no -1/null sentinel).
  5. write_snapshot's on-disk persistence + best-effort failure handling.
  6. PR3: the diagnostics block (activity metrics + cascade-marker age) —
     never wired into scoring, populated for real now.

Reuses tests/test_health_engine_metrics.py's fixture DB shape (a subset of
engram_core.py's real `nodes`/`edges`/`edit_history` table columns).
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from unittest import mock

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import engram_core  # noqa: E402
import health_engine  # noqa: E402
import health_engine_metrics as hem  # noqa: E402
from health_engine_config import UNAVAILABLE, AspectDef, EngineConfig, MetricDef  # noqa: E402


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

    def test_red_flags_still_empty_diagnostics_now_populated(self):
        """red_flags stays [] (DAG-violations-as-a-red-flag is out of scope
        for PR3 too — see health_engine.py's module docstring) — but PR3
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

    def test_empty_graph_all_signals_unavailable_except_clean_contradiction(self):
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
        """As of PR3, aspect B is real (not a stub) — a graph that gives
        every metric in all THREE aspects a clean, non-empty denominator now
        produces a real numeric composed score end to end. Before PR3, this
        same fixture would have left aspect B (and hence the composed score)
        permanently UNAVAILABLE via the PR2 stub."""
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
        assert isinstance(snap["scores"]["aspects"]["provenance_integrity"], float)
        assert isinstance(snap["scores"]["aspects"]["retrievability"], float)
        assert isinstance(snap["scores"]["composed"], float)

        # Fully covered + connected -> all four aspect-B raw signals
        # (badness-rate convention) read as perfect (0.0), and the aspect
        # score composes to 100.
        assert snap["signals"]["recall_summary_coverage"] == 0.0
        assert snap["signals"]["recall_keywords_coverage"] == 0.0
        assert snap["signals"]["embedding_coverage"] == 0.0
        assert snap["signals"]["giant_component_fraction"] == 0.0
        assert snap["scores"]["aspects"]["retrievability"] == pytest.approx(100.0)

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
# 6. Diagnostics (PR3) — activity metrics + cascade-marker age. Never wired
#    into scoring — see health_engine.py's module docstring.
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
