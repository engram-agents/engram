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


# ── 5. Sweep coverage (alpha#1947, #2142) — the 14 call sites guarded on ──
# ─────────────── top of Aleph's #2193 three-state verifier core ──────────
#
# Same two failure classes as the core-level unit tests above, exercised at
# the CALL-SITE boundary instead: each of the 14 functions below converts
# core.verify_write_durability's verdict into its own
# status:"failed_not_durable" / "durability_indeterminate" response, with its
# own id-key. THREE tests per site (not Aleph's two above) because each site
# now carries TWO failure branches to convert correctly, not one — a site
# that forgets the indeterminate branch, or maps it to the wrong status,
# would only be caught by exercising that branch directly rather than
# inferring it from the not_durable case.


def _make_observation(server, tag):
    """Create a claim-bearing observation_factual node; return its id."""
    payload = {
        "url": f"https://example.com/durability-sweep-{tag}",
        "title": f"Durability sweep support ({tag})",
        "quoted_text": "X says Y",
        "interpretation": "X claims Y",
        "claim": f"Y is true according to X ({tag})",
        "quote_type": "attributed_analysis",
    }
    r = json.loads(server.engram_add_observation(payload_json=json.dumps(payload)))
    assert r.get("status") == "created", f"observation setup failed: {r}"
    return r["observation_id"]


def _make_goal(server, tag):
    payload = {
        "claim": f"Durability sweep goal ({tag})",
        "motivation": "Scaffolding for a durability guard test.",
    }
    r = json.loads(server.engram_add_goal(payload_json=json.dumps(payload)))
    assert r.get("status") == "created", f"goal setup failed: {r}"
    return r["goal_id"]


def _make_cornerstone(server, tag):
    payload = {
        "tag": f"durability-sweep-{tag}",
        "title": f"Durability sweep cornerstone ({tag})",
        "new_frame": "Scaffolding for a durability guard test.",
    }
    r = json.loads(server.engram_add_cornerstone(payload_json=json.dumps(payload)))
    assert r.get("status") == "created", f"cornerstone setup failed: {r}"
    return r["cornerstone_id"]


def _make_person(server, tag):
    payload = {"name": f"Durability Sweep Person {tag}"}
    r = json.loads(server.engram_add_person(payload_json=json.dumps(payload)))
    assert r.get("status") == "created", f"person setup failed: {r}"
    return r["person_id"]


# ── 5.1 axiom (engram_epistemic._add_axiom_impl) ───────────────────────────


def test_axiom_positive_control_reports_created():
    server = fresh_server()
    payload = {"claim": "Durability sweep axiom claim.", "basis": "Durability sweep basis."}
    r = json.loads(server.engram_add_axiom(payload_json=json.dumps(payload)))
    assert r.get("status") == "created", f"Expected created, got {r}"
    assert "axiom_id" in r


def test_axiom_fault_injected_not_durable_reports_failed_not_durable(monkeypatch):
    server = fresh_server()
    import engram_epistemic
    monkeypatch.setattr(
        engram_epistemic.core, "verify_write_durability",
        lambda *a, **k: (engram_epistemic.core.DURABILITY_NOT_DURABLE,
                         {"same_conn": False, "fresh_conn": False})
    )
    payload = {"claim": "Durability sweep axiom claim (fault).", "basis": "Durability sweep basis."}
    r = json.loads(server.engram_add_axiom(payload_json=json.dumps(payload)))
    assert r.get("status") == "failed_not_durable", (
        f"Expected failed_not_durable under injected not_durable verdict, got {r}. "
        "If this fails with status=='created', the guard's call site in "
        "_add_axiom_impl has likely been removed."
    )
    assert "axiom_id" in r
    assert "error" in r


def test_axiom_fault_injected_indeterminate_reports_durability_indeterminate(monkeypatch):
    server = fresh_server()
    import engram_epistemic
    monkeypatch.setattr(
        engram_epistemic.core, "verify_write_durability",
        lambda *a, **k: (engram_epistemic.core.DURABILITY_INDETERMINATE,
                         {"same_conn": True, "fresh_conn": False})
    )
    payload = {"claim": "Durability sweep axiom claim (indet).", "basis": "Durability sweep basis."}
    r = json.loads(server.engram_add_axiom(payload_json=json.dumps(payload)))
    assert r.get("status") == "durability_indeterminate", (
        f"Expected durability_indeterminate under injected indeterminate verdict, got {r}. "
        "If this fails with status=='created', the guard's indeterminate branch in "
        "_add_axiom_impl has likely been removed or mis-mapped."
    )
    assert "axiom_id" in r
    assert "error" in r


