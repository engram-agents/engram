"""tests/test_partition_conformance.py — #2300: the CB/NCB red-flag channel.

Coverage:
  1. Ordinary per-invariant coverage (clean fixture -> not flagged; violating
     fixture -> flagged) for all five invariants.
  2. Six controls, each proven to be able to FAIL (broken -> red, restored
     -> green), not merely asserted once in the passing direction:
       a. the derivation control — monkeypatching
          ``engram_core.CLAIM_BEARING_TYPES`` changes the checker's derived
          ``ncb`` set and its counts. An enumerated checker would pass every
          other test in this file and fail only this one.
       b. the overlap control — invariants 1 and 3 can flag the identical
          edge set; ``distinct_total`` must be the union size, never the sum.
       c. the row-vs-node control — a single conceptual "group" (here: one
          NCB type) covering several nodes must be counted at node
          granularity, not collapsed to a GROUP BY row count.
       d. the CURRENT-ONLY control (round 2, #2300 — Lei's 2026-08-28
          13:44Z ruling) — a genuine invariant-1/3 violation whose endpoint
          is superseded (``is_current = 0``) must not be counted, because
          the remediation channel for a violation IS superseding/retracting
          the offending node.
       e. the invariant-5 reachability control (round 3, reviewer-fairy PR
          #2326 round-1 suggestion) — invariant 5's endpoint-currency join
          is dormant while ``cascade_ndc`` derives to empty by construction;
          this control monkeypatches one relation's ``dag`` flag to
          reach the guarded branch and proves the join discriminates a
          superseded endpoint once it actually runs.
       f. the invariant-4 supersede-gap control (round 4, PR #2326 round-2
          blocker) — a current prediction whose sole ``predicted_by``
          target has been superseded (``is_current = 0``) must be FLAGGED.
          Round 3 shipped a comment claiming this case was structurally
          unreachable; the round-2 reviewer falsified it against the real
          production paths (mint + supersede, no test-only shortcuts), and
          this control pins the fix (a target-currency filter, matching
          invariants 1/3/5) so the false negative cannot silently return.

Fixture shape: a minimal in-memory sqlite3 db carrying only the columns
``check_partition_conformance`` actually reads (nodes: id/type/confidence/
is_current; edges: id/source_id/target_id/relation) — no DATA_DIR, no
bootstrap, no embeddings. This module is a pure ``conn -> dict`` reader, so a
schema-shaped conn is a sufficient fixture; nothing here touches
``~/.engram/knowledge.db``.

Import-pattern contract: imports ``engram_core`` and ``engram_conformance``
directly; no ``import server``.
"""

from __future__ import annotations

import os
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import engram_conformance as conformance
import engram_core as core


# ---------------------------------------------------------------------------
# Fixture helpers
# ---------------------------------------------------------------------------


def _new_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        """CREATE TABLE nodes (
            id TEXT PRIMARY KEY,
            type TEXT NOT NULL,
            confidence REAL,
            is_current INTEGER DEFAULT 1,
            created_at TEXT
        )"""
    )
    conn.execute(
        """CREATE TABLE edges (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source_id TEXT,
            target_id TEXT,
            relation TEXT,
            created_at TEXT
        )"""
    )
    return conn


def _add_node(conn, node_id, node_type, confidence=None, is_current=1):
    conn.execute(
        "INSERT INTO nodes (id, type, confidence, is_current, created_at) "
        "VALUES (?, ?, ?, ?, 't')",
        (node_id, node_type, confidence, is_current),
    )


def _add_edge(conn, source_id, target_id, relation):
    conn.execute(
        "INSERT INTO edges (source_id, target_id, relation, created_at) "
        "VALUES (?, ?, ?, 't')",
        (source_id, target_id, relation),
    )


