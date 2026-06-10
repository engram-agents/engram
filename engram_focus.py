"""engram_focus — focus-mode (family G) impls for the ENGRAM MCP server.

Extracted from server.py in #872 wave 2.

HOUSE RULES (mirror engram_core.py § HOUSE RULES):
- Access shared state ONLY via ``import engram_core as core; core.NAME`` — never
  via ``from engram_core import NAME``.
- This module must not import server.py (acyclic: server → family → core).
- No module-level mutable assignments — all state lives in engram_core.
"""

import json
import re

import engram_core as core


# ---------------------------------------------------------------------------
# Focus mode: pin nodes to the compaction summary so they survive context loss
# ---------------------------------------------------------------------------
#
# The compaction summary is the only durable channel from pre- to post-compact
# self — normal recall is probabilistic and depends on the user prompt firing
# a matching semantic query. Focus mode converts "I want this to survive" from
# an implicit wish into an explicit, diagnosable state: focused nodes MUST be
# rendered into the compaction summary's "Currently focused" section by the
# pre-compact self. The post-compact self wakes up with them already in hand.
#
# Rotation: focus is work-path-scoped, not permanent. Finishing a task or
# pivoting topics should unfocus the stale nodes. The cap prevents bloat.
FOCUS_LIST_CAP = 15


# Focus-set name pattern: lowercase alphanumerics, underscore, hyphen; 1–50 chars.
# No spaces (CLI-friendly), no uppercase (case-insensitive lookup without ambiguity).
FOCUS_SET_NAME_PATTERN = re.compile(r"^[a-z0-9_-]{1,50}$")


def _resolve_set_members(node_ids_json, conn):
    """Resolve a saved set's raw IDs into currently-loadable nodes.

    For each raw ID: walk the supersede chain to the current version; drop
    if the chain ends at a retracted node or the ID doesn't exist. Saved
    sets store raw IDs (immutable); resolution happens at load time.

    Returns:
        Tuple of (loaded, auto_followed_supersede, dropped_retracted,
        dropped_missing). loaded is the list of current node IDs to focus;
        auto_followed_supersede is a list of (original_id, final_id) pairs;
        the two dropped lists are original IDs that didn't survive.
    """
    try:
        raw_ids = json.loads(node_ids_json) if node_ids_json else []
    except (json.JSONDecodeError, TypeError):
        raw_ids = []

    loaded = []
    followed = []
    dropped_retracted = []
    dropped_missing = []

    for raw_id in raw_ids:
        current_id = raw_id
        visited = set()
        while True:
            if current_id in visited:
                # Cycle shield — shouldn't happen given DAG invariant.
                dropped_missing.append(raw_id)
                break
            visited.add(current_id)
            row = conn.execute(
                "SELECT id, status, superseded_by FROM nodes WHERE id = ?",
                (current_id,),
            ).fetchone()
            if row is None:
                dropped_missing.append(raw_id)
                break
            if row["status"] == "retracted":
                dropped_retracted.append(raw_id)
                break
            if row["superseded_by"]:
                current_id = row["superseded_by"]
                continue
            if current_id != raw_id:
                followed.append((raw_id, current_id))
            loaded.append(current_id)
            break

    return loaded, followed, dropped_retracted, dropped_missing


def _set_active_set_name(name_or_none, conn):
    """Update focus_state singleton with the given name (or NULL for ad-hoc)."""
    conn.execute(
        "UPDATE focus_state SET active_set_name = ? WHERE singleton_key = 1",
        (name_or_none,),
    )


def _clear_active_set_name_if_diverged(conn):
    """Clear active_set_name if the current active list no longer matches the saved set.

    Called after engram_focus / engram_unfocus mutations. Ground truth: the
    set of currently focused node IDs vs the resolved members of the saved
    set. If they differ for any reason (node added, node removed, saved set
    deleted), the active list is now ad-hoc.
    """
    row = conn.execute(
        "SELECT active_set_name FROM focus_state WHERE singleton_key = 1"
    ).fetchone()
    if row is None or row["active_set_name"] is None:
        return

    saved_name = row["active_set_name"]
    saved_row = conn.execute(
        "SELECT node_ids FROM focus_sets WHERE name = ?",
        (saved_name,),
    ).fetchone()
    if saved_row is None:
        _set_active_set_name(None, conn)
        return

    resolved, _, _, _ = _resolve_set_members(saved_row["node_ids"], conn)
    saved_set = set(resolved)

    active_rows = conn.execute(
        "SELECT id FROM nodes WHERE focused_at IS NOT NULL"
    ).fetchall()
    active_set = {r["id"] for r in active_rows}

    if saved_set != active_set:
        _set_active_set_name(None, conn)


