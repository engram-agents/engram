"""Unit tests for engram_log_indexer.

Covers:
  - Empty logs dir: runs without crash, creates empty index.db
  - Single JSONL file with 5 events: all 5 indexed, schema correct
  - Re-run on same input: no duplicate rows (idempotency)
  - Promoted columns populated when present, NULL when absent
  - Malformed line (bad JSON): logged + skipped, other lines still indexed
  - full_rebuild: drops and re-indexes from scratch
  - L1 and L2 files both indexed (level column populated correctly)
  - Size gate (alpha #2785): unchanged files are not re-opened, appended
    files are re-indexed, an append landing mid-read is caught on the next
    tick (not lost), a shrink re-indexes + warns once, and per-line warnings
    are deduped in-process.
"""

from __future__ import annotations

import io
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from unittest.mock import patch

# Project root is the parent of tests/
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import engram_log_indexer
from engram_log_indexer import Indexer


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_event(
    session_id: str = "sess-test",
    turn: int = 1,
    event_type: str = "engram.x.y",
    level: int = 1,
    data: dict | None = None,
    uuid: str | None = None,
    ts: str | None = None,
    tool_use_id: str | None = None,
) -> dict:
    """Build a minimal well-formed event dict."""
    import uuid as _uuid_mod
    return {
        "ts": ts or f"2026-05-17T15:00:0{turn}.000Z",
        "sessionId": session_id,
        "turn": turn,
        "uuid": uuid or str(_uuid_mod.uuid4()),
        "parentUuid": None,
        "event_type": event_type,
        "tool_use_id": tool_use_id,
        "level": level,
        "data": data or {},
    }


