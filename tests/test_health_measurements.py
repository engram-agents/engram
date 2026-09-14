"""Tests for health/measurements.py — the L0 measurement layer
(engram-alpha#2537, PR1 of 3 of the health-v2 SSoT refactor).

Two jobs, kept deliberately separate:

1. **Per-measurement unit tests** — each registered ``MeasurementDef``
   returns the right COUNT (not rate) against a small fixture, spot-checking
   scope/current-only/distinct-not-row-count the same way
   ``test_health_engine_metrics.py`` already does at the rate level. Kept
   light since the underlying SQL is copied verbatim from already-tested
   signal functions — this file is not re-litigating that coverage.

2. **The acceptance criterion, with its required control** (#2537 build
   brief + Ariadne's amendment DM, both non-negotiable per that brief):
   for each of the (originally nine, now eight per engram-alpha#2905 PR-B's
   taint_rate/stale_derivation_rate unification into pending_premise_rate)
   signals, the value DERIVED from L0 measurements
   (arithmetic written here, mirroring each signal function's own formula —
   NOT shipped code; wiring signals as formulas over measurements is slice 2,
   #2537, out of scope for this PR) must equal the value the LIVE signal
   function in ``health/metrics.py`` returns, on the SAME connection
   (Ariadne's amendment #3 — two reads off one ``conn``, never two connections,
   so a concurrent-write artifact can't be mistaken for a decomposition bug).
   NOTE (PR-B): ``pending_premise_rate`` has NO live-oracle entry in
   ``_LIVE_SIGNAL_FNS`` below — ``health.metrics`` has no equivalent
   function (out of PR-B's declared scope, engram-alpha#2905) — so the
   acceptance loop below no longer independently cross-checks this one
   signal; see ``_LIVE_SIGNAL_FNS``'s own comment.

   The fixture is SYNTHETIC but GENUINELY POPULATED — every one of the 13
   counts is non-zero on it (Ariadne's amendment #2: an all-zero fixture
   passes an equality test against a broken decomposition, and passes a
   perturbation test too if the perturbation happens to land on an
   already-zero value; either half alone is a decoration). A one-off run of
   this same decomposition against a real, non-synthetic graph is reported
   in the PR body per Ariadne's DM — deliberately NOT a checked-in test
   (not reproducible in CI), so it does not appear in this file.

   ``test_perturbing_a_nonzero_measurement_is_detected`` is the required
   control: perturbs ``claim_bearing_pending_marker`` (real fixture
   value 2, not 0) and asserts the derived value changes. A test that has
   only ever been seen to pass has not been shown capable of failing.
"""

from __future__ import annotations

import dataclasses
import json
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

import engram_core  # noqa: E402
from health import measurements as hem_l0  # noqa: E402
from health import metrics  # noqa: E402
from health import model as health_model  # noqa: E402
from health import version as health_version  # noqa: E402
from health.config import UNAVAILABLE  # noqa: E402
from health.curves import CurveSpec  # noqa: E402
from health.signals import SIGNAL_DEFS  # noqa: E402


# ---------------------------------------------------------------------------
# Fixture DB — same real column shape test_health_engine_metrics.py uses
# (a subset of engram_core.py's CREATE TABLE nodes/edges/cascade_markers).
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


# ---------------------------------------------------------------------------
# Per-measurement unit tests — light spot-checks, one class per measurement.
# ---------------------------------------------------------------------------


class TestDenominators:
    def test_derivations_current_counts_current_derivations_only(self):
        conn = _make_conn()
        _insert(conn, "dv_1", "derivation")
        _insert(conn, "dv_2", "derivation", is_current=0)
        _insert(conn, "ct_1", "contradiction")
        assert hem_l0.m_derivations_current(conn) == 1

    def test_claim_bearing_current_spans_all_six_types(self):
        conn = _make_conn()
        for i, t in enumerate((
            "observation_factual", "observation_predictive", "derivation",
            "theory", "axiom", "conjecture",
        )):
            _insert(conn, f"n_{i}", t)
        _insert(conn, "lesson_1", "lesson")  # NOT claim-bearing (dropped 08-26)
        assert hem_l0.m_claim_bearing_current(conn) == 6

    def test_contradictions_current(self):
        conn = _make_conn()
        _insert(conn, "ct_1", "contradiction")
        _insert(conn, "ct_2", "contradiction", is_current=0)
        assert hem_l0.m_contradictions_current(conn) == 1

    def test_observations_current(self):
        conn = _make_conn()
        _insert(conn, "o_1", "observation_factual")
        _insert(conn, "o_2", "observation_predictive")
        _insert(conn, "o_3", "theory")  # not an observation
        assert hem_l0.m_observations_current(conn) == 2

    def test_evidence_current(self):
        conn = _make_conn()
        _insert(conn, "ev_1", "evidence")
        _insert(conn, "ev_2", "evidence", is_current=0)
        assert hem_l0.m_evidence_current(conn) == 1


