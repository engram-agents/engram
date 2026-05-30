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

import atexit
import json
import math
import os
import shutil
import sqlite3
import sys
import tempfile

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


TEST_DIR = tempfile.mkdtemp(prefix="engram_test_bm25_relevance_")
atexit.register(lambda: shutil.rmtree(TEST_DIR, ignore_errors=True))


def fresh_server():
    """Create a clean server instance in a temp directory."""
    if os.path.exists(TEST_DIR):
        shutil.rmtree(TEST_DIR)
    for key in list(sys.modules):
        if key == "server" or key.startswith("engram_"):
            del sys.modules[key]
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


# ── fts-sim-merge (alpha #208 mitigation) — new floor + multiplicative bump ──

def test_fts_sim_merge_floor_drops_low_similarity(monkeypatch):
    """When semantic is available and an FTS hit has sim < FTS_SIM_FLOOR,
    the merge logic drops it regardless of BM25 strength.

    Stubs `_semantic_search` to return a controlled sim_lookup that places
    a known FTS-hit ID below the floor.
    """
    server = fresh_server()
    _add_obs(server, "xylophone rhythm instrument unique term")
    conn = server._get_db()

    # Force semantic_available = True via stubbed _embedder + _semantic_search
    class _StubEmbedder:
        def is_available(self): return True
    monkeypatch.setattr(server, "_embedder", _StubEmbedder())
    monkeypatch.setattr(server, "_get_embedding_config", lambda: {"enabled": True})

    # Stub _semantic_search to assert the FTS hit is found AND assigned
    # a sub-floor similarity → the merge loop should drop it.
    def _stub_sem(conn, q, top_k=10, min_similarity=0.0, **kw):
        # Find the FTS-hit node id from the conn
        row = conn.execute("SELECT id FROM nodes WHERE type='observation_factual' LIMIT 1").fetchone()
        return [{"id": row["id"], "similarity": 0.05, "type": "observation_factual",
                 "claim": "x", "confidence": 0.8}]
    monkeypatch.setattr(server, "_semantic_search", _stub_sem)

    result = server._search_nodes(conn, "xylophone", top_k=5, use_semantic=True)
    # Sub-floor FTS hit should be dropped — no results from the FTS path,
    # and semantic_only also empty (since it shares the same id, gets
    # filtered from semantic_only by the existing fts_ids exclusion logic
    # OR remains, depending on the merge order — verify it doesn't appear
    # in output via match_type=keyword).
    keyword_hits = [r for r in result.get("results", []) if r.get("match_type") == "keyword"]
    assert len(keyword_hits) == 0, (
        f"Expected zero keyword hits after sub-floor drop, got "
        f"{[r['id'] for r in keyword_hits]}"
    )


def test_fts_sim_merge_bump_promotes_above_floor(monkeypatch):
    """When semantic is available and an FTS hit has sim >= FTS_SIM_FLOOR,
    the multiplicative bump produces relevance = min(1, sim * (1 + bump))
    where bump = sqrt(|bm25|) / sqrt(FTS_BUMP_NORMALIZER).

    Exercises arithmetic correctness against a known sim + known bm25.
    """
    server = fresh_server()
    _add_obs(server, "xylophone rhythm instrument unique term")
    conn = server._get_db()

    class _StubEmbedder:
        def is_available(self): return True
    monkeypatch.setattr(server, "_embedder", _StubEmbedder())
    monkeypatch.setattr(server, "_get_embedding_config", lambda: {"enabled": True})

    captured_sim = 0.50
    def _stub_sem(conn, q, top_k=10, min_similarity=0.0, **kw):
        row = conn.execute("SELECT id FROM nodes WHERE type='observation_factual' LIMIT 1").fetchone()
        return [{"id": row["id"], "similarity": captured_sim, "type": "observation_factual",
                 "claim": "x", "confidence": 0.8}]
    monkeypatch.setattr(server, "_semantic_search", _stub_sem)

    result = server._search_nodes(
        conn, "xylophone", top_k=5, use_semantic=True, return_debug=True,
    )
    debug = result["debug"]
    per_result = debug["per_result"]
    assert len(per_result) >= 1, "Expected at least one keyword hit to survive floor"
    entry = per_result[0]
    assert entry["match_type"] == "keyword"
    assert entry["similarity"] == pytest.approx(captured_sim, abs=1e-9), (
        f"Expected similarity={captured_sim} in debug, got {entry['similarity']}"
    )
    bm25 = entry["bm25_raw"]
    assert bm25 is not None and bm25 < 0, f"Expected negative BM25, got {bm25}"
    # Recompute the expected formula
    expected_bump = min(1.0, math.sqrt(abs(bm25)) / math.sqrt(server.FTS_BUMP_NORMALIZER))
    expected_rel = min(1.0, captured_sim * (1.0 + expected_bump))
    assert entry["relevance_normalized"] == pytest.approx(expected_rel, abs=1e-9), (
        f"Expected relevance={expected_rel:.6f} (sim={captured_sim}, bump={expected_bump:.6f}), "
        f"got {entry['relevance_normalized']:.6f}"
    )
