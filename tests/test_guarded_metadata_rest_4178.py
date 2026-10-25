"""Guarded read-then-write of nodes.metadata in engram_epistemic, the
engram_core.py `_apply_support_state` helper and tools/surgical.py
(engram-alpha #4178, PR 3 of 3; follow-up to #4166 / #4179).

The class (unchanged from #4166): a read of a node row whose metadata feeds a
later `UPDATE nodes SET ... metadata = ?` on the same id, unguarded, with NO
write transaction open on that connection at the read. sqlite3 (legacy
isolation) opens its transaction at the first DML, so a read AFTER an earlier
INSERT/UPDATE on the same connection (and before any commit) is protected.
A commit ENDS the transaction.

Sites (base 7045e979):
  A  _update_lesson_impl (epistemic ~1662): IN. The lesson row is the first
     statement on the connection (E_ul reads in_transaction False).
  B  _register_exemplar_impl support_lost clear (epistemic ~1826): IN. The
     target row is read BEFORE the edge INSERT; the commit after the INSERT
     ends that transaction, so the UPDATE opens a NEW one with the stale
     metadata read at the top (E_re: False at the read, False before the
     INSERT, False before the UPDATE).
  C  core._apply_support_state (core ~10347 mark / ~10356 clear): a HELPER;
     the read is in its two callers, both pass the RAW column value:
       C1 engram_revision._detect_zero_support (retract/supersede): EXEMPT.
          The node UPDATE of the same transaction precedes the read (E_rev_*:
          in_transaction True at the cs/ls read).
       C2 engram_lifecycle._reflect_impl (support-lost recompute): IN. The
          one SELECT that feeds every row precedes any DML on the connection
          (E_rf: False). The guard lives ONCE, in the helper, bound to the
          `metadata_raw` argument the helper already takes.
  D  core ~4255 `SET metadata = NULL` (bulk quarantine): NOT in the class, no
     read of metadata feeds the write (it is one set-based statement).
  E  tools/surgical.py metadata_patch (~730): the read IS unguarded and no
     transaction is open at it (SG_E False), but the tool is an offline repair
     tool that takes the store-maintenance lock for its whole run (#3882):
     EXEMPT by design, proven by SG_lock_* (the lock excludes the owner and
     every other maintenance tool; held during the read-write window).
Sweep: an AST scan of every string literal in the three files for
UPDATE+metadata (planted multi-line and f-string controls both found) lists
only the sites above; the other hits are docstrings and the CREATE TRIGGER.

Contract (written before the fix; IN-class cells are red on the base):
  S_x   a second writer changes ONLY metadata between read and write ->
        the tool's own error shape + `refusal: changed_concurrently`; the
        call changes nothing; the second writer's key survives.
  S_xb  a second writer changes ONLY is_current (metadata byte-identical) ->
        the same refusal/skip; the state clause earns its keep.
  S_xc  stored metadata in compact JSON spelling, seeded NON-empty, no second
        writer -> the call succeeds (the guard binds the RAW value read).
  E_*   `conn.in_transaction` proxy at each read (control-style: asserts the
        actual state, False or True).
Two places cannot return an error, so they SKIP and say so:
  B  the edge is already committed when the metadata write runs; the tool
     reports `created` plus `support_lost_discharge: skipped_changed_concurrently`.
  C  the helper's caller owns the commit and the loop; the helper writes
     nothing and returns "changed_concurrently" (reflect re-evaluates every
     node on its next run; the revision hook is a convenience).
"""

import json
import os
import shutil
import sys
import tempfile
import importlib.util
from pathlib import Path

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

REPO = Path(__file__).resolve().parent.parent


@pytest.fixture
def env(monkeypatch):
    d = tempfile.mkdtemp(prefix="engram_test_guarded_meta_4178_rest_")
    for key in list(sys.modules):
        if key == "server" or key.startswith("engram_"):
            del sys.modules[key]
    import server
    server._configure_paths(d)
    server._ensure_data_dir()
    os.environ["ENGRAM_NO_EMBEDDINGS"] = "1"
    monkeypatch.setenv("ENGRAM_HOME", d)
    yield server
    shutil.rmtree(d, ignore_errors=True)


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------

