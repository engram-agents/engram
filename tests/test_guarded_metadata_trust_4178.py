"""Guarded read-then-write of nodes.metadata in engram_trust.py (engram-alpha #4178,
module PR 1 of 3, follow-up to #4166 / #4179).

Class (unchanged from #4166): a read of a node row whose metadata feeds a later
`UPDATE nodes SET ... metadata = ?` on the same id, unguarded, with NO write
transaction open on that connection at the read.

Site table (engram_trust.py at base 7045e979):
  U   _update_person_impl, main node read (SELECT ... trust_tier ...) -> metadata
      UPDATE.  IN class (E0u proves in_transaction is False at the read).
  UC  _update_person_impl, cascade read of each citing node.  EXEMPT: the main
      UPDATE and _log_edit already opened the write transaction and nothing
      commits before the cascade (E2uc proves in_transaction is True).
  M   _add_special_moment_impl.  IN class (E0m).
  L   _set_person_lineage_impl, both read forms (explicit target_pn, and the
      self-anchor lookup).  IN class (E0l, E0ls).
  (_set_trust_tier_impl writes only the trust_tier column, never metadata: not
   in the class.  _backfill_special_moment_about_edges reads metadata but only
   INSERTs edges: not in the class.)

Contract (written before the fix; the IN-class cells are red on the base):
  X1  second writer changes ONLY the person's metadata between read and write
      -> refusal `changed_concurrently`, the second writer's key survives, our
      keys are absent, nothing logged, no cascade flags written.
  X1b second writer changes ONLY is_current -> refusal, metadata byte-identical.
  X1c metadata stored in compact JSON, no second writer -> the call succeeds
      (guard binds the RAW column value, not a re-serialized dict).
Each of X in {update_person (U), add_special_moment (M), set_person_lineage (L)}.
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
    d = tempfile.mkdtemp(prefix="engram_test_guarded_meta_4178_")
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


def _obs(server, tag):
    r = _call(
        server.engram_add_observation,
        url=f"https://example.com/{tag}", title=f"Src {tag}",
        quoted_text=f"{tag} verbatim quote text.",
        interpretation=f"{tag} interpretation text here.",
        claim=f"{tag} granite quarry output rose in spring.",
        quote_type="hard_data",
    )
    assert "observation_id" in r, r
    return r["observation_id"]


def _person(server, name, **extra):
    r = _call(server.engram_add_person, name=name, **extra)
    return r.get("person_id") or r.get("node_id") or r["id"]


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
    conn = server.core._get_db()
    try:
        m = json.loads(conn.execute(
            "SELECT metadata FROM nodes WHERE id = ?", (nid,)).fetchone()["metadata"])
        raw = json.dumps(m, separators=(",", ":"))
        assert raw != json.dumps(m), "fixture must differ from default spelling"
        conn.execute("UPDATE nodes SET metadata = ? WHERE id = ?", (raw, nid))
        conn.commit()
        return raw
    finally:
        conn.close()


def _event_count(server, action, nid):
    conn = server.core._get_db()
    try:
        return conn.execute(
            "SELECT COUNT(*) AS n FROM edit_history WHERE action = ? AND node_id = ?",
            (action, nid)).fetchone()["n"]
    finally:
        conn.close()


def _interfere_once(monkeypatch, server, nid, action):
    """Run `action()` (a second writer) the first time core._metadata_dict_for_write
    is called for `nid`: every site calls it after its read and before its write,
    with no DML on the call's own connection in between."""
    state = {"done": False}
    real = server.core._metadata_dict_for_write

    def interfering(raw, node_id, *a, **k):
        if not state["done"] and node_id == nid:
            state["done"] = True
            action()
        return real(raw, node_id, *a, **k)

    monkeypatch.setattr(server.core, "_metadata_dict_for_write", interfering)
    return state


# --- the three tools ----------------------------------------------------------

def _call_update_person(server, pn):
    return _call(server.engram_update_person, node_id=pn,
                 updates={"relation": "granite surveyor"}, reason="test")


def _call_special_moment(server, pn, anchor):
    return _call(server.engram_add_special_moment, person_id=pn,
                 description="a quiet quarry morning", node_id=anchor)


def _call_lineage(server, pn):
    return _call(server.engram_set_person_lineage, target_pn=pn,
                 model_id="claude-sonnet-4-6")


def _setup(server, which):
    """Returns (pn, invoke) where invoke() makes the tool call."""
    pn = _person(server, "Zorblax Quentin")
    if which == "U":
        return pn, lambda: _call_update_person(server, pn)
    if which == "M":
        anchor = _obs(server, "anchor")
        return pn, lambda: _call_special_moment(server, pn, anchor)
    return pn, lambda: _call_lineage(server, pn)


OURS = {"U": "relation", "M": "special_moments", "L": "model_family"}
LOGGED = {"U": "person_field_updated", "M": "special_moment_added", "L": None}


# --------------------------------------------------------------------------
# IN-class sites (red on the base)
# --------------------------------------------------------------------------

@pytest.mark.parametrize("which", ["U", "M", "L"])
def test_X1_second_writer_between_read_and_write_refused(env, monkeypatch, which):
    server = env
    pn, invoke = _setup(server, which)
    state = _interfere_once(
        monkeypatch, server, pn,
        lambda: _second_writer_sets_key(server, pn, "interfering_key", "written mid-call"))
    r = invoke()
    assert state["done"]
    assert r.get("refusal") == "changed_concurrently", r
    assert "changed" in r["error"] and "retry" in r["error"], r["error"]
    assert "nothing was changed" in r["error"].lower(), r["error"]
    meta = _meta(server, pn)
    assert meta.get("interfering_key") == "written mid-call"
    # person nodes are seeded with empty field_traces / special_moments, so
    # "ours is absent" means: no trace entry / no moment entry / no lineage key.
    if which == "U":
        assert "relation" not in meta.get("field_traces", {})
        assert meta.get("relation") != "granite surveyor"
    elif which == "M":
        assert not meta.get("special_moments")
    else:
        assert "model_family" not in meta
    if LOGGED[which]:
        assert _event_count(server, LOGGED[which], pn) == 0