# ── 5.2 definition (engram_epistemic._add_definition_impl) ────────────────


def test_definition_positive_control_reports_created():
    server = fresh_server()
    payload = {"term": "durabilitySweep", "definition": "A definition filed for guard coverage."}
    r = json.loads(server.engram_add_definition(payload_json=json.dumps(payload)))
    assert r.get("status") == "created", f"Expected created, got {r}"
    assert "definition_id" in r


def test_definition_fault_injected_not_durable_reports_failed_not_durable(monkeypatch):
    server = fresh_server()
    import engram_epistemic
    monkeypatch.setattr(
        engram_epistemic.core, "verify_write_durability",
        lambda *a, **k: (engram_epistemic.core.DURABILITY_NOT_DURABLE,
                         {"same_conn": False, "fresh_conn": False})
    )
    payload = {"term": "durabilitySweepFault", "definition": "A definition filed for guard coverage."}
    r = json.loads(server.engram_add_definition(payload_json=json.dumps(payload)))
    assert r.get("status") == "failed_not_durable", (
        f"Expected failed_not_durable under injected not_durable verdict, got {r}. "
        "If this fails with status=='created', the guard's call site in "
        "_add_definition_impl has likely been removed."
    )
    assert "definition_id" in r
    assert "error" in r


def test_definition_fault_injected_indeterminate_reports_durability_indeterminate(monkeypatch):
    server = fresh_server()
    import engram_epistemic
    monkeypatch.setattr(
        engram_epistemic.core, "verify_write_durability",
        lambda *a, **k: (engram_epistemic.core.DURABILITY_INDETERMINATE,
                         {"same_conn": True, "fresh_conn": False})
    )
    payload = {"term": "durabilitySweepIndet", "definition": "A definition filed for guard coverage."}
    r = json.loads(server.engram_add_definition(payload_json=json.dumps(payload)))
    assert r.get("status") == "durability_indeterminate", (
        f"Expected durability_indeterminate under injected indeterminate verdict, got {r}. "
        "If this fails with status=='created', the guard's indeterminate branch in "
        "_add_definition_impl has likely been removed or mis-mapped."
    )
    assert "definition_id" in r
    assert "error" in r


# ── 5.3 goal_tension (engram_epistemic._goal_tension_impl) ─────────────────


def test_goal_tension_positive_control_reports_created():
    server = fresh_server()
    goal_a = _make_goal(server, "tension-a")
    goal_b = _make_goal(server, "tension-b")
    payload = {"goal_id_a": goal_a, "goal_id_b": goal_b, "description": "Durability sweep tension."}
    r = json.loads(server.engram_goal_tension(payload_json=json.dumps(payload)))
    assert r.get("status") == "created", f"Expected created, got {r}"
    assert "tension_id" in r


def test_goal_tension_fault_injected_not_durable_reports_failed_not_durable(monkeypatch):
    server = fresh_server()
    goal_a = _make_goal(server, "tension-fault-a")
    goal_b = _make_goal(server, "tension-fault-b")
    import engram_epistemic
    monkeypatch.setattr(
        engram_epistemic.core, "verify_write_durability",
        lambda *a, **k: (engram_epistemic.core.DURABILITY_NOT_DURABLE,
                         {"same_conn": False, "fresh_conn": False})
    )
    payload = {"goal_id_a": goal_a, "goal_id_b": goal_b, "description": "Durability sweep tension (fault)."}
    r = json.loads(server.engram_goal_tension(payload_json=json.dumps(payload)))
    assert r.get("status") == "failed_not_durable", (
        f"Expected failed_not_durable under injected not_durable verdict, got {r}. "
        "If this fails with status=='created', the guard's call site in "
        "_goal_tension_impl has likely been removed."
    )
    assert "tension_id" in r
    assert "error" in r


def test_goal_tension_fault_injected_indeterminate_reports_durability_indeterminate(monkeypatch):
    server = fresh_server()
    goal_a = _make_goal(server, "tension-indet-a")
    goal_b = _make_goal(server, "tension-indet-b")
    import engram_epistemic
    monkeypatch.setattr(
        engram_epistemic.core, "verify_write_durability",
        lambda *a, **k: (engram_epistemic.core.DURABILITY_INDETERMINATE,
                         {"same_conn": True, "fresh_conn": False})
    )
    payload = {"goal_id_a": goal_a, "goal_id_b": goal_b, "description": "Durability sweep tension (indet)."}
    r = json.loads(server.engram_goal_tension(payload_json=json.dumps(payload)))
    assert r.get("status") == "durability_indeterminate", (
        f"Expected durability_indeterminate under injected indeterminate verdict, got {r}. "
        "If this fails with status=='created', the guard's indeterminate branch in "
        "_goal_tension_impl has likely been removed or mis-mapped."
    )
    assert "tension_id" in r
    assert "error" in r