# A representative CB type and a representative NCB type, read from the
# shipped constants rather than hand-picked literals, so this file does not
# itself become an enumeration site. `cascade` is CASCADE_RELATIONS itself
# (today, and by #2252 Stage 1.3, exactly {"derives_from"}).
_CB_TYPE = sorted(core.CLAIM_BEARING_TYPES)[0]
_NCB_TYPE = sorted(core.VALID_NODE_TYPES - core.CLAIM_BEARING_TYPES)[0]
# Pick a cascade relation the invariant actually INSPECTS. Post-#2357 that is
# every member of CASCADE_RELATIONS: invariant 5 checks `dag` (real edge-walk
# acyclicity) rather than the creation-order proxy, and `acknowledged_derives_from`
# carries dag=True — it is temporal_causal=False, which invariant 5 does not read.
# So there is no exempt set to subtract any more, and the plain derived pick keeps
# the "dormant, not dead" join demonstrated. Derived, not hardcoded, so this file
# does not become a second enumeration site.
_CASCADE_RELATION = sorted(core.CASCADE_RELATIONS)[0]


# ---------------------------------------------------------------------------
# 1 — ordinary per-invariant coverage
# ---------------------------------------------------------------------------


def test_clean_graph_flags_nothing():
    conn = _new_conn()
    _add_node(conn, "a1", _CB_TYPE, confidence=0.8)
    _add_node(conn, "a2", _CB_TYPE, confidence=0.9)
    _add_edge(conn, "a1", "a2", _CASCADE_RELATION)
    conn.commit()

    out = conformance.check_partition_conformance(conn)

    assert out["flare"] is False
    assert out["distinct_total"] == 0
    for key in (
        "invariant_1_cascade_edge_ncb_endpoint",
        "invariant_2_ncb_node_with_confidence",
        "invariant_3_cb_node_ncb_premise",
        "invariant_4_unpaired_prediction",
        "invariant_5_cascade_not_dag_safe",
    ):
        assert out[key]["count"] == 0, f"{key} flagged a clean graph"


def test_invariant_1_cascade_edge_with_ncb_endpoint():
    conn = _new_conn()
    _add_node(conn, "cb1", _CB_TYPE, confidence=0.8)
    _add_node(conn, "ncb1", _NCB_TYPE, confidence=None)
    _add_edge(conn, "cb1", "ncb1", _CASCADE_RELATION)
    conn.commit()

    out = conformance.check_partition_conformance(conn)
    inv1 = out["invariant_1_cascade_edge_ncb_endpoint"]
    assert inv1["count"] == 1
    assert inv1["entity_kind"] == "edge"
    assert out["flare"] is True


def test_invariant_2_ncb_node_with_confidence():
    conn = _new_conn()
    _add_node(conn, "ncb1", _NCB_TYPE, confidence=0.5)
    conn.commit()

    out = conformance.check_partition_conformance(conn)
    inv2 = out["invariant_2_ncb_node_with_confidence"]
    assert inv2["count"] == 1
    assert inv2["entity_kind"] == "node"
    assert inv2["sample_ids"] == ["ncb1"]
    assert out["flare"] is True


def test_invariant_2_null_confidence_not_flagged():
    conn = _new_conn()
    _add_node(conn, "ncb1", _NCB_TYPE, confidence=None)
    conn.commit()

    out = conformance.check_partition_conformance(conn)
    assert out["invariant_2_ncb_node_with_confidence"]["count"] == 0
    assert out["flare"] is False


def test_invariant_3_cb_node_with_ncb_premise():
    conn = _new_conn()
    _add_node(conn, "cb1", _CB_TYPE, confidence=0.8)
    _add_node(conn, "ncb1", _NCB_TYPE, confidence=None)
    _add_edge(conn, "cb1", "ncb1", _CASCADE_RELATION)
    conn.commit()

    out = conformance.check_partition_conformance(conn)
    inv3 = out["invariant_3_cb_node_ncb_premise"]
    assert inv3["count"] == 1
    assert inv3["entity_kind"] == "edge"


def test_invariant_3_not_flagged_when_source_is_ncb():
    """An NCB-sourced cascade edge into another NCB node flags invariant 1
    (any NCB endpoint) but NOT invariant 3 (which requires the SOURCE to be
    CB — a CB node's own premise list)."""
    conn = _new_conn()
    _add_node(conn, "ncb1", _NCB_TYPE, confidence=None)
    _add_node(conn, "ncb2", _NCB_TYPE, confidence=None)
    _add_edge(conn, "ncb1", "ncb2", _CASCADE_RELATION)
    conn.commit()

    out = conformance.check_partition_conformance(conn)
    assert out["invariant_1_cascade_edge_ncb_endpoint"]["count"] == 1
    assert out["invariant_3_cb_node_ncb_premise"]["count"] == 0