def _write_jsonl(path: Path, events: list[dict]) -> None:
    """Write a list of event dicts to a JSONL file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        for evt in events:
            f.write(json.dumps(evt, separators=(",", ":")) + "\n")


def _get_all_rows(db_path: Path) -> list[dict]:
    """Return all rows from events table as list of dicts."""
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute("SELECT * FROM events ORDER BY ts").fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def _table_exists(db_path: Path, table_name: str) -> bool:
    conn = sqlite3.connect(str(db_path))
    try:
        row = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
            (table_name,),
        ).fetchone()
        return row is not None
    finally:
        conn.close()


def _get_index_names(db_path: Path) -> set[str]:
    conn = sqlite3.connect(str(db_path))
    try:
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index'"
        ).fetchall()
        return {r[0] for r in rows}
    finally:
        conn.close()


def _get_column_names(db_path: Path, table: str) -> list[str]:
    conn = sqlite3.connect(str(db_path))
    try:
        rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
        return [r[1] for r in rows]
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Test base with tmp dir setup
# ---------------------------------------------------------------------------

class IndexerTestBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="engram_indexer_test_")
        self.logs_dir = Path(self.tmp) / "logs"
        self.sessions_dir = self.logs_dir / "sessions"
        self.db_path = self.logs_dir / "index.db"
        os.environ["ENGRAM_HOME"] = self.tmp

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)
        os.environ.pop("ENGRAM_HOME", None)


# ---------------------------------------------------------------------------
# Test classes
# ---------------------------------------------------------------------------

class TestEmptyLogsDir(IndexerTestBase):
    """Indexer runs without crash on empty logs dir; creates empty index.db."""

    def test_run_once_on_empty_dir(self):
        result = Indexer.run_once(logs_dir=self.logs_dir)
        self.assertEqual(result["files_processed"], 0)
        self.assertEqual(result["rows_inserted"], 0)

    def test_index_db_created_when_empty(self):
        Indexer.run_once(logs_dir=self.logs_dir)
        self.assertTrue(self.db_path.exists(), "index.db must be created even when no events")

    def test_events_table_created(self):
        Indexer.run_once(logs_dir=self.logs_dir)
        self.assertTrue(_table_exists(self.db_path, "events"))

    def test_wal_journal_mode(self):
        """index.db must use WAL journal mode to avoid write contention (#879)."""
        Indexer.run_once(logs_dir=self.logs_dir)
        conn = sqlite3.connect(str(self.db_path))
        mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
        conn.close()
        self.assertEqual(mode, "wal", "index.db must use WAL journal mode")

    def test_all_7_indexes_created(self):
        Indexer.run_once(logs_dir=self.logs_dir)
        idx = _get_index_names(self.db_path)
        expected = {
            "idx_events_type_ts",
            "idx_events_turn",
            "idx_events_session",
            "idx_events_tool_use",
            "idx_events_latency",
            "idx_events_hook_name",
            "idx_events_tool_name",
        }
        self.assertEqual(expected, idx & expected, f"Missing indexes: {expected - idx}")

    def test_schema_columns_match_spec(self):
        """Every column in DESIGN.md §6 must be present, in the right order."""
        Indexer.run_once(logs_dir=self.logs_dir)
        cols = _get_column_names(self.db_path, "events")
        expected_cols = [
            "uuid", "sessionId", "turn", "ts", "event_type", "tool_use_id", "level",
            "daemon_latency_ms", "fallback_to_fts",
            "hook_name", "hook_duration_ms", "hook_exit_code",
            "tool_name", "result_status",
            "data",
        ]
        self.assertEqual(expected_cols, cols)


class TestSingleFile5Events(IndexerTestBase):
    """5 events in one JSONL file → all 5 indexed, schema correct."""

    def setUp(self):
        super().setUp()
        self.session_id = "sess-abc"
        self.events = [
            _make_event(session_id=self.session_id, turn=i, event_type="engram.x.y", level=1)
            for i in range(1, 6)
        ]
        _write_jsonl(
            self.sessions_dir / f"{self.session_id}.l1.jsonl",
            self.events,
        )

    def test_all_5_rows_indexed(self):
        result = Indexer.run_once(logs_dir=self.logs_dir)
        self.assertEqual(result["rows_inserted"], 5)
        rows = _get_all_rows(self.db_path)
        self.assertEqual(len(rows), 5)

    def test_uuid_primary_key_matches(self):
        Indexer.run_once(logs_dir=self.logs_dir)
        rows = _get_all_rows(self.db_path)
        indexed_uuids = {r["uuid"] for r in rows}
        source_uuids = {e["uuid"] for e in self.events}
        self.assertEqual(indexed_uuids, source_uuids)

    def test_sessionId_column_populated(self):
        Indexer.run_once(logs_dir=self.logs_dir)
        rows = _get_all_rows(self.db_path)
        for row in rows:
            self.assertEqual(row["sessionId"], self.session_id)

    def test_level_column_populated(self):
        Indexer.run_once(logs_dir=self.logs_dir)
        rows = _get_all_rows(self.db_path)
        for row in rows:
            self.assertEqual(row["level"], 1)

    def test_data_column_is_valid_json(self):
        Indexer.run_once(logs_dir=self.logs_dir)
        rows = _get_all_rows(self.db_path)
        for row in rows:
            parsed = json.loads(row["data"])
            self.assertIsInstance(parsed, dict)


class TestIdempotency(IndexerTestBase):
    """Re-running on the same input must not duplicate rows."""

    def setUp(self):
        super().setUp()
        self.session_id = "sess-idem"
        events = [
            _make_event(session_id=self.session_id, turn=i, event_type="engram.x.y")
            for i in range(1, 4)
        ]
        _write_jsonl(self.sessions_dir / f"{self.session_id}.l1.jsonl", events)

    def test_second_run_inserts_no_new_rows(self):
        Indexer.run_once(logs_dir=self.logs_dir)
        result2 = Indexer.run_once(logs_dir=self.logs_dir)
        self.assertEqual(result2["rows_inserted"], 0)
        rows = _get_all_rows(self.db_path)
        self.assertEqual(len(rows), 3)

    def test_many_reruns_no_growth(self):
        for _ in range(5):
            Indexer.run_once(logs_dir=self.logs_dir)
        rows = _get_all_rows(self.db_path)
        self.assertEqual(len(rows), 3)


class TestPromotedColumns(IndexerTestBase):
    """Promoted columns populated correctly when present, NULL when absent."""

    def test_surface_fire_promoted_columns(self):
        session_id = "sess-surface"
        evt = _make_event(
            session_id=session_id,
            event_type="engram.surface.fire",
            level=1,
            data={
                "daemon_latency_ms": 187,
                "fallback_to_fts": False,
                "candidates_considered_count": 42,
            },
        )
        _write_jsonl(self.sessions_dir / f"{session_id}.l1.jsonl", [evt])
        Indexer.run_once(logs_dir=self.logs_dir)

        rows = _get_all_rows(self.db_path)
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["daemon_latency_ms"], 187)
        self.assertEqual(row["fallback_to_fts"], 0)  # False → 0
        # Non-surface columns must be NULL
        self.assertIsNone(row["hook_name"])
        self.assertIsNone(row["hook_duration_ms"])
        self.assertIsNone(row["hook_exit_code"])
        self.assertIsNone(row["tool_name"])
        self.assertIsNone(row["result_status"])

    def test_fallback_to_fts_true_becomes_1(self):
        session_id = "sess-fts"
        evt = _make_event(
            session_id=session_id,
            event_type="engram.surface.fire",
            level=1,
            data={"daemon_latency_ms": 50, "fallback_to_fts": True},
        )
        _write_jsonl(self.sessions_dir / f"{session_id}.l1.jsonl", [evt])
        Indexer.run_once(logs_dir=self.logs_dir)
        rows = _get_all_rows(self.db_path)
        self.assertEqual(rows[0]["fallback_to_fts"], 1)

    def test_hook_fire_promoted_columns(self):
        session_id = "sess-hook"
        evt = _make_event(
            session_id=session_id,
            event_type="engram.hook.fire",
            level=1,
            data={
                "hook_name": "engram-surface-hook",
                "hook_type": "UserPromptSubmit",
                "duration_ms": 187,
                "exit_code": 0,
                "stdout_bytes": 423,
            },
        )
        _write_jsonl(self.sessions_dir / f"{session_id}.l1.jsonl", [evt])
        Indexer.run_once(logs_dir=self.logs_dir)

        rows = _get_all_rows(self.db_path)
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["hook_name"], "engram-surface-hook")
        self.assertEqual(row["hook_duration_ms"], 187)
        self.assertEqual(row["hook_exit_code"], 0)
        # Non-hook columns must be NULL
        self.assertIsNone(row["daemon_latency_ms"])
        self.assertIsNone(row["fallback_to_fts"])
        self.assertIsNone(row["tool_name"])
        self.assertIsNone(row["result_status"])

    def test_tool_call_promoted_columns(self):
        session_id = "sess-tool"
        evt = _make_event(
            session_id=session_id,
            event_type="engram.tool.engram_call",
            level=1,
            tool_use_id="toolu_01XYZ",
            data={
                "tool_name": "engram_add_observation",
                "result_status": "created",
                "result_node_id": "ob_1942",
            },
        )
        _write_jsonl(self.sessions_dir / f"{session_id}.l1.jsonl", [evt])
        Indexer.run_once(logs_dir=self.logs_dir)

        rows = _get_all_rows(self.db_path)
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["tool_name"], "engram_add_observation")
        self.assertEqual(row["result_status"], "created")
        self.assertEqual(row["tool_use_id"], "toolu_01XYZ")
        # Non-tool columns must be NULL
        self.assertIsNone(row["daemon_latency_ms"])
        self.assertIsNone(row["fallback_to_fts"])
        self.assertIsNone(row["hook_name"])
        self.assertIsNone(row["hook_duration_ms"])
        self.assertIsNone(row["hook_exit_code"])

    def test_unrelated_event_type_all_promoted_null(self):
        """An event_type that doesn't match any promoted column mapping → all NULL."""
        session_id = "sess-unrelated"
        evt = _make_event(
            session_id=session_id,
            event_type="engram.recall_window.bump",
            level=1,
            data={"node_id": "ob_1942", "bump_alpha": 0.10},
        )
        _write_jsonl(self.sessions_dir / f"{session_id}.l1.jsonl", [evt])
        Indexer.run_once(logs_dir=self.logs_dir)

        rows = _get_all_rows(self.db_path)
        row = rows[0]
        for col in (
            "daemon_latency_ms", "fallback_to_fts", "hook_name",
            "hook_duration_ms", "hook_exit_code", "tool_name", "result_status",
        ):
            self.assertIsNone(row[col], f"Expected {col} to be NULL for unrelated event_type")

    def test_promoted_fields_absent_from_data_stay_null(self):
        """If a surface.fire event lacks daemon_latency_ms, column stays NULL."""
        session_id = "sess-absent"
        evt = _make_event(
            session_id=session_id,
            event_type="engram.surface.fire",
            level=1,
            data={"candidates_considered_count": 10},  # no daemon_latency_ms
        )
        _write_jsonl(self.sessions_dir / f"{session_id}.l1.jsonl", [evt])
        Indexer.run_once(logs_dir=self.logs_dir)

        rows = _get_all_rows(self.db_path)
        row = rows[0]
        self.assertIsNone(row["daemon_latency_ms"])
        self.assertIsNone(row["fallback_to_fts"])


