"""ENGRAM event indexer — JSONL-to-SQLite derived projection.

Spec: alpha #175 (two-level logging architecture)
Tracking: alpha #175

This module reads per-session JSONL event files from
~/.engram/logs/sessions/*.l{1,2}.jsonl and projects them into a
read-optimised SQLite index at ~/.engram/logs/index.db.

Design principles:
  - index.db is a DERIVED PROJECTION. Truth lives in JSONL files.
    If the index corrupts, call Indexer.full_rebuild() to regenerate.
  - IDEMPOTENT: run_once() can be called repeatedly without duplicating rows.
    INSERT OR IGNORE on uuid (PRIMARY KEY) provides the dedup guarantee.
  - Incremental: use max(ts) per sessionId to skip already-indexed rows.
  - Failure handling: malformed JSONL lines are logged to stderr and skipped;
    other failures (db locked, disk full) propagate up as exceptions.
"""

from __future__ import annotations

import atexit
import json
import os
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional


# Default logs directory — can be overridden by environment variable or
# by passing logs_dir explicitly to run_once() / full_rebuild().
def _default_logs_dir() -> Path:
    engram_home = Path(os.environ.get("ENGRAM_HOME") or str(Path.home() / ".engram"))
    return engram_home / "logs"


# DDL — must match DESIGN.md §6 exactly. Column order matches the spec table.
_CREATE_EVENTS_TABLE = """
CREATE TABLE IF NOT EXISTS events (
    uuid TEXT PRIMARY KEY,
    sessionId TEXT NOT NULL,
    turn INTEGER NOT NULL,
    ts TEXT NOT NULL,
    event_type TEXT NOT NULL,
    tool_use_id TEXT,
    level INTEGER NOT NULL,
    daemon_latency_ms INTEGER,
    fallback_to_fts INTEGER,
    hook_name TEXT,
    hook_duration_ms INTEGER,
    hook_exit_code INTEGER,
    tool_name TEXT,
    result_status TEXT,
    data JSON NOT NULL
)
"""

_CREATE_INDEXES = [
    "CREATE INDEX IF NOT EXISTS idx_events_type_ts ON events(event_type, ts)",
    "CREATE INDEX IF NOT EXISTS idx_events_turn ON events(turn)",
    "CREATE INDEX IF NOT EXISTS idx_events_session ON events(sessionId)",
    "CREATE INDEX IF NOT EXISTS idx_events_tool_use ON events(tool_use_id)",
    "CREATE INDEX IF NOT EXISTS idx_events_latency ON events(daemon_latency_ms)",
    "CREATE INDEX IF NOT EXISTS idx_events_hook_name ON events(hook_name)",
    "CREATE INDEX IF NOT EXISTS idx_events_tool_name ON events(tool_name)",
]

# file_state — per-file size gate (alpha #2785). Lets run_once() skip
# re-reading a JSONL file whose size hasn't changed since the last tick,
# instead of re-parsing every line of every file on every 60s pass.
#
# Keyed on SIZE, not mtime: session files are append-only (single writer,
# engram_log_emitter.py, "a"-mode open/close per event), and on ext4-under-
# WSL2 mtime granularity (~3.92ms) let 400 consecutive appends collapse to a
# single mtime value while size changed on every one -- an mtime gate has a
# silent permanent row-loss race (a final append lands in the same mtime
# bucket, the session ends, and that line is never indexed). Size is
# strictly monotone under append-only and changes on every write, at
# identical implementation cost. Do not add mtime as a second condition --
# it can only cause spurious re-reads, never catch anything size misses.
_CREATE_FILE_STATE_TABLE = """
CREATE TABLE IF NOT EXISTS file_state (
    path TEXT PRIMARY KEY,
    size INTEGER NOT NULL,
    indexed_at TEXT
)
"""

