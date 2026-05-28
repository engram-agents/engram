#!/usr/bin/env python3
"""migrate_db_trust_tier — one-shot DB migration for Layer-1 trust-tier mechanism.

Adds 4 sparse columns to the `nodes` table and backfills trust_tier='unknown'
for all existing pn_* (person) nodes. The server's startup migration handles
new installs automatically; this script is for upgrading an existing DB.

Design: schema-extension only. Zero data-loss risk — all 4 columns are purely
additive. Existing rows are unchanged except pn_* nodes, which get
trust_tier='unknown' (the default tier meaning "no signal yet").

Hard-cut migration with prod-safety guards (per migrate_config_v2/v3 pattern):
  - Dry-run mode prints the planned changes; nothing is written.
  - --live mode creates a timestamped backup BEFORE touching the DB.
  - Idempotent: ALTER TABLE is wrapped in try/except to skip columns that
    already exist. Safe to re-run on an already-migrated DB.

Usage:
    python tools/migrate_db_trust_tier.py --dry-run
    python tools/migrate_db_trust_tier.py --live

Flags:
    --db PATH    Path to knowledge.db (default: $ENGRAM_HOME/knowledge.db
                 or ~/.engram/knowledge.db).
    --dry-run    Show planned changes; don't write anything.
    --live       Apply the migration (required for actual writes).

Post-migration: restart the MCP server and run the per-person blessing ritual
described in ~/.engram/upgrade-guides/v1-trust-tier.md.
"""

import argparse
import os
import shutil
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path


# New columns to add. Each is (col_name, sql_typedef).
NEW_COLUMNS = [
    ("trust_tier",           "TEXT"),
    ("trust_signal_kind",    "TEXT"),
    ("trust_signal_polarity","REAL"),
    ("trust_signal_weight",  "REAL"),
]


def default_db_path() -> Path:
    base = os.environ.get("ENGRAM_HOME") or os.path.expanduser("~/.engram")
    return Path(base) / "knowledge.db"


def get_existing_columns(conn: sqlite3.Connection) -> set:
    return {r[1] for r in conn.execute("PRAGMA table_info(nodes)").fetchall()}


def run_migration(db_path: Path, dry_run: bool) -> int:
    """Run the migration. Returns 0 on success, non-zero on failure."""
    if not db_path.exists():
        print(f"ERROR: {db_path} does not exist", file=sys.stderr)
        return 1

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        existing_cols = get_existing_columns(conn)

        # Determine what needs doing
        cols_to_add = [(col, td) for col, td in NEW_COLUMNS if col not in existing_cols]
        # Gate the pn_count query on column existence: on a fresh DB (before
        # ALTER TABLE runs) the column doesn't exist yet, so querying
        # "WHERE trust_tier IS NULL" would raise OperationalError.  If the
        # column is absent every pn_* row will be backfilled after ALTER TABLE,
        # so the count is simply the total number of person nodes.
        if "trust_tier" in existing_cols:
            pn_count = conn.execute(
                "SELECT COUNT(*) as c FROM nodes WHERE type = 'person' AND trust_tier IS NULL"
            ).fetchone()["c"]
        else:
            pn_count = conn.execute(
                "SELECT COUNT(*) as c FROM nodes WHERE type = 'person'"
            ).fetchone()["c"]

        # Report planned changes
        print(f"== DB migration: Layer-1 trust-tier ==")
        print(f"  DB:   {db_path}")
        print(f"  Mode: {'dry-run' if dry_run else 'live'}")
        print()
        print("Planned changes:")
        if cols_to_add:
            for col, td in cols_to_add:
                print(f"  + ALTER TABLE nodes ADD COLUMN {col} {td}")
        else:
            print("  (all 4 columns already exist)")
        if pn_count > 0:
            print(f"  + UPDATE {pn_count} pn_* rows: trust_tier = 'unknown' (WHERE trust_tier IS NULL)")
        else:
            print("  (trust_tier backfill: all pn_* already have a non-null tier)")
        print()

        if dry_run:
            print("Dry-run complete. Re-run with --live to apply.")
            return 0

        # ── Pre-migration backup ──
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        backup = db_path.with_name(db_path.name + f".pre-migration-{stamp}.bak")
        try:
            shutil.copy2(str(db_path), str(backup))
            print(f"  Backup: {backup}")
        except Exception as e:
            print(f"ERROR: Failed to create backup: {e}", file=sys.stderr)
            return 1

        # ── Schema migration (idempotent per-column) ──
        for col, typedef in NEW_COLUMNS:
            try:
                conn.execute(f"ALTER TABLE nodes ADD COLUMN {col} {typedef}")
                print(f"  + Added column: {col} {typedef}")
            except sqlite3.OperationalError as e:
                if "duplicate column name" in str(e).lower():
                    print(f"  ~ Column already exists, skipping: {col}")
                else:
                    print(f"ERROR: Unexpected error adding column {col}: {e}", file=sys.stderr)
                    conn.close()
                    return 1

        # ── Data backfill (idempotent via WHERE IS NULL) ──
        cursor = conn.execute(
            "UPDATE nodes SET trust_tier = 'unknown' WHERE type = 'person' AND trust_tier IS NULL"
        )
        updated = cursor.rowcount
        conn.commit()
        print(f"  + Backfilled trust_tier='unknown' for {updated} pn_* rows.")
        print()
        print("Schema migration complete.")
        print()
        print("Next steps:")
        print("  Per-person tier blessings are NOT automatic.")
        print("  See ~/.engram/upgrade-guides/v1-trust-tier.md for the interactive")
        print("  ritual with your primary user.")
        print()
        print(f"  Rollback: cp {backup} {db_path}")
        return 0

    finally:
        conn.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--db", type=Path, default=default_db_path(),
                        help="Path to knowledge.db")
    parser.add_argument("--dry-run", action="store_true",
                        help="Show planned changes; don't write anything")
    parser.add_argument("--live", action="store_true",
                        help="Apply the migration (required for actual writes)")
    args = parser.parse_args()

    if not args.dry_run and not args.live:
        print("ERROR: Must specify either --dry-run or --live.", file=sys.stderr)
        print("  --dry-run  Show planned changes without writing.", file=sys.stderr)
        print("  --live     Apply the migration.", file=sys.stderr)
        return 1

    if args.dry_run and args.live:
        print("ERROR: Cannot specify both --dry-run and --live.", file=sys.stderr)
        return 1

    return run_migration(args.db, dry_run=args.dry_run)


if __name__ == "__main__":
    sys.exit(main())