def _call(fn, **p):
    return json.loads(fn(payload_json=json.dumps(p)))


def _axiom(server, tag):
    r = _call(server.engram_add_axiom, claim=f"Fixture premise {tag} holds.",
              basis="test fixture basis")
    assert "error" not in r, r
    return r["axiom_id"]


def _lesson(server, tag="a"):
    ax = _axiom(server, f"inc-{tag}")
    r = _call(server.engram_add_lesson, claim=f"Lesson pattern {tag}.",
              incident_ids=ax, logical_chain=f"Pattern derived from {tag}.",
              scaffolding_nudge="Watch for this pattern.")
    assert "error" not in r, r
    return r["lesson_id"], ax


def _row(server, nid):
    conn = server.core._get_db()
    try:
        return dict(conn.execute("SELECT * FROM nodes WHERE id = ?", (nid,)).fetchone())
    finally:
        conn.close()


def _meta(server, nid):
    return json.loads(_row(server, nid)["metadata"])


def _raw_set(server, nid, raw):
    conn = server.core._get_db()
    try:
        conn.execute("UPDATE nodes SET metadata = ? WHERE id = ?", (raw, nid))
        conn.commit()
    finally:
        conn.close()


def _second_writer_sets_key(server, nid, key, value):
    conn = server.core._get_db()
    try:
        m = json.loads(conn.execute(
            "SELECT metadata FROM nodes WHERE id = ?", (nid,)).fetchone()["metadata"])
        m[key] = value
        conn.execute("UPDATE nodes SET metadata = ? WHERE id = ?", (json.dumps(m), nid))
        conn.commit()
    finally:
        conn.close()


def _second_writer_sets_state(server, nid, **cols):
    assert set(cols) <= {"is_current"}
    conn = server.core._get_db()
    try:
        for col, val in cols.items():
            conn.execute(f"UPDATE nodes SET {col} = ? WHERE id = ?", (val, nid))
        conn.commit()
    finally:
        conn.close()


def _compact(server, nid, **extra):
    """Rewrite `nid`'s metadata as compact JSON with NON-empty seeded keys
    (same content plus `extra`, different spelling). Returns the raw string."""
    m = _meta(server, nid)
    m.setdefault("zz_seed", 1)
    m.setdefault("aa_seed", 2)
    m.update(extra)
    raw = json.dumps(m, separators=(",", ":"))
    assert raw != json.dumps(m), "fixture must differ from default spelling"
    _raw_set(server, nid, raw)
    return raw


def _edit_count(server, nid, action):
    conn = server.core._get_db()
    try:
        return conn.execute(
            "SELECT COUNT(*) AS n FROM edit_history WHERE node_id = ? AND action = ?",
            (nid, action)).fetchone()["n"]
    finally:
        conn.close()


def _interfere_once(monkeypatch, server, attr, nid, action, node_arg_index):
    """Patch core.<attr> so the FIRST call whose positional arg
    [node_arg_index] is `nid` runs `action()` (a second writer, on its own
    connection) before delegating."""
    state = {"done": False}
    real = getattr(server.core, attr)

    def interfering(*a, **k):
        if (not state["done"] and len(a) > node_arg_index
                and a[node_arg_index] == nid):
            state["done"] = True
            action()
        return real(*a, **k)

    monkeypatch.setattr(server.core, attr, interfering)
    return state


class Probe:
    """Connection proxy: records (sql, params, conn.in_transaction BEFORE the
    statement) for every execute()."""

    def __init__(self, conn, log):
        self._c = conn
        self._log = log

    def execute(self, sql, *a, **k):
        self._log.append((" ".join(sql.split()), a[0] if a else (), self._c.in_transaction))
        return self._c.execute(sql, *a, **k)

    def __getattr__(self, name):
        return getattr(self._c, name)


def _probe_core(monkeypatch, server):
    log = []
    real = server.core._get_db
    monkeypatch.setattr(server.core, "_get_db",
                        lambda *a, **k: Probe(real(*a, **k), log))
    return log


