#!/usr/bin/env python3
"""migrate_stranded_supported_by.py — one-time cleanup for #1587.

PURPOSE
-------
The forward fix (PR #1281, merged 2026-06-22) ensures that cs/ls supported_by
edges are re-rooted to the current person successor when a person node is
superseded. But edges that were already stranded BEFORE #1281 landed are not
covered by the forward fix.

This script finds those stranded edges — supported_by edges pointing at a
non-current (superseded) person node where a current successor exists — and
adds a parallel re-rooted edge to the successor. Old edges are preserved as
audit trail (same as the about-migration pattern in engram_revision.py).

USAGE
-----
    python3 migrate_stranded_supported_by.py [--db PATH] [--apply]

    --db PATH     Path to knowledge.db (default: ~/.engram/knowledge.db)
    --apply       Actually write the re-rooted edges. Without this flag, the
                  script only prints a human-readable report — no writes.

SAFETY
------
- Dry-run by default: prints a human-readable report of what would change;
  pass --apply to write. Matches the human-review-first posture of the
  sibling migrate_person_nodes.py — a real-graph mutation should never be
  the bare-command default (false assurance from a mis-positioned safety
  signal is worse than none).
- Idempotent: skips edges that already exist (source → successor, supported_by).
- Never deletes old edges (same pattern as the forward fix at line 722 of
  engram_revision.py).
- Each new edge is logged via edit_history with action='supported_by_migrated'
  (same action name used by the forward fix for consistency).

SCOPE
-----
Per the spec (#1587), the primary targets are cornerstone/lesson nodes whose
supported_by edges point at a superseded person node. The query is install-agnostic: it finds all
stranded supported_by edges across ALL person nodes in the graph.
"""

import argparse
import json
import os
import sqlite3
import sys
from datetime import datetime, timezone


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _log_edit(conn: sqlite3.Connection, action: str, node_id: str,
               node_type: str, details: dict):
    """Best-effort audit log — mirrors engram_core._log_edit."""
    try:
        conn.execute(
            """INSERT INTO edit_history (timestamp, turn, action, node_id, node_type, details)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (_now(), 0, action, node_id, node_type, json.dumps(details)),
        )
    except Exception:
        pass


def _get_db(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


def find_stranded_edges(conn: sqlite3.Connection) -> list:
    """Return list of stranded supported_by edges with their successor.

    A stranded edge is:
      source_id --[supported_by]--> old_person_id
    where old_person.is_current = 0, old_person.type = 'person', AND there
    exists a current successor reached via: successor --[supersedes]--> old_person
    where successor.is_current = 1.

    Supersedes edge direction (engram_revision.py:647): the NEW node is the
    source, the OLD node is the target — i.e. new --[supersedes]--> old.
    So the successor is sup.source_id and the old node is sup.target_id.
    """
    rows = conn.execute(
        """
        SELECT
            e.source_id,
            e.target_id  AS old_person_id,
            e.created_at AS edge_created_at,
            sup.source_id AS successor_id
        FROM edges e
        -- e targets a non-current person node
        JOIN nodes old_pn ON old_pn.id = e.target_id
            AND old_pn.is_current = 0
            AND old_pn.type = 'person'
        -- find the supersedes edge: new_node --[supersedes]--> old_pn
        JOIN edges sup ON sup.target_id = old_pn.id
            AND sup.relation = 'supersedes'
        -- successor must be current
        JOIN nodes succ ON succ.id = sup.source_id
            AND succ.is_current = 1
        -- only supported_by edges
        WHERE e.relation = 'supported_by'
          -- skip if re-rooted edge already exists (i.e. an edge to the
          -- SUCCESSOR, sup.source_id — NOT sup.target_id, which is just
          -- old_pn.id again and would self-match e, making this NOT EXISTS
          -- always false and the whole query always empty)
          AND NOT EXISTS (
              SELECT 1 FROM edges e2
              WHERE e2.source_id = e.source_id
                AND e2.target_id = sup.source_id
                AND e2.relation = 'supported_by'
          )
        ORDER BY e.source_id
        """
    ).fetchall()
    return [dict(r) for r in rows]


def run_migration(db_path: str, dry_run: bool = False) -> None:
    """Find and re-root stranded supported_by edges."""
    conn = _get_db(db_path)
    try:
        stranded = find_stranded_edges(conn)

        if not stranded:
            print("No stranded supported_by edges found. Nothing to migrate.")
            return

        print(f"Found {len(stranded)} stranded supported_by edge(s):")
        for edge in stranded:
            src_row = conn.execute(
                "SELECT id, type FROM nodes WHERE id = ?", (edge["source_id"],)
            ).fetchone()
            src_type = src_row["type"] if src_row else "unknown"
            print(
                f"  {edge['source_id']} ({src_type})"
                f" --[supported_by]--> {edge['old_person_id']} (superseded)"
                f" → will re-root to {edge['successor_id']}"
            )

        if dry_run:
            print("\n[DRY RUN] No writes performed.")
            return

        now = _now()
        rerooted = 0
        for edge in stranded:
            src = edge["source_id"]
            old_pn = edge["old_person_id"]
            successor = edge["successor_id"]

            # Insert re-rooted edge.
            try:
                conn.execute(
                    "INSERT INTO edges (source_id, target_id, relation, created_at)"
                    " VALUES (?, ?, 'supported_by', ?)",
                    (src, successor, now),
                )
            except sqlite3.IntegrityError:
                # Edge already exists (race or concurrent run) — skip.
                print(f"  SKIP (already exists): {src} → {successor}")
                continue

            # Determine source node type for _log_edit.
            src_row = conn.execute(
                "SELECT type FROM nodes WHERE id = ?", (src,)
            ).fetchone()
            src_type = src_row["type"] if src_row else "unknown"

            _log_edit(conn, "supported_by_migrated", successor, "person", {
                "supported_by_source_id": src,
                "migrated_from": old_pn,
                "migration_script": "migrate_stranded_supported_by.py",
            })
            rerooted += 1

        conn.commit()
        print(
            f"\nMigration complete: {rerooted} edge(s) re-rooted."
            f" Old edges preserved as audit trail."
        )
    finally:
        conn.close()


def main():
    parser = argparse.ArgumentParser(
        description="Re-root stranded cs/ls supported_by edges to current person successors."
    )
    parser.add_argument(
        "--db",
        default=os.path.expanduser("~/.engram/knowledge.db"),
        help="Path to knowledge.db (default: ~/.engram/knowledge.db)",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Actually write the re-rooted edges. Without this flag, only a "
             "dry-run report is printed and the database is not modified.",
    )
    args = parser.parse_args()

    if not os.path.exists(args.db):
        print(f"ERROR: Database not found at {args.db}", file=sys.stderr)
        print("Use --db PATH to specify the correct path.", file=sys.stderr)
        sys.exit(1)

    run_migration(args.db, dry_run=not args.apply)


if __name__ == "__main__":
    main()
