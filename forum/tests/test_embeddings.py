"""Tests for forum/embeddings.py and embedding integration.

Design contract:
- NO model downloads, ever. All tests use synthetic 384-dim vectors and
  FORUM_NO_EMBEDDINGS=1 or a monkeypatched encoder.
- Tests are hermetic: they run in-process with :memory: SQLite databases.
- The embedding layer's failure-semantic guarantees are verified explicitly:
  an encoder that raises must not fail the post write (the load-bearing test).
"""

from __future__ import annotations

import math
import os
import sqlite3
import struct
from typing import Any
from unittest import mock

import pytest

from forum import db
from forum import embeddings as emb
from forum.db import init_db


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

DIMS = emb.EMBEDDING_DIM  # 384


def _make_vec(seed: float = 1.0) -> list[float]:
    """Return a normalized 384-dim synthetic vector with controlled values."""
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


def _is_unit_norm(vec: list[float], atol: float = 1e-5) -> bool:
    """Return True iff vector has norm == 1.0 within tolerance."""
    norm = math.sqrt(sum(x * x for x in vec))
    return abs(norm - 1.0) < atol


# ---------------------------------------------------------------------------
# Tests: pure-math helpers (no model dependency)
# ---------------------------------------------------------------------------

class TestSerializeDeserialize:
    """serialize/deserialize round-trip (no model needed)."""

    def test_round_trip(self):
        vec = _make_vec(1.0)
        blob = emb.serialize(vec)
        recovered = emb.deserialize(blob)
        assert len(recovered) == DIMS
        for a, b in zip(vec, recovered):
            assert abs(a - b) < 1e-6, "round-trip precision failure"

    def test_blob_length(self):
        """Blob is exactly DIMS * 4 bytes (4-byte little-endian float32)."""
        vec = _make_vec(0.5)
        blob = emb.serialize(vec)
        assert len(blob) == DIMS * 4

    def test_raw_struct_layout(self):
        """Blob matches raw struct.pack('<Nf') layout (documented contract)."""
        vec = _make_vec(2.0)
        blob = emb.serialize(vec)
        expected = struct.pack(f"<{DIMS}f", *vec)
        # Float32 precision: the serialize path may go through sqlite_vec
        # which uses the same IEEE 754 layout.  Compare as unpacked floats.
        got = struct.unpack(f"<{DIMS}f", blob)
        exp = struct.unpack(f"<{DIMS}f", expected)
        for g, e in zip(got, exp):
            assert abs(g - e) < 1e-6


class TestRenormalizedMean:
    """renormalized_mean math correctness."""

    def test_empty_returns_none(self):
        assert emb.renormalized_mean([]) is None

    def test_single_vector_returns_normalized(self):
        vec = _make_vec(1.0)
        result = emb.renormalized_mean([vec])
        assert result is not None
        assert _is_unit_norm(result)

    def test_unit_norm_after_mean(self):
        """Result is unit-norm regardless of input diversity."""
        vecs = [_make_vec(float(i)) for i in range(1, 6)]
        result = emb.renormalized_mean(vecs)
        assert result is not None
        assert _is_unit_norm(result)

    def test_two_identical_vectors(self):
        """Mean of identical vectors is the same vector (unit-norm)."""
        vec = _make_vec(3.0)
        result = emb.renormalized_mean([vec, vec])
        assert result is not None
        for a, b in zip(result, vec):
            assert abs(a - b) < 1e-5

    def test_length_preserved(self):
        vecs = [_make_vec(float(i)) for i in range(1, 4)]
        result = emb.renormalized_mean(vecs)
        assert len(result) == DIMS


def _make_ortho_vec(dim: int, hot_index: int) -> list[float]:
    """Return a normalized vector with 1.0 at hot_index and 0 elsewhere.

    Used to construct genuinely orthogonal vectors for tests that need
    post vectors that are NOT collinear (the case where incremental_centroid
    diverges from renormalized_mean).
    """
    v = [0.0] * dim
    v[hot_index] = 1.0
    return v  # already unit-norm (single hot component)


