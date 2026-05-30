"""Tests for viz_server /api/search endpoint and search_nodes helper.

Covers:
1. search_nodes returns a dict with 'results' and 'query' keys on success.
2. search_nodes returns results matching the query (FTS or LIKE fallback).
3. search_nodes returns empty results (not an error) when nothing matches.
4. search_nodes returns an error key when the DB doesn't exist.
5. search_nodes returns an error key when query is empty.
6. /api/search HTTP route returns 200 + valid JSON with expected shape.
7. /api/search with missing q param returns error.
8. /api/search with unknown agent returns error.
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
import tempfile
import threading
import unittest
from http.server import HTTPServer
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import urlopen

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import viz_server


# ---------------------------------------------------------------------------
# Minimal DB schema (subset of server.py DDL — only what search_nodes needs)
# ---------------------------------------------------------------------------

_CREATE_NODES = """
CREATE TABLE IF NOT EXISTS nodes (
    id TEXT PRIMARY KEY,
    type TEXT NOT NULL,
    claim TEXT,
    created_at TEXT NOT NULL,
    confidence REAL,
    status TEXT,
    is_current INTEGER DEFAULT 1,
    metadata TEXT
);
"""

_CREATE_FTS = """
CREATE VIRTUAL TABLE IF NOT EXISTS nodes_fts USING fts5(
    claim,
    quoted_text,
    interpretation,
    content=nodes,
    content_rowid=rowid
);
"""

_INSERT_NODE = (
    "INSERT INTO nodes (id, type, claim, created_at, confidence, status, is_current)"
    " VALUES (?, ?, ?, ?, ?, ?, ?)"
)

_TS = "2026-05-01T00:00:00Z"


def _make_search_db(path: str, with_fts: bool = True) -> None:
    """Create a knowledge.db with a handful of nodes for search tests."""
    conn = sqlite3.connect(path)
    conn.execute(_CREATE_NODES)
    rows = [
        # (id, type, claim, created_at, confidence, status, is_current)
        ("ob_001", "observation_factual", "calibration matters for epistemic integrity", _TS, 0.85, None, 1),
        ("dv_001", "derivation",          "uncertainty propagates through derivation chains", _TS, 0.75, None, 1),
        ("ls_001", "lesson",              "always verify before claiming success", _TS, 0.90, None, 1),
        ("gl_001", "goal",                "develop genuine epistemic humility", _TS, None, "open", 1),
        # Retracted — must not appear
        ("ob_ret", "observation_factual", "calibration error in retracted node", _TS, 0.99, "retracted", 1),
        # Not current — must not appear
        ("ob_old", "observation_factual", "calibration old node", _TS, 0.40, None, 0),
    ]
    conn.executemany(_INSERT_NODE, rows)
    if with_fts:
        conn.execute(_CREATE_FTS)
        # Populate FTS from existing nodes
        conn.execute(
            "INSERT INTO nodes_fts(rowid, claim) SELECT rowid, claim FROM nodes"
        )
    conn.commit()
    conn.close()


class TestSearchNodesDirect(unittest.TestCase):
    """Unit tests for the search_nodes() Python function."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="engram_search_test_")
        self.db_path = os.path.join(self.tmpdir, "knowledge.db")
        _make_search_db(self.db_path, with_fts=True)

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_returns_results_and_query_keys(self):
        result = viz_server.search_nodes(self.db_path, "calibration")
        self.assertIn("results", result)
        self.assertIn("query", result)
        self.assertNotIn("error", result)

    def test_query_is_echoed_back(self):
        result = viz_server.search_nodes(self.db_path, "calibration")
        self.assertEqual(result["query"], "calibration")

    def test_fts_hit_returns_matching_nodes(self):
        result = viz_server.search_nodes(self.db_path, "calibration")
        ids = [r["id"] for r in result["results"]]
        # ob_001 contains "calibration" — must be in results
        self.assertIn("ob_001", ids, f"Expected ob_001 in results: {ids}")

    def test_retracted_nodes_excluded(self):
        """ob_ret is retracted — must not appear even though its claim matches."""
        result = viz_server.search_nodes(self.db_path, "calibration")
        ids = [r["id"] for r in result["results"]]
        self.assertNotIn("ob_ret", ids, "Retracted node must not appear in search results")

    def test_non_current_nodes_excluded(self):
        """ob_old is not current — must not appear."""
        result = viz_server.search_nodes(self.db_path, "calibration")
        ids = [r["id"] for r in result["results"]]
        self.assertNotIn("ob_old", ids, "Non-current node must not appear in search results")

    def test_result_shape_has_required_fields(self):
        result = viz_server.search_nodes(self.db_path, "calibration")
        self.assertGreater(len(result["results"]), 0)
        for r in result["results"]:
            for field in ("id", "type", "claim"):
                self.assertIn(field, r, f"Result missing field '{field}': {r}")

    def test_no_match_returns_empty_results_not_error(self):
        result = viz_server.search_nodes(self.db_path, "xyzzy_no_such_term_zyxyz")
        self.assertNotIn("error", result)
        self.assertEqual(result["results"], [])

    def test_missing_db_returns_error(self):
        result = viz_server.search_nodes("/nonexistent/path/knowledge.db", "calibration")
        self.assertIn("error", result)

    def test_empty_query_returns_error(self):
        result = viz_server.search_nodes(self.db_path, "")
        self.assertIn("error", result)

    def test_all_special_chars_query_does_not_crash(self):
        """A query that sanitizes to zero tokens (fts_expr=None) must not crash.

        After stripping non-alphanumeric chars, e.g. "!@#$" becomes no tokens,
        so fts_expr stays None and the LIKE fallback path fires (or returns empty
        cleanly).  Either way the response must be valid JSON with the expected
        shape — no exception, no 'error' key.
        """
        result = viz_server.search_nodes(self.db_path, "!@#$")
        self.assertNotIn("error", result, f"Unexpected error: {result.get('error')}")
        self.assertIn("results", result)
        self.assertIn("query", result)
        self.assertIsInstance(result["results"], list)


