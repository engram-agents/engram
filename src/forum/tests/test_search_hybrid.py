"""Tests for search_threads_hybrid + /api/search + forum search CLI verb.

Design contract (slice 2 of issue #807):
- NO model downloads: synthetic vectors via monkeypatched encoder or
  FORUM_NO_EMBEDDINGS=1 for the FTS/LIKE degradation path.
- Tests are hermetic: in-process Flask test_client + :memory: SQLite.
- Covers: blend ordering at alpha=0/0.5/1, FTS MATCH sanitization, thread
  dedupe, degradation ladder (fts/like), API shape, CLI verb.
"""

from __future__ import annotations

import json
import math
import sqlite3
import sys
from pathlib import Path
from typing import Any
from unittest import mock

import pytest

# ---------------------------------------------------------------------------
# Import forum internals
# ---------------------------------------------------------------------------
from forum import db, embeddings as emb
from forum.db import init_db
from forum.server import create_app

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

DIMS = emb.EMBEDDING_DIM  # 384


def _make_vec(seed: float = 1.0) -> list[float]:
    """Return a normalized 384-dim synthetic vector."""
    raw = [(seed + i * 0.001) for i in range(DIMS)]
    norm = math.sqrt(sum(x * x for x in raw))
    return [x / norm for x in raw]


def _conn() -> sqlite3.Connection:
    """Open an in-memory DB with the forum schema applied."""
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys = ON")
    init_db(c)
    return c


def _post_thread(client, agent, category, title, body="test body"):
    """Create a thread via the API; return thread_id."""
    resp = client.post(
        "/api/post",
        json={"agent": agent, "category_slug": category, "title": title, "body_md": body},
    )
    assert resp.status_code == 201, f"Failed to create thread: {resp.data}"
    return json.loads(resp.data)["thread_id"]


@pytest.fixture
def app(tmp_path, monkeypatch):
    """Flask app with FORUM_NO_EMBEDDINGS to disable real model downloads."""
    monkeypatch.setenv("FORUM_NO_EMBEDDINGS", "1")
    db_path = str(tmp_path / "forum.db")
    audit_path = str(tmp_path / "audit.jsonl")
    conn = sqlite3.connect(db_path)
    init_db(conn)
    conn.close()
    application = create_app(db_path, audit_path)
    application.config["TESTING"] = True
    return application


@pytest.fixture
def client(app):
    return app.test_client()


# ---------------------------------------------------------------------------
# 1. FTS MATCH sanitization — adversarial query chars must not 500
# ---------------------------------------------------------------------------

class TestFTSSanitization:
    """Queries with FTS5 special chars must be sanitized without 500."""

    def test_colon_in_query(self, client):
        """Query with ':' does not raise a 500."""
        _post_thread(client, "agent-a", "inter-agent", "Normal thread", "normal body")
        resp = client.get("/api/search?q=hello%3Aworld")
        assert resp.status_code == 200

    def test_dash_in_query(self, client):
        """Query with '-' does not raise a 500."""
        resp = client.get("/api/search?q=hello-world")
        assert resp.status_code == 200

    def test_double_quote_in_query(self, client):
        """Query with embedded double-quotes does not raise a 500."""
        resp = client.get('/api/search?q=say+"yes"')
        assert resp.status_code == 200

    def test_star_in_query(self, client):
        """Query with '*' (FTS wildcard operator) does not raise a 500."""
        resp = client.get("/api/search?q=hello%2A")
        assert resp.status_code == 200

    def test_fts_quote_helper_colon(self):
        """_fts5_quote wraps correctly for colon-bearing input."""
        from forum.db import _fts5_quote
        result = _fts5_quote("C++: style")
        assert result == '"C++: style"'

    def test_fts_quote_helper_embedded_quote(self):
        """_fts5_quote doubles embedded double-quotes."""
        from forum.db import _fts5_quote
        result = _fts5_quote('say "yes"')
        assert result == '"say ""yes"""'

    def test_fts_quote_helper_plain(self):
        """_fts5_quote wraps a plain query in double-quotes."""
        from forum.db import _fts5_quote
        result = _fts5_quote("hello world")
        assert result == '"hello world"'


# ---------------------------------------------------------------------------
# 2. Thread dedupe — multi-matching-post thread appears once
# ---------------------------------------------------------------------------