# DESIGN INTENT — engram_focus
# ----------------------------
# DETERMINISTIC compaction-bridge: focused nodes MUST appear verbatim in the
# next compaction summary, so the post-compact agent wakes up pointed at the
# exact nodes the pre-compact agent named as load-bearing.
#
# Normal recall is probabilistic (cache-miss / surface-budget / drift) — focus
# is the substrate's "I cannot afford to forget this" channel. Use sparingly:
# the FOCUS_LIST_CAP (15) is intentional, both to prevent summary bloat AND
# to force the agent to make deliberate pin-or-rotate choices.
#
# Reason field is REQUIRED (ob_NNNN): forced justification at pin-time becomes
# durable accountability — the reason renders verbatim into the compaction
# summary, so post-compact me reads BOTH the ID and WHY pre-compact me chose
# to pin it. Prevents performative focus.
#
# Three valid pin shapes (per ob_NNNN family):
#   - Strong derivations/theories the current work depends on
#   - Conjectures being actively tested
#   - Key observations whose absence would force re-derivation from scratch
#
# Idempotency (ob_NNNN): re-pinning an already-focused node refreshes the
# focused_at + reason — no error, no surprise. Safe to call defensively.
#
# Companion tools: engram_unfocus (rotate out stale pins), engram_focus_save
# (capture current set under a name), engram_focus_load / focus_swap (restore /
# switch saved sets), engram_focus_sets (list available sets), engram_list_focused
# (pure-read inspection, no refresh).
def _focus_impl(node_ids: str, reason: str) -> str:
    """Impl for engram_focus — callable with named kwargs for in-server callers."""
    ids = [s.strip() for s in core._as_csv(node_ids).split(",") if s.strip()]
    if not ids:
        return json.dumps({"status": "error", "error": "No node IDs provided"})
    if not reason or not reason.strip():
        return json.dumps({"status": "error", "error": "reason is required"})

    conn = core._get_db()
    try:
        now = core._now()

        # Validate — must exist and be current
        existing = conn.execute(
            f"SELECT id, type, claim, focused_at FROM nodes WHERE id IN ({','.join('?' * len(ids))})",
            ids,
        ).fetchall()
        found_map = {r["id"]: r for r in existing}
        missing = [i for i in ids if i not in found_map]

        current_focus_rows = conn.execute(
            "SELECT id FROM nodes WHERE focused_at IS NOT NULL AND is_current = 1"
        ).fetchall()
        current_focus_ids = {r["id"] for r in current_focus_rows}

        to_add = [i for i in ids if i in found_map and i not in current_focus_ids]
        refresh = [i for i in ids if i in current_focus_ids]

        projected_size = len(current_focus_ids) + len(to_add)
        if projected_size > FOCUS_LIST_CAP:
            return json.dumps({
                "status": "rejected",
                "error": (
                    f"Focus list cap ({FOCUS_LIST_CAP}) would be exceeded: "
                    f"currently {len(current_focus_ids)} focused, "
                    f"adding {len(to_add)} new → {projected_size}. "
                    "Unfocus stale nodes first via engram_unfocus, or review "
                    "current focus with engram_list_focused."
                ),
                "current_focus_size": len(current_focus_ids),
                "current_focus_ids": sorted(current_focus_ids),
                "would_add": to_add,
                "cap": FOCUS_LIST_CAP,
            })

        # Apply — batch update
        placeholders = ",".join("?" * len(to_add + refresh))
        if to_add or refresh:
            conn.execute(
                f"UPDATE nodes SET focused_at = ?, focus_reason = ? "
                f"WHERE id IN ({placeholders})",
                [now, reason.strip()] + to_add + refresh,
            )

        for nid in to_add + refresh:
            core._log_edit(
                conn, "focused", nid, found_map[nid]["type"],
                {"reason": reason.strip(), "refresh": nid in refresh},
            )

        # If adding/refreshing pushed active off the currently-loaded saved
        # set, clear active_set_name — the active list is now ad-hoc.
        _clear_active_set_name_if_diverged(conn)
        if to_add or refresh:
            core._utility_reward(conn, to_add + refresh, action="focus")
        conn.commit()

        focused_now = conn.execute(
            "SELECT id FROM nodes WHERE focused_at IS NOT NULL AND is_current = 1"
        ).fetchall()
        state_now = conn.execute(
            "SELECT active_set_name FROM focus_state WHERE singleton_key = 1"
        ).fetchone()

        return json.dumps({
            "status": "ok",
            "focused": to_add,
            "refreshed": refresh,
            "missing": missing,
            "reason": reason.strip(),
            "focus_list_size": len(focused_now),
            "cap": FOCUS_LIST_CAP,
            "active_set_name": state_now["active_set_name"] if state_now else None,
        })
    finally:
        conn.close()


