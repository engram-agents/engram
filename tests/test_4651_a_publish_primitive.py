"""Contract cells for the #4651 part A shared strict embedding-publication primitive.

Written BEFORE the implementation (contract-first). A controlled encoder is used
and no model is fetched. Real sqlite-vec must RUN: a missing vec store is a hard
failure here, never a skip.
"""
from __future__ import annotations

import dataclasses
import hashlib
import importlib
import json
import math
import os
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

TEST_DIR = Path(tempfile.mkdtemp(prefix="engram_test_4651_a_publish_"))

GOOD = [0.5 + i / 1000 for i in range(384)]
OTHER = [2.5 + i / 1000 for i in range(384)]
OLD = [7.0 + i / 1000 for i in range(384)]
MARKER = "embedding_refresh_pending"


def fresh_server():
    if TEST_DIR.exists():
        shutil.rmtree(TEST_DIR)
    for key in list(sys.modules):
        if key == "server" or key.startswith("engram_"):
            del sys.modules[key]
    os.environ["ENGRAM_NO_EMBEDDINGS"] = "1"
    os.environ["ENGRAM_NO_POLARITY"] = "1"
    import server
    server._configure_paths(TEST_DIR)
    server._ensure_data_dir()
    return server


def add_obs(srv, claim: str) -> str:
    result = json.loads(srv.engram_add_observation(payload_json=json.dumps({
        "quoted_text": "test quote", "interpretation": "test interpretation",
        "claim": claim, "quote_type": "personal_communication",
        "url": "https://example.com/test", "title": "Test",
    })))
    assert "error" not in result, result
    return result["observation_id"]


class FakeEmbedder:
    def __init__(self, available=True, vector=None):
        self.available = available
        self.vector = vector
        self.texts: list[str] = []

    def is_available(self):
        return self.available

    def embed(self, text, _model):
        self.texts.append(text)
        if self.vector is not None:
            return list(self.vector)
        value = float(len(text))
        return [value + i / 1000 for i in range(384)]


class ExplodingEmbedder:
    def is_available(self):
        raise AssertionError("prepared mode must not probe the embedder")

    def embed(self, *_a, **_k):
        raise AssertionError("prepared mode must not encode")


