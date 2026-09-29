"""engram_revision — family E: retract / supersede / resolve + cascade helpers.

Extracted from server.py as part of #872 wave 6.

Family E covers the transactional retraction-cascade invariant — the most
delicate wave.  Moved atomically per D6 ("never split _walk_cascade_downstream
from its callers"): all three cascade helpers and all three impls ship together
in one commit.

NOTE: module name ``engram_revision`` was chosen because the three obvious
alternatives collide with @mcp.tool function names in server.py:
``engram_retract``, ``engram_supersede``, and ``engram_resolve`` are all
@mcp.tool-decorated in server.py.  The wave convention therefore mandates the
alias form: ``import engram_revision as _revision_mod``.

Rolling promotions to engram_core (wave 5, already done): ``_create_derivation``
and its callee chain live in core and are consumed by ``_resolve_impl`` here via
``core._create_derivation``.  ``_count_live_exemplars`` is in core (wave 3) and
consumed by ``_detect_zero_support`` here via ``core._count_live_exemplars``.

``_retract_impl`` replacement path is purely relational, like
``_supersede_impl``: it accepts ``replacement_id``, naming a node the caller
already created via that node's own canonical creation tool
(``engram_add_observation`` for observations). ``_retract_impl`` never mints a
node itself — no callable injection across the module boundary is needed for
this (#2612 retired the earlier mint-inside-retraction path, which took this
role via an injected creator callable).

House rules (wave pattern):
  - Shared state ONLY via ``import engram_core as core`` + call-time ``core.X``.
  - Never import from server.py (acyclic: server → family → core).
  - Stateless beyond constants.
"""

from __future__ import annotations

import json
import sqlite3
import uuid

import engram_core as core

from engram_log_emitter import Emitter, emit_if_initialized

# alpha #2574 WS5: engram_telemetry_store.py ships at tier=convenience (WS1's
# own tiering judgment call -- src/build/packaging/tiers.json), while THIS
# file ships at tier=essential. A plain top-level import here would make an
# essential-tier module hard-depend on a convenience-tier one, breaking an
# Essential-only build (tests/test_tiers_manifest.py's
# test_import_dep_tier_consistency enforces exactly this). Guarded import +
# no-op stub keeps this module's own tool functions gating-nothing-T1
# (docs/2574-telemetry-design.md §8) even if the store ships without it.
try:
    from engram_telemetry_store import write_telemetry_record
except Exception:  # pragma: no cover -- import-failure fallback
    def write_telemetry_record(manifest_id, event_type, fields=None):
        return False


# #2252 Stage 1.0b: the cascade walk's walked-relation set, DERIVED from
# EDGE_CLASSIFICATIONS's `cascade` flag (engram_core.CASCADE_RELATIONS)
# instead of hardcoded as a SQL literal — flipping a relation's `cascade`
# flag now changes which edges the three walkers below traverse.
# sorted() gives a stable placeholder order; set iteration order must never
# leak into generated SQL.
_CASCADE_RELATIONS_SORTED = sorted(core.CASCADE_RELATIONS)
_CASCADE_RELATION_PLACEHOLDERS = ",".join("?" * len(_CASCADE_RELATIONS_SORTED))

# #2406: of CLAIM_BEARING_TYPES, the identity-layer subset that must surface
# to a human rather than be adjudicated by the machine or an agent —
# currently just 'axiom' (cornerstone/goal are NOT in CLAIM_BEARING_TYPES,
# so the cascade walk returns early on them and they can never carry a
# marker at all; see engram_core._is_claim_bearing). A named set rather than
# an inline == "axiom" check so a future type promoted to identity-layer
# status is one addition here, not a re-derivation at every call site.
_IDENTITY_LAYER_CLAIM_BEARING_TYPES = frozenset({"axiom"})


# ---------------------------------------------------------------------------
# Family-E constants (moved from server.py — originals deleted same-commit)
# ---------------------------------------------------------------------------

VALID_ERROR_TYPES = {
    "fabricated_quote",   # quoted_text not in evidence source
    "wrong_citation",     # claim doesn't follow from the quote
    "wrong_evidence",     # cited wrong source
    "hallucinated_claim", # claim not supported by any evidence
    "duplicate",          # same claim already existed
    "other",              # anything else
}

VALID_INITIATION_TYPES = frozenset({"user_prompted", "gate_prompted", "unprompted"})

# #2613: the retraction replacement contract. A closed set, deliberately with
# no free-text sibling (docs/2613-retraction-replacement-contract.md §4) — the
# set exists to be scanned ("which retractions had no replacement, and why?"),
# and a closed-set-plus-note splits the answer across two fields. Members are
# founded on distinct epistemic situations, not frequency data:
#   NONE_BECAUSE_NO_SUCCESSOR        — the claim was simply wrong; nothing
#                                       true stands in its place.
#   NONE_BECAUSE_DUPLICATE           — the surviving node already existed.
#   NONE_BECAUSE_SUPERSEDED_ELSEWHERE — a successor exists but is not this
#                                       retraction's to name.
#   NONE_BECAUSE_UNKNOWN             — no successor could be determined; the
#                                       escape hatch, and the only member that
#                                       requires replacement_absent_note (§4's
#                                       "naming the escape hatch costs a
#                                       sentence; naming a real category costs
#                                       nothing").
VALID_REPLACEMENT_ABSENT_REASONS = frozenset({
    "NONE_BECAUSE_NO_SUCCESSOR",
    "NONE_BECAUSE_DUPLICATE",
    "NONE_BECAUSE_SUPERSEDED_ELSEWHERE",
    "NONE_BECAUSE_UNKNOWN",
})

# Discipline hint returned when the threshold gate fires on engram_resolve.
# Surfaces in the result JSON as "discipline_hint" alongside "note" to guide
# callers away from chain-dilution and toward legitimate paths forward.
_THRESHOLD_GATE_DISCIPLINE_HINT = (
    "Do NOT refile another low-confidence resolution — that's chain dilution. "
    "Either (a) deepen supporting nodes' confidence first, "
    "(b) file a task tracking the structural blocker "
    "(e.g. needs primary source / needs human decision / needs empirical replication), "
    "or (c) accept partial-resolution as the calibrated state. "
    "Best practice: inspect-first before composing resolution."
)


