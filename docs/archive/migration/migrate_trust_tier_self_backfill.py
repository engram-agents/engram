#!/usr/bin/env python3
"""migrate_trust_tier_self_backfill — backfill trust_tier='self' for the
self-anchor person node (pn_* with metadata.is_self=true).

## Background

PR #442 extended TIER_RANK to include 'self' (rank 6) and 'primary_user'
(rank 5) as assignable trust tiers via engram_set_trust_tier. Existing installs
that have a self-anchor pn_* node (created via engram_add_person(is_self=True))
will have trust_tier='unknown' — this migration backfills those nodes to
trust_tier='self'.

Primary_user backfill is intentionally NOT automated: Lei may have multiple
human pn_* nodes that need explicit attestation. Use the blessing ritual
described in the upgrade guide after merge.

## Design

  - Finds all pn_* nodes where json_extract(metadata, '$.is_self') = 1 AND
    trust_tier != 'self'.
  - Sets trust_tier = 'self' for each.
  - Idempotent: WHERE clause excludes already-self-tiered nodes. Re-run is a no-op.
  - Logs each mutation in edit_history with action 'trust_tier_set_migration_self'.

## Usage

    # Preview what would change (no DB writes)
    python tools/migration/migrate_trust_tier_self_backfill.py --dry-run

    # Apply (takes a timestamped backup of knowledge.db before any writes)
    python tools/migration/migrate_trust_tier_self_backfill.py --live [--db PATH]

## Flags

    --db PATH   Path to knowledge.db (default: $ENGRAM_HOME/knowledge.db
                or ~/.engram/knowledge.db).
    --dry-run   Preview all planned changes; no DB writes (default mode).
    --live      Apply the migration. Takes a backup before writing.
"""

import argparse
import json
import os
import shutil
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path


def default_db_path() -> Path:
    base = (
        os.environ.get("ENGRAM_HOME")
        or os.path.expanduser("~/.engram")
    )
    return Path(base) / "knowledge.db"


def _get_db(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def _get_current_turn(conn: sqlite3.Connection) -> int:
    """Return the current turn counter from config (for edit_history logging)."""
    try:
        row = conn.execute(
            "SELECT value FROM config WHERE key = 'current_turn'"
        ).fetchone()
        return int(row["value"]) if row else 0
    except Exception:
        return 0


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def plan(conn: sqlite3.Connection) -> list:
    """Compute the migration plan without writing anything.

    Returns a list of dicts: [{id, current_tier}] for nodes that will be updated.

    Only considers is_current=1 nodes. A superseded self-anchor retains
    is_self=true in metadata but has is_current=0 — the WHERE clause must exclude
    it to prevent two nodes from ending up with trust_tier='self' (singleton
    violation).
    """
    rows = conn.execute(
        """SELECT id, trust_tier
           FROM nodes
           WHERE type = 'person'
             AND json_extract(metadata, '$.is_self') = 1
             AND is_current = 1
             AND (trust_tier IS NULL OR trust_tier != 'self')"""
    ).fetchall()
    return [{"id": row["id"], "current_tier": row["trust_tier"]} for row in rows]


def apply_migration(conn: sqlite3.Connection, migration_plan: list) -> dict:
    """Execute the migration. Caller is responsible for commit/rollback.

    Returns:
        {
          "updated": int,  — number of nodes whose trust_tier was set to 'self'
          "details": list of per-node dicts
        }
    """
    now = _now()
    turn = _get_current_turn(conn)
    details = []

    for entry in migration_plan:
        node_id = entry["id"]
        previous_tier = entry["current_tier"]

        # Re-read to ensure the node is still in the expected state.
        row = conn.execute(
            "SELECT id, trust_tier FROM nodes WHERE id = ? AND type = 'person'",
            (node_id,),
        ).fetchone()
        if not row:
            details.append({"id": node_id, "action": "skipped_not_found"})
            continue
        if row["trust_tier"] == "self":
            details.append({"id": node_id, "action": "skipped_already_self"})
            continue

        # Set trust_tier = 'self'
        conn.execute(
            "UPDATE nodes SET trust_tier = 'self' WHERE id = ?", (node_id,)
        )

        # Log in edit_history
        try:
            conn.execute(
                """INSERT INTO edit_history
                   (timestamp, turn, action, node_id, node_type, details)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (
                    now, turn, "trust_tier_set_migration_self",
                    node_id, "person",
                    json.dumps({
                        "from_tier": previous_tier,
                        "to_tier": "self",
                        "migration": "migrate_trust_tier_self_backfill",
                    }),
                ),
            )
        except Exception:
            pass  # Never block mutation for logging failure

        details.append({
            "id": node_id,
            "action": "updated",
            "from_tier": previous_tier,
            "to_tier": "self",
        })

    updated = sum(1 for d in details if d.get("action") == "updated")
    return {"updated": updated, "details": details}


def _make_backup(db_path: Path) -> Path:
    """Write a timestamped copy of db_path before any writes. Returns backup path."""
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup_path = db_path.parent / f"{db_path.stem}.pre-trust-tier-self-{ts}{db_path.suffix}"
    shutil.copy2(str(db_path), str(backup_path))
    return backup_path


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Backfill trust_tier='self' for the self-anchor person node.",
        epilog="Default mode is --dry-run. Use --live to apply.",
    )
    parser.add_argument(
        "--db", type=Path, default=default_db_path(),
        help=(
            "Path to knowledge.db "
            "(default: $ENGRAM_HOME/knowledge.db or ~/.engram/knowledge.db)"
        ),
    )
    mode_group = parser.add_mutually_exclusive_group()
    mode_group.add_argument(
        "--dry-run", action="store_true", default=True,
        help="Preview changes without writing (default).",
    )
    mode_group.add_argument(
        "--live", action="store_true", default=False,
        help="Apply migration. Takes a timestamped DB backup before any writes.",
    )
    args = parser.parse_args()
    live = args.live

    if not args.db.exists():
        print(f"ERROR: {args.db} does not exist", file=sys.stderr)
        return 1

    print("== migrate_trust_tier_self_backfill ==")
    print(f"  Database: {args.db}")
    print(f"  Mode:     {'live (--live)' if live else 'dry-run (add --live to apply)'}")
    print()

    conn = _get_db(args.db)
    try:
        migration_plan = plan(conn)
    finally:
        conn.close()

    total = len(migration_plan)
    print(f"Person nodes with is_self=true and trust_tier != 'self': {total}")

    if total == 0:
        print("Nothing to do — no affected nodes found. Migration is idempotent.")
        return 0

    print()
    for entry in migration_plan:
        print(f"  {entry['id']}: trust_tier '{entry['current_tier']}' → 'self'")
    print()

    if not live:
        print("Dry-run complete. Re-run with --live to apply.")
        return 0

    # Live mode: backup + apply.
    backup_path = _make_backup(args.db)
    print(f"Backup written: {backup_path}")

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

    print()
    print("Migration complete.")
    print(f"  Updated: {result['updated']} node(s) → trust_tier='self'")
    print()
    print("Next step:")
    print("  For primary_user backfill, use the blessing ritual described in the")
    print("  upgrade guide — primary_user requires explicit attestation per person.")
    print()
    print(f"  Rollback: cp {backup_path} {args.db}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