class Ctx:
    """One fresh server + the publish module + helpers."""

    def __init__(self, monkeypatch):
        self.srv = fresh_server()
        self.core = self.srv._recall_summaries_mod.core
        self.pub = importlib.import_module("engram_embedding_publish")
        self.mp = monkeypatch
        conn = self.srv._get_db()
        try:
            has_vec = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE name = 'vec_nodes'"
            ).fetchone() is not None
        finally:
            conn.close()
        assert self.core._VEC_BACKEND_AVAILABLE and self.core._sqlite_vec is not None, (
            "REAL sqlite-vec is required; these cells must run, not skip")
        assert has_vec, "REAL vec_nodes store is required in the fixture"

    # -- db helpers
    def db(self):
        return self.srv._get_db()

    def fvec(self, vector):
        return self.core._sqlite_vec.serialize_float32(vector)

    def node(self, claim="a claim", *, summary=None, keywords=None, metadata=None,
             seed=None):
        nid = add_obs(self.srv, claim)
        conn = self.db()
        try:
            if summary is not None or keywords is not None:
                conn.execute(
                    "UPDATE nodes SET recall_summary = ?, recall_keywords = ? WHERE id = ?",
                    (summary, None if keywords is None else json.dumps(keywords), nid))
            if metadata is not None:
                try:
                    json.loads(metadata)
                except ValueError:
                    # A legacy damaged row: the write guard (#3425) refuses new
                    # invalid JSON, so the fixture lifts it for this one test DB.
                    conn.execute("DROP TRIGGER IF EXISTS nodes_metadata_json_guard_update")
                conn.execute("UPDATE nodes SET metadata = ? WHERE id = ?", (metadata, nid))
            if seed is not None:
                conn.execute("DELETE FROM vec_nodes WHERE node_id = ?", (nid,))
                conn.execute("INSERT INTO vec_nodes VALUES (?, ?)", (nid, self.fvec(seed)))
                conn.execute("UPDATE nodes SET embedding = ? WHERE id = ?",
                             (json.dumps(seed), nid))
            conn.commit()
        finally:
            conn.close()
        return nid

    def sql(self, statement, params=()):
        conn = self.db()
        try:
            conn.execute(statement, params)
            conn.commit()
        finally:
            conn.close()

    def snap(self, nid):
        conn = self.db()
        try:
            row = conn.execute(
                "SELECT embedding, metadata, recall_summary, is_current FROM nodes WHERE id = ?",
                (nid,)).fetchone()
            vec = conn.execute(
                "SELECT embedding FROM vec_nodes WHERE node_id = ?", (nid,)).fetchone()
            return (None if row is None else tuple(row), None if vec is None else bytes(vec[0]))
        finally:
            conn.close()

    # -- prepared helpers
    def prep(self, nid, vector=GOOD, **override):
        conn = self.db()
        try:
            text, digest = self.pub.current_recipe(conn, nid)
        finally:
            conn.close()
        fields = dict(vector=vector, text_sha256=digest, epoch=self.pub.current_epoch())
        fields.update(override)
        return self.pub.PreparedVector(**fields)

    def publish(self, nid, prepared=None):
        conn = self.db()
        try:
            result = self.pub.publish_embedding(conn, nid, prepared=prepared)
            conn.commit()
            return result
        finally:
            conn.close()

    def install_encoder(self, available=True, vector=None):
        fake = FakeEmbedder(available, vector)
        self.mp.setattr(self.core, "_embedder", fake)
        return fake

    def marker(self, nid):
        raw = self.snap(nid)[0][1]
        return None if raw is None else json.loads(raw)


@pytest.fixture
def ctx(monkeypatch):
    return Ctx(monkeypatch)


def assert_deferred(result, nid, reason):
    assert result.node_id == nid
    assert result.status == "deferred", result
    assert result.reason == reason, result
    assert result.vector_changed is None
    assert result.debt_cleared is False


# --------------------------------------------------------------------- C1
def test_c1_prepared_never_encodes(ctx):
    nid = ctx.node("c1 claim")
    ctx.mp.setattr(ctx.core, "_embedder", ExplodingEmbedder())

    def boom(*_a, **_k):
        raise AssertionError("prepared mode must not call the compute path")

    ctx.mp.setattr(ctx.core, "_compute_and_store_embedding", boom)
    result = ctx.publish(nid, ctx.prep(nid))
    assert result.status == "refreshed", result


# --------------------------------------------------------------------- C2
def test_c2_not_current_and_missing_defer_without_writes(ctx):
    nid = ctx.node("c2 claim", seed=OLD, metadata=json.dumps({"keep": 1}))
    prepared = ctx.prep(nid)
    ctx.sql("UPDATE nodes SET is_current = 0 WHERE id = ?", (nid,))
    before = ctx.snap(nid)
    assert_deferred(ctx.publish(nid, prepared), nid, "not_current")
    assert ctx.snap(nid) == before
    missing = ctx.publish("nd_missing_4651", prepared)
    assert_deferred(missing, "nd_missing_4651", "not_current")
    assert ctx.snap("nd_missing_4651") == (None, None)


# --------------------------------------------------------------------- C3
def test_c3_empty_text_defers_and_empty_hash_never_matches(ctx):
    nid = ctx.node("c3 claim", seed=OLD)
    ctx.sql("UPDATE nodes SET claim = '', recall_summary = NULL, recall_keywords = NULL "
            "WHERE id = ?", (nid,))
    assert ctx.core._embedding_text_for_node(ctx.db(), nid) == ""
    before = ctx.snap(nid)
    wrong = ctx.pub.PreparedVector(GOOD, "0" * 64, ctx.pub.current_epoch())
    assert_deferred(ctx.publish(nid, wrong), nid, "empty_text")
    control = ctx.pub.PreparedVector(
        GOOD, ctx.pub.text_sha256(""), ctx.pub.current_epoch())
    assert_deferred(ctx.publish(nid, control), nid, "empty_text")
    assert ctx.snap(nid) == before


