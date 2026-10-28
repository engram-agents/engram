"""#2732: keep planner statistics COMPLETE and FULL (not sampled, not frozen).

Contract (written before the implementation; every cell is a stated behaviour):

  A  After the migration ladder applies ANY rung, a full unscoped ANALYZE runs on the same
     connection before the ladder returns. A run that applies no rung runs no ANALYZE.
  B  The checkpoint seed gate is widened: full unscoped ANALYZE when sqlite_stat1 is absent OR
     any index of a user table has no sqlite_stat1 row.
  C  advance_turn=True (sleep, once a day) runs a full unscoped ANALYZE after the existing work.
     The nap path (advance_turn=False) runs none unless gate B fires.
  D  PRAGMA optimize still runs every checkpoint, AFTER A/B/C.
  E  All best-effort: an ANALYZE failure never breaks the checkpoint nor skips the optimize.

  S1  (defect 1, headline) a new index + a NAP checkpoint leaves nodes.is_current stats full, not
      sampled. On SQLite >= 3.46.0 plain `PRAGMA optimize` re-analyzes the whole table under a
      temporary analysis limit; skipped below 3.46.0 where that does not happen.
      Step that makes it pass: B (the new index has no stat1 row, so the nap seeds fully before
      optimize runs).
  S2  migration v7 -> v8 leaves full stats AND a stat1 row for idx_nodes_created_at (step A).
  S3  defect 2: advance_turn refreshes drifted stats (step C); the nap path leaves them (cheap).
  S4  gate B: a deleted index stat1 row is back, full, after a nap.
  S5  best-effort: ANALYZE failing does not break the checkpoint; PRAGMA optimize still attempted.
  S6  an up-to-date open runs no ANALYZE.
  S2/S3 also assert "unscoped": a NON-nodes table's index gets a full stat1 row (A and C).
  S7  contract D: ANALYZE precedes `PRAGMA optimize` (nap where gate B fires; advance_turn).
  S8  a nap whose only stat-less indexes belong to EMPTY tables runs NO full ANALYZE.
  S9  (round 3, G) 20x growth then a NAP leaves is_current stats full (optimize trigger b).
  S10 (round 3, G) every PRAGMA optimize is issued with the one constant _OPTIMIZE_MASK = 0xffee.

Discriminating controls (recorded in the handoff): (a) drop C -> S3 advance cell only;
(b) gate B back to stat1-absent only -> S4 only; (c) drop A -> S2 only (S1 stays green via B).
"""
from __future__ import annotations

import os
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_TESTS_DIR = str(Path(__file__).resolve().parent)
if _TESTS_DIR not in sys.path:
    sys.path.insert(0, _TESTS_DIR)
from conftest import fresh_module  # noqa: E402

import engram_lifecycle  # noqa: E402

N_ROWS = 4000
NEW_INDEX = "idx_nodes_probe_2732"


@pytest.fixture
def env(monkeypatch):
    """A fresh lifecycle + core against a temp data dir, DB bootstrapped through the real ladder."""
    monkeypatch.setenv("ENGRAM_NO_EMBEDDINGS", "1")
    monkeypatch.delenv("ENGRAM_SKIP_PRE_MIGRATION_BACKUP", raising=False)
    lc = fresh_module(engram_lifecycle)
    core = lc.core
    d = tempfile.mkdtemp(prefix="engram_test_2732_")
    core._configure_paths(d)
    core._ensure_data_dir()
    core._get_db().close()
    try:
        yield lc, core
    finally:
        shutil.rmtree(d, ignore_errors=True)


def _raw(core):
    return sqlite3.connect(str(core.DB_PATH))


def _fill(c, start, count):
    # is_current has two values; the majority value covers 80% of the rows.
    c.executemany(
        "INSERT INTO nodes (id, type, claim, created_at, is_current, status) "
        "VALUES (?,?,?,?,?,'active')",
        [
            (f"n{i:06d}", "observation" if i % 2 else "derivation", f"claim {i}",
             f"2026-10-01T00:00:{i % 60:02d}Z", 1 if i % 5 else 0)
            for i in range(start, start + count)
        ],
    )
    c.commit()


PROBE_TABLE = "probe_2732"
PROBE_INDEX = "idx_probe_2732"


def _add_probe(c, rows):
    """A NON-nodes user table with an index. ``rows`` may be 0 (an empty table)."""
    c.execute(f"CREATE TABLE {PROBE_TABLE} (a INTEGER, b TEXT)")
    c.execute(f"CREATE INDEX {PROBE_INDEX} ON {PROBE_TABLE}(a)")
    c.executemany(f"INSERT INTO {PROBE_TABLE} VALUES (?, 'x')", [(i % 4,) for i in range(rows)])
    c.commit()


