"""Contract tests for the positional-row chain path (#3665 U6b stage 2a).

``engram_core.generalization_chain`` and the helpers it calls
(``_resolve_to_current``, ``_current_successor``, the two ``json_each``
hydration reads) must read every row by INDEX, never by column name, so the
same function serves three connection shapes with IDENTICAL output:

  (i)   a ``sqlite3.Row`` connection (what the hook uses today),
  (ii)  a plain-tuple connection (``row_factory = None``),
  (iii) a stub whose ``execute(sql, params)`` returns an object offering
        ``fetchone()`` / ``fetchall()`` ONLY, yielding plain tuples (the shape
        the later owner-client shim has: no ``row_factory``, no ``keys()``,
        no cursor iteration).

Also pins the exact set of SQL statements the chain path executes: a changed
or added statement re-keys the byte-exact read registry and must be noticed.
"""

import sqlite3

import pytest

import engram_core as core

# Every statement the chain path executes, found by tracing (Recorder below)
# over all fixtures in this file.
EXPECTED_STATEMENTS = frozenset({
    "SELECT id, type FROM nodes WHERE id = ?",
    "SELECT is_current FROM nodes WHERE id = ?",
    "SELECT superseded_by FROM nodes WHERE id = ?",
    "SELECT superseded_by, is_current FROM nodes WHERE id = ?",
    """SELECT target_id FROM edges
               WHERE source_id = ? AND relation IN ('exemplifies', 'instantiates')""",
    # #3542: the parent read goes through dag_edge_set's LIVE/target template
    # (relations bound via json_each); the literal is pasted from what the Recorder
    # observed, not computed from the module constant.
    (
        "SELECT e.source_id AS source_id, e.target_id AS target_id, "
        "e.relation AS relation FROM edges e JOIN nodes s ON s.id = e.source_id "
        "JOIN nodes t ON t.id = e.target_id WHERE e.relation IN "
        "(SELECT value FROM json_each(?)) AND (s.is_current = 1 AND s.status != "
        "'retracted') AND (t.is_current = 1 AND t.status != 'retracted') AND "
        "e.target_id = ? ORDER BY e.source_id, e.target_id, e.relation"
    ),
    "SELECT id, recall_summary FROM nodes WHERE id IN (SELECT value FROM json_each(?))",
    "SELECT id, claim FROM nodes WHERE id IN (SELECT value FROM json_each(?))",
})


def _new_conn(row_factory):
    conn = sqlite3.connect(":memory:")
    conn.row_factory = row_factory
    conn.execute(
        """CREATE TABLE nodes (
            id TEXT PRIMARY KEY, type TEXT NOT NULL, is_current INTEGER DEFAULT 1,
            status TEXT DEFAULT 'active', superseded_by TEXT, claim TEXT, recall_summary TEXT, created_at TEXT
        )"""
    )
    conn.execute(
        """CREATE TABLE edges (
            id INTEGER PRIMARY KEY AUTOINCREMENT, source_id TEXT, target_id TEXT,
            relation TEXT, created_at TEXT
        )"""
    )
    return conn


def _node(conn, nid, ntype="cornerstone", current=1, sup=None):
    conn.execute(
        "INSERT INTO nodes (id, type, is_current, superseded_by, claim,"
        " recall_summary, created_at) VALUES (?, ?, ?, ?, ?, ?, 't')",
        (nid, ntype, current, sup, "claim:" + nid, "sum:" + nid),
    )


def _edge(conn, src, tgt, rel="generalizes"):
    conn.execute(
        "INSERT INTO edges (source_id, target_id, relation, created_at)"
        " VALUES (?, ?, ?, 't')",
        (src, tgt, rel),
    )


def fx_principle(c):
    _node(c, "ls_1", "lesson")
    _node(c, "cs_1")
    _edge(c, "cs_1", "ls_1")


def fx_concrete(c):
    _node(c, "ob_1", "observation")
    _node(c, "ls_1", "lesson")
    _node(c, "ls_2", "lesson")
    _node(c, "cs_1")
    _edge(c, "ob_1", "ls_1", "exemplifies")
    _edge(c, "ob_1", "ls_2", "instantiates")
    _edge(c, "cs_1", "ls_1")


def fx_dag(c):
    _node(c, "ls_1", "lesson")
    _node(c, "cs_1")
    _node(c, "cs_2")
    _node(c, "ax_1", "axiom")
    _edge(c, "cs_1", "ls_1")
    _edge(c, "cs_2", "ls_1")
    _edge(c, "ax_1", "cs_1")
    _edge(c, "ax_1", "cs_2")


def fx_supersede(c):
    _node(c, "ls_1", "lesson", current=0, sup="ls_2")
    _node(c, "ls_2", "lesson", current=0, sup="ls_3")
    _node(c, "ls_3", "lesson")
    _node(c, "cs_old", current=0, sup="cs_new")
    _node(c, "cs_new")
    _edge(c, "cs_old", "ls_3")


