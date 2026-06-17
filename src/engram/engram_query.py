"""Family C — query / recall / surface / pattern cluster.

Extracted from server.py in #872 wave 8.

HOUSE RULES (identical to engram_core.py contract):
  - Shared state ONLY via ``import engram_core as core`` + call-time ``core.X``.
  - No ``from engram_core import`` (Rule A of the seam gate).
  - No module-level assignment of any of the 14 MUTABLE_NAMES from engram_core
    (Rule B of the seam gate).
  - Stateless module: no mutable global state beyond constants.
  - No import of server.py (acyclic: family modules must not import server).

Module/tool name collision: the @mcp.tool ``engram_query`` lives in server.py;
importing this module as a bare name would shadow that tool function at delegation
time.  Caller (server.py) uses the alias form::

    import engram_query as _query_mod

See PR #920 decision log for the corrected _pattern_* attribution (moved here
with _query_pattern_impl, not to family H).
"""

from __future__ import annotations

import json
import math
import os
import sqlite3
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Union

import engram_core as core
from engram_log_emitter import emit_if_initialized


# ---------------------------------------------------------------------------
# C-local constants (inspect view helpers)
# ---------------------------------------------------------------------------

# Type aliases for engram_query — allow shorthands and un-suffixed names that
# agents commonly pass (e.g. "observation" instead of "observation_factual").
_TYPE_ALIASES: dict[str, str] = {
    "observation": "observation_factual",
    "ob": "observation_factual",
}

# Edge relations that carry logical/epistemic weight — shown with full
# recall_summary in the recall view.
_LOGICAL_SUBSTRATE_RELATIONS = frozenset({
    "derives_from",
    "supported_by",
    "supersedes",
    "contradicts",
    "resolves",
    "retracts",
    "exemplifies",
    "subtask_of",
    "tensions",
})

# Edge relations that supply context/reference only — shown with keywords only
# in the recall view.
_CONTEXTUAL_RELATIONS = frozenset({
    "cites",
    "about",
    "serves",
})

# For recall view: cap per-group size to avoid response bloat when a popular
# node has hundreds of dependents. Applied to each group independently.
_INSPECT_RECALL_GROUP_CAP = 10


# ---------------------------------------------------------------------------
# Pattern cluster — corrected attribution from #920 decision log.
# Inventory attributed these to family H; unbounded grep in wave 7 showed all
# 7 _pattern_* functions are consumed exclusively by _query_pattern_impl
# (family C).  They ride wave 8 with their consumer.
# ---------------------------------------------------------------------------

PATTERN_QUERY_PRESETS: dict[str, dict[str, float | int]] = {
    # "balanced" cosine_threshold v4-calibrated 2026-05-09.
    # high_precision / high_recall: ±0.10 symmetric deltas off balanced cosine,
    # set 2026-05-11 by principled-delta rationale rather than full pattern-
    # specific calibration study (see active-work/query-pattern-preset-rationale-
    # 2026-05-11.md for the full argument). v4 study showed useful similarity
    # range is 0.45–0.65 — anything above 0.65 misses real corroborations;
    # anything below 0.45 admits false positives. Symmetric ±0.10 brackets
    # balanced (0.55) at both ends of that empirically-validated range.
    # Pattern-specific telemetry remains the right calibration path long-term
    # per the KnowQL-inspired design; these values are the "what would a careful
    # reader pick given the v4 data and no pattern-specific telemetry yet" answer.
    # Whether to make this dict config-loadable (Tier 2 in issue #76's tier
    # model) vs leave hardcoded (Tier 3) is the open design question — for
    # now the dict stays here, values calibrated against v4 evidence.
    "high_precision": {"cosine_threshold": 0.65, "top_k": 5,  "min_confidence": 0.70},
    "balanced":       {"cosine_threshold": 0.55, "top_k": 15, "min_confidence": 0.50},
    "high_recall":    {"cosine_threshold": 0.45, "top_k": 30, "min_confidence": 0.00},
}


def _pattern_telemetry_log(pattern_name: str, preset: str,
                            args: dict, candidates: list[dict]) -> None:
    """Append one entry per call to enable empirical preset calibration.

    The schema mirrors the KnowQL-inspired design's calibration plan: (pattern_name, args,
    candidate_id, score, agent_action) tuples. agent_action is filled
    later out-of-band when the agent acts on a candidate; this hook only
    captures the surfacing event.

    Best-effort: telemetry failures must never block a pattern call.
    """
    try:
        path = Path(core.DATA_DIR) / "pattern_query_telemetry.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        entry = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "pattern_name": pattern_name,
            "preset": preset,
            "args": args,
            "candidate_count": len(candidates),
            "candidate_ids": [c.get("id") for c in candidates],
            "scores": [c.get("score") for c in candidates],
        }
        with open(path, "a") as f:
            f.write(json.dumps(entry) + "\n")
    except Exception:
        pass


def _pattern_contradiction_obsolescence_ready(
    conn, preset_params: dict, **_filters
) -> list[dict]:
    """Active contradictions where one side is retracted/superseded.

    A contradiction with one side already obsolete is resolution-by-
    obsolescence ready: the agent can write a derivation citing the
    obsolescence event as the resolution mechanism. This is mechanical
    (no embeddings) — the ranking is by how unambiguous the obsolescence
    signal is.

    Score: 1.0 if exactly one side is non-current AND the contradiction
    is still status='active'; 0.5 if both sides are non-current (likely
    already moot but the contradiction node wasn't resolved); 0.0 if
    neither side is obsolete (returned only at high_recall).

    Returns: list of {id, claim, side_a, side_b, side_a_status,
    side_b_status, score, suggested_action}.
    """
    threshold = float(preset_params.get("cosine_threshold", 0.50))
    top_k = int(preset_params.get("top_k", 15))

    contradictions = conn.execute(
        "SELECT id, claim, status FROM nodes "
        "WHERE type='contradiction' AND is_current=1 AND status='active' "
        "ORDER BY id DESC"
    ).fetchall()

    candidates: list[dict] = []
    for ct_id, ct_claim, ct_status in contradictions:
        sides = conn.execute(
            "SELECT target_id FROM edges WHERE source_id=? AND relation='contradicts'",
            (ct_id,),
        ).fetchall()
        if len(sides) != 2:
            continue
        side_a_id, side_b_id = sides[0][0], sides[1][0]
        side_a = conn.execute(
            "SELECT id, claim, is_current, status FROM nodes WHERE id=?",
            (side_a_id,),
        ).fetchone()
        side_b = conn.execute(
            "SELECT id, claim, is_current, status FROM nodes WHERE id=?",
            (side_b_id,),
        ).fetchone()
        if not side_a or not side_b:
            continue

        a_obsolete = side_a[2] == 0
        b_obsolete = side_b[2] == 0
        if a_obsolete and not b_obsolete:
            score = 1.0
            obsolete_side, surviving_side = "a", "b"
        elif b_obsolete and not a_obsolete:
            score = 1.0
            obsolete_side, surviving_side = "b", "a"
        elif a_obsolete and b_obsolete:
            score = 0.5
            obsolete_side, surviving_side = "both", None
        else:
            score = 0.0
            obsolete_side, surviving_side = None, None

        if score < threshold:
            continue

        candidates.append({
            "id": ct_id,
            "claim": (ct_claim[:160] + "...") if ct_claim and len(ct_claim) > 160 else ct_claim,
            "score": score,
            "side_a": {"id": side_a[0], "is_current": bool(side_a[2]), "status": side_a[3]},
            "side_b": {"id": side_b[0], "is_current": bool(side_b[2]), "status": side_b[3]},
            "obsolete_side": obsolete_side,
            "surviving_side": surviving_side,
            "suggested_action": (
                f"Resolve via engram_resolve({ct_id}, claim='Resolution-by-obsolescence: "
                f"side {obsolete_side} is no longer current.', supporting_ids='{side_a[0] if surviving_side == 'a' else side_b[0]}', ...)"
                if surviving_side else
                "Both sides obsolete — write resolution citing both obsolescence events."
            ),
        })

    candidates.sort(key=lambda c: c["score"], reverse=True)
    return candidates[:top_k]


def _pattern_open_question_answerable(
    conn, preset_params: dict, **_filters
) -> list[dict]:
    """Open questions with a derivation chain nearby that may resolve them.

    For each open question, semantic-search for derivations whose claim is
    similar to the question's content. Threshold by cosine_threshold and
    rank by best-match similarity. Only questions that have at least one
    candidate derivation above threshold survive.

    Returns: list of {id, claim, score, candidate_resolution, alternates,
    suggested_action}.
    """
    threshold = float(preset_params.get("cosine_threshold", 0.50))
    top_k = int(preset_params.get("top_k", 15))
    min_conf = float(preset_params.get("min_confidence", 0.0))

    questions = conn.execute(
        "SELECT id, claim FROM nodes "
        "WHERE type='question' AND is_current=1 AND status='open' "
        "ORDER BY id DESC LIMIT 200"
    ).fetchall()

    candidates: list[dict] = []
    for q_id, q_claim in questions:
        if not q_claim:
            continue
        # top_k=5 here is intentionally narrower than DEDUP_TOP_K — this is a
        # per-question inner lookup over up to N questions; we want the few
        # best derivation candidates per question, not the dedup-pool size.
        matches = core._semantic_search(
            conn, q_claim, top_k=5,
            min_similarity=threshold,
            type_filter={"derivation"},
            importance_threshold=0.0,
        )
        matches = [m for m in matches if (m.get("confidence") or 0) >= min_conf]
        if not matches:
            continue
        best = matches[0]
        candidates.append({
            "id": q_id,
            "claim": (q_claim[:160] + "...") if len(q_claim) > 160 else q_claim,
            "score": best["similarity"],
            "candidate_resolution": {
                "id": best["id"],
                "claim": (best["claim"][:160] + "...") if best["claim"] and len(best["claim"]) > 160 else best["claim"],
                "confidence": best["confidence"],
            },
            # NOTE: alternates/candidate_resolution/suggested_action are discarded by
            # _build_tiered_results; they survive only in _pattern_telemetry_log (see #366).
            "alternates": [
                {"id": m["id"], "similarity": m["similarity"]}
                for m in matches[1:]
            ],
            "suggested_action": (
                f"Inspect {best['id']} and consider engram_resolve({q_id}, "
                f"claim=..., supporting_ids='{best['id']}', ...) if it answers the question."
            ),
        })

    candidates.sort(key=lambda c: c["score"], reverse=True)
    return candidates[:top_k]


def _pattern_stale_load_bearing(
    conn, preset_params: dict, **_filters
) -> list[dict]:
    """High-importance, low-recall non-cornerstone nodes — re-engagement candidates.

    Heuristic score: inflation_gap = importance_base * decay_base^current_turn
    - importance_score. This measures how far behind the decay curve a node is
    — i.e., how long since it was last recalled. Nodes with a large gap were
    once important (high base) and haven't been re-recalled in many turns.
    Cornerstones and feeling_reports are excluded.

    Returns: list of {id, type, claim, score, importance_base, inflation_gap,
    recall_count, suggested_action}.
    """
    top_k = int(preset_params.get("top_k", 15))
    importance_floor = 0.7  # gates on importance_base (time-invariant)

    mem = core._get_memory_config()
    current_turn = mem.get("current_turn", 0)
    decay_base = mem.get("decay_base", 1.014)

    # Pre-sort heuristic: fetch top 200 by importance_base DESC, then re-sort
    # by inflation_gap. A node with lower base but very old recall_turn could
    # have a larger gap and be cut here — accepted as approximation cost since
    # the final ranking is by gap and the pool is wide enough (200) to catch
    # typical cases. If empirical signal shows misses, raise the limit.
    rows = conn.execute(
        "SELECT id, type, claim, importance_base, importance_score, recall_count "
        "FROM nodes "
        "WHERE is_current=1 "
        "AND type NOT IN ('cornerstone', 'feeling_report') "
        "AND COALESCE(importance_base, 0) > ? "
        "ORDER BY importance_base DESC LIMIT 200",
        (importance_floor,),
    ).fetchall()

    candidates: list[dict] = []
    for nid, ntype, claim, imp_base, imp_score, recalls in rows:
        recalls = recalls or 0
        imp_base = imp_base or 0.0
        imp_score = imp_score or imp_base  # if never recalled, score equals base
        imp_if_now = imp_base * (decay_base ** current_turn)
        gap = imp_if_now - imp_score  # how far behind the inflation curve
        score = gap
        candidates.append({
            "id": nid,
            "type": ntype,
            "claim": (claim[:160] + "...") if claim and len(claim) > 160 else claim,
            "score": round(score, 3),
            "importance_base": round(imp_base, 3),
            "inflation_gap": round(gap, 3),
            "recall_count": recalls,
            "suggested_action": (
                f"Re-engage with {nid} (engram_inspect) or supersede if outdated. "
                f"High importance_base + large inflation gap suggests load-bearing-but-cold."
            ),
        })

    candidates.sort(key=lambda c: c["score"], reverse=True)
    return candidates[:top_k]