# projection_verdicts — alpha #2574 WS4. A dedicated table (not a synthetic
# row in `events`): `events` has several NOT NULL columns (sessionId, turn,
# ts, data) that don't naturally fit a tick-level aggregate, and this table
# answers a different question ("was this type understood?") from what
# `events` answers ("what happened?"). Still "the same store" as the design
# doc's §6 requires — same index.db file, same connection. Small and read
# rarely (self-monitoring / occasional review, not a hot query path) — no
# index needed. Fresh row per tick, not a cumulative running total per
# (event_type, manifest_id): simpler, avoids UPDATE-vs-INSERT races, and
# matches the drop-count mechanism's own "fold into the next write" pattern
# (see _write_verdict_rows) rather than maintaining running state across
# ticks.
#
# NOTE — `count` carries two DIFFERENT units depending on the row's
# event_type, and they must never be summed/averaged together: for an
# ordinary unprojected-event-type row, count is an occurrence count (how
# many events of that type this tick); for the self-healing fold-in row
# (event_type="engram.indexer.verdict_write_dropped"), count is instead the
# cumulative number of dropped verdict-row ENTRIES across consecutive
# failed write attempts (len(rows) per attempt, summed) -- not a count of
# attempts, and not an event-occurrence count either.
_CREATE_PROJECTION_VERDICTS_TABLE = """
CREATE TABLE IF NOT EXISTS projection_verdicts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts_utc TEXT NOT NULL,
    event_type TEXT NOT NULL,
    manifest_id TEXT,
    count INTEGER NOT NULL
)
"""


def _init_schema(conn: sqlite3.Connection) -> None:
    """Create events table, file_state table, projection_verdicts table, and
    all indexes if they don't already exist."""
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute(_CREATE_EVENTS_TABLE)
    conn.execute(_CREATE_FILE_STATE_TABLE)
    conn.execute(_CREATE_PROJECTION_VERDICTS_TABLE)
    for stmt in _CREATE_INDEXES:
        conn.execute(stmt)
    conn.commit()


# In-process dedup for the per-line malformed/non-object/missing-ts warnings
# emitted by _index_file. Deliberately NOT persisted to index.db or any
# other cross-process store: a daemon restart SHOULD re-report a bad line,
# because a restart is when someone is looking. Keyed on (path, lineno) so
# a genuinely new bad line in a growing file still warns.
_WARNED_LINES: set[tuple[str, int]] = set()


def _warn_once(path_key: str, lineno: int, message: str) -> None:
    """Print `message` to stderr the first time (path_key, lineno) is seen
    in this process; silently skip on subsequent occurrences."""
    key = (path_key, lineno)
    if key in _WARNED_LINES:
        return
    _WARNED_LINES.add(key)
    print(message, file=sys.stderr)


# Promoted column mapping (per DESIGN.md §6 + §4 data field names):
#   engram.surface.fire  → daemon_latency_ms, fallback_to_fts
#   engram.hook.fire     → hook_name, hook_duration_ms (from "duration_ms"),
#                          hook_exit_code (from "exit_code")
#   engram.tool.engram_call → tool_name, result_status
#
# Most events won't populate most columns — absent dimensions stay NULL
# (None). fallback_to_fts is coerced to INTEGER (1/0) per spec.
#
# Registry (alpha #2574 WS4): one small projector function per declared
# event_type, keyed in _PROJECTIONS below. The set of declared projections is
# enumerable by reading the dict, not by tracing control flow, and a lookup
# miss on an event_type not in this dict is a distinguishable, countable
# event (see _extract_promoted's `is_projected` return) rather than a silent
# fall-through.

def _project_surface_fire(data: dict) -> dict:
    """Promoted columns for engram.surface.fire: daemon_latency_ms,
    fallback_to_fts (coerced to INTEGER — SQLite has no native bool)."""
    out: dict = {}
    if "daemon_latency_ms" in data:
        out["daemon_latency_ms"] = data["daemon_latency_ms"]
    if "fallback_to_fts" in data:
        out["fallback_to_fts"] = 1 if data["fallback_to_fts"] else 0
    return out


def _project_hook_fire(data: dict) -> dict:
    """Promoted columns for engram.hook.fire: hook_name, hook_duration_ms
    (from data's "duration_ms"), hook_exit_code (from data's "exit_code")."""
    out: dict = {}
    if "hook_name" in data:
        out["hook_name"] = data["hook_name"]
    if "duration_ms" in data:
        out["hook_duration_ms"] = data["duration_ms"]
    if "exit_code" in data:
        out["hook_exit_code"] = data["exit_code"]
    return out


def _project_tool_call(data: dict) -> dict:
    """Promoted columns for engram.tool.engram_call: tool_name,
    result_status."""
    out: dict = {}
    if "tool_name" in data:
        out["tool_name"] = data["tool_name"]
    if "result_status" in data:
        out["result_status"] = data["result_status"]
    return out


