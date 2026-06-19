"""Rebuild derived state on a restored forum DB.

Usage:
    python tools/forum_regen_derived.py --db /path/to/forum.db
    python tools/forum_regen_derived.py --db /path/to/forum.db --dry-run

This is the regen-path counterpart to tools/forum_backup.py's derived-data
stripping.  After restoring a stripped dump (forum.sql), run this tool to
rebuild the three layers of derived state that forum_backup strips:

  1. Schema: re-run forum.db.init_db() to recreate posts_fts / vec_posts /
     vec_threads tables and INSERT/DELETE/UPDATE triggers.  init_db is
     idempotent (IF NOT EXISTS everywhere) -- safe to run on a DB that already
     has these tables.

  2. FTS rebuild: INSERT INTO posts_fts(posts_fts) VALUES ('rebuild') to
     re-index all post rows.

     FTS rebuild safety note (the #727 distinction):
     A bare 'rebuild' command re-indexes ALL rows from the content table
     (posts) without any exclusion.  This is SAFE for forum posts because
     the forum has NO retraction-exclusion semantics -- every post in the
     posts table should appear in FTS.  This is distinct from the engram
     nodes_fts case (issue #727), where retracted nodes must be excluded from
     the FTS index: a bare rebuild there would corrupt the exclusions and return
     retracted claims in search results.  Forum posts are never retracted, so
     the rebuild is always correct.

  3. Embedding + centroid backfill: delegated to tools/forum_backfill_embeddings.py.
     That script embeds all posts with embedding IS NULL, recomputes thread
     centroids, and then runs its own FTS rebuild pass.  Running this tool
     calls it as a subprocess (or imports run_backfill directly when available).
     The schema step above ensures posts_fts exists before the backfill runs.

Restore workflow:
    sqlite3 new_forum.db < forum.sql
    python tools/forum_regen_derived.py --db new_forum.db
    # forum_regen_derived delegates embedding backfill to forum_backfill_embeddings.py
"""

from __future__ import annotations

import argparse
import sqlite3
import subprocess
import sys
from pathlib import Path


DEFAULT_DB = "/home/agents-shared/forum/forum.db"


def _repo_root() -> Path:
    """Return the repository root (parent of this script's directory)."""
    return Path(__file__).resolve().parent.parent


def _ensure_repo_in_path() -> None:
    """Add the repo root to sys.path so forum package imports work."""
    root = str(_repo_root())
    if root not in sys.path:
        sys.path.insert(0, root)


