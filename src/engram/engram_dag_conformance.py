"""engram_dag_conformance — #2357: the real-acyclicity conformance check.

Split out of engram_conformance.py deliberately, not folded into it:
engram_conformance.py's own docstring names the CB/NCB partition as
"THE SINGLE PROPERTY THIS MODULE EXISTS TO PROTECT" (#2300) — a DAG-cycle
check over an unrelated relation set is a different property with a
different owner, and belongs in its own module for the same reason #2300's
checker doesn't also check CASCADE_RELATIONS taint propagation.

THE PROPERTY THIS MODULE PROTECTS: no live edge among the relations flagged
``dag: True`` in ``core.EDGE_CLASSIFICATIONS`` (``core.DAG_RELATIONS``) forms
a cycle when walked over the graph's CURRENT nodes — each edge's endpoints
resolved through the supersedes chain to their current node (#2813), not
merely nodes/edges that happen to already carry ``is_current = 1`` raw.

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

CURRENT-ONLY (Lei, 2026-08-28 ruling on #2300 Defect 2) — but the MECHANISM
here diverges from engram_conformance.py's own CURRENT-ONLY invariants as of
#2813, and it is a DERIVATION from the same ruling, not an exception to it.
``_invariant_1`` there states its reason plainly: "both endpoints filtered to
``is_current = 1``. The remediation for a violation IS superseding or
retracting the offending node, so a non-current endpoint must not keep
counting — otherwise the fix could never move this diagnostic." That reason
is sound for a partition invariant, where superseding the offending node
genuinely IS the fix.

It inverts for a DAG cycle. Superseding a node inside a cycle is NOT the
remediation — the cycle survives through the node's current successor, so
dropping the superseded endpoint at the SQL level does not let a real
violation clear; it makes the audit blind to a cycle that only appears once
you resolve that endpoint (the exact bug #2813 fixes; write-time twin:
``core._generalizes_cycle_path``, found in #2812's review). So this module
resolves each edge's endpoints through the supersedes chain to their current
successor before walking (``_live_dag_edges``'s ``_resolve_current`` helper)
rather than filtering raw ``is_current = 1`` — an edge is only dropped when
that chain dead-ends with no live node to attribute it to (e.g. a retracted
node with no replacement), never merely because one endpoint is superseded.
Same ruling, same CURRENT-ONLY policy, applied to a case where the stated
reason for the mechanism points the opposite way — don't assume the two
modules resolve edges identically just because both are "CURRENT-ONLY."
"""

from __future__ import annotations

import sqlite3

import engram_core as core


# #2943, round 2 (found live on two fleet graphs): the original fix exempted
# only supersedes/retracts edges themselves from resolution, but the phantom
# self-loop is NOT specific to those two relations — it fires for ANY dag
# edge whose target has been superseded specifically BY the edge's own
# source, regardless of what relation the edge itself is on. Concretely: a
# node that supersedes another commonly ALSO carries a plain `derives_from`
# (or `cites`) edge to the very claim it replaced — e.g. node N derives_from
# (or cites) node M, and N ALSO supersedes M. The derives_from/cites edge is
# not exempt under the relation-based carve-out, so it still gets resolved:
# resolve(N) = N (current), resolve(M) = N (M's own successor, via the SAME
# supersedes fact) -> (N, N), the identical phantom. Confirmed live on two
# independent graphs with this exact shape, one via derives_from+supersedes,
# one via cites+supersedes.
#
# The correct invariant, refined once more (a colleague's live-graph review
# caught the first cut of this too broad): dropping EVERY resolved self-loop
# unconditionally also drops a GENUINE one-node cycle — a literal raw
# self-edge already in the table (e.g. some_relation edge whose RAW source_id
# equals its RAW target_id) is a real, if degenerate, DAG violation and must
# still be reported, not silently swallowed alongside the resolution
# artifact. The distinguishing test is whether the RAW endpoints already
# equalled each other BEFORE resolution: if raw_sid == raw_tid, resolution
# didn't manufacture anything — this is a genuine self-loop, keep it. If
# raw_sid != raw_tid but resolution makes them equal, THAT is the artifact
# (an edge whose target was independently superseded by the edge's own
# source) — drop it. Dropping only the manufactured case can still never
# hide a genuine multi-node cycle: a self-loop edge (raw or resolved) adds no
# reachability to any node other than itself, so removing the ARTIFACT kind
# changes nothing about whether a real cycle elsewhere in the graph is
# found. No relation-based special-casing is needed at all — _HISTORY_
# RELATIONS is retired; every dag edge resolves through the chain uniformly,
# exactly as #2827/#2813 originally intended, and the raw-vs-resolved
# self-loop distinction (not a relation carve-out) is what separates the
# artifact from the real thing.


