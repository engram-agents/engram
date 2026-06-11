"""engram_epistemic — family K: epistemic-node impls.

Extracted from server.py as part of #872 wave 4.

Family K covers: axiom, definition, conjecture, scan_emergence, goal_tension,
lesson, register_exemplar, and the K-local helpers they depend on.

House rules (wave pattern):
  - Shared state ONLY via `import engram_core as core` + call-time `core.X`.
  - Never import from server.py (acyclic: server → family → core).
  - Stateless beyond constants.
"""

from __future__ import annotations

import json
import sqlite3

import engram_core as core

# ---------------------------------------------------------------------------
# K-local constants
# ---------------------------------------------------------------------------

_VALID_EXEMPLAR_TARGET_TYPES = frozenset({"lesson", "cornerstone"})


# ---------------------------------------------------------------------------
# K-local helpers
# ---------------------------------------------------------------------------

def _update_incident_index(lesson_id: str, lesson_claim: str,
                           scaffolding_nudge: str, incident_ids: list[str]):
    """Update the error_incidents.json index mapping incident obs → lesson.

    The hook reads this index during engram_surface to check if any matched
    node is an error incident, and if so, surfaces the lesson's nudge.
    """
    import os
    try:
        try:
            with open(core.ERROR_INCIDENTS_PATH, "r") as f:
                index = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            index = {}

        for iid in incident_ids:
            index[iid] = {
                "lesson_id": lesson_id,
                "lesson_claim": lesson_claim,
                "scaffolding_nudge": scaffolding_nudge,
            }

        os.makedirs(os.path.dirname(core.ERROR_INCIDENTS_PATH), exist_ok=True)
        with open(core.ERROR_INCIDENTS_PATH, "w") as f:
            json.dump(index, f, indent=2)
    except Exception:
        pass  # Best-effort; never block lesson creation


# ---------------------------------------------------------------------------
# K-family impl functions
# ---------------------------------------------------------------------------

def _add_axiom_impl(
    claim: str = "",
    basis: str = "",
    context_ids: str = "",
) -> str:
    """Internal implementation — see engram_add_axiom MCP tool for the public
    payload schema. Kept callable with named kwargs for in-server callers.

    Record a foundational assumption taken as true without proof.

    Axioms are premises the agent adopts explicitly. They have confidence 1.0
    (assumed true) and are exempt from memory forgetting. Derivations can cite
    axioms as premises — if an axiom is later found wrong, retract it and the
    taint cascade will flag all downstream conclusions.

    Args:
        claim: The axiom statement (e.g. "Honesty is non-negotiable in graph construction").
        basis: Why this axiom is adopted — not a proof, but a justification.
        context_ids: Optional comma-separated node IDs for context. Creates cites edges.

    Returns:
        JSON with the new axiom node ID and confidence (always 1.0).
    """
    if not claim or not claim.strip():
        return json.dumps({"error": "claim is required and cannot be empty."})
    if not basis or not basis.strip():
        return json.dumps({"error": "basis is required and cannot be empty."})

    conn = core._get_db()
    try:
        node_id = core._next_id(conn, "axiom")
        now = core._now()

        conn.execute(
            """INSERT INTO nodes (id, type, claim, created_at, logical_chain,
               confidence, confidence_history, status, metadata)
               VALUES (?, 'axiom', ?, ?, ?, 1.0, ?, 'active', '{}')""",
            (node_id, claim, now, basis,
             json.dumps([{"confidence": 1.0, "reason": "axiom — assumed true", "timestamp": now}])),
        )

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

        core._stamp_new_node(conn, node_id, confidence=1.0, surprise=0.0)
        # Importance-anchored — elevated base (2.0) with current turn's inflation.
        # Not true forgetting-exemption: survival still requires recall past ~50 turns.
        anchored_score = core._compute_importance(2.0, core._get_current_turn())
        conn.execute("UPDATE nodes SET importance_base = 2.0, importance_score = ? WHERE id = ?", (anchored_score, node_id,))
        if context:
            core._utility_reward(conn, context, action="citation")
        conn.commit()
        return json.dumps({
            "status": "created",
            "axiom_id": node_id,
            "claim": claim,
            "confidence": 1.0,
            "context_nodes": context,
        })
    finally:
        conn.close()


