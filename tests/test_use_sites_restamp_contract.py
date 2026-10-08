"""Contract tests: every "in mind, with detail, and shown relevant" USE-site
restamps the node (recall_turn + importance_score, NOT recall_count).

Lei's ruling (2026-10-08): refresh a memory whenever it is in the agent's
context and shown to be relevant; call the single restamp function at each place
that is a proxy for that.  The use-action table (USE_ALPHA) is already the one
place every use-site passes through (`_utility_reward(conn, ids, action=)`), so
that table also decides which actions restamp.  Written BEFORE the
implementation (project convention "Contract-first tests").

Contract:
  1. engram_restamp.RESTAMP_ACTIONS is a subset of USE_ALPHA's keys, contains
     derive/supersede/contradict/resolve/register_exemplar/lesson_incident/
     citation/focus/focus_load/history/mention, and excludes inspect/subgraph
     (they restamp + count recall via _refresh_recall) and unfocus.
  2. For EACH action in the set, `_utility_reward(conn, [id], action=a)` sets
     recall_turn = current_turn and importance_score = compute_importance(...),
     leaves recall_count unchanged, and bumps utility by that action's alpha.
  3. For inspect/subgraph/unfocus/unknown actions it does NOT touch
     recall_turn/importance_score (utility still bumps).
  4. End to end through derive, resolve, focus (real tool impls).
  5. inspect / get_subgraph with dream_mode=True still do NOT restamp.
  6. The mention hook's utility bump follows the table's "mention" entry.
"""

import atexit
import importlib
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import engram_restamp as _collect_time_restamp  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent
HOOK_PATH = REPO_ROOT / "src" / "engram" / "hooks" / "claude" / "engram-utility-credit-mention-stop.py"

TEST_DIR = tempfile.mkdtemp(prefix="engram_test_use_sites_restamp_")
atexit.register(lambda: shutil.rmtree(TEST_DIR, ignore_errors=True))

TURN = 50
DECAY = 1.014
SENTINEL_TURN = -7
SENTINEL_SCORE = 0.123

# The spec's contents.  The parametrized cases are the UNION of this and the set itself
# (so a future addition is covered automatically AND a removal reddens its own case,
# not silently drops it from collection).
SPEC_RESTAMP = {
    "derive", "supersede", "contradict", "resolve", "register_exemplar",
    "lesson_incident", "citation", "focus", "focus_load", "history", "mention",
}
NO_RESTAMP = ["inspect", "subgraph", "unfocus", "no_such_action_xyz"]

_SET = SPEC_RESTAMP | set(getattr(_collect_time_restamp, "RESTAMP_ACTIONS", ()))


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


def _ax(server, i=0):
    r = json.loads(server.engram_add_axiom(payload_json=json.dumps({
        "claim": f"Axiom {i} for use-site restamp test.",
        "basis": f"restamp fixture axiom {i}.",
    })))
    return r["axiom_id"]


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
        r = conn.execute(
            "SELECT recall_turn, importance_score, recall_count, importance_base, "
            "utility_score FROM nodes WHERE id = ?", (nid,)
        ).fetchone()
        return dict(r)
    finally:
        conn.close()


def _expected_score(base):
    return round((base or 0.5) * DECAY ** TURN, 6)


def _assert_restamped(st):
    assert st["recall_turn"] == TURN, st
    assert st["importance_score"] == pytest.approx(_expected_score(st["importance_base"]), abs=1e-9)


def _assert_untouched(st):
    assert st["recall_turn"] == SENTINEL_TURN, st
    assert st["importance_score"] == pytest.approx(SENTINEL_SCORE, abs=1e-9)


def _reward(server, nid, action):
    conn = server._get_db()
    try:
        server._utility_reward(conn, [nid], action=action)
        conn.commit()
    finally:
        conn.close()


# --- 1. the set --------------------------------------------------------------


def test_restamp_actions_is_subset_of_alpha_table_and_has_spec_members():
    server = fresh_server()
    rs = importlib.import_module("engram_restamp")
    assert hasattr(rs, "RESTAMP_ACTIONS"), "the one named set of restamping actions"
    assert set(rs.RESTAMP_ACTIONS) <= set(rs.USE_ALPHA), "every restamp action has an alpha"
    assert SPEC_RESTAMP <= set(rs.RESTAMP_ACTIONS)
    for excluded in ("inspect", "subgraph", "unfocus"):
        assert excluded not in rs.RESTAMP_ACTIONS
    # engram_core keeps thin re-exports of the same objects
    assert server.USE_ALPHA is rs.USE_ALPHA or server.USE_ALPHA == rs.USE_ALPHA
    assert server.USE_ALPHA_DEFAULT == rs.USE_ALPHA_DEFAULT


# --- 2/3. _utility_reward, per action ----------------------------------------


@pytest.mark.parametrize("action", sorted(_SET))
def test_restamping_action_restamps_without_recall_count_and_bumps_utility(action):
    server = fresh_server()
    nid = _ax(server)
    _sentinel(server, [nid])
    before = _state(server, nid)
    _reward(server, nid, action)
    st = _state(server, nid)
    _assert_restamped(st)
    assert st["recall_count"] == before["recall_count"], "use must not count as recall"
    alpha = importlib.import_module("engram_restamp").USE_ALPHA[action]
    assert st["utility_score"] == pytest.approx(alpha, abs=1e-9)


