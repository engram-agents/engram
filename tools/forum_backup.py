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
  on the live DB -- iterdump() iterates rows without a lock, so a concurrent
  write can produce a torn/inconsistent dump.
- Idempotent commits: git status --porcelain is checked before committing; if
  nothing changed since the last backup, the run exits 0 without creating an
  empty commit.
- Derived-data stripping: as of forum hybrid-search (issue #807), forum.db
  carries derived/regenerable data -- posts_fts* shadow tables, vec_posts and
  vec_threads vec0 virtual tables, and embedding BLOB columns on posts/threads.
  The dump strips all of these so git diffs stay small and grow linearly.
  Default (strip) keeps the dump textually clean; --keep-derived restores old
  behaviour for debugging. Regen path after restore: tools/forum_regen_derived.py.
- The snapshot temp DB is mutated before iterdump (NULL embedding columns, drop
  derived tables/triggers). This is safe by construction -- the snapshot is a
  throwaway copy created by the sqlite3 backup API, never the live DB.
- vec0 virtual table removal is extension-free (#1057): vec_posts and vec_threads
  are removed via PRAGMA writable_schema (deleting their sqlite_master rows)
  rather than DROP TABLE, which would require the sqlite-vec extension loaded.
  The plain shadow tables (vec_posts_chunks, etc.) are then dropped normally.
  This preserves the stdlib-only-runs-under-system-python contract so the systemd
  backup unit works without sqlite_vec installed.
- Forum server is NOT stopped during backup -- the sqlite3.backup() API handles
  live-consistency at the C library level.

Restore-safety tripwire: this dump's correctness depends on forum/db.py's
init_db staying user_version-free + IF-NOT-EXISTS idempotent. Verified at
slice-3 implementation time (2026-06-04): forum/db.py has NO PRAGMA user_version
at any point -- neither in SCHEMA_SQL nor in init_db's ALTER guards. The
user_version-free premise holds. If a user_version-gated one-shot migration is
ever added there, this dump must start emitting `PRAGMA user_version = N;` -- see
engram-alpha #781 for the restore-DOA failure class this prevents.

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
import re
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
# Derived-data exclusion constants
# ---------------------------------------------------------------------------
# Tables/prefixes that are derived (regenerable) and must never reach the dump.
# posts_fts matches the FTS5 virtual table + all its shadow tables
#   (posts_fts_data, posts_fts_idx, posts_fts_docsize, posts_fts_config,
#    posts_fts_content if present).
# vec_posts* and vec_threads* match the sqlite-vec vec0 tables + any shadow.
_DERIVED_PREFIXES = ("posts_fts", "vec_posts", "vec_threads")

# SQLite-internal tables that may appear in sqlite_master but carry no
# user-data and are never in the stripped dump.  Excluded from row-count
# parity checks so that snapshot (which may have these tables due to
# AUTOINCREMENT or FTS shadow-table bookkeeping) compares cleanly against
# the restored DB (which never sees them in a stripped dump).
# sqlite_sequence: AUTOINCREMENT sequence tracker -- vec0 shadow tables use
#   AUTOINCREMENT, so creating/dropping them leaves sqlite_sequence present
#   (but empty) in the snapshot; the stripped dump filters it out (B1 fix).
_SQLITE_INTERNAL_TABLES = frozenset({"sqlite_sequence", "sqlite_stat1", "sqlite_stat4"})


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


def _load_vec_best_effort(conn: sqlite3.Connection) -> bool:
    """Load the sqlite-vec extension on *conn* if available.  Returns True on success.

    Best-effort: swallows all exceptions so callers that merely want to avoid
    "no such module: vec0" errors on SELECT/COUNT can call this unconditionally.
    Required before any operation that touches vec0 virtual tables on a
    potentially-vec-bearing connection (test helpers, _verify_dump, etc.).
    """
    try:
        import sqlite_vec  # type: ignore
        conn.enable_load_extension(True)
        sqlite_vec.load(conn)
        conn.enable_load_extension(False)
        return True
    except Exception:
        return False


def _strip_derived_from_snapshot(conn: sqlite3.Connection) -> dict[str, int]:
    """Strip derived data from *conn* (a SNAPSHOT -- safe to mutate).

    The snapshot is a throwaway copy created by the sqlite3 backup API; mutating
    it never touches the live DB.  Steps:
      1. NULL the embedding columns on posts + threads (if they exist).
      2. Drop FTS triggers (posts_fts_insert, posts_fts_delete, posts_fts_update).
      3. Remove vec0 virtual-table declarations extension-free via writable_schema
         (only when vec0 virtual tables exist; vec-less DBs skip this block).
      4. Drop remaining derived tables: vec shadow tables (vec_posts_chunks etc.)
         and posts_fts via normal DROP TABLE IF EXISTS.  Dropping posts_fts also
         removes all its shadow tables automatically.

    vec0 removal detail (#1057): DROP TABLE on a vec0 virtual table requires the
    sqlite-vec extension loaded on the connection.  The systemd backup unit runs
    system python3 (no sqlite_vec), so we instead remove the vec0 virtual-table
    rows directly from sqlite_master (PRAGMA writable_schema=ON) and then drop the
    plain shadow tables normally.  This is extension-free and stdlib-only.
    writable_schema is turned OFF and committed before returning; the block is
    entered only when vec0 virtual tables actually exist.

    FTS5 (posts_fts) is unaffected: FTS5 is compiled into stdlib sqlite3, so
    DROP TABLE posts_fts works under system python3 and is unchanged.

    Returns a stats dict: {embeddings_nulled, triggers_dropped, tables_dropped}.
    tables_dropped counts all removed tables: vec0 virtual + vec shadow + fts.
    """
    stats: dict[str, int] = {
        "embeddings_nulled": 0,
        "triggers_dropped": 0,
        "tables_dropped": 0,
    }

    # --- 1. NULL embedding columns (if present) ---
    post_cols = {r[1] for r in conn.execute("PRAGMA table_info(posts)").fetchall()}
    if "embedding" in post_cols:
        # Count only rows whose embedding was non-NULL before the UPDATE.
        stats["embeddings_nulled"] += conn.execute(
            "SELECT COUNT(*) FROM posts WHERE embedding IS NOT NULL"
        ).fetchone()[0]
        conn.execute("UPDATE posts SET embedding = NULL")

    thread_cols = {r[1] for r in conn.execute("PRAGMA table_info(threads)").fetchall()}
    if "embedding" in thread_cols:
        stats["embeddings_nulled"] += conn.execute(
            "SELECT COUNT(*) FROM threads WHERE embedding IS NOT NULL"
        ).fetchone()[0]
        conn.execute("UPDATE threads SET embedding = NULL")

    conn.commit()

    # --- 2. Drop FTS triggers ---
    # Two-branch OR is belt-and-suspenders by design:
    #   name LIKE 'posts_fts_%' — prefix match covers all standard triggers
    #     (posts_fts_insert, posts_fts_delete, posts_fts_update) and any future
    #     variants that follow the naming convention.
    #   sql LIKE '%posts_fts%' — fallback that catches hypothetical renamed
    #     triggers (e.g. custom names) whose body still references posts_fts.
    # Both branches are kept so a rename doesn't silently leave a dangling trigger.
    triggers = [
        r[0]
        for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='trigger' "
            "AND (name LIKE 'posts_fts_%' OR sql LIKE '%posts_fts%')"
        ).fetchall()
    ]
    for tn in triggers:
        conn.execute(f"DROP TRIGGER IF EXISTS [{tn}]")
        stats["triggers_dropped"] += 1

    # --- 3. Remove vec0 virtual-table declarations via writable_schema (extension-free) ---
    # Dropping a vec0 virtual table normally requires the sqlite-vec extension loaded
    # on the connection; the systemd backup unit runs system python3 without it (#1057).
    # Instead: identify vec0 virtual tables, delete their sqlite_master rows directly
    # (writable_schema=ON), then commit.  The remaining vec shadow tables (plain btree)
    # are dropped normally in step 4.
    # Guard: only enter the writable_schema block when vec0 virtual tables exist, so
    # vec-less DBs never touch writable_schema at all.
    vec0_virtual = [
        r[0]
        for r in conn.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type='table' AND sql LIKE 'CREATE VIRTUAL TABLE%vec0%'"
        ).fetchall()
    ]
    if vec0_virtual:
        conn.execute("PRAGMA writable_schema=ON")
        try:
            for vt in vec0_virtual:
                conn.execute(
                    "DELETE FROM sqlite_master WHERE type='table' AND name=?", (vt,)
                )
                stats["tables_dropped"] += 1
        finally:
            # try/finally so writable_schema is ALWAYS turned back OFF even if a
            # DELETE raises -- leaving it ON would let later statements on this
            # connection corrupt the schema. (Reviewer-fairy suggestion, #1089.)
            conn.execute("PRAGMA writable_schema=OFF")
        conn.commit()

    # --- 4. Drop remaining derived tables via normal DROP TABLE IF EXISTS ---
    # After step 3, vec0 virtual-table rows are gone from sqlite_master, so the
    # remaining vec_posts*/vec_threads* entries are plain shadow tables (btree) --
    # no extension needed to drop them.  posts_fts is an FTS5 virtual table (stdlib,
    # always droppable); dropping it automatically removes its shadow tables.
    # NOTE: these patterns cover the two known vec0 table prefixes (vec_posts,
    # vec_threads). Step 3's detection is name-agnostic (matches any CREATE VIRTUAL
    # TABLE...vec0), so a new vec0 table with a different prefix would have its
    # virtual-table row removed there but its shadow tables left behind here --
    # add its prefix to this query if a new vec0 table is ever introduced.
    derived_tables = [
        r[0]
        for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND (name LIKE 'vec_posts%' OR name LIKE 'vec_threads%' "
            "     OR name LIKE 'posts_fts%')"
        ).fetchall()
    ]
    for tn in derived_tables:
        conn.execute(f"DROP TABLE IF EXISTS [{tn}]")
        stats["tables_dropped"] += 1

    conn.commit()
    return stats


