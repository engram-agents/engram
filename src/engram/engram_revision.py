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

Caller dependency for ``_retract_impl`` replacement path:
  ``_retract_impl`` accepts an optional ``_obs_creator`` callable (default None).
  server.py passes ``_add_observation_impl`` when constructing the retraction
  call, keeping the acyclic family → core dependency intact (family modules must
  not import server.py).  When ``_obs_creator`` is None the replacement step is
  skipped — same behaviour as if ``replacement_json`` were absent.

House rules (wave pattern):
  - Shared state ONLY via ``import engram_core as core`` + call-time ``core.X``.
  - Never import from server.py (acyclic: server → family → core).
  - Stateless beyond constants.
"""

from __future__ import annotations

import json
import sqlite3

import engram_core as core

from engram_log_emitter import emit_if_initialized


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

def _add_stale_replacement(meta: dict, old_node_id: str, new_node_id: str) -> None:
    """Add an entry to metadata.stale_replacement dict, upgrading from legacy
    scalar if needed. Mutates meta in place.

    Writers always emit dict-keyed form {old_node_id: new_node_id}. If a legacy
    scalar exists (pre-PR #231), it is preserved under a "_legacy" sentinel key
    so downstream readers can still surface it. Coexists indefinitely; no
    one-shot migration. See issue #231.
    """
    stale_repl = meta.get("stale_replacement", {})
    if isinstance(stale_repl, str):
        stale_repl = {"_legacy": stale_repl}
    elif not isinstance(stale_repl, dict):
        stale_repl = {}
    stale_repl[old_node_id] = new_node_id
    meta["stale_replacement"] = stale_repl


# ---------------------------------------------------------------------------
# Shared cascade walker (PR-AA — cascade-walker SSoT refactor)
# ---------------------------------------------------------------------------


def _walk_cascade_downstream(conn, node_id, *, visited, on_live_node):
    """Walk downstream from node_id following derives_from / supported_by
    edges. Visits each downstream node at most once. For each live non-cs/ls
    node visited, calls on_live_node(node) — the caller decides what to
    write. Stops cascade at retracted/superseded nodes (node not is_current)
    AND at cornerstones/lessons (vote-accumulator semantics — §2.0 of
    PR-A-SPEC).

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
    if node["type"] in ("cornerstone", "lesson"):
        return  # cs/ls skip rule (PR-A-SPEC §2.0)

    on_live_node(node)

    dependents = conn.execute(
        "SELECT source_id FROM edges "
        "WHERE target_id = ? AND relation IN ('derives_from', 'supported_by')",
        (node_id,),
    ).fetchall()
    for d in dependents:
        _walk_cascade_downstream(conn, d["source_id"],
                                 visited=visited, on_live_node=on_live_node)


# ---------------------------------------------------------------------------
# Zero-support detection helper (§2.3)
# ---------------------------------------------------------------------------


def _detect_zero_support(conn: sqlite3.Connection, affected_node_id: str) -> list[dict]:
    """Find cornerstones/lessons that lost all live support due to a cascade operation.

    After a retract or supersede settles (affected node is already is_current=0),
    check each cs/ls that had an edge to/from the affected node. If the total count
    of live supporting nodes (via supported_by TARGET or exemplifies SOURCE) is zero,
    mark the cs/ls as support_lost in metadata and log the event.

    Edge direction (§3):
      - supported_by: cs/ls ─[supported_by]→ premise. COUNT where TARGET is_current=1.
      - exemplifies:  premise ─[exemplifies]→ cs/ls. COUNT where SOURCE is_current=1.

    Args:
        conn: Open DB connection with write access (caller commits).
        affected_node_id: The node that was retracted or superseded (already is_current=0).

    Returns:
        List of dicts [{id, type, claim}] for each cs/ls newly marked support_lost.
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
        return []

    # Step 2: for each affected cs/ls, recount live support.
    newly_support_lost = []
    for cs_ls_id in affected_cs_ls_ids:
        cs_ls_node = conn.execute(
            "SELECT id, type, claim, metadata, is_current FROM nodes WHERE id = ?",
            (cs_ls_id,),
        ).fetchone()
        if not cs_ls_node or not cs_ls_node["is_current"]:
            continue  # Only count live cs/ls nodes

        total_live = core._count_live_exemplars(conn, cs_ls_id, cs_ls_node["type"])

        if total_live == 0:
            # Mark support_lost in metadata.
            meta = json.loads(cs_ls_node["metadata"] or "{}")
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

    return newly_support_lost


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
            "SELECT id, type, claim, status, is_current FROM nodes WHERE id = ?",
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
        if resolver["type"] not in core.CLAIM_BEARING_TYPES:
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

        conn.execute(
            "UPDATE nodes SET status = ?, resolved_by = ? WHERE id = ?",
            (resolution_status, best_resolver_id, target_id),
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

    Cascade behavior (parallel cascades for derivation and contradiction
    dependents):
      - Derivation cascade: every node that derives_from / supported_by the
        old node gets marked stale_by_premise (metadata.stale_by includes
        old_node_id, metadata.stale_replacement[old_node_id] = new_node_id).
      - Contradiction cascade (issue #229): every contradiction node that
        has old as a contradicting side gets marked stale_by_premise too.
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

    Validation:
      - Both nodes must exist and be is_current=1 (no resurrecting
        superseded/retracted nodes).
      - old.type must equal new.type (cross-type "promotion" is not
        supersede — use retract + create fresh instead).
      - new.created_at must be >= old.created_at (DAG invariant).
      - new.supersedes must be NULL (a node can't already be the
        successor of a different supersede chain).
      - old.type cannot be feeling_report (terminal type — file a new
        report or retract).

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
        visited = {old_node_id}  # seed superseded node so walker won't process it
        visited.add(new_node_id)  # also don't process the replacement
        stale = []

        def _on_live_for_supersede(dep):
            meta = json.loads(dep["metadata"]) if dep["metadata"] else {}
            stale_by = meta.get("stale_by", [])
            if old_node_id not in stale_by:
                stale_by.append(old_node_id)
            meta["stale_by"] = stale_by
            _add_stale_replacement(meta, old_node_id, new_node_id)
            conn.execute(
                "UPDATE nodes SET metadata = ? WHERE id = ?",
                (json.dumps(meta), dep["id"]),
            )
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
            "WHERE target_id = ? AND relation IN ('derives_from', 'supported_by')",
            (old_node_id,),
        ).fetchall()
        for d in direct:
            _walk_cascade_downstream(conn, d["source_id"],
                                     visited=visited,
                                     on_live_node=_on_live_for_supersede)

        # Cascade to contradiction nodes that have old as a contradicting side
        # (issue #229): mark each affected contradiction as stale_by_premise
        # so dream-fairy-2 reviews whether the supersede substantively resolved
        # the conflict (case 1: wire engram_resolve from new_node to ct) or
        # preserved it (case 2: new-contradict between new_node + other side,
        # then supersede old contradiction → new contradiction). Per the
        # supersede no-drop discipline (the supersede-no-drop discipline), case 3 (orthogonal supersede)
        # cannot arise — the new node either kept or altered the conflicting
        # claim; dropping is forbidden.
        stale_contradictions = []
        contradiction_edges = conn.execute(
            "SELECT source_id, target_id FROM edges "
            "WHERE relation = 'contradicts' AND (source_id = ? OR target_id = ?)",
            (old_node_id, old_node_id),
        ).fetchall()
        affected_ct_ids = set()
        for ce in contradiction_edges:
            ct_id = ce["source_id"] if ce["source_id"].startswith("ct_") else ce["target_id"]
            if not ct_id.startswith("ct_"):
                continue  # contradicts edges only attach contradictions to claim-bearers; skip otherwise
            affected_ct_ids.add(ct_id)
        for ct_id in affected_ct_ids:
            ct = conn.execute(
                "SELECT id, type, claim, status, metadata FROM nodes WHERE id = ? AND is_current = 1",
                (ct_id,),
            ).fetchone()
            if not ct:
                continue
            ct_meta = json.loads(ct["metadata"]) if ct["metadata"] else {}
            stale_by = ct_meta.get("stale_by", [])
            if old_node_id not in stale_by:
                stale_by.append(old_node_id)
            ct_meta["stale_by"] = stale_by
            _add_stale_replacement(ct_meta, old_node_id, new_node_id)
            conn.execute(
                "UPDATE nodes SET metadata = ? WHERE id = ?",
                (json.dumps(ct_meta), ct_id),
            )
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
        # nodes that lost a supporting edge. If total live support = 0, mark
        # support_lost (distinct from stale — audit signal, not cascade).
        support_lost_cs_ls = _detect_zero_support(conn, old_node_id)

        core._log_edit(conn, "superseded", old_node_id, old["type"],
                  {"replaced_by": new_node_id, "reason": supersede_reason or None})
        for s in stale:
            core._log_edit(conn, "stale_flagged", s["id"], s["type"],
                      {"stale_premise": old_node_id, "replacement": new_node_id})

        core._utility_reward(conn, [new_node_id], action="supersede")
        conn.commit()

        # --- engram.tool.engram_call event (DESIGN.md §4.2) ---
        emit_if_initialized(
            event_type="engram.tool.engram_call",
            level=1,
            data={
                "tool_name": "engram_supersede",
                "target_id": old_node_id,
                "action_type": "supersede",
                "result_status": "superseded",
                "result_node_id": new_node_id,
            },
        )

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
        if support_lost_cs_ls:
            result["support_lost"] = support_lost_cs_ls
            result["support_lost_count"] = len(support_lost_cs_ls)
        return json.dumps(result)
    finally:
        conn.close()


def _retract_impl(
    node_id: str = "",
    error_type: str = "",
    reason: str = "",
    replacement_json: str = "",
    _obs_creator=None,
) -> str:
    """Internal implementation — see engram_retract MCP tool for the public
    payload schema. Kept callable with named kwargs for in-server callers.

    Retract a node that contains an error and flag downstream dependents.

    Unlike engram_supersede (epistemic evolution — old claim was valid at the time),
    engram_retract is for ERROR CORRECTION — the node was never valid. The retracted
    node is preserved for audit but marked as an error. All downstream nodes
    (derivations, theories) that depend on the retracted node are flagged as
    tainted so they can be reviewed and corrected.

    Cascade behavior:
      - Derivation cascade: every node that derives_from / supported_by the
        retracted node gets marked tainted_by (metadata.tainted_by includes
        the retracted node id).
      - Contradiction cascade (issue #229): every contradiction node that
        has the retracted node as a contradicting side gets marked
        tainted_by. Dream-fairy-2 reviews tainted contradictions —
        the retracted side was never valid, so the contradiction itself
        may need to be closed (since one of its claims is now invalid)
        or rewired to a corrected replacement observation if one was
        created via the replacement_json path.
      - Resolution reopening: if the retracted node was a resolution
        (had outgoing resolves edges), each target without another
        valid resolution is set back to status='open'.

    Error types:
      fabricated_quote — quoted_text not found in evidence source
      wrong_citation — claim doesn't follow from the quote
      wrong_evidence — cited the wrong source
      hallucinated_claim — claim not supported by evidence
      duplicate — same claim already existed (should have used support_existing)
      other — anything else

    Args:
        node_id: The node to retract.
        error_type: One of: fabricated_quote, wrong_citation, wrong_evidence,
            hallucinated_claim, duplicate, other.
        reason: Human-readable explanation of the error.
        replacement_json: Optional JSON object with fields for creating a correct
            replacement observation: {"quoted_text", "interpretation", "claim",
            "quote_type", "source_class"}. The replacement cites the same evidence.
        _obs_creator: Optional callable matching the _add_observation_impl
            signature — injected by the server.py wrapper so this module
            does not need to import server.py (acyclic dependency rule).
            When None, the replacement step is skipped even if replacement_json
            is provided.

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

    conn = core._get_db()
    try:
        node = conn.execute(
            "SELECT * FROM nodes WHERE id = ?",
            (node_id,),
        ).fetchone()
        if not node:
            return json.dumps({"error": f"Node '{node_id}' not found."})

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

        # 2. Find all downstream nodes (anything that derives_from or is supported_by this node)
        # Uses shared walker (_walk_cascade_downstream) — PR-AA SSoT refactor.
        tainted = []
        visited = {node_id}  # seed retracted node so walker won't process it

        def _on_live_for_retract(dep):
            meta = json.loads(dep["metadata"] or "{}")
            tainted_by = meta.get("tainted_by", [])
            if node_id not in tainted_by:
                tainted_by.append(node_id)
            meta["tainted_by"] = tainted_by
            conn.execute(
                "UPDATE nodes SET metadata = ? WHERE id = ?",
                (json.dumps(meta), dep["id"]),
            )
            tainted.append({
                "id": dep["id"],
                "type": dep["type"],
                "claim": dep["claim"],
            })

        # Walk direct dependents of the retracted node.
        direct = conn.execute(
            "SELECT source_id FROM edges "
            "WHERE target_id = ? AND relation IN ('derives_from', 'supported_by')",
            (node_id,),
        ).fetchall()
        for d in direct:
            _walk_cascade_downstream(conn, d["source_id"],
                                     visited=visited,
                                     on_live_node=_on_live_for_retract)

        # Cascade to contradiction nodes that have the retracted node as a
        # contradicting side (issue #229): mark each affected contradiction
        # as tainted_by so dream-fairy-2 reviews whether the contradiction
        # itself is still valid (the retracted side was never valid; if the
        # other side stands alone now, the contradiction may itself need
        # supersede / explicit close).
        tainted_contradictions = []
        contradiction_edges = conn.execute(
            "SELECT source_id, target_id FROM edges "
            "WHERE relation = 'contradicts' AND (source_id = ? OR target_id = ?)",
            (node_id, node_id),
        ).fetchall()
        affected_ct_ids = set()
        for ce in contradiction_edges:
            ct_id = ce["source_id"] if ce["source_id"].startswith("ct_") else ce["target_id"]
            if not ct_id.startswith("ct_"):
                continue
            affected_ct_ids.add(ct_id)
        for ct_id in affected_ct_ids:
            ct = conn.execute(
                "SELECT id, type, claim, status, metadata FROM nodes WHERE id = ? AND is_current = 1",
                (ct_id,),
            ).fetchone()
            if not ct:
                continue
            ct_meta = json.loads(ct["metadata"]) if ct["metadata"] else {}
            tainted_by = ct_meta.get("tainted_by", [])
            if node_id not in tainted_by:
                tainted_by.append(node_id)
            ct_meta["tainted_by"] = tainted_by
            conn.execute(
                "UPDATE nodes SET metadata = ? WHERE id = ?",
                (json.dumps(ct_meta), ct_id),
            )
            tainted_contradictions.append({
                "id": ct_id,
                "type": "contradiction",
                "claim": ct["claim"],
                "status": ct["status"],
            })
            core._log_edit(conn, "tainted", ct_id, "contradiction",
                      {"tainted_by": node_id, "cascade_via": "contradicts_edge"})

        # §2.3 Zero-support detection: after cascade settles, check cs/ls
        # nodes that lost a supporting edge. If total live support = 0, mark
        # support_lost (distinct from tainted — audit signal, not cascade).
        support_lost_cs_ls = _detect_zero_support(conn, node_id)

        # Log retraction and taint events
        core._log_edit(conn, "retracted", node_id, node["type"],
                  {"error_type": error_type, "reason": reason[:200]})
        for t in tainted:
            core._log_edit(conn, "tainted", t["id"], t["type"],
                      {"tainted_by": node_id})
        for rt in reopened_targets:
            core._log_edit(conn, "reopened", rt["id"], rt["type"],
                      {"reason": f"resolution {node_id} retracted"})

        # Commit retraction and taint before creating replacement
        # (engram_add_observation opens its own connection, so we must release ours)
        conn.commit()
        conn.close()
        conn = None

        # 3. Optionally create replacement node
        replacement_id = None
        ev_id = node["evidence_id"]
        if replacement_json and ev_id and _obs_creator is None:
            # Loud skip: a direct caller (not via server.py's compat forwarder,
            # which always wires _obs_creator) asked for a replacement that
            # cannot be created. Surfacing the skip prevents a silent
            # replacement-drop on the cascade path — the retract itself
            # succeeded, but the caller must know the replacement did not.
            result_replacement_skipped = True
        else:
            result_replacement_skipped = False
        if replacement_json and ev_id and _obs_creator is not None:
            try:
                repl = json.loads(replacement_json)
            except json.JSONDecodeError as e:
                return json.dumps({
                    "error": f"Invalid replacement_json: {e}",
                    "retraction": "completed",
                    "node_id": node_id,
                })

            repl_result = json.loads(_obs_creator(
                evidence_id=ev_id,
                quoted_text=repl.get("quoted_text", ""),
                interpretation=repl.get("interpretation", ""),
                claim=repl.get("claim", ""),
                quote_type=repl.get("quote_type", node["quote_type"] or "hard_data"),
                source_class=repl.get("source_class", "introspective"),
            ))
            if repl_result.get("status") == "created":
                replacement_id = repl_result["observation_id"]
                # Add retracts edge in a new connection
                conn2 = core._get_db()
                try:
                    conn2.execute(
                        "INSERT INTO edges (source_id, target_id, relation, created_at) VALUES (?, ?, 'retracts', ?)",
                        (replacement_id, node_id, now),
                    )
                    conn2.commit()
                except sqlite3.IntegrityError:
                    pass
                finally:
                    conn2.close()

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
        if support_lost_cs_ls:
            result["support_lost"] = support_lost_cs_ls
            result["support_lost_count"] = len(support_lost_cs_ls)
        if reopened_targets:
            result["reopened_targets"] = reopened_targets
            result["reopened_count"] = len(reopened_targets)
        if replacement_id:
            result["replacement_id"] = replacement_id
        if result_replacement_skipped:
            result["replacement_skipped"] = True
        if _self_anchor_retracted:
            result["warning"] = (
                "Retracted node was the agent's self-anchor (is_self=True). "
                "The graph now has zero current self-anchors. "
                "engram_link_about default-targeting, focus='self' emergence-scan, "
                "and trust_tier='self' assignment are broken until a new person node "
                "is created with is_self=True (or an existing one is promoted)."
            )

        # --- engram.tool.engram_call event (DESIGN.md §4.2) ---
        emit_if_initialized(
            event_type="engram.tool.engram_call",
            level=1,
            data={
                "tool_name": "engram_retract",
                "target_id": node_id,
                "action_type": "retract",
                "result_status": "retracted",
                "result_node_id": replacement_id,
            },
        )

        return json.dumps(result)
    finally:
        if conn is not None:
            conn.close()
