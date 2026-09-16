"""tests/test_dag_conformance.py — #2357: the real-acyclicity conformance check.

Fixture shape: same discipline as test_partition_conformance.py — a minimal
in-memory sqlite3 db carrying only the columns `check_dag_conformance`
(and, as of #2813, `_live_dag_edges`'s supersedes-resolution) actually
reads (nodes: id/is_current/superseded_by; edges:
source_id/target_id/relation).

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
  7. #2813 regression: a cycle that only closes once a superseded
     endpoint is resolved through the REAL supersedes mechanism
     (is_current=0 + superseded_by, no edge) to its current successor —
     the old SQL-level `is_current = 1` filter dropped the edge outright
     and missed this; must now be caught, with the superseded node itself
     never appearing in the resolved cycle path.
  8. #2813 control: a dag-relation edge touching a node whose supersedes
     chain dead-ends (retracted, no replacement) must be dropped safely —
     no crash, no false cycle.

(Two more tests exist below — test_deep_chain_no_recursion_error and
test_deep_chain_with_cycle_still_found — pre-existing at #2813 time and
not enumerated in this list before this file was touched; left as found,
not renumbered, since fixing that drift is outside #2813's scope.)

#2943 regression coverage (appended, not renumbered into the list above for
the same reason as the two tests noted just above): a vanilla single
supersedes/retracts edge was reporting a phantom one-node cycle
(cycle_path: [X, X]), because `_live_dag_edges` resolved EVERY dag-relation
edge's endpoints through `_resolve_current` -- including supersedes/retracts
edges themselves, whose own target's resolution IS the edge's source by
construction (A supersedes B => resolve(B) == A => edge (A, B) resolves to
the tautological self-loop (A, A)).

Round 1 fixed this by carving supersedes/retracts out of the
resolve-through-chain treatment (a `_HISTORY_RELATIONS` relation-based
special case). Round 2 (Kepler's live-graph counter-example) found the
carve-out was too narrow: the phantom fires for ANY dag edge whose target
has been superseded specifically by the edge's own source, regardless of
that edge's OWN relation -- e.g. a node that both `derives_from` AND
`supersedes` the same predecessor still manufactures the identical
self-loop via its (non-exempt) `derives_from` edge. Round 2 replaces the
relation-based carve-out with the correct general invariant: a resolved
edge whose endpoints collapse to the SAME node is dropped, never added as
a self-loop, regardless of which relation it is on -- this can never mask
a genuine multi-node cycle, since a self-loop edge adds no reachability to
any node besides itself. `_HISTORY_RELATIONS` is retired; every dag edge
resolves through the chain uniformly again, exactly as #2827/#2813
originally intended.

The cross-relation real-cycle case (#2813's own motivating shape, covered
above by test_cycle_through_superseded_endpoint_is_caught) and a genuine
cycle purely among supersedes edges must both remain caught -- see
test_vanilla_supersedes_edge_not_flagged,
test_vanilla_retracts_edge_not_flagged,
test_genuine_cycle_among_supersedes_edges_still_caught,
test_derives_from_and_supersedes_same_target_not_flagged, and
test_cites_and_supersedes_same_target_not_flagged below.

Round 3 (a colleague's live-graph review): round 2's "drop any resolved
self-loop" rule was too broad -- it also silently drops a GENUINE raw
self-edge already in the table (source_id == target_id, no resolution
involved), which is the simplest real DAG violation there is. Fixed by
distinguishing raw-already-equal (keep, real cycle) from resolution-
manufactured-equal (drop, artifact) -- see
test_genuine_raw_self_edge_still_caught below.

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
            superseded_by TEXT,
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


def _add_node(conn, node_id, is_current=1, superseded_by=None):
    conn.execute(
        "INSERT INTO nodes (id, type, is_current, superseded_by, created_at) "
        "VALUES (?, 'observation_factual', ?, ?, 't')",
        (node_id, is_current, superseded_by),
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


def test_cycle_through_superseded_endpoint_is_caught():
    """#2813: the exact bug this issue fixes. The OLD `_live_dag_edges`
    filtered edges on `s.is_current = 1 AND t.is_current = 1` at the SQL
    level -- so once `a` is superseded (real mechanism: is_current=0,
    superseded_by='a2', no edge created for the supersession itself), the
    a->b edge simply vanished from the walk instead of being resolved onto
    a2->b, and the resulting a2 -> b -> a2 cycle went undetected.

    Regression: assert the cycle IS caught, and that the superseded node
    `a` itself never appears in the resolved cycle path (only its current
    successor a2 should) -- exactly the resolve-through-supersedes behavior
    the old SQL-level filter never performed."""
    conn = _new_conn()
    _add_node(conn, "a", is_current=0, superseded_by="a2")
    _add_node(conn, "b", is_current=1)
    _add_node(conn, "a2", is_current=1)
    _add_edge(conn, "a", "b", "derives_from")  # a -> b (a now superseded by a2)
    _add_edge(conn, "b", "a2", "derives_from")  # b -> a2, closing a2 -> b -> a2
    conn.commit()

    out = dagconf.check_dag_conformance(conn)
    assert out["cycle_found"] is True, (
        "resolving a's supersede chain onto a2 turns a->b into a2->b, which "
        "closes a2 -> b -> a2 via the existing b->a2 edge; this must be caught"
    )
    assert {"a2", "b"}.issubset(set(out["cycle_path"])), (
        f"cycle path {out['cycle_path']} should include both a2 and b"
    )
    assert "a" not in out["cycle_path"], (
        "the superseded node a must never appear in a resolved cycle path -- "
        "only its current successor a2 should"
    )


def test_cycle_through_superseded_endpoint_as_target_is_caught():
    """#2813 reviewer-fairy round-1 finding: the sibling test above only ever
    puts the superseded node in the edge SOURCE position (`a -> b`), so a
    plausible-wrong implementation that resolves the source endpoint but
    leaves the TARGET endpoint raw still passes it -- in that graph the raw
    and resolved target ids happen to coincide. This test puts the
    superseded node ONLY in the edge TARGET position: `p_old` never appears
    as a source in the raw edge set.

    p (current), q (current), p_old (superseded, superseded_by='p'). Raw
    edges: p -> q, q -> p_old. Resolving p_old onto p turns q -> p_old into
    q -> p, closing p -> q -> p -- a cycle invisible until the TARGET
    endpoint is resolved."""
    conn = _new_conn()
    _add_node(conn, "p", is_current=1)
    _add_node(conn, "q", is_current=1)
    _add_node(conn, "p_old", is_current=0, superseded_by="p")
    _add_edge(conn, "p", "q", "derives_from")       # p -> q
    _add_edge(conn, "q", "p_old", "derives_from")   # q -> p_old (p_old superseded by p)
    conn.commit()

    out = dagconf.check_dag_conformance(conn)
    assert out["cycle_found"] is True, (
        "resolving p_old's supersede chain onto p turns q->p_old into q->p, "
        "which closes p -> q -> p via the existing p->q edge; this must be "
        "caught"
    )
    assert {"p", "q"}.issubset(set(out["cycle_path"])), (
        f"cycle path {out['cycle_path']} should include both p and q"
    )
    assert "p_old" not in out["cycle_path"], (
        "the superseded node p_old must never appear in a resolved cycle "
        "path -- only its current successor p should"
    )


def test_dead_ended_supersede_chain_dropped_not_crashed():
    """#2813 control: a retracted node with NO replacement (is_current=0,
    superseded_by=NULL) that carries a dag-relation edge to a live node
    must have that edge silently dropped -- there is no live node left to
    attribute it to -- not crash the walk and not manufacture a false
    cycle. Mirrors the "dead-ended chain: no live node to attribute the
    edge to" reasoning `core._generalizes_cycle_path` uses for its own
    resolve-then-walk. This graph has exactly one dag-relation edge, and
    it is the one that gets dropped, so zero live edges remain -- the only
    correct answer is cycle_found False."""
    conn = _new_conn()
    _add_node(conn, "x", is_current=0, superseded_by=None)  # retracted, no successor
    _add_node(conn, "y", is_current=1)
    _add_edge(conn, "x", "y", "derives_from")
    conn.commit()

    out = dagconf.check_dag_conformance(conn)
    assert out["cycle_found"] is False, (
        "x's chain dead-ends (retracted, no superseded_by) -- the x->y edge "
        "must be dropped, leaving zero live dag edges to cycle over"
    )
    assert out["cycle_path"] == []


def test_vanilla_supersedes_edge_not_flagged():
    """#2943: a single, completely normal supersedes edge (A supersedes B;
    B is now non-current, superseded_by='A') must NOT report a cycle.

    Root cause of the bug this guards: `_live_dag_edges` resolves EVERY
    dag-relation edge's endpoints through `_resolve_current`. Resolving B
    (superseded_by='A') returns A, so the raw edge (A, B) resolves to
    (A, A) -- a tautological self-loop manufactured purely by resolving a
    supersedes edge's target through the very relation the edge represents.
    This happens for EVERY vanilla supersedes edge in any graph. The fix
    (round 2, general): a resolved edge whose endpoints collapse to the
    same node is dropped, never added -- see the module docstring above."""
    conn = _new_conn()
    _add_node(conn, "a", is_current=1)
    _add_node(conn, "b", is_current=0, superseded_by="a")
    _add_edge(conn, "a", "b", "supersedes")
    conn.commit()

    out = dagconf.check_dag_conformance(conn)
    assert out["cycle_found"] is False, (
        f"a single vanilla supersedes edge must never report a cycle -- got "
        f"{out}"
    )
    assert out["cycle_path"] == []


def test_vanilla_retracts_edge_not_flagged():
    """#2943 sibling: same phantom-self-loop bug, on `retracts` instead of
    `supersedes` -- both relations are dag=True and both are the historical
    record that defines node currency, so both hit the same collapse."""
    conn = _new_conn()
    _add_node(conn, "a", is_current=1)
    _add_node(conn, "b", is_current=0, superseded_by="a")
    _add_edge(conn, "a", "b", "retracts")
    conn.commit()

    out = dagconf.check_dag_conformance(conn)
    assert out["cycle_found"] is False, (
        f"a single vanilla retracts edge must never report a cycle -- got "
        f"{out}"
    )
    assert out["cycle_path"] == []


def test_derives_from_and_supersedes_same_target_not_flagged():
    """#2943 round 2 (found live on a real fleet graph): a node that both
    `derives_from` AND `supersedes` the same predecessor must not report a
    cycle via the (non-history) derives_from edge.

    `newer` derives_from `older` (a normal citation of the claim it's
    replacing) AND `newer` supersedes `older`. The supersedes edge itself
    is fine either way, but the derives_from edge is NOT on a history
    relation -- under the round-1 relation-based carve-out it still got
    resolved: resolve(newer)=newer (current), resolve(older)=newer
    (older's own successor, via the very supersedes fact) -> the identical
    phantom self-loop (newer, newer). This is what proved the round-1
    fix's relation-based carve-out was scoped to the wrong axis."""
    conn = _new_conn()
    _add_node(conn, "newer", is_current=1)
    _add_node(conn, "older", is_current=0, superseded_by="newer")
    _add_edge(conn, "newer", "older", "derives_from")
    _add_edge(conn, "newer", "older", "supersedes")
    conn.commit()

    out = dagconf.check_dag_conformance(conn)
    assert out["cycle_found"] is False, (
        f"derives_from + supersedes to the same predecessor must never "
        f"report a cycle -- got {out}"
    )
    assert out["cycle_path"] == []


def test_cites_and_supersedes_same_target_not_flagged():
    """#2943 round 2 sibling (found live on a second, independent fleet
    graph): the identical collapse via `cites` instead of `derives_from` --
    confirms the fix is relation-agnostic, not specific to one non-history
    relation."""
    conn = _new_conn()
    _add_node(conn, "newest", is_current=1)
    _add_node(conn, "oldest", is_current=0, superseded_by="newest")
    _add_edge(conn, "newest", "oldest", "cites")
    _add_edge(conn, "newest", "oldest", "supersedes")
    conn.commit()

    out = dagconf.check_dag_conformance(conn)
    assert out["cycle_found"] is False, (
        f"cites + supersedes to the same predecessor must never report a "
        f"cycle -- got {out}"
    )
    assert out["cycle_path"] == []


def test_genuine_cycle_among_supersedes_edges_still_caught():
    """#2943 control: the fix must not create a blind spot. A genuine
    malformed 2-cycle purely among supersedes edges -- P and Q both
    is_current=1, P supersedes Q AND Q supersedes P -- is an actually
    impossible/invariant-violating state and must still be caught. Since
    both nodes are already current, resolution is the identity for both
    and the raw 2-cycle is found unchanged."""
    conn = _new_conn()
    _add_node(conn, "p", is_current=1)
    _add_node(conn, "q", is_current=1)
    _add_edge(conn, "p", "q", "supersedes")
    _add_edge(conn, "q", "p", "supersedes")
    conn.commit()

    out = dagconf.check_dag_conformance(conn)
    assert out["cycle_found"] is True, (
        f"a genuine 2-cycle purely among supersedes edges must still be "
        f"caught -- got {out}"
    )
    assert {"p", "q"}.issubset(set(out["cycle_path"]))


def test_genuine_raw_self_edge_still_caught():
    """#2943 round 3 (a colleague's live-graph review, real catch): round 2's
    blanket "drop any resolved self-loop" rule was too broad -- it also drops
    a GENUINE one-node cycle, a literal raw self-edge already in the table
    (source_id == target_id before any resolution happens at all). That is
    the simplest real DAG violation there is, and it must still be reported.

    single_node cites itself directly -- no supersedes/resolution involved
    whatsoever, is_current=1 throughout. Distinguishing test from the
    manufactured-self-loop cases above: here the RAW endpoints already
    equalled each other before resolution, so this is not an artifact of
    resolving a superseded target -- it's a genuine self-reference."""
    conn = _new_conn()
    _add_node(conn, "single_node", is_current=1)
    _add_edge(conn, "single_node", "single_node", "cites")
    conn.commit()

    out = dagconf.check_dag_conformance(conn)
    assert out["cycle_found"] is True, (
        f"a genuine raw self-edge must still be reported as a cycle -- got "
        f"{out}"
    )
    assert "single_node" in out["cycle_path"]


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
