"""Tests for health_engine_metrics.py — the aspect A/B/C signal + scoring
function catalogue (engram-alpha#1991, PR2+PR3 of 4).

Builds a small, real, throwaway SQLite fixture DB (the ``nodes``/``edges``
tables' real column shape, a subset of engram_core.py's CREATE TABLE) per
test, and asserts each signal function returns the EXACT expected rate
against it — not just "doesn't crash". Explicitly covers the
empty_denominator UNAVAILABLE case (zero derivations -> stale_derivation_rate
is UNAVAILABLE, not 0 or a ZeroDivisionError) and the empty_denominator
"clean" case (zero contradictions -> open_contradiction_rate is 0.0, not
UNAVAILABLE) per the PR2 spec's explicit test requirements.

Aspect B's four metrics (PR3) all return the badness-rate COMPLEMENT of
their coverage-style names (see health_engine_metrics.py's module docstring
"SIGN CONVENTION" note) — every test below asserts against that complement,
not a plain coverage percentage.
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
from unittest import mock

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import engram_core  # noqa: E402
import health_engine_metrics as hem  # noqa: E402
from health_engine_config import UNAVAILABLE  # noqa: E402


# ---------------------------------------------------------------------------
# Fixture DB — real nodes-table column shape (subset of engram_core.py's
# CREATE TABLE nodes; only the columns any signal function in this module
# actually SELECTs).
# ---------------------------------------------------------------------------

_NODES_DDL = """
CREATE TABLE nodes (
    id             TEXT PRIMARY KEY,
    type           TEXT NOT NULL,
    created_at     TEXT NOT NULL,
    evidence_id    TEXT,
    status         TEXT DEFAULT 'active',
    is_current     INTEGER DEFAULT 1,
    metadata       TEXT DEFAULT '{}',
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


def _insert_marker(conn, node_id, root_op, status="pending", cascade_event=None):
    """Insert a cascade_markers row for node_id/root_op.

    cascade_event defaults to an auto-generated unique value per call (each
    call = a distinct cascade event by default), since the
    UNIQUE(node_id, cascade_event) constraint means two markers on the same
    node need distinct cascade_event values to coexist. Pass an explicit
    cascade_event to simulate the same node caught in a specific event.
    """
    global _marker_seq
    _marker_seq += 1
    if cascade_event is None:
        cascade_event = f"cev_{_marker_seq}"
    conn.execute(
        "INSERT INTO cascade_markers"
        " (node_id, cascade_event, root_node, root_op, status, created_at)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        (node_id, cascade_event, "root_x", root_op, status, "t0"),
    )
    conn.commit()


# ---------------------------------------------------------------------------
# Aspect A — stale_derivation_rate
# ---------------------------------------------------------------------------


class TestStaleDerivationRate:
    def test_empty_denominator_is_unavailable(self):
        """Zero current derivations -> UNAVAILABLE, not 0 or a ZeroDivisionError."""
        conn = _make_conn()
        assert hem.stale_derivation_rate(conn) is UNAVAILABLE

    def test_mixed_stale_and_fresh_derivations(self):
        conn = _make_conn()
        _insert(conn, "dv_1", "derivation")
        _insert(conn, "dv_2", "derivation")
        _insert_marker(conn, "dv_1", "supersede")
        assert hem.stale_derivation_rate(conn) == 0.5

    def test_resolved_marker_does_not_count_as_stale(self):
        """A cascade_markers row with status='resolved' must NOT count —
        this is the literal defect being fixed (resolving a marker must
        change the rate). Also the cascade_markers-era translation of the
        old metadata-based test that checked an empty ``stale_by`` list
        didn't count — same underlying intent ("a technically-present but
        non-signaling marker must not count"), expressed against the new
        source."""
        conn = _make_conn()
        _insert(conn, "dv_1", "derivation")
        _insert(conn, "dv_2", "derivation")
        _insert_marker(conn, "dv_1", "supersede", status="resolved")
        assert hem.stale_derivation_rate(conn) == 0.0

    def test_all_stale(self):
        conn = _make_conn()
        _insert(conn, "dv_1", "derivation")
        _insert(conn, "dv_2", "derivation")
        _insert_marker(conn, "dv_1", "supersede")
        _insert_marker(conn, "dv_2", "supersede")
        assert hem.stale_derivation_rate(conn) == 1.0

    def test_scope_is_derivations_only_not_all_node_types(self):
        """A stale-marked non-derivation node (e.g. a contradiction, which
        the old whole-graph formula also counted) must NOT affect this
        metric — scope is derivations only, per 'stale-DERIVATION rate'."""
        conn = _make_conn()
        _insert(conn, "dv_1", "derivation")
        _insert(conn, "ct_1", "contradiction")
        _insert_marker(conn, "ct_1", "supersede")
        assert hem.stale_derivation_rate(conn) == 0.0

    def test_non_current_derivations_excluded(self):
        conn = _make_conn()
        _insert(conn, "dv_1", "derivation", is_current=0)
        _insert(conn, "dv_2", "derivation")
        _insert_marker(conn, "dv_1", "supersede")
        assert hem.stale_derivation_rate(conn) == 0.0

    def test_retract_marker_does_not_count_as_stale(self):
        """Only root_op='supersede' counts for stale — a pending
        root_op='retract' marker on a derivation must NOT count (that's
        taint_rate's signal, not stale_derivation_rate's)."""
        conn = _make_conn()
        _insert(conn, "dv_1", "derivation")
        _insert(conn, "dv_2", "derivation")
        _insert_marker(conn, "dv_1", "retract")
        assert hem.stale_derivation_rate(conn) == 0.0

    def test_two_pending_markers_count_once_not_twice(self):
        """A node caught in TWO cascade events (two pending marker rows)
        still counts ONCE in the numerator — COUNT(DISTINCT n.id), not
        marker-row count."""
        conn = _make_conn()
        _insert(conn, "dv_1", "derivation")
        _insert(conn, "dv_2", "derivation")
        _insert_marker(conn, "dv_1", "supersede", cascade_event="cev_a")
        _insert_marker(conn, "dv_1", "supersede", cascade_event="cev_b")
        assert hem.stale_derivation_rate(conn) == 0.5


# ---------------------------------------------------------------------------
# Aspect A — taint_rate
# ---------------------------------------------------------------------------


class TestTaintRate:
    def test_empty_denominator_is_unavailable(self):
        conn = _make_conn()
        assert hem.taint_rate(conn) is UNAVAILABLE

    def test_mixed_tainted_and_clean_claim_bearing_nodes(self):
        conn = _make_conn()
        _insert(conn, "ob_1", "observation_factual")
        _insert(conn, "ob_2", "observation_factual")
        _insert(conn, "dv_1", "derivation")
        _insert_marker(conn, "ob_1", "retract")
        assert hem.taint_rate(conn) == 1.0 / 3

    def test_non_claim_bearing_nodes_excluded_from_denominator(self):
        """A tainted-looking marker on a non-claim-bearing node type (e.g.
        definition) must not count toward either numerator or denominator —
        only claim-bearing types are candidates."""
        conn = _make_conn()
        _insert(conn, "ob_1", "observation_factual")
        _insert(conn, "df_1", "definition")
        _insert_marker(conn, "ob_1", "retract")
        _insert_marker(conn, "df_1", "retract")
        assert hem.taint_rate(conn) == 1.0

    def test_non_current_excluded(self):
        conn = _make_conn()
        _insert(conn, "ob_1", "observation_factual", is_current=0)
        _insert(conn, "ob_2", "observation_factual")
        _insert_marker(conn, "ob_1", "retract")
        assert hem.taint_rate(conn) == 0.0

    def test_resolved_marker_does_not_count_as_tainted(self):
        """A cascade_markers row with status='resolved' must NOT count —
        the literal defect being fixed (resolving a marker must change the
        rate)."""
        conn = _make_conn()
        _insert(conn, "ob_1", "observation_factual")
        _insert(conn, "ob_2", "observation_factual")
        _insert_marker(conn, "ob_1", "retract", status="resolved")
        assert hem.taint_rate(conn) == 0.0

    def test_supersede_marker_does_not_count_as_tainted(self):
        """Only root_op='retract' counts for taint — a pending
        root_op='supersede' marker must NOT count (that's
        stale_derivation_rate's signal, not taint_rate's)."""
        conn = _make_conn()
        _insert(conn, "ob_1", "observation_factual")
        _insert(conn, "ob_2", "observation_factual")
        _insert_marker(conn, "ob_1", "supersede")
        assert hem.taint_rate(conn) == 0.0

    def test_two_pending_markers_count_once_not_twice(self):
        """A node caught in TWO cascade events (two pending marker rows)
        still counts ONCE in the numerator — COUNT(DISTINCT n.id), not
        marker-row count."""
        conn = _make_conn()
        _insert(conn, "ob_1", "observation_factual")
        _insert(conn, "ob_2", "observation_factual")
        _insert_marker(conn, "ob_1", "retract", cascade_event="cev_a")
        _insert_marker(conn, "ob_1", "retract", cascade_event="cev_b")
        assert hem.taint_rate(conn) == 0.5


# ---------------------------------------------------------------------------
# Aspect A — open_contradiction_rate
# ---------------------------------------------------------------------------


class TestOpenContradictionRate:
    def test_empty_denominator_is_clean_zero(self):
        """Zero contradictions -> 0.0 (clean), not UNAVAILABLE — the design
        doc's own worked example for empty_denominator='clean'."""
        conn = _make_conn()
        assert hem.open_contradiction_rate(conn) == 0.0
        assert hem.open_contradiction_rate(conn) is not UNAVAILABLE

    def test_active_counts_as_open(self):
        conn = _make_conn()
        _insert(conn, "ct_1", "contradiction", status="active")
        _insert(conn, "ct_2", "contradiction", status="resolved")
        assert hem.open_contradiction_rate(conn) == 0.5

    def test_partially_resolved_counts_as_open(self):
        conn = _make_conn()
        _insert(conn, "ct_1", "contradiction", status="partially_resolved")
        _insert(conn, "ct_2", "contradiction", status="resolved")
        assert hem.open_contradiction_rate(conn) == 0.5

    def test_all_resolved_is_zero(self):
        conn = _make_conn()
        _insert(conn, "ct_1", "contradiction", status="resolved")
        _insert(conn, "ct_2", "contradiction", status="resolved")
        assert hem.open_contradiction_rate(conn) == 0.0

    def test_all_open_is_one(self):
        conn = _make_conn()
        _insert(conn, "ct_1", "contradiction", status="active")
        _insert(conn, "ct_2", "contradiction", status="partially_resolved")
        assert hem.open_contradiction_rate(conn) == 1.0


# ---------------------------------------------------------------------------
# Aspect C — uncited_observation_rate
# ---------------------------------------------------------------------------


class TestUncitedObservationRate:
    def test_empty_denominator_is_unavailable(self):
        conn = _make_conn()
        assert hem.uncited_observation_rate(conn) is UNAVAILABLE

    def test_mixed_cited_and_uncited(self):
        conn = _make_conn()
        _insert(conn, "ev_1", "evidence")
        _insert(conn, "ob_1", "observation_factual", evidence_id="ev_1")
        _insert(conn, "ob_2", "observation_factual", evidence_id=None)
        _insert(conn, "ob_3", "observation_predictive", evidence_id=None)
        assert hem.uncited_observation_rate(conn) == 2.0 / 3

    def test_non_observation_types_excluded(self):
        conn = _make_conn()
        _insert(conn, "ob_1", "observation_factual", evidence_id=None)
        _insert(conn, "dv_1", "derivation", evidence_id=None)
        assert hem.uncited_observation_rate(conn) == 1.0

    def test_non_current_excluded(self):
        conn = _make_conn()
        _insert(conn, "ob_1", "observation_factual", evidence_id=None, is_current=0)
        _insert(conn, "ob_2", "observation_factual", evidence_id="ev_1")
        assert hem.uncited_observation_rate(conn) == 0.0


# ---------------------------------------------------------------------------
# Aspect C — dangling_evidence_rate
# ---------------------------------------------------------------------------


class TestDanglingEvidenceRate:
    def test_empty_denominator_is_unavailable(self):
        conn = _make_conn()
        assert hem.dangling_evidence_rate(conn) is UNAVAILABLE

    def test_mixed_dangling_and_cited(self):
        conn = _make_conn()
        _insert(conn, "ev_1", "evidence")
        _insert(conn, "ev_2", "evidence")
        _insert(conn, "ob_1", "observation_factual", evidence_id="ev_2")
        assert hem.dangling_evidence_rate(conn) == 0.5

    def test_citation_from_non_current_observation_does_not_rescue_evidence(self):
        """An evidence node cited only by a non-current (superseded/retracted)
        observation is still dangling — the citing observation itself must
        be current."""
        conn = _make_conn()
        _insert(conn, "ev_1", "evidence")
        _insert(conn, "ob_1", "observation_factual", evidence_id="ev_1", is_current=0)
        assert hem.dangling_evidence_rate(conn) == 1.0

    def test_non_current_evidence_excluded_from_denominator(self):
        conn = _make_conn()
        _insert(conn, "ev_1", "evidence", is_current=0)
        _insert(conn, "ev_2", "evidence")
        _insert(conn, "ob_1", "observation_factual", evidence_id="ev_2")
        assert hem.dangling_evidence_rate(conn) == 0.0

    def test_all_dangling_is_one(self):
        conn = _make_conn()
        _insert(conn, "ev_1", "evidence")
        _insert(conn, "ev_2", "evidence")
        assert hem.dangling_evidence_rate(conn) == 1.0


# ---------------------------------------------------------------------------
# Aspect B — recall_summary_coverage
# ---------------------------------------------------------------------------


class TestRecallSummaryCoverage:
    def test_empty_denominator_is_unavailable(self):
        conn = _make_conn()
        assert hem.recall_summary_coverage(conn) is UNAVAILABLE

    def test_mixed_covered_and_missing_returns_missing_rate(self):
        """Badness-rate convention: return value is the MISSING fraction,
        not the coverage fraction."""
        conn = _make_conn()
        _insert(conn, "dv_1", "derivation", recall_summary="a short summary")
        _insert(conn, "ob_1", "observation_factual", recall_summary=None)
        assert hem.recall_summary_coverage(conn) == 0.5

    def test_all_covered_is_zero(self):
        conn = _make_conn()
        _insert(conn, "dv_1", "derivation", recall_summary="s1")
        _insert(conn, "ob_1", "observation_factual", recall_summary="s2")
        assert hem.recall_summary_coverage(conn) == 0.0

    def test_all_missing_is_one(self):
        conn = _make_conn()
        _insert(conn, "dv_1", "derivation")
        _insert(conn, "ob_1", "observation_factual")
        assert hem.recall_summary_coverage(conn) == 1.0

    def test_whitespace_only_summary_counts_as_missing(self):
        """Mirrors engram_core._validate_recall_fields's own .strip() check
        — a whitespace-only summary is absent, not a real one."""
        conn = _make_conn()
        _insert(conn, "dv_1", "derivation", recall_summary="   ")
        assert hem.recall_summary_coverage(conn) == 1.0

    def test_non_claim_bearing_nodes_excluded(self):
        """A definition node (recall_summary-capable at write time, but NOT
        claim-bearing) must not affect the rate either way."""
        conn = _make_conn()
        _insert(conn, "dv_1", "derivation", recall_summary=None)
        _insert(conn, "df_1", "definition", recall_summary="covered")
        assert hem.recall_summary_coverage(conn) == 1.0

    def test_non_current_excluded(self):
        conn = _make_conn()
        _insert(conn, "dv_1", "derivation", recall_summary=None, is_current=0)
        _insert(conn, "ob_1", "observation_factual", recall_summary="s")
        assert hem.recall_summary_coverage(conn) == 0.0


# ---------------------------------------------------------------------------
# Aspect B — recall_keywords_coverage
# ---------------------------------------------------------------------------


class TestRecallKeywordsCoverage:
    def test_empty_denominator_is_unavailable(self):
        conn = _make_conn()
        assert hem.recall_keywords_coverage(conn) is UNAVAILABLE

    def test_mixed_covered_and_missing_returns_missing_rate(self):
        conn = _make_conn()
        _insert(conn, "dv_1", "derivation", recall_keywords=["kw1", "kw2"])
        _insert(conn, "ob_1", "observation_factual", recall_keywords=None)
        assert hem.recall_keywords_coverage(conn) == 0.5

    def test_empty_list_counts_as_missing(self):
        conn = _make_conn()
        _insert(conn, "dv_1", "derivation", recall_keywords=[])
        assert hem.recall_keywords_coverage(conn) == 1.0

    def test_malformed_json_counts_as_missing(self):
        conn = _make_conn()
        _insert(conn, "dv_1", "derivation")
        conn.execute(
            "UPDATE nodes SET recall_keywords = ? WHERE id = ?",
            ("{not valid json", "dv_1"),
        )
        conn.commit()
        assert hem.recall_keywords_coverage(conn) == 1.0

    def test_non_claim_bearing_nodes_excluded(self):
        conn = _make_conn()
        _insert(conn, "dv_1", "derivation", recall_keywords=None)
        _insert(conn, "df_1", "definition", recall_keywords=["kw"])
        assert hem.recall_keywords_coverage(conn) == 1.0

    def test_non_current_excluded(self):
        conn = _make_conn()
        _insert(conn, "dv_1", "derivation", recall_keywords=None, is_current=0)
        _insert(conn, "ob_1", "observation_factual", recall_keywords=["kw"])
        assert hem.recall_keywords_coverage(conn) == 0.0


# ---------------------------------------------------------------------------
# Aspect B — embedding_coverage
# ---------------------------------------------------------------------------


class TestEmbeddingCoverage:
    """All tests below except the two in the 'embedder structurally
    unavailable' block patch ``core._embedder.is_available`` to True — the
    real test suite runs with ENGRAM_NO_EMBEDDINGS=1, under which it's
    False, and these tests are exercising the per-node coverage-counting
    logic, not the availability gate itself (that's covered separately
    below, per Borges's PR3 colleague review 2026-08-23 blocker 1)."""

    def test_empty_denominator_is_unavailable(self):
        with mock.patch("engram_core._embedder.is_available", return_value=True):
            conn = _make_conn()
            assert hem.embedding_coverage(conn) is UNAVAILABLE

    def test_default_fixture_state_is_all_missing(self):
        """Mirrors the real ENGRAM_NO_EMBEDDINGS=1 test-suite gotcha flagged
        in the PR3 build spec: without explicitly setting the embedding
        column, every node reads as uncovered."""
        with mock.patch("engram_core._embedder.is_available", return_value=True):
            conn = _make_conn()
            _insert(conn, "dv_1", "derivation")
            _insert(conn, "ob_1", "observation_factual")
            assert hem.embedding_coverage(conn) == 1.0

    def test_mixed_covered_and_missing_returns_missing_rate(self):
        """A real populated embedding column (set directly in the fixture,
        not via the real embedding pipeline) proves the 'has coverage'
        branch actually works, not just the always-missing default."""
        with mock.patch("engram_core._embedder.is_available", return_value=True):
            conn = _make_conn()
            _insert(conn, "dv_1", "derivation", embedding=json.dumps([0.1, 0.2, 0.3]))
            _insert(conn, "ob_1", "observation_factual", embedding=None)
            assert hem.embedding_coverage(conn) == 0.5

    def test_all_covered_is_zero(self):
        with mock.patch("engram_core._embedder.is_available", return_value=True):
            conn = _make_conn()
            _insert(conn, "dv_1", "derivation", embedding=json.dumps([0.1]))
            _insert(conn, "ob_1", "observation_factual", embedding=json.dumps([0.2]))
            assert hem.embedding_coverage(conn) == 0.0

    def test_non_claim_bearing_nodes_excluded(self):
        with mock.patch("engram_core._embedder.is_available", return_value=True):
            conn = _make_conn()
            _insert(conn, "dv_1", "derivation", embedding=None)
            _insert(conn, "df_1", "definition", embedding=json.dumps([0.1]))
            assert hem.embedding_coverage(conn) == 1.0

    def test_non_current_excluded(self):
        with mock.patch("engram_core._embedder.is_available", return_value=True):
            conn = _make_conn()
            _insert(conn, "dv_1", "derivation", embedding=None, is_current=0)
            _insert(conn, "ob_1", "observation_factual", embedding=json.dumps([0.1]))
            assert hem.embedding_coverage(conn) == 0.0

    # -- embedder structurally unavailable: a CONFIGURATION state, must gate
    #    to UNAVAILABLE regardless of what the embedding column holds
    #    (Borges, PR3 colleague review, 2026-08-23, blocker 1) --------------

    def test_embedder_unavailable_is_unavailable_even_with_real_data(self):
        """Even a graph with real, populated embeddings must read as
        UNAVAILABLE (not a real coverage number) when the embedder is
        structurally unavailable in THIS install -- the column contents are
        irrelevant to a metric that can't be trusted to reflect a live
        install's actual retrievability."""
        with mock.patch("engram_core._embedder.is_available", return_value=False):
            conn = _make_conn()
            _insert(conn, "dv_1", "derivation", embedding=json.dumps([0.1]))
            _insert(conn, "ob_1", "observation_factual", embedding=json.dumps([0.2]))
            assert hem.embedding_coverage(conn) is UNAVAILABLE

    def test_embeddings_disabled_in_config_is_unavailable(self):
        """The other half of the compound gate: is_available() True but
        embeddings disabled via config still reads as UNAVAILABLE."""
        with mock.patch("engram_core._embedder.is_available", return_value=True), \
             mock.patch("engram_core._get_embedding_config", return_value={"enabled": False}):
            conn = _make_conn()
            _insert(conn, "dv_1", "derivation", embedding=json.dumps([0.1]))
            assert hem.embedding_coverage(conn) is UNAVAILABLE

    def test_default_test_suite_state_is_unavailable(self):
        """No patching at all -- the real ENGRAM_NO_EMBEDDINGS=1 test-suite
        default. Documents the gate is actually active under the same
        conditions every other test in this module runs under."""
        conn = _make_conn()
        _insert(conn, "dv_1", "derivation")
        assert hem.embedding_coverage(conn) is UNAVAILABLE


# ---------------------------------------------------------------------------
# Aspect B — giant_component_fraction
# ---------------------------------------------------------------------------


class TestGiantComponentFraction:
    def test_empty_denominator_is_unavailable(self):
        conn = _make_conn()
        assert hem.giant_component_fraction(conn) is UNAVAILABLE

    def test_single_node_is_fully_connected(self):
        conn = _make_conn()
        _insert(conn, "dv_1", "derivation")
        assert hem.giant_component_fraction(conn) == 0.0

    def test_disconnected_nodes_are_fully_fragmented(self):
        """Two claim-bearing nodes, no edge between them -> each is its own
        singleton component -> largest=1/total=2 -> fragmentation 0.5."""
        conn = _make_conn()
        _insert(conn, "dv_1", "derivation")
        _insert(conn, "ob_1", "observation_factual")
        assert hem.giant_component_fraction(conn) == 0.5

    def test_edge_between_claim_bearing_nodes_connects_them(self):
        conn = _make_conn()
        _insert(conn, "dv_1", "derivation")
        _insert(conn, "ob_1", "observation_factual")
        _insert_edge(conn, "dv_1", "ob_1", "derives_from")
        assert hem.giant_component_fraction(conn) == 0.0

    def test_edge_through_non_claim_bearing_node_does_not_bridge(self):
        """The induced subgraph only counts an edge if BOTH endpoints are
        claim-bearing — a path through a non-claim-bearing node (e.g.
        evidence) must NOT connect the two claim-bearing nodes either side
        of it."""
        conn = _make_conn()
        _insert(conn, "dv_1", "derivation")
        _insert(conn, "ob_1", "observation_factual")
        _insert(conn, "ev_1", "evidence")
        _insert_edge(conn, "dv_1", "ev_1", "derives_from")
        _insert_edge(conn, "ev_1", "ob_1", "cites")
        # dv_1 and ob_1 remain disconnected in the induced claim-bearing
        # subgraph -> still fully fragmented, same as no edges at all.
        assert hem.giant_component_fraction(conn) == 0.5

    def test_three_node_chain_all_claim_bearing_is_fully_connected(self):
        conn = _make_conn()
        _insert(conn, "dv_1", "derivation")
        _insert(conn, "dv_2", "derivation")
        _insert(conn, "ob_1", "observation_factual")
        _insert_edge(conn, "dv_1", "dv_2", "derives_from")
        _insert_edge(conn, "dv_2", "ob_1", "derives_from")
        assert hem.giant_component_fraction(conn) == 0.0

    def test_non_current_nodes_excluded_from_population(self):
        conn = _make_conn()
        _insert(conn, "dv_1", "derivation", is_current=0)
        _insert(conn, "ob_1", "observation_factual")
        assert hem.giant_component_fraction(conn) == 0.0


# ---------------------------------------------------------------------------
# Shared aspect scoring — aspect_geometric_mean_scoring
# ---------------------------------------------------------------------------


class TestAspectGeometricMeanScoring:
    def test_all_perfect_rates_score_100(self):
        assert hem.aspect_geometric_mean_scoring({"m1": 0.0, "m2": 0.0}) == 100.0

    def test_any_unavailable_propagates(self):
        assert hem.aspect_geometric_mean_scoring(
            {"m1": 0.0, "m2": UNAVAILABLE}
        ) is UNAVAILABLE

    def test_empty_raw_by_metric_is_unavailable(self):
        assert hem.aspect_geometric_mean_scoring({}) is UNAVAILABLE

    def test_single_metric_matches_the_transform_directly(self):
        # good = 100*(1-rate); geometric mean of a single value is itself.
        assert hem.aspect_geometric_mean_scoring({"m1": 0.25}) == 75.0

    def test_full_collapse_floors_at_one_not_zero(self):
        """rate=1.0 -> good=0, floored to 1 (design doc §3's floor, applied
        one level down per this PR's aspect-scoring judgment call) — never a
        literal 0, which would make the geometric mean permanently zero
        regardless of other metrics."""
        assert hem.aspect_geometric_mean_scoring({"m1": 1.0}) == 1.0

    def test_two_metrics_geometric_mean_is_lower_than_arithmetic_mean(self):
        """One perfect metric + one badly collapsed metric should read closer
        to the collapsed one than a naive average would — the whole point of
        geometric mean over arithmetic mean (a collapsed metric drags the
        aspect down, not averaged away)."""
        score = hem.aspect_geometric_mean_scoring({"good": 0.0, "bad": 0.9})
        arithmetic_mean = (100.0 + 10.0) / 2  # 55.0
        assert score < arithmetic_mean
        assert score == pytest.approx((100.0 * 10.0) ** 0.5)
