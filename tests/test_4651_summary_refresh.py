"""Contract tests for #4651 slice 1 strict summary-refresh embedding writes.

These tests deliberately use a controlled encoder; no model is fetched or loaded.
"""
from __future__ import annotations

import json
import os
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

TEST_DIR = Path(tempfile.mkdtemp(prefix="engram_test_4651_summary_refresh_"))


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


def entry(node_id: str, summary: str, keywords: list[str]) -> dict:
    return {"node_id": node_id, "recall_summary": summary, "recall_keywords": keywords}


class FakeEmbedder:
    def __init__(self, available=True):
        self.available = available
        self.texts: list[str] = []

    def is_available(self):
        return self.available

    def embed(self, text, _model):
        self.texts.append(text)
        value = float(len(text))
        return [value + i / 1000 for i in range(384)]


def install_fake_encoder(monkeypatch, srv, available=True):
    fake = FakeEmbedder(available)
    core = srv._recall_summaries_mod.core
    monkeypatch.setattr(core, "_embedder", fake)
    return fake, core


def node_row(srv, node_id):
    conn = srv._get_db()
    try:
        return conn.execute(
            "SELECT recall_summary, recall_keywords, embedding, metadata FROM nodes WHERE id = ?", (node_id,)
        ).fetchone()
    finally:
        conn.close()


def apply(srv, entries):
    return json.loads(srv.engram_set_recall_summaries(payload_json=json.dumps({"summaries": entries})))


def vec_row(srv, node_id):
    conn = srv._get_db()
    try:
        row = conn.execute("SELECT embedding FROM vec_nodes WHERE node_id = ?", (node_id,)).fetchone()
        return None if row is None else row[0]
    finally:
        conn.close()


def require_real_vec(srv, core):
    if not core._VEC_BACKEND_AVAILABLE or core._sqlite_vec is None:
        pytest.skip("REAL sqlite-vec unavailable in this interpreter")
    conn = srv._get_db()
    try:
        if conn.execute("SELECT 1 FROM sqlite_master WHERE name = 'vec_nodes'").fetchone() is None:
            pytest.skip("REAL sqlite-vec store unavailable in this isolated fixture")
    finally:
        conn.close()


def seed_vector_pair(srv, core, node_id, vector, *, summary="old summary", keywords=None, metadata=None):
    """Install a deliberately consistent JSON + REAL vec baseline for rollback controls."""
    conn = srv._get_db()
    try:
        conn.execute("DELETE FROM vec_nodes WHERE node_id = ?", (node_id,))
        conn.execute("INSERT INTO vec_nodes VALUES (?, ?)", (
            node_id, core._sqlite_vec.serialize_float32(vector),
        ))
        conn.execute(
            "UPDATE nodes SET recall_summary = ?, recall_keywords = ?, embedding = ?, metadata = ? WHERE id = ?",
            (summary, json.dumps(keywords or ["old-one", "old-two", "old-three"]),
             json.dumps(vector), json.dumps(metadata or {}), node_id),
        )
        conn.commit()
    finally:
        conn.close()


def test_summary_only_and_keywords_only_refresh_full_canonical_recipe(monkeypatch):
    """Each field-only edit embeds the full post-update recipe, not bare claim text."""
    srv = fresh_server()
    node_id = add_obs(srv, "canonical claim")
    fake, _core = install_fake_encoder(monkeypatch, srv)

    first = apply(srv, [entry(node_id, "first summary", ["old-one", "old-two", "old-three"])])
    assert first["ok"] == [{"node_id": node_id, "embedding_refresh": "refreshed"}], first
    assert fake.texts[-1] == "canonical claim\nfirst summary old-one old-two old-three"
    assert json.loads(node_row(srv, node_id)["embedding"]) == [
        float(len(fake.texts[-1])) + i / 1000 for i in range(384)
    ]

    summary_only = apply(srv, [entry(node_id, "changed summary", ["old-one", "old-two", "old-three"])])
    assert summary_only["ok"][0]["embedding_refresh"] == "refreshed", summary_only
    assert fake.texts[-1] == "canonical claim\nchanged summary old-one old-two old-three"

    keywords_only = apply(srv, [entry(node_id, "changed summary", ["new-one", "new-two", "new-three"])])
    assert keywords_only["ok"][0]["embedding_refresh"] == "refreshed", keywords_only
    assert fake.texts[-1] == "canonical claim\nchanged summary new-one new-two new-three"
    assert json.loads(node_row(srv, node_id)["embedding"]) == [
        float(len(fake.texts[-1])) + i / 1000 for i in range(384)
    ]


