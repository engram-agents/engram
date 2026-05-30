"""Tests for viz_server calibration endpoint and get_calibration_data helper.

Covers:
1. get_calibration_data returns dict with expected top-level keys when given
   a populated test DB.
2. Empty DB (nodes table exists but no rows) returns sensible empty buckets,
   not errors.
3. /api/calibration route returns 200 + valid JSON via the HTTP handler.
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from http.server import HTTPServer
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import urlopen

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import viz_server


# ---------------------------------------------------------------------------
# Minimal nodes-table schema (matches server.py DDL for the columns we query)
# ---------------------------------------------------------------------------
_CREATE_NODES = """
CREATE TABLE IF NOT EXISTS nodes (
    id TEXT PRIMARY KEY,
    type TEXT NOT NULL,
    created_at TEXT NOT NULL,
    confidence REAL,
    is_current INTEGER DEFAULT 1,
    status TEXT,
    quote_type TEXT,
    metadata TEXT
);
"""

_INSERT_NODE = (
    "INSERT INTO nodes (id, type, created_at, confidence, is_current, status, quote_type, metadata)"
    " VALUES (?, ?, ?, ?, ?, ?, ?, ?)"
)

# ISO timestamps: OLD = outside 7-turn (>168h ago), RECENT = within 7-turn window but >24h,
# NOW = within the 1-turn (24h) window.
_TS_OLD    = "2020-01-01T00:00:00Z"
_TS_RECENT = "2026-05-01T00:00:00Z"   # definitely > 168h ago in any test run
_TS_NOW    = "2099-01-01T00:00:00Z"   # far future — always within the 24h window (never)
# Use an approach where we just use very old timestamps for OLD, moderately old for RECENT,
# and we only check the n-<=corpus_n invariant rather than exact filtering.
# More robust: use epoch-relative times so we don't need to mock datetime.


def _make_populated_db(path: str) -> None:
    """Populate a knowledge.db with a variety of node types for calibration tests."""
    # Use a consistent "old" timestamp for most nodes (outside all time windows).
    ts = "2026-05-20T12:00:00Z"
    # Use a time-relative timestamp for the 3 active observation_factual nodes so they
    # fall inside the 24h this_turn window regardless of when the test runs.  Without
    # this the drift_by_type entries are always empty and the drift arithmetic assertion
    # in test_drift_by_type_shape_when_present never actually executes.
    ts_now = (datetime.now(timezone.utc) - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    conn = sqlite3.connect(path)
    conn.execute(_CREATE_NODES)
    rows = [
        # (id, type, created_at, confidence, is_current, status, quote_type, metadata)
        ("ob_001", "observation_factual", ts_now, 0.80, 1, None, "strong_evidence", '{"source_class":"direct"}'),
        ("ob_002", "observation_factual", ts_now, 0.65, 1, None, "weak_evidence",   '{"source_class":"indirect"}'),
        ("ob_003", "observation_factual", ts_now, 0.90, 1, None, "strong_evidence", '{"source_class":"direct"}'),
        ("dv_001", "derivation",          ts, 0.75, 1, None, None, '{"reasoning_type":"inductive"}'),
        ("dv_002", "derivation",          ts, 0.55, 1, None, None, '{"reasoning_type":"deductive"}'),
        ("cn_001", "conjecture",          ts, 0.40, 1, None, None, None),
        ("ls_001", "lesson",              ts, 0.85, 1, None, None, None),
        # Retracted node — must NOT appear in calibration distributions
        ("ob_ret", "observation_factual", ts, 0.99, 1, "retracted", None, None),
        # Old (not current) node — must NOT appear
        ("ob_old", "observation_factual", ts, 0.30, 0, None, None, None),
    ]
    conn.executemany(_INSERT_NODE, rows)
    conn.commit()
    conn.close()


class TestGetCalibrationDataPopulated(unittest.TestCase):

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="engram_cal_test_")
        self.db_path = os.path.join(self.tmpdir, "knowledge.db")
        _make_populated_db(self.db_path)

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_top_level_keys_present(self):
        result = viz_server.get_calibration_data(self.db_path)
        self.assertNotIn("error", result, f"Unexpected error: {result.get('error')}")
        for key in ("corpus", "this_turn", "rolling_7", "drift_by_type"):
            self.assertIn(key, result, f"Missing top-level key: {key}")

    def test_corpus_sub_keys_present(self):
        corpus = viz_server.get_calibration_data(self.db_path)["corpus"]
        for sub in ("by_type", "by_quote_type", "by_reasoning_type", "by_source_class"):
            self.assertIn(sub, corpus, f"Missing corpus sub-key: {sub}")

    def test_by_type_includes_primary_types(self):
        by_type = viz_server.get_calibration_data(self.db_path)["corpus"]["by_type"]
        for ntype in ("observation_factual", "derivation", "conjecture", "lesson"):
            self.assertIn(ntype, by_type, f"Missing type in by_type: {ntype}")

    def test_retracted_and_noncurrent_excluded(self):
        """Confidence 0.99 (retracted) and 0.30 (old) must not skew p95."""
        by_type = viz_server.get_calibration_data(self.db_path)["corpus"]["by_type"]
        obs = by_type["observation_factual"]
        # Only 3 active, non-retracted obs: 0.80, 0.65, 0.90
        self.assertEqual(obs["n"], 3, f"Expected n=3 after excluding retracted+old, got {obs['n']}")
        # p95 should be ≤ 0.90 (not 0.99 from retracted)
        self.assertLessEqual(obs["p95"], 0.90 + 1e-6,
                             f"p95 should be ≤ 0.90 (retracted excluded), got {obs['p95']}")

    def test_by_quote_type_groups_correctly(self):
        bq = viz_server.get_calibration_data(self.db_path)["corpus"]["by_quote_type"]
        # strong_evidence: ob_001 (0.80) + ob_003 (0.90) → mean 0.85
        self.assertIn("strong_evidence", bq)
        self.assertEqual(bq["strong_evidence"]["n"], 2)
        self.assertAlmostEqual(bq["strong_evidence"]["mean"], 0.85, places=2)

    def test_by_reasoning_type_groups_derivations(self):
        brt = viz_server.get_calibration_data(self.db_path)["corpus"]["by_reasoning_type"]
        self.assertIn("inductive", brt)
        self.assertIn("deductive", brt)

    def test_drift_by_type_shape_when_present(self):
        """When drift_by_type entries exist, they must have the required fields and correct delta math."""
        drift = viz_server.get_calibration_data(self.db_path)["drift_by_type"]
        # ob_001/ob_002/ob_003 use ts_now (1h ago) so they fall inside the 24h this_turn
        # window and produce ≥3 samples for observation_factual — guaranteeing at least one
        # drift entry and exercising the arithmetic assertion below.
        self.assertIsInstance(drift, dict)
        self.assertIn("observation_factual", drift,
                      "Expected drift entry for observation_factual (ts_now nodes in this_turn)")
        for ntype, d in drift.items():
            for field in ("this_turn_p50", "corpus_p50", "delta", "n_this_turn"):
                self.assertIn(field, d, f"drift_by_type[{ntype}] missing {field}")
            self.assertAlmostEqual(
                d["delta"],
                round(d["this_turn_p50"] - d["corpus_p50"], 3),
                places=3,
            )

    def test_rolling_7_window_sub_keys_present(self):
        """rolling_7 response must have the same shape as corpus."""
        result = viz_server.get_calibration_data(self.db_path)
        rolling_7 = result["rolling_7"]
        for sub in ("by_type", "by_quote_type", "by_reasoning_type", "by_source_class"):
            self.assertIn(sub, rolling_7, f"rolling_7 missing sub-key: {sub}")
        # rolling_7.by_type must include all 6 confidence-bearing types (as n=0 if empty)
        for ntype in ("observation_factual", "derivation", "conjecture", "lesson",
                      "axiom", "definition"):
            self.assertIn(ntype, rolling_7["by_type"],
                          f"rolling_7.by_type missing placeholder for {ntype}")

    def test_this_turn_sub_keys_present(self):
        """this_turn response must have the same shape as corpus."""
        result = viz_server.get_calibration_data(self.db_path)
        this_turn = result["this_turn"]
        for sub in ("by_type", "by_quote_type", "by_reasoning_type", "by_source_class"):
            self.assertIn(sub, this_turn, f"this_turn missing sub-key: {sub}")


class TestGetCalibrationDataEmpty(unittest.TestCase):

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="engram_cal_empty_test_")
        self.db_path = os.path.join(self.tmpdir, "knowledge.db")
        conn = sqlite3.connect(self.db_path)
        conn.execute(_CREATE_NODES)
        conn.commit()
        conn.close()

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_empty_db_returns_expected_keys_not_error(self):
        result = viz_server.get_calibration_data(self.db_path)
        self.assertNotIn("error", result, f"Empty DB raised error: {result.get('error')}")
        for key in ("corpus", "this_turn", "rolling_7", "drift_by_type"):
            self.assertIn(key, result)

    def test_empty_by_type_returns_zero_n_placeholders(self):
        """Every confidence-bearing type must appear in by_type with n=0 (not absent)."""
        by_type = viz_server.get_calibration_data(self.db_path)["corpus"]["by_type"]
        for ntype in ("observation_factual", "derivation", "conjecture", "lesson",
                      "axiom", "definition"):
            self.assertIn(ntype, by_type, f"{ntype} absent from empty by_type")
            self.assertEqual(by_type[ntype]["n"], 0,
                             f"Expected n=0 for {ntype} in empty DB, got {by_type[ntype]}")

    def test_empty_db_drift_is_empty(self):
        result = viz_server.get_calibration_data(self.db_path)
        self.assertEqual(result["drift_by_type"], {},
                         "drift_by_type should be empty for an empty DB")


class TestGetCalibrationDataMissingDB(unittest.TestCase):

    def test_missing_db_returns_error_key(self):
        result = viz_server.get_calibration_data("/nonexistent/path/knowledge.db")
        self.assertIn("error", result)


class TestCalibrationEndpointHTTP(unittest.TestCase):
    """Verify /api/calibration route returns 200 + valid JSON with expected shape."""

    @classmethod
    def setUpClass(cls):
        cls.tmpdir = tempfile.mkdtemp(prefix="engram_cal_http_test_")
        cls.db_path = os.path.join(cls.tmpdir, "knowledge.db")
        _make_populated_db(cls.db_path)

        # Wire AGENTS so the handler can resolve db_path
        cls._saved_agents = viz_server.AGENTS
        cls._saved_default = viz_server.DEFAULT_AGENT
        viz_server.AGENTS = {
            "test_agent": {"name": "test_agent", "label": "Test", "db": cls.db_path}
        }
        viz_server.DEFAULT_AGENT = "test_agent"

        cls.server = HTTPServer(("127.0.0.1", 0), viz_server.Handler)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever)
        cls.thread.daemon = True
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        viz_server.AGENTS = cls._saved_agents
        viz_server.DEFAULT_AGENT = cls._saved_default
        import shutil
        shutil.rmtree(cls.tmpdir, ignore_errors=True)

    def _get(self, path: str):
        url = f"http://127.0.0.1:{self.port}{path}"
        try:
            with urlopen(url, timeout=5) as resp:
                return resp.status, json.loads(resp.read())
        except HTTPError as e:
            return e.code, json.loads(e.read())

    def test_calibration_endpoint_returns_200(self):
        status, body = self._get("/api/calibration?agent=test_agent")
        self.assertEqual(status, 200)

    def test_calibration_endpoint_returns_expected_keys(self):
        _, body = self._get("/api/calibration?agent=test_agent")
        self.assertNotIn("error", body, f"Endpoint error: {body.get('error')}")
        for key in ("corpus", "this_turn", "rolling_7", "drift_by_type", "agent"):
            self.assertIn(key, body, f"Missing key: {key}")

    def test_calibration_endpoint_unknown_agent_returns_error(self):
        status, body = self._get("/api/calibration?agent=no_such_agent")
        self.assertEqual(status, 404)
        self.assertIn("error", body)

    def test_health_page_contains_calibration_card_text(self):
        url = f"http://127.0.0.1:{self.port}/health"
        with urlopen(url, timeout=5) as resp:
            html = resp.read().decode()
        self.assertIn("Confidence Calibration", html)

    def test_stats_page_contains_confidence_trends_text(self):
        url = f"http://127.0.0.1:{self.port}/stats"
        with urlopen(url, timeout=5) as resp:
            html = resp.read().decode()
        self.assertIn("Confidence Trends", html)


if __name__ == "__main__":
    unittest.main()
