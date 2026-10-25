"""Guarded read-then-write of nodes.metadata in engram_tasks (engram-alpha #4178,
PR 2 of 3; follow-up to #4166 / #4179).

The class (unchanged from #4166): a read of a node row whose metadata feeds a
later `UPDATE nodes SET ... metadata = ?` on the same id, unguarded, with NO
write transaction open on that connection at the read. sqlite3 (legacy
isolation) opens its transaction at the first DML, so a read AFTER an earlier
INSERT/UPDATE on the same connection is protected.

Sites in src/engram/engram_tasks.py (base 7045e979):
  A  _add_task_impl parent auto-promote (~338): EXEMPT. The node INSERT (~277)
     opened the transaction before the parent read (~316). Proven by E_add
     (in_transaction True at the read).
  B  _update_task_impl (~519, multi-line multi-column UPDATE): IN.
  C  _deactivate_goal_impl (~888): IN.
  D  _activate_goal_impl (~953): IN.

Contract (written before the fix; IN-class cells are red on the base):
  S_x   a second writer changes ONLY the node's metadata between the read and
        the write -> refusal `changed_concurrently` with the tool's own
        `error`; nothing changed by the call; the second writer's key survives.
  S_xb  a second writer changes ONLY state (B: status; C/D: is_current),
        metadata byte-identical -> refusal; the state clause earns its keep.
  S_xc  stored metadata in compact JSON spelling, seeded non-empty, no second
        writer -> the call SUCCEEDS (the guard binds the RAW value read).
  E_*   `conn.in_transaction` proxy at each read: E0_* controls read False
        (so the probe can say False), E_add reads True (exempt).
Interference points are chosen so no DML has run on the call's connection:
B, C patch core._metadata_dict_for_write (called after the read, before the
write); D patches core._metadata_dict (the read-side helper it calls there).
"""

import json
import os
import shutil
import sys
import tempfile

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


@pytest.fixture
def env():
    d = tempfile.mkdtemp(prefix="engram_test_guarded_meta_4178_tasks_")
    for key in list(sys.modules):
        if key == "server" or key.startswith("engram_"):
            del sys.modules[key]
    import server
    server._configure_paths(d)
    server._ensure_data_dir()
    os.environ["ENGRAM_NO_EMBEDDINGS"] = "1"
    yield server
    shutil.rmtree(d, ignore_errors=True)


def _call(fn, **p):
    return json.loads(fn(payload_json=json.dumps(p)))


def _task(server, desc="Tidy the granite quarry ledger.", **extra):
    r = _call(server.engram_add_task, description=desc, **extra)
    assert "task_id" in r, r
    return r["task_id"]


def _goal(server, claim="Keep the quarry ledger honest."):
    r = json.loads(server.engram_add_goal(json.dumps(
        {"claim": claim, "motivation": "testing"})))
    assert "goal_id" in r, r
    return r["goal_id"]


def _row(server, nid):
    conn = server.core._get_db()
    try:
        return dict(conn.execute("SELECT * FROM nodes WHERE id = ?", (nid,)).fetchone())
    finally:
        conn.close()


def _meta(server, nid):
    return json.loads(_row(server, nid)["metadata"])


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
    assert set(cols) <= {"is_current", "status"}
    conn = server.core._get_db()
    try:
        for col, val in cols.items():
            conn.execute(f"UPDATE nodes SET {col} = ? WHERE id = ?", (val, nid))
        conn.commit()
    finally:
        conn.close()


def _store_compact_metadata(server, nid):
    """Rewrite `nid`'s metadata as compact JSON with non-empty seeded keys
    (same content, different spelling). Returns the raw stored string."""
    conn = server.core._get_db()
    try:
        m = json.loads(conn.execute(
            "SELECT metadata FROM nodes WHERE id = ?", (nid,)).fetchone()["metadata"])
        m.setdefault("zz_seed", 1)
        m.setdefault("aa_seed", 2)
        raw = json.dumps(m, separators=(",", ":"))
        assert raw != json.dumps(m), "fixture must differ from default spelling"
        conn.execute("UPDATE nodes SET metadata = ? WHERE id = ?", (raw, nid))
        conn.commit()
        return raw
    finally:
        conn.close()


def _edit_count(server, nid, like):
    conn = server.core._get_db()
    try:
        return conn.execute(
            "SELECT COUNT(*) AS n FROM edit_history WHERE node_id = ? AND action LIKE ?",
            (nid, like)).fetchone()["n"]
    finally:
        conn.close()