# DESIGN INTENT — engram_unfocus
# ------------------------------
# Rotate-out of the focus pin set. Pair to engram_focus — together they're
# the manual focus-management loop. The FOCUS_LIST_CAP (15) forces this
# discipline: pinning new things requires unpinning old ones once the list is
# full. That's the point — focus stays current.
#
# Typical triggers for unfocus:
#   - Task finished; its supporting derivations no longer need the pin
#   - Topic pivot; old thread's anchors no longer load-bearing
#   - Focused conjecture resolved (it's a derivation now, surfaces normally)
#   - Node stable enough to survive via normal recall without pinning
#
# Idempotent: unfocusing an already-unfocused node returns `already_unfocused`
# (success), not error. Safe to call defensively over a mixed-state set.
def _unfocus_impl(node_ids: str) -> str:
    """Impl for engram_unfocus — callable with named kwargs for in-server callers."""
    ids = [s.strip() for s in core._as_csv(node_ids).split(",") if s.strip()]
    if not ids:
        return json.dumps({"status": "error", "error": "No node IDs provided"})

    conn = core._get_db()
    try:
        rows = conn.execute(
            f"SELECT id, type, focused_at FROM nodes WHERE id IN ({','.join('?' * len(ids))})",
            ids,
        ).fetchall()
        found_map = {r["id"]: r for r in rows}
        missing = [i for i in ids if i not in found_map]

        to_release = [i for i in ids if i in found_map and found_map[i]["focused_at"] is not None]
        already_unfocused = [i for i in ids if i in found_map and found_map[i]["focused_at"] is None]

        if to_release:
            placeholders = ",".join("?" * len(to_release))
            conn.execute(
                f"UPDATE nodes SET focused_at = NULL, focus_reason = NULL "
                f"WHERE id IN ({placeholders})",
                to_release,
            )
            for nid in to_release:
                core._log_edit(conn, "unfocused", nid, found_map[nid]["type"], {})
            # Removing a node from the active list diverges it from the
            # saved set — clear active_set_name if that's what just happened.
            _clear_active_set_name_if_diverged(conn)
            core._utility_reward(conn, to_release, action="unfocus")
            conn.commit()

        remaining = conn.execute(
            "SELECT COUNT(*) as n FROM nodes WHERE focused_at IS NOT NULL AND is_current = 1"
        ).fetchone()["n"]
        state_now = conn.execute(
            "SELECT active_set_name FROM focus_state WHERE singleton_key = 1"
        ).fetchone()

        return json.dumps({
            "status": "ok",
            "released": to_release,
            "already_unfocused": already_unfocused,
            "missing": missing,
            "focus_list_size": remaining,
            "active_set_name": state_now["active_set_name"] if state_now else None,
        })
    finally:
        conn.close()