class TestNumerators:
    def test_claim_bearing_pending_marker_dedupes_one_node_both_marker_kinds(self):
        """THE LOAD-BEARING DEDUP PROPERTY (engram-alpha#2905 PR-B): a node
        carrying BOTH a pending retract marker row AND a pending supersede
        marker row counts ONCE, not twice -- COUNT(DISTINCT n.id), not
        COUNT(n.id). This is the whole reason the issue calls out "a node
        with both marker kinds counts once" as load-bearing."""
        conn = _make_conn()
        _insert(conn, "ax_1", "axiom")
        _insert_marker(conn, "ax_1", "retract")
        _insert_marker(conn, "ax_1", "supersede")
        assert hem_l0.m_claim_bearing_pending_marker(conn) == 1

    def test_claim_bearing_pending_marker_dedup_control_via_row_count(self):
        """THE REQUIRED CONTROL for the dedup test above (same discipline as
        this module's other "a test that has only ever been seen to pass has
        not been shown capable of failing" controls): on the SAME fixture as
        the dedup test (one node, two marker rows), a plain COUNT(n.id) --
        i.e. the mutation ``COUNT(DISTINCT n.id)`` -> ``COUNT(n.id)`` would
        produce -- is 2, genuinely different from the real function's 1.
        Proves the dedup assertion above is discriminating, not vacuously
        true on a fixture where distinct-vs-row count happen to coincide."""
        conn = _make_conn()
        _insert(conn, "ax_1", "axiom")
        _insert_marker(conn, "ax_1", "retract")
        _insert_marker(conn, "ax_1", "supersede")
        claim_bearing_types = tuple(sorted(engram_core.CLAIM_BEARING_TYPES))
        placeholders = ",".join("?" * len(claim_bearing_types))
        row_count = conn.execute(
            f"""SELECT COUNT(n.id) as c
                FROM nodes n
                JOIN cascade_markers m ON m.node_id = n.id
                WHERE n.is_current = 1 AND n.type IN ({placeholders})
                  AND m.root_op IN ('retract', 'supersede') AND m.status = 'pending'""",
            claim_bearing_types,
        ).fetchone()["c"]
        assert row_count == 2, (
            "control setup failed: expected 2 marker rows on the one node"
        )
        distinct_count = hem_l0.m_claim_bearing_pending_marker(conn)
        assert distinct_count == 1
        assert distinct_count != row_count, (
            "control failed: COUNT(DISTINCT n.id) coincided with the row "
            "count on this fixture -- it would not catch the "
            "COUNT(DISTINCT n.id) -> COUNT(n.id) mutation"
        )

    def test_claim_bearing_pending_marker_two_different_nodes_count_two(self):
        """The straightforward control (per the PR-B spec): two DIFFERENT
        nodes, each with one marker (one retract, one supersede) -- both
        count, distinctly, not collapsed together."""
        conn = _make_conn()
        _insert(conn, "ax_1", "axiom")
        _insert_marker(conn, "ax_1", "retract")
        _insert(conn, "dv_1", "derivation")
        _insert_marker(conn, "dv_1", "supersede")
        assert hem_l0.m_claim_bearing_pending_marker(conn) == 2

    def test_claim_bearing_pending_marker_distinct_nodes_same_root_op(self):
        """Mirrors the old per-measurement spot-check (pre-PR-B): multiple
        marker rows of the SAME root_op on one node still count once."""
        conn = _make_conn()
        _insert(conn, "dv_1", "derivation")
        _insert_marker(conn, "dv_1", "supersede", cascade_event="a")
        _insert_marker(conn, "dv_1", "supersede", cascade_event="b")  # 2nd event, same node
        _insert_marker(conn, "dv_1", "retract")  # different root_op, same node -- still 1
        assert hem_l0.m_claim_bearing_pending_marker(conn) == 1

    def test_contradictions_open_excludes_resolved(self):
        conn = _make_conn()
        _insert(conn, "ct_1", "contradiction", status="active")
        _insert(conn, "ct_2", "contradiction", status="partially_resolved")
        _insert(conn, "ct_3", "contradiction", status="resolved")
        assert hem_l0.m_contradictions_open(conn) == 2

    def test_observations_without_evidence_id(self):
        conn = _make_conn()
        _insert(conn, "o_1", "observation_factual", evidence_id="ev_1")
        _insert(conn, "o_2", "observation_predictive", evidence_id=None)
        assert hem_l0.m_observations_without_evidence_id(conn) == 1

    def test_evidence_uncited(self):
        conn = _make_conn()
        _insert(conn, "ev_1", "evidence")
        _insert(conn, "ev_2", "evidence")
        _insert(conn, "o_1", "observation_factual", evidence_id="ev_1")
        assert hem_l0.m_evidence_uncited(conn) == 1

    def test_claim_bearing_without_recall_summary_treats_whitespace_as_missing(self):
        conn = _make_conn()
        _insert(conn, "ax_1", "axiom", recall_summary="a real summary")
        _insert(conn, "ax_2", "axiom", recall_summary="   ")
        _insert(conn, "ax_3", "axiom", recall_summary=None)
        assert hem_l0.m_claim_bearing_without_recall_summary(conn) == 2

    def test_claim_bearing_without_recall_keywords_treats_malformed_as_missing(self):
        conn = _make_conn()
        _insert(conn, "ax_1", "axiom", recall_keywords=["a", "b"])
        _insert(conn, "ax_2", "axiom", recall_keywords=[])  # empty list
        _insert(conn, "ax_3", "axiom")  # NULL
        conn.execute("UPDATE nodes SET recall_keywords = 'not json{' WHERE id = 'ax_3'")
        conn.commit()
        assert hem_l0.m_claim_bearing_without_recall_keywords(conn) == 2

    def test_claim_bearing_null_embedding(self):
        conn = _make_conn()
        _insert(conn, "ax_1", "axiom", embedding="[0.1]")
        _insert(conn, "ax_2", "axiom", embedding=None)
        assert hem_l0.m_claim_bearing_null_embedding(conn) == 1

    def test_giant_component_size_induced_subgraph_only(self):
        conn = _make_conn()
        _insert(conn, "dv_1", "derivation")
        _insert(conn, "dv_2", "derivation")
        _insert(conn, "ax_1", "axiom")
        _insert(conn, "ev_1", "evidence")  # NOT claim-bearing
        _insert_edge(conn, "dv_1", "dv_2", "supports")
        _insert_edge(conn, "dv_2", "ev_1", "cites")  # bridges through non-claim-bearing — must not connect ax_1
        assert hem_l0.m_claim_bearing_giant_component_size(conn) == 2  # {dv_1, dv_2}; ax_1 singleton

    def test_giant_component_size_zero_on_empty_population(self):
        conn = _make_conn()
        assert hem_l0.m_claim_bearing_giant_component_size(conn) == 0