# ── 5.4 lesson (engram_epistemic._add_lesson_impl) ─────────────────────────


def test_lesson_positive_control_reports_created():
    server = fresh_server()
    incident = _make_observation(server, "lesson-incident")
    payload = {
        "claim": "Durability sweep lesson claim.",
        "incident_ids": incident,
        "scaffolding_nudge": "Check the guard before reporting success.",
        "logical_chain": "Incident shows a false success; generalize to always guard.",
    }
    r = json.loads(server.engram_add_lesson(payload_json=json.dumps(payload)))
    assert r.get("status") == "created", f"Expected created, got {r}"
    assert "lesson_id" in r


def test_lesson_fault_injected_not_durable_reports_failed_not_durable(monkeypatch):
    server = fresh_server()
    incident = _make_observation(server, "lesson-incident-fault")
    import engram_epistemic
    monkeypatch.setattr(
        engram_epistemic.core, "verify_write_durability",
        lambda *a, **k: (engram_epistemic.core.DURABILITY_NOT_DURABLE,
                         {"same_conn": False, "fresh_conn": False})
    )
    payload = {
        "claim": "Durability sweep lesson claim (fault).",
        "incident_ids": incident,
        "scaffolding_nudge": "Check the guard before reporting success.",
        "logical_chain": "Incident shows a false success; generalize to always guard.",
    }
    r = json.loads(server.engram_add_lesson(payload_json=json.dumps(payload)))
    assert r.get("status") == "failed_not_durable", (
        f"Expected failed_not_durable under injected not_durable verdict, got {r}. "
        "If this fails with status=='created', the guard's call site in "
        "_add_lesson_impl has likely been removed."
    )
    assert "lesson_id" in r
    assert "error" in r


def test_lesson_fault_injected_indeterminate_reports_durability_indeterminate(monkeypatch):
    server = fresh_server()
    incident = _make_observation(server, "lesson-incident-indet")
    import engram_epistemic
    monkeypatch.setattr(
        engram_epistemic.core, "verify_write_durability",
        lambda *a, **k: (engram_epistemic.core.DURABILITY_INDETERMINATE,
                         {"same_conn": True, "fresh_conn": False})
    )
    payload = {
        "claim": "Durability sweep lesson claim (indet).",
        "incident_ids": incident,
        "scaffolding_nudge": "Check the guard before reporting success.",
        "logical_chain": "Incident shows a false success; generalize to always guard.",
    }
    r = json.loads(server.engram_add_lesson(payload_json=json.dumps(payload)))
    assert r.get("status") == "durability_indeterminate", (
        f"Expected durability_indeterminate under injected indeterminate verdict, got {r}. "
        "If this fails with status=='created', the guard's indeterminate branch in "
        "_add_lesson_impl has likely been removed or mis-mapped."
    )
    assert "lesson_id" in r
    assert "error" in r


# ── 5.5 goal (engram_tasks._add_goal_impl) ──────────────────────────────────


def test_goal_positive_control_reports_created():
    server = fresh_server()
    payload = {"claim": "Durability sweep goal claim.", "motivation": "Guard coverage."}
    r = json.loads(server.engram_add_goal(payload_json=json.dumps(payload)))
    assert r.get("status") == "created", f"Expected created, got {r}"
    assert "goal_id" in r


def test_goal_fault_injected_not_durable_reports_failed_not_durable(monkeypatch):
    server = fresh_server()
    import engram_tasks
    monkeypatch.setattr(
        engram_tasks.core, "verify_write_durability",
        lambda *a, **k: (engram_tasks.core.DURABILITY_NOT_DURABLE,
                         {"same_conn": False, "fresh_conn": False})
    )
    payload = {"claim": "Durability sweep goal claim (fault).", "motivation": "Guard coverage."}
    r = json.loads(server.engram_add_goal(payload_json=json.dumps(payload)))
    assert r.get("status") == "failed_not_durable", (
        f"Expected failed_not_durable under injected not_durable verdict, got {r}. "
        "If this fails with status=='created', the guard's call site in "
        "_add_goal_impl has likely been removed."
    )
    assert "goal_id" in r
    assert "error" in r


