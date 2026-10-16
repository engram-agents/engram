"""Contract tests for #2746: ``rebuild_fts_index`` honours the FTS sync
triggers' contract -- a SUPERSEDED row (``is_current=0``, not retracted) is
absent from ``nodes_fts`` after a rebuild, exactly as
``nodes_fts_supersede_remove`` leaves it on the live-write path.

Written BEFORE the implementation (contract-first). Cells:

  C1 superseded row absent after rebuild; a current row is still found
  C2 row both retracted AND non-current: rebuild must not raise
     (a second 'delete' on an already-excluded rowid is SQLITE_CORRUPT_VTAB)
  C3 retracted-but-current row stays excluded (pre-existing behaviour)
  C4 legacy fixtures without ``is_current`` / without ``status`` still rebuild
  C5 idempotent: a second rebuild raises nothing and changes nothing
  C6 live-trigger path and rebuild path index the same rowids
  C7 the #2678 detector reports ok on a rebuilt graph holding a superseded row
"""

from __future__ import annotations

import os
import sqlite3
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import engram_backup  # noqa: E402

CURRENT = "quokkaalpha"        # current + active  -> must be indexed
SUPERSEDED = "wombatsuper"     # is_current=0, active -> must NOT be indexed
BOTH = "narwhalboth"           # retracted AND is_current=0 -> must NOT be indexed
RETRACTED = "ocelotretr"       # retracted, is_current=1 -> must NOT be indexed
TERMS = (CURRENT, SUPERSEDED, BOTH, RETRACTED)

_ROWS = [
    # id, term, status, is_current
    ("ob_cur", CURRENT, "active", 1),
    ("ob_sup", SUPERSEDED, "active", 0),
    ("ob_both", BOTH, "retracted", 0),
    ("ob_ret", RETRACTED, "retracted", 1),
]


def _build(path: str, *, with_status: bool = True, with_is_current: bool = True,
           rows=_ROWS) -> None:
    """nodes table only (no FTS): rebuild_fts_index creates the index itself,
    as it does after a restore. Rows are inserted in their final state."""
    cols = ["id TEXT PRIMARY KEY", "claim TEXT", "quoted_text TEXT", "interpretation TEXT"]
    if with_status:
        cols.append("status TEXT DEFAULT 'active'")
    if with_is_current:
        cols.append("is_current INTEGER DEFAULT 1")
    conn = sqlite3.connect(path)
    conn.execute(f"CREATE TABLE nodes ({', '.join(cols)})")
    for nid, term, status, is_cur in rows:
        names = ["id", "claim", "quoted_text", "interpretation"]
        vals = [nid, f"claim {term}", "q", "i"]
        if with_status:
            names.append("status")
            vals.append(status)
        if with_is_current:
            names.append("is_current")
            vals.append(is_cur)
        conn.execute(
            f"INSERT INTO nodes({', '.join(names)}) VALUES ({', '.join('?' * len(vals))})",
            vals,
        )
    conn.commit()
    conn.close()


def _found(db: str, term: str) -> list[int]:
    conn = sqlite3.connect(db)
    try:
        return sorted(
            r[0]
            for r in conn.execute(
                "SELECT rowid FROM nodes_fts WHERE nodes_fts MATCH ?", (term,)
            ).fetchall()
        )
    finally:
        conn.close()


def _snapshot(db: str) -> dict[str, list[int]]:
    return {t: _found(db, t) for t in TERMS}


@pytest.fixture
def db(tmp_path):
    p = str(tmp_path / "knowledge.db")
    _build(p)
    return p


def test_c1_superseded_row_absent_current_row_found(db):
    assert engram_backup.rebuild_fts_index(db) is True
    assert _found(db, SUPERSEDED) == [], "superseded row must not be indexed (#2746)"
    assert len(_found(db, CURRENT)) == 1, "control: current row must be indexed"