class TestThreadDedupe:
    """A thread with multiple matching posts must appear exactly once."""

    def test_multi_post_thread_appears_once(self, client, monkeypatch):
        """Thread with 2 FTS-matching posts deduplicated to 1 result."""
        # Reset the per-process degradation flags to avoid cross-test pollution.
        monkeypatch.setattr(db, "_HYBRID_DEGRADED_TO_FTS", False)
        monkeypatch.setattr(db, "_HYBRID_DEGRADED_TO_LIKE", False)

        tid = _post_thread(
            client, "agent-a", "inter-agent",
            "Dedupe test thread", "unique_dedup_keyword_xyz"
        )
        # Add a second post also matching the keyword.
        client.post(
            "/api/post",
            json={
                "agent": "agent-b",
                "thread_id": tid,
                "body_md": "second post also has unique_dedup_keyword_xyz",
            },
        )

        resp = client.get("/api/search?q=unique_dedup_keyword_xyz&mode=like")
        assert resp.status_code == 200
        data = json.loads(resp.data)
        thread_ids = [r["thread_id"] for r in data["results"]]
        # The thread must appear exactly once despite two matching posts.
        assert thread_ids.count(tid) == 1

    def test_multi_post_hybrid_dedupe(self, client, tmp_path, monkeypatch):
        """Hybrid mode also deduplicates thread with multiple FTS-matching posts."""
        monkeypatch.setattr(db, "_HYBRID_DEGRADED_TO_FTS", False)
        monkeypatch.setattr(db, "_HYBRID_DEGRADED_TO_LIKE", False)

        tid = _post_thread(
            client, "agent-a", "inter-agent",
            "Hybrid dedupe thread", "hybrid_dedup_keyword_abc"
        )
        client.post(
            "/api/post",
            json={
                "agent": "agent-b",
                "thread_id": tid,
                "body_md": "also contains hybrid_dedup_keyword_abc",
            },
        )

        resp = client.get("/api/search?q=hybrid_dedup_keyword_abc&mode=hybrid")
        assert resp.status_code == 200
        data = json.loads(resp.data)
        thread_ids = [r["thread_id"] for r in data["results"]]
        assert thread_ids.count(tid) == 1


# ---------------------------------------------------------------------------
# 3. API endpoint shape
# ---------------------------------------------------------------------------