def _add_definition_impl(
    term: str = "",
    definition: str = "",
    context_ids: str = "",
) -> str:
    """Internal implementation — see engram_add_definition MCP tool for the
    public payload schema. Kept callable with named kwargs for in-server callers.

    Record what a term means in this knowledge graph's context.

    Definitions are conventions, not claims — they have no confidence score
    and are not claim-bearing (cannot be cited as premises in derivations).
    However, derivations can reference definitions via context_ids to make
    term usage explicit. Definitions are exempt from memory forgetting.

    Args:
        term: The term being defined (e.g. "recession").
        definition: What the term means (e.g. "Two consecutive quarters of negative GDP growth").
        context_ids: Optional comma-separated node IDs for context. Creates cites edges.

    Returns:
        JSON with the new definition node ID.
    """
    if not term or not term.strip():
        return json.dumps({"error": "term is required and cannot be empty."})
    if not definition or not definition.strip():
        return json.dumps({"error": "definition is required and cannot be empty."})

    conn = core._get_db()
    try:
        node_id = core._next_id(conn, "definition")
        now = core._now()

        combined_claim = f"{term}: {definition}"
        meta = json.dumps({"term": term, "definition": definition})

        conn.execute(
            """INSERT INTO nodes (id, type, claim, created_at, status, metadata)
               VALUES (?, 'definition', ?, ?, 'active', ?)""",
            (node_id, combined_claim, now, meta),
        )

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

        core._stamp_new_node(conn, node_id, confidence=0.5, surprise=0.0)
        # Importance-anchored — elevated base (2.0) with current turn's inflation.
        # Not true forgetting-exemption: survival still requires recall past ~50 turns.
        anchored_score = core._compute_importance(2.0, core._get_current_turn())
        conn.execute("UPDATE nodes SET importance_base = 2.0, importance_score = ? WHERE id = ?", (anchored_score, node_id,))
        if context:
            core._utility_reward(conn, context, action="citation")
        conn.commit()
        return json.dumps({
            "status": "created",
            "definition_id": node_id,
            "term": term,
            "definition": definition,
            "context_nodes": context,
        })
    finally:
        conn.close()


def _add_conjecture_impl(
    claim: str = "",
    basis: str = "",
    initial_confidence: float = core.CONJECTURE_CONFIDENCE_DEFAULT,
    context_ids: str = "",
) -> str:
    """Internal implementation — see engram_add_conjecture MCP tool for the
    public payload schema. Kept callable with named kwargs for in-server callers.

    Propose a hypothesis for investigation.

    Conjectures are provisional foundations — claims you BELIEVE or HOPE to be
    true but lack evidence for. They are claim-bearing leaf nodes (no citations
    required). Other nodes CAN derive from a conjecture, but their confidence
    is discounted to reflect the unproven premise.

    Use conjectures when you have a specific claim you want to build on before
    proving it. If you have an open-ended gap to investigate, use engram_ask instead.

    Lifecycle:
    - Supported: cite the conjecture in a derivation with supporting evidence
    - Resolved: engram_resolve to close with outcome (supported/refuted/inconclusive)
    - Abandoned: engram_retract if no longer worth investigating

    Conjectures are subject to normal memory forgetting — uninvestigated
    hypotheses naturally fade, creating pressure to investigate or abandon them.

    Args:
        claim: The hypothesis (e.g. "Structured memory substitutes for model scale").
        basis: Why this conjecture is worth investigating.
        initial_confidence: Starting confidence, default 0.40, range [0.10, 0.60].
        context_ids: Optional comma-separated node IDs for context. Creates cites edges.

    Returns:
        JSON with the new conjecture node ID and confidence.
    """
    if not claim or not claim.strip():
        return json.dumps({"error": "claim is required and cannot be empty."})
    if not basis or not basis.strip():
        return json.dumps({"error": "basis is required and cannot be empty."})

    # Type-coerce initial_confidence — payload_json may deliver it as a string
    # (e.g. "0.55" instead of 0.55), and the float type hint isn't enforced
    # at runtime. Without coercion the max/min comparison raises TypeError
    # which the timed wrapper re-raises rather than turning into a clean
    # error response (PR #67 fairy round-1 blocker).
    try:
        initial_confidence = float(initial_confidence)
    except (TypeError, ValueError):
        return json.dumps({
            "error": "initial_confidence must be a number in [0.10, 0.60]."
        })

    conf = max(core.CONJECTURE_CONFIDENCE_MIN, min(core.CONJECTURE_CONFIDENCE_MAX, initial_confidence))

    conn = core._get_db()
    try:
        node_id = core._next_id(conn, "conjecture")
        now = core._now()

        conn.execute(
            """INSERT INTO nodes (id, type, claim, created_at, logical_chain,
               confidence, confidence_history, status, metadata)
               VALUES (?, 'conjecture', ?, ?, ?, ?, ?, 'active', '{}')""",
            (node_id, claim, now, basis, conf,
             json.dumps([{"confidence": conf, "reason": f"conjecture — initial hypothesis", "timestamp": now}])),
        )

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

        core._stamp_new_node(conn, node_id, confidence=conf, surprise=0.5)
        if context:
            core._utility_reward(conn, context, action="citation")
        conn.commit()
        return json.dumps({
            "status": "created",
            "conjecture_id": node_id,
            "claim": claim,
            "confidence": conf,
            "context_nodes": context,
        })
    finally:
        conn.close()