class TestEnvironmentFacts:
    def test_embedder_installed_available_reads_core(self):
        conn = _make_conn()
        with mock.patch("engram_core._embedder.is_available", return_value=True):
            assert hem_l0.m_embedder_installed_available(conn) is True
        with mock.patch("engram_core._embedder.is_available", return_value=False):
            assert hem_l0.m_embedder_installed_available(conn) is False

    def test_embedder_config_enabled_reads_core(self):
        conn = _make_conn()
        with mock.patch("engram_core._get_embedding_config", return_value={"enabled": False}):
            assert hem_l0.m_embedder_config_enabled(conn) is False
        with mock.patch("engram_core._get_embedding_config", return_value={"enabled": True}):
            assert hem_l0.m_embedder_config_enabled(conn) is True

    def test_claim_bearing_types_resolved_is_sorted_and_matches_core(self):
        conn = _make_conn()
        resolved = hem_l0.m_claim_bearing_types_resolved(conn)
        assert resolved == tuple(sorted(engram_core.CLAIM_BEARING_TYPES))
        assert list(resolved) == sorted(resolved)  # sorted, per module contract


class TestRegistryAndComputeAll:
    def test_registry_has_sixteen_entries(self):
        """13 counts + 2 embedder booleans + 1 resolved type list = 16
        registered MeasurementDefs (engram-alpha#2905 PR-B: 17 -> 16, two
        numerators unified into one); raw_schema_version is stamped
        separately by compute_all_measurements, not registered as a
        MeasurementDef."""
        assert len(hem_l0.MEASUREMENTS) == 16

    def test_compute_all_measurements_is_json_safe_and_stamps_version(self):
        conn = _make_conn()
        _insert(conn, "ax_1", "axiom")
        with mock.patch("engram_core._embedder.is_available", return_value=True):
            result = hem_l0.compute_all_measurements(conn)
        assert result["raw_schema_version"] == hem_l0.RAW_SCHEMA_VERSION
        # The stamp is health_version.MAJOR, not a local throwaway constant
        # (Lei's amendment 2) — checked against the canonical module
        # directly, not just against this module's own re-exported alias.
        assert result["raw_schema_version"] == health_version.MAJOR
        assert isinstance(result["claim_bearing_types_resolved"], list)
        json.dumps(result)  # must not raise — every value is JSON-safe
        assert len(result) == 17  # 16 measurements + the version stamp

    def test_every_measurement_has_a_nonempty_population_label(self):
        """population is DATA the L0 hash gate hashes, not decoration — a
        blank one would silently drop that measurement's identity from the
        gate's coverage."""
        for name, mdef in hem_l0.MEASUREMENTS.items():
            assert isinstance(mdef.population, str) and mdef.population.strip(), (
                f"{name} has an empty/missing population label"
            )


class TestL0HashGate:
    """The L0 hash gate (Borges's amendment-2 placement call: "it belongs
    with the definition of the set it pins"). Pins a hash over the
    canonical L0 set — every MeasurementDef's (name, population,
    definition_inputs), sorted — so an add/remove/redefine of a
    measurement (including a silent redefinition via a remote constant
    like CLAIM_BEARING_TYPES, resolved into definition_inputs) fails this
    test until health.version.MAJOR is bumped and the hash below is
    re-pinned IN THE SAME COMMIT. See health.measurements.canonical_l0_hash's
    own docstring for the full design.

    HOW TO UPDATE THIS PIN, when a real MAJOR-worthy change lands: bump
    health.version.MAJOR, run
    ``python3 -c "import sys; sys.path.insert(0,'src/engram');
    from health import measurements as h; print(h.canonical_l0_hash())"``,
    paste the new digest below, in the SAME commit as the MAJOR bump."""

    _PINNED_L0_HASH = "fa69ad5675040420c472fcb2452125b88b058d6fef03817549b4aba559da2542"
    # Re-pinned by engram-alpha#2905 PR-B: two L0 numerators retired
    # (derivations_pending_supersede_marker, claim_bearing_pending_retract_
    # marker) and one added (claim_bearing_pending_marker, unifying both),
    # in the same commit as health.version.MAJOR's bump to 2.

    def test_l0_set_hash_matches_pinned_value(self):
        computed = hem_l0.canonical_l0_hash()
        assert computed == self._PINNED_L0_HASH, (
            f"L0 set changed (computed {computed!r} != pinned "
            f"{self._PINNED_L0_HASH!r}) — if this is a deliberate "
            f"add/remove/redefine of a measurement, bump health_version."
            f"MAJOR and update _PINNED_L0_HASH above IN THE SAME COMMIT; "
            f"if it's accidental, revert the change that caused it."
        )

    def test_definition_inputs_resolves_the_constants_value_not_just_its_name(self):
        """THE 2026-08-26 REGRESSION TEST (Ariadne's blocker finding on this
        PR, then Borges's correction to the fix via her relay): every
        claim-bearing-scoped measurement's ``definition_inputs`` must hold
        the RESOLVED sorted type tuple, not a string naming the
        ``CLAIM_BEARING_TYPES`` symbol — a symbol-naming string is
        byte-identical before and after a type is added/removed from the
        real constant, so the hash gate would stay green through exactly
        the incident it exists to catch. Directly simulates that incident:
        patch ``engram_core.CLAIM_BEARING_TYPES`` to a set missing a real
        member (mirrors ``lesson`` being dropped 2026-08-26) and confirm
        the RESOLUTION FUNCTION itself is sensitive to that removal, then
        that swapping the real registry's resolved value for the shrunk
        one — run through the REAL ``canonical_l0_hash()``, not a
        reimplementation of its hashing logic — actually changes the pin."""
        real_types = hem_l0._resolve_claim_bearing_types()
        assert "theory" in real_types  # sanity: the real constant has it

        shrunk = frozenset(engram_core.CLAIM_BEARING_TYPES) - {"theory"}
        with mock.patch("engram_core.CLAIM_BEARING_TYPES", shrunk):
            shrunk_types = hem_l0._resolve_claim_bearing_types()

        assert shrunk_types != real_types, (
            "resolution did not change when a real type was removed — "
            "this is the exact bug the fix addresses"
        )
        assert "theory" not in shrunk_types

        # The consequence that actually matters: patch every affected
        # MeasurementDef's definition_inputs to the shrunk tuple and run
        # the REAL canonical_l0_hash() against the live (patched) registry
        # — not a hand-rolled reimplementation of its hashing logic.
        affected = [
            name for name, d in hem_l0.MEASUREMENTS.items()
            if d.definition_inputs == real_types
        ]
        assert len(affected) >= 6, (
            f"expected at least 6 claim-bearing-scoped measurements to "
            f"declare definition_inputs, found {len(affected)}: {affected}"
        )
        patch = {
            name: dataclasses.replace(hem_l0.MEASUREMENTS[name], definition_inputs=shrunk_types)
            for name in affected
        }
        with mock.patch.dict(hem_l0.MEASUREMENTS, patch):
            shrunk_hash = hem_l0.canonical_l0_hash()

        assert shrunk_hash != self._PINNED_L0_HASH, (
            "the 2026-08-26-shaped incident would NOT have failed the gate "
            "(definition_inputs still names the symbol, not its value)"
        )

    def test_gate_actually_fails_on_a_perturbed_set(self):
        """The required control, same discipline as the acceptance test's
        own perturbation demo: a hash gate that has never been seen to
        fail is not shown to be discriminating. Perturb one measurement's
        population label THROUGH THE REAL canonical_l0_hash() (via
        mock.patch.dict on the live registry, not a hand-rolled
        reimplementation of the hashing logic — a reviewer-fairy suggestion
        adopted here) and confirm the computed hash changes."""
        real_hash = hem_l0.canonical_l0_hash()
        assert real_hash == self._PINNED_L0_HASH  # sanity: matches the pin

        some_name = next(iter(hem_l0.MEASUREMENTS))
        perturbed_def = dataclasses.replace(
            hem_l0.MEASUREMENTS[some_name],
            population=hem_l0.MEASUREMENTS[some_name].population + " (perturbed)",
        )
        with mock.patch.dict(hem_l0.MEASUREMENTS, {some_name: perturbed_def}):
            perturbed_hash = hem_l0.canonical_l0_hash()

        assert perturbed_hash != real_hash, (
            "control failed: perturbing a population label did not change the hash"
        )


