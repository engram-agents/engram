#!/usr/bin/env python3
"""migrate_prediction_pairing_edges.py — one-time cleanup for #2252 W6.

PURPOSE
-------
W6 splits the prediction <-> observation_predictive pairing edge out of the
overloaded `supported_by` relation into its own `predicted_by` relation (same
direction: prediction is the SOURCE, the predictive observation is the
TARGET). The forward fix (engram_observation.py) mints `predicted_by` for
every NEW predictive observation from the day this lands. But predictions
minted BEFORE that day still carry the old `supported_by` pairing edge, and
nothing retypes them automatically.

This script finds those old-style pairing edges — `supported_by` edges whose
source is a `prediction` node and whose target is an `observation_predictive`
node — and adds a PARALLEL `predicted_by` edge alongside each one. The old
`supported_by` edge is never deleted; it stays as audit trail (same pattern
as the sibling migrate_stranded_supported_by.py).

Retiring the stale `supported_by` pairing edges entirely is W3's migration,
deliberately not this one (#2252 W6 spec, "Migration" section).

USAGE
-----
    python3 migrate_prediction_pairing_edges.py [--db PATH] [--apply]

    --db PATH     Path to knowledge.db (default: ~/.engram/knowledge.db)
    --apply       Actually write the parallel predicted_by edges. Without
                  this flag, the script only prints a human-readable report
                  — no writes.

SAFETY
------
- Dry-run by default: prints a human-readable report of what would change;
  pass --apply to write. A real-graph mutation should never be the bare-
  command default (same posture as migrate_stranded_supported_by.py and
  migrate_person_nodes.py).
- Idempotent: skips a pairing edge that already has a matching predicted_by
  edge (same source_id, target_id).
- Never deletes or re-types the old supported_by edge — additive only, never
  in-place re-typing (removable: False on supported_by exists precisely to
  stop casual edge mutation; see #2252 W6 spec).
- Each new edge is logged via edit_history with action='predicted_by_migrated'.

SCOPE
-----
Per the W6 spec, the target set is edges where relation='supported_by' AND
source type='prediction' AND target type='observation_predictive'. The query
is install-agnostic: it finds all such edges across the whole graph, not just
a specific node subset.
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


def find_old_style_pairing_edges(conn: sqlite3.Connection) -> list:
    """Return list of old-style prediction-pairing edges still on supported_by.

    An old-style pairing edge is:
        prediction --[supported_by]--> observation_predictive
    (source type='prediction', target type='observation_predictive') that
    does not yet have a matching predicted_by edge (same source_id,
    target_id) — i.e. it has not already been migrated.
    """
    rows = conn.execute(
        """
        SELECT
            e.source_id  AS prediction_id,
            e.target_id  AS observation_id,
            e.created_at AS edge_created_at
        FROM edges e
        JOIN nodes pred ON pred.id = e.source_id AND pred.type = 'prediction'
        JOIN nodes obs  ON obs.id  = e.target_id AND obs.type = 'observation_predictive'
        WHERE e.relation = 'supported_by'
          AND NOT EXISTS (
              SELECT 1 FROM edges e2
              WHERE e2.source_id = e.source_id
                AND e2.target_id = e.target_id
                AND e2.relation = 'predicted_by'
          )
        ORDER BY e.source_id
        """
    ).fetchall()
    return [dict(r) for r in rows]


def run_migration(db_path: str, dry_run: bool = False) -> None:
    """Find and add parallel predicted_by edges for old-style pairings."""
    conn = _get_db(db_path)
    try:
        old_style = find_old_style_pairing_edges(conn)

        if not old_style:
            print("No old-style prediction-pairing supported_by edges found. Nothing to migrate.")
            return

        print(f"Found {len(old_style)} old-style prediction-pairing edge(s):")
        for edge in old_style:
            print(
                f"  {edge['prediction_id']} (prediction)"
                f" --[supported_by]--> {edge['observation_id']} (observation_predictive)"
                f" → will add parallel [predicted_by] edge"
            )

        if dry_run:
            print("\n[DRY RUN] No writes performed.")
            return

        now = _now()
        migrated = 0
        for edge in old_style:
            pred_id = edge["prediction_id"]
            obs_id = edge["observation_id"]

            try:
                conn.execute(
                    "INSERT INTO edges (source_id, target_id, relation, created_at)"
                    " VALUES (?, ?, 'predicted_by', ?)",
                    (pred_id, obs_id, now),
                )
            except sqlite3.IntegrityError:
                # Edge already exists (race or concurrent run) — skip.
                print(f"  SKIP (already exists): {pred_id} → {obs_id}")
                continue

            _log_edit(conn, "predicted_by_migrated", pred_id, "prediction", {
                "predicted_by_target_id": obs_id,
                "migrated_from_relation": "supported_by",
                "migration_script": "migrate_prediction_pairing_edges.py",
            })
            migrated += 1

        conn.commit()
        print(
            f"\nMigration complete: {migrated} parallel predicted_by edge(s) added."
            f" Old supported_by edges preserved as audit trail."
        )
    finally:
        conn.close()


def main():
    parser = argparse.ArgumentParser(
        description="Add parallel predicted_by edges for old-style prediction<->"
                     "observation_predictive pairing edges still on supported_by."
    )
    parser.add_argument(
        "--db",
        default=os.path.expanduser("~/.engram/knowledge.db"),
        help="Path to knowledge.db (default: ~/.engram/knowledge.db)",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Actually write the parallel predicted_by edges. Without this "
             "flag, only a dry-run report is printed and the database is "
             "not modified.",
    )
    args = parser.parse_args()

    if not os.path.exists(args.db):
        print(f"ERROR: Database not found at {args.db}", file=sys.stderr)
        print("Use --db PATH to specify the correct path.", file=sys.stderr)
        sys.exit(1)

    run_migration(args.db, dry_run=not args.apply)


if __name__ == "__main__":
    main()
