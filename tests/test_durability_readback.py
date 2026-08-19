"""Durability read-back guard tests (alpha#2074 + #2097, gates v0.4.0-rc1).

Motivating incident: alpha#1947 (2026-08-07) — during a WAL/shm split-brain
window, two observations filed through the degraded server returned full
`status: created` success payloads (node id, evidence id, confidence, dedup
neighbours) and were silently lost when the server was replaced. A caller
had no way to tell a durable write from a doomed one from the payload alone.

The guard (`engram_core._verify_durable_write`) re-reads the just-written row
on the SAME connection, after commit(), and asserts content (non-empty
claim), not just existence. On a miss it reports `status:
"failed_not_durable"` instead of `created` — no auto-retry.

Coverage, per the task's mutation-verified requirement:
  1. Positive control — a genuinely durable write reports `created`, and the
     row is independently confirmed present with real content.
  2. Fault-injected negative — `core._verify_durable_write` is monkeypatched
     to return False (simulating a post-commit read-back miss / empty-claim
     displacement), and the call site must convert that into
     `failed_not_durable` rather than reporting `created`. This test is
     mutation-sensitive: if the guard's call site is deleted from
     `_add_observation_impl` / `_add_conjecture_impl`, this test FAILS
     (status reverts to "created" despite the injected fault) — verified by
     hand, see the coder-fairy handoff for both runs' output.
  3. Unit-level coverage of `_verify_durable_write` itself: missing row,
     empty-claim row, and a real-claim row.
"""

import atexit
import json
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


TEST_DIR = tempfile.mkdtemp(prefix="engram_test_durability_readback_")
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


# ── 1. Positive control — genuine durable writes report created ───────────


def test_observation_positive_control_reports_created_and_is_durable():
    server = fresh_server()
    payload = {
        "url": "https://example.com/durability-obs",
        "title": "Durability control",
        "quoted_text": "X says Y",
        "interpretation": "X claims Y",
        "claim": "Y is true according to X",
        "quote_type": "attributed_analysis",
    }
    r = json.loads(server.engram_add_observation(payload_json=json.dumps(payload)))
    assert r.get("status") == "created", f"Expected created, got {r}"
    node_id = r["observation_id"]

    # Independent confirmation the row genuinely exists with real content —
    # not merely that the guard was told to say yes.
    import engram_core as core
    conn = core._get_db()
    try:
        row = conn.execute("SELECT claim FROM nodes WHERE id = ?", (node_id,)).fetchone()
        assert row is not None, "row should exist after a real commit"
        assert row["claim"] and row["claim"].strip(), "claim should be non-empty"
    finally:
        conn.close()


def test_conjecture_positive_control_reports_created_and_is_durable():
    server = fresh_server()
    payload = {
        "claim": "Structured memory substitutes for model scale.",
        "basis": "Working hypothesis from ob_0001 series.",
    }
    r = json.loads(server.engram_add_conjecture(payload_json=json.dumps(payload)))
    assert r.get("status") == "created", f"Expected created, got {r}"
    node_id = r["conjecture_id"]

    import engram_core as core
    conn = core._get_db()
    try:
        row = conn.execute("SELECT claim FROM nodes WHERE id = ?", (node_id,)).fetchone()
        assert row is not None
        assert row["claim"] and row["claim"].strip()
    finally:
        conn.close()


# ── 2. Fault-injected negative — mutation-sensitive ────────────────────────


def test_observation_fault_injected_readback_miss_reports_failed_not_durable(monkeypatch):
    server = fresh_server()
    import engram_observation

    monkeypatch.setattr(
        engram_observation.core, "_verify_durable_write", lambda *a, **k: False
    )

    payload = {
        "url": "https://example.com/durability-obs-fault",
        "title": "Durability fault",
        "quoted_text": "X says Y",
        "interpretation": "X claims Y",
        "claim": "Y is true according to X (fault-injected)",
        "quote_type": "attributed_analysis",
    }
    r = json.loads(server.engram_add_observation(payload_json=json.dumps(payload)))
    assert r.get("status") == "failed_not_durable", (
        f"Expected failed_not_durable under injected read-back miss, got {r}. "
        "If this assertion fails with status=='created', the guard's call "
        "site in _add_observation_impl has likely been removed."
    )
    assert "observation_id" in r
    assert "error" in r


def test_conjecture_fault_injected_readback_miss_reports_failed_not_durable(monkeypatch):
    server = fresh_server()
    import engram_epistemic

    monkeypatch.setattr(
        engram_epistemic.core, "_verify_durable_write", lambda *a, **k: False
    )

    payload = {
        "claim": "Structured memory substitutes for model scale (fault-injected).",
        "basis": "Working hypothesis from ob_0001 series.",
    }
    r = json.loads(server.engram_add_conjecture(payload_json=json.dumps(payload)))
    assert r.get("status") == "failed_not_durable", (
        f"Expected failed_not_durable under injected read-back miss, got {r}. "
        "If this assertion fails with status=='created', the guard's call "
        "site in _add_conjecture_impl has likely been removed."
    )
    assert "conjecture_id" in r
    assert "error" in r


# ── 3. Unit-level coverage of the shared helper ────────────────────────────


def test_verify_durable_write_unit_cases():
    server = fresh_server()
    import engram_core as core

    conn = core._get_db()
    try:
        # Missing row.
        assert core._verify_durable_write(conn, "ob_9999") is False

        # Empty-claim row — the alpha#1947-class displacement shape.
        node_id = core._next_id(conn, "observation_factual")
        now = core._now()
        conn.execute(
            "INSERT INTO nodes (id, type, claim, created_at) VALUES (?, ?, ?, ?)",
            (node_id, "observation_factual", "", now),
        )
        conn.commit()
        assert core._verify_durable_write(conn, node_id) is False

        # Whitespace-only claim — also counts as missing.
        node_id2 = core._next_id(conn, "observation_factual")
        conn.execute(
            "INSERT INTO nodes (id, type, claim, created_at) VALUES (?, ?, ?, ?)",
            (node_id2, "observation_factual", "   ", now),
        )
        conn.commit()
        assert core._verify_durable_write(conn, node_id2) is False

        # Real claim.
        node_id3 = core._next_id(conn, "observation_factual")
        conn.execute(
            "INSERT INTO nodes (id, type, claim, created_at) VALUES (?, ?, ?, ?)",
            (node_id3, "observation_factual", "a real claim", now),
        )
        conn.commit()
        assert core._verify_durable_write(conn, node_id3) is True
    finally:
        conn.close()
