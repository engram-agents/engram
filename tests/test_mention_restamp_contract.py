"""Contract tests: a node id mentioned in the agent's prose restamps recall_turn.

Lei's ruling (2026-10-08, engram-alpha#2005 section 3): keep the forgetting
formula, but let a prose mention refresh the time stamp.  Written BEFORE the
implementation (project convention "Contract-first tests").

Contract:
  1. Mentioned id in DB -> recall_turn == current_turn;
     importance_score == importance_base * decay_base**current_turn (the same
     function deliberate recall uses); recall_count UNCHANGED; utility_score
     bumped exactly as before.
  2. Mentioned id absent from DB -> skipped, no error, no row created.
  3. Same id mentioned twice -> restamped once.
  4. `_refresh_recall` (deliberate recall) still restamps AND increments
     recall_count, via the SAME shared function (count_recall=True).
  5. A restamp failure is VISIBLE in the hook log and does not lose the
     utility bump.
  6. The shared module imports in the FLATTENED deployed plugin layout with
     CLAUDE_PLUGIN_ROOT unset.
"""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SRC_ENGRAM = REPO_ROOT / "src" / "engram"
HOOK_PATH = SRC_ENGRAM / "hooks" / "claude" / "engram-utility-credit-mention-stop.py"

TURN = 100
DECAY = 1.014
BASE = 0.8
SENTINEL_TURN = -7


def _make_db(path: Path, rows) -> None:
    conn = sqlite3.connect(str(path))
    conn.execute(
        "CREATE TABLE nodes (id TEXT PRIMARY KEY, utility_score REAL, "
        "importance_base REAL, importance_score REAL, recall_turn INTEGER, "
        "recall_count INTEGER)"
    )
    conn.executemany(
        "INSERT INTO nodes (id, utility_score, importance_base, importance_score, "
        "recall_turn, recall_count) VALUES (?, ?, ?, ?, ?, ?)",
        rows,
    )
    conn.commit()
    conn.close()


def _write_config(home: Path) -> None:
    (home / "config.json").write_text(
        json.dumps({"memory": {"current_turn": TURN, "decay_base": DECAY}})
    )


def _row(db: Path, nid: str):
    conn = sqlite3.connect(str(db))
    try:
        return conn.execute(
            "SELECT utility_score, importance_base, importance_score, "
            "recall_turn, recall_count FROM nodes WHERE id = ?",
            (nid,),
        ).fetchone()
    finally:
        conn.close()


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("ENGRAM_HOME", str(tmp_path))
    monkeypatch.delenv("ENGRAM_RUNTIME_DIR", raising=False)
    _write_config(tmp_path)
    return tmp_path