# DESIGN INTENT — engram_list_focused
# -----------------------------------
# Pure-read inspection of the focus list (ob_NNNN). No recall refresh, no
# edit log entry, no mutation — the safest tool in the focus family to call
# any time.
#
# Use cases:
#   - Work-stream start: "what did pre-compact me pin?" (already in compaction
#     summary, but engram_list_focused has the structured + ordered form).
#   - Mid-session check: verify what's currently guaranteed to cross into the
#     next context — useful before pin/unpin decisions.
#   - Pre-compact ritual (engram-nap §7): inspect focus list before any
#     rotation decisions.
#
# Ordered by focused_at ASC (oldest first) so the agent sees what's been
# pinned the longest — these are usually the cornerstone-like load-bearing
# anchors vs the recent pins that might be more rotation-eligible.
#
# active_set_name: names the saved focus set currently loaded (or null if
# active list is ad-hoc / never loaded / diverged from last-loaded set).
def _list_focused_impl() -> str:
    """Impl for engram_list_focused — pure-read, no mutations."""
    conn = core._get_db()
    try:
        rows = conn.execute(
            """SELECT id, type, claim, confidence, focus_reason, focused_at
               FROM nodes
               WHERE focused_at IS NOT NULL AND is_current = 1
               ORDER BY focused_at ASC"""
        ).fetchall()
        items = [{
            "id": r["id"],
            "type": r["type"],
            "claim": r["claim"],
            "confidence": r["confidence"],
            "focus_reason": r["focus_reason"],
            "focused_at": r["focused_at"],
        } for r in rows]
        state = conn.execute(
            "SELECT active_set_name FROM focus_state WHERE singleton_key = 1"
        ).fetchone()
        return json.dumps({
            "status": "ok",
            "count": len(items),
            "cap": FOCUS_LIST_CAP,
            "active_set_name": state["active_set_name"] if state else None,
            "focused": items,
        })
    finally:
        conn.close()


# DESIGN INTENT — engram_focus_save
# ---------------------------------
# Named-set bookmark of the current active focus list. Lets the agent capture
# the working-context of one project / thread, switch to another via
# engram_focus_load, and come back later. Multi-project agents can carry
# multiple coherent focus contexts without each pin-rotation destroying
# context from prior threads.
#
# Save is a BOOKMARK not a rotation — active list unchanged after save (just
# tagged with active_set_name). Identity holds: active list IS the saved set
# until a mutation (focus / unfocus) diverges them.
#
# Raw-IDs storage with cascade-resolution at LOAD time, not save time. If a
# node in a saved set is later superseded, the load operation follows the
# supersede edge to the replacement (per ob_NNNN cascade-resolution design)
# — sets stay live across graph evolution.
#
# Name validation: ^[a-z0-9_-]{1,50}$ — strict to keep set names URL/CLI
# safe + readable. No spaces, no uppercase. Description is optional; used as
# default focus_reason when loading.
def _focus_save_impl(name: str, description: str = "", overwrite: bool = False) -> str:
    """Impl for engram_focus_save — callable with named kwargs for in-server callers."""
    name = name.strip()
    if not FOCUS_SET_NAME_PATTERN.match(name):
        return json.dumps({
            "status": "error",
            "error": (
                f"Invalid name '{name}': must match ^[a-z0-9_-]{{1,50}}$ "
                "(lowercase alphanumerics, underscore, hyphen; 1–50 chars)."
            ),
        })

    conn = core._get_db()
    try:
        active_rows = conn.execute(
            "SELECT id FROM nodes WHERE focused_at IS NOT NULL AND is_current = 1 "
            "ORDER BY focused_at ASC"
        ).fetchall()
        if not active_rows:
            return json.dumps({
                "status": "error",
                "error": (
                    "Active focus list is empty — nothing to save. "
                    "Focus nodes first, then save."
                ),
            })
        active_ids = [r["id"] for r in active_rows]

        existing = conn.execute(
            "SELECT name FROM focus_sets WHERE name = ?",
            (name,),
        ).fetchone()
        if existing and not overwrite:
            return json.dumps({
                "status": "error",
                "error": (
                    f"Focus set '{name}' already exists. "
                    "Pass overwrite=True to replace."
                ),
            })

        now = core._now()
        if existing:
            conn.execute(
                "UPDATE focus_sets SET node_ids = ?, description = ?, created_at = ? "
                "WHERE name = ?",
                (json.dumps(active_ids), description.strip(), now, name),
            )
            action = "overwritten"
        else:
            conn.execute(
                "INSERT INTO focus_sets "
                "(name, node_ids, description, created_at, load_count) "
                "VALUES (?, ?, ?, ?, 0)",
                (name, json.dumps(active_ids), description.strip(), now),
            )
            action = "created"

        _set_active_set_name(name, conn)
        conn.commit()

        return json.dumps({
            "status": "ok",
            "action": action,
            "name": name,
            "node_count": len(active_ids),
            "node_ids": active_ids,
            "active_set_name": name,
        })
    finally:
        conn.close()