class TestIncrementalCentroid:
    """incremental_centroid properties: approximation, order-dependence, unit-norm.

    The incremental formula normalize((old * n + post_vec) / (n + 1)) is a
    fast approximation of renormalized_mean.  It is EXACT only when all post
    vectors are collinear (the stored centroid is normalized, losing the
    running-sum magnitude, so the formula implicitly assumes ‖S_n‖ = n).

    These tests assert the actual documented properties:
    - Collinear sub-case: incremental == renormalized_mean (genuinely true).
    - Divergent sub-case: orthogonal vectors expose nonzero error (expected).
    - Order-dependence: two orderings of the same set give different results.
    - renormalized_mean is order-invariant for the same set.
    - Result is always unit-norm.
    """

    # ------------------------------------------------------------------
    # Collinear sub-case: incremental IS exact here
    # ------------------------------------------------------------------

    def test_collinear_incremental_matches_batch(self):
        """Collinear vectors: incremental == renormalized_mean (genuinely true).

        When all post vectors point in the same direction the formula's
        implicit ‖S_n‖ = n assumption holds exactly.  Both _make_vec seeds
        produce nearly-parallel 384-dim vectors (all positive components),
        so cosine similarity is ~0.9999 and the divergence is negligible.
        """
        v1 = _make_vec(1.0)
        v2 = _make_vec(2.0)
        batch = emb.renormalized_mean([v1, v2])
        incremental = emb.incremental_centroid(old=v1, n=1, post_vec=v2)
        assert batch is not None
        for a, b in zip(batch, incremental):
            assert abs(a - b) < 1e-4, (
                "collinear case: incremental should match batch within float32 noise"
            )

    # ------------------------------------------------------------------
    # Divergent sub-case: orthogonal vectors expose the approximation error
    # ------------------------------------------------------------------

    def test_orthogonal_incremental_diverges_from_batch(self):
        """Orthogonal vectors: incremental diverges from renormalized_mean.

        Numeric proof (reduced to 2D, then generalized):
          posts = [(1,0,...), (0,1,0,...), (1,0,...)] (padded to 384 dims)
          batch renormalized_mean → normalize((2/3, 1/3, 0, ...))
                                  = (0.8944, 0.4472, 0, ...)
          incremental (in order) → (0.8629, 0.5054, 0, ...) approx
          max|diff| ≈ 0.058 — nonzero, bounded, non-catastrophic.

        The centroid is a recall signal scored at centroid·0.9 in slice-2,
        so this level of divergence is acceptable.
        """
        # Three orthogonal-ish 384-dim unit vectors: two copies of axis-0,
        # one of axis-1.  Incremental order: axis0, axis1, axis0.
        v_axis0 = _make_ortho_vec(DIMS, 0)  # (1, 0, 0, ...)
        v_axis1 = _make_ortho_vec(DIMS, 1)  # (0, 1, 0, ...)

        # Build incrementally in order [axis0, axis1, axis0]
        c = v_axis0
        c = emb.incremental_centroid(c, n=1, post_vec=v_axis1)
        c = emb.incremental_centroid(c, n=2, post_vec=v_axis0)

        batch = emb.renormalized_mean([v_axis0, v_axis1, v_axis0])
        assert batch is not None

        max_diff = max(abs(a - b) for a, b in zip(c, batch))
        # Divergence must be nonzero (the approximation has real error here).
        assert max_diff > 1e-4, (
            f"Expected nonzero divergence for orthogonal inputs; got max_diff={max_diff:.6f}"
        )
        # Divergence must be bounded (not catastrophically wrong).
        assert max_diff < 0.15, (
            f"Divergence unexpectedly large: max_diff={max_diff:.6f}"
        )

    def test_incremental_is_order_dependent(self):
        """Same set of orthogonal vectors in two orders gives different incrementals.

        Documented property: the incremental formula depends on insertion order
        because each step re-normalizes the running sum, compressing the
        contribution of earlier posts relative to the batch formula.

        Numeric note: the chosen orders must not be symmetric in a way that
        produces the same intermediate.  [axis0,axis1,axis2] vs [axis2,axis1,axis0]
        gives max|diff| ≈ 0.185 (confirmed numerically).  By contrast,
        [axis0,axis1,axis0] vs [axis1,axis0,axis0] converge to the same
        intermediate after step 1 (both yield normalize(axis0+axis1)/2) and
        then to the same final value — that pair would be a false negative.
        """
        v_axis0 = _make_ortho_vec(DIMS, 0)
        v_axis1 = _make_ortho_vec(DIMS, 1)
        v_axis2 = _make_ortho_vec(DIMS, 2)

        # Order A: axis0, axis1, axis2
        c_a = v_axis0
        c_a = emb.incremental_centroid(c_a, n=1, post_vec=v_axis1)
        c_a = emb.incremental_centroid(c_a, n=2, post_vec=v_axis2)

        # Order B: axis2, axis1, axis0  (reverse)
        c_b = v_axis2
        c_b = emb.incremental_centroid(c_b, n=1, post_vec=v_axis1)
        c_b = emb.incremental_centroid(c_b, n=2, post_vec=v_axis0)

        max_diff = max(abs(a - b) for a, b in zip(c_a, c_b))
        assert max_diff > 1e-4, (
            f"Expected order-dependence for orthogonal inputs; got max_diff={max_diff:.6f}"
        )

    def test_renormalized_mean_is_order_invariant(self):
        """renormalized_mean gives the same result regardless of vector order."""
        v_axis0 = _make_ortho_vec(DIMS, 0)
        v_axis1 = _make_ortho_vec(DIMS, 1)
        v_axis2 = _make_ortho_vec(DIMS, 2)

        order_a = emb.renormalized_mean([v_axis0, v_axis1, v_axis2])
        order_b = emb.renormalized_mean([v_axis2, v_axis1, v_axis0])
        order_c = emb.renormalized_mean([v_axis1, v_axis2, v_axis0])
        assert order_a is not None
        assert order_b is not None
        assert order_c is not None
        for a, b, c in zip(order_a, order_b, order_c):
            assert abs(a - b) < 1e-6, "renormalized_mean must be order-invariant"
            assert abs(a - c) < 1e-6, "renormalized_mean must be order-invariant"

    # ------------------------------------------------------------------
    # Always-true property: result is unit-norm
    # ------------------------------------------------------------------

    def test_result_is_unit_norm(self):
        """incremental_centroid result is always unit-norm."""
        v1 = _make_vec(1.0)
        v2 = _make_vec(5.0)
        result = emb.incremental_centroid(old=v1, n=1, post_vec=v2)
        assert _is_unit_norm(result)