def _probe_stat(c):
    r = c.execute("SELECT stat FROM sqlite_stat1 WHERE tbl=? AND idx=?",
                  (PROBE_TABLE, PROBE_INDEX)).fetchone()
    return r[0] if r else None


def _stat(c, idx):
    r = c.execute("SELECT stat FROM sqlite_stat1 WHERE tbl='nodes' AND idx=?", (idx,)).fetchone()
    return r[0] if r else None


def _rows_in_stats(c):
    """The table row count as sqlite_stat1 last saw it (first integer of any nodes index row)."""
    return int(_stat(c, "idx_nodes_created_at").split()[0])


def _seeded(core, rows=N_ROWS):
    c = _raw(core)
    _fill(c, 0, rows)
    c.execute("ANALYZE")
    c.commit()
    return c


def _nap(lc):
    return lc._checkpoint_internal("t", advance_turn=False)


def _advance(lc):
    return lc._checkpoint_internal("t", advance_turn=True)


# ------------------------------------------------------------------ S1


@pytest.mark.skipif(
    sqlite3.sqlite_version_info < (3, 46, 0),
    reason="SQLite < 3.46.0: PRAGMA optimize has no temporary analysis limit, so a new index "
    "does not get the table re-analyzed with sampled statistics (3.46.0 release note)",
)
def test_s1_new_index_then_nap_keeps_is_current_stats_full(env):
    lc, core = env
    c = _seeded(core)
    assert int(_stat(c, "idx_nodes_current").split()[0]) == N_ROWS  # precondition: full
    c.execute(f"CREATE INDEX {NEW_INDEX} ON nodes(claim)")
    c.commit()
    c.close()
    _nap(lc)
    c = _raw(core)
    try:
        avg = int(_stat(c, "idx_nodes_current").split()[1])
    finally:
        c.close()
    assert avg > 0.4 * N_ROWS, f"is_current stats are sampled (avg rows/key {avg}, true {N_ROWS // 2})"


# ------------------------------------------------------------------ S2


def test_s2_migration_rung_leaves_full_stats_and_new_index_row(env):
    lc, core = env
    c = _seeded(core)
    c.execute("DROP INDEX IF EXISTS idx_nodes_created_at")
    c.execute("DELETE FROM sqlite_stat1 WHERE idx='idx_nodes_created_at'")
    c.execute("PRAGMA user_version = 7")
    c.commit()
    _add_probe(c, 400)  # non-nodes, non-empty, indexed, no stat1 row yet
    assert _probe_stat(c) is None
    c.row_factory = sqlite3.Row
    core._run_db_one_time_setup(c)
    c.commit()
    try:
        assert c.execute("PRAGMA user_version").fetchone()[0] == core.LATEST_SCHEMA_VERSION
        assert _stat(c, "idx_nodes_created_at") is not None, "no stat1 row for the rung's new index"
        assert int(_stat(c, "idx_nodes_current").split()[0]) == N_ROWS
        assert int(_stat(c, "idx_nodes_current").split()[1]) == N_ROWS // 2
        # unscoped: a rung's ANALYZE must reach tables other than nodes
        assert _probe_stat(c) == "400 100", _probe_stat(c)
    finally:
        c.close()


# ------------------------------------------------------------------ S3


def test_s3_advance_turn_refreshes_drifted_stats(env):
    lc, core = env
    c = _seeded(core)
    # A non-nodes indexed table whose stat1 row EXISTS but is stale. (An index with no row at
    # all would be rescued by the later PRAGMA optimize, which would mask a scoped ANALYZE.)
    _add_probe(c, 400)
    c.execute("ANALYZE")
    c.commit()
    assert _probe_stat(c) == "400 100"
    c.executemany(f"INSERT INTO {PROBE_TABLE} VALUES (?, 'x')", [(i % 4,) for i in range(1200)])
    c.commit()
    _fill(c, N_ROWS, 2 * N_ROWS)
    assert _rows_in_stats(c) == N_ROWS  # precondition: stats are now stale
    c.close()
    _advance(lc)
    c = _raw(core)
    try:
        assert _rows_in_stats(c) == 3 * N_ROWS
        # unscoped: the daily refresh must reach tables other than nodes
        assert _probe_stat(c) == "1600 400", _probe_stat(c)
    finally:
        c.close()