@pytest.mark.parametrize("action", NO_RESTAMP)
def test_non_restamping_action_does_not_touch_recall_turn_or_importance(action):
    server = fresh_server()
    nid = _ax(server)
    _sentinel(server, [nid])
    before = _state(server, nid)
    _reward(server, nid, action)
    st = _state(server, nid)
    _assert_untouched(st)
    assert st["recall_count"] == before["recall_count"]
    assert st["utility_score"] > 0.0, "utility still bumps"


def test_missing_id_is_skipped_and_only_updated_ids_restamp():
    server = fresh_server()
    nid = _ax(server)
    _sentinel(server, [nid])
    conn = server._get_db()
    try:
        n = server._utility_reward(conn, [nid, "ob_9999"], action="citation")
        conn.commit()
    finally:
        conn.close()
    assert n == 1
    _assert_restamped(_state(server, nid))


# --- 4. end to end through real tool impls ------------------------------------


def test_derive_restamps_its_premises():
    server = fresh_server()
    a, b = _ax(server, 1), _ax(server, 2)
    _sentinel(server, [a, b])
    r = json.loads(server.engram_derive(payload_json=json.dumps({
        "claim": "Derived claim for restamp test.",
        "supporting_ids": f"{a},{b}",
        "logical_chain": "Premises jointly entail it.",
        "reasoning_type": "deductive_modus_ponens",
    })))
    assert "error" not in r, r
    for nid in (a, b):
        st = _state(server, nid)
        _assert_restamped(st)
        assert st["recall_count"] == 0


def test_resolve_restamps_target_and_resolver():
    server = fresh_server()
    ob = json.loads(server.engram_add_observation(payload_json=json.dumps({
        "url": "https://example.com/a", "title": "A",
        "quoted_text": "X says Y is the case.", "interpretation": "X claims Y holds.",
        "claim": "Y is the case according to X.", "quote_type": "hard_data",
    })))["observation_id"]
    q_id = json.loads(server.engram_ask(payload_json=json.dumps(
        {"question": "Is Y the case?"})))["question_id"]
    dv_id = json.loads(server.engram_derive(payload_json=json.dumps({
        "claim": "Yes, Y is the case based on X's account.", "supporting_ids": ob,
        "logical_chain": "X attests Y.", "reasoning_type": "authority_expert",
    })))["derivation_id"]
    _sentinel(server, [q_id, dv_id])
    r = json.loads(server.engram_resolve(payload_json=json.dumps(
        {"target_id": q_id, "resolving_node_id": dv_id})))
    assert r.get("status") == "resolution_created", r
    _assert_restamped(_state(server, q_id))
    _assert_restamped(_state(server, dv_id))


def test_focus_restamps_focused_ids():
    server = fresh_server()
    a, b = _ax(server, 1), _ax(server, 2)
    _sentinel(server, [a, b])
    r = json.loads(server.engram_focus(payload_json=json.dumps(
        {"node_ids": f"{a},{b}", "reason": "restamp test"})))
    assert "error" not in r, r
    for nid in (a, b):
        _assert_restamped(_state(server, nid))


# --- 5. dream_mode regression -------------------------------------------------


def test_inspect_dream_mode_does_not_restamp():
    server = fresh_server()
    nid = _ax(server)
    _sentinel(server, [nid])
    json.loads(server.engram_inspect(payload_json=json.dumps(
        {"node_id": nid, "dream_mode": True})))
    _assert_untouched(_state(server, nid))


def test_get_subgraph_dream_mode_does_not_restamp():
    server = fresh_server()
    nid = _ax(server)
    _sentinel(server, [nid])
    json.loads(server.engram_get_subgraph(payload_json=json.dumps(
        {"node_id": nid, "dream_mode": True})))
    _assert_untouched(_state(server, nid))


# --- 6. mention hook uses the shared table ------------------------------------


def test_mention_hook_alpha_follows_the_shared_table(tmp_path, monkeypatch):
    import sqlite3
    import importlib.util

    monkeypatch.setenv("ENGRAM_HOME", str(tmp_path))
    monkeypatch.delenv("ENGRAM_RUNTIME_DIR", raising=False)
    (tmp_path / "config.json").write_text(
        json.dumps({"memory": {"current_turn": TURN, "decay_base": DECAY}}))
    db = tmp_path / "knowledge.db"
    conn = sqlite3.connect(str(db))
    conn.execute(
        "CREATE TABLE nodes (id TEXT PRIMARY KEY, utility_score REAL, "
        "importance_base REAL, importance_score REAL, recall_turn INTEGER, "
        "recall_count INTEGER)")
    conn.execute("INSERT INTO nodes VALUES ('ob_1234', 0.0, 0.8, 0.1, -7, 3)")
    conn.commit()
    conn.close()
    spec = importlib.util.spec_from_file_location("use_sites_hook_under_test", HOOK_PATH)
    hook = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(hook)

    assert not hasattr(hook, "ALPHA_MENTION"), "private alpha constant must be gone"
    monkeypatch.setitem(hook.engram_restamp.USE_ALPHA, "mention", 0.37)
    hook.credit_mentions("used ob_1234", db_path=str(db),
                         config_path=str(tmp_path / "config.json"))
    conn = sqlite3.connect(str(db))
    util, rturn, rcount = conn.execute(
        "SELECT utility_score, recall_turn, recall_count FROM nodes WHERE id='ob_1234'"
    ).fetchone()
    conn.close()
    assert util == pytest.approx(0.37, abs=1e-9)
    assert (rturn, rcount) == (TURN, 3)