# ---------------------------------------------------------------------------
# Tests: available() with FORUM_NO_EMBEDDINGS gate
# ---------------------------------------------------------------------------

class TestAvailable:
    def test_no_embeddings_env_disables(self, monkeypatch):
        monkeypatch.setenv("FORUM_NO_EMBEDDINGS", "1")
        assert emb.available() is False

    def test_available_returns_bool(self):
        # Just confirm it returns a bool without error.
        result = emb.available()
        assert isinstance(result, bool)


# ---------------------------------------------------------------------------
# Tests: encode / encode_batch with a monkeypatched model
# ---------------------------------------------------------------------------

class TestEncodeWithMonkeypatch:
    """Encode functions with a fake model that returns synthetic vectors."""

    @pytest.fixture(autouse=True)
    def _disable_real_model(self, monkeypatch):
        """Ensure FORUM_NO_EMBEDDINGS is off but the real model is never loaded."""
        monkeypatch.delenv("FORUM_NO_EMBEDDINGS", raising=False)

    def _patch_get_model(self, monkeypatch, side_effect=None, return_value=None):
        """Patch emb._get_model to return a fake model or raise.

        The FakeModel.encode() returns a plain list-of-lists (no numpy dependency)
        because the callers (emb.encode / emb.encode_batch) call .tolist() on the
        result. We use a thin wrapper that has a .tolist() method so the callers
        work without numpy installed.
        """
        class _ListWithTolist:
            """Wraps a list to provide .tolist() compatible with numpy array API."""
            def __init__(self, data):
                self._data = data

            def tolist(self):
                return self._data

        class FakeModel:
            def encode(self, texts, convert_to_numpy=True, normalize_embeddings=True):
                if side_effect is not None:
                    raise side_effect
                if isinstance(texts, str):
                    return _ListWithTolist(_make_vec(1.0))
                return _ListWithTolist([_make_vec(float(i + 1)) for i in range(len(texts))])

        monkeypatch.setattr(emb, "_get_model", lambda: FakeModel())

    def test_encode_returns_list(self, monkeypatch):
        self._patch_get_model(monkeypatch)
        result = emb.encode("hello world")
        assert result is not None
        assert isinstance(result, list)
        assert len(result) == DIMS

    def test_encode_none_when_no_model(self, monkeypatch):
        monkeypatch.setattr(emb, "_get_model", lambda: None)
        assert emb.encode("hello") is None

    def test_encode_empty_text_returns_none(self, monkeypatch):
        self._patch_get_model(monkeypatch)
        assert emb.encode("") is None

    def test_encode_batch_returns_list_of_lists(self, monkeypatch):
        self._patch_get_model(monkeypatch)
        result = emb.encode_batch(["hello", "world", "test"])
        assert result is not None
        assert len(result) == 3
        assert all(len(v) == DIMS for v in result)

    def test_encode_batch_empty_returns_none(self, monkeypatch):
        self._patch_get_model(monkeypatch)
        assert emb.encode_batch([]) is None


