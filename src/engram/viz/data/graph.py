"""viz.data.graph — graph-read data assembly for /api/graph and
/api/search (engram-alpha#2555 PR A, pure relocation of viz_server.py's
``search_nodes``/``get_graph``, byte-identical function bodies).

No HTTP, no HTML — unit-testable in isolation.
"""

from __future__ import annotations

import json
import os
import sqlite3


def search_nodes(db_path: str, query: str, limit: int = 20) -> dict:
    """FTS5-based node search for the /api/search endpoint.

    Falls back to a LIKE scan when nodes_fts is unavailable (e.g. schema
    version pre-dates FTS setup). Returns at most `limit` results ordered
    by FTS rank (best match first) or by recency for the fallback path.

    Result shape per node: {id, type, claim, confidence, status}.
    """
    if not os.path.exists(db_path):
        return {"results": [], "error": f"Database not found: {db_path}"}
    if not query or not query.strip():
        return {"results": [], "error": "query is required"}
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row

        # Check whether nodes_fts exists
        fts_exists = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='nodes_fts'"
        ).fetchone() is not None

        results = []
        if fts_exists:
            # FTS5 path — use MATCH with a simple token sanitization:
            # strip non-alphanumeric chars that FTS5 doesn't handle well,
            # append * for prefix matching on the last token.
            import re as _re
            tokens = _re.sub(r"[^\w\s]", " ", query).split()
            if tokens:
                fts_expr = " ".join(tokens[:-1] + [tokens[-1] + "*"])
            else:
                fts_expr = None

            if fts_expr:
                rows = conn.execute(
                    """SELECT n.id, n.type, n.claim, n.confidence, n.status
                       FROM nodes n
                       JOIN nodes_fts f ON n.rowid = f.rowid
                       WHERE f.nodes_fts MATCH ?
                         AND n.is_current = 1
                         AND (n.status IS NULL OR n.status NOT IN ('retracted'))
                       ORDER BY rank
                       LIMIT ?""",
                    (fts_expr, limit),
                ).fetchall()
                results = [dict(r) for r in rows]

        if not results:
            # Fallback: LIKE scan on claim
            like_pat = f"%{query.replace('%', '').replace('_', '')}%"
            rows = conn.execute(
                """SELECT id, type, claim, confidence, status
                   FROM nodes
                   WHERE claim LIKE ?
                     AND is_current = 1
                     AND (status IS NULL OR status NOT IN ('retracted'))
                   ORDER BY created_at DESC
                   LIMIT ?""",
                (like_pat, limit),
            ).fetchall()
            results = [dict(r) for r in rows]

        conn.close()
        return {"results": results, "query": query}

    except sqlite3.OperationalError as e:
        return {"results": [], "error": str(e)}
    except Exception as e:
        return {"results": [], "error": str(e)}


def get_graph(db_path: str) -> dict:
    if not os.path.exists(db_path):
        return {"nodes": [], "edges": [], "error": f"Database not found: {db_path}"}
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()

        # All nodes — dynamically select available columns to handle
        # both old (pre-memory) and new schemas
        col_names = {r[1] for r in cur.execute("PRAGMA table_info(nodes)").fetchall()}
        base_cols = [
            "id", "type", "claim", "created_at",
            "source_url", "source_title", "source_domain", "source_accessed", "content_snippet",
            "evidence_id", "quoted_text", "interpretation", "quote_type",
            "predicted_event", "resolution_timeframe", "status", "resolved_by",
            "logical_chain", "confidence", "confidence_history", "is_current",
            "supersedes", "superseded_by", "metadata",
        ]
        # Memory-management and semantic columns (added in later schema versions)
        extra_cols = ["importance_base", "importance_score", "recall_turn",
                      "recall_count", "memory_status", "source_class", "utility_score",
                      # feeling_report-specific columns
                      "reported_state", "trigger_text", "categorical_tag",
                      "intensity_hint", "nudge_source",
                      # recall summary + keywords (added for detail panel top display)
                      "recall_summary", "recall_keywords"]
        select_cols = base_cols + [c for c in extra_cols if c in col_names]
        cur.execute(f"SELECT {','.join(select_cols)} FROM nodes ORDER BY id")
        nodes = []
        for row in cur.fetchall():
            n = dict(row)
            # Ensure booleans are plain Python ints for JSON
            n["is_current"] = int(n["is_current"]) if n["is_current"] is not None else 1
            # Parse recall_keywords from JSON string to list so JS receives an array
            if "recall_keywords" in n and n["recall_keywords"]:
                try:
                    n["recall_keywords"] = json.loads(n["recall_keywords"])
                except (json.JSONDecodeError, TypeError):
                    n["recall_keywords"] = []
            nodes.append(n)

        # Cascade markers (#2013/#2009 slice 1): tainted_by / stale_by used
        # to live in metadata and were rendered client-side straight off
        # d.metadata. They're in cascade_markers now — attach them here as
        # top-level node fields (one bulk query, not one per node) so the
        # JS below reads d.tainted_by / d.stale_by directly instead of
        # trying to parse them out of a metadata blob that no longer
        # carries them. stale_replacement (the JS "superseded by" hint) is
        # reconstructed the same way as engram_lifecycle's reflect report:
        # a stale marker's root_node IS the directly-superseded node under
        # slice 1's uniform severity, so its own superseded_by column names
        # the replacement — no per-downstream cache needed.
        marker_rows = cur.execute(
            "SELECT node_id, root_node, root_op FROM cascade_markers WHERE status = 'pending'"
        ).fetchall()
        tainted_map: dict = {}
        stale_map: dict = {}
        stale_roots: set = set()
        for mr in marker_rows:
            target = tainted_map if mr["root_op"] == "retract" else stale_map
            bucket = target.setdefault(mr["node_id"], [])
            if mr["root_node"] not in bucket:
                bucket.append(mr["root_node"])
            if mr["root_op"] == "supersede":
                stale_roots.add(mr["root_node"])
        replacement_of = {}
        if stale_roots:
            placeholders = ",".join("?" for _ in stale_roots)
            for r in cur.execute(
                f"SELECT id, superseded_by FROM nodes WHERE id IN ({placeholders})",
                tuple(stale_roots),
            ).fetchall():
                replacement_of[r["id"]] = r["superseded_by"]
        for n in nodes:
            tb = tainted_map.get(n["id"])
            if tb:
                n["tainted_by"] = tb
            sb = stale_map.get(n["id"])
            if sb:
                n["stale_by"] = sb
                n["stale_replacement"] = {
                    root: replacement_of.get(root) for root in sb
                }

        # All edges
        cur.execute("""
            SELECT source_id, target_id, relation, created_at
            FROM edges
            ORDER BY id
        """)
        edges = [
            {"source_id": r["source_id"], "target_id": r["target_id"],
             "relation": r["relation"], "created_at": r["created_at"]}
            for r in cur.fetchall()
        ]

        conn.close()
        return {"nodes": nodes, "edges": edges}

    except sqlite3.OperationalError as e:
        # Table may not exist yet
        if "no such table" in str(e):
            return {"nodes": [], "edges": [], "error": "Schema not initialised yet — start the MCP server first."}
        return {"nodes": [], "edges": [], "error": str(e)}
    except Exception as e:
        return {"nodes": [], "edges": [], "error": str(e)}