def test_invariant_4_unpaired_prediction():
    conn = _new_conn()
    _add_node(conn, "pr_1", "prediction", confidence=None)
    conn.commit()

    out = conformance.check_partition_conformance(conn)
    inv4 = out["invariant_4_unpaired_prediction"]
    assert inv4["count"] == 1
    assert inv4["entity_kind"] == "node"
    assert inv4["sample_ids"] == ["pr_1"]


def test_invariant_4_paired_prediction_not_flagged():
    conn = _new_conn()
    _add_node(conn, "pr_1", "prediction", confidence=None)
    _add_node(conn, "ob_1", "observation_predictive", confidence=0.7)
    for relation in core.PREDICTION_PAIRING_RELATIONS:
        _add_edge(conn, "pr_1", "ob_1", relation)
    conn.commit()

    out = conformance.check_partition_conformance(conn)
    assert out["invariant_4_unpaired_prediction"]["count"] == 0


def test_invariant_5_derives_to_empty_by_construction():
    """#2300: invariant 5 is a static invariant holding by construction
    today — every cascade relation also has dag:True in EDGE_CLASSIFICATIONS
    (#2357 renamed dag_check -> temporal_causal; this invariant follows the
    new `dag` flag, not temporal_causal — see engram_conformance.py's
    `_derive_cascade` docstring), so cascade_ndc is the empty set and no
    fixture can make invariant 5 non-empty without also editing
    EDGE_CLASSIFICATIONS itself. This test pins that emptiness rather than
    special-casing it."""
    non_dag_cascade = {
        r for r, c in core.EDGE_CLASSIFICATIONS.items()
        if r in core.CASCADE_RELATIONS and c["dag_family"] is None
    }
    assert non_dag_cascade == set(), (
        "a cascade relation now lacks dag — invariant 5 should no "
        "longer be vacuous; this test's premise needs updating alongside it"
    )

    conn = _new_conn()
    _add_node(conn, "cb1", _CB_TYPE, confidence=0.8)
    _add_node(conn, "cb2", _CB_TYPE, confidence=0.8)
    _add_edge(conn, "cb1", "cb2", _CASCADE_RELATION)
    conn.commit()

    out = conformance.check_partition_conformance(conn)
    assert out["invariant_5_cascade_not_dag_safe"]["count"] == 0


# ---------------------------------------------------------------------------
# 2 — the six controls (#2300's actual point)
# ---------------------------------------------------------------------------


def test_control_a_derivation_can_fail_and_recover(monkeypatch):
    """The derivation control. Monkeypatch CLAIM_BEARING_TYPES to ADD the
    graph's only NCB type, and confirm the checker's derived `ncb` set (and
    therefore invariants 1/2/3, all NCB-conditioned) tracks the patch.

    Proof it can fail: an enumerated checker (one that hardcoded its own
    NCB type list instead of deriving `ncb = present_types - cb`) would
    return the SAME counts before and after this patch, because it never
    reads CLAIM_BEARING_TYPES for the ncb computation. This test's own
    "after" assertions are the red/green pair: verified by hand (see
    handoff) by temporarily hardcoding `ncb = {"contradiction"}` (a fixed
    literal, chosen to match this fixture's NCB type so the "before"
    assertions still passed) inside `_derive_cb_ncb` in
    engram_conformance.py — the "after" assertions then failed (counts held
    at 1 instead of dropping to 0, since the hardcoded set never responds to
    the CLAIM_BEARING_TYPES patch); reverting to the derived form made all
    assertions pass again.
    """
    conn = _new_conn()
    _add_node(conn, "cb1", _CB_TYPE, confidence=0.8)
    _add_node(conn, "ncb1", _NCB_TYPE, confidence=0.5)
    _add_edge(conn, "cb1", "ncb1", _CASCADE_RELATION)
    conn.commit()

    before = conformance.check_partition_conformance(conn)
    assert before["invariant_1_cascade_edge_ncb_endpoint"]["count"] == 1
    assert before["invariant_2_ncb_node_with_confidence"]["count"] == 1
    assert before["invariant_3_cb_node_ncb_premise"]["count"] == 1

    monkeypatch.setattr(
        core, "CLAIM_BEARING_TYPES", core.CLAIM_BEARING_TYPES | {_NCB_TYPE}
    )

    after = conformance.check_partition_conformance(conn)
    assert after["invariant_1_cascade_edge_ncb_endpoint"]["count"] == 0, (
        "invariant 1 did not track the CLAIM_BEARING_TYPES patch — ncb is "
        "not genuinely derived from the shipped constant"
    )
    assert after["invariant_2_ncb_node_with_confidence"]["count"] == 0
    assert after["invariant_3_cb_node_ncb_premise"]["count"] == 0


