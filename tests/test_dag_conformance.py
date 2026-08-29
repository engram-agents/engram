"""tests/test_dag_conformance.py — #2357: the real-acyclicity conformance check.

Fixture shape: same discipline as test_partition_conformance.py — a minimal
in-memory sqlite3 db carrying only the columns `check_dag_conformance`
actually reads (nodes: id/is_current; edges: source_id/target_id/relation).

Coverage:
  1. Clean graph (no dag=True edges at all) -> cycle_found False.
  2. A genuine acyclic chain over dag=True relations -> cycle_found False.
  3. Borges's exact construction (PR #2342 round-1 finding, the motivating
     case for #2357): base -> P, X derives_from P, P' derives_from X
     (legal — X is unmarked at that moment), supersede P -> P' (modeled here
     as a plain `supersedes` edge, since this module doesn't care which
     dag=True relation forms the cycle), then acknowledge X (X
     acknowledged_derives_from P') — closes X -> P' -> X. Must report
     cycle_found True with a path containing both X and P'.
  4. CURRENT-ONLY: a cycle that exists solely among non-current
     (is_current=0) nodes must NOT be flagged — same remediation-channel
     reasoning as engram_conformance.py's CB/NCB invariants (Lei,
     2026-08-28 ruling on #2300 Defect 2): supersede/retract is how a real
     violation gets fixed, so counting non-current state could never let
     this metric register progress.
  5. Control: a relation NOT in DAG_RELATIONS (e.g. `about`, dag=False)
     forming a cycle must NOT be flagged — proves the walk is scoped to
     `dag: True` relations, not every edge in the graph.
  6. Control: monkeypatching DAG_RELATIONS empty makes a genuine cycle
     invisible — proves the checker actually walks the relation set it's
     given rather than being vacuously clean by construction.

Import-pattern contract (#948 wave C): imports engram_core and
engram_dag_conformance directly; no ``import server``.
"""

from __future__ import annotations

import os
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import engram_core as core
import engram_dag_conformance as dagconf


