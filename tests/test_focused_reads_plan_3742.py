"""Contract tests for #3742 (part of #3613): the five unordered focused reads plan on
`idx_nodes_focused`, not on `idx_nodes_current` (nearly the whole table) or a full SCAN.

Written BEFORE the implementation (docs/internal/CONTRACT-FIRST-TESTS.md). Design seat: Ariadne
(build spec, issue comment 6150180014); fix shape ruled by Kepler (comment 6150142362):
`ORDER BY focused_at` on the id-list reads, the range form `focused_at >= ''` on the COUNT.
Not `INDEXED BY`, not a partial index, not a schema change.

The contract:

  C1  for each of S1..S5, `EXPLAIN QUERY PLAN` on the statement AS THE CODE HOLDS IT names
      `idx_nodes_focused`; for the four id-list statements (S1, S2, S3, S5) there is also no
      `USE TEMP B-TREE FOR ORDER BY` (an ORDER BY that moved the plan to another index and
      sorts afterwards is not the fix). The statement text is never a copy in this file: it is
      captured from the real function with `set_trace_callback` (the statements take no
      parameters, so the traced text is the literal). S1 is also checked against the owner
      registry, which must hold the identical text.
  C2  old text and new text return the same id set (S4: the same count) on a fixture holding all
      four {focused, not} x {current, not} cases. The OLD text is written inline here: it is the
      baseline being compared against, the one place a copy is correct.
  C3  S4's range form `focused_at >= ''` agrees with `IS NOT NULL` on every value the column can
      hold (ISO text; NULL excluded by both).

PLAN FIXTURE, AND WHY IT RUNS `ANALYZE`: measured on SQLite 3.45.1, the dev plans only reproduce
on a store that carries `sqlite_stat1`, and so does the fix. Without statistics the planner has
no cardinality for `is_current = 1` and keeps `idx_nodes_current` even WITH the ORDER BY (the
only statement that moves without stats is S5). A store that has been through a checkpoint
carries `sqlite_stat1`: engram_lifecycle.py seeds it once with ANALYZE and keeps it with
PRAGMA optimize (#2559), and the two live-store copies measured for this issue (Ariadne's, comment
6150110901, and Sol's) both have it. A store that has never checkpointed does not, and gets the
benefit on S5 only. The fixture runs ANALYZE, so the cells are about the checkpointed store. The smallest fixture that reproduced dev's bad plans on 3.45.1 was
100 current rows + 9 focused; this one uses a few hundred so the plan is not on a knife edge.
"""
from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
_SRC_ENGRAM = str(REPO_ROOT / "src" / "engram")
_HOOKS_SRC = str(REPO_ROOT / "src" / "engram" / "hooks" / "claude")
for _p in (_SRC_ENGRAM, _HOOKS_SRC):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import engram_core  # noqa: E402
import engram_focus  # noqa: E402

TEMPLATES = REPO_ROOT / "src" / "engram" / "engramd_read_templates.json"

# The OLD statements (dev 511195b3), the baseline for C2/C3 only.
OLD_IDS_CURRENT = "SELECT id FROM nodes WHERE focused_at IS NOT NULL AND is_current = 1"
OLD_COUNT = "SELECT COUNT(*) as n FROM nodes WHERE focused_at IS NOT NULL AND is_current = 1"
OLD_IDS_ANY = "SELECT id FROM nodes WHERE focused_at IS NOT NULL"

N_CURRENT = 600       # unfocused + current: makes `is_current = 1` match most of the table
N_NOT_CURRENT = 60    # unfocused + not current
N_FOCUSED_CURRENT = 9
N_FOCUSED_NOT_CURRENT = 3


def _insert(conn, nid, current, focused_at):
    conn.execute(
        "INSERT INTO nodes (id, type, claim, created_at, is_current, focused_at) "
        "VALUES (?, 'observation', ?, '2026-10-01T00:00:00', ?, ?)",
        (nid, "claim " + nid, current, focused_at),
    )


