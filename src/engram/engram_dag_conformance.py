"""engram_dag_conformance — #2357: the real-acyclicity conformance check.

Split out of engram_conformance.py deliberately, not folded into it:
engram_conformance.py's own docstring names the CB/NCB partition as
"THE SINGLE PROPERTY THIS MODULE EXISTS TO PROTECT" (#2300) — a DAG-cycle
check over an unrelated relation set is a different property with a
different owner, and belongs in its own module for the same reason #2300's
checker doesn't also check CASCADE_RELATIONS taint propagation.

THE PROPERTY THIS MODULE PROTECTS: no CURRENT edge among the relations of one
acyclicity FAMILY (``dag_family`` in ``core.EDGE_CLASSIFICATIONS``, #3210:
``grounding`` and ``abstraction``; the union of both is ``core.DAG_RELATIONS``)
forms a cycle. An edge is current iff BOTH of its RAW endpoints are live
(``core._CURRENT_EDGE_PREDICATE``, built from the one node-liveness predicate).

#3542 (Lei, 2026-10-21) CURRENT-ONLY, with no resolution: the DAG requirement
ranges over current nodes and current edges only; a non-current node and its
edges are provenance, not live structure. Until #3542 this audit resolved each
endpoint through the supersedes chain to its current successor (#2813), and so
reported "frozen-closed" cycles -- cycles that exist only because a superseded
node's edges were re-attributed to its successor, which no current edge forms
and which no remediation can clear (the history is immutable). That resolved
walk is kept as ``_resolved_dag_edges`` and feeds ONE informational field,
``frozen_closed_components``; it never touches the gate (``cycle_count`` /
``cycle_found`` / ``cycles``) or any health verdict. The older text below about
resolving endpoints (``_resolve_current``) describes that retained informational
walk; the gate itself no longer resolves.

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

History of the #2813 resolve-through-supersedes mechanism (superseded by
#3542 for the gate, kept for the informational field): an earlier CURRENT-ONLY
reading (Lei, 2026-08-28, #2300 Defect 2) filtered raw ``is_current = 1``; #2813
argued that inverts for a DAG cycle (superseding a node inside a cycle is not
the remediation, because the cycle survives through its successor) and
resolved endpoints instead. Lei's 2026-10-21 ruling settles it the other way:
a cycle that survives only through a superseded node is provenance.

WHAT THE CYCLE REPORT IS, EXACTLY: one cycle per DFS back edge in each family
(at least one per cyclic component). It is NOT an enumeration of all
elementary cycles, ``cycle_count`` depends on DFS order, and fixing the
reported cycles and re-running may reveal more. The ``cycles`` list is capped
per family (``MAX_CYCLES_PER_FAMILY``); ``cycle_count`` is the full number.
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


def _live_dag_edges(
    conn: sqlite3.Connection, relations: "frozenset[str] | None" = None,
    stats: "dict | None" = None,
) -> "list[tuple[str, str]]":
    """(source_id, target_id) for every CURRENT edge on a DAG-family relation
    (all of ``core.DAG_RELATIONS``, or just ``relations`` when given -- #3210
    walks one family at a time). THE GATE'S EDGE SET (#3542).

    Read through ``core.dag_edge_set(live_only=True)`` -- the one definition of
    which edges count: both raw endpoints live, no resolution through supersede
    chains, no re-attribution of a superseded endpoint's edges to its successor.
    Edges with a non-current endpoint are provenance and not counted
    (``stats["dropped_non_current"]``). A genuine raw self-edge on a current node
    (#2943) is kept: it is a real one-node cycle.
    """
    live = core.dag_edge_set(conn, relations, live_only=True)
    edges = sorted({(s, t) for s, t, _r in live})
    if stats is not None:
        # #3210 rider: the population the walk ranged over, so a clean result
        # says what was checked rather than a bare zero.
        raw = core.dag_edge_set(conn, relations, live_only=False)
        stats["raw_edges"] = len(raw)
        stats["edges"] = len(edges)
        stats["nodes"] = len({n for e in edges for n in e})
        stats["dropped_non_current"] = len(raw) - len(live)
    return edges


def _resolved_dag_edges(
    conn: sqlite3.Connection, relations: "frozenset[str] | None" = None,
) -> "list[tuple[str, str]]":
    """INFORMATIONAL ONLY (#3542): the pre-#3542 resolved graph. Never the gate.

    Starts from ``core.dag_edge_set(live_only=False)`` -- the same function the
    gate reads, filter off -- then puts both endpoints of each edge through the
    supersedes chain (``_resolve_current``) to their CURRENT successor (#2813).
    An edge is dropped only when a chain dead-ends with no live node to attribute
    it to. #2943: an edge whose RESOLVED endpoints collapse to one node is
    dropped only when its RAW endpoints did not already -- resolution
    manufactured that self-loop (a node that both ``derives_from`` and
    ``supersedes`` its predecessor); a genuine raw self-edge is kept.
    """
    _resolved: "dict[str, str | None]" = {}

    def _res(nid: str) -> "str | None":
        if nid not in _resolved:
            _resolved[nid] = _resolve_current(conn, nid)
        return _resolved[nid]

    edges: "set[tuple[str, str]]" = set()
    for raw_sid, raw_tid, _rel in core.dag_edge_set(conn, relations, live_only=False):
        r_sid, r_tid = _res(raw_sid), _res(raw_tid)
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


def _find_all_cycles(edges: "list[tuple[str, str]]") -> "list[list[str]]":
    """The cycles a three-color DFS finds in ``edges`` -- one per back edge.

    ``_find_cycle`` stops at the first cycle; this keeps going, so two
    disjoint cycles are BOTH reported (#3210). It is a back edge detector,
    not an elementary-cycle enumerator: each cycle returned is real (first ==
    last, closing the loop) and every strongly connected component with a
    cycle contributes at least one, but a component with many interlocking
    cycles reports one per back edge the DFS meets, not all of them (a
    complete 3-node digraph has 5 elementary cycles; this returns fewer). The
    count depends on DFS order, and repairing the reported cycles and
    re-running may reveal more.

    Iterative (explicit stack), for the same measured reason as
    ``_find_cycle`` (no recursion limit on a long chain). Deterministic: start
    nodes and neighbours are visited in sorted order.
    """
    adjacency: dict[str, list[str]] = {}
    for src, tgt in edges:
        adjacency.setdefault(src, []).append(tgt)
    for k in adjacency:
        adjacency[k] = sorted(set(adjacency[k]))

    WHITE, GRAY, BLACK = 0, 1, 2
    color: dict[str, int] = {}
    cycles: list[list[str]] = []
    _EXHAUSTED = object()

    for start in sorted(adjacency):
        if color.get(start, WHITE) != WHITE:
            continue
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
                idx = path.index(neighbor)
                cycles.append(path[idx:] + [neighbor])
    return cycles


def _cyclic_sccs(edges: "list[tuple[str, str]]") -> "list[set[str]]":
    """Strongly connected components of ``edges`` that contain a cycle (size
    >= 2, or a single node with a self-edge). Iterative Tarjan (no recursion
    limit on a long chain), deterministic order."""
    adjacency: "dict[str, list[str]]" = {}
    self_loops: "set[str]" = set()
    for src, tgt in edges:
        adjacency.setdefault(src, []).append(tgt)
        adjacency.setdefault(tgt, [])
        if src == tgt:
            self_loops.add(src)
    for k in adjacency:
        adjacency[k] = sorted(set(adjacency[k]))
    index: "dict[str, int]" = {}
    low: "dict[str, int]" = {}
    on_stack: "set[str]" = set()
    stack: "list[str]" = []
    out: "list[set[str]]" = []
    counter = 0
    for root in sorted(adjacency):
        if root in index:
            continue
        work = [(root, iter(adjacency[root]))]
        index[root] = low[root] = counter
        counter += 1
        stack.append(root)
        on_stack.add(root)
        while work:
            node, it = work[-1]
            advanced = False
            for nxt in it:
                if nxt not in index:
                    index[nxt] = low[nxt] = counter
                    counter += 1
                    stack.append(nxt)
                    on_stack.add(nxt)
                    work.append((nxt, iter(adjacency[nxt])))
                    advanced = True
                    break
                if nxt in on_stack:
                    low[node] = min(low[node], index[nxt])
            if advanced:
                continue
            work.pop()
            if work:
                parent = work[-1][0]
                low[parent] = min(low[parent], low[node])
            if low[node] == index[node]:
                comp: "set[str]" = set()
                while True:
                    w = stack.pop()
                    on_stack.discard(w)
                    comp.add(w)
                    if w == node:
                        break
                if len(comp) > 1 or node in self_loops:
                    out.append(comp)
    return out


def _frozen_closed_components(
    current_edges: "list[tuple[str, str]]",
    resolved_edges: "list[tuple[str, str]]",
) -> "list[list[str]]":
    """INFORMATIONAL (#3542): cycles of the RESOLVED graph that the current-only
    gate does not see. One entry per cyclic strongly connected component of
    ``resolved_edges`` whose sub-graph of CURRENT edges (restricted to that
    component's nodes) is acyclic -- the component closes only through
    re-attributed edges of superseded nodes. A component that already contains a
    current cycle belongs to the gate and is not listed. Each entry is an
    example cycle path (first == last) through the component."""
    current = set(current_edges)
    out: "list[list[str]]" = []
    for comp in _cyclic_sccs(resolved_edges):
        inside_current = [e for e in current if e[0] in comp and e[1] in comp]
        if _find_all_cycles(inside_current):
            continue
        inside_resolved = [e for e in resolved_edges
                           if e[0] in comp and e[1] in comp]
        cyc = _find_cycle(sorted(inside_resolved))
        out.append(cyc or sorted(comp))
    return out


# Output bound (#3210 review): the ``cycles`` list is cut at this many entries
# per family; ``cycle_count`` stays the full number.
MAX_CYCLES_PER_FAMILY = 20


def check_dag_conformance(conn: sqlite3.Connection) -> dict:
    """Read-only. Never writes, never remediates, never migrates.

    #3210: walks each acyclicity FAMILY (``core.DAG_FAMILIES``) separately --
    a cycle is only meaningful within a family (``generalizes`` runs general ->
    specific, grounding runs dependent -> premise, so a deduction-shaped pair
    is a legal union 2-cycle) -- and reports one cycle per DFS back edge in
    each family, not only the first. That is NOT an enumeration of all
    elementary cycles: every cyclic component yields at least one, a component
    with interlocking cycles yields one per back edge, ``cycle_count`` depends
    on DFS order, and fixing the reported cycles and re-running may reveal
    more. Returns:

      - ``cycles``: list of ``{"family": str, "path": [node ids]}``, one per
        back edge found (path first == last), families in
        ``core.DAG_FAMILIES`` order. Capped at ``MAX_CYCLES_PER_FAMILY`` (20)
        entries per family; ``cycle_count`` is never capped.
      - ``cycle_count``: the full number of back-edge cycles found (before
        the cap; not an elementary-cycle count).
      - ``cycles_truncated``: True if any family's ``cycles`` list was cut at
        the cap (each family dict carries its own ``cycles_truncated``).
      - ``cycle_found``: bool -- any cycle at all. (Legacy key, kept for
        engram_diagnose consumers.)
      - ``cycle_path``: the FIRST cycle's node ids across families, else an
        empty list. (Legacy key.)
      - ``families``: per-family measurement population (#3210 rider): for
        each family ``relations`` / ``relation_count`` walked, ``nodes`` and
        ``edges`` the walk ranged over (CURRENT edges only, #3542),
        ``raw_edges`` before, ``dropped_non_current`` (edges with a
        non-current endpoint: provenance, not counted), ``cycle_count`` and a
        one-line ``summary`` (e.g. "grounding: 8 relations, N nodes, M edges, 0
        cycles") so a clean result states what was checked; plus the
        informational ``frozen_closed_components`` for that family.
      - ``frozen_closed_components`` (#3542, INFORMATIONAL, never a verdict):
        the number of cyclic components that appear in the pre-#3542 RESOLVED
        graph (superseded endpoints re-attributed to their successors) but NOT
        among current edges, so they are not on the gate; and
        ``frozen_closed_example_path`` the first such component's cycle path
        (empty list when none). Reported so the old finding is never silently
        dropped; it does not move ``cycle_found`` / ``cycle_count`` / ``cycles``
        or any health verdict.
      - ``dag_relation_count``: how many relations were walked, so a caller
        can tell "clean" from "walked nothing" (e.g. if DAG_RELATIONS were
        ever accidentally emptied). (Legacy key.)

    Mirrors check_partition_conformance's shape (engram_conformance.py) --
    zero-or-investigate, no severity, no scoring, no "acknowledged" state.
    A cycle within a family is an impossible state, not a degradation, for
    the same reason a CB/NCB partition violation is.
    """
    cycles: list[dict] = []
    families: dict[str, dict] = {}
    frozen_paths: list[list[str]] = []
    for family in core.DAG_FAMILIES:
        # Intersect with core.DAG_RELATIONS: the audit's walked set stays
        # bounded by that constant (tests monkeypatch it to prove the walk
        # really reads it), and the family split can only narrow it.
        walked = core.dag_family_relations(family) & core.DAG_RELATIONS
        stats: dict = {}
        edges = _live_dag_edges(conn, walked, stats)
        found = _find_all_cycles(edges)
        frozen = _frozen_closed_components(
            edges, _resolved_dag_edges(conn, walked))
        frozen_paths.extend(frozen)
        for path in found[:MAX_CYCLES_PER_FAMILY]:
            cycles.append({"family": family, "path": path})
        families[family] = {
            "relations": sorted(walked),
            "relation_count": len(walked),
            "nodes": stats.get("nodes", 0),
            "edges": stats.get("edges", 0),
            "raw_edges": stats.get("raw_edges", 0),
            "dropped_non_current": stats.get("dropped_non_current", 0),
            "cycle_count": len(found),
            "cycles_truncated": len(found) > MAX_CYCLES_PER_FAMILY,
            "frozen_closed_components": len(frozen),
        }
        families[family]["summary"] = (
            f"{family}: {len(walked)} relations, {families[family]['nodes']} "
            f"nodes, {families[family]['edges']} edges, {len(found)} cycles"
        )
    return {
        "cycle_found": bool(cycles),
        "cycle_path": cycles[0]["path"] if cycles else [],
        "cycles": cycles,
        "cycle_count": sum(f["cycle_count"] for f in families.values()),
        "cycles_truncated": any(
            f["cycles_truncated"] for f in families.values()),
        "families": families,
        "frozen_closed_components": len(frozen_paths),
        "frozen_closed_example_path": frozen_paths[0] if frozen_paths else [],
        "dag_relation_count": len(core.DAG_RELATIONS),
    }
