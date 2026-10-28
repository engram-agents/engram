"""Guarded read-then-write of nodes.metadata (engram-alpha #4166).

The class: a `SELECT ... FROM nodes WHERE id = ?` reads a row whose metadata
feeds a later `UPDATE nodes SET ... metadata = ?` on the same id, the UPDATE is
unguarded, AND no write transaction is open on the connection at the read.
Python's sqlite3 (legacy isolation) opens its transaction at the first DML
statement, so a read AFTER an earlier INSERT/UPDATE/DELETE on the same
connection is protected (the connection holds the RESERVED lock).

Contract (written before the fix; the IN-class cells are red on the base):
  S1  engram_retract, main node: a second writer changes ONLY the target's
      metadata between the read and the retract write -> the call refuses with
      refusal `changed_concurrently`; status / is_current / metadata stay as
      the second writer left them; the second writer's key survives; no
      'retracted' event is logged.
  S4  engram_add_observation, evidence-node passthrough (evidence_type /
      original_url): a second writer changes ONLY the evidence node's metadata
      between its read and the id allocation that opens its transaction -> refusal
      `changed_concurrently`; the observation insert is rolled back too; the
      second writer's key survives and the passthrough key is absent.
  E2  engram_retract, paired-prediction branch: EXEMPT. Proven by capturing
      `conn.in_transaction` at the prediction read (must be True).
  E3  engram_supersede, self-anchor transfer: EXEMPT. Same proof.
  E0  control for the E-cells: the SAME probe at the retract main-node read
      (site S1) reads False, so the probe can say "no transaction open" and
      the E-cells' True is a measurement, not a constant.
Proof style for the E-cells: a thin proxy over the connection records
`in_transaction` immediately before the watched SELECT executes. Chosen over a
second-connection blocking write because it is deterministic (no lock
timeouts) and reads the exact state the exemption depends on.
"""

import json
import os
import shutil
import sys
import tempfile

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_RETRACT = dict(error_type="other", reason="test retraction",
                replacement_absent_because="NONE_BECAUSE_NO_SUCCESSOR")


@pytest.fixture
def env():
    d = tempfile.mkdtemp(prefix="engram_test_guarded_meta_4166_")
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


def _obs(server, tag, **extra):
    r = _call(
        server.engram_add_observation,
        url=f"https://example.com/{tag}", title=f"Src {tag}",
        quoted_text=f"{tag} verbatim quote text.",
        interpretation=f"{tag} interpretation text here.",
        claim=f"{tag} granite quarry output rose in spring.",
        quote_type="hard_data", **extra,
    )
    assert "observation_id" in r, r
    return r["observation_id"]


def _person(server, name):
    r = _call(server.engram_add_person, name=name)
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
    """A second connection commits ONE metadata key on `nid` and nothing else."""
    conn = server.core._get_db()
    try:
        m = json.loads(conn.execute(
            "SELECT metadata FROM nodes WHERE id = ?", (nid,)).fetchone()["metadata"])
        m[key] = value
        conn.execute("UPDATE nodes SET metadata = ? WHERE id = ?", (json.dumps(m), nid))
        conn.commit()
    finally:
        conn.close()


def _store_compact_metadata(server, nid):
    """Rewrite `nid`'s stored metadata as compact JSON (same content, different
    spelling from default json.dumps). Returns the raw stored string."""
    conn = server.core._get_db()
    try:
        m = json.loads(conn.execute(
            "SELECT metadata FROM nodes WHERE id = ?", (nid,)).fetchone()["metadata"])
        # Evidence metadata starts as {}; seed keys so the spelling can differ.
        m.setdefault("zz_seed", 1)
        m.setdefault("aa_seed", 2)
        raw = json.dumps(m, separators=(",", ":"))
        assert raw != json.dumps(m), "fixture must differ from default spelling"
        conn.execute("UPDATE nodes SET metadata = ? WHERE id = ?", (raw, nid))
        conn.commit()
        return raw
    finally:
        conn.close()


def _second_writer_sets_state(server, nid, **cols):
    """A second connection commits ONLY state columns (metadata untouched)."""
    assert set(cols) <= {"is_current", "status"}
    conn = server.core._get_db()
    try:
        for col, val in cols.items():
            conn.execute(f"UPDATE nodes SET {col} = ? WHERE id = ?", (val, nid))
        conn.commit()
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


# --------------------------------------------------------------------------
# IN-class sites (red on the base)
# --------------------------------------------------------------------------

