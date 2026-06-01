#!/usr/bin/env python3
"""migrate_supports_to_supported_by — rename all 'supports' edges to 'supported_by'.

Fixes the direction-inconsistency bug (PR #406): the old 'supports' relation was
created in two incompatible directions across code paths:

  - Cornerstone path: (cornerstone, supporter, 'supports')
    → source=dependent, target=premise. CORRECT direction.
  - Prediction path:  (observation, prediction, 'supports')
    → source=premise, target=dependent. WRONG direction.

The new convention is 'supported_by' with uniform direction:
  (source, target, 'supported_by') = "source depends on target" (source=dependent,
  target=premise), identical to 'derives_from'.

Migration rules (applied per edge with relation='supports'):
  1. If target node type = 'prediction':
       → This is the inverted prediction-path edge.
       → SWAP source_id ↔ target_id AND rename relation to 'supported_by'.
         Result: (prediction_id, observation_id, 'supported_by').
  2. Else:
       → Rename in place: relation = 'supported_by'. Source/target unchanged.

Idempotent: running twice is a no-op (no 'supports' edges remain after first run).

Usage:
    python tools/migration/migrate_supports_to_supported_by.py [--db PATH] [--apply]

Flags:
    --db PATH   Path to knowledge.db (default: $ENGRAM_HOME/knowledge.db
                or ~/.engram/knowledge.db).
    --apply     Execute the migration. Default is dry-run (read-only preview).
"""

import argparse
import os
import sqlite3
import sys
from pathlib import Path


def default_db_path() -> Path:
    base = os.environ.get("ENGRAM_HOME") or os.path.expanduser("~/.engram")
    return Path(base) / "knowledge.db"