class TestDerivedHashGate:
    """The DERIVED-layer hash gate (engram-alpha#2563, Gap 2). Same shape as
    ``TestL0HashGate`` above, one layer up: pins a hash over the declared
    scoring model built ON TOP of L0 — aspects, metric bindings, curve
    specs (incl. ``knots``, #2576/#2578), and signal definitions — so a
    derived-only change (a curve reshaped, an importance reweighted, a
    metric reassigned to a different aspect, a signal's numerator/
    denominator changed) fails this test until ``health.version.MINOR`` is
    bumped and the hash below is re-pinned IN THE SAME COMMIT. See
    ``health.measurements.canonical_derived_hash``'s own docstring for the
    full design and the boundary it draws against L0's hash above.

    HOW TO UPDATE THIS PIN, when a real MINOR-worthy change lands: bump
    health.version.MINOR (or MAJOR, per ``required_severity``'s rule if the
    L0 hash ALSO changed), run
    ``python3 -c "import sys; sys.path.insert(0,'src/engram');
    from health import measurements as h; print(h.canonical_derived_hash())"``,
    paste the new digest below, in the SAME commit as the bump."""

    _PINNED_DERIVED_HASH = "41a6c92c2f429b805845b3e0f05666242fe0ed5c6949d4560c0a1404a11ca0db"
    # Re-pinned by engram-alpha#2905 PR-B: taint_rate + stale_derivation_rate
    # (PR-A's interim curve rebind onto badness_pending_premise) unified into
    # ONE signal, pending_premise_rate, on the same curve, in the same
    # epistemic_integrity metric-binding slot -- in the same commit as
    # health.version.MAJOR's bump to 2 (which resets MINOR to 0, per this
    # class's own docstring instructions: a MAJOR bump re-pins BOTH hashes).
    #
    # Prior pin history (PR-A, the 2026-09-13 Lei+Kepler curves session): two
    # new curves added to curves.json (badness_pending_premise,
    # goodness_coverage) and six metric-binding changes in health/model.py's
    # MODEL_SPEC (three curve rebinds, one importance hold-out, two importance
    # reweights — see health/version.py's MINOR docstring for the full list).

    def test_derived_set_hash_matches_pinned_value(self):
        computed = hem_l0.canonical_derived_hash()
        assert computed == self._PINNED_DERIVED_HASH, (
            f"derived set changed (computed {computed!r} != pinned "
            f"{self._PINNED_DERIVED_HASH!r}) — if this is a deliberate "
            f"change to a curve/metric/aspect/signal declaration, bump "
            f"health.version.MINOR (or MAJOR if the L0 hash also changed) "
            f"and update _PINNED_DERIVED_HASH above IN THE SAME COMMIT; "
            f"if it's accidental, revert the change that caused it."
        )

    def test_gate_actually_fails_on_a_perturbed_curve(self):
        """The required control, same discipline as L0's own perturbation
        test. Reshapes a real curve's ``knots`` THROUGH THE REAL
        ``canonical_derived_hash()`` (via ``mock.patch.dict`` on the live
        ``MODEL_SPEC.curves`` registry, not a hand-rolled reimplementation
        of the hashing logic) and confirms the computed hash changes — the
        exact property #2576 exists to protect (a curve's ``knots`` are the
        one declared field its actual SHAPE is built from; reshaping them
        must be visible to this hash, not silently absorbed by a
        byte-identical dataclass elsewhere)."""
        real_hash = hem_l0.canonical_derived_hash()
        assert real_hash == self._PINNED_DERIVED_HASH  # sanity: matches the pin

        some_name = next(iter(health_model.MODEL_SPEC.curves))
        original = health_model.MODEL_SPEC.curves[some_name]
        # A genuinely different, still-valid (sorted x, same domain and
        # direction) knot sequence — nudges the FINAL knot's y by a small
        # epsilon, which stays a real reshape no matter how many knots the
        # curve currently has. (Engram-alpha#2788 shrank badness_default —
        # the first curve in this dict, hence `some_name` here — to two
        # knots, which made the PREVIOUS "keep only the first and last
        # knot" construction a no-op for a curve that already had only
        # two; nudging a y-value instead of dropping knots is robust to
        # knot count.)
        last_x, last_y = original.knots[-1]
        reshaped = CurveSpec(
            name=original.name,
            knots=original.knots[:-1] + ((last_x, last_y + 0.001),),
            monotone_direction=original.monotone_direction,
            output_range=original.output_range,
        )

        with mock.patch.dict(health_model.MODEL_SPEC.curves, {some_name: reshaped}):
            perturbed_hash = hem_l0.canonical_derived_hash()

        assert perturbed_hash != real_hash, (
            "control failed: reshaping a curve's knots did not change the "
            "derived hash"
        )

    def test_gate_actually_fails_on_a_perturbed_signal(self):
        """Same control, one layer further: perturbing a ``SignalDef``'s
        declared arithmetic (through the real ``SIGNAL_DEFS`` registry, not
        a reimplementation) must also be visible to the derived hash — the
        curve control above shows the L3 (curve) layer is covered, this
        shows L2 (signal) is too."""
        real_hash = hem_l0.canonical_derived_hash()
        assert real_hash == self._PINNED_DERIVED_HASH  # sanity: matches the pin

        some_name = next(iter(SIGNAL_DEFS))
        perturbed_def = dataclasses.replace(
            SIGNAL_DEFS[some_name],
            doc=SIGNAL_DEFS[some_name].doc + " (perturbed)",
        )

        with mock.patch.dict(SIGNAL_DEFS, {some_name: perturbed_def}):
            perturbed_hash = hem_l0.canonical_derived_hash()

        assert perturbed_hash != real_hash, (
            "control failed: perturbing a signal's doc did not change the "
            "derived hash"
        )

    def test_derived_hash_unaffected_by_l0_only_change(self):
        """The boundary the two hashes exist to draw: a change to an L0
        measurement (what ``canonical_l0_hash`` covers) must NOT move the
        derived hash — otherwise every MAJOR-worthy L0 change would also
        force an unnecessary MINOR-shaped signal, defeating the point of
        having two independent hashes that ``required_severity`` can tell
        apart."""
        real_derived_hash = hem_l0.canonical_derived_hash()

        some_name = next(iter(hem_l0.MEASUREMENTS))
        perturbed_def = dataclasses.replace(
            hem_l0.MEASUREMENTS[some_name],
            population=hem_l0.MEASUREMENTS[some_name].population + " (perturbed)",
        )
        with mock.patch.dict(hem_l0.MEASUREMENTS, {some_name: perturbed_def}):
            still_derived_hash = hem_l0.canonical_derived_hash()

        assert still_derived_hash == real_derived_hash, (
            "an L0-only perturbation moved the derived hash — the two "
            "hashes are supposed to be independent"
        )