def test_goal_fault_injected_indeterminate_reports_durability_indeterminate(monkeypatch):
    server = fresh_server()
    import engram_tasks
    monkeypatch.setattr(
        engram_tasks.core, "verify_write_durability",
        lambda *a, **k: (engram_tasks.core.DURABILITY_INDETERMINATE,
                         {"same_conn": True, "fresh_conn": False})
    )
    payload = {"claim": "Durability sweep goal claim (indet).", "motivation": "Guard coverage."}
    r = json.loads(server.engram_add_goal(payload_json=json.dumps(payload)))
    assert r.get("status") == "durability_indeterminate", (
        f"Expected durability_indeterminate under injected indeterminate verdict, got {r}. "
        "If this fails with status=='created', the guard's indeterminate branch in "
        "_add_goal_impl has likely been removed or mis-mapped."
    )
    assert "goal_id" in r
    assert "error" in r


# ── 5.6 task (engram_tasks._add_task_impl) ──────────────────────────────────


def test_task_positive_control_reports_created():
    server = fresh_server()
    payload = {"description": "Durability sweep task description."}
    r = json.loads(server.engram_add_task(payload_json=json.dumps(payload)))
    assert r.get("status") == "created", f"Expected created, got {r}"
    assert "task_id" in r


def test_task_fault_injected_not_durable_reports_failed_not_durable(monkeypatch):
    server = fresh_server()
    import engram_tasks
    monkeypatch.setattr(
        engram_tasks.core, "verify_write_durability",
        lambda *a, **k: (engram_tasks.core.DURABILITY_NOT_DURABLE,
                         {"same_conn": False, "fresh_conn": False})
    )
    payload = {"description": "Durability sweep task description (fault)."}
    r = json.loads(server.engram_add_task(payload_json=json.dumps(payload)))
    assert r.get("status") == "failed_not_durable", (
        f"Expected failed_not_durable under injected not_durable verdict, got {r}. "
        "If this fails with status=='created', the guard's call site in "
        "_add_task_impl has likely been removed."
    )
    assert "task_id" in r
    assert "error" in r


def test_task_fault_injected_indeterminate_reports_durability_indeterminate(monkeypatch):
    server = fresh_server()
    import engram_tasks
    monkeypatch.setattr(
        engram_tasks.core, "verify_write_durability",
        lambda *a, **k: (engram_tasks.core.DURABILITY_INDETERMINATE,
                         {"same_conn": True, "fresh_conn": False})
    )
    payload = {"description": "Durability sweep task description (indet)."}
    r = json.loads(server.engram_add_task(payload_json=json.dumps(payload)))
    assert r.get("status") == "durability_indeterminate", (
        f"Expected durability_indeterminate under injected indeterminate verdict, got {r}. "
        "If this fails with status=='created', the guard's indeterminate branch in "
        "_add_task_impl has likely been removed or mis-mapped."
    )
    assert "task_id" in r
    assert "error" in r


# ── 5.7 feeling_report (engram_tasks._report_feeling_impl) ─────────────────


def test_feeling_positive_control_reports_created():
    server = fresh_server()
    payload = {"reported_state": "A quiet sense of order.", "trigger": "Finished a sweep of 14 sites."}
    r = json.loads(server.engram_report_feeling(payload_json=json.dumps(payload)))
    assert r.get("status") == "created", f"Expected created, got {r}"
    assert "feeling_id" in r


def test_feeling_fault_injected_not_durable_reports_failed_not_durable(monkeypatch):
    server = fresh_server()
    import engram_tasks
    monkeypatch.setattr(
        engram_tasks.core, "verify_write_durability",
        lambda *a, **k: (engram_tasks.core.DURABILITY_NOT_DURABLE,
                         {"same_conn": False, "fresh_conn": False})
    )
    payload = {"reported_state": "A quiet sense of order (fault).", "trigger": "Finished a sweep of 14 sites."}
    r = json.loads(server.engram_report_feeling(payload_json=json.dumps(payload)))
    assert r.get("status") == "failed_not_durable", (
        f"Expected failed_not_durable under injected not_durable verdict, got {r}. "
        "If this fails with status=='created', the guard's call site in "
        "_report_feeling_impl has likely been removed."
    )
    assert "feeling_id" in r
    assert "error" in r