@pytest.fixture
def hook(home):
    spec = importlib.util.spec_from_file_location("mention_restamp_hook_under_test", HOOK_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def db(home):
    path = home / "knowledge.db"
    _make_db(
        path,
        [
            ("ob_1234", 0.0, BASE, 0.1, SENTINEL_TURN, 3),
            ("dv_0567", 0.5, 0.5, 0.1, SENTINEL_TURN, 0),
        ],
    )
    return path


def _credit(hook, db, home, msg):
    return hook.credit_mentions(
        msg, db_path=str(db), config_path=str(home / "config.json")
    )


# --- 1. mentioned id present ------------------------------------------------


def test_mention_restamps_recall_turn_and_importance_not_recall_count(hook, db, home):
    _credit(hook, db, home, "I relied on ob_1234 here.")
    util, base, score, rturn, rcount = _row(db, "ob_1234")
    assert rturn == TURN
    assert score == pytest.approx(round(BASE * DECAY**TURN, 6), abs=1e-9)
    assert rcount == 3, "a mention must NOT increment recall_count"
    assert util == pytest.approx(0.10, abs=1e-9), "utility bump must be unchanged"
    assert base == BASE


def test_mention_uses_importance_default_when_base_null(hook, home):
    db = home / "knowledge.db"
    _make_db(db, [("ob_4321", 0.0, None, 0.1, SENTINEL_TURN, 0)])
    _credit(hook, db, home, "see ob_4321")
    _, _, score, rturn, _ = _row(db, "ob_4321")
    assert rturn == TURN
    assert score == pytest.approx(round(0.5 * DECAY**TURN, 6), abs=1e-9)


def test_unmentioned_node_untouched(hook, db, home):
    _credit(hook, db, home, "I relied on ob_1234 here.")
    util, _, score, rturn, rcount = _row(db, "dv_0567")
    assert (util, score, rturn, rcount) == (0.5, 0.1, SENTINEL_TURN, 0)


# --- 2. absent id ------------------------------------------------------------


def test_absent_id_skipped_no_error_no_row_created(hook, db, home):
    stats = _credit(hook, db, home, "a url fragment in_9999 and ob_1234")
    conn = sqlite3.connect(str(db))
    ids = {r[0] for r in conn.execute("SELECT id FROM nodes")}
    conn.close()
    assert "in_9999" not in ids
    assert ids == {"ob_1234", "dv_0567"}
    assert _row(db, "ob_1234")[3] == TURN
    assert stats["mentioned_count"] == 2


# --- 3. dedup ---------------------------------------------------------------


def test_same_id_twice_restamped_once(hook, db, home, monkeypatch):
    real_restamp = hook.engram_restamp.restamp

    seen = []

    def spy(conn, node_ids, **kw):
        seen.append(list(node_ids))
        return real_restamp(conn, node_ids, **kw)

    monkeypatch.setattr(hook.engram_restamp, "restamp", spy)
    _credit(hook, db, home, "ob_1234 and again ob_1234, finally ob_1234")
    assert seen == [["ob_1234"]]


# --- 4. deliberate recall regression ----------------------------------------


def _row_factory_conn(db: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db))
    conn.row_factory = sqlite3.Row
    return conn


def test_refresh_recall_still_restamps_and_increments_recall_count(db, monkeypatch):
    import engram_core as core
    import engram_query

    monkeypatch.setattr(
        core, "_get_memory_config", lambda: {"current_turn": TURN, "decay_base": DECAY}
    )
    conn = _row_factory_conn(db)
    engram_query._refresh_recall(conn, ["ob_1234", "no_such_0001"])
    conn.close()
    util, _, score, rturn, rcount = _row(db, "ob_1234")
    assert rturn == TURN
    assert rcount == 4
    assert score == pytest.approx(round(BASE * DECAY**TURN, 6), abs=1e-9)
    assert util == 0.0, "deliberate recall never touched utility_score"


def test_refresh_recall_goes_through_the_shared_restamp(db, monkeypatch):
    """Single source of truth: _refresh_recall delegates, with count_recall=True."""
    import engram_core as core
    import engram_query
    import engram_restamp

    monkeypatch.setattr(
        core, "_get_memory_config", lambda: {"current_turn": TURN, "decay_base": DECAY}
    )
    calls = []
    real = engram_restamp.restamp

    def spy(conn, node_ids, **kw):
        calls.append(kw)
        return real(conn, node_ids, **kw)

    monkeypatch.setattr(engram_restamp, "restamp", spy)
    conn = _row_factory_conn(db)
    engram_query._refresh_recall(conn, ["ob_1234"])
    conn.close()
    assert len(calls) == 1 and calls[0].get("count_recall") is True


def test_restamp_function_contract_both_modes(db):
    import engram_restamp

    conn = sqlite3.connect(str(db))
    mem = {"current_turn": TURN, "decay_base": DECAY}
    engram_restamp.restamp(conn, ["ob_1234"], count_recall=False, memory_config=mem)
    engram_restamp.restamp(conn, ["dv_0567"], count_recall=True, memory_config=mem)
    conn.commit()
    conn.close()
    assert _row(db, "ob_1234")[3:] == (TURN, 3)
    assert _row(db, "dv_0567")[3:] == (TURN, 1)


