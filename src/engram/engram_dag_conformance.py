"""engram_dag_conformance — #2357: the real-acyclicity conformance check.

Split out of engram_conformance.py deliberately, not folded into it:
engram_conformance.py's own docstring names the CB/NCB partition as
"THE SINGLE PROPERTY THIS MODULE EXISTS TO PROTECT" (#2300) — a DAG-cycle
check over an unrelated relation set is a different property with a
different owner, and belongs in its own module for the same reason #2300's
checker doesn't also check CASCADE_RELATIONS taint propagation.

THE PROPERTY THIS MODULE PROTECTS: no live edge among the relations flagged
``dag: True`` in ``core.EDGE_CLASSIFICATIONS`` (``core.DAG_RELATIONS``) forms
a cycle when walked over ``is_current = 1`` nodes and edges.

Why this needs a real walk, not the timestamp comparison every other
dag-flag consumer uses (``_supersede_impl``, ``_add_edge_impl``'s generic
guard, the startup consistency scan): all three of those check creation-order
(now ``temporal_causal``), which was a sound acyclicity PROXY only as long as
every dag-bearing edge also pointed backwards in creation time. The
``acknowledged_derives_from`` relation (#2013's cascade-frontier machine)
breaks that correlation on purpose — it points FORWARD in time by
construction, so a timestamp check cannot protect it, but a real cycle
still needs catching (see its EDGE_CLASSIFICATIONS entry for the concrete
construction: base -> P, X derives_from P, P-prime derives_from X, supersede
P -> P-prime, acknowledge X closes X -> P-prime -> X).

Two placements, per the #2357 design (Lei/Borges, 2026-08-29): this module is
the FULL-GRAPH BATCH placement (conformance/health, run periodically or
on-demand). The TARGETED "would this specific edge close a cycle" check at
edge-write time is a separate, deliberately deferred placement — it depends
on a reachability walker that exists only on an unmerged branch (#2349); see
that issue for the write-time guard. This module does not need it and does
not wait for it.

CURRENT-ONLY, same convention as engram_conformance.py (Lei, 2026-08-28
ruling on #2300 Defect 2): the walk considers only ``is_current = 1`` nodes
and edges — a cycle that exists solely among superseded/retracted history is
not a live hazard, and the remediation channel (supersede/retract) is how a
real violation would be fixed, so counting non-current state could never let
the metric register progress.
"""

from __future__ import annotations

import sqlite3

import engram_core as core


def _live_dag_edges(conn: sqlite3.Connection) -> "list[tuple[str, str]]":
    """(source_id, target_id) for every live edge on a `dag: True` relation.

    Derived from ``core.DAG_RELATIONS`` at call time, never hand-enumerated —
    same discipline as engram_conformance.py's cb/ncb/cascade derivations.
    """
    relations = sorted(core.DAG_RELATIONS)
    if not relations:
        return []
    placeholders = ",".join("?" * len(relations))
    rows = conn.execute(
        f"""SELECT e.source_id AS sid, e.target_id AS tid
            FROM edges e
            JOIN nodes s ON e.source_id = s.id
            JOIN nodes t ON e.target_id = t.id
            WHERE e.relation IN ({placeholders})
              AND s.is_current = 1 AND t.is_current = 1""",
        tuple(relations),
    ).fetchall()
    return [(r["sid"], r["tid"]) for r in rows]


def _find_cycle(edges: "list[tuple[str, str]]") -> "list[str] | None":
    """Standard three-color DFS cycle detection over a directed edge list.

    Returns the cycle as a list of node ids (first == last, closing the
    loop) if one exists, else None. No depth budget by design — a silent
    truncation here would make this exact check blind to the cycle it
    exists to catch (see the shared cascade walker's own 4096-node budget,
    which is fine for a display/read path and unacceptable for a guard).
    Graphs large enough to make unbounded DFS a real cost are a scaling
    problem for a future iteration, not a correctness one to trade away now.

    Iterative (explicit stack), not recursive — measured, not assumed: a
    naive recursive version raises RecursionError on a synthetic 2000-node
    linear chain (Python's default recursion limit is 1000; this project's
    own live graph's deepest chain is nowhere near that today, but "no depth
    budget by design" has to mean an iterative walk, not a recursive one
    that silently inherits Python's own budget instead — the failure mode
    an unhandled exception here produces (crashing the entire diagnostics
    call, per its caller in engram_stats.py, not just this one metric) is
    exactly what "unacceptable for a guard" was arguing against (reviewer-
    fairy round 1, PR #2360).
    """
    adjacency: dict[str, list[str]] = {}
    for src, tgt in edges:
        adjacency.setdefault(src, []).append(tgt)

    WHITE, GRAY, BLACK = 0, 1, 2
    color: dict[str, int] = {}
    _EXHAUSTED = object()

    for start in list(adjacency.keys()):
        if color.get(start, WHITE) != WHITE:
            continue
        # Explicit stack of (node, live iterator over its neighbors) frames,
        # paired with `path` (the current root-to-frontier chain) so a
        # closing edge can slice out exactly the cycle, same as the
        # recursive version's call-stack + path list did.
        stack: list[tuple[str, "object"]] = [(start, iter(adjacency.get(start, ())))]
        path: list[str] = [start]
        color[start] = GRAY
        while stack:
            node, neighbors = stack[-1]
            neighbor = next(neighbors, _EXHAUSTED)
            if neighbor is _EXHAUSTED:
                stack.pop()
                path.pop()
                color[node] = BLACK
                continue
            state = color.get(neighbor, WHITE)
            if state == WHITE:
                color[neighbor] = GRAY
                path.append(neighbor)
                stack.append((neighbor, iter(adjacency.get(neighbor, ()))))
            elif state == GRAY:
                # Closing edge found — extract the cycle from `path`.
                idx = path.index(neighbor)
                return path[idx:] + [neighbor]
            # BLACK: already fully explored, no-op — continue this frame's
            # remaining neighbors on the next while-loop pass.
    return None


def check_dag_conformance(conn: sqlite3.Connection) -> dict:
    """Read-only. Never writes, never remediates, never migrates.

    Runs a real edge-walk cycle detector over every live ``dag: True``
    relation and returns:

      - ``cycle_found``: bool.
      - ``cycle_path``: the cycle's node ids (first == last) if found, else
        an empty list. Not capped — a cycle among dag-bearing relations is
        rare enough by design that the full path is always useful context,
        unlike the high-volume categories elsewhere in the conformance
        surface.
      - ``dag_relation_count``: how many relations were walked, so a caller
        can tell "clean" from "walked nothing" (e.g. if DAG_RELATIONS were
        ever accidentally emptied).

    Mirrors check_partition_conformance's shape (engram_conformance.py) —
    zero-or-investigate, no severity, no scoring, no "acknowledged" state.
    A cycle among dag-bearing relations is an impossible state, not a
    degradation, for the same reason a CB/NCB partition violation is.
    """
    edges = _live_dag_edges(conn)
    cycle = _find_cycle(edges)
    return {
        "cycle_found": cycle is not None,
        "cycle_path": cycle or [],
        "dag_relation_count": len(core.DAG_RELATIONS),
    }