def test_s3_nap_leaves_drifted_stats_alone(env):
    lc, core = env
    c = _seeded(core)
    _fill(c, N_ROWS, 2 * N_ROWS)
    c.close()
    _nap(lc)
    c = _raw(core)
    try:
        assert _rows_in_stats(c) == N_ROWS, "nap ran a full ANALYZE: it must stay cheap"
    finally:
        c.close()


# ------------------------------------------------------------------ S4


def test_s4_missing_index_stat_row_is_restored_full_by_a_nap(env):
    lc, core = env
    c = _seeded(core)
    c.execute("DELETE FROM sqlite_stat1 WHERE idx='idx_nodes_type'")
    c.commit()
    assert _stat(c, "idx_nodes_type") is None
    c.close()
    _nap(lc)
    c = _raw(core)
    try:
        # two types => a FULL ANALYZE reports exactly N/2 rows per key; on SQLite >= 3.46 the
        # optimize-only rescue of a missing row is sampled (~1001), which this rejects.
        assert _stat(c, "idx_nodes_type").split()[:2] == [str(N_ROWS), str(N_ROWS // 2)]
    finally:
        c.close()


# ------------------------------------------------------------------ S5


def test_s5_failing_analyze_does_not_break_checkpoint_or_skip_optimize(env, monkeypatch):
    lc, core = env
    c = _seeded(core)
    c.close()
    seen = {"analyze": 0, "optimize": 0}
    real_get_db = core._get_db

    def auth(action, arg1, arg2, dbname, source):
        if action == sqlite3.SQLITE_ANALYZE:
            seen["analyze"] += 1
            return sqlite3.SQLITE_DENY
        if action == sqlite3.SQLITE_PRAGMA and arg1 == "optimize":
            seen["optimize"] += 1
        return sqlite3.SQLITE_OK

    def get_db(*a, **k):
        conn = real_get_db(*a, **k)
        conn.set_authorizer(auth)
        return conn

    monkeypatch.setattr(core, "_get_db", get_db)
    out = _advance(lc)  # advance path always attempts the ANALYZE (step C)
    assert '"status": "checkpoint_saved"' in out or "checkpoint_saved" in out, out
    assert seen["analyze"] >= 1, "the test never made ANALYZE fail"
    assert seen["optimize"] >= 1, "PRAGMA optimize was skipped after the ANALYZE failure"


# ------------------------------------------------------------------ S6


def test_s6_up_to_date_open_runs_no_analyze(env):
    lc, core = env
    c = _raw(core)
    c.row_factory = sqlite3.Row
    assert c.execute("PRAGMA user_version").fetchone()[0] == core.LATEST_SCHEMA_VERSION
    stmts = []
    c.set_trace_callback(stmts.append)
    core._run_db_one_time_setup(c)
    c.commit()
    c.set_trace_callback(None)
    c.close()
    analyzes = [s for s in stmts if s.lstrip().upper().startswith("ANALYZE")]
    assert analyzes == [], analyzes
    assert stmts, "trace callback saw nothing: the test would pass vacuously"


# ------------------------------------------------------------------ S7


def _record_order(monkeypatch, core):
    """Record, per checkpoint connection, the TOP-LEVEL statements ENGRAM issues, in order.

    Deliberately NOT an authorizer: SQLITE_ANALYZE would also fire for the ANALYZE that SQLite
    runs INSIDE `PRAGMA optimize`, and that is build-sensitive. Measured: SQLite 3.45.1 re-analyzes
    an EMPTY table that has an index inside optimize (ANALYZE never writes a stat1 row for an empty
    table, so its index always lacks stats); 3.53.0 does not. Contract D is only about the ANALYZE
    statements ENGRAM issues preceding the optimize ENGRAM issues, so only those are recorded.
    The recording proxy mirrors tests/test_health_engine_checkpoint_wiring.py.
    """
    events = []
    real_get_db = core._get_db

    class _RecordingProxy:
        def __init__(self, real):
            self._real = real

        def execute(self, sql, *args, **kwargs):
            head = sql.strip().rstrip(";").strip().upper().replace(" ", "")
            if head.startswith("ANALYZE"):
                events.append("analyze")
            elif head.startswith("PRAGMAOPTIMIZE"):
                events.append("optimize")
            return self._real.execute(sql, *args, **kwargs)

        def __getattr__(self, name):
            return getattr(self._real, name)

    monkeypatch.setattr(core, "_get_db", lambda *a, **k: _RecordingProxy(real_get_db(*a, **k)))
    return events


def _assert_analyze_before_optimize(events):
    assert "analyze" in events, f"no top-level ANALYZE was issued, the order is untested: {events}"
    assert events.count("optimize") == 1, events
    last_analyze = max(i for i, e in enumerate(events) if e == "analyze")
    assert last_analyze < events.index("optimize"), f"ANALYZE issued after PRAGMA optimize: {events}"


def test_s7_nap_where_gate_b_fires_analyzes_before_optimize(env, monkeypatch):
    lc, core = env
    c = _seeded(core)
    c.execute("DELETE FROM sqlite_stat1 WHERE idx='idx_nodes_type'")  # gate B fires
    c.commit()
    c.close()
    events = _record_order(monkeypatch, core)
    _nap(lc)
    _assert_analyze_before_optimize(events)


def test_s7_advance_turn_analyzes_before_optimize(env, monkeypatch):
    lc, core = env
    c = _seeded(core)
    c.close()
    events = _record_order(monkeypatch, core)
    _advance(lc)
    _assert_analyze_before_optimize(events)


# ------------------------------------------------------------------ S8


def test_s8_nap_with_statless_indexes_only_on_empty_tables_runs_no_full_analyze(env):
    lc, core = env
    c = _seeded(core)
    _add_probe(c, 0)  # an explicit index of an EMPTY table: ANALYZE never writes a row for it
    assert _probe_stat(c) is None
    _fill(c, N_ROWS, 2 * N_ROWS)  # drift: a full ANALYZE would be visible as updated nodes stats
    assert _rows_in_stats(c) == N_ROWS
    c.close()
    _nap(lc)
    c = _raw(core)
    try:
        assert _rows_in_stats(c) == N_ROWS, "gate B fired on an empty table's index: nap ran a full ANALYZE"
        assert _probe_stat(c) is None  # still no row: the precondition held throughout
    finally:
        c.close()


# ------------------------------------------------------------------ S9 / S10 (round 3, contract G)

_NEEDS_346 = pytest.mark.skipif(
    sqlite3.sqlite_version_info < (3, 46, 0),
    reason="SQLite < 3.46.0: PRAGMA optimize has no temporary analysis limit (bit 0x10), so it "
    "never writes sampled statistics (3.46.0 release note; measured 3.45.1)",
)


@_NEEDS_346
def test_s9_large_growth_then_nap_keeps_is_current_stats_full(env):
    """Trigger (b): optimize re-analyzes a table whose row count grew 10-fold. Without G that
    ANALYZE runs under a temporary analysis_limit and writes SAMPLED stats. Every index of a
    non-empty table has a stat row, so gate B stays quiet and only optimize can act.
    S9 grows the table 20x, not 10x, because SQLite 3.49.1's 10-fold check did not fire on this
    schema at 10x or 12x and did at 15x (#4040). If optimize ever does not re-analyze, the vacuity
    guard fails loudly rather than passing."""
    lc, core = env
    c = _seeded(core)  # full stats, N_ROWS rows, ~N_ROWS/2 rows per is_current key
    assert int(_stat(c, "idx_nodes_current").split()[1]) == N_ROWS // 2
    growth = 20
    total = growth * N_ROWS
    _fill(c, N_ROWS, total - N_ROWS)
    assert _rows_in_stats(c) == N_ROWS  # precondition: stats are stale
    c.close()
    _nap(lc)
    c = _raw(core)
    try:
        nrows = int(_stat(c, "idx_nodes_current").split()[0])
        avg = int(_stat(c, "idx_nodes_current").split()[1])
    finally:
        c.close()
    assert nrows == total, f"optimize never re-analyzed (stat rows {nrows}); the test is vacuous: raise growth (#4040)"
    assert avg > 0.4 * (total // 2), f"is_current stats are sampled (avg rows/key {avg}, true {total // 2})"


def _optimize_sql_events(monkeypatch, core):
    events = []
    real_get_db = core._get_db

    def get_db(*a, **k):
        conn = real_get_db(*a, **k)
        conn.set_trace_callback(
            lambda s: events.append(s) if s.strip().upper().startswith("PRAGMA OPTIMIZE") else None
        )
        return conn

    monkeypatch.setattr(core, "_get_db", get_db)
    return events


def test_s10_optimize_uses_the_one_mask_constant(env, monkeypatch):
    lc, core = env
    assert lc._OPTIMIZE_MASK == 0xFFEE
    assert lc._OPTIMIZE_MASK == 0xFFFE & ~0x00010  # default mask minus the analysis-limit bit
    c = _seeded(core)
    c.close()
    events = _optimize_sql_events(monkeypatch, core)
    _nap(lc)
    assert events, "no PRAGMA optimize seen: the test would pass vacuously"
    assert all(e.strip().rstrip(";").replace(" ", "").lower() == f"pragmaoptimize({lc._OPTIMIZE_MASK:#x})"
               for e in events), events
