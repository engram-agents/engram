"""Integration tests for the ENGRAM event-logging pipeline.

Exercises the full emitter → JSONL → indexer → query pipeline end-to-end.
Spec: ~/borges-desk/projects/engram-logging/DESIGN.md
Tracking: alpha #175

Each test class corresponds to a distinct pipeline property:
  TestEmitterToIndexerRoundtrip       — emit N events, run indexer, verify all land in index.db
  TestEventSchemaCompliance           — emitted JSONL lines conform to DESIGN.md §3 schema
  TestCohortAlignedQueries            — per-turn GROUP BY rollup returns correct counts
  TestPrivacyPartitioning             — L1/L2 files separate, level column correct, filtered queries work
  TestIndexerHighWaterMarkLevelScoped — L2 event earlier than L1 is NOT silently skipped
  TestMalformedDataResilience         — corrupted line logged to stderr, clean lines survive, no crash
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
import uuid as _uuid_mod
from contextlib import redirect_stderr
from datetime import datetime, timezone, timedelta
from pathlib import Path

# Project root is parent of tests/
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import engram_log_emitter
from engram_log_emitter import Emitter
from engram_log_indexer import Indexer


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _make_ts(offset_seconds: float = 0.0) -> str:
    """Return an ISO timestamp (UTC) offset by the given number of seconds from epoch start."""
    # Use a fixed base so tests are deterministic and sortable.
    base = datetime(2026, 5, 17, 15, 0, 0, tzinfo=timezone.utc)
    dt = base + timedelta(seconds=offset_seconds)
    ms = dt.microsecond // 1000
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{ms:03d}Z"


def _make_event_dict(
    session_id: str = "sess-test",
    turn: int = 1,
    event_type: str = "engram.x.y",
    level: int = 1,
    data: dict | None = None,
    ts: str | None = None,
    tool_use_id: str | None = None,
    parent_uuid: str | None = None,
) -> dict:
    """Build a minimal well-formed event dict (for writing directly to JSONL)."""
    return {
        "ts": ts or _make_ts(turn),
        "sessionId": session_id,
        "turn": turn,
        "uuid": str(_uuid_mod.uuid4()),
        "parentUuid": parent_uuid,
        "event_type": event_type,
        "tool_use_id": tool_use_id,
        "level": level,
        "data": data or {},
    }


def _write_jsonl(path: Path, events: list[dict]) -> None:
    """Write a list of event dicts to a JSONL file (compact JSON, one event per line)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        for evt in events:
            f.write(json.dumps(evt, separators=(",", ":")) + "\n")


def _append_jsonl(path: Path, events: list[dict]) -> None:
    """Append event dicts to an existing JSONL file."""
    with open(path, "a", encoding="utf-8") as f:
        for evt in events:
            f.write(json.dumps(evt, separators=(",", ":")) + "\n")


def _query_db(db_path: Path, sql: str, params: tuple = ()) -> list[dict]:
    """Run a query against index.db and return rows as list of dicts."""
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(sql, params).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def _count_rows(db_path: Path, where: str = "", params: tuple = ()) -> int:
    sql = f"SELECT COUNT(*) FROM events{(' WHERE ' + where) if where else ''}"
    conn = sqlite3.connect(str(db_path))
    try:
        return conn.execute(sql, params).fetchone()[0]
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Test base — temp dir + emitter singleton reset
# ---------------------------------------------------------------------------

class IntegrationTestBase(unittest.TestCase):
    """Sets up an isolated temp dir with its own logs_dir + resets emitter singleton."""

    def setUp(self):
        # Reset module-level emitter singleton so each test starts fresh.
        engram_log_emitter._EMITTER_INSTANCE = None

        self.tmp = tempfile.mkdtemp(prefix="engram_int_test_")
        self.logs_dir = Path(self.tmp) / "logs"
        self.sessions_dir = self.logs_dir / "sessions"
        self.db_path = self.logs_dir / "index.db"
        # Point ENGRAM_HOME at tmp so emitter won't try to reach the real db.
        os.environ["ENGRAM_HOME"] = self.tmp

    def tearDown(self):
        engram_log_emitter._EMITTER_INSTANCE = None
        shutil.rmtree(self.tmp, ignore_errors=True)
        os.environ.pop("ENGRAM_HOME", None)

    def _fresh_emitter(self, session_id: str = "sess-int", turn: int = 1) -> Emitter:
        """Return a freshly initialised emitter bound to the test logs_dir."""
        return Emitter.init(
            session_id=session_id,
            transcript_path=f"/tmp/fake-{session_id}.jsonl",
            initial_turn=turn,
            logs_dir=self.logs_dir,
        )

    def _run_indexer(self) -> dict:
        """Run indexer over the test logs_dir and return summary."""
        return Indexer.run_once(logs_dir=self.logs_dir)


# ---------------------------------------------------------------------------
# 1. TestEmitterToIndexerRoundtrip
# ---------------------------------------------------------------------------

