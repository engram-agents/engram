"""#3210 — acyclicity is per FAMILY of relations (grounding / abstraction).

Contract cells (written before the implementation; see the issue's design
comments by Borges and Aleph):

  C1  deduction-shaped: S supported_by G, then G --generalizes--> S. No
      write-time refusal; check_dag_conformance reports no cycle.
  C2  induction-shaped: G supported_by S, then G --generalizes--> S. Same.
  C3  an acknowledgement edge that would close a loop THROUGH a `cites` edge
      (not a cascade relation) is refused at write time, with the
      engram_retype_edge repair route named; if the edge is written anyway,
      conformance reports the cycle under family "grounding".
  C4  a pure generalizes cycle is still refused at write time.
  C5  two disjoint cycles in one family are BOTH reported (`cycles`).
  C6  an ack loop through derives_from alone is still refused (old guard).

Table cells (TABLE-class: they assert the family declaration itself, so a
mutation of the table is expected to redden them):
  - DAG_RELATIONS is exactly the old nine.
  - every relation has dag_family in {grounding, abstraction, None}.
  - old conformance keys present and consistent with the new ones.
  - retype guard is family-aware (refuse a grounding cycle; admit a
    deduction-shaped pair retyped to generalizes).

Import-pattern contract (#948): imports engram_core / engram_dag_conformance
directly for the in-memory cells; the DB-backed cells use fresh_server().
"""

from __future__ import annotations

import atexit
import json
import os
import shutil
import sqlite3
import sys
import tempfile

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

TEST_DIR = tempfile.mkdtemp(prefix="engram_test_dag_family_3210_")
atexit.register(lambda: shutil.rmtree(TEST_DIR, ignore_errors=True))

_OLD_NINE = frozenset({
    "cites", "derives_from", "acknowledged_derives_from", "predicted_by",
    "retracts", "subtask_of", "supersedes", "supported_by", "generalizes",
})


# ---------------------------------------------------------------------------
# in-memory fixtures (same shape as test_dag_conformance.py)
# ---------------------------------------------------------------------------

def _mem():
    import engram_core as core
    import engram_dag_conformance as dagconf
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        "CREATE TABLE nodes (id TEXT PRIMARY KEY, type TEXT NOT NULL, "
        "is_current INTEGER DEFAULT 1, superseded_by TEXT, created_at TEXT)"
    )
    conn.execute(
        "CREATE TABLE edges (id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "source_id TEXT, target_id TEXT, relation TEXT, created_at TEXT)"
    )
    return conn, core, dagconf


def _n(conn, *ids):
    for i in ids:
        conn.execute(
            "INSERT INTO nodes (id, type, is_current, created_at) "
            "VALUES (?, 'observation_factual', 1, 't')", (i,))


def _e(conn, s, t, rel):
    conn.execute(
        "INSERT INTO edges (source_id, target_id, relation, created_at) "
        "VALUES (?, ?, ?, 't')", (s, t, rel))


# ---------------------------------------------------------------------------
# C1 / C2 — deduction- and induction-shaped generalizes edges are legal
# ---------------------------------------------------------------------------

def test_c1_deduction_shaped_generalizes_not_refused_not_flagged():
    conn, core, dagconf = _mem()
    _n(conn, "s", "g")
    _e(conn, "s", "g", "supported_by")       # specific rests on general
    assert core._generalizes_cycle_path(conn, "g", "s") is None
    _e(conn, "g", "s", "generalizes")        # the write the guard admitted
    out = dagconf.check_dag_conformance(conn)
    assert out["cycle_found"] is False, out
    assert out["cycles"] == [], out


def test_c2_induction_shaped_generalizes_not_refused_not_flagged():
    conn, core, dagconf = _mem()
    _n(conn, "s", "g")
    _e(conn, "g", "s", "supported_by")       # general rests on specific
    assert core._generalizes_cycle_path(conn, "g", "s") is None
    _e(conn, "g", "s", "generalizes")
    out = dagconf.check_dag_conformance(conn)
    assert out["cycle_found"] is False, out
    assert out["cycles"] == [], out