def test_control_b_overlap_is_union_not_sum():
    """The overlap control. A single edge that violates BOTH invariant 1 and
    invariant 3 (a CB-sourced cascade edge into an NCB target — flags 1 via
    "any NCB endpoint" and 3 via "CB source, NCB target") must contribute
    ONE id to `distinct_total`, not two. A naive sum would read 1 + 1 = 2.

    Proof it can fail: replacing the union with a plain sum of the five
    per-invariant counts changes this fixture's total from 1 to 2. Verified
    by hand (see handoff): temporarily changing
    `check_partition_conformance`'s `distinct_total` line to
    `sum(block["count"] for block in (...))` made this test fail
    (2 != 1); reverting to the union made it pass.
    """
    conn = _new_conn()
    _add_node(conn, "cb1", _CB_TYPE, confidence=0.8)
    _add_node(conn, "ncb1", _NCB_TYPE, confidence=None)
    _add_edge(conn, "cb1", "ncb1", _CASCADE_RELATION)
    conn.commit()

    out = conformance.check_partition_conformance(conn)
    assert out["invariant_1_cascade_edge_ncb_endpoint"]["count"] == 1
    assert out["invariant_3_cb_node_ncb_premise"]["count"] == 1
    naive_sum = sum(
        out[k]["count"]
        for k in (
            "invariant_1_cascade_edge_ncb_endpoint",
            "invariant_2_ncb_node_with_confidence",
            "invariant_3_cb_node_ncb_premise",
            "invariant_4_unpaired_prediction",
            "invariant_5_cascade_not_dag_safe",
        )
    )
    assert naive_sum == 2, "fixture no longer exercises the overlap (both invariants must fire)"
    assert out["distinct_total"] == 1, (
        f"distinct_total ({out['distinct_total']}) should be the UNION size "
        f"(1, the single shared edge), not the sum ({naive_sum})"
    )


def test_control_c_row_vs_node_count():
    """The row-vs-node control. Three NCB nodes of the SAME type, each
    carrying confidence, must be counted as 3 (nodes), never collapsed to 1
    (the single GROUP BY row a `GROUP BY type` query would produce).

    Proof it can fail: replacing invariant 2's node-level SELECT with a
    `SELECT type, COUNT(*) ... GROUP BY type` and reporting the ROW count
    reduces this fixture's count from 3 to 1. Verified by hand (see
    handoff): temporarily rewriting `_invariant_2` to
    `SELECT type, COUNT(*) as c FROM nodes WHERE ... GROUP BY type` and
    returning `len(rows)` (the row count) made this test fail (1 != 3);
    reverting to the per-node SELECT made it pass.
    """
    conn = _new_conn()
    _add_node(conn, "ncb1", _NCB_TYPE, confidence=0.3)
    _add_node(conn, "ncb2", _NCB_TYPE, confidence=0.4)
    _add_node(conn, "ncb3", _NCB_TYPE, confidence=0.5)
    conn.commit()

    # Sanity: this fixture really is one GROUP BY row's worth of distinct
    # types, so a row-count regression would read 1 here.
    group_by_rows = conn.execute(
        "SELECT type, COUNT(*) as c FROM nodes WHERE confidence IS NOT NULL "
        "GROUP BY type"
    ).fetchall()
    assert len(group_by_rows) == 1, "fixture must be a single GROUP BY row"

    out = conformance.check_partition_conformance(conn)
    inv2 = out["invariant_2_ncb_node_with_confidence"]
    assert inv2["count"] == 3, (
        f"invariant 2 reported {inv2['count']} — expected the NODE count "
        f"(3), not the GROUP BY row count (1)"
    )
    assert set(inv2["sample_ids"]) == {"ncb1", "ncb2", "ncb3"}


