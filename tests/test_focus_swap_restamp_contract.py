"""Contract test: focus_swap restamps only the INCOMING (loaded) node set.

Lei's rule (2026-10-08): "removing from mind is not use" -- unfocus does not
restamp.  A swap's outgoing set is removed from mind, so it must not restamp
either; the incoming set (focused again, its content injected) does.  The
outgoing set still gets utility credit, now via the table's "unfocus" entry.
Written BEFORE the implementation (project convention "Contract-first tests").
"""

import atexit
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

TEST_DIR = tempfile.mkdtemp(prefix="engram_test_focus_swap_restamp_")
atexit.register(lambda: shutil.rmtree(TEST_DIR, ignore_errors=True))

TURN = 50
DECAY = 1.014
SENTINEL_TURN = -7
SENTINEL_SCORE = 0.123


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
    cfg_path = Path(TEST_DIR) / "config.json"
    cfg = json.loads(cfg_path.read_text()) if cfg_path.exists() else {}
    cfg["memory"] = {**cfg.get("memory", {}), "current_turn": TURN, "decay_base": DECAY}
    cfg_path.write_text(json.dumps(cfg))
    return server


def _ax(server, i):
    r = json.loads(server.engram_add_axiom(payload_json=json.dumps({
        "claim": f"Axiom {i} for focus-swap restamp test.",
        "basis": f"focus swap fixture axiom {i}.",
    })))
    return r["axiom_id"]


def _state(server, nid):
    conn = server._get_db()
    try:
        return dict(conn.execute(
            "SELECT recall_turn, importance_score, utility_score FROM nodes WHERE id = ?",
            (nid,),
        ).fetchone())
    finally:
        conn.close()


def _sentinel(server, ids):
    conn = server._get_db()
    try:
        for nid in ids:
            conn.execute(
                "UPDATE nodes SET recall_turn = ?, importance_score = ?, "
                "utility_score = 0.0 WHERE id = ?",
                (SENTINEL_TURN, SENTINEL_SCORE, nid),
            )
        conn.commit()
    finally:
        conn.close()


def _focus(server, ids):
    r = json.loads(server.engram_focus(payload_json=json.dumps(
        {"node_ids": ",".join(ids), "reason": "swap restamp test"})))
    assert "error" not in r, r


def _swap(server, **kw):
    r = json.loads(server.engram_focus_swap(payload_json=json.dumps(kw)))
    assert r.get("status") == "ok", r
    return r


def _setup():
    """incoming set {a,b} saved as 'incoming'; then outgoing {c} focused."""
    server = fresh_server()
    a, b, c = _ax(server, 1), _ax(server, 2), _ax(server, 3)
    _focus(server, [a, b])
    _swap(server, save_as="incoming")          # empties the active list
    _focus(server, [c])
    _sentinel(server, [a, b, c])               # reset stamps after setup
    return server, a, b, c


def test_swap_does_not_restamp_outgoing_ids():
    server, a, b, c = _setup()
    _swap(server, save_as="outgoing", load="incoming")
    st = _state(server, c)
    assert st["recall_turn"] == SENTINEL_TURN, st
    assert st["importance_score"] == pytest.approx(SENTINEL_SCORE, abs=1e-9)


def test_swap_restamps_incoming_ids():
    server, a, b, c = _setup()
    _swap(server, save_as="outgoing", load="incoming")
    for nid in (a, b):
        st = _state(server, nid)
        assert st["recall_turn"] == TURN, st
        assert st["importance_score"] != pytest.approx(SENTINEL_SCORE, abs=1e-9)


def test_swap_outgoing_still_gets_utility_credit_via_unfocus_alpha():
    server, a, b, c = _setup()
    _swap(server, save_as="outgoing", load="incoming")
    assert _state(server, c)["utility_score"] == pytest.approx(0.05)   # unfocus alpha
    assert _state(server, a)["utility_score"] == pytest.approx(0.10)   # focus alpha