def test_X1_update_person_refusal_writes_no_cascade_flags(env, monkeypatch):
    server = env
    pn = _person(server, "Zorblax Quentin")
    ob = _obs(server, "citer")
    assert "error" not in _call(server.engram_link_about, node_id=ob, person_id=pn)
    before = _row(server, ob)["metadata"]
    _interfere_once(monkeypatch, server, pn,
                    lambda: _second_writer_sets_key(server, pn, "k", "v"))
    r = _call_update_person(server, pn)
    assert r.get("refusal") == "changed_concurrently", r
    assert _row(server, ob)["metadata"] == before


@pytest.mark.parametrize("which", ["U", "M", "L"])
def test_X1b_state_only_change_refused(env, monkeypatch, which):
    server = env
    pn, invoke = _setup(server, which)
    raw_before = _row(server, pn)["metadata"]
    state = _interfere_once(
        monkeypatch, server, pn,
        lambda: _second_writer_sets_state(server, pn, is_current=0))
    r = invoke()
    assert state["done"]
    assert r.get("refusal") == "changed_concurrently", r
    row = _row(server, pn)
    assert row["metadata"] == raw_before
    assert row["is_current"] == 0
    if LOGGED[which]:
        assert _event_count(server, LOGGED[which], pn) == 0


@pytest.mark.parametrize("which", ["U", "M", "L"])
def test_X1c_succeeds_on_compact_json_metadata(env, which):
    server = env
    pn, invoke = _setup(server, which)
    _store_compact_metadata(server, pn)
    r = invoke()
    assert "error" not in r and "refusal" not in r, r
    assert OURS[which] in _meta(server, pn)


def test_X1c_lineage_self_anchor_path_succeeds_on_compact_json(env):
    server = env
    pn = _person(server, "Sol Self", is_self=True)
    _store_compact_metadata(server, pn)
    r = _call(server.engram_set_person_lineage, model_id="claude-sonnet-4-6")
    assert "error" not in r and "refusal" not in r, r
    assert _meta(server, pn)["model_family"] == "claude-sonnet"


def test_X1_lineage_self_anchor_path_second_writer_refused(env, monkeypatch):
    server = env
    pn = _person(server, "Sol Self", is_self=True)
    state = _interfere_once(
        monkeypatch, server, pn,
        lambda: _second_writer_sets_key(server, pn, "interfering_key", "mid"))
    r = _call(server.engram_set_person_lineage, model_id="claude-sonnet-4-6")
    assert state["done"]
    assert r.get("refusal") == "changed_concurrently", r
    assert "model_family" not in _meta(server, pn)


# --------------------------------------------------------------------------
# in_transaction proxy cells: controls (False) and the EXEMPT site (True)
# --------------------------------------------------------------------------

class _Probe:
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


_Q_U = "SELECT id, type, is_current, metadata, trust_tier FROM nodes WHERE id = ?"
_Q_UC = "SELECT id, type, metadata FROM nodes WHERE id = ? AND is_current = 1"
_Q_M = "SELECT id, type, is_current, metadata FROM nodes WHERE id = ?"
_Q_L = "SELECT id, metadata FROM nodes WHERE id=? AND type='person' AND is_current=1"
_Q_LS = ("SELECT id, metadata FROM nodes WHERE type='person' AND json_extract("
         "CASE WHEN json_valid(metadata) THEN metadata END, '$.is_self')=1 AND is_current=1")


def test_E0u_control_update_person_main_read_outside_a_transaction(env, monkeypatch):
    server = env
    pn = _person(server, "Zorblax Quentin")
    log = _install_probe(monkeypatch, server, {_Q_U})
    r = _call_update_person(server, pn)
    assert "error" not in r, r
    assert log == [False], log


def test_E0m_control_special_moment_read_outside_a_transaction(env, monkeypatch):
    server = env
    pn = _person(server, "Zorblax Quentin")
    anchor = _obs(server, "anchor")
    log = _install_probe(monkeypatch, server, {_Q_M})
    r = _call_special_moment(server, pn, anchor)
    assert "error" not in r, r
    assert log == [False], log


def test_E0l_control_lineage_explicit_target_read_outside_a_transaction(env, monkeypatch):
    server = env
    pn = _person(server, "Zorblax Quentin")
    log = _install_probe(monkeypatch, server, {_Q_L})
    r = _call_lineage(server, pn)
    assert "error" not in r, r
    assert log == [False], log


def test_E0ls_control_lineage_self_anchor_read_outside_a_transaction(env, monkeypatch):
    server = env
    _person(server, "Sol Self", is_self=True)
    log = _install_probe(monkeypatch, server, {_Q_LS})
    r = _call(server.engram_set_person_lineage, model_id="claude-sonnet-4-6")
    assert "error" not in r, r
    assert log == [False], log


def test_E2uc_update_person_cascade_read_inside_open_transaction(env, monkeypatch):
    server = env
    pn = _person(server, "Zorblax Quentin")
    ob = _obs(server, "citer")
    assert "error" not in _call(server.engram_link_about, node_id=ob, person_id=pn)
    log = _install_probe(monkeypatch, server, {_Q_UC})
    r = _call_update_person(server, pn)
    assert r.get("cascade_flagged_nodes") == [ob], r
    assert log == [True], log
