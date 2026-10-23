"""#3766 (part of #2652): an index on nodes(created_at), added by migration rung 8.

The U9b checkpoint reads (`session_checkpoint_block`'s COUNT and LIST statements) filter
`created_at >= ?`; without an index both are a full `SCAN nodes`. Design ruling: engram-alpha
#3766, option 2 -- one more index on `nodes`, no query change, no `INDEXED BY`.

Contract (written before the implementation; every cell is a stated behaviour):

  C1  PLAN PIN. `EXPLAIN QUERY PLAN` of the two statements AS THE HOOK HOLDS THEM
      (`_CHECKPOINT_COUNT_SQL`, `_CHECKPOINT_LIST_SQL`, imported from the hook, never copied)
      names `idx_nodes_created_at`; the LIST plan has no `USE TEMP B-TREE FOR ORDER BY`. Under
      three statistics states: no `sqlite_stat1` at all, `sqlite_stat1` after ANALYZE, and a
      STALE `sqlite_stat1` (ANALYZE on the empty table, then the rows arrive).
  C2  A store built from scratch and a store migrated up from user_version 7 carry the same
      index names AND the same index SQL; both end at the latest version (10 since #4075).
  C3  Idempotent: setup twice on a v8 store changes nothing and says nothing about migration 8;
      a v7 store that already HAS the index (crash between CREATE INDEX and the version bump)
      migrates to 8 with exactly one index of that name.
  C4  A failing CREATE INDEX leaves the ladder retryable: user_version stays 7, startup does not
      crash, stderr names migration 8, the next startup lands 8 + the index. With the pre-migration
      backup forced to fail, rung 8 does not run.
  C5  Answers unchanged: the two statements return the same COUNT and the same capped list with
      and without the index, on a fixture whose LIMIT cut falls inside a tie group on
      `created_at`; the expected list is computed independently in Python.
  C6  The pre-migration backup gate is honoured: a populated v7 store gets a backup before rung 8.
  C7  Self-heal (design addendum): at user_version >= 8 the index is re-ensured on every start;
      it does NOT run at user_version 7 (it must not paper over a failed rung 8).

Mutation targets (recorded in the handoff): index on a different column -> C1/C2 red; delete the
self-heal block -> C7 red.
"""
from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_TESTS_DIR = str(Path(__file__).resolve().parent)
if _TESTS_DIR not in sys.path:
    sys.path.insert(0, _TESTS_DIR)
from conftest import fresh_module  # noqa: E402

import engram_core  # noqa: E402
from test_session_start_owner_read_3735 import (  # noqa: E402,F401 -- fixtures and helpers reused
    load_ss,
    stub_modules,
)

IDX = "idx_nodes_created_at"
INDEX_LIST_SQL = "SELECT name, sql FROM sqlite_master WHERE type='index' ORDER BY name"
_BASE = datetime(2026, 10, 12, 1, 0, 0)

# #4029: in-process hook/tools loads run inside this window (sys.path / sys.modules restored).
pytestmark = pytest.mark.usefixtures("isolate_hook_loads")


def ts(i: int) -> str:
    return (_BASE + timedelta(seconds=i)).strftime("%Y-%m-%dT%H:%M:%SZ")


# --------------------------------------------------------------- helpers


@pytest.fixture
def core(monkeypatch):
    """A fresh engram_core against a temp data dir, DB bootstrapped through the real ladder."""
    monkeypatch.setenv("ENGRAM_NO_EMBEDDINGS", "1")
    monkeypatch.delenv("ENGRAM_SKIP_PRE_MIGRATION_BACKUP", raising=False)
    mod = fresh_module(engram_core)
    d = tempfile.mkdtemp(prefix="engram_test_3766_")
    mod._configure_paths(d)
    mod._ensure_data_dir()
    conn = mod._get_db()
    conn.close()
    try:
        yield mod
    finally:
        import shutil

        shutil.rmtree(d, ignore_errors=True)


def _raw(core):
    return sqlite3.connect(str(core.DB_PATH))


def _version(c) -> int:
    return c.execute("PRAGMA user_version").fetchone()[0]