def _is_internal_table_stmt(stmt: str) -> bool:
    """Return True if *stmt* is a SQLite-internal-table statement.

    Matches only statements whose target IS an internal table — anchored at
    the statement keyword + quoted table name so post/thread content that
    merely *mentions* an internal table name cannot trigger a false positive.

    iterdump() emits each statement as a single item (possibly with embedded
    newlines for CREATE TABLE DDL), but INSERT/DELETE statements are always
    single-line.  The start-anchor is therefore robust for the INSERT/DELETE
    cases that carry the silent-data-loss risk.
    """
    s = stmt.lstrip()
    return any(
        s.startswith(f'DELETE FROM "{t}"') or s.startswith(f'INSERT INTO "{t}" ')
        for t in _SQLITE_INTERNAL_TABLES
    )


def _dump_to_sql(snap_path: str, keep_derived: bool = False) -> str:
    """Return the iterdump() output of *snap_path* as a string.

    When keep_derived=False (default): strips embedding columns and derived
    tables/triggers from the snapshot before dumping.  The snapshot is a
    throwaway copy; mutation is safe by construction.

    After stripping, filters any remaining sqlite-internal-table statements
    from the dump.  Background: vec0 shadow tables use AUTOINCREMENT, so
    creating them creates sqlite_sequence in the source DB.  Stripping drops
    the vec tables but leaves sqlite_sequence present-but-empty in the
    snapshot.  iterdump() then emits a bare DELETE FROM "sqlite_sequence";
    with no matching CREATE TABLE ... AUTOINCREMENT in the dump -- restoring
    this into a fresh DB dies with "no such table: sqlite_sequence".

    Filter rule: drop all sqlite-internal-table statements UNLESS the
    stripped snapshot's sqlite_master contains a table with AUTOINCREMENT
    (ground truth -- queried directly, not inferred from dump text).  Today
    no forum base table uses AUTOINCREMENT, so this filter is always
    lossless.  If a future base table adds AUTOINCREMENT, sqlite_sequence
    will be kept because SQLite recreates it automatically on first INSERT
    into any AUTOINCREMENT table, making the sequence rows needed for correct
    behaviour.

    Statement-level filtering: uses _is_internal_table_stmt() which anchors
    the match at the statement keyword + quoted table name.  A post/thread
    whose body_md merely *mentions* an internal table name (e.g.
    "sqlite_sequence") is an INSERT INTO "posts" statement -- its start does
    NOT match the anchor -- so it is kept verbatim.

    When keep_derived=True: dumps everything unchanged (old behaviour, for
    debugging via --keep-derived).
    """
    conn = sqlite3.connect(snap_path)
    # Load vec best-effort so iterdump/row-counts don't raise on vec-bearing DBs
    # in the --keep-derived path (which retains vec0 DDL and needs the extension).
    # The strip path (keep_derived=False) no longer needs the extension: step 3 of
    # _strip_derived_from_snapshot removes vec0 tables extension-free via
    # writable_schema.  This call is a no-op on the strip path (harmless) and
    # required on the keep_derived=True path.
    _load_vec_best_effort(conn)
    try:
        if not keep_derived:
            _strip_derived_from_snapshot(conn)
        # Query ground truth BEFORE closing: does the stripped snapshot contain
        # any AUTOINCREMENT base table?  If yes, sqlite_sequence statements in
        # the dump are valid (SQLite creates sqlite_sequence automatically when
        # a table with AUTOINCREMENT is created, so the sequence rows are
        # meaningful).  If no, all internal-table statements are noise and must
        # be filtered.  Querying sqlite_master here (not scanning dump text)
        # avoids false positives from post/thread content that mentions
        # AUTOINCREMENT.
        # Note: if a future forum base table adds AUTOINCREMENT, this query
        # will return a row and sqlite_sequence filtering is skipped -- correct
        # behaviour, because the sequence rows are then load-bearing.
        #
        # #834: probe for the EXACT 'PRIMARY KEY AUTOINCREMENT' token, not a bare
        # 'AUTOINCREMENT' substring.  SQLite only accepts AUTOINCREMENT in that
        # one position, so a bare substring also matches a column named e.g.
        # 'notautoincrement' or a DDL comment mentioning the keyword -- a false
        # positive that would keep sqlite_sequence statements and make a
        # fresh-DB restore die ("no such table: sqlite_sequence").  sqlite_master
        # stores CREATE sql verbatim (the author's whitespace), so normalize
        # whitespace runs to single spaces before the check ('PRIMARY  KEY' etc).
        if keep_derived:
            has_autoincrement = False
        else:
            has_autoincrement = any(
                "PRIMARY KEY AUTOINCREMENT" in " ".join(row[0].upper().split())
                for row in conn.execute(
                    "SELECT sql FROM sqlite_master"
                    " WHERE type='table' AND sql IS NOT NULL"
                ).fetchall()
            )
        lines = list(conn.iterdump())
    finally:
        conn.close()

    if not keep_derived and not has_autoincrement:
        lines = [ln for ln in lines if not _is_internal_table_stmt(ln)]

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


