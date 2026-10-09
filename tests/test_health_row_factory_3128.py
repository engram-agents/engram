"""engram-alpha#3128: health reads must not depend on the CALLER's row_factory.

Contract (stated by the spec, asserted here BEFORE the fix existed):

C1. Every public ``conn``-taking function in ``health.metrics`` (the eight
    signal functions) and every ``m_*`` registered in
    ``health.measurements.MEASUREMENTS`` (plus ``compute_all_measurements``)
    returns the SAME value on a PLAIN ``sqlite3.connect(path)`` connection
    (``row_factory is None``, tuple rows) as on a ``sqlite3.Row`` connection
    to the same file, and does not raise.
C2. Same for ``health.engine``'s ``_activity_diagnostics``,
    ``_cascade_marker_age_diagnostics``, ``_compute_diagnostics``,
    ``_count_null_embeddings``, and for the read-only ``health.series``
    functions that take a ``conn`` (the six ``*_needs_*`` predicates and
    ``_dump_table_rows``).
C3. A health call never mutates the caller's connection: ``conn.row_factory``
    is still ``None`` afterwards.
C4. Non-vacuity: the fixture populates every population the functions count,
    so "both sides return 0 / 1.0" cannot be how they agree.
"""

from __future__ import annotations

import inspect
import os
import sqlite3
import sys
from datetime import datetime, timedelta, timezone

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.append(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src", "engram"))

from health import engine as health_engine  # noqa: E402
from health import measurements as hem_l0  # noqa: E402
from health import metrics  # noqa: E402
from health import series  # noqa: E402

from test_health_measurements import _build_populated_fixture  # noqa: E402


def _now_iso(delta: timedelta = timedelta(0)) -> str:
    return (datetime.now(timezone.utc) + delta).strftime("%Y-%m-%dT%H:%M:%SZ")


@pytest.fixture(scope="module")
def graph_db(tmp_path_factory):
    """File-backed copy of the populated measurements fixture, plus rows for
    the engine's activity / cascade-age populations."""
    mem = _build_populated_fixture()
    path = str(tmp_path_factory.mktemp("rf3128") / "graph.db")
    disk = sqlite3.connect(path)
    mem.backup(disk)
    mem.close()
    now = _now_iso()
    old = _now_iso(timedelta(days=-5))
    rows = [
        # (id, type, created_at, status)
        ("q_1", "question", now, "resolved"),
        ("q_2", "question", old, "active"),
        ("pr_1", "prediction", old, "resolved"),
        ("pr_2", "prediction", old, "active"),
        ("cj_9", "conjecture", old, "partially_resolved"),
        ("cj_8", "conjecture", old, "active"),
    ]
    for nid, typ, created, status in rows:
        disk.execute(
            "INSERT INTO nodes (id, type, created_at, status, is_current) VALUES (?, ?, ?, ?, 1)",
            (nid, typ, created, status),
        )
    disk.execute(
        "INSERT INTO edges (source_id, target_id, relation, created_at) VALUES (?, ?, 'supersedes', ?)",
        ("dv_1", "dv_2", now),
    )
    # A pending marker on a CURRENT node with a parseable (5-day-old) stamp.
    disk.execute(
        "INSERT INTO cascade_markers (node_id, cascade_event, root_node, root_op, status, created_at)"
        " VALUES ('dv_1', 'cev_age', 'root_y', 'retract', 'pending', ?)",
        (old,),
    )
    disk.commit()
    disk.close()
    return path


def _plain(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    assert conn.row_factory is None
    return conn


def _named(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    return conn


# ---------------------------------------------------------------------------
# Enumerations (from the modules, with expected-count guards)
# ---------------------------------------------------------------------------

_METRIC_FNS = {
    n: f
    for n, f in inspect.getmembers(metrics, inspect.isfunction)
    if not n.startswith("_")
    and f.__module__ == metrics.__name__
    and "conn" in inspect.signature(f).parameters
}
_MEASUREMENT_FNS = {name: d.compute_fn for name, d in hem_l0.MEASUREMENTS.items()}


def test_enumerations_have_the_expected_size():
    assert len(_METRIC_FNS) == 8, sorted(_METRIC_FNS)
    assert len(_MEASUREMENT_FNS) == 16, sorted(_MEASUREMENT_FNS)


# ---------------------------------------------------------------------------
# C1
# ---------------------------------------------------------------------------


def _assert_same_on_plain_and_row(graph_db, call):
    plain = _plain(graph_db)
    named = _named(graph_db)
    try:
        got_plain = call(plain)
        got_named = call(named)
        assert got_plain == got_named
        assert plain.row_factory is None  # C3
        return got_plain
    finally:
        plain.close()
        named.close()


@pytest.mark.parametrize("name", sorted(_METRIC_FNS))
def test_c1_metric_signal_function(graph_db, name):
    _assert_same_on_plain_and_row(graph_db, _METRIC_FNS[name])


@pytest.mark.parametrize("name", sorted(_MEASUREMENT_FNS))
def test_c1_measurement(graph_db, name):
    _assert_same_on_plain_and_row(graph_db, _MEASUREMENT_FNS[name])


def test_c1_compute_all_measurements(graph_db):
    out = _assert_same_on_plain_and_row(graph_db, hem_l0.compute_all_measurements)
    assert set(_MEASUREMENT_FNS) <= set(out)


# ---------------------------------------------------------------------------
# C2 — engine
# ---------------------------------------------------------------------------


def test_c2_activity_diagnostics(graph_db):
    out = _assert_same_on_plain_and_row(graph_db, health_engine._activity_diagnostics)
    # C4: non-vacuous, every resolution population present and non-trivial.
    assert out["question_resolution"] == {"total": 2, "resolved": 1, "rate": 0.5}


def test_c2_cascade_marker_age_diagnostics(graph_db):
    out = _assert_same_on_plain_and_row(
        graph_db, lambda c: health_engine._cascade_marker_age_diagnostics(c, 0)
    )
    # C4: only the parseable 5-day-old marker counts ('t0' stamps are skipped).
    assert out["count"] == 1
    assert out["max_age_turns"] >= 5


def test_c2_compute_diagnostics(graph_db):
    out = _assert_same_on_plain_and_row(
        graph_db, lambda c: health_engine._compute_diagnostics(c, 0)
    )
    assert out["activity"]["question_resolution"]["total"] == 2
    assert out["cascade_marker_age"]["count"] == 1


def test_c2_count_null_embeddings(graph_db):
    out = _assert_same_on_plain_and_row(
        graph_db, lambda c: health_engine._count_null_embeddings(c, "claim_bearing_nodes")
    )
    assert out > 0  # C4


# ---------------------------------------------------------------------------
# C2 — series (read-only functions taking a conn)
# ---------------------------------------------------------------------------

_LEGACY_DDL = """
CREATE TABLE unresolved_snapshots (
    snapshot_file TEXT PRIMARY KEY, anchor TEXT NULL,
    reason TEXT CHECK(reason IN ('no_child_commit','shared_anchor')), seen_at TEXT);
CREATE TABLE equality_checks (
    commit_sha TEXT NOT NULL, health_major INTEGER NOT NULL, verdict TEXT,
    diff TEXT, checked_at TEXT);
CREATE TABLE derived_points (
    commit_sha TEXT NOT NULL, health_version TEXT NOT NULL, signals TEXT NOT NULL,
    scores TEXT NOT NULL, computed_at TEXT NOT NULL, engine_build TEXT NOT NULL);
CREATE TABLE replay_runs (
    run_id INTEGER PRIMARY KEY AUTOINCREMENT, started_at TEXT,
    reason TEXT CHECK(reason IN ('ingest','initial')));
CREATE TABLE declarations (
    health_version TEXT PRIMARY KEY, signal_defs TEXT, curve_defs TEXT,
    config TEXT, first_seen_commit TEXT);
INSERT INTO declarations VALUES ('v1', '{"fn": "engram.health.x"}', NULL, NULL, 'abc');
"""

_NEEDS_PREDICATES = [
    series._unresolved_snapshots_needs_migration,
    series._equality_checks_needs_migration,
    series._derived_points_needs_migration,
    series._declarations_needs_model_defs_migration,
    series._replay_runs_needs_migration,
]


@pytest.fixture(scope="module")
def legacy_db(tmp_path_factory):
    path = str(tmp_path_factory.mktemp("rf3128s") / "legacy.db")
    conn = sqlite3.connect(path)
    conn.executescript(_LEGACY_DDL)
    conn.commit()
    conn.close()
    return path


@pytest.fixture(scope="module")
def current_store(tmp_path_factory):
    path = str(tmp_path_factory.mktemp("rf3128c") / "series.db")
    series.open_store(path).close()
    return path


@pytest.fixture(scope="module")
def dirty_store(tmp_path_factory):
    """Current-schema store whose declarations row carries the layout-prefix
    artifact ``_declarations_needs_prefix_heal`` looks for."""
    path = str(tmp_path_factory.mktemp("rf3128d") / "dirty.db")
    conn = series.open_store(path)
    conn.execute(
        "INSERT INTO declarations (health_version, signal_defs) VALUES ('v1', ?)",
        ('{"fn": "engram.health.x"}',),
    )
    conn.commit()
    conn.close()
    return path


def test_series_predicate_count():
    # five schema-migration predicates + the prefix-heal predicate below
    assert len(_NEEDS_PREDICATES) == 5


def test_c2_series_declarations_needs_prefix_heal(dirty_store, current_store):
    fn = series._declarations_needs_prefix_heal
    assert _assert_same_on_plain_and_row(dirty_store, fn) is True
    assert _assert_same_on_plain_and_row(current_store, fn) is False


@pytest.mark.parametrize("fn", _NEEDS_PREDICATES, ids=lambda f: f.__name__)
def test_c2_series_needs_predicates(legacy_db, current_store, fn):
    # C4: a legacy store answers True, a current store False -- so agreement
    # with the Row-connection answer is not "both False".
    assert _assert_same_on_plain_and_row(legacy_db, fn) is True
    assert _assert_same_on_plain_and_row(current_store, fn) is False


def test_c2_series_dump_table_rows(legacy_db):
    rows = _assert_same_on_plain_and_row(
        legacy_db, lambda c: series._dump_table_rows(c, "declarations")
    )
    assert [r["health_version"] for r in rows] == ["v1"]


# ---------------------------------------------------------------------------
# C3 (explicit) and C4 (direct values)
# ---------------------------------------------------------------------------


def test_c3_connection_row_factory_untouched_after_every_call(graph_db):
    plain = _plain(graph_db)
    try:
        for fn in list(_METRIC_FNS.values()) + list(_MEASUREMENT_FNS.values()):
            fn(plain)
        hem_l0.compute_all_measurements(plain)
        health_engine._compute_diagnostics(plain, 0)
        assert plain.row_factory is None
    finally:
        plain.close()


def test_c4_direct_values_on_plain_connection(graph_db):
    plain = _plain(graph_db)
    try:
        # Fixture documented in test_health_measurements: 7 claim-bearing
        # nodes, one missing recall_summary, 2 missing recall_keywords.
        cb = hem_l0.m_claim_bearing_current(plain)
        assert cb >= 7
        s = metrics.recall_summary_coverage(plain)
        assert 0.0 < s < 1.0
        assert s == pytest.approx(1.0 - hem_l0.m_claim_bearing_without_recall_summary(plain) / cb)
        k = metrics.recall_keywords_coverage(plain)
        assert 0.0 < k < 1.0
        # fixture's {4} and {2} components, bridged by the supersedes edge
        # the graph_db fixture adds between dv_1 and dv_2.
        assert hem_l0.m_claim_bearing_giant_component_size(plain) == 6
        assert 0.0 < metrics.giant_component_fraction(plain) < 1.0
    finally:
        plain.close()


# ---------------------------------------------------------------------------
# C3 (strong form): row_factory is never READ OR WRITTEN on the connection
# ---------------------------------------------------------------------------


class _RecordingConnection(sqlite3.Connection):
    """Records every access to ``row_factory`` made AFTER ``armed`` is set.

    Writes are caught by ``__setattr__``; reads by ``__getattribute__``. Both
    are Python-level hooks on a Python subclass of the C type, so they see any
    attribute access made from Python code (health code is pure Python). They
    would not see a read made inside the C module itself, which health code
    cannot do on its own.
    """

    armed = False
    accesses: list

    def __setattr__(self, name, value):
        if name == "row_factory" and self.__dict__.get("armed"):
            self.__dict__["accesses"].append(("write", value))
        super().__setattr__(name, value)

    def __getattribute__(self, name):
        if name == "row_factory" and object.__getattribute__(self, "__dict__").get("armed"):
            object.__getattribute__(self, "__dict__")["accesses"].append(("read", None))
        return super().__getattribute__(name)


def _recording(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path, factory=_RecordingConnection)
    conn.__dict__["accesses"] = []
    conn.__dict__["armed"] = True
    return conn


def test_c3_recording_connection_actually_records(graph_db):
    # The instrument must be able to see both kinds of access, or the
    # zero-access assertion below discriminates nothing.
    conn = _recording(graph_db)
    try:
        conn.row_factory = sqlite3.Row
        _ = conn.row_factory
        assert [k for k, _v in conn.__dict__["accesses"]] == ["write", "read"]
    finally:
        conn.close()


def test_c3_row_factory_never_read_or_written_by_any_routed_function(graph_db, legacy_db, dirty_store):
    """A helper that set ``conn.row_factory = Row`` around the call and
    restored it would pass the 'ends None' checks above; this one fails it."""
    conn = _recording(graph_db)
    try:
        for fn in list(_METRIC_FNS.values()) + list(_MEASUREMENT_FNS.values()):
            fn(conn)
        hem_l0.compute_all_measurements(conn)
        health_engine._activity_diagnostics(conn)
        health_engine._cascade_marker_age_diagnostics(conn, 0)
        health_engine._compute_diagnostics(conn, 0)
        health_engine._count_null_embeddings(conn, "claim_bearing_nodes")
        assert conn.__dict__["accesses"] == []
    finally:
        conn.close()
    conn = _recording(legacy_db)
    try:
        for fn in _NEEDS_PREDICATES:
            fn(conn)
        series._dump_table_rows(conn, "declarations")
        assert conn.__dict__["accesses"] == []
    finally:
        conn.close()
    conn = _recording(dirty_store)
    try:
        series._declarations_needs_prefix_heal(conn)
        assert conn.__dict__["accesses"] == []
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# C2 — the two routed series.py migration sites, on a PLAIN connection
# ---------------------------------------------------------------------------


def test_c2_migrate_derived_points_on_plain_connection(legacy_db, tmp_path):
    import shutil

    path = str(tmp_path / "legacy_copy.db")
    shutil.copy(legacy_db, path)
    conn = _plain(path)
    try:
        assert series._derived_points_needs_migration(conn) is True
        series._migrate_derived_points(conn)  # reads COUNT(*) AS c by name
        assert conn.row_factory is None
        cols = [r[1] for r in conn.execute("PRAGMA table_info(derived_points)")]
        assert "l0_source" in cols and "replay_decl_hash" in cols
        assert series._derived_points_needs_migration(conn) is False
    finally:
        conn.close()


def test_c2_migrate_declarations_prefix_heal_on_plain_connection(dirty_store, tmp_path):
    import shutil

    path = str(tmp_path / "dirty_copy.db")
    shutil.copy(dirty_store, path)
    conn = _plain(path)
    try:
        assert series._declarations_needs_prefix_heal(conn) is True
        series._migrate_declarations_prefix_heal(conn)  # reads rows by name
        assert conn.row_factory is None
        assert series._declarations_needs_prefix_heal(conn) is False
        (stored,) = conn.execute(
            "SELECT signal_defs FROM declarations WHERE health_version = 'v1'"
        ).fetchone()
        assert "engram.health." not in stored and "health.x" in stored
    finally:
        conn.close()
