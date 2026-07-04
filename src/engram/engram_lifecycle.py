"""engram_lifecycle — lifecycle/checkpoint (family D) impls for the ENGRAM MCP server.

Extracted from server.py in #872 wave 3.

HOUSE RULES (mirror engram_core.py § HOUSE RULES):
- Access shared state ONLY via ``import engram_core as core; core.NAME`` — never
  via ``from engram_core import NAME``.
- This module must not import server.py (acyclic: server → family → core).
- No module-level mutable assignments — all state lives in engram_core.

Wave-3 structural note — diagnostic snapshot and calibration snapshot:
  ``_checkpoint_internal`` previously called ``engram_diagnose()`` (a server.py
  @mcp.tool) to store a diagnostic snapshot inside diagnostic_history.  Because
  family modules may not import server.py, that call has been lifted into the
  server.py wrappers (``engram_nap`` / ``engram_advance_turn``), which add the
  ``diagnostic_snapshot`` key to the result after calling the lifecycle impl.
  Behaviour is preserved end-to-end; only the call site moves.

  Similarly, ``_reflect_impl`` previously called ``engram_stats()`` to append a
  ``calibration_snapshot``.  That call is now in the ``engram_reflect`` wrapper
  in server.py.  The impl returns the full report without calibration_snapshot;
  the wrapper appends it.
"""

import json
import os
from pathlib import Path

import engram_core as core


# ---------------------------------------------------------------------------
# Reflect-output shaping constants (family-local — single consumer: _reflect_impl
# / _slim_reflect_briefing)
# ---------------------------------------------------------------------------

_REFLECT_TOP_N = {
    "active_goals": 10,
    "active_tasks": 10,
    "active_lessons": 10,
    "unresolved_goal_tensions": 5,
    "unresolved_contradictions": 5,
    "tainted_nodes": 5,
    "stale_nodes": 5,
    "support_lost_nodes": 5,
    "overdue_predictions": 5,
    "open_conjectures": 5,
    "recent_feeling_reports": 5,
    "weakly_grounded": 3,
    "thin_support_derivations": 3,
    "open_questions": 3,
    "uncited_observations": 3,
    "same_source_review": 3,
    "retracted_nodes": 3,
    "open_predictions": 3,
}
_REFLECT_FIELD_MAX = 200
_REFLECT_ACTIONS_MAX = 5
_REFLECT_TRUNC_FIELDS = {
    "claim", "question", "description", "goal", "task", "reason", "interpretation",
    "predicted_event", "event", "reported_state", "trigger",
}
_REFLECT_LIST_ORDER = [
    "active_goals", "active_tasks", "active_lessons",
    "unresolved_goal_tensions", "unresolved_contradictions",
    "tainted_nodes", "stale_nodes", "support_lost_nodes",
    "overdue_predictions", "open_conjectures",
    "recent_feeling_reports",
    "weakly_grounded", "thin_support_derivations", "open_questions",
    "uncited_observations", "same_source_review", "retracted_nodes",
    "open_predictions",
]


# ---------------------------------------------------------------------------
# Checkpoint logic (shared by nap + advance_turn)
# ---------------------------------------------------------------------------

