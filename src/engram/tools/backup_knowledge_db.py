"""Dated knowledge.db backup tool — create a WAL-safe SQL snapshot at ~/.engram/db-backup/.

Produces one dated archive per day: knowledge-YYYY-MM-DD.sql.  Skips silently if today's
archive already exists.  Optionally prunes archives older than DAYS days.

Uses engram_backup.dump_stripped (pure-Python, WAL-safe hot backup) — no sqlite3 CLI
dependency.  Stdlib only; no external packages required.

Backup layers: db-backup/ lives inside ~/.engram/ — git-independent (survives .git
corruption; works without git) but NOT off-disk (a full ~/.engram/ loss takes it too).
The per-nap git auto-push is the off-disk layer.  The three layers are complementary.

Usage:
    python3 backup_knowledge_db.py [--db PATH] [--out-dir DIR] [--retain DAYS] [--dry-run]
"""

from __future__ import annotations

import argparse
import datetime
import glob
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import engram_backup  # noqa: E402


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="backup_knowledge_db",
        description="Create a dated WAL-safe SQL snapshot of knowledge.db.",
    )
    p.add_argument(
        "--db",
        default="~/.engram/knowledge.db",
        help="Source database path (default: ~/.engram/knowledge.db).",
    )
    p.add_argument(
        "--out-dir",
        default="~/.engram/db-backup/",
        help="Destination directory for dated archives (default: ~/.engram/db-backup/).",
    )
    p.add_argument(
        "--retain",
        type=int,
        default=0,
        help="If > 0, prune .sql archives older than DAYS days. Default: 0 (keep all).",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Print what would happen; do not create or delete files.",
    )
    return p.parse_args(argv)


def _prune_old_archives(out_dir: str, retain_days: int, dry_run: bool) -> None:
    """Delete knowledge-YYYY-MM-DD.sql files older than retain_days days."""
    cutoff = datetime.date.today() - datetime.timedelta(days=retain_days)
    pattern = os.path.join(out_dir, "knowledge-*.sql")
    for path in sorted(glob.glob(pattern)):
        fname = os.path.basename(path)
        # Parse date from filename: knowledge-YYYY-MM-DD.sql
        try:
            date_str = fname[len("knowledge-"):-len(".sql")]
            file_date = datetime.date.fromisoformat(date_str)
        except ValueError:
            # Filename doesn't match expected pattern — skip.
            continue
        if file_date < cutoff:
            if dry_run:
                print(f"[dry-run] would prune: {path}")
            else:
                os.unlink(path)
                print(f"pruned: {path}")


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)

    db_path = os.path.expanduser(args.db)
    out_dir = os.path.expanduser(args.out_dir)

    # Verify source database exists.
    if not os.path.exists(db_path):
        print(f"error: database not found: {db_path}", file=sys.stderr)
        return 1

    # Create out-dir if absent.
    if not os.path.exists(out_dir):
        if args.dry_run:
            print(f"[dry-run] would create directory: {out_dir}")
        else:
            os.makedirs(out_dir, exist_ok=True)

    today = datetime.date.today().isoformat()  # YYYY-MM-DD
    out_sql_path = os.path.join(out_dir, f"knowledge-{today}.sql")

    # Skip if today's archive already exists.
    if os.path.exists(out_sql_path):
        print(f"backup for {today} already exists, skipping")
        # Still run retention pruning even when skipping the backup.
        if args.retain > 0:
            _prune_old_archives(out_dir, args.retain, args.dry_run)
        return 0

    if args.dry_run:
        print(f"[dry-run] would write backup: {out_sql_path}")
        if args.retain > 0:
            _prune_old_archives(out_dir, args.retain, args.dry_run)
        return 0

    # Perform the backup.
    try:
        stats = engram_backup.dump_stripped(db_path, out_sql_path)
    except Exception as exc:
        print(f"error: backup failed: {exc}", file=sys.stderr)
        return 1

    print(
        f"backup written: {out_sql_path} "
        f"({stats['bytes']} bytes, {stats['statements']} statements)"
    )

    # Retention pruning.
    if args.retain > 0:
        _prune_old_archives(out_dir, args.retain, args.dry_run)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