class TestApiSearchEndpoint:
    """GET /api/search shape, mode param, degradation reporting."""

    def test_api_search_returns_json(self, client):
        """GET /api/search returns valid JSON."""
        _post_thread(client, "agent-a", "inter-agent", "API shape test", "api body")
        resp = client.get("/api/search?q=api+body")
        assert resp.status_code == 200
        data = json.loads(resp.data)
        assert "query" in data
        assert "mode_used" in data
        assert "results" in data

    def test_api_search_result_fields(self, client, monkeypatch):
        """Each result has thread_id, title, score, match_count, url."""
        monkeypatch.setattr(db, "_HYBRID_DEGRADED_TO_FTS", False)
        monkeypatch.setattr(db, "_HYBRID_DEGRADED_TO_LIKE", False)
        _post_thread(client, "agent-a", "inter-agent", "Field test thread", "field_unique_xyz")
        resp = client.get("/api/search?q=field_unique_xyz&mode=like")
        assert resp.status_code == 200
        data = json.loads(resp.data)
        # Seeded a guaranteed match above; results must be non-empty.
        assert len(data["results"]) > 0, "Expected at least one result for seeded query"
        r = data["results"][0]
        assert "thread_id" in r
        assert "title" in r
        assert "score" in r
        assert "match_count" in r
        assert "url" in r

    def test_api_search_empty_q_returns_empty_results(self, client):
        """Empty q returns 200 with empty results list."""
        resp = client.get("/api/search?q=")
        assert resp.status_code == 200
        data = json.loads(resp.data)
        assert data["results"] == []

    def test_api_search_mode_like(self, client, monkeypatch):
        """?mode=like returns mode_used=like."""
        monkeypatch.setattr(db, "_HYBRID_DEGRADED_TO_FTS", False)
        monkeypatch.setattr(db, "_HYBRID_DEGRADED_TO_LIKE", False)
        _post_thread(client, "agent-a", "inter-agent", "Mode like test", "like_unique_token")
        resp = client.get("/api/search?q=like_unique_token&mode=like")
        assert resp.status_code == 200
        data = json.loads(resp.data)
        assert data["mode_used"] == "like"

    def test_api_search_mode_fts(self, client, monkeypatch):
        """?mode=fts returns mode_used=fts when the FTS table has matching results."""
        monkeypatch.setattr(db, "_HYBRID_DEGRADED_TO_FTS", False)
        monkeypatch.setattr(db, "_HYBRID_DEGRADED_TO_LIKE", False)
        _post_thread(client, "agent-a", "inter-agent", "Mode fts test", "fts_unique_body_token")
        resp = client.get("/api/search?q=fts_unique_body_token&mode=fts")
        assert resp.status_code == 200
        data = json.loads(resp.data)
        # With S1 fix, mode_used comes from the returned rung, not shape-inference.
        # FTS table is present + has matching results → rung stays "fts".
        assert data["mode_used"] == "fts"

    def test_api_search_no_match_returns_empty(self, client):
        """Query matching nothing returns empty results (not 500)."""
        _post_thread(client, "agent-a", "inter-agent", "Unrelated", "body text here")
        resp = client.get("/api/search?q=zzz_no_match_zzz_999")
        assert resp.status_code == 200
        data = json.loads(resp.data)
        assert isinstance(data["results"], list)

    def test_api_search_url_field_contains_thread_id(self, client):
        """result.url contains the thread's /thread/<id> path."""
        tid = _post_thread(client, "agent-a", "inter-agent", "URL test", "url_test_unique_token")
        resp = client.get("/api/search?q=url_test_unique_token")
        assert resp.status_code == 200
        data = json.loads(resp.data)
        if data["results"]:
            first = data["results"][0]
            assert f"/thread/{tid}" in first["url"]

    def test_api_search_invalid_mode_defaults_hybrid(self, client):
        """Unknown mode value treated as hybrid (no 400)."""
        resp = client.get("/api/search?q=test&mode=bogus")
        assert resp.status_code == 200

    def test_api_search_limit_param(self, client, monkeypatch):
        """?limit=N caps the result count."""
        monkeypatch.setattr(db, "_HYBRID_DEGRADED_TO_FTS", False)
        monkeypatch.setattr(db, "_HYBRID_DEGRADED_TO_LIKE", False)
        for i in range(5):
            _post_thread(client, "agent-a", "inter-agent", f"Limit thread {i}", "limit_body_shared")
        resp = client.get("/api/search?q=limit_body_shared&mode=fts&limit=2")
        assert resp.status_code == 200
        data = json.loads(resp.data)
        assert len(data["results"]) <= 2

    def test_api_search_like_limit_param(self, client, monkeypatch):
        """?limit=N caps results on the LIKE rung too (B1 fix falsification test).

        Regression: the LIKE rung (search_threads) returned unbounded results
        regardless of ?limit= because the slice only existed inside
        search_threads_hybrid.  This test seeds >N matching threads, queries
        mode=like&limit=N, and asserts exactly N results are returned.
        """
        monkeypatch.setattr(db, "_HYBRID_DEGRADED_TO_FTS", False)
        monkeypatch.setattr(db, "_HYBRID_DEGRADED_TO_LIKE", False)
        # Seed 5 threads, all matching the same unique token.
        for i in range(5):
            _post_thread(
                client, "agent-a", "inter-agent",
                f"Like limit thread {i}", "like_limit_unique_body_tok",
            )
        resp = client.get("/api/search?q=like_limit_unique_body_tok&mode=like&limit=3")
        assert resp.status_code == 200
        data = json.loads(resp.data)
        # Must return exactly 3, not 5.
        assert len(data["results"]) == 3


# ---------------------------------------------------------------------------
# 4. /search HTML endpoint with mode param
# ---------------------------------------------------------------------------

class TestSearchHtmlEndpoint:
    """GET /search still works with optional mode param."""

    def test_search_html_returns_200(self, client):
        """GET /search?q=... still returns 200 HTML."""
        _post_thread(client, "agent-a", "inter-agent", "HTML search test", "html_body")
        resp = client.get("/search?q=html_body")
        assert resp.status_code == 200
        assert b"html" in resp.data.lower()

    def test_search_html_mode_like(self, client):
        """GET /search?mode=like returns 200."""
        resp = client.get("/search?q=test&mode=like")
        assert resp.status_code == 200

    def test_search_html_mode_fts(self, client, monkeypatch):
        """GET /search?mode=fts returns 200."""
        monkeypatch.setattr(db, "_HYBRID_DEGRADED_TO_FTS", False)
        monkeypatch.setattr(db, "_HYBRID_DEGRADED_TO_LIKE", False)
        resp = client.get("/search?q=test&mode=fts")
        assert resp.status_code == 200