def _pattern_cornerstone_candidate(
    conn, preset_params: dict, **_filters
) -> list[dict]:
    """Heavily-cited observations/derivations that may warrant cornerstone anchoring.

    Candidate pool is **observations and derivations only** — emergent-practice
    nodes whose load-bearing role has accumulated through use. Axioms, goals,
    and definitions are categorically excluded: they are fundamental *by type*
    (declared at creation), so their high type-anchored importance_base is not
    an emergence signal — nominating them was the #180 false-positive. See
    the engram-* skills / MCP docstrings for the axiom/goal/cornerstone semantic distinctions.

    A cornerstone candidate is a node that has been load-bearing in the
    graph (cited by many derivations / observations) but isn't yet anchored
    as a cornerstone. Score: importance_base × (1 + citation_count / 10).
    Gating uses importance_base (time-invariant) so the threshold is stable
    across turns.

    Returns: list of {id, type, claim, score, importance_base, citation_count,
    recall_count, suggested_action}.
    """
    top_k = int(preset_params.get("top_k", 15))
    min_citations = 3  # below this, not load-bearing enough to anchor

    rows = conn.execute(
        "SELECT id, type, claim, importance_base, recall_count "
        "FROM nodes "
        "WHERE is_current=1 "
        "AND (type LIKE 'observation%' OR type = 'derivation') "
        "AND COALESCE(importance_base, 0) > 0.7 "
        "ORDER BY importance_base DESC LIMIT 50"
    ).fetchall()

    candidates: list[dict] = []
    for nid, ntype, claim, imp_base, recalls in rows:
        cite_count = conn.execute(
            "SELECT COUNT(*) FROM edges WHERE target_id=? "
            "AND relation IN ('supported_by', 'cites', 'derives_from')",
            (nid,),
        ).fetchone()[0]
        if cite_count < min_citations:
            continue
        imp_base = imp_base or 0.0
        score = imp_base * (1 + cite_count / 10.0)
        candidates.append({
            "id": nid,
            "type": ntype,
            "claim": (claim[:160] + "...") if claim and len(claim) > 160 else claim,
            "score": round(score, 3),
            "importance_base": round(imp_base, 3),
            "citation_count": cite_count,
            "recall_count": recalls or 0,
            "suggested_action": (
                f"Consider engram_focus or engram_add_cornerstone for {nid} — "
                f"{cite_count} downstream citations suggest it's load-bearing."
            ),
        })

    candidates.sort(key=lambda c: c["score"], reverse=True)
    return candidates[:top_k]


def _pattern_tainted_still_valid(
    conn, preset_params: dict, **_filters
) -> list[dict]:
    """Tainted derivations whose substantive claim may still survive.

    A tainted derivation cited a node that was later retracted. The cascade
    flagged it tainted automatically — but the derivation's substantive
    claim may still hold under a corrected/superseded version of its
    premise. This pattern surfaces high-confidence tainted nodes for the
    agent to inspect and either re-derive cleanly or supersede with fresh
    citation.

    Score: the original confidence — high-confidence tainted derivations
    are most likely to have substance worth preserving.

    Returns: list of {id, type, claim, score, confidence, tainted_by,
    suggested_action}.
    """
    top_k = int(preset_params.get("top_k", 15))
    min_conf = float(preset_params.get("min_confidence", 0.0))

    # Taint is stored in metadata.tainted_by (array of upstream retracted/
    # superseded node IDs that triggered the cascade). The memory_status
    # field remains 'active' — the taint signal lives in the JSON metadata,
    # not in a top-level column. json_extract scans efficiently in SQLite.
    rows = conn.execute(
        "SELECT id, type, claim, confidence, metadata FROM nodes "
        "WHERE is_current=1 "
        "AND json_extract(metadata, '$.tainted_by') IS NOT NULL "
        "AND json_array_length(json_extract(metadata, '$.tainted_by')) > 0"
    ).fetchall()

    candidates: list[dict] = []
    for nid, ntype, claim, conf, metadata in rows:
        conf = conf or 0
        if conf < min_conf:
            continue
        try:
            md = json.loads(metadata or "{}")
        except (ValueError, json.JSONDecodeError):
            md = {}
        tainted_by = md.get("tainted_by") or []
        candidates.append({
            "id": nid,
            "type": ntype,
            "claim": (claim[:160] + "...") if claim and len(claim) > 160 else claim,
            "score": round(conf, 3),
            "confidence": round(conf, 3),
            "tainted_by": tainted_by,
            "suggested_action": (
                f"Inspect {nid} and its tainted_by={tainted_by}: if the substantive "
                f"claim survives the retraction's correction, write a fresh derivation "
                f"and supersede; otherwise leave tainted."
            ),
        })

    candidates.sort(key=lambda c: c["score"], reverse=True)
    return candidates[:top_k]


def _pattern_recent_resolution_echo(
    conn, preset_params: dict, **_filters
) -> list[dict]:
    """Recent resolutions whose logic may apply to similar still-open questions.

    Resolutions are derivations linked via 'resolves' edges to a target
    (typically a question). When a resolution lands, similar still-open
    questions may be resolvable by the same logical chain. This pattern
    semantic-searches for those echo candidates.

    Score: cosine similarity between the resolver's claim and each
    candidate question's claim.

    Returns: list of {id, claim, score, echo_resolution, suggested_action}.
    """
    threshold = float(preset_params.get("cosine_threshold", 0.50))
    top_k = int(preset_params.get("top_k", 15))

    recent_resolutions = conn.execute(
        "SELECT e.source_id, e.target_id, n.claim "
        "FROM edges e JOIN nodes n ON n.id = e.source_id "
        "WHERE e.relation='resolves' AND n.is_current=1 "
        "ORDER BY n.created_at DESC LIMIT 30"
    ).fetchall()

    candidates: list[dict] = []
    seen_questions: set[str] = set()
    for resolver_id, resolved_id, resolution_claim in recent_resolutions:
        if not resolution_claim:
            continue
        # top_k=5 here is intentionally narrower than DEDUP_TOP_K — per-resolution
        # inner lookup, want the few best question candidates per resolution.
        matches = core._semantic_search(
            conn, resolution_claim, top_k=5,
            min_similarity=threshold,
            type_filter={"question"},
            importance_threshold=0.0,
        )
        for m in matches:
            if m["id"] == resolved_id or m["id"] in seen_questions:
                continue
            row = conn.execute(
                "SELECT status FROM nodes WHERE id=?", (m["id"],)
            ).fetchone()
            if not row or row[0] != "open":
                continue
            seen_questions.add(m["id"])
            candidates.append({
                "id": m["id"],
                "claim": (m["claim"][:160] + "...") if m["claim"] and len(m["claim"]) > 160 else m["claim"],
                "score": m["similarity"],
                "echo_resolution": {
                    "resolver_id": resolver_id,
                    "originally_resolved": resolved_id,
                },
                "suggested_action": (
                    f"Inspect resolver {resolver_id} and consider whether the "
                    f"same logical chain applies: engram_resolve({m['id']}, ...)."
                ),
            })

    candidates.sort(key=lambda c: c["score"], reverse=True)
    return candidates[:top_k]


PATTERN_QUERY_REGISTRY: dict[str, callable] = {
    "contradiction_obsolescence_ready": _pattern_contradiction_obsolescence_ready,
    "open_question_answerable":          _pattern_open_question_answerable,
    "stale_load_bearing":                _pattern_stale_load_bearing,
    "cornerstone_candidate":             _pattern_cornerstone_candidate,
    "tainted_still_valid":               _pattern_tainted_still_valid,
    "recent_resolution_echo":            _pattern_recent_resolution_echo,
}


# ---------------------------------------------------------------------------
# C-local helpers
# ---------------------------------------------------------------------------

def _get_min_queryable_importance(
    conn: sqlite3.Connection,
    tier2_threshold: Optional[float] = None,
) -> float:
    """Lowest importance_score among currently-queryable nodes.

    Used as the normalization anchor for the importance amplifier in the
    composite formula. Adapts to graph state — when the graph grows past
    tier_2_max_nodes, the tier-2 threshold rises and this helper rises
    with it, because `_search_nodes` itself filters candidates by
    `importance_score >= tier2_threshold`. The "queryable" subset is
    bounded below by that filter; using the global graph min as the
    normalization anchor would over-amplify cornerstones once the floor
    moves up.

    Returns 1.0 (no-op normalization) for an empty or degenerate graph
    rather than dividing by zero. The idx_nodes_importance index makes
    this O(log n).

    Callers that already have tier2_threshold in scope (e.g. _search_nodes)
    should pass it explicitly to avoid a redundant _get_tier_threshold
    round-trip.
    """
    if tier2_threshold is None:
        tier2_threshold = core._get_tier_threshold(conn, 2)
    row = conn.execute(
        "SELECT MIN(importance_score) FROM nodes "
        "WHERE is_current = 1 AND importance_score > 0 "
        "  AND importance_score >= ?",
        (max(0.0, tier2_threshold),),
    ).fetchone()
    if row is None or row[0] is None or row[0] <= 0:
        return 1.0
    return row[0]


def _refresh_recall(conn: sqlite3.Connection, node_ids: list[str]):
    """Re-stamp recalled nodes with current turn's importance."""
    mem = core._get_memory_config()
    turn = mem.get("current_turn", 0)
    decay_base = mem.get("decay_base", 1.014)
    for nid in node_ids:
        row = conn.execute(
            "SELECT importance_base, recall_count FROM nodes WHERE id = ?",
            (nid,),
        ).fetchone()
        if row:
            new_score = core._compute_importance(
                row["importance_base"] or 0.5, turn, decay_base
            )
            conn.execute(
                """UPDATE nodes SET importance_score = ?, recall_turn = ?,
                   recall_count = ? WHERE id = ?""",
                (new_score, turn, (row["recall_count"] or 0) + 1, nid),
            )
    conn.commit()


def _get_neighbors(conn: sqlite3.Connection, node_id: str) -> list[dict]:
    """Get all edges involving a node, with the connected node's basic info."""
    edges = conn.execute(
        """
        SELECT e.relation, e.source_id, e.target_id,
               n.type as neighbor_type, n.claim as neighbor_claim,
               n.confidence as neighbor_confidence, n.is_current as neighbor_is_current,
               n.created_at as neighbor_created_at
        FROM edges e
        LEFT JOIN nodes n ON (
            CASE WHEN e.source_id = ? THEN e.target_id ELSE e.source_id END
        ) = n.id
        WHERE e.source_id = ? OR e.target_id = ?
    """,
        (node_id, node_id, node_id),
    ).fetchall()

    # Snapshot now once so every neighbor's humanized age is rendered against
    # a single clock reading (MECH-2 / the CLOCK-DRIFT-hygiene derivation).
    now = datetime.now(timezone.utc)
    result = []
    for e in edges:
        neighbor_id = e["target_id"] if e["source_id"] == node_id else e["source_id"]
        direction = "outgoing" if e["source_id"] == node_id else "incoming"
        neighbor_created_at = e["neighbor_created_at"]
        result.append(
            {
                "relation": e["relation"],
                "direction": direction,
                "neighbor_id": neighbor_id,
                "neighbor_type": e["neighbor_type"],
                "neighbor_claim": e["neighbor_claim"],
                "neighbor_confidence": e["neighbor_confidence"],
                "neighbor_created_at": neighbor_created_at,
                "neighbor_created_ago": core._humanized_ago(neighbor_created_at, now=now),
            }
        )
    return result


