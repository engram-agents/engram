"""engram_derive — family B: derive/inference + contradiction + question impls.

Extracted from server.py as part of #872 wave 5.

Family B covers: derive, contradict, ask — the reasoning + contradiction +
open-question impls.

NOTE: module name collides with the @mcp.tool ``engram_derive`` in server.py.
The wave-2 alias convention is mandatory: import this module as
``import engram_derive as _derive_mod``.

Rolling promotion to engram_core (same wave): _validate_premises,
_trace_evidence_roots, _validate_reasoning_structure, and _create_derivation
are shared with family E (_resolve_impl, wave 6) — their canonical copies
live in engram_core; server.py originals deleted same-commit.

House rules (wave pattern):
  - Shared state ONLY via ``import engram_core as core`` + call-time ``core.X``.
  - Never import from server.py (acyclic: server → family → core).
  - Stateless beyond constants.
"""

from __future__ import annotations

import json
import sqlite3

import engram_core as core

from engram_confidence import (
    REASONING_TYPES,
)

from engram_log_emitter import emit_if_initialized


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _check_premise_independence(conn: "sqlite3.Connection", premise_ids: list[str]) -> "dict | None":
    """Check whether premise observations share a single standpoint_lineage (collinear).

    Returns an advisory dict when all known-lineage observation premises come
    from one lineage — indicating that an inductive_generalization confidence
    boost is unwarranted.  Returns None when premises are diverse or no
    known-lineage observations exist (undetermined).

    Only observation-type nodes carry meaningful standpoint_lineage.  Derived
    nodes, axioms, and observations without standpoint_lineage are skipped.
    If fewer than 2 observations with known lineage are found, the gate cannot
    fire (not enough signal to conclude collinearity).
    """
    lineage_map: dict[str, list[str]] = {}  # lineage → [node_ids]
    for nid in premise_ids:
        row = conn.execute(
            "SELECT type, standpoint_lineage FROM nodes WHERE id = ?", (nid,)
        ).fetchone()
        if not row:
            continue
        if not row["type"].startswith("observation"):
            continue
        lineage = (row["standpoint_lineage"] or "").strip()
        if not lineage:
            continue
        lineage_map.setdefault(lineage, []).append(nid)

    n_clusters = len(lineage_map)
    if n_clusters != 1:
        # 0 → undetermined (no known lineages); ≥2 → diverse. Both are fine.
        return None

    shared_lineage = next(iter(lineage_map))
    collinear_ids = lineage_map[shared_lineage]
    # Require ≥2 same-lineage obs: a single known-lineage premise with others
    # having no lineage recorded is undetermined, not collinear.
    if len(collinear_ids) < 2:
        return None
    return {
        "collinear_verdict": "collinear",
        "shared_lineage": shared_lineage,
        "collinear_premise_ids": collinear_ids,
        "message": (
            f"independence_advisory: all {len(collinear_ids)} observation premise(s) "
            f"share standpoint_lineage '{shared_lineage}'. "
            "inductive_generalization confidence (0.95 level) assumes independent "
            "sources — same-lineage premises are correlated, not independent. "
            "Consider: (a) adding a cross-lineage observation before generalizing, "
            "(b) downgrading to abductive_best_explanation (0.55) to reflect the "
            "actual evidence strength, or (c) proceed if you have reasons to "
            "believe independence holds despite shared lineage (note in logical_chain)."
        ),
    }


# ---------------------------------------------------------------------------
# B impls
# ---------------------------------------------------------------------------

