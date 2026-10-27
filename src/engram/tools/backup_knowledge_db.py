"""Content-aware knowledge.db backup tool — create a WAL-safe SQL snapshot at $ENGRAM_HOME/db-backup/.

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
regardless of the watermark.

Retention (#4208). The serialized SQL dump is the single source of truth; binary
copies are for quick rollback only. By DEFAULT the tool keeps the newest 14 daily
archives plus the first archive of each calendar month (a monthly anchor) and prunes
the rest. The default policy is date-of-filename based (never mtime, never today's
date): "newest 14" counts distinct dates among the archives present, and a suffixed
same-day archive (-2, -3, ...) is kept or pruned together with its day. --prune-days N
(alias of --retain N) is an OVERRIDE: when N > 0 it prunes strictly by the date cutoff
(no 14-day / anchor policy). --keep-all prunes nothing (combining it with --prune-days N > 0
is a usage error). An EXPLICIT --retain 0 / --prune-days 0 keeps its pre-#4208 meaning
(keep all, like --keep-all; so --keep-all --prune-days 0 is not an error); only the ABSENT flag
selects the default policy. After a successful SQL write (not on a content-skip, not on
--dry-run) one consistent binary copy is written to db-backup/binary/knowledge-<ISO-UTC>.db
and only the 2 most recent are kept (--no-binary skips the copy and its rotation).
Every run then untracks (git rm --cached, files stay on disk) any binary copy git already
tracks under the home -- backups/**, db-backup/**, *.db.*, *.bak-* -- so it stops being
pushed off-disk; the next nap/sleep commit carries the removal.

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

Backup layers: db-backup/ lives inside $ENGRAM_HOME — git-independent (survives .git
corruption; works without git) but NOT off-disk (a full $ENGRAM_HOME loss takes it too).
The per-nap git auto-push is the off-disk layer.  The three layers are complementary.

Defaults follow $ENGRAM_HOME (else ~ plus the standard data directory): --db is
$ENGRAM_HOME/knowledge.db and --out-dir is $ENGRAM_HOME/db-backup.

Through the owner (U15 of #2652, #3872): behind `engramd.backup_reads_via_owner` in
config.json (default OFF), and only when --db is this home's store, the tool never opens
the live store. It asks the engramd owner for ONE quiesced snapshot into a 0700 temp dir
inside --out-dir, reads the watermark from that snapshot, makes the skip decision on it,
and feeds the same snapshot to dump_stripped, so the watermark and the archive describe the
same state. The cost is one full-store copy per run, deleted at once (on success, on skip
and on any error). Every owner failure — down, refusing, an INDETERMINATE answer (the
connection dropped or the owner's deadline passed after a commit was reserved), or a
malformed one — is an error: exit 1, no archive, no watermark change, never a fallback to
reading the live store. With --dry-run the owner is pinged only; a dry run creates no files,
so it cannot take the snapshot the skip decision needs. A --db naming another file keeps the
direct read-only path whatever the flag says.

A SIGKILL/SIGTERM mid-run skips the `finally`, leaving the 0700 `.engram-bk-owner-*` temp dir (a
full store copy) in --out-dir, where neither --retain nor anything else removes it. So every run
starts by sweeping stale ones, with three properties: prefix-exact (a plain string prefix, never a
glob; only real directories owned by the running user, never a symlink or file), age-bound (older
than 6 h, so a concurrent run's live temp dir is never touched), and failure-proof (the sweep's
own error prints one warning and never fails the backup). It runs on both flag paths, since an
orphan outlives a flag turned back OFF. The uid check cannot be pinned by a test cell on a
single-uid seat (ownership cannot be faked without root).

Usage:
    python3 backup_knowledge_db.py [--db PATH] [--out-dir DIR] [--retain|--prune-days DAYS | --keep-all] [--dry-run] [--force] [--no-binary]
"""

from __future__ import annotations

import argparse
import datetime
import glob
import json
import os
import re
import shutil
import sqlite3
import stat
import subprocess
import sys
import tempfile
import time
from pathlib import Path