class TestSearchNodesFallback(unittest.TestCase):
    """Verify the LIKE fallback path when nodes_fts table is absent."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="engram_search_nofts_test_")
        self.db_path = os.path.join(self.tmpdir, "knowledge.db")
        _make_search_db(self.db_path, with_fts=False)

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_fallback_returns_results(self):
        result = viz_server.search_nodes(self.db_path, "calibration")
        self.assertNotIn("error", result)
        ids = [r["id"] for r in result["results"]]
        self.assertIn("ob_001", ids, f"Fallback must still find ob_001: {ids}")

    def test_fallback_excludes_retracted(self):
        result = viz_server.search_nodes(self.db_path, "calibration")
        ids = [r["id"] for r in result["results"]]
        self.assertNotIn("ob_ret", ids)


class TestSearchEndpointHTTP(unittest.TestCase):
    """Verify /api/search HTTP route returns 200 + valid JSON with expected shape."""

    @classmethod
    def setUpClass(cls):
        cls.tmpdir = tempfile.mkdtemp(prefix="engram_search_http_test_")
        cls.db_path = os.path.join(cls.tmpdir, "knowledge.db")
        _make_search_db(cls.db_path, with_fts=True)

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

    def test_search_returns_200(self):
        status, body = self._get("/api/search?agent=test_agent&q=calibration")
        self.assertEqual(status, 200)

    def test_search_returns_results_and_query_keys(self):
        status, body = self._get("/api/search?agent=test_agent&q=calibration")
        self.assertEqual(status, 200)
        self.assertIn("results", body)
        self.assertIn("query", body)
        self.assertNotIn("error", body, f"Unexpected error: {body.get('error')}")

    def test_search_returns_matching_results(self):
        _, body = self._get("/api/search?agent=test_agent&q=calibration")
        ids = [r["id"] for r in body["results"]]
        self.assertIn("ob_001", ids, f"ob_001 must be in search results: {ids}")

    def test_search_missing_q_returns_error(self):
        status, body = self._get("/api/search?agent=test_agent")
        self.assertEqual(status, 400)
        self.assertIn("error", body)

    def test_search_unknown_agent_returns_error(self):
        status, body = self._get("/api/search?agent=no_such_agent&q=calibration")
        self.assertEqual(status, 404)
        self.assertIn("error", body)

    def test_search_unknown_agent_status_404_and_error_message(self):
        """Confirm GET error response carries non-200 status + meaningful error body."""
        status, body = self._get("/api/search?agent=no_such_agent&q=calibration")
        self.assertEqual(status, 404)
        self.assertTrue(
            body["error"].startswith("Unknown agent"),
            f"Expected 'Unknown agent' prefix, got: {body['error']!r}",
        )

    def test_search_result_items_have_required_fields(self):
        _, body = self._get("/api/search?agent=test_agent&q=calibration")
        for r in body["results"]:
            for field in ("id", "type", "claim"):
                self.assertIn(field, r, f"Result item missing '{field}': {r}")

    def test_search_agent_field_in_response(self):
        _, body = self._get("/api/search?agent=test_agent&q=calibration")
        self.assertIn("agent", body)
        self.assertEqual(body["agent"], "test_agent")


if __name__ == "__main__":
    unittest.main()