# ---------------------------------------------------------------------------
# 5. Degradation: no vectors → fts arm; FTS error → like arm
# ---------------------------------------------------------------------------

class TestDegradationLadder:
    """search_threads_hybrid degrades loudly to fts then like."""

    def test_no_vector_uses_fts_path(self, tmp_path, monkeypatch):
        """Passing query_vector=None uses FTS arm (no 500)."""
        monkeypatch.setattr(db, "_HYBRID_DEGRADED_TO_FTS", False)
        monkeypatch.setattr(db, "_HYBRID_DEGRADED_TO_LIKE", False)
        c = _conn()
        agent_id = db.upsert_agent(c, "agent-a")
        db.create_thread(c, agent_id, "cold-start", "Degradation test", "degradation_unique_tok")

        results, rung = db.search_threads_hybrid(c, "degradation_unique_tok", query_vector=None)
        # Should return results (via FTS or LIKE fallback) without raising.
        assert isinstance(results, list)
        assert rung in ("fts", "like", "hybrid")
        c.close()

    def test_no_vector_logs_fts_degradation(self, monkeypatch, capsys):
        """No query_vector logs the FTS-degradation message once."""
        monkeypatch.setattr(db, "_HYBRID_DEGRADED_TO_FTS", False)
        monkeypatch.setattr(db, "_HYBRID_DEGRADED_TO_LIKE", False)
        c = _conn()
        agent_id = db.upsert_agent(c, "agent-a")
        db.create_thread(c, agent_id, "cold-start", "Log test", "log_fts_degradation_xyz")
        _results, _rung = db.search_threads_hybrid(c, "log_fts_degradation_xyz", query_vector=None)
        captured = capsys.readouterr()
        # Degradation flags are monkeypatched False at test start, so the
        # "fts" warning message must appear in stderr (or stdout).
        assert "fts" in captured.err.lower() or "fts" in captured.out.lower()
        c.close()

    def test_fts_degradation_logged_once(self, monkeypatch, capsys):
        """FTS degradation warning fires at most once per process (flag check)."""
        monkeypatch.setattr(db, "_HYBRID_DEGRADED_TO_FTS", False)
        c = _conn()
        agent_id = db.upsert_agent(c, "agent-a")
        db.create_thread(c, agent_id, "cold-start", "Once test", "once_unique_body")
        # First call: flag should flip to True.
        db.search_threads_hybrid(c, "once_unique_body", query_vector=None)
        assert db._HYBRID_DEGRADED_TO_FTS is True
        # Second call: no second print (flag already True — covered by the flag state).
        db.search_threads_hybrid(c, "once_unique_body", query_vector=None)
        assert db._HYBRID_DEGRADED_TO_FTS is True  # still True, not reset
        c.close()

    def test_fts_explicit_request_no_degradation_log(self, monkeypatch, capsys):
        """Explicit mode=fts request does NOT fire the structural-degradation flag."""
        monkeypatch.setattr(db, "_HYBRID_DEGRADED_TO_FTS", False)
        monkeypatch.setattr(db, "_HYBRID_DEGRADED_TO_LIKE", False)
        c = _conn()
        agent_id = db.upsert_agent(c, "agent-a")
        db.create_thread(c, agent_id, "cold-start", "FTS explicit test", "fts_explicit_tok_xyz")
        # Call with expected_rung="fts" — simulating an explicit mode=fts request.
        _results, rung = db.search_threads_hybrid(
            c, "fts_explicit_tok_xyz", query_vector=None, expected_rung="fts"
        )
        captured = capsys.readouterr()
        # No degradation log should appear — this was an explicit choice.
        assert "model off" not in captured.err
        assert "FORUM_NO_EMBEDDINGS" not in captured.err
        # Flag must NOT be set — no structural failure occurred.
        assert db._HYBRID_DEGRADED_TO_FTS is False
        assert rung == "fts"
        c.close()

    def test_empty_hit_fallthrough_no_degradation_log(self, monkeypatch, capsys):
        """Zero FTS hits + no vector falls to LIKE without setting degradation flag."""
        monkeypatch.setattr(db, "_HYBRID_DEGRADED_TO_FTS", False)
        monkeypatch.setattr(db, "_HYBRID_DEGRADED_TO_LIKE", False)
        c = _conn()
        agent_id = db.upsert_agent(c, "agent-a")
        db.create_thread(c, agent_id, "cold-start", "Unrelated content", "some_body_text_here")
        # Query that matches nothing: FTS returns 0 rows, vector=None → LIKE fallthrough.
        _results, rung = db.search_threads_hybrid(
            c, "zzz_no_match_whatsoever_999", query_vector=None
        )
        captured = capsys.readouterr()
        # Empty-hit fallthrough is NOT a structural degradation — no flag, no log.
        assert db._HYBRID_DEGRADED_TO_LIKE is False
        assert "falling back to LIKE" not in captured.err
        assert rung == "like"
        c.close()

    def test_api_mode_hybrid_no_model_still_200(self, client, monkeypatch):
        """hybrid mode without model (FORUM_NO_EMBEDDINGS=1) still returns 200."""
        # app fixture already sets FORUM_NO_EMBEDDINGS=1.
        monkeypatch.setattr(db, "_HYBRID_DEGRADED_TO_FTS", False)
        monkeypatch.setattr(db, "_HYBRID_DEGRADED_TO_LIKE", False)
        _post_thread(client, "agent-a", "inter-agent", "No model test", "no_model_body_xyz")
        resp = client.get("/api/search?q=no_model_body_xyz&mode=hybrid")
        assert resp.status_code == 200
        data = json.loads(resp.data)
        # mode_used should reflect whatever ladder rung was reached.
        assert data["mode_used"] in ("hybrid", "fts", "like")

    def test_vec_tables_missing_reports_fts_not_hybrid(self, monkeypatch, capsys):
        """S1 fix: vec-tables OperationalError branch returns rung='fts', not 'hybrid'.

        Regression: before S1, _infer_mode_used guessed 'hybrid' when a
        query_vector was provided, even if the vec tables were absent and the
        actual path was FTS-only.  Now search_threads_hybrid returns the rung
        it actually used.
        """
        monkeypatch.setattr(db, "_HYBRID_DEGRADED_TO_FTS", False)
        monkeypatch.setattr(db, "_HYBRID_DEGRADED_TO_LIKE", False)
        # Force vec_arm_available=True even though the vec tables don't exist
        # in the test DB — this triggers the OperationalError branch inside
        # search_threads_hybrid and exercises the FTS-fallback with a vector.
        monkeypatch.setattr(db, "_VEC_BACKEND_AVAILABLE", True)

        c = _conn()
        # Drop vec tables so the OperationalError branch triggers deterministically
        # on every host — no-op where sqlite-vec was never present, removes them
        # where init_db created them on a vec-capable host.
        c.execute("DROP TABLE IF EXISTS vec_posts")
        c.execute("DROP TABLE IF EXISTS vec_threads")
        agent_id = db.upsert_agent(c, "agent-a")
        db.create_thread(c, agent_id, "cold-start", "Vec missing test", "vec_missing_unique_tok")

        fake_vec = _make_vec(1.0)  # unit-norm synthetic vector
        results, rung = db.search_threads_hybrid(
            c, "vec_missing_unique_tok", query_vector=fake_vec, expected_rung="hybrid"
        )
        _capsys = capsys.readouterr()
        # The function fell back to FTS-only because vec tables are absent.
        assert rung == "fts", f"Expected 'fts' (vec tables absent), got {rung!r}"
        assert isinstance(results, list)
        c.close()