def _checkpoint_internal(message: str, advance_turn: bool) -> str:
    """Internal checkpoint logic shared by engram_nap and engram_advance_turn.

    Computes graph stats, appends to session_log, snapshots + commits.
    If advance_turn is True, increments the global turn counter (driving the
    forgetting mechanism). If False, leaves the turn counter alone (nap mode)
    and arms a nap_checkpoint feeling nudge.

    The two public tools (engram_nap and engram_advance_turn) exist as
    distinct MCP entry points so a missed kwarg cannot accidentally promote
    a nap into a turn-advancing checkpoint — the prior single-tool design
    was bug-prone under context pressure (brief automated sessions especially).

    Note: the ``diagnostic_snapshot`` key is NOT in this function's return
    value. The server.py wrappers (engram_nap / engram_advance_turn) append
    it after calling this impl, using engram_diagnose() which lives in
    server.py. See module docstring for the wave-3 structural note.
    """
    mode = "session" if advance_turn else "nap"
    mem = core._get_memory_config()
    old_turn = mem.get("current_turn", 0)

    if advance_turn:
        new_turn = old_turn + 1
        core._set_current_turn(new_turn)
    else:
        new_turn = old_turn  # no advancement

    conn = core._get_db()
    try:
        # Collect overall stats
        node_count = conn.execute("SELECT COUNT(*) as c FROM nodes").fetchone()["c"]
        edge_count = conn.execute("SELECT COUNT(*) as c FROM edges").fetchone()["c"]
        current_count = conn.execute(
            "SELECT COUNT(*) as c FROM nodes WHERE is_current = 1"
        ).fetchone()["c"]

        type_counts = {}
        for row in conn.execute(
            "SELECT type, COUNT(*) as c FROM nodes WHERE is_current = 1 GROUP BY type"
        ).fetchall():
            type_counts[row["type"]] = row["c"]

        # What was added this turn (nodes created at the current turn)
        new_nodes = conn.execute(
            "SELECT id, type, claim FROM nodes WHERE recall_turn = ? ORDER BY id",
            (old_turn,),
        ).fetchall()
        new_node_summary = [
            {"id": r["id"], "type": r["type"], "claim": (r["claim"] or "")[:80]}
            for r in new_nodes
        ]

        # Memory tier stats (two-tier: queryable vs faded; tier-1 retired)
        t2 = core._get_tier_threshold(conn, 2)
        tier2_count = conn.execute(
            "SELECT COUNT(*) as c FROM nodes WHERE is_current = 1 AND COALESCE(importance_score, 0) >= ?",
            (t2,),
        ).fetchone()["c"] if t2 > 0 else current_count

        # Append to session log
        now = core._now()
        new_nodes_text = "\n".join(
            f"  - {n['id']} ({n['type']}): {n['claim']}" for n in new_node_summary
        ) if new_node_summary else "  (no new nodes)"

        mode_label = f"Turn {new_turn}" if mode != "nap" else f"Nap (turn {old_turn})"
        log_entry = (
            f"\n## {mode_label} — {now[:19]}\n\n"
            f"{message}\n\n"
            f"**New nodes ({len(new_node_summary)}):**\n{new_nodes_text}\n\n"
            f"**Graph totals:** {current_count} current nodes ({node_count} total), {edge_count} edges\n"
            f"**By type:** {', '.join(f'{t}: {c}' for t, c in sorted(type_counts.items()))}\n"
            f"**Memory tiers:** queryable={tier2_count}, total={current_count}\n"
            f"\n---\n"
        )

        # Create or append to log file
        if not core.LOG_PATH.exists():
            core.LOG_PATH.write_text("# KG Memory Session Log\n\n---\n", encoding="utf-8")
        with open(core.LOG_PATH, "a", encoding="utf-8") as f:
            f.write(log_entry)

        # Snapshot + git commit (durable version-controlled record + restore point)
        # Best-effort: never raises. If git is unavailable, the checkpoint still
        # succeeds and the version_control field reports why.
        version_control = core._commit_snapshot(conn, message, mode)

        result = {
            "status": "checkpoint_saved",
            "mode": mode,
            "previous_turn": old_turn,
            "current_turn": new_turn,
            "turn_advanced": mode != "nap",
            "message": message,
            "log_path": str(core.LOG_PATH),
            "new_nodes_this_session": len(new_node_summary),
            "graph_stats": {
                "total_nodes": node_count,
                "current_nodes": current_count,
                "total_edges": edge_count,
                "by_type": type_counts,
            },
            "memory_tiers": {
                "tier2_queryable": tier2_count,
                "tier3_total": current_count,
                "decay_base": mem.get("decay_base", 1.014),
            },
            "version_control": version_control,
        }

        # Nap-cycle feeling-report nudge (§6 Marker file protocol). After
        # session-end review the agent is already in a reflective posture,
        # so this is a low-cost capture point. The marker is written BEFORE
        # returning so any subsequent engram_report_feeling within TTL gets
        # auto-tagged `nap_checkpoint`. Null results are explicitly valid.
        if mode == "nap":
            result["feeling_report_nudge"] = (
                "Reviewing this session's work, did any specific moment "
                "produce a distinct internal state worth marking — surprise, "
                "recognition, dissonance, unusual satisfaction, discomfort "
                "that isn't task-friction? If yes, file via "
                "engram_report_feeling — it will be auto-tagged "
                "nap_checkpoint. If not, end the turn with NO output - do not "
                "reply to or acknowledge this check; a text-only acknowledgment "
                "wastes a turn. A null result is a valid data point — emitting "
                "no output is how you record it."
            )
            core._write_feeling_nudge("nap_checkpoint")

        return json.dumps(result)
    finally:
        conn.close()


def _nap_impl(message: str) -> str:
    """Impl for engram_nap — callable with named kwargs for in-server callers."""
    return _checkpoint_internal(message, advance_turn=False)


def _advance_turn_impl(message: str) -> str:
    """Impl for engram_advance_turn — callable with named kwargs for in-server callers."""
    return _checkpoint_internal(message, advance_turn=True)


# ---------------------------------------------------------------------------
# Reflect output shaping
# ---------------------------------------------------------------------------