# ---------------------------------------------------------------------------
# C4 — a pure generalizes cycle is still refused
# ---------------------------------------------------------------------------

def test_c4_pure_generalizes_cycle_still_refused_and_reported():
    conn, core, dagconf = _mem()
    _n(conn, "a", "b", "c")
    _e(conn, "a", "b", "generalizes")
    _e(conn, "b", "c", "generalizes")
    path = core._generalizes_cycle_path(conn, "c", "a")
    assert path is not None and path[0] == path[-1] == "c", path
    assert set(path) == {"a", "b", "c"}, path
    # and the audit reports one if it lands anyway
    _e(conn, "c", "a", "generalizes")
    out = dagconf.check_dag_conformance(conn)
    assert out["cycle_found"] is True and out["cycle_count"] == 1, out


# ---------------------------------------------------------------------------
# C5 — one cycle per back edge in a family is reported, not just the first
# ---------------------------------------------------------------------------

def test_c5_two_disjoint_cycles_in_one_family_both_reported():
    conn, core, dagconf = _mem()
    _n(conn, "a", "b", "c", "d")
    _e(conn, "a", "b", "derives_from")
    _e(conn, "b", "a", "derives_from")
    _e(conn, "c", "d", "derives_from")
    _e(conn, "d", "c", "derives_from")
    out = dagconf.check_dag_conformance(conn)
    assert out["cycle_count"] == 2, out
    node_sets = sorted(sorted(set(c["path"])) for c in out["cycles"])
    assert node_sets == [["a", "b"], ["c", "d"]], out
    assert all(c["family"] == "grounding" for c in out["cycles"]), out
    # legacy keys: first cycle across families
    assert out["cycle_found"] is True
    assert out["cycle_path"] == out["cycles"][0]["path"]


def test_c5b_cycles_carry_family_label_per_family():
    """TABLE-class: a grounding cycle and an abstraction cycle are each
    reported under their own family."""
    conn, core, dagconf = _mem()
    _n(conn, "a", "b", "x", "y")
    _e(conn, "a", "b", "derives_from")
    _e(conn, "b", "a", "derives_from")
    _e(conn, "x", "y", "generalizes")
    _e(conn, "y", "x", "generalizes")
    out = dagconf.check_dag_conformance(conn)
    assert sorted(c["family"] for c in out["cycles"]) == ["abstraction", "grounding"], out
    assert out["cycle_count"] == 2


def test_all_cycles_variant_is_iterative_on_a_long_chain():
    """No recursion: a 3000-node chain plus a closing edge must not raise."""
    _, core, dagconf = _mem()
    edges = [(f"n{i}", f"n{i+1}") for i in range(3000)] + [("n3000", "n0")]
    cycles = dagconf._find_all_cycles(edges)
    assert len(cycles) == 1 and cycles[0][0] == cycles[0][-1]


def test_old_conformance_keys_present_and_consistent():
    conn, core, dagconf = _mem()
    _n(conn, "a", "b")
    out = dagconf.check_dag_conformance(conn)
    assert out["cycle_found"] is False and out["cycle_path"] == []
    assert out["cycles"] == [] and out["cycle_count"] == 0
    assert out["dag_relation_count"] == len(core.DAG_RELATIONS)


# ---------------------------------------------------------------------------
# the shared helper
# ---------------------------------------------------------------------------

def test_helper_is_keyed_by_family_of_the_edge_written():
    conn, core, dagconf = _mem()
    _n(conn, "a", "b")
    _e(conn, "b", "a", "supported_by")        # grounding edge b -> a
    # writing a -> b in the GROUNDING family closes a cycle ...
    assert core._dag_cycle_path(conn, "derives_from", "a", "b") is not None
    # ... but the ABSTRACTION family never sees the supported_by edge
    assert core._dag_cycle_path(conn, "generalizes", "a", "b") is None
    # a relation outside every family has no walk at all
    assert core._dag_cycle_path(conn, "about", "a", "b") is None


