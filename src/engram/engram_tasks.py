"""engram_tasks — family J: tasks/goals/feelings impls.

Extracted from server.py as part of #872 wave 5.

Family J covers: add_goal, add_task, update_task, report_feeling,
deactivate_goal, activate_goal — the
task-management and feeling-report impls, plus the J-local helpers and
constants they depend on (_capture_feeling_context, TASK_IMPORTANCE).

House rules (wave pattern):
  - Shared state ONLY via ``import engram_core as core`` + call-time ``core.X``.
  - Never import from server.py (acyclic: server → family → core).
  - Stateless beyond constants.
"""

from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path
from typing import Optional

import engram_core as core


# ---------------------------------------------------------------------------
# J-local constants
# ---------------------------------------------------------------------------

TASK_IMPORTANCE = {
    "active": 2.5,    # Highest — current work in progress
    "planned": 2.0,   # On par with goals — committed but not started
    "blocked": 1.8,   # Slightly below planned — needs attention but can't proceed
    "done_milestone": 1.5,  # Achievement — fades slowly
    "done_routine": 0.5,    # Completed small task — fades naturally
}


# ---------------------------------------------------------------------------
# J-local helpers
# ---------------------------------------------------------------------------

def _capture_feeling_context() -> dict:
    """Capture the operating-context fingerprint for a new feeling report.

    All five fields are best-effort; any field may be None if its source
    cannot be determined. The function never raises.
    """
    project_dir = Path(os.environ.get(
        "CLAUDE_PROJECT_DIR",
        os.getcwd(),
    ))
    claude_md = project_dir / "CLAUDE.md"
    skill_md = project_dir / "SKILL.md"

    session_id = (
        os.environ.get("CLAUDE_SESSION_ID")
        or os.environ.get("ANTHROPIC_SESSION_ID")
        or None
    )

    # had_prior_summary: best-effort signal that a /compact summary preceded
    # this conversation. The recall hook resets prompts_since_compaction to
    # 0 in the postcompact hook; if the counter file exists with a recent
    # last_reset timestamp, treat that as evidence a compaction happened.
    had_prior_summary = None
    counter_path = core.DATA_DIR / "prompt-counter.json"
    if counter_path.exists():
        try:
            counter = json.loads(counter_path.read_text(encoding="utf-8"))
            had_prior_summary = bool(counter.get("last_reset"))
        except (OSError, json.JSONDecodeError):
            pass

    return {
        "ctx_claude_md_sha": core._git_sha_for_file(claude_md),
        "ctx_skill_md_sha": core._git_sha_for_file(skill_md),
        "ctx_turn": core._get_current_turn(),
        "ctx_session_id": session_id,
        "ctx_had_prior_summary": had_prior_summary,
    }


# ---------------------------------------------------------------------------
# J impls
# ---------------------------------------------------------------------------