# --------------------------------------------------------------------- C4
def test_c4_recipe_changed_defers_and_unchanged_refreshes(ctx):
    nid = ctx.node("c4 claim", summary="first", keywords=["a", "b", "c"], seed=OLD,
                   metadata=json.dumps({MARKER: {"reason": "x"}}))
    prepared = ctx.prep(nid)
    ctx.sql("UPDATE nodes SET recall_summary = 'second' WHERE id = ?", (nid,))
    before = ctx.snap(nid)
    assert_deferred(ctx.publish(nid, prepared), nid, "recipe_changed")
    assert ctx.snap(nid) == before
    assert MARKER in ctx.marker(nid)
    control = ctx.publish(nid, ctx.prep(nid))
    assert control.status == "refreshed", control


# --------------------------------------------------------------------- C5
@pytest.mark.parametrize("field", ["model", "dimension", "normalisation"])
def test_c5_epoch_changed_each_field_alone(ctx, field):
    nid = ctx.node("c5 claim", seed=OLD, metadata=json.dumps({MARKER: {"reason": "x"}}))
    epoch = ctx.pub.current_epoch()
    changed = {"model": "other-model", "dimension": 768, "normalisation": "unit"}[field]
    prepared = ctx.prep(nid, epoch=dataclasses.replace(epoch, **{field: changed}))
    before = ctx.snap(nid)
    assert_deferred(ctx.publish(nid, prepared), nid, "epoch_changed")
    assert ctx.snap(nid) == before


# --------------------------------------------------------------------- C6
def _bad_vectors():
    return {
        "short": [0.1] * 383,
        "long": [0.1] * 385,
        "nan": [0.1] * 383 + [float("nan")],
        "inf": [0.1] * 383 + [float("inf")],
        "neg_inf": [0.1] * 383 + [float("-inf")],
        "bool": [0.1] * 383 + [True],
        "not_a_list": "x" * 384,
        "tuple": tuple([0.1] * 384),
        "strings": ["0.1"] * 384,
        "none": None,
    }


@pytest.mark.parametrize("kind", sorted(_bad_vectors()))
def test_c6_invalid_vector_defers_without_writes(ctx, kind):
    nid = ctx.node("c6 claim", seed=OLD, metadata=json.dumps({MARKER: {"reason": "x"}}))
    prepared = ctx.prep(nid, vector=_bad_vectors()[kind])
    before = ctx.snap(nid)
    assert_deferred(ctx.publish(nid, prepared), nid, "invalid_vector")
    assert ctx.snap(nid) == before


# --------------------------------------------------------------------- C7
def test_c7_order_not_current_before_invalid_vector(ctx):
    nid = ctx.node("c7a")
    prepared = ctx.prep(nid, vector=[0.1, 0.2])
    ctx.sql("UPDATE nodes SET is_current = 0 WHERE id = ?", (nid,))
    assert_deferred(ctx.publish(nid, prepared), nid, "not_current")


def test_c7_order_empty_text_before_recipe_changed(ctx):
    nid = ctx.node("c7b")
    ctx.sql("UPDATE nodes SET claim = '' WHERE id = ?", (nid,))
    prepared = ctx.pub.PreparedVector(GOOD, "f" * 64, ctx.pub.current_epoch())
    assert_deferred(ctx.publish(nid, prepared), nid, "empty_text")