# ---------------------------------------------------------------------------
# TABLE cells
# ---------------------------------------------------------------------------

def test_table_dag_relations_exactly_the_old_nine():
    import engram_core as core
    assert core.DAG_RELATIONS == _OLD_NINE


def test_table_every_relation_has_a_valid_family():
    import engram_core as core
    for rel, cls in core.EDGE_CLASSIFICATIONS.items():
        assert "dag_family" in cls, rel
        assert cls["dag_family"] in {"grounding", "abstraction", None}, rel
    grounding = {r for r, c in core.EDGE_CLASSIFICATIONS.items()
                 if c["dag_family"] == "grounding"}
    assert grounding == _OLD_NINE - {"generalizes"}
    assert core.EDGE_CLASSIFICATIONS["generalizes"]["dag_family"] == "abstraction"
    # grounding contains the cascade set (the §12.4 sufficiency argument)
    assert core.CASCADE_RELATIONS <= grounding


# ---------------------------------------------------------------------------
# DB-backed cells
# ---------------------------------------------------------------------------

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
    os.environ["ENGRAM_NO_DB_GUARDS"] = "1"
    return server


def _raw_edge(core, rel, s, t):
    conn = core._get_db()
    try:
        conn.execute(
            "INSERT INTO edges (source_id, target_id, relation, created_at) "
            "VALUES (?, ?, ?, ?)", (s, t, rel, core._now()))
        conn.commit()
    finally:
        conn.close()


def _axiom(server, claim):
    r = json.loads(server.engram_add_axiom(payload_json=json.dumps(
        {"claim": claim, "basis": "b"})))
    assert "error" not in r, r
    return r["axiom_id"]


def _cornerstone(server, title):
    r = json.loads(server.engram_add_cornerstone(payload_json=json.dumps(
        {"tag": "t", "title": title, "new_frame": f"frame {title}"})))
    assert "error" not in r, r
    return r["cornerstone_id"]


def _add_generalizes(server, s, t):
    return json.loads(server.engram_add_edge(payload_json=json.dumps(
        {"source_id": s, "target_id": t, "relation": "generalizes"})))


def _conformance(core):
    import engram_dag_conformance as dagconf
    conn = core._get_db()
    try:
        return dagconf.check_dag_conformance(conn)
    finally:
        conn.close()


def test_c1_end_to_end_deduction_shape_through_add_edge():
    server = fresh_server()
    core = server.core
    ax = _axiom(server, "A general axiom.")
    cs = _cornerstone(server, "Specific CS")
    _raw_edge(core, "supported_by", cs, ax)           # cs rests on ax
    r = _add_generalizes(server, ax, cs)               # ax generalizes cs
    assert "error" not in r, r
    out = _conformance(core)
    assert out["cycle_found"] is False, out


def test_c2_end_to_end_induction_shape_through_add_edge():
    server = fresh_server()
    core = server.core
    ax = _axiom(server, "A general axiom.")
    cs = _cornerstone(server, "Specific CS")
    _raw_edge(core, "supported_by", ax, cs)           # ax rests on cs
    r = _add_generalizes(server, ax, cs)
    assert "error" not in r, r
    out = _conformance(core)
    assert out["cycle_found"] is False, out


def test_c4_end_to_end_pure_generalizes_cycle_refused_by_add_edge():
    server = fresh_server()
    cs1 = _cornerstone(server, "CS1")
    cs2 = _cornerstone(server, "CS2")
    assert "error" not in _add_generalizes(server, cs1, cs2)
    r = _add_generalizes(server, cs2, cs1)
    assert "error" in r and "cycle" in r["error"], r


# -- ack fixtures (public verbs; mirrors test_cascade_resolve.py's §12.7 case)

def _ack_mods():
    import importlib
    from conftest import fresh_module
    import engram_revision
    if os.path.exists(TEST_DIR):
        shutil.rmtree(TEST_DIR)
    os.environ["ENGRAM_NO_EMBEDDINGS"] = "1"
    rev = fresh_module(engram_revision)
    obs = importlib.import_module("engram_observation")
    derive = importlib.import_module("engram_derive")
    core = sys.modules["engram_core"]
    core._configure_paths(TEST_DIR)
    core._ensure_data_dir()
    return rev, obs, derive, core