def test_control_d_superseded_endpoint_not_flagged():
    """The CURRENT-ONLY control (Lei, 2026-08-28 13:44Z ruling on #2300
    Defect 2) — the point of round 2. A genuine invariant-1/invariant-3
    violation (CB-sourced cascade edge into an NCB target) whose NCB
    endpoint is superseded (``is_current = 0``) must NOT be counted by
    either invariant.

    Why this matters, in Lei's words (see the module docstring): the
    official way to FIX a partition violation IS to supersede/retract the
    violating node. If a superseded endpoint kept counting, the fix could
    never move this diagnostic and the surface would be useless for
    tracking remediation progress.

    Proof it can fail: verified by hand (see handoff) by temporarily
    removing the ``AND s.is_current = 1 AND t.is_current = 1`` clause from
    ``_invariant_1`` in engram_conformance.py — this test's invariant-1
    assertion then failed (count 1 instead of 0); restoring the clause made
    it pass again.
    """
    conn = _new_conn()
    _add_node(conn, "cb1", _CB_TYPE, confidence=0.8)
    _add_node(conn, "ncb1", _NCB_TYPE, confidence=None, is_current=0)
    _add_edge(conn, "cb1", "ncb1", _CASCADE_RELATION)
    conn.commit()

    out = conformance.check_partition_conformance(conn)
    assert out["invariant_1_cascade_edge_ncb_endpoint"]["count"] == 0, (
        "invariant 1 flagged an edge with a superseded (is_current=0) "
        "endpoint — CURRENT-ONLY filter missing or broken"
    )
    assert out["invariant_3_cb_node_ncb_premise"]["count"] == 0, (
        "invariant 3 flagged an edge with a superseded (is_current=0) "
        "endpoint — CURRENT-ONLY filter missing or broken"
    )
    assert out["flare"] is False