_PROJECTIONS: dict = {
    "engram.surface.fire": _project_surface_fire,
    "engram.hook.fire": _project_hook_fire,
    "engram.tool.engram_call": _project_tool_call,
}


def _extract_promoted(event_type: str, data: dict) -> tuple[dict, bool]:
    """Extract promoted-column values from event data dict via the
    _PROJECTIONS registry.

    Returns (promoted, is_projected): `promoted` is the dict of column
    values (NULL/None for absent dimensions); `is_projected` is True iff
    `event_type` has a declared projector in _PROJECTIONS. This is the
    load-bearing signal: "was this projected?" is a return value the caller
    reads, never an exception — an event_type unknown to this consumer is
    never a raised error, it's still stored with the same NULL-columns
    behavior as before, only now the miss is countable (alpha #2574 WS4).
    """
    promoted: dict = {
        "daemon_latency_ms": None,
        "fallback_to_fts": None,
        "hook_name": None,
        "hook_duration_ms": None,
        "hook_exit_code": None,
        "tool_name": None,
        "result_status": None,
    }
    projector = _PROJECTIONS.get(event_type)
    if projector is None:
        return promoted, False
    promoted.update(projector(data))
    return promoted, True


_INSERT_EVENT = """
INSERT OR IGNORE INTO events (
    uuid, sessionId, turn, ts, event_type, tool_use_id, level,
    daemon_latency_ms, fallback_to_fts,
    hook_name, hook_duration_ms, hook_exit_code,
    tool_name, result_status,
    data
) VALUES (
    :uuid, :sessionId, :turn, :ts, :event_type, :tool_use_id, :level,
    :daemon_latency_ms, :fallback_to_fts,
    :hook_name, :hook_duration_ms, :hook_exit_code,
    :tool_name, :result_status,
    :data
)
"""


def _index_file(
    conn: sqlite3.Connection, jsonl_path: Path, session_id: str, level: int
) -> tuple[int, dict]:
    """Index all new events from a single JSONL file.

    Uses max(ts) per (sessionId, level) to determine which events are already
    in the index; only events with ts > max_indexed_ts are inserted. Scoping
    the high-water mark to level is necessary because L1 and L2 files for the
    same session may contain events with overlapping or identical timestamps
    (e.g. an L2 sibling event may precede the last L1 event chronologically).
    INSERT OR IGNORE on uuid provides dedup as a second safety net.

    Returns (rows_inserted, unprojected_counts): rows_inserted is the number
    of rows inserted; unprojected_counts is a dict[tuple[str, str | None],
    int] mapping (event_type, manifest_id) -> occurrence count, for every
    event this file contained whose event_type had no declared projection
    (alpha #2574 WS4).
    """
    # Determine the high-water mark already in the index for this (session, level)
    row = conn.execute(
        "SELECT MAX(ts) FROM events WHERE sessionId = ? AND level = ?",
        (session_id, level),
    ).fetchone()
    max_ts: Optional[str] = row[0] if row else None

    rows_inserted = 0
    unprojected_counts: dict = {}
    with open(jsonl_path, "r", encoding="utf-8") as f:
        for lineno, raw_line in enumerate(f, 1):
            raw_line = raw_line.strip()
            if not raw_line:
                continue
            try:
                event = json.loads(raw_line)
            except json.JSONDecodeError as exc:
                _warn_once(
                    str(jsonl_path),
                    lineno,
                    f"[engram-indexer] malformed JSON in {jsonl_path}:{lineno}: {exc}; skipping",
                )
                continue

            # Valid JSON but not a JSON object (e.g. bare string, integer,
            # array) — the emitter only produces dict events, but externally-
            # generated JSONL files could contain anything. Skip with log.
            # Bug found by F4 integration tests 2026-05-17.
            if not isinstance(event, dict):
                _warn_once(
                    str(jsonl_path),
                    lineno,
                    f"[engram-indexer] non-object JSON in {jsonl_path}:{lineno} "
                    f"(got {type(event).__name__}); skipping",
                )
                continue

            event_ts = event.get("ts")
            if event_ts is None:
                _warn_once(
                    str(jsonl_path),
                    lineno,
                    f"[engram-indexer] missing 'ts' in {jsonl_path}:{lineno}; skipping",
                )
                continue

            # Skip already-indexed rows using the timestamp high-water mark.
            # INSERT OR IGNORE handles the edge case where two events in the
            # same file share the same ts (dedup falls back to uuid uniqueness).
            if max_ts is not None and event_ts <= max_ts:
                continue

            data = event.get("data", {})
            if not isinstance(data, dict):
                data = {}

            event_type = event.get("event_type", "")
            promoted, is_projected = _extract_promoted(event_type, data)
            if not is_projected:
                key = (event_type, data.get("manifest_id"))
                unprojected_counts[key] = unprojected_counts.get(key, 0) + 1

            params = {
                "uuid": event.get("uuid"),
                "sessionId": event.get("sessionId", session_id),
                "turn": event.get("turn"),
                "ts": event_ts,
                "event_type": event_type,
                "tool_use_id": event.get("tool_use_id"),
                "level": event.get("level", level),
                "data": json.dumps(data, separators=(",", ":")),
                **promoted,
            }

            conn.execute(_INSERT_EVENT, params)
            rows_inserted += 1

    # Deliberately no commit here (alpha #2785 item 2, round 2): the sole
    # caller (run_once) commits once, after it also writes the file_state
    # row for this file -- rows and file_state land in the SAME
    # transaction, so a crash between them rolls back both rather than
    # durably recording file_state while the rows it accounts for are
    # lost. Two commits per file (as before) doubled lock-acquisition
    # frequency for no correctness benefit.
    return rows_inserted, unprojected_counts