def _slim_reflect_briefing(report: dict) -> dict:
    """Trim a full reflect report to ~5KB pre-dream briefing size.

    - Always preserves `summary`, `memory_tiers`, `recommended_actions`.
    - Drops list categories with zero items (counts remain in `summary`).
    - Truncates each list to `_REFLECT_TOP_N[key]` with a `truncated` marker.
    - Shortens long string fields (claim/question/reason/etc) to _REFLECT_FIELD_MAX chars (currently 200).
    """
    def trunc_str(s):
        if not isinstance(s, str) or len(s) <= _REFLECT_FIELD_MAX:
            return s
        return s[: _REFLECT_FIELD_MAX - 1] + "…"

    def trunc_entry(entry):
        if not isinstance(entry, dict):
            return entry
        return {
            k: (trunc_str(v) if k in _REFLECT_TRUNC_FIELDS else v)
            for k, v in entry.items()
        }

    out = {}
    if "summary" in report:
        out["summary"] = report["summary"]
    if "memory_tiers" in report:
        out["memory_tiers"] = report["memory_tiers"]
    if "feeling_report_nudge" in report:
        out["feeling_report_nudge"] = report["feeling_report_nudge"]

    for key in _REFLECT_LIST_ORDER:
        v = report.get(key)
        if not v:
            continue
        n = _REFLECT_TOP_N.get(key, 5)
        slimmed = [trunc_entry(e) for e in v[:n]]
        if len(v) > n:
            out[key] = {
                "shown": slimmed,
                "total": len(v),
                "truncated": len(v) - n,
                "hint": (
                    f"Showing top {n} of {len(v)}; use engram_query "
                    "or engram_inspect to drill into the rest."
                ),
            }
        else:
            out[key] = slimmed

    if "recommended_actions" in report:
        actions = report["recommended_actions"]
        if len(actions) > _REFLECT_ACTIONS_MAX:
            out["recommended_actions"] = actions[:_REFLECT_ACTIONS_MAX]
            out["recommended_actions_truncated"] = len(actions) - _REFLECT_ACTIONS_MAX
        else:
            out["recommended_actions"] = actions
    if "calibration_snapshot" in report:
        out["calibration_snapshot"] = report["calibration_snapshot"]
    return out


# ---------------------------------------------------------------------------
# Reflect impl
# ---------------------------------------------------------------------------