# append (not insert(0)) -- #2565: putting src/engram at sys.path
# POSITION 0 outranks the repo-root tools/ package for the rest of the
# process, silently poisoning any test file collected afterward that
# needs the real tools.engine (the tools/-package-name-clash class,
# #2063/PR#2564's own shape, just reached via a tool script's own
# top-level import rather than a test file's).
sys.path.append(os.path.join(os.path.dirname(__file__), ".."))
import engram_backup  # noqa: E402
from tools import _owner_read as owner_read  # noqa: E402


_WATERMARK_FILENAME = ".watermark.json"

# --- retention + binary copies (#4208) ---------------------------------------
_KEEP_DAILY = 14
_BINARY_DIRNAME = "binary"
_BINARY_KEEP = 2
_BINARY_STAMP_FMT = "%Y-%m-%dT%H%M%SZ"

# --- owner snapshot (#3872) --------------------------------------------------
OWNER_FLAG_KEY = "backup_reads_via_owner"
_OWNER_TMP_PREFIX = ".engram-bk-owner-"
_OWNER_SNAPSHOT_NAME = "knowledge.snap.db"
# A JUDGEMENT, not a measurement: one run (snapshot <= 35 s + dump) takes on the order of a minute,
# so 6 h cannot catch a live run. It is the bound that protects a CONCURRENT run's temp dir from
# the sweep (deleting it would leave that run an indeterminate snapshot).
_OWNER_TMP_STALE_SECONDS = 6 * 3600


class BackupOwnerError(RuntimeError):
    """The owner could not give the backup its snapshot (flag ON). Raised, never answered from a direct
    read of the live store: a backup that silently came from somewhere else, or from a half-written
    snapshot, is the failure this exists to prevent."""


