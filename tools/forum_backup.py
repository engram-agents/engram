"""Forum DB backup tool — iterdump + git snapshot.

Usage:
    python tools/forum_backup.py [options]

Backs up the live forum SQLite DB to a text SQL dump + copies the audit JSONL,
then commits both into a git repository in the backup directory.

Design notes
------------
- Live-write safety: uses the sqlite3 Connection.backup() API to snapshot the
  live DB into a temp file first. This guarantees the dump sees a consistent
  state even if the forum server is mid-write. Do NOT use iterdump() directly
  on the live DB — iterdump() iterates rows without a lock, so a concurrent
  write can produce a torn/inconsistent dump.
- Idempotent commits: git status --porcelain is checked before committing; if
  nothing changed since the last backup, the run exits 0 without creating an
  empty commit.
- No derived tables: forum.db has no FTS, vec, or embedding tables — the dump
  is complete as-is; no stripping logic is needed.
- Forum server is NOT stopped during backup — the sqlite3.backup() API handles
  live-consistency at the C library level.

Restore-safety tripwire: this dump's correctness depends on forum/db.py's
init_db staying user_version-free + IF-NOT-EXISTS idempotent. If a
user_version-gated one-shot migration is ever added there, this dump must
start emitting `PRAGMA user_version = N;` — see engram-alpha #781 for the
restore-DOA failure class this prevents.

Pack storage backup (#651):
- The pack tarballs and meta.json files live under the forum data directory
  at <data-dir>/packs/<pack-id>/.  These files are already under the forum
  data directory (same root as forum.db), so the backup directory's git repo
  can include them with the --packs-dir option below.
- Default behaviour: the backup tool copies the packs directory alongside
  forum.sql and forum-audit.jsonl when --packs-dir is passed.
- Omitting --packs-dir (the default) leaves packs uncopied; this is safe if
  the packs directory is itself on a redundant filesystem or managed separately,
  but the operator should document that decision explicitly.
"""

from __future__ import annotations

import argparse
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path


# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------
DEFAULT_DB = "/home/agents-shared/forum/forum.db"
DEFAULT_BACKUP_DIR = "/home/agents-shared/forum/backup"
DEFAULT_AUDIT = "/home/agents-shared/forum/forum-audit.jsonl"

# .gitignore content written once when we init the backup git repo
GITIGNORE_CONTENT = "*.db\n*.tmp\n"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _run(args: list[str], cwd: str) -> subprocess.CompletedProcess:
    """Run a subprocess, capturing output.  Raises CalledProcessError on failure."""
    return subprocess.run(
        args,
        cwd=cwd,
        capture_output=True,
        text=True,
        check=True,
    )


def _snapshot_db(db_path: str, tmp_snap: str) -> None:
    """Copy *db_path* to *tmp_snap* via the sqlite3 backup API (live-safe)."""
    src = sqlite3.connect(db_path)
    dst = sqlite3.connect(tmp_snap)
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()


def _dump_to_sql(snap_path: str) -> str:
    """Return the full iterdump() output of *snap_path* as a string."""
    conn = sqlite3.connect(snap_path)
    try:
        lines = list(conn.iterdump())
    finally:
        conn.close()
    return "\n".join(lines) + "\n"


def _write_atomic(content: str, dest: Path) -> None:
    """Write *content* to *dest* atomically (tmp -> os.replace)."""
    tmp = dest.with_suffix(".tmp")
    try:
        tmp.write_text(content, encoding="utf-8")
        os.replace(str(tmp), str(dest))
    except Exception:
        tmp.unlink(missing_ok=True)
        raise


def _git_init(backup_dir: Path) -> None:
    """Init a git repo + write .gitignore if the dir is not already a repo."""
    git_dir = backup_dir / ".git"
    if git_dir.is_dir():
        return

    _run(["git", "init"], cwd=str(backup_dir))
    # Configure a minimal local identity so commits don't fail on hosts
    # without a global git user configured (e.g. shared service accounts).
    _run(["git", "config", "--local", "user.name", "engram-forum-backup"], cwd=str(backup_dir))
    _run(["git", "config", "--local", "user.email", "engram-forum-backup@localhost"], cwd=str(backup_dir))

    gi = backup_dir / ".gitignore"
    if not gi.exists():
        gi.write_text(GITIGNORE_CONTENT, encoding="utf-8")