# --- 5. visible failure ------------------------------------------------------


def test_restamp_failure_is_logged_and_utility_bump_survives(hook, db, home, monkeypatch):
    def boom(conn, node_ids, **kw):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(hook.engram_restamp, "restamp", boom)
    _credit(hook, db, home, "ob_1234")
    log = Path(hook.LOG_PATH).read_text()
    assert "restamp failed" in log and "database is locked" in log
    util, _, _, rturn, _ = _row(db, "ob_1234")
    assert util == pytest.approx(0.10, abs=1e-9)
    assert rturn == SENTINEL_TURN


def test_shared_module_import_failure_is_logged_and_bump_is_lost(hook, db, home):
    # Round 4: bump AND restamp are one shared sequence (use_reward), so when the
    # shared module cannot be imported there is nothing to bump with.  The failure
    # stays LOGGED ("bump_utility failed"), never silent; the bump is lost.
    hook.engram_restamp = None
    _credit(hook, db, home, "ob_1234")
    log = Path(hook.LOG_PATH).read_text()
    assert "bump_utility failed" in log and "engram_restamp unavailable" in log
    assert _row(db, "ob_1234")[0] == 0.0
    assert _row(db, "ob_1234")[3] == SENTINEL_TURN


def test_restamp_bad_config_is_logged_and_utility_bump_survives(hook, db, home):
    (home / "config.json").write_text("{not json")
    _credit(hook, db, home, "ob_1234")
    assert "restamp failed" in Path(hook.LOG_PATH).read_text()
    assert _row(db, "ob_1234")[0] == pytest.approx(0.10, abs=1e-9)


# --- 6. deployed (flattened) layout -----------------------------------------


def test_hook_restamps_in_flattened_deployed_layout_without_plugin_root(tmp_path):
    """Recreate the build's flattened layout (root/hooks/<hook>, root/engram_*.py)
    and run the hook as a subprocess with CLAUDE_PLUGIN_ROOT and
    ENGRAM_RUNTIME_DIR unset. The shared module must be found by the hook's
    resolve_runtime_dir walk, not a fixed parents[N]."""
    root = tmp_path / "plugin_root"
    (root / "hooks").mkdir(parents=True)
    for name in ("_hooklib.py",):
        shutil.copy(SRC_ENGRAM / "hooks" / "claude" / name, root / "hooks" / name)
    shutil.copy(HOOK_PATH, root / "hooks" / HOOK_PATH.name)
    for name in ("engram_ids.py", "engram_restamp.py"):
        shutil.copy(SRC_ENGRAM / name, root / name)

    home = tmp_path / "engram_home"
    home.mkdir()
    _write_config(home)
    db = home / "knowledge.db"
    _make_db(db, [("ob_1234", 0.0, BASE, 0.1, SENTINEL_TURN, 3)])

    env = {k: v for k, v in os.environ.items()
           if k not in ("CLAUDE_PLUGIN_ROOT", "ENGRAM_RUNTIME_DIR")}
    env["ENGRAM_HOME"] = str(home)
    payload = json.dumps({
        "stop_hook_active": False,
        "last_assistant_message": "used ob_1234",
        "session_id": "deployed-layout",
        "transcript_path": "",
    })
    proc = subprocess.run(
        [sys.executable, str(root / "hooks" / HOOK_PATH.name)],
        input=payload, text=True, env=env, capture_output=True, timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
    util, _, _, rturn, rcount = _row(db, "ob_1234")
    assert rturn == TURN, (proc.stderr, (home / "utility-credit-mention.log").read_text()
                           if (home / "utility-credit-mention.log").exists() else "")
    assert rcount == 3
    assert util == pytest.approx(0.10, abs=1e-9)