class TestMalformedLines(IndexerTestBase):
    """Malformed line logged + skipped; other lines still indexed."""

    def test_bad_json_skipped_rest_indexed(self):
        session_id = "sess-mal"
        good1 = _make_event(session_id=session_id, turn=1, event_type="engram.x.y")
        good2 = _make_event(session_id=session_id, turn=2, event_type="engram.x.y")

        path = self.sessions_dir / f"{session_id}.l1.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(json.dumps(good1) + "\n")
            f.write("{THIS IS NOT JSON\n")  # malformed line
            f.write(json.dumps(good2) + "\n")

        import io
        from contextlib import redirect_stderr

        buf = io.StringIO()
        with redirect_stderr(buf):
            result = Indexer.run_once(logs_dir=self.logs_dir)

        # Both good events indexed
        self.assertEqual(result["rows_inserted"], 2)
        rows = _get_all_rows(self.db_path)
        self.assertEqual(len(rows), 2)

        # Error reported to stderr
        stderr_output = buf.getvalue()
        self.assertIn("malformed", stderr_output.lower())

    def test_entirely_malformed_file_skipped(self):
        """A file of all-bad lines produces 0 rows but no exception."""
        session_id = "sess-allbad"
        path = self.sessions_dir / f"{session_id}.l1.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("NOT JSON\nALSO NOT JSON\n")

        try:
            result = Indexer.run_once(logs_dir=self.logs_dir)
        except Exception as exc:
            self.fail(f"Indexer raised on all-malformed file: {exc}")

        rows = _get_all_rows(self.db_path)
        self.assertEqual(len(rows), 0)