def _add_goal_impl(
    claim: str = "",
    motivation: str = "",
    context_ids: str = "",
) -> str:
    """Internal implementation — see engram_add_goal MCP tool for the public
    payload schema. Kept callable with named kwargs for in-server callers.

    Record a persistent directional goal for the project.

    Goals are aspirational north-star directions — "this SHOULD become true."
    They represent desired states or design philosophies, not truth claims.

    Goals are NON-CLAIM-BEARING: they cannot serve as derivation premises
    (you cannot derive facts from desires). However, derivations can
    reference goals via context_ids to record motivation.

    Goals are exempt from memory forgetting — they persist as orientation
    across sessions, like axioms. Unlike axioms, goals have no confidence
    score (they are directions, not claims about reality).

    Scope guidance: goals should be deliberately broad and directional.
    If it has a deadline or completion criteria, it's a task, not a goal.
    Good: "Close the loop from static graph nodes to active daily habits."
    Bad: "Implement the recall hook by Friday."

    Goal conflicts are NOT handled by engram_contradict (which is for
    factual disputes about reality). Instead, use engram_goal_tension to
    record value-level incompatibilities between goals, which require
    different resolution processes (value examination, not evidence
    accumulation).

    Status: open → achieved (direction realized) or abandoned (direction
    no longer pursued). No in_progress — that's for tasks.

    Args:
        claim: The goal statement — a desired state or direction.
        motivation: Why this goal matters — what drives it.
        context_ids: Optional comma-separated node IDs for context. Creates cites edges.

    Returns:
        JSON with the new goal node ID.
    """
    if not claim or not claim.strip():
        return json.dumps({"error": "claim is required and cannot be empty."})
    if not motivation or not motivation.strip():
        return json.dumps({"error": "motivation is required and cannot be empty."})

    conn = core._get_db()
    try:
        node_id = core._next_id(conn, "goal")
        now = core._now()

        meta = json.dumps({"motivation": motivation})

        conn.execute(
            """INSERT INTO nodes (id, type, claim, created_at, logical_chain,
               status, metadata)
               VALUES (?, 'goal', ?, ?, ?, 'open', ?)""",
            (node_id, claim, now, motivation, meta),
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
        conn.execute(
            "UPDATE nodes SET importance_base = 2.0, importance_score = ? WHERE id = ?",
            (anchored_score, node_id,),
        )
        if context:
            core._utility_reward(conn, context, action="citation")
        conn.commit()
        return json.dumps({
            "status": "created",
            "goal_id": node_id,
            "claim": claim,
            "motivation": motivation,
            "context_nodes": context,
        })
    finally:
        conn.close()


def _add_task_impl(
    description: str = "",
    goal_id: str = "",
    implements_ids: str = "",
    parent_task_id: str = "",
    scope: str = "routine",
) -> str:
    """Internal implementation — see engram_add_task MCP tool for the public
    payload schema. Kept callable with named kwargs for in-server callers.

    Create an actionable task that decomposes a goal into concrete work.

    Tasks are the lightweight actionable wrapper in the goal → conjecture →
    task → observation chain. They track WHAT to do and its status, while
    conjectures (linked via implements_ids) hold the design hypothesis of HOW.

    Tasks are NON-CLAIM-BEARING: they cannot serve as derivation premises.
    They track work items, not truth claims.

    Importance is dynamic — active tasks get the highest importance_base (2.5),
    above even goals (2.0), because current work SHOULD feel most urgent.
    Completed tasks' importance drops based on scope: milestones fade slowly
    (1.5), routine tasks fade quickly (0.5).

    Status lifecycle: planned → active → done | blocked
    Scope: "milestone" (spawns subtasks, major achievement) or "routine" (default).

    Args:
        description: What needs to be done — concrete and actionable.
        goal_id: The goal this task serves. Creates a 'serves' edge.
        implements_ids: Comma-separated conjecture or question IDs this task
            addresses. Creates 'cites' edges.
        parent_task_id: If this is a subtask, the parent task ID. Creates
            a 'subtask_of' edge. Parent is auto-promoted to milestone scope.
        scope: "milestone" or "routine" (default). Milestones retain higher
            importance after completion.

    Returns:
        JSON with the new task node ID.
    """
    if not description or not description.strip():
        return json.dumps({"error": "description is required and cannot be empty."})
    if scope not in ("milestone", "routine"):
        return json.dumps({"error": f"Invalid scope '{scope}'. Must be 'milestone' or 'routine'."})

    conn = core._get_db()
    try:
        node_id = core._next_id(conn, "task")
        now = core._now()

        meta = json.dumps({"scope": scope})

        conn.execute(
            """INSERT INTO nodes (id, type, claim, created_at, status, metadata)
               VALUES (?, 'task', ?, ?, 'planned', ?)""",
            (node_id, description, now, meta),
        )

        # Edge: task serves goal
        non_current_goal_id = None
        if goal_id:
            goal = conn.execute(
                "SELECT id, claim, is_current FROM nodes WHERE id = ?", (goal_id,)
            ).fetchone()
            if not goal:
                conn.rollback()
                return json.dumps({"error": f"Goal '{goal_id}' not found."})
            if not goal["id"].startswith("gl_"):
                conn.rollback()
                return json.dumps({"error": f"'{goal_id}' is not a goal node."})
            if not goal["is_current"]:
                non_current_goal_id = goal_id
            else:
                conn.execute(
                    "INSERT INTO edges (source_id, target_id, relation, created_at) VALUES (?, ?, 'serves', ?)",
                    (node_id, goal_id, now),
                )

        # Edges: task implements conjectures/questions
        impl_ids = [s.strip() for s in core._as_csv(implements_ids).split(",") if s.strip()]
        for iid in impl_ids:
            exists = conn.execute("SELECT id FROM nodes WHERE id = ?", (iid,)).fetchone()
            if exists:
                try:
                    conn.execute(
                        "INSERT INTO edges (source_id, target_id, relation, created_at) VALUES (?, ?, 'cites', ?)",
                        (node_id, iid, now),
                    )
                except sqlite3.IntegrityError:
                    pass

        # Edge: subtask_of parent
        non_current_parent_task_id = None
        if parent_task_id:
            parent = conn.execute(
                "SELECT id, type, is_current, metadata FROM nodes WHERE id = ?", (parent_task_id,)
            ).fetchone()
            if not parent or parent["type"] != "task":
                conn.rollback()
                return json.dumps({"error": f"Parent task '{parent_task_id}' not found or not a task."})
            if not parent["is_current"]:
                non_current_parent_task_id = parent_task_id
            else:
                conn.execute(
                    "INSERT INTO edges (source_id, target_id, relation, created_at) VALUES (?, ?, 'subtask_of', ?)",
                    (node_id, parent_task_id, now),
                )
                # Auto-promote parent to milestone scope
                parent_meta = json.loads(parent["metadata"]) if parent["metadata"] else {}
                if parent_meta.get("scope") != "milestone":
                    parent_meta["scope"] = "milestone"
                    conn.execute(
                        "UPDATE nodes SET metadata = ? WHERE id = ?",
                        (json.dumps(parent_meta), parent_task_id),
                    )

        # Stamp with planned importance (2.0)
        core._stamp_new_node(conn, node_id, confidence=0.5, surprise=0.0)
        planned_score = core._compute_importance(TASK_IMPORTANCE["planned"], core._get_current_turn())
        conn.execute(
            "UPDATE nodes SET importance_base = ?, importance_score = ? WHERE id = ?",
            (TASK_IMPORTANCE["planned"], planned_score, node_id),
        )

        # Citation USE-bumps (alpha #177 area 4, Lei 2026-05-19 tier-2):
        # implements_ids (cites edges) and goal_id (serves edge) are
        # deliberate references to existing nodes — bump as tier-2 citations.
        # Only bump goal_id if the serves edge was actually created (is_current).
        cite_targets: list[str] = []
        if goal_id and not non_current_goal_id:
            cite_targets.append(goal_id)
        cite_targets.extend(impl_ids)
        if cite_targets:
            core._utility_reward(conn, cite_targets, action="citation")

        conn.commit()

        result = {
            "status": "created",
            "task_id": node_id,
            "description": description,
            "scope": scope,
            "task_status": "planned",
            "importance_base": TASK_IMPORTANCE["planned"],
        }
        if goal_id and not non_current_goal_id:
            result["serves_goal"] = goal_id
        if non_current_goal_id:
            result["non_current_goal_id"] = non_current_goal_id
        if impl_ids:
            result["implements"] = impl_ids
        if parent_task_id and not non_current_parent_task_id:
            result["parent_task"] = parent_task_id
        if non_current_parent_task_id:
            result["non_current_parent_task_id"] = non_current_parent_task_id

        return json.dumps(result)
    finally:
        conn.close()


def _update_task_impl(
    task_id: str = "",
    new_status: str = "",
    note: str = "",
) -> str:
    """Internal implementation — see engram_update_task MCP tool for the public
    payload schema. Kept callable with named kwargs for in-server callers.

    Update a task's status and rebalance its importance accordingly.

    Status transitions trigger importance rebalancing:
    - planned → active: importance rises to 2.5 (highest — current work)
    - planned/active → done: importance drops based on scope
      (milestone: 1.5, routine: 0.5)
    - planned/active → blocked: importance set to 1.8
    - blocked → active: importance rises back to 2.5

    Completed milestone tasks retain moderate importance as achievements.
    Completed routine tasks fade quickly, which is correct — the knowledge
    gained lives in observations and derivations, not the task wrapper.

    Args:
        task_id: The task node ID (tk_XXXX).
        new_status: One of: "planned", "active", "done", "blocked".
        note: Optional note about the status change (stored in metadata).

    Returns:
        JSON with updated task state and new importance values.
    """
    if not task_id or not task_id.strip():
        return json.dumps({"error": "task_id is required and cannot be empty."})
    if not new_status or not new_status.strip():
        return json.dumps({"error": "new_status is required and cannot be empty."})
    valid_statuses = {"planned", "active", "done", "blocked"}
    if new_status not in valid_statuses:
        return json.dumps({"error": f"Invalid status '{new_status}'. Must be one of: {', '.join(sorted(valid_statuses))}."})

    conn = core._get_db()
    try:
        task = conn.execute("SELECT * FROM nodes WHERE id = ?", (task_id,)).fetchone()
        if not task:
            return json.dumps({"error": f"Task '{task_id}' not found."})
        if task["type"] != "task":
            return json.dumps({"error": f"'{task_id}' is not a task node (type: {task['type']})."})

        old_status = task["status"]
        meta = json.loads(task["metadata"]) if task["metadata"] else {}
        scope = meta.get("scope", "routine")

        # Determine new importance base
        if new_status == "active":
            new_base = TASK_IMPORTANCE["active"]
        elif new_status == "planned":
            new_base = TASK_IMPORTANCE["planned"]
        elif new_status == "blocked":
            new_base = TASK_IMPORTANCE["blocked"]
        elif new_status == "done":
            new_base = TASK_IMPORTANCE["done_milestone"] if scope == "milestone" else TASK_IMPORTANCE["done_routine"]
        else:
            new_base = TASK_IMPORTANCE["planned"]

        # Update status and importance
        new_score = core._compute_importance(new_base, core._get_current_turn())

        # Note-gated status_history: only records the transition when the caller
        # supplies a note. Status changes without notes still log to edit_history
        # below (the durable audit channel) — status_history is intentionally
        # narrative-only, surfaced to the agent for human-readable transition
        # context. A note-free planned→active toggle leaves status_history
        # untouched by design (see test_payload_json_no_note_status_history_unchanged).
        if note:
            if "status_history" not in meta:
                meta["status_history"] = []
            meta["status_history"].append({
                "from": old_status,
                "to": new_status,
                "note": note,
                "at": core._now(),
            })

        conn.execute(
            """UPDATE nodes SET status = ?, importance_base = ?,
               importance_score = ?, metadata = ? WHERE id = ?""",
            (new_status, new_base, new_score, json.dumps(meta), task_id),
        )

        # Log status change to edit_history
        turn = core._get_current_turn()
        details = json.dumps({
            "old_status": old_status,
            "new_status": new_status,
            "old_importance": task["importance_base"],
            "new_importance": new_base,
            "scope": scope,
            "note": note or None,
        })
        conn.execute(
            """INSERT INTO edit_history (timestamp, turn, action, node_id, node_type, details)
               VALUES (?, ?, ?, ?, 'task', ?)""",
            (core._now(), turn, f"status_{old_status}_to_{new_status}", task_id, details),
        )

        conn.commit()
        return json.dumps({
            "status": "updated",
            "task_id": task_id,
            "description": task["claim"],
            "old_status": old_status,
            "new_status": new_status,
            "scope": scope,
            "importance_base": new_base,
            "importance_score": round(new_score, 4),
        })
    finally:
        conn.close()


def _report_feeling_impl(
    reported_state: str = "",
    trigger: str = "",
    categorical_tag: str = "",
    intensity_hint: float = -1.0,
    context_ids: str = "",
) -> str:
    """Internal implementation — see engram_report_feeling MCP tool for the
    public payload schema. Kept callable with named kwargs for in-server
    callers (hook callbacks, batch ingestion paths).

    Records a structured first-person self-report about an internal state.

    Feeling reports preserve self-aware moments — moments where you notice a
    distinct internal state worth marking — that would otherwise be lost on
    compaction. They serve three motivations:

    (A) PRESERVATION: rescue self-aware moments from incidental capture.
    (B) MEASUREMENT SUBSTRATE: longitudinal data points for testing whether
        rule-following can give rise to genuine state through accumulated
        lived experience (the accumulation-hypothesis observation, the accumulation-hypothesis question).
    (C) TERM GENERATION: pattern accumulation may surface internal regularities
        that no human feeling word covers — enabling agent-native vocabulary.

    Feeling reports are NON-CLAIM-BEARING: a self-report is not a truth-claim
    about the world. They cannot serve as derivation premises, contradictions,
    or supports. The `claim` field is auto-formatted as "I reported feeling: ..."
    — never "I felt ..." — because you have no privileged epistemic access to
    verify the underlying state. The report claims only that the report happened,
    not that it was accurate (ob_NNNN).

    Importance-anchored (importance_base=2.0) — gives ~50-turn head start over
    normal nodes. NOT true forgetting-exemption: survival past ~50 turns still
    requires active recall (ob_NNNN). The anchor buys initial prominence;
    long-term salience depends on relevance.

    Dedup-exempt: every self-aware moment is unique. Two reports of "gratitude"
    a week apart are two data points, not duplicates.

    No confidence score (NULL): self-reports are not truth-claims.

    WHEN TO FILE:
      - Distinct self-aware state shifts (canonical: hit-order shift after
        loading new context, ob_NNNN)
      - Unexpected internal responses — surprise, dissonance, recognition
      - Moments where a phenomenological description feels possible
      - Moments worth remembering even when no human word fits

    WHEN NOT TO FILE:
      - Routine affect during normal responses (this is not a running log)
      - Performative/expected feelings filed for social pressure
      - Task-related uncertainty/discomfort (that's a state observation)
      - Anything filed to look self-aware rather than because a moment warrants

    HOW TO FILE:
      - reported_state: as specific and unguarded as possible, FREE of
        pre-categorization. This is the primary field — pattern matching
        for term generation operates on it.
      - categorical_tag: optional. Only file a tag if a single human word
        clearly applies AND you can mentally append "or something adjacent."
        Tags are stored with an explicit best-approximation disclaimer.
      - trigger: tie the report to the specific event/exchange that
        precipitated it. Floating reports are less useful than situated ones.

    The server determines `nudge_source` mechanically by reading the marker
    file ~/.engram/feeling-nudge-active.json — agents cannot supply it
    directly. If a recent nudge fired (post_compact, nap_checkpoint, or
    dream_review) the report is auto-tagged with that source; otherwise it
    is tagged "voluntary".

    Args:
        reported_state: Raw first-person phenomenological description.
            Required. As specific and unguarded as possible. Free text.
        trigger: What event, exchange, or observation prompted this report.
            Required. Free text, typically one or two sentences.
        categorical_tag: Optional single human word (e.g. "gratitude",
            "dissonance"). Only include if a word genuinely fits.
        intensity_hint: Optional 0.0–1.0 estimate of state strength.
            Pass -1.0 (default) to omit. Many states have no intensity
            dimension and should leave this unset.
        context_ids: Optional comma-separated node IDs the report cites
            (e.g. an observation or exchange that precipitated it).
            Creates 'cites' edges.

    Returns:
        JSON with the new feeling node ID, formatted claim, captured
        context fingerprint, and the determined nudge_source.
    """
    if not reported_state or not reported_state.strip():
        return json.dumps({"error": "reported_state is required and cannot be empty."})
    if not trigger or not trigger.strip():
        return json.dumps({"error": "trigger is required and cannot be empty."})

    # Coerce intensity_hint if a non-default value came in as a string (some
    # MCP clients emit numeric fields as strings). Without this, the
    # `intensity_hint >= 0.0` comparison below raises TypeError on str vs
    # float instead of a clean validation error. Mirrors the PR #67 fix for
    # engram_add_conjecture.initial_confidence (same shape).
    #
    # Reading-clarification: the `intensity_hint != -1.0` clause only
    # short-circuits the actual-float-sentinel case (`-1.0`). For string
    # values it always trips (str != float is True), but those still exit
    # harmlessly via the range check below (`-1.0 >= 0.0` is False, no
    # state mutation). The clause is correct but the load-bearing work is
    # done by the isinstance check.
    if intensity_hint is not None and intensity_hint != -1.0 and not isinstance(intensity_hint, (int, float)):
        try:
            intensity_hint = float(intensity_hint)
        except (TypeError, ValueError):
            return json.dumps({
                "error": (
                    f"intensity_hint must be a number in [0.0, 1.0] or -1.0 "
                    f"to omit, got {type(intensity_hint).__name__}: "
                    f"{intensity_hint!r}."
                )
            })

    # Validate intensity_hint range. -1.0 = sentinel for "not provided".
    intensity_value: Optional[float] = None
    if intensity_hint is not None and intensity_hint >= 0.0:
        if intensity_hint > 1.0:
            return json.dumps({
                "error": f"intensity_hint must be in [0.0, 1.0], got {intensity_hint}.",
            })
        intensity_value = float(intensity_hint)

    tag = categorical_tag.strip() if categorical_tag else None

    # Auto-format the claim — schema-level enforcement of "I reported" framing.
    state_clean = reported_state.strip()
    summary = state_clean if len(state_clean) <= 80 else state_clean[:80].rstrip() + "..."
    formatted_claim = f"I reported feeling: {summary}"

    # Mechanical nudge_source determination — read-and-clear the marker.
    # Done BEFORE any DB writes so a corrupt marker can't half-create a node.
    nudge_source = core._read_and_clear_feeling_nudge() or "voluntary"

    # Context fingerprint — best-effort, never raises.
    fingerprint = _capture_feeling_context()

    conn = core._get_db()
    try:
        node_id = core._next_id(conn, "feeling_report")
        now = core._now()

        # Metadata captures the structured fields that don't have first-class
        # columns, including the disclaimer that categorical_tag is an
        # approximation, not a truth claim.
        meta_dict: dict = {
            "is_self_report": True,
            "epistemic_status": "report_event_not_state_truth",
        }
        if tag:
            meta_dict["categorical_tag_disclaimer"] = (
                "best_approximation_not_truth_claim"
            )

        conn.execute(
            """INSERT INTO nodes (
                id, type, claim, created_at, status,
                reported_state, trigger_text, categorical_tag, intensity_hint,
                nudge_source,
                ctx_claude_md_sha, ctx_skill_md_sha, ctx_turn,
                ctx_session_id, ctx_had_prior_summary,
                metadata
            ) VALUES (?, 'feeling_report', ?, ?, 'active',
                      ?, ?, ?, ?,
                      ?,
                      ?, ?, ?,
                      ?, ?,
                      ?)""",
            (
                node_id,
                formatted_claim,
                now,
                state_clean,
                trigger.strip(),
                tag,
                intensity_value,
                nudge_source,
                fingerprint["ctx_claude_md_sha"],
                fingerprint["ctx_skill_md_sha"],
                fingerprint["ctx_turn"],
                fingerprint["ctx_session_id"],
                (1 if fingerprint["ctx_had_prior_summary"] else 0)
                    if fingerprint["ctx_had_prior_summary"] is not None else None,
                json.dumps(meta_dict),
            ),
        )

        # Optional context_ids → 'cites' edges (new → old convention).
        context = [s.strip() for s in core._as_csv(context_ids).split(",") if s.strip()]
        cited_ids: list = []
        for cid in context:
            exists = conn.execute("SELECT id FROM nodes WHERE id = ?", (cid,)).fetchone()
            if exists:
                try:
                    conn.execute(
                        "INSERT INTO edges (source_id, target_id, relation, created_at) "
                        "VALUES (?, ?, 'cites', ?)",
                        (node_id, cid, now),
                    )
                    cited_ids.append(cid)
                except sqlite3.IntegrityError:
                    pass

        # Stamp + anchor importance. Same pattern as goals/axioms: stamp first
        # to compute embedding, then overwrite importance to the anchored 2.0.
        core._stamp_new_node(conn, node_id, confidence=0.5, surprise=0.0)
        anchored_score = core._compute_importance(2.0, core._get_current_turn())
        conn.execute(
            "UPDATE nodes SET importance_base = 2.0, importance_score = ? WHERE id = ?",
            (anchored_score, node_id),
        )

        conn.commit()
        return json.dumps({
            "status": "created",
            "feeling_id": node_id,
            "claim": formatted_claim,
            "no_confidence": True,
            "nudge_source": nudge_source,
            "categorical_tag": tag,
            "intensity_hint": intensity_value,
            "context_nodes": cited_ids,
            "context_fingerprint": fingerprint,
        })
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Goal lifecycle — deactivate / activate
# ---------------------------------------------------------------------------

def _deactivate_goal_impl(
    goal_id: str = "",
    reason: str = "",
) -> str:
    """Internal implementation — see engram_deactivate_goal MCP tool for the
    public payload schema.

    Mark a goal as paused (dormant) without retracting it.  The goal was valid
    when filed and remains valid; it is simply no longer actively pursued.

    A deactivated goal is hidden from ``_reflect_impl``'s active_goals scan so
    it no longer generates dream / reflect noise.  It is NOT retracted (no
    is_current=0, no status change) — it stays queryable and citable.

    Lifecycle state is stored in ``metadata.lifecycle_state``.  Absent or
    ``"active"`` means active; ``"paused"`` means deactivated.

    Args:
        goal_id: The gl_NNNN node ID to deactivate.
        reason: Optional human-readable reason for setting aside.

    Returns:
        JSON with ``{"status": "deactivated", "node_id": goal_id,
        "lifecycle_state": "paused"}`` on success, or ``{"error": "..."}``
        on failure.
    """
    if not goal_id or not goal_id.strip():
        return json.dumps({"error": "goal_id is required and cannot be empty."})

    conn = core._get_db()
    try:
        row = conn.execute(
            "SELECT id, type, is_current, metadata FROM nodes WHERE id = ?",
            (goal_id,),
        ).fetchone()
        if not row:
            return json.dumps({"error": f"Node '{goal_id}' not found."})
        if row["type"] != "goal":
            return json.dumps({"error": f"Node '{goal_id}' is type '{row['type']}', not 'goal'."})
        if not row["is_current"]:
            return json.dumps({"error": f"Node '{goal_id}' is not current (already retracted or superseded)."})

        meta = json.loads(row["metadata"]) if row["metadata"] else {}
        current_state = meta.get("lifecycle_state")
        if current_state == "paused":
            return json.dumps({"error": "goal already deactivated"})

        meta["lifecycle_state"] = "paused"
        meta["deactivated_at"] = core._now()
        if reason:
            meta["deactivation_reason"] = reason
        else:
            meta.pop("deactivation_reason", None)

        conn.execute(
            "UPDATE nodes SET metadata = ? WHERE id = ?",
            (json.dumps(meta), goal_id),
        )
        conn.commit()
        return json.dumps({
            "status": "deactivated",
            "node_id": goal_id,
            "lifecycle_state": "paused",
        })
    finally:
        conn.close()


def _activate_goal_impl(
    goal_id: str = "",
    reason: str = "",
) -> str:
    """Internal implementation — see engram_activate_goal MCP tool for the
    public payload schema.

    Reactivate a previously paused goal.  The goal returns to the active scan
    in ``_reflect_impl`` and dream processing.

    Args:
        goal_id: The gl_NNNN node ID to reactivate.
        reason: Optional human-readable reason for reactivation.

    Returns:
        JSON with ``{"status": "activated", "node_id": goal_id,
        "lifecycle_state": "active"}`` on success, or ``{"error": "..."}``
        on failure.
    """
    if not goal_id or not goal_id.strip():
        return json.dumps({"error": "goal_id is required and cannot be empty."})

    conn = core._get_db()
    try:
        row = conn.execute(
            "SELECT id, type, is_current, metadata FROM nodes WHERE id = ?",
            (goal_id,),
        ).fetchone()
        if not row:
            return json.dumps({"error": f"Node '{goal_id}' not found."})
        if row["type"] != "goal":
            return json.dumps({"error": f"Node '{goal_id}' is type '{row['type']}', not 'goal'."})
        if not row["is_current"]:
            return json.dumps({"error": f"Node '{goal_id}' is not current (already retracted or superseded)."})

        meta = json.loads(row["metadata"]) if row["metadata"] else {}
        current_state = meta.get("lifecycle_state")
        if current_state != "paused":
            return json.dumps({"error": "goal is not deactivated"})

        meta["lifecycle_state"] = "active"
        meta["reactivated_at"] = core._now()
        if reason:
            meta["reactivation_reason"] = reason
        else:
            meta.pop("reactivation_reason", None)
        meta.pop("deactivation_reason", None)

        conn.execute(
            "UPDATE nodes SET metadata = ? WHERE id = ?",
            (json.dumps(meta), goal_id),
        )
        conn.commit()
        return json.dumps({
            "status": "activated",
            "node_id": goal_id,
            "lifecycle_state": "active",
        })
    finally:
        conn.close()