@pytest.fixture
def store(tmp_path, monkeypatch):
    """A store built by the real schema builder, ANALYZEd, holding the four cases."""
    monkeypatch.setenv("ENGRAM_HOME", str(tmp_path))
    db = tmp_path / "knowledge.db"
    conn = sqlite3.connect(str(db))
    conn.row_factory = sqlite3.Row
    engram_core._run_db_one_time_setup(conn)
    for i in range(N_CURRENT):
        _insert(conn, f"ob_u{i:05d}", 1, None)
    for i in range(N_NOT_CURRENT):
        _insert(conn, f"ob_n{i:05d}", 0, None)
    for i in range(N_FOCUSED_CURRENT):
        _insert(conn, f"ob_fc{i:04d}", 1, f"2026-10-12T00:00:{i:02d}")
    for i in range(N_FOCUSED_NOT_CURRENT):
        _insert(conn, f"ob_fn{i:04d}", 0, f"2026-10-12T00:01:{i:02d}")
    conn.commit()
    conn.execute("ANALYZE")
    conn.commit()
    conn.close()
    return db


def _open(db):
    conn = sqlite3.connect(str(db))
    conn.row_factory = sqlite3.Row
    return conn


def _traced(db, seen):
    conn = _open(db)
    conn.set_trace_callback(lambda s: seen.append(s))
    return conn


def _plan(db, sql):
    conn = sqlite3.connect(str(db))
    try:
        return [r[3] for r in conn.execute("EXPLAIN QUERY PLAN " + sql)]
    finally:
        conn.close()


def _focused_reads(seen):
    return [s for s in seen if s.lstrip().startswith("SELECT") and "FROM nodes WHERE focused_at" in s]


# ---- capture: run the REAL function, return the statement(s) it executed ----------------

def capture_s1(db):
    import _hooklib
    seen: list = []
    conn = _traced(db, seen)
    try:
        ids, ok = _hooklib._focused_ids(read_conn=conn)
    finally:
        conn.close()
    assert ok is True
    stmts = _focused_reads(seen)
    assert len(stmts) == 1, stmts
    return stmts[0]


def _run_focus(db, monkeypatch):
    seen: list = []
    conn = _traced(db, seen)
    monkeypatch.setattr(engram_core, "_get_db", lambda: conn)
    out = json.loads(engram_focus._focus_impl("ob_u00000", "3742 plan fixture"))
    assert out["status"] == "ok", out
    stmts = _focused_reads(seen)
    # no active set is loaded, so _clear_active_set_name_if_diverged returns before its read:
    # exactly the two reads of _focus_impl, in source order (current_focus_rows, focused_now)
    assert len(stmts) == 2, stmts
    return stmts


def capture_s2(db, monkeypatch):
    return _run_focus(db, monkeypatch)[0]


def capture_s3(db, monkeypatch):
    return _run_focus(db, monkeypatch)[1]


def capture_s4(db, monkeypatch):
    seen: list = []
    conn = _traced(db, seen)
    monkeypatch.setattr(engram_core, "_get_db", lambda: conn)
    out = json.loads(engram_focus._unfocus_impl("ob_fc0000"))
    assert out["status"] == "ok", out
    stmts = [s for s in seen if "COUNT(*)" in s and "focused_at" in s]
    assert len(stmts) == 1, stmts
    return stmts[0]


def capture_s5(db):
    conn = _open(db)
    ids = [r["id"] for r in conn.execute("SELECT id FROM nodes WHERE focused_at IS NOT NULL")]
    conn.execute(
        "INSERT INTO focus_sets (name, node_ids, created_at) VALUES ('s3742', ?, '2026-10-12T00:00:00')",
        (json.dumps(ids),),
    )
    conn.execute("UPDATE focus_state SET active_set_name = 's3742' WHERE singleton_key = 1")
    conn.commit()
    conn.close()
    seen: list = []
    conn = _traced(db, seen)
    try:
        engram_focus._clear_active_set_name_if_diverged(conn)
    finally:
        conn.close()
    stmts = _focused_reads(seen)
    assert len(stmts) == 1, stmts
    return stmts[0]


# =========================================================================================
# C1 -- plan pin, one cell per statement
# =========================================================================================

def _assert_plan(db, sql, *, id_list):
    plan = _plan(db, sql)
    joined = " | ".join(plan)
    assert "idx_nodes_focused" in joined, f"{sql!r} plans as {plan}"
    if id_list:
        assert "USE TEMP B-TREE FOR ORDER BY" not in joined, f"{sql!r} sorts after the scan: {plan}"