# ---------------------------------------------------------------------------
# Index payload for /api/graph + on-demand full-node lookup for /api/node
# (engram-alpha#2866 fix 4 -- see the 2026-09-26 issue comment "Re-profile
# and the fix-4 contract" for the design of record).
#
# get_graph() ships every field of every node -- 18-33 MB raw / 4-7.5 MB
# gzip on a real graph, though the page only needs a handful of fields per
# node until one is clicked. Both functions below are derived FROM
# get_graph()'s own output (never a separately-shaped query) so the index
# can never disagree with the full graph on any field it carries forward --
# get_graph() itself stays byte-for-byte unchanged (existing tests such as
# tests/test_viz_server_cascade_markers.py call it directly).
# ---------------------------------------------------------------------------

# The plain (non-derived) index whitelist keys -- copied through verbatim
# from get_graph's own node dict when present, and left OUT of the index
# node entirely when get_graph's node lacks them (never an invented
# default -- e.g. tainted_by/stale_by are only present on a node actually
# marked by a pending cascade_markers row; see get_graph's own
# warnings-omission convention above).
_INDEX_PLAIN_KEYS = (
    "id", "type", "status", "is_current", "confidence", "importance_score",
    "created_at", "tainted_by", "stale_by", "source_domain", "source_title",
)


def _index_node(full_node: dict) -> dict:
    """Reduce one full node (as returned by get_graph) to the index
    whitelist: the plain keys above, plus the two derived fields --
    `label` (first 80 chars of `claim`, "" if no claim, on every node
    regardless of type) and, ONLY for type == 'definition', `term` (read
    out of `metadata.term`)."""
    idx = {k: full_node[k] for k in _INDEX_PLAIN_KEYS if k in full_node}

    claim = full_node.get("claim") or ""
    idx["label"] = claim[:80]

    if full_node.get("type") == "definition":
        term = None
        meta = full_node.get("metadata")
        if meta:
            try:
                meta_obj = meta if isinstance(meta, dict) else json.loads(meta)
            except (json.JSONDecodeError, TypeError):
                meta_obj = None
            if isinstance(meta_obj, dict):
                term = meta_obj.get("term")
        idx["term"] = term

    return idx


def get_graph_index(db_path: str) -> dict:
    """/api/graph's payload (engram-alpha#2866 fix 4) -- same top-level
    shape as get_graph (nodes/edges, plus an `error` key on failure), but
    each node reduced to the index whitelist via _index_node(). Edges are
    passed through unchanged."""
    full = get_graph(db_path)
    result = {
        "nodes": [_index_node(n) for n in full.get("nodes", [])],
        "edges": full.get("edges", []),
    }
    if "error" in full:
        result["error"] = full["error"]
    return result


def get_node(db_path: str, node_id: str) -> dict | None:
    """The FULL node exactly as get_graph(db_path) would return it for
    `node_id` -- used by GET /api/node so the detail panel can fetch one
    node's complete data on demand instead of shipping every field of
    every node up front. None if the id is absent (or the graph itself
    failed to load, e.g. missing db / uninitialised schema)."""
    full = get_graph(db_path)
    if full.get("error"):
        return None
    for n in full.get("nodes", []):
        if n.get("id") == node_id:
            return n
    return None