# DESIGN INTENT — engram_focus_load
# ---------------------------------
# Restore a saved focus set into the active list. The cascade-resolution
# pairing with engram_focus_save: save captures raw IDs; load resolves them
# (supersede chains auto-followed, retracted nodes dropped, missing nodes
# reported).
#
# Default if_active="error" is the SAFETY DEFAULT — refuse to clobber a non-
# empty active list. Forces the agent to either (a) explicitly unfocus first
# or (b) use engram_focus_swap which atomically saves-then-loads.
#
# Use case: returning to a prior project. The active focus set drifted to
# the current thread; load("paper_sprint") restores the paper-context anchors
# in one move. Future-me reading the compaction summary post-load sees the
# right anchors without manual pin-by-pin reconstruction.
#
# Cascade resolution at load time is intentional (not save time) — saved sets
# stay live across graph evolution. A set saved months ago, where some nodes
# have been superseded, will load the up-to-date replacements via the supersede
# edges.
def _focus_load_impl(name: str, if_active: str = "error") -> str:
    """Impl for engram_focus_load — callable with named kwargs for in-server callers."""
    name = name.strip()
    if if_active not in {"error", "overwrite"}:
        return json.dumps({
            "status": "error",
            "error": f"if_active must be 'error' or 'overwrite', got '{if_active}'",
        })

    conn = core._get_db()
    try:
        saved_row = conn.execute(
            "SELECT name, node_ids, description FROM focus_sets WHERE name = ?",
            (name,),
        ).fetchone()
        if saved_row is None:
            return json.dumps({
                "status": "error",
                "error": (
                    f"No focus set named '{name}'. "
                    "Use engram_focus_sets() to list available sets."
                ),
            })

        state = conn.execute(
            "SELECT active_set_name FROM focus_state WHERE singleton_key = 1"
        ).fetchone()
        active_rows = conn.execute(
            "SELECT id, type FROM nodes "
            "WHERE focused_at IS NOT NULL AND is_current = 1 "
            "ORDER BY focused_at ASC"
        ).fetchall()

        # Resolve upfront so we can distinguish "truly already-active" from
        # "name matches but cascade state has shifted since last load."
        loaded, followed, dropped_retracted, dropped_missing = _resolve_set_members(
            saved_row["node_ids"], conn,
        )
        active_ids_set = {r["id"] for r in active_rows}
        resolved_set = set(loaded)
        name_matches = bool(state and state["active_set_name"] == name)

        # Already-active fast path: name matches AND resolved members match
        # current active list exactly. Refresh timestamps, return.
        if name_matches and active_rows and resolved_set == active_ids_set:
            now = core._now()
            conn.execute(
                "UPDATE focus_sets SET last_loaded_at = ?, "
                "load_count = load_count + 1 WHERE name = ?",
                (now, name),
            )
            conn.commit()
            return json.dumps({
                "status": "already_active",
                "name": name,
                "focused": [r["id"] for r in active_rows],
                "focus_list_size": len(active_rows),
                "active_set_name": name,
            })

        # Name matches but resolved differs — this is a reload to refresh
        # cascade state. Treat as implicit overwrite (no need for the user
        # to pass if_active='overwrite' to reload their own set).
        implicit_overwrite = name_matches and resolved_set != active_ids_set

        if active_rows and if_active == "error" and not implicit_overwrite:
            return json.dumps({
                "status": "error",
                "error": (
                    f"Active focus list has {len(active_rows)} node(s) "
                    f"(active_set_name={state['active_set_name'] if state else None}). "
                    "Pass if_active='overwrite' to unfocus first, or use "
                    "engram_focus_swap to save-current + load-other atomically."
                ),
                "current_focus_size": len(active_rows),
                "current_focus_ids": [r["id"] for r in active_rows],
                "current_active_set_name": state["active_set_name"] if state else None,
            })

        now = core._now()

        if active_rows and (if_active == "overwrite" or implicit_overwrite):
            ids_to_clear = [r["id"] for r in active_rows]
            type_map_clear = {r["id"]: r["type"] for r in active_rows}
            placeholders = ",".join("?" * len(ids_to_clear))
            conn.execute(
                f"UPDATE nodes SET focused_at = NULL, focus_reason = NULL "
                f"WHERE id IN ({placeholders})",
                ids_to_clear,
            )
            clear_reason = "reloaded_same_set" if implicit_overwrite else "cleared_for_load"
            for nid in ids_to_clear:
                core._log_edit(
                    conn, "unfocused", nid, type_map_clear.get(nid, "unknown"),
                    {"reason": clear_reason, "loading_set": name},
                )

        load_reason = (
            saved_row["description"].strip()
            if saved_row["description"] and saved_row["description"].strip()
            else f"loaded from set: {name}"
        )

        if loaded:
            placeholders = ",".join("?" * len(loaded))
            type_rows = conn.execute(
                f"SELECT id, type FROM nodes WHERE id IN ({placeholders})",
                loaded,
            ).fetchall()
            type_map = {r["id"]: r["type"] for r in type_rows}
            conn.execute(
                f"UPDATE nodes SET focused_at = ?, focus_reason = ? "
                f"WHERE id IN ({placeholders})",
                [now, load_reason] + loaded,
            )
            for nid in loaded:
                core._log_edit(
                    conn, "focused", nid, type_map.get(nid, "unknown"),
                    {"reason": load_reason, "from_set": name},
                )

        conn.execute(
            "UPDATE focus_sets SET last_loaded_at = ?, "
            "load_count = load_count + 1 WHERE name = ?",
            (now, name),
        )
        _set_active_set_name(name, conn)
        if loaded:
            core._utility_reward(conn, loaded, action="focus_load")
        conn.commit()

        return json.dumps({
            "status": "ok",
            "name": name,
            "loaded": loaded,
            "auto_followed_supersede": [
                {"original": o, "loaded": n} for (o, n) in followed
            ],
            "dropped_retracted": dropped_retracted,
            "dropped_missing": dropped_missing,
            "focus_list_size": len(loaded),
            "active_set_name": name,
        })
    finally:
        conn.close()