def test_c1_s1_hooklib_focused_ids_plans_on_idx_nodes_focused(store):
    sql = capture_s1(store)
    _assert_plan(store, sql, id_list=True)
    # the owner refuses a statement its registry does not hold: the SAME text, one site
    templates = json.loads(TEMPLATES.read_text())["templates"]
    held = [t for t in templates if t["sql"] == sql]
    assert len(held) == 1, "the owner registry does not hold the hook's exact _focused_ids text"
    assert held[0]["sites"] == ["hooks/claude/_hooklib.py:_focused_ids"]


def test_c1_s2_focus_current_focus_rows_plans_on_idx_nodes_focused(store, monkeypatch):
    _assert_plan(store, capture_s2(store, monkeypatch), id_list=True)


def test_c1_s3_focus_focused_now_plans_on_idx_nodes_focused(store, monkeypatch):
    _assert_plan(store, capture_s3(store, monkeypatch), id_list=True)


def test_c1_s4_unfocus_count_plans_on_idx_nodes_focused(store, monkeypatch):
    # an aggregate: ORDER BY cannot apply, so no temp-sort assertion
    _assert_plan(store, capture_s4(store, monkeypatch), id_list=False)


def test_c1_s5_clear_active_set_name_plans_on_idx_nodes_focused(store):
    _assert_plan(store, capture_s5(store), id_list=True)


# =========================================================================================
# C2 -- same answers, old text vs new text, on the four-case fixture
# =========================================================================================

def _ids(db, sql):
    conn = sqlite3.connect(str(db))
    try:
        return {r[0] for r in conn.execute(sql)}
    finally:
        conn.close()


def _count(db, sql):
    conn = sqlite3.connect(str(db))
    try:
        return conn.execute(sql).fetchone()[0]
    finally:
        conn.close()


def _expected_current():
    return {f"ob_fc{i:04d}" for i in range(N_FOCUSED_CURRENT)}


def test_c2_s1_same_ids(store):
    new = _ids(store, capture_s1(store))
    assert new == _ids(store, OLD_IDS_CURRENT) == _expected_current()


def test_c2_s2_s3_same_ids(store, monkeypatch):
    s2, s3 = _run_focus(store, monkeypatch)
    # _focus_impl focuses ob_u00000 after S2 ran and before S3: both are read on a store
    # that now holds it, so compare with the old text on the same final state
    final = _ids(store, OLD_IDS_CURRENT)
    assert _ids(store, s2) == final
    assert _ids(store, s3) == final == _expected_current() | {"ob_u00000"}


def test_c2_s4_same_count(store, monkeypatch):
    new_sql = capture_s4(store, monkeypatch)
    assert _count(store, new_sql) == _count(store, OLD_COUNT) == N_FOCUSED_CURRENT - 1


def test_c2_s5_same_ids(store):
    new = _ids(store, capture_s5(store))
    assert new == _ids(store, OLD_IDS_ANY) == _expected_current() | {
        f"ob_fn{i:04d}" for i in range(N_FOCUSED_NOT_CURRENT)}


# =========================================================================================
# C3 -- S4's range form against IS NOT NULL
# =========================================================================================

def test_c3_range_form_agrees_with_is_not_null_on_iso_text_and_null(store, monkeypatch):
    new_sql = capture_s4(store, monkeypatch)
    assert "focused_at >= ''" in new_sql
    # ISO text counted by both; NULL excluded by both (the unfocused current rows are NULL)
    assert _count(store, new_sql) == _count(store, OLD_COUNT) > 0
    only_null = sqlite3.connect(":memory:")
    only_null.execute("CREATE TABLE nodes (id TEXT, is_current INT, focused_at TEXT)")
    only_null.execute("INSERT INTO nodes VALUES ('a', 1, NULL)")
    only_null.execute("INSERT INTO nodes VALUES ('b', 1, '2026-10-12T00:00:00+00:00')")
    for sql in (OLD_COUNT, new_sql):
        assert only_null.execute(sql).fetchone()[0] == 1


def test_c3_a_text_affinity_column_cannot_hold_a_non_text_number(store):
    """focused_at is declared TEXT, so SQLite coerces a bound number to text on write: there is
    no integer for the range form to drop. This is why the range form and IS NOT NULL agree."""
    conn = sqlite3.connect(str(store))
    conn.execute("UPDATE nodes SET focused_at = 12345 WHERE id = 'ob_u00001'")
    conn.commit()
    assert conn.execute("SELECT typeof(focused_at) FROM nodes WHERE id = 'ob_u00001'").fetchone()[0] == "text"
    conn.close()