def test_c2_retracted_and_noncurrent_row_does_not_raise_and_is_absent(db):
    # One query / one delete pass: two passes would double-'delete' this rowid
    # and raise SQLITE_CORRUPT_VTAB.
    engram_backup.rebuild_fts_index(db)
    assert _found(db, BOTH) == []
    # A doubled 'delete' does not necessarily raise at rebuild time: it can
    # silently corrupt the index so that RANKED queries raise later
    # (measured: a two-pass implementation passes the plain MATCH above).
    # So also exercise the ranked path on every term.
    conn = sqlite3.connect(db)
    try:
        for t in TERMS:
            conn.execute(
                "SELECT rowid FROM nodes_fts WHERE nodes_fts MATCH ? ORDER BY rank", (t,)
            ).fetchall()
        conn.execute("SELECT rowid FROM nodes_fts WHERE nodes_fts MATCH 'claim' ORDER BY rank").fetchall()
    finally:
        conn.close()


def test_c3_retracted_but_current_row_still_excluded(db):
    engram_backup.rebuild_fts_index(db)
    assert _found(db, RETRACTED) == []


def test_c4_legacy_fixture_without_is_current_still_rebuilds(tmp_path):
    p = str(tmp_path / "legacy_no_is_current.db")
    _build(p, with_is_current=False)
    assert engram_backup.rebuild_fts_index(p) is True
    assert len(_found(p, CURRENT)) == 1
    assert len(_found(p, SUPERSEDED)) == 1  # no is_current column -> nothing to exclude on
    assert _found(p, RETRACTED) == []       # status guard still applies
    assert _found(p, BOTH) == []


def test_c4_legacy_fixture_without_status_still_rebuilds(tmp_path):
    p = str(tmp_path / "legacy_no_status.db")
    _build(p, with_status=False)
    assert engram_backup.rebuild_fts_index(p) is True
    assert len(_found(p, CURRENT)) == 1
    assert _found(p, SUPERSEDED) == []      # is_current guard still applies
    assert _found(p, BOTH) == []
    assert len(_found(p, RETRACTED)) == 1   # no status column -> only is_current=0 excluded


def test_c4_legacy_fixture_with_neither_column_indexes_everything(tmp_path):
    p = str(tmp_path / "legacy_neither.db")
    _build(p, with_status=False, with_is_current=False)
    assert engram_backup.rebuild_fts_index(p) is True
    for t in TERMS:
        assert len(_found(p, t)) == 1


def test_c5_rebuild_is_idempotent(db):
    engram_backup.rebuild_fts_index(db)
    first = _snapshot(db)
    engram_backup.rebuild_fts_index(db)
    assert _snapshot(db) == first
    assert first[SUPERSEDED] == first[BOTH] == first[RETRACTED] == []
    assert len(first[CURRENT]) == 1


def _build_live_with_real_triggers(path: str) -> None:
    """Live-write path: nodes + nodes_fts + the REAL five sync triggers from
    engram_core, then insert current rows and drive supersede/retract through
    UPDATEs (retract sets status AND is_current in one statement, as
    _retract_impl does)."""
    import engram_core

    cols = list(engram_backup.NODES_FTS_COLUMNS)
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE nodes (id TEXT PRIMARY KEY, "
        + ", ".join(f"{c} TEXT" for c in cols)
        + ", status TEXT DEFAULT 'active', is_current INTEGER DEFAULT 1)"
    )
    conn.execute(
        f"CREATE VIRTUAL TABLE nodes_fts USING fts5({', '.join(cols)}, "
        "content='nodes', content_rowid='rowid')"
    )
    engram_core._recreate_fts_sync_triggers(conn, cols, drop_first=False)
    for nid, term, _status, _cur in _ROWS:
        conn.execute("INSERT INTO nodes(id, claim) VALUES (?, ?)", (nid, f"claim {term}"))
    conn.execute("UPDATE nodes SET is_current=0 WHERE id='ob_sup'")
    conn.execute("UPDATE nodes SET status='retracted', is_current=0 WHERE id='ob_both'")
    conn.execute("UPDATE nodes SET status='retracted' WHERE id='ob_ret'")
    conn.commit()
    conn.close()