def _first(log, prefix, nid):
    hits = [e for e in log if e[0].startswith(prefix)
            and (nid is None or nid in tuple(e[1]))]
    assert hits, f"no statement starting {prefix!r} for {nid}: {[e[0][:60] for e in log]}"
    return hits[0]


# --------------------------------------------------------------------------
# control: the probe can say True
# --------------------------------------------------------------------------

def test_E0_probe_can_report_true_and_false(env):
    server = env
    conn = server.core._get_db()
    log = []
    p = Probe(conn, log)
    try:
        p.execute("SELECT 1")
        p.execute("INSERT INTO edit_history (timestamp, turn, action, node_id, node_type, details)"
                  " VALUES ('t', 0, 'probe_control', 'x', 'x', '{}')")
        p.execute("SELECT 2")
        conn.rollback()
    finally:
        conn.close()
    assert [e[2] for e in log] == [False, False, True]


# --------------------------------------------------------------------------
# A: engram_update_lesson
# --------------------------------------------------------------------------

def _update_lesson(server, lid, text="Watch harder."):
    return _call(server.engram_update_lesson, node_id=lid,
                 updates={"scaffolding_nudge": text})


def test_E_ul_no_transaction_at_the_read(env, monkeypatch):
    server = env
    lid, _ = _lesson(server)
    log = _probe_core(monkeypatch, server)
    r = _update_lesson(server, lid)
    assert r.get("status") == "updated", r
    read = _first(log, "SELECT id, type, is_current, metadata FROM nodes", lid)
    assert read[2] is False


def test_S_ul_second_writer_metadata_refused(env, monkeypatch):
    server = env
    lid, _ = _lesson(server)
    st = _interfere_once(
        monkeypatch, server, "_metadata_dict_for_write", lid,
        lambda: _second_writer_sets_key(server, lid, "interfering_key", "mid-call"), 1)
    r = _update_lesson(server, lid)
    assert st["done"]
    assert r.get("refusal") == "changed_concurrently", r
    assert "changed" in r["error"] and "retry" in r["error"], r["error"]
    assert "nothing was changed" in r["error"].lower(), r["error"]
    meta = _meta(server, lid)
    assert meta.get("interfering_key") == "mid-call"
    assert meta.get("scaffolding_nudge") == "Watch for this pattern."
    assert _edit_count(server, lid, "lesson_field_updated") == 0


def test_S_ul_state_only_change_refused(env, monkeypatch):
    server = env
    lid, _ = _lesson(server)
    raw_before = _row(server, lid)["metadata"]
    st = _interfere_once(
        monkeypatch, server, "_metadata_dict_for_write", lid,
        lambda: _second_writer_sets_state(server, lid, is_current=0), 1)
    r = _update_lesson(server, lid)
    assert st["done"]
    assert r.get("refusal") == "changed_concurrently", r
    row = _row(server, lid)
    assert row["is_current"] == 0  # the second writer's state stands
    assert row["metadata"] == raw_before
    assert _edit_count(server, lid, "lesson_field_updated") == 0


def test_S_ul_succeeds_on_compact_json(env):
    server = env
    lid, _ = _lesson(server)
    _compact(server, lid)
    r = _update_lesson(server, lid)
    assert r.get("status") == "updated", r
    meta = _meta(server, lid)
    assert meta["scaffolding_nudge"] == "Watch harder."
    assert meta["zz_seed"] == 1 and meta["aa_seed"] == 2


# --------------------------------------------------------------------------
# B: engram_register_exemplar support_lost clear
# --------------------------------------------------------------------------

def _flagged_lesson_and_exemplar(server, compact=False):
    lid, _ = _lesson(server, "tgt")
    exm = _axiom(server, "exemplar")
    _raw_set(server, lid, json.dumps({**_meta(server, lid), "support_lost": True}))
    if compact:
        _compact(server, lid)
    return lid, exm


def _register(server, lid, exm):
    return _call(server.engram_register_exemplar, target_id=lid, exemplar_id=exm)