class TestEmitterToIndexerRoundtrip(IntegrationTestBase):
    """Emit 10+ mixed events via the emitter, index them, verify all land in DB."""

    def test_all_10_events_appear_in_index(self):
        """Emit 10 events across 3 event_types and 2 levels; verify all in index.db."""
        session_id = "sess-roundtrip"
        emitter = self._fresh_emitter(session_id=session_id, turn=10)

        # 4 × engram.surface.fire (L1)
        for i in range(4):
            emitter.emit(
                event_type="engram.surface.fire",
                level=1,
                data={"daemon_latency_ms": 100 + i * 10, "fallback_to_fts": False, "candidates_considered_count": i},
            )

        # 3 × engram.tool.engram_call (L1)
        tools = ["engram_add_observation", "engram_derive", "engram_query"]
        for i, tool in enumerate(tools):
            emitter.emit(
                event_type="engram.tool.engram_call",
                level=1,
                tool_use_id=f"toolu_0{i}",
                data={"tool_name": tool, "result_status": "created", "result_node_id": f"ob_{i}"},
            )

        # 2 × engram.hook.fire (L1)
        for hook in ("engram-stop-hook", "engram-session-start-hook"):
            emitter.emit(
                event_type="engram.hook.fire",
                level=1,
                data={"hook_name": hook, "hook_type": "Stop", "duration_ms": 42, "exit_code": 0, "stdout_bytes": 100, "stderr_bytes": 0},
            )

        # 1 × L2 event (content level)
        emitter.emit(
            event_type="engram.surface.fire.l2",
            level=2,
            data={"prompt_text": "example user prompt for testing"},
        )

        # Confirm JSONL files exist before indexing
        l1_path = self.sessions_dir / f"{session_id}.l1.jsonl"
        l2_path = self.sessions_dir / f"{session_id}.l2.jsonl"
        self.assertTrue(l1_path.exists(), "L1 JSONL file must exist after emitting L1 events")
        self.assertTrue(l2_path.exists(), "L2 JSONL file must exist after emitting L2 events")

        # Count JSONL lines (9 L1 events + 1 L2 event)
        l1_lines = [ln for ln in l1_path.read_text().strip().split("\n") if ln]
        l2_lines = [ln for ln in l2_path.read_text().strip().split("\n") if ln]
        self.assertEqual(len(l1_lines), 9, "9 L1 events must be in the L1 JSONL file")
        self.assertEqual(len(l2_lines), 1, "1 L2 event must be in the L2 JSONL file")

        # Run indexer
        result = self._run_indexer()
        self.assertEqual(result["files_processed"], 2)
        self.assertEqual(result["rows_inserted"], 10)

        # Verify all 10 in the index
        total = _count_rows(self.db_path)
        self.assertEqual(total, 10)

    def test_promoted_columns_correct_for_each_event_type(self):
        """Verify indexer promoted columns are populated correctly for all 3 event types."""
        session_id = "sess-promoted"
        emitter = self._fresh_emitter(session_id=session_id, turn=5)

        # Emit one of each
        emitter.emit(
            "engram.surface.fire", 1,
            {"daemon_latency_ms": 187, "fallback_to_fts": True, "candidates_considered_count": 42},
        )
        emitter.emit(
            "engram.hook.fire", 1,
            {"hook_name": "engram-stop-hook", "hook_type": "Stop", "duration_ms": 12, "exit_code": 0, "stdout_bytes": 50, "stderr_bytes": 0},
        )
        emitter.emit(
            "engram.tool.engram_call", 1,
            {"tool_name": "engram_add_observation", "result_status": "created", "result_node_id": "ob_999"},
            tool_use_id="toolu_xyz",
        )

        self._run_indexer()

        rows = _query_db(self.db_path, "SELECT * FROM events ORDER BY event_type")
        self.assertEqual(len(rows), 3)

        by_type = {r["event_type"]: r for r in rows}

        # surface.fire promoted cols
        sf = by_type["engram.surface.fire"]
        self.assertEqual(sf["daemon_latency_ms"], 187)
        self.assertEqual(sf["fallback_to_fts"], 1)  # bool True → INTEGER 1
        self.assertIsNone(sf["hook_name"])
        self.assertIsNone(sf["tool_name"])

        # hook.fire promoted cols
        hf = by_type["engram.hook.fire"]
        self.assertEqual(hf["hook_name"], "engram-stop-hook")
        self.assertEqual(hf["hook_duration_ms"], 12)
        self.assertEqual(hf["hook_exit_code"], 0)
        self.assertIsNone(hf["daemon_latency_ms"])
        self.assertIsNone(hf["tool_name"])

        # tool.engram_call promoted cols
        tc = by_type["engram.tool.engram_call"]
        self.assertEqual(tc["tool_name"], "engram_add_observation")
        self.assertEqual(tc["result_status"], "created")
        self.assertEqual(tc["tool_use_id"], "toolu_xyz")
        self.assertIsNone(tc["daemon_latency_ms"])
        self.assertIsNone(tc["hook_name"])

    def test_level_column_matches_source_file(self):
        """level column in index.db must reflect the event's level field."""
        session_id = "sess-level-match"
        emitter = self._fresh_emitter(session_id=session_id, turn=1)
        emitter.emit("engram.surface.fire", 1, {"daemon_latency_ms": 50, "fallback_to_fts": False})
        emitter.emit("engram.surface.fire.l2", 2, {"prompt_text": "secret prompt text"})

        self._run_indexer()

        rows = _query_db(self.db_path, "SELECT level, event_type FROM events ORDER BY level")
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["level"], 1)
        self.assertEqual(rows[1]["level"], 2)