def _derive_impl(
    claim: str = "",
    supporting_ids: str = "",
    logical_chain: str = "",
    reasoning_type: str = "",
    derivation_mode: str = "chain",
    context_ids: str = "",
    use_stale: bool = False,
    use_contested: bool = False,
    warrant: str = "",
) -> str:
    """Internal implementation — see engram_derive MCP tool for the public
    payload schema. Kept callable with named kwargs for in-server callers.

    Create a new derived claim by combining evidence from existing nodes.

    A derivation is an inference the agent makes by reasoning over multiple
    observations or other derivations. The logical chain must explicitly show
    how the cited premises lead to the conclusion.

    You MUST specify the reasoning_type to classify the logical argument.
    This determines how confidence is computed:

    Deductive (discount 0.98 — truth-preserving):
      deductive_modus_ponens, deductive_modus_tollens,
      deductive_hypothetical_syllogism, deductive_disjunctive, deductive_reductio

    Inductive (discount 0.70–0.95 — evidence strengthens but never proves):
      inductive_generalization (0.95, corroborative, multi-source),
      inductive_enumeration (0.93, corroborative, single authoritative source
        that enumerates structurally-independent items — use when one SEP/textbook
        entry decomposes a property into n distinct instances),
      inductive_statistical (0.90), inductive_causal (0.85),
      inductive_analogy (0.70, weakest legitimate form)

    Abductive (capped — best explanation, alternatives may exist):
      abductive_best_explanation (cap 0.80), abductive_elimination (cap 0.90)

    Authority (trust transfer):
      authority_expert (0.95), authority_consensus (0.98, corroborative, multi-source)

    To resolve an open question, contradiction, or prediction, use engram_resolve
    instead — it creates a derivation with a 'resolves' edge.

    Args:
        claim: The atomic, falsifiable claim being derived.
        supporting_ids: Comma-separated list of claim-bearing node IDs (e.g. 'ob_NNNN_A,ob_NNNN_B,dv_NNNN').
        logical_chain: Explicit reasoning connecting the cited premises to the conclusion. Show your work.
        reasoning_type: The type of logical argument (see list above). This determines confidence computation.
        derivation_mode: LEGACY — use reasoning_type instead. If reasoning_type is provided, this is ignored.
            Kept for backward compatibility: "chain" or "corroboration".
        context_ids: Optional comma-separated node IDs for context references (e.g. definitions).
            Creates 'cites' edges (not 'derives_from') — these don't affect confidence computation.
        use_stale: Opt-in override for MECH-5 stale-premise guard. Set to True ONLY when
            you've judged that the upstream supersede does not affect the logic of your
            derivation — the new derivation will carry a metadata.built_on_stale audit
            marker so future maintenance tools can auto-redirect edges to replacements.
            Has no effect on tainted premises (taint is always a hard block — no override).
        use_contested: Opt-in override for MECH-5 contradicted-premise guard (#1654).
            Set to True ONLY when you're deliberately building on a premise that sits
            on an open, unresolved contradiction (a live ct_ node with no resolves
            closure yet) — e.g. composing the very derivation that will resolve it.
            Auto-stamps metadata.built_on_contested (never author-supplied — this is
            a security property, not a courtesy: it lets the graph count how many
            derivations are knowingly riding on a still-open dispute, making
            resolution pressure measurable). Has no effect on tainted premises.
        warrant: The Toulmin warrant — the general principle or rule that licenses this
            inference (why do these premises support this claim?). Stored in derive_meta
            for audit. Leave blank if the bridging principle is fully captured in
            logical_chain.

    Returns:
        JSON with the new derivation node ID, computed confidence, reasoning type, and supporting nodes.
        If premises are compromised, returns a structured block response:
          - BLOCKED_TAINTED: any premise was retracted; no override path.
          - BLOCKED_CONTRADICTED: premise(s) sit on an open contradiction and use_contested=False.
          - BLOCKED_STALE: premise(s) have superseded upstream and use_stale=False.
    """
    # Per the bool-string-truthy lesson: bool-string truthy trap at wrapper boundary. JSON-string
    # "false" is truthy in Python; without this check, a use_stale: "false"
    # payload would silently bypass the MECH-5 stale-premise guard.
    # Same guard pattern as _resolve_impl (server.py:9456).
    if not isinstance(use_stale, bool):
        return json.dumps({
            "error": (
                f"use_stale must be a JSON boolean (true/false), got "
                f"{type(use_stale).__name__}: {use_stale!r}. "
                "Some MCP clients emit booleans as strings — make sure your "
                "JSON encodes `true`/`false` not `\"true\"`/`\"false\"`."
            )
        })
    if not isinstance(use_contested, bool):
        return json.dumps({
            "error": (
                f"use_contested must be a JSON boolean (true/false), got "
                f"{type(use_contested).__name__}: {use_contested!r}. "
                "Some MCP clients emit booleans as strings — make sure your "
                "JSON encodes `true`/`false` not `\"true\"`/`\"false\"`."
            )
        })
    if warrant and not isinstance(warrant, str):
        return json.dumps({"error": f"warrant must be a string, got {type(warrant).__name__}"})

    if reasoning_type and reasoning_type not in REASONING_TYPES:
        return json.dumps({
            "error": f"Invalid reasoning_type '{reasoning_type}'.",
            "valid_types": sorted(REASONING_TYPES),
        })
    if not reasoning_type:
        if derivation_mode == "corroboration":
            reasoning_type = "inductive_generalization"
        else:
            reasoning_type = "deductive_hypothetical_syllogism"

    ids = [s.strip() for s in core._as_csv(supporting_ids).split(",") if s.strip()]
    if not ids:
        return json.dumps({"error": "At least one supporting node ID is required."})
    ctx = [s.strip() for s in core._as_csv(context_ids).split(",") if s.strip()]

    conn = core._get_db()
    try:
        missing, wrong_type = [], []
        for sid in ids:
            row = conn.execute("SELECT id, type FROM nodes WHERE id = ?", (sid,)).fetchone()
            if not row:
                missing.append(sid)
            elif row["type"] not in core.CLAIM_BEARING_TYPES:
                wrong_type.append(f"{sid} (type: {row['type']})")
        if missing:
            return json.dumps({"error": f"Supporting node(s) not found: {', '.join(missing)}"})
        if wrong_type:
            return json.dumps({
                "error": f"Protocol violation: derivations can only cite claim-bearing nodes "
                         f"(observations, derivations, theories, axioms, conjectures). "
                         f"Cannot cite: {', '.join(wrong_type)}. "
                         f"If you want to reference a definition, use context_ids instead of supporting_ids.",
            })

        # Independence advisory for inductive_generalization (#1313):
        # A generalization over same-lineage premises inflates confidence beyond
        # what the evidence warrants. Emit an advisory (not a hard-block) so the
        # agent can reconsider the reasoning_type before the derivation is filed.
        independence_advisory: dict | None = None
        if reasoning_type == "inductive_generalization":
            independence_advisory = _check_premise_independence(conn, ids)

        block, success = core._create_derivation(
            conn,
            claim=claim,
            supporting_ids=ids,
            logical_chain=logical_chain,
            reasoning_type=reasoning_type,
            context_ids=ctx,
            use_stale=use_stale,
            use_contested=use_contested,
            extra_meta={"warrant": warrant} if warrant else None,
        )
        if block is not None:
            # --- engram.tool.engram_call event — blocked path (DESIGN.md §4.2) ---
            emit_if_initialized(
                event_type="engram.tool.engram_call",
                level=1,
                data={
                    "tool_name": "engram_derive",
                    "supporting_ids_count": len(ids),
                    "reasoning_type": reasoning_type,
                    "premise_validation_warnings": [],
                    "result_status": block.get("status", "blocked"),
                    "result_node_id": None,
                },
            )
            if independence_advisory:
                block["independence_advisory"] = independence_advisory
            return json.dumps(block)

        conn.commit()

        result = {
            "status": "created",
            "derivation_id": success["node_id"],
            "claim": claim,
            "confidence": success["confidence"],
            "reasoning_type": success["reasoning_type"],
            "reasoning_class": success["reasoning_class"],
            "supporting_nodes": ids,
            "context_nodes": success["context_nodes"],
            "logical_chain_preview": logical_chain[:200],
        }
        if success["bumped_count"]:
            result["utility_bumped_count"] = success["bumped_count"]
        if success["structure_warnings"]:
            result["structure_warnings"] = success["structure_warnings"]
        if use_stale and success["stale_ids"]:
            result["built_on_stale"] = success["stale_ids"]
        if use_contested and success["contested_ids"]:
            result["built_on_contested"] = success["contested_ids"]
        if independence_advisory:
            result["independence_advisory"] = independence_advisory

        # --- engram.tool.engram_call event — success path (DESIGN.md §4.2) ---
        emit_if_initialized(
            event_type="engram.tool.engram_call",
            level=1,
            data={
                "tool_name": "engram_derive",
                "supporting_ids_count": len(ids),
                "reasoning_type": success["reasoning_type"],
                "premise_validation_warnings": success.get("structure_warnings") or [],
                "result_status": "created",
                "result_node_id": success["node_id"],
            },
        )

        return json.dumps(result)
    finally:
        conn.close()