# DESIGN INTENT — engram_focus_swap
# ---------------------------------
# The canonical "pivot" operation: atomic save-then-load. Lets the agent
# switch projects without manually composing save + unfocus + load
# (which is racy across compaction if mid-step).
#
# Both halves in ONE transaction: if the load target doesn't exist, the save
# rolls back too — never leaves the focus state half-broken.
#
# save_as == load is a no-op: saves current under the same name + returns
# already_active. Safe defensively callable.
#
# Use case: "I'm switching from paper-sprint to dream-cost investigation."
# engram_focus_swap(save_as="paper_sprint", load="dream_cost") — paper-sprint
# anchors captured, dream-cost anchors active, all atomic.
def _focus_swap_impl(save_as: str, load: str = "", description: str = "") -> str:
    """Impl for engram_focus_swap — callable with named kwargs for in-server callers."""
    save_as = save_as.strip()
    load = load.strip()
    if not FOCUS_SET_NAME_PATTERN.match(save_as):
        return json.dumps({
            "status": "error",
            "error": f"Invalid save_as '{save_as}': must match ^[a-z0-9_-]{{1,50}}$",
        })
    if load and not FOCUS_SET_NAME_PATTERN.match(load):
        return json.dumps({
            "status": "error",
            "error": f"Invalid load '{load}': must match ^[a-z0-9_-]{{1,50}}$",
        })

    conn = core._get_db()
    try:
        active_rows = conn.execute(
            "SELECT id, type FROM nodes "
            "WHERE focused_at IS NOT NULL AND is_current = 1 "
            "ORDER BY focused_at ASC"
        ).fetchall()
        if not active_rows:
            return json.dumps({
                "status": "error",
                "error": "Active focus list is empty — nothing to save.",
            })
        active_ids = [r["id"] for r in active_rows]
        active_type_map = {r["id"]: r["type"] for r in active_rows}

        now = core._now()

        # Save half
        existing_save = conn.execute(
            "SELECT name FROM focus_sets WHERE name = ?",
            (save_as,),
        ).fetchone()
        if existing_save:
            conn.execute(
                "UPDATE focus_sets SET node_ids = ?, description = ?, created_at = ? "
                "WHERE name = ?",
                (json.dumps(active_ids), description.strip(), now, save_as),
            )
        else:
            conn.execute(
                "INSERT INTO focus_sets "
                "(name, node_ids, description, created_at, load_count) "
                "VALUES (?, ?, ?, ?, 0)",
                (save_as, json.dumps(active_ids), description.strip(), now),
            )

        # No-load branch
        if not load:
            _set_active_set_name(save_as, conn)
            conn.commit()
            return json.dumps({
                "status": "ok",
                "saved": {
                    "name": save_as,
                    "node_count": len(active_ids),
                    "node_ids": active_ids,
                },
                "loaded": None,
                "active_set_name": save_as,
            })

        # Same-name no-op
        if save_as == load:
            _set_active_set_name(save_as, conn)
            conn.execute(
                "UPDATE focus_sets SET last_loaded_at = ?, "
                "load_count = load_count + 1 WHERE name = ?",
                (now, save_as),
            )
            conn.commit()
            return json.dumps({
                "status": "already_active",
                "saved": {"name": save_as, "node_count": len(active_ids)},
                "loaded": {"name": save_as, "node_count": len(active_ids)},
                "active_set_name": save_as,
            })

        # Load target validation — rollback save if missing
        load_row = conn.execute(
            "SELECT name, node_ids, description FROM focus_sets WHERE name = ?",
            (load,),
        ).fetchone()
        if load_row is None:
            conn.rollback()
            return json.dumps({
                "status": "error",
                "error": (
                    f"Load target set '{load}' does not exist. "
                    "No changes made (save rolled back)."
                ),
            })

        # Clear active
        placeholders_clear = ",".join("?" * len(active_ids))
        conn.execute(
            f"UPDATE nodes SET focused_at = NULL, focus_reason = NULL "
            f"WHERE id IN ({placeholders_clear})",
            active_ids,
        )
        for nid in active_ids:
            core._log_edit(
                conn, "unfocused", nid, active_type_map.get(nid, "unknown"),
                {"reason": "swap_out_to", "to_set": load, "saved_as": save_as},
            )

        # Resolve + focus load set
        loaded, followed, dropped_retracted, dropped_missing = _resolve_set_members(
            load_row["node_ids"], conn,
        )
        load_reason = (
            load_row["description"].strip()
            if load_row["description"] and load_row["description"].strip()
            else f"loaded from set: {load}"
        )

        if loaded:
            p2 = ",".join("?" * len(loaded))
            type_rows2 = conn.execute(
                f"SELECT id, type FROM nodes WHERE id IN ({p2})",
                loaded,
            ).fetchall()
            type_map2 = {r["id"]: r["type"] for r in type_rows2}
            conn.execute(
                f"UPDATE nodes SET focused_at = ?, focus_reason = ? "
                f"WHERE id IN ({p2})",
                [now, load_reason] + loaded,
            )
            for nid in loaded:
                core._log_edit(
                    conn, "focused", nid, type_map2.get(nid, "unknown"),
                    {"reason": load_reason, "from_set": load},
                )

        conn.execute(
            "UPDATE focus_sets SET last_loaded_at = ?, "
            "load_count = load_count + 1 WHERE name = ?",
            (now, load),
        )
        _set_active_set_name(load, conn)
        # Bump both the outgoing and incoming node sets — swap is focus on both
        swap_bump_ids = list(dict.fromkeys(active_ids + loaded))
        if swap_bump_ids:
            core._utility_reward(conn, swap_bump_ids, action="focus")
        conn.commit()

        return json.dumps({
            "status": "ok",
            "saved": {
                "name": save_as,
                "node_count": len(active_ids),
                "node_ids": active_ids,
            },
            "loaded": {
                "name": load,
                "node_count": len(loaded),
                "loaded_ids": loaded,
                "auto_followed_supersede": [
                    {"original": o, "loaded": n} for (o, n) in followed
                ],
                "dropped_retracted": dropped_retracted,
                "dropped_missing": dropped_missing,
            },
            "active_set_name": load,
        })
    finally:
        conn.close()