def _get_neighbors_enriched(
    conn: sqlite3.Connection,
    node_id: str,
    include_superseded: bool = False,
) -> tuple[list[dict], int]:
    """Fetch 1-hop neighbors with recall_summary + recall_keywords columns.

    Returns (neighbors, filtered_count) where filtered_count is the number of
    non-current neighbors that were suppressed (useful for the
    truncated_superseded_count hint). When include_superseded=True, filtered_count
    is always 0.

    Each neighbor dict has keys:
        id, type, relation, direction, recall_summary, recall_keywords,
        importance_score, recall_count
    (recall_count and importance_score are used for capping downstream groups)
    """
    edges = conn.execute(
        """
        SELECT e.relation, e.source_id, e.target_id,
               n.type          AS neighbor_type,
               n.recall_summary   AS neighbor_recall_summary,
               n.recall_keywords  AS neighbor_recall_keywords,
               n.importance_score AS neighbor_importance_score,
               n.recall_count     AS neighbor_recall_count,
               n.is_current       AS neighbor_is_current
        FROM edges e
        LEFT JOIN nodes n ON (
            CASE WHEN e.source_id = ? THEN e.target_id ELSE e.source_id END
        ) = n.id
        WHERE e.source_id = ? OR e.target_id = ?
        """,
        (node_id, node_id, node_id),
    ).fetchall()

    result = []
    filtered_count = 0
    for e in edges:
        neighbor_id = e["target_id"] if e["source_id"] == node_id else e["source_id"]
        # Filter non-current neighbors unless include_superseded=True.
        # A NULL is_current (orphan edge with no matching node row) is treated as
        # current — the LEFT JOIN means the neighbor may be missing from nodes, but
        # we don't silently drop the edge in that case.
        neighbor_is_current = e["neighbor_is_current"]
        if not include_superseded and neighbor_is_current is not None and neighbor_is_current == 0:
            filtered_count += 1
            continue
        direction = "outgoing" if e["source_id"] == node_id else "incoming"
        kw_raw = e["neighbor_recall_keywords"]
        try:
            keywords = json.loads(kw_raw) if kw_raw else []
        except (json.JSONDecodeError, TypeError):
            keywords = []
        result.append({
            "id": neighbor_id,
            "type": e["neighbor_type"],
            "relation": e["relation"],
            "direction": direction,
            "recall_summary": e["neighbor_recall_summary"],
            "recall_keywords": keywords,
            "importance_score": e["neighbor_importance_score"],
            "recall_count": e["neighbor_recall_count"],
        })
    return result, filtered_count


def _build_inspect_recall_view(
    conn: sqlite3.Connection,
    node: dict,
    node_id: str,
    dream_mode: bool,
    warnings: Optional[dict],
    include_superseded: bool = False,
) -> dict:
    """Build the recall-view response for engram_inspect."""
    neighbors, filtered_superseded_count = _get_neighbors_enriched(
        conn, node_id, include_superseded=include_superseded
    )

    # Partition into logical vs contextual.
    logical: list[dict] = []
    contextual: list[dict] = []
    for nb in neighbors:
        rel = nb["relation"]
        if rel in _LOGICAL_SUBSTRATE_RELATIONS:
            logical.append(nb)
        elif rel in _CONTEXTUAL_RELATIONS:
            contextual.append(nb)
        else:
            # Unknown relation — treat as contextual to avoid silent loss.
            contextual.append(nb)

    # Sort logical neighbors into upstream / downstream / lateral.
    # Upstream: outgoing edges — the inspected node uses/cites/resolves/supersedes
    #   the neighbor (outgoing derives_from, supported_by, supersedes, resolves, subtask_of).
    # Downstream: incoming edges — the neighbor uses/cites/resolves/supersedes the
    #   inspected node (incoming versions of the same relations, including incoming
    #   subtask_of children).
    # Lateral: contradicts (both dirs), tensions, exemplifies, retracts.
    upstream: list[dict] = []
    downstream: list[dict] = []
    lateral: list[dict] = []

    _UPSTREAM_OUTGOING = frozenset({
        "derives_from", "supported_by", "supersedes", "resolves", "subtask_of"
    })
    _LATERAL_RELATIONS = frozenset({
        "contradicts", "tensions", "exemplifies", "retracts"
    })

    for nb in logical:
        rel = nb["relation"]
        direction = nb["direction"]
        if rel in _LATERAL_RELATIONS:
            lateral.append(nb)
        elif rel in _UPSTREAM_OUTGOING and direction == "outgoing":
            upstream.append(nb)
        else:
            # incoming version of upstream relations → downstream;
            # also catches incoming retracts etc that aren't lateral.
            downstream.append(nb)

    # Cap each group by importance_score desc (best nodes first), preserving
    # a truncated_count field on the group when it fires.
    def _cap_group(items: list[dict], cap: int) -> tuple[list[dict], int]:
        if len(items) <= cap:
            return items, 0
        sorted_items = sorted(
            items,
            key=lambda x: (x.get("importance_score") or 0.0,
                           x.get("recall_count") or 0),
            reverse=True,
        )
        return sorted_items[:cap], len(items) - cap

    upstream_capped, upstream_remainder = _cap_group(upstream, _INSPECT_RECALL_GROUP_CAP)
    downstream_capped, downstream_remainder = _cap_group(downstream, _INSPECT_RECALL_GROUP_CAP)
    lateral_capped, lateral_remainder = _cap_group(lateral, _INSPECT_RECALL_GROUP_CAP)
    contextual_capped, contextual_remainder = _cap_group(contextual, _INSPECT_RECALL_GROUP_CAP)

    # Build compact neighbor dicts for recall view (strip internal scoring fields).
    def _trim_logical(items: list[dict]) -> list[dict]:
        return [
            {
                "id": nb["id"],
                "type": nb["type"],
                "relation": nb["relation"],
                "direction": nb["direction"],
                "recall_summary": nb["recall_summary"],
                "recall_keywords": nb["recall_keywords"],
            }
            for nb in items
        ]

    def _trim_contextual(items: list[dict]) -> list[dict]:
        return [
            {
                "id": nb["id"],
                "type": nb["type"],
                "relation": nb["relation"],
                "direction": nb["direction"],
                "recall_keywords": nb["recall_keywords"],
            }
            for nb in items
        ]

    logical_neighbors: dict = {
        "upstream": _trim_logical(upstream_capped),
        "downstream": _trim_logical(downstream_capped),
        "lateral": _trim_logical(lateral_capped),
    }
    if upstream_remainder:
        logical_neighbors["truncated_upstream_count"] = upstream_remainder
    if downstream_remainder:
        logical_neighbors["truncated_downstream_count"] = downstream_remainder
    if lateral_remainder:
        logical_neighbors["truncated_lateral_count"] = lateral_remainder

    # Strip heavyweight fields from the focus node for recall view.
    _RECALL_OMIT = frozenset({
        "confidence_history", "logical_chain", "metadata",
        "importance_score", "importance_base", "utility_score",
        "recall_turn", "recall_count",
    })
    node_recall = {k: v for k, v in node.items() if k not in _RECALL_OMIT}

    response: dict = {
        "node": node_recall,
        "view": "recall",
        "dream_mode": dream_mode,
        "logical_neighbors": logical_neighbors,
        "contextual_neighbors": _trim_contextual(contextual_capped),
        "neighbor_count": len(neighbors),
        "hint": "Use view=\"deep\" for full node detail + edge inventory; view=\"edges\" for just the connection map.",
    }
    if contextual_remainder:
        response["truncated_contextual_count"] = contextual_remainder
    if filtered_superseded_count:
        response["truncated_superseded_count"] = filtered_superseded_count
    if warnings:
        response["warnings"] = warnings
    return response


def _build_topology_entries(
    conn: sqlite3.Connection,
    node_id: str,
    include_superseded: bool = False,
) -> tuple[dict[str, list[dict]], int, int]:
    """Build the adjacency-map topology entries + edge count for a focal node.

    Used by both view='deep' and view='edges' on engram_inspect to produce the
    `{source_id: [{src, dst, relation}, ...]}` shape.

    Returns (topology, total_included, filtered_superseded_count).
    filtered_superseded_count is the number of non-current neighbor edges that were
    suppressed when include_superseded=False. Orphan edges (neighbor not in nodes
    table) are always included — a missing JOIN row means is_current is NULL, and
    we never silently drop an edge on an orphan.
    """
    edges = conn.execute(
        """
        SELECT e.relation, e.source_id, e.target_id,
               n.is_current AS neighbor_is_current
        FROM edges e
        LEFT JOIN nodes n ON (
            CASE WHEN e.source_id = ? THEN e.target_id ELSE e.source_id END
        ) = n.id
        WHERE e.source_id = ? OR e.target_id = ?
        """,
        (node_id, node_id, node_id),
    ).fetchall()

    # Build adjacency map keyed by focus node id.
    # Per-edge entry: {src, dst, relation}.
    edge_entries: list[dict] = []
    total = 0
    filtered_count = 0
    for e in edges:
        neighbor_id = e["target_id"] if e["source_id"] == node_id else e["source_id"]
        neighbor_is_current = e["neighbor_is_current"]
        # Filter non-current neighbors unless include_superseded=True.
        # NULL is_current (orphan edge — neighbor not in nodes table) is always included.
        if not include_superseded and neighbor_is_current is not None and neighbor_is_current == 0:
            filtered_count += 1
            continue
        direction = "outgoing" if e["source_id"] == node_id else "incoming"
        rel = e["relation"]
        if direction == "outgoing":
            src, dst = node_id, neighbor_id
        else:  # incoming
            src, dst = neighbor_id, node_id
        entry: dict = {"src": src, "dst": dst, "relation": rel}
        edge_entries.append(entry)
        total += 1

    return {node_id: edge_entries}, total, filtered_count


def _build_inspect_deep_view(
    conn: sqlite3.Connection,
    node: dict,
    node_id: str,
    dream_mode: bool,
    warnings: Optional[dict],
    include_superseded: bool = False,
) -> dict:
    """Build the deep-view response for engram_inspect."""
    topology, total, filtered_superseded_count = _build_topology_entries(
        conn, node_id, include_superseded=include_superseded
    )

    response: dict = {
        "node": node,
        "view": "deep",
        "dream_mode": dream_mode,
        "topology": topology,
        "neighbor_count": total,
    }
    if filtered_superseded_count:
        response["truncated_superseded_count"] = filtered_superseded_count
    if warnings:
        response["warnings"] = warnings
    return response


def _build_inspect_edges_view(
    conn: sqlite3.Connection,
    node_id: str,
    node_type: str,
    include_superseded: bool = False,
) -> dict:
    """Build the edges-view response for engram_inspect."""
    topology, total, filtered_superseded_count = _build_topology_entries(
        conn, node_id, include_superseded=include_superseded
    )

    response: dict = {
        "node_id": node_id,
        "node_type": node_type,
        "view": "edges",
        "topology": topology,
        "neighbor_count": total,
    }
    if filtered_superseded_count:
        response["truncated_superseded_count"] = filtered_superseded_count
    return response


def _extract_warnings(
    metadata: Union[str, dict, None],
    conn: sqlite3.Connection,
) -> Optional[dict]:
    """Convert metadata.tainted_by / metadata.stale_by into a structured
    warnings object for surface-level exposure (MECH-5).

    Previously these fields were buried inside the raw metadata JSON blob,
    so agents reading a tainted derivation saw the original confidence and
    proceeded as if the node were sound (the diagnosed-pattern derivation diagnosis).

    Returns None when the node is clean — callers should omit the field
    rather than render an empty object.

    Shape:
        {
            "tainted_by": [
                {"retracted_id", "retracted_claim_excerpt",
                 "retracted_at", "retraction_reason_excerpt"},
                ...
            ],
            "stale_by": [
                {"superseded_id", "replaced_by_id", "superseded_at"},
                ...
            ],
        }
    """
    if metadata is None:
        return None
    if isinstance(metadata, str):
        if not metadata:
            return None
        try:
            metadata = json.loads(metadata)
        except (json.JSONDecodeError, TypeError):
            return None
    if not isinstance(metadata, dict):
        return None

    tainted_ids = metadata.get("tainted_by") or []
    stale_ids = metadata.get("stale_by") or []
    if not tainted_ids and not stale_ids:
        return None

    warnings: dict = {}

    if tainted_ids:
        t_entries = []
        for rid in tainted_ids:
            row = conn.execute(
                "SELECT claim, metadata FROM nodes WHERE id = ?", (rid,)
            ).fetchone()
            if not row:
                t_entries.append({
                    "retracted_id": rid,
                    "retracted_claim_excerpt": None,
                    "retracted_at": None,
                    "retraction_reason_excerpt": None,
                })
                continue
            rmeta: dict = {}
            if row["metadata"]:
                try:
                    rmeta = json.loads(row["metadata"])
                except (json.JSONDecodeError, TypeError):
                    rmeta = {}
            claim = row["claim"] or ""
            reason = rmeta.get("retraction_reason") or ""
            t_entries.append({
                "retracted_id": rid,
                "retracted_claim_excerpt": claim[:core._WARNING_EXCERPT_LEN],
                "retracted_at": rmeta.get("retracted_at"),
                "retraction_reason_excerpt": reason[:core._WARNING_EXCERPT_LEN],
            })
        warnings["tainted_by"] = t_entries

    if stale_ids:
        s_entries = []
        for sid in stale_ids:
            row = conn.execute(
                "SELECT superseded_by FROM nodes WHERE id = ?", (sid,)
            ).fetchone()
            replaced_by = row["superseded_by"] if row else None
            superseded_at = None
            if replaced_by:
                repl_row = conn.execute(
                    "SELECT created_at FROM nodes WHERE id = ?", (replaced_by,)
                ).fetchone()
                if repl_row:
                    superseded_at = repl_row["created_at"]
            s_entries.append({
                "superseded_id": sid,
                "replaced_by_id": replaced_by,
                "superseded_at": superseded_at,
            })
        warnings["stale_by"] = s_entries

    return warnings if warnings else None


