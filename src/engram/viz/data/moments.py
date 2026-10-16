"""viz.data.moments — the special moments the agent chose to keep, grouped by
person, for the human-facing Moments page (engram-alpha#3511).

READ-ONLY by construction: the DB is opened ``mode=ro`` and nothing is ever
written. In particular this never touches ``metadata.special_moment_cursor``,
the rotation cursor ``engram_query`` writes when it renders a moment into
recall -- showing a moment on this page must not change what recall does.

Select and render, never generate: every string returned is something the
agent already wrote (a moment's description, the pointed node's own
recall_summary or claim). Nothing here composes new text.

Malformed stored data never raises: an unparseable metadata blob, a
``special_moments`` that is not a list, or an entry that is not a dict with
a non-empty string ``description`` is skipped, and every other valid moment
still renders.
"""

from __future__ import annotations

import json
import os
import sqlite3

from .sqlite_ro import connect_ro

# Fallback summary length when the pointed node has no recall_summary.
_CLAIM_TRUNCATE = 160


def _parse_metadata(raw) -> dict:
    if not isinstance(raw, str) or not raw:
        return {}
    try:
        meta = json.loads(raw)
    except (ValueError, TypeError):
        return {}
    return meta if isinstance(meta, dict) else {}


def _clean_str(value) -> str | None:
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _truncate(text: str, limit: int = _CLAIM_TRUNCATE) -> str:
    text = text.strip()
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


def _node_summary(conn: sqlite3.Connection, node_id, has_summary_col: bool):
    """Return the summary of a current, non-retracted node, else None."""
    if not isinstance(node_id, str) or not node_id:
        return None
    cols = "claim, recall_summary" if has_summary_col else "claim, NULL"
    row = conn.execute(
        f"SELECT {cols} FROM nodes WHERE id = ? AND is_current = 1"
        " AND (status IS NULL OR status != 'retracted')",
        (node_id,),
    ).fetchone()
    if row is None:
        return None
    claim, summary = row
    text = _clean_str(summary) or (_truncate(claim) if isinstance(claim, str) else "")
    # An existing node with no usable text is still an existing node: "" is
    # falsy but not None, so node_exists stays True.
    return text


def _valid_moments(meta: dict, conn: sqlite3.Connection, has_summary_col: bool) -> list[dict]:
    entries = meta.get("special_moments")
    if not isinstance(entries, list):
        return []
    out = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        description = _clean_str(entry.get("description"))
        if description is None:
            continue
        node_id = entry.get("node_id")
        summary = _node_summary(conn, node_id, has_summary_col)
        added_at = entry.get("added_at")
        out.append({
            "description": description,
            "added_at": added_at if isinstance(added_at, str) and added_at else None,
            "node_id": node_id if isinstance(node_id, str) and node_id else None,
            "node_summary": summary,
            "node_exists": summary is not None,
        })
    # Newest first; undated moments last. ISO-8601 strings sort lexically.
    out.sort(key=lambda m: m["added_at"] or "", reverse=True)
    return out


def _scan_persons(conn):
    """Yield ``(person_id, claim, meta, tier, moments)`` for every current,
    non-retracted person with at least one valid moment, lazily -- so a caller
    that only needs existence can stop at the first. Yields nothing when the
    DB is not an ENGRAM nodes table."""
    cols = {r[1] for r in conn.execute("PRAGMA table_info(nodes)").fetchall()}
    if not {"id", "type", "claim", "metadata", "is_current"} <= cols:
        return
    has_summary_col = "recall_summary" in cols
    tier_col = "trust_tier" if "trust_tier" in cols else "NULL"
    if "status" not in cols:
        return  # not an ENGRAM nodes table; both queries filter on status
    persons = conn.execute(
        f"SELECT id, claim, metadata, {tier_col} FROM nodes"
        f" WHERE type = 'person' AND is_current = 1"
        " AND (status IS NULL OR status != 'retracted')"
    ).fetchall()
    for person_id, claim, raw_meta, tier in persons:
        meta = _parse_metadata(raw_meta)
        moments = _valid_moments(meta, conn, has_summary_col)
        if moments:
            yield person_id, claim, meta, tier, moments


def has_moments(db_path: str, *, timeout: float | None = None) -> bool:
    """True iff ``load_moments(db_path)`` would be non-empty; stops at the
    first valid moment instead of building every group. ``timeout`` bounds how
    long a locked DB may stall the caller (the per-request nav check passes a
    short one). Errors propagate -- the caller decides to log and hide."""
    if not db_path or not os.path.exists(db_path):
        return False
    conn = connect_ro(db_path, timeout=timeout)
    try:
        return next(_scan_persons(conn), None) is not None
    finally:
        conn.close()


def load_moments(db_path: str) -> list[dict]:
    """Return one group per current, non-retracted person node that has at
    least one valid moment, newest-moment-first.

    Group shape: ``{person_id, name, relation, trust_tier, moments}``.
    Moment shape: ``{description, added_at, node_id, node_summary,
    node_exists}``. A moment whose node is missing, superseded or retracted
    is still returned, with ``node_exists=False`` and ``node_summary=None``.
    """
    if not db_path or not os.path.exists(db_path):
        return []
    conn = connect_ro(db_path)
    try:
        groups = []
        for person_id, claim, meta, tier, moments in _scan_persons(conn):
            name = _clean_str(meta.get("name"))
            if name is None and isinstance(claim, str):
                # claim is "<name> — <relation>" (see _add_person_impl).
                name = _clean_str(claim.split(" — ", 1)[0])
            groups.append({
                "person_id": person_id,
                "name": name or person_id,
                # Prefer "relation"; fall back to the deprecated pre-rename
                # "role" key (same rule as engram_trust.py).
                "relation": (_clean_str(meta.get("relation"))
                             or _clean_str(meta.get("role"))),
                "trust_tier": _clean_str(tier) or _clean_str(meta.get("trust_tier")),
                "moments": moments,
            })
        groups.sort(key=lambda g: g["moments"][0]["added_at"] or "", reverse=True)
        return groups
    finally:
        conn.close()