def _setup(core, conn=None) -> int:
    """Run the one-time setup once more (on `conn`, or a fresh connection); return user_version."""
    own = conn is None
    if own:
        conn = _raw(core)
        conn.row_factory = sqlite3.Row
    try:
        core._run_db_one_time_setup(conn)
        conn.commit()
        return _version(conn)
    finally:
        if own:
            conn.close()


def _rewind_to_v7(core, drop_index=True):
    c = _raw(core)
    if drop_index:
        c.execute(f"DROP INDEX IF EXISTS {IDX}")
    c.execute("PRAGMA user_version = 7")
    c.commit()
    c.close()


def _index_rows(core):
    c = _raw(core)
    try:
        return c.execute(INDEX_LIST_SQL).fetchall()
    finally:
        c.close()


def _insert_rows(c, rows):
    c.executemany(
        "INSERT INTO nodes (id, type, claim, created_at, is_current, status) VALUES (?,?,?,?,1,'active')",
        rows,
    )
    c.commit()


def _deny_create_index(conn, times=None):
    """A real engine-level fault: the authorizer refuses CREATE INDEX idx_nodes_created_at (the
    first `times` attempts, or always). The refusal surfaces as sqlite3.DatabaseError."""
    state = {"n": 0}

    def auth(action, arg1, arg2, dbname, source):
        if action == sqlite3.SQLITE_CREATE_INDEX and arg1 == IDX:
            state["n"] += 1
            if times is None or state["n"] <= times:
                return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK

    conn.set_authorizer(auth)
    return state


# ----------------------------------------------------------------- C1


def _plan(c, sql, since):
    return [r[3] for r in c.execute("EXPLAIN QUERY PLAN " + sql, (since,)).fetchall()]


@pytest.mark.parametrize("stats", ["no_stat1", "analyzed", "stale_stat1"])
def test_c1_checkpoint_statements_plan_the_created_at_index(core, monkeypatch, stats):
    hook = load_ss(Path(core.DATA_DIR), monkeypatch)
    c = _raw(core)
    has_stat1 = lambda: c.execute(  # noqa: E731
        "SELECT 1 FROM sqlite_master WHERE name='sqlite_stat1'").fetchone() is not None
    assert not has_stat1(), "fixture precondition: a freshly built store has never been ANALYZEd"
    if stats == "stale_stat1":
        c.execute("ANALYZE")  # stats describe the EMPTY table ...
        c.commit()
        assert has_stat1()
    _insert_rows(c, [(f"n{i:05d}", "observation", f"claim number {i}", ts(i)) for i in range(4000)])
    if stats == "analyzed":
        c.execute("ANALYZE")
        c.commit()
        assert has_stat1()
    if stats == "no_stat1":
        assert not has_stat1()
    since = ts(3995)  # selects only the last five rows
    assert c.execute("SELECT COUNT(*) FROM nodes WHERE created_at >= ?", (since,)).fetchone()[0] == 5
    count_plan = _plan(c, hook._CHECKPOINT_COUNT_SQL, since)
    list_plan = _plan(c, hook._CHECKPOINT_LIST_SQL, since)
    c.close()
    assert any(IDX in d for d in count_plan), f"COUNT plan does not use {IDX}: {count_plan}"
    assert any(IDX in d for d in list_plan), f"LIST plan does not use {IDX}: {list_plan}"
    assert not any("USE TEMP B-TREE FOR ORDER BY" in d for d in list_plan), list_plan


# ----------------------------------------------------------------- C2


def test_c2_fresh_and_migrated_stores_carry_the_same_indexes(core):
    c = _raw(core)
    assert _version(c) == core.LATEST_SCHEMA_VERSION
    c.close()
    fresh = _index_rows(core)
    assert any(name == IDX for name, _ in fresh)
    c = _raw(core)
    assert [r[2] for r in c.execute(f"PRAGMA index_info({IDX})").fetchall()] == ["created_at"]
    c.close()

    _rewind_to_v7(core)
    assert IDX not in [n for n, _ in _index_rows(core)]
    assert _setup(core) == core.LATEST_SCHEMA_VERSION
    migrated = _index_rows(core)
    assert migrated == fresh  # same names AND same SQL


