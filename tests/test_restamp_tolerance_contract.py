"""Contract: a recall-turn restamp is decoration on a write and must NEVER abort
or roll back the write, nor lose the utility bump (PR #3606 reviewer blocker).

Written BEFORE the implementation (project convention "Contract-first tests").

Contract for the server-side path (`_utility_reward`, restamp actions):
  1. The memory config is read inside a guard.  On ANY failure the restamp is
     skipped for that call (no invented turn: defaults would stamp turn 0), a
     WARNING naming the action, node count and exception is logged, and the
     utility bump + the caller's write still commit.
  2. The restamp UPDATEs run inside a SAVEPOINT on the caller's connection.  If
     they raise mid-way, only the restamp is rolled back (no partial restamp),
     `_utility_reward` returns normally with the utility bumps intact, a WARNING
     is logged, and the caller's own write still commits.
  3. Healthy config: unchanged (restamp happens, no warning).
"""

import atexit
import json
import logging
import os
import shutil
import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

TEST_DIR = tempfile.mkdtemp(prefix="engram_test_restamp_tolerance_")
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
    return json.loads(server.engram_add_axiom(payload_json=json.dumps({
        "claim": f"Axiom {i} for restamp tolerance test.",
        "basis": f"restamp tolerance fixture axiom {i}.",
    })))["axiom_id"]


def _sentinel(server, ids):
    conn = server._get_db()
    try:
        for nid in ids:
            conn.execute(
                "UPDATE nodes SET recall_turn = ?, importance_score = ? WHERE id = ?",
                (SENTINEL_TURN, SENTINEL_SCORE, nid),
            )
        conn.commit()
    finally:
        conn.close()


def _state(server, nid):
    conn = server._get_db()
    try:
        return dict(conn.execute(
            "SELECT recall_turn, importance_score, utility_score FROM nodes WHERE id = ?",
            (nid,),
        ).fetchone())
    finally:
        conn.close()


def _utility(server, ids):
    return {nid: _state(server, nid)["utility_score"] for nid in ids}


def _derive(server, a, b):
    return json.loads(server.engram_derive(payload_json=json.dumps({
        "claim": "Derived claim for restamp tolerance test.",
        "supporting_ids": f"{a},{b}",
        "logical_chain": "Premises jointly entail it.",
        "reasoning_type": "deductive_modus_ponens",
    })))


def _warnings(caplog):
    return [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]


def test_malformed_config_does_not_abort_focus(caplog):
    # focus creates no new node, so (unlike derive/supersede, whose
    # _stamp_new_node already read the config before this PR) it never read
    # config.json before the restamp work: the clean probe for this blocker.
    server = fresh_server()
    a, b = _ax(server, 1), _ax(server, 2)
    _sentinel(server, [a, b])
    before = _utility(server, [a, b])
    (Path(TEST_DIR) / "config.json").write_text("{ this is not json")
    with caplog.at_level(logging.WARNING):
        r = json.loads(server.engram_focus(payload_json=json.dumps(
            {"node_ids": f"{a},{b}", "reason": "tolerance test"})))
    assert "error" not in r, r
    conn = server._get_db()
    try:
        focused = {row[0] for row in conn.execute(
            "SELECT id FROM nodes WHERE focused_at IS NOT NULL")}
    finally:
        conn.close()
    assert {a, b} <= focused, "the focus write must exist after commit"
    for nid in (a, b):
        st = _state(server, nid)
        assert st["utility_score"] > (before[nid] or 0.0), st   # bump survived
        assert st["recall_turn"] == SENTINEL_TURN, st            # no invented turn
        assert st["importance_score"] == pytest.approx(SENTINEL_SCORE, abs=1e-9)
    msgs = _warnings(caplog)
    assert any("focus" in m and "ConfigUnreadableError" in m and "2 node" in m for m in msgs), msgs


def test_restamp_failure_rolls_back_only_the_restamp(caplog, monkeypatch):
    server = fresh_server()
    a, b = _ax(server, 1), _ax(server, 2)
    _sentinel(server, [a, b])
    before = _utility(server, [a, b])
    import engram_restamp

    def partial_then_raise(conn, node_ids, **kw):
        ids = list(node_ids)
        conn.execute(
            "UPDATE nodes SET recall_turn = ?, importance_score = ? WHERE id = ?",
            (TURN, 0.999, ids[0]),
        )
        raise RuntimeError("boom mid-restamp")

    monkeypatch.setattr(engram_restamp, "restamp", partial_then_raise)
    conn = server._get_db()
    try:
        # the caller's own write, pending in the same open transaction
        conn.execute("UPDATE nodes SET importance_base = 0.77 WHERE id = ?", (a,))
        with caplog.at_level(logging.WARNING):
            n = server._utility_reward(conn, [a, b], action="derive")
        assert n == 2
        conn.commit()
    finally:
        conn.close()
    for nid in (a, b):
        st = _state(server, nid)
        assert st["utility_score"] > (before[nid] or 0.0), st
        assert st["recall_turn"] == SENTINEL_TURN, st            # partial restamp undone
        assert st["importance_score"] == pytest.approx(SENTINEL_SCORE, abs=1e-9)
    conn = server._get_db()
    try:
        base = conn.execute("SELECT importance_base FROM nodes WHERE id = ?", (a,)).fetchone()[0]
    finally:
        conn.close()
    assert base == pytest.approx(0.77), "the caller's write must commit"
    msgs = _warnings(caplog)
    assert any("derive" in m and "RuntimeError" in m and "boom mid-restamp" in m for m in msgs), msgs


def test_healthy_config_restamps_and_logs_no_warning(caplog):
    server = fresh_server()
    a, b = _ax(server, 1), _ax(server, 2)
    _sentinel(server, [a, b])
    with caplog.at_level(logging.WARNING):
        r = _derive(server, a, b)
    assert "error" not in r, r
    for nid in (a, b):
        assert _state(server, nid)["recall_turn"] == TURN
    assert not [m for m in _warnings(caplog) if "restamp" in m.lower()]
