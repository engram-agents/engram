"""Curve-wiring test — REPURPOSED for PR5 (engram-alpha#1991, PR5 of 5,
"the dream-side contract", §7 item 5).

WHY THIS FILE CHANGED: this file used to prove that a hand-built
``piecewise_linear`` curve, rescaled from ``metric_goodness``'s exact shape,
matched PR1-3's per-metric scoring behavior EXACTLY (``LEGACY_GOODNESS_
TRANSITION_CURVE``'s own regression-safety role, still documented in
health/curves.py). That guarantee — "the first shipped curve-library
model must reproduce today's nine scores" — was the design doc's transition
rule for PR4's buildable slice, when the curve library was NOT YET wired
into any real scoring path (PR4's own module docstring: "additive-only,
this module is new and is not imported by health/engine.py ... in this
PR"). PR5 wires curves into the REAL scoring path directly (health/engine.
py's ``build_snapshot``, driven by health.engine.MODEL — round 3 moved
construction here from ``health.model.MODEL``, see that module's
docstring) — so asserting
"the curve reproduces the old formula" is no longer a meaningful regression
guard for the real path: the real path never computes ``metric_goodness``
anymore (health.metrics.aspect_geometric_mean_scoring /
metric_goodness are DEAD CODE from health/engine.py's perspective as of
PR5 — see that module's docstring). Lei's ruling (via Borges, 2026-09-02):
exact-score reproduction is not required for v2.

REPURPOSED (option (b) of the PR5 spec's "retire vs repurpose" choice,
preferred over deleting outright because this file's fixture-DB
infrastructure — a realistic multi-metric, multi-aspect graph exercising
all nine real signal functions at once — is worth reusing): this file now
tests that health.engine.MODEL's ONE v1 curve (``badness_default``),
wired through the REAL composition path (``health.engine.build_snapshot``),
produces SANE results against this same realistic fixture — bounded scores,
no NaN, and the documented (informational, not load-bearing) fact that
``badness_default`` happens to numerically match ``metric_goodness``'s old
shape, since it was built that way on purpose (health/curves.py's
``BADNESS_DEFAULT_CURVE`` docstring).
"""

from __future__ import annotations

import json
import math
import os
import sqlite3
import sys
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest  # noqa: E402

from health import engine as he  # noqa: E402
from health import metrics as hem  # noqa: E402
from health import model as hem_model  # noqa: E402
from health.config import UNAVAILABLE  # noqa: E402


# ---------------------------------------------------------------------------
# Fixture DB — same DDL/helper shape as tests/test_health_engine_metrics.py
# and tests/test_health_engine.py (this repo's established per-test-file
# fixture-DDL convention; each health.engine* test file defines its own
# copy rather than sharing one via conftest.py). UNCHANGED from the pre-PR5
# version of this file — this infrastructure is exactly what's worth
# reusing (see module docstring).
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
    non-UNAVAILABLE rate. UNCHANGED from the pre-PR5 version of this file —
    see module docstring for why this infrastructure survives the repurpose.

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
# The nine real metric bindings — read off health.engine.MODEL (round 3: the
# validated model, built by health/engine.py via health.model.load()
# — no longer importable ready-made as health.model.MODEL, see that
# module's own docstring), never hand-copied (same discipline the pre-PR5
# version of this file applied to health.engine.CONFIG).
# ---------------------------------------------------------------------------

_ALL_REAL_BINDINGS = he.MODEL.metrics