def test_c6_rebuild_agrees_with_live_trigger_path(tmp_path):
    live = str(tmp_path / "live.db")
    _build_live_with_real_triggers(live)
    live_snapshot = _snapshot(live)
    # Sanity: the trigger path itself excludes all three non-indexable rows.
    assert live_snapshot[SUPERSEDED] == live_snapshot[BOTH] == live_snapshot[RETRACTED] == []
    assert len(live_snapshot[CURRENT]) == 1

    # Rebuild a copy of the same final graph (the 6-column live schema).
    copy = str(tmp_path / "copy.db")
    src = sqlite3.connect(live)
    dst = sqlite3.connect(copy)
    dst.execute(
        "CREATE TABLE nodes (id TEXT PRIMARY KEY, "
        + ", ".join(f"{c} TEXT" for c in engram_backup.NODES_FTS_COLUMNS)
        + ", status TEXT DEFAULT 'active', is_current INTEGER DEFAULT 1)"
    )
    for row in src.execute("SELECT * FROM nodes ORDER BY rowid"):
        dst.execute(f"INSERT INTO nodes VALUES ({', '.join('?' * len(row))})", row)
    dst.commit()
    src.close()
    dst.close()
    engram_backup.rebuild_fts_index(copy)
    assert _snapshot(copy) == live_snapshot


def test_c7_detector_reports_ok_on_rebuilt_graph_with_superseded_row(db):
    engram_backup.rebuild_fts_index(db)
    conn = sqlite3.connect(db)
    conn.execute("CREATE VIRTUAL TABLE nodes_fts_vocab USING fts5vocab('nodes_fts', 'row')")
    result = engram_backup.check_fts_integrity(conn)
    conn.close()
    assert result["status"] == "ok", result
    assert result["probed_terms"] > 0


# ─── Real migration rung 4 (engram_core) ───────────────────────────────────
# Rung 4 delegates its FTS rebuild to rebuild_fts_index. Before #2746 it then
# re-asserted the superseded-row exclusion itself; once rebuild_fts_index
# excludes them too, that second 'delete' would hit an already-excluded rowid
# and silently corrupt ranked queries (#2742). Harness mirrors
# tests/test_engram_backup.py's migration-4 tests (real _get_db() over the
# production schema, regressed to the pre-rung-4 user_version=3 shape).

_M_SUPERSEDED = "wombatmigsuper"
_M_CURRENT = "quokkamigcurrent"
_M_RETRACTED = "ocelotmigretracted"


def _migration4_core_with_rows(tmp_dir):
    import importlib

    tests_dir = os.path.dirname(os.path.abspath(__file__))
    if tests_dir not in sys.path:
        sys.path.insert(0, tests_dir)
    from conftest import fresh_module
    from test_engram_backup import _build_pre_migration4_truncated_feeling_db

    engram_core = importlib.import_module("engram_core")
    core = fresh_module(engram_core)
    os.environ["ENGRAM_NO_EMBEDDINGS"] = "1"
    core._configure_paths(tmp_dir)
    core._ensure_data_dir()
    _build_pre_migration4_truncated_feeling_db(core)

    raw = sqlite3.connect(str(core.DB_PATH))
    for nid, term, status, is_cur in (
        ("ob_mig_sup", _M_SUPERSEDED, "active", 0),
        ("ob_mig_cur", _M_CURRENT, "active", 1),
        ("ob_mig_ret", _M_RETRACTED, "retracted", 1),
    ):
        raw.execute(
            "INSERT INTO nodes (id, type, created_at, claim, status, is_current) "
            "VALUES (?, 'observation', '2026-08-21T00:00:00Z', ?, ?, ?)",
            (nid, f"claim {term}", status, is_cur),
        )
    # Leave the CURRENT row as the only indexable doc. A doubled 'delete' only
    # makes ranked queries RAISE when the index is a single document. With
    # extra indexed rows it does not raise, but bm25() silently returns NULL
    # (ranking degraded; see test_c10 below), so the fixture is built to make
    # the defect loud.
    raw.execute("DELETE FROM nodes WHERE id = 'fl_migration4_round3'")
    raw.execute("INSERT INTO nodes_fts(nodes_fts) VALUES ('rebuild')")
    raw.commit()
    # The raw 'rebuild' above indexes every row (rung 4's own rebuild_fts_index
    # does the excluding), so count the rows that SHOULD stay indexed.
    indexed = raw.execute(
        "SELECT count(*) FROM nodes WHERE status IS NOT 'retracted' AND is_current IS NOT 0"
    ).fetchone()[0]
    assert indexed == 1, (
        f"C8/C9 fixture must leave exactly ONE indexed document (found {indexed}): "
        "the double-delete only raises on a one-document index, so a renamed "
        "helper row or an extra indexed row would silently weaken the cells"
    )
    assert raw.execute("PRAGMA user_version").fetchone()[0] == 3
    raw.close()
    core._db_setup_done_paths.clear()
    return core


