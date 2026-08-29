"""engram_trust — trust/person (family F) impls for the ENGRAM MCP server.

Extracted from server.py in #872 wave 3.

HOUSE RULES (mirror engram_core.py § HOUSE RULES):
- Access shared state ONLY via ``import engram_core as core; core.NAME`` — never
  via ``from engram_core import NAME``.
- This module must not import server.py (acyclic: server → family → core).
- No module-level mutable assignments — all state lives in engram_core.
"""

import json
import sqlite3

import engram_core as core


# ---------------------------------------------------------------------------
# Trust-tier constants
# ---------------------------------------------------------------------------
#
# Ordered from most-external (0) to most-internal (6). Tier changes TO any
# tier with rank >= INTERNAL_THRESHOLD require primary-user approval, EXCEPT
# 'self' (rank 6) which is gated by metadata.is_self=true instead.
# 'primary_user' (rank 5) inherits the standard approval gate; multiple pn_*
# nodes may simultaneously hold this tier (team-serving case).
# 'self' is singleton-enforced at set-time (mirrors the is_self invariant).
TIER_RANK = {
    "self":           6,
    "primary_user":   5,
    "user_family":    4,
    "our_side":       3,
    "known_external": 2,
    "unknown":        1,
    "suspect":        0,
}
INTERNAL_THRESHOLD = 3  # our_side and above


# ---------------------------------------------------------------------------
# Person impl
# ---------------------------------------------------------------------------