# ---------------------------------------------------------------------------
# THE ACCEPTANCE CRITERION + ITS REQUIRED CONTROL
# ---------------------------------------------------------------------------


def _build_populated_fixture() -> sqlite3.Connection:
    """A single, richly-populated fixture — every one of the 14 counts is
    non-zero on it. See module docstring for the component layout and why
    each node carries the roles it does.

    Claim-bearing nodes (7): dv_1, dv_2 (derivation), obs_1 (observation_
    factual), obs_2 (observation_predictive), ax_1 (axiom), cj_1
    (conjecture), th_1 (theory).

    Components (induced subgraph on claim-bearing nodes only):
      {dv_1, obs_1, ax_1, cj_1}  size 4  (giant)
      {dv_2, obs_2}              size 2
      {th_1}                     size 1  (singleton)
    An edge from ev_1 (evidence, non-claim-bearing) to dv_1 proves the
    induced-subgraph filter — it must not enlarge any component.
    """
    conn = _make_conn()

    _insert(conn, "dv_1", "derivation",
            recall_summary="s", recall_keywords=["a"], embedding="[0.1]")
    _insert(conn, "dv_2", "derivation",
            recall_summary=None, recall_keywords=["a"], embedding="[0.1]")
    _insert_marker(conn, "dv_2", "supersede")

    _insert(conn, "obs_1", "observation_factual", evidence_id="ev_1",
            recall_summary="s", recall_keywords=None, embedding="[0.1]")
    _insert(conn, "obs_2", "observation_predictive", evidence_id=None,
            recall_summary="s", recall_keywords=["a"], embedding=None)

    _insert(conn, "ax_1", "axiom",
            recall_summary="s", recall_keywords=["a"], embedding="[0.1]")
    _insert_marker(conn, "ax_1", "retract")

    _insert(conn, "cj_1", "conjecture",
            recall_summary="s", recall_keywords=["a"], embedding="[0.1]")

    _insert(conn, "th_1", "theory",
            recall_summary="s", recall_keywords=None, embedding=None)

    _insert(conn, "ct_1", "contradiction", status="active")
    _insert(conn, "ct_2", "contradiction", status="resolved")

    _insert(conn, "ev_1", "evidence")
    _insert(conn, "ev_2", "evidence")  # dangling — cited by nobody

    _insert_edge(conn, "dv_1", "obs_1", "supports")
    _insert_edge(conn, "obs_1", "ax_1", "supports")
    _insert_edge(conn, "ax_1", "cj_1", "supports")
    _insert_edge(conn, "dv_2", "obs_2", "supports")
    _insert_edge(conn, "ev_1", "dv_1", "cites")  # non-claim-bearing bridge — must not connect

    return conn