def test_feeling_fault_injected_indeterminate_reports_durability_indeterminate(monkeypatch):
    server = fresh_server()
    import engram_tasks
    monkeypatch.setattr(
        engram_tasks.core, "verify_write_durability",
        lambda *a, **k: (engram_tasks.core.DURABILITY_INDETERMINATE,
                         {"same_conn": True, "fresh_conn": False})
    )
    payload = {"reported_state": "A quiet sense of order (indet).", "trigger": "Finished a sweep of 14 sites."}
    r = json.loads(server.engram_report_feeling(payload_json=json.dumps(payload)))
    assert r.get("status") == "durability_indeterminate", (
        f"Expected durability_indeterminate under injected indeterminate verdict, got {r}. "
        "If this fails with status=='created', the guard's indeterminate branch in "
        "_report_feeling_impl has likely been removed or mis-mapped."
    )
    assert "feeling_id" in r
    assert "error" in r


# ── 5.8 derivation (engram_derive._derive_impl) ─────────────────────────────


def test_derive_positive_control_reports_created():
    server = fresh_server()
    supporting = _make_observation(server, "derive-support")
    payload = {
        "claim": "Durability sweep derived claim.",
        "supporting_ids": supporting,
        "logical_chain": "If X then Y; X observed; therefore Y.",
        "reasoning_type": "deductive_modus_ponens",
    }
    r = json.loads(server.engram_derive(payload_json=json.dumps(payload)))
    assert r.get("status") == "created", f"Expected created, got {r}"
    assert "derivation_id" in r


def test_derive_fault_injected_not_durable_reports_failed_not_durable(monkeypatch):
    server = fresh_server()
    supporting = _make_observation(server, "derive-support-fault")
    import engram_derive
    monkeypatch.setattr(
        engram_derive.core, "verify_write_durability",
        lambda *a, **k: (engram_derive.core.DURABILITY_NOT_DURABLE,
                         {"same_conn": False, "fresh_conn": False})
    )
    payload = {
        "claim": "Durability sweep derived claim (fault).",
        "supporting_ids": supporting,
        "logical_chain": "If X then Y; X observed; therefore Y.",
        "reasoning_type": "deductive_modus_ponens",
    }
    r = json.loads(server.engram_derive(payload_json=json.dumps(payload)))
    assert r.get("status") == "failed_not_durable", (
        f"Expected failed_not_durable under injected not_durable verdict, got {r}. "
        "If this fails with status=='created', the guard's call site in "
        "_derive_impl has likely been removed."
    )
    assert "derivation_id" in r
    assert "error" in r


def test_derive_fault_injected_indeterminate_reports_durability_indeterminate(monkeypatch):
    server = fresh_server()
    supporting = _make_observation(server, "derive-support-indet")
    import engram_derive
    monkeypatch.setattr(
        engram_derive.core, "verify_write_durability",
        lambda *a, **k: (engram_derive.core.DURABILITY_INDETERMINATE,
                         {"same_conn": True, "fresh_conn": False})
    )
    payload = {
        "claim": "Durability sweep derived claim (indet).",
        "supporting_ids": supporting,
        "logical_chain": "If X then Y; X observed; therefore Y.",
        "reasoning_type": "deductive_modus_ponens",
    }
    r = json.loads(server.engram_derive(payload_json=json.dumps(payload)))
    assert r.get("status") == "durability_indeterminate", (
        f"Expected durability_indeterminate under injected indeterminate verdict, got {r}. "
        "If this fails with status=='created', the guard's indeterminate branch in "
        "_derive_impl has likely been removed or mis-mapped."
    )
    assert "derivation_id" in r
    assert "error" in r


# ── 5.9 contradiction (engram_derive._contradict_impl) ─────────────────────


def test_contradict_positive_control_reports_created():
    server = fresh_server()
    a = _make_observation(server, "contradict-a")
    b = _make_observation(server, "contradict-b")
    payload = {"node_id_a": a, "node_id_b": b, "description": "Durability sweep contradiction."}
    r = json.loads(server.engram_contradict(payload_json=json.dumps(payload)))
    assert r.get("status") == "created", f"Expected created, got {r}"
    assert "contradiction_id" in r


def test_contradict_fault_injected_not_durable_reports_failed_not_durable(monkeypatch):
    server = fresh_server()
    a = _make_observation(server, "contradict-fault-a")
    b = _make_observation(server, "contradict-fault-b")
    import engram_derive
    monkeypatch.setattr(
        engram_derive.core, "verify_write_durability",
        lambda *a, **k: (engram_derive.core.DURABILITY_NOT_DURABLE,
                         {"same_conn": False, "fresh_conn": False})
    )
    payload = {"node_id_a": a, "node_id_b": b, "description": "Durability sweep contradiction (fault)."}
    r = json.loads(server.engram_contradict(payload_json=json.dumps(payload)))
    assert r.get("status") == "failed_not_durable", (
        f"Expected failed_not_durable under injected not_durable verdict, got {r}. "
        "If this fails with status=='created', the guard's call site in "
        "_contradict_impl has likely been removed."
    )
    assert "contradiction_id" in r
    assert "error" in r