# ---------------------------------------------------------------------------
# 2. TestEventSchemaCompliance
# ---------------------------------------------------------------------------

class TestEventSchemaCompliance(IntegrationTestBase):
    """Emitted JSONL lines conform to DESIGN.md §3 fixed top-level schema."""

    # All fixed top-level fields required by the spec (DESIGN.md §3)
    REQUIRED_FIELDS = frozenset({
        "ts", "sessionId", "turn", "uuid", "parentUuid",
        "event_type", "tool_use_id", "level", "data",
    })

    def _emit_one(self, emitter: Emitter, event_type: str, level: int, data: dict, **kwargs) -> dict:
        """Emit one event and return the parsed JSONL line."""
        lvl_suffix = f".l{level}"
        session_id = emitter.session_id
        jsonl_path = self.sessions_dir / f"{session_id}{lvl_suffix}.jsonl"

        # Record current line count to find the new line
        pre_count = 0
        if jsonl_path.exists():
            pre_count = len([l for l in jsonl_path.read_text().strip().split("\n") if l])

        emitter.emit(event_type=event_type, level=level, data=data, **kwargs)

        lines = [l for l in jsonl_path.read_text().strip().split("\n") if l]
        self.assertGreater(len(lines), pre_count, "emit() should have written a new line")
        return json.loads(lines[-1])

    def test_surface_fire_schema_compliance(self):
        """engram.surface.fire events have all required fields with correct types."""
        emitter = self._fresh_emitter(session_id="sess-schema-sf", turn=51)
        evt = self._emit_one(
            emitter,
            event_type="engram.surface.fire",
            level=1,
            data={"daemon_latency_ms": 150, "fallback_to_fts": False, "candidates_considered_count": 20},
        )
        self._assert_schema_valid(evt)
        self.assertEqual(evt["event_type"], "engram.surface.fire")
        self.assertEqual(evt["level"], 1)

    def test_tool_engram_call_schema_compliance(self):
        """engram.tool.engram_call events have all required fields."""
        emitter = self._fresh_emitter(session_id="sess-schema-tc", turn=10)
        evt = self._emit_one(
            emitter,
            event_type="engram.tool.engram_call",
            level=1,
            data={"tool_name": "engram_derive", "result_status": "created", "result_node_id": "dv_0001"},
            tool_use_id="toolu_abc123",
        )
        self._assert_schema_valid(evt)
        self.assertEqual(evt["tool_use_id"], "toolu_abc123")
        self.assertIsInstance(evt["data"], dict)

    def test_hook_fire_schema_compliance(self):
        """engram.hook.fire events have all required fields."""
        emitter = self._fresh_emitter(session_id="sess-schema-hf", turn=7)
        evt = self._emit_one(
            emitter,
            event_type="engram.hook.fire",
            level=1,
            data={"hook_name": "engram-stop-hook", "hook_type": "Stop", "duration_ms": 5, "exit_code": 0, "stdout_bytes": 30, "stderr_bytes": 0},
        )
        self._assert_schema_valid(evt)

    def test_l2_event_schema_compliance(self):
        """L2 events (level=2) also conform to the fixed top-level schema."""
        emitter = self._fresh_emitter(session_id="sess-schema-l2", turn=3)
        evt = self._emit_one(
            emitter,
            event_type="engram.surface.fire.l2",
            level=2,
            data={"prompt_text": "test prompt"},
        )
        self._assert_schema_valid(evt)
        self.assertEqual(evt["level"], 2)

    def test_parent_uuid_chaining(self):
        """parentUuid field carries through the chain correctly."""
        emitter = self._fresh_emitter(session_id="sess-parent-uuid", turn=1)
        u1 = emitter.emit("engram.surface.fire", 1, {"daemon_latency_ms": 10, "fallback_to_fts": False})
        self.assertIsNotNone(u1)

        jsonl_path = self.sessions_dir / "sess-parent-uuid.l1.jsonl"
        lines_before = len([l for l in jsonl_path.read_text().strip().split("\n") if l])

        emitter.emit("engram.surface.fire", 1, {"daemon_latency_ms": 20, "fallback_to_fts": False}, parent_uuid=u1)

        lines = [json.loads(l) for l in jsonl_path.read_text().strip().split("\n") if l]
        self.assertEqual(len(lines), 2)
        self.assertEqual(lines[1]["parentUuid"], u1)
        self.assertEqual(lines[0]["parentUuid"], None)

    def _assert_schema_valid(self, evt: dict) -> None:
        """Assert all fixed top-level fields present with correct types."""
        # All required fields present
        for field in self.REQUIRED_FIELDS:
            self.assertIn(field, evt, f"Missing required field: {field}")

        # data is a dict
        self.assertIsInstance(evt["data"], dict, "data must be a dict")

        # level is 1 or 2
        self.assertIn(evt["level"], (1, 2), f"level must be 1 or 2, got {evt['level']}")

        # ts parses as ISO timestamp (UTC Z-suffix)
        ts = evt["ts"]
        self.assertIsInstance(ts, str)
        try:
            datetime.strptime(ts, "%Y-%m-%dT%H:%M:%S.%fZ")
        except ValueError:
            self.fail(f"ts {ts!r} does not parse as ISO timestamp (expected YYYY-MM-DDTHH:MM:SS.mmmZ)")

        # uuid is a valid UUID4 string
        uid = evt["uuid"]
        self.assertIsInstance(uid, str)
        try:
            parsed = _uuid_mod.UUID(uid)
            self.assertEqual(parsed.version, 4, f"uuid must be version 4, got version {parsed.version}")
        except ValueError:
            self.fail(f"uuid {uid!r} is not a valid UUID string")

        # sessionId is a non-empty string
        self.assertIsInstance(evt["sessionId"], str)
        self.assertTrue(evt["sessionId"], "sessionId must be non-empty")

        # turn is an integer (or None only if config was absent — but in tests we always pass initial_turn)
        self.assertIsInstance(evt["turn"], int, "turn must be an integer")

        # event_type starts with "engram."
        self.assertTrue(evt["event_type"].startswith("engram."), "event_type must start with 'engram.'")


