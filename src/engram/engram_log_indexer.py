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

import json
import os
import sqlite3
import sys
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


def _init_schema(conn: sqlite3.Connection) -> None:
    """Create events table and all indexes if they don't already exist."""
    conn.execute(_CREATE_EVENTS_TABLE)
    for stmt in _CREATE_INDEXES:
        conn.execute(stmt)
    conn.commit()


def _extract_promoted(event_type: str, data: dict) -> dict:
    """Extract promoted-column values from event data dict.

    Promoted column mapping (per DESIGN.md §6 + §4 data field names):
      engram.surface.fire  → daemon_latency_ms, fallback_to_fts
      engram.hook.fire     → hook_name, hook_duration_ms (from "duration_ms"),
                             hook_exit_code (from "exit_code")
      engram.tool.engram_call → tool_name, result_status

    Most events won't populate most columns — returns NULL (None) for absent
    dimensions. fallback_to_fts is coerced to INTEGER (1/0) per spec.
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

    if event_type == "engram.surface.fire":
        if "daemon_latency_ms" in data:
            promoted["daemon_latency_ms"] = data["daemon_latency_ms"]
        if "fallback_to_fts" in data:
            # Convert boolean to INTEGER (SQLite has no native bool — DESIGN.md §6)
            promoted["fallback_to_fts"] = 1 if data["fallback_to_fts"] else 0

    elif event_type == "engram.hook.fire":
        if "hook_name" in data:
            promoted["hook_name"] = data["hook_name"]
        # Note: data field is "duration_ms"; promoted column is "hook_duration_ms"
        if "duration_ms" in data:
            promoted["hook_duration_ms"] = data["duration_ms"]
        # Note: data field is "exit_code"; promoted column is "hook_exit_code"
        if "exit_code" in data:
            promoted["hook_exit_code"] = data["exit_code"]

    elif event_type == "engram.tool.engram_call":
        if "tool_name" in data:
            promoted["tool_name"] = data["tool_name"]
        if "result_status" in data:
            promoted["result_status"] = data["result_status"]

    return promoted


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


def _index_file(conn: sqlite3.Connection, jsonl_path: Path, session_id: str, level: int) -> int:
    """Index all new events from a single JSONL file.

    Uses max(ts) per (sessionId, level) to determine which events are already
    in the index; only events with ts > max_indexed_ts are inserted. Scoping
    the high-water mark to level is necessary because L1 and L2 files for the
    same session may contain events with overlapping or identical timestamps
    (e.g. an L2 sibling event may precede the last L1 event chronologically).
    INSERT OR IGNORE on uuid provides dedup as a second safety net.

    Returns the number of rows inserted.
    """
    # Determine the high-water mark already in the index for this (session, level)
    row = conn.execute(
        "SELECT MAX(ts) FROM events WHERE sessionId = ? AND level = ?",
        (session_id, level),
    ).fetchone()
    max_ts: Optional[str] = row[0] if row else None

    rows_inserted = 0
    with open(jsonl_path, "r", encoding="utf-8") as f:
        for lineno, raw_line in enumerate(f, 1):
            raw_line = raw_line.strip()
            if not raw_line:
                continue
            try:
                event = json.loads(raw_line)
            except json.JSONDecodeError as exc:
                print(
                    f"[engram-indexer] malformed JSON in {jsonl_path}:{lineno}: {exc}; skipping",
                    file=sys.stderr,
                )
                continue

            # Valid JSON but not a JSON object (e.g. bare string, integer,
            # array) — the emitter only produces dict events, but externally-
            # generated JSONL files could contain anything. Skip with log.
            # Bug found by F4 integration tests 2026-05-17.
            if not isinstance(event, dict):
                print(
                    f"[engram-indexer] non-object JSON in {jsonl_path}:{lineno} "
                    f"(got {type(event).__name__}); skipping",
                    file=sys.stderr,
                )
                continue

            event_ts = event.get("ts")
            if event_ts is None:
                print(
                    f"[engram-indexer] missing 'ts' in {jsonl_path}:{lineno}; skipping",
                    file=sys.stderr,
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
            promoted = _extract_promoted(event_type, data)

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

    if rows_inserted > 0:
        conn.commit()

    return rows_inserted


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
            files_processed, rows_inserted, errors (count of malformed lines
            is not returned here — it goes to stderr).
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
            total_rows_inserted = 0

            # Process l1 and l2 JSONL files — both are indexed (privacy
            # partitioning is the emitter's job; the indexer reads both)
            for level in (1, 2):
                for jsonl_path in sorted(sessions_dir.glob(f"*.l{level}.jsonl")):
                    # Derive session_id from filename: <session_id>.l1.jsonl
                    stem = jsonl_path.name  # e.g. "abc-123.l1.jsonl"
                    session_id = stem[: stem.index(f".l{level}.jsonl")]
                    rows = _index_file(conn, jsonl_path, session_id, level)
                    files_processed += 1
                    total_rows_inserted += rows

        finally:
            conn.close()

        return {"files_processed": files_processed, "rows_inserted": total_rows_inserted}

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