def test_c7_order_recipe_changed_before_epoch_changed(ctx):
    nid = ctx.node("c7c", summary="one", keywords=["a", "b", "c"])
    epoch = dataclasses.replace(ctx.pub.current_epoch(), model="other-model")
    prepared = ctx.prep(nid, epoch=epoch)
    ctx.sql("UPDATE nodes SET recall_summary = 'two' WHERE id = ?", (nid,))
    assert_deferred(ctx.publish(nid, prepared), nid, "recipe_changed")


def test_c7_order_epoch_changed_before_invalid_vector(ctx):
    nid = ctx.node("c7d")
    epoch = dataclasses.replace(ctx.pub.current_epoch(), model="other-model")
    assert_deferred(ctx.publish(nid, ctx.prep(nid, vector=[1.0], epoch=epoch)),
                    nid, "epoch_changed")


def test_c7_order_invalid_vector_before_malformed_metadata(ctx):
    nid = ctx.node("c7e", metadata="[1, 2]")
    assert_deferred(ctx.publish(nid, ctx.prep(nid, vector=[1.0])), nid, "invalid_vector")


# --------------------------------------------------------------------- C8
@pytest.mark.parametrize("raw", ["[1, 2]", '"x"', "5", "{not json"])
def test_c8_malformed_metadata_defers_without_writes(ctx, raw):
    nid = ctx.node("c8 claim", seed=OLD, metadata=raw)
    before = ctx.snap(nid)
    assert_deferred(ctx.publish(nid, ctx.prep(nid)), nid, "malformed_metadata")
    assert ctx.snap(nid) == before


def _reject_metadata_updates(ctx, nid):
    ctx.sql(
        "CREATE TRIGGER reject_meta BEFORE UPDATE OF metadata ON nodes "
        "WHEN NEW.id = '%s' BEGIN SELECT RAISE(FAIL, 'metadata write'); END" % nid)


def test_c8_sql_null_metadata_refreshes_and_stays_null(ctx):
    nid = ctx.node("c8 null claim")
    ctx.sql("UPDATE nodes SET metadata = NULL WHERE id = ?", (nid,))
    _reject_metadata_updates(ctx, nid)
    result = ctx.publish(nid, ctx.prep(nid))
    assert result.status == "refreshed", result
    assert ctx.snap(nid)[0][1] is None


def test_c8_raw_object_metadata_keeps_exact_bytes(ctx):
    raw = '{"z":1,"a":{"nested":[3,2]}}'
    nid = ctx.node("c8 raw claim", metadata=raw)
    _reject_metadata_updates(ctx, nid)
    result = ctx.publish(nid, ctx.prep(nid))
    assert result.status == "refreshed", result
    assert ctx.snap(nid)[0][1] == raw


# --------------------------------------------------------------------- C9
def test_c9_success_writes_both_stores(ctx):
    nid = ctx.node("c9 claim")
    result = ctx.publish(nid, ctx.prep(nid, vector=GOOD))
    assert result.status == "refreshed" and result.reason is None, result
    snap = ctx.snap(nid)
    assert snap[0][0] == json.dumps(GOOD)
    assert snap[1] == ctx.fvec(GOOD)


# -------------------------------------------------------------------- C10
def test_c10_prepared_and_compute_paths_leave_identical_bytes(ctx):
    a = ctx.node("c10 same text")
    b = ctx.node("c10 same text")
    ctx.install_encoder(vector=GOOD)
    assert ctx.publish(a, ctx.prep(a, vector=GOOD)).status == "refreshed"
    assert ctx.publish(b).status == "refreshed"
    sa, sb = ctx.snap(a), ctx.snap(b)
    assert sa[0][0] == sb[0][0] == json.dumps(GOOD)
    assert sa[1] == sb[1] == ctx.fvec(GOOD)