# ----------------------------------------------------------------- C3


def test_c3_setup_twice_on_a_v8_store_changes_nothing_and_is_silent(core, capsys):
    c = _raw(core)
    assert _version(c) == core.LATEST_SCHEMA_VERSION
    c.close()
    before = _index_rows(core)
    capsys.readouterr()
    assert _setup(core) == core.LATEST_SCHEMA_VERSION
    assert _setup(core) == core.LATEST_SCHEMA_VERSION
    err = capsys.readouterr().err
    assert "migration 8" not in err
    assert _index_rows(core) == before


def test_c3_v7_store_that_already_has_the_index_migrates_to_8_with_one_index(core):
    # crash between CREATE INDEX and the version bump: index present, version 7
    _rewind_to_v7(core, drop_index=False)
    c = _raw(core)
    assert _version(c) == 7
    assert c.execute("SELECT COUNT(*) FROM sqlite_master WHERE type='index' AND name=?", (IDX,)).fetchone()[0] == 1
    c.close()
    assert _setup(core) == core.LATEST_SCHEMA_VERSION
    c = _raw(core)
    assert c.execute("SELECT COUNT(*) FROM sqlite_master WHERE type='index' AND name=?", (IDX,)).fetchone()[0] == 1
    c.close()


# ----------------------------------------------------------------- C4


def test_c4_failing_create_index_stays_at_v7_does_not_crash_and_retries(core, capsys):
    _rewind_to_v7(core)
    conn = _raw(core)
    conn.row_factory = sqlite3.Row
    _deny_create_index(conn)
    capsys.readouterr()
    assert _setup(core, conn) == 7  # did not raise
    err = capsys.readouterr().err
    assert "migration 8" in err and "FAILED" in err, err
    conn.set_authorizer(None)
    conn.close()
    assert IDX not in [n for n, _ in _index_rows(core)]
    # next startup, fault removed
    assert _setup(core) == core.LATEST_SCHEMA_VERSION
    assert IDX in [n for n, _ in _index_rows(core)]


@pytest.mark.skipif(hasattr(os, "geteuid") and os.geteuid() == 0, reason="root ignores directory permissions")
def test_c4_failed_pre_migration_backup_blocks_rung_8(core, capsys):
    c = _raw(core)
    _insert_rows(c, [("n0", "observation", "a row worth protecting", ts(0))])
    c.close()
    _rewind_to_v7(core)
    backup_dir = Path(core.DATA_DIR) / "db-backup"
    backup_dir.mkdir(parents=True, exist_ok=True)
    os.chmod(str(backup_dir), 0o500)
    try:
        capsys.readouterr()
        assert _setup(core) == 7
        err = capsys.readouterr().err
        # the gate fired (a backup was attempted and failed) -- not merely "version happens to be 7"
        assert "pre-migration backup FAILED" in err, err
    finally:
        os.chmod(str(backup_dir), 0o700)
    assert IDX not in [n for n, _ in _index_rows(core)]


# ----------------------------------------------------------------- C5


def _statement_answers(c, hook, since):
    count = c.execute(hook._CHECKPOINT_COUNT_SQL, (since,)).fetchone()[0]
    ids = [r[0] for r in c.execute(hook._CHECKPOINT_LIST_SQL, (since,)).fetchall()]
    return count, ids