class TestFullRebuild(IndexerTestBase):
    """full_rebuild drops and re-indexes from scratch."""

    def test_full_rebuild_after_run_once(self):
        session_id = "sess-rebuild"
        events = [
            _make_event(session_id=session_id, turn=i, event_type="engram.x.y")
            for i in range(1, 4)
        ]
        _write_jsonl(self.sessions_dir / f"{session_id}.l1.jsonl", events)

        # Initial index
        Indexer.run_once(logs_dir=self.logs_dir)
        rows_before = _get_all_rows(self.db_path)
        self.assertEqual(len(rows_before), 3)

        # full_rebuild should produce same result
        result = Indexer.full_rebuild(logs_dir=self.logs_dir)
        self.assertEqual(result["rows_inserted"], 3)
        rows_after = _get_all_rows(self.db_path)
        self.assertEqual(len(rows_after), 3)

        # Verify UUIDs match
        before_uuids = {r["uuid"] for r in rows_before}
        after_uuids = {r["uuid"] for r in rows_after}
        self.assertEqual(before_uuids, after_uuids)

    def test_full_rebuild_on_empty_dir(self):
        """full_rebuild on empty dir should not crash and produce empty index."""
        result = Indexer.full_rebuild(logs_dir=self.logs_dir)
        self.assertEqual(result["rows_inserted"], 0)
        self.assertTrue(self.db_path.exists())

    def test_full_rebuild_removes_stale_rows(self):
        """full_rebuild starts fresh — stale rows from previous runs are gone."""
        session_id = "sess-stale"
        events = [
            _make_event(session_id=session_id, turn=i, event_type="engram.x.y")
            for i in range(1, 4)
        ]
        jsonl_path = self.sessions_dir / f"{session_id}.l1.jsonl"
        _write_jsonl(jsonl_path, events)

        Indexer.run_once(logs_dir=self.logs_dir)
        self.assertEqual(len(_get_all_rows(self.db_path)), 3)

        # Simulate: the JSONL was reduced to 1 event (truncated/replaced)
        _write_jsonl(jsonl_path, [events[0]])

        # full_rebuild re-indexes from scratch — only 1 row now
        Indexer.full_rebuild(logs_dir=self.logs_dir)
        self.assertEqual(len(_get_all_rows(self.db_path)), 1)


class TestL1AndL2BothIndexed(IndexerTestBase):
    """L1 and L2 files are both indexed; level column populated correctly."""

    def test_l1_and_l2_both_indexed(self):
        session_id = "sess-levels"

        l1_events = [
            _make_event(session_id=session_id, turn=1, event_type="engram.surface.fire", level=1),
            _make_event(session_id=session_id, turn=2, event_type="engram.hook.fire", level=1),
        ]
        l2_events = [
            _make_event(session_id=session_id, turn=1, event_type="engram.surface.fire.l2", level=2),
        ]

        _write_jsonl(self.sessions_dir / f"{session_id}.l1.jsonl", l1_events)
        _write_jsonl(self.sessions_dir / f"{session_id}.l2.jsonl", l2_events)

        result = Indexer.run_once(logs_dir=self.logs_dir)
        self.assertEqual(result["files_processed"], 2)
        self.assertEqual(result["rows_inserted"], 3)

        rows = _get_all_rows(self.db_path)
        self.assertEqual(len(rows), 3)

        # Verify level column populated from event data
        levels = {r["level"] for r in rows}
        self.assertIn(1, levels)
        self.assertIn(2, levels)

    def test_level_column_from_event_data(self):
        """level comes from the event's own 'level' field, not the filename."""
        session_id = "sess-level-col"
        evt = _make_event(session_id=session_id, turn=1, event_type="engram.x.y", level=2)
        _write_jsonl(self.sessions_dir / f"{session_id}.l2.jsonl", [evt])

        Indexer.run_once(logs_dir=self.logs_dir)
        rows = _get_all_rows(self.db_path)
        self.assertEqual(rows[0]["level"], 2)


class TestIncrementalIndexing(IndexerTestBase):
    """Incremental indexing: new events appended to JSONL are picked up on next run."""

    def test_new_events_appended_after_first_run(self):
        session_id = "sess-inc"
        events_batch1 = [
            _make_event(session_id=session_id, turn=i, event_type="engram.x.y")
            for i in range(1, 4)
        ]
        jsonl_path = self.sessions_dir / f"{session_id}.l1.jsonl"
        _write_jsonl(jsonl_path, events_batch1)

        Indexer.run_once(logs_dir=self.logs_dir)
        self.assertEqual(len(_get_all_rows(self.db_path)), 3)

        # Append 2 more events with later timestamps
        events_batch2 = [
            _make_event(
                session_id=session_id,
                turn=i,
                event_type="engram.x.y",
                ts=f"2026-05-17T15:00:1{i-3}.000Z",  # later timestamp
            )
            for i in range(4, 6)
        ]
        with open(jsonl_path, "a", encoding="utf-8") as f:
            for evt in events_batch2:
                f.write(json.dumps(evt, separators=(",", ":")) + "\n")

        result = Indexer.run_once(logs_dir=self.logs_dir)
        self.assertEqual(result["rows_inserted"], 2)
        self.assertEqual(len(_get_all_rows(self.db_path)), 5)


# ---------------------------------------------------------------------------
# Size gate (alpha #2785)
# ---------------------------------------------------------------------------