# -------------------------------------------------------------------- C11
def _fake_store(ctx, mode):
    """A store step that leaves the stores in a deliberately wrong state while
    reporting success, so only the primitive's own verification can catch it."""
    real = ctx.core._store_embedding_vector

    def store(conn, node_id, vector):
        if mode == "mirror_absent":
            conn.execute("UPDATE nodes SET embedding = ? WHERE id = ?",
                         (json.dumps(vector), node_id))
            conn.execute("DELETE FROM vec_nodes WHERE node_id = ?", (node_id,))
        elif mode == "mirror_other_bytes":
            conn.execute("UPDATE nodes SET embedding = ? WHERE id = ?",
                         (json.dumps(vector), node_id))
            conn.execute("DELETE FROM vec_nodes WHERE node_id = ?", (node_id,))
            conn.execute("INSERT INTO vec_nodes VALUES (?, ?)",
                         (node_id, ctx.fvec(OTHER)))
        elif mode == "canonical_mismatch":
            conn.execute("UPDATE nodes SET embedding = ? WHERE id = ?",
                         (json.dumps(OTHER), node_id))
            conn.execute("DELETE FROM vec_nodes WHERE node_id = ?", (node_id,))
            conn.execute("INSERT INTO vec_nodes VALUES (?, ?)",
                         (node_id, ctx.fvec(vector)))
        else:  # pragma: no cover
            raise AssertionError(mode)
        return ctx.core.EmbeddingOutcome(status="embedded", node_id=node_id,
                                         vec_mirror_ok=True)

    ctx.mp.setattr(ctx.core, "_store_embedding_vector", store)
    return real


@pytest.mark.parametrize("mode,reason", [
    ("mirror_absent", "mirror"),
    ("mirror_other_bytes", "mirror"),
    ("canonical_mismatch", "canonical"),
])
def test_c11_verifier_catches_a_lying_store(ctx, mode, reason):
    nid = ctx.node("c11 claim", seed=OLD, metadata=json.dumps({"keep": "k"}))
    prepared = ctx.prep(nid)
    before = ctx.snap(nid)
    _fake_store(ctx, mode)
    result = ctx.publish(nid, prepared)
    assert result.status == "pending" and result.reason == reason, result
    assert result.vector_changed is None
    after = ctx.snap(nid)
    assert after[0][0] == before[0][0] and after[1] == before[1]
    meta = ctx.marker(nid)
    assert meta["keep"] == "k" and meta[MARKER]["reason"] == reason


def test_c11_real_insert_after_delete_failure_restores_both_stores(ctx):
    nid = ctx.node("c11 real", seed=OLD, metadata=json.dumps({"keep": "k"}))
    prepared = ctx.prep(nid)
    before = ctx.snap(nid)
    conn = ctx.db()
    conn.set_authorizer(lambda action, arg1, _a2, _db, _src: (
        sqlite3.SQLITE_DENY if action == sqlite3.SQLITE_INSERT and arg1 == "vec_nodes"
        else sqlite3.SQLITE_OK))
    try:
        result = ctx.pub.publish_embedding(conn, nid, prepared=prepared)
        conn.commit()
    finally:
        conn.close()
    assert result.status == "pending" and result.reason == "mirror", result
    after = ctx.snap(nid)
    assert after[0][0] == before[0][0]
    assert after[1] == before[1] == ctx.fvec(OLD)
    assert ctx.marker(nid)[MARKER]["reason"] == "mirror"