def _checkpoint_wal(conn: sqlite3.Connection, db_path: Path) -> Optional[tuple]:
    """Drain index.db's WAL to zero, reporting a busy result instead of
    discarding it. Returns the pragma's ``(busy, log_frames,
    checkpointed_frames)`` row so callers (and tests) can see the outcome.

    Extracted from ``run_once`` for one reason: the busy branch is not
    reachable on demand from the outside -- it needs a concurrent reader
    holding a snapshot at exactly the right moment -- so inline it would be
    a warning nobody had ever seen fire. As a function it can be handed a
    stub connection and exercised directly.
    """
    row = conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
    if row and row[0] != 0:
        print(
            f"[engram-indexer] WAL checkpoint busy on {db_path} "
            f"(busy={row[0]}, log_frames={row[1]}, checkpointed={row[2]}); "
            f"-wal not fully drained -- an immutable=1 reader may see a short "
            f"count until a later pass drains it",
            file=sys.stderr,
        )
    return row


_VERDICT_WRITE_DROP_COUNT = 0  # in-process counter; see docstring below
# No lock: this module's two real call sites (engram-surface-daemon.py's
# dedicated daemon thread, and viz/data/stats.py's bootstrap call) run in
# SEPARATE PROCESSES, not separate threads within one process -- a
# threading.Lock guards nothing across a process boundary, and each
# process holds its own counter and its own atexit flush. Within either
# process there is exactly one calling thread. Verified 2026-09-11 (colleague
# review, PR #2850): an earlier version of this comment named these same
# two call sites as the reason for a lock, which was simply wrong -- a false
# statement shipping beside the code it was meant to protect. The invariant
# is single-thread-per-process; if that ever stops being true, the counter
# needs synchronization again, added deliberately then, not defensively now.