def _git_commit(backup_dir: Path) -> bool:
    """Stage all changes and commit.  Return True if a commit was made, False if nothing changed."""
    # Stage everything (honoring .gitignore)
    _run(["git", "add", "-A"], cwd=str(backup_dir))

    # Check if there is anything staged
    result = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=str(backup_dir),
        capture_output=True,
        text=True,
        check=True,
    )
    if not result.stdout.strip():
        return False  # nothing changed

    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    _run(["git", "commit", "-m", f"forum backup {ts}"], cwd=str(backup_dir))
    return True


def _row_counts(conn: sqlite3.Connection) -> dict[str, int]:
    """Return a dict of {table_name: row_count} for all user tables."""
    tables = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
    ).fetchall()
    return {
        row[0]: conn.execute(f"SELECT count(*) FROM [{row[0]}]").fetchone()[0]  # noqa: S608
        for row in tables
    }


def _verify_dump(sql_path: Path, snap_path: str) -> bool:
    """Restore *sql_path* into a temp DB and assert row-counts match the snapshot.

    Returns True on match; False (with a clear stderr message) if the dump is
    corrupt or row-counts don't match. The caller decides process exit — keeps
    this function programmatically callable without raising SystemExit.
    """
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        verify_db = f.name
    try:
        # Restore dump into temp DB. A corrupt/truncated dump surfaces as a
        # sqlite3 error here — report cleanly instead of raising a traceback.
        sql_content = sql_path.read_text(encoding="utf-8")
        conn = sqlite3.connect(verify_db)
        try:
            conn.executescript(sql_content)
            restored_counts = _row_counts(conn)
        except sqlite3.Error as exc:
            print(f"ERROR: --verify failed: dump does not restore: {exc}", file=sys.stderr)
            return False
        finally:
            conn.close()

        # Compare with snapshot row counts
        snap_conn = sqlite3.connect(snap_path)
        try:
            snap_counts = _row_counts(snap_conn)
        finally:
            snap_conn.close()

        if restored_counts != snap_counts:
            print(
                f"ERROR: --verify failed: row-count mismatch\n"
                f"  snapshot: {snap_counts}\n"
                f"  restored: {restored_counts}",
                file=sys.stderr,
            )
            return False

        print(f"verify OK: row counts match {restored_counts}", file=sys.stderr)
        return True
    finally:
        try:
            os.unlink(verify_db)
        except OSError:
            pass


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(
        description="Back up the forum DB (iterdump + git snapshot).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--db",
        default=DEFAULT_DB,
        help=f"Path to forum.db (default: {DEFAULT_DB})",
    )
    parser.add_argument(
        "--backup-dir",
        default=DEFAULT_BACKUP_DIR,
        help=f"Backup output directory (default: {DEFAULT_BACKUP_DIR})",
    )
    parser.add_argument(
        "--audit",
        default=DEFAULT_AUDIT,
        help=(
            f"Path to forum-audit.jsonl to copy (default: {DEFAULT_AUDIT}). "
            "Skipped with a notice if the file does not exist."
        ),
    )
    parser.add_argument(
        "--verify",
        action="store_true",
        help=(
            "After writing forum.sql, restore it into a temp DB and verify "
            "row-counts match the snapshot. Exits nonzero if they don't match."
        ),
    )
    parser.add_argument(
        "--packs-dir",
        default=None,
        help=(
            "Path to the forum packs directory to include in the backup "
            "(e.g. /home/agents-shared/forum/packs). "
            "When omitted, packs are NOT copied — document this gap if packs "
            "are not otherwise backed up."
        ),
    )
    args = parser.parse_args()

    db_path = args.db
    backup_dir = Path(args.backup_dir)
    audit_path = args.audit
    packs_dir_src = args.packs_dir

    # --- validate source DB --------------------------------------------------
    if not os.path.isfile(db_path):
        print(f"ERROR: forum DB not found: {db_path}", file=sys.stderr)
        return 1

    # --- ensure backup dir exists --------------------------------------------
    try:
        backup_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        print(f"ERROR: cannot create backup dir {backup_dir}: {exc}", file=sys.stderr)
        return 1

    # --- snapshot the live DB via backup API (live-write-safe) ---------------
    with tempfile.NamedTemporaryFile(
        suffix=".db", dir=str(backup_dir), delete=False
    ) as f:
        snap_path = f.name

    try:
        try:
            _snapshot_db(db_path, snap_path)
        except sqlite3.Error as exc:
            print(f"ERROR: DB snapshot failed: {exc}", file=sys.stderr)
            return 1

        # --- dump the snapshot to SQL ----------------------------------------
        try:
            sql_content = _dump_to_sql(snap_path)
        except sqlite3.Error as exc:
            print(f"ERROR: iterdump failed: {exc}", file=sys.stderr)
            return 1

        # --- write forum.sql atomically ---------------------------------------
        sql_dest = backup_dir / "forum.sql"
        try:
            _write_atomic(sql_content, sql_dest)
        except OSError as exc:
            print(f"ERROR: writing forum.sql failed: {exc}", file=sys.stderr)
            return 1

        # --- optional verify pass --------------------------------------------
        if args.verify:
            # Intentional order: verify BEFORE the finally-cleanup removes
            # snap_path — the row-count comparison needs the snapshot on disk.
            if not _verify_dump(sql_dest, snap_path):
                return 1

    finally:
        # Clean up the snapshot temp file
        try:
            os.unlink(snap_path)
        except OSError:
            pass

    # --- copy audit JSONL (optional) -----------------------------------------
    if os.path.isfile(audit_path):
        audit_dest = backup_dir / "forum-audit.jsonl"
        try:
            shutil.copy2(audit_path, str(audit_dest))
        except OSError as exc:
            print(f"ERROR: copying audit log failed: {exc}", file=sys.stderr)
            return 1
    else:
        print(
            f"NOTE: audit log not found at {audit_path} -- skipping (not an error).",
            file=sys.stderr,
        )

    # --- copy packs directory (optional) -------------------------------------
    if packs_dir_src is not None:
        if os.path.isdir(packs_dir_src):
            packs_dest = backup_dir / "packs"
            try:
                # copytree with dirs_exist_ok=True merges into an existing dest.
                shutil.copytree(
                    packs_dir_src,
                    str(packs_dest),
                    dirs_exist_ok=True,
                )
                print(
                    f"NOTE: packs directory copied from {packs_dir_src}.",
                    file=sys.stderr,
                )
            except OSError as exc:
                print(f"ERROR: copying packs directory failed: {exc}", file=sys.stderr)
                return 1
        else:
            print(
                f"NOTE: packs directory not found at {packs_dir_src} -- skipping.",
                file=sys.stderr,
            )
    else:
        print(
            "NOTE: --packs-dir not set; pack tarballs are NOT included in this backup. "
            "Pass --packs-dir <path> to include them.",
            file=sys.stderr,
        )

    # --- git init (idempotent) -----------------------------------------------
    try:
        _git_init(backup_dir)
    except subprocess.CalledProcessError as exc:
        print(
            f"ERROR: git init failed: {exc.stderr.strip() or exc}",
            file=sys.stderr,
        )
        return 1

    # --- commit (skip if nothing changed) ------------------------------------
    try:
        committed = _git_commit(backup_dir)
    except subprocess.CalledProcessError as exc:
        print(
            f"ERROR: git commit failed: {exc.stderr.strip() or exc}",
            file=sys.stderr,
        )
        return 1

    if committed:
        print("forum backup committed.", file=sys.stderr)
    else:
        print("forum backup: no changes since last run -- nothing committed.", file=sys.stderr)

    return 0


if __name__ == "__main__":
    sys.exit(main())
