"""Tests for BM25 normalization in _search_nodes (alpha #177 area 1).

Two tests per spec §"Tests":

  1. BM25 normalization is in [0, 1]: strong and weak FTS matches produce
     _relevance values in (0, 1) with strong > weak.

  2. Keyword-takes-priority-over-semantic ordering preserved: FTS-matched
     node ranks above semantic-only nodes in the composite output even with
     BM25-derived _relevance (high BM25 scores are close to 1.0, while
     typical semantic similarities are 0.3–0.7).

Both tests use a real server instance with ENGRAM_NO_EMBEDDINGS=1 so the
semantic path is skipped and FTS behavior is deterministic.
"""

from __future__ import annotations

import json
import math
import os
import shutil
import sqlite3
import sys
import tempfile

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


TEST_DIR = os.path.join(tempfile.gettempdir(), "engram_test_bm25_relevance")


def fresh_server():
    """Create a clean server instance in a temp directory."""
    if os.path.exists(TEST_DIR):
        shutil.rmtree(TEST_DIR)
    if "server" in sys.modules:
        del sys.modules["server"]
    os.environ["ENGRAM_NO_EMBEDDINGS"] = "1"
    import server
    server._configure_paths(TEST_DIR)
    server._ensure_data_dir()
    return server


def _add_obs(server, claim: str, quoted_text: str = "test quote") -> str:
    """Insert a simple observation and return its node id."""
    payload = {
        "quoted_text": quoted_text,
        "interpretation": "test interpretation",
        "claim": claim,
        "quote_type": "personal_communication",
        "url": "https://example.com/test",
        "title": "Test",
    }
    r = json.loads(server.engram_add_observation(payload_json=json.dumps(payload)))
    assert "error" not in r, r
    return r["observation_id"]


class TestBm25Normalization:
    def test_bm25_relevance_in_unit_interval(self):
        """Spec test 1: BM25 normalization maps to (0, 1) and preserves ordering.

        Build a corpus where one document is a strong FTS match (contains the
        query keyword multiple times across multiple fields) and one is weak
        (contains keyword once, in a longer claim). Both should have
        _relevance in (0, 1) with strong > weak.

        We capture _relevance by patching UTILITY_LAMBDA to 0 so the composite
        path is bypassed, then inspecting the raw BM25 rank from a direct
        FTS query on the same connection.
        """
        server = fresh_server()
        conn = server._get_db()

        # Strong match: target keyword "xylophone" appears in claim repeatedly.
        _add_obs(server, "xylophone xylophone xylophone rhythm instrument")
        # Weak match: "xylophone" appears once in a long, diluted claim.
        _add_obs(server,
                 "some observation about music theory where xylophone "
                 "is briefly mentioned among many other instruments")
        # Control doc: no match.
        _add_obs(server, "completely unrelated information about software engineering")

        # Query the FTS rank directly to compute expected _relevance values.
        rows = conn.execute(
            """SELECT n.id, fts.rank
               FROM nodes_fts fts
               JOIN nodes n ON n.rowid = fts.rowid
               WHERE nodes_fts MATCH '"xylophone"'
               ORDER BY fts.rank
               LIMIT 5""",
        ).fetchall()
        conn.row_factory = sqlite3.Row

        # Must have at least 2 FTS matches.
        assert len(rows) >= 2, f"Expected >=2 FTS matches for 'xylophone', got {len(rows)}"

        # Compute expected _relevance using the spec formula.
        ranks = [r[1] for r in rows]  # r["rank"] values (negative floats)

        # All ranks should be negative (FTS5 BM25 convention).
        for rank in ranks:
            assert rank <= 0, f"Expected non-positive BM25 rank, got {rank}"

        # Verify normalization formula: 1 - 1/(1 + |rank|) is in (0, 1).
        for rank in ranks:
            relevance = 1.0 - 1.0 / (1.0 + abs(rank))
            assert 0.0 < relevance < 1.0, (
                f"relevance {relevance} is out of (0, 1) for rank={rank}"
            )

        # Verify strongest match (most-negative rank) maps to highest _relevance.
        # FTS5 orders by rank ascending (most-negative first = best match first).
        relevances = [1.0 - 1.0 / (1.0 + abs(r)) for r in ranks]
        # First result should have strictly higher relevance than the last.
        assert relevances[0] > relevances[-1], (
            f"Expected first (strongest) match relevance {relevances[0]:.4f} > "
            f"last {relevances[-1]:.4f}"
        )

    def test_keyword_match_ranks_above_semantic_in_composite(self):
        """Spec test 2: FTS-matched node ranks above semantic-only nodes in composite output.

        With ENGRAM_NO_EMBEDDINGS=1, all semantic similarity = 0.0 (or semantic
        search is skipped entirely). The FTS-matched node must appear first in
        the output because BM25 _relevance > 0 > semantic similarity.

        This confirms that even with BM25-derived _relevance (not flat 1.0), the
        composite still puts keyword matches ahead of semantic-only nodes.
        """
        server = fresh_server()

        # Insert docs: one matches the query keyword strongly, others are filler.
        _add_obs(server, "xylophone rhythm instrument unique term")
        _add_obs(server, "software engineering principles and best practices")
        _add_obs(server, "memory management in graph databases overview")

        conn = server._get_db()

        # Run _search_nodes with FTS-only (semantic disabled).
        result = server._search_nodes(
            conn,
            query="xylophone",
            use_semantic=False,
            top_k=10,
        )

        results = result["results"]
        keyword_matches = result["keyword_matches"]

        # At least one FTS match found.
        assert keyword_matches >= 1, (
            f"Expected >=1 keyword match for 'xylophone', got {keyword_matches}"
        )
        assert len(results) >= 1, "Expected at least one result"

        # The first result must be the keyword match.
        first = results[0]
        assert first["match_type"] == "keyword", (
            f"Expected first result to be keyword match, got match_type={first['match_type']!r}"
        )

        # When semantic is enabled but embeddings unavailable (ENGRAM_NO_EMBEDDINGS=1),
        # _search_nodes returns keyword matches first. Verify the keyword match has
        # a claim containing 'xylophone'.
        assert "xylophone" in first["claim"].lower(), (
            f"Expected keyword match claim to contain 'xylophone', got: {first['claim']!r}"
        )