class TestSizeGateSkipsUnchangedFile(IndexerTestBase):
    """A file whose on-disk size is unchanged between ticks must not be
    re-opened on the second tick."""

    def setUp(self):
        super().setUp()
        self.session_id = "sess-sizegate"
        events = [
            _make_event(session_id=self.session_id, turn=i, event_type="engram.x.y",
                        ts=f"2026-05-17T15:00:0{i}.000Z")
            for i in range(1, 3)
        ]
        _write_jsonl(self.sessions_dir / f"{self.session_id}.l1.jsonl", events)

    def test_unchanged_file_not_reopened_on_second_tick(self):
        Indexer.run_once(logs_dir=self.logs_dir)  # first tick: indexes + records size

        # Count opens by call count, not by row counts -- a row-count
        # assertion can't distinguish "skipped" from "read and found
        # nothing new" (both leave rows_inserted at 0).
        with patch.object(
            engram_log_indexer, "_index_file", wraps=engram_log_indexer._index_file
        ) as mock_idx:
            result = Indexer.run_once(logs_dir=self.logs_dir)

        mock_idx.assert_not_called()
        self.assertEqual(result["files_processed"], 0)
        self.assertEqual(result["files_skipped"], 1)


class TestSizeGateReindexesGrownFile(IndexerTestBase):
    """A file appended to between ticks is re-indexed; new rows land."""

    def test_appended_file_reindexed_new_rows_land(self):
        session_id = "sess-grow"
        path = self.sessions_dir / f"{session_id}.l1.jsonl"
        _write_jsonl(
            path,
            [_make_event(session_id=session_id, turn=1, event_type="engram.x.y",
                         ts="2026-05-17T15:00:01.000Z")],
        )

        r1 = Indexer.run_once(logs_dir=self.logs_dir)
        self.assertEqual(r1["rows_inserted"], 1)
        self.assertEqual(r1["files_skipped"], 0)

        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(
                _make_event(session_id=session_id, turn=2, event_type="engram.x.y",
                            ts="2026-05-17T15:00:02.000Z"),
                separators=(",", ":"),
            ) + "\n")

        r2 = Indexer.run_once(logs_dir=self.logs_dir)
        self.assertEqual(r2["rows_inserted"], 1)
        self.assertEqual(r2["files_processed"], 1)
        self.assertEqual(r2["files_skipped"], 0)
        rows = _get_all_rows(self.db_path)
        self.assertEqual(len(rows), 2)


class TestSizeGatePreReadStat(IndexerTestBase):
    """The gate must persist the stat SIZE TAKEN BEFORE THE READ, never a
    post-read re-stat or EOF offset (design doc §2). A post-read stat would
    silently drop any bytes that land between the pre-read stat and EOF,
    because it would record them as already-indexed."""

    def setUp(self):
        super().setUp()
        self.session_id = "sess-preread"
        self.jsonl_path = self.sessions_dir / f"{self.session_id}.l1.jsonl"
        self.initial_events = [
            _make_event(session_id=self.session_id, turn=1, event_type="engram.x.y",
                        ts="2026-05-17T15:00:01.000Z"),
            _make_event(session_id=self.session_id, turn=2, event_type="engram.x.y",
                        ts="2026-05-17T15:00:02.000Z"),
        ]
        _write_jsonl(self.jsonl_path, self.initial_events)
        self.late_event = _make_event(
            session_id=self.session_id, turn=3, event_type="engram.x.y",
            ts="2026-05-17T15:00:03.000Z",
        )

    def test_append_during_read_is_indexed_on_next_tick(self):
        """Simulate a write landing during the read window (right as
        _index_file finishes, modelling a write landing anywhere in the
        [pre-read stat, EOF] window). The appended event must be captured
        by the FOLLOWING run_once() call, not lost."""
        real_index_file = engram_log_indexer._index_file

        def _wrapped_index_file(*args, **kwargs):
            result = real_index_file(*args, **kwargs)
            with open(self.jsonl_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(self.late_event, separators=(",", ":")) + "\n")
            return result

        with patch.object(engram_log_indexer, "_index_file", side_effect=_wrapped_index_file):
            result1 = Indexer.run_once(logs_dir=self.logs_dir)
        self.assertEqual(result1["rows_inserted"], 2, "first tick indexes the initial 2 events")

        # Second tick, unpatched: the appended event must now be visible.
        result2 = Indexer.run_once(logs_dir=self.logs_dir)
        self.assertEqual(
            result2["rows_inserted"], 1,
            "the event appended during the first tick's read must be indexed "
            "on the next tick, not silently dropped",
        )
        rows = _get_all_rows(self.db_path)
        self.assertEqual(len(rows), 3)


class TestSizeGateShrink(IndexerTestBase):
    """A shrink (impossible under append-only, so a violated invariant) must
    re-index the file and warn exactly once, without crashing."""

    def test_shrink_reindexes_and_warns_once(self):
        session_id = "sess-shrink"
        path = self.sessions_dir / f"{session_id}.l1.jsonl"
        events = [
            _make_event(session_id=session_id, turn=i, event_type="engram.x.y",
                        ts=f"2026-05-17T15:00:0{i}.000Z")
            for i in range(1, 4)
        ]
        _write_jsonl(path, events)

        Indexer.run_once(logs_dir=self.logs_dir)  # records the larger size

        # Simulate a shrink (violates append-only) -- rewrite with less
        # content. The gate must not crash on this.
        _write_jsonl(path, [events[0]])

        buf = io.StringIO()
        with redirect_stderr(buf):
            result = Indexer.run_once(logs_dir=self.logs_dir)

        stderr_out = buf.getvalue()
        self.assertIn("shrank", stderr_out.lower())
        self.assertEqual(stderr_out.lower().count("shrank"), 1, "must warn exactly once")
        self.assertEqual(result["files_processed"], 1, "shrink must be treated as changed, not skipped")


