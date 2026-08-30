"""Content-aware knowledge.db backup tool — create a WAL-safe SQL snapshot at ~/.engram/db-backup/.

Produces knowledge-YYYY-MM-DD.sql for the first backup on a given local day, or
knowledge-YYYY-MM-DD-2.sql, -3.sql, ... for the second/third/... backup on the
SAME local day — a same-day file is never overwritten and never causes a lost
snapshot.

Skip decisions are content-aware, not filename-aware (#2151): before backing
up, the tool reads a content watermark (row count + newest timestamp) for
`nodes` (REQUIRED — a DB without it isn't a valid backup target, and reading
it errors loud) plus `edges` and `edit_history` (OPTIONAL/opportunistic — a
DB missing either just degrades to a weaker watermark, #2244, never a
refusal to back up) from the live DB via a read-only connection — WAL-safe,
never a file mtime — and compares it against a small `.watermark.json`
sidecar left by the last successful backup. A skip requires ALL of: a
persisted watermark exists, it matches the live watermark, AND the archive
file it names is still present on disk (a watermark match alone is not
enough — see #2235 review, below). A same-day file that predates the
current DB state, has no matching watermark, or whose named archive was
deleted, is never trusted as "already covers this state" and a fresh
archive is written instead. Pass --force to always take a real backup
regardless of the watermark. Optionally prunes archives older than DAYS
days (suffixed archives prune by the same date as their unsuffixed
sibling).

Watermark coverage is deliberately partial, not a full-database fingerprint
— know its blind spots before trusting a "skipping" line:
- It covers `nodes`, `edges`, and `edit_history` (routine graph-building
  activity — filing observations/derivations, wiring edges — touches the
  first two directly; `edit_history` is ENGRAM's own append-only audit log
  of tool-mediated mutations, including the pure-in-place-UPDATE case —
  supersede/retract — that inserting no new node or edge row would
  otherwise hide, since every ENGRAM write path that mutates a row also
  appends an edit_history row). Other tables (`focus_sets`,
  `diagnostic_history`, `tool_timing`, ...) are invisible to it; a change
  confined to one of them produces a false "up to date" skip.
- `edit_history` coverage is bounded to TOOL-MEDIATED mutations — a raw SQL
  UPDATE against `nodes`/`edges` outside ENGRAM's own write paths (direct
  DB surgery) leaves no edit_history row either and is still invisible.
  Direct-DB-surgery workflows take their own backup by protocol; this tool
  is not meant to substitute for that discipline.
- A DB lacking `edges` or `edit_history` entirely (any DB not opened by a
  current-version ENGRAM server, or a non-ENGRAM SQLite file) degrades to a
  weaker watermark covering only what's actually present — it never makes
  the tool refuse to back up (#2244; every other decision in this file
  fails toward backing up, and this is that same direction applied to
  optional-table absence).
--force is the escape hatch for any of the above: it bypasses the watermark
entirely and always takes a real backup.

Uses engram_backup.dump_stripped (pure-Python, WAL-safe hot backup) — no sqlite3 CLI
dependency.  Stdlib only; no external packages required.

Backup layers: db-backup/ lives inside ~/.engram/ — git-independent (survives .git
corruption; works without git) but NOT off-disk (a full ~/.engram/ loss takes it too).
The per-nap git auto-push is the off-disk layer.  The three layers are complementary.

Usage:
    python3 backup_knowledge_db.py [--db PATH] [--out-dir DIR] [--retain DAYS] [--dry-run] [--force]
"""

from __future__ import annotations

import argparse
import datetime
import glob
import json
import os
import re
import sqlite3
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import engram_backup  # noqa: E402


_WATERMARK_FILENAME = ".watermark.json"
_ARCHIVE_RE = re.compile(r"^knowledge-(\d{4}-\d{2}-\d{2})(-\d+)?\.sql$")


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
    p.add_argument(
        "--force",
        action="store_true",
        help="Skip the watermark comparison and always perform a real backup "
        "(still suffix-on-collision if today's archive already exists).",
    )
    return p.parse_args(argv)


def _prune_old_archives(out_dir: str, retain_days: int, dry_run: bool) -> None:
    """Delete knowledge-YYYY-MM-DD[-N].sql files older than retain_days days.

    A suffixed same-day archive (knowledge-YYYY-MM-DD-2.sql, ...) prunes by
    the SAME date as its unsuffixed sibling -- the suffix is not a date
    component, just a same-day collision counter.
    """
    cutoff = datetime.date.today() - datetime.timedelta(days=retain_days)
    pattern = os.path.join(out_dir, "knowledge-*.sql")
    for path in sorted(glob.glob(pattern)):
        fname = os.path.basename(path)
        m = _ARCHIVE_RE.match(fname)
        if not m:
            # Filename doesn't match expected pattern — skip.
            continue
        try:
            file_date = datetime.date.fromisoformat(m.group(1))
        except ValueError:
            continue
        if file_date < cutoff:
            if dry_run:
                print(f"[dry-run] would prune: {path}")
            else:
                os.unlink(path)
                print(f"pruned: {path}")


