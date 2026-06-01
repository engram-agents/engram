#!/usr/bin/env python3
"""migrate_cascade_semantics_v1 — clear false-positive taint/stale markers from
cornerstones and lessons; detect zero-support survivors.

## Background

The old cascade engine marked cornerstones and lessons with `tainted_by` /
`stale_by` entries whenever a supporting node was retracted or superseded,
treating them identically to derivations. This is semantically wrong: a
cornerstone or lesson accumulates *instance-votes* — retracting one instance
does NOT invalidate the pattern if other instances still support it.

This migration (PR A of the cascade-semantics-fix project, 2026-05-28):

  1. Finds every cornerstone/lesson with `tainted_by` or `stale_by` in metadata.
  2. Clears those entries (they were generated under the old — now-incorrect —
     cascade model).
  3. Recounts live support for each affected node (via `supported_by` TARGET +
     `exemplifies` SOURCE edges where the other node is `is_current=1`).
  4. If live support is zero after clearing, sets `support_lost: true` in
     metadata — the pattern has no empirical grounding and needs awake-state
     review.
  5. Logs each mutation in `edit_history` with action `cascade_migration_v1`.

Idempotent: re-running is a no-op (no cs/ls will still have bogus markers
after the first pass; `support_lost` already-set nodes are not double-flagged).

Expected migration footprint on a fresh Ariadne graph (2026-05-28):
  - cs_0003: clears 1 tainted_by entry + 1 stale_by entry; live-support
    count remains >1 → no support_lost flag.
  - Other cornerstones/lessons: sweep expected to find near-zero additional
    affected nodes.

## Usage

    # Preview what would change (no DB writes)
    python tools/migration/migrate_cascade_semantics_v1.py --dry-run

    # Apply (takes a timestamped backup of knowledge.db before any writes)
    python tools/migration/migrate_cascade_semantics_v1.py --live [--db PATH]

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
    base = os.environ.get("ENGRAM_HOME") or os.path.expanduser("~/.engram")
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


def plan(conn: sqlite3.Connection) -> dict:
    """Compute the migration plan without writing anything.

    Returns:
        {
          "affected": list of {id, type, tainted_by, stale_by},
          "total": int,
        }
    where tainted_by / stale_by are the current values that will be cleared.
    """
    rows = conn.execute(
        """SELECT id, type, claim, metadata
           FROM nodes
           WHERE type IN ('cornerstone', 'lesson')
             AND is_current = 1
             AND (metadata LIKE '%"tainted_by"%' OR metadata LIKE '%"stale_by"%')"""
    ).fetchall()

    affected = []
    for row in rows:
        meta = json.loads(row["metadata"] or "{}")
        tainted_by = meta.get("tainted_by", [])
        stale_by = meta.get("stale_by", [])
        if not tainted_by and not stale_by:
            continue  # metadata LIKE matched something else — skip
        affected.append({
            "id": row["id"],
            "type": row["type"],
            "claim": (row["claim"] or "")[:120],
            "tainted_by": tainted_by,
            "stale_by": stale_by,
        })

    return {
        "affected": affected,
        "total": len(affected),
    }


def _count_live_support(conn: sqlite3.Connection, node_id: str) -> int:
    """Count live supporting nodes for a cs/ls via both edge relations.

    Edge direction (§3 of PR-A-SPEC):
      - supported_by: cs/ls ─[supported_by]→ premise (count where TARGET is_current=1)
      - exemplifies:  premise ─[exemplifies]→ cs/ls (count where SOURCE is_current=1)
    """
    # Outgoing supported_by: TARGET must be is_current=1
    sb_live = conn.execute(
        """SELECT COUNT(*) as c
           FROM edges e
           JOIN nodes n ON n.id = e.target_id
           WHERE e.source_id = ?
             AND e.relation = 'supported_by'
             AND n.is_current = 1""",
        (node_id,),
    ).fetchone()["c"]

    # Incoming exemplifies: SOURCE must be is_current=1
    ex_live = conn.execute(
        """SELECT COUNT(*) as c
           FROM edges e
           JOIN nodes n ON n.id = e.source_id
           WHERE e.target_id = ?
             AND e.relation = 'exemplifies'
             AND n.is_current = 1""",
        (node_id,),
    ).fetchone()["c"]

    return sb_live + ex_live


def apply_migration(conn: sqlite3.Connection, migration_plan: dict) -> dict:
    """Execute the migration. Caller is responsible for commit/rollback.

    Returns:
        {
          "cleared_taint": int,   — nodes that had tainted_by cleared
          "cleared_stale": int,   — nodes that had stale_by cleared
          "support_lost_set": int — nodes newly flagged support_lost
          "total_processed": int,
          "details": list of per-node dicts
        }
    """
    cleared_taint = 0
    cleared_stale = 0
    support_lost_set = 0
    now = _now()
    turn = _get_current_turn(conn)
    details = []

    for entry in migration_plan["affected"]:
        node_id = entry["id"]
        node_type = entry["type"]

        # Re-read metadata fresh to avoid working from stale plan data.
        row = conn.execute(
            "SELECT metadata FROM nodes WHERE id = ? AND is_current = 1",
            (node_id,),
        ).fetchone()
        if not row:
            details.append({"id": node_id, "action": "skipped_not_current"})
            continue

        meta = json.loads(row["metadata"] or "{}")
        changed = False
        action_details: dict = {}

        # Step 2: clear tainted_by and stale_by.
        if meta.get("tainted_by"):
            action_details["cleared_tainted_by"] = meta.pop("tainted_by")
            cleared_taint += 1
            changed = True
        if meta.get("stale_by"):
            action_details["cleared_stale_by"] = meta.pop("stale_by")
            cleared_stale += 1
            changed = True
        # Also clear stale_replacement if present (companion to stale_by).
        if "stale_replacement" in meta and action_details.get("cleared_stale_by"):
            action_details["cleared_stale_replacement"] = meta.pop("stale_replacement")

        if not changed:
            details.append({"id": node_id, "action": "skipped_nothing_to_clear"})
            continue

        # Step 3: recount live support after clearing.
        live_support = _count_live_support(conn, node_id)
        action_details["live_support_count"] = live_support

        # Step 4: set support_lost if no live support remains.
        if live_support == 0 and not meta.get("support_lost"):
            meta["support_lost"] = True
            support_lost_set += 1
            action_details["support_lost"] = True

        # Write updated metadata.
        conn.execute(
            "UPDATE nodes SET metadata = ? WHERE id = ?",
            (json.dumps(meta), node_id),
        )

        # Step 5: log in edit_history so the audit trail records why these
        # markers vanished (action = cascade_migration_v1).
        try:
            conn.execute(
                """INSERT INTO edit_history
                   (timestamp, turn, action, node_id, node_type, details)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (now, turn, "cascade_migration_v1", node_id, node_type,
                 json.dumps(action_details)),
            )
        except Exception:
            pass  # Never block mutation for logging failure

        details.append({
            "id": node_id,
            "type": node_type,
            "action": "migrated",
            **action_details,
        })

    return {
        "cleared_taint": cleared_taint,
        "cleared_stale": cleared_stale,
        "support_lost_set": support_lost_set,
        "total_processed": len(details),
        "details": details,
    }