# ---------------------------------------------------------------------------
# 3. TestCohortAlignedQueries
# ---------------------------------------------------------------------------

class TestCohortAlignedQueries(IntegrationTestBase):
    """Emit events across simulated turns; verify per-turn GROUP BY rollup is correct."""

    def test_per_turn_group_by_returns_correct_counts(self):
        """Emit 3+2+4 events across turns 10, 11, 12; GROUP BY turn must return 3 rows."""
        session_id = "sess-cohort"
        # We can't reset the turn inside a single emitter instance (it caches for 60s).
        # Strategy: write JSONL directly with explicit turns, then index.
        # This is a pipeline integration test — exercising the indexer's turn column.
        events = []
        # Turn 10: 3 events
        for i in range(3):
            events.append(_make_event_dict(
                session_id=session_id, turn=10,
                event_type="engram.surface.fire", level=1,
                data={"daemon_latency_ms": 100 + i, "fallback_to_fts": False},
                ts=_make_ts(10.0 + i * 0.1),
            ))
        # Turn 11: 2 events
        for i in range(2):
            events.append(_make_event_dict(
                session_id=session_id, turn=11,
                event_type="engram.hook.fire", level=1,
                data={"hook_name": "engram-stop-hook", "hook_type": "Stop", "duration_ms": 10, "exit_code": 0, "stdout_bytes": 20, "stderr_bytes": 0},
                ts=_make_ts(11.0 + i * 0.1),
            ))
        # Turn 12: 4 events
        for i in range(4):
            events.append(_make_event_dict(
                session_id=session_id, turn=12,
                event_type="engram.tool.engram_call", level=1,
                data={"tool_name": "engram_query", "result_status": "ok"},
                ts=_make_ts(12.0 + i * 0.1),
            ))

        _write_jsonl(self.sessions_dir / f"{session_id}.l1.jsonl", events)
        result = self._run_indexer()
        self.assertEqual(result["rows_inserted"], 9)

        # GROUP BY turn
        rows = _query_db(
            self.db_path,
            "SELECT turn, COUNT(*) as cnt FROM events GROUP BY turn ORDER BY turn",
        )
        self.assertEqual(len(rows), 3, "Must have exactly 3 distinct turns")
        by_turn = {r["turn"]: r["cnt"] for r in rows}
        self.assertEqual(by_turn[10], 3)
        self.assertEqual(by_turn[11], 2)
        self.assertEqual(by_turn[12], 4)

    def test_emitter_turn_flows_through_to_index(self):
        """Turn set via initial_turn on emitter appears in index.db turn column."""
        session_id = "sess-turn-flow"
        emitter = self._fresh_emitter(session_id=session_id, turn=77)
        emitter.emit("engram.surface.fire", 1, {"daemon_latency_ms": 50, "fallback_to_fts": False})
        emitter.emit("engram.hook.fire", 1, {"hook_name": "engram-stop-hook", "hook_type": "Stop", "duration_ms": 5, "exit_code": 0, "stdout_bytes": 10, "stderr_bytes": 0})

        self._run_indexer()

        rows = _query_db(self.db_path, "SELECT turn FROM events")
        self.assertEqual(len(rows), 2)
        for row in rows:
            self.assertEqual(row["turn"], 77, "All events must carry the emitter's initial_turn=77")

    def test_multi_session_turn_isolation(self):
        """Events from different sessions with the same turn value are independent."""
        for sess, turn in [("sess-A", 5), ("sess-B", 5), ("sess-C", 7)]:
            events = [_make_event_dict(
                session_id=sess, turn=turn,
                event_type="engram.hook.fire", level=1,
                data={"hook_name": "engram-stop-hook", "hook_type": "Stop", "duration_ms": 1, "exit_code": 0, "stdout_bytes": 5, "stderr_bytes": 0},
                ts=_make_ts(float(turn)),
            )]
            _write_jsonl(self.sessions_dir / f"{sess}.l1.jsonl", events)

        self._run_indexer()

        # Per-session per-turn query
        rows = _query_db(self.db_path, "SELECT sessionId, turn, COUNT(*) as cnt FROM events GROUP BY sessionId, turn ORDER BY sessionId")
        self.assertEqual(len(rows), 3)
        by_sess = {r["sessionId"]: (r["turn"], r["cnt"]) for r in rows}
        self.assertEqual(by_sess["sess-A"], (5, 1))
        self.assertEqual(by_sess["sess-B"], (5, 1))
        self.assertEqual(by_sess["sess-C"], (7, 1))