# DESIGN INTENT — engram_focus_sets
# ---------------------------------
# Inventory tool: "what saved focus sets do I have available?" Recently-used
# first so the working sets surface before archival ones.
#
# load_count + last_loaded_at: empirical usage signal. Sets that haven't been
# touched in months are candidates for engram_focus_delete_set cleanup.
#
# is_active flag: marks the currently-loaded set (active_set_name match).
# Disambiguates "saved AND currently-active" from "saved but cold-storage."
def _focus_sets_impl() -> str:
    """Impl for engram_focus_sets — pure-read, no mutations."""
    conn = core._get_db()
    try:
        rows = conn.execute(
            "SELECT name, node_ids, description, created_at, last_loaded_at, load_count "
            "FROM focus_sets "
            "ORDER BY COALESCE(last_loaded_at, '0000') DESC, created_at DESC"
        ).fetchall()
        state = conn.execute(
            "SELECT active_set_name FROM focus_state WHERE singleton_key = 1"
        ).fetchone()
        active_set_name = state["active_set_name"] if state else None

        sets = []
        for r in rows:
            try:
                nids = json.loads(r["node_ids"])
            except (json.JSONDecodeError, TypeError):
                nids = []
            sets.append({
                "name": r["name"],
                "node_count": len(nids),
                "description": r["description"] or "",
                "created_at": r["created_at"],
                "last_loaded_at": r["last_loaded_at"],
                "load_count": r["load_count"],
                "is_active": r["name"] == active_set_name,
            })
        return json.dumps({
            "status": "ok",
            "count": len(sets),
            "active_set_name": active_set_name,
            "sets": sets,
        })
    finally:
        conn.close()


