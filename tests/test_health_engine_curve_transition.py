"""Transition-rule test — regression safety for PR1-3 (engram-alpha#1991,
PR4 of 4, buildable slice, spec item 4).

For each of the nine real metrics (read off health_engine.py's real
``CONFIG``, never hand-copied), constructs a ``piecewise_linear`` curve
reproducing ``metric_goodness``'s exact shape rescaled from its ``[1,100]``
output range to the Layer-3 curve contract's ``[0,1]`` range, runs the
metric's REAL ``signal_fn`` against a small realistic fixture database to
get an actual current rate, and asserts the rescaled curve matches
``metric_goodness(rate) / 100`` for that rate within a tight tolerance.

This is the guard against a silent factor/formula error in the rescaling:
if PR1-3's per-metric scoring behavior and this PR's curve-library
rescaling of the SAME transform ever diverge, this test fails.
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest  # noqa: E402

import health_engine as he  # noqa: E402
import health_engine_metrics as hem  # noqa: E402
from health_engine_config import UNAVAILABLE  # noqa: E402
from health_engine_curves import LEGACY_GOODNESS_TRANSITION_CURVE  # noqa: E402


# ---------------------------------------------------------------------------
# Fixture DB — same DDL/helper shape as tests/test_health_engine_metrics.py
# and tests/test_health_engine.py (this repo's established per-test-file
# fixture-DDL convention; each health_engine* test file defines its own
# copy rather than sharing one via conftest.py).
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


def _build_fixture_conn() -> sqlite3.Connection:
    """One small, realistic fixture DB exercising ALL NINE real metrics'
    signal functions at once, each landing on a non-degenerate,
    non-UNAVAILABLE rate.

    JUDGMENT CALL (flagged in the coder-fairy report, per the spec's
    explicit "use your judgment and say which you picked and why"): a
    FIXTURE DB was chosen over connecting to a real ENGRAM db. A fixture is
    fully self-contained (no dependency on this test environment happening
    to have a real ~/.engram database with the right shape of data
    available, read-only or otherwise), deterministic (doesn't drift as a
    real graph's content changes over time), and matches this test suite's
    own established convention (every other health_engine* test file
    builds a small real-shape SQLite fixture rather than touching a live
    db). The trade-off, stated honestly: this proves "the rescaled curve
    matches metric_goodness's transform for whatever rate a REAL signal_fn
    produces against a plausible row shape" — it does NOT prove the curve
    stays correct against every rate value a real, larger, messier
    production graph might produce. That gap is acceptable for this test's
    purpose: the transform itself (``max(1-rate, 0.01)``) is already
    verified pointwise across the WHOLE [0,1] domain by the dense-sampled
    property tests in tests/test_health_engine_curves.py. What THIS test
    specifically adds beyond those is "does the wiring from a REAL
    signal_fn's return value into curve/metric_goodness stay correct",
    not "is the curve correct for all inputs" (already covered elsewhere).

    Layout (deliberately cross-cutting — the same 4 claim-bearing nodes
    dv_1/dv_2/ob_1/ob_2 feed taint_rate AND all four aspect-B metrics, per
    their shared "claim_bearing_nodes" denominator population):
      - dv_1, dv_2: derivations. dv_1 carries a pending supersede marker
        (stale); dv_1 also carries a real recall_summary/recall_keywords/
        embedding (covered on all three aspect-B coverage axes). dv_2 has
        none of those (missing on all three).
      - ob_1, ob_2: observation_factual (also claim-bearing). ob_1 has no
        evidence_id (uncited) and carries a pending retract marker
        (tainted); ob_2 has evidence_id="ev_1" (cited) and is covered on
        all three aspect-B axes, mirroring dv_1.
      - One edge dv_1 -> ob_1 so giant_component_fraction lands on a
        non-degenerate 0.5 (2 of the 4 claim-bearing nodes connected) —
        not the trivial "everything isolated" 1.0.
      - ev_1 (cited by ob_2, not dangling), ev_2 (cited by nobody,
        dangling) for dangling_evidence_rate.
      - ct_1 (active), ct_2 (resolved) for open_contradiction_rate.
    """
    conn = _make_conn()

    _insert(
        conn, "dv_1", "derivation",
        recall_summary="a real summary",
        recall_keywords=["a", "b"],
        embedding=json.dumps([0.1, 0.2]),
    )
    _insert(conn, "dv_2", "derivation")
    _insert_marker(conn, "dv_1", "supersede")  # dv_1 is stale

    _insert(conn, "ob_1", "observation_factual", evidence_id=None)
    _insert(
        conn, "ob_2", "observation_factual", evidence_id="ev_1",
        recall_summary="a second summary",
        recall_keywords=["kw"],
        embedding=json.dumps([0.3]),
    )
    _insert_marker(conn, "ob_1", "retract")  # ob_1 is tainted

    _insert(conn, "ev_1", "evidence")  # cited by ob_2 -> not dangling
    _insert(conn, "ev_2", "evidence")  # cited by nobody -> dangling

    _insert(conn, "ct_1", "contradiction", status="active")
    _insert(conn, "ct_2", "contradiction", status="resolved")

    _insert_edge(conn, "dv_1", "ob_1", "relates_to")

    return conn


# ---------------------------------------------------------------------------
# The rescaled curve under test (spec item 4, step 1): metric_goodness's
# exact shape, ``max(100*(1-rate), 1.0)``, rescaled from its [1,100] output
# range to the curve contract's [0,1] range by dividing by 100. Showing the
# work, per the spec's explicit "get this rescaling right — show your work
# in a comment" instruction:
#
#     metric_goodness(rate) / 100
#   = max(100*(1-rate), 1.0) / 100
#   = max(100*(1-rate)/100, 1.0/100)      [dividing max(a, b) by a positive
#                                           constant c distributes over the
#                                           max: max(a, b)/c == max(a/c, b/c)
#                                           for c > 0, since dividing by a
#                                           positive c preserves ordering]
#   = max(1-rate, 0.01)
#
# ...which is exactly a decreasing piecewise-linear curve from (0.0, 1.0)
# down to (0.99, 0.01): that segment's slope is
# (0.01 - 1.0) / (0.99 - 0.0) == -1, i.e. curve(x) == 1 - x identically for
# x in [0, 0.99] — matching (1 - rate) there exactly — then FLAT at the
# 0.01 floor from (0.99, 0.01) to (1.0, 0.01), matching max(1-rate, 0.01)
# for rate in [0.99, 1.0], where (1 - rate) <= 0.01 and the floor wins.
# So this curve equals metric_goodness(rate)/100 EXACTLY (modulo float
# rounding) for every rate in [0, 1], not just at a few sampled points —
# the tight 1e-9 tolerance below is float-rounding headroom, not slack for
# a formula mismatch.
#
# Round 2 (engram-alpha#1991 PR4, forum #382): this used to be an inline
# ``functools.partial(piecewise_linear, knots=...)`` re-derivation, kept in
# sync with health_engine_curves.py's shipped shape only by hand. It now
# references the shipped ``LEGACY_GOODNESS_TRANSITION_CURVE`` constant
# directly, so this test asserts against the actual shipped object rather
# than a second local re-derivation that could silently drift from it.
_TRANSITION_CURVE = LEGACY_GOODNESS_TRANSITION_CURVE.curve_fn


# ---------------------------------------------------------------------------
# The nine real metrics — read off health_engine.py's CONFIG, never
# hand-copied (spec item 4: "do not hand-copy metric names").
# ---------------------------------------------------------------------------

_ALL_REAL_METRICS = tuple(
    metric for aspect in he.CONFIG.aspects for metric in aspect.metrics
)


def test_config_has_exactly_nine_real_metrics():
    """Sanity check on this test file's own premise — if this ever drifts
    (a tenth metric added, one removed), the parametrize below would
    silently test a different N; this makes that visible instead."""
    assert len(_ALL_REAL_METRICS) == 9
    assert {m.name for m in _ALL_REAL_METRICS} == {
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


@pytest.mark.parametrize("metric", _ALL_REAL_METRICS, ids=lambda m: m.name)
def test_transition_curve_matches_metric_goodness_for_a_real_rate(metric):
    """For each real metric: run its REAL signal_fn against the fixture db
    to get an actual current rate, then assert the rescaled curve matches
    metric_goodness's transform of that SAME rate, within tight float
    tolerance (1e-9).

    Wrapped in a mock of ``engram_core._embedder.is_available`` for every
    metric (not just embedding_coverage) — harmless for the other eight,
    which never touch the embedder, and necessary for embedding_coverage,
    which would otherwise read UNAVAILABLE under this test suite's default
    ENGRAM_NO_EMBEDDINGS-equivalent environment (no embedder installed),
    per the precedent in tests/test_health_engine_metrics.py's
    TestEmbeddingCoverage class.
    """
    with mock.patch("engram_core._embedder.is_available", return_value=True):
        conn = _build_fixture_conn()
        rate = metric.signal_fn(conn)

    assert rate is not UNAVAILABLE, (
        f"{metric.name}: fixture DB produced UNAVAILABLE — the fixture is "
        f"supposed to give every real metric a non-degenerate live rate; "
        f"this is a fixture-construction bug, not the thing under test"
    )

    expected = hem.metric_goodness(rate) / 100.0
    actual = _TRANSITION_CURVE(rate)
    assert actual == pytest.approx(expected, abs=1e-9), (
        f"{metric.name}: curve({rate!r})={actual!r} != "
        f"metric_goodness({rate!r})/100={expected!r}"
    )