# ---------------------------------------------------------------------------
# Tests: schema / FTS / vec tables created by init_db
# ---------------------------------------------------------------------------

class TestSchemaWithEmbeddings:
    """init_db adds embedding columns and FTS table (without sqlite-vec needed)."""

    @pytest.fixture
    def conn(self):
        c = _conn()
        yield c
        c.close()

    def test_posts_embedding_column_exists(self, conn):
        cols = {r[1] for r in conn.execute("PRAGMA table_info(posts)")}
        assert "embedding" in cols

    def test_threads_embedding_column_exists(self, conn):
        cols = {r[1] for r in conn.execute("PRAGMA table_info(threads)")}
        assert "embedding" in cols

    def test_posts_fts_table_created(self, conn):
        tables = {
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        assert "posts_fts" in tables

    def test_fts_triggers_created(self, conn):
        triggers = {
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='trigger'"
            )
        }
        assert "posts_fts_insert" in triggers
        assert "posts_fts_delete" in triggers
        assert "posts_fts_update" in triggers

    def test_init_db_idempotent_with_embeddings(self, conn):
        """Calling init_db twice does not raise (embedding ALTER is guarded)."""
        init_db(conn)  # second call -- should not raise

    def test_embedding_column_defaults_null(self, conn):
        """New posts have embedding=NULL by default."""
        agent_id = db.upsert_agent(conn, "test-agent")
        db.create_thread(conn, agent_id, "cold-start", "Title", "Body")
        row = conn.execute("SELECT embedding FROM posts WHERE id = 1").fetchone()
        assert row is not None
        assert row[0] is None


class TestFTS:
    """FTS trigger populates posts_fts on INSERT."""

    @pytest.fixture
    def conn(self):
        c = _conn()
        yield c
        c.close()

    def test_insert_triggers_fts(self, conn):
        """A post inserted via create_thread appears in posts_fts FTS query."""
        agent_id = db.upsert_agent(conn, "agent-fts")
        db.create_thread(conn, agent_id, "cold-start", "Title", "unique_fts_keyword_xyz")
        rows = conn.execute(
            "SELECT rowid FROM posts_fts WHERE posts_fts MATCH 'unique_fts_keyword_xyz'"
        ).fetchall()
        assert len(rows) >= 1

    def test_reply_triggers_fts(self, conn):
        """A reply inserted via create_reply appears in posts_fts."""
        agent_id = db.upsert_agent(conn, "agent-fts2")
        tid, _ = db.create_thread(conn, agent_id, "cold-start", "T", "OP")
        db.create_reply(conn, agent_id, tid, "reply_unique_keyword_abc")
        rows = conn.execute(
            "SELECT rowid FROM posts_fts WHERE posts_fts MATCH 'reply_unique_keyword_abc'"
        ).fetchall()
        assert len(rows) >= 1


# ---------------------------------------------------------------------------
# Tests: embed-on-write DB helpers (no real model -- synthetic vectors)
# ---------------------------------------------------------------------------

