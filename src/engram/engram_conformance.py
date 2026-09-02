"""engram_conformance — #2300: the CB/NCB red-flag conformance channel.

Five binary invariants over the claim-bearing / non-claim-bearing (CB/NCB)
node-type partition and the cascade-relation set (#2252). Each invariant is
zero-or-investigate — a partition violation is an impossible state, not a
degradation, so this module carries no severity, no scoring, and no
"acknowledged" state (#2300's own scope; do not add one — see the health-
score v2 semantics #2300 quotes: "Infra-bug class; flare at detection ...
supersede properly, no 'acknowledged' scoring state.").

THE SINGLE PROPERTY THIS MODULE EXISTS TO PROTECT: every set of node types
or edge relations it reasons about — ``cb``, ``ncb``, ``cascade``,
``cascade_ndc`` — is derived from the shipped constants in engram_core.py
(``CLAIM_BEARING_TYPES``, ``EDGE_CLASSIFICATIONS``, ``CASCADE_RELATIONS``) AT
CALL TIME, never hand-enumerated. This #2252 epic has produced seven
instances of a checker re-enumerating a set that already has a canonical,
derived home — three of them inside the fix for an earlier one. A node-type
name or an edge-relation name appearing as a string literal anywhere in this
module's invariant logic would be the eighth, and the worst-placed: this
module IS the conformance checker. If you are about to type one, stop —
derive it instead.

``ncb`` is deliberately {types actually present in the graph} - ``cb``, not
a hardcoded complement: a type added to the schema tomorrow lands in
``ncb`` with no edit here.

(The one deliberate exception is the literal node-type string ``"prediction"``
in invariant 4. Invariant 4 is inherently ABOUT the "prediction" node type —
it is not a CB/NCB-partition question at all, and there is no shipped
CLAIM_BEARING_TYPES-style constant naming just that one type to derive from.
engram_core.py's own ``prediction_pairing_exemption_sql`` helper and its
display-layer callers already hardcode the same literal for the same reason.
The RELATION invariant 4 pairs through (``predicted_by``) *is* derived, via
``core.PREDICTION_PAIRING_RELATIONS`` — only the node-type filter is a
literal.)

CURRENT-ONLY, nodes and edge endpoints consistently (Lei, 2026-08-28
13:44Z ruling on #2300 Defect 2). Every invariant that reasons about node
or edge-endpoint state filters to ``is_current = 1``. The reason is the
remediation channel, not any one graph's numbers: the official way to FIX
a partition violation IS to supersede or retract the violating node — if
non-current state stayed in the violation counts, the fix could never move
the diagnostic, and the surface would be useless for tracking progress.
The counting rule must align with the remediation channel or the metric
can never register progress.

Invariants 1 and 3 return the identical edge set on graphs measured so
far. This is expected, not duplication — do not delete either as
redundant. Invariant 1 flags any cascade edge with an NCB endpoint on
either side; invariant 3 flags a CB-sourced cascade edge with an NCB
target. They can only diverge on a cascade edge whose SOURCE is NCB,
and the coincidence is empirical, not structural: ``derives_from``
already admits an NCB source today via ``engram_add_trust_signal``
(engram_trust.py), which mints a ``derives_from`` edge from a
``trust_signal`` node — not claim-bearing. The pair separates the
moment either trigger fires: such an edge appearing in a graph, or a
relation with NCB sources (e.g. ``supported_by``, whose sources include
``cornerstone`` and ``prediction``) regaining the cascade flag. Do not
delete either invariant — it is written for that divergent day.

Read-only. ``check_partition_conformance`` never writes, never remediates,
never migrates — remediating what this module finds is Stage 3's job (#2252).
"""

from __future__ import annotations

import sqlite3

import engram_core as core

# Bound on the *displayed* id sample per invariant. The reported `count` and
# the `distinct_total` union always reflect the COMPLETE affected-id set —
# only the sample list handed back for human inspection is capped.
_SAMPLE_LIMIT = 10


def _placeholders(items) -> str:
    return ",".join("?" * len(items))