def test_keywords_only_refreshes_full_canonical_recipe(monkeypatch):
    """A keyword-only rewrite gets its own regression control and full-text oracle."""
    srv = fresh_server()
    node_id = add_obs(srv, "keyword-only claim")
    fake, _core = install_fake_encoder(monkeypatch, srv)
    baseline = apply(srv, [entry(node_id, "stable summary", ["old-one", "old-two", "old-three"])])
    assert baseline["ok"][0]["embedding_refresh"] == "refreshed", baseline
    result = apply(srv, [entry(node_id, "stable summary", ["new-one", "new-two", "new-three"])])
    assert result["ok"][0]["embedding_refresh"] == "refreshed", result
    assert fake.texts[-1] == "keyword-only claim\nstable summary new-one new-two new-three"
    assert json.loads(node_row(srv, node_id)["embedding"]) == [
        float(len(fake.texts[-1])) + i / 1000 for i in range(384)
    ]

def test_unavailable_embedder_preserves_old_vector_and_persists_debt(monkeypatch):
    srv = fresh_server()
    node_id = add_obs(srv, "unavailable claim")
    conn = srv._get_db()
    try:
        conn.execute(
            "UPDATE nodes SET embedding = ?, metadata = ? WHERE id = ?",
            ("[9.0, 8.0]", json.dumps({"unrelated": "keep", "embedding_restore_state": "separate"}), node_id),
        )
        conn.commit()
    finally:
        conn.close()
    install_fake_encoder(monkeypatch, srv, available=False)

    result = apply(srv, [entry(node_id, "accepted while unavailable", ["one", "two", "three"])])
    assert result["applied"] == 1 and result["failed"] == 0, result
    assert result["ok"] == [{"node_id": node_id, "embedding_refresh": "pending", "embedding_refresh_reason": "unavailable"}], result
    assert result["needs_reembed"] == [node_id], result
    row = node_row(srv, node_id)
    assert row["recall_summary"] == "accepted while unavailable"
    assert row["embedding"] == "[9.0, 8.0]"
    metadata = json.loads(row["metadata"])
    assert metadata["embedding_refresh_pending"]["reason"] == "unavailable"
    assert metadata["unrelated"] == "keep" and metadata["embedding_restore_state"] == "separate"


@pytest.mark.parametrize("failure", ["text", "embed_exception", "empty", "store"])
def test_compute_failures_keep_summary_but_retain_old_json_vector(monkeypatch, failure):
    """Every non-mirror compute failure is summary-success + scoped debt, never NULL repair."""
    srv = fresh_server()
    node_id = add_obs(srv, f"{failure} failure claim")
    fake, core = install_fake_encoder(monkeypatch, srv)
    conn = srv._get_db()
    try:
        conn.execute("UPDATE nodes SET embedding = ? WHERE id = ?", ("[8.0, 7.0]", node_id))
        conn.commit()
    finally:
        conn.close()
    if failure == "text":
        monkeypatch.setattr(core, "_embedding_text_for_node", lambda _conn, _id: (_ for _ in ()).throw(RuntimeError("text")))
    elif failure == "embed_exception":
        monkeypatch.setattr(fake, "embed", lambda _text, _model: (_ for _ in ()).throw(RuntimeError("embed")))
    elif failure == "empty":
        monkeypatch.setattr(fake, "embed", lambda _text, _model: [])
    else:
        original_execute = None
        # A narrowly targeted authorizer makes the helper's JSON UPDATE fail
        # while allowing the prior summary UPDATE and debt bookkeeping.
        original_get_db = srv._recall_summaries_mod.core._get_db

        def authorizing_db():
            conn = original_get_db()
            conn.set_authorizer(
                lambda action, arg1, arg2, _db, _source: (
                    __import__("sqlite3").SQLITE_DENY
                    if action == __import__("sqlite3").SQLITE_UPDATE and arg1 == "nodes" and arg2 == "embedding"
                    else __import__("sqlite3").SQLITE_OK
                )
            )
            return conn

        monkeypatch.setattr(srv._recall_summaries_mod.core, "_get_db", authorizing_db)

    result = apply(srv, [entry(node_id, f"{failure} accepted summary", ["one", "two", "three"])])
    expected_reason = "embed" if failure in {"embed_exception", "empty"} else failure
    assert result["ok"] == [{
        "node_id": node_id, "embedding_refresh": "pending",
        "embedding_refresh_reason": expected_reason,
    }], result
    row = node_row(srv, node_id)
    assert row["recall_summary"] == f"{failure} accepted summary"
    assert row["embedding"] == "[8.0, 7.0]"