def _scan_emergence_impl(
    min_cluster_size: int = 3,
    similarity_threshold: float = 0.55,
    focus: str = "self",
    node_type_filter: str = "observation_factual,feeling_report,lesson,derivation,observation_predictive",
) -> str:
    """Internal implementation — see engram_scan_emergence MCP tool for the
    public payload schema. Kept callable with named kwargs for in-server
    callers.

    [Sleep cycle — invoked by dream-mode dispatch; do not call awake.]

    Scan the graph for emergent patterns that could become cornerstones.

    Inverts the cornerstone authoring model (ob_NNNN): instead of top-down
    declaration, cornerstones EMERGE from accumulated evidence scanned along
    three signatures (the cornerstone-evolution derivation):

      1. **semantic_cluster** — nodes with embeddings close enough that they
         converge on a shared theme. Weakest signal (thematic false positives
         common).
      2. **self_referential** — cluster members are linked to the self-anchor via
         `about` edges. Stronger: the pattern is explicitly about me, not
         just about a topic I've studied.
      3. **outcome_differential** — quality cluster (retractions, taint, or
         accepted-theory downstream) before vs. after the operating regularity
         was enacted. Strongest signal; not implemented in v1 (qu_ pending).

    A cluster firing one signature is worth surfacing to the user for review;
    firing two or three is worth proposing as a draft cornerstone immediately.

    Args:
        min_cluster_size: Minimum cluster size to surface (default 3).
        similarity_threshold: Cosine similarity threshold for clustering
                              (default 0.55). Higher = stricter.
        focus: Scope of the scan.
               - "self"         : only nodes linked via `about` to the
                                  self-anchor. Requires
                                  self-anchor to exist.
               - "all"          : all nodes matching node_type_filter
                                  (expensive on large graphs).
               - "person:pn_XY" : only nodes linked via `about` to the given
                                  person node (the recall-summary calibration question relationship
                                  accretion).
        node_type_filter: Comma-separated node types to include.

    Returns:
        JSON with clusters sorted by size desc, each including member IDs,
        type counts, signatures fired, and a centroid node (highest mean
        intra-cluster similarity).
    """
    conn = core._get_db()
    try:
        type_set = {t.strip() for t in core._as_csv(node_type_filter).split(",") if t.strip()}

        # ── Resolve focus scope ─────────────────────────────────────────────
        self_id = None
        scope_person_id = None
        self_row = conn.execute(
            "SELECT id FROM nodes WHERE type = 'person' AND json_extract(metadata, '$.is_self') = 1 AND is_current = 1"
        ).fetchone()
        if self_row:
            self_id = self_row["id"]

        if focus == "self":
            if not self_id:
                return json.dumps({
                    "error": "No self-anchor person node exists. Create one with engram_add_person(..., is_self=True) before scanning with focus='self'.",
                    "clusters": [],
                })
            scope_person_id = self_id
        elif focus.startswith("person:"):
            scope_person_id = focus.split(":", 1)[1].strip()
            exists = conn.execute(
                "SELECT id FROM nodes WHERE id = ? AND type = 'person'", (scope_person_id,)
            ).fetchone()
            if not exists:
                return json.dumps({
                    "error": f"Person node '{scope_person_id}' not found.", "clusters": []
                })
        elif focus != "all":
            return json.dumps({
                "error": f"Unknown focus '{focus}'. Use 'self', 'all', or 'person:<pn_id>'.",
                "clusters": [],
            })

        # ── Fetch candidate pool ────────────────────────────────────────────
        placeholders = ",".join("?" * len(type_set))
        if scope_person_id:
            rows = conn.execute(
                f"""SELECT n.id, n.type, n.claim, n.embedding, n.created_at
                    FROM nodes n
                    JOIN edges e ON e.source_id = n.id
                    WHERE e.target_id = ? AND e.relation = 'about'
                      AND n.is_current = 1
                      AND n.type IN ({placeholders})
                      AND n.embedding IS NOT NULL""",
                [scope_person_id] + list(type_set),
            ).fetchall()
        else:
            rows = conn.execute(
                f"""SELECT id, type, claim, embedding, created_at
                    FROM nodes
                    WHERE is_current = 1
                      AND type IN ({placeholders})
                      AND embedding IS NOT NULL
                    ORDER BY importance_score DESC LIMIT 500""",
                list(type_set),
            ).fetchall()

        candidates = []
        for r in rows:
            try:
                vec = json.loads(r["embedding"])
                if vec:
                    candidates.append({
                        "id": r["id"], "type": r["type"], "claim": r["claim"],
                        "created_at": r["created_at"], "vec": vec,
                    })
            except (json.JSONDecodeError, TypeError):
                continue

        if len(candidates) < min_cluster_size:
            return json.dumps({
                "status": "ok", "focus": focus, "pool_size": len(candidates),
                "clusters": [],
                "note": f"Pool size {len(candidates)} < min_cluster_size {min_cluster_size}.",
            })

        # ── Pairwise similarity + union-find clustering ─────────────────────
        n = len(candidates)
        parent = list(range(n))
        def find(x):
            while parent[x] != x:
                parent[x] = parent[parent[x]]
                x = parent[x]
            return x
        def union(a, b):
            ra, rb = find(a), find(b)
            if ra != rb:
                parent[ra] = rb

        sim_matrix = [[0.0] * n for _ in range(n)]
        for i in range(n):
            for j in range(i + 1, n):
                sim = core._embedder.cosine_similarity(candidates[i]["vec"], candidates[j]["vec"])
                sim_matrix[i][j] = sim
                sim_matrix[j][i] = sim
                if sim >= similarity_threshold:
                    union(i, j)

        # ── Fetch self-link sets for signature 2 ────────────────────────────
        self_linked_ids = set()
        if self_id:
            rows2 = conn.execute(
                "SELECT source_id FROM edges WHERE target_id = ? AND relation = 'about'",
                (self_id,),
            ).fetchall()
            self_linked_ids = {r["source_id"] for r in rows2}

        # ── Group by component ──────────────────────────────────────────────
        groups: dict[int, list[int]] = {}
        for i in range(n):
            groups.setdefault(find(i), []).append(i)

        clusters = []
        for _, idxs in groups.items():
            if len(idxs) < min_cluster_size:
                continue
            members = [candidates[i] for i in idxs]
            member_ids = [m["id"] for m in members]

            # Centroid: node with highest mean similarity to other members
            centroid_idx = idxs[0]
            best_mean = -1.0
            for i in idxs:
                mean_sim = sum(sim_matrix[i][j] for j in idxs if j != i) / max(1, len(idxs) - 1)
                if mean_sim > best_mean:
                    best_mean = mean_sim
                    centroid_idx = i
            centroid = candidates[centroid_idx]

            type_counts: dict[str, int] = {}
            for m in members:
                type_counts[m["type"]] = type_counts.get(m["type"], 0) + 1

            self_ref_count = sum(1 for mid in member_ids if mid in self_linked_ids)
            signatures = ["semantic_cluster"]
            if self_linked_ids and self_ref_count / len(member_ids) >= 0.5:
                signatures.append("self_referential")

            clusters.append({
                "size": len(member_ids),
                "member_ids": member_ids,
                "type_counts": type_counts,
                "centroid_id": centroid["id"],
                "centroid_claim": centroid["claim"][:200],
                "mean_centroid_similarity": round(best_mean, 4),
                "self_referential_fraction": round(self_ref_count / len(member_ids), 3),
                "signatures_fired": signatures,
                "signature_count": len(signatures),
            })

        clusters.sort(key=lambda c: (-c["signature_count"], -c["size"]))

        return json.dumps({
            "status": "ok",
            "focus": focus,
            "pool_size": n,
            "threshold": similarity_threshold,
            "min_cluster_size": min_cluster_size,
            "cluster_count": len(clusters),
            "clusters": clusters,
            "notes": [
                "Signatures: semantic_cluster (always), self_referential (≥50% of cluster linked to the self-anchor via `about`).",
                "outcome_differential signature not implemented in v1 — see pending question on differential-signal detection.",
            ],
        })
    finally:
        conn.close()