_SEQ = [0]


def _ob(obs):
    _SEQ[0] += 1
    r = json.loads(obs._add_observation_impl(
        url=f"https://example.com/s3210-{_SEQ[0]}", title=f"S{_SEQ[0]}",
        quoted_text="Base.", interpretation="Base.", claim="Base observation.",
        quote_type="official_statement"))
    assert "error" not in r, r
    return r["observation_id"]


def _dv(derive, claim, supports):
    r = json.loads(derive._derive_impl(
        claim=claim, supporting_ids=",".join(supports),
        logical_chain=f"Derivation: {claim}",
        reasoning_type="deductive_modus_ponens"))
    assert "error" not in r, r
    return r["derivation_id"]


def _build_cites_ack_scenario():
    """The #3210 shape, public verbs plus ONE raw `cites` edge (a legacy
    citation, the way one sits in a real graph; cites is not addable after
    creation): base ob; p derives ob; x derives p; p2 derives ob and CITES x.
    supersede(p -> p2) is legal under §12.3 (p2 is not downstream of p over
    CASCADE relations -- the cites edge is not one). Acknowledging x then
    writes x --acknowledged_derives_from--> p2, closing x -> p2 -> x, but
    ONLY through the `cites` edge: the cascade-only guard cannot see it."""
    rev, obs, derive, core = _ack_mods()
    ob = _ob(obs)
    p = _dv(derive, "p.", [ob])
    x = _dv(derive, "x from p.", [p])
    p2 = _dv(derive, "p2 from ob.", [ob])
    _raw_edge(core, "cites", p2, x)
    s1 = json.loads(rev._supersede_impl(
        old_node_id=p, new_node_id=p2, supersede_reason="r1"))
    assert "error" not in s1, s1
    return rev, core, dict(x=x, p2=p2)


def _build_ack_scenario(closing_relation):
    """The test_cascade_resolve §12.7 fixture (derives_from only):
    dv1, dv2 -> ob; dv3 -> dv2; dv4 -> dv3; dv5 -> dv1;
    supersede(dv1 -> dv4), supersede(dv2 -> dv5). Returns (rev, core, ids)."""
    assert closing_relation == "derives_from"
    rev, obs, derive, core = _ack_mods()
    ob = _ob(obs)
    dv1 = _dv(derive, "dv1.", [ob])
    dv2 = _dv(derive, "dv2.", [ob])
    dv3 = _dv(derive, "dv3 from dv2.", [dv2])
    dv4 = _dv(derive, "dv4 from dv3.", [dv3])
    dv5 = _dv(derive, "dv5 from dv1.", [dv1])
    s1 = json.loads(rev._supersede_impl(
        old_node_id=dv1, new_node_id=dv4, supersede_reason="r1"))
    assert "error" not in s1, s1
    s2 = json.loads(rev._supersede_impl(
        old_node_id=dv2, new_node_id=dv5, supersede_reason="r2"))
    assert "error" not in s2, s2
    return rev, core, dict(dv3=dv3, dv4=dv4, dv5=dv5)


def test_c3_ack_loop_through_cites_refused_with_retype_hint():
    rev, core, ids = _build_cites_ack_scenario()
    # Precondition: the cascade-only guard (§12.7) would ADMIT this ack -- no
    # cascade-relation path from the replacement back to the node. Only the
    # `cites` edge closes the loop, so the refusal below is the new check's.
    conn = core._get_db()
    try:
        assert rev._cascade_reaches(conn, ids["x"], ids["p2"]) is False
    finally:
        conn.close()
    r = json.loads(rev._cascade_resolve_impl(
        node_id=ids["x"], resolution_kind="acknowledged"))
    assert "error" in r, r
    assert r["guard"] == "acknowledgement_would_close_grounding_cycle", r
    blob = json.dumps(r)
    assert "engram_retype_edge" in blob and "about" in blob and "cites" in blob, r
    # nothing was written
    conn = core._get_db()
    try:
        n = conn.execute(
            "SELECT COUNT(*) AS c FROM edges "
            "WHERE relation = 'acknowledged_derives_from'").fetchone()["c"]
    finally:
        conn.close()
    assert n == 0
    # And had the edge landed (legacy state), conformance names the cycle.
    _raw_edge(core, "acknowledged_derives_from", ids["x"], ids["p2"])
    out = _conformance(core)
    assert out["cycle_found"] is True, out
    assert [c["family"] for c in out["cycles"]] == ["grounding"], out