# Expected non-zero L0 values on the fixture above, asserted once here so
# the derivation math below and the fixture docstring can't silently drift
# apart from each other.
_EXPECTED_L0 = {
    "derivations_current": 2,
    "claim_bearing_current": 7,
    "contradictions_current": 2,
    "observations_current": 2,
    "evidence_current": 2,
    "claim_bearing_pending_marker": 2,  # dv_2 (supersede) + ax_1 (retract) -- two distinct nodes
    "contradictions_open": 1,
    "observations_without_evidence_id": 1,
    "evidence_uncited": 1,
    "claim_bearing_without_recall_summary": 1,       # dv_2
    "claim_bearing_without_recall_keywords": 2,       # obs_1, th_1
    "claim_bearing_null_embedding": 2,                # obs_2, th_1
    "claim_bearing_giant_component_size": 4,          # {dv_1,obs_1,ax_1,cj_1}
}


def _rate(numerator: int, denominator: int) -> "float | _Unavailable":
    """Shared empty-denominator policy for the eight measurement-derived
    rates below: ALL EIGHT currently declare "unavailable" (never "clean")
    on an empty population — see each live signal_fn's own docstring
    (``open_contradiction_rate``'s explicitly changed from "clean" to
    "unavailable" in engram-alpha#2520 round 2, closing the last holdout).
    Centralized here so the policy is asserted once, not risk drifting
    per-signal inside this helper — a real off-line run against a live
    graph (PR body) is what actually caught this needing to exist: a
    fresh throwaway derivation without this branch silently mismatched on
    ``contradictions_current == 0``, the exact condition this function
    exists to handle correctly instead of raising ZeroDivisionError."""
    if denominator == 0:
        return UNAVAILABLE
    return numerator / denominator


def _derive_signals_from_measurements(m: dict) -> dict:
    """Mirrors each of the (originally nine, now eight per engram-alpha#2905
    PR-B) LIVE signal functions' own arithmetic AND empty-denominator
    policy, reading ONLY from an L0 measurements dict — this is what slice 2
    will ship as real code; here it exists solely to prove the decomposition
    is complete and correct today, on ANY graph (not just a fixture
    engineered to keep every denominator non-zero). NOT shipped, NOT
    imported by health/engine.py — see module docstring."""
    derived = {}

    derived["pending_premise_rate"] = _rate(
        m["claim_bearing_pending_marker"], m["claim_bearing_current"]
    )
    derived["open_contradiction_rate"] = _rate(
        m["contradictions_open"], m["contradictions_current"]
    )
    derived["uncited_observation_rate"] = _rate(
        m["observations_without_evidence_id"], m["observations_current"]
    )
    derived["dangling_evidence_rate"] = _rate(
        m["evidence_uncited"], m["evidence_current"]
    )
    # NOTE (engram-alpha#2538, merged 2026-09-04, rebased onto here): these
    # four — recall_summary_coverage, recall_keywords_coverage,
    # embedding_coverage, giant_component_fraction — now return LITERAL
    # coverage (1.0 = fully covered), not the badness-rate complement they
    # returned when this test file was first written. The L0 numerators
    # below are still "missing"/"without" COUNTS — #2538 touched only the
    # four signal functions' own arithmetic, never what L0 measures (the
    # measurement definitions this PR ships are untouched) — so the flip
    # lives entirely in these four formulas: ``1 - without/total`` instead
    # of ``without/total``. A clean git rebase did NOT catch this — no
    # textual overlap between this file and #2538's changed file — verified
    # by re-running the full acceptance test post-rebase, which is what
    # caught it (see this PR's own body for the concrete before/after
    # numbers this produced).
    derived["recall_summary_coverage"] = _rate(
        m["claim_bearing_without_recall_summary"], m["claim_bearing_current"]
    )
    if derived["recall_summary_coverage"] is not UNAVAILABLE:
        derived["recall_summary_coverage"] = 1.0 - derived["recall_summary_coverage"]

    derived["recall_keywords_coverage"] = _rate(
        m["claim_bearing_without_recall_keywords"], m["claim_bearing_current"]
    )
    if derived["recall_keywords_coverage"] is not UNAVAILABLE:
        derived["recall_keywords_coverage"] = 1.0 - derived["recall_keywords_coverage"]

    # embedding_coverage: L1 decides UNAVAILABLE from the environment facts
    # FIRST — that gate is separate and earlier than the denominator
    # question (#2537's own boundary rule) — then falls through to the same
    # empty-denominator policy as every other signal.
    if not (m["embedder_installed_available"] and m["embedder_config_enabled"]):
        derived["embedding_coverage"] = UNAVAILABLE
    else:
        derived["embedding_coverage"] = _rate(
            m["claim_bearing_null_embedding"], m["claim_bearing_current"]
        )
        if derived["embedding_coverage"] is not UNAVAILABLE:
            derived["embedding_coverage"] = 1.0 - derived["embedding_coverage"]

    # giant_component_fraction is now the literal connected fraction
    # (largest/total), not its complement — no "1 - " here at all.
    derived["giant_component_fraction"] = _rate(
        m["claim_bearing_giant_component_size"], m["claim_bearing_current"]
    )

    return derived