def test_S1_retract_main_node_second_writer_between_read_and_write(env, monkeypatch):
    server = env
    nid = _obs(server, "s1")
    status_before = _row(server, nid)["status"]
    state = {"done": False}
    real = server.core._pending_markers_for_node

    # `_retract_impl` calls core._pending_markers_for_node after reading the
    # node (SELECT *) and before the retract UPDATE; no DML has run on its
    # connection by then. The second writer lands inside that window.
    def interfering(conn, node_id):
        if not state["done"] and node_id == nid:
            state["done"] = True
            _second_writer_sets_key(server, nid, "interfering_key", "written mid-call")
        return real(conn, node_id)

    monkeypatch.setattr(server.core, "_pending_markers_for_node", interfering)
    r = _call(server.engram_retract, node_id=nid, **_RETRACT)
    assert state["done"]
    assert r.get("refusal") == "changed_concurrently", r
    assert "changed" in r["error"] and "retry" in r["error"], r["error"]
    assert "nothing was changed" in r["error"].lower(), r["error"]
    row = _row(server, nid)
    assert row["status"] != "retracted" and row["is_current"] == 1
    meta = json.loads(row["metadata"])
    assert meta.get("interfering_key") == "written mid-call"
    assert "retracted" not in meta and "retraction_reason" not in meta
    assert row["status"] == status_before  # status untouched by the refused call
    assert _event_count(server, "retracted", nid) == 0


@pytest.mark.parametrize("cols", [{"is_current": 0}, {"status": "retracted"}],
                         ids=["is_current_0", "status_retracted"])
def test_S1b_retract_state_only_change_refused(env, monkeypatch, cols):
    """Second writer changes ONLY state (metadata byte-identical): the guard's
    `is_current = 1 AND COALESCE(status,'') != 'retracted'` clauses must refuse."""
    server = env
    nid = _obs(server, "s1b")
    raw_before = _row(server, nid)["metadata"]
    state = {"done": False}
    real = server.core._pending_markers_for_node

    def interfering(conn, node_id):
        if not state["done"] and node_id == nid:
            state["done"] = True
            _second_writer_sets_state(server, nid, **cols)
        return real(conn, node_id)

    monkeypatch.setattr(server.core, "_pending_markers_for_node", interfering)
    r = _call(server.engram_retract, node_id=nid, **_RETRACT)
    assert state["done"]
    assert r.get("refusal") == "changed_concurrently", r
    row = _row(server, nid)
    assert row["metadata"] == raw_before  # no retract metadata written
    for col, val in cols.items():
        assert row[col] == val  # the second writer's state stands
    assert _event_count(server, "retracted", nid) == 0


def test_S1c_retract_succeeds_on_compact_json_metadata(env):
    """Raw-value binding: stored metadata spelled differently from default
    json.dumps must still retract when nothing changed concurrently."""
    server = env
    nid = _obs(server, "s1c")
    _store_compact_metadata(server, nid)
    r = _call(server.engram_retract, node_id=nid, **_RETRACT)
    assert "error" not in r and "refusal" not in r, r
    row = _row(server, nid)
    assert row["status"] == "retracted" and row["is_current"] == 0
    assert json.loads(row["metadata"]).get("retracted") is True


def test_S4_observation_evidence_passthrough_second_writer_refused(env, monkeypatch):
    server = env
    _obs(server, "s4")  # creates the evidence node for this URL
    conn = server.core._get_db()
    try:
        ev_id = conn.execute(
            "SELECT id FROM nodes WHERE type = 'evidence'").fetchone()["id"]
        n_obs_before = conn.execute(
            "SELECT COUNT(*) AS n FROM nodes WHERE type = 'observation'").fetchone()["n"]
    finally:
        conn.close()
    status_before = _row(server, ev_id)["status"]
    state = {"done": False}
    real = server.core._similar_existing_matches

    # `_add_observation_impl` reads the evidence row first; its first DML is
    # the id allocation (core._next_id -> INSERT INTO sequences), which opens
    # the write transaction. core._similar_existing_matches runs BEFORE that,
    # so the evidence read is still outside any transaction there, and the
    # second writer lands inside the unprotected window.
    def interfering(*a, **k):
        if not state["done"]:
            state["done"] = True
            _second_writer_sets_key(server, ev_id, "interfering_key", "written mid-call")
        return real(*a, **k)

    monkeypatch.setattr(server.core, "_similar_existing_matches", interfering)
    r = _call(
        server.engram_add_observation,
        url="https://example.com/s4", title="Src s4",
        quoted_text="s4 second verbatim quote text.",
        interpretation="s4 second interpretation text here.",
        claim="s4 granite quarry output fell in autumn.",
        quote_type="hard_data", evidence_type="web_page",
    )
    assert state["done"]
    assert r.get("refusal") == "changed_concurrently", r
    assert "changed" in r["error"] and "retry" in r["error"], r["error"]
    meta = _meta(server, ev_id)
    assert meta.get("interfering_key") == "written mid-call"
    assert "evidence_type" not in meta
    assert "retracted" not in meta
    assert _row(server, ev_id)["status"] == status_before
    conn = server.core._get_db()
    try:
        n_obs_after = conn.execute(
            "SELECT COUNT(*) AS n FROM nodes WHERE type = 'observation'").fetchone()["n"]
    finally:
        conn.close()
    assert n_obs_after == n_obs_before  # the observation insert rolled back too