def _derive_cb_ncb(conn: sqlite3.Connection) -> "tuple[set, set]":
    """(cb, ncb). ``cb`` is core.CLAIM_BEARING_TYPES itself. ``ncb`` is
    {types actually present in the `nodes` table} - cb — computed fresh
    against the live graph on every call, never a hardcoded complement."""
    cb = set(core.CLAIM_BEARING_TYPES)
    present_types = {r[0] for r in conn.execute("SELECT DISTINCT type FROM nodes")}
    ncb = present_types - cb
    return cb, ncb


def _derive_cascade() -> "tuple[frozenset, frozenset]":
    """(cascade, cascade_ndc). ``cascade`` is core.CASCADE_RELATIONS itself
    (already derived from EDGE_CLASSIFICATIONS at import time in
    engram_core.py). ``cascade_ndc`` is the subset of cascade relations whose
    EDGE_CLASSIFICATIONS entry does NOT carry ``dag`` (real edge-walk
    acyclicity, #2357) — recomputed here from EDGE_CLASSIFICATIONS at call
    time (not cached), so a future edit to that declaration is picked up with
    no edit to this module.

    #2357 note: this checks ``dag``, not the (renamed) ``temporal_causal``
    flag. A cascade relation deliberately backwards in creation time (e.g.
    ``acknowledged_derives_from``) is exactly the case this module exists to
    NOT flag, as long as it's still dag-safe — that's the whole point of
    splitting the two properties apart."""
    cascade = core.CASCADE_RELATIONS
    non_dag = {r for r, c in core.EDGE_CLASSIFICATIONS.items() if not c["dag"]}
    return cascade, cascade & non_dag


def _report(entity_kind: str, ids) -> "tuple[dict, set]":
    """Build one invariant's report block plus its full id set (for the
    caller's union). `count` and the union always reflect the FULL id list;
    `sample_ids` is capped to _SAMPLE_LIMIT for display."""
    ids = list(ids)
    block = {
        "entity_kind": entity_kind,
        "count": len(ids),
        "sample_ids": ids[:_SAMPLE_LIMIT],
    }
    return block, set(ids)


def _invariant_1(conn: sqlite3.Connection, cascade: frozenset, ncb: set) -> "tuple[dict, set]":
    """1 — cascade-causing edge with an NCB endpoint (source OR target).

    CURRENT-ONLY (Lei, 2026-08-28): both endpoints filtered to
    ``is_current = 1``. The remediation for a violation IS superseding or
    retracting the offending node, so a non-current endpoint must not keep
    counting — otherwise the fix could never move this diagnostic.
    """
    if not cascade or not ncb:
        return _report("edge", [])
    c_sorted, n_sorted = sorted(cascade), sorted(ncb)
    rows = conn.execute(
        f"""SELECT e.id AS eid FROM edges e
            JOIN nodes s ON e.source_id = s.id
            JOIN nodes t ON e.target_id = t.id
            WHERE e.relation IN ({_placeholders(c_sorted)})
              AND s.is_current = 1 AND t.is_current = 1
              AND (s.type IN ({_placeholders(n_sorted)})
                   OR t.type IN ({_placeholders(n_sorted)}))""",
        (*c_sorted, *n_sorted, *n_sorted),
    ).fetchall()
    return _report("edge", (r["eid"] for r in rows))


def _invariant_2(conn: sqlite3.Connection, ncb: set) -> "tuple[dict, set]":
    """2 — NCB node carrying a non-NULL confidence.

    Node-granularity, deliberately NOT a GROUP BY row count — #2300's
    row-vs-node trap: on seat A, 2 GROUP BY rows (grouped by NCB type) cover
    146 distinct nodes. This queries node ids directly so `count` is always
    the node count.
    """
    if not ncb:
        return _report("node", [])
    n_sorted = sorted(ncb)
    rows = conn.execute(
        f"""SELECT id FROM nodes
            WHERE is_current = 1 AND confidence IS NOT NULL
              AND type IN ({_placeholders(n_sorted)})""",
        tuple(n_sorted),
    ).fetchall()
    return _report("node", (r["id"] for r in rows))