def _interfere_once(monkeypatch, server, attr, nid, action, node_arg_index=None):
    """Patch core.<attr> so the FIRST relevant call runs `action()` (a second
    writer) before delegating. node_arg_index: positional index of the node id
    arg to match on (None = fire on the first call)."""
    state = {"done": False}
    real = getattr(server.core, attr)

    def interfering(*a, **k):
        match = node_arg_index is None or (
            len(a) > node_arg_index and a[node_arg_index] == nid)
        if not state["done"] and match:
            state["done"] = True
            action()
        return real(*a, **k)

    monkeypatch.setattr(server.core, attr, interfering)
    return state


# --------------------------------------------------------------------------
# B: engram_update_task
# --------------------------------------------------------------------------

def test_S_update_task_second_writer_metadata_refused(env, monkeypatch):
    server = env
    tid = _task(server)
    st = _interfere_once(
        monkeypatch, server, "_metadata_dict_for_write", tid,
        lambda: _second_writer_sets_key(server, tid, "interfering_key", "mid-call"), 1)
    r = _call(server.engram_update_task, task_id=tid, new_status="active", note="go")
    assert st["done"]
    assert r.get("refusal") == "changed_concurrently", r
    assert "changed" in r["error"] and "retry" in r["error"], r["error"]
    assert "nothing was changed" in r["error"].lower(), r["error"]
    row = _row(server, tid)
    assert row["status"] == "planned"
    meta = json.loads(row["metadata"])
    assert meta.get("interfering_key") == "mid-call"
    assert "status_history" not in meta
    assert _edit_count(server, tid, "status_%") == 0


def test_S_update_task_state_only_change_refused(env, monkeypatch):
    server = env
    tid = _task(server)
    raw_before = _row(server, tid)["metadata"]
    st = _interfere_once(
        monkeypatch, server, "_metadata_dict_for_write", tid,
        lambda: _second_writer_sets_state(server, tid, status="blocked"), 1)
    r = _call(server.engram_update_task, task_id=tid, new_status="done", note="x")
    assert st["done"]
    assert r.get("refusal") == "changed_concurrently", r
    row = _row(server, tid)
    assert row["status"] == "blocked"  # the second writer's state stands
    assert row["metadata"] == raw_before
    assert _edit_count(server, tid, "status_%") == 0


def test_S_update_task_succeeds_on_compact_json(env):
    server = env
    tid = _task(server)
    _store_compact_metadata(server, tid)
    r = _call(server.engram_update_task, task_id=tid, new_status="active", note="go")
    assert "error" not in r and "refusal" not in r, r
    row = _row(server, tid)
    assert row["status"] == "active"
    meta = json.loads(row["metadata"])
    assert meta["status_history"][0]["note"] == "go"
    assert meta["zz_seed"] == 1 and meta["aa_seed"] == 2
    assert _edit_count(server, tid, "status_planned_to_active") == 1


# --------------------------------------------------------------------------
# C: engram_deactivate_goal
# --------------------------------------------------------------------------

def test_S_deactivate_goal_second_writer_metadata_refused(env, monkeypatch):
    server = env
    gid = _goal(server)
    st = _interfere_once(
        monkeypatch, server, "_metadata_dict_for_write", gid,
        lambda: _second_writer_sets_key(server, gid, "interfering_key", "mid-call"), 1)
    r = _call(server.engram_deactivate_goal, goal_id=gid, reason="pause")
    assert st["done"]
    assert r.get("refusal") == "changed_concurrently", r
    assert "changed" in r["error"] and "retry" in r["error"], r["error"]
    meta = _meta(server, gid)
    assert meta.get("interfering_key") == "mid-call"
    assert "lifecycle_state" not in meta and "deactivated_at" not in meta


def test_S_deactivate_goal_state_only_change_refused(env, monkeypatch):
    server = env
    gid = _goal(server)
    raw_before = _row(server, gid)["metadata"]
    st = _interfere_once(
        monkeypatch, server, "_metadata_dict_for_write", gid,
        lambda: _second_writer_sets_state(server, gid, is_current=0), 1)
    r = _call(server.engram_deactivate_goal, goal_id=gid, reason="pause")
    assert st["done"]
    assert r.get("refusal") == "changed_concurrently", r
    row = _row(server, gid)
    assert row["is_current"] == 0
    assert row["metadata"] == raw_before


def test_S_deactivate_goal_succeeds_on_compact_json(env):
    server = env
    gid = _goal(server)
    _store_compact_metadata(server, gid)
    r = _call(server.engram_deactivate_goal, goal_id=gid, reason="pause")
    assert r.get("status") == "deactivated" and "refusal" not in r, r
    meta = _meta(server, gid)
    assert meta["lifecycle_state"] == "paused"
    assert meta["zz_seed"] == 1 and meta["aa_seed"] == 2