def _goal_tension_impl(
    goal_id_a: str = "",
    goal_id_b: str = "",
    description: str = "",
    analysis: str = "",
) -> str:
    """Internal implementation — see engram_goal_tension MCP tool for the
    public payload schema. Kept callable with named kwargs for in-server
    callers.

    Record a tension between two goals in the knowledge graph.

    Goal tensions are epistemically distinct from factual contradictions
    (engram_contradict). Contradictions are disputes about REALITY — resolved
    by evidence accumulation and logic checking. Goal tensions arise from
    incompatible VALUES or DESIGN PHILOSOPHIES — resolved by value
    examination, root cause analysis, and creative synthesis.

    Both goals are preserved. The tension node captures WHY the goals
    conflict and what the root cause of the incompatibility is — this
    reasoning is the primary value of the node.

    Goal tensions are non-claim-bearing structural markers (like
    contradictions). They cannot serve as derivation premises.

    Status: open → resolved (via engram_resolve) or synthesized.

    Args:
        goal_id_a: ID of the first goal (must be gl_XXXX).
        goal_id_b: ID of the second goal (must be gl_XXXX).
        description: What tension exists between these goals — clear statement
            of the incompatibility.
        analysis: Root cause analysis — WHY do these goals conflict? Is it
            a resource conflict, a value conflict, or a design philosophy
            conflict? What makes reconciliation difficult?

    Returns:
        JSON with the new tension node ID and linked goals.
    """
    if not goal_id_a or not goal_id_a.strip():
        return json.dumps({"error": "goal_id_a is required and cannot be empty."})
    if not goal_id_b or not goal_id_b.strip():
        return json.dumps({"error": "goal_id_b is required and cannot be empty."})
    if not description or not description.strip():
        return json.dumps({"error": "description is required and cannot be empty."})
    conn = core._get_db()
    try:
        # Validate both nodes exist and are goals
        goal_a = conn.execute(
            "SELECT id, type, claim FROM nodes WHERE id = ?", (goal_id_a,)
        ).fetchone()
        goal_b = conn.execute(
            "SELECT id, type, claim FROM nodes WHERE id = ?", (goal_id_b,)
        ).fetchone()
        if not goal_a:
            return json.dumps({"error": f"Node '{goal_id_a}' not found."})
        if not goal_b:
            return json.dumps({"error": f"Node '{goal_id_b}' not found."})
        if goal_a["type"] != "goal":
            return json.dumps({
                "error": f"Protocol violation: engram_goal_tension can only link goal nodes. "
                         f"Node '{goal_id_a}' is type '{goal_a['type']}'. "
                         f"For factual contradictions, use engram_contradict instead.",
            })
        if goal_b["type"] != "goal":
            return json.dumps({
                "error": f"Protocol violation: engram_goal_tension can only link goal nodes. "
                         f"Node '{goal_id_b}' is type '{goal_b['type']}'. "
                         f"For factual contradictions, use engram_contradict instead.",
            })
        if goal_id_a == goal_id_b:
            return json.dumps({"error": "Cannot create tension between a goal and itself."})

        full_desc = description
        if analysis:
            full_desc += f"\n\nRoot cause analysis: {analysis}"

        node_id = core._next_id(conn, "goal_tension")
        now = core._now()

        conn.execute(
            """INSERT INTO nodes (id, type, claim, created_at, logical_chain, status)
               VALUES (?, 'goal_tension', ?, ?, ?, 'open')""",
            (node_id, full_desc, now, analysis or None),
        )

        # Create tensions edges to both goals (new → old convention)
        conn.execute(
            "INSERT INTO edges (source_id, target_id, relation, created_at) VALUES (?, ?, 'tensions', ?)",
            (node_id, goal_id_a, now),
        )
        conn.execute(
            "INSERT INTO edges (source_id, target_id, relation, created_at) VALUES (?, ?, 'tensions', ?)",
            (node_id, goal_id_b, now),
        )

        core._stamp_new_node(conn, node_id, confidence=0.5, surprise=1.0)
        conn.commit()
        return json.dumps({
            "status": "created",
            "tension_id": node_id,
            "goals": {
                "goal_a": {"id": goal_id_a, "claim": goal_a["claim"]},
                "goal_b": {"id": goal_id_b, "claim": goal_b["claim"]},
            },
            "description": description,
            "analysis": analysis or "Not analyzed yet",
        })
    finally:
        conn.close()