def _write_verdict_rows(conn: sqlite3.Connection, tick_unprojected: dict) -> int:
    """Write this tick's unprojected-event-type counts as rows in
    projection_verdicts. Returns the number of rows actually written (0 if
    there was nothing to write or the write failed).

    Catch-and-drop, two mechanisms (neither covers the other's hole, both
    ship):
      1. An in-process counter (_VERDICT_WRITE_DROP_COUNT) incremented on
         every caught write failure; flushed to stderr with a distinct
         marker when the process exits (see _flush_verdict_write_drops_atexit).
      2. The accumulated drop count folded into the NEXT successful write,
         as an extra row with event_type="engram.indexer.verdict_write_dropped" --
         self-healing, lands in the same store the report already reads.

    Deliberately narrow try: this wraps ONLY the INSERT + commit, not the
    counting/aggregation above it, so a real bug in the aggregation logic
    still raises and is visible in tests/CI rather than silently counted as
    a "drop".

    Never lets a verdict-write failure abort the tick -- but only WITH the
    except branch's conn.rollback(): without it, a failure that leaves the
    connection mid-transaction would make the very next statement run_once()
    executes on this SAME connection (_checkpoint_wal's WAL checkpoint) raise
    an uncaught OperationalError. The rollback is what keeps the connection
    usable for that call.
    """
    global _VERDICT_WRITE_DROP_COUNT

    ts_utc = datetime.now(timezone.utc).isoformat()
    rows = [
        (ts_utc, event_type, manifest_id, count)
        for (event_type, manifest_id), count in tick_unprojected.items()
    ]
    if _VERDICT_WRITE_DROP_COUNT:
        rows.append(
            (ts_utc, "engram.indexer.verdict_write_dropped", None, _VERDICT_WRITE_DROP_COUNT)
        )
    if not rows:
        return 0

    try:
        conn.executemany(
            "INSERT INTO projection_verdicts (ts_utc, event_type, manifest_id, count) "
            "VALUES (?, ?, ?, ?)",
            rows,
        )
        conn.commit()
    except sqlite3.Error as exc:
        conn.rollback()
        # Increment by len(rows) -- the number of verdict-row entries lost
        # in THIS failed attempt -- not by a flat 1. A flat 1 records only
        # that an attempt failed, discarding how much was in it; the tick
        # that produced `rows` may have carried many distinct (event_type,
        # manifest_id) entries, and the eventual fold-in row's count is the
        # only trace of how much was lost. len(rows) is still an undercount
        # relative to the raw occurrences those entries aggregated, but it
        # is a BOUNDED undercount rather than an unknowable one (colleague
        # review, PR #2850).
        _VERDICT_WRITE_DROP_COUNT += len(rows)
        drop_count = _VERDICT_WRITE_DROP_COUNT
        print(
            f"[engram-indexer] verdict-row write failed: {exc}; dropped "
            f"(in-process drop count now {drop_count})",
            file=sys.stderr,
        )
        return 0

    _VERDICT_WRITE_DROP_COUNT = 0  # folded into the write that just succeeded
    return len(rows)


def _flush_verdict_write_drops_atexit() -> None:
    """atexit hook: if any verdict-row writes were dropped and never got a
    chance to self-heal into a later successful write (process exiting with
    a nonzero _VERDICT_WRITE_DROP_COUNT), flush the count to stderr with a
    distinct marker so it isn't silently lost. Complements (does not
    replace) the self-healing row in _write_verdict_rows."""
    drop_count = _VERDICT_WRITE_DROP_COUNT
    if drop_count:
        print(
            f"[engram-indexer] VERDICT_WRITE_DROPPED_AT_EXIT count={drop_count}",
            file=sys.stderr,
        )


atexit.register(_flush_verdict_write_drops_atexit)


