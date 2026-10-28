"""Contract tests for generalization_chain's two hydration reads (#3324, U4c-0).

The two ``IN (...)`` hydration reads inside ``engram_core.generalization_chain``
must use ONE fixed SQL template each, with the reached set bound as a single
JSON-array parameter::

    SELECT id, recall_summary FROM nodes WHERE id IN (SELECT value FROM json_each(?))
    SELECT id, claim FROM nodes WHERE id IN (SELECT value FROM json_each(?))

Why: the downstream byte-exact read registry (#3196) can admit a fixed template
and cannot admit an f-string whose text varies with set size; and a single
bound parameter removes SQLite's variable-count limit from this path.

Contract asserted here:
  1. Behaviour equals the old per-placeholder f-string implementation (kept as
     a private oracle below) for empty / one / many / hostile-character /
     absent / over-999 id sets.
  2. Exactly ONE json_each read per column when the set is non-empty, ZERO
     when empty, and the count does not grow with chain size.
  3. Template shape: both literals present verbatim; no f-string or
     concatenated SQL argument to any ``execute`` inside the function.
  4. Loudness: nothing swallows ``sqlite3.OperationalError`` around the reads;
     a missing json_each raises rather than returning empty.
"""

import ast
import inspect
import sqlite3
import textwrap

import pytest

import engram_core as core

SQL_SUMMARY = (
    "SELECT id, recall_summary FROM nodes WHERE id IN (SELECT value FROM json_each(?))"
)
SQL_CLAIM = "SELECT id, claim FROM nodes WHERE id IN (SELECT value FROM json_each(?))"


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------

def _new_conn():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
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


def _add_node(conn, nid, ntype="cornerstone"):
    conn.execute(
        "INSERT INTO nodes (id, type, is_current, claim, recall_summary, created_at)"
        " VALUES (?, ?, 1, ?, ?, 't')",
        (nid, ntype, f"claim:{nid}", f"sum:{nid}"),
    )


def _add_edge(conn, src, tgt):
    conn.execute(
        "INSERT INTO edges (source_id, target_id, relation, created_at)"
        " VALUES (?, ?, 'generalizes', 't')",
        (src, tgt),
    )


def _fan(parent_ids, absent=()):
    """lesson ls_X generalized by every parent id (all heads). ``absent`` ids
    get an edge but no node row."""
    conn = _new_conn()
    _add_node(conn, "ls_X", "lesson")
    for pid in parent_ids:
        _add_node(conn, pid)
        _add_edge(conn, pid, "ls_X")
    for pid in absent:
        _add_edge(conn, pid, "ls_X")
    return conn


def _old_hydrate(conn, nodes, heads):
    """Private copy of the PRE-#3324 f-string hydration logic (the oracle)."""
    summaries, claims = {}, {}
    if nodes:
        _ph = ",".join("?" * len(nodes))
        for row in conn.execute(
            f"SELECT id, recall_summary FROM nodes WHERE id IN ({_ph})", tuple(nodes)
        ):
            if row[0] in nodes:
                summaries[row[0]] = row[1]
    if heads:
        _ph = ",".join("?" * len(heads))
        for row in conn.execute(
            f"SELECT id, claim FROM nodes WHERE id IN ({_ph})", tuple(heads)
        ):
            if row[0] in nodes:
                claims[row[0]] = row[1]
    return summaries, claims


# ---------------------------------------------------------------------------
# 1. equivalence with the old implementation
# ---------------------------------------------------------------------------

_HOSTILE = ['nd_ünï', 'nd_"dq"', "nd_'sq'", "nd_back\\slash", "nd_a,b", "nd_]["]

EQUIV_CASES = {
    "lone_node_no_parents": ([], ()),
    "one": (["cs_1"], ()),
    "many": ([f"cs_{i}" for i in range(25)], ()),
    "non_ascii": (["nd_ünï", "cs_日本"], ()),
    "quotes_and_backslash": (_HOSTILE, ()),
    "absent_id": (["cs_1", "cs_2"], ("cs_ghost",)),
    "1500_ids": ([f"cs_{i}" for i in range(1500)], ()),
}


