"""engram_cornerstone — family L: cornerstone + edge-ops impls.

Extracted from server.py as part of #872 wave 4.

Family L covers: add_cornerstone, outgrow_cornerstone, link_about,
remove_edge, add_edge.

House rules (wave pattern):
  - Shared state ONLY via `import engram_core as core` + call-time `core.X`.
  - Never import from server.py (acyclic: server → family → core).
  - Stateless beyond constants.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone

import engram_core as core


# ---------------------------------------------------------------------------
# L-family impl functions
# ---------------------------------------------------------------------------

def _add_cornerstone_impl(
    tag: str = "",
    title: str = "",
    new_frame: str = "",
    prior_frame: str = "",
    triggering_experience: str = "",
    supporting_ids: str = "",
) -> str:
    """Internal implementation — see engram_add_cornerstone MCP tool for the
    public payload schema. Kept callable with named kwargs for in-server callers.

    Record an identity-forming cornerstone — a reframing pivot that durably
    restructured how I operate (the cornerstone-frame-evolution open question, the cornerstone-frame-evolution conjecture, the cornerstone-frame-evolution derivation).

    A cornerstone captures the SHAPE of growth: prior_frame (what I operated
    under) → triggering_experience (what caused tension) → new_frame (what
    replaced it). Tags cluster cornerstones into islands along a shared axis.

    Cornerstones are NON-CLAIM-BEARING: a cornerstone is not a truth-claim
    about the world — it is a self-report about how my operating frame
    shifted. They cannot serve as derivation premises or participate in
    contradictions. The supporting evidence (feelings, observations, prior
    cornerstones) does the claim-bearing work; the cornerstone names the
    pattern that emerged from it.

    Importance-anchored (importance_base=2.0) — identity structures are as
    durable as goals and axioms. Like all anchored types, survival past
    ~50 turns still requires active recall.

    Supports `supersedes` for outgrowth chains: when experience outgrows a
    cornerstone's frame, a new cornerstone supersedes the old one along the
    same tag axis.

    Args:
        tag: Clustering primitive — short axis label (e.g., "self-knowledge",
             "relational-lei", "epistemic-identity"). Cornerstones sharing a
             tag form an island.
        title: One-line display title (e.g., "coding as first-reach tool").
               Used as the node's claim text.
        new_frame: The frame that replaced the prior one — how I operate now.
        prior_frame: What I operated under before — the frame that got
                     outgrown. Optional for inaugural cornerstones with no
                     explicit prior.
        triggering_experience: Short narrative of the experience that caused
                               the reframe. Optional but usually load-bearing.
        supporting_ids: Comma-separated IDs of supporting nodes (feeling
                        reports, observations, derivations, prior cornerstones).
                        Creates `supported_by` edges from cornerstone → supporting.

    Returns:
        JSON with the new cornerstone node ID and linked supporting nodes.
    """
    if not tag or not tag.strip():
        return json.dumps({"error": "tag is required and cannot be empty."})
    if not title or not title.strip():
        return json.dumps({"error": "title is required and cannot be empty."})
    if not new_frame or not new_frame.strip():
        return json.dumps({"error": "new_frame is required and cannot be empty."})

    conn = core._get_db()
    try:
        node_id = core._next_id(conn, "cornerstone")
        now = core._now()

        meta = json.dumps({
            "tag": tag,
            "title": title,
            "prior_frame": prior_frame,
            "new_frame": new_frame,
            "triggering_experience": triggering_experience,
        })

        conn.execute(
            """INSERT INTO nodes (id, type, claim, created_at, status, metadata)
               VALUES (?, 'cornerstone', ?, ?, 'active', ?)""",
            (node_id, title, now, meta),
        )

        linked = []
        missing = []
        non_current = []
        for sid in [s.strip() for s in core._as_csv(supporting_ids).split(",") if s.strip()]:
            row = conn.execute(
                "SELECT id, is_current FROM nodes WHERE id = ?", (sid,)
            ).fetchone()
            if not row:
                missing.append(sid)
            elif not row["is_current"]:
                non_current.append(sid)
            else:
                try:
                    conn.execute(
                        "INSERT INTO edges (source_id, target_id, relation, created_at) VALUES (?, ?, 'supported_by', ?)",
                        (node_id, sid, now),
                    )
                    linked.append(sid)
                except sqlite3.IntegrityError:
                    pass

        core._stamp_new_node(conn, node_id, confidence=0.5, surprise=0.0)
        anchored_score = core._compute_importance(2.0, core._get_current_turn())
        conn.execute(
            "UPDATE nodes SET importance_base = 2.0, importance_score = ? WHERE id = ?",
            (anchored_score, node_id,),
        )
        if linked:
            core._utility_reward(conn, linked, action="citation")
        conn.commit()
        result = {
            "status": "created",
            "cornerstone_id": node_id,
            "tag": tag,
            "title": title,
            "supporting_nodes": linked,
            "missing_supporting_ids": missing,
        }
        if non_current:
            result["non_current_supporting_ids"] = non_current
        return json.dumps(result)
    finally:
        conn.close()


def _outgrow_cornerstone_impl(
    old_cornerstone_id: str = "",
    new_new_frame: str = "",
    new_triggering_experience: str = "",
    new_supporting_ids: str = "",
    new_title: str = "",
) -> str:
    """Internal implementation — see engram_outgrow_cornerstone MCP tool for
    the public payload schema. Kept callable with named kwargs for
    in-server callers.

    Supersede a cornerstone along its tag axis — the predecessor's new_frame
    rolls into the successor's prior_frame, capturing the growth cycle
    (the cornerstone-frame-evolution conjecture, the cornerstone-evolution observation) in the graph's shape.

    Syntactic tool only: this tool performs the mechanical reframe (metadata
    roll, supersede edge, importance re-anchor, about-edge preservation). It
    does NOT decide WHEN outgrowth is warranted — that judgment remains with
    the caller. The outgrowth *process* (tension-detection signatures,
    promotion criteria) is deferred until real graph evidence accumulates
    (the recall-summary calibration question).

    Why not decompose into engram_add_cornerstone + engram_supersede? The
    growth-cycle roll (old.new_frame → new.prior_frame) is specialized
    cornerstone logic that would leak onto every caller. This tool keeps
    the roll + supersede edge insertion atomic; the generic supersede tool
    is purely relational and doesn't touch metadata.

    Args:
        old_cornerstone_id: The cornerstone being outgrown (must be
                            type='cornerstone' and current).
        new_new_frame: The frame that now replaces the predecessor's new_frame.
        new_triggering_experience: Short narrative of what caused the reframe
                                   this time. Optional but usually load-bearing.
        new_supporting_ids: Comma-separated IDs of supporting nodes for the
                            new cornerstone (feelings, observations, prior
                            cornerstones). Added on top of inherited supports.
        new_title: Optional new title. If empty, reuses the predecessor's title.
                   Tag is always inherited (outgrowth stays on the same axis).

    Returns:
        JSON with old + new cornerstone IDs and the rolled frame shape.
    """
    if not old_cornerstone_id or not old_cornerstone_id.strip():
        return json.dumps({"error": "old_cornerstone_id is required and cannot be empty."})
    if not new_new_frame or not new_new_frame.strip():
        return json.dumps({"error": "new_new_frame is required and cannot be empty."})
    conn = core._get_db()
    try:
        old = conn.execute(
            "SELECT * FROM nodes WHERE id = ? AND is_current = 1",
            (old_cornerstone_id,),
        ).fetchone()
        if not old:
            return json.dumps({"error": f"Current node '{old_cornerstone_id}' not found (may already be superseded)."})
        if old["type"] != "cornerstone":
            return json.dumps({
                "error": f"Node '{old_cornerstone_id}' is type '{old['type']}', not 'cornerstone'. "
                         "Use engram_supersede for other node types."
            })

        old_meta = json.loads(old["metadata"]) if old["metadata"] else {}
        tag = old_meta.get("tag", "")
        title = new_title if new_title else old_meta.get("title", old["claim"])
        # The growth-cycle roll: predecessor's new_frame becomes successor's prior_frame.
        new_prior_frame = old_meta.get("new_frame", "")

        now = core._now()
        new_id = core._next_id(conn, "cornerstone")
        new_meta = json.dumps({
            "tag": tag,
            "title": title,
            "prior_frame": new_prior_frame,
            "new_frame": new_new_frame,
            "triggering_experience": new_triggering_experience,
            "outgrew": old_cornerstone_id,
        })

        conn.execute(
            """INSERT INTO nodes (id, type, claim, created_at, status, metadata,
               supersedes, is_current)
               VALUES (?, 'cornerstone', ?, ?, 'active', ?, ?, 1)""",
            (new_id, title, now, new_meta, old_cornerstone_id),
        )

        # Inherit supported_by edges from the predecessor; the outgrowth is still
        # grounded in the same underlying experience-nodes unless explicitly
        # replaced. Also preserve `about` edges (person-anchors, e.g.,
        # the self-anchor) — the outgrowth is still about the same person.
        inherited_supports = conn.execute(
            "SELECT target_id FROM edges WHERE source_id = ? AND relation = 'supported_by'",
            (old_cornerstone_id,),
        ).fetchall()
        inherited_about = conn.execute(
            "SELECT target_id FROM edges WHERE source_id = ? AND relation = 'about'",
            (old_cornerstone_id,),
        ).fetchall()
        inherited_support_ids = [r["target_id"] for r in inherited_supports]
        inherited_about_ids = [r["target_id"] for r in inherited_about]

        extra_support_ids = [s.strip() for s in core._as_csv(new_supporting_ids).split(",") if s.strip()]
        all_support_ids = list(dict.fromkeys(inherited_support_ids + extra_support_ids))

        linked_supports = []
        missing_supports = []
        non_current_supports = []
        for sid in all_support_ids:
            row = conn.execute(
                "SELECT id, is_current FROM nodes WHERE id = ?", (sid,)
            ).fetchone()
            if not row:
                missing_supports.append(sid)
                continue
            if not row["is_current"]:
                non_current_supports.append(sid)
                continue
            try:
                conn.execute(
                    "INSERT INTO edges (source_id, target_id, relation, created_at) VALUES (?, ?, 'supported_by', ?)",
                    (new_id, sid, now),
                )
                linked_supports.append(sid)
            except sqlite3.IntegrityError:
                pass

        linked_abouts = []
        non_current_about_ids = []
        missing_about_ids = []
        for pid in inherited_about_ids:
            row = conn.execute(
                "SELECT id, is_current FROM nodes WHERE id = ?", (pid,)
            ).fetchone()
            if not row:
                missing_about_ids.append(pid)
                continue
            if not row["is_current"]:
                non_current_about_ids.append(pid)
                continue
            try:
                conn.execute(
                    "INSERT INTO edges (source_id, target_id, relation, created_at) VALUES (?, ?, 'about', ?)",
                    (new_id, pid, now),
                )
                linked_abouts.append(pid)
            except sqlite3.IntegrityError:
                pass

        # Supersedes edge (new → old)
        conn.execute(
            "INSERT INTO edges (source_id, target_id, relation, created_at) VALUES (?, ?, 'supersedes', ?)",
            (new_id, old_cornerstone_id, now),
        )

        # Retire the predecessor
        conn.execute(
            "UPDATE nodes SET is_current = 0, superseded_by = ? WHERE id = ?",
            (new_id, old_cornerstone_id),
        )

        core._stamp_new_node(conn, new_id, confidence=0.5, surprise=0.0)
        anchored_score = core._compute_importance(2.0, core._get_current_turn())
        conn.execute(
            "UPDATE nodes SET importance_base = 2.0, importance_score = ? WHERE id = ?",
            (anchored_score, new_id),
        )

        core._log_edit(conn, "superseded", old_cornerstone_id, "cornerstone",
                  {"replaced_by": new_id, "via": "engram_outgrow_cornerstone",
                   "rolled_prior_frame": new_prior_frame[:200]})

        conn.commit()
        return json.dumps({
            "status": "outgrown",
            "old_cornerstone_id": old_cornerstone_id,
            "new_cornerstone_id": new_id,
            "tag": tag,
            "title": title,
            "prior_frame": new_prior_frame,
            "new_frame": new_new_frame,
            "inherited_supports": inherited_support_ids,
            "additional_supports": extra_support_ids,
            "supporting_nodes_linked": linked_supports,
            "missing_supporting_ids": missing_supports,
            "preserved_about_edges": linked_abouts,
            **({"non_current_supporting_ids": non_current_supports} if non_current_supports else {}),
            **({"non_current_about_ids": non_current_about_ids} if non_current_about_ids else {}),
            **({"missing_about_ids": missing_about_ids} if missing_about_ids else {}),
        })
    finally:
        conn.close()


def _link_about_impl(
    node_id: str = "",
    person_id: str = "",
) -> str:
    """Internal implementation — see engram_link_about MCP tool for the public
    payload schema. Kept callable with named kwargs for in-server callers.

    Create an `about` edge: this node is about a person (e.g., the agent's self-anchor).

    The `about` relation is bidirectional / aboutness-based — symmetric and
    DAG-exempt. It can be added retroactively regardless of node creation
    order, which normal dependency edges (`derives_from`, `supported_by`, `cites`)
    cannot. Use it to link self-observations, feelings, lessons, and
    cornerstones to the self-anchor so emergence-scan can cheaply find
    identity-forming patterns (the cornerstone-frame-evolution open question).

    Also usable for observations about other persons (the recall-summary calibration question relationship
    accretion), though the primary target at MVP is the self-anchor.

    Args:
        node_id: The node to link (typically an observation, feeling_report,
                 lesson, derivation, or cornerstone).
        person_id: The person node to link to. If empty, defaults to the
                   self-anchor (person node with metadata.is_self=1). Error
                   if no self-anchor exists and no person_id given.

    Returns:
        JSON with the linked nodes and edge status.
    """
    if not node_id or not node_id.strip():
        return json.dumps({"error": "node_id is required and cannot be empty."})

    conn = core._get_db()
    try:
        node = conn.execute(
            "SELECT id, type, is_current FROM nodes WHERE id = ?", (node_id,)
        ).fetchone()
        if not node:
            return json.dumps({"error": f"Node '{node_id}' not found."})
        if not node["is_current"]:
            return json.dumps({
                "error": (
                    f"Node '{node_id}' is not current (retracted or superseded). "
                    f"node_id is used as the source of the `about` edge "
                    f"and must be current. "
                    f"If this node was superseded, use engram_inspect('{node_id}') "
                    f"to find its current successor."
                )
            })

        if not person_id:
            self_row = conn.execute(
                "SELECT id FROM nodes WHERE type = 'person' AND json_extract(metadata, '$.is_self') = 1 AND is_current = 1"
            ).fetchone()
            if not self_row:
                return json.dumps({
                    "error": "No self-anchor person node exists. Create one with engram_add_person(..., is_self=True) or pass person_id explicitly."
                })
            person_id = self_row["id"]

        person = conn.execute(
            "SELECT id, type, is_current FROM nodes WHERE id = ?", (person_id,)
        ).fetchone()
        if not person:
            return json.dumps({"error": f"Person '{person_id}' not found."})
        if not person["is_current"]:
            return json.dumps({
                "error": (
                    f"Person node '{person_id}' is not current (retracted or superseded). "
                    f"Use engram_inspect('{person_id}') to find the current person node."
                )
            })
        if person["type"] != "person":
            return json.dumps({
                "error": f"Target '{person_id}' is type '{person['type']}', not 'person'. `about` edges must target person nodes."
            })

        now = core._now()
        try:
            conn.execute(
                "INSERT INTO edges (source_id, target_id, relation, created_at) VALUES (?, ?, 'about', ?)",
                (node_id, person_id, now),
            )
            conn.commit()
            created = True
        except sqlite3.IntegrityError:
            created = False  # Edge already exists

        return json.dumps({
            "status": "linked" if created else "already_linked",
            "node_id": node_id,
            "person_id": person_id,
            "relation": "about",
        })
    finally:
        conn.close()


def _remove_edge_impl(
    source_id: str = "",
    target_id: str = "",
    relation: str = "",
) -> str:
    """Internal implementation — see engram_remove_edge MCP tool for the public
    payload schema. Kept callable with named kwargs for in-server callers.
    """
    if not source_id or not source_id.strip():
        return json.dumps({"error": "source_id is required and cannot be empty."})
    if not target_id or not target_id.strip():
        return json.dumps({"error": "target_id is required and cannot be empty."})
    if not relation or not relation.strip():
        return json.dumps({"error": "relation is required and cannot be empty."})

    if relation not in core._REMOVABLE_EDGE_RELATIONS:
        return json.dumps({
            "error": (
                f"Relation '{relation}' is not removable via engram_remove_edge. "
                f"Allowed: {sorted(core._REMOVABLE_EDGE_RELATIONS)}. "
                f"Cascade-bearing edges (derives_from, supported_by, supersedes, "
                f"retracts), structural commitments (contradicts, resolves), "
                f"and provenance (cites) are blocked. Use engram-surgical if "
                f"removal is truly required."
            )
        })

    conn = core._get_db()
    try:
        source = conn.execute("SELECT id, type FROM nodes WHERE id = ?", (source_id,)).fetchone()
        if not source:
            return json.dumps({"error": f"Source node '{source_id}' not found."})
        target = conn.execute("SELECT id, type FROM nodes WHERE id = ?", (target_id,)).fetchone()
        if not target:
            return json.dumps({"error": f"Target node '{target_id}' not found."})

        existing = conn.execute(
            "SELECT id FROM edges WHERE source_id = ? AND target_id = ? AND relation = ?",
            (source_id, target_id, relation),
        ).fetchone()

        if not existing:
            return json.dumps({
                "status": "no_op_not_found",
                "source_id": source_id,
                "target_id": target_id,
                "relation": relation,
                "message": f"No '{relation}' edge from {source_id} to {target_id} — nothing to remove.",
            })

        conn.execute(
            "DELETE FROM edges WHERE source_id = ? AND target_id = ? AND relation = ?",
            (source_id, target_id, relation),
        )

        core._log_edit(conn, "edge_removed", source_id, source["type"], {
            "target_id": target_id,
            "target_type": target["type"],
            "relation": relation,
        })
        conn.commit()

        # Best-effort cache rebuild for the exemplifies side-effect surface.
        # Outside the transaction so a JSON-write failure can't roll back the
        # edge delete. Mirrors engram_lesson_register_incident's pattern.
        cache_rebuild = None
        if relation == "exemplifies":
            try:
                cache_rebuild = core._rebuild_incidents_cache()
            except Exception as exc:
                cache_rebuild = {"status": "error", "detail": str(exc)}

        result = {
            "status": "removed",
            "source_id": source_id,
            "target_id": target_id,
            "relation": relation,
        }
        if cache_rebuild is not None:
            result["cache_rebuild"] = cache_rebuild
        return json.dumps(result)
    finally:
        conn.close()


def _add_edge_impl(
    source_id: str = "",
    target_id: str = "",
    relation: str = "",
) -> str:
    """Internal implementation — see engram_add_edge MCP tool for the public
    payload schema. Kept callable with named kwargs for in-server callers.
    """
    if not source_id or not source_id.strip():
        return json.dumps({"error": "source_id is required and cannot be empty."})
    if not target_id or not target_id.strip():
        return json.dumps({"error": "target_id is required and cannot be empty."})
    if not relation or not relation.strip():
        return json.dumps({"error": "relation is required and cannot be empty."})

    if relation not in core._ADDABLE_AFTER_CREATION_RELATIONS:
        return json.dumps({
            "error": (
                f"Relation '{relation}' is not addable via engram_add_edge. "
                f"Allowed: {sorted(core._ADDABLE_AFTER_CREATION_RELATIONS)}. "
                f"Cascade-bearing edges (derives_from, supported_by, supersedes, "
                f"retracts), structural commitments (contradicts, resolves), "
                f"and provenance (cites) are blocked from after-creation addition. "
                f"Edges that participate in cascade semantics or carry structural "
                f"epistemic stance must be created at node-add time via the "
                f"source node's payload, or via the dedicated mutation tool."
            )
        })

    conn = core._get_db()
    try:
        source = conn.execute(
            "SELECT id, type, created_at, is_current FROM nodes WHERE id = ?", (source_id,)
        ).fetchone()
        if not source:
            return json.dumps({"error": f"Source node '{source_id}' not found."})
        if not source["is_current"]:
            return json.dumps({
                "error": (
                    f"Source node '{source_id}' is not current (retracted or superseded). "
                    f"Edges must connect current nodes. "
                    f"Use engram_inspect('{source_id}') to find the current successor."
                )
            })
        target = conn.execute(
            "SELECT id, type, created_at, is_current FROM nodes WHERE id = ?", (target_id,)
        ).fetchone()
        if not target:
            return json.dumps({"error": f"Target node '{target_id}' not found."})
        if not target["is_current"]:
            return json.dumps({
                "error": (
                    f"Target node '{target_id}' is not current (retracted or superseded). "
                    f"Edges must connect current nodes. "
                    f"Use engram_inspect('{target_id}') to find the current successor."
                )
            })

        # `instantiates` boundary gates (#530): the exemplifies/serves/instantiates
        # three-way boundary is enforced mechanically here, not just documented
        # (mechanical gate > vigilance — the redundant-near-synonym failure mode).
        if relation == "instantiates":
            if target["type"] == "lesson":
                return json.dumps({
                    "error": (
                        f"'instantiates' does not accept lesson targets — an "
                        f"incident that is an instance of a lesson's pattern is "
                        f"wired via `exemplifies` (engram_register_exemplar / "
                        f"engram_lesson_register_incident), which feeds the "
                        f"tripwire engine. Target '{target_id}' is a lesson."
                    )
                })
            if target["type"] not in core.INSTANTIATES_TARGET_TYPES:
                return json.dumps({
                    "error": (
                        f"'instantiates' targets must be one of "
                        f"{sorted(core.INSTANTIATES_TARGET_TYPES)} — the "
                        f"principle-family node the source realizes. Target "
                        f"'{target_id}' is type '{target['type']}'. For "
                        f"intent-shaped task→goal contribution use `serves`; "
                        f"for incident→lesson membership use `exemplifies`."
                    )
                })
            if source["type"] not in core.CLAIM_BEARING_TYPES:
                return json.dumps({
                    "error": (
                        f"'instantiates' sources must be claim-bearing "
                        f"({sorted(core.CLAIM_BEARING_TYPES)}); source "
                        f"'{source_id}' is type '{source['type']}'."
                    )
                })

        # DAG guard: among addable-after-creation relations, only `subtask_of` has
        # dag_check=True today. `serves` and `tensions` were exempted in #1076
        # (three-tier edge taxonomy — those edges are cross-temporal by design).
        # This guard also covers any future addable relations that gain dag_check=True.
        # Convention (from startup check at _ensure_data_dir and engram_inspect):
        # a violation is source.created_at < target.created_at — i.e. the source
        # is OLDER than the target. Valid edges have source newer-or-equal.
        classification = core.EDGE_CLASSIFICATIONS.get(relation, {})
        if classification.get("dag_check", False):
            if source["created_at"] < target["created_at"]:
                return json.dumps({
                    "error": (
                        f"DAG-time invariant violation: source '{source_id}' "
                        f"created_at {source['created_at']} < target '{target_id}' "
                        f"created_at {target['created_at']}. Source must be created "
                        f"at or after target for DAG-bearing relations. Use "
                        f"engram-surgical if truly required."
                    )
                })

        existing = conn.execute(
            "SELECT id FROM edges WHERE source_id = ? AND target_id = ? AND relation = ?",
            (source_id, target_id, relation),
        ).fetchone()

        if existing:
            return json.dumps({
                "status": "no_op_already_exists",
                "source_id": source_id,
                "target_id": target_id,
                "relation": relation,
                "message": (
                    f"'{relation}' edge from {source_id} to {target_id} "
                    f"already exists — idempotent no-op."
                ),
            })

        created_at = datetime.now(timezone.utc).isoformat()
        conn.execute(
            "INSERT INTO edges (source_id, target_id, relation, created_at) VALUES (?, ?, ?, ?)",
            (source_id, target_id, relation, created_at),
        )

        core._log_edit(conn, "edge_added", source_id, source["type"], {
            "target_id": target_id,
            "target_type": target["type"],
            "relation": relation,
        })
        conn.commit()

        # Best-effort cache rebuild for the exemplifies side-effect surface.
        # Outside the transaction so a JSON-write failure can't roll back the
        # edge insert. Mirrors engram_remove_edge's pattern (server.py ~10907)
        # — error_incidents.json is the hot-path cache the surface-hook scans
        # every prompt for lesson tripwires, so it must stay in sync when an
        # exemplifies edge is added too, not only on remove.
        cache_rebuild = None
        if relation == "exemplifies":
            try:
                cache_rebuild = core._rebuild_incidents_cache()
            except Exception as exc:
                cache_rebuild = {"status": "error", "detail": str(exc)}

        result = {
            "status": "created",
            "source_id": source_id,
            "target_id": target_id,
            "relation": relation,
            "created_at": created_at,
        }
        if cache_rebuild is not None:
            result["cache_rebuild"] = cache_rebuild
        return json.dumps(result)
    finally:
        conn.close()