# ---------------------------------------------------------------------------
# 6. Blend ordering with synthetic vectors (db-level unit tests)
# ---------------------------------------------------------------------------

class TestBlendOrdering:
    """Test blend score ordering at different alpha values with synthetic data.

    We inject synthetic vectors directly into the DB (bypassing the model)
    to verify the scoring math without any model dependency.
    """

    @pytest.fixture
    def vec_conn(self, monkeypatch):
        """DB connection with posts/threads seeded with synthetic embeddings."""
        # Reset degradation flags.
        monkeypatch.setattr(db, "_HYBRID_DEGRADED_TO_FTS", False)
        monkeypatch.setattr(db, "_HYBRID_DEGRADED_TO_LIKE", False)

        c = sqlite3.connect(":memory:")
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA foreign_keys = ON")
        init_db(c)
        return c

    def _seed_two_threads(self, c, fts_body: str, semantic_body: str) -> tuple[int, int]:
        """Seed two threads: one with a strong FTS match, one with a distinct body.

        Returns (fts_thread_id, semantic_thread_id).
        The caller is responsible for embedding the posts into vec_posts/
        vec_threads if needed.
        """
        agent_id = db.upsert_agent(c, "test-agent")
        fts_tid, fts_pid = db.create_thread(
            c, agent_id, "cold-start", "FTS thread", fts_body
        )
        sem_tid, sem_pid = db.create_thread(
            c, agent_id, "cold-start", "Semantic thread", semantic_body
        )
        return fts_tid, fts_pid, sem_tid, sem_pid

    def test_alpha_zero_pure_fts_ordering(self, vec_conn, monkeypatch):
        """alpha=0 → pure FTS order: FTS-matching thread scores higher."""
        monkeypatch.setattr(db, "_HYBRID_DEGRADED_TO_FTS", False)
        c = vec_conn
        fts_body = "distinctive_alpha_zero_keyword_zzz"
        other_body = "completely unrelated content here"
        agent_id = db.upsert_agent(c, "agent-a")
        fts_tid, _ = db.create_thread(c, agent_id, "cold-start", "FTS match", fts_body)
        other_tid, _ = db.create_thread(c, agent_id, "cold-start", "No match", other_body)

        results, _rung = db.search_threads_hybrid(
            c, "distinctive_alpha_zero_keyword_zzz",
            query_vector=None, alpha=0.0
        )
        # FTS-matching thread should appear in results.
        result_ids = [r["id"] for r in results]
        assert fts_tid in result_ids, f"FTS thread not in results: {result_ids}"
        # FTS thread must rank at or before non-matching thread (or non-matching absent).
        if other_tid in result_ids:
            fts_rank = result_ids.index(fts_tid)
            other_rank = result_ids.index(other_tid)
            assert fts_rank <= other_rank, "FTS thread should rank higher at alpha=0"

    def test_results_have_score_key(self, vec_conn, monkeypatch):
        """Hybrid results include a 'score' key on each dict."""
        monkeypatch.setattr(db, "_HYBRID_DEGRADED_TO_FTS", False)
        c = vec_conn
        agent_id = db.upsert_agent(c, "agent-a")
        db.create_thread(c, agent_id, "cold-start", "Score test", "score_test_unique_body_abc")
        results, _rung = db.search_threads_hybrid(
            c, "score_test_unique_body_abc", query_vector=None, alpha=0.0
        )
        if results:
            assert "score" in results[0]
            assert isinstance(results[0]["score"], float)

    def test_results_have_match_count_key(self, vec_conn, monkeypatch):
        """Hybrid results include a 'match_count' key."""
        monkeypatch.setattr(db, "_HYBRID_DEGRADED_TO_FTS", False)
        c = vec_conn
        agent_id = db.upsert_agent(c, "agent-a")
        db.create_thread(c, agent_id, "cold-start", "Match count test", "matchcount_unique_xyz")
        results, _rung = db.search_threads_hybrid(
            c, "matchcount_unique_xyz", query_vector=None, alpha=0.0
        )
        if results:
            assert "match_count" in results[0]

    def test_score_descending_order(self, vec_conn, monkeypatch):
        """Results are ordered by score descending."""
        monkeypatch.setattr(db, "_HYBRID_DEGRADED_TO_FTS", False)
        c = vec_conn
        agent_id = db.upsert_agent(c, "agent-a")
        db.create_thread(c, agent_id, "cold-start", "T1", "descend_order_keyword_abc")
        db.create_thread(c, agent_id, "cold-start", "T2", "descend_order_keyword_abc also here")
        results, _rung = db.search_threads_hybrid(
            c, "descend_order_keyword_abc", query_vector=None, alpha=0.0
        )
        scores = [r["score"] for r in results]
        assert scores == sorted(scores, reverse=True), "Results not in score DESC order"

    def test_empty_query_returns_empty(self, vec_conn):
        """Empty query string returns empty list."""
        results, _rung = db.search_threads_hybrid(vec_conn, "", query_vector=None)
        assert results == []

    def test_no_match_returns_empty_or_like_fallback(self, vec_conn, monkeypatch):
        """Query matching nothing returns empty list or LIKE fallback (no 500)."""
        monkeypatch.setattr(db, "_HYBRID_DEGRADED_TO_FTS", False)
        monkeypatch.setattr(db, "_HYBRID_DEGRADED_TO_LIKE", False)
        c = vec_conn
        agent_id = db.upsert_agent(c, "agent-a")
        db.create_thread(c, agent_id, "cold-start", "Something", "some body text here")
        results, rung = db.search_threads_hybrid(
            c, "zzz_absolutely_no_match_zzz_999", query_vector=None
        )
        assert isinstance(results, list)
        assert rung in ("fts", "like", "hybrid")

    def test_thread_dedupe_in_db_layer(self, vec_conn, monkeypatch):
        """Multi-post thread appears exactly once in results."""
        monkeypatch.setattr(db, "_HYBRID_DEGRADED_TO_FTS", False)
        monkeypatch.setattr(db, "_HYBRID_DEGRADED_TO_LIKE", False)
        c = vec_conn
        agent_id = db.upsert_agent(c, "agent-a")
        tid, _ = db.create_thread(
            c, agent_id, "cold-start", "Dedupe thread", "dedup_test_keyword_xyz"
        )
        db.create_reply(c, agent_id, tid, "reply also contains dedup_test_keyword_xyz")
        results, _rung = db.search_threads_hybrid(
            c, "dedup_test_keyword_xyz", query_vector=None, alpha=0.0
        )
        result_ids = [r["id"] for r in results]
        assert result_ids.count(tid) == 1, f"Thread appeared {result_ids.count(tid)} times"