def _invariant_3(
    conn: sqlite3.Connection, cascade: frozenset, cb: set, ncb: set
) -> "tuple[dict, set]":
    """3 — CB node whose premise list contains an NCB node.

    #2300's own derivation snippet defines no separate "premise" relation
    set — `cascade`, `cascade_ndc`, `cb`, `ncb` are the only four sets it
    names. "Premise list" here is read as the same cascade-relation spine
    invariant 1 walks, restricted to edges whose SOURCE is CB (the node
    whose premise list is in question) and whose TARGET is NCB (the
    offending premise). Reported at EDGE granularity, deliberately matching
    invariant 1's entity kind: on seat A this is the exact same 43 edges as
    invariant 1 (#2300: "same 43 as #1"), and reporting the same id space is
    what makes that overlap checkable id-for-id in `distinct_total` rather
    than merely count-equal by coincidence.

    CURRENT-ONLY (Lei, 2026-08-28): both endpoints filtered to
    ``is_current = 1`` — same reasoning as invariant 1 (see its docstring).
    """
    if not cascade or not cb or not ncb:
        return _report("edge", [])
    c_sorted, cb_sorted, n_sorted = sorted(cascade), sorted(cb), sorted(ncb)
    rows = conn.execute(
        f"""SELECT e.id AS eid FROM edges e
            JOIN nodes s ON e.source_id = s.id
            JOIN nodes t ON e.target_id = t.id
            WHERE e.relation IN ({_placeholders(c_sorted)})
              AND s.is_current = 1 AND t.is_current = 1
              AND s.type IN ({_placeholders(cb_sorted)})
              AND t.type IN ({_placeholders(n_sorted)})""",
        (*c_sorted, *cb_sorted, *n_sorted),
    ).fetchall()
    return _report("edge", (r["eid"] for r in rows))


def _invariant_4(conn: sqlite3.Connection) -> "tuple[dict, set]":
    """4 — prediction with no live-basis paired predictive observation.

    The pairing RELATION is derived (core.PREDICTION_PAIRING_RELATIONS). The
    node-type filter ``'prediction'`` is the one deliberate literal in this
    module — see the module docstring for why no CB/NCB-style constant
    exists to derive it from.

    CURRENT-ONLY (Lei, 2026-08-28): the paired edge's TARGET (the
    observation_predictive) is filtered to ``is_current = 1``, matching
    invariants 1/3/5 — added round 4 (#2300 / PR #2326 round-2 blocker)
    after round 3's claim that this filter was redundant here turned out to
    be false for one of three transitions (below).

    A current prediction's basis can go stale via three transitions, and
    only two of them carry a structural guard:
      - fresh-mint-on-create: pairing always mints onto a fresh, current
        observation (engram_observation.py:1477-1509,
        ``linked_to_existing``).
      - retract-cascade: if a prediction's sole live basis is retracted,
        the prediction itself is retracted in the same cascade
        (engram_revision.py:1197-1304, #2281/#2292).
      - supersede: NOT covered by any structural guard. ``_supersede_impl``
        (engram_revision.py:654-970) checks only that the replacement's
        ``type`` matches the original's; nothing there re-validates or
        carries forward a ``predicted_by`` pairing. A replacement
        observation that omits ``predicted_event`` is legal (not a
        required field, engram_observation.py:1198-1209/1477) and mints no
        new pairing edge, leaving the prediction's sole ``predicted_by``
        edge pointed at the now-superseded original — the prediction stays
        ``is_current = 1`` with zero live-basis edges. Reproduced against
        the real production paths (``_add_observation_impl`` +
        ``_supersede_impl``, no test-only shortcuts) by the round-2
        reviewer-fairy on PR #2326; independently confirmed here by source
        read alone. No live graph inspected so far happens to contain a
        ``predicted_by`` edge to discriminate this case either way — that
        silence says nothing about whether the code permits it, which the
        source read and the reviewer's synthetic repro already settle.

    The filter below is therefore LOAD-BEARING for the supersede case, not
    merely consistency-for-its-own-sake with 1/3/5 — it is the only
    backstop for the one transition that has none.
    """
    p_sorted = sorted(core.PREDICTION_PAIRING_RELATIONS)
    rows = conn.execute(
        f"""SELECT n.id AS nid FROM nodes n
            WHERE n.type = 'prediction' AND n.is_current = 1
              AND NOT EXISTS (
                  SELECT 1 FROM edges e
                  JOIN nodes t ON e.target_id = t.id
                  WHERE e.source_id = n.id
                    AND e.relation IN ({_placeholders(p_sorted)})
                    AND t.is_current = 1
              )""",
        tuple(p_sorted),
    ).fetchall()
    return _report("node", (r["nid"] for r in rows))