class Indexer:
    """Projects JSONL event files into a derived SQLite index at index.db.

    Public API:
        Indexer.run_once(logs_dir=None)   — incremental pass, idempotent
        Indexer.full_rebuild(logs_dir=None) — drop + recreate, re-index all

    Can also be invoked as a script:
        python3 engram_log_indexer.py     → run_once with default logs_dir
    """

    @staticmethod
    def run_once(logs_dir: Optional[Path] = None) -> dict:
        """Run one full incremental pass over all JSONL files in logs_dir/sessions/.

        Idempotent: re-running on the same input produces no duplicate rows.
        Malformed lines are logged to stderr and skipped.

        Returns a summary dict with keys:
            files_processed — files actually opened and parsed this tick
                (unchanged-size files are NOT counted here; see
                files_skipped).
            files_skipped — files whose on-disk size matched the stored
                file_state size, or whose stat() failed (vanished mid-tick,
                or a real I/O error warned via _warn_once) and so were not
                opened this tick.
            rows_inserted, errors (count of malformed lines is not returned
                here — it goes to stderr).
            file_state_pruned — number of file_state rows removed this
                tick because their path no longer appears among the
                session files glob() found (alpha #2785 item 4).
            verdict_rows_written — number of projection_verdicts rows
                written this tick for event_types with no declared
                projection (alpha #2574 WS4), via _write_verdict_rows.
        """
        if logs_dir is None:
            logs_dir = _default_logs_dir()
        logs_dir = Path(logs_dir)

        sessions_dir = logs_dir / "sessions"
        db_path = logs_dir / "index.db"

        # Ensure directories exist (may be first run with no events yet)
        sessions_dir.mkdir(parents=True, exist_ok=True)

        conn = sqlite3.connect(str(db_path))
        try:
            _init_schema(conn)

            files_processed = 0
            files_skipped = 0
            total_rows_inserted = 0

            # Paths seen this tick across BOTH levels (alpha #2785 item 4:
            # orphan file_state pruning). Populated from glob() results
            # directly, not gated on a successful stat() -- a file that
            # fails to stat (the OSError branch above) still exists on
            # disk and must not be pruned just because it couldn't be read
            # this tick.
            seen_paths: set[str] = set()

            # Unprojected-event-type counts, accumulated across ALL files
            # processed this tick (both l1 and l2, across the whole level
            # loop below) -- merged by summing on shared (event_type,
            # manifest_id) keys, per file. Persisted as projection_verdicts
            # rows after the loop (alpha #2574 WS4).
            tick_unprojected: dict[tuple, int] = {}

            # Process l1 and l2 JSONL files — both are indexed (privacy
            # partitioning is the emitter's job; the indexer reads both)
            for level in (1, 2):
                for jsonl_path in sorted(sessions_dir.glob(f"*.l{level}.jsonl")):
                    # Derive session_id from filename: <session_id>.l1.jsonl
                    stem = jsonl_path.name  # e.g. "abc-123.l1.jsonl"
                    session_id = stem[: stem.index(f".l{level}.jsonl")]
                    path_key = str(jsonl_path)
                    seen_paths.add(path_key)

                    # Size gate (alpha #2785): stat BEFORE reading, and it is
                    # this pre-read size -- not a post-read re-stat or EOF
                    # offset -- that gets persisted below. A post-read stat
                    # would silently drop any bytes appended DURING the
                    # read: they'd already be in the file when we re-stat,
                    # so the gate would record them as already-indexed and
                    # the next tick would skip them forever. Recording the
                    # pre-read size means an append landing mid-read is
                    # simply not yet reflected in the stored size, so the
                    # next tick sees size > stored and re-parses the whole
                    # file -- safe, because the ts high-water mark + INSERT
                    # OR IGNORE make a full re-parse idempotent.
                    try:
                        current_size = os.stat(jsonl_path).st_size
                    except FileNotFoundError:
                        # File vanished between glob() and stat() -- benign,
                        # skip silently for this tick rather than
                        # propagating.
                        files_skipped += 1
                        continue
                    except OSError as exc:
                        # A real I/O failure (PermissionError,
                        # IsADirectoryError, disk error, ...) distinct from
                        # the benign vanished-file case above. Warn once,
                        # then skip only this file -- never let it
                        # propagate, which would abort the tick for every
                        # remaining file. Keyed on lineno=0 (a file-level
                        # condition, not a line) because per-line warnings
                        # use real 1-based line numbers, so 0 cannot
                        # collide with one.
                        _warn_once(
                            path_key,
                            0,
                            f"[engram-indexer] cannot stat {jsonl_path}: "
                            f"{exc}; skipping",
                        )
                        files_skipped += 1
                        continue

                    row = conn.execute(
                        "SELECT size FROM file_state WHERE path = ?",
                        (path_key,),
                    ).fetchone()
                    stored_size = row[0] if row else None

                    if stored_size is not None and current_size == stored_size:
                        # Unchanged since last tick -- do not open the file.
                        files_skipped += 1
                        continue

                    if stored_size is not None and current_size < stored_size:
                        # Shrink is impossible under the append-only
                        # invariant (single writer, "a"-mode opens only) --
                        # something violated it. Don't crash; warn once and
                        # fall through to re-index like any changed file.
                        print(
                            f"[engram-indexer] file shrank: {jsonl_path} "
                            f"({stored_size} -> {current_size} bytes); "
                            f"re-indexing",
                            file=sys.stderr,
                        )

                    rows, file_unprojected = _index_file(conn, jsonl_path, session_id, level)
                    files_processed += 1
                    total_rows_inserted += rows
                    for key, count in file_unprojected.items():
                        tick_unprojected[key] = tick_unprojected.get(key, 0) + count

                    conn.execute(
                        "INSERT OR REPLACE INTO file_state (path, size, indexed_at) "
                        "VALUES (?, ?, ?)",
                        (path_key, current_size, datetime.now(timezone.utc).isoformat()),
                    )
                    conn.commit()

            # Orphan file_state pruning (alpha #2785 item 4): a session
            # file's row must not outlive the file itself, or file_state
            # accumulates rows for sessions deleted long ago. Scoped to
            # seen_paths (the paths this tick's glob() actually returned
            # across BOTH levels) rather than an os.path.exists() check
            # per stored row -- same result, no extra stat syscalls, and
            # it cannot race a file being created mid-tick. Done ONCE here,
            # after both levels have run, not inside the level loop above:
            # an l2 file is not orphaned merely because the l1 pass didn't
            # see it.
            existing_paths = {
                row[0] for row in conn.execute("SELECT path FROM file_state").fetchall()
            }
            orphaned_paths = existing_paths - seen_paths
            file_state_pruned = len(orphaned_paths)
            if orphaned_paths:
                conn.executemany(
                    "DELETE FROM file_state WHERE path = ?",
                    [(p,) for p in orphaned_paths],
                )
                conn.commit()

            # Persist this tick's unprojected-event-type counts as
            # projection_verdicts rows (alpha #2574 WS4). Catch-and-drop on
            # write failure inside _write_verdict_rows itself -- never lets
            # a verdict-write failure abort the tick.
            verdict_rows_written = _write_verdict_rows(conn, tick_unprojected)

            # Drain the WAL to zero (#2571). An immutable=1 reader (viz's
            # /api/stats -- see viz/data/stats.py) ignores -wal entirely, so
            # any committed-but-not-yet-checkpointed rows would be invisible
            # to it. TRUNCATE empties the WAL rather than merely folding it
            # back into the main db file (which FULL permits but does not
            # guarantee -- see sqlite3 docs on wal_checkpoint).
            #
            # BEST-EFFORT, NOT GUARANTEED, and the failure is silent by
            # default: under contention -- another connection holding an open
            # transaction on this file -- the pragma returns
            # (busy=1, log_frames, checkpointed_frames) and does NOT raise,
            # leaving -wal non-empty. Measured on a MINIMAL SYNTHETIC RIG (a
            # few-row table, not index.db's own schema): with a concurrent
            # reader mid-transaction the call returned (1, 3, 3) with -wal at
            # 12,392 bytes; uncontended, (0, 0, 0) and -wal 0. A second,
            # independently-built rig reproduced the MECHANISM exactly and got
            # DIFFERENT figures -- (1, 5, 5), 20,632 bytes. So treat busy != 0
            # and "-wal stays non-empty" as the reproducible facts; the frame
            # counts and byte sizes are illustrative of one rig, not constants
            # to match against a real index.db.
            # So log the busy status rather than discarding it. No data
            # is at risk (this loop already commits per file, and the
            # pruning step above commits its own delete) and the state
            # self-heals on the next pass, but a reader that lands inside the
            # window sees a short count, and without this log there is nothing
            # to correlate that against.
            _checkpoint_wal(conn, db_path)

        finally:
            conn.close()

        return {
            "files_processed": files_processed,
            "files_skipped": files_skipped,
            "rows_inserted": total_rows_inserted,
            "file_state_pruned": file_state_pruned,
            "verdict_rows_written": verdict_rows_written,
        }

    @staticmethod
    def full_rebuild(logs_dir: Optional[Path] = None) -> dict:
        """Drop and recreate index.db, then re-index all JSONL files from scratch.

        Recovery path when index.db is corrupt or schema has changed.
        Truth lives in JSONL files; the index is always rebuildable.

        Returns the same summary dict as run_once().
        """
        if logs_dir is None:
            logs_dir = _default_logs_dir()
        logs_dir = Path(logs_dir)

        db_path = logs_dir / "index.db"

        # Clear the in-process warn-once dedup set: a full rebuild is
        # exactly the "someone is looking" recovery moment the _WARNED_LINES
        # design comment says should re-report every bad line, not silently
        # rely on whatever this process happened to have already warned
        # about before the rebuild was requested.
        _WARNED_LINES.clear()

        # Drop existing index by removing the file (SQLite: simplest reset)
        if db_path.exists():
            db_path.unlink()

        # run_once will recreate schema + re-index all files
        return Indexer.run_once(logs_dir=logs_dir)


if __name__ == "__main__":
    result = Indexer.run_once()
    print(
        f"[engram-indexer] done: {result['files_processed']} file(s) processed, "
        f"{result['rows_inserted']} row(s) inserted"
    )