def _owner_spec() -> "owner_read.OwnerReadSpec":
    return owner_read.OwnerReadSpec(
        flag_key=OWNER_FLAG_KEY,
        error_cls=BackupOwnerError,
        home=owner_read.engram_home(),
        never_empty="Did not fall back to reading the live store directly: no backup was taken.",
    )


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="backup_knowledge_db",
        description="Create a dated WAL-safe SQL snapshot of knowledge.db.",
    )
    p.add_argument(
        "--db",
        default=str(owner_read.default_db_path()),
        help="Source database path (default: $ENGRAM_HOME/knowledge.db). Does not move the "
        "untracking step, which always acts on $ENGRAM_HOME.",
    )
    p.add_argument(
        "--out-dir",
        default=str(owner_read.engram_home() / "db-backup"),
        help="Destination directory for dated archives (default: $ENGRAM_HOME/db-backup). "
        "Does not move the untracking step, which always acts on $ENGRAM_HOME.",
    )
    p.add_argument(
        "--retain",
        "--prune-days",
        dest="retain",
        type=int,
        default=None,
        help="OVERRIDE: if > 0, prune .sql archives older than DAYS days and nothing else "
        "(no 14-day / monthly-anchor policy). An explicit 0 keeps all archives (as before "
        "#4208). Flag absent = the default policy (newest 14 daily archives + the first "
        "archive of each month).",
    )
    p.add_argument(
        "--keep-all",
        action="store_true",
        help="Prune no .sql archive (the pre-#4208 behaviour). Cannot be combined with --prune-days.",
    )
    p.add_argument(
        "--no-binary",
        action="store_true",
        help="Skip the rollback binary copy in <out-dir>/binary/ (and its rotation).",
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
    args = p.parse_args(argv)
    if args.retain is not None and args.retain < 0:
        p.error(f"--prune-days/--retain must be 0 or a positive number of days (got {args.retain})")
    if args.keep_all and args.retain is not None and args.retain > 0:
        p.error("--keep-all and --prune-days (--retain) are mutually exclusive")
    return args


def _prune_old_archives(out_dir: str, retain_days: int, dry_run: bool) -> None:
    """Delete knowledge-YYYY-MM-DD[-N].sql files older than retain_days days.

    A suffixed same-day archive (knowledge-YYYY-MM-DD-2.sql, ...) prunes by
    the SAME date as its unsuffixed sibling -- the suffix is not a date
    component, just a same-day collision counter.
    """
    import engram_core  # lazy: owns the archive-name regex
    cutoff = datetime.date.today() - datetime.timedelta(days=retain_days)
    pattern = os.path.join(out_dir, "knowledge-*.sql")
    for path in sorted(glob.glob(pattern)):
        fname = os.path.basename(path)
        m = engram_core.BACKUP_ARCHIVE_RE.match(fname)
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


def _prune_default_policy(out_dir: str, dry_run: bool) -> None:
    """Default retention (#4208): keep the newest _KEEP_DAILY distinct archive dates plus the
    first (earliest-dated) archive date of each calendar month; prune everything else.

    Date-of-filename based: "newest 14" and "first of the month" are computed from the dates
    in the archive names relative to each other, never from mtime or today's date. Every file
    of a kept day (suffixed -2, -3 ... included) is kept; every file of a pruned day is pruned.
    Names that do not match engram_core.BACKUP_ARCHIVE_RE are never touched.
    """
    import engram_core  # lazy: owns the archive-name regex
    by_date: dict = {}
    for path in glob.glob(os.path.join(out_dir, "knowledge-*.sql")):
        m = engram_core.BACKUP_ARCHIVE_RE.match(os.path.basename(path))
        if not m:
            continue
        try:
            d = datetime.date.fromisoformat(m.group(1))
        except ValueError:
            continue
        by_date.setdefault(d, []).append(path)
    dates = sorted(by_date)
    keep = set(dates[-_KEEP_DAILY:])
    first_of_month: dict = {}
    for d in dates:  # ascending: the first date seen per (year, month) is its anchor
        first_of_month.setdefault((d.year, d.month), d)
    keep.update(first_of_month.values())
    for d in dates:
        if d in keep:
            continue
        for path in sorted(by_date[d]):
            if dry_run:
                print(f"[dry-run] would prune: {path}")
            else:
                os.unlink(path)
                print(f"pruned: {path}")


def _apply_retention(out_dir: str, args: argparse.Namespace) -> None:
    """The one retention dispatch (#4208), run wherever pruning ran before: --keep-all prunes
    nothing; an explicit --prune-days/--retain 0 also keeps all; N > 0 prunes strictly by date
    cutoff; flag absent (None) -> the default policy (newest 14 + monthly anchors)."""
    if args.keep_all or args.retain == 0:
        return
    if args.retain is not None:
        _prune_old_archives(out_dir, args.retain, args.dry_run)
    else:
        _prune_default_policy(out_dir, args.dry_run)


def _untrack_ignored_binaries(engram_home: Path, dry_run: bool) -> list:
    """Untrack (git rm --cached; the file stays on disk) every binary copy git tracks under
    *engram_home*: backups/**, db-backup/**, *.db.*, *.bak-*. Returns the untracked paths.

    Always acts on *engram_home* (the caller passes $ENGRAM_HOME), whatever --db / --out-dir say:
    a run pointed at a sandbox --db or --out-dir still untracks in the real home.

    Does not commit -- the next nap/sleep commit carries the removal. Never raises: a git
    failure prints one warning to stderr and returns what was untracked so far.
    """
    import engram_core  # lazy: the CLI's other paths never need it

    home = Path(engram_home)
    if not (home / ".git").exists():
        print(f"untrack: {home} is not a git repo, skipped")
        return []
    try:
        paths = engram_core._tracked_binary_paths(home)
    except Exception as exc:
        print(f"warning: untrack skipped: {type(exc).__name__}: {exc}", file=sys.stderr)
        return []
    done: list = []
    for path in paths:
        if dry_run:
            print(f"[dry-run] would untrack: {path}")
            done.append(path)
            continue
        try:
            r = subprocess.run(
                [engram_core.GIT_EXE, "--literal-pathspecs", "-C", str(home), "rm", "--cached", "--quiet", "--", path],
                capture_output=True, text=True, timeout=engram_core.GIT_TIMEOUT,
                env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
            )
        except subprocess.TimeoutExpired as exc:
            # Stop issuing calls: a SIGKILLed git can leave .git/index.lock behind, which
            # the next run or commit will report; more calls would only pile onto a stuck repo.
            print(f"warning: git rm --cached timed out for {path} ({exc}); "
                  "remaining paths are skipped this run", file=sys.stderr)
            break
        except OSError as exc:
            print(f"warning: git rm --cached failed for {path}: {type(exc).__name__}: {exc}", file=sys.stderr)
            continue
        if r.returncode != 0:
            print(f"warning: git rm --cached failed for {path}: {r.stderr.strip()}", file=sys.stderr)
            continue
        print(f"untracked (stays on disk, no longer pushed): {path}")
        done.append(path)
    return done


def _housekeeping(out_dir: str, args: argparse.Namespace) -> None:
    """Retention, then untracking tracked binary copies (once per run)."""
    _apply_retention(out_dir, args)
    _untrack_ignored_binaries(owner_read.engram_home(), args.dry_run)


def _binary_dir(out_dir: str) -> str:
    return os.path.join(out_dir, _BINARY_DIRNAME)


def _next_binary_path(out_dir: str) -> str:
    """binary/knowledge-<ISO-UTC>.db for now; if that name is taken (two writes inside one
    second), step forward one second until free so names stay unique and sort by time."""
    t = datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0)
    bdir = _binary_dir(out_dir)
    while True:
        cand = os.path.join(bdir, f"knowledge-{t.strftime(_BINARY_STAMP_FMT)}.db")
        if not os.path.exists(cand):
            return cand
        t += datetime.timedelta(seconds=1)