# DESIGN INTENT — engram_focus_delete_set
# ---------------------------------------
# Drop the bookmark, not the focus. This is the key distinction: the saved
# set is metadata; deleting it removes the name + content stored in focus_sets
# table. The currently-focused NODES (focused_at fields on the nodes
# themselves) are UNAFFECTED.
#
# If the deleted set was active: active_set_name clears to NULL (the active
# list becomes ad-hoc — still pinning the same nodes, just no longer
# identified-as a named set).
#
# Use case: archival cleanup of stale named sets identified via
# engram_focus_sets (low load_count + old last_loaded_at). Bookmark removal,
# not focus removal.
def _focus_delete_set_impl(name: str) -> str:
    """Impl for engram_focus_delete_set — callable with named kwargs for in-server callers."""
    name = name.strip()
    conn = core._get_db()
    try:
        row = conn.execute(
            "SELECT name, node_ids FROM focus_sets WHERE name = ?",
            (name,),
        ).fetchone()
        if row is None:
            return json.dumps({
                "status": "error",
                "error": f"No focus set named '{name}'",
            })

        state = conn.execute(
            "SELECT active_set_name FROM focus_state WHERE singleton_key = 1"
        ).fetchone()
        was_active = bool(state and state["active_set_name"] == name)

        conn.execute("DELETE FROM focus_sets WHERE name = ?", (name,))
        if was_active:
            _set_active_set_name(None, conn)
        conn.commit()

        try:
            node_count = len(json.loads(row["node_ids"]))
        except (json.JSONDecodeError, TypeError):
            node_count = 0

        return json.dumps({
            "status": "ok",
            "deleted": name,
            "node_count": node_count,
            "was_active": was_active,
            "active_set_name": None if was_active else (
                state["active_set_name"] if state else None
            ),
        })
    finally:
        conn.close()