def _reflect_impl(summary_top_k: int = 5) -> str:
    """Impl for engram_reflect — callable with named kwargs for in-server callers.

    Note: the ``calibration_snapshot`` key is NOT in this function's return
    value. The server.py engram_reflect wrapper appends it after calling this
    impl, using engram_stats() which lives in server.py. See module docstring
    for the wave-3 structural note.
    """
    summary_top_k = max(0, int(summary_top_k))
    conn = core._get_db()
    try:
        report = {}

        # Gate reflect sub-scans on tier-2 (queryable) threshold; tier-1 is retired.
        t2 = core._get_tier_threshold(conn, 2)

        # 1. Unresolved contradictions (open and partially resolved)
        # LOW-VOLUME: source-swap description → recall_summary with claim fallback.
        contradictions = conn.execute(
            "SELECT id, claim, recall_summary, status FROM nodes WHERE type = 'contradiction' AND status IN ('active', 'partially_resolved') AND is_current = 1"
        ).fetchall()
        report["unresolved_contradictions"] = [
            {"id": c["id"], "description": core._reflect_rs_or_claim(c, "claim"), "status": c["status"]} for c in contradictions
        ]

        # 2. Open and partially resolved questions (with category/lacks/staleness)
        # LOW-VOLUME: source-swap question → recall_summary with claim fallback.
        questions = conn.execute(
            """SELECT id, claim, recall_summary, created_at, status,
                      question_category, question_lacks,
                      last_assessed_turn, last_assessed_at
               FROM nodes
               WHERE type = 'question'
                 AND status IN ('open', 'partially_resolved')
                 AND is_current = 1"""
        ).fetchall()
        current_turn = core._get_current_turn()
        report["open_questions"] = []
        for q in questions:
            entry = {
                "id": q["id"], "question": core._reflect_rs_or_claim(q, "claim"),
                "since": q["created_at"], "status": q["status"],
            }
            if q["question_category"]:
                entry["category"] = q["question_category"]
            if q["question_lacks"]:
                entry["lacks"] = q["question_lacks"]
            if q["last_assessed_turn"] is not None:
                entry["last_assessed_turn"] = q["last_assessed_turn"]
                entry["turns_since_assessed"] = current_turn - q["last_assessed_turn"]
            if q["last_assessed_at"]:
                entry["last_assessed_at"] = q["last_assessed_at"]
            report["open_questions"].append(entry)

        # 3. Overdue predictions (past resolution timeframe, still unresolved)
        now_str = core._now()[:10]
        overdue = conn.execute(
            """SELECT id, predicted_event, resolution_timeframe, status FROM nodes
               WHERE type = 'prediction' AND status IN ('open', 'partially_resolved')
               AND resolution_timeframe IS NOT NULL AND resolution_timeframe < ?""",
            (now_str,),
        ).fetchall()
        report["overdue_predictions"] = [
            {"id": p["id"], "event": p["predicted_event"], "deadline": p["resolution_timeframe"], "status": p["status"]}
            for p in overdue
        ]

        # 4. Open predictions (not yet overdue)
        open_preds = conn.execute(
            """SELECT id, predicted_event, resolution_timeframe, status FROM nodes
               WHERE type = 'prediction' AND status IN ('open', 'partially_resolved')
               AND (resolution_timeframe IS NULL OR resolution_timeframe >= ?)""",
            (now_str,),
        ).fetchall()
        report["open_predictions"] = [
            {"id": p["id"], "event": p["predicted_event"], "deadline": p["resolution_timeframe"], "status": p["status"]}
            for p in open_preds
        ]

        # 4b. Open conjectures awaiting investigation
        # LOW-VOLUME: source-swap claim → recall_summary with claim fallback.
        conjectures = conn.execute(
            """SELECT id, claim, recall_summary, confidence, created_at FROM nodes
               WHERE type = 'conjecture' AND status = 'active' AND is_current = 1"""
        ).fetchall()
        report["open_conjectures"] = [
            {"id": c["id"], "claim": core._reflect_rs_or_claim(c, "claim"), "confidence": c["confidence"], "since": c["created_at"]}
            for c in conjectures
        ]

        # 4c. Active goals (persistent orientation)
        # LOW-VOLUME: source-swap goal → recall_summary with claim fallback.
        goals = conn.execute(
            """SELECT id, claim, recall_summary, created_at FROM nodes
               WHERE type = 'goal' AND status = 'open' AND is_current = 1
               AND (json_extract(metadata, '$.lifecycle_state') IS NULL
                    OR json_extract(metadata, '$.lifecycle_state') = 'active')"""
        ).fetchall()
        report["active_goals"] = [
            {"id": g["id"], "goal": core._reflect_rs_or_claim(g, "claim"), "since": g["created_at"]}
            for g in goals
        ]

        # 4c2. Active tasks (actionable work items)
        # LOW-VOLUME: source-swap task → recall_summary with claim fallback.
        tasks = conn.execute(
            """SELECT id, claim, recall_summary, status, metadata, created_at FROM nodes
               WHERE type = 'task' AND status IN ('planned', 'active', 'blocked') AND is_current = 1
               ORDER BY CASE status WHEN 'active' THEN 0 WHEN 'blocked' THEN 1 WHEN 'planned' THEN 2 END"""
        ).fetchall()
        active_tasks = []
        for t in tasks:
            t_meta = json.loads(t["metadata"]) if t["metadata"] else {}
            serves = conn.execute(
                "SELECT target_id FROM edges WHERE source_id = ? AND relation = 'serves'",
                (t["id"],),
            ).fetchall()
            active_tasks.append({
                "id": t["id"],
                "task": core._reflect_rs_or_claim(t, "claim"),
                "status": t["status"],
                "scope": t_meta.get("scope", "routine"),
                "since": t["created_at"],
                "serves_goals": [s["target_id"] for s in serves],
            })
        report["active_tasks"] = active_tasks

        # 4c3. Known people (relational layer)
        people = conn.execute(
            """SELECT id, claim, metadata, created_at FROM nodes
               WHERE type = 'person' AND is_current = 1
               ORDER BY created_at"""
        ).fetchall()
        if people:
            report["known_people"] = [
                {
                    "id": p["id"],
                    "name": json.loads(p["metadata"]).get("name", "") if p["metadata"] else "",
                    "role": json.loads(p["metadata"]).get("role", "") if p["metadata"] else "",
                    "since": p["created_at"],
                }
                for p in people
            ]

        # 4d. Unresolved goal tensions
        # LOW-VOLUME: source-swap description → recall_summary with claim fallback.
        tensions = conn.execute(
            """SELECT id, claim, recall_summary, status, created_at FROM nodes
               WHERE type = 'goal_tension' AND status IN ('open', 'partially_resolved') AND is_current = 1"""
        ).fetchall()
        goal_tensions = []
        for t_node in tensions:
            linked_goals = conn.execute(
                "SELECT target_id FROM edges WHERE source_id = ? AND relation = 'tensions'",
                (t_node["id"],),
            ).fetchall()
            goal_tensions.append({
                "id": t_node["id"],
                "description": core._reflect_rs_or_claim(t_node, "claim"),
                "status": t_node["status"],
                "since": t_node["created_at"],
                "goals": [g["target_id"] for g in linked_goals],
            })
        report["unresolved_goal_tensions"] = goal_tensions

        # 4d2. Active lessons (error-learning patterns)
        # LOW-VOLUME: source-swap claim → recall_summary with claim fallback.
        lessons = conn.execute(
            """SELECT id, claim, recall_summary, confidence, metadata, created_at FROM nodes
               WHERE type = 'lesson' AND is_current = 1
               ORDER BY id DESC"""
        ).fetchall()
        active_lessons = []
        for ls in lessons:
            ls_meta = json.loads(ls["metadata"]) if ls["metadata"] else {}
            # Incidents point AT the lesson via `exemplifies` (incident → lesson).
            # Filter to is_current=1 sources so the list matches exemplar_count
            # semantics (live exemplars only; superseded source nodes excluded).
            incident_edges = conn.execute(
                """SELECT e.source_id
                   FROM edges e
                   JOIN nodes n ON n.id = e.source_id
                   WHERE e.target_id = ? AND e.relation = 'exemplifies'
                     AND n.is_current = 1""",
                (ls["id"],),
            ).fetchall()
            active_lessons.append({
                "id": ls["id"],
                "claim": core._reflect_rs_or_claim(ls, "claim"),
                "confidence": ls["confidence"],
                "scaffolding_nudge": ls_meta.get("scaffolding_nudge", ""),
                "exemplar_count": core._count_live_exemplars(conn, ls["id"], "lesson"),
                "incidents": [e["source_id"] for e in incident_edges],
                "since": ls["created_at"],
            })
        report["active_lessons"] = active_lessons

        # 4e. Recent feeling reports (last 5, with motivation-C highlighting).
        # Highlight rules:
        #   - tagless: candidates for term-generation pattern analysis (no
        #     human word fit at write time)
        #   - different_context: ctx_claude_md_sha differs from current SHA,
        #     candidates for longitudinal cross-context comparison
        recent_feelings = conn.execute(
            """SELECT id, claim, reported_state, trigger_text, categorical_tag,
                      nudge_source, ctx_claude_md_sha, ctx_turn, created_at
               FROM nodes
               WHERE type = 'feeling_report' AND is_current = 1
                 AND status = 'active'
               ORDER BY id DESC LIMIT 5"""
        ).fetchall()
        current_claude_sha = core._git_sha_for_file(
            Path(os.environ.get(
                "CLAUDE_PROJECT_DIR",
                os.getcwd(),
            )) / "CLAUDE.md"
        )
        recent_feeling_reports = []
        for f in recent_feelings:
            state = f["reported_state"] or f["claim"] or ""
            highlights = []
            if not f["categorical_tag"]:
                highlights.append("tagless")
            if (current_claude_sha
                and f["ctx_claude_md_sha"]
                and f["ctx_claude_md_sha"] != current_claude_sha):
                highlights.append("different_context")
            recent_feeling_reports.append({
                "id": f["id"],
                "reported_state": state[:200],
                "trigger": f["trigger_text"],
                "categorical_tag": f["categorical_tag"],
                "nudge_source": f["nudge_source"],
                "ctx_turn": f["ctx_turn"],
                "since": f["created_at"],
                "highlights": highlights,
            })
        report["recent_feeling_reports"] = recent_feeling_reports

        # 5. Weakly-grounded nodes (low confidence, in queryable memory)
        # HIGH-VOLUME: tiered by importance_score DESC. Top summary_top_k get
        # summary-style (claim from recall_summary OR claim); remainder get
        # keyword-style (keywords from recall_keywords; no "claim" key).
        weak = conn.execute(
            """SELECT id, type, claim, recall_summary, recall_keywords,
                      confidence, COALESCE(importance_score, 0) AS imp
               FROM nodes
               WHERE is_current = 1 AND confidence IS NOT NULL
               AND confidence < 0.5 AND type NOT IN ('evidence', 'contradiction', 'question')
               AND COALESCE(importance_score, 0) >= ?
               ORDER BY imp DESC, confidence ASC LIMIT 10""",
            (t2,),
        ).fetchall()
        weakly_grounded = []
        for i, w in enumerate(weak):
            if i < summary_top_k:
                # Tier 1: summary-style
                weakly_grounded.append({
                    "id": w["id"], "type": w["type"],
                    "claim": core._reflect_rs_or_claim(w, "claim"),
                    "confidence": w["confidence"],
                })
            else:
                # Tier 2: keyword-style (no "claim" key)
                entry: dict = {"id": w["id"], "type": w["type"], "confidence": w["confidence"]}
                kws = core._reflect_keywords(w)
                if kws is not None:
                    entry["keywords"] = kws
                weakly_grounded.append(entry)
        report["weakly_grounded"] = weakly_grounded

        # 6. Single-source derivations (in queryable memory)
        # HIGH-VOLUME: tiered by importance_score DESC. Top summary_top_k get
        # summary-style; remainder get keyword-style.
        derivations = conn.execute(
            """SELECT id, claim, recall_summary, recall_keywords, confidence,
                      COALESCE(importance_score, 0) AS imp
               FROM nodes
               WHERE type = 'derivation' AND is_current = 1
               AND COALESCE(importance_score, 0) >= ?
               ORDER BY imp DESC, id ASC""",
            (t2,),
        ).fetchall()
        # Filter to thin-support (support_count <= 1), preserving importance order.
        thin_support_candidates = []
        for d in derivations:
            support_count = conn.execute(
                "SELECT COUNT(*) as c FROM edges WHERE source_id = ? AND relation = 'derives_from'",
                (d["id"],),
            ).fetchone()["c"]
            if support_count <= 1:
                thin_support_candidates.append((d, support_count))
        thin_support = []
        for i, (d, support_count) in enumerate(thin_support_candidates):
            if i < summary_top_k:
                # Tier 1: summary-style
                thin_support.append({
                    "id": d["id"],
                    "claim": core._reflect_rs_or_claim(d, "claim"),
                    "confidence": d["confidence"],
                    "support_count": support_count,
                })
            else:
                # Tier 2: keyword-style (no "claim" key)
                entry = {"id": d["id"], "support_count": support_count,
                         "confidence": d["confidence"]}  # may be None
                kws = core._reflect_keywords(d)
                if kws is not None:
                    entry["keywords"] = kws
                thin_support.append(entry)
        report["thin_support_derivations"] = thin_support

        # 7. Same-source observation tension check (in queryable memory)
        evidence_with_multiple = conn.execute(
            """SELECT evidence_id, COUNT(*) as obs_count FROM nodes
               WHERE type IN ('observation_factual', 'observation_predictive')
               AND is_current = 1 AND evidence_id IS NOT NULL
               AND COALESCE(importance_score, 0) >= ?
               GROUP BY evidence_id HAVING obs_count >= 2""",
            (t2,),
        ).fetchall()

        same_source_tensions = []
        for ev in evidence_with_multiple:
            # Don't dump every observation ID — only flag the source. The
            # agent can call engram_inspect(evidence_id) for the full list.
            same_source_tensions.append(
                {
                    "evidence_id": ev["evidence_id"],
                    "observation_count": ev["obs_count"],
                }
            )
        report["same_source_review"] = same_source_tensions

        # 8. Uncited observations (in queryable memory, not referenced by any derivation)
        # HIGH-VOLUME: tiered by importance_score DESC. Top summary_top_k get
        # summary-style; remainder get keyword-style.
        uncited = conn.execute(
            """SELECT n.id, n.claim, n.recall_summary, n.recall_keywords,
                      n.confidence, COALESCE(n.importance_score, 0) AS imp
               FROM nodes n
               WHERE n.type IN ('observation_factual', 'observation_predictive')
               AND n.is_current = 1
               AND COALESCE(n.importance_score, 0) >= ?
               AND NOT EXISTS (
                   SELECT 1 FROM edges e
                   WHERE e.target_id = n.id AND e.relation IN ('derives_from', 'supported_by')
                   AND e.source_id IN (SELECT id FROM nodes WHERE type IN ('derivation', 'theory'))
               )
               AND NOT EXISTS (
                   SELECT 1 FROM edges e
                   WHERE e.target_id = n.id AND e.relation = 'supported_by'
                   AND e.source_id IN (SELECT id FROM nodes WHERE type = 'prediction')
               )
               ORDER BY imp DESC, n.id ASC
               LIMIT 15""",
            (t2,),
        ).fetchall()
        uncited_observations = []
        for i, u in enumerate(uncited):
            if i < summary_top_k:
                # Tier 1: summary-style
                uncited_observations.append({
                    "id": u["id"],
                    "claim": core._reflect_rs_or_claim(u, "claim"),
                    "confidence": u["confidence"],
                })
            else:
                # Tier 2: keyword-style (no "claim" key)
                entry = {"id": u["id"], "confidence": u["confidence"]}
                kws = core._reflect_keywords(u)
                if kws is not None:
                    entry["keywords"] = kws
                uncited_observations.append(entry)
        report["uncited_observations"] = uncited_observations

        # 9. Tainted nodes (downstream of retracted nodes)
        tainted_nodes = conn.execute(
            # Anchor to JSON list opening — metadata written with default json.dumps separators.
            """SELECT id, type, claim, confidence, metadata FROM nodes
               WHERE is_current = 1 AND metadata LIKE '%"tainted_by": [%'"""
        ).fetchall()
        report["tainted_nodes"] = []
        for t in tainted_nodes:
            meta = json.loads(t["metadata"] or "{}")
            report["tainted_nodes"].append({
                "id": t["id"],
                "type": t["type"],
                "claim": t["claim"],
                "confidence": t["confidence"],
                "tainted_by": meta.get("tainted_by", []),
            })

        # 10. Stale nodes (downstream of superseded premises)
        stale_nodes = conn.execute(
            # Anchor to JSON list opening — metadata written with default json.dumps separators.
            """SELECT id, type, claim, confidence, metadata FROM nodes
               WHERE is_current = 1 AND metadata LIKE '%"stale_by": [%'"""
        ).fetchall()
        report["stale_nodes"] = []
        for s in stale_nodes:
            meta = json.loads(s["metadata"] or "{}")
            _sr = meta.get("stale_replacement")
            if isinstance(_sr, dict):
                # Python 3.7+ dicts preserve insertion order, so reversed() returns
                # the most-recent entry first. For re-supersede-same-key cases (same
                # old_node_id cascaded twice), dict update behavior overwrites — only
                # the latest replacement survives for that specific old_node_id. This
                # is correct: the older cascade's replacement was itself superseded,
                # so the chain naturally points to the most-recent.
                _replacement_hint = next(reversed(_sr.values())) if _sr else None
                _replacement_hints_all = _sr
            elif isinstance(_sr, str):
                _replacement_hint = _sr
                _replacement_hints_all = {"_legacy": _sr}
            else:
                _replacement_hint = None
                _replacement_hints_all = {}
            report["stale_nodes"].append({
                "id": s["id"],
                "type": s["type"],
                "claim": s["claim"],
                "confidence": s["confidence"],
                "stale_by": meta.get("stale_by", []),
                "replacement_hint": _replacement_hint,
                "replacement_hints_all": _replacement_hints_all,
            })

        # 11. Retracted nodes (for audit visibility) — slim entries; the
        # full retraction_reason is available via engram_inspect.
        retracted = conn.execute(
            "SELECT id, type, claim, metadata FROM nodes WHERE status = 'retracted' ORDER BY id DESC"
        ).fetchall()
        report["retracted_nodes"] = []
        for r in retracted:
            meta = json.loads(r["metadata"] or "{}")
            report["retracted_nodes"].append({
                "id": r["id"],
                "type": r["type"],
                "claim": r["claim"],
                "error_type": meta.get("error_type", "unknown"),
            })

        # 12. Support-lost cornerstones/lessons (§2.4) — primary channel for
        # dream-master to find cs/ls that lost all live empirical support.
        # Trivial rewires handled inside the dream; non-trivial cases surface
        # to awake state via the flagged-for-user mechanism.
        support_lost_rows = conn.execute(
            # Anchor to serialized boolean — metadata written with default json.dumps separators.
            """SELECT id, type, claim FROM nodes
               WHERE is_current = 1
                 AND type IN ('cornerstone', 'lesson')
                 AND metadata LIKE '%"support_lost": true%'"""
        ).fetchall()
        report["support_lost_nodes"] = [
            {"id": r["id"], "type": r["type"], "claim": r["claim"]}
            for r in support_lost_rows
        ]

        # Summary counts (two-tier: queryable vs faded; tier-1 retired)
        total_current = conn.execute(
            "SELECT COUNT(*) as c FROM nodes WHERE is_current = 1"
        ).fetchone()["c"]
        tier2_count = conn.execute(
            "SELECT COUNT(*) as c FROM nodes WHERE is_current = 1 AND COALESCE(importance_score, 0) >= ?",
            (t2,),
        ).fetchone()["c"] if t2 > 0 else total_current

        report["summary"] = {
            "contradictions": len(report["unresolved_contradictions"]),
            "open_questions": len(report["open_questions"]),
            "active_goals": len(report["active_goals"]),
            "goal_tensions": len(report["unresolved_goal_tensions"]),
            "open_conjectures": len(report["open_conjectures"]),
            "overdue_predictions": len(report["overdue_predictions"]),
            "weakly_grounded": len(report["weakly_grounded"]),
            "thin_support": len(report["thin_support_derivations"]),
            "sources_to_review": len(report["same_source_review"]),
            "uncited_observations": len(report["uncited_observations"]),
            "tainted_nodes": len(report["tainted_nodes"]),
            "stale_nodes": len(report["stale_nodes"]),
            "retracted_nodes": len(report["retracted_nodes"]),
            "support_lost_nodes": len(report["support_lost_nodes"]),
            "recent_feeling_reports": len(report["recent_feeling_reports"]),
        }
        report["memory_tiers"] = {
            "tier2_queryable": tier2_count,
            "tier3_total": total_current,
            "current_turn": core._get_current_turn(),
        }

        # Generate recommended actions
        actions = []
        if report["active_goals"]:
            actions.append("Review active goals — check whether recent work aligns with stated directions. Goals are persistent orientation, not tasks.")
        if report.get("active_tasks"):
            blocked = [t for t in report["active_tasks"] if t["status"] == "blocked"]
            active = [t for t in report["active_tasks"] if t["status"] == "active"]
            if blocked:
                actions.append(f"Unblock {len(blocked)} blocked task(s) — investigate what's preventing progress.")
            if active:
                actions.append(f"Continue {len(active)} active task(s) — these are your current work in progress.")
        if report["unresolved_goal_tensions"]:
            actions.append("Examine unresolved goal tensions — these are value-level conflicts between goals requiring root cause analysis, not evidence accumulation.")
        if report["unresolved_contradictions"]:
            actions.append("Investigate unresolved contradictions — trace each side's evidence chain and determine the root cause.")
        if report["overdue_predictions"]:
            actions.append("Check overdue predictions — search for evidence of actual outcomes and resolve them.")
        if report["open_questions"]:
            # Provide category-aware guidance if any questions have categories
            categorized = [q for q in report["open_questions"] if q.get("category")]
            stale = [q for q in report["open_questions"] if q.get("turns_since_assessed", 0) >= 10]
            if stale:
                actions.append(f"Review {len(stale)} stale question(s) not assessed in 10+ turns.")
            elif categorized:
                from collections import Counter
                cat_counts = Counter(q["category"] for q in categorized)
                cat_summary = ", ".join(f"{c}: {n}" for c, n in cat_counts.most_common())
                actions.append(f"Research open questions ({cat_summary}) — pick the highest-priority one.")
            else:
                actions.append("Research open questions — pick the highest-priority one and search for answers.")
        if report["open_conjectures"]:
            actions.append("Investigate open conjectures — search for evidence to support, refute, or resolve them.")
        if report["weakly_grounded"]:
            actions.append("Strengthen weak claims — search for additional evidence or downgrade claims that can't be better supported.")
        if report["thin_support_derivations"]:
            actions.append("Diversify thin derivations — find independent sources to corroborate single-source claims.")
        if report["same_source_review"]:
            actions.append("Audit same-source observations — verify you extracted balanced, representative claims from each source.")
        if report["uncited_observations"]:
            actions.append("Integrate uncited observations — consider whether they support or challenge existing derivations.")
        if report["tainted_nodes"]:
            actions.append("Review tainted nodes — these depend on retracted nodes and may need correction or re-derivation from clean sources.")
        if report["stale_nodes"]:
            actions.append("Review stale nodes — their premises were superseded. Consider superseding these too with updated reasoning.")
        if report["retracted_nodes"]:
            actions.append("Audit retracted nodes — check if all downstream effects have been addressed.")
        if report["support_lost_nodes"]:
            actions.append(
                "Review support-lost cornerstones/lessons — each has lost all live empirical support. "
                "Either link new exemplifying instances, or retract the pattern if it no longer holds."
            )
        report["recommended_actions"] = actions

        # Dream-cycle feeling-report nudge (§6 Marker file protocol). The
        # tool return IS the delivery channel — no separate hook needed.
        # Writing the marker BEFORE returning means any subsequent
        # engram_report_feeling call within TTL gets auto-tagged
        # `dream_review`. The agent is explicitly invited to report
        # nothing — null results are valid data points.
        report["feeling_report_nudge"] = (
            "Did revisiting any of these nodes produce a reportable internal "
            "state? Examples: recognizing a node you'd forgotten, dissonance "
            "about a prior conclusion, pattern recognition across multiple "
            "reviewed items, satisfaction at closing a contradiction. "
            "If yes, file a feeling report via engram_report_feeling — it "
            "will be auto-tagged dream_review. If not, end the turn with NO "
            "output - do not reply to or acknowledge this check; a text-only "
            "acknowledgment wastes a turn. A null result is a valid data "
            "point — emitting no output is how you record it."
        )

        core._write_feeling_nudge("dream_review")

        return json.dumps(core._strip_agent_facing(_slim_reflect_briefing(report)))
    finally:
        conn.close()