def test_E_re_transaction_state_around_the_commit(env, monkeypatch):
    server = env
    lid, exm = _flagged_lesson_and_exemplar(server)
    log = _probe_core(monkeypatch, server)
    r = _register(server, lid, exm)
    assert r.get("status") == "created", r
    read = _first(log, "SELECT id, type, claim, metadata, is_current FROM nodes", lid)
    ins = _first(log, "INSERT INTO edges", exm)
    upd = _first(log, "UPDATE nodes SET metadata", lid)
    assert read[2] is False  # the metadata read: no transaction open
    assert ins[2] is False   # the INSERT is the first DML: it opens the one the commit ends
    assert upd[2] is False   # the commit ended it: the UPDATE runs in a NEW transaction
    assert log.index(read) < log.index(ins) < log.index(upd)


def test_S_re_second_writer_metadata_skips_discharge(env, monkeypatch):
    server = env
    lid, exm = _flagged_lesson_and_exemplar(server)
    # _count_live_exemplars(conn, target_id, type) runs AFTER the edge commit
    # and BEFORE the guarded write: the window, with no transaction open.
    st = _interfere_once(
        monkeypatch, server, "_count_live_exemplars", lid,
        lambda: _second_writer_sets_key(server, lid, "interfering_key", "mid-call"), 1)
    r = _register(server, lid, exm)
    assert st["done"]
    assert r.get("status") == "created", r  # the edge IS committed
    assert r.get("support_lost_discharge") == "skipped_changed_concurrently", r
    assert "refusal" not in r  # success shape: not a failed call
    meta = _meta(server, lid)
    assert meta.get("interfering_key") == "mid-call"  # not overwritten by stale metadata
    assert meta.get("support_lost") is True           # discharge skipped, not faked
    assert _edit_count(server, lid, "support_lost_cleared") == 0


def test_S_re_state_only_change_skips_discharge(env, monkeypatch):
    server = env
    lid, exm = _flagged_lesson_and_exemplar(server)
    st = _interfere_once(
        monkeypatch, server, "_count_live_exemplars", lid,
        lambda: _second_writer_sets_state(server, lid, is_current=0), 1)
    r = _register(server, lid, exm)
    assert st["done"]
    assert r.get("status") == "created", r
    assert r.get("support_lost_discharge") == "skipped_changed_concurrently", r
    assert _row(server, lid)["is_current"] == 0
    assert _edit_count(server, lid, "support_lost_cleared") == 0


def test_S_re_succeeds_on_compact_json(env):
    server = env
    lid, exm = _flagged_lesson_and_exemplar(server, compact=True)
    r = _register(server, lid, exm)
    assert r.get("status") == "created", r
    assert "support_lost_discharge" not in r, r
    meta = _meta(server, lid)
    assert "support_lost" not in meta
    assert meta["zz_seed"] == 1 and meta["aa_seed"] == 2
    assert _edit_count(server, lid, "support_lost_cleared") == 1


# --------------------------------------------------------------------------
# C: core._apply_support_state, through its two callers
# --------------------------------------------------------------------------

def _cornerstone(server, ax):
    r = _call(server.engram_add_cornerstone, tag="cs-4178", title="Cornerstone 4178",
              new_frame="Principle for 4178.", supporting_ids=ax)
    assert "error" not in r, r
    return r["cornerstone_id"]


def _retract(server, nid):
    return json.loads(server._retract_impl(
        node_id=nid, error_type="hallucinated_claim", reason="fixture retraction",
        replacement_absent_because="NONE_BECAUSE_UNKNOWN",
        replacement_absent_note="test fixture -- replacement decision not under test"))


def test_E_rev_retract_transaction_open_at_the_read(env, monkeypatch):
    """C1, retract path: the node UPDATE precedes the cs/ls read in the same
    transaction -> EXEMPT."""
    server = env
    ax = _axiom(server, "sole")
    cs = _cornerstone(server, ax)
    log = _probe_core(monkeypatch, server)
    r = _retract(server, ax)
    assert "error" not in r, r
    read = _first(log, "SELECT id, type, claim, metadata, is_current FROM nodes", cs)
    assert read[2] is True