# -------------------------------------------------------------------- C12
def test_c12_per_node_isolation_on_one_connection(ctx):
    a = ctx.node("c12 a", seed=OLD)
    b = ctx.node("c12 b", seed=OLD, metadata=json.dumps({"keep": "kb"}))
    c = ctx.node("c12 c", seed=OLD)
    pa, pb, pc = ctx.prep(a), ctx.prep(b), ctx.prep(c)
    before_b = ctx.snap(b)
    real = ctx.core._store_embedding_vector

    def store(conn, node_id, vector):
        if node_id == b:
            conn.execute("UPDATE nodes SET embedding = ? WHERE id = ?",
                         (json.dumps(OTHER), node_id))
            return ctx.core.EmbeddingOutcome(status="failed", node_id=node_id,
                                             detail="boom", stage="store")
        return real(conn, node_id, vector)

    ctx.mp.setattr(ctx.core, "_store_embedding_vector", store)
    conn = ctx.db()
    try:
        ra = ctx.pub.publish_embedding(conn, a, prepared=pa)
        rb = ctx.pub.publish_embedding(conn, b, prepared=pb)
        rc = ctx.pub.publish_embedding(conn, c, prepared=pc)
        conn.commit()
    finally:
        conn.close()
    assert (ra.status, rc.status) == ("refreshed", "refreshed")
    assert rb.status == "pending" and rb.reason == "store", rb
    assert ctx.snap(a)[0][0] == json.dumps(GOOD) and ctx.snap(a)[1] == ctx.fvec(GOOD)
    assert ctx.snap(c)[0][0] == json.dumps(GOOD) and ctx.snap(c)[1] == ctx.fvec(GOOD)
    after_b = ctx.snap(b)
    assert after_b[0][0] == before_b[0][0] and after_b[1] == before_b[1]
    meta = ctx.marker(b)
    assert meta["keep"] == "kb" and meta[MARKER]["reason"] == "store"


# -------------------------------------------------------------------- C13
@pytest.mark.parametrize("value", [{"reason": "x", "at": "t"}, None, False, 0])
def test_c13_present_marker_removed_after_verified_success(ctx, value):
    nid = ctx.node("c13 claim", metadata=json.dumps(
        {"keep": {"n": [1, 0]}, "zero": 0, MARKER: value}))
    result = ctx.publish(nid, ctx.prep(nid))
    assert result.status == "refreshed" and result.debt_cleared is True, result
    assert ctx.marker(nid) == {"keep": {"n": [1, 0]}, "zero": 0}


def test_c13_no_marker_success_does_not_claim_debt_cleared(ctx):
    nid = ctx.node("c13 none", metadata=json.dumps({"keep": 1}))
    result = ctx.publish(nid, ctx.prep(nid))
    assert result.status == "refreshed" and result.debt_cleared is False, result


def test_c13_failure_sets_marker_and_keeps_unrelated_keys(ctx):
    nid = ctx.node("c13 fail", seed=OLD, metadata=json.dumps({"keep": {"n": [1, 0]}}))
    prepared = ctx.prep(nid)
    _fake_store(ctx, "mirror_absent")
    result = ctx.publish(nid, prepared)
    assert result.status == "pending" and result.debt_cleared is False, result
    meta = ctx.marker(nid)
    assert meta["keep"] == {"n": [1, 0]}
    assert meta[MARKER]["reason"] == "mirror" and meta[MARKER]["stage"] == "strict_refresh"


def test_c13_failure_never_clears_an_existing_marker(ctx):
    nid = ctx.node("c13 stay", seed=OLD, metadata=json.dumps({MARKER: {"reason": "old"}}))
    prepared = ctx.prep(nid)
    _fake_store(ctx, "mirror_absent")
    result = ctx.publish(nid, prepared)
    assert result.status == "pending" and result.debt_cleared is False, result
    assert MARKER in ctx.marker(nid)


# -------------------------------------------------------------------- C14
def test_c14_vector_changed_reports_byte_change(ctx):
    nid = ctx.node("c14 claim", seed=GOOD)
    same = ctx.publish(nid, ctx.prep(nid, vector=GOOD))
    assert same.status == "refreshed" and same.vector_changed is False, same
    differ = ctx.publish(nid, ctx.prep(nid, vector=OTHER))
    assert differ.status == "refreshed" and differ.vector_changed is True, differ


def test_c14_vector_changed_none_for_deferred_and_pending(ctx):
    nid = ctx.node("c14 other", seed=OLD)
    deferred = ctx.publish(nid, ctx.prep(nid, vector=[1.0]))
    assert deferred.status == "deferred" and deferred.vector_changed is None
    prepared = ctx.prep(nid)
    _fake_store(ctx, "mirror_absent")
    pending = ctx.publish(nid, prepared)
    assert pending.status == "pending" and pending.vector_changed is None