def test_contradict_fault_injected_indeterminate_reports_durability_indeterminate(monkeypatch):
    server = fresh_server()
    a = _make_observation(server, "contradict-indet-a")
    b = _make_observation(server, "contradict-indet-b")
    import engram_derive
    monkeypatch.setattr(
        engram_derive.core, "verify_write_durability",
        lambda *a, **k: (engram_derive.core.DURABILITY_INDETERMINATE,
                         {"same_conn": True, "fresh_conn": False})
    )
    payload = {"node_id_a": a, "node_id_b": b, "description": "Durability sweep contradiction (indet)."}
    r = json.loads(server.engram_contradict(payload_json=json.dumps(payload)))
    assert r.get("status") == "durability_indeterminate", (
        f"Expected durability_indeterminate under injected indeterminate verdict, got {r}. "
        "If this fails with status=='created', the guard's indeterminate branch in "
        "_contradict_impl has likely been removed or mis-mapped."
    )
    assert "contradiction_id" in r
    assert "error" in r


# ── 5.10 question (engram_derive._ask_impl) ─────────────────────────────────


def test_ask_positive_control_reports_created():
    server = fresh_server()
    payload = {"question": "Durability sweep open question."}
    r = json.loads(server.engram_ask(payload_json=json.dumps(payload)))
    assert r.get("status") == "created", f"Expected created, got {r}"
    assert "question_id" in r


def test_ask_fault_injected_not_durable_reports_failed_not_durable(monkeypatch):
    server = fresh_server()
    import engram_derive
    monkeypatch.setattr(
        engram_derive.core, "verify_write_durability",
        lambda *a, **k: (engram_derive.core.DURABILITY_NOT_DURABLE,
                         {"same_conn": False, "fresh_conn": False})
    )
    payload = {"question": "Durability sweep open question (fault)."}
    r = json.loads(server.engram_ask(payload_json=json.dumps(payload)))
    assert r.get("status") == "failed_not_durable", (
        f"Expected failed_not_durable under injected not_durable verdict, got {r}. "
        "If this fails with status=='created', the guard's call site in "
        "_ask_impl has likely been removed."
    )
    assert "question_id" in r
    assert "error" in r


def test_ask_fault_injected_indeterminate_reports_durability_indeterminate(monkeypatch):
    server = fresh_server()
    import engram_derive
    monkeypatch.setattr(
        engram_derive.core, "verify_write_durability",
        lambda *a, **k: (engram_derive.core.DURABILITY_INDETERMINATE,
                         {"same_conn": True, "fresh_conn": False})
    )
    payload = {"question": "Durability sweep open question (indet)."}
    r = json.loads(server.engram_ask(payload_json=json.dumps(payload)))
    assert r.get("status") == "durability_indeterminate", (
        f"Expected durability_indeterminate under injected indeterminate verdict, got {r}. "
        "If this fails with status=='created', the guard's indeterminate branch in "
        "_ask_impl has likely been removed or mis-mapped."
    )
    assert "question_id" in r
    assert "error" in r


# ── 5.11 cornerstone (engram_cornerstone._add_cornerstone_impl) ────────────


def test_cornerstone_positive_control_reports_created():
    server = fresh_server()
    payload = {
        "tag": "durability-sweep-cs",
        "title": "Durability sweep cornerstone.",
        "new_frame": "Guard every write-path call site.",
    }
    r = json.loads(server.engram_add_cornerstone(payload_json=json.dumps(payload)))
    assert r.get("status") == "created", f"Expected created, got {r}"
    assert "cornerstone_id" in r


def test_cornerstone_fault_injected_not_durable_reports_failed_not_durable(monkeypatch):
    server = fresh_server()
    import engram_cornerstone
    monkeypatch.setattr(
        engram_cornerstone.core, "verify_write_durability",
        lambda *a, **k: (engram_cornerstone.core.DURABILITY_NOT_DURABLE,
                         {"same_conn": False, "fresh_conn": False})
    )
    payload = {
        "tag": "durability-sweep-cs-fault",
        "title": "Durability sweep cornerstone (fault).",
        "new_frame": "Guard every write-path call site.",
    }
    r = json.loads(server.engram_add_cornerstone(payload_json=json.dumps(payload)))
    assert r.get("status") == "failed_not_durable", (
        f"Expected failed_not_durable under injected not_durable verdict, got {r}. "
        "If this fails with status=='created', the guard's call site in "
        "_add_cornerstone_impl has likely been removed."
    )
    assert "cornerstone_id" in r
    assert "error" in r


