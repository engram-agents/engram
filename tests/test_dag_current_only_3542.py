"""#3542 (+#2952) — DAG acyclicity ranges over CURRENT nodes and CURRENT edges only.

Lei's ruling (2026-10-21, quoted in #3542 by Kepler): non-current nodes and their
edges are provenance, not live structure. An edge counts for acyclicity iff BOTH
of its RAW endpoints are live (``core._LIVE_NODE_PREDICATE``; retracted is already
non-current). No resolution through supersede chains when judging cycles.

Contract cells (written before the implementation):

  C1  a cycle that closes only through a superseded source (Q derives_from old D;
      D superseded by D'; D' derives_from Q): gate cycle_count 0, informational
      ``frozen_closed_components`` 1 with an example path.
  C2  a cycle among current nodes only: gate count 1, informational 0.
  C3  on C1's graph every exempt traversal (display / carry semantics) terminates.
  C4  write time: an edge that closes a cycle only via a superseded node is
      ALLOWED; one that closes a cycle among current nodes is still REFUSED.
  C5  ``_cascade_reaches``: a path only through a non-current intermediate is
      False; a current-only path is True.
  C6  #2943: a genuine raw self-edge on a current node is still reported.

Plus the shared-predicate cell: the edge predicate is built from the one
node-liveness predicate and is never a second definition.
"""

import importlib
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import atexit
import time
from pathlib import Path

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_TESTS_DIR = str(Path(__file__).resolve().parent)
if _TESTS_DIR not in sys.path:
    sys.path.insert(0, _TESTS_DIR)

TEST_DIR = tempfile.mkdtemp(prefix="engram_test_dag_current_only_3542_")
atexit.register(lambda: shutil.rmtree(TEST_DIR, ignore_errors=True))

_SEQ = [0]


def _fresh():
    if os.path.exists(TEST_DIR):
        shutil.rmtree(TEST_DIR)
    for key in list(sys.modules):
        if key == "server" or key.startswith("engram_"):
            del sys.modules[key]
    import server
    server._configure_paths(TEST_DIR)
    server._ensure_data_dir()
    os.environ["ENGRAM_NO_EMBEDDINGS"] = "1"
    os.environ["ENGRAM_NO_DB_GUARDS"] = "1"
    return server


def _ob(server, claim="A source claim."):
    _SEQ[0] += 1
    r = json.loads(server._add_observation_impl(
        url=f"https://example.com/s{_SEQ[0]}", title=f"S{_SEQ[0]}",
        quoted_text=claim, interpretation=f"I: {claim}", claim=f"{claim} {_SEQ[0]}",
        quote_type="hard_data"))
    assert "observation_id" in r, r
    return r["observation_id"]


def _cs(server, title):
    r = json.loads(server.engram_add_cornerstone(payload_json=json.dumps(
        {"tag": "t", "title": title, "new_frame": f"frame {title}"})))
    assert "error" not in r, r
    return r["cornerstone_id"]


def _sql(core, sql, args=()):
    c = sqlite3.connect(str(core.DB_PATH))
    try:
        c.execute(sql, args)
        c.commit()
    finally:
        c.close()


def _edge(core, s, t, rel):
    _sql(core, "INSERT INTO edges (source_id, target_id, relation, created_at) "
               "VALUES (?, ?, ?, ?)", (s, t, rel, "2026-10-21T00:00:00"))


def _supersede_raw(core, old, new):
    _sql(core, "UPDATE nodes SET is_current = 0, superseded_by = ? WHERE id = ?",
         (new, old))


def _conn(core):
    c = sqlite3.connect(str(core.DB_PATH))
    c.row_factory = sqlite3.Row
    return c


def _gate(core):
    import engram_dag_conformance as dagc
    c = _conn(core)
    try:
        return dagc.check_dag_conformance(c)
    finally:
        c.close()


def _frozen_graph(server):
    """Q derives_from D(old); D superseded by D2; D2 derives_from Q.
    Raw edges Q->D, D2->Q. Resolved: Q->D2, D2->Q = a cycle that closes only
    through the superseded D."""
    core = server.core
    q, d, d2 = _ob(server, "Q"), _ob(server, "D"), _ob(server, "D2")
    _supersede_raw(core, d, d2)
    _edge(core, q, d, "derives_from")
    _edge(core, d2, q, "derives_from")
    return q, d, d2


# -- C1 ----------------------------------------------------------------------