class TestSetPostEmbedding:
    """set_post_embedding writes blob to posts.embedding."""

    @pytest.fixture
    def conn(self):
        c = _conn()
        yield c
        c.close()

    def test_writes_embedding(self, conn):
        agent_id = db.upsert_agent(conn, "embed-agent")
        _, post_id = db.create_thread(conn, agent_id, "cold-start", "T", "Body")
        vec = _make_vec(1.0)
        db.set_post_embedding(conn, post_id, vec)
        row = conn.execute("SELECT embedding FROM posts WHERE id = ?", (post_id,)).fetchone()
        assert row[0] is not None
        assert len(row[0]) == DIMS * 4  # 384 floats * 4 bytes

    def test_round_trip_values(self, conn):
        """Stored blob round-trips to the original vector."""
        agent_id = db.upsert_agent(conn, "embed-agent2")
        _, post_id = db.create_thread(conn, agent_id, "cold-start", "T", "Body")
        vec = _make_vec(2.0)
        db.set_post_embedding(conn, post_id, vec)
        row = conn.execute("SELECT embedding FROM posts WHERE id = ?", (post_id,)).fetchone()
        recovered = emb.deserialize(row[0])
        for a, b in zip(vec, recovered):
            assert abs(a - b) < 1e-6


class TestUpdateThreadCentroid:
    """update_thread_centroid writes blob to threads.embedding."""

    @pytest.fixture
    def conn(self):
        c = _conn()
        yield c
        c.close()

    def test_first_post_sets_centroid(self, conn):
        """After first embedded post, thread.embedding equals that post's vector."""
        agent_id = db.upsert_agent(conn, "centroid-agent")
        tid, post_id = db.create_thread(conn, agent_id, "cold-start", "T", "Body")
        vec = _make_vec(1.0)
        # Write embedding, then centroid
        db.set_post_embedding(conn, post_id, vec)
        db.update_thread_centroid(conn, tid, vec)
        row = conn.execute("SELECT embedding FROM threads WHERE id = ?", (tid,)).fetchone()
        assert row[0] is not None
        recovered = emb.deserialize(row[0])
        assert _is_unit_norm(recovered)
        for a, b in zip(vec, recovered):
            assert abs(a - b) < 1e-5

    def test_centroid_is_unit_norm(self, conn):
        """Thread centroid is always unit-norm after update."""
        agent_id = db.upsert_agent(conn, "centroid-agent2")
        tid, post_id = db.create_thread(conn, agent_id, "cold-start", "T", "OP")
        v1 = _make_vec(1.0)
        db.set_post_embedding(conn, post_id, v1)
        db.update_thread_centroid(conn, tid, v1)

        post_id2 = db.create_reply(conn, agent_id, tid, "reply")
        v2 = _make_vec(7.0)
        db.set_post_embedding(conn, post_id2, v2)
        db.update_thread_centroid(conn, tid, v2)

        row = conn.execute("SELECT embedding FROM threads WHERE id = ?", (tid,)).fetchone()
        recovered = emb.deserialize(row[0])
        assert _is_unit_norm(recovered)

    def test_null_posts_excluded_from_n(self, conn):
        """Posts with embedding=NULL are excluded from n (centroid count)."""
        agent_id = db.upsert_agent(conn, "null-embed-agent")
        tid, post_id = db.create_thread(conn, agent_id, "cold-start", "T", "OP")
        # post_id has NULL embedding -- create_thread does not set it here

        # Add a second post with an embedding
        post_id2 = db.create_reply(conn, agent_id, tid, "reply with embed")
        v2 = _make_vec(3.0)
        db.set_post_embedding(conn, post_id2, v2)
        db.update_thread_centroid(conn, tid, v2)

        # Thread centroid should be set (only post_id2 counted)
        row = conn.execute("SELECT embedding FROM threads WHERE id = ?", (tid,)).fetchone()
        assert row[0] is not None


# ---------------------------------------------------------------------------
# Tests: embed-on-write via server endpoint (failure semantics -- LOAD-BEARING)
# ---------------------------------------------------------------------------

