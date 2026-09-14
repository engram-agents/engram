"""Regression tests for viz.data.stats stats-tab plumbing (engram-alpha#2555
PR A retarget -- was viz_server, now split across viz.agents/viz.data.stats/
viz.data.config).

Round-1 PR #185 review caught a real bug: `_resolve_logs_index` used the
wrong AGENTS dict key (`db_path` instead of `db`), so the stats dashboard
always returned the empty-state and was non-functional. These tests would
have caught it.

Per viz/agents.py's module-level comment, AGENTS structure is:
  {name: {"name": str, "label": str, "db": str (resolved abs path)}}
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import unittest
import uuid as uuid_mod
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from engram_log_indexer import Indexer, _checkpoint_wal
from viz import agents as viz_agents
from viz.data import config as viz_config_data
from viz.data import stats as viz_stats_data


class TestResolveLogsIndex(unittest.TestCase):
    """Verify the AGENTS lookup uses the correct key name."""

    def setUp(self):
        self._saved_agents = viz_agents.AGENTS

    def tearDown(self):
        viz_agents.AGENTS = self._saved_agents

    def test_resolves_to_logs_index_db_for_known_agent(self):
        viz_agents.AGENTS = {
            "alice": {"name": "alice", "label": "Alice", "db": "/home/alice/.engram/knowledge.db"}
        }
        result = viz_stats_data._resolve_logs_index("alice")
        self.assertEqual(result, "/home/alice/.engram/logs/index.db")

    def test_returns_empty_for_unknown_agent(self):
        viz_agents.AGENTS = {
            "alice": {"name": "alice", "label": "Alice", "db": "/home/alice/.engram/knowledge.db"}
        }
        result = viz_stats_data._resolve_logs_index("bob")
        self.assertEqual(result, "")

    def test_handles_engram_gemini_path_correctly(self):
        viz_agents.AGENTS = {
            "g": {"name": "g", "label": "Gemini", "db": "/home/g/.engram-gemini/knowledge.db"}
        }
        result = viz_stats_data._resolve_logs_index("g")
        self.assertEqual(result, "/home/g/.engram-gemini/logs/index.db")


class TestResolveAgentConfig(unittest.TestCase):
    """Verify viz_server reads tier sizes from agent's config.json, not
    hardcoded defaults. Regression coverage for the bug flagged
    2026-05-17: viz showed many nodes as tier 3 because TIER2_MAX=1000 was
    hardcoded in JS while production config had tier2_max_nodes=4000."""

    def setUp(self):
        self._saved_agents = viz_agents.AGENTS
        self.tmpdir = tempfile.mkdtemp(prefix="engram_viz_cfg_test_")
        self.engram_home = Path(self.tmpdir) / "agent_dir"
        self.engram_home.mkdir()
        (self.engram_home / "knowledge.db").write_bytes(b"")
        viz_agents.AGENTS = {
            "test": {"name": "test", "label": "Test", "db": str(self.engram_home / "knowledge.db")}
        }

    def tearDown(self):
        viz_agents.AGENTS = self._saved_agents
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_reads_tier_sizes_from_config_json(self):
        # Drop a config.json with production-shaped tier sizes.
        # tier1_max_nodes is retired (#1220) and no longer returned.
        config_path = self.engram_home / "config.json"
        config_path.write_text('{"memory": {"tier2_max_nodes": 4000, "decay_base": 1.014}}')
        cfg = viz_config_data._resolve_agent_config("test")
        self.assertNotIn("tier1_max_nodes", cfg)
        self.assertEqual(cfg["tier2_max_nodes"], 4000)
        self.assertEqual(cfg["decay_base"], 1.014)

    def test_returns_defaults_when_config_missing(self):
        # No config.json file in the engram_home dir
        cfg = viz_config_data._resolve_agent_config("test")
        self.assertNotIn("tier1_max_nodes", cfg)
        self.assertEqual(cfg["tier2_max_nodes"], 1000)  # default
        self.assertEqual(cfg["decay_base"], 1.014)

    def test_returns_defaults_when_config_malformed(self):
        (self.engram_home / "config.json").write_text("not valid json {{")
        cfg = viz_config_data._resolve_agent_config("test")
        self.assertNotIn("tier1_max_nodes", cfg)
        self.assertEqual(cfg["tier2_max_nodes"], 1000)  # default

    def test_returns_defaults_when_memory_section_absent(self):
        (self.engram_home / "config.json").write_text('{"trust_pool": []}')
        cfg = viz_config_data._resolve_agent_config("test")
        self.assertEqual(cfg["tier2_max_nodes"], 1000)

    def test_returns_defaults_for_unknown_agent(self):
        cfg = viz_config_data._resolve_agent_config("nonexistent")
        self.assertEqual(cfg["tier2_max_nodes"], 1000)

    def test_partial_config_falls_back_per_field(self):
        # Only tier2_max_nodes set; decay_base should default
        (self.engram_home / "config.json").write_text('{"memory": {"tier2_max_nodes": 4000}}')
        cfg = viz_config_data._resolve_agent_config("test")
        self.assertNotIn("tier1_max_nodes", cfg)
        self.assertEqual(cfg["tier2_max_nodes"], 4000)  # from config
        self.assertEqual(cfg["decay_base"], 1.014)  # default


class TestGetStatsDataEmptyStates(unittest.TestCase):
    """Verify graceful empty-state responses (the UI relies on the shape)."""

    def test_nonexistent_index_db_returns_empty(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            result = viz_stats_data.get_stats_data(os.path.join(tmpdir, "index.db"))
        self.assertTrue(result.get("empty"))
        self.assertIn("reason", result)

    def test_empty_string_path_returns_empty(self):
        result = viz_stats_data.get_stats_data("")
        self.assertTrue(result.get("empty"))

    def test_index_db_without_events_table_returns_empty(self):
        # Create a SQLite DB with no events table
        import sqlite3
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tf:
            path = tf.name
        try:
            conn = sqlite3.connect(path)
            conn.execute("CREATE TABLE unrelated (x INTEGER)")
            conn.close()
            result = viz_stats_data.get_stats_data(path)
            self.assertTrue(result.get("empty"))
            self.assertIn("events table", result.get("reason", ""))
        finally:
            os.unlink(path)

    def test_query_error_returns_empty_not_dropped_connection(self):
        """Commit 1 regression (engram-alpha#2571): a query-time sqlite3.Error
        -- the read-only-write failure the /api/stats endpoint actually hit
        in production, where mode=ro still needs -shm/-wal access on a WAL
        db it cannot write to -- must produce {"empty": True, "reason": ...},
        not propagate uncaught and drop the connection with no response.

        Simulated via a mocked connection (not a live cross-user file) --
        the real failure needs two OS users and a WAL index.db, which isn't
        reproducible deterministically in this test environment. What must
        hold regardless of *which* sqlite3.Error fires is: the query block's
        except clause catches it and returns the same {"empty", "reason"}
        shape the open handler already uses.
        """
        import sqlite3
        from unittest import mock

        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tf:
            path = tf.name
        try:
            mock_conn = mock.MagicMock()
            mock_conn.execute.side_effect = sqlite3.OperationalError(
                "attempt to write a readonly database"
            )
            with mock.patch.object(sqlite3, "connect", return_value=mock_conn):
                result = viz_stats_data.get_stats_data(path)
        finally:
            os.unlink(path)

        self.assertTrue(result.get("empty"), f"Expected empty:True, got {result}")
        self.assertIn("reason", result)
        self.assertIn("attempt to write a readonly database", result["reason"])
        # The finally block must still close the connection -- "not a
        # dropped connection" means cleanup happens even on a query error.
        mock_conn.close.assert_called_once()


class TestGetStatsDataPopulated(unittest.TestCase):
    """Verify stats queries return correctly-shaped payloads against real schema."""

    def setUp(self):
        # Build a synthetic index.db with the DESIGN.md §6 schema + a few events
        self.tmpdir = tempfile.mkdtemp(prefix="engram_viz_stats_test_")
        self.db_path = os.path.join(self.tmpdir, "index.db")
        import sqlite3
        conn = sqlite3.connect(self.db_path)
        conn.executescript("""
            CREATE TABLE events (
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
                data TEXT NOT NULL
            );
        """)
        # Use a recent timestamp within the 7-day stats window so Panel 1 counts fire.
        # Hardcoded dates rot as the 7-day window slides; use now()-1day instead.
        recent = (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
        # Insert 3 L1 events + 1 L2 event (Stats should ignore L2)
        conn.executemany(
            "INSERT INTO events VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [
                ("u1", "s1", 5, recent, "engram.surface.fire", None, 1,
                 100, 0, None, None, None, None, None, "{}"),
                ("u2", "s1", 5, recent, "engram.hook.fire", None, 1,
                 None, None, "engram-stop-hook", 50, 0, None, None, "{}"),
                ("u3", "s1", 5, recent, "engram.tool.engram_call", None, 1,
                 None, None, None, None, None, "engram_add_observation", "created", "{}"),
                ("u4-l2", "s1", 5, recent, "engram.surface.fire.l2", None, 2,
                 None, None, None, None, None, None, None, '{"prompt_text":"secret"}'),
            ],
        )
        conn.commit()
        conn.close()

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_returns_populated_payload_with_all_panels(self):
        result = viz_stats_data.get_stats_data(self.db_path)
        self.assertFalse(result.get("empty"), f"Unexpected empty: {result}")
        # All 5 panels present
        self.assertIn("surface", result)
        self.assertIn("hooks", result)
        self.assertIn("tools", result)
        self.assertIn("cohort", result)
        self.assertIn("errors", result)
        # total_events counts only L1
        self.assertEqual(result["total_events"], 3)

    def test_l2_events_excluded_from_all_panels(self):
        """Privacy invariant: L2 events MUST NOT appear in any of the 5 panels.

        Fixture has 3 L1 events + 1 L2 event. Each panel checks that only
        the L1 events are counted/visible. Round-2 fairy review pointed out
        that the original version of this test only inspected Panels 1-3,
        leaving Panels 4 (cohort) and 5 (errors) un-asserted — a future L2
        event with a matching event_type could slip through without the test
        catching the level-filter regression.
        """
        result = viz_stats_data.get_stats_data(self.db_path)
        # total_events excludes L2
        self.assertEqual(result["total_events"], 3)
        # Panel 1 (surface): only the 1 L1 surface fire
        self.assertEqual(result["surface"]["fires_7d"], 1)
        # Panel 2 (hooks): no L2 hook_name leaked
        for h in result["hooks"]:
            self.assertFalse(
                (h["hook_name"] or "").endswith(".l2"),
                f"L2 hook leaked into Panel 2: {h}",
            )
        # Panel 3 (tools): no L2 tool_name leaked
        for t in result["tools"]:
            self.assertFalse(
                (t["tool_name"] or "").endswith(".l2"),
                f"L2 tool leaked into Panel 3: {t}",
            )
        # Panel 4 (cohort): rolled up by turn. Fixture has 3 L1 events all
        # at turn=5, plus 1 L2 event also at turn=5. With L1-only filter,
        # cohort should show 1 turn row with events=3 (NOT 4).
        self.assertEqual(len(result["cohort"]), 1, f"Cohort panel: expected 1 turn row, got {result['cohort']}")
        cohort_row = result["cohort"][0]
        self.assertEqual(cohort_row["turn"], 5)
        self.assertEqual(cohort_row["events"], 3, f"Cohort events should be 3 (L1 only), got {cohort_row['events']}")
        # Panel 5 (errors): no fixture event is an error → should be empty.
        # If the level filter regressed, the L2 fixture event would NOT be
        # an error either (no failed result_status, no nonzero hook_exit_code),
        # so this test alone doesn't catch level-filter regression on Panel 5.
        # Stronger Panel 5 coverage requires a fixture with an L2 error event
        # — added in test_l2_error_event_excluded_from_errors_panel below.
        self.assertEqual(result["errors"], [], f"Errors panel should be empty, got {result['errors']}")

    def test_l2_error_event_excluded_from_errors_panel(self):
        """Privacy invariant for Panel 5: an L2 event with error-shaped fields
        must not appear in the errors panel. Stronger coverage than the
        general privacy test — requires fixture with an L2 error event."""
        import sqlite3
        conn = sqlite3.connect(self.db_path)
        # Inject an L2 event that LOOKS like an error (would match the errors
        # panel's predicate if level filter were missing)
        conn.execute(
            "INSERT INTO events VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                "u5-l2-err", "s1", 5, "2026-05-17T10:01:00Z",
                "engram.tool.engram_call", None, 2,
                None, None, None, None, None,
                "engram_add_observation", "broken_result_status", "{}",
            ),
        )
        conn.commit()
        conn.close()
        result = viz_stats_data.get_stats_data(self.db_path)
        # The L2 error event must NOT appear in the errors panel
        for e in result["errors"]:
            self.assertNotEqual(
                e["tool_name"], "engram_add_observation",
                f"L2 error event leaked into Panel 5: {e}",
            )


class TestGetStatsDataAutoRun(unittest.TestCase):
    """Verify get_stats_data auto-runs the indexer when index.db is absent (issue #1048)."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="engram_stats_autorun_")
        self.logs_dir = Path(self.tmpdir)
        (self.logs_dir / "sessions").mkdir()
        self.index_db_path = str(self.logs_dir / "index.db")

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_auto_run_creates_index_db_when_absent(self):
        """Auto-run creates index.db even when no JSONL events are present."""
        self.assertFalse(os.path.exists(self.index_db_path))
        result = viz_stats_data.get_stats_data(self.index_db_path)
        self.assertTrue(os.path.exists(self.index_db_path))
        # 0 events → empty state, but index.db was built (not the missing-db error)
        self.assertTrue(result.get("empty"))
        self.assertNotIn("No index.db at expected location", result.get("reason", ""))

    def test_auto_run_indexes_existing_jsonl_events(self):
        """With JSONL events present, auto-run populates index and stats return data."""
        recent = (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
        event = {
            "uuid": "test-uuid-autorun-001",
            "sessionId": "test-session-autorun",
            "turn": 1,
            "ts": recent,
            "event_type": "engram.surface.call",
            "data": {},
        }
        (self.logs_dir / "sessions" / "test-session-autorun.l1.jsonl").write_text(
            json.dumps(event) + "\n"
        )
        self.assertFalse(os.path.exists(self.index_db_path))
        result = viz_stats_data.get_stats_data(self.index_db_path)
        self.assertTrue(os.path.exists(self.index_db_path))
        self.assertFalse(result.get("empty"), f"Expected stats, got empty: {result}")
        self.assertIn("total_events", result)
        self.assertEqual(result["total_events"], 1)

    def test_error_hint_uses_script_dir_not_tilde_engram(self):
        """Fallback error hint must reference the script dir, not ~/.engram."""
        saved = viz_stats_data._INDEXER_AVAILABLE
        try:
            viz_stats_data._INDEXER_AVAILABLE = False
            result = viz_stats_data.get_stats_data(self.index_db_path)
            reason = result.get("reason", "")
            self.assertIn("engram_log_indexer.py", reason)
            self.assertNotIn("~/.engram/engram_log_indexer.py", reason)
        finally:
            viz_stats_data._INDEXER_AVAILABLE = saved


class TestConcurrentReaderSeesCheckpointedData(unittest.TestCase):
    """Commits 2+3 regression (engram-alpha#2571) -- the load-bearing test.

    A test that only asserts "the endpoint no longer errors" passes on BOTH
    the fixed and the unfixed code, because SQLite auto-checkpoints a WAL
    database when the LAST connection to it closes -- and in a naive,
    single-connection test, the indexer's own conn.close() IS the last
    close, which accidentally folds the WAL into the main db file even
    without commit 2's explicit PRAGMA. That accident would mask exactly
    the bug commit 3's immutable=1 read depends on commit 2 to prevent.

    To force the real race, this test holds a SEPARATE reader connection
    open to index.db for the entire indexer run -- so when the indexer's
    own connection closes, it is NOT the last connection, and SQLite's
    implicit checkpoint-on-close does not fire. Without commit 2's explicit
    PRAGMA wal_checkpoint(TRUNCATE), the new row would sit only in -wal; an
    immutable=1 reader (commit 3) ignores -wal entirely and would miss it
    -- the stale read viz/data/health.py's #184 history warns immutable=1
    causes without a checkpoint guarantee.
    """

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="engram_viz_concurrent_test_")
        self.logs_dir = Path(self.tmpdir)
        self.sessions_dir = self.logs_dir / "sessions"
        self.sessions_dir.mkdir(parents=True, exist_ok=True)
        self.db_path = str(self.logs_dir / "index.db")

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _write_event(self, session_id: str) -> None:
        recent = (datetime.now(timezone.utc) - timedelta(hours=1)).strftime(
            "%Y-%m-%dT%H:%M:%S.000Z"
        )
        event = {
            "ts": recent,
            "sessionId": session_id,
            "turn": 1,
            "uuid": str(uuid_mod.uuid4()),
            "parentUuid": None,
            "event_type": "engram.surface.fire",
            "tool_use_id": None,
            "level": 1,
            "data": {},
        }
        path = self.sessions_dir / f"{session_id}.l1.jsonl"
        path.write_text(json.dumps(event) + "\n")

    def test_concurrent_reader_during_indexer_run_sees_new_event(self):
        # Baseline pass: create schema + an empty index.db (no events yet).
        Indexer.run_once(logs_dir=self.logs_dir)
        baseline = viz_stats_data.get_stats_data(self.db_path)
        self.assertTrue(baseline.get("empty"), f"Expected empty baseline, got {baseline}")

        # Open a SEPARATE connection and actually touch the db, so it
        # genuinely holds the WAL machinery attached (not just an idle,
        # never-queried handle) for the duration of the indexer run below.
        concurrent_reader = sqlite3.connect(self.db_path)
        concurrent_reader.execute("SELECT COUNT(*) FROM events").fetchone()

        try:
            # Write a new event, then run the indexer WHILE concurrent_reader
            # is still open -- this is the deliberate race the spec requires.
            self._write_event("concurrent-sess")
            result = Indexer.run_once(logs_dir=self.logs_dir)
            self.assertEqual(result["rows_inserted"], 1, f"Indexer summary: {result}")

            # The viz endpoint opens its OWN fresh connection per request
            # (mode=ro&immutable=1 post-commit-3) -- exactly as production
            # does. If the indexer hadn't checkpointed to zero (commit 2),
            # and the concurrent reader suppressed the implicit
            # checkpoint-on-close, this immutable=1 read would be stale.
            stats = viz_stats_data.get_stats_data(self.db_path)
        finally:
            concurrent_reader.close()

        self.assertFalse(stats.get("empty"), f"Stale read: endpoint still reports empty: {stats}")
        self.assertEqual(
            stats.get("total_events"), 1, f"Expected the new event to be visible: {stats}"
        )

    def test_indexer_checkpoints_wal_to_zero_even_with_concurrent_reader(self):
        """Narrower unit check on the mechanism itself: after run_once
        completes, the -wal sidecar is drained to zero bytes even though
        another connection was open throughout -- proving commit 2's
        explicit PRAGMA (not the implicit close-time checkpoint, which a
        concurrent reader suppresses) is what drained it."""
        Indexer.run_once(logs_dir=self.logs_dir)

        concurrent_reader = sqlite3.connect(self.db_path)
        concurrent_reader.execute("SELECT COUNT(*) FROM events").fetchone()
        try:
            self._write_event("checkpoint-sess")
            Indexer.run_once(logs_dir=self.logs_dir)

            # Measure -wal size WHILE concurrent_reader is still open. This
            # must happen before concurrent_reader.close() -- closing it
            # would itself become the new "last connection" and trigger
            # SQLite's own implicit checkpoint-on-close, which would drain
            # the WAL regardless of whether commit 2's explicit PRAGMA ran
            # and mask exactly the gap this test exists to catch.
            wal_path = Path(self.db_path + "-wal")
            wal_size = wal_path.stat().st_size if wal_path.exists() else 0
        finally:
            concurrent_reader.close()

        self.assertEqual(
            wal_size,
            0,
            f"-wal not drained to zero (size={wal_size} bytes) while a "
            "concurrent reader was still attached; an immutable=1 reader "
            "would miss its content",
        )


class TestCheckpointBusyIsReported(unittest.TestCase):
    """The busy branch of _checkpoint_wal, exercised directly.

    WHAT THIS COVERS AND WHAT IT DOES NOT, stated because the distinction
    is the whole reason the helper was extracted:

    * COVERED: given a busy pragma result, the helper reports it on stderr
      rather than discarding it, and returns the row.
    * NOT COVERED: that a busy result actually OCCURS under production
      contention. That is not reachable on demand -- it needs a concurrent
      reader holding a snapshot at exactly the right instant -- and the
      sibling test above deliberately asserts the OPPOSITE (that the WAL
      drains to zero with a concurrent reader attached), which is the
      normal case. The busy case was measured by hand instead, on a
      MINIMAL SYNTHETIC RIG rather than on index.db: with a reader
      mid-transaction on the same file, `PRAGMA wal_checkpoint(TRUNCATE)`
      returns busy WITHOUT raising and leaves -wal non-empty. Two
      independently-built rigs agreed on that and disagreed on the
      figures -- (1, 3, 3)/12,392 bytes and (1, 5, 5)/20,632 bytes -- so
      the (1, 3, 3) below is a plausible sample value for the stub, NOT a
      constant any real checkpoint is expected to return.

    So this is a positive control for the handler, not for the trigger. It
    exists because the alternative was shipping a warning line that had
    never once been seen to fire.
    """

    class _StubConn:
        def __init__(self, row):
            self._row = row
            self.sql = None

        def execute(self, sql):
            self.sql = sql
            return self

        def fetchone(self):
            return self._row

    def _run(self, row):
        conn = self._StubConn(row)
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            returned = _checkpoint_wal(conn, "/tmp/index.db")
        return conn, err.getvalue(), returned

    def test_busy_result_is_reported_on_stderr(self):
        conn, err, returned = self._run((1, 3, 3))
        self.assertEqual(conn.sql, "PRAGMA wal_checkpoint(TRUNCATE)")
        self.assertEqual(returned, (1, 3, 3))
        self.assertIn("WAL checkpoint busy", err)
        # the numbers themselves must reach the log, not just the word
        self.assertIn("busy=1", err)
        self.assertIn("log_frames=3", err)
        self.assertIn("checkpointed=3", err)
        self.assertIn("/tmp/index.db", err)

    def test_clean_result_is_silent(self):
        """The falsifier for the test above: if this also printed, the
        assertion there would pass on a helper that logs unconditionally."""
        _, err, returned = self._run((0, 0, 0))
        self.assertEqual(returned, (0, 0, 0))
        self.assertEqual(err, "")

    def test_none_row_does_not_raise(self):
        """A pragma returning no row at all must not become a TypeError
        inside the indexer's own finally block."""
        _, err, returned = self._run(None)
        self.assertIsNone(returned)
        self.assertEqual(err, "")


if __name__ == "__main__":
    unittest.main()