def test_c1_frozen_closed_cycle_not_on_gate_but_reported_informationally():
    server = _fresh()
    q, d, d2 = _frozen_graph(server)
    out = _gate(server.core)
    assert out["cycle_count"] == 0, out
    assert out["cycle_found"] is False, out
    assert out["cycles"] == [], out
    assert out["frozen_closed_components"] == 1, out
    path = out["frozen_closed_example_path"]
    assert path and path[0] == path[-1], out
    assert {q, d2} <= set(path), (path, q, d2)
    assert d not in path, "the superseded node must not appear in the resolved path"
    assert out["families"]["grounding"]["frozen_closed_components"] == 1, out
    assert out["families"]["abstraction"]["frozen_closed_components"] == 0, out


def test_c1b_retracted_endpoint_edge_is_non_current():
    server = _fresh()
    core = server.core
    a, b = _ob(server, "A"), _ob(server, "B")
    _edge(core, a, b, "derives_from")
    _edge(core, b, a, "derives_from")
    assert _gate(core)["cycle_count"] == 1
    _sql(core, "UPDATE nodes SET status = 'retracted', is_current = 0 WHERE id = ?", (b,))
    assert _gate(core)["cycle_count"] == 0


# -- C2 ----------------------------------------------------------------------

def test_c2_current_only_cycle_still_on_gate_not_informational():
    server = _fresh()
    core = server.core
    a, b, c = _ob(server, "A"), _ob(server, "B"), _ob(server, "C")
    _edge(core, a, b, "derives_from")
    _edge(core, b, c, "derives_from")
    _edge(core, c, a, "derives_from")
    out = _gate(core)
    assert out["cycle_count"] == 1, out
    assert out["cycle_found"] is True
    assert set(out["cycle_path"]) == {a, b, c}
    assert out["frozen_closed_components"] == 0, out
    assert out["frozen_closed_example_path"] == [], out


def test_c2b_current_cycle_and_frozen_cycle_are_counted_separately():
    server = _fresh()
    core = server.core
    a, b = _ob(server, "A"), _ob(server, "B")
    _edge(core, a, b, "derives_from")
    _edge(core, b, a, "derives_from")
    _frozen_graph(server)
    out = _gate(core)
    assert out["cycle_count"] == 1, out
    assert out["frozen_closed_components"] == 1, out


# -- C3 ----------------------------------------------------------------------

def test_c3_exempt_traversals_terminate_on_a_frozen_closed_graph():
    server = _fresh()
    core = server.core
    q, d, d2 = _frozen_graph(server)
    rev = sys.modules["engram_revision"]
    conn = _conn(core)
    try:
        t0 = time.time()
        for start in (q, d, d2):
            up = core._walk_cascade_upstream(
                conn, start, collect_if=lambda r: True, descend_if=lambda r: True)
            assert isinstance(up, set)
            leaves = core._trace_observation_leaves(conn, start)
            assert isinstance(leaves, set)
            seen = []
            rev._walk_cascade_downstream(
                conn, start, visited=set(), on_live_node=lambda n: seen.append(n["id"]))
            assert len(seen) == len(set(seen)), "each node visited at most once"
            chain = core.generalization_chain(conn, start)
            assert isinstance(chain, dict)
            core._resolve_to_current(conn, start)
        assert time.time() - t0 < 30
    finally:
        conn.close()


def test_c3b_generalization_chain_terminates_on_a_frozen_closed_generalizes_cycle():
    server = _fresh()
    core = server.core
    c1, c2, c3 = _cs(server, "CS1"), _cs(server, "CS2"), _cs(server, "CS3")
    _supersede_raw(core, c2, c3)
    _edge(core, c1, c2, "generalizes")   # resolves to c1 -> c3
    _edge(core, c3, c1, "generalizes")
    conn = _conn(core)
    try:
        chain = core.generalization_chain(conn, c1)
        assert isinstance(chain, dict)
    finally:
        conn.close()
    out = _gate(core)
    assert out["cycle_count"] == 0, out
    assert out["families"]["abstraction"]["frozen_closed_components"] == 1, out


# -- C4 ----------------------------------------------------------------------

def test_c4_write_time_edge_closing_cycle_only_via_superseded_node_is_allowed():
    server = _fresh()
    core = server.core
    q, d, d2 = _ob(server, "Q"), _ob(server, "D"), _ob(server, "D2")
    _supersede_raw(core, d, d2)
    _edge(core, q, d, "derives_from")          # Q -> D (old)
    conn = _conn(core)
    try:
        # D2 --derives_from--> Q would close Q->D->(D2)->Q only through D.
        assert core._dag_cycle_path(conn, "derives_from", d2, q) is None
        # an edge whose own endpoint is non-current is provenance
        assert core._dag_cycle_path(conn, "derives_from", q, d) is None
    finally:
        conn.close()