def _resolve_current(conn: sqlite3.Connection, node_id: str) -> "str | None":
    """Resolve ``node_id`` to its CURRENT version via the supersedes chain.

    Returns ``node_id`` itself if it is already current, the current
    successor (via ``core._current_successor``) if not, or None if
    ``node_id`` doesn't exist or its chain dead-ends without reaching a
    current node (e.g. a retracted node with no replacement).

    #2813: the batch-walk twin of the write-time guard fixed for the
    ``generalizes`` relation in #2812's review (``core._generalizes_cycle_path``
    / ``core._resolve_to_current``) — same shape, same reason: an edge
    touching a superseded endpoint should attribute to that endpoint's
    CURRENT successor, not vanish from the walk.
    """
    row = conn.execute(
        "SELECT is_current FROM nodes WHERE id = ?", (node_id,)
    ).fetchone()
    if not row:
        return None
    if row["is_current"] == 1:
        return node_id
    return core._current_successor(conn, node_id)


def _live_dag_edges(conn: sqlite3.Connection) -> "list[tuple[str, str]]":
    """(source_id, target_id) for every live edge on a `dag: True` relation,
    with both endpoints resolved through the supersedes chain to their
    CURRENT node.

    Derived from ``core.DAG_RELATIONS`` at call time, never hand-enumerated —
    same discipline as engram_conformance.py's cb/ncb/cascade derivations.

    #2813: an earlier version filtered ``s.is_current = 1 AND t.is_current =
    1`` at the SQL level instead — any edge touching a superseded endpoint
    vanished from the walk entirely rather than being resolved onto that
    endpoint's current successor, so the batch cycle audit was blind to a
    cycle that only appears once you resolve a superseded endpoint to its
    current replacement. Now: select every edge on a dag relation with no
    currency filter at the SQL level, then resolve each endpoint in Python
    via ``_resolve_current``; an edge is dropped only when a resolution
    dead-ends with no live node to attribute it to (e.g. a retracted node
    with no replacement) — the same "dead-ended chain" reasoning
    ``core._generalizes_cycle_path`` uses for its own resolve-then-walk.

    #2943 (round 3): an edge whose RESOLVED endpoints collapse to the same
    node is dropped ONLY when its RAW endpoints did not already — i.e. only
    when resolution itself manufactured the self-loop, never when a genuine
    raw self-edge already existed in the table. See the module-level comment
    above `_resolve_current`'s call sites for the full reasoning (a colleague's
    live-graph review caught round 2's blanket drop as too broad). This
    replaces the round-1 relation-based `_HISTORY_RELATIONS` carve-out
    entirely; every dag edge now resolves through the chain uniformly.
    """
    relations = sorted(core.DAG_RELATIONS)
    if not relations:
        return []
    placeholders = ",".join("?" * len(relations))
    rows = conn.execute(
        f"""SELECT source_id AS sid, target_id AS tid
            FROM edges
            WHERE relation IN ({placeholders})""",
        tuple(relations),
    ).fetchall()

    _resolved: "dict[str, str | None]" = {}

    def _res(nid: str) -> "str | None":
        if nid not in _resolved:
            _resolved[nid] = _resolve_current(conn, nid)
        return _resolved[nid]

    edges: "set[tuple[str, str]]" = set()
    for row in rows:
        raw_sid, raw_tid = row["sid"], row["tid"]
        r_sid = _res(raw_sid)
        r_tid = _res(raw_tid)
        if r_sid is None or r_tid is None:
            continue  # dead-ended chain: no live node to attribute the edge to
        if r_sid == r_tid and raw_sid != raw_tid:
            continue  # resolution MANUFACTURED this self-loop -- not a real cycle
        edges.add((r_sid, r_tid))
    return sorted(edges)


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
