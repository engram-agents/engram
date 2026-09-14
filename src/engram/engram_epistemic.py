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
import re
import sqlite3

import engram_core as core

# ---------------------------------------------------------------------------
# K-local constants
# ---------------------------------------------------------------------------

_VALID_EXEMPLAR_TARGET_TYPES = frozenset({"lesson", "cornerstone"})

# alpha#1944 — a lesson's PreToolUse behavior at action-moment. "advisory"
# (default) injects the nudge and always allows the call, same as today's
# only behavior. "shadow" is a candidate for blocking: still always allows
# the call, but the hook logs every would-have-blocked fire so a promotion
# gate can measure precision before anything actually blocks. "blocking"
# denies the first matching call and requires a single-use
# ENGRAM_OVERRIDE token to let an identical retry through (see
# engram-lesson-tripwire-hook.py). Promotion path is advisory -> shadow ->
# blocking, earned by a measured zero-false-positive shadow window, never
# declared at mint.
PREVENTION_TYPES = frozenset({"advisory", "shadow", "blocking"})


def _looks_like_prose(pattern: str) -> bool:
    """Discriminate a command-shape regex from a dead-prose situation_pattern.

    Threshold: a literal-space count over 8 (#1864 colleague round, Ariadne +
    Borges, validated on 33 real patterns across 3 graphs — live patterns
    carry 0-5 literal spaces, prose carries 16-70, nothing lands in 6-15,
    so 8 sits in that empty band). The first version of this predicate also
    required an ABSENCE of \\s/\\b/(?: constructs, on the theory that a real
    command-shape regex always writes \\s+ rather than a literal space. That
    leaked on two real, live patterns: a multi-command alternation naturally
    carries literal spaces (`(?:gh pr diff|git diff|gh pr view)` IS three
    space-separated words), and a bare command with no alternation at all
    (`gh issue close`) has neither a construct nor much reason to need one.
    Isolated in its own predicate so a future threshold change is a
    one-function edit.
    """
    return pattern.count(" ") > 8