def _assert_migration4_index_correct(conn):
    def both(term):
        plain = conn.execute(
            "SELECT rowid FROM nodes_fts WHERE nodes_fts MATCH ?", (term,)
        ).fetchall()
        ranked = conn.execute(
            "SELECT rowid FROM nodes_fts WHERE nodes_fts MATCH ? ORDER BY rank", (term,)
        ).fetchall()
        return plain, ranked

    # Ranked query on the term shared by every fixture row: this is the one
    # that raises SQLITE_CORRUPT_VTAB after a doubled 'delete'.
    conn.execute(
        "SELECT rowid FROM nodes_fts WHERE nodes_fts MATCH 'claim' ORDER BY rank"
    ).fetchall()
    assert both(_M_SUPERSEDED) == ([], []), "superseded term must be absent (plain and ranked)"
    assert both(_M_RETRACTED) == ([], []), "retracted term must be absent (plain and ranked)"
    plain, ranked = both(_M_CURRENT)
    assert len(plain) == 1 and len(ranked) == 1, "current term must be found (plain and ranked)"
    result = engram_backup.check_fts_integrity(conn)
    assert result["status"] == "ok", result


def test_c8_real_migration4_does_not_double_delete_superseded_rows():
    import shutil
    import tempfile

    d = tempfile.mkdtemp(prefix="engram_test_2746_rung4_")
    try:
        core = _migration4_core_with_rows(d)
        conn = core._get_db()
        assert conn.execute("PRAGMA user_version").fetchone()[0] >= 4
        _assert_migration4_index_correct(conn)
        conn.close()
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_c9_real_migration4_retry_after_failed_first_attempt(monkeypatch):
    """First attempt faults right after the real rebuild (stays at v3); the
    retry runs the whole rung again and must still leave a correct index."""
    import shutil
    import tempfile

    d = tempfile.mkdtemp(prefix="engram_test_2746_rung4_retry_")
    try:
        core = _migration4_core_with_rows(d)
        live = sys.modules["engram_backup"]
        original = live.rebuild_fts_index

        def _faulty(db_path, *a, **kw):
            original(db_path, *a, **kw)
            raise sqlite3.DatabaseError("simulated crash right after rebuild")

        monkeypatch.setattr(live, "rebuild_fts_index", _faulty)
        conn = core._get_db()
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 3
        conn.close()

        monkeypatch.setattr(live, "rebuild_fts_index", original)
        core._db_setup_done_paths.clear()
        conn = core._get_db()
        assert conn.execute("PRAGMA user_version").fetchone()[0] >= 4
        _assert_migration4_index_correct(conn)
        conn.close()
    finally:
        shutil.rmtree(d, ignore_errors=True)


# ─── verify_roundtrip structural parity (reviewer round 1, B1) ────────────
# verify_roundtrip rebuilds a recon DB with rebuild_fts_index and asserts that
# nodes_fts_docsize holds exactly one row per INDEXABLE node. Indexable now
# means: not retracted AND not superseded (is_current=0), matching the sync
# triggers. `IS NOT` keeps NULL-valued rows counted, as the triggers do.


def _roundtrip_source(path: str, *, rows, with_is_current=True, with_status=True) -> None:
    _build(path, rows=rows, with_is_current=with_is_current, with_status=with_status)
    conn = sqlite3.connect(path)
    conn.execute("ALTER TABLE nodes ADD COLUMN embedding TEXT")
    conn.execute(
        "CREATE VIRTUAL TABLE nodes_fts USING fts5(claim, quoted_text, interpretation, "
        "content='nodes', content_rowid='rowid')"
    )
    conn.execute("INSERT INTO nodes_fts(nodes_fts) VALUES ('rebuild')")
    conn.commit()
    conn.close()


_V_ROWS = [
    ("ob_a", CURRENT, "active", 1),
    ("ob_b", "quokkabeta", "active", 1),
    ("ob_sup", SUPERSEDED, "active", 0),
    ("ob_ret", RETRACTED, "retracted", 1),
]


