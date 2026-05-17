"""Unit tests for engram_log_indexer.

Covers:
  - Empty logs dir: runs without crash, creates empty index.db
  - Single JSONL file with 5 events: all 5 indexed, schema correct
  - Re-run on same input: no duplicate rows (idempotency)
  - Promoted columns populated when present, NULL when absent
  - Malformed line (bad JSON): logged + skipped, other lines still indexed
  - full_rebuild: drops and re-indexes from scratch
  - L1 and L2 files both indexed (level column populated correctly)
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

# Project root is the parent of tests/
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

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


if __name__ == "__main__":
    unittest.main()