def test_c5_answers_unchanged_with_and_without_the_index_on_ties(core, monkeypatch):
    hook = load_ss(Path(core.DATA_DIR), monkeypatch)
    c = _raw(core)
    tie = ts(500)
    rows = []
    for i in range(20):  # before the window
        rows.append((f"pre{i:03d}", "observation", f"before {i}", ts(i)))
    # 260 rows share ONE created_at; ids descend while rowids ascend, so id order != rowid order
    for k in range(260):
        rows.append((f"tie{259 - k:03d}", "observation", f"tied claim {k}", tie))
    for i in range(30):  # after the tie group, plus blank (excluded) rows inside the window
        rows.append((f"post{i:03d}", "observation", f"after {i}", ts(600 + i)))
    for i in range(5):
        rows.append((f"blank{i}", "observation", "   ", ts(550 + i)))
    _insert_rows(c, rows)
    since = ts(500)
    expected_pool = sorted(
        c.execute("SELECT id, created_at, rowid, claim FROM nodes WHERE created_at >= ?", (since,)).fetchall(),
        key=lambda r: (r[1], r[2]),
    )
    expected_pool = [r for r in expected_pool if r[3].strip()]
    expected_count = len(expected_pool)
    expected_ids = [r[0] for r in expected_pool][:200]
    assert expected_count > 200 and len([r for r in expected_pool if r[1] == tie]) >= 250  # the cut is inside the tie

    with_index = _statement_answers(c, hook, since)
    c.execute(f"DROP INDEX {IDX}")
    c.commit()
    without_index = _statement_answers(c, hook, since)
    c.close()
    assert with_index == without_index == (expected_count, expected_ids)


# ----------------------------------------------------------------- C6


def test_c6_a_populated_v7_store_gets_a_pre_migration_backup_before_rung_8(core):
    c = _raw(core)
    _insert_rows(c, [("n0", "observation", "a row worth protecting", ts(0))])
    c.close()
    _rewind_to_v7(core)
    assert _setup(core) == core.LATEST_SCHEMA_VERSION
    backups = sorted((Path(core.DATA_DIR) / "db-backup").glob("pre-migration-v7-*.db"))
    assert len(backups) == 1, [p.name for p in backups]
    b = sqlite3.connect(str(backups[0]))
    try:
        assert _version(b) == 7  # the restore point is the PRE-rung-8 state
    finally:
        b.close()


# ----------------------------------------------------------------- C7


def test_c7_self_heal_recreates_a_lost_index_on_a_v8_store(core, capsys):
    fresh = _index_rows(core)
    c = _raw(core)
    assert _version(c) == core.LATEST_SCHEMA_VERSION
    c.execute(f"DROP INDEX IF EXISTS {IDX}")
    c.commit()
    c.close()
    assert IDX not in [n for n, _ in _index_rows(core)]
    capsys.readouterr()
    assert _setup(core) == core.LATEST_SCHEMA_VERSION
    err = capsys.readouterr().err
    assert _index_rows(core) == fresh  # same names and same SQL as a fresh store
    assert err == "", err


def test_c7_self_heal_does_not_paper_over_a_failed_rung_8(core, capsys):
    _rewind_to_v7(core)
    conn = _raw(core)
    conn.row_factory = sqlite3.Row
    # refuse ONLY the first CREATE INDEX: an ungated self-heal would succeed on its second attempt
    state = _deny_create_index(conn, times=1)
    capsys.readouterr()
    assert _setup(core, conn) == 7
    err = capsys.readouterr().err
    conn.set_authorizer(None)
    conn.close()
    assert state["n"] >= 1
    assert "migration 8" in err and "FAILED" in err, err
    assert IDX not in [n for n, _ in _index_rows(core)]


def test_c7_self_heal_failure_is_loud_not_fatal_and_retries(core, capsys):
    fresh = _index_rows(core)
    c = _raw(core)
    assert _version(c) == core.LATEST_SCHEMA_VERSION
    c.execute(f"DROP INDEX IF EXISTS {IDX}")
    c.commit()
    c.close()
    conn = _raw(core)
    conn.row_factory = sqlite3.Row
    _deny_create_index(conn)
    capsys.readouterr()
    version = _setup(core, conn)  # (1) must not raise
    err = capsys.readouterr().err
    conn.set_authorizer(None)
    conn.close()
    assert err.count("created_at index self-heal FAILED") == 1, err  # (2) loud, exactly once
    assert version == core.LATEST_SCHEMA_VERSION  # (3) the version is untouched
    assert IDX not in [n for n, _ in _index_rows(core)]  # (4) still absent
    # fault removed: the next start restores it, silently
    assert _setup(core) == core.LATEST_SCHEMA_VERSION
    assert _index_rows(core) == fresh
    assert capsys.readouterr().err == ""
