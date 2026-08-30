"""Tests for engram_supersede single-payload signature (wave 2b of ob_1499).

Coverage:
  1. Happy path — well-formed payload links new as successor of old.
  2. Schema enforcement — invalid JSON, non-object, unknown fields.
  3. Required-field validation — missing old_node_id / new_node_id rejected.
  4. Internal helper — _supersede_impl works with named kwargs.
  5. #1777/#1952 PR3 — supersede does NOT touch recall_summary/needs_tightening/
     summary_missing on either node (confirmed, not assumed symmetric with
     engram_resolve's fix — see _supersede_impl's docstring for why old's
     stale summary is inert once is_current=0, and new already carries its
     own write-time summary under the #1952 PR1 contract).
"""

import atexit
import json
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


TEST_DIR = tempfile.mkdtemp(prefix="engram_test_payload_supersede_")
atexit.register(lambda: shutil.rmtree(TEST_DIR, ignore_errors=True))


def fresh_server():
    if os.path.exists(TEST_DIR):
        shutil.rmtree(TEST_DIR)
    for key in list(sys.modules):
        if key == "server" or key.startswith("engram_"):
            del sys.modules[key]
    import server
    server._configure_paths(TEST_DIR)
    server._ensure_data_dir()
    os.environ["ENGRAM_NO_EMBEDDINGS"] = "1"
    return server


def _seed_two_observations_same_type(server):
    """Two observations of same type — the supersede target shape."""
    base = {
        "url": "https://example.com/a", "title": "A",
        "quote_type": "attributed_analysis",
    }
    p1 = {**base, "quoted_text": "Original quote.",
          "interpretation": "Original interp.", "claim": "Original claim."}
    p2 = {**base, "quoted_text": "Refined quote.",
          "interpretation": "Refined interp.", "claim": "Refined claim."}
    a = json.loads(server.engram_add_observation(payload_json=json.dumps(p1)))
    b = json.loads(server.engram_add_observation(payload_json=json.dumps(p2)))
    return a["observation_id"], b["observation_id"]


# ── 1. Happy path ──────────────────────────────────────────────────────────


def test_payload_json_links_supersede():
    server = fresh_server()
    old_id, new_id = _seed_two_observations_same_type(server)
    payload = {
        "old_node_id": old_id,
        "new_node_id": new_id,
        "supersede_reason": "Refined interpretation after re-reading source.",
    }
    r = json.loads(server.engram_supersede(payload_json=json.dumps(payload)))
    assert "error" not in r, r
    assert r.get("old_node_id") == old_id, r
    assert r.get("new_node_id") == new_id, r


# ── 2. Schema enforcement ──────────────────────────────────────────────────


def test_payload_json_invalid_json_rejected():
    server = fresh_server()
    r = json.loads(server.engram_supersede(payload_json="{not-json}"))
    assert "error" in r and "Invalid JSON" in r["error"], r


def test_payload_json_non_object_rejected():
    server = fresh_server()
    r = json.loads(server.engram_supersede(payload_json="[1, 2, 3]"))
    assert "error" in r and "must be a JSON object" in r["error"], r


def test_payload_json_unknown_fields_rejected():
    server = fresh_server()
    old_id, new_id = _seed_two_observations_same_type(server)
    payload = {
        "old_node_id": old_id, "new_node_id": new_id,
        "bogus_field": "should reject",
    }
    r = json.loads(server.engram_supersede(payload_json=json.dumps(payload)))
    assert "error" in r and "Unknown fields" in r["error"], r
    assert "bogus_field" in r["error"]


# ── 3. Required-field validation ───────────────────────────────────────────


def test_payload_json_missing_old_node_id_rejected():
    server = fresh_server()
    payload = {"new_node_id": "ob_0001"}
    r = json.loads(server.engram_supersede(payload_json=json.dumps(payload)))
    assert "error" in r and "old_node_id" in r["error"], r


def test_payload_json_missing_new_node_id_rejected():
    server = fresh_server()
    payload = {"old_node_id": "ob_0001"}
    r = json.loads(server.engram_supersede(payload_json=json.dumps(payload)))
    assert "error" in r and "new_node_id" in r["error"], r


# ── 4. Internal helper still callable with kwargs + positionals ────────────