def test_c6_ack_loop_through_derives_from_alone_still_refused():
    rev, core, ids = _build_ack_scenario("derives_from")
    a1 = json.loads(rev._cascade_resolve_impl(
        node_id=ids["dv3"], resolution_kind="acknowledged"))
    assert "error" not in a1, a1
    a2 = json.loads(rev._cascade_resolve_impl(
        node_id=ids["dv5"], resolution_kind="acknowledged"))
    assert a2.get("guard") == "acknowledgement_would_close_cascade_cycle", a2


# -- retype guard is family-aware

def _retype(server, s, t, old, new):
    return json.loads(server.engram_retype_edge(payload_json=json.dumps({
        "source_id": s, "target_id": t, "relation": old,
        "new_relation": new, "reason": "test"})))


def _obs(server, claim, n):
    r = json.loads(server.engram_add_observation(payload_json=json.dumps({
        "url": f"https://example.com/r3210-{n}", "title": "T",
        "quoted_text": "X says Y.", "interpretation": "X claims Y.",
        "claim": claim, "quote_type": "hard_data"})))
    assert "error" not in r, r
    return r["observation_id"]


def test_retype_to_grounding_relation_closing_grounding_cycle_refused(monkeypatch):
    server = fresh_server()
    core = server.core
    monkeypatch.setitem(core.RETYPE_ALLOWED_MOVES,
                        ("about", "acknowledged_derives_from"), None)
    a = _obs(server, "A claim.", 1)
    b = _obs(server, "B claim.", 2)
    _raw_edge(core, "about", a, b)
    _raw_edge(core, "derives_from", b, a)          # grounding path b -> a
    r = _retype(server, a, b, "about", "acknowledged_derives_from")
    assert "error" in r and "cycle" in r["error"], r


def test_retype_deduction_shaped_pair_to_generalizes_admitted(monkeypatch):
    server = fresh_server()
    core = server.core
    monkeypatch.setitem(core.RETYPE_ALLOWED_MOVES, ("about", "generalizes"), None)
    ax = _axiom(server, "A general axiom.")
    cs = _cornerstone(server, "Specific CS")
    _raw_edge(core, "supported_by", cs, ax)        # cs rests on ax
    _raw_edge(core, "about", ax, cs)
    r = _retype(server, ax, cs, "about", "generalizes")
    assert r.get("status") == "retyped", r


# ---------------------------------------------------------------------------
# rider: a clean result names the population it checked
# ---------------------------------------------------------------------------

def test_clean_result_carries_population_counts_that_move_with_the_graph():
    conn, core, dagconf = _mem()
    _n(conn, "a", "b", "c", "old", "new")
    _e(conn, "a", "b", "derives_from")
    before = dagconf.check_dag_conformance(conn)
    g = before["families"]["grounding"]
    assert before["cycle_found"] is False
    assert g["relation_count"] == 8 and g["cycle_count"] == 0
    assert (g["nodes"], g["edges"]) == (2, 1), g
    assert before["families"]["abstraction"]["relation_count"] == 1
    assert "grounding: 8 relations, 2 nodes, 1 edges, 0 cycles" == g["summary"], g
    _e(conn, "b", "c", "supported_by")
    after = dagconf.check_dag_conformance(conn)["families"]["grounding"]
    assert (after["nodes"], after["edges"]) == (3, 2), after
    # an edge on the other family does not change the grounding population
    _e(conn, "a", "c", "generalizes")
    both = dagconf.check_dag_conformance(conn)["families"]
    assert (both["grounding"]["edges"], both["abstraction"]["edges"]) == (2, 1)