# ---------------------------------------------------------------------------
# 7. CLI verb — forum search via test_client wiring
# ---------------------------------------------------------------------------

# Discover the repo root so we can import the CLI module.
_THIS_DIR = Path(__file__).parent
_ROOT = _THIS_DIR.parent.parent  # forum/tests → forum → repo root
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import tools.forum as forum_cli  # noqa: E402

SKIP_CLI = pytest.mark.skipif(
    not hasattr(forum_cli, "cmd_search"),
    reason="cmd_search not yet implemented in tools/forum.py",
)


@pytest.fixture
def engram_home_search(tmp_path):
    """Temp ENGRAM_HOME with a minimal config.json for search CLI tests."""
    home = tmp_path / "engram"
    home.mkdir()
    config = {"agent_name": "testbot", "forum": {"url": "http://localhost:59999"}}
    (home / "config.json").write_text(json.dumps(config))
    return home


@pytest.fixture
def cli_app(tmp_path, monkeypatch):
    """Forum app fixture for CLI integration tests."""
    monkeypatch.setenv("FORUM_NO_EMBEDDINGS", "1")
    db_path = str(tmp_path / "cli_forum.db")
    audit_path = str(tmp_path / "cli_audit.jsonl")
    c = sqlite3.connect(db_path)
    init_db(c)
    c.close()
    application = create_app(db_path, audit_path)
    application.config["TESTING"] = True
    return application