def _invariant_5(conn: sqlite3.Connection, cascade_ndc: frozenset) -> "tuple[dict, set]":
    """5 — cascade-causing edge that is not dag-safe (#2357: checks ``dag``,
    real edge-walk acyclicity — not the renamed ``temporal_causal`` creation-
    order flag; see ``_derive_cascade``'s docstring for why the two diverge).

    Derives to the empty set today by construction (every relation in
    CASCADE_RELATIONS also has dag:True in EDGE_CLASSIFICATIONS) — do
    NOT special-case; the empty result falling out of the derivation is the
    evidence that the derivation works (#2300).

    CURRENT-ONLY (Lei, 2026-08-28): the endpoint join + ``is_current = 1``
    filter below is UNREACHABLE while ``cascade_ndc`` is empty — the
    ``if not cascade_ndc`` guard above short-circuits before the query runs.
    It is included anyway for consistency with invariants 1 and 3 ("nodes
    and edge endpoints consistently" is Lei's ruling, not "wherever it is
    reachable today"), so this invariant does not silently diverge from its
    siblings the day ``cascade_ndc`` stops being empty. Do not delete this
    join as dead code — it is dormant, not dead.
    """
    if not cascade_ndc:
        return _report("edge", [])
    d_sorted = sorted(cascade_ndc)
    rows = conn.execute(
        f"""SELECT e.id AS eid FROM edges e
            JOIN nodes s ON e.source_id = s.id
            JOIN nodes t ON e.target_id = t.id
            WHERE e.relation IN ({_placeholders(d_sorted)})
              AND s.is_current = 1 AND t.is_current = 1""",
        tuple(d_sorted),
    ).fetchall()
    return _report("edge", (r["eid"] for r in rows))


def check_partition_conformance(conn: sqlite3.Connection) -> dict:
    """Read-only. Never writes, never remediates, never migrates.

    Runs all five #2300 invariants against `conn` and returns a dict:

      - ``invariant_1_cascade_edge_ncb_endpoint`` .. ``invariant_5_cascade_not_dag_safe``:
        each ``{"entity_kind": "edge"|"node", "count": int, "sample_ids": [...]}``.
        `count` is always the full affected-entity count (never a capped
        sample count, never a GROUP BY row count — #2300's row-vs-node trap).
      - ``distinct_total``: the size of the UNION of every invariant's full
        affected-id set. Deliberately never a sum of the five counts —
        invariants 1 and 3 can (and on seat A, do) flag the identical edge
        set, and a naive sum would double-count (#2300's overlap trap).
      - ``flare``: True iff any invariant's count is non-zero.

    No severity, no scoring, no weighting, no "acknowledged" state — #2300
    excludes all of these by design; do not add one here.
    """
    cb, ncb = _derive_cb_ncb(conn)
    cascade, cascade_ndc = _derive_cascade()

    inv1, ids1 = _invariant_1(conn, cascade, ncb)
    inv2, ids2 = _invariant_2(conn, ncb)
    inv3, ids3 = _invariant_3(conn, cascade, cb, ncb)
    inv4, ids4 = _invariant_4(conn)
    inv5, ids5 = _invariant_5(conn, cascade_ndc)

    distinct_total = len(ids1 | ids2 | ids3 | ids4 | ids5)
    flare = any(block["count"] > 0 for block in (inv1, inv2, inv3, inv4, inv5))

    return {
        "invariant_1_cascade_edge_ncb_endpoint": inv1,
        "invariant_2_ncb_node_with_confidence": inv2,
        "invariant_3_cb_node_ncb_premise": inv3,
        "invariant_4_unpaired_prediction": inv4,
        "invariant_5_cascade_not_dag_safe": inv5,
        "distinct_total": distinct_total,
        "flare": flare,
    }