# engram-alpha#2905 PR-B: "pending_premise_rate" deliberately has NO entry
# here. health.metrics (the pre-existing, independent-oracle SQL file) has
# no equivalent function -- adding one is out of PR-B's declared scope
# (Scope: measurements.py, signals.py, model.py, version.py, plus their
# test files -- health/metrics.py is not among them). This means
# test_l0_decomposition_matches_live_signals_exactly below no longer
# independently cross-checks pending_premise_rate against a live-SQL
# oracle -- a real, flagged reduction in this class's coverage, not a
# silent one. Flagged in the PR-B coder-fairy handoff as a load-bearing
# scope gap requiring a parent decision (a companion health/metrics.py +
# health/engine.py CONFIG change) before this coverage can be restored.
_LIVE_SIGNAL_FNS = {
    "open_contradiction_rate": metrics.open_contradiction_rate,
    "uncited_observation_rate": metrics.uncited_observation_rate,
    "dangling_evidence_rate": metrics.dangling_evidence_rate,
    "recall_summary_coverage": metrics.recall_summary_coverage,
    "recall_keywords_coverage": metrics.recall_keywords_coverage,
    "embedding_coverage": metrics.embedding_coverage,
    "giant_component_fraction": metrics.giant_component_fraction,
}


class TestAcceptanceCriterion:
    def test_fixture_matches_documented_expected_l0_values(self):
        """Guards the fixture itself against silent drift from its own
        docstring/_EXPECTED_L0 table — fails loudly if a future edit changes
        the fixture without updating what it's documented to produce."""
        conn = _build_populated_fixture()
        with mock.patch("engram_core._embedder.is_available", return_value=True):
            measurements = hem_l0.compute_all_measurements(conn)
        for name, expected in _EXPECTED_L0.items():
            assert measurements[name] == expected, (
                f"{name}: fixture produced {measurements[name]!r}, "
                f"expected {expected!r} per _EXPECTED_L0"
            )
        assert measurements["embedder_installed_available"] is True
        assert measurements["embedder_config_enabled"] is True

    def test_l0_decomposition_matches_live_signals_exactly(self):
        """THE ACCEPTANCE CRITERION. Same connection for both sides
        (Ariadne's amendment #3) — one populated fixture, one conn, read
        twice: once through compute_all_measurements, once through each
        live signal_fn in health/metrics.py. Equality is exact, not
        approximate — same machine, same Python, same stored inputs, so any
        difference is a defect, not float noise."""
        conn = _build_populated_fixture()
        with mock.patch("engram_core._embedder.is_available", return_value=True), \
             mock.patch("engram_core._get_embedding_config", return_value={"enabled": True}):
            measurements = hem_l0.compute_all_measurements(conn)
            derived = _derive_signals_from_measurements(measurements)
            for name, live_fn in _LIVE_SIGNAL_FNS.items():
                live_value = live_fn(conn)  # SAME conn, no reconnect
                assert derived[name] == live_value, (
                    f"{name}: derived from L0 = {derived[name]!r}, "
                    f"live signal_fn = {live_value!r}"
                )

    def test_perturbing_a_nonzero_measurement_is_detected(self):
        """THE REQUIRED CONTROL (non-optional per the #2537 build brief and
        Ariadne's amendment #2). Perturbs
        ``claim_bearing_without_recall_summary`` — real fixture value 1, NOT
        0 (per the amendment: a perturbation landing on an already-zero
        value demonstrates nothing) — and asserts the equality check FAILS.
        A test that has only ever been seen to pass has not been shown
        capable of failing.

        NOTE (engram-alpha#2905 PR-B): this control previously perturbed
        ``claim_bearing_pending_retract_marker`` (now retired, unified into
        ``claim_bearing_pending_marker``). Switched to
        ``claim_bearing_without_recall_summary`` because
        ``pending_premise_rate`` has no live-oracle entry in
        ``_LIVE_SIGNAL_FNS`` (see that dict's own comment) — this control
        needs a measurement whose signal IS still independently oracled."""
        conn = _build_populated_fixture()
        with mock.patch("engram_core._embedder.is_available", return_value=True), \
             mock.patch("engram_core._get_embedding_config", return_value={"enabled": True}):
            measurements = hem_l0.compute_all_measurements(conn)
            assert measurements["claim_bearing_without_recall_summary"] == 1  # non-zero, per amendment #2

            perturbed = dict(measurements)
            perturbed["claim_bearing_without_recall_summary"] = 2  # corrupt: 1 -> 2

            derived = _derive_signals_from_measurements(perturbed)
            live_recall_summary_coverage = metrics.recall_summary_coverage(conn)  # same conn, unperturbed graph

            assert derived["recall_summary_coverage"] != live_recall_summary_coverage, (
                "control failed to detect the perturbation: derived "
                f"{derived['recall_summary_coverage']!r} vs live "
                f"{live_recall_summary_coverage!r} — "
                "the perturbation should have produced a mismatch"
            )