# ---------------------------------------------------------------------------
# 4. TestPrivacyPartitioning
# ---------------------------------------------------------------------------

class TestPrivacyPartitioning(IntegrationTestBase):
    """L1/L2 events land in separate files; indexer handles both; queries filter correctly."""

    def test_separate_files_materialize_on_disk(self):
        """Emit L1 and L2 events; verify separate JSONL files on disk."""
        session_id = "sess-privacy"
        emitter = self._fresh_emitter(session_id=session_id, turn=1)
        emitter.emit("engram.surface.fire", 1, {"daemon_latency_ms": 10, "fallback_to_fts": False})
        emitter.emit("engram.surface.fire.l2", 2, {"prompt_text": "secret"})

        l1 = self.sessions_dir / f"{session_id}.l1.jsonl"
        l2 = self.sessions_dir / f"{session_id}.l2.jsonl"
        self.assertTrue(l1.exists(), "L1 JSONL file must exist")
        self.assertTrue(l2.exists(), "L2 JSONL file must exist")

        # L1 file must NOT contain the L2 event
        l1_events = [json.loads(ln) for ln in l1.read_text().strip().split("\n") if ln]
        self.assertEqual(len(l1_events), 1)
        self.assertEqual(l1_events[0]["level"], 1)
        self.assertNotIn("prompt_text", l1_events[0]["data"])

        # L2 file must NOT contain the L1 event
        l2_events = [json.loads(ln) for ln in l2.read_text().strip().split("\n") if ln]
        self.assertEqual(len(l2_events), 1)
        self.assertEqual(l2_events[0]["level"], 2)

    def test_indexer_populates_level_column_correctly(self):
        """Indexer sets level column from event data for both L1 and L2 events."""
        session_id = "sess-level-col"
        emitter = self._fresh_emitter(session_id=session_id, turn=2)
        emitter.emit("engram.surface.fire", 1, {"daemon_latency_ms": 50, "fallback_to_fts": False})
        emitter.emit("engram.surface.fire.l2", 2, {"prompt_text": "test"})

        self._run_indexer()

        rows = _query_db(self.db_path, "SELECT level FROM events ORDER BY level")
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["level"], 1)
        self.assertEqual(rows[1]["level"], 2)

    def test_l1_only_query_excludes_l2_events(self):
        """WHERE level = 1 query returns only L1 events."""
        session_id = "sess-l1-query"
        emitter = self._fresh_emitter(session_id=session_id, turn=3)
        # Emit 3 L1 events and 2 L2 events
        for i in range(3):
            emitter.emit("engram.surface.fire", 1, {"daemon_latency_ms": 10 + i, "fallback_to_fts": False})
        for i in range(2):
            emitter.emit("engram.surface.fire.l2", 2, {"prompt_text": f"prompt {i}"})

        self._run_indexer()

        l1_count = _count_rows(self.db_path, "level = 1")
        l2_count = _count_rows(self.db_path, "level = 2")
        total = _count_rows(self.db_path)

        self.assertEqual(l1_count, 3, "WHERE level=1 must return exactly 3 rows")
        self.assertEqual(l2_count, 2, "WHERE level=2 must return exactly 2 rows")
        self.assertEqual(total, 5, "Total must be 5 (L1 + L2 both indexed)")

    def test_l1_query_correct_after_l2_file_deleted(self):
        """Deleting the L2 JSONL file does not affect L1 rows already in index.db."""
        session_id = "sess-delete-l2"
        emitter = self._fresh_emitter(session_id=session_id, turn=1)
        emitter.emit("engram.surface.fire", 1, {"daemon_latency_ms": 30, "fallback_to_fts": False})
        emitter.emit("engram.surface.fire.l2", 2, {"prompt_text": "will be deleted"})

        self._run_indexer()

        # Verify both are in index.db
        self.assertEqual(_count_rows(self.db_path), 2)

        # Delete the L2 JSONL file (simulating privacy-purge workflow)
        l2_path = self.sessions_dir / f"{session_id}.l2.jsonl"
        l2_path.unlink()
        self.assertFalse(l2_path.exists())

        # Run indexer again — should not crash; L1 row still in DB (uuid dedup)
        result = self._run_indexer()
        self.assertEqual(result["rows_inserted"], 0, "Second run inserts 0 new rows (idempotency)")

        # L1 row remains; L2 row was already indexed, it stays in the DB
        # (the index is a projection of what was indexed — purging source JSONL
        # doesn't retroactively remove already-indexed rows; that's a separate
        # retention-policy concern outside the indexer's scope)
        l1_count = _count_rows(self.db_path, "level = 1")
        self.assertEqual(l1_count, 1, "L1 event must still be in index.db")

    def test_both_levels_indexed_not_just_l1(self):
        """L2 events MUST appear in index.db (access control is at query layer, not indexer)."""
        session_id = "sess-l2-indexed"
        events = [
            _make_event_dict(session_id=session_id, turn=1, event_type="engram.surface.fire", level=1, ts=_make_ts(1.0)),
            _make_event_dict(session_id=session_id, turn=1, event_type="engram.surface.fire.l2", level=2, ts=_make_ts(1.1)),
        ]
        _write_jsonl(self.sessions_dir / f"{session_id}.l1.jsonl", events[:1])
        _write_jsonl(self.sessions_dir / f"{session_id}.l2.jsonl", events[1:])

        result = self._run_indexer()
        self.assertEqual(result["files_processed"], 2)
        self.assertEqual(result["rows_inserted"], 2)

        l2_rows = _query_db(self.db_path, "SELECT * FROM events WHERE level = 2")
        self.assertEqual(len(l2_rows), 1, "L2 events must be in index.db")