def test_control_e_invariant_5_endpoint_filter_reaches_guarded_branch(monkeypatch):
    """The invariant-5 reachability control (reviewer-fairy, PR #2326 round
    1 suggestion). ``_invariant_5``'s ``is_current`` endpoint join is
    commented "dormant, not dead" but, as shipped, ``cascade_ndc`` derives
    to the empty set (every CASCADE_RELATIONS member also has ``dag: True``
    in EDGE_CLASSIFICATIONS today — #2357 renamed the old ``dag_check`` to
    ``temporal_causal`` and this invariant now follows the new ``dag`` flag),
    so the ``if not cascade_ndc`` guard short-circuits before the join's
    query ever runs — the filter is asserted, not demonstrated.

    This test reaches the guarded branch without touching the shipped
    constants permanently: it flips one cascade relation's own ``dag``
    entry to False via ``monkeypatch.setitem`` on the nested
    ``EDGE_CLASSIFICATIONS[<relation>]`` dict (auto-restored at teardown —
    the shipped module-level dict is never left mutated for other tests).
    ``core.CASCADE_RELATIONS`` itself is a frozenset built once at import
    time from the *original* ``cascade`` flags, so it is untouched by this
    patch; only ``dag`` — which ``_derive_cascade`` re-reads from
    ``EDGE_CLASSIFICATIONS`` at CALL time, not import time — moves, which is
    exactly the lever ``cascade_ndc = cascade & non_dag`` needs to become
    non-empty.

    With the branch reached, this builds two edges on the (now-marked)
    non-dag-safe cascade relation — one with both endpoints current, one
    with a superseded (``is_current = 0``) endpoint — and asserts invariant
    5 flags ONLY the both-current edge, proving the endpoint join + filter
    behave like invariants 1/3 when they actually run.

    Proof it can fail: verified by hand (see handoff) by temporarily
    stripping the ``AND s.is_current = 1 AND t.is_current = 1`` clause from
    ``_invariant_5`` in engram_conformance.py — this test's count assertion
    then failed (count 2 instead of 1, sample_ids included the superseded
    edge); restoring the clause made it pass again, with the rest of the
    suite green throughout.
    """
    monkeypatch.setitem(core.EDGE_CLASSIFICATIONS[_CASCADE_RELATION], "dag_family", None)

    # Sanity: the patch really does make cascade_ndc non-empty, else this
    # test would silently fall back to exercising the short-circuit guard
    # it claims to bypass.
    cascade, cascade_ndc = conformance._derive_cascade()
    assert cascade_ndc == {_CASCADE_RELATION}, (
        "monkeypatching EDGE_CLASSIFICATIONS[relation]['dag_family'] did not "
        "make cascade_ndc non-empty — this test is not reaching the "
        "guarded branch it exists to cover"
    )

    conn = _new_conn()
    _add_node(conn, "cb1", _CB_TYPE, confidence=None)
    _add_node(conn, "cb2", _CB_TYPE, confidence=None)
    current_edge_id = conn.execute(
        "INSERT INTO edges (source_id, target_id, relation, created_at) "
        "VALUES (?, ?, ?, 't')",
        ("cb1", "cb2", _CASCADE_RELATION),
    ).lastrowid

    _add_node(conn, "cb3", _CB_TYPE, confidence=None)
    _add_node(conn, "cb4", _CB_TYPE, confidence=None, is_current=0)
    conn.execute(
        "INSERT INTO edges (source_id, target_id, relation, created_at) "
        "VALUES (?, ?, ?, 't')",
        ("cb3", "cb4", _CASCADE_RELATION),
    )
    conn.commit()

    out = conformance.check_partition_conformance(conn)
    inv5 = out["invariant_5_cascade_not_dag_safe"]
    assert inv5["entity_kind"] == "edge"
    assert inv5["count"] == 1, (
        f"invariant 5 flagged {inv5['count']} edges once cascade_ndc was "
        "non-empty; expected exactly the both-current edge — the "
        "superseded-endpoint edge must be excluded by the is_current join"
    )
    assert inv5["sample_ids"] == [current_edge_id]
    assert out["flare"] is True


def test_control_f_invariant_4_superseded_basis_flagged():
    """The invariant-4 supersede-gap control (round 4, PR #2326 round-2
    blocker). A current prediction whose ONLY ``predicted_by`` target is a
    superseded (``is_current = 0``) observation_predictive must be FLAGGED —
    this is the gap round 3's comment on ``_invariant_4`` incorrectly
    claimed could not happen. See that function's current docstring for the
    corrected three-transition analysis: fresh-mint and retract-cascade
    carry structural guards, supersede does not.

    Fixture models the round-2 reviewer's reproduction: a prediction paired
    to an observation_predictive that has since been superseded, with no
    other ``predicted_by`` edge pointing anywhere live. Before round 4's
    target-currency filter, this prediction had a ``predicted_by`` edge (to
    a now-non-current node) and so was NOT flagged — the false negative
    this control exists to prevent from recurring.

    Proof it can fail: verified by hand (see handoff) by temporarily
    stripping the ``AND t.is_current = 1`` clause added to ``_invariant_4``'s
    query in engram_conformance.py — this test's count assertion then
    failed (count 0 instead of 1, sample_ids empty); restoring the clause
    made it pass again, with the rest of the suite green throughout.
    """
    conn = _new_conn()
    _add_node(conn, "pr_1", "prediction", confidence=None)
    _add_node(conn, "ob_1", "observation_predictive", confidence=0.7, is_current=0)
    for relation in core.PREDICTION_PAIRING_RELATIONS:
        _add_edge(conn, "pr_1", "ob_1", relation)
    conn.commit()

    out = conformance.check_partition_conformance(conn)
    inv4 = out["invariant_4_unpaired_prediction"]
    assert inv4["count"] == 1, (
        "invariant 4 did not flag a prediction whose sole predicted_by "
        "target is superseded — the target-currency filter is missing or "
        "broken"
    )
    assert inv4["sample_ids"] == ["pr_1"]
    assert out["flare"] is True
