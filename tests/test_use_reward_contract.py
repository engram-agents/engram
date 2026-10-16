"""Contract: `engram_restamp.use_reward` owns the whole "bump utility, then restamp
if the action restamps" sequence (PR #3606 round 4: one function, two callers).

Written BEFORE the implementation (project convention "Contract-first tests").

  1. Returns (bumped, restamped, restamp_error).  Counts are of rows actually
     updated: duplicates collapse, missing ids are skipped.
  2. restamp_error is None on success (and for a non-restamping action).
  3. A malformed/unreadable config -> restamp_error starts "config:" naming the
     exception type; bump kept; nothing restamped.
  4. A restamp failure mid-way -> restamp_error starts "restamp:"; the SAVEPOINT
     rolls back ONLY the restamp (no partial restamp), the bump and the caller's
     pending write remain.
  5. Never commits (a caller-side rollback undoes the bump too).
  6. memory_config may be a zero-arg callable, evaluated lazily: never called for
     a non-restamping action, and its failure is a "config:" error.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

import engram_restamp as rs

TURN = 100
DECAY = 1.014
SENT = -7
MEM = {"current_turn": TURN, "decay_base": DECAY}


@pytest.fixture
def conn():
    c = sqlite3.connect(":memory:")
    c.execute(
        "CREATE TABLE nodes (id TEXT PRIMARY KEY, utility_score REAL, "
        "importance_base REAL, importance_score REAL, recall_turn INTEGER, "
        "recall_count INTEGER)"
    )
    c.executemany(
        "INSERT INTO nodes VALUES (?, 0.0, 0.8, 0.1, ?, 3)",
        [("ob_0001", SENT), ("ob_0002", SENT)],
    )
    c.commit()
    yield c
    c.close()


def _turns(conn):
    return dict(conn.execute("SELECT id, recall_turn FROM nodes"))


def _util(conn):
    return dict(conn.execute("SELECT id, utility_score FROM nodes"))


def test_success_counts_and_no_error(conn):
    r = rs.use_reward(conn, ["ob_0001", "ob_0001", "ob_0002", "ob_9999"],
                      "mention", memory_config=MEM)
    assert (r.bumped, r.restamped, r.restamp_error) == (2, 2, None)
    assert _turns(conn) == {"ob_0001": TURN, "ob_0002": TURN}
    assert _util(conn)["ob_0001"] == pytest.approx(0.10)


def test_empty_and_all_missing(conn):
    assert tuple(rs.use_reward(conn, [], "mention", memory_config=MEM)) == (0, 0, None)
    assert tuple(rs.use_reward(conn, ["ob_9999"], "mention", memory_config=MEM)) == (0, 0, None)


def test_non_restamp_action_bumps_only(conn):
    assert "unfocus" not in rs.RESTAMP_ACTIONS
    r = rs.use_reward(conn, ["ob_0001"], "unfocus", memory_config=MEM)
    assert tuple(r) == (1, 0, None)
    assert _turns(conn)["ob_0001"] == SENT
    assert _util(conn)["ob_0001"] == pytest.approx(0.05)


def test_malformed_config_names_config_stage_and_keeps_bump(conn, tmp_path):
    cfg = tmp_path / "config.json"
    cfg.write_text("{not json")
    r = rs.use_reward(conn, ["ob_0001"], "mention", config_path=cfg)
    assert r.bumped == 1 and r.restamped == 0
    assert r.restamp_error.startswith("config: ConfigUnreadableError")
    assert _turns(conn)["ob_0001"] == SENT
    assert _util(conn)["ob_0001"] == pytest.approx(0.10)


def test_no_config_source_is_a_config_error(conn):
    r = rs.use_reward(conn, ["ob_0001"], "mention")
    assert r.restamp_error.startswith("config: ValueError")


def test_config_path_success(conn, tmp_path):
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps({"memory": MEM}))
    r = rs.use_reward(conn, ["ob_0001"], "mention", config_path=cfg)
    assert (r.bumped, r.restamped, r.restamp_error) == (1, 1, None)


def test_restamp_failure_names_restamp_stage_and_rolls_back_only_restamp(conn, monkeypatch):
    def partial_then_raise(c, ids, **kw):
        c.execute("UPDATE nodes SET recall_turn = ? WHERE id = ?", (TURN, list(ids)[0]))
        raise RuntimeError("boom mid-restamp")

    monkeypatch.setattr(rs, "restamp", partial_then_raise)
    conn.execute("UPDATE nodes SET importance_base = 0.77 WHERE id = 'ob_0001'")  # caller's write
    r = rs.use_reward(conn, ["ob_0001", "ob_0002"], "derive", memory_config=MEM)
    assert r.bumped == 2 and r.restamped == 0
    assert r.restamp_error == "restamp: RuntimeError: boom mid-restamp"
    assert _turns(conn) == {"ob_0001": SENT, "ob_0002": SENT}, "no partial restamp"
    assert _util(conn)["ob_0001"] == pytest.approx(0.15), "bump kept"
    conn.commit()
    assert conn.execute(
        "SELECT importance_base FROM nodes WHERE id='ob_0001'").fetchone()[0] == 0.77


def test_never_commits(conn):
    rs.use_reward(conn, ["ob_0001"], "mention", memory_config=MEM)
    conn.rollback()
    assert _util(conn)["ob_0001"] == 0.0 and _turns(conn)["ob_0001"] == SENT


def test_callable_memory_config_is_lazy_and_guarded(conn):
    calls = []

    def provider():
        calls.append(1)
        return MEM

    rs.use_reward(conn, ["ob_0001"], "unfocus", memory_config=provider)
    assert calls == [], "never read for a non-restamping action"
    rs.use_reward(conn, ["ob_9999"], "mention", memory_config=provider)
    assert calls == [], "never read when nothing was bumped"
    r = rs.use_reward(conn, ["ob_0001"], "mention", memory_config=provider)
    assert calls == [1] and r.restamp_error is None

    def bad():
        raise OSError("unreadable")

    r = rs.use_reward(conn, ["ob_0002"], "mention", memory_config=bad)
    assert r.restamp_error == "config: OSError: unreadable" and r.bumped == 1


def test_alpha_override(conn):
    rs.use_reward(conn, ["ob_0001"], "mention", memory_config=MEM, alpha=0.5)
    assert _util(conn)["ob_0001"] == pytest.approx(0.5)


def test_success_path_releases_its_savepoint(tmp_path):
    """The success path must RELEASE its SAVEPOINT.  On an autocommit connection
    the outermost RELEASE is what ends the implicit transaction: without it the
    savepoint (and the restamp) stay open -- visible as in_transaction and as a
    restamp a second connection cannot see.  Both observables differ."""
    db = tmp_path / "n.db"
    c = sqlite3.connect(str(db), isolation_level=None)
    c.execute(
        "CREATE TABLE nodes (id TEXT PRIMARY KEY, utility_score REAL, "
        "importance_base REAL, importance_score REAL, recall_turn INTEGER, "
        "recall_count INTEGER)"
    )
    c.execute("INSERT INTO nodes VALUES ('ob_0001', 0.0, 0.8, 0.1, ?, 3)", (SENT,))
    try:
        r = rs.use_reward(c, ["ob_0001"], "mention", memory_config=MEM)
        assert (r.bumped, r.restamped, r.restamp_error) == (1, 1, None)
        assert not c.in_transaction, "success path left its SAVEPOINT open"
        other = sqlite3.connect(str(db), timeout=0.2)
        try:
            assert other.execute(
                "SELECT recall_turn FROM nodes WHERE id='ob_0001'").fetchone()[0] == TURN
        finally:
            other.close()
    finally:
        c.close()