def test_E_rev_supersede_transaction_open_at_the_read(env, monkeypatch):
    """C1, supersede path -> EXEMPT."""
    server = env
    ax = _axiom(server, "sole")
    cs = _cornerstone(server, ax)
    ax2 = _axiom(server, "replacement")
    log = _probe_core(monkeypatch, server)
    r = json.loads(server._supersede_impl(old_node_id=ax, new_node_id=ax2,
                                          supersede_reason="fixture"))
    assert "error" not in r, r
    read = _first(log, "SELECT id, type, claim, metadata, is_current FROM nodes", cs)
    assert read[2] is True


def _kill(server, nid):
    _second_writer_sets_state(server, nid, is_current=0)


def _reflect(server):
    r = _call(server.engram_reflect)
    assert "error" not in r, r
    return r


def test_E_rf_no_transaction_at_the_read(env, monkeypatch):
    """C2: the reflect SELECT that feeds every row runs before any DML."""
    server = env
    lid, ax = _lesson(server)
    _kill(server, ax)
    log = _probe_core(monkeypatch, server)
    r = _reflect(server)
    assert [x["id"] for x in r["support_lost_marked"]] == [lid]
    read = _first(log, "SELECT id, type, claim, metadata, (metadata LIKE", None)
    assert read[2] is False
    first_dml = next(e for e in log if e[0].startswith(("UPDATE", "INSERT")))
    assert log.index(read) < log.index(first_dml)


@pytest.mark.parametrize("branch", ["mark", "clear"])
def test_S_rf_second_writer_metadata_skips(env, monkeypatch, branch):
    server = env
    lid, ax = _lesson(server)
    if branch == "mark":
        _kill(server, ax)  # zero live support, unflagged -> reflect would MARK
    else:
        _raw_set(server, lid, json.dumps({**_meta(server, lid), "support_lost": True}))
        # live support, flagged -> reflect would CLEAR
    before = _meta(server, lid)
    # reflect calls _count_live_exemplars for this node BEFORE its support-lost
    # SELECT (an earlier section), so that is not the window. The helper's own
    # _metadata_dict_for_write(raw, node_id, tool) runs after the read and
    # before the UPDATE, with no DML on the connection in between.
    st = _interfere_once(
        monkeypatch, server, "_metadata_dict_for_write", lid,
        lambda: _second_writer_sets_key(server, lid, "interfering_key", "mid-call"), 1)
    r = _reflect(server)
    assert st["done"]
    meta = _meta(server, lid)
    assert meta.get("interfering_key") == "mid-call"  # not overwritten by stale metadata
    assert meta.get("support_lost") == before.get("support_lost")  # skipped, not faked
    assert lid not in [x["id"] for x in r.get("support_lost_marked", [])]
    assert lid not in [x["id"] for x in r.get("support_lost_cleared", [])]
    assert _edit_count(server, lid, "support_lost") == 0
    assert _edit_count(server, lid, "support_lost_cleared") == 0


@pytest.mark.parametrize("branch", ["mark", "clear"])
def test_S_rf_state_only_change_skips(env, monkeypatch, branch):
    server = env
    lid, ax = _lesson(server)
    if branch == "mark":
        _kill(server, ax)
    else:
        _raw_set(server, lid, json.dumps({**_meta(server, lid), "support_lost": True}))
    raw_before = _row(server, lid)["metadata"]
    st = _interfere_once(
        monkeypatch, server, "_metadata_dict_for_write", lid,
        lambda: _second_writer_sets_state(server, lid, is_current=0), 1)
    r = _reflect(server)
    assert st["done"]
    row = _row(server, lid)
    assert row["is_current"] == 0
    assert row["metadata"] == raw_before
    assert lid not in [x["id"] for x in r.get("support_lost_marked", [])]
    assert lid not in [x["id"] for x in r.get("support_lost_cleared", [])]
    assert _edit_count(server, lid, "support_lost") == 0
    assert _edit_count(server, lid, "support_lost_cleared") == 0


def test_S_rf_mark_succeeds_on_compact_json(env):
    server = env
    lid, ax = _lesson(server)
    _compact(server, lid)
    _kill(server, ax)
    r = _reflect(server)
    assert [x["id"] for x in r["support_lost_marked"]] == [lid]
    meta = _meta(server, lid)
    assert meta["support_lost"] is True
    assert meta["zz_seed"] == 1 and meta["aa_seed"] == 2
    assert _edit_count(server, lid, "support_lost") == 1