def test_wrong_dimension_bypass_is_pending_and_restores_old_json(monkeypatch):
    srv = fresh_server()
    node_id = add_obs(srv, "wrong dimension claim")
    fake, core = install_fake_encoder(monkeypatch, srv)
    if not core._VEC_BACKEND_AVAILABLE or core._sqlite_vec is None:
        pytest.skip("REAL sqlite-vec unavailable in this interpreter")
    monkeypatch.setattr(fake, "embed", lambda _text, _model: [1.0, 2.0])
    conn = srv._get_db()
    try:
        if conn.execute("SELECT 1 FROM sqlite_master WHERE name = 'vec_nodes'").fetchone() is None:
            pytest.skip("REAL sqlite-vec store unavailable in this isolated fixture")
        conn.execute("UPDATE nodes SET embedding = ? WHERE id = ?", ("[6.0, 5.0]", node_id))
        conn.commit()
    finally:
        conn.close()
    result = apply(srv, [entry(node_id, "wrong dimension accepted", ["one", "two", "three"])])
    assert result["ok"][0]["embedding_refresh_reason"] == "mirror", result
    assert node_row(srv, node_id)["embedding"] == "[6.0, 5.0]"


def test_mirror_delete_then_insert_failure_rolls_back_both_vector_stores(monkeypatch):
    srv = fresh_server()
    node_id = add_obs(srv, "mirror failure claim")
    _fake, core = install_fake_encoder(monkeypatch, srv)
    if not core._VEC_BACKEND_AVAILABLE or core._sqlite_vec is None:
        pytest.skip("REAL sqlite-vec unavailable in this interpreter")
    conn = srv._get_db()
    try:
        if conn.execute("SELECT 1 FROM sqlite_master WHERE name = 'vec_nodes'").fetchone() is None:
            pytest.skip("REAL sqlite-vec store unavailable in this isolated fixture")
        old_vector = [7.0] * 384
        old_mirror = core._sqlite_vec.serialize_float32(old_vector)
        conn.execute("INSERT INTO vec_nodes VALUES (?, ?)", (node_id, old_mirror))
        conn.execute("UPDATE nodes SET embedding = ? WHERE id = ?", ("[7.0, 6.0]", node_id))
        conn.commit()
    finally:
        conn.close()

    def delete_then_fail(conn, nid):
        conn.execute("UPDATE nodes SET embedding = ? WHERE id = ?", ("[1.0]", nid))
        conn.execute("DELETE FROM vec_nodes WHERE node_id = ?", (nid,))
        return core.EmbeddingOutcome(status="embedded", node_id=nid, vec_mirror_ok=False)

    monkeypatch.setattr(core, "_compute_and_store_embedding", delete_then_fail)

    result = apply(srv, [entry(node_id, "summary survives mirror failure", ["one", "two", "three"])])
    assert result["ok"] == [{"node_id": node_id, "embedding_refresh": "pending", "embedding_refresh_reason": "mirror"}], result
    row = node_row(srv, node_id)
    assert row["recall_summary"] == "summary survives mirror failure"
    assert row["embedding"] == "[7.0, 6.0]"
    assert json.loads(row["metadata"])["embedding_refresh_pending"]["reason"] == "mirror"
    conn = srv._get_db()
    try:
        assert conn.execute("SELECT embedding FROM vec_nodes WHERE node_id = ?", (node_id,)).fetchone()[0] == old_mirror
    finally:
        conn.close()