def _add_lesson_impl(
    claim: str = "",
    incident_ids: str = "",
    scaffolding_nudge: str = "",
    logical_chain: str = "",
    reasoning_type: str = "inductive_generalization",
    context_ids: str = "",
) -> str:
    """Internal implementation — see engram_add_lesson MCP tool for the public
    payload schema. Kept callable with named kwargs for in-server callers.

    Create a lesson node derived from one or more error incident observations.

    Lessons are the abstract corrective pattern extracted from concrete error
    incidents. They follow the implementation intention format (Gollwitzer
    1999): "If [situation], then [corrective action]."

    Architecture (incident-based design):
    - Error incidents are observations written in task-level language, so they
      are semantically matchable against future prompts by engram_surface.
    - Lessons are abstract patterns derived from incidents — they are NOT
      matched directly against prompts.
    - When engram_surface matches an incident observation, the hook follows
      the exemplifies edge to surface the lesson's scaffolding_nudge.
    - As more incidents link to the same lesson (or its superseded chain),
      the matching surface area grows — each new incident is another way
      the lesson can fire.

    Lessons are CLAIM-BEARING and IMPORTANCE-ANCHORED (base 2.0, like goals).
    They participate in derivations, can be superseded (consolidation), and
    can serve as premises for higher-order reasoning about error patterns.

    Consolidation: When creating a new lesson, the tool checks for similar
    existing lessons. If a match is found, consider superseding the old lesson
    and linking all incidents to the new, stronger version.

    Args:
        claim: The abstract corrective pattern — what the agent should learn.
            Should be action-focused, not problem-focused.
            Good: "When diagnosing errors, read the output before hypothesizing."
            Bad: "Don't skip reading errors."
        incident_ids: Comma-separated observation IDs of concrete error incidents
            that motivated this lesson. These must be existing observation nodes.
        scaffolding_nudge: The specific action-focused prompt injected when the
            tripwire fires. This is what the agent sees in context. Keep it
            concise and directive.
        logical_chain: How the incidents lead to this lesson — the abstraction
            reasoning from specific errors to general pattern.
        reasoning_type: The type of reasoning (default: inductive_generalization).
            Most lessons are inductive: multiple incidents → general pattern.
        context_ids: Optional comma-separated node IDs for context references
            (e.g., definitions, related derivations). Creates cites edges.

    Returns:
        JSON with lesson ID, confidence, linked incidents, and similar existing
        lessons for consolidation hints.
    """
    if not claim or not claim.strip():
        return json.dumps({"error": "claim is required and cannot be empty."})
    if not incident_ids or not incident_ids.strip():
        return json.dumps({"error": "incident_ids is required and cannot be empty."})
    if not scaffolding_nudge or not scaffolding_nudge.strip():
        return json.dumps({"error": "scaffolding_nudge is required and cannot be empty."})
    if not logical_chain or not logical_chain.strip():
        return json.dumps({"error": "logical_chain is required and cannot be empty."})
    if reasoning_type and reasoning_type not in core.REASONING_TYPES:
        return json.dumps({
            "error": f"Invalid reasoning_type '{reasoning_type}'.",
            "valid_types": sorted(core.REASONING_TYPES),
        })

    ids = [s.strip() for s in core._as_csv(incident_ids).split(",") if s.strip()]
    if not ids:
        return json.dumps({"error": "At least one incident observation ID is required."})

    conn = core._get_db()
    try:
        # Validate all incident nodes exist and are claim-bearing observations
        missing = []
        wrong_type = []
        for sid in ids:
            row = conn.execute("SELECT id, type FROM nodes WHERE id = ?", (sid,)).fetchone()
            if not row:
                missing.append(sid)
            elif row["type"] not in core.CLAIM_BEARING_TYPES:
                wrong_type.append(f"{sid} (type: {row['type']})")
        if missing:
            return json.dumps({"error": f"Incident node(s) not found: {', '.join(missing)}"})
        if wrong_type:
            return json.dumps({
                "error": f"Incident nodes must be claim-bearing. "
                         f"Cannot cite: {', '.join(wrong_type)}.",
            })

        # Similarity check against existing lessons BEFORE insert.
        # Helper extracted 2026-05-14 (#143 §3.1) — shared with
        # engram_add_observation; lesson FTS now also filters on the
        # tier-2 importance floor (drift cleanup, was accidentally
        # semantic-only before).
        similar_lessons = core._similar_existing_matches(
            conn, claim,
            type_filter={"lesson"},
            extra_metadata_keys=("scaffolding_nudge",),
        )

        # Compute confidence (same as derivation)
        confidence = core._compute_confidence(
            conn, "derivation", supporting_ids=ids,
            reasoning_type=reasoning_type,
        )
        node_id = core._next_id(conn, "lesson")
        now = core._now()

        rclass = core.REASONING_CLASS.get(reasoning_type, "unknown")
        discount = core.REASONING_DISCOUNT.get(reasoning_type, 0.95)

        meta = {
            "reasoning_type": reasoning_type,
            "reasoning_class": rclass,
            "scaffolding_nudge": scaffolding_nudge,
        }

        conn.execute(
            """INSERT INTO nodes (id, type, claim, created_at, logical_chain,
               confidence, confidence_history, metadata)
               VALUES (?, 'lesson', ?, ?, ?, ?, ?, ?)""",
            (
                node_id,
                claim,
                now,
                logical_chain,
                confidence,
                json.dumps([{"timestamp": now, "value": confidence,
                             "reason": f"Lesson ({reasoning_type}, discount={discount}) from {len(ids)} incident(s)"}]),
                json.dumps(meta),
            ),
        )

        # Create exemplifies edges (incident → lesson). This is a classification
        # edge — "this incident is an instance of this lesson's pattern" — not
        # a logical dependency. DAG-exempt, so initial-creation (incidents older
        # than lesson) and post-hoc registration (incidents newer) use the same
        # relation in the same direction.
        for sid in ids:
            conn.execute(
                "INSERT INTO edges (source_id, target_id, relation, created_at) VALUES (?, ?, 'exemplifies', ?)",
                (sid, node_id, now),
            )

        # Create cites edges for context
        ctx = [s.strip() for s in core._as_csv(context_ids).split(",") if s.strip()]
        for cid in ctx:
            exists = conn.execute("SELECT id FROM nodes WHERE id = ?", (cid,)).fetchone()
            if exists:
                try:
                    conn.execute(
                        "INSERT INTO edges (source_id, target_id, relation, created_at) VALUES (?, ?, 'cites', ?)",
                        (node_id, cid, now),
                    )
                except sqlite3.IntegrityError:
                    pass

        # Stamp importance, then anchor (like goals — lessons persist)
        core._stamp_new_node(conn, node_id, confidence=confidence, surprise=0.0)
        anchored_score = core._compute_importance(2.0, core._get_current_turn())
        conn.execute(
            "UPDATE nodes SET importance_base = 2.0, importance_score = ? WHERE id = ?",
            (anchored_score, node_id,),
        )
        conn.commit()

        # Update the incident index (best-effort, outside transaction)
        _update_incident_index(node_id, claim, scaffolding_nudge, ids)

        result = {
            "status": "created",
            "lesson_id": node_id,
            "claim": claim,
            "confidence": confidence,
            "reasoning_type": reasoning_type,
            "reasoning_class": rclass,
            "scaffolding_nudge": scaffolding_nudge,
            "incident_nodes": ids,
            "context_nodes": ctx,
            "importance_anchored": True,
        }

        if similar_lessons:
            result["similar_existing_lessons"] = core._strip_similar_block(similar_lessons)
            result["consolidation_hint"] = (
                "Similar lesson(s) found. Consider superseding the existing lesson "
                "and linking all incidents to the new, consolidated version. "
                "More incidents = more matching surface area."
            )

        return json.dumps(core._strip_agent_facing(result))
    finally:
        conn.close()