def _new_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        """CREATE TABLE nodes (
            id TEXT PRIMARY KEY,
            type TEXT NOT NULL,
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


def _add_node(conn, node_id, is_current=1):
    conn.execute(
        "INSERT INTO nodes (id, type, is_current, created_at) VALUES (?, 'observation_factual', ?, 't')",
        (node_id, is_current),
    )


def _add_edge(conn, source_id, target_id, relation):
    conn.execute(
        "INSERT INTO edges (source_id, target_id, relation, created_at) VALUES (?, ?, ?, 't')",
        (source_id, target_id, relation),
    )


def test_clean_graph_no_cycle():
    conn = _new_conn()
    _add_node(conn, "a")
    _add_node(conn, "b")
    _add_edge(conn, "a", "b", "derives_from")
    conn.commit()

    out = dagconf.check_dag_conformance(conn)
    assert out["cycle_found"] is False
    assert out["cycle_path"] == []
    assert out["dag_relation_count"] > 0


def test_acyclic_chain_no_cycle():
    """A -> B -> C -> D over derives_from — a real chain, not a cycle."""
    conn = _new_conn()
    for n in ("a", "b", "c", "d"):
        _add_node(conn, n)
    _add_edge(conn, "a", "b", "derives_from")
    _add_edge(conn, "b", "c", "derives_from")
    _add_edge(conn, "c", "d", "derives_from")
    conn.commit()

    out = dagconf.check_dag_conformance(conn)
    assert out["cycle_found"] is False


def test_borges_construction_closes_a_cycle():
    """The motivating case (PR #2342 round-1 finding): base -> P, X
    derives_from P, P' derives_from X, supersede P -> P', acknowledge X.
    The ack edge closes X -> P' -> X.
    """
    conn = _new_conn()
    for n in ("base", "p", "x", "p_prime"):
        _add_node(conn, n)
    _add_edge(conn, "p", "base", "derives_from")        # base -> P
    _add_edge(conn, "x", "p", "derives_from")            # X derives_from P
    _add_edge(conn, "p_prime", "x", "derives_from")      # P' derives_from X
    _add_edge(conn, "p_prime", "p", "supersedes")        # supersede P -> P'
    _add_edge(conn, "x", "p_prime", "acknowledged_derives_from")  # acknowledge X
    conn.commit()

    out = dagconf.check_dag_conformance(conn)
    assert out["cycle_found"] is True, (
        "the ack edge (x -acknowledged_derives_from-> p_prime) closes "
        "x -> p_prime -> x via the existing p_prime-derives_from-x edge; "
        "this must be caught"
    )
    assert {"x", "p_prime"}.issubset(set(out["cycle_path"])), (
        f"cycle path {out['cycle_path']} should include both x and p_prime"
    )


def test_cycle_among_noncurrent_nodes_not_flagged():
    """CURRENT-ONLY (mirrors engram_conformance.py's CB/NCB discipline): a
    cycle that exists solely among is_current=0 nodes is not a live hazard —
    the remediation channel (supersede/retract) already acted on it."""
    conn = _new_conn()
    _add_node(conn, "a", is_current=0)
    _add_node(conn, "b", is_current=0)
    _add_edge(conn, "a", "b", "derives_from")
    _add_edge(conn, "b", "a", "supersedes")
    conn.commit()

    out = dagconf.check_dag_conformance(conn)
    assert out["cycle_found"] is False, (
        "a cycle among non-current nodes must not be flagged — CURRENT-ONLY"
    )


def test_non_dag_relation_cycle_not_flagged():
    """A cycle over a relation NOT in DAG_RELATIONS (dag=False) must not be
    flagged — proves the walk is scoped to dag=True relations specifically,
    not every edge in the graph."""
    assert "about" not in core.DAG_RELATIONS, (
        "test premise: 'about' must be dag=False for this control to mean "
        "anything"
    )
    conn = _new_conn()
    _add_node(conn, "a")
    _add_node(conn, "b")
    _add_edge(conn, "a", "b", "about")
    _add_edge(conn, "b", "a", "about")
    conn.commit()

    out = dagconf.check_dag_conformance(conn)
    assert out["cycle_found"] is False


def test_deep_chain_no_recursion_error():
    """Reviewer-fairy round 1 (PR #2360) stress test: a naive recursive DFS
    raises RecursionError on a deep chain (Python's default limit is 1000).
    _find_cycle is iterative (explicit stack) specifically so a long-lived
    install's derives_from/supported_by/acknowledged_derives_from chain
    can't crash the entire diagnostics call (engram_stats.py has no
    try/except around this call site, unlike the timestamp-DAG startup
    scan, which does). 2000 nodes, no cycle — must return cleanly."""
    conn = _new_conn()
    for i in range(2000):
        _add_node(conn, f"n{i}")
    for i in range(1999):
        _add_edge(conn, f"n{i}", f"n{i + 1}", "derives_from")
    conn.commit()

    out = dagconf.check_dag_conformance(conn)
    assert out["cycle_found"] is False


def test_deep_chain_with_cycle_still_found():
    """Same 2000-node depth, but closed into a cycle at the far end — the
    iterative rewrite must still find it, not just avoid crashing."""
    conn = _new_conn()
    for i in range(2000):
        _add_node(conn, f"n{i}")
    for i in range(1999):
        _add_edge(conn, f"n{i}", f"n{i + 1}", "derives_from")
    _add_edge(conn, "n1999", "n0", "derives_from")
    conn.commit()

    out = dagconf.check_dag_conformance(conn)
    assert out["cycle_found"] is True
    assert len(out["cycle_path"]) > 1000


def test_control_empty_dag_relations_hides_a_real_cycle(monkeypatch):
    """Proof the checker isn't vacuously clean by construction: monkeypatch
    DAG_RELATIONS to empty and confirm a genuine cycle becomes invisible —
    demonstrating the check function actually walks the relation set it's
    given, rather than always returning cycle_found=False regardless of
    input."""
    conn = _new_conn()
    for n in ("a", "b", "c"):
        _add_node(conn, n)
    _add_edge(conn, "a", "b", "derives_from")
    _add_edge(conn, "b", "c", "derives_from")
    _add_edge(conn, "c", "a", "derives_from")
    conn.commit()

    # Sanity: with the real DAG_RELATIONS, this cycle IS caught.
    out_real = dagconf.check_dag_conformance(conn)
    assert out_real["cycle_found"] is True

    monkeypatch.setattr(core, "DAG_RELATIONS", frozenset())
    out_patched = dagconf.check_dag_conformance(conn)
    assert out_patched["cycle_found"] is False
    assert out_patched["dag_relation_count"] == 0