def test_same_input_retry_clears_only_its_own_debt_and_diagnose_is_current_only(monkeypatch):
    srv = fresh_server()
    node_a = add_obs(srv, "retry A")
    node_b = add_obs(srv, "retry B")
    old = add_obs(srv, "historical debt")
    conn = srv._get_db()
    try:
        conn.execute("UPDATE nodes SET is_current = 0 WHERE id = ?", (old,))
        marker = json.dumps({"embedding_refresh_pending": {"reason": "unavailable", "at": "2026-01-01T00:00:00Z"}})
        conn.execute("UPDATE nodes SET metadata = ? WHERE id IN (?, ?)", (marker, node_a, old))
        conn.commit()
    finally:
        conn.close()
    fake, _core = install_fake_encoder(monkeypatch, srv)

    initial_a = apply(srv, [entry(node_a, "A retry", ["one", "two", "three"])])
    assert initial_a["ok"][0]["embedding_refresh"] == "refreshed", initial_a
    # Establish A/B debt after A's first success, then replay exactly the
    # same A input: the retry must refresh A and leave B's debt untouched.
    conn = srv._get_db()
    try:
        conn.execute("UPDATE nodes SET metadata = ? WHERE id IN (?, ?)", (marker, node_a, node_b))
        conn.commit()
    finally:
        conn.close()
    result = apply(srv, [entry(node_a, "A retry", ["one", "two", "three"])])
    assert result["ok"][0]["embedding_refresh"] == "refreshed", result
    assert "embedding_refresh_pending" not in json.loads(node_row(srv, node_a)["metadata"])
    assert "embedding_refresh_pending" in json.loads(node_row(srv, node_b)["metadata"])
    diagnose = json.loads(srv.engram_diagnose())
    debt = diagnose["embedding_refresh_debt"]
    assert debt["count"] == 1 and debt["sample_ids"] == [node_b] and debt["flare"] is True, debt
    assert old not in debt["sample_ids"]
    assert fake.texts[-1] == "retry A\nA retry one two three"


@pytest.mark.parametrize("non_object_metadata", ["[1, 2, 3]", '"scalar"', "null"])
def test_diagnose_ignores_non_object_metadata_while_counting_valid_refresh_debt(
    monkeypatch, non_object_metadata,
):
    """Debt diagnosis is read-only and ignores valid JSON that is not an object."""
    srv = fresh_server()
    valid = add_obs(srv, "valid refresh debt")
    non_object = add_obs(srv, "non-object metadata")
    marker = json.dumps({
        "embedding_refresh_pending": {
            "reason": "unavailable", "stage": "strict_refresh", "at": "2026-01-01T00:00:00Z",
        },
    })
    conn = srv._get_db()
    try:
        conn.execute("UPDATE nodes SET metadata = ? WHERE id = ?", (marker, valid))
        conn.execute("UPDATE nodes SET metadata = ? WHERE id = ?", (non_object_metadata, non_object))
        conn.commit()
    finally:
        conn.close()

    diagnose = json.loads(srv.engram_diagnose())

    debt = diagnose["embedding_refresh_debt"]
    assert debt["count"] == 1 and debt["sample_ids"] == [valid] and debt["flare"] is True, debt
    assert node_row(srv, non_object)["metadata"] == non_object_metadata


def test_generic_reembed_preserves_summary_refresh_debt_marker(monkeypatch):
    """Only strict summary refresh may clear its own pending-debt marker."""
    srv = fresh_server()
    node_id = add_obs(srv, "generic re-embed marked node")
    marker = json.dumps({
        "embedding_refresh_pending": {
            "reason": "unavailable", "stage": "strict_refresh", "at": "2026-01-01T00:00:00Z",
        },
    })
    conn = srv._get_db()
    try:
        conn.execute("UPDATE nodes SET metadata = ? WHERE id = ?", (marker, node_id))
        conn.commit()
    finally:
        conn.close()
    fake, core = install_fake_encoder(monkeypatch, srv)

    conn = srv._get_db()
    try:
        counts = core._reembed_nodes(conn, [node_id], caller_name="test_generic_reembed")
    finally:
        conn.close()

    assert counts == {"processed": 1, "embedded": 1, "skipped": 0, "failed": 0}
    assert fake.texts == ["generic re-embed marked node"]
    assert json.loads(node_row(srv, node_id)["metadata"])["embedding_refresh_pending"]["reason"] == "unavailable"


def test_successful_real_vec_refresh_matches_fresh_float32_oracle(monkeypatch):
    """A strict success verifies both canonical JSON and the REAL vec mirror bytes."""
    srv = fresh_server()
    node_id = add_obs(srv, "real mirror claim")
    fake, core = install_fake_encoder(monkeypatch, srv)
    require_real_vec(srv, core)

    result = apply(srv, [entry(node_id, "real mirror summary", ["one", "two", "three"])])
    assert result["ok"] == [{"node_id": node_id, "embedding_refresh": "refreshed"}], result
    expected = [float(len(fake.texts[-1])) + i / 1000 for i in range(384)]
    assert json.loads(node_row(srv, node_id)["embedding"]) == expected
    assert vec_row(srv, node_id) == core._sqlite_vec.serialize_float32(expected)