def _validate_situation_pattern(pattern: str, bypass_prose_check: bool) -> tuple:
    """Shared #1864 gate for a non-empty situation_pattern value.

    Used by both _add_lesson_impl (creation) and _update_lesson_impl (#1826,
    in-place correction) so the compile-check + prose-shape check + bypass
    contract can't drift between the two call sites — #1864 itself shipped
    with a threshold bug found only because two independent colleagues ran
    the real predicate against real data; a second, un-synced copy of this
    logic is exactly the kind of surface a future fix could land in only one
    of. Caller is responsible for the empty-string skip (clearing a pattern
    is always ungated, by design, at both call sites) — this function assumes
    a non-empty pattern.

    Returns (error_response, prose_bypass_exercised): error_response is a
    dict ready for json.dumps() on failure, or None on success.
    """
    try:
        re.compile(pattern)
    except re.error as e:
        return ({
            "error": f"situation_pattern is not a valid regex: {e}. "
                     f"Pattern: {pattern!r}",
        }, False)
    # mutation-tested: see test_prose_situation_pattern_blocked_by_default
    # and test_mutation_check_prose_gate_actually_blocks
    if _looks_like_prose(pattern):
        if not bypass_prose_check:
            n_spaces = pattern.count(" ")
            return ({
                "error": (
                    f"`situation_pattern` looks like prose, not a regex: "
                    f"{n_spaces} literal space(s), over the 8-space "
                    f"threshold (#1864: live patterns run 0-5, prose "
                    f"runs 16-70). It is a valid regex but will never "
                    f"match a real command, so the tripwire would be "
                    f"silently dead. A command-shape pattern looks like "
                    f"`(?:gh\\s+pr\\s+create|ia\\s+write)`. If this is "
                    f"intentional, pass bypass_prose_check=true."
                ),
            }, False)
        return (None, True)
    return (None, False)


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
    try:
        core._rebuild_principle_triggers()  # unified registry (#1698 slice 1)
    except Exception:
        pass  # best-effort; legacy cache above remains the hooks' read path


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

        core._refuse_ncb_confidence("axiom", 1.0)  # #2252 Stage 1.3 write-path guard
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

        # Importance-anchored — elevated base (2.0) with current turn's
        # inflation. Not true forgetting-exemption: survival still requires
        # recall past ~50 turns. Pass importance_base directly so
        # _stamp_new_node writes the anchor in one pass instead of computing
        # an interim value from confidence/surprise and then overwriting it
        # [alpha#2266, alpha#2268].
        core._stamp_new_node(conn, node_id, importance_base=2.0)
        if context:
            core._utility_reward(conn, context, action="citation")
        conn.commit()

        # Durability dual read-back (alpha#2193; supersedes the single
        # same-connection read of #2074/#1947). Same-conn alone is BLIND to
        # writer-side displacement; fresh-conn alone FALSE-ALARMS on
        # reader-side corruption. Disagreement is reported, not guessed.
        # See core.verify_write_durability.
        _verdict, _durability = core.verify_write_durability(conn, node_id)
        if _verdict != core.DURABILITY_DURABLE:
            _indeterminate = _verdict == core.DURABILITY_INDETERMINATE
            return json.dumps({
                "status": (
                    "durability_indeterminate" if _indeterminate
                    else "failed_not_durable"
                ),
                "axiom_id": node_id,
                "durability": _durability,
                "error": (
                    (
                        "Write COMMITTED but its durability cannot be "
                        "confirmed: the writing connection and a fresh "
                        "connection DISAGREE about whether the row is "
                        "present, and nothing available here says which "
                        "view is correct. The node id above may or may not "
                        "resolve. Do NOT cite it until re-verified, and do "
                        "NOT retry automatically -- a retry risks a "
                        "double-write if the row did land. Re-read after a "
                        "checkpoint, or escalate. See alpha#2193."
                    ) if _indeterminate else (
                        "Write did not verify as durable on read-back after "
                        "commit -- the row is missing or its claim is empty "
                        "on BOTH the writing connection and a fresh one. Do "
                        "NOT retry automatically; a retry after a partial "
                        "commit risks a double-write. See alpha#1947, #2142."
                    )
                ),
            })

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
    recall_summary: str = "",
    recall_keywords: list = None,
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
        recall_summary: Always provide when you can — ≤140-char recall scent (#1952 PR1).
            Omission clean-accepts as 0 chars; soft-accepted up to 279 chars
            (flagged needs_tightening); rejected at 280+ chars.
        recall_keywords: Always provide when you can — 3-6 short content keywords.
            Never rejected regardless of count.

    Returns:
        JSON with the new definition node ID.
    """
    if not term or not term.strip():
        return json.dumps({"error": "term is required and cannot be empty."})
    if not definition or not definition.strip():
        return json.dumps({"error": "definition is required and cannot be empty."})

    _recall_summary, _recall_keywords, _needs_tightening, _summary_missing, _recall_error = (
        core._validate_recall_fields(recall_summary, recall_keywords)
    )
    if _recall_error:
        return json.dumps({"error": _recall_error})

    conn = core._get_db()
    try:
        node_id = core._next_id(conn, "definition")
        now = core._now()

        combined_claim = f"{term}: {definition}"
        meta = json.dumps({"term": term, "definition": definition})

        core._refuse_ncb_confidence("definition", None)  # #2252 Stage 1.3 write-path guard
        conn.execute(
            """INSERT INTO nodes (id, type, claim, created_at, status, metadata,
               recall_summary, recall_keywords, needs_tightening, summary_missing)
               VALUES (?, 'definition', ?, ?, 'active', ?, ?, ?, ?, ?)""",
            (
                node_id, combined_claim, now, meta,
                _recall_summary or None,
                json.dumps(_recall_keywords) if _recall_keywords else None,
                1 if _needs_tightening else 0,
                1 if _summary_missing else 0,
            ),
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

        # Importance-anchored — elevated base (2.0) with current turn's
        # inflation. Not true forgetting-exemption: survival still requires
        # recall past ~50 turns. Pass importance_base directly so
        # _stamp_new_node writes the anchor in one pass instead of computing
        # an interim value from confidence/surprise and then overwriting it
        # [alpha#2266, alpha#2268].
        core._stamp_new_node(conn, node_id, importance_base=2.0)
        if context:
            core._utility_reward(conn, context, action="citation")
        conn.commit()

        # Durability dual read-back (alpha#2193; supersedes the single
        # same-connection read of #2074/#1947). Same-conn alone is BLIND to
        # writer-side displacement; fresh-conn alone FALSE-ALARMS on
        # reader-side corruption. Disagreement is reported, not guessed.
        # See core.verify_write_durability.
        _verdict, _durability = core.verify_write_durability(conn, node_id)
        if _verdict != core.DURABILITY_DURABLE:
            _indeterminate = _verdict == core.DURABILITY_INDETERMINATE
            return json.dumps({
                "status": (
                    "durability_indeterminate" if _indeterminate
                    else "failed_not_durable"
                ),
                "definition_id": node_id,
                "durability": _durability,
                "error": (
                    (
                        "Write COMMITTED but its durability cannot be "
                        "confirmed: the writing connection and a fresh "
                        "connection DISAGREE about whether the row is "
                        "present, and nothing available here says which "
                        "view is correct. The node id above may or may not "
                        "resolve. Do NOT cite it until re-verified, and do "
                        "NOT retry automatically -- a retry risks a "
                        "double-write if the row did land. Re-read after a "
                        "checkpoint, or escalate. See alpha#2193."
                    ) if _indeterminate else (
                        "Write did not verify as durable on read-back after "
                        "commit -- the row is missing or its claim is empty "
                        "on BOTH the writing connection and a fresh one. Do "
                        "NOT retry automatically; a retry after a partial "
                        "commit risks a double-write. See alpha#1947, #2142."
                    )
                ),
            })

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
    recall_summary: str = "",
    recall_keywords: list = None,
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
        recall_summary: Always provide when you can — ≤140-char recall scent (#1952 PR1).
            Omission clean-accepts as 0 chars; soft-accepted up to 279 chars
            (flagged needs_tightening); rejected at 280+ chars.
        recall_keywords: Always provide when you can — 3-6 short content keywords.
            Never rejected regardless of count.

    Returns:
        JSON with the new conjecture node ID and confidence.
    """
    if not claim or not claim.strip():
        return json.dumps({"error": "claim is required and cannot be empty."})
    if not basis or not basis.strip():
        return json.dumps({"error": "basis is required and cannot be empty."})

    _recall_summary, _recall_keywords, _needs_tightening, _summary_missing, _recall_error = (
        core._validate_recall_fields(recall_summary, recall_keywords)
    )
    if _recall_error:
        return json.dumps({"error": _recall_error})

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

        core._refuse_ncb_confidence("conjecture", conf)  # #2252 Stage 1.3 write-path guard
        conn.execute(
            """INSERT INTO nodes (id, type, claim, created_at, logical_chain,
               confidence, confidence_history, status, metadata, recall_summary,
               recall_keywords, needs_tightening, summary_missing)
               VALUES (?, 'conjecture', ?, ?, ?, ?, ?, 'active', '{}', ?, ?, ?, ?)""",
            (node_id, claim, now, basis, conf,
             json.dumps([{"confidence": conf, "reason": f"conjecture — initial hypothesis", "timestamp": now}]),
             _recall_summary or None,
             json.dumps(_recall_keywords) if _recall_keywords else None,
             1 if _needs_tightening else 0,
             1 if _summary_missing else 0),
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

        # Durability read-back guard (alpha#2097, #1947 incident) — verify the
        # commit actually landed on THIS connection before reporting
        # status:created. See core.verify_write_durability (dual read, alpha#2193).
        _verdict, _durability = core.verify_write_durability(conn, node_id)
        if _verdict != core.DURABILITY_DURABLE:
            return json.dumps(core.durability_failure_response(
                _verdict, _durability, "conjecture_id", node_id, "#2097"))

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

        core._refuse_ncb_confidence("goal_tension", None)  # #2252 Stage 1.3 write-path guard
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

        # Durability dual read-back (alpha#2193; supersedes the single
        # same-connection read of #2074/#1947). Same-conn alone is BLIND to
        # writer-side displacement; fresh-conn alone FALSE-ALARMS on
        # reader-side corruption. Disagreement is reported, not guessed.
        # See core.verify_write_durability.
        _verdict, _durability = core.verify_write_durability(conn, node_id)
        if _verdict != core.DURABILITY_DURABLE:
            _indeterminate = _verdict == core.DURABILITY_INDETERMINATE
            return json.dumps({
                "status": (
                    "durability_indeterminate" if _indeterminate
                    else "failed_not_durable"
                ),
                "tension_id": node_id,
                "durability": _durability,
                "error": (
                    (
                        "Write COMMITTED but its durability cannot be "
                        "confirmed: the writing connection and a fresh "
                        "connection DISAGREE about whether the row is "
                        "present, and nothing available here says which "
                        "view is correct. The node id above may or may not "
                        "resolve. Do NOT cite it until re-verified, and do "
                        "NOT retry automatically -- a retry risks a "
                        "double-write if the row did land. Re-read after a "
                        "checkpoint, or escalate. See alpha#2193."
                    ) if _indeterminate else (
                        "Write did not verify as durable on read-back after "
                        "commit -- the row is missing or its claim is empty "
                        "on BOTH the writing connection and a fresh one. Do "
                        "NOT retry automatically; a retry after a partial "
                        "commit risks a double-write. See alpha#1947, #2142."
                    )
                ),
            })

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
    situation_pattern: str = "",
    bypass_prose_check: bool = False,
    prevention_type: str = "advisory",
    recall_summary: str = "",
    recall_keywords: list = None,
    generalized_by: str = "",
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

    Lessons are NON-CLAIM-BEARING and IMPORTANCE-ANCHORED (base 2.0, like
    goals) (alpha#2252). They carry no confidence score and cannot serve as
    derivation premises or exemplar sources — the honest maturity signal is
    `core._count_live_exemplars`, not a numeric confidence. They can still
    be superseded (consolidation).

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
        situation_pattern: Optional regex matched against the pending Bash
            command / MCP call at action-moment by the PreToolUse tripwire
            hook (see engram_add_lesson's docstring for the exact matched
            surface). Must compile; must not read as English prose unless
            bypass_prose_check=True (#1864).
        bypass_prose_check: Default False. Overrides the prose-shape gate on
            situation_pattern — see engram_add_lesson's docstring for the
            discriminator + audit-stamp contract.
        prevention_type: One of "advisory" (default), "shadow", "blocking"
            (alpha#1944). Advisory only ever injects the nudge. Shadow and
            blocking are candidates for the PreToolUse tripwire hook to
            actually deny the call — see PREVENTION_TYPES's docstring.
            No lesson may be minted directly as "blocking"; promotion is
            earned via engram_update_lesson after a measured
            zero-false-positive shadow window.
        recall_summary: Always provide when you can — ≤140-char recall scent (#1952 PR1).
            Omission clean-accepts as 0 chars; soft-accepted up to 279 chars
            (flagged needs_tightening); rejected at 280+ chars.
        recall_keywords: Always provide when you can — 3-6 short content keywords.
            Never rejected regardless of count.
        generalized_by: Comma-separated IDs of principle nodes that
            generalize this lesson (#2753 epic WS1, D4). Must be current
            nodes of type lesson or cornerstone (a lesson->lesson
            meta-lesson pair, or cornerstone->lesson — see
            GENERALIZES_ALLOWED_PAIRS). Writes
            `<parent> generalizes <new lesson>` edges at mint time,
            cycle-guarded. Strict: an invalid parent (missing, non-current,
            or wrong-type pair) refuses the whole call before anything is
            created — `generalizes` establishes structural taxonomy, unlike
            incident_ids/context_ids this is not a best-effort field.

    Returns:
        JSON with lesson ID, linked incidents, and similar existing
        lessons for consolidation hints. Returns {"error": "..."} if
        situation_pattern fails to compile as a regex, or looks like prose and
        bypass_prose_check is not set.
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
    if prevention_type not in PREVENTION_TYPES:
        return json.dumps({
            "error": f"Invalid prevention_type '{prevention_type}'.",
            "valid_types": sorted(PREVENTION_TYPES),
        })
    # #1944 — eligibility to block is earned, never declared at mint. A
    # fresh lesson has zero fires and therefore zero measured precision;
    # minting straight to "blocking" would deadlock the first agent who
    # hits a false positive with no prior warning it could happen.
    # Promotion is engram_update_lesson's job, after a real shadow window.
    if prevention_type == "blocking":
        return json.dumps({
            "error": (
                "A lesson cannot be minted directly as prevention_type="
                "'blocking'. Mint as 'advisory' (default) or 'shadow', let "
                "it run a real shadow window, then promote via "
                "engram_update_lesson once a zero-false-positive window is "
                "measured."
            )
        })

    _recall_summary, _recall_keywords, _needs_tightening, _summary_missing, _recall_error = (
        core._validate_recall_fields(recall_summary, recall_keywords)
    )
    if _recall_error:
        return json.dumps({"error": _recall_error})

    # Per the bool-string-truthy lesson: bool-string truthy trap at wrapper boundary. JSON-string
    # "false" is truthy in Python; without this check, a bypass_prose_check: "false"
    # payload would silently bypass the #1864 prose-shape guard.
    # Same guard pattern as _derive_impl's use_stale/use_contested (engram_derive.py).
    if not isinstance(bypass_prose_check, bool):
        return json.dumps({
            "error": (
                f"bypass_prose_check must be a JSON boolean (true/false), got "
                f"{type(bypass_prose_check).__name__}: {bypass_prose_check!r}. "
                "Some MCP clients emit booleans as strings — make sure your "
                "JSON encodes `true`/`false` not `\"true\"`/`\"false\"`."
            )
        })

    # #1864 — situation_pattern must be a compilable, command-shape regex.
    # Compile check runs BEFORE the prose-shape check regardless of
    # bypass_prose_check: the bypass only overrides the prose gate, never
    # lets a malformed regex through.
    prose_bypass_exercised = False
    if situation_pattern:
        error, prose_bypass_exercised = _validate_situation_pattern(
            situation_pattern, bypass_prose_check
        )
        if error is not None:
            return json.dumps(error)

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
            elif not core._is_claim_bearing(row["type"]):
                wrong_type.append(f"{sid} (type: {row['type']})")
        if missing:
            return json.dumps({"error": f"Incident node(s) not found: {', '.join(missing)}"})
        if wrong_type:
            return json.dumps({
                "error": f"Incident nodes must be claim-bearing. "
                         f"Cannot cite: {', '.join(wrong_type)}.",
            })

        # `generalized_by` validation (#2753 epic WS1, D4) — STRICT,
        # fail-before-insert: unlike incident_ids/context_ids (which either
        # hard-fail on missing incidents or best-effort skip missing
        # context), a `generalizes` parent establishes structural taxonomy,
        # and a mechanically-enforced endpoint gate already exists
        # everywhere else this relation is written (_add_edge_impl in
        # engram_cornerstone.py) — silently skipping an invalid parent here
        # would leave a lesson dangling out of its intended abstraction
        # hierarchy with no signal that happened.
        generalized_by_ids = list(dict.fromkeys(
            s.strip() for s in core._as_csv(generalized_by).split(",") if s.strip()
        ))
        for pid in generalized_by_ids:
            prow = conn.execute(
                "SELECT id, type, is_current FROM nodes WHERE id = ?", (pid,)
            ).fetchone()
            if not prow:
                return json.dumps({
                    "error": f"generalized_by parent '{pid}' not found."
                })
            if not prow["is_current"]:
                return json.dumps({
                    "error": (
                        f"generalized_by parent '{pid}' is not current "
                        f"(retracted or superseded). Use "
                        f"engram_inspect('{pid}') to find the current "
                        f"successor."
                    )
                })
            pair = (prow["type"], "lesson")
            if pair not in core.GENERALIZES_ALLOWED_PAIRS:
                allowed_sources = sorted(
                    t for t, tgt in core.GENERALIZES_ALLOWED_PAIRS if tgt == "lesson"
                )
                return json.dumps({
                    "error": (
                        f"'generalizes' does not accept a "
                        f"{prow['type']} -> lesson pair (parent '{pid}'). "
                        f"Allowed source types for a lesson target: "
                        f"{allowed_sources}."
                    )
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

        # Lessons are non-claim-bearing (alpha#2252) — no confidence is
        # computed or stored. confidence/confidence_history are omitted from
        # the INSERT below, matching the house convention for NCB types
        # (cornerstone/goal): confidence defaults to NULL (no column
        # DEFAULT), confidence_history defaults to '[]' (schema DEFAULT).
        # The honest maturity signal for a lesson is
        # core._count_live_exemplars, not a numeric confidence — see Q2 in
        # the epic's design doc.
        node_id = core._next_id(conn, "lesson")
        now = core._now()

        # reasoning_type/reasoning_class are unrelated metadata (still worth
        # keeping) — only the confidence computation that used to consume
        # reasoning_type + the discount table is dead now.
        rclass = core.REASONING_CLASS.get(reasoning_type, "unknown")

        meta = {
            "reasoning_type": reasoning_type,
            "reasoning_class": rclass,
            "scaffolding_nudge": scaffolding_nudge,
            "prevention_type": prevention_type,
        }
        if situation_pattern:
            meta["situation_pattern"] = situation_pattern
        if prose_bypass_exercised:
            # Auto-stamped only when _looks_like_prose actually fired AND
            # bypass_prose_check=True let it through — never merely because
            # the caller passed the flag (#1864, following engram_derive's
            # built_on_stale/built_on_contested precedent: the stamp records
            # that an override was exercised against a real block condition).
            meta["bypass_prose_check"] = True

        core._refuse_ncb_confidence("lesson", None)  # #2252 Stage 1.3 write-path guard
        conn.execute(
            """INSERT INTO nodes (id, type, claim, created_at, logical_chain,
               metadata, recall_summary,
               recall_keywords, needs_tightening, summary_missing)
               VALUES (?, 'lesson', ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                node_id,
                claim,
                now,
                logical_chain,
                json.dumps(meta),
                _recall_summary or None,
                json.dumps(_recall_keywords) if _recall_keywords else None,
                1 if _needs_tightening else 0,
                1 if _summary_missing else 0,
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

        # `generalized_by` edge writes (#2753 epic WS1, D4): `<parent>
        # generalizes <new lesson>`, cycle-guarded. Structurally
        # unreachable for a brand-new node (it carries no prior generalizes
        # edges of its own), but checked anyway — every `generalizes` write
        # path goes through the same guard (core._generalizes_cycle_path),
        # so the invariant is uniform and grep-able rather than "trust the
        # caller can't construct a cycle here."
        generalizing_parents = []
        for pid in generalized_by_ids:
            cycle_path = core._generalizes_cycle_path(conn, pid, node_id)
            if cycle_path is not None:
                return json.dumps({
                    "error": (
                        f"'generalizes' edge {pid} -> {node_id} would "
                        f"close a cycle among live `generalizes` edges: "
                        f"{' -> '.join(cycle_path)}. Refused — see #2753 "
                        f"epic D1 (write-time cycle guard)."
                    )
                })
            conn.execute(
                "INSERT INTO edges (source_id, target_id, relation, created_at) VALUES (?, ?, 'generalizes', ?)",
                (pid, node_id, now),
            )
            generalizing_parents.append(pid)

        # Lessons are importance-anchored at a fixed value (2.0), like goals
        # and cornerstones — lessons persist. Pass importance_base directly
        # so _stamp_new_node writes the anchor in one pass instead of
        # computing an interim value from confidence/surprise and then
        # overwriting it [alpha#2266, alpha#2268].
        core._stamp_new_node(conn, node_id, importance_base=2.0)
        conn.commit()

        # Durability dual read-back (alpha#2193; supersedes the single
        # same-connection read of #2074/#1947). Same-conn alone is BLIND to
        # writer-side displacement; fresh-conn alone FALSE-ALARMS on
        # reader-side corruption. Disagreement is reported, not guessed.
        # See core.verify_write_durability.
        _verdict, _durability = core.verify_write_durability(conn, node_id)
        if _verdict != core.DURABILITY_DURABLE:
            _indeterminate = _verdict == core.DURABILITY_INDETERMINATE
            return json.dumps({
                "status": (
                    "durability_indeterminate" if _indeterminate
                    else "failed_not_durable"
                ),
                "lesson_id": node_id,
                "durability": _durability,
                "error": (
                    (
                        "Write COMMITTED but its durability cannot be "
                        "confirmed: the writing connection and a fresh "
                        "connection DISAGREE about whether the row is "
                        "present, and nothing available here says which "
                        "view is correct. The node id above may or may not "
                        "resolve. Do NOT cite it until re-verified, and do "
                        "NOT retry automatically -- a retry risks a "
                        "double-write if the row did land. Re-read after a "
                        "checkpoint, or escalate. See alpha#2193."
                    ) if _indeterminate else (
                        "Write did not verify as durable on read-back after "
                        "commit -- the row is missing or its claim is empty "
                        "on BOTH the writing connection and a fresh one. Do "
                        "NOT retry automatically; a retry after a partial "
                        "commit risks a double-write. See alpha#1947, #2142."
                    )
                ),
            })

        # Update the incident index (best-effort, outside transaction)
        _update_incident_index(node_id, claim, scaffolding_nudge, ids)

        result = {
            "status": "created",
            "lesson_id": node_id,
            "claim": claim,
            "reasoning_type": reasoning_type,
            "reasoning_class": rclass,
            "scaffolding_nudge": scaffolding_nudge,
            "prevention_type": prevention_type,
            "incident_nodes": ids,
            "context_nodes": ctx,
            "importance_anchored": True,
        }
        if generalizing_parents:
            result["generalized_by"] = generalizing_parents

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


# ---------------------------------------------------------------------------
# Lesson update impl (engram_update_lesson, #1826)
# ---------------------------------------------------------------------------

# Fields that may be changed in-place via engram_update_lesson — mechanical
# retrieval/actionability metadata that can be refined from real firing
# experience without touching the lesson's epistemic content (#1826, widened
# 2026-08-03 per Lei: situation_pattern is a tunable machine-matching cue;
# scaffolding_nudge is the paired "what to do when it fires" text, same
# non-epistemic class). Do not widen further without a fresh issue: claim,
# logical_chain, reasoning_type remain supersede-only — those ARE the
# load-bearing content. (Pre-alpha#2252, reasoning_type also fed a
# now-removed confidence computation; lessons are non-claim-bearing and
# store no confidence, so that no longer applies — the supersede-only
# rule stands on the load-bearing-content rationale alone.)
#
# prevention_type joined the mutable set with alpha#1944: promotion
# (advisory/shadow -> blocking) and auto-demotion (blocking -> advisory,
# on a measured override-rate threshold) are both in-place transitions on
# the SAME lesson, not a new epistemic claim — mechanical/actionability
# metadata, same class as situation_pattern.
_LESSON_MUTABLE_FIELDS = frozenset({"situation_pattern", "scaffolding_nudge", "prevention_type"})

# #2000 — the subset of _LESSON_MUTABLE_FIELDS that error_incidents.json
# actually carries a copy of (see core._rebuild_incidents_cache: it stores
# scaffolding_nudge per incident, keyed via exemplifies edges off the
# lesson; situation_pattern drives the surface-hook's pattern matching
# against that same cache). Currently identical to _LESSON_MUTABLE_FIELDS,
# but named separately since the two sets answer different questions
# (what's editable vs. what the cache needs refreshed for) and could
# diverge if a future mutable field doesn't touch the cache.
_LESSON_CACHE_FIELDS = frozenset({"situation_pattern", "scaffolding_nudge"})


def _update_lesson_impl(
    node_id: str = "",
    updates: dict = None,
    reason: str = "",
    bypass_prose_check: bool = False,
) -> str:
    """Internal implementation — see engram_update_lesson MCP tool.

    Update mutable fields on an existing lesson node in-place (no supersede,
    no new node). Records prior values in metadata.field_traces (append-only,
    same structure as _update_person_impl). Every call is also logged to the
    edit_history audit table via core._log_edit(action="lesson_field_updated"),
    same as engram_update_person's "person_field_updated" entries — a mutation
    to an existing node still shows up as a graph-mutation event, distinct
    from node creation, without minting a new node.

    Mutable fields (#1826, widened 2026-08-03; #1944 widened again to add
    prevention_type): situation_pattern, scaffolding_nudge, prevention_type
    — all mechanical/actionability metadata tunable from real firing
    experience, not the lesson's epistemic content (claim, logical_chain,
    reasoning_type stay supersede-only). prevention_type is the sole field
    where an update MAY promote a lesson to "blocking" (minting cannot);
    engram_add_lesson refuses "blocking" at creation for exactly this
    reason.

    situation_pattern, when set to a non-empty value, runs the SAME #1864
    validation _add_lesson_impl runs at creation time — must compile as a
    regex, and must not look like prose (per _looks_like_prose) unless
    bypass_prose_check=True. Clearing it to "" is always allowed, ungated —
    falling back to locus-3 semantic-only recall is a legitimate correction,
    not a prose violation. scaffolding_nudge has no such gate — it's free text,
    not a regex, so there's no prose-shape failure mode to guard against.

    Args:
        node_id: Lesson node ID (ls_NNNN). Must be type='lesson', is_current=1.
        updates: Dict of field → new_value. Mutable fields: situation_pattern,
                 scaffolding_nudge, prevention_type. Unknown-field updates are
                 rejected with a clear error.
        reason: Optional human-readable explanation for the change.
        bypass_prose_check: Default False. Overrides the prose-shape gate on
            a new situation_pattern value — see engram_add_lesson's docstring
            for the discriminator + audit-stamp contract. Only stamped into
            metadata when the gate actually fired and was overridden. Has no
            effect on scaffolding_nudge (nothing to bypass).

    Returns:
        JSON with status, node_id, updated_fields, field_traces_delta. When
        the update touches situation_pattern or scaffolding_nudge (#2000),
        also includes cache_rebuild — the error_incidents.json refresh
        result, same shape _register_exemplar_impl returns for lesson
        targets — so the caller can see the file-backed tripwire cache was
        resynced rather than assume it. Omitted when neither field is
        touched. Returns {"error": "..."} on any validation failure (unknown
        field, node not found / wrong type / not current, invalid regex, or
        prose-shaped pattern without bypass_prose_check).
    """
    if updates is None:
        updates = {}
    if not node_id or not node_id.strip():
        return json.dumps({"error": "node_id is required and cannot be empty."})
    if not updates:
        return json.dumps({"error": "updates must be a non-empty dict of field → new_value."})

    unknown_fields = set(updates.keys()) - _LESSON_MUTABLE_FIELDS
    if unknown_fields:
        return json.dumps({
            "error": (
                f"Unknown lesson field(s): {sorted(unknown_fields)}. "
                f"Mutable (updatable): {sorted(_LESSON_MUTABLE_FIELDS)}."
            )
        })

    # Per the bool-string-truthy trap (same guard _add_lesson_impl uses): a
    # JSON-string "false" is truthy in Python; without this check, a
    # bypass_prose_check: "false" payload would silently bypass the #1864
    # prose-shape guard.
    if not isinstance(bypass_prose_check, bool):
        return json.dumps({
            "error": (
                f"bypass_prose_check must be a JSON boolean (true/false), got "
                f"{type(bypass_prose_check).__name__}: {bypass_prose_check!r}. "
                "Some MCP clients emit booleans as strings — make sure your "
                "JSON encodes `true`/`false` not `\"true\"`/`\"false\"`."
            )
        })

    # #1826 — situation_pattern updates get the same #1864 gate as creation.
    # Compile check runs BEFORE the prose-shape check regardless of
    # bypass_prose_check: the bypass only overrides the prose gate, never
    # lets a malformed regex through. Empty string always skips both checks —
    # clearing the field back to locus-3-only recall is ungated by design.
    prose_bypass_exercised = False
    if "situation_pattern" in updates:
        new_pattern = updates["situation_pattern"]
        if new_pattern:
            error, prose_bypass_exercised = _validate_situation_pattern(
                new_pattern, bypass_prose_check
            )
            if error is not None:
                return json.dumps(error)

    # #1944 — a prevention_type update must land on a real state. Unlike
    # minting (which refuses "blocking" outright), an update MAY promote to
    # "blocking" — that's the whole point of the promotion path, earned
    # after a shadow window elsewhere, not re-litigated here.
    if "prevention_type" in updates:
        new_prevention_type = updates["prevention_type"]
        if new_prevention_type not in PREVENTION_TYPES:
            return json.dumps({
                "error": f"Invalid prevention_type '{new_prevention_type}'.",
                "valid_types": sorted(PREVENTION_TYPES),
            })

    conn = core._get_db()
    try:
        row = conn.execute(
            "SELECT id, type, is_current, metadata FROM nodes WHERE id = ?",
            (node_id.strip(),),
        ).fetchone()
        if not row:
            return json.dumps({"error": f"Node '{node_id}' not found."})
        if row["type"] != "lesson":
            return json.dumps({
                "error": (
                    f"Node '{node_id}' is type '{row['type']}', not 'lesson'. "
                    f"engram_update_lesson only operates on lesson nodes."
                )
            })
        if not row["is_current"]:
            return json.dumps({
                "error": (
                    f"Node '{node_id}' is not current (retracted or superseded). "
                    f"Use engram_inspect('{node_id}') to find the current successor."
                )
            })

        meta = json.loads(row["metadata"] or "{}")
        if "field_traces" not in meta:
            meta["field_traces"] = {}

        now = core._now()
        updated_fields = {}
        field_traces_delta = {}

        for field, new_value in updates.items():
            current_value = meta.get(field)

            # Append prior value to trace (append-only — never mutate existing entries).
            if field not in meta["field_traces"]:
                meta["field_traces"][field] = []
            meta["field_traces"][field].append({
                "prior_value": current_value,
                "changed_at": now,
            })
            field_traces_delta[field] = {"prior_value": current_value, "changed_at": now}

            # Apply the new value.
            meta[field] = new_value
            updated_fields[field] = new_value

        if prose_bypass_exercised:
            # Auto-stamped only when _looks_like_prose actually fired AND
            # bypass_prose_check=True let it through — never merely because
            # the caller passed the flag. Mirrors _add_lesson_impl's
            # prose_bypass_exercised contract (#1864, #1826).
            meta["bypass_prose_check"] = True

        conn.execute(
            "UPDATE nodes SET metadata = ? WHERE id = ?",
            (json.dumps(meta), node_id.strip()),
        )

        core._log_edit(conn, "lesson_field_updated", node_id.strip(), "lesson", {
            "updated_fields": updated_fields,
            "reason": reason or None,
        })

        conn.commit()

        result = {
            "status": "updated",
            "node_id": node_id.strip(),
            "updated_fields": updated_fields,
            "field_traces_delta": field_traces_delta,
        }
        if prose_bypass_exercised:
            result["prose_bypass_exercised"] = True
    finally:
        conn.close()

    # #2000 — the incidents cache (error_incidents.json) carries a copy of
    # scaffolding_nudge (and is keyed off situation_pattern-driven matching
    # at the surface-hook layer); a correction to either field must refresh
    # it or the file-backed tripwire surface keeps serving the pre-correction
    # text indefinitely. Mirrors _register_exemplar_impl's lesson-target
    # cache_rebuild shape so callers can see the refresh happened. Cheap
    # symmetry, not a blanket rebuild — skipped when the update touches
    # neither cache-relevant field.
    if _LESSON_CACHE_FIELDS & set(updated_fields.keys()):
        try:
            cache_result = core._rebuild_incidents_cache()
        except Exception as exc:
            cache_result = {"status": "error", "detail": str(exc)}
        result["cache_rebuild"] = cache_result

    return json.dumps(result)


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
        if not core._is_claim_bearing(exemplar_row["type"]):
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

        # #2252 Stage 1.1 Part 3 — cheap write-time discharge: registering
        # this exemplar IS a re-wire event against target_id, so clear a
        # stale support_lost marker right here instead of waiting for the
        # next reflect run. Convenience only, same as engram_revision's
        # Part 2 hook — engram_lifecycle._reflect_impl's state-based
        # recompute (Part 1) is the load-bearing discharge and covers this
        # target even if this hook is ever skipped or bypassed. It IS
        # bypassable: engram_add_edge can create an 'exemplifies' edge
        # directly without going through this function (the relation is in
        # core._ADDABLE_AFTER_CREATION_RELATIONS, gated only for
        # 'instantiates'->lesson) — a third exemplar-creation surface
        # alongside this one and the initial-creation path in
        # _add_lesson_impl, neither of which routes through this hook.
        target_meta = json.loads(target_row["metadata"] or "{}")
        if target_meta.get("support_lost") and exemplar_count > 0:
            target_meta.pop("support_lost", None)
            conn.execute(
                "UPDATE nodes SET metadata = ? WHERE id = ?",
                (json.dumps(target_meta), target_id),
            )
            core._log_edit(conn, "support_lost_cleared", target_id, target_row["type"],
                      {"trigger": exemplar_id})
            conn.commit()
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

    # Tripwire cache refresh — each target type has its own hot-path cache
    # scanned by the surface hook (#1691: cornerstone anchors join lessons).
    if target_row["type"] == "lesson":
        try:
            cache_result = core._rebuild_incidents_cache()
        except Exception as exc:
            cache_result = {"status": "error", "detail": str(exc)}
        result["cache_rebuild"] = cache_result
    elif target_row["type"] == "cornerstone":
        try:
            cache_result = core._rebuild_cornerstone_anchors_cache()
        except Exception as exc:
            cache_result = {"status": "error", "detail": str(exc)}
        result["cache_rebuild"] = cache_result
    # Unified registry (#1698 slice 1) — additive alongside the legacy
    # caches until the unified hook check lands (design doc §2 migration).
    if target_row["type"] in ("lesson", "cornerstone"):
        try:
            core._rebuild_principle_triggers()
        except Exception:
            pass  # best-effort; legacy caches remain the hooks' read path
        # Reset-on-incident (#1698 slice 3 §3): a NEW exemplar just landed
        # against this principle — full strength again.
        try:
            core._reset_principle_enactments(target_id)
        except Exception:
            pass  # best-effort; never fail exemplar registration on this

    return json.dumps(result)