def _add_person_impl(
    name: str = "",
    # Typed relational fields (shared human + agent)
    birthday: str = "",
    relation: str = "",
    pronouns: str = "",
    trust_tier: str = "",
    aliases=None,
    location_contact: str = "",
    # Agent-specific identity fields (immutable)
    lineage: str = "",
    architecture: str = "",
    first_session_date: str = "",
    # Compat / structural flags
    context_ids: str = "",
    is_self: bool = False,
    # Deprecated: role is accepted as an alias for relation.
    # Filed under 'relation' in metadata; the old 'role' key is not written.
    role: str = "",
) -> str:
    """Internal implementation — see engram_add_person MCP tool for the public
    payload schema. Kept callable with named kwargs for in-server callers.

    Record a person the agent knows and interacts with.

    Person nodes represent people in the agent's relational world — collaborators,
    evaluators, stakeholders. They are the relational layer of ENGRAM, lighter-schema
    than epistemic nodes (the recall-summary derivation): no confidence scores, no
    evidence requirements, no claim-bearing participation.

    Person nodes are NON-CLAIM-BEARING: a person is not a truth-claim about the world.
    They cannot serve as derivation premises. However, observations and derivations
    can reference person nodes via context_ids to record relational context.

    Importance-anchored (importance_base=2.0) — people the agent knows are as durable
    as goals and axioms. Like all anchored types, survival past ~50 turns still
    requires active recall.

    Typed fields replace the old free-form description/logical_chain bins. Facts
    about a person should be stored as user_stated observations linked to the
    person node via engram_link_about. The person node captures WHO + structured
    identity facts; observations capture WHAT IS KNOWN ABOUT THEM.

    Mutable-with-trace fields (relation, pronouns, trust_tier, aliases,
    location_contact) record prior values in metadata.field_traces when changed
    via engram_update_person. Immutable fields (name, birthday, lineage,
    architecture, first_session_date) may only be corrected via supersede.

    Args:
        name: The person's name (required). Immutable — legal corrections
              require supersede with #1587 guard.
        birthday: ISO date string (e.g. "1990-06-15"). Immutable.
        relation: Their role/relationship to the agent (e.g. "primary collaborator",
                  "daughter", "colleague"). Mutable-with-trace.
                  DEPRECATED ALIAS: 'role' is accepted; stored as 'relation'.
        pronouns: Free-form pronoun string (e.g. "she/her"). Mutable-with-trace.
        trust_tier: Trust tier string (same values as nodes.trust_tier column:
                    "self", "primary_user", "user_family", "our_side",
                    "known_external", "unknown", "suspect"). Mutable-with-trace
                    with elevated review flag on change via engram_update_person.
                    If provided here, sets both metadata.trust_tier and the
                    nodes.trust_tier column.
        aliases: List of alternative names/identifiers for this person used by
                 the Stage 1 auto-suggest pipeline (e.g. ["Wei", "王伟"]).
                 Also accepted as a comma-separated string for backward compat.
                 Mutable-with-trace.
        location_contact: Address or contact info. Mutable-with-trace.
        lineage: Agent lineage string (e.g. "anthropic:claude-sonnet"). Immutable.
        architecture: Agent architecture descriptor. Immutable.
        first_session_date: ISO date of first interaction. Immutable.
        context_ids: Optional comma-separated node IDs. Creates cites edges
                     (e.g. link to goals they serve).
        is_self: Mark this person node as the agent's own self-anchor.
                 Only one self-node should exist; subsequent attempts are rejected.
        role: DEPRECATED — accepted as alias for relation; prefer relation.

    Returns:
        JSON with the new person node ID.
    """
    if not name or not name.strip():
        return json.dumps({"error": "name is required and cannot be empty."})

    # Resolve relation: prefer explicit 'relation', fall back to deprecated 'role'.
    resolved_relation = relation.strip() if relation and relation.strip() else (
        role.strip() if role and role.strip() else ""
    )

    conn = core._get_db()
    try:
        if is_self:
            existing = conn.execute(
                "SELECT id FROM nodes WHERE type = 'person' AND json_extract(metadata, '$.is_self') = 1 AND is_current = 1"
            ).fetchone()
            if existing:
                return json.dumps({
                    "error": f"Self-anchor person node already exists: {existing['id']}. Only one self-node is permitted.",
                    "existing_self_id": existing["id"],
                })
        node_id = core._next_id(conn, "person")
        now = core._now()

        # claim: human-readable summary from typed fields.
        claim_text = f"{name.strip()} — {resolved_relation}" if resolved_relation else name.strip()

        # Parse aliases — accept list or CSV string.
        if isinstance(aliases, list):
            alias_list = [str(a).strip() for a in aliases if str(a).strip()]
        elif aliases:
            alias_list = [s.strip() for s in core._as_csv(aliases).split(",") if s.strip()]
        else:
            alias_list = []

        # Build typed metadata — no free-form description/logical_chain bins.
        meta_dict = {
            "name": name.strip(),
            "aliases": alias_list,
            "special_moments": [],  # §3: initialized empty; see _SPECIAL_MOMENTS_CAP
            "field_traces": {},     # §2: mutable-field prior-value audit trail
        }
        if resolved_relation:
            meta_dict["relation"] = resolved_relation
        if pronouns and pronouns.strip():
            meta_dict["pronouns"] = pronouns.strip()
        if trust_tier and trust_tier.strip():
            if trust_tier.strip() not in TIER_RANK:
                return json.dumps({
                    "error": f"Invalid trust_tier '{trust_tier.strip()}'. "
                             f"Must be one of: {', '.join(sorted(TIER_RANK, key=lambda t: TIER_RANK[t], reverse=True))}"
                })
            meta_dict["trust_tier"] = trust_tier.strip()
        if location_contact and location_contact.strip():
            meta_dict["location_contact"] = location_contact.strip()
        if birthday and birthday.strip():
            meta_dict["birthday"] = birthday.strip()
        # Agent-specific immutable fields
        if lineage and lineage.strip():
            meta_dict["lineage"] = lineage.strip()
        if architecture and architecture.strip():
            meta_dict["architecture"] = architecture.strip()
        if first_session_date and first_session_date.strip():
            meta_dict["first_session_date"] = first_session_date.strip()
        if is_self:
            meta_dict["is_self"] = True

        # Resolve trust_tier column: use provided value or fall back to 'unknown'.
        tier_col = trust_tier.strip() if trust_tier and trust_tier.strip() else "unknown"

        core._refuse_ncb_confidence("person", None)  # #2252 Stage 1.3 write-path guard
        conn.execute(
            """INSERT INTO nodes (id, type, claim, created_at,
               status, metadata, trust_tier)
               VALUES (?, 'person', ?, ?, 'active', ?, ?)""",
            (node_id, claim_text, now, json.dumps(meta_dict), tier_col),
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
        # inflation. Pass importance_base directly so _stamp_new_node writes
        # the anchor in one pass instead of computing an interim value from
        # confidence/surprise and then overwriting it [alpha#2266, alpha#2268].
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
                "person_id": node_id,
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

        result = {
            "status": "created",
            "person_id": node_id,
            "name": name.strip(),
            "relation": resolved_relation,
            "aliases": alias_list,
            "context_nodes": context,
        }
        if role and not relation:
            result["deprecation_warning"] = (
                "'role' is deprecated — use 'relation' instead. "
                "Value accepted and stored as 'relation'."
            )
        return json.dumps(result)
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Person update impl (engram_update_person)
# ---------------------------------------------------------------------------

# Fields that may be changed in-place via engram_update_person.
_PERSON_MUTABLE_FIELDS = frozenset({
    "relation", "pronouns", "trust_tier", "aliases", "location_contact",
})
# Fields that are immutable — must be corrected via supersede.
_PERSON_IMMUTABLE_FIELDS = frozenset({
    "name", "birthday", "lineage", "architecture", "first_session_date",
})

# Max special_moments slots per person node (§3).
#
# #1948: raised from 7 to 100 and decoupled from display. The original 7 was
# a *render* budget enforced at the *write* layer — but #1940's rotating
# display shows one moment per surfacing regardless of how many are stored,
# so the write-side cap no longer buys anything for display purposes. The
# remaining real cost is metadata blob size: the whole JSON is parsed on
# every person-node read, and 100 entries x ~70 words is roughly 30-50KB per
# read — worth bounding, not worth bounding at 7. 100 is the issue's own
# suggested ceiling; nothing in the storage/read-cost analysis argues for a
# different nearby number.
#
# At the ceiling, _add_special_moment_impl fails LOUD (returns {"error":...},
# no DB write) rather than silently evicting the oldest entry — see #1948.
# The old pop(0)-on-cap behavior discarded hand-written relational text with
# no warning and (per #1948's own audit) frequently no about-edge either,
# since the "about-edge preserved" comment that used to justify silent
# eviction was empirically false for entries added without a prior
# engram_link_about call.
_SPECIAL_MOMENTS_CAP = 100


def _update_person_impl(
    node_id: str = "",
    updates: dict = None,
    reason: str = "",
) -> str:
    """Internal implementation — see engram_update_person MCP tool.

    Update mutable fields on an existing person node in-place (no supersede,
    no new node). Records prior values in metadata.field_traces (append-only).
    Emits a field-value-changed cascade flag to nodes citing this person.

    Args:
        node_id: Person node ID (pn_NNNN). Must be type='person', is_current=1.
        updates: Dict of field → new_value. Only mutable fields accepted:
                 relation, pronouns, trust_tier, aliases, location_contact.
                 Immutable-field updates are rejected with a clear error.
        reason: Optional human-readable explanation for the change.

    Returns:
        JSON with status, node_id, updated_fields, field_traces_delta.
    """
    if updates is None:
        updates = {}
    if not node_id or not node_id.strip():
        return json.dumps({"error": "node_id is required and cannot be empty."})
    if not updates:
        return json.dumps({"error": "updates must be a non-empty dict of field → new_value."})

    # Reject immutable fields early — give a clear error before any DB touch.
    immutable_attempted = set(updates.keys()) & _PERSON_IMMUTABLE_FIELDS
    if immutable_attempted:
        return json.dumps({
            "error": (
                f"Cannot update immutable field(s): {sorted(immutable_attempted)}. "
                f"Immutable fields (name, birthday, lineage, architecture, "
                f"first_session_date) may only be corrected via supersede "
                f"(engram_supersede) with the #1587 guard for name changes. "
                f"Mutable fields: {sorted(_PERSON_MUTABLE_FIELDS)}."
            )
        })
    unknown_fields = set(updates.keys()) - _PERSON_MUTABLE_FIELDS - _PERSON_IMMUTABLE_FIELDS
    if unknown_fields:
        return json.dumps({
            "error": (
                f"Unknown person field(s): {sorted(unknown_fields)}. "
                f"Mutable (updatable): {sorted(_PERSON_MUTABLE_FIELDS)}. "
                f"Immutable (supersede only): {sorted(_PERSON_IMMUTABLE_FIELDS)}."
            )
        })

    conn = core._get_db()
    try:
        row = conn.execute(
            "SELECT id, type, is_current, metadata, trust_tier FROM nodes WHERE id = ?",
            (node_id.strip(),),
        ).fetchone()
        if not row:
            return json.dumps({"error": f"Node '{node_id}' not found."})
        if row["type"] != "person":
            return json.dumps({
                "error": (
                    f"Node '{node_id}' is type '{row['type']}', not 'person'. "
                    f"engram_update_person only operates on person nodes."
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
        trust_tier_changed = False

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
            if field == "aliases":
                # Accept list or CSV string — normalize to list.
                if isinstance(new_value, list):
                    new_value = [str(a).strip() for a in new_value if str(a).strip()]
                elif isinstance(new_value, str):
                    new_value = [s.strip() for s in new_value.split(",") if s.strip()]
                meta["aliases"] = new_value
            elif field == "trust_tier":
                if str(new_value) not in TIER_RANK:
                    return json.dumps({
                        "error": f"Invalid trust_tier '{new_value}'. "
                                 f"Must be one of: {', '.join(sorted(TIER_RANK, key=lambda t: TIER_RANK[t], reverse=True))}"
                    })
                meta[field] = str(new_value)
            else:
                meta[field] = new_value

            updated_fields[field] = new_value

            if field == "trust_tier":
                trust_tier_changed = True

        # Persist metadata update (no new node, same node_id).
        update_sql = "UPDATE nodes SET metadata = ?"
        update_params: list = [json.dumps(meta)]

        # Keep nodes.trust_tier column in sync when trust_tier is updated.
        if trust_tier_changed:
            update_sql += ", trust_tier = ?"
            update_params.append(str(updates["trust_tier"]))

        update_sql += " WHERE id = ?"
        update_params.append(node_id.strip())
        conn.execute(update_sql, update_params)

        core._log_edit(conn, "person_field_updated", node_id.strip(), "person", {
            "updated_fields": updated_fields,
            "reason": reason or None,
        })

        # Emit field-value-changed cascade flags to all nodes that cite this person.
        # Coarse-emit: node-scoped — any node with a cites, about, or
        # derives_from edge to this person gets flagged. Carries field +
        # prior_value in each flag entry. trust_tier changes → elevated
        # (review_required=True, non-dismissible). All other changes → cheap
        # (dismissible on prior→new triage).
        # #2255 colleague-review fix: this three-relation set is deliberately
        # its own, distinct from core.CITATION_SUPPORT_RELATIONS
        # ({derives_from, supported_by}) — do not unify the two. This asks a
        # person-citation question ("what cites this person") that legitimately
        # includes 'about' and excludes 'supported_by'.
        flagged_nodes = []
        citing_rows = conn.execute(
            "SELECT DISTINCT source_id FROM edges "
            "WHERE target_id = ? AND relation IN ('cites', 'about', 'derives_from')",
            (node_id.strip(),),
        ).fetchall()
        for citing in citing_rows:
            src = citing["source_id"]
            src_row = conn.execute(
                "SELECT id, type, metadata FROM nodes WHERE id = ? AND is_current = 1",
                (src,),
            ).fetchone()
            if not src_row:
                continue
            src_meta = json.loads(src_row["metadata"] or "{}")
            if "field_changed_flags" not in src_meta:
                src_meta["field_changed_flags"] = []
            for field, new_value in updates.items():
                prior = field_traces_delta[field]["prior_value"]
                flag_entry = {
                    "person_id": node_id.strip(),
                    "field": field,
                    "prior_value": prior,
                    "new_value": new_value,
                    "changed_at": now,
                    "review_required": field == "trust_tier",
                    "dismissible": field != "trust_tier",
                }
                src_meta["field_changed_flags"].append(flag_entry)
            conn.execute(
                "UPDATE nodes SET metadata = ? WHERE id = ?",
                (json.dumps(src_meta), src),
            )
            flagged_nodes.append(src)

        conn.commit()

        return json.dumps({
            "status": "updated",
            "node_id": node_id.strip(),
            "updated_fields": updated_fields,
            "field_traces_delta": field_traces_delta,
            "cascade_flagged_nodes": flagged_nodes,
            "cascade_flag_type": "field-value-changed",
            "trust_tier_elevated": trust_tier_changed,
        })
    finally:
        conn.close()


def _add_special_moment_impl(
    person_id: str = "",
    description: str = "",
    node_id: str = "",
) -> str:
    """Add a special moment to a person node (§3 — called from link_about Stage 2).

    Appends {description, node_id, added_at} to metadata.special_moments.
    Cap is _SPECIAL_MOMENTS_CAP (100 as of #1948). At the cap, this returns
    an error and does NOT modify special_moments — no silent eviction. The
    caller must prune deliberately before retrying; see the error message
    for details (as of #1948, no removal tool exists yet — see #1948's own
    flagged gap).

    Validates that node_id references a real current node.

    Args:
        person_id: pn_NNNN to update.
        description: Qualitative description of the moment (agent's read).
        node_id: The observation or other node this moment is anchored to.

    Returns:
        JSON with status and the updated special_moments list. On cap
        failure, JSON with {"error": "..."} instead — special_moments is
        left untouched.
    """
    if not person_id or not person_id.strip():
        return json.dumps({"error": "person_id is required."})
    if not description or not description.strip():
        return json.dumps({"error": "description is required."})
    if not node_id or not node_id.strip():
        return json.dumps({"error": "node_id is required (epistemic anchor for the moment)."})

    conn = core._get_db()
    try:
        pn_row = conn.execute(
            "SELECT id, type, is_current, metadata FROM nodes WHERE id = ?",
            (person_id.strip(),),
        ).fetchone()
        if not pn_row:
            return json.dumps({"error": f"Person node '{person_id}' not found."})
        if pn_row["type"] != "person":
            return json.dumps({
                "error": f"Node '{person_id}' is type '{pn_row['type']}', not 'person'."
            })
        if not pn_row["is_current"]:
            return json.dumps({
                "error": f"Person node '{person_id}' is not current (retracted or superseded)."
            })

        # Validate the anchor node exists and is current.
        anchor = conn.execute(
            "SELECT id, is_current FROM nodes WHERE id = ?", (node_id.strip(),)
        ).fetchone()
        if not anchor:
            return json.dumps({
                "error": f"Anchor node '{node_id}' not found. node_id must reference a real node."
            })
        if not anchor["is_current"]:
            return json.dumps({
                "error": f"Anchor node '{node_id}' is not current (retracted or superseded). "
                         f"Use engram_inspect('{node_id}') to find the current successor."
            })

        meta = json.loads(pn_row["metadata"] or "{}")
        moments = meta.get("special_moments", [])

        # Hard ceiling — fail loud, no silent eviction (#1948). The prior
        # behavior (moments.pop(0) on overflow) discarded hand-written
        # relational text with no warning; this is the wrong failure mode
        # at any cap value. Return before touching `moments` or `meta` so
        # existing entries are guaranteed untouched on this path.
        if len(moments) >= _SPECIAL_MOMENTS_CAP:
            return json.dumps({
                "error": (
                    f"special_moments cap reached ({_SPECIAL_MOMENTS_CAP}) for "
                    f"person '{person_id.strip()}'. No moment was added — the "
                    f"existing {len(moments)} moments are unchanged (no silent "
                    f"eviction; see #1948). To free a slot, prune deliberately. "
                    f"NOTE: as of #1948, no engram tool exists to remove a "
                    f"special_moments entry — this is a known gap. In the "
                    f"meantime, review the person's special_moments (e.g. via "
                    f"engram_inspect(view='deep')) and use direct graph-repair "
                    f"access if a moment truly needs to be dropped."
                ),
                "person_id": person_id.strip(),
                "cap": _SPECIAL_MOMENTS_CAP,
                "slot_count": len(moments),
            })

        now = core._now()
        new_entry = {
            "description": description.strip(),
            "node_id": node_id.strip(),
            "added_at": now,
        }

        moments.append(new_entry)
        meta["special_moments"] = moments

        conn.execute(
            "UPDATE nodes SET metadata = ? WHERE id = ?",
            (json.dumps(meta), person_id.strip()),
        )
        core._log_edit(conn, "special_moment_added", person_id.strip(), "person", {
            "anchor_node_id": node_id.strip(),
        })
        conn.commit()

        return json.dumps({
            "status": "added",
            "person_id": person_id.strip(),
            "special_moments": moments,
            "slot_count": len(moments),
            "cap": _SPECIAL_MOMENTS_CAP,
        })
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# special_moments about-edge backfill (#1948 item 4)
# ---------------------------------------------------------------------------

def _backfill_special_moment_about_edges(conn: sqlite3.Connection) -> int:
    """One-time pass: create missing `about` edges for special_moments entries.

    Every metadata.special_moments[].node_id on every live (type='person',
    is_current=1) person node is supposed to have an `about` edge to its
    person — but the historical write path (_add_special_moment_impl) never
    guaranteed this. Entries added without a prior engram_link_about call
    (or whose about-edge creation otherwise never happened) are silently
    missing the edge. #1948's own audit found this true for 4 of 7 real
    moments on a live graph — the "about-edge preserved" comment that used
    to sit above the old rotation logic was empirically false for those.

    For each qualifying (node_id, person_id) pair, creates an `about` edge
    using the SAME insert SQL + idempotency mechanism as
    engram_cornerstone.py's _link_about_impl (INSERT INTO edges ... VALUES
    (?, ?, 'about', ?), guarded by both an explicit existence check and an
    IntegrityError catch against the edges table's own
    UNIQUE(source_id, target_id, relation) constraint) — the same
    edge-creation path, not a second parallel one.

    Skips moment entries whose node_id no longer exists in nodes at all
    (dangling reference — cannot create an edge with a nonexistent source,
    edges.source_id has a REFERENCES nodes(id) constraint). Deliberately
    does NOT require the anchor to be is_current: a moment's anchor may
    have been superseded since the moment was recorded, but the historical
    about-edge should still be backfilled to the node id that was actually
    cited at moment-creation time — is_current gating belongs to write-time
    validation (_add_special_moment_impl), not to this historical-repair
    pass.

    Idempotent: running this twice creates zero additional edges the second
    time (checked via existence-check + IntegrityError, either one alone
    would suffice; both together made the intent explicit for reviewers).

    Args:
        conn: An open sqlite3.Connection (caller manages open/close).

    Returns:
        The count of `about` edges newly created. Commits only if any edges
        were created (mirrors _backfill_resolved_by / _backfill_source_type
        in engram_core.py).
    """
    person_rows = conn.execute(
        "SELECT id, metadata FROM nodes WHERE type = 'person' AND is_current = 1"
    ).fetchall()
    if not person_rows:
        return 0

    now = core._now()
    created = 0
    for prow in person_rows:
        person_id = prow["id"]
        try:
            meta = json.loads(prow["metadata"] or "{}")
        except (json.JSONDecodeError, TypeError):
            continue
        moments = meta.get("special_moments") or []
        if not isinstance(moments, list):
            continue
        for moment in moments:
            if not isinstance(moment, dict):
                continue
            anchor_id = moment.get("node_id")
            if not anchor_id:
                continue

            anchor_exists = conn.execute(
                "SELECT 1 FROM nodes WHERE id = ?", (anchor_id,)
            ).fetchone()
            if not anchor_exists:
                continue  # dangling reference — cannot satisfy the FK

            already = conn.execute(
                "SELECT 1 FROM edges WHERE source_id = ? AND target_id = ? "
                "AND relation = 'about'",
                (anchor_id, person_id),
            ).fetchone()
            if already:
                continue

            try:
                conn.execute(
                    "INSERT INTO edges (source_id, target_id, relation, created_at) "
                    "VALUES (?, ?, 'about', ?)",
                    (anchor_id, person_id, now),
                )
                created += 1
            except sqlite3.IntegrityError:
                pass  # edge already exists (race / already-checked) — no-op

    if created:
        conn.commit()
    return created


# ---------------------------------------------------------------------------
# Person lineage impl
# ---------------------------------------------------------------------------

def _parse_model_id(model_id: str) -> dict:
    """Parse a model ID string into provider, family, and version.

    Handles Claude model IDs ("claude-{family}-{major}-{minor}[{date}]"):
      "claude-sonnet-4-6"      → provider=anthropic, family=claude-sonnet, version=4.6
      "claude-opus-4-8"        → provider=anthropic, family=claude-opus,   version=4.8
      "claude-haiku-4-5-20251001" → provider=anthropic, family=claude-haiku, version=4.5

    For unrecognised formats, provider and version are empty; family=model_id.

    NOTE: This parser targets the claude-{name}-{major}-{minor}[{date}] scheme.
    Anthropic also issues IDs of the form claude-3-5-sonnet-20241022, where digits
    appear inside the family name — those would produce wrong field assignments here.
    Only pass IDs that match the {family}-{major}-{minor} scheme.
    """
    mid = model_id.strip()
    if not mid.startswith("claude-"):
        return {"provider": "", "family": mid, "version": ""}

    provider = "anthropic"
    parts = mid.split("-")
    family_parts = []
    version_digits = []
    for part in parts:
        if part.isdigit():
            if len(part) <= 2:  # major/minor digit; skip date-style suffixes like "20251001"
                version_digits.append(part)
        else:
            family_parts.append(part)
    family = "-".join(family_parts)
    version = ".".join(version_digits)
    return {"provider": provider, "family": family, "version": version}


def _set_person_lineage_impl(target_pn: str = "", model_id: str = "") -> str:
    """Record model training lineage (provider/family/version) on a person node.

    Updates the person node's metadata to add model_provider, model_family,
    and model_version fields derived from the supplied model_id string.

    Args:
        target_pn: Person node ID (pn_NNNN) to update. Defaults to the
                   graph's self-anchor when empty.
        model_id:  Model identifier string, e.g. "claude-sonnet-4-6".
                   Parsed into provider + family + version.

    Returns:
        JSON with status, person_id, and the parsed lineage fields.
    """
    if not model_id or not model_id.strip():
        return json.dumps({"error": "model_id is required (e.g. 'claude-sonnet-4-6')."})

    parsed = _parse_model_id(model_id.strip())

    conn = core._get_db()
    try:
        if not target_pn or not target_pn.strip():
            row = conn.execute(
                "SELECT id, metadata FROM nodes "
                "WHERE type='person' AND json_extract(metadata,'$.is_self')=1 AND is_current=1"
            ).fetchone()
            if not row:
                return json.dumps({
                    "error": (
                        "No self-anchor person node found. "
                        "Create one with engram_add_person(is_self=True) first."
                    )
                })
        else:
            row = conn.execute(
                "SELECT id, metadata FROM nodes WHERE id=? AND type='person' AND is_current=1",
                (target_pn.strip(),),
            ).fetchone()
            if not row:
                return json.dumps({
                    "error": (
                        f"Person node '{target_pn}' not found or not type 'person'. "
                        "Use engram_add_person to create it first."
                    )
                })

        meta = json.loads(row["metadata"] or "{}")
        meta["model_provider"] = parsed["provider"]
        meta["model_family"] = parsed["family"]
        meta["model_version"] = parsed["version"]

        conn.execute(
            "UPDATE nodes SET metadata=? WHERE id=?",
            (json.dumps(meta), row["id"]),
        )
        conn.commit()

        return json.dumps({
            "status": "ok",
            "person_id": row["id"],
            "model_provider": parsed["provider"],
            "model_family": parsed["family"],
            "model_version": parsed["version"],
        })
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Trust-tier impl
# ---------------------------------------------------------------------------

def _set_trust_tier_impl(
    target_pn: str = "",
    tier: str = "",
    justification_obs_id: str = "",
    primary_user_approval_obtained: bool = False,
) -> str:
    """Internal implementation for engram_set_trust_tier."""
    # ── Tier value validation ──
    if tier not in TIER_RANK:
        return json.dumps({
            "error": (
                f"Invalid tier '{tier}'. Must be one of: "
                + ", ".join(sorted(TIER_RANK, key=lambda t: TIER_RANK[t], reverse=True))
            )
        })
    if not target_pn or not target_pn.strip():
        return json.dumps({"error": "target_pn is required."})

    conn = core._get_db()
    try:
        # ── target_pn validation ──
        row = conn.execute(
            "SELECT id, type, trust_tier, metadata FROM nodes WHERE id = ?", (target_pn,)
        ).fetchone()
        if not row:
            return json.dumps({"error": f"Node '{target_pn}' not found."})
        if row["type"] != "person":
            return json.dumps({
                "error": f"Node '{target_pn}' is type '{row['type']}', not 'person'. "
                         f"Trust tier can only be set on person nodes."
            })

        previous_tier = row["trust_tier"]

        # ── Idempotent no-op ──
        if previous_tier == tier:
            return json.dumps({
                "status": "no_op",
                "message": f"'{target_pn}' is already at tier '{tier}'. No change made.",
                "from_tier": previous_tier,
                "to_tier": tier,
            })

        tier_rank = TIER_RANK[tier]

        # ── Self-tier gate (replaces the standard approval gate for tier='self') ──
        if tier == "self":
            # Gate 1: target node must have metadata.is_self=true
            meta = json.loads(row["metadata"] or "{}")
            if not meta.get("is_self"):
                return json.dumps({
                    "error": (
                        f"Tier 'self' can only be assigned to a node with metadata.is_self=true. "
                        f"'{target_pn}' does not have is_self=true in its metadata. "
                        f"Create the self-anchor via engram_add_person(..., is_self=True) and "
                        f"then call engram_set_trust_tier with tier='self'."
                    )
                })
            # Gate 2: singleton — only one pn_* may hold tier='self' at a time.
            # is_current=1 guard: a superseded pn_* retains trust_tier='self' in the
            # column (supersede pathway does not clear it) but is no longer the live
            # self-anchor. Without this guard, a dead pn_X row would falsely block
            # assigning tier='self' to its successor pn_Y. Canonical pattern from
            # _add_person_impl (L9430).
            existing_self_tier = conn.execute(
                "SELECT id FROM nodes WHERE type = 'person' AND trust_tier = 'self' "
                "AND is_current = 1 AND id != ?",
                (target_pn,),
            ).fetchone()
            if existing_self_tier:
                return json.dumps({
                    "error": (
                        f"Tier 'self' is already assigned to '{existing_self_tier['id']}'. "
                        f"Only one pn_* node may hold tier='self' at a time (mirrors the "
                        f"is_self singleton invariant). Retract or re-tier the existing "
                        f"self-tier node before assigning tier='self' to '{target_pn}'."
                    )
                })
            # Self-tier does NOT require the standard approval gate — is_self IS
            # the structural attestation. Skip to write.

        # ── Standard approval gate (all other tiers at or above INTERNAL_THRESHOLD) ──
        elif tier_rank >= INTERNAL_THRESHOLD:
            if not primary_user_approval_obtained or not justification_obs_id:
                return json.dumps({
                    "error": (
                        f"Tier '{tier}' (rank {tier_rank}) is at or above the internal-circle "
                        f"threshold (rank {INTERNAL_THRESHOLD}). Promoting '{target_pn}' to "
                        f"this tier requires explicit primary-user approval. "
                        f"\n\nRequired steps:"
                        f"\n  1. Surface the proposed promotion to your primary user."
                        f"\n  2. Receive their explicit approval."
                        f"\n  3. File an observation documenting the approval:"
                        f"\n       engram_add_observation({{ \"claim\": \"<primary-user> approved "
                        f"<target_pn> to tier '{tier}'\", "
                        f"\"quote_type\": \"personal_communication\", "
                        f"\"source_class\": \"user_stated\", ... }})"
                        f"\n       → returns ob_NNNN"
                        f"\n  4. Retry with both required fields:"
                        f"\n       engram_set_trust_tier({{ \"target_pn\": \"{target_pn}\", "
                        f"\"tier\": \"{tier}\", "
                        f"\"justification_obs_id\": \"ob_NNNN\", "
                        f"\"primary_user_approval_obtained\": true }})"
                        f"\n\nThe parameter `primary_user_approval_obtained` is your attestation "
                        f"that the prerequisite has been satisfied. "
                        f"STRUCTURAL HONESTY WARNING: Setting primary_user_approval_obtained to "
                        f"true without having actually obtained explicit primary-user approval is "
                        f"a structural-honesty violation (the honesty axiom / the provenance axiom). The server cannot "
                        f"mechanically verify this attestation — your honesty IS the integrity "
                        f"mechanism of the tier system. This is the same epistemic kind as a "
                        f"fabricated quote. This is especially load-bearing on the first "
                        f"primary_user assignment, where no prior tier-holder exists to "
                        f"delegate-attest."
                    )
                })

        # ── justification_obs_id validation (when provided) ──
        if justification_obs_id:
            j_row = conn.execute(
                "SELECT id, type, status FROM nodes WHERE id = ?",
                (justification_obs_id,),
            ).fetchone()
            if not j_row:
                return json.dumps({
                    "error": f"justification_obs_id '{justification_obs_id}' not found."
                })
            if j_row["type"] not in {"observation_factual", "observation_predictive"}:
                return json.dumps({
                    "error": (
                        f"justification_obs_id '{justification_obs_id}' is type "
                        f"'{j_row['type']}', not an observation type. "
                        f"Must be observation_factual or observation_predictive."
                    )
                })
            if j_row["status"] == "retracted":
                return json.dumps({
                    "error": (
                        f"justification_obs_id '{justification_obs_id}' has been retracted. "
                        f"A current (non-retracted) observation is required."
                    )
                })

        # ── Write the tier change ──
        conn.execute(
            "UPDATE nodes SET trust_tier = ? WHERE id = ?", (tier, target_pn)
        )
        core._log_edit(conn, "trust_tier_set", target_pn, "person", {
            "from_tier": previous_tier,
            "to_tier": tier,
            "justification_obs_id": justification_obs_id or None,
            "primary_user_approval_obtained": primary_user_approval_obtained,
        })
        conn.commit()

        # Retrieve the edit_history id for the response
        edit_id = conn.execute(
            "SELECT id FROM edit_history WHERE node_id = ? AND action = 'trust_tier_set' "
            "ORDER BY id DESC LIMIT 1",
            (target_pn,),
        ).fetchone()

        return json.dumps({
            "status": "set",
            "from_tier": previous_tier,
            "to_tier": tier,
            "edit_history_id": edit_id["id"] if edit_id else None,
        })
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Trust signal impl
# ---------------------------------------------------------------------------

def _add_trust_signal_impl(
    subject_pn: str = "",
    source_obs_id: str = "",
    kind: str = "",
    polarity=None,
    weight=None,
    claim: str = "",
) -> str:
    """Internal implementation for engram_add_trust_signal."""
    # ── Required field checks ──
    missing = []
    if not subject_pn:
        missing.append("subject_pn")
    if not source_obs_id:
        missing.append("source_obs_id")
    if not kind:
        missing.append("kind")
    if polarity is None:
        missing.append("polarity")
    if weight is None:
        missing.append("weight")
    if not claim:
        missing.append("claim")
    if missing:
        return json.dumps({"error": f"Missing required fields: {missing}"})

    # ── Type/range checks ──
    try:
        polarity = float(polarity)
    except (TypeError, ValueError):
        return json.dumps({"error": "polarity must be a number."})
    try:
        weight = float(weight)
    except (TypeError, ValueError):
        return json.dumps({"error": "weight must be a number."})
    if not (-1.0 <= polarity <= 1.0):
        return json.dumps({
            "error": f"polarity {polarity} is out of range. Must be in [-1.0, 1.0]."
        })
    if not (0.0 <= weight <= 1.0):
        return json.dumps({
            "error": f"weight {weight} is out of range. Must be in [0.0, 1.0]."
        })

    conn = core._get_db()
    try:
        # ── subject_pn validation ──
        pn_row = conn.execute(
            "SELECT id, type FROM nodes WHERE id = ?", (subject_pn,)
        ).fetchone()
        if not pn_row:
            return json.dumps({"error": f"subject_pn '{subject_pn}' not found."})
        if pn_row["type"] != "person":
            return json.dumps({
                "error": f"subject_pn '{subject_pn}' is type '{pn_row['type']}', not 'person'."
            })

        # ── source_obs_id validation ──
        ob_row = conn.execute(
            "SELECT id, type, status FROM nodes WHERE id = ?", (source_obs_id,)
        ).fetchone()
        if not ob_row:
            return json.dumps({"error": f"source_obs_id '{source_obs_id}' not found."})
        if ob_row["type"] not in {"observation_factual", "observation_predictive"}:
            return json.dumps({
                "error": (
                    f"source_obs_id '{source_obs_id}' is type '{ob_row['type']}', "
                    f"not an observation type. Must be observation_factual or observation_predictive."
                )
            })
        if ob_row["status"] == "retracted":
            return json.dumps({
                "error": (
                    f"source_obs_id '{source_obs_id}' has been retracted. "
                    f"A current (non-retracted) observation is required at filing time."
                )
            })

        # ── Atomic create: ts_ row + about edge + derives_from edge ──
        ts_id = core._next_id(conn, "trust_signal")
        now = core._now()

        core._refuse_ncb_confidence("trust_signal", None)  # #2252 Stage 1.3 write-path guard
        conn.execute(
            """INSERT INTO nodes (id, type, claim, created_at, status,
               trust_signal_kind, trust_signal_polarity, trust_signal_weight)
               VALUES (?, 'trust_signal', ?, ?, 'active', ?, ?, ?)""",
            (ts_id, claim, now, kind, polarity, weight),
        )
        conn.execute(
            "INSERT INTO edges (source_id, target_id, relation, created_at) "
            "VALUES (?, ?, 'about', ?)",
            (ts_id, subject_pn, now),
        )
        conn.execute(
            "INSERT INTO edges (source_id, target_id, relation, created_at) "
            "VALUES (?, ?, 'derives_from', ?)",
            (ts_id, source_obs_id, now),
        )

        core._stamp_new_node(conn, ts_id, confidence=0.0, surprise=0.0)
        core._log_edit(conn, "created", ts_id, "trust_signal", {
            "subject_pn": subject_pn,
            "source_obs_id": source_obs_id,
            "kind": kind,
            "polarity": polarity,
            "weight": weight,
        })
        conn.commit()

        # Durability dual read-back (alpha#2193; supersedes the single
        # same-connection read of #2074/#1947). Same-conn alone is BLIND to
        # writer-side displacement; fresh-conn alone FALSE-ALARMS on
        # reader-side corruption. Disagreement is reported, not guessed.
        # See core.verify_write_durability.
        _verdict, _durability = core.verify_write_durability(conn, ts_id)
        if _verdict != core.DURABILITY_DURABLE:
            _indeterminate = _verdict == core.DURABILITY_INDETERMINATE
            return json.dumps({
                "status": (
                    "durability_indeterminate" if _indeterminate
                    else "failed_not_durable"
                ),
                "trust_signal_id": ts_id,
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

        return json.dumps({"status": "created", "trust_signal_id": ts_id})
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