def _decode_embedding(raw) -> list[float] | None:
    """Decode a node's embedding field to a list[float], or None if unavailable.

    The embedding column stores JSON-serialised float arrays (set by
    _compute_and_store_embedding).  Handles three cases:
      - str  → JSON-parse to list[float]
      - list → already decoded (e.g. when a caller stored the raw list)
      - None / anything else → return None (cold-start; no diversity penalty)
    """
    if raw is None:
        return None
    if isinstance(raw, list):
        return raw
    if isinstance(raw, (str, bytes)):
        try:
            decoded = json.loads(raw)
            if isinstance(decoded, list):
                return decoded
        except (json.JSONDecodeError, ValueError):
            pass
    return None


def _max_cosine_to_selected(candidate: dict, selected: list[dict]) -> float:
    """Return max cosine similarity between candidate and any already-selected node.

    Uses EmbeddingManager.cosine_similarity (pure Python, no numpy dep).
    Returns 0.0 when either embedding is absent (cold-start: no penalty).
    """
    e_c = _decode_embedding(candidate.get("_embedding"))
    if e_c is None:
        return 0.0
    best = 0.0
    for s in selected:
        e_s = _decode_embedding(s.get("_embedding"))
        if e_s is None:
            continue
        sim = core._embedder.cosine_similarity(e_c, e_s)
        if sim > best:
            best = sim
    return best


def _mmr_rerank(
    candidates: list[dict],
    top_k: int,
    mmr_lambda: float,
) -> list[dict]:
    """Maximal Marginal Relevance reranker (Tier 3 of the ranking pipeline).

    Selects up to top_k nodes from candidates, greedily maximising:

        mmr(c) = composite(c) × (1 − (1 − mmr_lambda) × max_sim(c, selected))

    where composite(c) is the pre-computed _composite score and max_sim is
    the highest cosine similarity between c and any already-selected node.

    Input contract:
        Each candidate has "_composite" (float) annotated by the upstream
        composite phase. The caller (_search_nodes) is expected to have
        sorted the input by _composite desc — MMR's first-slot pick relies
        on candidate[0] being the highest-composite. As a safety net, MMR
        re-sorts internally; the upstream sort + internal sort are
        intentionally redundant so neither side has to trust the other's
        invariant silently.
        Each candidate may optionally have "_embedding" (decoded list[float]
        or raw JSON str); when absent, max_sim=0 → no diversity penalty.

    Args:
        candidates: list of node dicts, sorted by _composite desc.
        top_k: desired output size (≤ len(candidates)).
        mmr_lambda: ∈ [0, 1].  1 = pure composite (no diversity); 0 = full
            diversity penalty.

    Returns:
        MMR-ordered list, length ≤ top_k.  The "_embedding" key is preserved
        on each dict so the caller can strip it after the full pipeline.
    """
    if not candidates:
        return []
    remaining = list(candidates)
    # Defensive re-sort: pre-MMR sort by the caller is the documented input
    # contract (see docstring), but the cost of re-sorting is negligible and
    # the redundancy protects against either side's invariant breaking
    # silently.  First slot is highest composite (before diversity matters).
    remaining.sort(key=lambda c: c.get("_composite", 0.0), reverse=True)
    selected: list[dict] = [remaining.pop(0)]
    while remaining and len(selected) < top_k:
        best: dict | None = None
        best_score = float("-inf")
        for c in remaining:
            max_sim = _max_cosine_to_selected(c, selected)
            mmr_score = c.get("_composite", 0.0) * (1.0 - (1.0 - mmr_lambda) * max_sim)
            if mmr_score > best_score:
                best_score = mmr_score
                best = c
        if best is None:
            break
        remaining.remove(best)
        selected.append(best)
    return selected