def _watermark_path(out_dir: str) -> str:
    return os.path.join(out_dir, _WATERMARK_FILENAME)


def _probe_optional_table_watermark(conn: sqlite3.Connection, table: str, timestamp_col: str) -> tuple:
    """COUNT(*)/MAX(<timestamp_col>) for an OPTIONAL watermark table, or
    (None, None) if the table doesn't exist.

    `table`/`timestamp_col` are always this file's own hardcoded literals
    (see the two call sites below) -- never user/CLI input -- so the f-string
    SQL construction here is not an injection surface.

    #2244: a DB that has never had `table` created (any DB not opened by a
    current-version ENGRAM server, or a non-ENGRAM SQLite file passed via
    --db) must not make the whole watermark read -- and therefore the whole
    backup -- fail. (None, None) degrades gracefully rather than raising:
    _watermarks_match compares it structurally like any other value, so a
    persisted watermark that also has (None, None) for this table still
    matches (as much confidence as is available), while a live (None, None)
    against a persisted REAL count correctly mismatches and forces a real
    backup -- the same fail-toward-backing-up direction as every other
    watermark decision in this file, never fail-closed.
    """
    try:
        return conn.execute(f"SELECT COUNT(*), MAX({timestamp_col}) FROM {table}").fetchone()
    except sqlite3.OperationalError as exc:
        if "no such table" not in str(exc):
            raise
        return (None, None)


def _read_live_watermark(db_path: str) -> dict:
    """Read-only content watermark from the live DB: {row_count,
    max_created_at} for `nodes` (REQUIRED -- a DB without it isn't a valid
    target and the read fails loud), plus {edge_count, edge_max_created_at}
    for `edges` and {history_count, history_max_timestamp} for
    `edit_history` (both OPTIONAL/opportunistic -- see
    _probe_optional_table_watermark).

    Opens a read-only URI connection (never a write lock, never interferes
    with a live server) and queries through SQLite's own reader, which
    transparently merges WAL content -- correct even when writes have landed
    in the WAL sidecar and not yet been checkpointed into the main file (the
    reason this must never be approximated via file mtimes; see module
    docstring / #2151). Covers `edges` as well as `nodes` (#2235 review: a
    change confined to `edges` -- e.g. a new derivation edge with no new
    node -- was invisible to a nodes-only watermark and produced a false
    "up to date" skip). Also covers `edit_history` (#2235 review, round 2):
    ENGRAM's own append-only audit log of tool-mediated mutations, which
    gets a new row on every supersede/retract even when that mutation is a
    pure in-place UPDATE with no new node or edge row -- closing the last
    gap `nodes`+`edges` alone left open for tool-mediated writes. `edges`
    and `edit_history` are probed independently rather than in one query,
    so a DB missing one (or both) degrades to a weaker watermark instead of
    the tool refusing to back up at all (#2244 -- every other decision in
    this file fails toward backing up; querying an absent auxiliary table
    unconditionally was the one that failed closed).
    """
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        row_count, max_created_at = conn.execute(
            "SELECT COUNT(*), MAX(created_at) FROM nodes"
        ).fetchone()
        edge_count, edge_max_created_at = _probe_optional_table_watermark(conn, "edges", "created_at")
        history_count, history_max_timestamp = _probe_optional_table_watermark(conn, "edit_history", "timestamp")
    finally:
        conn.close()
    return {
        "row_count": row_count,
        "max_created_at": max_created_at,
        "edge_count": edge_count,
        "edge_max_created_at": edge_max_created_at,
        "history_count": history_count,
        "history_max_timestamp": history_max_timestamp,
    }


_PERSISTED_WATERMARK_REQUIRED_KEYS = (
    "row_count", "max_created_at",
    "edge_count", "edge_max_created_at",
    "history_count", "history_max_timestamp",
)


def _load_persisted_watermark(out_dir: str) -> dict | None:
    """Return the watermark left by the last successful backup, or None.

    Missing or unparseable sidecar is treated as "no prior watermark" --
    fail toward backing up, never toward silently skipping. A sidecar
    written by an older version of this tool that predates a watermark
    field this version computes (e.g. pre-#2235-round-2: no
    history_count/history_max_timestamp; pre-#2235-round-1: also no
    edge_count/edge_max_created_at) fails this same check and is likewise
    treated as absent -- it cannot attest to coverage it never computed, so
    the safe move is one fresh backup under the new watermark shape, not
    trusting it as a match.
    """
    path = _watermark_path(out_dir)
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or any(k not in data for k in _PERSISTED_WATERMARK_REQUIRED_KEYS):
        return None
    return data


def _watermarks_match(persisted: dict, live: dict) -> bool:
    return all(persisted.get(k) == live.get(k) for k in _PERSISTED_WATERMARK_REQUIRED_KEYS)