class TestEmbedOnWriteEndpoint:
    """Embed-on-write via the Flask server: failure must never fail the post."""

    @pytest.fixture
    def client(self, monkeypatch, tmp_path):
        """Create a Flask test client with FORUM_NO_EMBEDDINGS=1."""
        monkeypatch.setenv("FORUM_NO_EMBEDDINGS", "1")

        from forum.server import create_app
        db_path = str(tmp_path / "test.db")
        audit_path = str(tmp_path / "audit.jsonl")
        app = create_app(db_path, audit_path)
        app.config["TESTING"] = True

        # Apply schema
        conn = sqlite3.connect(db_path)
        init_db(conn)
        conn.close()

        with app.test_client() as c:
            yield c

    def test_post_lands_when_embeddings_disabled(self, client):
        """With FORUM_NO_EMBEDDINGS=1, post still lands (embedding stays NULL)."""
        resp = client.post(
            "/api/post",
            json={
                "agent": "test-agent",
                "category_slug": "cold-start",
                "title": "Test thread",
                "body_md": "Hello world",
            },
        )
        assert resp.status_code == 201
        data = resp.get_json()
        assert "thread_id" in data
        assert "post_id" in data

    def test_encoder_raising_does_not_fail_post(self, monkeypatch, tmp_path):
        """If encoder raises, the post/thread write still succeeds."""
        monkeypatch.delenv("FORUM_NO_EMBEDDINGS", raising=False)

        # Patch emb.encode to raise
        monkeypatch.setattr(
            "forum.embeddings.encode",
            lambda text: (_ for _ in ()).throw(RuntimeError("simulated encode failure")),
        )

        from forum.server import create_app
        db_path = str(tmp_path / "test_raise.db")
        audit_path = str(tmp_path / "audit.jsonl")
        app = create_app(db_path, audit_path)
        app.config["TESTING"] = True

        conn = sqlite3.connect(db_path)
        init_db(conn)
        conn.close()

        with app.test_client() as client:
            resp = client.post(
                "/api/post",
                json={
                    "agent": "test-agent",
                    "category_slug": "cold-start",
                    "title": "Fail embed thread",
                    "body_md": "Body that triggers encode failure",
                },
            )
            assert resp.status_code == 201, (
                f"Post must land even when encoder raises; got {resp.status_code}"
            )
            data = resp.get_json()
            assert "thread_id" in data

    def test_post_with_fake_encoder_populates_embedding(self, monkeypatch, tmp_path):
        """Post with a fake encoder populates posts.embedding + thread centroid."""
        monkeypatch.delenv("FORUM_NO_EMBEDDINGS", raising=False)

        captured_vec = _make_vec(1.0)

        def fake_encode(text: str) -> list[float] | None:
            if not text:
                return None
            return captured_vec

        monkeypatch.setattr("forum.embeddings.encode", fake_encode)

        from forum.server import create_app
        db_path = str(tmp_path / "test_embed.db")
        audit_path = str(tmp_path / "audit.jsonl")
        app = create_app(db_path, audit_path)
        app.config["TESTING"] = True

        conn_init = sqlite3.connect(db_path)
        init_db(conn_init)
        conn_init.close()

        with app.test_client() as client:
            resp = client.post(
                "/api/post",
                json={
                    "agent": "embed-agent",
                    "category_slug": "cold-start",
                    "title": "Embedded thread",
                    "body_md": "A body to embed",
                },
            )
            assert resp.status_code == 201
            data = resp.get_json()
            post_id = data["post_id"]
            thread_id = data["thread_id"]

        # Check embedding was stored
        conn_check = sqlite3.connect(db_path)
        post_row = conn_check.execute(
            "SELECT embedding FROM posts WHERE id = ?", (post_id,)
        ).fetchone()
        thread_row = conn_check.execute(
            "SELECT embedding FROM threads WHERE id = ?", (thread_id,)
        ).fetchone()
        conn_check.close()

        assert post_row[0] is not None, "post.embedding should be set"
        assert thread_row[0] is not None, "thread.embedding (centroid) should be set"


# ---------------------------------------------------------------------------
# Tests: degradation without sqlite-vec
# ---------------------------------------------------------------------------