# ---------------------------------------------------------------------------
# 5. TestIndexerHighWaterMarkLevelScoped
# ---------------------------------------------------------------------------

class TestIndexerHighWaterMarkLevelScoped(IntegrationTestBase):
    """L2 event with ts < last L1 ts must NOT be silently skipped.

    DESIGN.md §6 documents this bug that was found during F3 development:
    a single-sessionId HWM (not scoped to level) would skip L2 events
    whose timestamps happen to precede the last L1 event in the same session.
    The correct implementation scopes max(ts) to (sessionId, level).
    """

    def test_l2_event_before_l1_event_not_skipped(self):
        """An L2 event at T0 must be indexed even when the L1 HWM is at T1 > T0."""
        session_id = "sess-hwm"
        # T1 > T0: L1 has a later timestamp than L2
        t0 = _make_ts(100.0)  # L2 at T0
        t1 = _make_ts(200.0)  # L1 at T1 (later)

        l1_event = _make_event_dict(session_id=session_id, turn=5, event_type="engram.surface.fire", level=1, ts=t1)
        l2_event = _make_event_dict(session_id=session_id, turn=5, event_type="engram.surface.fire.l2", level=2, ts=t0)

        _write_jsonl(self.sessions_dir / f"{session_id}.l1.jsonl", [l1_event])
        _write_jsonl(self.sessions_dir / f"{session_id}.l2.jsonl", [l2_event])

        result = self._run_indexer()
        self.assertEqual(result["files_processed"], 2)
        self.assertEqual(result["rows_inserted"], 2, "Both L1 (T1) and L2 (T0 < T1) must be indexed")

        rows = _query_db(self.db_path, "SELECT level, ts FROM events ORDER BY ts")
        self.assertEqual(len(rows), 2)
        levels = {r["level"] for r in rows}
        self.assertIn(1, levels, "L1 event must be in index.db")
        self.assertIn(2, levels, "L2 event must be in index.db even though its ts < L1 HWM")

    def test_level_scoped_hwm_incremental_indexing(self):
        """Incremental run: only new events per (sessionId, level) are inserted."""
        session_id = "sess-hwm-inc"
        t1 = _make_ts(10.0)
        t2 = _make_ts(20.0)
        t3 = _make_ts(5.0)   # L2 event earlier than both L1 events

        l1_evt_1 = _make_event_dict(session_id=session_id, turn=1, event_type="engram.surface.fire", level=1, ts=t1)
        l1_evt_2 = _make_event_dict(session_id=session_id, turn=2, event_type="engram.surface.fire", level=1, ts=t2)
        l2_evt = _make_event_dict(session_id=session_id, turn=1, event_type="engram.surface.fire.l2", level=2, ts=t3)

        # Write all three events
        _write_jsonl(self.sessions_dir / f"{session_id}.l1.jsonl", [l1_evt_1, l1_evt_2])
        _write_jsonl(self.sessions_dir / f"{session_id}.l2.jsonl", [l2_evt])

        result = self._run_indexer()
        self.assertEqual(result["rows_inserted"], 3, "All 3 events must be indexed on first run")

        # Second run: no new events, 0 insertions
        result2 = self._run_indexer()
        self.assertEqual(result2["rows_inserted"], 0, "Second run must not duplicate rows")
        self.assertEqual(_count_rows(self.db_path), 3)

    def test_same_session_l1_and_l2_hwm_are_independent(self):
        """HWM for L1 and L2 of the same session are tracked independently."""
        session_id = "sess-hwm-indep"

        # Round 1: index 2 L1 events and 1 L2 event
        l1_events_r1 = [
            _make_event_dict(session_id=session_id, turn=1, event_type="engram.surface.fire", level=1, ts=_make_ts(1.0)),
            _make_event_dict(session_id=session_id, turn=2, event_type="engram.surface.fire", level=1, ts=_make_ts(2.0)),
        ]
        l2_events_r1 = [
            _make_event_dict(session_id=session_id, turn=1, event_type="engram.surface.fire.l2", level=2, ts=_make_ts(0.5)),
        ]
        _write_jsonl(self.sessions_dir / f"{session_id}.l1.jsonl", l1_events_r1)
        _write_jsonl(self.sessions_dir / f"{session_id}.l2.jsonl", l2_events_r1)

        r1 = self._run_indexer()
        self.assertEqual(r1["rows_inserted"], 3)

        # Round 2: append 1 new L1 event and 1 new L2 event (both later timestamps)
        l1_new = _make_event_dict(session_id=session_id, turn=3, event_type="engram.surface.fire", level=1, ts=_make_ts(3.0))
        l2_new = _make_event_dict(session_id=session_id, turn=3, event_type="engram.surface.fire.l2", level=2, ts=_make_ts(3.5))

        _append_jsonl(self.sessions_dir / f"{session_id}.l1.jsonl", [l1_new])
        _append_jsonl(self.sessions_dir / f"{session_id}.l2.jsonl", [l2_new])

        r2 = self._run_indexer()
        self.assertEqual(r2["rows_inserted"], 2, "Only the 2 new events (1 L1 + 1 L2) should be inserted")
        self.assertEqual(_count_rows(self.db_path), 5)