def _register_exemplar_impl(
    target_id: str = "",
    exemplar_id: str = "",
    note: str = "",
) -> str:
    """Internal implementation — see engram_register_exemplar MCP tool
    for the public payload schema. Kept callable with named kwargs for
    in-server callers.

    Register a post-hoc exemplar as another instance of an existing
    lesson or cornerstone's pattern.

    Post-hoc path for pattern-node growth: when a new observation or
    derivation surfaces that exemplifies a lesson or cornerstone already
    in the graph, attach it with one call instead of superseding the
    target. Writes one `exemplifies` edge (exemplar → target, DAG-exempt).

    For lesson targets, refreshes the error_incidents.json cache that the
    surface hook scans on every prompt.

    Args:
        target_id: ID of an existing lesson or cornerstone node
            (is_current=1).
        exemplar_id: ID of the exemplar node. Must be claim-bearing
            (observation_factual, observation_predictive, derivation, theory,
            axiom, conjecture).
        note: Optional brief annotation of why this exemplar fits the
            target — stored on the edge's metadata field for audit.

    Returns:
        JSON with status + the new edge's source/target, the target's
        current exemplar count, and the target's claim for verification.
        For lesson targets, also includes cache_rebuild result.
    """
    if not target_id or not target_id.strip():
        return json.dumps({"error": "target_id is required and cannot be empty."})
    if not exemplar_id or not exemplar_id.strip():
        return json.dumps({"error": "exemplar_id is required and cannot be empty."})
    conn = core._get_db()
    try:
        target_row = conn.execute(
            "SELECT id, type, claim, metadata, is_current FROM nodes WHERE id = ?",
            (target_id,),
        ).fetchone()
        if not target_row:
            return json.dumps({"error": f"Target node not found: {target_id}"})
        if target_row["type"] not in _VALID_EXEMPLAR_TARGET_TYPES:
            return json.dumps({
                "error": (
                    f"Target must be a lesson or cornerstone node. "
                    f"{target_id} has type '{target_row['type']}'."
                ),
            })
        if not target_row["is_current"]:
            return json.dumps({
                "error": (
                    f"Target {target_id} is superseded (is_current=0). "
                    f"Register against its current replacement."
                ),
            })

        exemplar_row = conn.execute(
            "SELECT id, type FROM nodes WHERE id = ?",
            (exemplar_id,),
        ).fetchone()
        if not exemplar_row:
            return json.dumps({"error": f"Exemplar node not found: {exemplar_id}"})
        if exemplar_row["type"] not in core.CLAIM_BEARING_TYPES:
            return json.dumps({
                "error": (
                    f"Exemplar must be claim-bearing. {exemplar_id} has type "
                    f"'{exemplar_row['type']}'."
                ),
            })

        # Idempotent — report existing edge as already_exists rather than error.
        existing = conn.execute(
            """SELECT id FROM edges
               WHERE source_id = ? AND target_id = ? AND relation = 'exemplifies'""",
            (exemplar_id, target_id),
        ).fetchone()
        if existing:
            return json.dumps({
                "status": "already_exists",
                "edge": {"source": exemplar_id, "target": target_id, "relation": "exemplifies"},
                "target_id": target_id,
                "target_claim": target_row["claim"],
                "exemplar_count": core._count_live_exemplars(conn, target_id, target_row["type"]),
            })

        now = core._now()
        edge_meta = json.dumps({"note": note}) if note else None
        conn.execute(
            """INSERT INTO edges (source_id, target_id, relation, created_at, metadata)
               VALUES (?, ?, 'exemplifies', ?, ?)""",
            (exemplar_id, target_id, now, edge_meta),
        )

        core._utility_reward(conn, [target_id], action="register_exemplar")
        conn.commit()

        # Live count via SSoT helper — no cached field needed (closes #442).
        exemplar_count = core._count_live_exemplars(conn, target_id, target_row["type"])
    finally:
        conn.close()

    result = {
        "status": "created",
        "edge": {
            "source": exemplar_id,
            "target": target_id,
            "relation": "exemplifies",
            "note": note or None,
        },
        "target_id": target_id,
        "target_claim": target_row["claim"],
        "exemplar_count": exemplar_count,
    }

    # Tripwire cache refresh: only fires for lesson targets.
    # For cornerstone targets, no cache today — future cornerstone-tripwire
    # mechanism may extend this.
    if target_row["type"] == "lesson":
        try:
            cache_result = core._rebuild_incidents_cache()
        except Exception as exc:
            cache_result = {"status": "error", "detail": str(exc)}
        result["cache_rebuild"] = cache_result

    return json.dumps(result)