# -------------------------------------------------------------------- C15
def _sha(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def test_c15_recipe_helpers_agree_across_node_types(ctx):
    nodes = {
        "evidence": ctx.node("e claim"),
        "prediction": ctx.node("p claim"),
        "feeling": ctx.node("I reported feeling: calm"),
        "summary_only": ctx.node("s claim", summary="only a summary"),
        "keywords_only": ctx.node("k claim", keywords=["kw1", "kw2", "kw3"]),
        "non_ascii": ctx.node("café 北京", summary="résumé 东京"),
    }
    ctx.sql("UPDATE nodes SET type = 'evidence', source_title = 'Title é' WHERE id = ?",
            (nodes["evidence"],))
    ctx.sql("UPDATE nodes SET type = 'prediction', predicted_event = 'it rains' WHERE id = ?",
            (nodes["prediction"],))
    ctx.sql("UPDATE nodes SET type = 'feeling_report' WHERE id = ?", (nodes["feeling"],))
    conn = ctx.db()
    try:
        for label, nid in nodes.items():
            expected = ctx.core._embedding_text_for_node(conn, nid)
            assert expected, label
            text, digest = ctx.pub.current_recipe(conn, nid)
            assert text == expected, label
            assert digest == _sha(expected) == ctx.pub.text_sha256(expected), label
            claim = conn.execute("SELECT claim FROM nodes WHERE id = ?", (nid,)).fetchone()[0]
            if label != "non_ascii":
                assert digest != _sha(claim) or expected == claim, label
        assert ctx.pub.current_recipe(conn, nodes["feeling"])[0] == "calm"
        assert ctx.pub.current_recipe(conn, nodes["non_ascii"])[0].startswith("café 北京")
    finally:
        conn.close()
    assert ctx.pub.text_sha256("café 北京") == _sha("café 北京")
    assert ctx.pub.text_sha256("café 北京") != hashlib.sha256(
        "café 北京".encode("utf-16")).hexdigest()


# -------------------------------------------------------------------- C16
def test_c16_current_epoch(ctx):
    epoch = ctx.pub.current_epoch()
    assert (epoch.model, epoch.dimension, epoch.normalisation) == (
        ctx.core._get_embedding_config().get("model", ctx.core.DEFAULT_EMBEDDING_MODEL),
        384, "model-default")
    assert ctx.pub.EMBED_DIMENSION == 384 and ctx.pub.EMBED_NORMALISATION == "model-default"
    ctx.mp.setattr(ctx.core, "_get_embedding_config", lambda: {"model": "cfg-model"})
    assert ctx.pub.current_epoch().model == "cfg-model"
    ctx.mp.setattr(ctx.core, "_get_embedding_config", lambda: {})
    assert ctx.pub.current_epoch().model == ctx.core.DEFAULT_EMBEDDING_MODEL


# -------------------------------------------------------------------- C17
def test_c17_compute_mode_through_the_new_function_keeps_the_old_shape(ctx):
    nid = ctx.node("c17 claim", summary="s", keywords=["a", "b", "c"])
    ctx.install_encoder()
    conn = ctx.db()
    try:
        out = ctx.srv._recall_summaries_mod._strict_refresh_embedding(conn, nid, None)
        conn.commit()
    finally:
        conn.close()
    assert out == ({"embedding_refresh": "refreshed"}, None)
    direct = ctx.publish(nid)
    assert direct.status == "refreshed" and direct.reason is None, direct

    other = ctx.node("c17 unavailable", seed=OLD)
    ctx.install_encoder(available=False)
    conn = ctx.db()
    try:
        out = ctx.srv._recall_summaries_mod._strict_refresh_embedding(conn, other, None)
        conn.commit()
    finally:
        conn.close()
    assert out == ({"embedding_refresh": "pending",
                    "embedding_refresh_reason": "unavailable"}, None)
    assert ctx.snap(other)[0][0] == json.dumps(OLD)
    again = ctx.publish(other)
    assert again.status == "pending" and again.reason == "unavailable", again


def test_c17_compute_mode_does_not_apply_the_prepared_checks(ctx):
    # Compute mode is today's refresh: no is_current gate, no metadata gate.
    nid = ctx.node("c17 legacy", metadata="[1, 2]")
    ctx.install_encoder()
    result = ctx.publish(nid)
    assert result.status == "refreshed", result


# -------------------------------------------------------------------- C18
def test_c18_tier_entry_is_essential_like_recall_summaries():
    tiers = json.loads((PROJECT_ROOT / "src" / "build" / "packaging" / "tiers.json").read_text())

    def walk(node):
        if isinstance(node, dict):
            if "path" in node and "tier" in node:
                yield node["path"], node["tier"]
            for value in node.values():
                yield from walk(value)
        elif isinstance(node, list):
            for value in node:
                yield from walk(value)

    found = {}
    for path, tier in walk(tiers):
        found.setdefault(path, []).append(tier)
    assert found.get("engram_embedding_publish.py") == ["essential"]
    assert found.get("engram_recall_summaries.py") == ["essential"]
    assert (PROJECT_ROOT / "src" / "engram" / "engram_embedding_publish.py").is_file()


# ---- A1 delta cells (verifier survivors) ---------------------------------
# -------------------------------------------------------------------- D1
def test_d1_integer_valued_vector_is_a_valid_prepared_vector(ctx):
    for vector in ([1] * 384, [1, 2.5] * 192):
        nid = ctx.node("d1 claim", seed=OLD)
        result = ctx.publish(nid, ctx.prep(nid, vector=vector))
        assert result.status == "refreshed" and result.reason is None, result
        snap = ctx.snap(nid)
        assert snap[0][0] == json.dumps(vector)
        assert snap[0][0].startswith("[1, ")
        assert snap[1] == ctx.fvec(vector)


# -------------------------------------------------------------------- D2
def test_d2_vec_backend_flag_off_with_a_real_vec_table_is_pending_mirror(ctx):
    nid = ctx.node("d2 claim", seed=OLD, metadata=json.dumps({"keep": "k"}))
    prepared = ctx.prep(nid)
    before = ctx.snap(nid)
    ctx.mp.setattr(ctx.core, "_VEC_BACKEND_AVAILABLE", False)
    result = ctx.publish(nid, prepared)
    # Restore the flag before reading back: snap() opens a connection, and a
    # connection opened with the flag off cannot load the vec0 module.
    ctx.mp.setattr(ctx.core, "_VEC_BACKEND_AVAILABLE", True)
    assert result.status == "pending" and result.reason == "mirror", result
    assert result.vector_changed is None
    after = ctx.snap(nid)
    assert after[0][0] == before[0][0] and after[1] == before[1]
    meta = ctx.marker(nid)
    assert meta["keep"] == "k" and meta[MARKER]["reason"] == "mirror"


# -------------------------------------------------------------------- D3
def test_d3_a_raising_store_step_is_reported_not_propagated(ctx):
    nid = ctx.node("d3 claim", seed=OLD, metadata=json.dumps({"keep": "k"}))
    prepared = ctx.prep(nid)
    before = ctx.snap(nid)

    def store(_conn, _node_id, _vector):
        raise RuntimeError("boom")

    ctx.mp.setattr(ctx.core, "_store_embedding_vector", store)
    result = ctx.publish(nid, prepared)
    assert result.status == "pending" and result.reason == "embedding", result
    assert result.vector_changed is None
    after = ctx.snap(nid)
    assert after[0][0] == before[0][0] and after[1] == before[1]
    meta = ctx.marker(nid)
    assert meta["keep"] == "k" and meta[MARKER]["reason"] == "embedding"