class TestWarnOnceDedup(IndexerTestBase):
    """Per-line malformed/non-object/missing-ts warnings are deduped
    in-process: the same (path, lineno) warns only once per process, even
    across ticks on a still-growing file; a fresh process re-warns."""

    def test_warn_once_growing_file_then_fresh_process_warns_again(self):
        session_id = "sess-warn-combo"
        path = self.sessions_dir / f"{session_id}.l1.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)

        # Bad line at lineno 1.
        with open(path, "w", encoding="utf-8") as f:
            f.write("{THIS IS NOT JSON\n")

        buf1 = io.StringIO()
        with redirect_stderr(buf1):
            Indexer.run_once(logs_dir=self.logs_dir)
        self.assertEqual(buf1.getvalue().lower().count("malformed"), 1)

        # Append a good line -- file grows, so the gate re-reads from the
        # top and re-encounters the same bad line at the same lineno. Same
        # process: must NOT warn a second time.
        good1 = _make_event(session_id=session_id, turn=1, event_type="engram.x.y",
                             ts="2026-05-17T15:00:01.000Z")
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(good1, separators=(",", ":")) + "\n")

        buf2 = io.StringIO()
        with redirect_stderr(buf2):
            Indexer.run_once(logs_dir=self.logs_dir)
        self.assertEqual(
            buf2.getvalue().lower().count("malformed"), 0,
            "the same bad line at the same lineno must not warn twice in one process",
        )

        # Simulate a daemon restart: the in-process dedup set resets. A
        # restart alone doesn't force a re-read (file_state, in index.db,
        # persists across restarts) -- what makes the warning fire again in
        # production is the file continuing to grow afterward, so append
        # again too.
        engram_log_indexer._WARNED_LINES.clear()
        good2 = _make_event(session_id=session_id, turn=2, event_type="engram.x.y",
                             ts="2026-05-17T15:00:02.000Z")
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(good2, separators=(",", ":")) + "\n")

        buf3 = io.StringIO()
        with redirect_stderr(buf3):
            Indexer.run_once(logs_dir=self.logs_dir)
        self.assertEqual(
            buf3.getvalue().lower().count("malformed"), 1,
            "a fresh process (dedup set cleared) must re-report the same bad "
            "line once the file grows again",
        )


class TestFileStateMigration(IndexerTestBase):
    """An existing index.db with no file_state table (pre-#2785) gains one
    and indexes everything once."""

    def test_missing_file_state_table_gains_one_and_indexes_all(self):
        session_id = "sess-migrate"
        events = [
            _make_event(session_id=session_id, turn=i, event_type="engram.x.y",
                        ts=f"2026-05-17T15:00:0{i}.000Z")
            for i in range(1, 4)
        ]
        _write_jsonl(self.sessions_dir / f"{session_id}.l1.jsonl", events)

        # Establish an index.db as it would have looked before this feature:
        # events already indexed, but no file_state table.
        Indexer.run_once(logs_dir=self.logs_dir)
        conn = sqlite3.connect(str(self.db_path))
        try:
            conn.execute("DROP TABLE file_state")
            conn.commit()
        finally:
            conn.close()
        self.assertFalse(_table_exists(self.db_path, "file_state"))

        Indexer.run_once(logs_dir=self.logs_dir)
        self.assertTrue(_table_exists(self.db_path, "file_state"))
        # Already-indexed rows are deduped via the ts high-water mark +
        # INSERT OR IGNORE -- "indexes everything once" is a property of the
        # resulting table (3 unique rows), not of this tick's insert count.
        rows = _get_all_rows(self.db_path)
        self.assertEqual(len(rows), 3)


# ---------------------------------------------------------------------------
# Round-2 additions (alpha #2785 review): OSError-vs-FileNotFoundError split
# on the pre-read stat, single-commit-per-file crash safety, full_rebuild
# clearing the warn-once set, and orphan file_state pruning.
# ---------------------------------------------------------------------------