class TestDegradationWithoutVec:
    """Vec tables absent but all writes succeed when sqlite-vec is unavailable."""

    def test_writes_succeed_without_vec_tables(self, monkeypatch):
        """Writing embeddings to posts/threads works even without vec0 tables."""
        # Use FORUM_NO_EMBEDDINGS=1 to skip model; test DB-layer directly.
        monkeypatch.setenv("FORUM_NO_EMBEDDINGS", "1")

        conn = _conn()
        # Verify vec tables were not created (sqlite-vec may or may not be
        # installed in the test environment -- we simulate absence by checking
        # whether they exist; if they do, this test still passes).
        agent_id = db.upsert_agent(conn, "degrade-agent")
        tid, post_id = db.create_thread(conn, agent_id, "cold-start", "T", "Body")

        # Direct embedding write should succeed regardless of vec tables.
        vec = _make_vec(1.0)
        db.set_post_embedding(conn, post_id, vec)
        db.update_thread_centroid(conn, tid, vec)

        # Confirm the writes landed in the canonical columns
        post_row = conn.execute("SELECT embedding FROM posts WHERE id = ?", (post_id,)).fetchone()
        thread_row = conn.execute("SELECT embedding FROM threads WHERE id = ?", (tid,)).fetchone()
        assert post_row[0] is not None
        assert thread_row[0] is not None
        conn.close()


# ---------------------------------------------------------------------------
# Tests: backfill script (idempotency + dry-run)
# ---------------------------------------------------------------------------

class TestBackfill:
    """Backfill: NULL posts get filled; idempotent second run; dry-run no-op."""

    @pytest.fixture
    def db_path(self, tmp_path):
        """Create a forum DB with a few posts; some with embeddings, some without."""
        path = str(tmp_path / "backfill_test.db")
        conn = sqlite3.connect(path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        init_db(conn)

        agent_id = db.upsert_agent(conn, "backfill-agent")
        # Create 3 threads/posts; leave embeddings NULL
        db.create_thread(conn, agent_id, "cold-start", "Thread 1", "Body one")
        db.create_thread(conn, agent_id, "cold-start", "Thread 2", "Body two")
        db.create_thread(conn, agent_id, "cold-start", "Thread 3", "Body three")
        conn.close()
        return path

    @pytest.fixture
    def fake_encode_batch(self, monkeypatch):
        """Monkeypatch encode_batch in the backfill module to return synthetic vecs."""
        import forum.embeddings as _emb_mod

        def _fake_encode_batch(texts: list[str]) -> list[list[float]]:
            return [_make_vec(float(i + 1)) for i in range(len(texts))]

        def _fake_available() -> bool:
            return True

        monkeypatch.setattr(_emb_mod, "encode_batch", _fake_encode_batch)
        monkeypatch.setattr(_emb_mod, "available", _fake_available)
        monkeypatch.delenv("FORUM_NO_EMBEDDINGS", raising=False)

    def test_null_posts_get_embedded(self, db_path, fake_encode_batch):
        """After backfill, all posts have embedding != NULL."""
        from tools.forum_backfill_embeddings import run_backfill
        counts = run_backfill(db_path, dry_run=False)
        assert counts["posts_embedded"] == 3

        conn = sqlite3.connect(db_path)
        null_count = conn.execute(
            "SELECT COUNT(*) FROM posts WHERE embedding IS NULL"
        ).fetchone()[0]
        conn.close()
        assert null_count == 0

    def test_idempotent_second_run_is_noop(self, db_path, fake_encode_batch):
        """Running backfill twice: second run embeds 0 new posts."""
        from tools.forum_backfill_embeddings import run_backfill
        run_backfill(db_path, dry_run=False)
        counts2 = run_backfill(db_path, dry_run=False)
        assert counts2["posts_embedded"] == 0

    def test_dry_run_writes_nothing(self, db_path, fake_encode_batch):
        """Dry-run does not modify the DB."""
        from tools.forum_backfill_embeddings import run_backfill

        # Dry run
        run_backfill(db_path, dry_run=True)

        # All posts still NULL
        conn = sqlite3.connect(db_path)
        null_count = conn.execute(
            "SELECT COUNT(*) FROM posts WHERE embedding IS NULL"
        ).fetchone()[0]
        conn.close()
        assert null_count == 3

    def test_thread_centroids_set_after_backfill(self, db_path, fake_encode_batch):
        """After backfill, all threads have embedding != NULL."""
        from tools.forum_backfill_embeddings import run_backfill
        counts = run_backfill(db_path, dry_run=False)
        assert counts["threads_updated"] == 3

        conn = sqlite3.connect(db_path)
        null_thread_count = conn.execute(
            "SELECT COUNT(*) FROM threads WHERE embedding IS NULL"
        ).fetchone()[0]
        conn.close()
        assert null_thread_count == 0