def test_v1_verify_roundtrip_ok_with_superseded_and_retracted_rows(tmp_path):
    p = str(tmp_path / "src.db")
    _roundtrip_source(p, rows=_V_ROWS)
    report = engram_backup.verify_roundtrip(p)  # must not raise AssertionError
    assert report is not None


def test_v2_null_is_current_row_counts_as_indexed(tmp_path):
    p = str(tmp_path / "src.db")
    _roundtrip_source(p, rows=_V_ROWS + [("ob_null", "quokkanull", "active", None)])
    engram_backup.verify_roundtrip(p)  # parity must hold with the NULL row indexed
    rb = str(tmp_path / "rb.db")
    _build(rb, rows=_V_ROWS + [("ob_null", "quokkanull", "active", None)])
    engram_backup.rebuild_fts_index(rb)
    assert len(_found(rb, "quokkanull")) == 1


def test_v3_legacy_fixture_without_is_current_passes(tmp_path):
    p = str(tmp_path / "src.db")
    _roundtrip_source(p, rows=_V_ROWS, with_is_current=False)
    engram_backup.verify_roundtrip(p)


def test_v3b_legacy_fixture_without_status_passes(tmp_path):
    p = str(tmp_path / "src.db")
    _roundtrip_source(p, rows=_V_ROWS, with_status=False)
    engram_backup.verify_roundtrip(p)


def test_c10_documents_silent_form_of_the_old_rung4_double_delete(tmp_path):
    """Why rung 4's re-assert had to go (documentation, not a regression
    guard for live code): run the OLD rung-4 statement by hand after a
    rebuild that already excluded the superseded row. With one extra indexed
    row nothing raises, but bm25() returns NULL (ranking silently degraded)
    where a clean rebuild returns a real score."""
    clean = str(tmp_path / "clean.db")
    dirty = str(tmp_path / "dirty.db")
    rows = _ROWS + [("ob_extra", "quokkaextra", "active", 1)]
    for p in (clean, dirty):
        _build(p, rows=rows)
        engram_backup.rebuild_fts_index(p)

    cols = ["claim", "quoted_text", "interpretation"]
    sel = ", ".join(f"COALESCE({c}, '')" for c in cols)
    conn = sqlite3.connect(dirty)
    conn.execute(
        f"INSERT INTO nodes_fts(nodes_fts, rowid, {', '.join(cols)}) "
        f"SELECT 'delete', rowid, {sel} FROM nodes "
        "WHERE is_current = 0 AND status != 'retracted'"
    )
    conn.commit()
    conn.close()

    def bm25s(db):
        # 'claim' is the token every fixture row shares.
        c = sqlite3.connect(db)
        try:
            return [
                r[0]
                for r in c.execute(
                    "SELECT bm25(nodes_fts) FROM nodes_fts WHERE nodes_fts MATCH 'claim'"
                ).fetchall()
            ]
        finally:
            c.close()

    clean_scores = bm25s(clean)
    assert clean_scores and all(v is not None for v in clean_scores)
    dirty_scores = bm25s(dirty)
    assert dirty_scores and all(v is None for v in dirty_scores), (
        "double delete should have silently NULLed bm25 (no exception raised)"
    )


# ─── One shared predicate helper (Borges colleague review) ────────────────
# engram_backup._fts_row_predicates(columns) -> (exclude_sql, indexable_sql)
# is the single source of "which rows are excluded from / belong in nodes_fts",
# used by rebuild_fts_index step 2 (exclude_sql) and verify_roundtrip's parity
# count (indexable_sql). The two are deliberately NOT mechanical negations.

import itertools  # noqa: E402

_EXPECTED_PREDICATES = {
    (False, False): (None, None),
    (True, False): ("status = 'retracted'", "status IS NOT 'retracted'"),
    (False, True): ("is_current = 0", "is_current IS NOT 0"),
    (True, True): (
        "status = 'retracted' OR is_current = 0",
        "status IS NOT 'retracted' AND is_current IS NOT 0",
    ),
}


