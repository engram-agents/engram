"""Durability read-back guard tests (alpha#2074 + #2097, gates v0.4.0-rc1).

Motivating incident: alpha#1947 (2026-08-07) — during a WAL/shm split-brain
window, two observations filed through the degraded server returned full
`status: created` success payloads (node id, evidence id, confidence, dedup
neighbours) and were silently lost when the server was replaced. A caller
had no way to tell a durable write from a doomed one from the payload alone.

The guard (`engram_core.verify_write_durability`) re-reads the just-written row
on the SAME connection AND through a fresh one (dual read, alpha#2193), after commit(), and asserts content (non-empty
claim), not just existence. On a miss it reports `status:
"failed_not_durable"` instead of `created` — no auto-retry.

Coverage, per the task's mutation-verified requirement:
  1. Positive control — a genuinely durable write reports `created`, and the
     row is independently confirmed present with real content.
  2. Fault-injected negative — `core.verify_write_durability` is monkeypatched
     to return False (simulating a post-commit read-back miss / empty-claim
     displacement), and the call site must convert that into
     `failed_not_durable` rather than reporting `created`. This test is
     mutation-sensitive: if the guard's call site is deleted from
     `_add_observation_impl` / `_add_conjecture_impl`, this test FAILS
     (status reverts to "created" despite the injected fault) — verified by
     hand, see the coder-fairy handoff for both runs' output.
  3. Unit-level coverage of `verify_write_durability` itself: missing row,
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
        engram_observation.core, "verify_write_durability",
        lambda *a, **k: (engram_observation.core.DURABILITY_NOT_DURABLE,
                         {"same_conn": False, "fresh_conn": False})
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
        engram_epistemic.core, "verify_write_durability",
        lambda *a, **k: (engram_epistemic.core.DURABILITY_NOT_DURABLE,
                         {"same_conn": False, "fresh_conn": False})
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


def test_verify_write_durability_unit_cases(tmp_path):
    """Three verdicts, exercised on a real on-disk DB.

    Positive control first: a healthy write must return DURABLE, otherwise a
    later 'no indeterminate seen' result would be an instrument that cannot
    say yes rather than a system that is well.
    """
    import sqlite3
    from src.engram import engram_core as core

    db = tmp_path / "k.db"
    conn = sqlite3.connect(str(db))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("CREATE TABLE nodes (id TEXT PRIMARY KEY, claim TEXT)")
    conn.commit()

    # 1. healthy — both views agree the row is there
    conn.execute("INSERT INTO nodes VALUES ('ob_ok', 'a real claim')")
    conn.commit()
    verdict, detail = core.verify_write_durability(conn, "ob_ok")
    assert verdict == core.DURABILITY_DURABLE, detail
    assert detail["same_conn"] is True and detail["fresh_conn"] is True

    # 2. genuinely absent — both views agree it is not there
    verdict, detail = core.verify_write_durability(conn, "ob_never_written")
    assert verdict == core.DURABILITY_NOT_DURABLE, detail

    # 3. empty claim counts as missing (carried over from the #1947 behaviour)
    conn.execute("INSERT INTO nodes VALUES ('ob_empty', '   ')")
    conn.commit()
    verdict, _ = core.verify_write_durability(conn, "ob_empty")
    assert verdict == core.DURABILITY_NOT_DURABLE

    # 4. THE alpha#2193 SHAPE — writing connection still sees a row that disk
    #    no longer backs. Under the old single same-conn read this returned
    #    True and the caller reported `created`; that is the silent loss.
    conn.execute("INSERT INTO nodes VALUES ('ob_ghost', 'claim')")
    conn.commit()
    for suffix in ("", "-wal", "-shm"):
        f = db.parent / (db.name + suffix)
        if f.exists():
            f.unlink()
    rebuilt = sqlite3.connect(str(db))
    rebuilt.execute("PRAGMA journal_mode=WAL")
    rebuilt.execute("CREATE TABLE nodes (id TEXT PRIMARY KEY, claim TEXT)")
    rebuilt.commit()
    rebuilt.close()

    verdict, detail = core.verify_write_durability(conn, "ob_ghost")
    assert verdict == core.DURABILITY_INDETERMINATE, detail
    assert detail["same_conn"] is True
    assert detail["fresh_conn"] is False
    conn.close()


def test_in_memory_connection_degrades_and_says_so():
    """An in-memory conn has no second view; it must NOT be reported
    indeterminate on every write, and it must not silently pretend to two."""
    import sqlite3
    from src.engram import engram_core as core

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE nodes (id TEXT PRIMARY KEY, claim TEXT)")
    conn.execute("INSERT INTO nodes VALUES ('ob_mem', 'claim')")
    conn.commit()

    verdict, detail = core.verify_write_durability(conn, "ob_mem")
    assert verdict == core.DURABILITY_DURABLE, detail
    assert detail.get("in_memory") is True
    assert detail["fresh_conn"] is None

    verdict, detail = core.verify_write_durability(conn, "ob_absent")
    assert verdict == core.DURABILITY_NOT_DURABLE, detail
    conn.close()


# ── 4. Coverage for what THIS change adds (alpha#2236 review) ──────────────
# The pre-existing fault-injection tests only ever inject NOT_DURABLE, so the
# third verdict -- the entire point of moving off a boolean -- was verified by
# hand in review and guarded by nothing. A later edit collapsing the
# indeterminate branch would have gone unnoticed.

def _indeterminate(*_a, **_k):
    from src.engram import engram_core as core
    return core.DURABILITY_INDETERMINATE, {"same_conn": True, "fresh_conn": False}


def test_observation_indeterminate_is_distinguishable(monkeypatch):
    server = fresh_server()
    import engram_observation

    monkeypatch.setattr(
        engram_observation.core, "verify_write_durability",
        lambda *a, **k: (engram_observation.core.DURABILITY_INDETERMINATE,
                         {"same_conn": True, "fresh_conn": False})
    )
    payload = {
        "url": "https://example.com/durability-obs-indeterminate",
        "title": "Durability indeterminate",
        "quoted_text": "X says Y",
        "interpretation": "X claims Y",
        "claim": "Y is true according to X (indeterminate-injected)",
        "quote_type": "attributed_analysis",
    }
    r = json.loads(server.engram_add_observation(payload_json=json.dumps(payload)))
    assert r.get("status") == "durability_indeterminate", (
        f"Expected durability_indeterminate under injected disagreement, got {r}. "
        "A status of failed_not_durable here means the three-way branch has "
        "been collapsed back to two."
    )
    assert r["durability"]["same_conn"] is True
    assert "DISAGREE" in r["error"]
    assert "did not verify as durable" not in r["error"]


def test_conjecture_indeterminate_is_distinguishable(monkeypatch):
    server = fresh_server()
    import engram_epistemic

    monkeypatch.setattr(
        engram_epistemic.core, "verify_write_durability",
        lambda *a, **k: (engram_epistemic.core.DURABILITY_INDETERMINATE,
                         {"same_conn": True, "fresh_conn": False})
    )
    payload = {
        "claim": "Structured memory substitutes for model scale (indeterminate).",
        "basis": "Working hypothesis.",
    }
    r = json.loads(server.engram_add_conjecture(payload_json=json.dumps(payload)))
    assert r.get("status") == "durability_indeterminate", r
    assert "DISAGREE" in r["error"]


def test_broken_writing_connection_reports_indeterminate(tmp_path):
    """same_conn_error branch: a writing connection too broken to read itself
    must be indeterminate, never a confident not_durable."""
    import sqlite3
    from src.engram import engram_core as core
    db = tmp_path / "k.db"
    conn = sqlite3.connect(str(db)); conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE nodes (id TEXT PRIMARY KEY, claim TEXT)"); conn.commit()
    conn.close()
    verdict, detail = core.verify_write_durability(conn, "ob_x")
    assert verdict == core.DURABILITY_INDETERMINATE
    assert detail["same_conn"] is None and "same_conn_error" in detail


def test_unopenable_fresh_connection_reports_indeterminate(tmp_path):
    """fresh_conn_error branch: if the second view cannot be opened at all,
    that is indeterminate -- NOT a pass on the strength of the first view."""
    import os, sqlite3, stat
    from src.engram import engram_core as core
    db = tmp_path / "k.db"
    conn = sqlite3.connect(str(db)); conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("CREATE TABLE nodes (id TEXT PRIMARY KEY, claim TEXT)")
    conn.execute("INSERT INTO nodes VALUES ('ob_p','claim')"); conn.commit()
    os.chmod(str(db), 0)
    try:
        verdict, detail = core.verify_write_durability(conn, "ob_p")
    finally:
        os.chmod(str(db), stat.S_IRUSR | stat.S_IWUSR)
        conn.close()
    if os.geteuid() == 0:
        return  # root ignores the mode; nothing to assert
    assert verdict == core.DURABILITY_INDETERMINATE
    assert detail["same_conn"] is True
    assert detail["fresh_conn"] is None and "fresh_conn_error" in detail


def test_path_resolution_failure_is_indeterminate_not_a_silent_guess():
    """The reviewed alpha#2236 defect: if PRAGMA database_list itself raises,
    the old code fell through to the in-memory branch and returned a verdict
    computed from the same-conn read alone, flagged `in_memory: True` -- a
    silent guess wearing a confident label.

    Unreachable through ordinary use (the same-conn read above it has already
    succeeded), so it is reached here with a connection wrapper that answers
    SELECTs and refuses exactly that one pragma. Without this, the fix is
    unguarded: reverting it leaves every test passing.
    """
    import sqlite3
    from src.engram import engram_core as core

    class _RefusesPragma:
        def __init__(self, real):
            self._real = real

        def execute(self, sql, *args, **kwargs):
            if "database_list" in sql:
                raise sqlite3.OperationalError("simulated: cannot answer pragma")
            return self._real.execute(sql, *args, **kwargs)

    real = sqlite3.connect(":memory:")
    real.row_factory = sqlite3.Row
    real.execute("CREATE TABLE nodes (id TEXT PRIMARY KEY, claim TEXT)")
    real.execute("INSERT INTO nodes VALUES ('ob_z', 'a real claim')")
    real.commit()

    verdict, detail = core.verify_write_durability(_RefusesPragma(real), "ob_z")
    real.close()

    # The same-conn read SAW the row. A silent guess would call that durable.
    assert detail["same_conn"] is True
    assert verdict == core.DURABILITY_INDETERMINATE, (
        f"path resolution failed but verdict was {verdict!r} -- the silent-guess "
        "fallthrough has been reintroduced"
    )
    assert "db_path_resolution_error" in detail
    assert detail.get("in_memory") is not True, (
        "a failed path lookup must not be reported as genuinely in-memory"
    )