def test_c4b_write_time_cycle_among_current_nodes_still_refused():
    server = _fresh()
    core = server.core
    a, b, c = _ob(server, "A"), _ob(server, "B"), _ob(server, "C")
    _edge(core, a, b, "derives_from")
    _edge(core, b, c, "derives_from")
    conn = _conn(core)
    try:
        path = core._dag_cycle_path(conn, "derives_from", c, a)
        assert path is not None and path[0] == path[-1], path
        assert set(path) == {a, b, c}, path
        # raw self-edge on a current node is still refused
        assert core._dag_cycle_path(conn, "derives_from", a, a) == [a, a]
    finally:
        conn.close()


def test_c4d_self_edge_on_a_non_current_node_is_provenance_not_refused():
    server = _fresh()
    core = server.core
    a, a2 = _ob(server, "A"), _ob(server, "A2")
    _supersede_raw(core, a, a2)
    conn = _conn(core)
    try:
        assert core._dag_cycle_path(conn, "derives_from", a, a) is None
        assert core._dag_cycle_path(conn, "derives_from", a2, a2) == [a2, a2]
    finally:
        conn.close()


def test_c4c_generalizes_end_to_end_allowed_via_superseded_refused_among_current():
    server = _fresh()
    core = server.core
    c1, c2, c3 = _cs(server, "CS1"), _cs(server, "CS2"), _cs(server, "CS3")
    r = json.loads(server.engram_add_edge(payload_json=json.dumps(
        {"source_id": c1, "target_id": c2, "relation": "generalizes"})))
    assert "error" not in r, r
    _supersede_raw(core, c2, c3)
    # c3 generalizes c1 would close c1 -> c2(old) -> ... only via the superseded c2
    r = json.loads(server.engram_add_edge(payload_json=json.dumps(
        {"source_id": c3, "target_id": c1, "relation": "generalizes"})))
    assert "error" not in r, r
    # now c1 -> ... c3 -> c1 among current nodes: c1 generalizes c3 closes a live cycle
    r = json.loads(server.engram_add_edge(payload_json=json.dumps(
        {"source_id": c1, "target_id": c3, "relation": "generalizes"})))
    assert "error" in r and "cycle" in r["error"], r


# -- C5 ----------------------------------------------------------------------

def test_c5_cascade_reaches_ignores_paths_through_non_current_intermediates():
    server = _fresh()
    core = server.core
    rev = sys.modules["engram_revision"]
    top, mid, low = _ob(server, "top"), _ob(server, "mid"), _ob(server, "low")
    _edge(core, low, mid, "derives_from")      # low derives from mid
    _edge(core, mid, top, "derives_from")      # mid derives from top
    conn = _conn(core)
    try:
        assert rev._cascade_reaches(conn, top, low) is True
    finally:
        conn.close()
    _supersede_raw(core, mid, top)
    conn = _conn(core)
    try:
        assert rev._cascade_reaches(conn, top, low) is False
    finally:
        conn.close()


def test_c5b_cascade_reaches_current_only_path_is_true():
    server = _fresh()
    core = server.core
    rev = sys.modules["engram_revision"]
    a, b, c, dead = (_ob(server, n) for n in ("a", "b", "c", "dead"))
    _edge(core, c, b, "derives_from")
    _edge(core, b, a, "derives_from")
    _edge(core, c, dead, "derives_from")
    _supersede_raw(core, dead, a)
    conn = _conn(core)
    try:
        assert rev._cascade_reaches(conn, a, c) is True
        assert rev._cascade_reaches(conn, c, a) is False
    finally:
        conn.close()


# -- C6 ----------------------------------------------------------------------

def test_c6_genuine_raw_self_edge_on_current_node_still_reported():
    server = _fresh()
    core = server.core
    a = _ob(server, "A")
    _edge(core, a, a, "derives_from")
    out = _gate(core)
    assert out["cycle_count"] == 1, out
    assert out["cycle_path"] == [a, a], out
    assert out["frozen_closed_components"] == 0, out


def test_c6b_raw_self_edge_on_non_current_node_is_provenance():
    server = _fresh()
    core = server.core
    a, a2 = _ob(server, "A"), _ob(server, "A2")
    _supersede_raw(core, a, a2)
    _edge(core, a, a, "derives_from")
    assert _gate(core)["cycle_count"] == 0


# -- shared predicate --------------------------------------------------------

def test_edge_predicate_is_built_from_the_one_node_liveness_predicate():
    server = _fresh()
    core = server.core
    tmpl = core._CURRENT_EDGE_PREDICATE
    s = core._LIVE_NODE_PREDICATE.format(a="s")
    t = core._LIVE_NODE_PREDICATE.format(a="t")
    rendered = tmpl.format(s="s", t="t")
    assert s in rendered and t in rendered, rendered
    a, b = _ob(server, "A"), _ob(server, "B")
    conn = _conn(core)
    try:
        assert core._edge_is_current(conn, a, b) is True
        _supersede_raw(core, b, a)
        conn.close()
        conn = _conn(core)
        assert core._edge_is_current(conn, a, b) is False
        assert core._edge_is_current(conn, b, a) is False
        assert core._edge_is_current(conn, a, None) is False
    finally:
        conn.close()