def test_S4c_observation_evidence_passthrough_succeeds_on_compact_json(env):
    """Raw-value binding at the passthrough guard: compact-spelled evidence
    metadata, no second writer -> the passthrough must be written."""
    server = env
    _obs(server, "s4c")
    conn = server.core._get_db()
    try:
        ev_id = conn.execute(
            "SELECT id FROM nodes WHERE type = 'evidence'").fetchone()["id"]
    finally:
        conn.close()
    _store_compact_metadata(server, ev_id)
    r = _call(
        server.engram_add_observation,
        url="https://example.com/s4c", title="Src s4c",
        quoted_text="s4c second verbatim quote text.",
        interpretation="s4c second interpretation text here.",
        claim="s4c granite quarry output fell in autumn.",
        quote_type="hard_data", evidence_type="web_page",
    )
    assert "observation_id" in r and "refusal" not in r, r
    assert _meta(server, ev_id).get("evidence_type") == "web_page"


# --------------------------------------------------------------------------
# EXEMPT sites: a write transaction is already open at the read
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


def test_E0_control_retract_main_read_is_outside_a_transaction(env, monkeypatch):
    server = env
    nid = _obs(server, "e0")
    log = _install_probe(monkeypatch, server, {"SELECT * FROM nodes WHERE id = ?"})
    r = _call(server.engram_retract, node_id=nid, **_RETRACT)
    assert "error" not in r, r
    assert log and log[0] is False, log  # first match is the main-node read


def test_E0b_control_observation_evidence_read_is_outside_a_transaction(env, monkeypatch):
    server = env
    _obs(server, "e0b")
    log = _install_probe(
        monkeypatch, server,
        {"SELECT id, source_url, metadata FROM nodes WHERE id = ? AND type = 'evidence'"})
    _obs(server, "e0b", evidence_type="web_page")
    assert log == [False], log


def test_E2_retract_paired_prediction_read_inside_open_transaction(env, monkeypatch):
    server = env
    r0 = _call(
        server.engram_add_observation,
        url="https://example.com/e2", title="Src e2",
        quoted_text="Model signals an upcoming event.",
        interpretation="Signal predicts event.", claim="Event will occur.",
        quote_type="attributed_analysis", is_predictive=True,
        predicted_event="Event occurs within 60 days.",
    )
    assert "error" not in r0, r0
    oid, pid = r0["observation_id"], r0["prediction_id"]
    log = _install_probe(
        monkeypatch, server,
        {"SELECT id, status, metadata, is_current FROM nodes WHERE id = ?"})
    r = _call(server.engram_retract, node_id=oid, **_RETRACT)
    assert [x["id"] for x in r["retracted_predictions"]] == [pid], r
    assert log == [True], log


def test_E3_supersede_self_anchor_transfer_read_inside_open_transaction(env, monkeypatch):
    server = env
    old = _person(server, "Zorblax Quentin")
    new = _person(server, "Zorblax Quentin Jr")
    _second_writer_sets_key(server, old, "is_self", True)
    log = _install_probe(
        monkeypatch, server, {"SELECT metadata FROM nodes WHERE id = ?"})
    r = _call(server.engram_supersede, old_node_id=old, new_node_id=new,
              supersede_reason="renamed")
    assert "error" not in r, r
    assert _meta(server, new).get("is_self") is True  # the transfer branch ran
    assert log == [True], log