def _make_backup(db_path: Path) -> Path:
    """Write a timestamped copy of db_path before any writes. Returns backup path."""
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup_path = db_path.parent / f"{db_path.stem}.pre-cascade-migration-v1-{ts}{db_path.suffix}"
    shutil.copy2(str(db_path), str(backup_path))
    return backup_path


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Clear false-positive taint/stale markers from cornerstones/lessons.",
        epilog="Default mode is --dry-run. Use --live to apply.",
    )
    parser.add_argument(
        "--db", type=Path, default=default_db_path(),
        help="Path to knowledge.db (default: $ENGRAM_HOME/knowledge.db or ~/.engram/knowledge.db)",
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

    print("== migrate_cascade_semantics_v1 ==")
    print(f"  Database: {args.db}")
    print(f"  Mode:     {'live (--live)' if live else 'dry-run (add --live to apply)'}")
    print()

    conn = _get_db(args.db)
    try:
        migration_plan = plan(conn)
    finally:
        conn.close()

    total = migration_plan["total"]
    print(f"Cornerstones/lessons with false-positive taint/stale markers: {total}")
    if total == 0:
        print("Nothing to do — no affected nodes found. Migration is idempotent.")
        return 0

    print()
    for entry in migration_plan["affected"]:
        parts = []
        if entry["tainted_by"]:
            parts.append(f"tainted_by: {entry['tainted_by']}")
        if entry["stale_by"]:
            parts.append(f"stale_by: {entry['stale_by']}")
        print(f"  {entry['id']} ({entry['type']}): {'; '.join(parts)}")
        print(f"    claim: {entry['claim']!r}")
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
    print(f"  Cleared tainted_by entries:  {result['cleared_taint']} node(s)")
    print(f"  Cleared stale_by entries:    {result['cleared_stale']} node(s)")
    print(f"  Newly flagged support_lost:  {result['support_lost_set']} node(s)")
    print(f"  Total processed:             {result['total_processed']}")

    if result["support_lost_set"] > 0:
        print()
        print("WARNING: Some nodes were flagged support_lost (zero live empirical support).")
        print("  Review these via engram_reflect or engram_diagnose in the next awake session.")
        support_lost_nodes = [
            d for d in result["details"]
            if d.get("support_lost")
        ]
        for n in support_lost_nodes:
            print(f"    {n['id']} ({n.get('type', '?')})")

    return 0


if __name__ == "__main__":
    sys.exit(main())