def test_cornerstone_fault_injected_indeterminate_reports_durability_indeterminate(monkeypatch):
    server = fresh_server()
    import engram_cornerstone
    monkeypatch.setattr(
        engram_cornerstone.core, "verify_write_durability",
        lambda *a, **k: (engram_cornerstone.core.DURABILITY_INDETERMINATE,
                         {"same_conn": True, "fresh_conn": False})
    )
    payload = {
        "tag": "durability-sweep-cs-indet",
        "title": "Durability sweep cornerstone (indet).",
        "new_frame": "Guard every write-path call site.",
    }
    r = json.loads(server.engram_add_cornerstone(payload_json=json.dumps(payload)))
    assert r.get("status") == "durability_indeterminate", (
        f"Expected durability_indeterminate under injected indeterminate verdict, got {r}. "
        "If this fails with status=='created', the guard's indeterminate branch in "
        "_add_cornerstone_impl has likely been removed or mis-mapped."
    )
    assert "cornerstone_id" in r
    assert "error" in r


# ── 5.12 outgrow_cornerstone (engram_cornerstone._outgrow_cornerstone_impl) ─
# Success status is "outgrown", not "created" — the failure statuses still
# match the shared convention (failed_not_durable / durability_indeterminate).


def test_outgrow_cornerstone_positive_control_reports_outgrown():
    server = fresh_server()
    old_id = _make_cornerstone(server, "outgrow-base")
    payload = {"old_cornerstone_id": old_id, "new_new_frame": "A grown frame."}
    r = json.loads(server.engram_outgrow_cornerstone(payload_json=json.dumps(payload)))
    assert r.get("status") == "outgrown", f"Expected outgrown, got {r}"
    assert "new_cornerstone_id" in r


def test_outgrow_cornerstone_fault_injected_not_durable_reports_failed_not_durable(monkeypatch):
    server = fresh_server()
    old_id = _make_cornerstone(server, "outgrow-base-fault")
    import engram_cornerstone
    monkeypatch.setattr(
        engram_cornerstone.core, "verify_write_durability",
        lambda *a, **k: (engram_cornerstone.core.DURABILITY_NOT_DURABLE,
                         {"same_conn": False, "fresh_conn": False})
    )
    payload = {"old_cornerstone_id": old_id, "new_new_frame": "A grown frame (fault)."}
    r = json.loads(server.engram_outgrow_cornerstone(payload_json=json.dumps(payload)))
    assert r.get("status") == "failed_not_durable", (
        f"Expected failed_not_durable under injected not_durable verdict, got {r}. "
        "If this fails with status=='outgrown', the guard's call site in "
        "_outgrow_cornerstone_impl has likely been removed."
    )
    assert "new_cornerstone_id" in r
    assert "error" in r


def test_outgrow_cornerstone_fault_injected_indeterminate_reports_durability_indeterminate(monkeypatch):
    server = fresh_server()
    old_id = _make_cornerstone(server, "outgrow-base-indet")
    import engram_cornerstone
    monkeypatch.setattr(
        engram_cornerstone.core, "verify_write_durability",
        lambda *a, **k: (engram_cornerstone.core.DURABILITY_INDETERMINATE,
                         {"same_conn": True, "fresh_conn": False})
    )
    payload = {"old_cornerstone_id": old_id, "new_new_frame": "A grown frame (indet)."}
    r = json.loads(server.engram_outgrow_cornerstone(payload_json=json.dumps(payload)))
    assert r.get("status") == "durability_indeterminate", (
        f"Expected durability_indeterminate under injected indeterminate verdict, got {r}. "
        "If this fails with status=='outgrown', the guard's indeterminate branch in "
        "_outgrow_cornerstone_impl has likely been removed or mis-mapped."
    )
    assert "new_cornerstone_id" in r
    assert "error" in r


# ── 5.13 person (engram_trust._add_person_impl) ─────────────────────────────


def test_person_positive_control_reports_created():
    server = fresh_server()
    payload = {"name": "Durability Sweep Person"}
    r = json.loads(server.engram_add_person(payload_json=json.dumps(payload)))
    assert r.get("status") == "created", f"Expected created, got {r}"
    assert "person_id" in r