def _search_nodes(
    conn: sqlite3.Connection,
    query: str,
    types: str = "",
    min_confidence: float = 0.0,
    include_superseded: bool = False,
    top_k: int = 10,
    use_semantic: bool = True,
    embed_query: str | None = None,
    *,
    return_debug: bool = False,
) -> dict:
    """Pure FTS5 + semantic search.  No side effects (no refresh, no neighbor expansion).

    Args:
        query: the FTS5 keyword query (also used as semantic query when
            embed_query is None — backward-compat default).
        embed_query: optional separate semantic-search query string. When
            provided, semantic search uses this instead of `query`. Used by
            the auto-surface hook for short prompts that benefit from
            prev-response-tail prepending on the embedding side WITHOUT
            polluting FTS (which stays keyword-matched against `query`).
            See alpha #177.
        return_debug: if True, include a ``debug`` key in the returned dict
            with intermediate ranking signals (keyword extraction, FTS/semantic
            candidate counts, per-result scores). Default False — never bloats
            normal responses. Used by SxS comparison UI and retrieval diagnostics.

    Returns dict with:
        results: list of node dicts (from _node_to_dict) with match_type/similarity added
        keyword_matches: count of FTS hits
        semantic_matches: count of semantic-only hits
        semantic_available: bool
        debug: (only present when return_debug=True) intermediate ranking signals

    Three-tier ranking pipeline (Lei 2026-05-19 architecture) with special-type
    bypass (Lei 2026-05-19 PM, lone-person-node case):

        TIER 1 — Raw retrieval (FTS + semantic, lossy floors only)
            ── FTS5 keyword search: BM25-ranked, capped at (top_k × TIER1_MULTIPLIER)//2,
               post-filtered for type/superseded/confidence/tier2.
            ── Semantic search: cosine-similarity-ranked, min_similarity =
               FTS_SIM_FLOOR (0.30), overfetch to max(top_k × TIER1_MULTIPLIER, 100).
            ── Merge: FTS hits first, then semantic-only; FTS hits get
               relevance via floor+bump (alpha #207/#208), semantic-only
               keep their cosine similarity.
            Output contract: ≤ top_k × TIER1_MULTIPLIER candidates after merge cap;
            unsorted list with _relevance + optional _embedding annotated.

        SPECIAL-TYPE PARTITION — After Tier 1, split the pool by type:
            ── special_pool: types in SPECIAL_TYPES_BYPASS (person, definition, goal,
               axiom, contradiction, question, conjecture, lesson).
               These skip Tiers 2 + 3 entirely. Filter: similarity >= FTS_SIM_FLOOR.
               Sort by similarity desc. All qualifying nodes surface regardless of
               how many generic results exist.
            ── generic_output: everything else (dominantly observation_factual,
               derivation). Proceeds through Tiers 2 + 3 as usual.

        TIER 2 — Composite ranking applied to generic_output only
            composite = relevance × util_amp × imp_amp
                      = relevance × (1 + UTIL_BETA × utility_score)
                                  × (1 + IMP_BETA × normalized_importance)
            Then SORT + CUT to top_k × TIER2_MULTIPLIER.
            Output contract: top_k × TIER2_MULTIPLIER candidates sorted by _composite desc.

        TIER 3 — MMR diversity rerank applied to generic_output only
            mmr(c) = composite(c) × (1 − (1 − MMR_LAMBDA) × max_sim(c, selected))
            Greedy selection of top_k from the TIER2-sized pool.
            Output contract: top_k candidates, MMR-ordered.

        FINAL MERGE — generic_output (top_k) + special_pool (all qualifying).
            top_k is the GENERIC-result budget; special-type results are
            always-include, so len(results) may exceed top_k.
            Callers needing strict top_k can slice further.
            Dedup by node ID (should not occur in practice since types partition cleanly).

        Post-pipeline strip: remove _composite + _embedding internal keys.

    Boundary contracts are explicit (sort before MMR, MMR documents
    the expectation) — see Lei's "quick_sort on 100 items is cheap,
    failed-assumption is devastating" framing 2026-05-19.
    """
    # Default embed_query to query if not explicitly provided (backward compat).
    effective_embed_query = embed_query if embed_query is not None else query
    type_filter = set(t.strip() for t in core._as_csv(types).split(",") if t.strip()) or None
    if type_filter:
        type_filter = {_TYPE_ALIASES.get(t, t) for t in type_filter}
    tier2_threshold = core._get_tier_threshold(conn, 2)

    # --- Debug signal capture (pre-search) ---
    # Collected here, populated incrementally, assembled at return.
    _dbg_extracted_keywords: list = []
    _dbg_fts_candidate_count: int = 0
    _dbg_semantic_candidate_count: int = 0
    _dbg_fts_cap_value: Optional[int] = None
    # Per-result debug signals keyed by node ID (populated during merge loops).
    _dbg_per_result: dict[str, dict] = {}

    if return_debug:
        # Extract keywords using the same function _sanitize_fts_query calls
        # internally — captures its output without modifying it.
        try:
            from engram_idf import extract_keywords
            _dbg_extracted_keywords = list(extract_keywords(conn, query, min_idf=4.0, top_k=5))
        except Exception:
            _dbg_extracted_keywords = []

    # --- Tier-size derivation (Lei 2026-05-19 architecture discussion) ---
    # Tier 1: large raw pool per source.  Tier 2: composite-shrink target.
    # Tier 3 returns top_k (the caller's requested count).
    tier1_size = top_k * core.TIER1_MULTIPLIER   # e.g. 300 for top_k=10
    tier2_size = top_k * core.TIER2_MULTIPLIER   # e.g.  40 for top_k=10

    # ══ TIER 1: Raw retrieval (FTS + semantic) ══════════════════════════
    # Retrieve a large raw pool per source (TIER1_MULTIPLIER × top_k per path).
    # Output: ≤ tier1_size candidates with _relevance + optional _embedding;
    # order is interleaved (FTS-first then semantic-only), NOT sorted.

    # --- FTS keyword search ---
    fts_ids: set[str] = set()
    fts_results: list[sqlite3.Row] = []
    fts_query = core._sanitize_fts_query(query, conn)
    fts_cap = max(1, tier1_size // 2)  # reservation cap for FTS (half of tier-1 pool)
    try:
        if not fts_query:
            raise sqlite3.OperationalError("empty FTS query")
        # LIMIT is tier1_size * 3 (= top_k * TIER1_MULTIPLIER * 3) so the
        # SQL fetch always supplies enough rows for fts_cap (= tier1_size // 2)
        # to be the binding constraint, regardless of TIER1_MULTIPLIER's
        # value. Previously hardcoded as top_k * 3 — became the binding
        # constraint at the old MMR_POOL_MULTIPLIER ≥ 3.
        fts_rows = conn.execute(
            """SELECT n.*, fts.rank
               FROM nodes_fts fts
               JOIN nodes n ON n.rowid = fts.rowid
               WHERE nodes_fts MATCH ?
               ORDER BY fts.rank
               LIMIT ?""",
            (fts_query, tier1_size * 3),
        ).fetchall()

        if return_debug:
            _dbg_fts_candidate_count = len(fts_rows)

        for r in fts_rows:
            if type_filter and r["type"] not in type_filter:
                continue
            # Retracted nodes never surface, regardless of include_superseded flag.
            # Defense-in-depth: trigger removes them from FTS at retraction time,
            # but if any leak through (pre-trigger DBs not yet migrated, the
            # supersede-then-retract path before round-2 fix, or future
            # contamination paths), this catches them.
            if r["status"] == "retracted":
                continue
            if not include_superseded and not r["is_current"]:
                continue
            if r["confidence"] is not None and r["confidence"] < min_confidence:
                continue
            if tier2_threshold > 0 and (r["importance_score"] or 0) < tier2_threshold:
                continue
            fts_results.append(r)
            fts_ids.add(r["id"])
            # Cap FTS at half of tier1_size so semantic results retain real estate
            # in the merged output. With the new IDF-driven OR-match,
            # _sanitize_fts_query can return 40-100+ hits for a single query;
            # without this cap, FTS would fill every slot and crowd out the
            # high-quality semantic matches that carried the baseline.
            # The composite re-rank at the merge tail still sorts the
            # combined pool, so strong FTS hits can still outrank weak
            # semantic ones — the cap just ensures both paths get a seat.
            if len(fts_results) >= fts_cap:
                break

        if return_debug and len(fts_results) >= fts_cap:
            _dbg_fts_cap_value = fts_cap

    except sqlite3.OperationalError:
        pass  # FTS query syntax error — fall through to semantic

    # --- Semantic search (complement, not replace) ---
    # Uses effective_embed_query (= explicit embed_query if provided, else
    # the same query as FTS). This split lets the auto-surface hook prepend
    # prev-response-tail to the SEMANTIC query for short prompts without
    # polluting FTS keyword matching with arbitrary prior-context tokens.
    semantic_only: list[dict] = []
    # Similarity lookup for FTS-hit nodes — populated below from the semantic
    # search results. Used to apply the floor + multiplicative bump (alpha
    # #207/#208) when merging FTS hits with semantic hits.
    sim_lookup: dict[str, float] = {}
    semantic_available = use_semantic and core._embedder.is_available() and core._get_embedding_config().get("enabled", True)
    if semantic_available:
        # Overfetch semantic so FTS-hit similarities (if above floor) are
        # available for the bump computation. min_similarity=FTS_SIM_FLOOR
        # naturally drops "truly unrelated" candidates so we don't waste rows.
        sem_results = core._semantic_search(
            conn, effective_embed_query,
            top_k=max(top_k * core.TIER1_MULTIPLIER, 100),
            min_similarity=core.FTS_SIM_FLOOR,
            type_filter=type_filter,
            importance_threshold=tier2_threshold,
            include_superseded=include_superseded,
        )
        if return_debug:
            _dbg_semantic_candidate_count = len(sem_results)
        for s in sem_results:
            sim_lookup[s["id"]] = s["similarity"]
            if s["id"] not in fts_ids:
                if s["confidence"] is not None and s["confidence"] < min_confidence:
                    continue
                semantic_only.append(s)

    # --- Merge: FTS first, then semantic-only ---
    # FTS-merge formula (alpha #207/#208 mitigation):
    #   if no semantic available: fall back to old 1 - 1/(1+|bm25|) normalization
    #   else: floor at FTS_SIM_FLOOR, multiplicative bump on sim
    #     final = min(1.0, sim * (1.0 + sqrt(|bm25|) / sqrt(FTS_BUMP_NORMALIZER)))
    #   Floor drops nodes where semantic says "truly unrelated" regardless of
    #   how strong FTS appears (closes the FTS-leak).
    output: list[dict] = []
    seen: set[str] = set()

    for r in fts_results:
        if r["id"] in seen:
            continue
        seen.add(r["id"])
        bm25_rank = r["rank"]  # negative float, more-negative = better
        node_sim = sim_lookup.get(r["id"]) if semantic_available else None

        if semantic_available:
            if node_sim is None or node_sim < core.FTS_SIM_FLOOR:
                # Semantic says off-topic — drop regardless of FTS strength
                continue
            bump = min(1.0, math.sqrt(abs(bm25_rank)) / math.sqrt(core.FTS_BUMP_NORMALIZER))
            relevance_norm = min(1.0, node_sim * (1.0 + bump))
        else:
            # Fallback: old BM25-only normalization (semantic unavailable)
            relevance_norm = 1.0 - 1.0 / (1.0 + abs(bm25_rank))

        node = core._node_to_dict(r)
        # Preserve embedding for MMR cosine computation (stripped after MMR).
        # r is an sqlite3.Row from SELECT n.* which includes the embedding col.
        raw_emb = r["embedding"] if "embedding" in r.keys() else None
        if raw_emb is not None:
            node["_embedding"] = raw_emb
        node["match_type"] = "keyword"
        node["_relevance"] = relevance_norm
        if node_sim is not None:
            node["similarity"] = node_sim
        if return_debug:
            _dbg_per_result[r["id"]] = {
                "id": r["id"],
                # Use the same "keyword" label the result-node API uses
                # (line 2063) so debug consumers can cross-reference cleanly.
                "match_type": "keyword",
                "bm25_raw": bm25_rank,
                "relevance_normalized": relevance_norm,
                "similarity": node_sim,
                "composite_score": None,   # computed below after re-rank
                "utility_score": node.get("utility_score"),
                "importance_score": node.get("importance_score"),
                "importance_base": node.get("importance_base"),
                "confidence": node.get("confidence"),
                "surprise_score": None,    # not stored as a column
                "recall_turn": node.get("recall_turn"),
            }
        output.append(node)

    # Fill remaining tier-1 slots with semantic-only hits (up to tier1_size total).
    # Large tier-1 pool ensures tier-2 composite shrink has enough candidates.
    remaining = tier1_size - len(output)
    for s in semantic_only[:remaining]:
        if s["id"] in seen:
            continue
        seen.add(s["id"])
        full = conn.execute("SELECT * FROM nodes WHERE id = ?", (s["id"],)).fetchone()
        if full:
            node = core._node_to_dict(full)
            # Preserve embedding for MMR cosine computation (stripped after MMR).
            raw_emb = full["embedding"] if "embedding" in full.keys() else None
            if raw_emb is not None:
                node["_embedding"] = raw_emb
            node["match_type"] = "semantic"
            node["similarity"] = s["similarity"]
            node["_relevance"] = s["similarity"]
            if return_debug:
                _dbg_per_result[s["id"]] = {
                    "id": s["id"],
                    "match_type": "semantic",
                    "bm25_raw": None,
                    "relevance_normalized": None,
                    "similarity": s["similarity"],
                    "composite_score": None,   # computed below after re-rank
                    "utility_score": node.get("utility_score"),
                    "importance_score": node.get("importance_score"),
                    "importance_base": node.get("importance_base"),
                    "confidence": node.get("confidence"),
                    "surprise_score": None,    # not stored as a column
                    "recall_turn": node.get("recall_turn"),
                }
            output.append(node)

    # ══ SPECIAL-TYPE PARTITION ══════════════════════════════════════════
    # After Tier 1, split the merged pool into special-type and generic paths.
    # Special types (SPECIAL_TYPES_BYPASS) skip Tiers 2 + 3 entirely: they
    # only need similarity >= FTS_SIM_FLOOR to surface. This ensures sparse
    # anchor-types (person, definition, goal) and status-bearing types
    # (axiom, contradiction, question, conjecture, lesson) are never buried
    # by composite amplifiers on common observation_factual / derivation nodes.
    # Lone-person-node case (2026-05-19 PM): a specific person-node case sim=0.3786 above floor but
    # composite=0.57 loses to goodnight obs composite=0.91.
    #
    # Similarity for a node is in node["similarity"] (set during Tier-1 merge
    # for both FTS-hit and semantic paths). When semantic is unavailable,
    # FTS-only nodes won't have "similarity" set — fall back to treating them
    # as passing the floor (FTS already applied its own relevance gate).
    special_pool: list[dict] = []
    generic_output: list[dict] = []
    for node in output:
        if node.get("type") in core.SPECIAL_TYPES_BYPASS:
            sim = node.get("similarity")
            # When semantic unavailable, sim is None; treat as passing floor.
            if sim is None or sim >= core.FTS_SIM_FLOOR:
                special_pool.append(node)
            # If sim is explicitly below floor, drop (inconsistent but defensive).
        else:
            generic_output.append(node)
    # Sort special_pool by similarity desc (None → 0.0 for sort stability).
    special_pool.sort(key=lambda n: n.get("similarity") or 0.0, reverse=True)
    # Strip _relevance from special nodes (not needed — they bypass composite).
    for node in special_pool:
        node.pop("_relevance", None)

    # Diversity-preserving cap (SPECIAL_POOL_CAP). Reserve top-1 per type that
    # has members in the pool, then fill remaining slots from the rest by
    # similarity desc. Preserves the lone-person-node rescue (the single best person
    # node won't be starved by 9 high-sim definitions) while bounding the
    # pool to a sensible size for surface-channel rendering.
    if len(special_pool) > core.SPECIAL_POOL_CAP:
        seen_types: set[str] = set()
        top_per_type: list[dict] = []
        remainder: list[dict] = []
        for n in special_pool:  # already sim-desc sorted
            ntype = n.get("type", "")
            if ntype not in seen_types:
                seen_types.add(ntype)
                top_per_type.append(n)
            else:
                remainder.append(n)
        # remainder is already sim-desc (preserves the input order from special_pool)
        slots_left = max(0, core.SPECIAL_POOL_CAP - len(top_per_type))
        capped = top_per_type + remainder[:slots_left]
        # Resort the final cohort by similarity desc for consistent output order.
        capped.sort(key=lambda n: n.get("similarity") or 0.0, reverse=True)
        special_pool = capped

    # Tier 2 + 3 operate on generic_output only.
    output = generic_output

    # ══ TIER 2: Composite ranking (multiplicative amplifiers) ═══════════
    # Lei 2026-05-19. Replaces the additive λ-blend with pure multiplicative
    # composition: relevance is the base, utility + importance are amplifiers.
    #   composite = relevance × (1 + UTIL_BETA × utility_score)
    #                         × (1 + IMP_BETA × normalized_importance)
    # Then SORT + CUT to top_k × TIER2_MULTIPLIER (= 40 default).
    # _composite is NOT popped here — Tier 3 MMR pass needs it.
    # _composite + _embedding stripped after Tier 3 completes.
    min_queryable_imp = _get_min_queryable_importance(conn, tier2_threshold=tier2_threshold)
    imp_norm_factor = 1.0 / min_queryable_imp
    for node in output:
        rel = node.pop("_relevance", 0.5) or 0.0
        util = node.get("utility_score", 0) or 0.0
        imp = node.get("importance_score", 0) or 0.0
        util_amp = 1.0 + core.UTIL_BETA * util
        imp_amp = 1.0 + core.IMP_BETA * (imp * imp_norm_factor)
        composite = rel * util_amp * imp_amp
        node["_composite"] = composite
        if return_debug and node.get("id") in _dbg_per_result:
            _dbg_per_result[node["id"]]["composite_score"] = composite
            # NEW debug fields surfacing the amp decomposition for harness diagnostics
            _dbg_per_result[node["id"]]["util_amp"] = util_amp
            _dbg_per_result[node["id"]]["imp_amp"] = imp_amp
            _dbg_per_result[node["id"]]["imp_norm_factor"] = imp_norm_factor
    # Sort by composite desc.  Contract with _mmr_rerank: input is sorted
    # (MMR's first-slot pick assumes candidate[0] is highest-composite).
    # MMR also re-sorts internally as a safety net, but enforcing the sort
    # here makes the boundary explicit and inspectable in debugging.
    output.sort(key=lambda x: x.get("_composite", 0), reverse=True)
    # Tier-2 shrink: cut to top_k × TIER2_MULTIPLIER best-composite candidates.
    # This is the tier-boundary cut — tier-1 retrieved broadly, tier-2 shrinks
    # before handing off to MMR.  The sort above ensures we keep the top.
    output = output[:tier2_size]

    # ── TIER 3: MMR diversity reranker (alpha #178 area 2) ───────────────
    # Input: tier2_size candidates (top_k × TIER2_MULTIPLIER, composite-sorted).
    # Output: top_k nodes, MMR-ordered for diversity.
    # When MMR_LAMBDA=1 the output is identical to the composite-sorted order.
    # When embedder is disabled (ENGRAM_NO_EMBEDDINGS=1) all nodes have
    # _embedding=None → max_sim=0 everywhere → MMR reduces to composite order.
    output = _mmr_rerank(output, top_k=top_k, mmr_lambda=core.MMR_LAMBDA)

    # Strip internal-only fields before returning to callers.
    for node in output:
        node.pop("_composite", None)
        node.pop("_embedding", None)
    # Strip _embedding from special_pool too (set during Tier 1).
    for node in special_pool:
        node.pop("_embedding", None)

    # ══ FINAL MERGE: generic (top_k) + special-type bypass results ══════
    # top_k is the generic-result budget. Special-type results append after,
    # so len(results) may exceed top_k. Dedup by node ID (types partition
    # cleanly in practice, but the guard is cheap).
    generic_ids = {n["id"] for n in output}
    for node in special_pool:
        if node["id"] not in generic_ids:
            output.append(node)

    # keyword_matches counts post-floor (after the sim<FTS_SIM_FLOOR drop)
    # so callers see the actual contributing FTS pool, not the pre-floor
    # candidate count. Pre-floor count is in `debug.fts_candidate_count`.
    kw_post_floor = sum(1 for n in output if n.get("match_type") == "keyword")
    base_result = {
        "results": output,
        "keyword_matches": kw_post_floor,
        "semantic_matches": len(semantic_only),
        "semantic_available": core._embedder.is_available(),
    }

    if return_debug:
        # Build per_result list in the same order as output (post-sort).
        per_result_ordered = [
            _dbg_per_result[node["id"]]
            for node in output
            if node.get("id") in _dbg_per_result
        ]
        base_result["debug"] = {
            "extracted_keywords": _dbg_extracted_keywords,
            "fts_query_string": fts_query,
            "fts_candidate_count": _dbg_fts_candidate_count,
            "semantic_candidate_count": _dbg_semantic_candidate_count,
            "fts_cap_applied": _dbg_fts_cap_value,
            # Always emit the actual query passed to the embedder (whether
            # it was the caller's `query` arg or an explicit `embed_query`
            # override for prepending). The flag distinguishes the two cases.
            "embed_query_used": effective_embed_query,
            "embed_query_overridden": embed_query is not None,
            "per_result": per_result_ordered,
            "snapshot_turn": core._get_current_turn(),
        }

    return base_result


def _build_tiered_results(
    results: list[dict],
    summary_top_k: int,
) -> list[dict]:
    """Convert a ranked list of node dicts to the Wave-C tiered return shape.

    Tier 1 (first ``summary_top_k`` entries):
        {"id": "...", "summary": "<recall_summary or truncated claim>"}

    Tier 2 (remaining entries):
        {"id": "...", "keywords": [...]}   if recall_keywords present and non-empty
        {"id": "..."}                       otherwise (bare-ID, intentional)

    Ordering = input ranking order.  No score or position fields.
    Type is carried by the ID prefix (ax_, ob_, dv_, etc.).

    Note: for engram_query_pattern, Tier 2 entries are bare-ID because the
    per-pattern result builders construct dicts from raw SQL without pulling
    recall_keywords.  Future enhancement: update each ``_pattern_*`` function
    to include ``recall_keywords`` for richer Tier 2 rendering on pattern
    queries.

    Args:
        results: Ranked list of node dicts (already processed — metadata stripped,
            neighbors added for engram_query; raw pattern dicts for query_pattern).
        summary_top_k: How many top entries get summaries.  0 = all tier-2;
            >= len(results) = all tier-1.  Caller is responsible for clamping
            to valid range before calling.
    """
    tiered: list[dict] = []
    for i, node in enumerate(results):
        node_id = node.get("id", "")
        confidence = node.get("confidence")
        if i < summary_top_k:
            # Tier 1: prefer recall_summary, fall back to truncated claim.
            summary = node.get("recall_summary") or None
            if not summary:
                claim = node.get("claim") or ""
                summary = (claim[:160] + "...") if len(claim) > 160 else claim
            tiered.append({"id": node_id, "confidence": confidence, "summary": summary})
        else:
            # Tier 2: prefer recall_keywords; bare-ID (+ confidence) if missing/empty.
            kw = node.get("recall_keywords")
            # recall_keywords may arrive as a list (already parsed by _node_to_dict)
            # or as a JSON string if the node came from a pattern impl that does
            # its own dict construction.  Normalise here.
            if isinstance(kw, str):
                try:
                    kw = json.loads(kw)
                except (ValueError, json.JSONDecodeError):
                    kw = None
            if kw:
                tiered.append({"id": node_id, "confidence": confidence, "keywords": kw})
            else:
                tiered.append({"id": node_id, "confidence": confidence})
    return tiered


def _conn_total_count(where_clause: str, params: list) -> int:
    """Run COUNT(*) for the given WHERE clause. Helper for _list_impl."""
    conn = core._get_db()
    try:
        return conn.execute(f"SELECT COUNT(*) FROM nodes WHERE {where_clause}", params).fetchone()[0]
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Impl functions (moved from server.py)
# ---------------------------------------------------------------------------

def _surface_impl(
    query: str,
    top_k: int = 10,
    semantic: bool = True,
    embed_query: str | None = None,
) -> str:
    """Impl for engram_surface — callable with named kwargs for in-server callers."""
    conn = core._get_db()
    try:
        search = _search_nodes(conn, query, top_k=top_k, use_semantic=semantic,
                               embed_query=embed_query)
        results = search["results"]

        if not results:
            return json.dumps({
                "query": query,
                "match_count": 0,
                "message": "No matching nodes found in the knowledge graph.",
            })

        # Snapshot wall-clock now once so every humanized timestamp in this
        # surface response is rendered against the same reading (MECH-2 of
        # the time-awareness derivation / the CLOCK-DRIFT-hygiene derivation).
        now_utc = datetime.now(timezone.utc)

        # --- Type breakdown ---
        type_counts: dict[str, int] = {}
        for n in results:
            t = n.get("type", "unknown")
            type_counts[t] = type_counts.get(t, 0) + 1

        # --- Special nodes (always worth reviewing) ---
        # Covers all SPECIAL_TYPES_BYPASS types: status-bearing (axiom,
        # contradiction, question, conjecture, lesson) and sparse anchor-types
        # (definition, person, goal). All now bypass composite+MMR in
        # _search_nodes, so they can arrive here even when relevance is low.
        # Display cap: top-3 by retrieval order (symmetric with top_claims).
        # Retrieval cap (SPECIAL_POOL_CAP=10) is separate — this is the display
        # slice only.
        special = []
        for n in results:
            ntype = n.get("type", "")
            if ntype in core.SPECIAL_TYPES_BYPASS:
                entry: dict = {
                    "id": n["id"], "type": ntype,
                    "claim": n.get("claim", ""),
                    "created_ago": core._humanized_ago(n.get("created_at"), now=now_utc),
                    "recall_summary": n.get("recall_summary"),
                    "recall_keywords": json.loads(n.get("recall_keywords") or "null"),
                }
                if ntype == "question":
                    entry["status"] = n.get("status", "open")
                elif ntype == "conjecture":
                    entry["status"] = n.get("status", "open")
                    entry["confidence"] = n.get("confidence")
                elif ntype == "axiom":
                    entry["confidence"] = n.get("confidence")
                special.append(entry)
        special = special[:3]

        # --- Top claims (MMR-ordered, non-special) ---
        # Historically this block re-sorted claim_nodes by raw confidence-desc
        # before slicing [:3], which masked the composite+MMR ranked order
        # produced by _search_nodes (ob_NNNN, alpha #178 area 1).
        # The sort is intentionally removed: _search_nodes already delivers
        # results in composite+MMR order, so claim_nodes[:3] preserves that
        # canonical ranking.  All SPECIAL_TYPES_BYPASS types are excluded here
        # — they are surfaced in special_nodes above.
        claim_nodes = [
            n for n in results
            if n.get("type", "") not in (
                "axiom", "contradiction", "question", "conjecture", "evidence",
                "lesson", "definition", "person", "goal",
            )
            and n.get("confidence") is not None
        ]
        top_claims = [
            {
                "id": n["id"], "type": n["type"],
                "claim": n.get("claim", ""),
                "confidence": n.get("confidence"),
                "created_ago": core._humanized_ago(n.get("created_at"), now=now_utc),
                "recall_summary": n.get("recall_summary"),
                "recall_keywords": json.loads(n.get("recall_keywords") or "null"),
            }
            for n in claim_nodes[:3]
        ]

        # --- Age signal ---
        mem = core._get_memory_config()
        current_turn = mem.get("current_turn", 0)
        recall_turns = [n.get("recall_turn", 0) for n in results if n.get("recall_turn") is not None]
        old_nodes = [n["id"] for n in results if (n.get("recall_turn") or 0) < max(0, current_turn - 2)]

        # Humanized wall-clock range across the matched set — converts the
        # recall_turn-only age signal (which measures checkpoints, not
        # wall-clock) into a real-time cue, covering T4 (gap-since-last-session)
        # of the time-awareness derivation.
        created_ats = [n.get("created_at") for n in results if n.get("created_at")]
        if created_ats:
            newest_iso = max(created_ats)
            oldest_iso = min(created_ats)
            wall_clock_range = {
                "newest_ago": core._humanized_ago(newest_iso, now=now_utc),
                "oldest_ago": core._humanized_ago(oldest_iso, now=now_utc),
            }
        else:
            wall_clock_range = None

        # --- Stale / tainted counts + structured per-node warnings ---
        # MECH-5: in addition to aggregate counts, surface the full
        # warnings shape per-node so agents can act on specific taints
        # without a follow-up engram_inspect call.
        stale_count = 0
        tainted_count = 0
        warned_nodes: dict[str, dict] = {}
        for n in results:
            meta_str = n.get("metadata", "")
            if isinstance(meta_str, str) and meta_str:
                # Anchored to the serialized JSON list shape to avoid
                # false positives from claim text containing these phrases.
                if '"stale_by": [' in meta_str:
                    stale_count += 1
                if '"tainted_by": [' in meta_str:
                    tainted_count += 1
            w = _extract_warnings(meta_str, conn)
            if w:
                warned_nodes[n["id"]] = w
        # Attach warnings inline on top_claims / special entries so callers
        # can notice without cross-referencing the warned_nodes map.
        for entry in top_claims:
            if entry["id"] in warned_nodes:
                entry["warnings"] = warned_nodes[entry["id"]]
        for entry in special:
            if entry["id"] in warned_nodes:
                entry["warnings"] = warned_nodes[entry["id"]]

        # --- All matched IDs for follow-up ---
        matched_ids = [n["id"] for n in results]

        # --- Per-node metadata for "Others" digest rendering (Lei 2026-05-19 PM) ---
        # Surfaces id + type + recall_keywords for every matched node so the
        # hook can render a faceted "Others:" list with keyword prefixes for
        # the nodes not in top_claims/special_nodes. Keywords-only (no summary)
        # keeps the lossy-by-design noetic-register principle intact: the agent
        # gets a richer index for "which one should I deliberate-recall on"
        # without flooding context with claim content.
        matched_meta = []
        for n in results:
            kw_raw = n.get("recall_keywords")
            try:
                kw = json.loads(kw_raw) if kw_raw else None
            except (json.JSONDecodeError, TypeError):
                kw = None
            matched_meta.append({
                "id": n["id"],
                "type": n.get("type"),
                "recall_keywords": kw,
            })

        summary = {
            "query": query,
            "match_count": len(results),
            "type_counts": type_counts,
            "special_nodes": special,
            "top_claims": top_claims,
            "age": {
                "current_turn": current_turn,
                "recall_turn_range": [min(recall_turns), max(recall_turns)] if recall_turns else None,
                "wall_clock_range": wall_clock_range,
                "not_recalled_recently": old_nodes,
            },
            "issues": {},
            "matched_ids": matched_ids,
            "matched_meta": matched_meta,
            "search_methods": {
                "keyword_matches": search["keyword_matches"],
                "semantic_matches": search["semantic_matches"],
                "semantic_available": search["semantic_available"],
            },
            "hint": "Use engram_inspect(node_id) for full detail on a specific node, or engram_get_subgraph(node_id) for full evidence chain.",
        }
        if stale_count:
            summary["issues"]["stale_nodes"] = stale_count
        if tainted_count:
            summary["issues"]["tainted_nodes"] = tainted_count
        if warned_nodes:
            summary["issues"]["warnings_by_id"] = warned_nodes

        # --- Tier A/B comparison (ob_NNNN, the keyword-recognition-signal conjecture) ---
        test_config = mem.get("test")
        if test_config and test_config.get("enabled"):
            test_max = test_config.get("tier2_max_nodes", 1000)
            # Compute test threshold using same logic as _get_tier_threshold
            total_current = conn.execute(
                "SELECT COUNT(*) as c FROM nodes WHERE is_current = 1"
            ).fetchone()["c"]
            if total_current <= test_max:
                test_threshold = 0.0
            else:
                row_t = conn.execute(
                    """SELECT importance_score FROM nodes
                       WHERE is_current = 1
                       ORDER BY importance_score DESC
                       LIMIT 1 OFFSET ?""",
                    (test_max - 1,),
                ).fetchone()
                test_threshold = row_t["importance_score"] if row_t else 0.0

            # Check which base results would be excluded by test config
            tier2_threshold_base = core._get_tier_threshold(conn, 2)
            surviving_ids = set()
            excluded = []
            for n in results:
                imp = n.get("importance_score", 0) or 0
                if imp < test_threshold:
                    excluded.append(n)
                else:
                    surviving_ids.add(n["id"])

            # For each excluded node, check 1-hop reachability from surviving nodes
            excluded_details = []
            for n in excluded:
                neighbors = _get_neighbors(conn, n["id"])
                reachable_via = [
                    nb["neighbor_id"] for nb in neighbors
                    if nb["neighbor_id"] in surviving_ids
                ]
                excluded_details.append({
                    "id": n["id"],
                    "type": n.get("type", ""),
                    "claim": (n.get("claim") or "")[:100],
                    "importance_score": round(n.get("importance_score", 0) or 0, 4),
                    "reachable_via": reachable_via[:3],
                })

            reachable_count = sum(1 for e in excluded_details if e["reachable_via"])
            summary["tier_ab"] = {
                "base": {
                    "tier2_max_nodes": mem.get("tier2_max_nodes", 2000),
                    "threshold": round(tier2_threshold_base, 4),
                    "total_searchable": total_current,
                },
                "test": {
                    "tier2_max_nodes": test_max,
                    "threshold": round(test_threshold, 4),
                    "total_searchable": min(total_current, test_max),
                },
                "results_excluded_by_test": len(excluded_details),
                "excluded_but_reachable": reachable_count,
                "excluded_and_orphaned": len(excluded_details) - reachable_count,
                "excluded_details": excluded_details,
                "interpretation": (
                    f"Of {len(results)} results, {len(excluded_details)} would be excluded "
                    f"by test config (tier2={test_max}). "
                    f"{reachable_count} of those are reachable via 1-hop graph links "
                    f"from surviving results (recallable through thinking chains). "
                    f"{len(excluded_details) - reachable_count} would be truly lost."
                ),
            }

        return json.dumps(core._strip_agent_facing(summary))
    finally:
        conn.close()


def _inspect_impl(
    node_id: str,
    view: str = "recall",
    dream_mode: bool = False,
    include_superseded: bool = False,
) -> str:
    """Impl for engram_inspect — callable with named kwargs for in-server callers."""
    _VALID_VIEWS = {"recall", "deep", "edges"}
    if view not in _VALID_VIEWS:
        return json.dumps({
            "error": f"Invalid view '{view}'. Must be one of: recall, deep, edges."
        })

    conn = core._get_db()
    try:
        row = conn.execute("SELECT * FROM nodes WHERE id = ?", (node_id,)).fetchone()
        if not row:
            return json.dumps({"error": f"Node '{node_id}' not found."})

        # For edges view, skip the heavy node fetch — just need id + type.
        if view == "edges":
            node_type = row["type"]

            # Refresh + reward still fire for edges view (deliberate probe).
            if not dream_mode:
                _refresh_recall(conn, [node_id])
                core._utility_reward(conn, [node_id], action="inspect")
                conn.commit()

            # edges view is pure-topology browsing: the early-return here is intentional —
            # it skips the question-node last_assessed_at bump below (pure-topology calls
            # should not carry assessment side-effects).
            return json.dumps(core._strip_agent_facing(
                _build_inspect_edges_view(conn, node_id, node_type,
                                          include_superseded=include_superseded)
            ))

        # recall + deep: build the full node dict.
        node = core._node_to_dict(row)

        # Parse metadata for structured fields (needed by both recall + deep).
        meta = {}
        if node.get("metadata"):
            try:
                meta = json.loads(node["metadata"])
            except (json.JSONDecodeError, TypeError):
                pass
        node["parsed_metadata"] = meta

        # MECH-2 (the time-awareness derivation): render humanized wall-clock ages alongside the
        # ISO timestamps the schema already stores. Additive — the ISO fields
        # stay, so precision isn't lost. `created_ago` is always rendered;
        # the other two only if the source field is present.
        now_utc = datetime.now(timezone.utc)
        node["created_ago"] = core._humanized_ago(node.get("created_at"), now=now_utc)
        if node.get("focused_at"):
            node["focused_ago"] = core._humanized_ago(node.get("focused_at"), now=now_utc)
        if node.get("last_assessed_at"):
            node["last_assessed_ago"] = core._humanized_ago(node.get("last_assessed_at"), now=now_utc)

        # MECH-5: surface taint / stale as a top-level warning.
        warnings = _extract_warnings(meta, conn)

        # Refresh recall on the target node ONLY (skip in dream mode)
        if not dream_mode:
            _refresh_recall(conn, [node_id])
            core._utility_reward(conn, [node_id], action="inspect")
            conn.commit()

        # Update assessment timestamp on question nodes (orthogonal to dream_mode)
        if node.get("type") == "question":
            conn.execute(
                "UPDATE nodes SET last_assessed_turn = ?, last_assessed_at = ? WHERE id = ?",
                (core._get_current_turn(), core._now(), node_id),
            )
            conn.commit()

        if view == "recall":
            return json.dumps(core._strip_agent_facing(
                _build_inspect_recall_view(conn, node, node_id, dream_mode, warnings,
                                           include_superseded=include_superseded)
            ))
        else:  # view == "deep"
            return json.dumps(core._strip_agent_facing(
                _build_inspect_deep_view(conn, node, node_id, dream_mode, warnings,
                                          include_superseded=include_superseded)
            ))
    finally:
        conn.close()


def _query_impl(
    query: str,
    types: str = "",
    min_confidence: float = 0.0,
    include_superseded: bool = False,
    top_k: int = 10,
    return_debug: bool = False,
    summary_top_k: int = 3,
) -> str:
    """Impl for engram_query — callable with named kwargs for in-server callers."""
    conn = core._get_db()
    try:
        search = _search_nodes(
            conn, query, types=types, min_confidence=min_confidence,
            include_superseded=include_superseded, top_k=top_k,
            return_debug=return_debug,
        )

        # Enrich results with neighbors (needed for both debug + tiered paths:
        # debug returns the full enriched dicts; tiered path needs recall_keywords
        # which come from _node_to_dict / _search_nodes, but warnings need conn).
        output = []
        for node in search["results"]:
            # MECH-5: extract warnings BEFORE popping metadata so agents
            # reading query results see taint / stale status.
            w = _extract_warnings(node.get("metadata"), conn)
            node.pop("metadata", None)
            node["neighbors"] = _get_neighbors(conn, node["id"])
            if w:
                node["warnings"] = w
            output.append(node)

        # Refresh recall for all accessed nodes.
        if output:
            accessed_ids = [n["id"] for n in output]
            _refresh_recall(conn, accessed_ids)

        # --- engram.tool.engram_call event (DESIGN.md §4.2) ---
        # Decision context: query length + result count + type filter used.
        # source_class_filter is not a parameter of engram_query (only type
        # filter exists); emitted as None. Noted in phase-3 F1 handoff.
        emit_if_initialized(
            event_type="engram.tool.engram_call",
            level=1,
            data={
                "tool_name": "engram_query",
                "query_text_len": len(query),
                "result_count": len(output),
                "type_filter": types or None,
                "source_class_filter": None,
            },
        )

        # return_debug=True: preserve full legacy shape verbatim (eval/harness contract).
        if return_debug:
            response_body: dict = {
                "query": query,
                "results": output,
                "count": len(output),
                "search_methods": {
                    "keyword_matches": search["keyword_matches"],
                    "semantic_matches": search["semantic_matches"],
                    "semantic_available": search["semantic_available"],
                },
                "message": (
                    f"Found {len(output)} matching node(s)."
                    if output
                    else "No matching nodes found in the knowledge graph."
                ),
            }
            if "debug" in search:
                response_body["debug"] = search["debug"]
            return json.dumps(core._strip_agent_facing(response_body))

        # Default path: tiered summary/keywords shape (Wave C recall_summary rollout).
        # Clamp summary_top_k to [0, len(output)] so callers don't have to worry
        # about off-by-one edge cases.
        effective_top_k = max(0, min(summary_top_k, len(output)))
        tiered = _build_tiered_results(output, effective_top_k)
        return json.dumps(core._strip_agent_facing({
            "results": tiered,
            "query": query,
            "total_matches": len(output),
        }))
    finally:
        conn.close()


def _subgraph_impl(
    node_id: str,
    depth: int = 2,
    direction: str = "both",
    view: str = "recall",
    dream_mode: bool = False,
) -> str:
    """Impl for engram_get_subgraph — callable with named kwargs for in-server callers."""
    if view not in ("recall", "edges"):
        if view == "deep":
            return json.dumps({
                "error": (
                    "view='deep' is not supported by engram_get_subgraph. "
                    "Use engram_inspect(node_id, view='deep') for full node content."
                )
            })
        return json.dumps({
            "error": f"Invalid view '{view}'. Valid values: 'recall', 'edges'."
        })

    if direction not in ("up", "down", "both"):
        return json.dumps({
            "error": f"Invalid direction '{direction}'. Valid values: 'up', 'down', 'both'."
        })

    conn = core._get_db()
    try:
        root_row = conn.execute("SELECT * FROM nodes WHERE id = ?", (node_id,)).fetchone()
        if not root_row:
            return json.dumps({"error": f"Node '{node_id}' not found."})

        # BFS traversal — track hop_distance per node.
        # visited maps node_id → hop_distance
        visited: dict[str, int] = {}
        edges_out: list[dict] = []

        queue: deque[tuple[str, int]] = deque()
        queue.append((node_id, 0))

        while queue:
            nid, hop = queue.popleft()
            if nid in visited:
                continue
            visited[nid] = hop

            if hop >= depth:
                # At the depth boundary — record the node but don't traverse further
                continue

            # Traverse edges based on direction
            if direction in ("up", "both"):
                # Outgoing edges from this node (toward evidence/sources)
                outgoing = conn.execute(
                    "SELECT * FROM edges WHERE source_id = ?", (nid,)
                ).fetchall()
                for e in outgoing:
                    edges_out.append(dict(e))
                    if e["target_id"] not in visited:
                        queue.append((e["target_id"], hop + 1))

            if direction in ("down", "both"):
                # Incoming edges to this node (toward dependents)
                incoming = conn.execute(
                    "SELECT * FROM edges WHERE target_id = ?", (nid,)
                ).fetchall()
                for e in incoming:
                    edges_out.append(dict(e))
                    if e["source_id"] not in visited:
                        queue.append((e["source_id"], hop + 1))

        # Deduplicate edges (source_id, target_id, relation) and keep only those
        # where BOTH endpoints are in the subgraph (topology truncation rule).
        seen_edge_keys: set[tuple] = set()
        unique_edges: list[dict] = []
        for e in edges_out:
            src, tgt, rel = e["source_id"], e["target_id"], e["relation"]
            key = (src, tgt, rel)
            if key in seen_edge_keys:
                continue
            # Drop boundary edges whose far endpoint is outside the subgraph
            if src not in visited or tgt not in visited:
                continue
            seen_edge_keys.add(key)
            edge_entry: dict = {"source_id": src, "target_id": tgt, "relation": rel}
            unique_edges.append(edge_entry)

        # Build adjacency-map topology: keyed by source node ID.
        # Each entry: {to, relation, direction (outgoing/incoming relative to key node)}
        topology: dict[str, list[dict]] = {nid: [] for nid in visited}
        for e in unique_edges:
            src, tgt, rel = e["source_id"], e["target_id"], e["relation"]
            entry_out: dict = {"to": tgt, "relation": rel, "direction": "outgoing"}
            entry_in: dict = {"to": src, "relation": rel, "direction": "incoming"}
            topology[src].append(entry_out)
            topology[tgt].append(entry_in)

        # Build content dict for view="recall"
        content: Optional[dict] = None
        if view == "recall":
            content = {}
            for nid, hop in visited.items():
                node_row = conn.execute(
                    "SELECT type, recall_summary, recall_keywords FROM nodes WHERE id = ?",
                    (nid,),
                ).fetchone()
                if not node_row:
                    continue
                entry: dict = {
                    "type": node_row["type"],
                    "hop_distance": hop,
                }
                kw_raw = node_row["recall_keywords"]
                kw = None
                if kw_raw:
                    try:
                        kw = json.loads(kw_raw)
                    except (json.JSONDecodeError, TypeError):
                        kw = None
                if kw is not None:
                    entry["recall_keywords"] = kw
                # recall_summary: root + 1-hop only; suppress at 2+
                if hop <= 1:
                    rs = node_row["recall_summary"]
                    if rs:
                        entry["recall_summary"] = rs
                content[nid] = entry

        # Memory management
        if visited:
            # Utility bump: root only (the agent's explicit USE target)
            core._utility_reward(conn, [node_id], action="subgraph")
            # Recall refresh: all neighbours except root, unless dream_mode
            if not dream_mode:
                neighbor_ids = [nid for nid in visited if nid != node_id]
                if neighbor_ids:
                    _refresh_recall(conn, neighbor_ids)
            conn.commit()

        response: dict = {
            "root": node_id,
            "depth": depth,
            "direction": direction,
            "view": view,
            "dream_mode": dream_mode,
            "topology": topology,
            "node_count": len(visited),
            "edge_count": len(unique_edges),
        }
        if content is not None:
            response["content"] = content

        return json.dumps(core._strip_agent_facing(response))
    finally:
        conn.close()


def _history_impl(
    mode: str = "edits",
    node_id: str = "",
    action: str = "",
    since: str = "",
    limit: int = 50,
) -> str:
    """Impl for engram_history — callable with named kwargs for in-server callers."""
    limit = min(max(limit, 1), 200)
    conn = core._get_db()
    try:
        if mode == "diagnostics":
            query = "SELECT * FROM diagnostic_history"
            params: list = []
            if since:
                query += " WHERE timestamp > ?"
                params.append(since)
            query += " ORDER BY id DESC LIMIT ?"
            params.append(limit)
            rows = conn.execute(query, params).fetchall()

            snapshots = []
            for r in rows:
                metrics = json.loads(r["metrics"])
                snapshots.append({
                    "turn": r["turn"],
                    "timestamp": r["timestamp"],
                    "checkpoint_mode": r["checkpoint_mode"],
                    "health_score": metrics.get("health_score"),
                    "total_nodes": metrics.get("structure", {}).get("total_current"),
                    "total_edges": metrics.get("structure", {}).get("total_edges"),
                    "tainted": metrics.get("epistemic", {}).get("tainted_nodes"),
                    "stale": metrics.get("epistemic", {}).get("stale_nodes"),
                    "embedding_pct": metrics.get("memory", {}).get("embedding_coverage", {}).get("coverage_pct"),
                })
            return json.dumps({
                "mode": "diagnostics",
                "count": len(snapshots),
                "snapshots": snapshots,
                "hint": "For full metrics of a specific snapshot, use engram_history with a tighter time filter.",
            })

        # Default: edits mode
        conditions = []
        params = []
        if node_id:
            conditions.append("node_id = ?")
            params.append(node_id)
        if action:
            conditions.append("action = ?")
            params.append(action)
        if since:
            conditions.append("timestamp > ?")
            params.append(since)

        where = (" WHERE " + " AND ".join(conditions)) if conditions else ""
        query = f"SELECT * FROM edit_history{where} ORDER BY id DESC LIMIT ?"
        params.append(limit)

        rows = conn.execute(query, params).fetchall()
        entries = []
        for r in rows:
            entries.append({
                "id": r["id"],
                "timestamp": r["timestamp"],
                "turn": r["turn"],
                "action": r["action"],
                "node_id": r["node_id"],
                "node_type": r["node_type"],
                "details": json.loads(r["details"] or "{}"),
            })

        # Total count for context
        count_query = f"SELECT COUNT(*) as c FROM edit_history{where}"
        total = conn.execute(count_query, params[:-1]).fetchone()["c"]

        if node_id:
            core._utility_reward(conn, [node_id], action="history")
            conn.commit()

        return json.dumps({
            "mode": "edits",
            "shown": len(entries),
            "total_matching": total,
            "entries": entries,
        })
    finally:
        conn.close()


def _list_impl(
    node_type: str = "",
    status: str = "",
    sort_by: str = "id",
    limit: int = 100,
    filters_json: str = "",
    fields_json: str = "",
    unlimited: bool = False,
    include_superseded: bool = False,
) -> str:
    """Impl for engram_list — callable with named kwargs for in-server callers."""
    from engram_filter import (
        FilterError,
        parse_filters,
        validate_fields,
        contains_field,
    )

    structured_mode = bool(filters_json) or bool(fields_json)

    # Parse + validate structured-filter inputs up front so errors are
    # reported before any DB work.
    filter_sql_frag: str = ""
    filter_params: list = []
    projected_fields: list[str] = []

    if filters_json:
        try:
            filters_obj = json.loads(filters_json)
        except json.JSONDecodeError as e:
            return json.dumps({"status": "error", "error": f"filters_json: invalid JSON — {e}"})
        try:
            filter_sql_frag, filter_params = parse_filters(filters_obj)
        except FilterError as e:
            return json.dumps({"status": "error", "error": str(e)})

        # Conflict check: if filters_json includes a 'type' or 'status' atomic
        # AND the caller also set the legacy kwarg, refuse rather than merge.
        # (Per issue #81 Item 11.) The recursive walk via contains_field is
        # more precise than a textual scan: it inspects condition fields only,
        # avoiding false positives where a value happens to equal "type" or
        # "status" (e.g., {"field": "claim", "op": "contains", "value": "type"}).
        if node_type and contains_field(filters_obj, "type"):
            return json.dumps({
                "status": "error",
                "error": "conflict: 'node_type' kwarg AND a 'type' field in filters_json are both set. Use one or the other."
            })
        if status and contains_field(filters_obj, "status"):
            return json.dumps({
                "status": "error",
                "error": "conflict: 'status' kwarg AND a 'status' field in filters_json are both set. Use one or the other."
            })

    if fields_json:
        try:
            fields_obj = json.loads(fields_json)
        except json.JSONDecodeError as e:
            return json.dumps({"status": "error", "error": f"fields_json: invalid JSON — {e}"})
        try:
            projected_fields = validate_fields(fields_obj)
        except FilterError as e:
            return json.dumps({"status": "error", "error": str(e)})

    # Build WHERE clause. Default is current-revision only; `include_superseded`
    # opt-in relaxes that predicate so the historical layer becomes visible
    # (text-layer leak audits, e.g., Mao-Cao prototype the text-layer-leak-audit prototype / the text-layer-leak-audit lesson).
    where_parts: list[str] = []
    if not include_superseded:
        where_parts.append("is_current = 1")
    params: list = []
    if node_type:
        where_parts.append("type = ?")
        params.append(node_type)
    if status:
        where_parts.append("status = ?")
        params.append(status)
    if filter_sql_frag:
        where_parts.append(filter_sql_frag)
        params.extend(filter_params)

    # Sentinel: if include_superseded=True AND no other predicates are set,
    # where_parts is empty and `WHERE {where}` would be malformed. The 1=1
    # no-op preserves the existing query-building shape and is harmless.
    if not where_parts:
        where_parts.append("1=1")

    where = " AND ".join(where_parts)

    sort_map = {
        "id": "id ASC",
        "created": "created_at DESC",
        "importance": "importance_score DESC",
        "recalls": "recall_count DESC",
    }
    order = sort_map.get(sort_by, "id ASC")

    # Limit semantics (per issue #81 + Lei's state-table refinement):
    #   unlimited=True OR limit=None  → no cap
    #   limit == 0                    → count-only (no rows fetched)
    #   limit > 0                     → cap at that int
    #   limit < 0                     → reject as malformed
    if limit is not None and limit < 0:
        return json.dumps({"status": "error", "error": "limit must be >= 0 or unlimited=True"})

    if unlimited:
        effective_limit = None  # SQL "LIMIT -1" returns all rows; we'll just omit LIMIT
    elif limit == 0:
        effective_limit = 0  # count-only
    elif structured_mode:
        # No hard ceiling in structured-filter mode — caller has the precision
        # they need via filters. The default 100 still applies if they didn't
        # touch limit. Otherwise honor whatever they passed.
        effective_limit = limit
    else:
        # Legacy single-field-mode ceiling of 500.
        effective_limit = max(1, min(limit, 500))

    # Compute total_matched regardless of effective_limit.
    total = _conn_total_count(where, params)

    # Determine columns to fetch.
    if projected_fields:
        # Always include 'id' so callers can correlate even if they didn't
        # ask for it; preserves order otherwise.
        cols_to_fetch = ["id"] + [f for f in projected_fields if f != "id"]
        select_cols = ", ".join(f'"{c}"' for c in cols_to_fetch)
    else:
        # Default compact triage shape. recall_summary is fetched alongside
        # claim so the triage entry can surface the curated summary when one
        # has been backfilled (per Wave-D recall_summary rollout).
        cols_to_fetch = [
            "id", "type", "claim", "status", "confidence", "recall_count",
            "importance_score", "created_at", "memory_status", "recall_summary",
        ]
        select_cols = ", ".join(f'"{c}"' for c in cols_to_fetch)

    if effective_limit == 0:
        rows: list = []
    else:
        conn = core._get_db()
        query = f"SELECT {select_cols} FROM nodes WHERE {where} ORDER BY {order}"
        if effective_limit is not None:
            query += " LIMIT ?"
            rows = conn.execute(query, params + [effective_limit]).fetchall()
        else:
            rows = conn.execute(query, params).fetchall()

    entries = []
    if projected_fields:
        # Custom projection: return raw column values keyed by name.
        for row in rows:
            entry = dict(zip(cols_to_fetch, row))
            entries.append(entry)
    else:
        # Default compact triage shape (matches legacy output).
        # The `claim` output key sources from recall_summary when populated
        # (curated dense recognition string, up to 200 chars) or falls back
        # to the original claim truncated to 130 chars for legacy nodes that
        # haven't been backfilled yet. Key name unchanged for downstream compat.
        for row in rows:
            nid, ntype, claim, st, conf, recalls, imp, created, mem_st, rs = row
            if rs and len(rs) <= 200:
                # recall_summary present and within safety bound — use as-is
                # (validator hard cap is 200 chars (see RECALL_SUMMARY_HARD_CAP);
                # the 200 bound here matches that cap exactly, defense-in-depth
                # for hypothetical direct-SQL bypasses).
                claim_display = rs
            elif rs:
                # recall_summary present but unusually long (data integrity issue
                # upstream, defense-in-depth) — truncate to cap with ellipsis
                claim_display = rs[:200] + "..."
            else:
                # No recall_summary — fall back to truncated claim (legacy behavior)
                claim_display = (claim[:130] + "...") if claim and len(claim) > 130 else (claim or "")
            entry = {
                "id": nid,
                "type": ntype,
                "claim": claim_display,
                "status": st,
                "recall_count": recalls,
                "created": created[:10] if created else None,
            }
            if conf is not None:
                entry["confidence"] = round(conf, 3)
            entries.append(entry)

    truncated_count = (total - len(entries)) if total > len(entries) else 0

    response = {
        "status": "ok",
        "total_matched": total,
        # total_matching kept as alias for backward compat with any callers
        # that read the legacy field name.
        "total_matching": total,
        "shown": len(entries),
        "truncated": truncated_count,
        "sort_by": sort_by,
        "mode": "structured" if structured_mode else "single_field",
        "nodes": entries,
    }
    # Legacy single-field mode also reports the resolved filters in the old
    # shape for backward compat with any callers that read that field.
    if not structured_mode:
        response["filters"] = {"type": node_type or "all", "status": status or "all"}
    return json.dumps(core._strip_agent_facing(response))


def _query_pattern_impl(
    pattern_name: str,
    preset: str = "balanced",
    cosine_threshold_override: float = -1.0,
    top_k_override: int = -1,
    min_confidence_override: float = -1.0,
    summary_top_k: int = 3,
) -> str:
    """Impl for engram_query_pattern — callable with named kwargs for in-server callers."""
    if pattern_name not in PATTERN_QUERY_REGISTRY:
        return json.dumps({
            "error": f"Unknown pattern_name '{pattern_name}'. "
                     f"Registered: {sorted(PATTERN_QUERY_REGISTRY.keys())}.",
        })
    if preset not in PATTERN_QUERY_PRESETS:
        return json.dumps({
            "error": f"Unknown preset '{preset}'. "
                     f"Available: {sorted(PATTERN_QUERY_PRESETS.keys())}.",
        })

    params = dict(PATTERN_QUERY_PRESETS[preset])
    if cosine_threshold_override >= 0:
        params["cosine_threshold"] = cosine_threshold_override
    if top_k_override >= 0:
        params["top_k"] = top_k_override
    if min_confidence_override >= 0:
        params["min_confidence"] = min_confidence_override

    conn = core._get_db()
    try:
        impl = PATTERN_QUERY_REGISTRY[pattern_name]
        candidates = impl(conn, params)
    finally:
        conn.close()

    # Telemetry log: internal eval surface — preserved verbatim (the KnowQL-inspired design discipline).
    # The tier transformation applies to the agent-facing return shape only.
    _pattern_telemetry_log(pattern_name, preset,
                            {"params_used": params}, candidates)

    # --- engram.tool.engram_call event (DESIGN.md §4.2) ---
    # engram_query_pattern has no free-text query; query_text_len is the
    # pattern_name length as a proxy. source_class_filter and type_filter
    # are not parameters of this tool — emitted as None. See phase-3 F1
    # handoff ambiguous-decisions log.
    emit_if_initialized(
        event_type="engram.tool.engram_call",
        level=1,
        data={
            "tool_name": "engram_query_pattern",
            "query_text_len": len(pattern_name),
            "result_count": len(candidates),
            "type_filter": None,
            "source_class_filter": None,
            "pattern_name": pattern_name,
            "preset": preset,
        },
    )

    # Apply tiered render after pattern-internal ranking is complete.
    # Clamp summary_top_k to [0, len(candidates)].
    effective_top_k = max(0, min(summary_top_k, len(candidates)))
    tiered = _build_tiered_results(candidates, effective_top_k)

    return json.dumps(core._strip_agent_facing({
        "pattern_name": pattern_name,
        "preset": preset,
        "results": tiered,
        "total_matches": len(candidates),
    }))