# -- C8: generalization_chain is a cycle JUDGE (cycle_detected) --------------
# Reviewer round 1 on #4010 (blocker): the chain resolved endpoints through
# supersede chains, so it reported cycle_detected on a graph the gate and the
# write guard call clean, and the surface hook then dropped the principle.

def _load_surface_hook():
    import importlib.util
    hook_path = (Path(__file__).resolve().parent.parent / "src" / "engram" / "hooks"
                 / "claude" / "engram-surface-hook.py")
    spec = importlib.util.spec_from_file_location("engram_surface_hook_3542_test", str(hook_path))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _render_with_real_chain(core, ids):
    import unittest.mock
    hook = _load_surface_hook()
    with unittest.mock.patch.object(
            hook, "_import_generalization_chain", return_value=core.generalization_chain):
        return hook._render_triggered_principles_section(ids, db_path=core.DB_PATH)


def test_c8_chain_reports_no_cycle_when_it_closes_only_through_a_superseded_node():
    server = _fresh()
    core = server.core
    a, b, b2 = _cs(server, "A"), _cs(server, "B"), _cs(server, "B2")
    _edge(core, b, a, "generalizes")     # B generalizes A
    _edge(core, a, b2, "generalizes")    # A generalizes B2
    _supersede_raw(core, b, b2)          # resolved: B2 -> A -> B2
    assert _gate(core)["cycle_count"] == 0
    conn = _conn(core)
    try:
        chain = core.generalization_chain(conn, a)
    finally:
        conn.close()
    assert chain["cycle_detected"] is False, chain
    assert chain["heads"] == [a], chain
    assert set(chain["nodes"]) == {a}, chain


def test_c8_surface_hook_keeps_a_principle_whose_cycle_closes_only_through_a_superseded_node():
    server = _fresh()
    core = server.core
    a, b, b2, c = (_cs(server, n) for n in ("A", "B", "B2", "C"))
    _edge(core, b, a, "generalizes")
    _edge(core, a, b2, "generalizes")
    _edge(core, c, a, "generalizes")     # C is a live head above A
    _supersede_raw(core, b, b2)
    block = _render_with_real_chain(core, [a])
    assert "malformed" not in block.lower(), block
    assert f"principle: {c}" in block, block


def test_c8_chain_still_reports_a_cycle_among_current_nodes():
    server = _fresh()
    core = server.core
    x, y = _cs(server, "X"), _cs(server, "Y")
    _edge(core, x, y, "generalizes")
    _edge(core, y, x, "generalizes")
    conn = _conn(core)
    try:
        chain = core.generalization_chain(conn, x)
    finally:
        conn.close()
    assert chain["cycle_detected"] is True, chain
    block = _render_with_real_chain(core, [x])
    assert "malformed" in block.lower(), block


# -- dag_edge_set refuses source_id AND target_id together, before any SQL -----

class _ExecuteCounter:
    """A connection stand-in that records every execute call."""
    def __init__(self):
        self.calls = []

    def execute(self, *args, **kwargs):
        self.calls.append(args)
        raise AssertionError("execute must not be reached")


def test_dag_edge_set_refuses_source_and_target_together_and_runs_no_sql():
    server = _fresh()
    core = server.core
    conn = _ExecuteCounter()
    for live_only in (True, False):
        with pytest.raises(ValueError):
            core.dag_edge_set(conn, ("derives_from",), live_only=live_only,
                              source_id="a", target_id="b")
    assert conn.calls == []


# -- dag_edge_set's DEFAULT is live_only=True (protects a caller that omits it) --

def test_dag_edge_set_default_is_live_only_by_signature_and_by_behaviour():
    import inspect
    server = _fresh()
    core = server.core
    assert inspect.signature(core.dag_edge_set).parameters["live_only"].default is True
    a, b, c, dead = (_ob(server, n) for n in ("a", "b", "c", "dead"))
    _supersede_raw(core, dead, a)
    _edge(core, a, b, "derives_from")      # both endpoints live
    _edge(core, c, dead, "derives_from")   # target non-current
    conn = _conn(core)
    try:
        got = core.dag_edge_set(conn, ("derives_from",))     # live_only omitted
        assert got == [(a, b, "derives_from")], got
        both = core.dag_edge_set(conn, ("derives_from",), live_only=False)
        assert {(s, t) for s, t, _r in both} == {(a, b), (c, dead)}, both
    finally:
        conn.close()