def fx_truncated(c):
    for i in range(7):
        _node(c, "cs_%d" % i)
    for i in range(6):
        _edge(c, "cs_%d" % (i + 1), "cs_%d" % i)


def fx_cycle(c):
    _node(c, "cs_a")
    _node(c, "cs_b")
    _edge(c, "cs_a", "cs_b")
    _edge(c, "cs_b", "cs_a")
    _node(c, "cs_head")
    _edge(c, "cs_head", "cs_a")


def fx_deadend(c):
    _node(c, "ls_1", "lesson")
    _node(c, "cs_gone", current=0)  # retracted: no successor
    _edge(c, "cs_gone", "ls_1")
    _node(c, "ls_x", "lesson", current=0)  # start itself dead-ends


def fx_supersede_cycle(c):
    _node(c, "ls_1", "lesson", current=0, sup="ls_2")
    _node(c, "ls_2", "lesson", current=0, sup="ls_1")


# (name, fixture, start id, max_depth)
CASES = [
    ("principle", fx_principle, "ls_1", 4),
    ("concrete", fx_concrete, "ob_1", 4),
    ("dag", fx_dag, "ls_1", 4),
    ("supersede", fx_supersede, "ls_1", 4),
    ("supersede-from-concrete", fx_supersede, "cs_old", 4),
    ("truncated", fx_truncated, "cs_0", 3),
    ("cycle", fx_cycle, "cs_a", 4),
    ("deadend", fx_deadend, "ls_1", 4),
    ("deadend-start", fx_deadend, "ls_x", 4),
    ("supersede-cycle", fx_supersede_cycle, "ls_1", 4),
    ("missing", fx_principle, "nope", 4),
]
IDS = [c[0] for c in CASES]


class _StubCursor:
    """fetchone()/fetchall() ONLY: no iteration, no description, no keys."""

    def __init__(self, rows):
        self._rows = [tuple(r) for r in rows]

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def fetchall(self):
        return list(self._rows)


class StubConn:
    """execute(sql, params) -> _StubCursor of plain tuples. Nothing else."""

    def __init__(self, tuple_conn):
        self._c = tuple_conn

    def execute(self, sql, params=()):
        return _StubCursor(self._c.execute(sql, params).fetchall())


class Recorder:
    """Proxy logging every SQL text executed; delegates to a real conn."""

    def __init__(self, conn):
        self._c = conn
        self.sql = []

    def execute(self, sql, params=()):
        self.sql.append(sql)
        return self._c.execute(sql, params)


def _run(fixture, start, depth, kind):
    if kind == "row":
        c = _new_conn(sqlite3.Row)
        fixture(c)
        return core.generalization_chain(c, start, max_depth=depth)
    c = _new_conn(None)
    fixture(c)
    if kind == "tuple":
        return core.generalization_chain(c, start, max_depth=depth)
    return core.generalization_chain(StubConn(c), start, max_depth=depth)


@pytest.mark.parametrize("name,fixture,start,depth", CASES, ids=IDS)
def test_row_connection_baseline_is_sane(name, fixture, start, depth):
    out = _run(fixture, start, depth, "row")
    assert out["start_id"] == start


@pytest.mark.parametrize("name,fixture,start,depth", CASES, ids=IDS)
def test_plain_tuple_connection_equals_row_connection(name, fixture, start, depth):
    assert _run(fixture, start, depth, "tuple") == _run(fixture, start, depth, "row")


@pytest.mark.parametrize("name,fixture,start,depth", CASES, ids=IDS)
def test_stub_connection_equals_row_connection(name, fixture, start, depth):
    assert _run(fixture, start, depth, "stub") == _run(fixture, start, depth, "row")


def test_fixtures_cover_the_interesting_shapes():
    outs = {n: _run(f, s, d, "row") for n, f, s, d in CASES}
    assert outs["principle"]["heads"] == ["cs_1"]
    assert outs["concrete"]["roots"] == ["ls_1", "ls_2"]
    assert len(outs["dag"]["nodes"]["ls_1"]["parents"]) == 2
    assert outs["supersede"]["start_resolved_id"] == "ls_3"
    assert outs["truncated"]["truncated"] is True
    assert outs["cycle"]["cycle_detected"] is True
    assert outs["deadend"]["heads"] == ["ls_1"]
    assert outs["deadend-start"]["start_resolved_id"] is None
    assert outs["missing"]["nodes"] == {}


def test_executed_statement_set_is_exactly_the_known_set():
    seen = set()
    for _n, fixture, start, depth in CASES:
        c = _new_conn(sqlite3.Row)
        fixture(c)
        rec = Recorder(c)
        core.generalization_chain(rec, start, max_depth=depth)
        seen.update(rec.sql)
    assert seen == EXPECTED_STATEMENTS