class TestOSErrorOnStatWarnsAndSkipsFile(IndexerTestBase):
    """A real I/O failure (PermissionError, standing in for the general
    OSError case) on the pre-read stat() must warn ONCE, skip only that
    file, and NOT abort the tick -- a sibling file must still be indexed.
    This is the discriminator against the pre-fix bare `except OSError`,
    which silently skipped forever with no warning at all."""

    def setUp(self):
        super().setUp()
        self.bad_session = "sess-badstat"
        self.good_session = "sess-goodstat"
        self.bad_path = self.sessions_dir / f"{self.bad_session}.l1.jsonl"
        self.good_path = self.sessions_dir / f"{self.good_session}.l1.jsonl"
        _write_jsonl(self.bad_path, [
            _make_event(session_id=self.bad_session, turn=1, event_type="engram.x.y",
                        ts="2026-05-17T15:00:01.000Z"),
        ])
        _write_jsonl(self.good_path, [
            _make_event(session_id=self.good_session, turn=1, event_type="engram.x.y",
                        ts="2026-05-17T15:00:01.000Z"),
        ])

    def _stat_side_effect(self, real_stat, path, *args, **kwargs):
        if str(path) == str(self.bad_path):
            raise PermissionError(f"[Errno 13] Permission denied: '{path}'")
        return real_stat(path, *args, **kwargs)

    def test_permission_error_warns_once_sibling_still_indexed(self):
        real_stat = os.stat

        buf1 = io.StringIO()
        with patch("engram_log_indexer.os.stat",
                    side_effect=lambda p, *a, **kw: self._stat_side_effect(real_stat, p, *a, **kw)):
            with redirect_stderr(buf1):
                result1 = Indexer.run_once(logs_dir=self.logs_dir)

        # The bad file is skipped; the tick is NOT aborted -- the sibling
        # good file is still indexed.
        self.assertEqual(result1["files_skipped"], 1)
        self.assertEqual(result1["files_processed"], 1, "sibling file must still be indexed")
        self.assertEqual(result1["rows_inserted"], 1)
        rows = _get_all_rows(self.db_path)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["sessionId"], self.good_session)

        stderr1 = buf1.getvalue()
        self.assertIn("cannot stat", stderr1.lower())
        self.assertEqual(stderr1.lower().count("cannot stat"), 1, "must warn exactly once")

        # Second tick, same permission failure -- deduped via _warn_once
        # (path, lineno=0): must NOT warn again in the same process.
        buf2 = io.StringIO()
        with patch("engram_log_indexer.os.stat",
                    side_effect=lambda p, *a, **kw: self._stat_side_effect(real_stat, p, *a, **kw)):
            with redirect_stderr(buf2):
                Indexer.run_once(logs_dir=self.logs_dir)
        self.assertEqual(
            buf2.getvalue().lower().count("cannot stat"), 0,
            "the same unreadable file must not warn a second time across ticks",
        )


class TestFileNotFoundOnStatStaysSilent(IndexerTestBase):
    """FileNotFoundError from stat() -- the file vanishing between glob()
    and stat() -- remains the benign, SILENT case: no warning at all,
    distinguishing it from the warn-and-skip OSError case above."""

    def setUp(self):
        super().setUp()
        self.session_id = "sess-vanished"
        self.path = self.sessions_dir / f"{self.session_id}.l1.jsonl"
        _write_jsonl(self.path, [
            _make_event(session_id=self.session_id, turn=1, event_type="engram.x.y",
                        ts="2026-05-17T15:00:01.000Z"),
        ])

    def test_filenotfound_silent_and_skipped(self):
        real_stat = os.stat

        def _stat_side_effect(path, *args, **kwargs):
            if str(path) == str(self.path):
                raise FileNotFoundError(f"[Errno 2] No such file or directory: '{path}'")
            return real_stat(path, *args, **kwargs)

        buf = io.StringIO()
        with patch("engram_log_indexer.os.stat", side_effect=_stat_side_effect):
            with redirect_stderr(buf):
                result = Indexer.run_once(logs_dir=self.logs_dir)

        self.assertEqual(result["files_skipped"], 1)
        self.assertEqual(result["files_processed"], 0)
        self.assertEqual(buf.getvalue(), "", "FileNotFoundError must stay completely silent")


class _CrashBeforeCommitConn:
    """Proxies a real sqlite3.Connection, raising when it sees the
    file_state INSERT OR REPLACE statement -- simulating a process crash
    landing between _index_file's row writes (already executed against
    the real connection) and run_once's ensuing single commit.

    sqlite3.Connection itself cannot be monkeypatched directly (its
    methods are read-only slots on an immutable C type -- confirmed: both
    class- and instance-level patch attempts on `execute` raise
    TypeError/AttributeError), hence this proxy sits at the
    sqlite3.connect() boundary instead.
    """

    def __init__(self, real_conn):
        self._real = real_conn

    def execute(self, sql, *args, **kwargs):
        if sql.strip().startswith("INSERT OR REPLACE INTO file_state"):
            raise RuntimeError("simulated crash before commit")
        return self._real.execute(sql, *args, **kwargs)

    def __getattr__(self, name):
        return getattr(self._real, name)