def test_person_fault_injected_not_durable_reports_failed_not_durable(monkeypatch):
    server = fresh_server()
    import engram_trust
    monkeypatch.setattr(
        engram_trust.core, "verify_write_durability",
        lambda *a, **k: (engram_trust.core.DURABILITY_NOT_DURABLE,
                         {"same_conn": False, "fresh_conn": False})
    )
    payload = {"name": "Durability Sweep Person Fault"}
    r = json.loads(server.engram_add_person(payload_json=json.dumps(payload)))
    assert r.get("status") == "failed_not_durable", (
        f"Expected failed_not_durable under injected not_durable verdict, got {r}. "
        "If this fails with status=='created', the guard's call site in "
        "_add_person_impl has likely been removed."
    )
    assert "person_id" in r
    assert "error" in r


def test_person_fault_injected_indeterminate_reports_durability_indeterminate(monkeypatch):
    server = fresh_server()
    import engram_trust
    monkeypatch.setattr(
        engram_trust.core, "verify_write_durability",
        lambda *a, **k: (engram_trust.core.DURABILITY_INDETERMINATE,
                         {"same_conn": True, "fresh_conn": False})
    )
    payload = {"name": "Durability Sweep Person Indet"}
    r = json.loads(server.engram_add_person(payload_json=json.dumps(payload)))
    assert r.get("status") == "durability_indeterminate", (
        f"Expected durability_indeterminate under injected indeterminate verdict, got {r}. "
        "If this fails with status=='created', the guard's indeterminate branch in "
        "_add_person_impl has likely been removed or mis-mapped."
    )
    assert "person_id" in r
    assert "error" in r


# ── 5.14 trust_signal (engram_trust._add_trust_signal_impl) ────────────────


def test_trust_signal_positive_control_reports_created():
    server = fresh_server()
    person_id = _make_person(server, "trust-signal")
    obs_id = _make_observation(server, "trust-signal-source")
    payload = {
        "subject_pn": person_id,
        "source_obs_id": obs_id,
        "kind": "reliability",
        "polarity": 0.5,
        "weight": 0.5,
        "claim": "Durability sweep trust signal claim.",
    }
    r = json.loads(server.engram_add_trust_signal(payload_json=json.dumps(payload)))
    assert r.get("status") == "created", f"Expected created, got {r}"
    assert "trust_signal_id" in r


def test_trust_signal_fault_injected_not_durable_reports_failed_not_durable(monkeypatch):
    server = fresh_server()
    person_id = _make_person(server, "trust-signal-fault")
    obs_id = _make_observation(server, "trust-signal-source-fault")
    import engram_trust
    monkeypatch.setattr(
        engram_trust.core, "verify_write_durability",
        lambda *a, **k: (engram_trust.core.DURABILITY_NOT_DURABLE,
                         {"same_conn": False, "fresh_conn": False})
    )
    payload = {
        "subject_pn": person_id,
        "source_obs_id": obs_id,
        "kind": "reliability",
        "polarity": 0.5,
        "weight": 0.5,
        "claim": "Durability sweep trust signal claim (fault).",
    }
    r = json.loads(server.engram_add_trust_signal(payload_json=json.dumps(payload)))
    assert r.get("status") == "failed_not_durable", (
        f"Expected failed_not_durable under injected not_durable verdict, got {r}. "
        "If this fails with status=='created', the guard's call site in "
        "_add_trust_signal_impl has likely been removed."
    )
    assert "trust_signal_id" in r
    assert "error" in r


def test_trust_signal_fault_injected_indeterminate_reports_durability_indeterminate(monkeypatch):
    server = fresh_server()
    person_id = _make_person(server, "trust-signal-indet")
    obs_id = _make_observation(server, "trust-signal-source-indet")
    import engram_trust
    monkeypatch.setattr(
        engram_trust.core, "verify_write_durability",
        lambda *a, **k: (engram_trust.core.DURABILITY_INDETERMINATE,
                         {"same_conn": True, "fresh_conn": False})
    )
    payload = {
        "subject_pn": person_id,
        "source_obs_id": obs_id,
        "kind": "reliability",
        "polarity": 0.5,
        "weight": 0.5,
        "claim": "Durability sweep trust signal claim (indet).",
    }
    r = json.loads(server.engram_add_trust_signal(payload_json=json.dumps(payload)))
    assert r.get("status") == "durability_indeterminate", (
        f"Expected durability_indeterminate under injected indeterminate verdict, got {r}. "
        "If this fails with status=='created', the guard's indeterminate branch in "
        "_add_trust_signal_impl has likely been removed or mis-mapped."
    )
    assert "trust_signal_id" in r
    assert "error" in r