def _write_binary_copy(out_dir: str, source_path: str) -> None:
    """One consistent binary copy of *source_path* (sqlite backup API: WAL-safe hot copy), then
    rotate to the _BINARY_KEEP newest. A failure warns on stderr and never fails the run: the
    SQL archive, the single source of truth, is already written."""
    import engram_core  # lazy: owns the binary-name regex
    bdir = _binary_dir(out_dir)
    target = _next_binary_path(out_dir)
    tmp_path = None
    try:
        os.makedirs(bdir, mode=0o700, exist_ok=True)
        os.chmod(bdir, 0o700)
        for stray in glob.glob(os.path.join(glob.escape(bdir), ".knowledge-*.db.tmp")):
            os.unlink(stray)  # a kill between tmp creation and os.replace leaves one forever
        tmp_path = os.path.join(bdir, "." + os.path.basename(target) + ".tmp")
        src = sqlite3.connect(f"file:{source_path}?mode=ro", uri=True)
        try:
            dst = sqlite3.connect(tmp_path)
            try:
                src.backup(dst)
            finally:
                dst.close()
        finally:
            src.close()
        os.replace(tmp_path, target)
        tmp_path = None
    except Exception as exc:
        print(f"warning: binary copy failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        if tmp_path and os.path.exists(tmp_path):
            os.unlink(tmp_path)
        return
    print(f"binary copy written: {target}")
    names = sorted(n for n in os.listdir(bdir) if engram_core.BACKUP_BINARY_RE.match(n))
    for n in names[:-_BINARY_KEEP]:
        victim = os.path.join(bdir, n)
        os.unlink(victim)
        print(f"pruned binary: {victim}")


def _sweep_stale_owner_tmpdirs(out_dir: str, dry_run: bool, now: float | None = None) -> None:
    """Remove `.engram-bk-owner-*` temp dirs a killed run left in out_dir (see module docstring).

    Prefix-exact, age-bound (_OWNER_TMP_STALE_SECONDS), real dirs owned by this uid only. Never raises:
    any failure prints one warning line to stderr and returns.
    """
    try:
        if not os.path.isdir(out_dir):
            return
        if now is None:
            now = time.time()
        uid = os.getuid()
        with os.scandir(out_dir) as it:
            entries = list(it)
        for entry in entries:
            if not entry.name.startswith(_OWNER_TMP_PREFIX):
                continue
            path = os.path.join(out_dir, entry.name)
            st = os.lstat(path)
            if not stat.S_ISDIR(st.st_mode):
                continue
            if st.st_uid != uid:
                continue
            if not now - st.st_mtime > _OWNER_TMP_STALE_SECONDS:
                continue
            if dry_run:
                print(f"[dry-run] would remove stale owner temp dir: {path}")
            else:
                shutil.rmtree(path)
                print(f"removed stale owner temp dir: {path} (age {(now - st.st_mtime) / 3600:.0f}h)")
    except Exception as exc:
        print(f"warning: stale owner temp-dir sweep failed: {type(exc).__name__}: {exc}", file=sys.stderr)


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


def _main_via_owner(args: argparse.Namespace, spec: "owner_read.OwnerReadSpec", out_dir: str) -> int:
    """The flag-ON path (#3872): the live store is never opened. ONE owner snapshot, in a 0700 temp dir
    inside out_dir, feeds both the watermark and the archive, and is removed on every exit."""
    today = datetime.date.today().isoformat()  # YYYY-MM-DD
    try:
        if args.dry_run:
            owner_read.ping(spec)
            if not os.path.exists(out_dir):
                print(f"[dry-run] would create directory: {out_dir}")
            target_path = _next_available_archive_path(out_dir, today)
            print(
                f"[dry-run] would snapshot via the engramd owner and write backup: {target_path} "
                "(a dry run takes no snapshot, so it cannot tell whether the backup would be skipped)"
            )
            if not args.no_binary:
                print(f"[dry-run] would write binary copy: {_next_binary_path(out_dir)}")
            _housekeeping(out_dir, args)
            return 0

        os.makedirs(out_dir, exist_ok=True)
        tmpdir = tempfile.mkdtemp(prefix=_OWNER_TMP_PREFIX, dir=out_dir)
        try:
            os.chmod(tmpdir, 0o700)
            snap = owner_read.snapshot(spec, Path(os.path.abspath(tmpdir)) / _OWNER_SNAPSHOT_NAME)
            try:
                live_watermark = _read_live_watermark(str(snap))
            except Exception as exc:
                print(f"error: could not read watermark from the owner snapshot: {exc}", file=sys.stderr)
                return 1
            return _skip_or_backup(args, out_dir, today, live_watermark, str(snap))
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)
    except BackupOwnerError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)

    db_path = os.path.expanduser(args.db)
    out_dir = os.path.expanduser(args.out_dir)

    # Before the flag branch: an orphan outlives a flag turned back OFF. Before any mkdtemp: the run's
    # own temp dir can never be swept mid-run.
    _sweep_stale_owner_tmpdirs(out_dir, args.dry_run)

    # Flag ON and --db is this home's store: the owner path. It checks no file and opens no store.
    spec = _owner_spec()
    if owner_read.flag_applies(spec, Path(db_path)):
        return _main_via_owner(args, spec, out_dir)

    # Verify the source store is a usable regular file (#3962). Never opens it: a `mode=ro` open of a FIFO blocks
    # on macOS, and `os.path.exists` reports a loop, a dangling link and an unsearchable parent as "not found".
    try:
        owner_read.check_store_path(Path(db_path))
    except FileNotFoundError:
        print(f"error: database not found: {db_path}", file=sys.stderr)
        return 1
    except owner_read.StoreUnusable as exc:
        print(f"error: {exc}", file=sys.stderr)
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

    return _skip_or_backup(args, out_dir, today, live_watermark, db_path)