class TestMeasurementLabels:
    """engram-alpha#2580: MeasurementDef.label + measurement_labels_jsonable().
    Labels are display-only presentation metadata, NOT measurement identity
    — the L0 hash gate above (TestL0HashGate) is the falsifier for that
    claim and must stay green, unchanged, alongside these tests."""

    def test_every_measurement_has_a_nonempty_label_distinct_from_its_key(self):
        """A label that merely repeats the key is not a label — the whole
        point is a human-readable display name a viz/API consumer can show
        without a key->name table of its own."""
        for name, mdef in hem_l0.MEASUREMENTS.items():
            assert isinstance(mdef.label, str) and mdef.label.strip(), (
                f"{name} has an empty/missing label"
            )
            assert mdef.label != name, (
                f"{name}'s label merely repeats its key — not a real label"
            )

    def test_measurement_labels_jsonable_keys_match_measurements(self):
        labels = hem_l0.measurement_labels_jsonable()
        assert set(labels) == set(hem_l0.MEASUREMENTS)
        for name, label in labels.items():
            assert label == hem_l0.MEASUREMENTS[name].label

    def test_label_is_excluded_from_the_l0_hash_inputs(self):
        """THE FALSIFIER for "labels don't move identity": build the hash
        input the same way canonical_l0_hash() does (see that function's
        own docstring — the (name, population, definition_inputs) triple),
        for a MeasurementDef copy with its label changed, and assert the
        hash INPUT is unchanged. This does not merely re-assert the pinned
        hash (TestL0HashGate already does that) — it demonstrates label is
        not even in the tuple canonical_l0_hash() builds, so no future
        change to canonical_l0_hash()'s implementation could accidentally
        make it hash label without this test catching it."""
        some_name = next(iter(hem_l0.MEASUREMENTS))
        original = hem_l0.MEASUREMENTS[some_name]

        def hash_input_for(mdef):
            return (mdef.name, mdef.population, list(mdef.definition_inputs))

        relabeled = dataclasses.replace(original, label=original.label + " (renamed)")
        assert original.label != relabeled.label  # sanity: the copy actually differs
        assert hash_input_for(original) == hash_input_for(relabeled), (
            "changing label altered the L0 hash's own input tuple — "
            "label must stay presentation-only"
        )

        # And the real gate, unaffected by construction:
        with mock.patch.dict(hem_l0.MEASUREMENTS, {some_name: relabeled}):
            relabeled_hash = hem_l0.canonical_l0_hash()
        assert relabeled_hash == TestL0HashGate._PINNED_L0_HASH, (
            "relabeling a measurement changed canonical_l0_hash()'s output"
        )


# ---------------------------------------------------------------------------
# #2575 PR B — MeasurementDef.replay (D2/§3)
# ---------------------------------------------------------------------------


class TestReplayField:
    """``MeasurementDef.replay`` is L0 metadata OUTSIDE ``canonical_l0_hash``
    -- exactly like ``label`` (see ``TestReplayIsExcludedFromTheL0Hash``
    below, mirroring ``test_label_is_excluded_from_the_l0_hash_inputs``
    above) -- and D2's ``replay_decl_hash`` is the fingerprint that tracks
    it instead (see ``test_replay_declarations_today``)."""

    def test_replay_is_excluded_from_the_l0_hash_inputs(self):
        """Mirror of ``test_label_is_excluded_from_the_l0_hash_inputs``
        above: flip EVERY def's ``replay`` on a copy of the registry (not
        just one, since ``replay`` — unlike ``label`` — has a closed
        3-value domain and a single flip could coincidentally leave the
        canonical tuple's SORT ORDER unchanged; flipping every def's value
        makes the falsifier exercise the full registry) and assert
        ``canonical_l0_hash()`` is UNCHANGED."""
        flipped = {
            name: dataclasses.replace(
                mdef, replay="graph" if mdef.replay != "graph" else "stripped"
            )
            for name, mdef in hem_l0.MEASUREMENTS.items()
        }
        # sanity: the copy actually differs, for every single entry
        for name in hem_l0.MEASUREMENTS:
            assert hem_l0.MEASUREMENTS[name].replay != flipped[name].replay

        def hash_input_for(mdef):
            return (mdef.name, mdef.population, list(mdef.definition_inputs))

        for name in hem_l0.MEASUREMENTS:
            assert hash_input_for(hem_l0.MEASUREMENTS[name]) == hash_input_for(
                flipped[name]
            ), "changing replay altered the L0 hash's own input tuple"

        with mock.patch.dict(hem_l0.MEASUREMENTS, flipped):
            flipped_hash = hem_l0.canonical_l0_hash()
        assert flipped_hash == TestL0HashGate._PINNED_L0_HASH, (
            "flipping every measurement's replay value changed "
            "canonical_l0_hash()'s output — replay must stay outside the "
            "L0 hash gate, exactly like label"
        )

    def test_replay_declarations_today(self):
        """Exactly three non-``graph`` declarations today (D2/§3's table):
        ``claim_bearing_null_embedding`` is ``stripped``; the two embedder
        booleans are ``environment``; every other one of the 16 registered
        measurements is ``graph`` (13 of them, per engram-alpha#2905 PR-B's
        unification of two numerators into one). ``replay_decl_hash()`` is a
        64-hex-char sha256 digest, and it CHANGES when a single declared
        value is flipped on a registry copy (never touching the real
        module-level ``MEASUREMENTS``)."""
        declarations = hem_l0.replay_declarations()
        assert declarations == dict(sorted(declarations.items())), (
            "replay_declarations() must be sorted by name"
        )

        non_graph = {name: v for name, v in declarations.items() if v != "graph"}
        assert non_graph == {
            "claim_bearing_null_embedding": "stripped",
            "embedder_installed_available": "environment",
            "embedder_config_enabled": "environment",
        }
        graph_names = {name for name, v in declarations.items() if v == "graph"}
        assert len(graph_names) == 13
        assert len(declarations) == 16

        current_hash = hem_l0.replay_decl_hash()
        assert isinstance(current_hash, str)
        assert len(current_hash) == 64
        assert all(c in "0123456789abcdef" for c in current_hash)

        # Flip one declared value on a COPY of the registry -- the real
        # module registry (and thus the pinned L0 hash) is untouched.
        some_name = next(iter(hem_l0.MEASUREMENTS))
        original = hem_l0.MEASUREMENTS[some_name]
        flipped_one = dataclasses.replace(
            original, replay="graph" if original.replay != "graph" else "stripped"
        )
        with mock.patch.dict(hem_l0.MEASUREMENTS, {some_name: flipped_one}):
            flipped_hash = hem_l0.replay_decl_hash()
        assert flipped_hash != current_hash, (
            "flipping one measurement's declared replay value did not "
            "change replay_decl_hash() — the D2 fingerprint would silently "
            "fail to detect a declaration flip"
        )
        # And after the patch context exits, the real hash is unaffected.
        assert hem_l0.replay_decl_hash() == current_hash