@pytest.mark.parametrize("name", list(EQUIV_CASES))
def test_new_equals_old_oracle(name):
    parents, absent = EQUIV_CASES[name]
    conn = _fan(parents, absent)
    chain = core.generalization_chain(conn, "ls_X")
    nodes, heads = chain["nodes"], chain["heads"]

    # the walk must really have reached what the case claims (non-vacuous)
    for pid in parents:
        assert pid in nodes, (name, pid)
    assert "cs_ghost" not in nodes

    old_sum, old_claim = _old_hydrate(conn, nodes, heads)
    for nid, entry in nodes.items():
        assert entry["recall_summary"] == old_sum.get(nid), (name, nid)
    for hid in heads:
        assert chain["nodes"][hid]["claim"] == old_claim.get(hid), (name, hid)
    # real text came back (guards an all-None == all-None pass)
    for pid in parents:
        assert nodes[pid]["recall_summary"] == f"sum:{pid}"
        assert nodes[pid]["claim"] == f"claim:{pid}"
    # non-heads carry no claim (a lone node is its own head)
    assert ("claim" in nodes["ls_X"]) == ("ls_X" in heads)


# ---------------------------------------------------------------------------
# 2. query count
# ---------------------------------------------------------------------------

def _hydration_reads(conn_builder, start, **kw):
    conn = conn_builder()
    seen = []
    conn.set_trace_callback(seen.append)
    chain = core.generalization_chain(conn, start, **kw)
    summ = [s for s in seen if "json_each" in s and "recall_summary" in s]
    clm = [s for s in seen if "json_each" in s and "id, claim" in s]
    return chain, summ, clm


def _linear(n):
    def build():
        conn = _new_conn()
        for i in range(n):
            _add_node(conn, f"cs_{i}")
        for i in range(n - 1):
            _add_edge(conn, f"cs_{i}", f"cs_{i + 1}")
        return conn
    return build


def test_one_json_each_read_per_column_independent_of_chain_size():
    counts = {}
    for n in (2, 50):
        chain, summ, clm = _hydration_reads(_linear(n), f"cs_{n - 1}", max_depth=60)
        assert len(chain["nodes"]) == n  # walk really reached the whole chain
        counts[n] = (len(summ), len(clm))
    assert counts[2] == (1, 1), counts
    assert counts[50] == (1, 1), counts


def test_empty_reached_set_issues_no_hydration_read():
    # unknown start id -> nothing reached, nothing hydrated
    chain, summ, clm = _hydration_reads(_new_conn, "ls_missing")
    assert chain["nodes"] == {}
    assert summ == [] and clm == []


# ---------------------------------------------------------------------------
# 3. template shape (static)
# ---------------------------------------------------------------------------

def _fn_ast():
    return ast.parse(textwrap.dedent(inspect.getsource(core.generalization_chain)))


def test_template_literals_present_and_no_dynamic_sql():
    tree = _fn_ast()
    constants = set()
    for call in ast.walk(tree):
        if isinstance(call, ast.Call) and isinstance(call.func, ast.Attribute) \
                and call.func.attr == "execute":
            sql = call.args[0]
            assert isinstance(sql, ast.Constant) and isinstance(sql.value, str), (
                f"execute() SQL at line {call.lineno} is not a plain string literal "
                f"({type(sql).__name__}): f-strings / concatenation / names are barred"
            )
            constants.add(sql.value)
    assert SQL_SUMMARY in constants
    assert SQL_CLAIM in constants


# ---------------------------------------------------------------------------
# 4. loudness
# ---------------------------------------------------------------------------

def test_no_try_except_in_generalization_chain():
    tree = _fn_ast()
    tries = [n.lineno for n in ast.walk(tree) if isinstance(n, ast.Try)]
    assert tries == [], f"try/except in generalization_chain (lines {tries}) could swallow OperationalError"


def test_missing_json_each_raises_not_empty():
    conn = _fan(["cs_1", "cs_2"])

    class _NoJsonEach:
        def __init__(self, real):
            self._real = real

        def execute(self, sql, *a, **kw):
            if "json_each" in sql:
                raise sqlite3.OperationalError("no such table: json_each")
            return self._real.execute(sql, *a, **kw)

        def __getattr__(self, name):
            return getattr(self._real, name)

    with pytest.raises(sqlite3.OperationalError):
        core.generalization_chain(_NoJsonEach(conn), "ls_X")