def test_S_rf_clear_succeeds_on_compact_json(env):
    server = env
    lid, ax = _lesson(server)
    _compact(server, lid, support_lost=True)
    r = _reflect(server)
    assert [x["id"] for x in r["support_lost_cleared"]] == [lid]
    meta = _meta(server, lid)
    assert "support_lost" not in meta
    assert meta["zz_seed"] == 1 and meta["aa_seed"] == 2
    assert _edit_count(server, lid, "support_lost_cleared") == 1


def test_C_quarantine_bulk_update_is_not_in_the_class():
    """core ~4255 `UPDATE nodes SET metadata = NULL WHERE <invalid json>`: one
    set-based statement; no metadata value read feeds a write. Pinned so a
    future edit that starts reading metadata there is noticed."""
    import re
    src = (REPO / "src" / "engram" / "engram_core.py").read_text()
    assert re.search(r'UPDATE nodes SET metadata = NULL WHERE \{invalid_where\}', src)
    assert 'SELECT id, metadata, ? FROM nodes' in src  # the archive copy is SQL-to-SQL


# --------------------------------------------------------------------------
# E: tools/surgical.py metadata_patch -- EXEMPT by design (store-maint lock)
# --------------------------------------------------------------------------

def _load_surgical():
    sys.path.insert(0, str(REPO))
    spec = importlib.util.spec_from_file_location("surgical_4178", REPO / "tools" / "surgical.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_SG_E_read_has_no_transaction_so_exemption_rests_on_the_lock(env):
    server = env
    lid, _ = _lesson(server)
    sg = _load_surgical()
    assert str(sg.DB_PATH) == str(server.core.DB_PATH)
    log = []
    real = sg._get_db
    sg._get_db = lambda: Probe(real(), log)
    r = sg.metadata_patch("preview", lid, {"k": "v"}, "reason", "ob_fixture")
    assert r.get("phase") == "preview", r
    read = _first(log, "SELECT * FROM nodes WHERE id = ?", lid)
    assert read[2] is False  # the class definition WOULD cover this read: only the lock exempts it


def test_SG_lock_held_by_owner_refuses_before_any_store_access(env, capsys, monkeypatch):
    server = env
    lid, _ = _lesson(server)
    raw_before = _row(server, lid)["metadata"]
    sg = _load_surgical()
    core = sys.modules["engram_core"]
    fd = core.acquire_store_maint_lock(str(sg.DB_PATH), core.STORE_MAINT_OWNER_ROLE)
    try:
        monkeypatch.setattr(sys, "argv", [
            "surgical.py", "execute", "metadata_patch", "--node-id", lid,
            "--value", '{"k": "v"}', "--reason", "r", "--bug-ref", "ob_fixture",
            "--token", "x"])
        monkeypatch.setattr(sg, "metadata_patch",
                            lambda *a, **k: pytest.fail("ran under a held store lock"))
        with pytest.raises(SystemExit) as ex:
            sg.main()
        assert ex.value.code == core.STORE_MAINT_REFUSED_EXIT
    finally:
        core.release_store_maint_lock(fd)
    assert _row(server, lid)["metadata"] == raw_before
    assert "REFUSED" in capsys.readouterr().err


def test_SG_lock_is_held_while_metadata_patch_runs(env, monkeypatch):
    """The window between metadata_patch's read and write is inside the lock:
    from within the call, a second maintenance acquirer is refused."""
    server = env
    lid, _ = _lesson(server)
    sg = _load_surgical()
    core = sys.modules["engram_core"]
    seen = {}

    def stub(*a, **k):
        try:
            core.acquire_store_maint_lock(str(sg.DB_PATH), "second-tool")
            seen["refused"] = False
        except core.StoreMaintLockHeld:
            seen["refused"] = True
        return {"phase": "stub"}

    monkeypatch.setattr(sg, "metadata_patch", stub)
    monkeypatch.setattr(sys, "argv", [
        "surgical.py", "preview", "metadata_patch", "--node-id", lid,
        "--value", '{"k": "v"}', "--reason", "r", "--bug-ref", "ob_fixture"])
    sg.main()
    assert seen == {"refused": True}