def _contradict_impl(
    node_id_a: str = "",
    node_id_b: str = "",
    description: str = "",
    root_cause: str = "",
) -> str:
    """Internal implementation — see engram_contradict MCP tool for the public
    payload schema. Kept callable with named kwargs for in-server callers.

    Explicitly flag a conflict between two nodes in the knowledge graph.

    Creates a Contradiction node linking both conflicting claims. Both sides
    are preserved with their evidence chains. Resolution is a deliberate act
    requiring future reasoning, not an automatic overwrite.

    Use this when you discover that two claims in your graph cannot both be
    true, whether they disagree on facts or reach opposing conclusions.

    Args:
        node_id_a: ID of the first conflicting node.
        node_id_b: ID of the second conflicting node.
        description: Clear description of the contradiction — what conflicts and why.
        root_cause: Optional analysis of the root cause: do the sources cite different evidence, or interpret the same evidence differently?

    Returns:
        JSON with the new contradiction node ID and linked claims.
    """
    if not node_id_a or not node_id_a.strip():
        return json.dumps({"error": "node_id_a is required and cannot be empty."})
    if not node_id_b or not node_id_b.strip():
        return json.dumps({"error": "node_id_b is required and cannot be empty."})
    if not description or not description.strip():
        return json.dumps({"error": "description is required and cannot be empty."})

    conn = core._get_db()
    try:
        # Validate both nodes exist and are claim-bearing
        node_a = conn.execute("SELECT id, type, claim, is_current FROM nodes WHERE id = ?", (node_id_a,)).fetchone()
        node_b = conn.execute("SELECT id, type, claim, is_current FROM nodes WHERE id = ?", (node_id_b,)).fetchone()
        if not node_a:
            return json.dumps({"error": f"Node '{node_id_a}' not found."})
        if not node_b:
            return json.dumps({"error": f"Node '{node_id_b}' not found."})
        if node_a["type"] not in core.CLAIM_BEARING_TYPES:
            return json.dumps({
                "error": f"Protocol violation: contradictions can only link claim-bearing nodes "
                         f"(observations, derivations, theories, axioms, conjectures). Node '{node_id_a}' is type '{node_a['type']}'. "
                         f"Extract observations from evidence sources first, then flag contradictions between those observations.",
            })
        if node_b["type"] not in core.CLAIM_BEARING_TYPES:
            return json.dumps({
                "error": f"Protocol violation: contradictions can only link claim-bearing nodes "
                         f"(observations, derivations, theories, axioms, conjectures). Node '{node_id_b}' is type '{node_b['type']}'. "
                         f"Extract observations from evidence sources first, then flag contradictions between those observations.",
            })
        # #1010: is_current guard — contradicting a superseded/retracted node
        # corrupts bookkeeping on a claim that is no longer live. Hard-reject;
        # walk the superseded_by chain to name the current successor so the
        # caller has a one-step path to the legitimate target.
        for nid, node in ((node_id_a, node_a), (node_id_b, node_b)):
            if node["is_current"] != 1:
                successor_id = core._current_successor(conn, nid)
                if successor_id:
                    hint = (
                        f"Its current successor is '{successor_id}' — "
                        f"contradict that node instead."
                    )
                else:
                    hint = (
                        "It has no current successor (retracted, or the "
                        "supersede chain ends non-current) — filing a "
                        "contradiction against a retracted node has no "
                        "epistemic effect."
                    )
                return json.dumps({
                    "error": f"Node '{nid}' is not current "
                             f"(superseded or retracted). {hint}",
                })

        full_desc = description
        if root_cause:
            full_desc += f"\n\nRoot cause analysis: {root_cause}"

        node_id = core._next_id(conn, "contradiction")
        now = core._now()

        conn.execute(
            """INSERT INTO nodes (id, type, claim, created_at, status)
               VALUES (?, 'contradiction', ?, ?, 'active')""",
            (node_id, full_desc, now),
        )

        # Create contradiction edges to both nodes
        conn.execute(
            "INSERT INTO edges (source_id, target_id, relation, created_at) VALUES (?, ?, 'contradicts', ?)",
            (node_id, node_id_a, now),
        )
        conn.execute(
            "INSERT INTO edges (source_id, target_id, relation, created_at) VALUES (?, ?, 'contradicts', ?)",
            (node_id, node_id_b, now),
        )

        core._stamp_new_node(conn, node_id, confidence=0.5, surprise=1.0)
        core._utility_reward(conn, [node_id_a, node_id_b], action="contradict")
        conn.commit()

        # --- engram.tool.engram_call event (DESIGN.md §4.2) ---
        # target_id captures both conflicting nodes; the new contradiction node
        # id is emitted as result_node_id. Includes both node IDs since
        # contradictions link two targets (not a single "target").
        emit_if_initialized(
            event_type="engram.tool.engram_call",
            level=1,
            data={
                "tool_name": "engram_contradict",
                "target_id": node_id_a,
                "target_id_b": node_id_b,
                "action_type": "contradict",
                "result_status": "created",
                "result_node_id": node_id,
            },
        )

        return json.dumps(
            {
                "status": "created",
                "contradiction_id": node_id,
                "conflicts": {
                    "node_a": {"id": node_id_a, "claim": node_a["claim"]},
                    "node_b": {"id": node_id_b, "claim": node_b["claim"]},
                },
                "description": description,
                "root_cause": root_cause or "Not analyzed yet",
            }
        )
    finally:
        conn.close()