def test_model_has_exactly_nine_real_metric_bindings():
    """Sanity check on this test file's own premise — if this ever drifts
    (a tenth metric added, one removed, e.g. the pending_marker_ratio
    merge), the parametrize below would silently test a different N; this
    makes that visible instead."""
    assert len(_ALL_REAL_BINDINGS) == 9
    assert {m.signal for m in _ALL_REAL_BINDINGS} == {
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


@pytest.mark.parametrize("binding", _ALL_REAL_BINDINGS, ids=lambda m: m.signal)
def test_badness_default_curve_produces_sane_scores_for_every_real_metric(binding):
    """For each real metric binding: run its REAL signal function (via
    health.model.SIGNALS — the name registry, clarification 2)
    against the fixture db to get an actual current rate, then assert its
    bound curve (``badness_default`` for all nine today) produces a SANE
    score: bounded to [0,1] (its declared output_range), never NaN.

    Wrapped in a mock of ``engram_core._embedder.is_available`` for every
    metric (not just embedding_coverage) — harmless for the other eight,
    necessary for embedding_coverage — same precedent as
    tests/test_health_engine_metrics.py's TestEmbeddingCoverage class.
    """
    signal_fn = hem_model.SIGNALS[binding.signal]
    with mock.patch("engram_core._embedder.is_available", return_value=True):
        conn = _build_fixture_conn()
        rate = signal_fn(conn)

    assert rate is not UNAVAILABLE, (
        f"{binding.signal}: fixture DB produced UNAVAILABLE — the fixture is "
        f"supposed to give every real metric a non-degenerate live rate; "
        f"this is a fixture-construction bug, not the thing under test"
    )

    curve = he.MODEL.curves[binding.curve]
    score = curve.curve_fn(rate)
    assert isinstance(score, float)
    assert not math.isnan(score)
    lo, hi = curve.output_range
    assert lo <= score <= hi, f"{binding.signal}: score {score!r} outside {curve.output_range!r}"

    # Informational, NOT the load-bearing guard anymore (see module
    # docstring): badness_default was built to numerically reproduce
    # metric_goodness's old shape on purpose (health/curves.py's
    # BADNESS_DEFAULT_CURVE docstring) — this documents that fact still
    # holds, it does not assert it as a requirement going forward.
    assert score == pytest.approx(hem.metric_goodness(rate) / 100.0, abs=1e-9)


def test_full_build_snapshot_wired_through_the_new_model_produces_sane_results():
    """End-to-end integration check (the PR5 spec's step 3 manual-sanity
    ask, promoted to a committed test since the fixture already exists
    here): run the REAL health.engine.build_snapshot against this
    realistic, multi-metric fixture and assert the whole pipeline — signal
    computation -> curve -> per-aspect weighted sum -> composed — produces
    sane, bounded numbers with no NaN / no None-where-a-number-is-expected.

    Also re-confirms, at the INTEGRATION level (not just the unit level in
    tests/test_health_engine_model.py / tests/test_health_engine.py's
    TestRealModelCompositionConsequence), the RESOLVED (round 2 of
    engram-alpha#2520, "hold out, renormalize, report") consequence:
    provenance_integrity has zero admitted metrics under the v1 table, so
    its OWN score is UNAVAILABLE, but this no longer propagates to
    ``composed`` — its 25 points are held out and ``composed`` is
    renormalized over the remaining 75, landing on a REAL number even on
    this healthy-ish fixture (PR5 round 1 asserted the OPPOSITE here; see
    health/engine.py's module docstring "RESOLVED" section for why).
    """
    with mock.patch("engram_core._embedder.is_available", return_value=True):
        conn = _build_fixture_conn()
        snap = he.build_snapshot(conn, "nap")

    for name, value in snap["scores"]["metrics"].items():
        if value == "UNAVAILABLE":
            continue
        assert isinstance(value, float)
        assert not math.isnan(value)
        assert 0.0 <= value <= 1.0, f"{name}: score {value!r} outside [0,1]"

    assert isinstance(snap["scores"]["aspects"]["epistemic_integrity"], float)
    assert isinstance(snap["scores"]["aspects"]["retrievability"], float)
    assert 0.0 <= snap["scores"]["aspects"]["epistemic_integrity"] <= 1.0
    assert 0.0 <= snap["scores"]["aspects"]["retrievability"] <= 1.0

    # provenance_integrity's OWN score is still UNAVAILABLE (zero admitted
    # metrics, unchanged by round 2) -- what changed is that this no
    # longer poisons the top-level composed score.
    assert snap["scores"]["aspects"]["provenance_integrity"] == "UNAVAILABLE"
    assert isinstance(snap["scores"]["composed"], float)
    assert not math.isnan(snap["scores"]["composed"])
    assert 0.0 <= snap["scores"]["composed"] <= 100.0
    assert snap["scores"]["unscored_aspects"] == ["provenance_integrity"]
    assert snap["scores"]["points_in_play"] == pytest.approx(75.0)
    assert snap["scores"]["composed_coverage"] == pytest.approx(0.75)