def test_real_core_insert_after_delete_failure_restores_both_vectors_and_marks_debt(monkeypatch):
    """The actual core helper's vec INSERT failure rolls back its preceding DELETE."""
    srv = fresh_server()
    node_id = add_obs(srv, "actual core mirror failure")
    fake, core = install_fake_encoder(monkeypatch, srv)
    require_real_vec(srv, core)
    old_vector = [7.0 + i / 1000 for i in range(384)]
    old_metadata = {"unrelated": "keep"}
    seed_vector_pair(srv, core, node_id, old_vector, metadata=old_metadata)
    old_json = node_row(srv, node_id)["embedding"]
    old_mirror = vec_row(srv, node_id)
    original_get_db = core._get_db

    def authorizing_db():
        conn = original_get_db()
        conn.set_authorizer(
            lambda action, arg1, _arg2, _db, _source: (
                sqlite3.SQLITE_DENY
                if action == sqlite3.SQLITE_INSERT and arg1 == "vec_nodes"
                else sqlite3.SQLITE_OK
            )
        )
        return conn

    monkeypatch.setattr(core, "_get_db", authorizing_db)
    result = apply(srv, [entry(node_id, "new summary survives", ["one", "two", "three"])])
    assert fake.texts, "the real _compute_and_store_embedding path must have embedded first"
    assert result["ok"] == [{
        "node_id": node_id, "embedding_refresh": "pending", "embedding_refresh_reason": "mirror",
    }], result
    row = node_row(srv, node_id)
    assert row["recall_summary"] == "new summary survives"
    assert row["embedding"] == old_json
    assert vec_row(srv, node_id) == old_mirror
    metadata = json.loads(row["metadata"])
    assert metadata["unrelated"] == "keep"
    assert metadata["embedding_refresh_pending"]["reason"] == "mirror"


@pytest.mark.parametrize("via_file", [False, True], ids=["inline", "file"])
def test_rejected_peers_preserve_vectors_and_debt_while_valid_peer_applies(monkeypatch, tmp_path, via_file):
    """Validation/not-found/non-current peers have no effects in either payload transport."""
    srv = fresh_server()
    invalid = add_obs(srv, "invalid peer")
    missing = "ob_999999"
    old = add_obs(srv, "historical peer")
    valid = add_obs(srv, "valid peer")
    fake, core = install_fake_encoder(monkeypatch, srv)
    require_real_vec(srv, core)
    conn = srv._get_db()
    try:
        conn.execute("UPDATE nodes SET is_current = 0 WHERE id = ?", (old,))
        conn.commit()
    finally:
        conn.close()
    debt = {"embedding_refresh_pending": {"reason": "unavailable", "stage": "strict_refresh", "at": "2026-01-01T00:00:00Z"}}
    for number, node_id in enumerate((invalid, old), start=1):
        seed_vector_pair(srv, core, node_id, [float(number)] * 384, metadata=debt)
    before = {node_id: (dict(node_row(srv, node_id)), vec_row(srv, node_id)) for node_id in (invalid, old)}
    entries = [
        entry(invalid, "invalid must not apply", ["only", "two"]),
        entry(missing, "missing must not apply", ["one", "two", "three"]),
        entry(old, "old must not apply", ["one", "two", "three"]),
        entry(valid, "valid does apply", ["one", "two", "three"]),
    ]
    if via_file:
        payload_file = tmp_path / "mixed.json"
        payload_file.write_text(json.dumps({"summaries": entries}), encoding="utf-8")
        result = json.loads(srv.engram_set_recall_summaries(payload_json=json.dumps({"summaries_file": str(payload_file)})))
    else:
        result = apply(srv, entries)
    assert result["applied"] == 1 and result["failed"] == 3, result
    assert result["ok"] == [{"node_id": valid, "embedding_refresh": "refreshed"}], result
    assert fake.texts, "the valid peer should still perform its strict refresh"
    for node_id, expected in before.items():
        assert (dict(node_row(srv, node_id)), vec_row(srv, node_id)) == expected


class AvailabilitySequence:
    def __init__(self, availability):
        self.availability = iter(availability)
        self.texts = []

    def is_available(self):
        return next(self.availability)

    def embed(self, text, _model):
        self.texts.append(text)
        return [1.0] * 384