def _ask_impl(
    question: str = "",
    context_ids: str = "",
    category: str = "",
    lacks: str = "",
) -> str:
    """Internal implementation — see engram_ask MCP tool for the public
    payload schema. Kept callable with named kwargs for in-server callers.

    Register an open question as a node in the knowledge graph.

    Questions are research directives — signposts that guide investigation.
    They are NOT claim-bearing: no node can cite a question as a premise.
    Their value is in driving future research (dream sessions, deep dives).
    When answered, the answer (a new observation, derivation, etc.) enters
    the reasoning graph — not the question itself.

    Use this when you have an open-ended gap to investigate. If you have a
    specific claim you believe and want to derive from before proving, use
    engram_add_conjecture instead.

    Args:
        question: The specific, answerable question to investigate.
        context_ids: Optional comma-separated node IDs that are relevant but insufficient to answer the question.
        category: Optional question category — what kind of question. One of:
            research (needs external sources), design (architecture/protocol decision),
            implementation (known design, needs code), planning (needs human decision),
            meta (about ENGRAM itself or the process).
        lacks: Optional blocker — what's missing to resolve. One of:
            external_evidence, empirical_data, human_decision, implementation,
            synthesis, prerequisite. Can be updated later during sweeps.

    Returns:
        JSON with the new question node ID.
    """
    if not question or not question.strip():
        return json.dumps({"error": "question is required and cannot be empty."})

    VALID_CATEGORIES = {"research", "design", "implementation", "planning", "meta"}
    VALID_LACKS = {"external_evidence", "empirical_data", "human_decision",
                   "implementation", "synthesis", "prerequisite"}

    if category and category not in VALID_CATEGORIES:
        return json.dumps({"error": f"Invalid category '{category}'. Must be one of: {sorted(VALID_CATEGORIES)}"})
    if lacks and lacks not in VALID_LACKS:
        return json.dumps({"error": f"Invalid lacks '{lacks}'. Must be one of: {sorted(VALID_LACKS)}"})

    conn = core._get_db()
    try:
        node_id = core._next_id(conn, "question")
        now = core._now()

        conn.execute(
            """INSERT INTO nodes (id, type, claim, created_at, status,
                       question_category, question_lacks, last_assessed_turn, last_assessed_at)
               VALUES (?, 'question', ?, ?, 'open', ?, ?, ?, ?)""",
            (node_id, question, now,
             category or None, lacks or None,
             core._get_current_turn(), now),
        )

        # Link to context nodes if provided
        context = [s.strip() for s in core._as_csv(context_ids).split(",") if s.strip()]
        for cid in context:
            exists = conn.execute("SELECT id FROM nodes WHERE id = ?", (cid,)).fetchone()
            if exists:
                try:
                    conn.execute(
                        "INSERT INTO edges (source_id, target_id, relation, created_at) VALUES (?, ?, 'cites', ?)",
                        (node_id, cid, now),
                    )
                except sqlite3.IntegrityError:
                    pass

        core._stamp_new_node(conn, node_id, confidence=0.5, surprise=0.5)
        if context:
            core._utility_reward(conn, context, action="citation")
        conn.commit()

        result = {
            "status": "created",
            "question_id": node_id,
            "question": question,
            "context_nodes": context,
            "message": "Question registered. Research this during your next inquiry or reflection cycle.",
        }
        if category:
            result["category"] = category
        if lacks:
            result["lacks"] = lacks
        return json.dumps(result)
    finally:
        conn.close()
