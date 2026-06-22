"""Regression tests for viz_server stats-tab plumbing.

Round-1 PR #185 review caught a real bug: `_resolve_logs_index` used the
wrong AGENTS dict key (`db_path` instead of `db`), so the stats dashboard
always returned the empty-state and was non-functional. These tests would
have caught it.

Per the viz_server.py:33 module-level comment, AGENTS structure is:
  {name: {"name": str, "label": str, "db": str (resolved abs path)}}
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import viz_server


class TestResolveLogsIndex(unittest.TestCase):
    """Verify the AGENTS lookup uses the correct key name."""

    def setUp(self):
        self._saved_agents = viz_server.AGENTS

    def tearDown(self):
        viz_server.AGENTS = self._saved_agents

    def test_resolves_to_logs_index_db_for_known_agent(self):
        viz_server.AGENTS = {
            "alice": {"name": "alice", "label": "Alice", "db": "/home/alice/.engram/knowledge.db"}
        }
        result = viz_server._resolve_logs_index("alice")
        self.assertEqual(result, "/home/alice/.engram/logs/index.db")

    def test_returns_empty_for_unknown_agent(self):
        viz_server.AGENTS = {
            "alice": {"name": "alice", "label": "Alice", "db": "/home/alice/.engram/knowledge.db"}
        }
        result = viz_server._resolve_logs_index("bob")
        self.assertEqual(result, "")

    def test_handles_engram_gemini_path_correctly(self):
        viz_server.AGENTS = {
            "g": {"name": "g", "label": "Gemini", "db": "/home/g/.engram-gemini/knowledge.db"}
        }
        result = viz_server._resolve_logs_index("g")
        self.assertEqual(result, "/home/g/.engram-gemini/logs/index.db")


class TestResolveAgentConfig(unittest.TestCase):
    """Verify viz_server reads tier sizes from agent's config.json, not
    hardcoded defaults. Regression coverage for the bug flagged
    2026-05-17: viz showed many nodes as tier 3 because TIER2_MAX=1000 was
    hardcoded in JS while production config had tier2_max_nodes=4000."""

    def setUp(self):
        self._saved_agents = viz_server.AGENTS
        self.tmpdir = tempfile.mkdtemp(prefix="engram_viz_cfg_test_")
        self.engram_home = Path(self.tmpdir) / "agent_dir"
        self.engram_home.mkdir()
        (self.engram_home / "knowledge.db").write_bytes(b"")
        viz_server.AGENTS = {
            "test": {"name": "test", "label": "Test", "db": str(self.engram_home / "knowledge.db")}
        }

    def tearDown(self):
        viz_server.AGENTS = self._saved_agents
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_reads_tier_sizes_from_config_json(self):
        # Drop a config.json with production-shaped tier sizes
        config_path = self.engram_home / "config.json"
        config_path.write_text('{"memory": {"tier1_max_nodes": 200, "tier2_max_nodes": 4000, "decay_base": 1.014}}')
        cfg = viz_server._resolve_agent_config("test")
        self.assertEqual(cfg["tier1_max_nodes"], 200)
        self.assertEqual(cfg["tier2_max_nodes"], 4000)
        self.assertEqual(cfg["decay_base"], 1.014)

    def test_returns_defaults_when_config_missing(self):
        # No config.json file in the engram_home dir
        cfg = viz_server._resolve_agent_config("test")
        self.assertEqual(cfg["tier1_max_nodes"], 200)
        self.assertEqual(cfg["tier2_max_nodes"], 1000)  # default
        self.assertEqual(cfg["decay_base"], 1.014)

    def test_returns_defaults_when_config_malformed(self):
        (self.engram_home / "config.json").write_text("not valid json {{")
        cfg = viz_server._resolve_agent_config("test")
        self.assertEqual(cfg["tier1_max_nodes"], 200)
        self.assertEqual(cfg["tier2_max_nodes"], 1000)  # default

    def test_returns_defaults_when_memory_section_absent(self):
        (self.engram_home / "config.json").write_text('{"trust_pool": []}')
        cfg = viz_server._resolve_agent_config("test")
        self.assertEqual(cfg["tier2_max_nodes"], 1000)

    def test_returns_defaults_for_unknown_agent(self):
        cfg = viz_server._resolve_agent_config("nonexistent")
        self.assertEqual(cfg["tier2_max_nodes"], 1000)

    def test_partial_config_falls_back_per_field(self):
        # Only tier2_max_nodes set; others should default
        (self.engram_home / "config.json").write_text('{"memory": {"tier2_max_nodes": 4000}}')
        cfg = viz_server._resolve_agent_config("test")
        self.assertEqual(cfg["tier1_max_nodes"], 200)  # default
        self.assertEqual(cfg["tier2_max_nodes"], 4000)  # from config
        self.assertEqual(cfg["decay_base"], 1.014)  # default


class TestGetStatsDataEmptyStates(unittest.TestCase):
    """Verify graceful empty-state responses (the UI relies on the shape)."""

    def test_nonexistent_index_db_returns_empty(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            result = viz_server.get_stats_data(os.path.join(tmpdir, "index.db"))
        self.assertTrue(result.get("empty"))
        self.assertIn("reason", result)

    def test_empty_string_path_returns_empty(self):
        result = viz_server.get_stats_data("")
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
            result = viz_server.get_stats_data(path)
            self.assertTrue(result.get("empty"))
            self.assertIn("events table", result.get("reason", ""))
        finally:
            os.unlink(path)


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
        result = viz_server.get_stats_data(self.db_path)
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
        result = viz_server.get_stats_data(self.db_path)
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
        result = viz_server.get_stats_data(self.db_path)
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
        result = viz_server.get_stats_data(self.index_db_path)
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
        result = viz_server.get_stats_data(self.index_db_path)
        self.assertTrue(os.path.exists(self.index_db_path))
        self.assertFalse(result.get("empty"), f"Expected stats, got empty: {result}")
        self.assertIn("total_events", result)
        self.assertEqual(result["total_events"], 1)

    def test_error_hint_uses_script_dir_not_tilde_engram(self):
        """Fallback error hint must reference the script dir, not ~/.engram."""
        saved = viz_server._INDEXER_AVAILABLE
        try:
            viz_server._INDEXER_AVAILABLE = False
            result = viz_server.get_stats_data(self.index_db_path)
            reason = result.get("reason", "")
            self.assertIn("engram_log_indexer.py", reason)
            self.assertNotIn("~/.engram/engram_log_indexer.py", reason)
        finally:
            viz_server._INDEXER_AVAILABLE = saved


if __name__ == "__main__":
    unittest.main()