def _get_db(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def plan(conn: sqlite3.Connection) -> dict:
    """Return the migration plan without mutating anything.

    Returns:
        {
          "rename_in_place": list of (source_id, target_id),
          "flip_and_rename": list of (old_source_id, old_target_id),
          "total": int,
        }
    """
    rows = conn.execute(
        "SELECT e.source_id, e.target_id, nt.type AS target_type, ns.type AS source_type "
        "FROM edges e "
        "JOIN nodes nt ON nt.id = e.target_id "
        "JOIN nodes ns ON ns.id = e.source_id "
        "WHERE e.relation = 'supports'"
    ).fetchall()

    rename_in_place = []
    flip_and_rename = []

    for row in rows:
        # The inverted (bug-producing) edge is specifically the prediction path's
        # (observation_predictive, prediction) shape — the is_predictive observation
        # handler is the only code path that creates an obs→prediction supports edge,
        # and it types that observation 'observation_predictive' (server.py: node_type
        # = "observation_predictive" if is_predictive). Keying on BOTH endpoints is
        # precise to that one path — a cornerstone supported_by a prediction
        # (source=cornerstone) is already correct-direction and must be renamed in
        # place, never flipped.
        if row["target_type"] == "prediction" and row["source_type"] == "observation_predictive":
            flip_and_rename.append((row["source_id"], row["target_id"]))
        else:
            rename_in_place.append((row["source_id"], row["target_id"]))

    return {
        "rename_in_place": rename_in_place,
        "flip_and_rename": flip_and_rename,
        "total": len(rows),
    }


def apply_migration(conn: sqlite3.Connection, migration_plan: dict) -> dict:
    """Execute the migration. Caller is responsible for commit/rollback.

    Returns:
        {
          "renamed_in_place": int,
          "flipped": int,
          "skipped_collision": list of (source_id, target_id),
          "total_processed": int,
        }
    """
    renamed_count = 0
    flipped_count = 0
    skipped = []

    # 1. Rename-in-place edges: just update the relation name.
    for (src, tgt) in migration_plan["rename_in_place"]:
        conn.execute(
            "UPDATE edges SET relation = 'supported_by' "
            "WHERE source_id = ? AND target_id = ? AND relation = 'supports'",
            (src, tgt),
        )
        renamed_count += 1

    # 2. Flip-and-rename edges: swap source↔target, rename relation.
    #    Old: (observation, prediction, 'supports')
    #    New: (prediction, observation, 'supported_by')
    #
    #    Must handle the UNIQUE(source_id, target_id, relation) constraint:
    #    after the flip the new (prediction, observation, 'supported_by') edge
    #    might already exist if a previous partial migration ran.
    for (old_src, old_tgt) in migration_plan["flip_and_rename"]:
        # old_src = observation_id, old_tgt = prediction_id
        new_src = old_tgt   # prediction becomes source
        new_tgt = old_src   # observation becomes target

        # Check for collision: would the new (new_src, new_tgt, 'supported_by') already exist?
        collision = conn.execute(
            "SELECT 1 FROM edges WHERE source_id = ? AND target_id = ? AND relation = 'supported_by'",
            (new_src, new_tgt),
        ).fetchone()

        if collision:
            # The correctly-directed edge already exists; just delete the stale inverted one.
            conn.execute(
                "DELETE FROM edges WHERE source_id = ? AND target_id = ? AND relation = 'supports'",
                (old_src, old_tgt),
            )
            skipped.append((old_src, old_tgt))
        else:
            conn.execute(
                "UPDATE edges "
                "SET source_id = ?, target_id = ?, relation = 'supported_by' "
                "WHERE source_id = ? AND target_id = ? AND relation = 'supports'",
                (new_src, new_tgt, old_src, old_tgt),
            )
            flipped_count += 1

    return {
        "renamed_in_place": renamed_count,
        "flipped": flipped_count,
        "skipped_collision": skipped,
        "total_processed": renamed_count + flipped_count + len(skipped),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument(
        "--db", type=Path, default=default_db_path(),
        help="Path to knowledge.db (default: $ENGRAM_HOME/knowledge.db or ~/.engram/knowledge.db)",
    )
    parser.add_argument(
        "--apply", action="store_true",
        help="Execute the migration (default: dry-run, read-only preview).",
    )
    args = parser.parse_args()

    if not args.db.exists():
        print(f"ERROR: {args.db} does not exist", file=sys.stderr)
        return 1

    conn = _get_db(args.db)
    try:
        migration_plan = plan(conn)
    finally:
        conn.close()

    total = migration_plan["total"]
    rename_count = len(migration_plan["rename_in_place"])
    flip_count = len(migration_plan["flip_and_rename"])

    print(f"== migrate_supports_to_supported_by ==")
    print(f"  Database: {args.db}")
    print(f"  Mode:     {'live (--apply)' if args.apply else 'dry-run (add --apply to execute)'}")
    print()
    print(f"Edges with relation='supports' found: {total}")
    print(f"  Rename-in-place (cornerstone-style): {rename_count}")
    print(f"  Flip-and-rename (prediction-style):  {flip_count}")
    print()

    if total == 0:
        print("Nothing to do — no 'supports' edges found. Migration is idempotent.")
        return 0

    if migration_plan["rename_in_place"]:
        print("Rename-in-place:")
        for src, tgt in migration_plan["rename_in_place"]:
            print(f"  ({src}, {tgt}, 'supports') → ({src}, {tgt}, 'supported_by')")

    if migration_plan["flip_and_rename"]:
        print("Flip-and-rename (swap source↔target):")
        for src, tgt in migration_plan["flip_and_rename"]:
            print(f"  ({src}, {tgt}, 'supports') → ({tgt}, {src}, 'supported_by')")

    print()

    if not args.apply:
        print("Dry-run complete. Re-run with --apply to execute.")
        return 0

    conn = _get_db(args.db)
    try:
        result = apply_migration(conn, migration_plan)
        conn.commit()
    except Exception as e:
        conn.rollback()
        print(f"ERROR during migration: {e}", file=sys.stderr)
        return 1
    finally:
        conn.close()

    print(f"Migration complete.")
    print(f"  Renamed in place: {result['renamed_in_place']}")
    print(f"  Flipped:          {result['flipped']}")
    if result["skipped_collision"]:
        print(f"  Skipped (collision — correct edge already existed): {len(result['skipped_collision'])}")
        for src, tgt in result["skipped_collision"]:
            print(f"    stale inverted edge ({src}, {tgt}) deleted; ({tgt}, {src}, 'supported_by') already present")
    print(f"  Total processed:  {result['total_processed']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