# ---------------------------------------------------------------------------
# Anchored no-derived-statements guard regex
# ---------------------------------------------------------------------------
# Anchored at the statement keyword + table target so that post/thread content
# which *mentions* "posts_fts" or "vec_posts" in its text does NOT match.
# (e.g. INSERT INTO "posts" VALUES(... 'posts_fts virtual table ...')).
# Only lines whose statement target IS the derived table will match.
_NO_DERIVED_RE = re.compile(
    r'^\s*(CREATE\s+(VIRTUAL\s+)?TABLE\s+(IF\s+NOT\s+EXISTS\s+)?["\']?'
    r'(posts_fts|vec_posts|vec_threads)'
    r'|INSERT\s+INTO\s+["\']?(posts_fts|vec_posts|vec_threads)'
    r'|CREATE\s+TRIGGER\s+["\']?(posts_fts|vec_posts|vec_threads)'
    r'|CREATE\s+TRIGGER\s+\S*\s+.*posts_fts)',
    re.IGNORECASE,
)


def _verify_dump(sql_path: Path, snap_path: str, keep_derived: bool = False) -> bool:
    """Restore *sql_path* into a temp DB and verify correctness.

    When keep_derived=False (default, strip mode):
      - Verifies base-table row parity between the pre-strip snapshot baseline
        and the restored DB (base tables only: excludes posts_fts* / vec_posts*
        / vec_threads*).  The baseline is captured from the snapshot BEFORE
        stripping so that strip-induced base-row loss is detectable.
      - Asserts NO derived-table statements leaked into the dump (anchored
        regex guard on posts_fts, vec_posts, vec_threads).
      - Asserts all-NULL embedding columns in the restored posts + threads (when
        the column exists), confirming embeddings were stripped.

    When keep_derived=True (--keep-derived mode): falls back to comparing all
    table row counts including derived tables (old full-equality behaviour).

    Returns True on success; False (with a clear stderr message) on failure.
    The caller decides process exit -- keeps this function programmatically
    callable without raising SystemExit.
    """
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        verify_db = f.name
    try:
        # --- Capture parity baseline from snapshot BEFORE stripping (S2) ---
        # Loading sqlite-vec first so _row_counts doesn't raise on vec tables.
        snap_conn = sqlite3.connect(snap_path)
        _load_vec_best_effort(snap_conn)
        if not keep_derived:
            try:
                snap_base_counts_pre_strip = {
                    name: cnt
                    for name, cnt in _row_counts(snap_conn).items()
                    if not any(name.startswith(p) for p in _DERIVED_PREFIXES)
                    and name not in _SQLITE_INTERNAL_TABLES
                }
            except sqlite3.OperationalError as exc:
                # Extension unavailable AND vec tables present: cannot count safely.
                # Fail with an actionable message instead of a raw exception.
                print(
                    f"ERROR: --verify failed: cannot read snapshot row counts "
                    f"(sqlite-vec unavailable but vec0 tables exist): {exc}\n"
                    f"  Install sqlite-vec on a host where it is available.",
                    file=sys.stderr,
                )
                snap_conn.close()
                return False
        else:
            try:
                snap_counts_pre_strip = {
                    name: cnt
                    for name, cnt in _row_counts(snap_conn).items()
                    if name not in _SQLITE_INTERNAL_TABLES
                }
            except sqlite3.OperationalError as exc:
                print(
                    f"ERROR: --verify failed: cannot read snapshot row counts "
                    f"(sqlite-vec unavailable but vec0 tables exist): {exc}",
                    file=sys.stderr,
                )
                snap_conn.close()
                return False
        snap_conn.close()

        # Restore dump into temp DB.
        sql_content = sql_path.read_text(encoding="utf-8")
        conn = sqlite3.connect(verify_db)
        # Load vec best-effort on the restore connection too (needed for --keep-derived
        # full-count path when the dump retains vec0 DDL).
        _load_vec_best_effort(conn)
        try:
            try:
                conn.executescript(sql_content)
            except sqlite3.Error as exc:
                print(f"ERROR: --verify failed: dump does not restore: {exc}", file=sys.stderr)
                return False

            if not keep_derived:
                # --- No-derived-statements guard ---
                leaked = [ln for ln in sql_content.splitlines() if _NO_DERIVED_RE.search(ln)]
                if leaked:
                    print(
                        f"ERROR: --verify failed: derived-table statements leaked into dump:\n"
                        + "\n".join(f"  {ln}" for ln in leaked[:3]),
                        file=sys.stderr,
                    )
                    return False

                # --- Base-table parity vs pre-strip snapshot baseline ---
                restored_base_counts = {
                    name: cnt
                    for name, cnt in _row_counts(conn).items()
                    if not any(name.startswith(p) for p in _DERIVED_PREFIXES)
                    and name not in _SQLITE_INTERNAL_TABLES
                }

                if restored_base_counts != snap_base_counts_pre_strip:
                    print(
                        f"ERROR: --verify failed: base-table row-count mismatch\n"
                        f"  snapshot pre-strip (base): {snap_base_counts_pre_strip}\n"
                        f"  restored (base): {restored_base_counts}",
                        file=sys.stderr,
                    )
                    return False

                # --- Embeddings-NULL check ---
                post_cols = {r[1] for r in conn.execute("PRAGMA table_info(posts)").fetchall()}
                if "embedding" in post_cols:
                    non_null = conn.execute(
                        "SELECT COUNT(*) FROM posts WHERE embedding IS NOT NULL"
                    ).fetchone()[0]
                    if non_null > 0:
                        print(
                            f"ERROR: --verify failed: {non_null} posts still have non-NULL "
                            f"embedding in restored DB (stripping failed).",
                            file=sys.stderr,
                        )
                        return False

                thread_cols = {r[1] for r in conn.execute("PRAGMA table_info(threads)").fetchall()}
                if "embedding" in thread_cols:
                    non_null = conn.execute(
                        "SELECT COUNT(*) FROM threads WHERE embedding IS NOT NULL"
                    ).fetchone()[0]
                    if non_null > 0:
                        print(
                            f"ERROR: --verify failed: {non_null} threads still have non-NULL "
                            f"embedding in restored DB (stripping failed).",
                            file=sys.stderr,
                        )
                        return False

                print(
                    f"verify OK: base rows match {restored_base_counts}; "
                    f"no derived statements; embeddings NULL.",
                    file=sys.stderr,
                )
            else:
                # keep_derived mode: full row-count comparison including derived tables.
                restored_counts = {
                    name: cnt
                    for name, cnt in _row_counts(conn).items()
                    if name not in _SQLITE_INTERNAL_TABLES
                }

                if restored_counts != snap_counts_pre_strip:
                    print(
                        f"ERROR: --verify failed: row-count mismatch\n"
                        f"  snapshot: {snap_counts_pre_strip}\n"
                        f"  restored: {restored_counts}",
                        file=sys.stderr,
                    )
                    return False

                print(f"verify OK: row counts match {restored_counts}", file=sys.stderr)

            return True
        finally:
            conn.close()
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
            "correctness (base-table row parity, no derived statements, "
            "embeddings NULL). Exits nonzero if verification fails."
        ),
    )
    parser.add_argument(
        "--packs-dir",
        default=None,
        help=(
            "Path to the forum packs directory to include in the backup "
            "(e.g. /home/agents-shared/forum/packs). "
            "When omitted, packs are NOT copied -- document this gap if packs "
            "are not otherwise backed up."
        ),
    )
    parser.add_argument(
        "--keep-derived",
        action="store_true",
        default=False,
        help=(
            "Dump everything including derived tables (posts_fts*, vec_posts, "
            "vec_threads) and embedding BLOBs -- restores old behaviour for "
            "debugging. Default: strip derived data (correct for production)."
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
            sql_content = _dump_to_sql(snap_path, keep_derived=args.keep_derived)
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
            # snap_path -- base-table comparison needs the snapshot on disk.
            if not _verify_dump(sql_dest, snap_path, keep_derived=args.keep_derived):
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