def _persisted_archive_exists(out_dir: str, persisted: dict) -> bool:
    """Does the archive file the persisted watermark claims to cover still
    exist on disk?

    A watermark match alone is not sufficient grounds to skip -- if the
    archive itself is gone (deleted, moved, never actually written), the
    skip has no real backup behind it. Reproduced live (#2235 review): back
    up, delete the .sql, keep .watermark.json, re-run -> "backup up to
    date... skipping" with rc=0 and ZERO archives on disk -- #2151's exact
    failure shape, recurring through a different mechanism. Control:
    deleting the sidecar too makes the same invocation write a real archive,
    confirming the sidecar (not something else) was what caused the skip.

    Passed through os.path.basename() before joining -- normally `archive`
    is always a bare filename (`_write_watermark` writes it via
    os.path.basename(target_path)), but the sidecar is user-editable JSON;
    os.path.join silently discards out_dir if `archive` were ever an
    absolute path, so basename() keeps the check confined to out_dir even
    against a hand-edited or corrupted sidecar (reviewer-fairy finding,
    #2235 round-2 review).
    """
    archive = persisted.get("archive")
    if not archive:
        return False
    return os.path.exists(os.path.join(out_dir, os.path.basename(archive)))


def _write_watermark(out_dir: str, live_watermark: dict, archive_filename: str) -> None:
    with open(_watermark_path(out_dir), "w", encoding="utf-8") as f:
        json.dump(
            {
                "row_count": live_watermark["row_count"],
                "max_created_at": live_watermark["max_created_at"],
                "edge_count": live_watermark["edge_count"],
                "edge_max_created_at": live_watermark["edge_max_created_at"],
                "history_count": live_watermark["history_count"],
                "history_max_timestamp": live_watermark["history_max_timestamp"],
                "archive": archive_filename,
            },
            f,
        )


def _next_available_archive_path(out_dir: str, today: str) -> str:
    """First unused knowledge-{today}[-N].sql path.

    Unsuffixed (knowledge-{today}.sql) if free; otherwise the first unused
    -2, -3, ... suffix.  A same-day archive is never overwritten.
    """
    base = os.path.join(out_dir, f"knowledge-{today}.sql")
    if not os.path.exists(base):
        return base
    n = 2
    while True:
        candidate = os.path.join(out_dir, f"knowledge-{today}-{n}.sql")
        if not os.path.exists(candidate):
            return candidate
        n += 1


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

    # Content watermark of the live DB -- read-only, WAL-safe.  Computed up
    # front regardless of --force so a real backup (below) can persist the
    # exact state it captured.
    try:
        live_watermark = _read_live_watermark(db_path)
    except Exception as exc:
        print(f"error: could not read watermark from {db_path}: {exc}", file=sys.stderr)
        return 1

    # Content-aware skip: only a genuine watermark match against the last
    # successful backup's sidecar, AND that backup's archive file still
    # being on disk, means "nothing new since the last backup, and it's
    # still there". A same-day file with no matching watermark, a watermark
    # whose archive went missing, or --force, is NOT trusted as already
    # covering the current state -- fail toward backing up, never toward
    # silently skipping (#2151, and the archive-existence gap of the same
    # shape found in #2235 review).
    if not args.force:
        persisted_watermark = _load_persisted_watermark(out_dir)
        if persisted_watermark is not None and _watermarks_match(persisted_watermark, live_watermark):
            if _persisted_archive_exists(out_dir, persisted_watermark):
                # reviewer-fairy suggestion (#2245): a table absent from the
                # live DB (e.g. edit_history on a DB that predates it) reads
                # as bare "None" in this human-facing line otherwise --
                # "n/a" reads as intentional, not as a value that leaked in.
                def fmt(v):
                    return "n/a" if v is None else v
                print(
                    f"backup up to date ({live_watermark['row_count']} nodes, "
                    f"{fmt(live_watermark['edge_count'])} edges, "
                    f"{fmt(live_watermark['history_count'])} edit_history rows, latest node "
                    f"{live_watermark['max_created_at']}, latest edge "
                    f"{fmt(live_watermark['edge_max_created_at'])}, latest history "
                    f"{fmt(live_watermark['history_max_timestamp'])}) — matches "
                    f"{out_dir}/{persisted_watermark.get('archive')}, skipping"
                )
                # Still run retention pruning even when skipping the backup.
                if args.retain > 0:
                    _prune_old_archives(out_dir, args.retain, args.dry_run)
                return 0
            print(
                f"watermark matches the last successful backup, but its archive "
                f"({out_dir}/{persisted_watermark.get('archive')}) is missing from disk "
                "-- not trusting the skip; writing a fresh backup instead"
            )

    target_path = _next_available_archive_path(out_dir, today)

    if args.dry_run:
        print(f"[dry-run] would write backup: {target_path}")
        if args.retain > 0:
            _prune_old_archives(out_dir, args.retain, args.dry_run)
        return 0

    # Perform the backup.
    try:
        stats = engram_backup.dump_stripped(db_path, target_path)
    except Exception as exc:
        print(f"error: backup failed: {exc}", file=sys.stderr)
        return 1

    print(
        f"backup written: {target_path} "
        f"({stats['bytes']} bytes, {stats['statements']} statements)"
    )

    # Persist the watermark this backup actually covers.
    _write_watermark(out_dir, live_watermark, os.path.basename(target_path))

    # Retention pruning.
    if args.retain > 0:
        _prune_old_archives(out_dir, args.retain, args.dry_run)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