def test_needs_reembed_is_latest_applied_debt_per_target_in_stable_order(monkeypatch):
    """A later strict success removes only that target's earlier response debt."""
    srv = fresh_server()
    node_a = add_obs(srv, "duplicate target A")
    node_b = add_obs(srv, "still pending target B")
    _fake, core = install_fake_encoder(monkeypatch, srv)
    monkeypatch.setattr(core, "_embedder", AvailabilitySequence([False, False, True]))
    result = apply(srv, [
        entry(node_a, "A pending", ["one", "two", "three"]),
        entry(node_b, "B pending", ["one", "two", "three"]),
        entry(node_a, "A refreshed", ["one", "two", "three"]),
    ])
    assert [item["embedding_refresh"] for item in result["ok"]] == ["pending", "pending", "refreshed"], result
    assert result["needs_reembed"] == [node_b], result
    assert "embedding_refresh_pending" not in json.loads(node_row(srv, node_a)["metadata"])
    assert "embedding_refresh_pending" in json.loads(node_row(srv, node_b)["metadata"])


def test_needs_reembed_latest_pending_replaces_earlier_success(monkeypatch):
    srv = fresh_server()
    node_id = add_obs(srv, "success then pending")
    _fake, core = install_fake_encoder(monkeypatch, srv)
    monkeypatch.setattr(core, "_embedder", AvailabilitySequence([True, False]))
    result = apply(srv, [
        entry(node_id, "first succeeds", ["one", "two", "three"]),
        entry(node_id, "second pending", ["one", "two", "three"]),
    ])
    assert [item["embedding_refresh"] for item in result["ok"]] == ["refreshed", "pending"], result
    assert result["needs_reembed"] == [node_id], result
    assert "embedding_refresh_pending" in json.loads(node_row(srv, node_id)["metadata"])


@pytest.mark.parametrize("later", ["invalid", "error"])
def test_needs_reembed_ignores_later_non_applied_attempt(monkeypatch, later):
    srv = fresh_server()
    node_id = add_obs(srv, f"pending then {later}")
    _fake, core = install_fake_encoder(monkeypatch, srv, available=False)
    if later == "error":
        # The first marker write is allowed.  The second (successful-refresh
        # clear) is rejected, so its outer entry savepoint reports an error.
        conn = srv._get_db()
        try:
            conn.execute(
                "CREATE TRIGGER reject_marker_clear BEFORE UPDATE OF metadata ON nodes "
                "WHEN NEW.id = '%s' AND NEW.metadata NOT LIKE '%%embedding_refresh_pending%%' "
                "BEGIN SELECT RAISE(FAIL, 'marker clear rejected'); END" % node_id
            )
            conn.commit()
        finally:
            conn.close()
        monkeypatch.setattr(core, "_embedder", AvailabilitySequence([False, True]))
        second = entry(node_id, "second errors", ["one", "two", "three"])
    else:
        second = entry(node_id, "invalid", ["only", "two"])
    result = apply(srv, [entry(node_id, "first pending", ["one", "two", "three"]), second])
    assert result["ok"][0]["embedding_refresh"] == "pending", result
    assert result["failed"] == 1, result
    assert result["needs_reembed"] == [node_id], result
    assert "embedding_refresh_pending" in json.loads(node_row(srv, node_id)["metadata"])


def test_marker_bookkeeping_failure_rolls_back_that_entry_but_keeps_earlier_peer(monkeypatch):
    srv = fresh_server()
    earlier = add_obs(srv, "earlier peer")
    target = add_obs(srv, "bookkeeping failure peer")
    _fake, core = install_fake_encoder(monkeypatch, srv)
    require_real_vec(srv, core)
    old_vector = [9.0] * 384
    old_metadata = {"unrelated": "survives"}
    seed_vector_pair(srv, core, target, old_vector, metadata=old_metadata)
    before_target = (dict(node_row(srv, target)), vec_row(srv, target))
    conn = srv._get_db()
    try:
        conn.execute(
            "CREATE TRIGGER reject_target_metadata BEFORE UPDATE OF metadata ON nodes "
            "WHEN NEW.id = '%s' BEGIN SELECT RAISE(FAIL, 'bookkeeping unavailable'); END" % target
        )
        conn.commit()
    finally:
        conn.close()
    result = apply(srv, [
        entry(earlier, "earlier accepted", ["one", "two", "three"]),
        entry(target, "target must roll back", ["one", "two", "three"]),
    ])
    assert result["applied"] == 1 and result["failed"] == 1, result
    assert result["ok"] == [{"node_id": earlier, "embedding_refresh": "refreshed"}], result
    assert result["errors"][0]["node_id"] == target and "bookkeeping unavailable" in result["errors"][0]["error"]
    assert node_row(srv, earlier)["recall_summary"] == "earlier accepted"
    assert (dict(node_row(srv, target)), vec_row(srv, target)) == before_target