def run_regen(db_path: str, dry_run: bool = False) -> dict[str, object]:
    """Rebuild derived state on *db_path*.

    Returns a report dict with keys:
      schema_applied (bool), fts_rebuilt (bool),
      backfill_result (dict from run_backfill or None if dry-run/unavailable),
      backfill_failed (bool)
    """
    _ensure_repo_in_path()

    report: dict[str, object] = {
        "schema_applied": False,
        "fts_rebuilt": False,
        "backfill_result": None,
        "backfill_failed": False,
    }

    # ------------------------------------------------------------------
    # Step 1: Apply schema via init_db (idempotent).
    # Recreates posts_fts, vec_posts, vec_threads, and the FTS triggers.
    # IF NOT EXISTS everywhere -- safe on a DB that already has these.
    # ------------------------------------------------------------------
    print(f"[regen] Step 1: applying schema (init_db) to {db_path!r}.", file=sys.stderr)
    if not dry_run:
        try:
            from forum.db import init_db  # type: ignore
        except ImportError as exc:
            print(
                f"[regen] ERROR: cannot import forum.db.init_db: {exc}",
                file=sys.stderr,
            )
            return report

        conn = sqlite3.connect(db_path)
        conn.execute("PRAGMA foreign_keys = ON")
        try:
            init_db(conn)
            conn.commit()
        finally:
            conn.close()
        report["schema_applied"] = True
        print("[regen] Step 1 complete: schema applied.", file=sys.stderr)
    else:
        print("[regen] Step 1 (dry-run): would call init_db() -- skipping.", file=sys.stderr)
        report["schema_applied"] = True  # dry-run: pretend it would succeed

    # ------------------------------------------------------------------
    # Step 2: Bare FTS rebuild.
    # posts_fts now exists (created by Step 1).  Re-index all posts rows.
    #
    # Safe here (the #727 distinction): forum posts have no retraction-
    # exclusion semantics, so a full rebuild simply re-indexes every row.
    # Contrast with engram nodes_fts (#727): retracted nodes must be
    # excluded from that index, making bare rebuild unsafe there.  Forum
    # has no such constraint -- every post in posts belongs in posts_fts.
    # ------------------------------------------------------------------
    print("[regen] Step 2: rebuilding posts_fts.", file=sys.stderr)
    if not dry_run:
        conn = sqlite3.connect(db_path)
        try:
            try:
                conn.execute("INSERT INTO posts_fts(posts_fts) VALUES ('rebuild')")
                conn.commit()
                fts_count = conn.execute("SELECT COUNT(*) FROM posts_fts").fetchone()[0]
                print(
                    f"[regen] Step 2 complete: posts_fts has {fts_count} rows.",
                    file=sys.stderr,
                )
                report["fts_rebuilt"] = True
            except sqlite3.OperationalError as exc:
                print(
                    f"[regen] WARNING: posts_fts rebuild failed: {exc}",
                    file=sys.stderr,
                )
        finally:
            conn.close()
    else:
        print("[regen] Step 2 (dry-run): would rebuild posts_fts -- skipping.", file=sys.stderr)
        report["fts_rebuilt"] = True  # dry-run: pretend it would succeed

    # ------------------------------------------------------------------
    # Step 3: Embedding + centroid backfill.
    # Delegated to tools/forum_backfill_embeddings.py.
    # Prefer direct import (run_backfill) for efficiency and in-process
    # error reporting; fall back to subprocess if the module cannot be
    # imported (e.g. running outside the repo).
    # ------------------------------------------------------------------
    print("[regen] Step 3: delegating embedding backfill.", file=sys.stderr)
    if dry_run:
        print(
            "[regen] Step 3 (dry-run): would invoke forum_backfill_embeddings.py "
            "--db <db> [--dry-run] -- skipping.",
            file=sys.stderr,
        )
        report["backfill_result"] = {"dry_run": True}
        return report

    # Try direct import first.
    backfill_result: dict[str, object] | None = None
    try:
        # Ensure tools/ directory is importable (for run from project root).
        tools_dir = str(_repo_root() / "tools")
        if tools_dir not in sys.path:
            sys.path.insert(0, tools_dir)

        from forum_backfill_embeddings import run_backfill  # type: ignore

        backfill_result = run_backfill(db_path, dry_run=False)
        print(
            f"[regen] Step 3 complete (direct import): "
            f"posts_embedded={backfill_result.get('posts_embedded')}, "
            f"threads_updated={backfill_result.get('threads_updated')}, "
            f"fts_rows={backfill_result.get('fts_rows_after_rebuild')}.",
            file=sys.stderr,
        )
    except ImportError:
        # Fall back to subprocess.
        backfill_script = _repo_root() / "tools" / "forum_backfill_embeddings.py"
        if not backfill_script.exists():
            print(
                f"[regen] ERROR: cannot find forum_backfill_embeddings.py at "
                f"{backfill_script}; embedding backfill skipped.",
                file=sys.stderr,
            )
            report["backfill_failed"] = True
        else:
            result = subprocess.run(
                [sys.executable, str(backfill_script), "--db", db_path],
                capture_output=False,  # let backfill print its own progress
            )
            if result.returncode != 0:
                print(
                    f"[regen] ERROR: forum_backfill_embeddings.py exited with "
                    f"code {result.returncode}.",
                    file=sys.stderr,
                )
                report["backfill_failed"] = True
            else:
                backfill_result = {"subprocess": True, "returncode": 0}
                print("[regen] Step 3 complete (subprocess).", file=sys.stderr)

    report["backfill_result"] = backfill_result
    return report


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Rebuild forum derived state (schema + FTS + embeddings) after restore."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--db",
        default=DEFAULT_DB,
        help=f"Path to the restored forum.db (default: {DEFAULT_DB}).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        default=False,
        help="Report what would be done without writing anything.",
    )
    args = parser.parse_args()

    db_path = args.db

    if not Path(db_path).exists():
        print(f"ERROR: database not found: {db_path!r}", file=sys.stderr)
        return 1

    report = run_regen(db_path, dry_run=args.dry_run)

    if args.dry_run:
        print("[regen] dry-run complete -- no writes performed.", file=sys.stderr)
    else:
        ok = report.get("schema_applied") and report.get("fts_rebuilt")
        if not ok:
            print("[regen] WARNING: regen completed with errors; check output above.", file=sys.stderr)
            return 1

        # A requested backfill that actually failed must be reflected in exit code.
        # The dry_run path (backfill_result={dry_run: True}) exits 0 -- backfill
        # was explicitly not requested, so no failure.  A missing script or a
        # nonzero subprocess exit both set backfill_failed=True in the report.
        if report.get("backfill_failed"):
            print(
                "[regen] WARNING: embedding backfill failed (see output above); "
                "semantic layer may be incomplete.",
                file=sys.stderr,
            )
            return 1

        print("[regen] regen complete.", file=sys.stderr)

    return 0


if __name__ == "__main__":
    sys.exit(main())