class TestCrashBetweenRowsAndCommitIsSafe(IndexerTestBase):
    """Item 2 (round 2): _index_file's row writes and run_once's file_state
    upsert now share ONE commit, in the same transaction. A failure landing
    between them must lose the rows (sqlite3 rolls back an uncommitted
    transaction on close -- confirmed empirically) rather than durably
    record file_state while the rows it accounts for are gone. The next
    tick must then re-index the file cleanly: no rows lost, no duplicates,
    no file_state row left behind describing rows that don't exist."""

    def test_crash_before_commit_then_clean_reindex_next_tick(self):
        session_id = "sess-crash"
        path = self.sessions_dir / f"{session_id}.l1.jsonl"
        _write_jsonl(path, [
            _make_event(session_id=session_id, turn=1, event_type="engram.x.y",
                        ts="2026-05-17T15:00:01.000Z"),
            _make_event(session_id=session_id, turn=2, event_type="engram.x.y",
                        ts="2026-05-17T15:00:02.000Z"),
        ])

        real_connect = sqlite3.connect

        def _connect_side_effect(*args, **kwargs):
            return _CrashBeforeCommitConn(real_connect(*args, **kwargs))

        with patch("engram_log_indexer.sqlite3.connect", side_effect=_connect_side_effect):
            with self.assertRaises(RuntimeError):
                Indexer.run_once(logs_dir=self.logs_dir)

        # Nothing survived the crash: no event rows, no file_state row.
        rows = _get_all_rows(self.db_path)
        self.assertEqual(len(rows), 0, "uncommitted rows must not survive the crash")
        conn = sqlite3.connect(str(self.db_path))
        try:
            fs_row = conn.execute(
                "SELECT 1 FROM file_state WHERE path = ?", (str(path),)
            ).fetchone()
        finally:
            conn.close()
        self.assertIsNone(fs_row, "file_state must never be durable while its rows are not")

        # Next tick, unpatched: full clean re-index, no rows lost, no dup.
        result = Indexer.run_once(logs_dir=self.logs_dir)
        self.assertEqual(result["rows_inserted"], 2)
        rows = _get_all_rows(self.db_path)
        self.assertEqual(len(rows), 2)


class TestFullRebuildClearsWarnSet(IndexerTestBase):
    """full_rebuild is the recovery path for a corrupt/stale index -- the
    "someone is looking" moment the _WARNED_LINES design comment says
    should re-report everything. It must clear the in-process warn-once
    dedup set before re-indexing, even within the SAME process where the
    bad line already warned once via a prior run_once()."""

    def test_full_rebuild_clears_warned_lines(self):
        session_id = "sess-rebuild-warn"
        path = self.sessions_dir / f"{session_id}.l1.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write("{THIS IS NOT JSON\n")

        buf1 = io.StringIO()
        with redirect_stderr(buf1):
            Indexer.run_once(logs_dir=self.logs_dir)
        self.assertEqual(buf1.getvalue().lower().count("malformed"), 1)

        # Same process, same bad line, same lineno -- a plain run_once()
        # would NOT re-warn (per TestWarnOnceDedup above). full_rebuild
        # must, because it is the dedicated recovery path.
        buf2 = io.StringIO()
        with redirect_stderr(buf2):
            Indexer.full_rebuild(logs_dir=self.logs_dir)
        self.assertEqual(
            buf2.getvalue().lower().count("malformed"), 1,
            "full_rebuild must clear the warn-once set and re-report the bad line",
        )


class TestOrphanFileStatePruning(IndexerTestBase):
    """A file_state row for a session file removed from disk must be
    pruned; a surviving file's row must be left completely untouched --
    same size, same indexed_at timestamp, from the tick that created it.

    The two files are deliberately on DIFFERENT levels (l1 vs l2): this is
    what makes the round-2 mutation control ("prune scoped to a single
    level's own glob, executed inside the level loop -- orphans l2 rows")
    visible. A same-level pairing would not discriminate against that
    plausible-wrong implementation -- see the round-2 handoff notes.
    """

    def test_deleted_file_prunes_row_survivor_untouched(self):
        keep_session = "sess-keep"
        gone_session = "sess-gone"
        keep_path = self.sessions_dir / f"{keep_session}.l2.jsonl"
        gone_path = self.sessions_dir / f"{gone_session}.l1.jsonl"
        _write_jsonl(keep_path, [
            _make_event(session_id=keep_session, turn=1, event_type="engram.x.y",
                        ts="2026-05-17T15:00:01.000Z", level=2),
        ])
        _write_jsonl(gone_path, [
            _make_event(session_id=gone_session, turn=1, event_type="engram.x.y",
                        ts="2026-05-17T15:00:01.000Z"),
        ])

        r1 = Indexer.run_once(logs_dir=self.logs_dir)
        self.assertEqual(r1["file_state_pruned"], 0)

        conn = sqlite3.connect(str(self.db_path))
        try:
            keep_row_before = conn.execute(
                "SELECT size, indexed_at FROM file_state WHERE path = ?",
                (str(keep_path),),
            ).fetchone()
        finally:
            conn.close()
        self.assertIsNotNone(keep_row_before)

        gone_path.unlink()

        r2 = Indexer.run_once(logs_dir=self.logs_dir)
        self.assertEqual(r2["file_state_pruned"], 1)
        self.assertEqual(
            r2["files_skipped"], 1,
            "the untouched l2 file must be SKIPPED (unchanged size), not re-indexed",
        )

        conn = sqlite3.connect(str(self.db_path))
        try:
            gone_row = conn.execute(
                "SELECT 1 FROM file_state WHERE path = ?", (str(gone_path),)
            ).fetchone()
            keep_row_after = conn.execute(
                "SELECT size, indexed_at FROM file_state WHERE path = ?",
                (str(keep_path),),
            ).fetchone()
        finally:
            conn.close()
        self.assertIsNone(gone_row, "deleted file's file_state row must be pruned")
        self.assertEqual(
            keep_row_after, keep_row_before,
            "surviving file's row (a DIFFERENT level than the deleted file) must be untouched",
        )


if __name__ == "__main__":
    unittest.main()