# --------------------------------------------------------------------------
# D: engram_activate_goal
# --------------------------------------------------------------------------

def _paused_goal(server):
    gid = _goal(server)
    r = _call(server.engram_deactivate_goal, goal_id=gid, reason="pause")
    assert r.get("status") == "deactivated", r
    return gid


def test_S_activate_goal_second_writer_metadata_refused(env, monkeypatch):
    server = env
    gid = _paused_goal(server)
    st = _interfere_once(
        monkeypatch, server, "_metadata_dict", gid,
        lambda: _second_writer_sets_key(server, gid, "interfering_key", "mid-call"))
    r = _call(server.engram_activate_goal, goal_id=gid, reason="resume")
    assert st["done"]
    assert r.get("refusal") == "changed_concurrently", r
    assert "changed" in r["error"] and "retry" in r["error"], r["error"]
    meta = _meta(server, gid)
    assert meta.get("interfering_key") == "mid-call"
    assert meta["lifecycle_state"] == "paused" and "reactivated_at" not in meta


def test_S_activate_goal_state_only_change_refused(env, monkeypatch):
    server = env
    gid = _paused_goal(server)
    raw_before = _row(server, gid)["metadata"]
    st = _interfere_once(
        monkeypatch, server, "_metadata_dict", gid,
        lambda: _second_writer_sets_state(server, gid, is_current=0))
    r = _call(server.engram_activate_goal, goal_id=gid, reason="resume")
    assert st["done"]
    assert r.get("refusal") == "changed_concurrently", r
    row = _row(server, gid)
    assert row["is_current"] == 0
    assert row["metadata"] == raw_before


def test_S_activate_goal_succeeds_on_compact_json(env):
    server = env
    gid = _paused_goal(server)
    _store_compact_metadata(server, gid)
    r = _call(server.engram_activate_goal, goal_id=gid, reason="resume")
    assert r.get("status") == "activated" and "refusal" not in r, r
    meta = _meta(server, gid)
    assert meta["lifecycle_state"] == "active"
    assert meta["zz_seed"] == 1 and meta["aa_seed"] == 2


# --------------------------------------------------------------------------
# in_transaction proxy cells (one per site)
# --------------------------------------------------------------------------

class _Probe:
    """Records `in_transaction` just before each watched SQL text executes."""

    def __init__(self, conn, watched, log):
        self._c, self._watched, self._log = conn, watched, log

    def execute(self, sql, *args):
        norm = " ".join(sql.split())
        if norm in self._watched:
            self._log.append(self._c.in_transaction)
        return self._c.execute(sql, *args)

    def __getattr__(self, name):
        return getattr(self._c, name)


def _install_probe(monkeypatch, server, watched):
    log = []
    real = server.core._get_db
    monkeypatch.setattr(
        server.core, "_get_db",
        lambda *a, **k: _Probe(real(*a, **k), watched, log))
    return log


_ROW4 = "SELECT id, type, is_current, metadata FROM nodes WHERE id = ?"


def test_E0_control_update_task_read_outside_transaction(env, monkeypatch):
    server = env
    tid = _task(server)
    log = _install_probe(monkeypatch, server, {"SELECT * FROM nodes WHERE id = ?"})
    r = _call(server.engram_update_task, task_id=tid, new_status="active")
    assert r.get("status") == "updated", r
    assert log == [False], log


def test_E0_control_deactivate_goal_read_outside_transaction(env, monkeypatch):
    server = env
    gid = _goal(server)
    log = _install_probe(monkeypatch, server, {_ROW4})
    r = _call(server.engram_deactivate_goal, goal_id=gid)
    assert r.get("status") == "deactivated", r
    assert log == [False], log


def test_E0_control_activate_goal_read_outside_transaction(env, monkeypatch):
    server = env
    gid = _paused_goal(server)
    log = _install_probe(monkeypatch, server, {_ROW4})
    r = _call(server.engram_activate_goal, goal_id=gid)
    assert r.get("status") == "activated", r
    assert log == [False], log


def test_E_add_task_parent_read_inside_open_transaction(env, monkeypatch):
    """Site A is EXEMPT: the new task's INSERT precedes the parent read."""
    server = env
    parent = _task(server, "Parent task for the ledger.")
    log = _install_probe(monkeypatch, server, {_ROW4})
    r = _call(server.engram_add_task, description="Child task.", parent_task_id=parent)
    assert r.get("parent_task") == parent, r
    assert log == [True], log
    assert _meta(server, parent).get("scope") == "milestone"  # promotion still works