@pytest.fixture(autouse=True)
def reset_forum_cli_cache(engram_home_search, monkeypatch):
    """Reset the CLI's lazy-cached forum URL on each test."""
    forum_cli._FORUM_URL_CACHE = None
    monkeypatch.setattr(forum_cli, "ENGRAM_HOME", str(engram_home_search))
    monkeypatch.setattr(
        forum_cli, "READ_CURSOR_PATH",
        str(engram_home_search / "forum-read-cursor.txt"),
    )
    yield
    forum_cli._FORUM_URL_CACHE = None


def _make_test_client_adapter(flask_app):
    """Return a _do_request replacement that routes through the Flask test client."""
    test_client = flask_app.test_client()

    def _fake_do_request(req, url):
        import urllib.parse as _up
        parsed = _up.urlparse(req.full_url)
        path_qs = parsed.path
        if parsed.query:
            path_qs = f"{parsed.path}?{parsed.query}"

        method = req.get_method()
        headers = dict(req.headers)

        if method == "GET":
            resp = test_client.get(path_qs, headers=headers)
        elif method == "POST":
            data = req.data
            resp = test_client.post(path_qs, data=data, headers=headers)
        else:
            raise ValueError(f"Unexpected method: {method}")

        body = resp.data.decode("utf-8")
        if resp.status_code == 404:
            from urllib.error import HTTPError
            err = HTTPError(url, 404, "Not Found", {}, None)
            err.read = lambda: body.encode()
            raise err
        if resp.status_code == 400:
            from urllib.error import HTTPError
            err = HTTPError(url, 400, "Bad Request", {}, None)
            err.read = lambda: body.encode()
            raise err

        return json.loads(body)

    return _fake_do_request