def test_population_counts_report_dropped_edges():
    conn, core, dagconf = _mem()
    _n(conn, "n")
    conn.execute(
        "INSERT INTO nodes (id, type, is_current, superseded_by, created_at) "
        "VALUES ('o', 'observation_factual', 0, 'n', 't')")
    conn.execute(
        "INSERT INTO nodes (id, type, is_current, superseded_by, created_at) "
        "VALUES ('dead', 'observation_factual', 0, NULL, 't')")
    _e(conn, "n", "o", "derives_from")       # resolves to n -> n: manufactured
    _e(conn, "n", "dead", "derives_from")    # dead-ended chain
    g = dagconf.check_dag_conformance(conn)["families"]["grounding"]
    assert g["raw_edges"] == 2 and g["edges"] == 0, g
    assert g["dropped_manufactured_self_loops"] == 1, g
    assert g["dropped_dead_ended"] == 1, g


# ---------------------------------------------------------------------------
# Round-2 cells (reviewer findings on the first tip)
# ---------------------------------------------------------------------------

def test_grounding_refusal_names_both_repair_routes():
    """The retype route exists only for a retypable edge on the closing path
    (cites / supported_by / derives_from that is not the node's last cascade
    premise); otherwise the path needs engram_surgical. The refusal must name
    both, in the error text and the remedy."""
    rev, core, ids = _build_cites_ack_scenario()
    r = json.loads(rev._cascade_resolve_impl(
        node_id=ids["x"], resolution_kind="acknowledged"))
    assert r["guard"] == "acknowledgement_would_close_grounding_cycle", r
    for field in ("error", "remedy"):
        text = r[field]
        assert "engram_retype_edge" in text, (field, text)
        assert "engram_surgical" in text, (field, text)
        assert "last cascade premise" in text, (field, text)


def test_cycles_are_one_per_back_edge_not_a_census():
    """On a complete 3-node digraph there are 5 elementary cycles; the audit
    reports fewer (one per DFS back edge), and says so in its docs."""
    conn, core, dagconf = _mem()
    _n(conn, "a", "b", "c")
    for s in "abc":
        for t in "abc":
            if s != t:
                _e(conn, s, t, "derives_from")
    out = dagconf.check_dag_conformance(conn)
    assert out["cycle_found"] is True
    assert 1 <= out["cycle_count"] < 5, out
    assert "back edge" in dagconf.check_dag_conformance.__doc__
    assert "back edge" in dagconf._find_all_cycles.__doc__
    assert "back edge" in dagconf.__doc__


def test_cycles_list_is_capped_per_family_with_truncation_flag():
    conn, core, dagconf = _mem()
    names = [f"n{i}" for i in range(50)]
    _n(conn, *names)
    for i in range(25):
        _e(conn, names[2 * i], names[2 * i + 1], "derives_from")
        _e(conn, names[2 * i + 1], names[2 * i], "derives_from")
    out = dagconf.check_dag_conformance(conn)
    assert out["cycle_count"] == 25, out["cycle_count"]
    assert len(out["cycles"]) == 20
    assert out["cycles_truncated"] is True
    g = out["families"]["grounding"]
    assert g["cycle_count"] == 25 and g["cycles_truncated"] is True
    assert out["families"]["abstraction"]["cycles_truncated"] is False
    # legacy keys unchanged
    assert out["cycle_found"] is True
    assert out["cycle_path"] == out["cycles"][0]["path"]


def test_no_truncation_flag_when_under_the_limit():
    conn, core, dagconf = _mem()
    _n(conn, "a", "b")
    _e(conn, "a", "b", "derives_from")
    _e(conn, "b", "a", "derives_from")
    out = dagconf.check_dag_conformance(conn)
    assert out["cycle_count"] == 1 and len(out["cycles"]) == 1
    assert out["cycles_truncated"] is False
    assert out["families"]["grounding"]["cycles_truncated"] is False