# ---------------------------------------------------------------------------
# Cascade helpers
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Cascade markers (engram-alpha#2013 / #2009 slice 1)
# ---------------------------------------------------------------------------
#
# Replaces the old metadata.stale_by / metadata.tainted_by / metadata.
# stale_replacement fields (issue #231's dict-keyed form included) with rows
# in the cascade_markers table (schema: engram_core._init_db). One marker
# per (node_id, cascade_event); see the table's own docstring in
# engram_core.py for the full design rationale, the never-delete-resolved
# invariant, and the deliberately-unresolved orphan-marker residue.
#
# _add_stale_replacement is retired, not migrated: the concept it served
# (a downstream node's replacement, cached at marking time) doesn't survive
# the redesign in the same shape. A downstream node's OWN eventual
# replacement is decided when THAT node resolves — a later event, not
# knowable at the time the original cascade marks it. What a downstream
# node's *premise* was replaced by is now the immediate_cause/dead_premises
# derived read below, computed from the premise's own resolution rather
# than cached onto the downstream node at write time.


def _new_cascade_event() -> str:
    """Generate a fresh cascade_event id — one per retract/supersede call,
    shared by every marker that call's downstream walk writes. Not a graph
    node id (doesn't follow the `xx_NNNN` TYPE_PREFIX convention in
    engram_ids.py) — cascade_event never appears as a nodes.id.
    """
    return f"cev_{uuid.uuid4().hex[:16]}"


def _write_cascade_marker(conn, node_id: str, cascade_event: str,
                           root_node: str, root_op: str, now: str) -> None:
    """Write one pending cascade marker for node_id. Called once per
    downstream node visited by _walk_cascade_downstream's on_live_node
    callback (marking is idempotent per event because the walker's visited
    set already guarantees on_live_node fires at most once per node — see
    _walk_cascade_downstream's docstring). Also used to mark contradiction
    (ct_) nodes reached via the parallel contradicts-edge cascade, same
    cascade_event as the derivation-cascade call in the same revision.

    root_op must be 'retract' or 'supersede' — enforced by the table's own
    CHECK constraint; this function does not re-validate.
    """
    conn.execute(
        "INSERT OR IGNORE INTO cascade_markers "
        "(node_id, cascade_event, root_node, root_op, status, created_at) "
        "VALUES (?, ?, ?, ?, 'pending', ?)",
        (node_id, cascade_event, root_node, root_op, now),
    )


def _cascade_reaches(conn, ancestor_id: str, candidate_id: str) -> bool:
    """True iff `candidate_id` is DOWNSTREAM of `ancestor_id` over the cascade
    relations — i.e. candidate transitively derives from ancestor.

    §12.3's guardrail predicate. Walks upstream from `candidate_id` and asks
    whether `ancestor_id` is reachable, which is cheaper than enumerating a
    node's whole descendant set and is the direction the question is asked in
    ("may this replacement replace that node?").

    RANGES OVER THE DERIVED CASCADE SET, NEVER A RELATION LITERAL. §12.6, and
    here it is load-bearing rather than stylistic: §12.4's sufficiency proof
    rests on `marked ⇒ downstream via cascade relations`, and
    `acknowledged_derives_from` JOINS that set. A guard written against
    `derives_from` alone would keep passing while silently ceasing to be
    sufficient the moment a relation is added — the proof would become a
    coincidence and nothing would say so.

    NO DEPTH BUDGET, DELIBERATELY. The shared `core._walk_cascade_upstream`
    carries `max_depth=4096` as a total-node budget whose exhaustion is
    SILENT — a truncated walk returns a partial set indistinguishable from a
    complete one (engram-alpha#2339). That is tolerable for a derived READ and
    unacceptable for a GUARD: a partial ancestor set would simply miss
    `ancestor_id` and wave the cycle through, which is the one outcome this
    function exists to prevent. Termination does not need the budget — the
    visited set bounds the walk at one visit per node on a finite graph, and it
    does so even if a cycle already exists, which is precisely the state this
    guard is deployed into.
    """
    if ancestor_id == candidate_id:
        return True
    seen = {candidate_id}
    frontier = [candidate_id]
    while frontier:
        current = frontier.pop()
        rows = conn.execute(
            f"SELECT target_id FROM edges WHERE source_id = ? "
            f"AND relation IN ({_CASCADE_RELATION_PLACEHOLDERS})",
            (current, *_CASCADE_RELATIONS_SORTED),
        ).fetchall()
        for r in rows:
            t = r["target_id"]
            if t == ancestor_id:
                return True
            if t not in seen:
                seen.add(t)
                frontier.append(t)
    return False


def _affected_contradiction_ids(conn, node_id: str) -> list:
    """Contradiction nodes that have node_id as one of their contradicting
    sides (issue #229's parallel cascade).

    WHY THIS IS A SEPARATE WALK AND NOT PART OF THE DERIVATION CASCADE:
    contradictions are NON-claim-bearing, so _walk_cascade_downstream stops
    before ever reaching one (#2252 W0). They are only ever marked by this
    query, run against the edges of the node being operated on — which is
    what makes the covered-set proof in §6 NOT extend to them. That proof is
    about the derivation walk: everything downstream of a node already
    carries the root event's markers. A contradiction attached to a
    MID-CHAIN node was never marked by the root's cascade, because the root
    ran this query against its OWN edges, not the mid-chain node's.

    Extracted into one named helper because the same query was hand-copied
    into _supersede_impl and _retract_impl, and the resolution path now
    needs a third caller — three copies of a walk whose subtlety is exactly
    that it does not follow the rule the other walk follows.
    """
    rows = conn.execute(
        "SELECT source_id, target_id FROM edges "
        "WHERE relation = 'contradicts' AND (source_id = ? OR target_id = ?)",
        (node_id, node_id),
    ).fetchall()
    out = set()
    for ce in rows:
        ct_id = ce["source_id"] if ce["source_id"].startswith("ct_") else ce["target_id"]
        if not ct_id.startswith("ct_"):
            continue  # contradicts edges only attach contradictions to claim-bearers
        if ct_id == node_id:
            # SELF-MATCH. A contradiction node owns the contradicts edges, so
            # calling this with a ct_ id returns that node itself — which is
            # not "a contradiction that has node_id as a side", and directly
            # contradicts this function's own docstring. Harmless at today's
            # three call sites only because each re-fetches with is_current=1
            # and the ct is still current, i.e. it is defused by an unrelated
            # guard rather than by anything here. Excluded explicitly so the
            # helper means what it says on its own. (Round-2 reviewer.)
            continue
        out.add(ct_id)
    return sorted(out)


def _resolve_own_markers(conn, node_id: str, resolution_kind: str, now: str,
                          replacement_id: str = None) -> list:
    """Resolve every PENDING marker sitting on node_id, because node_id has
    itself just been superseded or retracted. Returns the cascade_event ids
    that were resolved (for the caller's response payload).

    WHY THIS EXISTS (#2324, spec revision 2026-08-28 16:29Z): with
    backfilled and live markers both hard-blocking, a marked node's ONLY
    resolution routes before the frontier verb ships are supersede and
    retract. If those two verbs did not clear the node's own markers, a
    marked node would have no route at all — the guard would keep refusing
    citations of a node that has since been properly re-derived, forever.

    The resolution_kind vocabulary is the schema's, and the mapping is the
    obvious one: superseding a marked node means the concern was addressed
    by a NEW claim ('superseded', replacement recorded); retracting it means
    the node was withdrawn rather than fixed ('retracted', no replacement).
    Neither is 'acknowledged' — that third kind is reserved for the frontier
    verb's "I looked, the drift does not change my claim" case, which by
    definition leaves the node itself unchanged and so cannot be reached
    from here.

    NOTE this makes an existing read live rather than merely adding one:
    _dead_premises filters on status='resolved' AND resolution_kind IN
    ('superseded','retracted'), and until this function existed NOTHING in
    the codebase wrote status='resolved' — so that predicate could never be
    satisfied and the function always returned []. A structurally vacuous
    production read, of the same shape as the three vacuous test guards
    #2009 already fixed.
    """
    pending = conn.execute(
        "SELECT cascade_event FROM cascade_markers "
        "WHERE node_id = ? AND status = 'pending'",
        (node_id,),
    ).fetchall()
    if not pending:
        return []
    conn.execute(
        "UPDATE cascade_markers "
        "SET status = 'resolved', resolution_kind = ?, "
        "    resolution_replacement_id = ?, resolved_at = ? "
        "WHERE node_id = ? AND status = 'pending'",
        (resolution_kind, replacement_id, now, node_id),
    )
    return [r["cascade_event"] for r in pending]


def _immediate_cause(conn, node_id: str, cascade_event: str) -> list:
    """Derived read (never stored): the direct premises of node_id that
    themselves carry a marker for cascade_event — i.e. which of node_id's
    OWN premises is the reason node_id was marked. One-hop by construction
    (Ariadne, correcting the relayed premises(X) phrasing to direct_premises
    (X) — the one-hop read is the whole point of the field it replaces,
    #1969's direct-premise marking refinement).

    Core primitives (_direct_premise_ids, table access) live in
    engram_core.py — this module may call core.X per the family→core
    dependency direction, never the reverse (house rule at module top).
    """
    premises = core._direct_premise_ids(conn, node_id)
    if not premises:
        return []
    placeholders = ",".join("?" for _ in premises)
    rows = conn.execute(
        f"SELECT node_id FROM cascade_markers "
        f"WHERE cascade_event = ? AND node_id IN ({placeholders}) "
        f"ORDER BY node_id",
        (cascade_event, *premises),
    ).fetchall()
    return [r["node_id"] for r in rows]


def _dead_premises(conn, node_id: str, cascade_event: str) -> list:
    """Derived read (never stored): which of node_id's direct premises
    GENUINELY moved under cascade_event — i.e. resolved with kind in
    {superseded, retracted}. Excludes 'acknowledged' deliberately: an
    acknowledged premise's marker resolved, but the premise itself never
    substantively changed, so it isn't dead. This is why the field cannot
    be written at marking time (Lei's diamond argument, via #2013 comment
    2026-08-14T21:23:31Z / Kepler's world crossing seq 1911): at marking
    time a merely-marked premise is not yet known to be dead at all.
    """
    premises = core._direct_premise_ids(conn, node_id)
    if not premises:
        return []
    placeholders = ",".join("?" for _ in premises)
    rows = conn.execute(
        f"SELECT node_id FROM cascade_markers "
        f"WHERE cascade_event = ? AND node_id IN ({placeholders}) "
        f"AND status = 'resolved' "
        f"AND resolution_kind IN ('superseded', 'retracted') "
        f"ORDER BY node_id",
        (cascade_event, *premises),
    ).fetchall()
    return [r["node_id"] for r in rows]


# ---------------------------------------------------------------------------
# engram_cascade_frontier (engram-alpha#2013 slice 3 — derived reads)
# ---------------------------------------------------------------------------
# READ-ONLY throughout: no INSERT/UPDATE/DELETE anywhere below. The frontier
# is a PURE FUNCTION of cascade_markers + edges + nodes state — no queue, no
# cursor, no persisted frontier state. Composes with core._walk_cascade_upstream
# (the shared premise-direction primitive) and the existing derived reads
# above (_immediate_cause, _dead_premises) — neither is modified.
#
# Frontier definition (spec, verbatim): a node is on the frontier iff it
# carries >=1 PENDING cascade marker AND none of its cascade-ancestors (full
# transitive closure via CASCADE_RELATIONS, not merely direct premises — the
# spec explicitly names _walk_cascade_upstream as the mechanism) currently
# carries a pending marker, for ANY cascade_event, against ANY root. Checked
# per-NODE across ALL of that node's own events at once (a node touched by
# two cascade events is frontier only once every pending direct-and-indirect
# ancestor, across both events, has resolved) — never per-event.

_CASCADE_FRONTIER_DEFAULT_LIMIT = 5
_CASCADE_FRONTIER_CLAIM_GIST_LEN = 160


def _cascade_ancestors(conn, node_id: str) -> set:
    """ancestors(node_id): the full transitive closure over CASCADE_RELATIONS
    via the shared core._walk_cascade_upstream primitive (full-ancestor mode:
    collect_if=descend_if=lambda row: True). INCLUDES node_id itself — this
    matches the walk's own established "ancestors(X)" terminology/precedent
    (tests/test_cascade_derived_sets.py::test_walk_cascade_upstream_full_ancestor_mode,
    whose full_mode assertion includes the start node). Callers that need
    PROPER ancestors (excluding node_id) subtract {node_id} themselves at
    the call site — kept out of this helper so there is exactly one place
    (here) deciding what "ancestors(X)" means, and every call site is
    explicit about whether it wants the self-inclusive or self-exclusive
    reading.
    """
    return core._walk_cascade_upstream(
        conn, node_id,
        collect_if=lambda row: True,
        descend_if=lambda row: True,
    )


def _cascade_blocked(conn, node_id: str, pending_node_ids: set) -> bool:
    """True iff node_id has a cascade-ancestor (excluding itself) that
    currently carries >=1 PENDING marker, for ANY cascade_event and ANY
    root_node — the frontier-blocking check. ``pending_node_ids`` is the
    pre-computed set of every node_id in the graph with >=1 pending marker
    (one query, shared across every candidate this call is made for) rather
    than a fresh query per candidate.

    Spec phrases the frontier rule two ways, and this implements the
    TRANSITIVE one: #2013 §3 defines it over the full cascade-ancestor
    closure ("pending markers all of whose cascade-ancestors are
    resolved"), while §10.2's corollary phrases it over direct premises only
    ("ALL pending direct premises resolved"). Do not narrow this to
    direct-premises-only — transitive is the CORRECT reading, not merely the
    cautious one, for the reason below.

    Under Invariant 6 (a marker resolves only while its node is itself on
    the frontier) the two readings pick out the identical set: by
    induction, a resolved direct premise was unblocked at the moment it
    resolved, so every one of ITS ancestors was already resolved too —
    direct-premises-clean implies ancestors-clean. But invariant 6 is a
    DESIGN RULE, not something any code enforces at write time:
    _resolve_own_markers clears every pending marker on node_id alone
    (``UPDATE cascade_markers ... WHERE node_id = ? AND status =
    'pending'``), with no check that node_id's own cascade-ancestors are
    clean first. Nothing in the write path stops a node's marker from
    resolving while an ancestor's marker is still pending.

    So divergence is not confined to a one-time migration — #2324's
    backfilled markers (synthesized from retired metadata rather than
    produced by a live cascade) are one instance of invariant 6 not
    holding, not the category. Ordinary out-of-order resolution through the
    live retract/supersede path produces it too: given a real chain
    R1 -> G -> P1 -> N (derives_from edges, no backfill), retract R1
    (markers land on G, P1, N via the derivation cascade), then supersede
    P1 with a replacement that does not cite G. P1's own marker clears
    (supersede resolves the superseded node's own pending markers
    unconditionally) while G's marker is untouched and stays pending.
    Direct-premises-only would read N as unblocked (its one direct premise,
    P1, is clean) even though G — still upstream of N via P1's edges,
    which supersede does not rewire — is still live. Transitive correctly
    keeps N blocked: it withholds frontier status while ANY upstream marker
    is still pending, rather than trusting a resolved direct premise to
    imply a clean spine above it. (Regression fixture:
    test_transitive_catches_out_of_order_resolution_direct_premises_would_miss
    in tests/test_cascade_frontier.py.)
    """
    ancestors = _cascade_ancestors(conn, node_id) - {node_id}
    return any(a in pending_node_ids for a in ancestors)


def _cascade_event_creation_time(conn, cascade_event: str) -> str:
    """The event's own ordering key: earliest created_at among ALL markers
    (any status) sharing cascade_event. cascade_event carries no timestamp
    column of its own. Every marker one retract/supersede call writes shares
    the same ``now`` (see _write_cascade_marker's callers), so this is
    normally just that shared timestamp; MIN() is a defensive choice for
    backfilled events, where #2324's per-node 'recovered' provenance can
    carry genuinely different original timestamps across markers nominally
    sharing one migration-synthesized event id.
    """
    row = conn.execute(
        "SELECT MIN(created_at) AS t FROM cascade_markers WHERE cascade_event = ?",
        (cascade_event,),
    ).fetchone()
    return row["t"] if row and row["t"] else ""


def _root_replacement_decision(conn, root_node: str, root_op: str) -> dict | None:
    """#2613 §5: the cascade ROOT carries no cascade_markers row — markers
    are written for affected DOWNSTREAM nodes, never for the node whose
    retraction/supersession created the cascade — so when the root is
    itself one of node_id's direct premises, _dead_premises' marker-based
    query structurally cannot find it (there is no 'resolved' marker row to
    match). The root's decision lives on its own retraction/supersession
    EVENT instead: §3 change 3 puts `replacement_id` /
    `replacement_absent_because` on a retraction's own edit_history row;
    supersede already records `replaced_by` there. Read it from there
    rather than from a marker row that by construction does not exist —
    "the design's answer falls straight out of event-not-edge" (§5).

    Returns None only if no matching event is found (should not happen for
    a genuine cascade root, but this is a read, not an assumed invariant).
    """
    action = "retracted" if root_op == "retract" else "superseded"
    row = conn.execute(
        "SELECT details, timestamp FROM edit_history "
        "WHERE node_id = ? AND action = ? "
        "ORDER BY id DESC LIMIT 1",
        (root_node, action),
    ).fetchone()
    if not row:
        return None
    details = json.loads(row["details"] or "{}")
    replacement_id = (
        details.get("replacement_id") if root_op == "retract"
        else details.get("replaced_by")
    )
    return {
        "premise_id": root_node,
        "resolution_kind": action,
        "resolution_replacement_id": replacement_id,
        "resolved_at": row["timestamp"],
        "replacement_absent_because": details.get("replacement_absent_because"),
        "from_root_event": True,
    }


def _cascade_dead_premise_detail(conn, node_id: str, cascade_event: str,
                                  root_node: str = "", root_op: str = "") -> list:
    """Detail companion to _dead_premises: which of node_id's direct
    premises genuinely moved (resolved superseded/retracted) under
    cascade_event, plus the resolution_kind / replacement id / resolved_at
    each carries. Reuses _dead_premises for the id set (never re-derives its
    filter) and pulls only the extra columns that function does not return.

    #2613 §5: when root_node is itself one of node_id's direct premises, it
    is genuinely dead but _dead_premises' marker-based query can never find
    it (see _root_replacement_decision's docstring) — folded in here from
    the root's own event, entries sorted together so the caller sees one
    coherent list regardless of source.
    """
    dead_ids = _dead_premises(conn, node_id, cascade_event)
    direct_premises = set(core._direct_premise_ids(conn, node_id))
    root_is_direct_dead = bool(
        root_node and root_node in direct_premises and root_node not in dead_ids
    )
    if not dead_ids and not root_is_direct_dead:
        return []
    detail = []
    if dead_ids:
        placeholders = ",".join("?" for _ in dead_ids)
        rows = conn.execute(
            f"SELECT node_id, resolution_kind, resolution_replacement_id, resolved_at "
            f"FROM cascade_markers WHERE cascade_event = ? AND node_id IN ({placeholders}) "
            f"ORDER BY node_id",
            (cascade_event, *dead_ids),
        ).fetchall()
        detail.extend([
            {
                "premise_id": r["node_id"],
                "resolution_kind": r["resolution_kind"],
                "resolution_replacement_id": r["resolution_replacement_id"],
                "resolved_at": r["resolved_at"],
            }
            for r in rows
        ])
    if root_is_direct_dead:
        root_detail = _root_replacement_decision(conn, root_node, root_op)
        if root_detail:
            detail.append(root_detail)
    detail.sort(key=lambda d: d["premise_id"])
    return detail


def _cascade_frontier_node_summary(conn, node_id: str) -> dict:
    """One frontier node's summary entry: id, type, a truncated claim gist
    (truncation explicitly flagged, never silent), and per-event marker
    detail (root_node / root_op / which direct premises died and how).

    Every PENDING marker on node_id contributes one entry to "events" — a
    multi-event node surfaces ALL of its events here, never collapsed to a
    single value (Invariant 8: default to over-surfacing; see the
    coder-fairy handoff's ambiguous-decision log for why "events" is a list
    rather than the spec's singular "root_op" field).
    """
    node_row = conn.execute(
        "SELECT type, claim FROM nodes WHERE id = ?", (node_id,)
    ).fetchone()
    claim = (node_row["claim"] or "") if node_row else ""
    node_type = node_row["type"] if node_row else "UNKNOWN"

    truncated = len(claim) > _CASCADE_FRONTIER_CLAIM_GIST_LEN
    claim_gist = (
        claim[:_CASCADE_FRONTIER_CLAIM_GIST_LEN] + "..." if truncated else claim
    )

    direct_premises = set(core._direct_premise_ids(conn, node_id))
    events = []
    for m in core._pending_markers_for_node(conn, node_id):
        cascade_event = m["cascade_event"]
        root_node = m["root_node"]
        root_op = m["root_op"]
        events.append({
            "cascade_event": cascade_event,
            "root_node": root_node,
            "root_op": root_op,
            # Classification label, NOT a block-strength directive — both
            # root_ops hard-block equally (core._cascade_marker_severity's
            # own docstring warns against reading it as authority). Surfaced
            # as a machine-labeled hint per Invariant 8, never unmarked.
            "severity_label_hint": core._cascade_marker_severity(root_op),
            "created_at": m["created_at"],
            "root_is_direct_premise": root_node in direct_premises,
            "immediate_cause": _immediate_cause(conn, node_id, cascade_event),
            "dead_premises": _cascade_dead_premise_detail(
                conn, node_id, cascade_event, root_node=root_node, root_op=root_op
            ),
        })

    events.sort(key=lambda e: (e["created_at"], e["cascade_event"]))
    earliest_created_at = events[0]["created_at"] if events else ""
    earliest_event = events[0]["cascade_event"] if events else ""

    return {
        "id": node_id,
        "type": node_type,
        "claim_gist": claim_gist,
        "claim_gist_truncated": truncated,
        "events": events,
        "_earliest_pending_created_at": earliest_created_at,
        "_earliest_pending_event": earliest_event,
    }


def _cascade_frontier_packet(conn, node_id: str, summary: dict) -> dict:
    """Full adjudication packet for one frontier node — the K (``limit``)
    nodes this pass admits. Everything the summary carries, plus the full
    (untruncated) claim, confidence, recall_summary, created_at, is_current.
    Invariant 8: show ALL the raw information the agent needs to
    adjudicate, never only the machine's compressed summary-tier take.
    """
    row = conn.execute(
        "SELECT claim, confidence, recall_summary, created_at, is_current, type "
        "FROM nodes WHERE id = ?", (node_id,),
    ).fetchone()
    return {
        "id": node_id,
        "type": row["type"] if row else summary.get("type"),
        "claim": row["claim"] if row else None,
        "confidence": row["confidence"] if row else None,
        "recall_summary": row["recall_summary"] if row else None,
        "created_at": row["created_at"] if row else None,
        "is_current": bool(row["is_current"]) if row else None,
        "events": summary["events"],
    }


def _cascade_frontier_impl(node_id: str = None, limit: int = None) -> str:
    """Impl for engram_cascade_frontier — see the @mcp.tool wrapper's
    docstring in server.py for the full contract. READ-ONLY throughout: no
    write ever happens on this path (no marker mutation, no confidence
    recompute, no recall/utility side effects).

    Mode B (node_id is None): every frontier node in the graph.
    Mode C (node_id given): the frontier restricted to
    ancestors(node_id) ∩ pending — node_id's own pending-marked
    cascade-ancestors (plus node_id itself, when node_id is itself already
    frontier — ancestors(node_id) includes node_id per _cascade_ancestors'
    documented convention). Also returns a labeled blast-radius ESTIMATE.

    Recomputed fresh from marker state on every call — no queue, no cursor,
    no persisted frontier state; identical input state always yields
    identical output (deterministic ordering, see the sort key below).
    """
    if limit is None:
        limit = _CASCADE_FRONTIER_DEFAULT_LIMIT
    if not isinstance(limit, int) or isinstance(limit, bool) or limit < 0:
        return json.dumps({
            "error": f"limit must be a non-negative integer, got {limit!r}"
        })

    conn = core._get_db()
    try:
        if node_id is not None:
            root_row = conn.execute(
                "SELECT id FROM nodes WHERE id = ?", (node_id,)
            ).fetchone()
            if not root_row:
                return json.dumps({"error": f"Node '{node_id}' not found."})

        pending_rows = conn.execute(
            "SELECT DISTINCT node_id FROM cascade_markers WHERE status = 'pending'"
        ).fetchall()
        pending_node_ids = {r["node_id"] for r in pending_rows}

        if node_id is None:
            candidates = set(pending_node_ids)
        else:
            ancestors = _cascade_ancestors(conn, node_id)
            candidates = ancestors & pending_node_ids

        frontier_ids = [
            nid for nid in candidates
            if not _cascade_blocked(conn, nid, pending_node_ids)
        ]

        # Ordering (Lei's ruling, settles §9.4 item 3): event-grouped first
        # tier (by the EVENT's own earliest-ever marker timestamp, across
        # every marker sharing that event id, any status), then created_at
        # ascending within a node's own earliest pending event, tie-break on
        # cascade_event id (keeps groups from interleaving under a timestamp
        # tie) then on node id for full determinism. ORDERING IS
        # PRESENTATION-ONLY — nothing in this function reads position as
        # meaning; recomputing this sort from identical marker state always
        # yields identical order.
        summaries = [_cascade_frontier_node_summary(conn, nid) for nid in frontier_ids]
        event_time_cache: dict = {}

        def _event_order_key(ev: str) -> str:
            if ev not in event_time_cache:
                event_time_cache[ev] = _cascade_event_creation_time(conn, ev)
            return event_time_cache[ev]

        summaries.sort(
            key=lambda s: (
                _event_order_key(s["_earliest_pending_event"]),
                s["_earliest_pending_event"],
                s["_earliest_pending_created_at"],
                s["id"],
            )
        )
        for s in summaries:
            del s["_earliest_pending_created_at"]
            del s["_earliest_pending_event"]

        packets = [
            _cascade_frontier_packet(conn, s["id"], s)
            for s in summaries[:limit]
        ]

        response: dict = {
            "mode": "B" if node_id is None else "C",
            "node_id": node_id,
            "frontier": summaries,
            "frontier_count": len(summaries),
            "total_pending_nodes": len(pending_node_ids),
            "packets": packets,
            "packets_returned": len(packets),
            "packets_limit": limit,
        }

        # Invariant-1 anomaly check (READ-ONLY — flags, never repairs). If
        # `candidates` (self-inclusive: pending_node_ids in mode B,
        # ancestors(node_id) ∩ pending in mode C) is non-empty but the
        # frontier came out empty, no candidate is currently resolvable —
        # a state that should be UNREACHABLE in a well-formed graph
        # (Invariant 1: "no pending marker survives a completed mode-B
        # run" presupposes the frontier always contains a minimal unblocked
        # element while pending markers remain). Silently returning
        # frontier_count: 0 here is indistinguishable from "all clear" to a
        # caller that isn't independently cross-checking total_pending_nodes
        # — exactly the condition that would let a mode-B sweep loop
        # (frontier -> resolve) terminate clean while pending markers are
        # still stuck. Surfaced ONLY on this branch — omitted entirely from
        # the healthy-path response (never emitted as null/false) so its
        # mere presence is the loud signal, consistent with how the mode-C-
        # only fields below are absent rather than null in mode B.
        if not summaries and candidates:
            if node_id is None:
                _anomaly_scope = (
                    f"{len(pending_node_ids)} node(s) graph-wide carry a "
                    "pending cascade marker (total_pending_nodes) but "
                    "frontier_count is 0 — nothing is resolvable."
                )
            else:
                _anomaly_scope = (
                    f"{len(candidates)} node(s) in {node_id}'s own "
                    "pending-ancestor set (self-inclusive) carry a pending "
                    "cascade marker but frontier_count is 0 — nothing in "
                    "that set is resolvable."
                )
            response["invariant_1_violation_suspected"] = (
                "ANOMALY SUSPECTED, not confirmed — machine assessment, not "
                "a directive. " + _anomaly_scope + " This state should be "
                "unreachable in a well-formed graph. Most likely cause: a "
                "CYCLE in CASCADE_RELATIONS — the time-ordered-DAG "
                "integrity scan is advisory, not blocking, so a cycle can "
                "exist in a live graph and every node on it blocks every "
                "other, leaving no minimal unblocked element. Also "
                "possible: an out-of-order resolution elsewhere in the "
                "cascade machine. A mode-B sweep (the frontier -> resolve "
                "loop) MUST NOT treat this response as a completed, clean "
                "sweep — Invariant 1 (no pending marker survives a "
                "completed mode-B run) is not satisfied while this field "
                "is present."
            )

        if node_id is not None:
            # ancestors(node_id) ∩ pending, EXCLUDING node_id itself — "the
            # pending-marked cascade-ancestors of the target" the spec names
            # for the blast estimate (distinct from `candidates` above,
            # which is self-inclusive for frontier-membership purposes).
            ancestors_pending = candidates - {node_id}
            events_in_play: set = set()
            for anc in ancestors_pending:
                for m in core._pending_markers_for_node(conn, anc):
                    events_in_play.add(m["cascade_event"])
            blast_ids: set = set()
            if events_in_play:
                placeholders = ",".join("?" for _ in events_in_play)
                rows = conn.execute(
                    f"SELECT DISTINCT node_id FROM cascade_markers "
                    f"WHERE cascade_event IN ({placeholders})",
                    tuple(events_in_play),
                ).fetchall()
                blast_ids = {r["node_id"] for r in rows}
            response["pending_ancestor_count"] = len(ancestors_pending)
            response["blast_radius_estimate"] = len(blast_ids)
            response["blast_radius_estimate_note"] = (
                "ESTIMATE, not a guarantee. blast_radius_estimate = total "
                "distinct nodes ever marked (pending OR resolved) by the "
                f"cascade_event(s) reaching {node_id}'s pending ancestors — "
                "the larger historical footprint. pending_ancestor_count is "
                "the SMALLER, minimal set that must resolve before node_id "
                "itself clears (full pending_ancestor_count ~= blast_radius_"
                "estimate; the claim you need now unblocks by resolving "
                "pending_ancestor_count nodes, typically << blast_radius_"
                "estimate). Every junction where an agent resolves a "
                "premise as 'acknowledged' (claim unchanged) rather than "
                "superseding/retracting it further changes what the "
                "eventual real footprint turns out to be — resolve, then "
                "re-call this tool rather than trusting either number as "
                "fixed."
            )

        return json.dumps(response)
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# engram_cascade_resolve (engram-alpha#2013 slice 4 — the adjudication verb)
# ---------------------------------------------------------------------------
# The WRITE half of the cascade machine. Slice 3's frontier answers "which
# marked nodes are ready to adjudicate"; this answers "the agent has decided,
# record it".
#
# Four properties the spec fixes, restated here because each one is a place a
# later edit could plausibly go the other way:
#
#  1. PURE-WIRE — never creates a node (Lei's ruling, §10.2). A `superseded`
#     resolution names an ALREADY-EXISTING replacement, created beforehand via
#     its own canonical tool, exactly as engram_supersede requires. Bundling
#     creation into the resolution risks orphans on a mid-way failure.
#  2. PER-NODE, not per-marker (§10.2) — there is deliberately no
#     `cascade_event` parameter. One judgement stamps the same
#     resolution_kind on ALL of the node's pending markers. The per-event
#     rows are retained for audit. What makes this safe rather than an
#     over-resolve is that the frontier packet the agent adjudicates from
#     shows EVERY pending event on the node (slice 3's `events` list), so the
#     single judgement is made with all the deltas in view.
#  3. THE AGENT DECIDES (§4). The machine propagates, orders, computes
#     confidence and enforces eligibility. It never picks a resolution shape
#     — with exactly one exception, the trivial auto-resolution below, which
#     is decided by PROOF (no premise actually moved) rather than judgement.
#  4. CONFIDENCE IS ALWAYS RECOMPUTED, however the marker resolves, and the
#     recompute is TRANSITIVE (§4). Note the machine carries two propagations
#     with OPPOSITE rules: markers do NOT re-cascade on resolution, confidence
#     DOES.

_CASCADE_RESOLUTION_KINDS = ("acknowledged", "retracted", "superseded")

# root_op -> the resolution_kind that root op amounts to, for a cascade ROOT
# that is also a direct premise of the node being adjudicated. The root never
# carries a marker for its own event (the walker seeds it into `visited`), so
# without this mapping the MODAL case — "the premise I derived from is the
# thing that got retracted" — would read as no affected premises at all.
# This is the 2026-08-26 amendment: the root counts as a premise resolving as
# root_op.
_CASCADE_ROOT_OP_AS_RESOLUTION = {"retract": "retracted", "supersede": "superseded"}


def _cascade_affected_premises(conn, node_id: str) -> list:
    """Which of node_id's DIRECT premises are affected by node_id's OWN
    pending cascade events, and how each one resolved.

    Returns a list of dicts, one per (premise, event) pair, each carrying
    ``premise_id``, ``cascade_event``, ``via`` ("marker" or "root"),
    ``resolution_kind`` (None while the premise is still pending) and
    ``still_pending``. Deliberately NOT collapsed to one row per premise:
    a premise can be affected by two of the node's events and resolve
    differently under each, and invariant 8 says over-surface rather than
    pick one.

    "Direct premise" is core._direct_premise_ids — derives_from AND
    supported_by. That is broader than CASCADE_RELATIONS (supported_by lost
    its cascade flag in #2252 Stage 1.3, so a supported_by parent is never
    itself marked BY the walk) and it is the right breadth here: this read
    asks which of MY premises moved, not which edges conduct a cascade.
    """
    premises = set(core._direct_premise_ids(conn, node_id))
    if not premises:
        return []

    out = []
    for m in core._pending_markers_for_node(conn, node_id):
        event = m["cascade_event"]
        root_node = m["root_node"]

        placeholders = ",".join("?" for _ in premises)
        rows = conn.execute(
            f"SELECT node_id, status, resolution_kind FROM cascade_markers "
            f"WHERE cascade_event = ? AND node_id IN ({placeholders}) "
            f"ORDER BY node_id",
            (event, *sorted(premises)),
        ).fetchall()
        for r in rows:
            out.append({
                "premise_id": r["node_id"],
                "cascade_event": event,
                "via": "marker",
                "resolution_kind": r["resolution_kind"],
                "still_pending": r["status"] == "pending",
            })

        if root_node in premises:
            out.append({
                "premise_id": root_node,
                "cascade_event": event,
                "via": "root",
                "resolution_kind": _CASCADE_ROOT_OP_AS_RESOLUTION.get(m["root_op"]),
                "still_pending": False,
            })

    out.sort(key=lambda a: (a["cascade_event"], a["premise_id"], a["via"]))
    return out


def _cascade_eligible_shapes(conn, node_id: str, affected: list = None) -> dict:
    """§11's ``eligible(node)`` — which resolution shapes this node may take.

        {retract, supersede}                if any affected direct premise
                                            resolved as 'retracted'
        {retract, supersede, acknowledge}   otherwise

    Acknowledge is the only shape that can be withdrawn, and only retract
    withdraws it, because only retract removes a LEG from the argument: a
    superseded premise still supports the claim (differently), a retracted
    one never supported it at all. The predicate gates the AVAILABILITY of
    the option, never the choice — the agent still judges whether a
    superseded premise's delta touches the claim (§4).

    Returns {"eligible": [...], "acknowledge_blocked_by": [...],
    "affected_premises": [...]} — the blockers are named, not merely
    counted, so a refusal tells the agent WHICH premise closed the option
    (invariant 8: the agent must be able to re-derive the machine's verdict
    from what it was shown).

    Computed here and NEVER persisted; severity has no column (§1). Derived
    fresh at each node's own resolution turn, which is the only time it is
    knowable (§6).
    """
    if affected is None:
        affected = _cascade_affected_premises(conn, node_id)
    blockers = sorted({
        a["premise_id"] for a in affected if a["resolution_kind"] == "retracted"
    })
    eligible = ["retracted", "superseded"]
    if not blockers:
        eligible.append("acknowledged")
    return {
        "eligible": eligible,
        "acknowledge_blocked_by": blockers,
        "affected_premises": affected,
    }


def _cascade_recompute_confidence_transitive(conn, node_id: str) -> dict:
    """Recompute node_id's confidence and then every cascade-descendant's,
    to a fixpoint (§4: "the recompute is itself a propagation, and it must
    be transitive" — one hop is not enough, because a child was computed
    from the OLD value).

    WHY A FIXPOINT AND NOT ONE ORDERED PASS: correctness needs parents
    recomputed before children, and the descendant walk
    (_walk_cascade_downstream) is depth-first, which does not produce a
    topological order on a diamond — a node reachable by both a short and a
    long path can be visited before one of its own parents. Rather than
    build a second ordering primitive, this repeats whole passes until a
    pass changes nothing. Each pass advances the frontier of settled values
    by at least one level of the DAG, so it converges in at most
    depth(subgraph) + 1 passes; the bound below is len(order) + 1, which is
    >= that for any DAG.

    Non-convergence would mean a CYCLE in CASCADE_RELATIONS (the DAG
    integrity scan is advisory, not blocking, so one can exist in a live
    graph). That is reported, loudly and by name, rather than silently
    truncated at the bound — a partial recompute reported as a complete one
    is exactly the shape invariant 8 refuses.
    """
    order = []
    seen = set()

    def _collect(row):
        if row["id"] not in seen:
            seen.add(row["id"])
            order.append(row["id"])

    # The subject node first, but ONLY while it is still current. An
    # 'acknowledged' resolution leaves it current and its recompute is the
    # whole point (§4). A 'retracted' or 'superseded' resolution has already
    # set is_current=0 — a dead node's confidence is a historical record, and
    # rewriting it would edit the audit trail rather than repair the graph.
    # The descendant walk below is seeded from its dependents either way, so
    # skipping it never truncates the propagation.
    subject = conn.execute(
        "SELECT is_current FROM nodes WHERE id = ?", (node_id,)
    ).fetchone()
    if subject and subject["is_current"] == 1:
        seen.add(node_id)
        order.append(node_id)

    dependents = conn.execute(
        "SELECT source_id FROM edges "
        f"WHERE target_id = ? AND relation IN ({_CASCADE_RELATION_PLACEHOLDERS})",
        (node_id, *_CASCADE_RELATIONS_SORTED),
    ).fetchall()
    visited = {node_id}
    for d in dependents:
        _walk_cascade_downstream(conn, d["source_id"],
                                 visited=visited, on_live_node=_collect)

    changes = []
    skipped = []
    passes = 0
    max_passes = len(order) + 1
    # `converged` is tracked EXPLICITLY rather than inferred from
    # `passes >= max_passes`, because that inference is wrong on the modal
    # path and wrong in the direction that publishes a false claim.
    # Convergence means "a whole pass changed nothing"; hitting the bound
    # means "we stopped counting". They coincide only by accident.
    #
    # Two measured false positives from the inferred version, both ordinary:
    #   * a leaf resolved as retracted/superseded -> `order` is empty, so
    #     max_passes == 1 and the single pass trips `1 >= 1`;
    #   * a leaf acknowledged with a real confidence change -> one node,
    #     max_passes == 2, pass 1 changes it, pass 2 confirms, `2 >= 2`.
    # Both then told the agent the recompute was "PARTIAL, not final" and
    # blamed a CASCADE_RELATIONS cycle that did not exist, while `changed`
    # was in fact complete and correct. (Caught by the round-1 reviewer.)
    converged = False
    while passes < max_passes:
        passes += 1
        changed_this_pass = False
        for nid in order:
            d = core._recompute_node_confidence(conn, nid)
            if d["disposition"] == "recomputed":
                changed_this_pass = True
                changes.append(d)
            elif d["disposition"] == "skipped" and passes == 1:
                skipped.append(d)
        if not changed_this_pass:
            converged = True
            break

    result = {
        "nodes_considered": len(order),
        "changed": [
            {"node_id": c["node_id"], "old": c["old"], "new": c["new"]}
            for c in changes
        ],
        "skipped": skipped,
        "passes": passes,
    }
    if not converged:
        result["convergence_warning"] = (
            f"Confidence recompute did NOT converge within {max_passes} "
            f"passes over {len(order)} node(s). The recorded 'changed' list "
            f"is therefore PARTIAL, not final. This should be unreachable in "
            f"a well-formed graph: the most likely cause is a CYCLE in "
            f"CASCADE_RELATIONS (the time-ordered-DAG integrity scan is "
            f"advisory, not blocking, so a cycle can exist in a live graph). "
            f"Re-running this resolution's recompute is idempotent and safe "
            f"once the cycle is broken."
        )
    return result


class _CascadeCycleError(Exception):
    """Raised by `_cascade_record_acknowledgement_edges` (#2013 §12.7) when
    writing an `acknowledged_derives_from` edge would close a cycle over
    CASCADE_RELATIONS. `_cascade_resolve_impl`'s 'acknowledged' branch
    catches this and turns it into a refusal response — it must never reach
    a caller as a raw exception.
    """

    def __init__(self, node_id: str, replacement_id: str):
        self.node_id = node_id
        self.replacement_id = replacement_id
        super().__init__(
            f"acknowledging {node_id!r} would close a cascade cycle: "
            f"{replacement_id!r} already transitively derives from "
            f"{node_id!r} over CASCADE_RELATIONS."
        )


def _cascade_record_acknowledgement_edges(conn, node_id: str,
                                          affected: list, now: str) -> list:
    """§4: an acknowledgement records that the node now ALSO rests on each
    superseding premise — written as `acknowledged_derives_from` (#2013 §12.5),
    never as `derives_from`.

    WHY A DISTINCT RELATION, and it is not taxonomy for its own sake: the
    acknowledging node exists BEFORE the replacement it comes to rest on —
    that is what an acknowledgement IS — so this edge always inverts creation
    order. Filed as `derives_from` it produced one DAG-time violation per
    acknowledgement, measured, because `derives_from` carries
    `temporal_causal: True` (#2357 renamed that flag from dag_check; the
    creation-order semantics are the renamed flag's, NOT the new `dag`
    edge-walk flag's). `acknowledged_derives_from` carries
    `temporal_causal: False` for exactly that structural reason — and
    `dag: True`, because it is still not permitted to close a cycle — which
    keeps `derives_from`'s creation-order check honest everywhere else.

    WHY IT MUST EXIST AT ALL: an acknowledgement OUTLIVES the repair window.
    Once acknowledged the node stops being marked, so if the replacement is
    itself superseded LATER there is nothing re-reading the old premise and
    nothing for the cascade to walk. The edge is the "record DEPENDENCIES
    eagerly" half of §1 — a dependency is not a cached value, it is what lets
    the cascade SEE. It carries `cascade: True` and participates in confidence
    for the same reason.

    §12.4's ORIGINAL PROOF, AND WHY IT WAS INCOMPLETE: a cycle would need the
    replacement to derive from this node; the node carries a marker only if
    it is downstream of the superseded premise; so the replacement would have
    to be downstream of what it replaces — which `_supersede_impl`'s §12.3
    guardrail refuses. That reasoning is sound for a SINGLE supersede/
    acknowledgement pair, but §12.3 is a POINT-IN-TIME check: it is evaluated
    once, at supersede time, over the edge table as it stood THEN — and every
    acknowledgement written AFTER adds an edge to the very relation the check
    was evaluated over. Two individually-legal supersedes plus two
    acknowledgements can therefore still close a cycle across three nodes,
    measured (#2013 §12.7): confidence recompute stopped being idempotent
    (0.411 -> 0.322 -> 0.254 on identical re-runs of the same fixture).

    §12.7 THEREFORE ADDS A SECOND, WRITE-TIME GUARD, HERE: before writing an
    edge from `node_id` to `replacement`, this function refuses if
    `replacement` already transitively derives from `node_id` — i.e. if THIS
    edge would itself be the one that closes the cycle, regardless of
    whether §12.3 passed when each underlying supersede was considered alone.
    Reuses `_cascade_reaches` (never a second walker). Refusal raises
    `_CascadeCycleError`; `_cascade_resolve_impl` catches it and returns an
    error object, not partial success — no acknowledgement edges from this
    call are left written.

    The replacement is read at USE time from the premise's own resolution
    (invariant 2: a marker caches nothing derivable, and a replacement pointer
    is a cached VALUE that can drift). The OLD premise edge is deliberately
    left in place: it is the true historical provenance of the claim, and
    removing it would rewrite the node's derivation history rather than
    extend it.
    """
    out = []
    for a in affected:
        if a["resolution_kind"] != "superseded":
            continue
        premise_id = a["premise_id"]
        if a["via"] == "marker":
            row = conn.execute(
                "SELECT resolution_replacement_id FROM cascade_markers "
                "WHERE node_id = ? AND cascade_event = ?",
                (premise_id, a["cascade_event"]),
            ).fetchone()
            replacement = row["resolution_replacement_id"] if row else None
        else:
            # The cascade ROOT of a supersede event carries no marker of its
            # own; its replacement is the nodes.superseded_by pointer.
            row = conn.execute(
                "SELECT superseded_by FROM nodes WHERE id = ?", (premise_id,)
            ).fetchone()
            replacement = row["superseded_by"] if row else None
        if not replacement or replacement == node_id:
            continue
        exists = conn.execute(
            "SELECT 1 FROM edges WHERE source_id = ? AND target_id = ? "
            "AND relation = 'acknowledged_derives_from'",
            (node_id, replacement),
        ).fetchone()
        if not exists:
            # §12.7 WRITE-TIME GUARD: refuse if writing THIS edge would
            # close a cycle over CASCADE_RELATIONS. §12.3's supersede-time
            # guardrail is a point-in-time check, re-evaluated only when
            # each supersede happens; every acknowledgement grows the very
            # relation it was evaluated over, so a cycle can still close
            # here even though both underlying supersedes were
            # individually legal (measured, #2013 §12.7). Reuses
            # `_cascade_reaches` — do not write a second walker.
            if _cascade_reaches(conn, node_id, replacement):
                raise _CascadeCycleError(node_id, replacement)
            conn.execute(
                "INSERT INTO edges (source_id, target_id, relation, created_at) "
                "VALUES (?, ?, 'acknowledged_derives_from', ?)",
                (node_id, replacement, now),
            )
        out.append({"premise_id": premise_id, "replacement_id": replacement})
    return out


def _cascade_auto_resolve_trivial(conn, node_id: str, now: str) -> list:
    """§4's trivial-resolution rule, applied downstream of a node that has
    just resolved: a child auto-resolves as 'acknowledged' iff its derived
    DEAD-premise set is thereby empty — i.e. every affected premise resolved
    as 'acknowledged' (nothing actually moved) and none is still pending.

    This is the ONLY shape the machine decides for itself, and it is decided
    by PROOF rather than judgement: if no premise substantively changed,
    there is no delta for an agent to weigh. §10.2: trivial resolutions
    auto-resolve inside the machine and the agent never sees them on the
    frontier — which needs no cooperation from the frontier read, because
    they are already resolved by the time it next runs.

    Recurses: an auto-resolution can make ITS children trivial in turn.

    THE FRONTIER CHECK IS DEFENSIVE, AND I COULD NOT CONSTRUCT A LIVE CASE
    FOR IT — stated plainly because an earlier version of this docstring
    argued it was load-bearing, and that claim was more than the code can
    demonstrate. (The round-1 reviewer caught it as a surviving mutation:
    deleting the check breaks no test.)

    Why it is hard to reach: for the check to matter, every affected direct
    premise must have resolved 'acknowledged' while some further ancestor is
    still pending. But 'acknowledged' is written by exactly two paths —
    _cascade_resolve_impl and this function — and BOTH gate on the frontier
    first. The plain retract/supersede verbs write only 'retracted' /
    'superseded' via _resolve_own_markers, and either of those fails the
    all-acknowledged test one line above regardless. So through current
    write paths the two checks appear to coincide.

    THE REASON I FIRST GAVE FOR KEEPING IT WAS ALSO WRONG — corrected here
    from the round-2 reviewer, who could not falsify the unreachability
    either but found the mechanism that actually establishes it. I had
    appealed to #2324's BACKFILLED markers as a source of out-of-frontier
    -order resolutions. They are not: the backfill writes status='pending'
    unconditionally (engram_core.py's migration-5 block) and never writes a
    resolution at all, so it cannot produce a resolved-out-of-order marker.

    What actually closes the construction is the CITATION guard: engram_derive
    refuses to cite a pending-marked premise, and `use_stale` is retired with
    no effect — so a descendant cannot be created under a pending ancestor,
    which is invariant 4 holding mechanically rather than by convention.

    Kept anyway as one cheap query, on the general ground that invariant 6 is
    a design rule no write path enforces and this is the last gate before an
    automatic resolution the agent never sees. Asserting it is exercised is
    what was wrong, twice; if a future reader can construct the case, pin it.

    #2406 — IDENTITY-LAYER GATE. This function is the one shape the machine
    decides for itself, by proof rather than judgement — but "no delta
    reaches this claim" is a statement about the CLAIM, and for an
    identity-layer node (currently: axiom) the standing rule elsewhere in
    this system (engram-revise, the dream-master cascade-sweep section) is
    that the cascade having touched the node at all is what surfaces to the
    user, not whether the claim moved. So a candidate whose type is in
    `_IDENTITY_LAYER_CLAIM_BEARING_TYPES` is skipped here even when every
    other condition for trivial auto-resolve holds — it is left pending,
    which puts it on the frontier for an agent to adjudicate (and surface)
    explicitly, exactly as an axiom already must once it reaches the
    frontier by any other path. Filed as a gap and fixed same-day: #2401's
    dream-master text stated the surface-to-user rule for axioms reaching
    the frontier without this function honoring it, so an axiom whose
    premises all resolved 'acknowledged' would never have reached the
    frontier to surface at all.
    """
    # Collect the candidate set ONCE (every cascade-descendant of node_id),
    # then sweep it to a fixpoint. A single downward pass would be wrong for
    # the same reason the confidence recompute needs a fixpoint: the walk is
    # depth-first, so on a diamond a node can be examined and rejected as
    # still-blocked BEFORE the sibling path that unblocks it has resolved.
    # A rejected node is never re-examined by a queue-with-visited-set, and
    # the failure is silent — the node simply never auto-resolves and shows
    # up on the frontier for an agent to adjudicate by hand, which reads
    # exactly like a node that was never trivial.
    candidates = []
    seen = {node_id}

    def _collect(row):
        if row["id"] not in seen:
            seen.add(row["id"])
            candidates.append(row["id"])

    dependents = conn.execute(
        "SELECT source_id FROM edges "
        f"WHERE target_id = ? AND relation IN ({_CASCADE_RELATION_PLACEHOLDERS})",
        (node_id, *_CASCADE_RELATIONS_SORTED),
    ).fetchall()
    visited = {node_id}
    for d in dependents:
        _walk_cascade_downstream(conn, d["source_id"],
                                 visited=visited, on_live_node=_collect)

    auto = []
    resolved_here = set()
    while True:
        pending_rows = conn.execute(
            "SELECT DISTINCT node_id FROM cascade_markers WHERE status = 'pending'"
        ).fetchall()
        pending_node_ids = {r["node_id"] for r in pending_rows}

        progressed = False
        for child in candidates:
            if child in resolved_here:
                continue
            if not core._pending_markers_for_node(conn, child):
                continue
            if _cascade_blocked(conn, child, pending_node_ids):
                continue

            # #2406 — never auto-resolve an identity-layer node's markers;
            # leave it pending so it surfaces on the frontier instead.
            child_type_row = conn.execute(
                "SELECT type FROM nodes WHERE id = ?", (child,)
            ).fetchone()
            if child_type_row and child_type_row["type"] in _IDENTITY_LAYER_CLAIM_BEARING_TYPES:
                continue

            affected = _cascade_affected_premises(conn, child)
            if not affected:
                continue
            if any(a["still_pending"] for a in affected):
                continue
            if any(a["resolution_kind"] != "acknowledged" for a in affected):
                continue

            _resolve_own_markers(conn, child, "acknowledged", now)
            resolved_here.add(child)
            progressed = True
            auto.append({
                "node_id": child,
                "reason": "every affected direct premise resolved as "
                          "'acknowledged' — the derived dead-premise set is "
                          "empty, so no delta reaches this claim",
            })
        if not progressed:
            break
    return auto


def _cascade_resolve_impl(node_id: str = "", resolution_kind: str = "",
                          replacement_id: str = "", note: str = "",
                          error_type: str = "other",
                          replacement_absent_because: str = "",
                          replacement_absent_note: str = "") -> str:
    """Impl for engram_cascade_resolve — see the @mcp.tool wrapper's
    docstring in server.py for the full contract.

    Records the agent's adjudication of ONE frontier node: stamps
    ``resolution_kind`` on ALL of that node's pending markers, recomputes
    confidence transitively, and auto-resolves any child the decision
    renders trivial. Never creates a node.

    THE PHASE SPLIT, and its one honest limitation. Validation runs on its
    own connection; the 'retracted' and 'superseded' shapes then delegate to
    _retract_impl / _supersede_impl (each of which opens and commits its own
    connection — they are the ONE shared core for those verbs, per §10.2,
    reached here with cascade_triggering=False); the confidence recompute and
    trivial auto-resolution then run on a third connection. So the whole
    resolution is NOT one transaction. A failure between phases leaves the
    markers resolved and the confidence recompute not yet applied.

    That is a deliberate trade rather than an oversight, and it is bounded in
    a specific way: the recompute is a PURE FUNCTION of graph state and is
    idempotent, so the repair for a half-applied resolution is to run the
    recompute again — nothing is lost and nothing is double-counted. The
    alternative, inlining supersede's and retract's bodies to share one
    transaction, would fork the two verbs' semantics, which §10.2 rules out
    by name ("ONE shared retract core"). A divergent second retract
    implementation is a worse failure than a recoverable stale number.
    """
    if not node_id or not str(node_id).strip():
        return json.dumps({"error": "node_id is required and cannot be empty."})
    node_id = str(node_id).strip()

    if resolution_kind not in _CASCADE_RESOLUTION_KINDS:
        return json.dumps({
            "error": f"Invalid resolution_kind {resolution_kind!r}. Must be one "
                     f"of: {', '.join(sorted(_CASCADE_RESOLUTION_KINDS))}.",
        })

    # replacement_id is REQUIRED for 'superseded', PERMITTED for 'retracted',
    # refused otherwise (#2613 §3 change 2 — this guard used to refuse
    # replacement_id whenever resolution_kind != 'superseded', which made a
    # retraction reached through the frontier unable to name a successor
    # even when one demonstrably exists — the normal case in a sweep, since
    # you write the corrected node first and resolve second). Refusing
    # outright for 'acknowledged' is deliberate: a caller who passes a
    # replacement alongside 'acknowledged' has a different model of what
    # they are doing than the machine does, and silently dropping the
    # argument would leave that disagreement undetected.
    #
    # The two new fields (replacement_absent_because / _note) are threaded
    # through to _retract_impl with NO default that could satisfy the
    # requirement — the anti-pattern named in §3 is `error_type: str =
    # "other"` two lines up in this same signature, already satisfied on
    # this path by a default the caller never sees; do not repeat that
    # shape for the replacement decision. Validation of the decision itself
    # (mutually-exclusive, required-one, enum membership, UNKNOWN-needs-
    # note) is NOT duplicated here — it lives in _retract_impl alone, the
    # one chokepoint §3 names, so there is exactly one place it can drift.
    replacement_id = (replacement_id or "").strip()
    replacement_absent_because = (replacement_absent_because or "").strip()
    replacement_absent_note = (replacement_absent_note or "").strip()
    if resolution_kind == "superseded" and not replacement_id:
        return json.dumps({
            "error": "replacement_id is required when resolution_kind is "
                     "'superseded'. engram_cascade_resolve is PURE-WIRE — it "
                     "never creates a node. Create the replacement first via "
                     "its own canonical tool (engram_derive, "
                     "engram_add_observation, ...), then name it here.",
        })
    if resolution_kind == "acknowledged" and (
        replacement_id or replacement_absent_because or replacement_absent_note
    ):
        return json.dumps({
            "error": f"replacement_id / replacement_absent_because / "
                     f"replacement_absent_note are only meaningful when "
                     f"resolution_kind is 'superseded' or 'retracted' "
                     f"(got {resolution_kind!r}).",
        })
    # #3125: replacement_absent_because / replacement_absent_note are
    # retracted-only (per this function's own docstring in server.py:
    # "only meaningful when resolution_kind is 'retracted'") — a SEPARATE
    # rule from the replacement_id check above, which is meaningful for both
    # 'superseded' and 'retracted'. Do NOT fold this into the acknowledged
    # check above by widening it to `resolution_kind != "retracted"` across
    # all three fields — replacement_id is REQUIRED for 'superseded' (the
    # check a few lines above), so that widening would refuse every
    # legitimate superseded resolve.
    if resolution_kind != "retracted" and (
        replacement_absent_because or replacement_absent_note
    ):
        return json.dumps({
            "error": f"replacement_absent_because / replacement_absent_note "
                     f"are only meaningful when resolution_kind is "
                     f"'retracted' (got {resolution_kind!r}).",
        })

    conn = core._get_db()
    try:
        node = conn.execute(
            "SELECT id, type, is_current FROM nodes WHERE id = ?", (node_id,)
        ).fetchone()
        if not node:
            return json.dumps({"error": f"Node '{node_id}' not found."})
        if node["is_current"] != 1:
            return json.dumps({
                "error": f"Node '{node_id}' is not current (already superseded "
                         f"or retracted) — there is nothing left to adjudicate.",
            })

        pending = core._pending_markers_for_node(conn, node_id)
        if not pending:
            return json.dumps({
                "error": f"Node '{node_id}' carries no pending cascade markers "
                         f"— nothing to resolve. (Markers never return from "
                         f"'resolved' to 'pending'; invariant 6 has no reopen. "
                         f"A resolution later believed wrong is corrected the "
                         f"way every wrong claim is: supersede it, which fires "
                         f"a new cascade event through the normal path.)",
            })

        # Invariant 6: a marker resolves ONLY while its node is on the
        # frontier. Enforced here, at the write, rather than trusted — see
        # _cascade_blocked's docstring for why "the design rule holds" is not
        # the same as "the code enforces it".
        pending_rows = conn.execute(
            "SELECT DISTINCT node_id FROM cascade_markers WHERE status = 'pending'"
        ).fetchall()
        pending_node_ids = {r["node_id"] for r in pending_rows}
        if _cascade_blocked(conn, node_id, pending_node_ids):
            blocking = sorted(
                (_cascade_ancestors(conn, node_id) - {node_id}) & pending_node_ids
            )
            return json.dumps({
                "error": f"Node '{node_id}' is NOT on the frontier — "
                         f"{len(blocking)} of its cascade-ancestors still carry "
                         f"pending markers, so its own eligible resolution "
                         f"shapes are not yet knowable (they are a function of "
                         f"how ALL its premises resolve). Resolve upstream "
                         f"first.",
                "blocking_ancestors": blocking,
                "next_call": f"engram_cascade_frontier(node_id={node_id!r}) — "
                             f"returns the minimal pending-ancestor slice that "
                             f"unblocks this node.",
            })

        affected = _cascade_affected_premises(conn, node_id)
        eligibility = _cascade_eligible_shapes(conn, node_id, affected)
        if resolution_kind not in eligibility["eligible"]:
            return json.dumps({
                "error": f"'{resolution_kind}' is not an eligible resolution "
                         f"shape for '{node_id}'. Acknowledge is available iff "
                         f"NO affected direct premise resolved as retracted — "
                         f"only retract removes a leg from the argument.",
                "eligible": eligibility["eligible"],
                "acknowledge_blocked_by": eligibility["acknowledge_blocked_by"],
                "affected_premises": affected,
            })

        if resolution_kind == "superseded":
            repl = conn.execute(
                "SELECT id FROM nodes WHERE id = ?", (replacement_id,)
            ).fetchone()
            if not repl:
                return json.dumps({
                    "error": f"replacement_id '{replacement_id}' not found. "
                             f"Create the replacement before resolving.",
                })

        node_type = node["type"]
        resolved_events = [m["cascade_event"] for m in pending]
    finally:
        conn.close()

    # ---- Phase 2: apply the resolution shape ----------------------------
    delegated = None
    if resolution_kind == "acknowledged":
        conn = core._get_db()
        try:
            now = core._now()
            try:
                ack_edges = _cascade_record_acknowledgement_edges(
                    conn, node_id, affected, now
                )
            except _CascadeCycleError as exc:
                # §12.7: refuse loudly, not partial success — nothing from
                # this call is committed (no ack edges, no marker
                # resolution, no audit entry).
                conn.rollback()
                return json.dumps({
                    "error": f"Cycle guard: acknowledging {exc.node_id!r} "
                             f"would write an acknowledged_derives_from edge "
                             f"to {exc.replacement_id!r}, which already "
                             f"transitively derives from {exc.node_id!r} — "
                             f"closing a cycle over CASCADE_RELATIONS. "
                             f"Acknowledgement is not allowed here. Resolve "
                             f"{exc.node_id!r} through engram_cascade_resolve "
                             f"instead, with resolution_kind='superseded' "
                             f"(supplying a replacement_id) or "
                             f"resolution_kind='retracted' — that resolution "
                             f"route's own supersede/retract stays open by "
                             f"design; only acknowledgement is refused here.",
                    "guard": "acknowledgement_would_close_cascade_cycle",
                    "node_id": exc.node_id,
                    "replacement_id": exc.replacement_id,
                    "relation": "acknowledged_derives_from",
                    "remedy": "resolve through engram_cascade_resolve with "
                              "resolution_kind='superseded' (supplying a "
                              "replacement_id) or resolution_kind='retracted' "
                              "— acknowledgement is not allowed here. That "
                              "resolution route's own supersede/retract "
                              "stays open by design.",
                    "next_call": f"engram_cascade_frontier(node_id={exc.node_id!r})",
                    "then": f"engram_cascade_resolve(node_id={exc.node_id!r}, "
                            f"resolution_kind=...)",
                    "phase": "apply",
                    "markers_unchanged": True,
                })
            _resolve_own_markers(conn, node_id, "acknowledged", now)
            core._log_edit(conn, "cascade_acknowledged", node_id, node_type,
                           {"cascade_events": resolved_events,
                            "note": (note or "")[:200]})
            conn.commit()
        finally:
            conn.close()
    elif resolution_kind == "superseded":
        ack_edges = []
        # _supersede_impl resolves the node's own pending markers itself
        # (_resolve_own_markers with kind 'superseded' + the replacement),
        # which is exactly this resolution. cascade_triggering=False stops it
        # opening a SECOND event over a provably-covered set.
        delegated = json.loads(_supersede_impl(
            old_node_id=node_id,
            new_node_id=replacement_id,
            supersede_reason=note or f"cascade resolution of {resolved_events}",
            cascade_triggering=False,
        ))
        if "error" in delegated:
            return json.dumps({
                "error": f"supersede core refused this resolution: "
                         f"{delegated['error']}",
                "phase": "apply",
                "markers_unchanged": True,
            })
    else:  # retracted
        ack_edges = []
        delegated = json.loads(_retract_impl(
            node_id=node_id,
            error_type=error_type or "other",
            reason=note or f"cascade resolution of {resolved_events}",
            replacement_id=replacement_id,
            replacement_absent_because=replacement_absent_because,
            replacement_absent_note=replacement_absent_note,
            cascade_triggering=False,
        ))
        if "error" in delegated:
            return json.dumps({
                "error": f"retract core refused this resolution: "
                         f"{delegated['error']}",
                "phase": "apply",
                "markers_unchanged": True,
            })

    # ---- Phase 3: contradictions, confidence recompute, auto-resolution -
    conn = core._get_db()
    try:
        now = core._now()

        # CONTRADICTION CASCADE — runs on the resolution path too, and this
        # is the one thing cascade_triggering must NOT sweep away.
        #
        # §6's covered-set proof licenses suppressing the DERIVATION walk:
        # everything downstream already carries the root event's markers.
        # It does NOT extend to contradictions, and the reason is structural
        # rather than incidental: contradictions are non-claim-bearing, so
        # _walk_cascade_downstream stops before reaching one. They are only
        # ever marked by a query against the OPERATED node's own contradicts
        # edges — so a contradiction attached to a MID-CHAIN node was never
        # marked by the root's cascade, because the root ran that query
        # against its own edges. Nothing covers it. Measured before fixing:
        # with `ob -> d1` and a contradiction on `d1`, retracting `ob` left
        # the contradiction unmarked, and resolving `d1` left it unmarked
        # too — while an ordinary supersede of an unmarked `d1` marks it.
        # A contradiction resolved through the cascade flow would simply
        # never be reviewed. (Round-1 reviewer's blocker 2.)
        #
        # ON A DEDICATED CONTRADICTION-ONLY EVENT, not the node's pre-existing
        # ones. The first version of this fix reused `resolved_events`, on the
        # reasoning that invariant 7 forbids opening a new event. That was
        # wrong, and wrong in the way this PR keeps being wrong: it reported
        # success for a write that silently did not happen.
        #
        # MEASURED (round-2 reviewer, reproduced here): with `ob -> d1`,
        # `ob -> d2` and a contradiction between d1 and d2, retract `ob`, then
        # resolve d1 and then d2. Resolving d1 marks the ct under event E.
        # Resolving d2 writes NOTHING — `UNIQUE(node_id, cascade_event)` plus
        # `INSERT OR IGNORE` make the second write a silent no-op — while the
        # response still listed the ct in `marked_contradictions`. A marker
        # row records ONE (root_node, root_op) pair, so one event id simply
        # cannot carry two different nodes' resolutions of the same
        # contradiction. If the ct's marker had already been adjudicated, d2's
        # revision would never have reached dream-fairy-2 at all; if d2 was
        # retracted, the ct would keep d1's `root_op: supersede` and classify
        # as stale rather than tainted.
        #
        # WHY A FRESH EVENT DOES NOT VIOLATE INVARIANT 7. What invariant 7
        # and §6 protect is the DERIVATION graph: a second event there would
        # mark a provably-covered set and force every downstream node to be
        # adjudicated twice before reaching "fully resolved" (invariant 9).
        # This event marks ONLY contradiction nodes — no claim-bearing node
        # gets a marker from it, so none of that applies. The contradiction
        # cascade was never covered by the covered-set proof in the first
        # place (that is blocker 2's whole finding), so it has no claim on
        # the no-new-event rule either.
        marked_contradictions = []
        contradiction_event = None
        if resolution_kind in ("retracted", "superseded"):
            _root_op = "retract" if resolution_kind == "retracted" else "supersede"
            _cts = [
                ct_id for ct_id in _affected_contradiction_ids(conn, node_id)
                if conn.execute(
                    "SELECT 1 FROM nodes WHERE id = ? AND is_current = 1", (ct_id,)
                ).fetchone()
            ]
            if _cts:
                contradiction_event = _new_cascade_event()
                for ct_id in _cts:
                    _write_cascade_marker(
                        conn, ct_id, contradiction_event, node_id, _root_op, now
                    )
                    # Same audit trail the ordinary retract/supersede paths
                    # write. Round-2 reviewer measured the divergence: the
                    # resolution path left ct edit_history at ['created']
                    # where the ordinary path leaves ['created',
                    # 'stale_flagged'].
                    core._log_edit(
                        conn,
                        "stale_flagged" if _root_op == "supersede" else "tainted",
                        ct_id, "contradiction",
                        {"cascade_via": "contradicts_edge",
                         "via_resolution_of": node_id},
                    )
                    marked_contradictions.append(ct_id)

        recompute = _cascade_recompute_confidence_transitive(conn, node_id)
        auto_resolved = _cascade_auto_resolve_trivial(conn, node_id, now)
        # An auto-resolution changes nothing about a claim (that is what makes
        # it trivial) but the confidence algebra still has to follow the
        # premise's own recomputed value — §4's recompute is unconditional,
        # "however its marker resolves".
        for a in auto_resolved:
            sub = _cascade_recompute_confidence_transitive(conn, a["node_id"])
            recompute["changed"].extend(sub["changed"])
            if "convergence_warning" in sub:
                recompute["convergence_warning"] = sub["convergence_warning"]
        conn.commit()
    finally:
        conn.close()

    response = {
        "node_id": node_id,
        "resolution_kind": resolution_kind,
        "resolved_events": resolved_events,
        "resolved_marker_count": len(resolved_events),
        "affected_premises": affected,
        "eligible_shapes": eligibility["eligible"],
        "confidence_recompute": recompute,
        "auto_resolved_trivial": auto_resolved,
        "marked_contradictions": marked_contradictions,
        "contradiction_cascade_event": contradiction_event,
        "acknowledgement_edges": ack_edges,
        "note": note or None,
        "re_cascade": (
            "SUPPRESSED by design for the DERIVATION cascade — no "
            "claim-bearing node was marked by this resolution, and no "
            "claim-bearing node's adjudication count changed. (A "
            "contradiction-only event IS opened when this node has "
            "contradictions — see contradiction_cascade_event and "
            "marked_contradictions; it marks no claim-bearing node, so the "
            "rule below does not reach it.) Everything downstream of this "
            "node already carries the "
            "original event's pending markers, and invariant 4 forbids a new "
            "descendant appearing while the node was pending, so a second "
            "event would mark a provably-covered set (cascade-machine design: "
            "no re-cascade during resolution). "
            "Confidence propagation is the OPPOSITE case and DID run "
            "transitively — see confidence_recompute."
        ),
    }
    if delegated is not None:
        response["delegated_to"] = (
            "_supersede_impl" if resolution_kind == "superseded"
            else "_retract_impl"
        )
        response["delegated_result"] = delegated
    return json.dumps(response)


# ---------------------------------------------------------------------------
# Shared cascade walker (PR-AA — cascade-walker SSoT refactor)
# ---------------------------------------------------------------------------


def _walk_cascade_downstream(conn, node_id, *, visited, on_live_node):
    """Walk downstream from node_id following engram_core.CASCADE_RELATIONS
    edges (the relations whose EDGE_CLASSIFICATIONS entry sets cascade=True —
    today derives_from and acknowledged_derives_from, the latter
    cascade-bearing by design per #2013 §12.5; the constant is the source of
    truth, not this list). supported_by lost its cascade flag in #2252
    Stage 1.3, and the W6 pairing edge predicted_by was minted cascade:False
    from the start, so neither ever reaches this walk. Visits each
    downstream node at most once. For each live
    claim-bearing node visited, calls on_live_node(node) — the caller
    decides what to write. Stops cascade at retracted/superseded nodes
    (node not is_current) AND at any non-claim-bearing (NCB) node — an NCB
    node has no claim to re-adjudicate, so cascade reaching it accomplishes
    nothing (#2252 W0). Cornerstones/lessons are the vote-accumulator
    *instance* of this rule, not the rule itself.

    Visit pattern (Lei 2026-05-28): visited check is at the top of the call,
    BEFORE on_live_node fires. This guarantees on_live_node fires exactly
    once per downstream node regardless of how many paths reach it. The
    caller seeds the cascade root into `visited` before calling so the root
    isn't re-processed.
    """
    if node_id in visited:
        return
    visited.add(node_id)

    node = conn.execute(
        "SELECT id, type, claim, metadata FROM nodes "
        "WHERE id = ? AND is_current = 1",
        (node_id,),
    ).fetchone()
    if not node:
        return  # retracted/superseded — stop cascade (parity with old _flag_stale)
    if not core._is_claim_bearing(node["type"]):
        return  # NCB: no claim to re-adjudicate, so nothing to taint (#2252 W0)

    on_live_node(node)

    dependents = conn.execute(
        "SELECT source_id FROM edges "
        f"WHERE target_id = ? AND relation IN ({_CASCADE_RELATION_PLACEHOLDERS})",
        (node_id, *_CASCADE_RELATIONS_SORTED),
    ).fetchall()
    for d in dependents:
        _walk_cascade_downstream(conn, d["source_id"],
                                 visited=visited, on_live_node=on_live_node)


# ---------------------------------------------------------------------------
# Zero-support detection helper (§2.3)
# ---------------------------------------------------------------------------


def _detect_zero_support(
    conn: sqlite3.Connection, affected_node_id: str
) -> "tuple[list[dict], list[dict]]":
    """Find cornerstones/lessons whose live-support state changed due to a
    cascade operation settling (retract or supersede).

    After a retract or supersede settles (affected node is already is_current=0),
    check each cs/ls that had an edge to/from the affected node and recount its
    total live support (via supported_by TARGET or exemplifies SOURCE):
      - count == 0 and not already marked → mark support_lost, log 'support_lost'.
      - count > 0 and currently marked → clear support_lost, log
        'support_lost_cleared' (#2252 Stage 1.1 Part 2).

    This write-time clear is a CONVENIENCE, not the load-bearing discharge —
    it only fires when a retract/supersede happens to touch this cs/ls's
    immediate neighborhood again. The state-based recompute in
    engram_lifecycle._reflect_impl (Stage 1.1 Part 1) is what actually
    guarantees discharge: it re-evaluates every marked node's live-support
    count on every reflect run regardless of whether any event fired here,
    which is also the only way to repair a marker whose setting event
    predates this code (e.g. a marker set before Stage 1.1 existed at all).
    Do not treat this hook as sufficient by itself.

    Edge direction (§3):
      - supported_by: cs/ls ─[supported_by]→ premise. COUNT where TARGET is_current=1.
      - exemplifies:  premise ─[exemplifies]→ cs/ls. COUNT where SOURCE is_current=1.

    Args:
        conn: Open DB connection with write access (caller commits).
        affected_node_id: The node that was retracted or superseded (already is_current=0).

    Returns:
        Tuple (newly_support_lost, newly_support_restored) — each a list of
        dicts [{id, type, claim}].
    """
    # Step 1: find all cs/ls that may be affected.
    # Case 1: affected node was the TARGET of a supported_by edge (cs/ls → affected).
    case1 = conn.execute(
        """SELECT e.source_id
           FROM edges e
           JOIN nodes n ON n.id = e.source_id
           WHERE e.target_id = ?
             AND e.relation = 'supported_by'
             AND n.type IN ('cornerstone', 'lesson')""",
        (affected_node_id,),
    ).fetchall()

    # Case 2: affected node was the SOURCE of an exemplifies edge (affected → cs/ls).
    case2 = conn.execute(
        """SELECT e.target_id
           FROM edges e
           JOIN nodes n ON n.id = e.target_id
           WHERE e.source_id = ?
             AND e.relation = 'exemplifies'
             AND n.type IN ('cornerstone', 'lesson')""",
        (affected_node_id,),
    ).fetchall()

    affected_cs_ls_ids = set()
    for row in case1:
        affected_cs_ls_ids.add(row["source_id"])
    for row in case2:
        affected_cs_ls_ids.add(row["target_id"])

    if not affected_cs_ls_ids:
        return [], []

    # Step 2: for each affected cs/ls, recount live support.
    newly_support_lost = []
    newly_support_restored = []
    for cs_ls_id in affected_cs_ls_ids:
        cs_ls_node = conn.execute(
            "SELECT id, type, claim, metadata, is_current FROM nodes WHERE id = ?",
            (cs_ls_id,),
        ).fetchone()
        if not cs_ls_node or not cs_ls_node["is_current"]:
            continue  # Only count live cs/ls nodes

        total_live = core._count_live_exemplars(conn, cs_ls_id, cs_ls_node["type"])
        meta = json.loads(cs_ls_node["metadata"] or "{}")

        if total_live == 0:
            # Mark support_lost in metadata.
            if not meta.get("support_lost"):
                meta["support_lost"] = True
                conn.execute(
                    "UPDATE nodes SET metadata = ? WHERE id = ?",
                    (json.dumps(meta), cs_ls_id),
                )
                core._log_edit(conn, "support_lost", cs_ls_id, cs_ls_node["type"],
                          {"trigger": affected_node_id})
                newly_support_lost.append({
                    "id": cs_ls_id,
                    "type": cs_ls_node["type"],
                    "claim": cs_ls_node["claim"],
                })
        elif meta.get("support_lost"):
            # #2252 Stage 1.1 Part 2 — cheap write-time clear: this retract/
            # supersede settling happened to touch cs_ls_id's neighborhood
            # again and support is live again. See the module-level note in
            # this function's docstring for why this is a convenience, not
            # the load-bearing discharge.
            meta.pop("support_lost", None)
            conn.execute(
                "UPDATE nodes SET metadata = ? WHERE id = ?",
                (json.dumps(meta), cs_ls_id),
            )
            core._log_edit(conn, "support_lost_cleared", cs_ls_id, cs_ls_node["type"],
                      {"trigger": affected_node_id})
            newly_support_restored.append({
                "id": cs_ls_id,
                "type": cs_ls_node["type"],
                "claim": cs_ls_node["claim"],
            })

    return newly_support_lost, newly_support_restored


# ---------------------------------------------------------------------------
# E impls
# ---------------------------------------------------------------------------

def _resolve_impl(
    target_id: str = "",
    resolving_node_id: str = "",
    prediction_outcome: str = "",
) -> str:
    """Internal implementation — see engram_resolve MCP tool for the public
    payload schema. Kept callable with named kwargs for in-server callers.

    Pure-wire semantics (issue #229, 2026-05-20):
      - Validates target is a resolvable type (question, contradiction,
        prediction, conjecture, goal_tension) and current.
      - Validates resolving_node is claim-bearing (observation, derivation,
        theory, axiom, conjecture) and current.
      - For predictions/conjectures: prediction_outcome required.
      - Inserts the resolves edge from resolving_node_id → target_id.
      - Computes target.status from MAX(confidence) across all current,
        non-retracted, non-superseded resolves of the target. This prevents
        regression (a low-confidence later resolve cannot downgrade a
        target previously resolved by a high-confidence resolver).
      - Idempotent on duplicate (resolving_node_id, target_id, 'resolves')
        — returns no_op without re-writing.
      - Flags target.needs_tightening=1 when a real status change occurs and
        the target already carries a recall_summary (#1777, #1952 PR3): a
        summary authored while the node was open ("Open: ...") goes stale the
        moment status flips, and nothing previously re-derived it. This is
        the interim/invalidate fix (an on-demand LLM regenerate inside a
        synchronous MCP handler has no precedent in this codebase) — the
        flagged node then surfaces in the batch-summary fairy's
        reviewer-of-outliers cohort so a correct summary gets authored on
        the next sleep cycle. A target with no recall_summary yet
        (recall_summary IS NULL — already summary_missing) is left alone;
        there is nothing stale to flag.

    Why pure-wire (#229 design):
      The old combo create-and-wire made it cheap to wrap prior weak
      resolution chains in new derivations. the chain-dilution contradiction-resolution saga locked through 7
      attempts this way (chain confidence diluting 0.585 → 0.392 → 0.384
      → 0.376 → 0.368 → 0.361) until the recall-summary derivation finally cited the root
      nodes (the strict-text-reading observation + the canonical saga-resolving derivation, both conf 0.95) and produced conf 0.931.
      Forcing callers to compose the resolving derivation explicitly via
      engram_derive makes citing roots the natural pattern.

    Args:
        target_id: The node ID to resolve.
        resolving_node_id: A claim-bearing node that resolves the target.
        prediction_outcome: Required for predictions/conjectures.

    Returns:
        JSON with the wired edge, target's new status, and the resolving
        node's confidence.
    """
    RESOLVABLE_TYPES = {"question", "contradiction", "prediction", "conjecture", "goal_tension"}
    VALID_PREDICTION_OUTCOMES = {"confirmed", "partially_confirmed", "refuted", "partially_refuted"}
    VALID_CONJECTURE_OUTCOMES = {"supported", "refuted", "inconclusive"}

    if not target_id or not target_id.strip():
        return json.dumps({"error": "target_id is required and cannot be empty."})
    if not resolving_node_id or not resolving_node_id.strip():
        return json.dumps({"error": "resolving_node_id is required and cannot be empty."})
    if target_id == resolving_node_id:
        return json.dumps({"error": "target_id and resolving_node_id must differ."})

    conn = core._get_db()
    try:
        target = conn.execute(
            "SELECT id, type, claim, status, is_current, recall_summary FROM nodes WHERE id = ?",
            (target_id,),
        ).fetchone()
        if not target:
            return json.dumps({"error": f"Target node '{target_id}' not found."})
        if target["type"] not in RESOLVABLE_TYPES:
            return json.dumps({
                "error": f"Protocol violation: can only resolve questions, contradictions, "
                         f"predictions, conjectures, or goal tensions. "
                         f"Node '{target_id}' is type '{target['type']}'.",
            })
        if target["is_current"] != 1:
            # #919: the docstring always promised this guard ("validates
            # target is a resolvable type AND CURRENT") but it was never
            # implemented — resolving a superseded/retracted target corrupts
            # resolved-state bookkeeping on a node that is no longer the live
            # one. Symmetric with the resolver-side guard below. The error
            # walks the supersede chain so a late-arriving legitimate
            # resolution has a one-step path to the right target.
            successor_id = core._current_successor(conn, target_id)
            if successor_id:
                hint = (
                    f"Its current successor is '{successor_id}' — resolve "
                    f"that node instead."
                )
            else:
                hint = (
                    "It has no current successor (retracted, or the "
                    "supersede chain ends non-current) — there is nothing "
                    "live to resolve."
                )
            return json.dumps({
                "error": f"Target node '{target_id}' is not current "
                         f"(superseded or retracted). {hint}",
            })

        resolver = conn.execute(
            "SELECT id, type, claim, confidence, is_current, status FROM nodes WHERE id = ?",
            (resolving_node_id,),
        ).fetchone()
        if not resolver:
            return json.dumps({"error": f"Resolving node '{resolving_node_id}' not found."})
        if not core._is_claim_bearing(resolver["type"]):
            return json.dumps({
                "error": f"Protocol violation: resolving node must be claim-bearing "
                         f"(observation, derivation, theory, axiom, conjecture). "
                         f"'{resolving_node_id}' is type '{resolver['type']}'. "
                         f"Compose a derivation via engram_derive first, then call "
                         f"engram_resolve to wire it.",
            })
        if resolver["is_current"] != 1:
            return json.dumps({
                "error": f"Resolving node '{resolving_node_id}' is not current "
                         f"(superseded or retracted). Cannot use as a resolution. "
                         f"Use the current canonical replacement instead.",
            })

        if target["type"] == "prediction":
            if not prediction_outcome:
                return json.dumps({
                    "error": "Predictions require prediction_outcome: "
                             "confirmed, partially_confirmed, partially_refuted, or refuted.",
                })
            if prediction_outcome not in VALID_PREDICTION_OUTCOMES:
                return json.dumps({
                    "error": f"Invalid prediction_outcome '{prediction_outcome}'. "
                             f"Must be one of: {', '.join(sorted(VALID_PREDICTION_OUTCOMES))}",
                })
        if target["type"] == "conjecture":
            if not prediction_outcome:
                return json.dumps({
                    "error": "Conjectures require prediction_outcome: "
                             "supported, refuted, or inconclusive.",
                })
            if prediction_outcome not in VALID_CONJECTURE_OUTCOMES:
                return json.dumps({
                    "error": f"Invalid conjecture outcome '{prediction_outcome}'. "
                             f"Must be one of: {', '.join(sorted(VALID_CONJECTURE_OUTCOMES))}",
                })

        # Idempotency: if this (resolver, target, 'resolves') edge already
        # exists, don't append a duplicate. Returning early also prevents
        # double utility-reward and double _log_edit for repeat calls.
        existing_edge = conn.execute(
            "SELECT id FROM edges WHERE source_id = ? AND target_id = ? AND relation = 'resolves'",
            (resolving_node_id, target_id),
        ).fetchone()
        if existing_edge:
            # Self-heal: if resolved_by is NULL (pre-#759 state or any prior
            # gap), compute and write it now. This makes re-calling engram_resolve
            # on an already-resolved contradiction idempotent AND corrective.
            # Only run if NULL — skip recomputation when already set.
            current_resolved_by = conn.execute(
                "SELECT resolved_by FROM nodes WHERE id = ?", (target_id,)
            ).fetchone()
            if current_resolved_by and current_resolved_by["resolved_by"] is None:
                best = core._best_resolver_for(conn, target_id)
                if best is not None:
                    conn.execute(
                        "UPDATE nodes SET resolved_by = ? WHERE id = ?",
                        (best["id"], target_id),
                    )
                    conn.commit()
            return json.dumps({
                "status": "no_op",
                "message": (
                    f"Edge ({resolving_node_id} → {target_id}, resolves) already exists. "
                    f"Target status: {target['status']}."
                ),
                "target_id": target_id,
                "resolving_node_id": resolving_node_id,
                "target_status": target["status"],
            })

        confidence = resolver["confidence"] if resolver["confidence"] is not None else 1.0
        config = json.loads(core.CONFIG_PATH.read_text()) if core.CONFIG_PATH.exists() else {}
        threshold = config.get("resolution_confidence_threshold", core.DEFAULT_RESOLUTION_THRESHOLD)

        now = core._now()
        conn.execute(
            "INSERT INTO edges (source_id, target_id, relation, created_at) VALUES (?, ?, 'resolves', ?)",
            (resolving_node_id, target_id, now),
        )

        # Compute status from MAX(confidence) across all current, non-retracted
        # resolvers (including the one we just wired). This prevents regression:
        # a later weak resolver cannot downgrade a target previously resolved
        # by a stronger one. For predictions/conjectures the categorical
        # outcome (prediction_outcome) wins last-writer regardless — the
        # outcome IS the status, no confidence projection.
        if target["type"] == "prediction":
            resolution_status = prediction_outcome
            best_resolver_id = resolving_node_id  # latest-wired for categorical outcomes
        elif target["type"] == "conjecture":
            resolution_status = prediction_outcome
            best_resolver_id = resolving_node_id  # latest-wired for categorical outcomes
        else:
            # Fetch the argmax-confidence current resolver (ORDER BY desc, LIMIT 1) so
            # that resolved_by always points at the strongest resolver, not the latest-wired
            # one. Uses the shared core._best_resolver_for helper (same query as the no_op
            # self-heal and _backfill_resolved_by paths).
            best_row = core._best_resolver_for(conn, target_id)
            max_conf = best_row["confidence"] if best_row and best_row["confidence"] is not None else confidence
            best_resolver_id = best_row["id"] if best_row else resolving_node_id
            if max_conf >= threshold:
                resolution_status = "resolved"
            else:
                resolution_status = "partially_resolved"

        # #1777: a status flip makes any existing recall_summary potentially
        # stale (it may have been authored while the node was still open —
        # "Open: ..." — and nothing re-derives it here). Flag needs_tightening
        # so the batch-summary fairy's reviewer-of-outliers cohort picks it up
        # next sleep cycle. Only meaningful when a summary actually exists;
        # a target with recall_summary IS NULL is already correctly tracked
        # via summary_missing and needs no additional flag.
        target_needs_tightening = 1 if target["recall_summary"] else 0
        conn.execute(
            "UPDATE nodes SET status = ?, resolved_by = ?, needs_tightening = ? WHERE id = ?",
            (resolution_status, best_resolver_id, target_needs_tightening, target_id),
        )
        core._log_edit(conn, "resolved", target_id, target["type"],
                  {"resolved_by": best_resolver_id, "resolution_status": resolution_status})

        core._utility_reward(conn, [target_id, resolving_node_id], action="resolve")
        conn.commit()

        result = {
            "status": "resolution_created",
            "target_id": target_id,
            "target_type": target["type"],
            "resolving_node_id": resolving_node_id,
            "resolving_node_type": resolver["type"],
            "confidence": confidence,
            "resolution_status": resolution_status,
        }
        # Safety: predictions and conjectures cannot reach this branch with
        # resolution_status == "partially_resolved". Their status is set from
        # prediction_outcome (lines above), which is validated against
        # VALID_PREDICTION_OUTCOMES / VALID_CONJECTURE_OUTCOMES — neither set
        # contains "partially_resolved". The discipline_hint is threshold-gate
        # specific and only applies to question/contradiction/goal_tension types
        # that use the MAX(confidence) >= threshold path (the else branch above).
        if resolution_status == "partially_resolved":
            result["note"] = (
                f"No current resolver of '{target_id}' reaches the resolution "
                f"threshold {threshold:.2f}. The {target['type']} remains "
                f"partially resolved. To strengthen, compose a derivation "
                f"via engram_derive that cites high-confidence root nodes, "
                f"then call engram_resolve again to wire it."
            )
            result["discipline_hint"] = _THRESHOLD_GATE_DISCIPLINE_HINT

        return json.dumps(result)
    finally:
        conn.close()


def _supersede_impl(
    old_node_id: str = "",
    new_node_id: str = "",
    supersede_reason: str = "",
    cascade_triggering: bool = True,
) -> str:
    """Internal implementation — see engram_supersede MCP tool for the public
    payload schema. Kept callable with named kwargs for in-server callers.

    Mark `new_node_id` as the successor of `old_node_id`.

    This is a purely relational operation — it does NOT create a new node.
    The caller creates the replacement via the canonical creation tool for
    its type (engram_derive, engram_add_observation, engram_add_axiom,
    engram_add_definition, engram_add_goal, engram_add_conjecture,
    engram_add_cornerstone, …) and then calls this tool with both IDs.

    The canonical creation tools are the single source of truth for each
    node type's confidence math, metadata, edges, and importance anchoring.
    Keeping supersede relational-only means those rules never diverge.

    Supersede no-drop discipline (the supersede-no-drop discipline, 2026-05-20):
      The new node MUST preserve every load-bearing claim of the old node.
      For each load-bearing claim in old, the only legitimate per-claim
      moves are: keep unchanged in new, alter in new (refine / sharpen /
      reframe / correct), or retract-separately via engram_retract on the
      specific part. Drop is forbidden — supersede sets old.is_current=0,
      and the old node disappears from queryable engram, so any unpreserved
      claim is silently lost.

    Atomic claims are unambiguous. Multi-claim nodes require explicit
    per-claim review before supersede.

    cascade_triggering (INTERNAL — not reachable from the MCP surface; the
    engram_supersede payload allowlist does not carry this field, and a test
    pins that): True (default) opens a NEW cascade event and marks
    everything downstream. False suppresses that, for the ONE caller that
    needs it — _cascade_resolve_impl, where this supersede IS the
    resolution of an existing cascade rather than a fresh revision. See the
    comment at the guard itself for why re-cascading there marks a provably
    covered set (#2013 §10.2 / §6 / invariant 7). Suppressing the cascade
    does NOT suppress _resolve_own_markers: the node's own pending markers
    still resolve, which is the whole point of the resolution path.

    Cascade behavior (parallel cascades for derivation and contradiction
    dependents; #2013/#2009 slice 1 — the cascade machine):
      - Derivation cascade: every node reachable via CASCADE_RELATIONS
        edges (engram_core.CASCADE_RELATIONS — today derives_from and
        acknowledged_derives_from; supported_by lost its cascade
        flag in #2252 Stage 1.3) gets a pending cascade_markers row
        (root_node=old_node_id, root_op='supersede'). The walk stops at
        non-claim-bearing nodes, so an NCB dependent gets no marker at
        all. The marker row carries no severity column — severity is
        derived at resolution time from root_op, per the 2026-08-26
        amendment; see engram_core.py's cascade_markers table docstring
        for the schema.
      - Contradiction cascade (issue #229): every contradiction node that
        has old as a contradicting side gets marked with the SAME
        cascade_event too.
        Dream-fairy-2 reviews each stale contradiction and decides whether
        the supersede substantively resolved the contradiction (case 1:
        wire engram_resolve from new_node to ct) or preserved the conflict
        (case 2: new-contradict between new_node + other side, then
        supersede old_ct → new_ct). Per the no-drop discipline, case 3
        (orthogonal supersede) cannot arise — new either kept or altered
        the conflicting claim; dropping is forbidden.

    Workflow:
      1. Draft the corrected/refined claim and create the new node with
         the right creation tool — pass the FULL explicit support list
         for the new node (no inheritance from the old — cite previous
         premises organically if they still apply).
      2. Call engram_supersede(old_id, new_id) to wire the supersede
         relationship: old is marked is_current=0, new.supersedes = old,
         a supersedes edge is inserted, and downstream dependents of old
         are flagged stale.

    recall_summary staleness (#1777, #1952 PR3 — checked, not assumed): unlike
    engram_resolve, supersede needs no recall_summary/needs_tightening
    handling here. old.is_current flips to 0, and every recall/surface/FTS
    path that renders recall_summary already gates on is_current=1 (see
    render_one_node_line, the nodes_fts sync triggers, _embedding_text_for_node
    callers) — a superseded node's stale summary is simply never surfaced
    again. new_node_id is a freshly-created node under its own creation
    tool's write-time recall_summary contract (#1952 PR1), so it already
    carries a current summary (or is correctly flagged summary_missing if its
    author omitted one). Confirmed empirically, not by symmetry with resolve.

    Validation:
      - Both nodes must exist and be is_current=1 (no resurrecting
        superseded/retracted nodes).
      - old.type must equal new.type (cross-type "promotion" is not
        supersede — use engram_retract + create a fresh node instead).
      - new.created_at must be >= old.created_at (DAG invariant).
      - new.supersedes must be NULL (a node can't already be the
        successor of a different supersede chain).
      - old.type cannot be feeling_report (terminal type — file a new
        report or use engram_retract).

    Evidence nodes (ev_XXXX) are supersede-capable in principle but
    usually the wrong tool — if a URL is wrong or a source is bad, use
    engram_retract and cite corrected evidence via a new observation.

    Args:
        old_node_id: The node being superseded.
        new_node_id: The replacement node, already created via its
            type's canonical creation tool.
        supersede_reason: Optional short rationale for WHY this
            revision exists (e.g., "reasoning_type correction:
            inductive_generalization → authority_expert", "narrower
            claim, original was over-generalized"). Lands in the
            supersedes edge's metadata column. The claim diff usually
            speaks for itself; add a reason only when the diff alone
            wouldn't make the motivation obvious.

    Returns:
        JSON with old_node_id, new_node_id, the stale downstream list,
        stale_count, plus stale_contradictions (contradictions cascaded
        via the contradicts-edge walk) and stale_contradiction_count.
    """
    if not old_node_id or not old_node_id.strip():
        return json.dumps({"error": "old_node_id is required and cannot be empty."})
    if not new_node_id or not new_node_id.strip():
        return json.dumps({"error": "new_node_id is required and cannot be empty."})

    conn = core._get_db()
    try:
        old = conn.execute(
            "SELECT id, type, is_current FROM nodes WHERE id = ?",
            (old_node_id,),
        ).fetchone()
        if not old:
            return json.dumps({"error": f"old_node_id '{old_node_id}' not found."})
        if old["is_current"] != 1:
            return json.dumps({
                "error": f"old_node_id '{old_node_id}' is not current "
                         f"(already superseded or retracted). "
                         f"Use engram_history or engram_inspect to see its state.",
            })
        if old["type"] == "feeling_report":
            return json.dumps({
                "error": "Protocol violation: feeling reports cannot be superseded. "
                         "A feeling report is a record of a moment, not a revisable claim. "
                         "If the report was filed in error, use engram_retract. "
                         "If your understanding has shifted, file a NEW feeling report.",
            })

        # #2343 / #2013 invariant 7 (first sentence): "the tool REFUSES a
        # supersede that supplies no resolution." The PLAIN verb reaching a
        # node that already carries a pending cascade marker is exactly that
        # case — the caller is revising a node whose own premises are still
        # under adjudication. Lei's ruling (2026-08-29): refuse, and point at
        # mode C so the WHOLE chain up to this node is adjudicated through
        # engram_cascade_resolve, rather than letting the plain verb clear
        # this node's own marker while the rest of the cascade stays
        # unadjudicated (a bypass wearing invariant 7's clothes). The
        # resolution route reaches this function with cascade_triggering=False
        # and is the ONLY legitimate way to supersede a marked node.
        if cascade_triggering:
            _pending_here = core._pending_markers_for_node(conn, old_node_id)
            if _pending_here:
                _events = sorted({m["cascade_event"] for m in _pending_here})
                return json.dumps({
                    "error": f"Node '{old_node_id}' carries {len(_pending_here)} pending "
                             f"cascade marker(s) ({len(_events)} event(s)) — a plain "
                             f"engram_supersede is refused on a node whose own "
                             f"premises are still under adjudication (cascade-machine "
                             f"invariant 7). Resolve the cascade through the "
                             f"frontier instead: read mode C for this node, "
                             f"adjudicate each frontier entry with "
                             f"engram_cascade_resolve, and when this node is "
                             f"itself on the frontier resolve it as 'superseded' "
                             f"with this replacement.",
                    "pending_events": _events,
                    "next_call": f"engram_cascade_frontier(node_id={old_node_id!r})",
                    "then": f"engram_cascade_resolve(node_id={old_node_id!r}, "
                            f"resolution_kind='superseded', "
                            f"replacement_id={new_node_id!r})",
                })

        new = conn.execute(
            "SELECT id, type, is_current, created_at, supersedes FROM nodes WHERE id = ?",
            (new_node_id,),
        ).fetchone()
        if not new:
            return json.dumps({
                "error": f"new_node_id '{new_node_id}' not found. "
                         f"Create the replacement node via its type's canonical creation "
                         f"tool (engram_derive, engram_add_observation, engram_add_axiom, "
                         f"engram_add_definition, engram_add_goal, engram_add_conjecture, "
                         f"engram_add_cornerstone, …) BEFORE calling engram_supersede.",
            })
        if new["is_current"] != 1:
            return json.dumps({
                "error": f"new_node_id '{new_node_id}' is not current "
                         f"(already superseded or retracted). Cannot use it as a replacement.",
            })
        if new_node_id == old_node_id:
            return json.dumps({"error": "old_node_id and new_node_id must differ."})
        if new["type"] != old["type"]:
            return json.dumps({
                "error": f"Type mismatch: old is '{old['type']}', new is '{new['type']}'. "
                         f"Supersede preserves type — superseding a conjecture with a "
                         f"derivation (or similar cross-type revision) is not supersede. "
                         f"If the correction changes the epistemic category, use "
                         f"engram_retract on the old node and cite the replacement "
                         f"organically in new derivations.",
            })

        # §12.3 GUARDRAIL — the replacement must not be DOWNSTREAM of what it
        # replaces. Superseding a node with something that transitively derives
        # from it is ill-formed on its face: you would be superseding your own
        # transitive premise, and thereby making the replacement stale by the
        # very act that installs it.
        #
        # WHY IT WAS INVISIBLE BEFORE THIS CHECK (measured, #2013 §12.2): the
        # supersede was ACCEPTED, the intervening node got a marker, and the
        # replacement got NONE despite being downstream — it escaped only
        # because the cascade walk below seeds `new_node_id` into `visited`, a
        # guard that exists to stop a BORN-MARKED replacement (invariant 7),
        # an unrelated concern. An ill-formed operation hidden by a mechanism
        # aimed at something else.
        #
        # WHAT THIS CHECK GETS RIGHT, AND WHERE THAT ARGUMENT STOPS. §12.4: the
        # ack edge runs X -> P', so a cycle needs P' -> ... -> X; X carries a
        # marker only if it is downstream of P, so P' deriving from X would put
        # P' downstream of P — exactly what this refuses. That reasoning is
        # sound for a SINGLE supersede/acknowledge pair evaluated in isolation.
        # But this check is a POINT-IN-TIME guard: it runs once, here, at
        # supersede time, over the edge table as it stood THEN. Every
        # acknowledgement written AFTER this call adds an edge to the very
        # relation the argument above was evaluated over, so it does not
        # extend to a SEQUENCE of supersedes and acknowledgements — two
        # individually-legal supersedes plus two acknowledgements can still
        # close a cycle across three nodes (measured, #2013 §12.7). This check
        # alone does not make the ack edge safe; §12.7's write-time guard in
        # `_cascade_record_acknowledgement_edges` (re-checks at the moment
        # each ack edge is actually written, over the graph as it stands then)
        # is what closes the gap this one leaves open.
        if _cascade_reaches(conn, old_node_id, new_node_id):
            return json.dumps({
                "error": f"Cycle guard: '{new_node_id}' transitively derives from "
                         f"'{old_node_id}', so it cannot replace it. Superseding a "
                         f"node with one of its own descendants would make the "
                         f"replacement stale by the act that installs it, and it is "
                         f"the one shape that lets an acknowledgement close a "
                         f"cascade cycle.",
                "guard": "supersede_replacement_is_downstream",
                "old_node_id": old_node_id,
                "new_node_id": new_node_id,
                "guidance": (
                    "The replacement must not rest on the node it replaces. "
                    "Either compose a replacement that does not cite anything "
                    "downstream of the superseded node, or — if the downstream "
                    "claim really is the successor — retract the old node "
                    "WITHOUT naming this node as replacement_id (a retract "
                    "naming the same downstream node is refused by the "
                    "equivalent guard on that path) and let the newer claim "
                    "stand on its own / be cited organically."
                ),
            })
        if new["supersedes"] is not None:
            return json.dumps({
                "error": f"new_node_id '{new_node_id}' already supersedes "
                         f"'{new['supersedes']}'. A node cannot be the successor of "
                         f"more than one supersede chain.",
            })

        old_created = conn.execute(
            "SELECT created_at FROM nodes WHERE id = ?", (old_node_id,)
        ).fetchone()["created_at"]
        if new["created_at"] < old_created:
            return json.dumps({
                "error": f"DAG invariant violation: new_node '{new_node_id}' was created "
                         f"at {new['created_at']}, before old_node '{old_node_id}' "
                         f"created at {old_created}. Supersede requires the replacement "
                         f"to be at least as new as the node it replaces.",
            })

        now = core._now()

        # Wire the supersedes relationship.
        conn.execute(
            "UPDATE nodes SET supersedes = ? WHERE id = ?",
            (old_node_id, new_node_id),
        )
        edge_meta = json.dumps({"reason": supersede_reason}) if supersede_reason else None
        conn.execute(
            "INSERT INTO edges (source_id, target_id, relation, created_at, metadata) "
            "VALUES (?, ?, 'supersedes', ?, ?)",
            (new_node_id, old_node_id, now, edge_meta),
        )
        conn.execute(
            "UPDATE nodes SET is_current = 0, superseded_by = ? WHERE id = ?",
            (new_node_id, old_node_id),
        )

        # Person-supersede: migrate incoming `about` edges to the new node.
        # When superseding a person node, any observation (or other node)
        # linked about→old_node becomes stranded after old is marked non-current.
        # Option A: old edge stays (audit trail); a live edge is inserted on the
        # current node for each source that doesn't already point at new_node.
        # Scope: `about` is the recall-routing person-edge. (A `cites`→person
        # edge via context_ids is annotation-only and intentionally NOT migrated.)
        # Gate on old type only — new is guaranteed the same type by the
        # type-equality validation above.
        if old["type"] == "person":
            about_sources = conn.execute(
                "SELECT source_id FROM edges WHERE target_id = ? AND relation = 'about'",
                (old_node_id,),
            ).fetchall()
            for row in about_sources:
                src = row["source_id"]
                already = conn.execute(
                    "SELECT 1 FROM edges WHERE source_id = ? AND target_id = ? AND relation = 'about'",
                    (src, new_node_id),
                ).fetchone()
                if not already:
                    conn.execute(
                        "INSERT INTO edges (source_id, target_id, relation, created_at)"
                        " VALUES (?, ?, 'about', ?)",
                        (src, new_node_id, now),
                    )
                    # Provenance: record the migrated edge in the audit trail so a
                    # later about-edge on new_node is explainable (mirrors the
                    # stale-cascade _log_edit calls below).
                    core._log_edit(conn, "about_migrated", new_node_id, new["type"],
                              {"about_source_id": src, "migrated_from": old_node_id})

            # Self-anchor transfer: if the superseded person was the agent's
            # self-anchor (metadata.is_self=1, paired with trust_tier='self'),
            # carry BOTH onto the replacement. Supersede has already flipped
            # old.is_current=0; every self-anchor lookup is `is_self=1 AND
            # is_current=1`, so without this transfer a self-anchor supersede
            # would leave ZERO current self-anchors — silently breaking
            # link_about default-targeting, focus='self' emergence-scan, and
            # tier='self' assignment. The add_person singleton guard blocks
            # setting is_self on the replacement at creation time (the old
            # anchor is still current then), and no metadata-edit tool exists,
            # so supersede is the only correct locus for the carry-over.
            #
            # The old node KEEPS its is_self in metadata as a historical
            # snapshot — consistent with how superseded nodes retain all their
            # other metadata; the singleton guard is is_current-scoped, so the
            # "one current self-anchor" invariant holds regardless.
            old_self = conn.execute(
                "SELECT metadata, trust_tier FROM nodes WHERE id = ?",
                (old_node_id,),
            ).fetchone()
            old_self_meta = json.loads(old_self["metadata"]) if old_self["metadata"] else {}
            if old_self_meta.get("is_self"):
                new_row = conn.execute(
                    "SELECT metadata FROM nodes WHERE id = ?", (new_node_id,)
                ).fetchone()
                new_self_meta = json.loads(new_row["metadata"]) if new_row["metadata"] else {}
                new_self_meta["is_self"] = True
                conn.execute(
                    "UPDATE nodes SET metadata = ?, trust_tier = ? WHERE id = ?",
                    (json.dumps(new_self_meta), old_self["trust_tier"], new_node_id),
                )
                core._log_edit(conn, "self_anchor_transferred", new_node_id, new["type"],
                          {"transferred_from": old_node_id,
                           "trust_tier": old_self["trust_tier"]})

            # Migrate incoming `supported_by` edges from cornerstones/lessons.
            # The cascade walker already skips cs/ls nodes (§2.0 skip rule), so
            # they are not stale-marked when a premise is superseded. But the edge
            # still points at the now-non-current person, leaving the cs/ls
            # anchored to a superseded node. Add a parallel edge to the new node.
            # Old edges are preserved as audit trail (same as about migration).
            # See #1209 (Aleph's remaining gap — cs/ls supported_by not rerooted).
            cs_ls_sources = conn.execute(
                "SELECT e.source_id FROM edges e "
                "JOIN nodes n ON n.id = e.source_id "
                "WHERE e.target_id = ? AND e.relation = 'supported_by' "
                "AND n.type IN ('cornerstone', 'lesson') AND n.is_current = 1",
                (old_node_id,),
            ).fetchall()
            for row in cs_ls_sources:
                src = row["source_id"]
                already = conn.execute(
                    "SELECT 1 FROM edges "
                    "WHERE source_id = ? AND target_id = ? AND relation = 'supported_by'",
                    (src, new_node_id),
                ).fetchone()
                if not already:
                    conn.execute(
                        "INSERT INTO edges (source_id, target_id, relation, created_at)"
                        " VALUES (?, ?, 'supported_by', ?)",
                        (src, new_node_id, now),
                    )
                    core._log_edit(conn, "supported_by_migrated", new_node_id, new["type"],
                              {"supported_by_source_id": src, "migrated_from": old_node_id})

        # Flag downstream dependents of the old node as stale.
        # Uses shared walker (_walk_cascade_downstream) — PR-AA SSoT refactor.
        # Cascade machine (#2013/#2009 slice 1): one cascade_event per this
        # call, shared by the derivation-cascade write below AND the
        # contradiction-cascade write further down — "one id shared by every
        # marker written by one revision" (#2013 §2).
        # The old node may itself be carrying pending markers from an
        # EARLIER cascade. Superseding it is one of the two resolution
        # routes that exist before the frontier verb ships, so clear them
        # here — otherwise a marked node stays blocked even after it has
        # been properly re-derived (#2324 spec revision).
        resolved_own = _resolve_own_markers(
            conn, old_node_id, "superseded", now, replacement_id=new_node_id
        )

        # #2013 §10.2 / §6 — "No re-cascade during resolution". When this
        # call IS the resolution of an existing cascade (the
        # engram_cascade_resolve path passes cascade_triggering=False), it
        # must NOT open a NEW cascade event. Invariant 7 states this for the
        # supersede side specifically: such a supersede is "a PARTIAL
        # RESOLUTION of the existing event, never a new one". Everything
        # downstream already carries the original event's pending markers,
        # and invariant 4 (no node is cited as a premise while pending)
        # means no new descendant can have appeared since. The categorical
        # line is marked-vs-unmarked, NOT which verb was called — hence the
        # default True, with only the resolution path passing False.
        stale = []
        stale_contradictions = []
        cascade_event = None
        if cascade_triggering:
            cascade_event = _new_cascade_event()
            visited = {old_node_id}  # seed superseded node so walker won't process it
            visited.add(new_node_id)  # also don't process the replacement

            def _on_live_for_supersede(dep):
                _write_cascade_marker(conn, dep["id"], cascade_event,
                                       old_node_id, "supersede", now)
                stale.append({
                    "id": dep["id"],
                    "type": dep["type"],
                    "claim": dep["claim"],
                    "stale_premise": old_node_id,
                    "replacement": new_node_id,
                })

            # Walk direct dependents of the superseded node.
            direct = conn.execute(
                "SELECT source_id FROM edges "
                f"WHERE target_id = ? AND relation IN ({_CASCADE_RELATION_PLACEHOLDERS})",
                (old_node_id, *_CASCADE_RELATIONS_SORTED),
            ).fetchall()
            for d in direct:
                _walk_cascade_downstream(conn, d["source_id"],
                                         visited=visited,
                                         on_live_node=_on_live_for_supersede)

            # Cascade to contradiction nodes that have old as a contradicting side
            # (issue #229): mark each affected contradiction with the SAME
            # cascade_event so dream-fairy-2 reviews whether the supersede
            # substantively resolved the conflict (case 1: wire engram_resolve
            # from new_node to ct) or preserved it (case 2: new-contradict
            # between new_node + other side, then supersede old contradiction →
            # new contradiction). Per the supersede no-drop discipline (the
            # supersede-no-drop discipline), case 3 (orthogonal supersede)
            # cannot arise — the new node either kept or altered the conflicting
            # claim; dropping is forbidden.
            affected_ct_ids = _affected_contradiction_ids(conn, old_node_id)
            for ct_id in affected_ct_ids:
                ct = conn.execute(
                    "SELECT id, type, claim, status, metadata FROM nodes WHERE id = ? AND is_current = 1",
                    (ct_id,),
                ).fetchone()
                if not ct:
                    continue
                _write_cascade_marker(conn, ct_id, cascade_event,
                                       old_node_id, "supersede", now)
                stale_contradictions.append({
                    "id": ct_id,
                    "type": "contradiction",
                    "claim": ct["claim"],
                    "status": ct["status"],
                    "stale_premise": old_node_id,
                    "replacement": new_node_id,
                })
                core._log_edit(conn, "stale_flagged", ct_id, "contradiction",
                          {"stale_premise": old_node_id, "replacement": new_node_id,
                           "cascade_via": "contradicts_edge"})

        # §2.3 Zero-support detection: after cascade settles, check cs/ls
        # nodes that lost or regained a supporting edge (distinct from
        # stale — audit signal, not cascade). #2252 Stage 1.1 Part 2 adds
        # the restore side; see _detect_zero_support's docstring for why
        # this write-time check is a convenience and not the load-bearing
        # discharge (that's engram_lifecycle._reflect_impl, Part 1).
        support_lost_cs_ls, support_restored_cs_ls = _detect_zero_support(conn, old_node_id)

        core._log_edit(conn, "superseded", old_node_id, old["type"],
                  {"replaced_by": new_node_id, "reason": supersede_reason or None})
        for s in stale:
            core._log_edit(conn, "stale_flagged", s["id"], s["type"],
                      {"stale_premise": old_node_id, "replacement": new_node_id})

        core._utility_reward(conn, [new_node_id], action="supersede")
        conn.commit()

        # --- engram.tool.engram_call event (DESIGN.md §4.2) ---
        # alpha #2574 WS5: site attestation + outcome recording +
        # protection (docs/2574-telemetry-design.md §1/§4/§5) -- new,
        # narrowly-scoped try/except; see engram_derive.py's _derive_impl
        # blocked-path site for the full rationale. This function's own
        # try/finally has no except, so an uncaught raise here would
        # propagate straight out of this MCP tool call, breaching §8.
        _site = "server.engram_supersede"
        try:
            _had_emitter = Emitter.get() is not None
            _uid = emit_if_initialized(
                event_type="engram.tool.engram_call",
                level=1,
                data={
                    "tool_name": "engram_supersede",
                    "target_id": old_node_id,
                    "action_type": "supersede",
                    "result_status": "superseded",
                    "result_node_id": new_node_id,
                },
                site="server.engram_supersede",
            )
            write_telemetry_record(
                manifest_id=_site,
                event_type="engram.tool.engram_call",
                fields={
                    "outcome": (
                        "delivered" if _uid is not None
                        else "dropped" if _had_emitter
                        else "no_emitter"
                    )
                },
            )
        except Exception:
            try:
                write_telemetry_record(
                    manifest_id=_site,
                    event_type="engram.tool.engram_call",
                    fields={"outcome": "init_failed"},
                )
            except Exception:
                pass

        result = {
            "status": "superseded",
            "old_node_id": old_node_id,
            "new_node_id": new_node_id,
            "supersede_reason": supersede_reason or None,
            "stale_downstream": stale,
            "stale_count": len(stale),
            "stale_contradictions": stale_contradictions,
            "stale_contradiction_count": len(stale_contradictions),
        }
        # Surfaced, not merely done: the caller needs to see that this
        # supersede also CLEARED the old node's own pending markers, because
        # that is now the primary way an agent gets a hard-blocked node
        # unblocked (#2324 spec revision). Omitted entirely when empty so
        # the common case reads unchanged.
        if resolved_own:
            result["resolved_own_markers"] = resolved_own
            result["resolved_own_marker_count"] = len(resolved_own)
        if support_lost_cs_ls:
            result["support_lost"] = support_lost_cs_ls
            result["support_lost_count"] = len(support_lost_cs_ls)
        if support_restored_cs_ls:
            result["support_restored"] = support_restored_cs_ls
            result["support_restored_count"] = len(support_restored_cs_ls)
        return json.dumps(result)
    finally:
        conn.close()


def _retract_impl(
    node_id: str = "",
    error_type: str = "",
    reason: str = "",
    replacement_id: str = "",
    replacement_absent_because: str = "",
    replacement_absent_note: str = "",
    initiation: str = "",
    initiation_gate: str = "",
    initiation_ref: str = "",
    cascade_triggering: bool = True,
) -> str:
    """Internal implementation — see engram_retract MCP tool for the public
    payload schema. Kept callable with named kwargs for in-server callers.

    Retract a node that contains an error and flag downstream dependents.

    Unlike engram_supersede (epistemic evolution — old claim was valid at the time),
    engram_retract is for ERROR CORRECTION — the node was never valid. The retracted
    node is preserved for audit but marked as an error. All downstream nodes
    (derivations, theories) that depend on the retracted node are flagged as
    tainted so they can be reviewed and corrected.

    Two refusals guard the target node, checked in order: (1) #2396 — a plain
    retract of a NON-CURRENT node (already superseded or retracted) is refused,
    mirroring _supersede_impl's is_current check; retract the current successor
    or record a contradiction/observation against the historical record instead.
    (2) #2343 / #2013 invariant 7 — a plain retract of a CURRENT node that still
    carries a pending cascade marker is refused and pointed at mode C.

    Cascade behavior (#2013/#2009 slice 1 — the cascade machine):
      - Derivation cascade: every node reachable via CASCADE_RELATIONS
        edges (engram_core.CASCADE_RELATIONS — today derives_from and
        acknowledged_derives_from; supported_by lost its cascade
        flag in #2252 Stage 1.3) gets a pending cascade_markers row
        (root_node=the retracted node, root_op='retract'). The walk stops
        at non-claim-bearing nodes, so an NCB dependent gets no marker at
        all. The marker row carries no severity column — severity is
        derived at resolution time from root_op, per the 2026-08-26
        amendment; see engram_core.py's cascade_markers table docstring
        for the schema.
      - Contradiction cascade (issue #229): every contradiction node that
        has the retracted node as a contradicting side gets marked with
        the SAME cascade_event. Dream-fairy-2 reviews tainted contradictions —
        the retracted side was never valid, so the contradiction itself
        may need to be closed (since one of its claims is now invalid)
        or rewired to a corrected replacement observation if one was
        named via replacement_id.
      - Resolution reopening: if the retracted node was a resolution
        (had outgoing resolves edges), each target without another
        valid resolution is set back to status='open'.
      - Prediction basis retraction (issue #2281): if the retracted node
        is an observation_predictive, any prediction paired to it via a
        predicted_by edge (prediction -> observation; #2252 W6 renamed
        this from supported_by, direction unchanged) gets RETRACTED
        (not merely tainted) unless another live observation_predictive
        still supports it — a voided evidential basis means the
        prediction was never validly grounded. Independent of the
        general derivation cascade above, which (post-#2252 Stage 1.3)
        does not reach predictions at all — supported_by's cascade flag
        is False and predictions are non-claim-bearing, so this is the
        only signal a voided prediction gets.

    Error types:
      fabricated_quote — quoted_text not found in evidence source
      wrong_citation — claim doesn't follow from the quote
      wrong_evidence — cited the wrong source
      hallucinated_claim — claim not supported by evidence
      duplicate — same claim already existed (should have used support_existing)
      other — anything else

    cascade_triggering (INTERNAL — not reachable from the MCP surface; the
    engram_retract payload allowlist does not carry this field, and a test
    pins that): True (default) opens a NEW cascade event and marks
    everything downstream. False suppresses that, for the ONE caller that
    needs it — _cascade_resolve_impl, where this retract IS the resolution
    of an existing cascade rather than a fresh error correction. See the
    comment at the guard itself for the covered-set argument (#2013 §10.2 /
    §6). The node's own pending markers still resolve either way.

    Args:
        node_id: The node to retract.
        error_type: One of: fabricated_quote, wrong_citation, wrong_evidence,
            hallucinated_claim, duplicate, other.
        reason: Human-readable explanation of the error.
        replacement_id: The replacement node's id, already created via its
            type's canonical creation tool (engram_add_observation, for
            observations) BEFORE calling engram_retract — a purely relational
            operation, it does NOT create a new node (matches
            _supersede_impl's new_node_id convention). Must name a node that
            exists, is_current=1, differs from node_id, and is not older than
            node_id (the retracts edge it lands on is temporal_causal=True).
            Checked for claim-bearing parity against the retracted node
            (#2613 §7): _is_claim_bearing(replacement) must equal
            _is_claim_bearing(node_id) — a floor, not same-kind; permits
            conjecture->derivation, refuses task->observation.
        replacement_absent_because (str): REQUIRED when replacement_id is
            not supplied (#2613) — the stated reason nothing replaces this
            claim. One of VALID_REPLACEMENT_ABSENT_REASONS
            (NONE_BECAUSE_NO_SUCCESSOR, NONE_BECAUSE_DUPLICATE,
            NONE_BECAUSE_SUPERSEDED_ELSEWHERE, NONE_BECAUSE_UNKNOWN).
            Mutually exclusive with replacement_id — supply exactly one.
        replacement_absent_note (str): free-text note, REQUIRED iff
            replacement_absent_because='NONE_BECAUSE_UNKNOWN' (the escape
            hatch costs a sentence). Recorded on the retraction event in its
            own field — deliberately NOT folded into `reason`, which is
            truncated at [:200] (#2613 §4's "the hazard").
        initiation (str, optional): Who initiated this retraction. One of:
            user_prompted — triggered by explicit user request;
            gate_prompted — triggered by an internal gate (see initiation_gate);
            unprompted — self-initiated without external prompt.
        initiation_gate (str, optional): Which gate triggered the retraction
            (e.g. 'honesty_axiom', 'deference_detector'). Meaningful only
            when initiation='gate_prompted'.
        initiation_ref (str, optional): Session ID at retract-time (auto-
            captured by the server wrapper; not a user-supplied field).
            Allows contingent-verification of the initiation tier while the
            session transcript lives.

    Returns:
        JSON with retraction details, tainted downstream nodes, and optional replacement.
    """
    if not node_id or not node_id.strip():
        return json.dumps({"error": "node_id is required and cannot be empty."})
    if not error_type or not error_type.strip():
        return json.dumps({"error": "error_type is required and cannot be empty."})
    if not reason or not reason.strip():
        return json.dumps({"error": "reason is required and cannot be empty."})

    if error_type not in VALID_ERROR_TYPES:
        return json.dumps({
            "error": f"Invalid error_type '{error_type}'. Must be one of: {', '.join(sorted(VALID_ERROR_TYPES))}"
        })

    if initiation and initiation not in VALID_INITIATION_TYPES:
        return json.dumps({
            "error": f"Invalid initiation '{initiation}'. Must be one of: {', '.join(sorted(VALID_INITIATION_TYPES))}"
        })

    if initiation_gate and initiation != "gate_prompted":
        return json.dumps({
            "error": "initiation_gate is only valid when initiation='gate_prompted'. "
                     f"Got initiation='{initiation or '(unset)'}' with initiation_gate='{initiation_gate}'."
        })

    conn = core._get_db()
    try:
        node = conn.execute(
            "SELECT * FROM nodes WHERE id = ?",
            (node_id,),
        ).fetchone()
        if not node:
            return json.dumps({"error": f"Node '{node_id}' not found."})

        # #2396: mirror _supersede_impl's :2128 refusal — a plain retract of a
        # NON-CURRENT node is refused, UNGATED by cascade_triggering (same as
        # supersede's sibling check). Without this, retracting an already-
        # superseded/retracted node reopens #2343's double-adjudication
        # through a second door and is not idempotent (retract twice -> two
        # markers on every descendant, unbounded). Real-graph incidence of
        # "both superseded and retracted": zero in 8454 nodes. The steelman
        # (retract-after-supersede as a genuine "I later learned it was
        # fabricated" reclassification) is real but has never occurred; wait
        # for a real instance and design against that, rather than building
        # machinery for a case that practice has never produced.
        #
        # This is intentionally NOT gated on cascade_triggering. The only
        # cascade_triggering=False caller is _cascade_resolve_impl, whose
        # targets are frontier nodes; frontier membership requires a pending
        # cascade marker, and every path that writes a cascade_markers row
        # filters is_current = 1 (_walk_cascade_downstream's WHERE clause,
        # the contradiction-cascade fetches, and the #2324 backfill's source
        # query) — so the resolution route never passes a non-current node
        # today. If a future change ever made it do so, refusing would be
        # the correct outcome, not a regression.
        if node["is_current"] != 1:
            _actual_status = "retracted" if node["status"] == "retracted" else "superseded"
            _successor = node["superseded_by"]
            # Named parts for readability (#2396 round 2, refined round 4).
            # _remedy branches on whether a successor exists: with one,
            # retract-the-successor and contradict-the-successor are both
            # actionable (engram_contradict itself refuses a non-current
            # target and redirects to the successor, so pointing there
            # directly matches what the next call will actually accept);
            # with none (a purely-retracted node has no successor),
            # "retract the successor" is not actionable advice and neither
            # is contradiction (engram_contradict refuses unconditionally
            # with no redirect) — offer only the observation remedy.
            _state = f"is not current (already {_actual_status})" + (
                f", superseded by '{_successor}'" if _successor else ""
            )
            _why = (
                f"A plain engram_retract on a non-current node is refused — "
                f"retracting an already-{_actual_status} node would reopen "
                f"descendants that were already adjudicated "
                f"and is not idempotent."
            )
            if _successor:
                _remedy = (
                    f"To fix this: retract the current successor "
                    f"'{_successor}' instead, or, if the point is that this "
                    f"historical record itself was invalid, record a "
                    f"contradiction against its successor '{_successor}' "
                    f"(engram_contradict refuses a non-current target and "
                    f"will redirect you there anyway), or file an "
                    f"observation (engram_add_observation) against the "
                    f"record."
                )
            else:
                _remedy = (
                    f"To fix this: if the point is that this historical "
                    f"record itself was invalid, file an observation "
                    f"(engram_add_observation) against the record — "
                    f"engram_contradict is not an option here, since it "
                    f"refuses any non-current target outright and this "
                    f"node has no successor to redirect to."
                )
            _result = {
                "error": f"Node '{node_id}' {_state}. {_why} {_remedy}",
                "next_call": f"engram_history(node_id={node_id!r})",
            }
            if _successor:
                _result["successor_id"] = _successor
            return json.dumps(_result)

        # #2343 / #2013 invariant 7: same refusal as _supersede_impl — a PLAIN
        # retract of a node that already carries a pending cascade marker is
        # refused and pointed at mode C; the resolution route (cascade_
        # triggering=False, via engram_cascade_resolve) is the only way to
        # retract a marked node. See the sibling comment in _supersede_impl.
        # The `and node["is_current"] == 1` clause that used to guard this
        # check is now unnecessary rather than merely dead (#2395): the
        # refusal immediately above already excludes non-current input, so
        # every node that reaches this line is already known to be current.
        if cascade_triggering:
            _pending_here = core._pending_markers_for_node(conn, node_id)
            if _pending_here:
                _events = sorted({m["cascade_event"] for m in _pending_here})
                return json.dumps({
                    "error": f"Node '{node_id}' carries {len(_pending_here)} pending "
                             f"cascade marker(s) ({len(_events)} event(s)) — a plain "
                             f"engram_retract is refused on a node whose own "
                             f"premises are still under adjudication (cascade-machine "
                             f"invariant 7). Resolve the cascade through the "
                             f"frontier instead: read mode C for this node, "
                             f"adjudicate each frontier entry with "
                             f"engram_cascade_resolve, and when this node is "
                             f"itself on the frontier resolve it as 'retracted'.",
                    "pending_events": _events,
                    "next_call": f"engram_cascade_frontier(node_id={node_id!r})",
                    "then": f"engram_cascade_resolve(node_id={node_id!r}, "
                            f"resolution_kind='retracted', "
                            f"error_type={error_type!r})",
                })

        # #2613: the replacement decision is required-always — every
        # author-initiated retraction must record either the node that
        # replaces the claim, or a stated reason why nothing does. This is
        # the ONE chokepoint (docs/2613-retraction-replacement-contract.md
        # §3): both engram_retract and engram_cascade_resolve's retracted
        # branch delegate here, so enforcing it in this one place gives both
        # paths the obligation by construction. NOT gated on
        # cascade_triggering — the resolution path must satisfy this
        # exactly like the organic path; a default threaded in from the
        # caller (the anti-pattern §3 names, error_type="other") would
        # satisfy the letter while nobody decided anything.
        #
        # Deliberately checked HERE — after the non-current-node refusal and
        # the pending-cascade-marker refusal above, not up front with
        # node_id/error_type/reason — so a caller who names an unretractable
        # node gets THAT specific, actionable refusal (with its successor/
        # frontier guidance) rather than a generic "you forgot a field"
        # message that would be true but useless: no decision they supplied
        # would have made the retraction possible anyway.
        replacement_id = (replacement_id or "").strip()
        replacement_absent_because = (replacement_absent_because or "").strip()
        replacement_absent_note = (replacement_absent_note or "").strip()

        if replacement_id and replacement_absent_because:
            return json.dumps({
                "error": "replacement_id and replacement_absent_because are "
                         "mutually exclusive — supply exactly one: either the "
                         "node that replaces this claim, or the reason nothing "
                         "does."
            })
        if not replacement_id and not replacement_absent_because:
            return json.dumps({
                "error": "A replacement decision is required (#2613): either "
                         "replacement_id (naming the node that replaces this "
                         "claim, already created via its own canonical creation "
                         "tool) or replacement_absent_because (one of: "
                         f"{', '.join(sorted(VALID_REPLACEMENT_ABSENT_REASONS))}) "
                         "explaining why nothing does.",
            })
        if replacement_absent_because and replacement_absent_because not in VALID_REPLACEMENT_ABSENT_REASONS:
            return json.dumps({
                "error": f"Invalid replacement_absent_because '{replacement_absent_because}'. "
                         f"Must be one of: {', '.join(sorted(VALID_REPLACEMENT_ABSENT_REASONS))}"
            })
        if replacement_absent_because == "NONE_BECAUSE_UNKNOWN" and not replacement_absent_note:
            return json.dumps({
                "error": "replacement_absent_because='NONE_BECAUSE_UNKNOWN' "
                         "requires replacement_absent_note explaining what "
                         "could not be determined — the escape hatch is the one "
                         "member that must name itself; the other three do not.",
            })

        # #2612: validate replacement_id up front, before any mutation — a
        # purely relational reference to a node the caller already created,
        # mirroring _supersede_impl's equivalent checks on new_node_id.
        if replacement_id:
            _repl_node = conn.execute(
                "SELECT id, is_current, created_at, type FROM nodes WHERE id = ?",
                (replacement_id,),
            ).fetchone()
            if not _repl_node:
                return json.dumps({
                    "error": f"replacement_id '{replacement_id}' not found. "
                             f"Create the replacement node via its type's canonical "
                             f"creation tool (engram_add_observation, engram_derive, "
                             f"engram_add_axiom, engram_add_definition, engram_add_goal, "
                             f"engram_add_conjecture, engram_add_cornerstone, …) BEFORE "
                             f"calling engram_retract.",
                })
            if _repl_node["is_current"] != 1:
                return json.dumps({
                    "error": f"replacement_id '{replacement_id}' is not current "
                             f"(already superseded or retracted). Cannot use it as a "
                             f"replacement.",
                })
            if replacement_id == node_id:
                return json.dumps({"error": "node_id and replacement_id must differ."})
            # core._now() is datetime.now(timezone.utc).isoformat(), uniformly
            # offset-aware, so plain string comparison here is sound — the
            # hazard is a future format change, not the comparison itself.
            if _repl_node["created_at"] < node["created_at"]:
                return json.dumps({
                    "error": f"replacement_id '{replacement_id}' was created at "
                             f"{_repl_node['created_at']}, before node_id '{node_id}' "
                             f"created at {node['created_at']}. The 'retracts' relation "
                             f"is temporal_causal — the replacement must be at least as "
                             f"new as the node it replaces.",
                })
            # §12.3 GUARDRAIL, ported from _supersede_impl (see that guard's
            # comment for the full cycle-closing argument) — the replacement
            # must not be DOWNSTREAM of the node being retracted. Naming a
            # pre-existing node as replacement_id (post-#2612) makes this
            # reachable in a way inline-minting never was: an inline-minted
            # node had no derives_from edges yet, so it could never be
            # downstream of anything. A pre-existing node can be.
            if _cascade_reaches(conn, node_id, replacement_id):
                return json.dumps({
                    "error": f"Cycle guard: '{replacement_id}' transitively derives "
                             f"from '{node_id}', so it cannot replace it. Retracting "
                             f"a node with one of its own descendants named as "
                             f"replacement would make the replacement stale by the "
                             f"act that installs it, and it is the one shape that "
                             f"lets an acknowledgement close a cascade cycle.",
                    "guard": "retract_replacement_is_downstream",
                    "node_id": node_id,
                    "replacement_id": replacement_id,
                    "guidance": (
                        "The replacement must not rest on the node it retracts. "
                        "Either name a replacement_id that does not cite anything "
                        "downstream of the retracted node, or — if the downstream "
                        "claim really is the correction — retract this node "
                        "WITHOUT a replacement_id and let the newer claim stand "
                        "on its own / be cited organically."
                    ),
                })

            # #2613 §7: claim-bearing PARITY, not an unconditional predicate
            # and not same-kind — a floor. _supersede_impl already refuses
            # any type mismatch (stricter than parity), so this check is
            # retract-only. Rejects the task->observation case that motivated
            # §7 (a non-claim-bearing node standing in for a claim-bearing
            # one); permits conjecture->derivation, which is the honest shape
            # for a cross-epistemic-category correction (_supersede_impl's
            # own error text routes exactly that case here, to retract).
            _target_cb = core._is_claim_bearing(node["type"])
            _repl_cb = core._is_claim_bearing(_repl_node["type"])
            if _target_cb != _repl_cb:
                return json.dumps({
                    "error": f"replacement_id '{replacement_id}' (type "
                             f"'{_repl_node['type']}', claim-bearing={_repl_cb}) "
                             f"does not match '{node_id}' (type "
                             f"'{node['type']}', claim-bearing={_target_cb}) "
                             f"on claim-bearing parity (#2613 §7). A "
                             f"replacement must be claim-bearing iff the "
                             f"node it replaces is — this is a floor, not a "
                             f"same-kind requirement (a conjecture may be "
                             f"replaced by a derivation).",
                    "guard": "retract_replacement_claim_bearing_parity",
                    "node_id": node_id,
                    "replacement_id": replacement_id,
                })

        now = core._now()

        # Self-anchor guard: retracting a person self-anchor leaves zero current
        # self-anchors — silently breaking link_about default-targeting,
        # focus='self' emergence-scan, and trust_tier='self' assignment.
        # Detect now (before the retract flips is_current=0) so we can warn.
        _retract_node_meta = json.loads(node["metadata"] or "{}")
        _self_anchor_retracted = (
            node["type"] == "person"
            and bool(_retract_node_meta.get("is_self"))
        )

        # 1. Mark the node as retracted
        retract_meta = json.loads(node["metadata"] or "{}")
        retract_meta["retracted"] = True
        retract_meta["error_type"] = error_type
        retract_meta["retraction_reason"] = reason
        retract_meta["retracted_at"] = now
        if initiation:
            retract_meta["initiation"] = initiation
        if initiation_gate:
            retract_meta["initiation_gate"] = initiation_gate
        if initiation_ref:
            retract_meta["initiation_ref"] = initiation_ref

        conn.execute(
            "UPDATE nodes SET status = 'retracted', is_current = 0, metadata = ? WHERE id = ?",
            (json.dumps(retract_meta), node_id),
        )

        # 1b. If retracted node was a resolution, reopen the resolved target.
        # A resolution creates a 'resolves' edge from the derivation to the
        # target (question, contradiction, prediction, conjecture, goal_tension).
        # Retracting the resolution should revert the target to 'open' so it
        # can be re-resolved with proper provenance.
        reopened_targets = []
        retract_config = json.loads(core.CONFIG_PATH.read_text()) if core.CONFIG_PATH.exists() else {}
        retract_threshold = retract_config.get("resolution_confidence_threshold", core.DEFAULT_RESOLUTION_THRESHOLD)
        resolution_edges = conn.execute(
            "SELECT target_id FROM edges WHERE source_id = ? AND relation = 'resolves'",
            (node_id,),
        ).fetchall()
        for re in resolution_edges:
            tid = re["target_id"]
            # Check if there's another non-retracted resolution for this target.
            # Also fetch confidence to find the strongest remaining resolver for
            # the resolved_by column update (issue #762).
            other_resolutions = conn.execute(
                """SELECT e.source_id, n.confidence FROM edges e
                   JOIN nodes n ON e.source_id = n.id
                   WHERE e.target_id = ? AND e.relation = 'resolves'
                   AND n.id != ? AND n.status != 'retracted' AND n.is_current = 1
                   ORDER BY n.confidence DESC""",
                (tid, node_id),
            ).fetchall()
            if not other_resolutions:
                # No other valid resolution — reopen the target and clear resolved_by.
                conn.execute(
                    "UPDATE nodes SET status = 'open', resolved_by = NULL WHERE id = ?",
                    (tid,),
                )
                target_node = conn.execute(
                    "SELECT type, claim FROM nodes WHERE id = ?", (tid,)
                ).fetchone()
                reopened_targets.append({
                    "id": tid,
                    "type": target_node["type"] if target_node else "unknown",
                    "claim": target_node["claim"] if target_node else "",
                })
            else:
                # Other resolvers remain — update resolved_by to the strongest one
                # and recompute status vs threshold (issue #762 coherence fix).
                # Predictions/conjectures use categorical outcome as status, so their
                # status is not recomputed — only the resolved_by pointer is updated.
                strongest = other_resolutions[0]
                target_type_row = conn.execute(
                    "SELECT type FROM nodes WHERE id = ?", (tid,)
                ).fetchone()
                target_type = target_type_row["type"] if target_type_row else ""
                if target_type in ("prediction", "conjecture"):
                    conn.execute(
                        "UPDATE nodes SET resolved_by = ? WHERE id = ?",
                        (strongest["source_id"], tid),
                    )
                else:
                    max_remaining = strongest["confidence"] if strongest["confidence"] is not None else 0.0
                    new_status = "resolved" if max_remaining >= retract_threshold else "partially_resolved"
                    conn.execute(
                        "UPDATE nodes SET resolved_by = ?, status = ? WHERE id = ?",
                        (strongest["source_id"], new_status, tid),
                    )

        # Audit trail: log each edge removal before the DELETE so the
        # deletions are visible via engram_history mode=edits.
        # Note: this runs AFTER the reopening loop above — that loop reads the
        # edges to decide which targets to reopen; this deletion uses the same
        # set of edges as its scope but issues them after the reads complete.
        for re in resolution_edges:
            # target_type omitted from data dict — JOIN against nodes at audit-read time
            # if needed; avoids per-edge SELECT during retract cascade.
            core._log_edit(conn, "edge_removed", node_id, node["type"],
                      {"relation": "resolves", "target_id": re["target_id"]})

        # Delete the resolves edges sourced from the retracted node.
        conn.execute(
            "DELETE FROM edges WHERE source_id = ? AND relation = 'resolves'",
            (node_id,),
        )

        # 1c. If the retracted node is an observation_predictive, retract any
        # prediction whose sole live evidential basis it was (issue #2281).
        # A prediction is minted paired to an observation_predictive via a
        # `predicted_by` edge (prediction --predicted_by--> observation,
        # engram_observation.py's decomposition step; #2252 W6 renamed this
        # from `supported_by`, direction unchanged). If that basis is
        # retracted and no OTHER live observation_predictive still supports
        # the same prediction, the prediction was never validly grounded —
        # void ab initio, not merely tainted. `retract` (not `supersede`) is
        # the correct terminal state per the retract/supersede split (the
        # prediction was never valid, it wasn't valid-then-updated); none of
        # the four VALID_PREDICTION_OUTCOMES fit either, since they all
        # answer "did the predicted event happen", a different question from
        # "was the basis ever real".
        #
        # Deliberately independent of the general downstream-cascade walk
        # below (_walk_cascade_downstream): #2252 Stage 1.3 flips
        # `supported_by`'s cascade flag to False (predictions are
        # non-claim-bearing, so cascade reaching one has nothing to
        # re-adjudicate) and, separately, the walker stops at any
        # non-claim-bearing node regardless of the edge's cascade flag. So
        # the general cascade neither taints nor retracts a prediction
        # through this edge — this branch is the only signal a voided
        # prediction gets.
        retracted_predictions = []
        if node["type"] == "observation_predictive":
            # #2252 W6: pairing relation read from the derived constant
            # (core.PREDICTION_PAIRING_RELATIONS), never a hand-listed
            # 'supported_by' literal — the pairing edge was split to
            # `predicted_by` and this query is the regression signal for a
            # missed rename (a prediction whose sole basis was retracted
            # would silently stop getting retracted in turn).
            _pred_pairing_placeholders = ",".join("?" * len(core.PREDICTION_PAIRING_RELATIONS))
            supporting_predictions = conn.execute(
                f"SELECT source_id FROM edges "
                f"WHERE target_id = ? AND relation IN ({_pred_pairing_placeholders}) "
                f"AND source_id IN (SELECT id FROM nodes WHERE type = 'prediction')",
                (node_id, *sorted(core.PREDICTION_PAIRING_RELATIONS)),
            ).fetchall()
            for sp in supporting_predictions:
                pred_id = sp["source_id"]
                # Skip if another live observation_predictive still grounds
                # this same prediction (a prediction can be re-minted onto
                # by multiple predictive observations describing the same
                # predicted_event — engram_observation.py's
                # "linked_to_existing" path). Type-filtered on the target
                # to match the outer supporting_predictions query's own
                # type filter — unreachable by any non-observation_predictive
                # target today (predicted_by edges onto a prediction are
                # only ever minted by that one decomposition path), but the
                # filter keeps the query honest against what the comment
                # claims rather than relying on that invariant holding
                # silently (Borges, PR #2292 colleague review).
                #
                # This is also why engram_supersede cannot orphan a
                # validly-grounded prediction, despite supersede's own
                # supported_by-edge migration being restricted to
                # cornerstones/lessons (see the migration site elsewhere in
                # this module): a supersede's replacement observation mints
                # its OWN predicted_by edge via this same linked_to_existing
                # path, so the prediction always re-acquires a live basis
                # from the replacement rather than depending on the old
                # edge being migrated (verified, Borges, PR #2292 colleague
                # review — a hypothesized false-positive here was built and
                # then disproved empirically).
                # #2252 W6: same derived-constant discipline as the
                # supporting_predictions query above — never a hand-listed
                # 'supported_by' literal.
                other_basis = conn.execute(
                    f"""SELECT 1 FROM edges e JOIN nodes n ON e.target_id = n.id
                       WHERE e.source_id = ? AND e.relation IN ({_pred_pairing_placeholders})
                       AND e.target_id != ? AND n.is_current = 1
                       AND n.type = 'observation_predictive'
                       LIMIT 1""",
                    (pred_id, *sorted(core.PREDICTION_PAIRING_RELATIONS), node_id),
                ).fetchone()
                if other_basis:
                    continue
                pred = conn.execute(
                    "SELECT id, status, metadata, is_current FROM nodes WHERE id = ?",
                    (pred_id,),
                ).fetchone()
                if not pred or not pred["is_current"]:
                    continue  # already retracted/superseded — nothing to do
                pred_meta = json.loads(pred["metadata"] or "{}")
                pred_meta["retracted"] = True
                pred_meta["error_type"] = error_type
                pred_meta["retraction_reason"] = (
                    f"Evidential basis retracted: paired observation_predictive "
                    f"{node_id} was retracted ({error_type}): {reason}"
                )
                pred_meta["retracted_at"] = now
                pred_meta["retracted_via_basis_retraction"] = node_id
                # A prediction can already have resolved (status a
                # RESOLVED_STATUSES value — confirmed/refuted/etc, engram_
                # core.py) before its basis is retracted after the fact.
                # Overwriting status='retracted' displaces that completed
                # falsification measurement from the live surface (it
                # survives in edit_history and the resolves edge stays
                # intact, but isn't visible without digging into the audit
                # log) — record it so it's recoverable from the node itself
                # (Borges, PR #2292 colleague review).
                if pred["status"] and pred["status"] != "open":
                    pred_meta["status_before_retraction"] = pred["status"]
                conn.execute(
                    "UPDATE nodes SET status = 'retracted', is_current = 0, metadata = ? WHERE id = ?",
                    (json.dumps(pred_meta), pred_id),
                )
                # #2613 §9: this is the MACHINE-WRITTEN retraction event the
                # invariant carves out — no author named a decision for this
                # specific node, so it is out of the "every author-initiated
                # retraction records a decision" population (the
                # `cascade_via` key is what a scan filters on to exclude it).
                # It inherits the ROOT's decision for display only, never as
                # its own recorded judgement.
                _pred_event_details = {
                    "error_type": error_type,
                    "reason": f"basis retracted: {node_id}"[:200],
                    "cascade_via": "prediction_basis_retraction",
                }
                if replacement_id:
                    _pred_event_details["inherited_replacement_id"] = replacement_id
                if replacement_absent_because:
                    _pred_event_details["inherited_replacement_absent_because"] = replacement_absent_because
                core._log_edit(conn, "retracted", pred_id, "prediction", _pred_event_details)
                retracted_predictions.append({"id": pred_id, "type": "prediction"})

        # 2. Find all downstream nodes via CASCADE_RELATIONS (the constant in
        #    engram_core is the source of truth for the set — supported_by lost
        #    its cascade flag in #2252 Stage 1.3, so it no longer reaches
        #    anything through this walk).
        # Uses shared walker (_walk_cascade_downstream) — PR-AA SSoT refactor.
        # Cascade machine (#2013/#2009 slice 1): one cascade_event per this
        # call, shared by the derivation-cascade write below AND the
        # contradiction-cascade write further down.
        # Same as supersede's: the node being retracted may itself carry
        # pending markers from an earlier cascade. Retraction is the other
        # resolution route available before the frontier verb (#2324 spec
        # revision) — no replacement, because a retracted node was
        # withdrawn rather than fixed.
        resolved_own = _resolve_own_markers(conn, node_id, "retracted", now)

        # #2013 §10.2 / §6 — "No re-cascade during resolution". When this
        # call IS the resolution of an existing cascade (the
        # engram_cascade_resolve path passes cascade_triggering=False), it
        # must NOT open a NEW cascade event. Everything downstream of this
        # node already carries the original event's pending markers, and
        # invariant 4 (no node is cited as a premise while pending) means it
        # cannot have acquired new descendants while pending — so a second
        # event would mark a provably-covered set, and would additionally
        # force every one of those nodes to be adjudicated twice before it
        # could reach "fully resolved" (invariant 9). An UNMARKED node
        # reached through the normal path still fires a normal cascade,
        # which is why the default is True and only the resolution path
        # passes False.
        #
        # SCOPE (#2343, read this before generalising): the "no new event"
        # rule above is a property of the RESOLUTION PATH only. It is NOT a
        # licence for the plain verb to supersede a MARKED node quietly —
        # the plain verb REFUSES a marked node (guard above; #2013 invariant
        # 7 first sentence, "the tool refuses a supersede that supplies no
        # resolution"). An earlier wording here — "the categorical line is
        # marked-vs-unmarked, NOT which verb was called" — was read by two
        # seats on 2026-08-29 as exactly that licence, and nearly shipped an
        # accept-and-resolve bypass. The line that matters is: marked ⇒ the
        # resolve verb is the only route; unmarked ⇒ plain verb, normal
        # cascade.
        tainted = []
        tainted_contradictions = []
        cascade_event = None
        if cascade_triggering:
            cascade_event = _new_cascade_event()
            visited = {node_id}  # seed retracted node so walker won't process it

            def _on_live_for_retract(dep):
                _write_cascade_marker(conn, dep["id"], cascade_event,
                                       node_id, "retract", now)
                tainted.append({
                    "id": dep["id"],
                    "type": dep["type"],
                    "claim": dep["claim"],
                })

            # Walk direct dependents of the retracted node.
            direct = conn.execute(
                "SELECT source_id FROM edges "
                f"WHERE target_id = ? AND relation IN ({_CASCADE_RELATION_PLACEHOLDERS})",
                (node_id, *_CASCADE_RELATIONS_SORTED),
            ).fetchall()
            for d in direct:
                _walk_cascade_downstream(conn, d["source_id"],
                                         visited=visited,
                                         on_live_node=_on_live_for_retract)

            # Cascade to contradiction nodes that have the retracted node as a
            # contradicting side (issue #229): mark each affected contradiction
            # with the SAME cascade_event so dream-fairy-2 reviews whether the
            # contradiction itself is still valid (the retracted side was never
            # valid; if the other side stands alone now, the contradiction may
            # itself need supersede / explicit close).
            affected_ct_ids = _affected_contradiction_ids(conn, node_id)
            for ct_id in affected_ct_ids:
                ct = conn.execute(
                    "SELECT id, type, claim, status, metadata FROM nodes WHERE id = ? AND is_current = 1",
                    (ct_id,),
                ).fetchone()
                if not ct:
                    continue
                _write_cascade_marker(conn, ct_id, cascade_event,
                                       node_id, "retract", now)
                tainted_contradictions.append({
                    "id": ct_id,
                    "type": "contradiction",
                    "claim": ct["claim"],
                    "status": ct["status"],
                })
                core._log_edit(conn, "tainted", ct_id, "contradiction",
                          {"tainted_by": node_id, "cascade_via": "contradicts_edge"})

        # §2.3 Zero-support detection: after cascade settles, check cs/ls
        # nodes that lost or regained a supporting edge (distinct from
        # tainted — audit signal, not cascade). #2252 Stage 1.1 Part 2 adds
        # the restore side; see _detect_zero_support's docstring for why
        # this write-time check is a convenience and not the load-bearing
        # discharge (that's engram_lifecycle._reflect_impl, Part 1).
        support_lost_cs_ls, support_restored_cs_ls = _detect_zero_support(conn, node_id)

        # Log retraction and taint events.
        # #2613 §3 change 3 / §5: the replacement decision is recorded HERE,
        # on the retraction event itself — not only as the (optional)
        # `retracts` edge below. This is what makes §9's invariant
        # ("checkable against the event log") true, and what lets §5's
        # frontier-summary root-read work: a cascade ROOT carries no
        # cascade_markers row (markers are written for affected DOWNSTREAM
        # nodes, never for the node whose retraction created the cascade),
        # so the decision has to live somewhere a node ALWAYS has — the
        # event — for a later reader (or _cascade_dead_premise_detail) to
        # find it when this node is itself a dead premise.
        _retract_event_details = {"error_type": error_type, "reason": reason[:200]}
        if replacement_id:
            _retract_event_details["replacement_id"] = replacement_id
        if replacement_absent_because:
            _retract_event_details["replacement_absent_because"] = replacement_absent_because
        if replacement_absent_note:
            _retract_event_details["replacement_absent_note"] = replacement_absent_note
        core._log_edit(conn, "retracted", node_id, node["type"], _retract_event_details)
        for t in tainted:
            core._log_edit(conn, "tainted", t["id"], t["type"],
                      {"tainted_by": node_id})
        for rt in reopened_targets:
            core._log_edit(conn, "reopened", rt["id"], rt["type"],
                      {"reason": f"resolution {node_id} retracted"})

        # 3. Optionally record the replacement edge — purely relational
        # (#2612: the replacement node itself was already created by the
        # caller via its own canonical creation tool, and validated above).
        # Written on the same connection, in the retraction's own
        # transaction, so "retraction done, replacement edge missing" is not
        # a reachable partial state.
        if replacement_id:
            try:
                conn.execute(
                    "INSERT INTO edges (source_id, target_id, relation, created_at) VALUES (?, ?, 'retracts', ?)",
                    (replacement_id, node_id, now),
                )
            except sqlite3.IntegrityError as _edge_exc:
                # #2613 §11 / Lei's root-cause rule (2026-09-07): a bare
                # `pass` here let a retraction COMMIT while its own event
                # (just logged above) reports a replacement whose edge was
                # never written — silent partial success, the exact shape
                # this contract exists to eliminate. Fail loud: roll back
                # the WHOLE retraction rather than accept a retraction whose
                # replacement_id names an edge that does not exist. Nothing
                # committed yet (conn.commit() is below this block), so the
                # rollback discards the status update, the event just
                # logged, and everything else in this call — an all-or-
                # nothing retraction, not a partial one.
                conn.rollback()
                return json.dumps({
                    "error": f"Failed to record the 'retracts' edge from "
                             f"'{replacement_id}' to '{node_id}': {_edge_exc}. "
                             f"The retraction was NOT applied — no state "
                             f"changed. (#2613 §11: a retraction that commits "
                             f"while its replacement edge silently fails to "
                             f"write is exactly the defect this refusal "
                             f"prevents.)",
                })

        conn.commit()

        result = {
            "status": "retracted",
            "node_id": node_id,
            "error_type": error_type,
            "reason": reason,
            "tainted_downstream": tainted,
            "tainted_count": len(tainted),
            "tainted_contradictions": tainted_contradictions,
            "tainted_contradiction_count": len(tainted_contradictions),
        }
        # Same as supersede's — see the note there. Retracting a marked node
        # is the other pre-frontier-verb resolution route.
        if resolved_own:
            result["resolved_own_markers"] = resolved_own
            result["resolved_own_marker_count"] = len(resolved_own)
        if initiation:
            result["initiation"] = initiation
        if initiation_gate:
            result["initiation_gate"] = initiation_gate
        if initiation_ref:
            result["initiation_ref"] = initiation_ref
        if support_lost_cs_ls:
            result["support_lost"] = support_lost_cs_ls
            result["support_lost_count"] = len(support_lost_cs_ls)
        if support_restored_cs_ls:
            result["support_restored"] = support_restored_cs_ls
            result["support_restored_count"] = len(support_restored_cs_ls)
        if reopened_targets:
            result["reopened_targets"] = reopened_targets
            result["reopened_count"] = len(reopened_targets)
        if retracted_predictions:
            result["retracted_predictions"] = retracted_predictions
            result["retracted_predictions_count"] = len(retracted_predictions)
        if replacement_id:
            result["replacement_id"] = replacement_id
        if _self_anchor_retracted:
            result["warning"] = (
                "Retracted node was the agent's self-anchor (is_self=True). "
                "The graph now has zero current self-anchors. "
                "engram_link_about default-targeting, focus='self' emergence-scan, "
                "and trust_tier='self' assignment are broken until a new person node "
                "is created with is_self=True (or an existing one is promoted)."
            )

        # --- engram.tool.engram_call event (DESIGN.md §4.2) ---
        # alpha #2574 WS5: see engram_revision.py's _supersede_impl site
        # (above, this file) for the full protection/outcome-recording
        # rationale (identical shape here).
        _site = "server.engram_retract"
        try:
            _had_emitter = Emitter.get() is not None
            _uid = emit_if_initialized(
                event_type="engram.tool.engram_call",
                level=1,
                data={
                    "tool_name": "engram_retract",
                    "target_id": node_id,
                    "action_type": "retract",
                    "result_status": "retracted",
                    "result_node_id": replacement_id or None,
                },
                site="server.engram_retract",
            )
            write_telemetry_record(
                manifest_id=_site,
                event_type="engram.tool.engram_call",
                fields={
                    "outcome": (
                        "delivered" if _uid is not None
                        else "dropped" if _had_emitter
                        else "no_emitter"
                    )
                },
            )
        except Exception:
            try:
                write_telemetry_record(
                    manifest_id=_site,
                    event_type="engram.tool.engram_call",
                    fields={"outcome": "init_failed"},
                )
            except Exception:
                pass

        return json.dumps(result)
    finally:
        conn.close()