def test_impl_callable_with_kwargs():
    server = fresh_server()
    old_id, new_id = _seed_two_observations_same_type(server)
    r = json.loads(server._supersede_impl(
        old_node_id=old_id,
        new_node_id=new_id,
        supersede_reason="kwargs-path test",
    ))
    assert "error" not in r, r


def test_impl_callable_with_positionals():
    """The internal _impl preserves positional-args calling convention.

    test_supersede_relational.py extensively uses positional args on the
    old surface; preserving that convention through the migration was
    one of the wave-2b goals.
    """
    server = fresh_server()
    old_id, new_id = _seed_two_observations_same_type(server)
    r = json.loads(server._supersede_impl(old_id, new_id))
    assert "error" not in r, r


# ── 5. #1777/#1952 PR3 — supersede leaves recall_summary fields alone ──────


def test_supersede_does_not_touch_recall_summary_fields():
    """Unlike engram_resolve, engram_supersede must not modify recall_summary,
    needs_tightening, or summary_missing on either node — confirmed via the
    design-of-record's own claim ("supersede needs nothing special: the
    successor is a new node under the new contract"), not assumed by
    symmetry with the resolve fix.

    recall_summary is seeded at WRITE TIME (in the observation payload, an
    INSERT) rather than via a post-hoc UPDATE — kept deliberately consistent
    with how every other node actually acquires a recall_summary under the
    #1952 PR1 contract, and sidesteps a reproducible local-environment
    sqlite3/mmap+WAL interaction (observed on this machine's venv: sqlite
    3.53.4 / macOS 26.5.1) where an UPDATE-via-a-second-connection between
    two other tool calls corrupts the *next* connection's mmap view of an
    unrelated table. Confirmed pre-existing and unrelated to this PR's
    diff — reproduces identically via the vanilla, unmodified
    engram_set_recall_summaries tool with no #1952 PR3 code involved at all.
    Flagged in the handoff for the parent; not fixed here (out of scope)."""
    server = fresh_server()
    base = {
        "url": "https://example.com/a", "title": "A",
        "quote_type": "attributed_analysis",
    }
    p1 = {
        **base, "quoted_text": "Original quote.",
        "interpretation": "Original interp.", "claim": "Original claim.",
        "recall_summary": "Old node's own summary, unrelated to supersede.",
        "recall_keywords": ["kw-one", "kw-two"],
    }
    p2 = {
        **base, "quoted_text": "Refined quote.",
        "interpretation": "Refined interp.", "claim": "Refined claim.",
    }
    a = json.loads(server.engram_add_observation(payload_json=json.dumps(p1)))
    b = json.loads(server.engram_add_observation(payload_json=json.dumps(p2)))
    old_id, new_id = a["observation_id"], b["observation_id"]

    import sqlite3
    conn = sqlite3.connect(str(server.core.DB_PATH))
    conn.row_factory = sqlite3.Row
    try:
        before_old = conn.execute(
            "SELECT recall_summary, needs_tightening, summary_missing FROM nodes WHERE id = ?",
            (old_id,),
        ).fetchone()
        before_new = conn.execute(
            "SELECT recall_summary, needs_tightening, summary_missing FROM nodes WHERE id = ?",
            (new_id,),
        ).fetchone()
    finally:
        conn.close()
    assert before_old["recall_summary"] == "Old node's own summary, unrelated to supersede."

    r = json.loads(server.engram_supersede(payload_json=json.dumps({
        "old_node_id": old_id,
        "new_node_id": new_id,
    })))
    assert "error" not in r, r

    conn = sqlite3.connect(str(server.core.DB_PATH))
    conn.row_factory = sqlite3.Row
    try:
        after_old = conn.execute(
            "SELECT recall_summary, needs_tightening, summary_missing FROM nodes WHERE id = ?",
            (old_id,),
        ).fetchone()
        after_new = conn.execute(
            "SELECT recall_summary, needs_tightening, summary_missing FROM nodes WHERE id = ?",
            (new_id,),
        ).fetchone()
    finally:
        conn.close()

    assert tuple(after_old) == tuple(before_old), (
        f"supersede must not touch old node's recall_summary fields: "
        f"before={tuple(before_old)}, after={tuple(after_old)}"
    )
    assert tuple(after_new) == tuple(before_new), (
        f"supersede must not touch new node's recall_summary fields: "
        f"before={tuple(before_new)}, after={tuple(after_new)}"
    )