# ---------------------------------------------------------------------------
# 6. TestMalformedDataResilience
# ---------------------------------------------------------------------------

class TestMalformedDataResilience(IntegrationTestBase):
    """Corrupted JSONL lines are skipped + logged to stderr; clean lines survive; no crash."""

    def test_partial_json_mid_file_skipped(self):
        """A partial-JSON line mid-file is skipped; surrounding good lines are indexed."""
        session_id = "sess-chaos"
        good1 = _make_event_dict(session_id=session_id, turn=1, event_type="engram.surface.fire", level=1, ts=_make_ts(1.0))
        good2 = _make_event_dict(session_id=session_id, turn=2, event_type="engram.hook.fire", level=1, ts=_make_ts(2.0),
                                  data={"hook_name": "engram-stop-hook", "hook_type": "Stop", "duration_ms": 10, "exit_code": 0, "stdout_bytes": 20, "stderr_bytes": 0})
        good3 = _make_event_dict(session_id=session_id, turn=3, event_type="engram.tool.engram_call", level=1, ts=_make_ts(3.0),
                                  data={"tool_name": "engram_query", "result_status": "ok"})

        path = self.sessions_dir / f"{session_id}.l1.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(json.dumps(good1, separators=(",", ":")) + "\n")
            f.write('{"ts":"2026-05-17T15:00:00.000Z","uuid":"abcd","this-is-broken\n')  # malformed
            f.write(json.dumps(good2, separators=(",", ":")) + "\n")
            f.write(json.dumps(good3, separators=(",", ":")) + "\n")

        buf = io.StringIO()
        with redirect_stderr(buf):
            result = self._run_indexer()

        # 3 clean lines indexed (malformed skipped)
        self.assertEqual(result["rows_inserted"], 3)
        self.assertEqual(_count_rows(self.db_path), 3)

        # Malformed line reported to stderr
        stderr_out = buf.getvalue()
        self.assertTrue(
            "malformed" in stderr_out.lower() or "json" in stderr_out.lower(),
            f"Expected malformed-JSON error on stderr, got: {stderr_out!r}",
        )

    def test_corrupted_line_does_not_crash_indexer(self):
        """Various corruption patterns (null bytes, truncated, empty, non-dict-JSON) must not crash.

        Covers two failure modes:
        - JSON parse errors (truncated, null bytes) → caught by json.JSONDecodeError handler
        - Valid JSON but non-dict shape (bare string, integer, array) → caught by
          isinstance(event, dict) guard (added 2026-05-17 after F4 integration tests
          found the original indexer lacked this guard, crashing with AttributeError
          when .get() was called on a non-dict)
        """
        session_id = "sess-corrupt"
        good = _make_event_dict(session_id=session_id, turn=1, event_type="engram.surface.fire", level=1, ts=_make_ts(1.0))

        path = self.sessions_dir / f"{session_id}.l1.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "wb") as f:
            # Good line first
            f.write((json.dumps(good, separators=(",", ":")) + "\n").encode("utf-8"))
            # Null bytes mid-line (invalid JSON parse)
            f.write(b'{"ts":"2026-05-17T15:00:01.000Z"\x00\x01\x02\n')
            # Completely empty line (skipped silently)
            f.write(b"\n")
            # Truncated JSON (parse error)
            f.write(b'{"ts":"2026-05-17T15:00:02.000Z","event_type":"engram.x.y\n')
            # Valid JSON, non-dict (bare string) — must not crash on .get()
            f.write(b'"not an object"\n')
            # Valid JSON, non-dict (integer) — must not crash on .get()
            f.write(b'42\n')
            # Valid JSON, non-dict (array) — must not crash on .get()
            f.write(b'[1, 2, 3]\n')

        try:
            buf = io.StringIO()
            with redirect_stderr(buf):
                result = self._run_indexer()
        except Exception as exc:
            self.fail(f"Indexer must not crash on corrupted input, raised: {exc!r}")

        # At least the good event should be indexed
        self.assertGreaterEqual(_count_rows(self.db_path), 1)

    def test_index_db_queryable_after_corruption(self):
        """After processing a file with corrupted lines, index.db remains queryable."""
        session_id = "sess-queryable"
        good_events = [
            _make_event_dict(session_id=session_id, turn=i, event_type="engram.hook.fire", level=1,
                              data={"hook_name": "engram-stop-hook", "hook_type": "Stop", "duration_ms": i, "exit_code": 0, "stdout_bytes": 10, "stderr_bytes": 0},
                              ts=_make_ts(float(i)))
            for i in range(1, 4)
        ]

        path = self.sessions_dir / f"{session_id}.l1.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(json.dumps(good_events[0], separators=(",", ":")) + "\n")
            f.write("NOT VALID JSON AT ALL\n")
            f.write(json.dumps(good_events[1], separators=(",", ":")) + "\n")
            f.write("{incomplete\n")
            f.write(json.dumps(good_events[2], separators=(",", ":")) + "\n")

        with redirect_stderr(io.StringIO()):
            self._run_indexer()

        # DB is still queryable (not corrupted)
        try:
            rows = _query_db(self.db_path, "SELECT * FROM events WHERE hook_name = ?", ("engram-stop-hook",))
        except Exception as exc:
            self.fail(f"index.db is not queryable after corruption: {exc!r}")

        self.assertEqual(len(rows), 3, "3 clean lines must be indexed despite corrupted lines")

    def test_all_bad_lines_no_crash(self):
        """A file consisting entirely of malformed lines produces 0 rows and no crash."""
        session_id = "sess-allbad"
        path = self.sessions_dir / f"{session_id}.l1.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("NOT JSON\nALSO NOT JSON\n{broken\n")

        try:
            buf = io.StringIO()
            with redirect_stderr(buf):
                result = self._run_indexer()
        except Exception as exc:
            self.fail(f"Indexer raised on all-malformed file: {exc!r}")

        self.assertEqual(_count_rows(self.db_path), 0)
        self.assertTrue(self.db_path.exists(), "index.db must still exist after all-bad file")


# ---------------------------------------------------------------------------
# 7. Additional pipeline integration: idempotency + multi-session
# ---------------------------------------------------------------------------

class TestPipelineIdempotencyAndMultiSession(IntegrationTestBase):
    """Full pipeline idempotency and multi-session correctness."""

    def test_run_indexer_twice_no_duplicates(self):
        """Running indexer twice on same JSONL produces no duplicate rows."""
        session_id = "sess-idem"
        emitter = self._fresh_emitter(session_id=session_id, turn=1)
        for i in range(5):
            emitter.emit("engram.surface.fire", 1, {"daemon_latency_ms": 100 + i, "fallback_to_fts": False})

        self._run_indexer()
        result2 = self._run_indexer()

        self.assertEqual(result2["rows_inserted"], 0, "Second run must not insert any rows")
        self.assertEqual(_count_rows(self.db_path), 5)

    def test_append_new_events_only_new_indexed(self):
        """After first index run, appending new events causes only those to be picked up."""
        session_id = "sess-append"
        emitter = self._fresh_emitter(session_id=session_id, turn=1)

        # First batch: 3 events
        for i in range(3):
            emitter.emit("engram.hook.fire", 1, {
                "hook_name": "engram-stop-hook", "hook_type": "Stop",
                "duration_ms": i, "exit_code": 0, "stdout_bytes": 10, "stderr_bytes": 0,
            })

        r1 = self._run_indexer()
        self.assertEqual(r1["rows_inserted"], 3)

        # Second batch: 2 more events (appended by emitter — timestamps will be slightly later)
        for i in range(2):
            emitter.emit("engram.hook.fire", 1, {
                "hook_name": "engram-session-start-hook", "hook_type": "SessionStart",
                "duration_ms": 10 + i, "exit_code": 0, "stdout_bytes": 20, "stderr_bytes": 0,
            })

        r2 = self._run_indexer()
        # New events must have later timestamps; 2 new rows expected
        # (note: if timestamps collide within 1ms, INSERT OR IGNORE on uuid is the safety net)
        self.assertGreater(r2["rows_inserted"], 0, "Appended events must produce new indexed rows")
        total = _count_rows(self.db_path)
        self.assertEqual(total, 5, "Total must be 5 after both batches indexed")

    def test_multi_session_events_isolated_in_index(self):
        """Events from two sessions each indexed correctly; session queries are isolated."""
        # Session A: 3 surface.fire events
        sess_a = "sess-multi-A"
        events_a = [
            _make_event_dict(session_id=sess_a, turn=1, event_type="engram.surface.fire", level=1,
                              data={"daemon_latency_ms": 100, "fallback_to_fts": False},
                              ts=_make_ts(float(i)))
            for i in range(1, 4)
        ]
        _write_jsonl(self.sessions_dir / f"{sess_a}.l1.jsonl", events_a)

        # Session B: 2 hook.fire events
        sess_b = "sess-multi-B"
        events_b = [
            _make_event_dict(session_id=sess_b, turn=2, event_type="engram.hook.fire", level=1,
                              data={"hook_name": "engram-stop-hook", "hook_type": "Stop", "duration_ms": 5, "exit_code": 0, "stdout_bytes": 10, "stderr_bytes": 0},
                              ts=_make_ts(float(i) + 10.0))
            for i in range(1, 3)
        ]
        _write_jsonl(self.sessions_dir / f"{sess_b}.l1.jsonl", events_b)

        result = self._run_indexer()
        self.assertEqual(result["files_processed"], 2)
        self.assertEqual(result["rows_inserted"], 5)

        a_count = _count_rows(self.db_path, "sessionId = ?", (sess_a,))
        b_count = _count_rows(self.db_path, "sessionId = ?", (sess_b,))
        self.assertEqual(a_count, 3)
        self.assertEqual(b_count, 2)


if __name__ == "__main__":
    unittest.main()