def _skip_or_backup(
    args: argparse.Namespace, out_dir: str, today: str, live_watermark: dict, source_path: str
) -> int:
    """The skip decision and the backup itself, shared by the direct and the owner paths. `source_path` is
    what dump_stripped reads: the live store (direct) or the owner's snapshot of it."""
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
                # Still run retention pruning even when skipping the backup (no binary copy).
                _housekeeping(out_dir, args)
                return 0
            print(
                f"watermark matches the last successful backup, but its archive "
                f"({out_dir}/{persisted_watermark.get('archive')}) is missing from disk "
                "-- not trusting the skip; writing a fresh backup instead"
            )

    target_path = _next_available_archive_path(out_dir, today)

    if args.dry_run:
        print(f"[dry-run] would write backup: {target_path}")
        if not args.no_binary:
            print(f"[dry-run] would write binary copy: {_next_binary_path(out_dir)}")
        _housekeeping(out_dir, args)
        return 0

    # Perform the backup.
    try:
        stats = engram_backup.dump_stripped(source_path, target_path)
    except Exception as exc:
        print(f"error: backup failed: {exc}", file=sys.stderr)
        return 1

    print(
        f"backup written: {target_path} "
        f"({stats['bytes']} bytes, {stats['statements']} statements)"
    )

    # Persist the watermark this backup actually covers.
    _write_watermark(out_dir, live_watermark, os.path.basename(target_path))

    # Rollback binary copy (owner path: from the owner snapshot; direct path: a hot copy of the
    # live store), then retention + untracking.
    if not args.no_binary:
        _write_binary_copy(out_dir, source_path)
    _housekeeping(out_dir, args)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