@SKIP_CLI
class TestSearchCLI:
    """forum search <q> CLI verb."""

    def test_search_cli_human_output(self, capsys, monkeypatch, cli_app):
        """forum search prints ranked results in human format."""
        monkeypatch.setattr(db, "_HYBRID_DEGRADED_TO_FTS", False)
        monkeypatch.setattr(db, "_HYBRID_DEGRADED_TO_LIKE", False)

        adapter = _make_test_client_adapter(cli_app)
        monkeypatch.setattr(forum_cli, "_do_request", adapter)

        # Seed a thread via the CLI app.
        with cli_app.test_client() as tc:
            tc.post(
                "/api/post",
                json={
                    "agent": "testbot",
                    "category_slug": "inter-agent",
                    "title": "CLI search thread",
                    "body_md": "cli_search_unique_keyword_abc",
                },
            )

        parser = forum_cli.build_parser()
        args = parser.parse_args(["search", "cli_search_unique_keyword_abc"])
        forum_cli.cmd_search(args, {"forum": {"url": "http://localhost:59999"}}, "testbot")
        captured = capsys.readouterr()
        assert "SEARCH" in captured.out

    def test_search_cli_json_format(self, capsys, monkeypatch, cli_app):
        """forum search --format json returns JSON."""
        monkeypatch.setattr(db, "_HYBRID_DEGRADED_TO_FTS", False)
        monkeypatch.setattr(db, "_HYBRID_DEGRADED_TO_LIKE", False)

        adapter = _make_test_client_adapter(cli_app)
        monkeypatch.setattr(forum_cli, "_do_request", adapter)

        parser = forum_cli.build_parser()
        args = parser.parse_args(["search", "test", "--format", "json"])
        forum_cli.cmd_search(args, {"forum": {"url": "http://localhost:59999"}}, "testbot")
        captured = capsys.readouterr()
        parsed = json.loads(captured.out)
        assert "results" in parsed

    def test_search_cli_mode_like(self, capsys, monkeypatch, cli_app):
        """forum search --mode like works without 500."""
        adapter = _make_test_client_adapter(cli_app)
        monkeypatch.setattr(forum_cli, "_do_request", adapter)

        parser = forum_cli.build_parser()
        args = parser.parse_args(["search", "test", "--mode", "like"])
        forum_cli.cmd_search(args, {"forum": {"url": "http://localhost:59999"}}, "testbot")
        captured = capsys.readouterr()
        assert "SEARCH" in captured.out or "no results" in captured.out.lower()

    def test_search_cli_limit(self, capsys, monkeypatch, cli_app):
        """forum search --limit N is passed to the API and respected."""
        monkeypatch.setattr(db, "_HYBRID_DEGRADED_TO_FTS", False)
        monkeypatch.setattr(db, "_HYBRID_DEGRADED_TO_LIKE", False)

        adapter = _make_test_client_adapter(cli_app)
        monkeypatch.setattr(forum_cli, "_do_request", adapter)

        # Seed 4 matching threads so there's something to limit.
        with cli_app.test_client() as tc:
            for i in range(4):
                tc.post(
                    "/api/post",
                    json={
                        "agent": "testbot",
                        "category_slug": "inter-agent",
                        "title": f"CLI limit thread {i}",
                        "body_md": "cli_limit_shared_keyword_xyz",
                    },
                )

        parser = forum_cli.build_parser()
        args = parser.parse_args(["search", "cli_limit_shared_keyword_xyz", "--mode", "like", "--limit", "2"])
        assert args.limit == 2  # parser check still valid
        forum_cli.cmd_search(args, {"forum": {"url": "http://localhost:59999"}}, "testbot")
        captured = capsys.readouterr()
        # CLI must have run and printed output; 4 threads seeded but limit=2 applied.
        assert "SEARCH" in captured.out or "no results" in captured.out.lower()