@pytest.mark.parametrize("has_status,has_is_current", list(_EXPECTED_PREDICATES))
def test_h1_helper_returns_exact_predicates_per_column_combination(has_status, has_is_current):
    cols = {"id", "claim", "quoted_text", "interpretation"}
    if has_status:
        cols.add("status")
    if has_is_current:
        cols.add("is_current")
    assert engram_backup._fts_row_predicates(cols) == _EXPECTED_PREDICATES[(has_status, has_is_current)]


@pytest.mark.parametrize("has_status,has_is_current", list(_EXPECTED_PREDICATES))
def test_h2_exclusion_and_indexable_are_an_exact_partition_even_with_nulls(
    tmp_path, has_status, has_is_current
):
    """Every status x is_current value combination, NULLs included (plain,
    superseded, retracted, retracted+superseded, NULL status, NULL is_current,
    all-NULL): each row is in exactly one of exclude / indexable."""
    cols = ["id TEXT"]
    names = ["id"]
    if has_status:
        cols.append("status TEXT")
        names.append("status")
    if has_is_current:
        cols.append("is_current INTEGER")
        names.append("is_current")
    p = str(tmp_path / "part.db")
    conn = sqlite3.connect(p)
    conn.execute(f"CREATE TABLE nodes ({', '.join(cols)})")
    i = 0
    for st, cur in itertools.product(("active", "retracted", None), (1, 0, None)):
        vals = [f"r{i}"]
        if has_status:
            vals.append(st)
        if has_is_current:
            vals.append(cur)
        conn.execute(
            f"INSERT INTO nodes({', '.join(names)}) VALUES ({', '.join('?' * len(vals))})", vals
        )
        i += 1
    exclude_sql, indexable_sql = engram_backup._fts_row_predicates(set(names))
    total = conn.execute("SELECT count(*) FROM nodes").fetchone()[0]
    if exclude_sql is None:
        assert indexable_sql is None
        conn.close()
        return
    n_ex = conn.execute(f"SELECT count(*) FROM nodes WHERE {exclude_sql}").fetchone()[0]
    n_ix = conn.execute(f"SELECT count(*) FROM nodes WHERE {indexable_sql}").fetchone()[0]
    both = conn.execute(
        f"SELECT count(*) FROM nodes WHERE ({exclude_sql}) AND ({indexable_sql})"
    ).fetchone()[0]
    conn.close()
    assert both == 0, "a row is both excluded and indexable"
    assert n_ex + n_ix == total, f"not a partition: {n_ex} + {n_ix} != {total}"


def test_h3a_rebuild_fts_index_consumes_the_helper(db, monkeypatch):
    """Behavioural spy (least brittle: survives refactors of the function
    body, fails only if the predicate is no longer the helper's). The helper
    is swapped for an unmistakable predicate (exclude rowid 1 regardless of
    status). rebuild_fts_index honours it only if step 2 takes its WHERE from
    the helper; a hand-written predicate would exclude the status/is_current
    rows instead."""
    calls = []

    def fake(columns):
        calls.append(set(columns))
        return ("rowid = 1", "rowid IS NOT 1")

    monkeypatch.setattr(engram_backup, "_fts_row_predicates", fake)
    engram_backup.rebuild_fts_index(db)
    assert calls, "rebuild_fts_index never asked the helper"
    assert _found(db, CURRENT) == [], "rowid 1 must be excluded by the helper's predicate"
    assert len(_found(db, SUPERSEDED)) == 1, "only the helper's predicate may apply"
    assert len(_found(db, RETRACTED)) == 1


def test_h3b_verify_roundtrip_consumes_the_helper(tmp_path, monkeypatch):
    """Same swapped helper. The recon rebuild excludes rowid 1, so docsize is
    3 of the 4 rows; the parity count passes only if it too reads the
    helper's indexable predicate (a hand-written one would expect 2)."""
    p = str(tmp_path / "src.db")
    _roundtrip_source(p, rows=_V_ROWS)
    calls = []

    def fake(columns):
        calls.append(set(columns))
        return ("rowid = 1", "rowid IS NOT 1")

    monkeypatch.setattr(engram_backup, "_fts_row_predicates", fake)
    engram_backup.verify_roundtrip(p)  # AssertionError if the parity count ignores the helper
    assert len(calls) >= 2  # recon rebuild + parity count
