"""Tests for the `generalizes` edge substrate (#2753 epic, WS1).

Covers the full write/read surface added for WS1:
  - EDGE_CLASSIFICATIONS / EDGE_TIERS / EDGE_TIER_DIVERGENCE_ACK wiring
    (dag_relation_count increments by exactly 1 — see test_edge_tier_audit.py
    for the hard-failing tier-ack test).
  - engram_add_edge / engram_remove_edge whitelist + endpoint gate + write-time
    cycle guard.
  - generalization_chain(): superseded-rung resolution, multiple parents,
    depth cap, concrete-node (exemplifies/instantiates) starting point.
  - generalized_by on engram_add_lesson / engram_add_cornerstone (mint-time
    edge writes, strict validation).
  - engram_outgrow_cornerstone carrying the predecessor's generalizes edges
    (both directions) to the successor.

Mutation-verification discipline (per WS1 spec): every control below has a
paired test that deliberately neuters the guard/mechanism under test and
confirms the ORIGINAL test's assertion would then FAIL — proving the
original test actually depends on the mechanism, not on something else.
Where the mechanism isn't behind a monkeypatch-able seam (the multi-parent
case — the parent lookup is an inline SQL query inside generalization_chain,
not a separately named function), the mutation is a deliberate INPUT
reduction (drop one of the two parent edges) that demonstrates the same
thing: the assertion is genuinely sensitive to the graph shape it claims to
be testing, not hardcoded.

Import-pattern contract (#948 wave C): imports server (via fresh_server(),
following the established test-fixture pattern in test_add_lesson_payload.py
/ test_add_cornerstone_payload.py / test_engram_add_edge.py) rather than
importing engram_core directly, except where a pure-function test needs the
lightweight in-memory sqlite3 fixture (test_dag_conformance.py's pattern).
"""

import atexit
import json
import os
import shutil
import sqlite3
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


TEST_DIR = tempfile.mkdtemp(prefix="engram_test_generalizes_")
atexit.register(lambda: shutil.rmtree(TEST_DIR, ignore_errors=True))


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


def _seed_incident(server) -> str:
    """Create a claim-bearing observation node usable as an incident."""
    payload = {
        "quoted_text": "I shipped without reading the error.",
        "interpretation": "Missed contextual cue from stderr.",
        "claim": "Skipped reading error output before hypothesizing root cause.",
        "quote_type": "personal_communication",
        "url": "https://example.com/generalizes-incident",
        "title": "Test incident",
    }
    r = json.loads(server.engram_add_observation(payload_json=json.dumps(payload)))
    assert "error" not in r, r
    return r["observation_id"]


def _seed_axiom(server, claim="Test axiom claim.") -> str:
    r = json.loads(server.engram_add_axiom(payload_json=json.dumps({
        "claim": claim, "basis": "test basis",
    })))
    assert "error" not in r, r
    return r["axiom_id"]


def _seed_goal(server, claim="Test goal claim.") -> str:
    r = json.loads(server.engram_add_goal(payload_json=json.dumps({
        "claim": claim, "motivation": "test motivation",
    })))
    assert "error" not in r, r
    return r["goal_id"]


def _seed_cornerstone(server, title="CS", generalized_by="") -> str:
    payload = {"tag": "test-axis", "title": title, "new_frame": f"frame-{title}"}
    if generalized_by:
        payload["generalized_by"] = generalized_by
    r = json.loads(server.engram_add_cornerstone(payload_json=json.dumps(payload)))
    assert "error" not in r, r
    return r["cornerstone_id"]


def _seed_lesson(server, incident_id=None, claim="Test lesson claim.", generalized_by="") -> str:
    if incident_id is None:
        incident_id = _seed_incident(server)
    payload = {
        "claim": claim,
        "incident_ids": incident_id,
        "scaffolding_nudge": "nudge",
        "logical_chain": "chain",
    }
    if generalized_by:
        payload["generalized_by"] = generalized_by
    r = json.loads(server.engram_add_lesson(payload_json=json.dumps(payload)))
    assert "error" not in r, r
    return r["lesson_id"]


# ---------------------------------------------------------------------------
# 1. EDGE_CLASSIFICATIONS / DAG_RELATIONS wiring
# ---------------------------------------------------------------------------

def test_generalizes_in_valid_relations_and_classifications():
    server = fresh_server()
    core = server.core
    assert "generalizes" in core.VALID_RELATIONS
    assert "generalizes" in core.EDGE_CLASSIFICATIONS
    c = core.EDGE_CLASSIFICATIONS["generalizes"]
    assert c["cascade"] is False
    assert c["temporal_causal"] is False
    assert c["dag_family"] == "abstraction"
    assert c["removable"] is True
    assert c["addable_after_creation"] is True
    assert c["provenance"] is False
    assert core.EDGE_TIERS["generalizes"] == "classification"
    assert "generalizes" in core.EDGE_TIER_DIVERGENCE_ACK


def test_generalizes_addable_and_removable_derived_sets():
    """#2753 spec item 7: engram_add_edge/engram_remove_edge whitelists.
    DERIVED from EDGE_CLASSIFICATIONS, not hand-enumerated -- verify
    empirically, same discipline as the dag_relation_count check below."""
    server = fresh_server()
    core = server.core
    assert "generalizes" in core._ADDABLE_AFTER_CREATION_RELATIONS
    assert "generalizes" in core._REMOVABLE_EDGE_RELATIONS


def test_dag_relation_count_increments_by_exactly_one():
    """#2753 spec: DAG_RELATIONS is derived from EDGE_CLASSIFICATIONS at call
    time (never hand-enumerated) -- verify empirically via
    engram_dag_conformance's dag_relation_count, rather than reasoning that
    it must work.

    Baseline measured on dev @ 78db1e07 (pre-WS1): DAG_RELATIONS = {cites,
    derives_from, acknowledged_derives_from, retracts, subtask_of,
    supersedes, supported_by, predicted_by} -- exactly 8 relations. Post-WS1
    it must be exactly 9 (the same 8, plus `generalizes`).
    """
    server = fresh_server()
    core = server.core
    assert len(core.DAG_RELATIONS) == 9, sorted(core.DAG_RELATIONS)
    assert "generalizes" in core.DAG_RELATIONS

    import engram_dag_conformance as dagconf
    conn = core._get_db()
    try:
        report = dagconf.check_dag_conformance(conn)
    finally:
        conn.close()
    assert report["dag_relation_count"] == 9, report
    assert report["cycle_found"] is False, report


# ---------------------------------------------------------------------------
# 2. engram_add_edge / engram_remove_edge: endpoint gate + cycle guard
# ---------------------------------------------------------------------------

def test_generalizes_add_edge_happy_path():
    server = fresh_server()
    cs1 = _seed_cornerstone(server, "CS1")
    cs2 = _seed_cornerstone(server, "CS2")
    r = json.loads(server.engram_add_edge(payload_json=json.dumps({
        "source_id": cs1, "target_id": cs2, "relation": "generalizes",
    })))
    assert "error" not in r, r
    assert r["status"] == "created", r


def test_generalizes_add_edge_idempotent():
    server = fresh_server()
    cs1 = _seed_cornerstone(server, "CS1")
    cs2 = _seed_cornerstone(server, "CS2")
    payload = json.dumps({"source_id": cs1, "target_id": cs2, "relation": "generalizes"})
    r1 = json.loads(server.engram_add_edge(payload_json=payload))
    assert r1["status"] == "created", r1
    r2 = json.loads(server.engram_add_edge(payload_json=payload))
    assert r2["status"] == "no_op_already_exists", r2


def test_generalizes_remove_edge_happy_path():
    server = fresh_server()
    cs1 = _seed_cornerstone(server, "CS1")
    cs2 = _seed_cornerstone(server, "CS2")
    server.engram_add_edge(payload_json=json.dumps({
        "source_id": cs1, "target_id": cs2, "relation": "generalizes",
    }))
    r = json.loads(server.engram_remove_edge(payload_json=json.dumps({
        "source_id": cs1, "target_id": cs2, "relation": "generalizes",
    })))
    assert r["status"] == "removed", r


# ── Endpoint refusal (mutation-verified) ────────────────────────────────

def test_generalizes_endpoint_refused_concrete_to_cornerstone():
    """obs -> cornerstone via `generalizes` is REFUSED -- that pairing is
    `instantiates` territory (concrete nodes never sit on either side of
    `generalizes`)."""
    server = fresh_server()
    inc_id = _seed_incident(server)
    cs1 = _seed_cornerstone(server, "CS1")
    r = json.loads(server.engram_add_edge(payload_json=json.dumps({
        "source_id": inc_id, "target_id": cs1, "relation": "generalizes",
    })))
    assert "error" in r, r
    assert "does not accept" in r["error"], r


def test_generalizes_endpoint_refused_lesson_to_cornerstone():
    """lesson -> cornerstone is not an allowed pair (only
    cornerstone -> lesson, the reverse direction, is)."""
    server = fresh_server()
    ls1 = _seed_lesson(server)
    cs1 = _seed_cornerstone(server, "CS1")
    r = json.loads(server.engram_add_edge(payload_json=json.dumps({
        "source_id": ls1, "target_id": cs1, "relation": "generalizes",
    })))
    assert "error" in r, r
    assert "does not accept" in r["error"], r


def test_mutation_check_endpoint_gate_actually_refuses():
    """Mutation-verify the endpoint gate: temporarily widen
    GENERALIZES_ALLOWED_PAIRS to include the offending pair and confirm the
    SAME call that test_generalizes_endpoint_refused_concrete_to_cornerstone
    expects to be refused is instead accepted -- proving that test exercises
    the real gate rather than being a false-green."""
    server = fresh_server()
    core = server.core
    inc_id = _seed_incident(server)
    cs1 = _seed_cornerstone(server, "CS1")

    core_mod = sys.modules["engram_core"]
    original = core_mod.GENERALIZES_ALLOWED_PAIRS
    try:
        core_mod.GENERALIZES_ALLOWED_PAIRS = original | {("observation_factual", "cornerstone")}
        r = json.loads(server.engram_add_edge(payload_json=json.dumps({
            "source_id": inc_id, "target_id": cs1, "relation": "generalizes",
        })))
        assert "error" not in r, (
            "mutation check did not reproduce expected bypass -- "
            f"gate may not be the actual blocker: {r}"
        )
        assert r["status"] == "created", r
    finally:
        core_mod.GENERALIZES_ALLOWED_PAIRS = original

    # Restore verified: same call refused again with the real gate.
    r2 = json.loads(server.engram_add_edge(payload_json=json.dumps({
        "source_id": inc_id, "target_id": _seed_cornerstone(server, "CS2"), "relation": "generalizes",
    })))
    assert "error" in r2, r2


# ── Cycle refusal (mutation-verified) ───────────────────────────────────

def test_generalizes_cycle_refused_with_path():
    """A generalizes B, B generalizes A -> second write REFUSED, error
    names the path."""
    server = fresh_server()
    cs_a = _seed_cornerstone(server, "A")
    cs_b = _seed_cornerstone(server, "B")
    r1 = json.loads(server.engram_add_edge(payload_json=json.dumps({
        "source_id": cs_a, "target_id": cs_b, "relation": "generalizes",
    })))
    assert r1["status"] == "created", r1

    r2 = json.loads(server.engram_add_edge(payload_json=json.dumps({
        "source_id": cs_b, "target_id": cs_a, "relation": "generalizes",
    })))
    assert "error" in r2, r2
    assert "cycle" in r2["error"].lower(), r2
    assert cs_a in r2["error"] and cs_b in r2["error"], r2


def test_generalizes_cycle_refused_longer_chain():
    """A cycle need not be a direct 2-node loop: A->B->C, then C->A closes
    a longer cycle and must still be refused."""
    server = fresh_server()
    cs_a = _seed_cornerstone(server, "A")
    cs_b = _seed_cornerstone(server, "B")
    cs_c = _seed_cornerstone(server, "C")
    for s, t in [(cs_a, cs_b), (cs_b, cs_c)]:
        r = json.loads(server.engram_add_edge(payload_json=json.dumps({
            "source_id": s, "target_id": t, "relation": "generalizes",
        })))
        assert r["status"] == "created", r

    r = json.loads(server.engram_add_edge(payload_json=json.dumps({
        "source_id": cs_c, "target_id": cs_a, "relation": "generalizes",
    })))
    assert "error" in r, r
    assert "cycle" in r["error"].lower(), r
    for node_id in (cs_a, cs_b, cs_c):
        assert node_id in r["error"], r


def test_mutation_check_cycle_guard_actually_refuses():
    """Mutation-verify the cycle guard (spec's step 6): temporarily
    neutralize core._generalizes_cycle_path (always report "no cycle") and
    confirm the SAME two-edge construction that
    test_generalizes_cycle_refused_with_path expects to be refused is
    instead created -- proving that test exercises the real guard, not a
    coincidental side-effect of something else.
    """
    server = fresh_server()
    cs_a = _seed_cornerstone(server, "A")
    cs_b = _seed_cornerstone(server, "B")
    server.engram_add_edge(payload_json=json.dumps({
        "source_id": cs_a, "target_id": cs_b, "relation": "generalizes",
    }))

    core_mod = sys.modules["engram_core"]
    original = core_mod._generalizes_cycle_path
    try:
        core_mod._generalizes_cycle_path = lambda conn, s, t: None
        r = json.loads(server.engram_add_edge(payload_json=json.dumps({
            "source_id": cs_b, "target_id": cs_a, "relation": "generalizes",
        })))
        assert "error" not in r, (
            "mutation check did not reproduce expected bypass -- "
            f"cycle guard may not be the actual blocker: {r}"
        )
        assert r["status"] == "created", r
    finally:
        core_mod._generalizes_cycle_path = original

    # Restore verified: a fresh cycle attempt is refused again.
    cs_c = _seed_cornerstone(server, "C")
    server.engram_add_edge(payload_json=json.dumps({
        "source_id": cs_a, "target_id": cs_c, "relation": "generalizes",
    }))
    r2 = json.loads(server.engram_add_edge(payload_json=json.dumps({
        "source_id": cs_c, "target_id": cs_a, "relation": "generalizes",
    })))
    assert "error" in r2, r2


# ---------------------------------------------------------------------------
# 3. generalization_chain()
# ---------------------------------------------------------------------------

def _new_conn():
    """Lightweight in-memory sqlite fixture -- same discipline as
    test_dag_conformance.py's _new_conn(), for pure-function tests of
    generalization_chain/_generalizes_cycle_path/_resolve_to_current that
    don't need the full server/schema."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        """CREATE TABLE nodes (
            id TEXT PRIMARY KEY,
            type TEXT NOT NULL,
            is_current INTEGER DEFAULT 1,
            superseded_by TEXT,
            claim TEXT,
            recall_summary TEXT,
            created_at TEXT
        )"""
    )
    conn.execute(
        """CREATE TABLE edges (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source_id TEXT,
            target_id TEXT,
            relation TEXT,
            created_at TEXT
        )"""
    )
    return conn


def _add_node(conn, node_id, node_type="cornerstone", is_current=1, superseded_by=None,
              claim=None, recall_summary=None):
    conn.execute(
        "INSERT INTO nodes (id, type, is_current, superseded_by, claim, recall_summary, created_at)"
        " VALUES (?, ?, ?, ?, ?, ?, 't')",
        (node_id, node_type, is_current, superseded_by, claim, recall_summary),
    )


def _add_edge(conn, source_id, target_id, relation):
    conn.execute(
        "INSERT INTO edges (source_id, target_id, relation, created_at) VALUES (?, ?, ?, 't')",
        (source_id, target_id, relation),
    )


def test_chain_superseded_rung_resolves_to_current():
    """Chain walks through a superseded node and returns the CURRENT
    version (the #1195 508-vs-162-vs-143 hazard the epic's design names)."""
    server = fresh_server()
    core = server.core
    conn = _new_conn()
    _add_node(conn, "cs_OLD", is_current=0, superseded_by="cs_NEW")
    _add_node(conn, "cs_NEW", is_current=1)
    _add_node(conn, "ls_X", node_type="lesson")
    _add_edge(conn, "cs_OLD", "ls_X", "generalizes")

    chain = core.generalization_chain(conn, "ls_X")
    assert "cs_NEW" in chain["heads"], chain
    assert "cs_OLD" not in chain["nodes"], chain
    assert chain["nodes"]["ls_X"]["parents"] == ["cs_NEW"], chain


def test_mutation_check_resolve_to_current_actually_resolves():
    """Mutation-verify the supersede-resolution step: temporarily neutralize
    core._resolve_to_current to identity (never follow the supersedes
    chain) and confirm the SAME fixture that
    test_chain_superseded_rung_resolves_to_current expects to resolve to
    cs_NEW instead surfaces the stale cs_OLD -- proving the original test
    exercises real resolution, not a coincidence of the fixture shape.
    """
    server = fresh_server()
    core_mod = sys.modules["engram_core"]
    conn = _new_conn()
    _add_node(conn, "cs_OLD", is_current=0, superseded_by="cs_NEW")
    _add_node(conn, "cs_NEW", is_current=1)
    _add_node(conn, "ls_X", node_type="lesson")
    _add_edge(conn, "cs_OLD", "ls_X", "generalizes")

    original = core_mod._resolve_to_current
    try:
        core_mod._resolve_to_current = lambda c, nid: nid  # identity: never resolves
        chain = core_mod.generalization_chain(conn, "ls_X")
        assert "cs_OLD" in chain["heads"], (
            "mutation check did not reproduce the stale-surface bug -- "
            f"resolution may not be the actual fixer: {chain}"
        )
        assert "cs_NEW" not in chain["nodes"], chain
    finally:
        core_mod._resolve_to_current = original

    # Restore verified: the real resolution surfaces cs_NEW again.
    chain2 = core_mod.generalization_chain(conn, "ls_X")
    assert "cs_NEW" in chain2["heads"], chain2


def test_chain_multiple_parents_returns_both_heads():
    """A lesson under two cornerstones returns both heads."""
    server = fresh_server()
    core = server.core
    conn = _new_conn()
    _add_node(conn, "cs_1")
    _add_node(conn, "cs_2")
    _add_node(conn, "ls_Y", node_type="lesson")
    _add_edge(conn, "cs_1", "ls_Y", "generalizes")
    _add_edge(conn, "cs_2", "ls_Y", "generalizes")

    chain = core.generalization_chain(conn, "ls_Y")
    assert set(chain["heads"]) == {"cs_1", "cs_2"}, chain
    assert sorted(chain["nodes"]["ls_Y"]["parents"]) == ["cs_1", "cs_2"], chain


def test_mutation_check_multiple_parents_actually_both_present():
    """Mutation-verify the multi-parent result by INPUT reduction (the
    parent lookup is an inline SQL query inside generalization_chain, not a
    separately monkeypatch-able helper -- this is the deliberate substitute
    named in this file's module docstring): drop the second parent edge and
    confirm the SAME walk now returns only ONE head -- proving
    test_chain_multiple_parents_returns_both_heads is actually sensitive to
    both edges being present, not hardcoded to report two heads regardless
    of the graph.
    """
    server = fresh_server()
    core = server.core
    conn = _new_conn()
    _add_node(conn, "cs_1")
    _add_node(conn, "cs_2")
    _add_node(conn, "ls_Y", node_type="lesson")
    _add_edge(conn, "cs_1", "ls_Y", "generalizes")
    # cs_2's edge deliberately omitted -- the "broken" (reduced) input.

    chain = core.generalization_chain(conn, "ls_Y")
    assert chain["heads"] == ["cs_1"], (
        "mutation check did not reproduce the single-parent shape -- "
        f"the original test's multi-parent claim may not be exercised: {chain}"
    )


def test_chain_depth_cap_truncates():
    """A chain longer than max_depth truncates rather than hanging."""
    server = fresh_server()
    core = server.core
    conn = _new_conn()
    for i in range(7):
        _add_node(conn, f"cs_{i}")
    for i in range(6):
        _add_edge(conn, f"cs_{i+1}", f"cs_{i}", "generalizes")

    chain = core.generalization_chain(conn, "cs_0", max_depth=4)
    assert chain["truncated"] is True, chain
    assert chain["heads"] == [], chain  # never reached the true head, cs_6
    assert len(chain["nodes"]) == 5, chain  # depths 0..4 inclusive


def test_chain_depth_cap_terminates_on_malformed_cycle():
    """Must terminate on a malformed cyclic graph even though the write
    guard should make one unreachable -- constructed via raw SQL insert,
    bypassing engram_add_edge's cycle guard entirely, to simulate data
    corruption. A long cycle (longer than max_depth) exercises the cap as
    the actual termination mechanism (a short cycle would terminate via the
    walk's own node-dedup before the cap ever binds -- see
    test_mutation_check_depth_cap_actually_bounds below for the control
    that isolates the cap specifically)."""
    server = fresh_server()
    core = server.core
    conn = _new_conn()
    ids = [f"cs_{i}" for i in range(8)]
    for nid in ids:
        _add_node(conn, nid)
    for i in range(8):
        _add_edge(conn, ids[i], ids[(i + 1) % 8], "generalizes")

    chain = core.generalization_chain(conn, "cs_0", max_depth=4)
    assert chain["truncated"] is True, chain
    assert len(chain["nodes"]) == 5, chain


def test_mutation_check_depth_cap_actually_bounds():
    """Mutation-verify the depth cap: the SAME 6-rung linear chain fixture
    that test_chain_depth_cap_truncates expects to truncate at max_depth=4
    does NOT truncate when max_depth is raised past the chain's real
    length -- proving `truncated` genuinely tracks whether the cap bound
    the walk, not a constant."""
    server = fresh_server()
    core = server.core
    conn = _new_conn()
    for i in range(7):
        _add_node(conn, f"cs_{i}")
    for i in range(6):
        _add_edge(conn, f"cs_{i+1}", f"cs_{i}", "generalizes")

    chain = core.generalization_chain(conn, "cs_0", max_depth=10)
    assert chain["truncated"] is False, (
        "mutation check did not reproduce the untruncated shape -- "
        f"max_depth may not be the actual bound: {chain}"
    )
    assert chain["heads"] == ["cs_6"], chain


def test_chain_concrete_node_starts_from_exemplifies_instantiates_targets():
    """For a concrete node (observation/derivation), the chain starts from
    the (resolved) targets of its own exemplifies/instantiates edges."""
    server = fresh_server()
    core = server.core
    conn = _new_conn()
    _add_node(conn, "ob_1", node_type="observation_factual")
    _add_node(conn, "ls_Z", node_type="lesson")
    _add_node(conn, "cs_Z")
    _add_edge(conn, "ob_1", "ls_Z", "exemplifies")
    _add_edge(conn, "cs_Z", "ls_Z", "generalizes")

    chain = core.generalization_chain(conn, "ob_1")
    assert chain["roots"] == ["ls_Z"], chain
    assert chain["heads"] == ["cs_Z"], chain


def test_generalization_chain_end_to_end_via_real_server():
    """End-to-end sanity through the real server/schema (not the
    lightweight fixture): axiom -> cornerstone -> lesson, chain from the
    lesson resolves all the way to the axiom head."""
    server = fresh_server()
    ax_id = _seed_axiom(server)
    cs1 = _seed_cornerstone(server, "CS1", generalized_by=ax_id)
    ls1 = _seed_lesson(server, generalized_by=cs1)

    conn = server.core._get_db()
    try:
        chain = server.core.generalization_chain(conn, ls1)
    finally:
        conn.close()
    assert chain["heads"] == [ax_id], chain
    assert chain["nodes"][cs1]["parents"] == [ax_id], chain


# ---------------------------------------------------------------------------
# 4. generalized_by on engram_add_lesson / engram_add_cornerstone
# ---------------------------------------------------------------------------

def test_add_cornerstone_generalized_by_axiom():
    server = fresh_server()
    ax_id = _seed_axiom(server)
    r = json.loads(server.engram_add_cornerstone(payload_json=json.dumps({
        "tag": "t", "title": "CS", "new_frame": "f", "generalized_by": ax_id,
    })))
    assert "error" not in r, r
    assert r["generalized_by"] == [ax_id], r


def test_add_cornerstone_generalized_by_goal():
    server = fresh_server()
    gl_id = _seed_goal(server)
    r = json.loads(server.engram_add_cornerstone(payload_json=json.dumps({
        "tag": "t", "title": "CS", "new_frame": "f", "generalized_by": gl_id,
    })))
    assert "error" not in r, r
    assert r["generalized_by"] == [gl_id], r


def test_add_cornerstone_generalized_by_missing_parent_refused():
    server = fresh_server()
    r = json.loads(server.engram_add_cornerstone(payload_json=json.dumps({
        "tag": "t", "title": "CS", "new_frame": "f", "generalized_by": "ax_9999",
    })))
    assert "error" in r, r
    assert "not found" in r["error"], r
    assert "cornerstone_id" not in r, r  # nothing created


def test_add_cornerstone_generalized_by_wrong_type_parent_refused():
    """A lesson cannot generalize a cornerstone (only
    cornerstone/axiom/goal -> cornerstone is allowed)."""
    server = fresh_server()
    ls1 = _seed_lesson(server)
    r = json.loads(server.engram_add_cornerstone(payload_json=json.dumps({
        "tag": "t", "title": "CS", "new_frame": "f", "generalized_by": ls1,
    })))
    assert "error" in r, r
    assert "does not accept" in r["error"], r
    assert "cornerstone_id" not in r, r


def test_add_lesson_generalized_by_cornerstone():
    server = fresh_server()
    cs1 = _seed_cornerstone(server, "CS1")
    inc_id = _seed_incident(server)
    r = json.loads(server.engram_add_lesson(payload_json=json.dumps({
        "claim": "claim", "incident_ids": inc_id, "scaffolding_nudge": "n",
        "logical_chain": "c", "generalized_by": cs1,
    })))
    assert "error" not in r, r
    assert r["generalized_by"] == [cs1], r


def test_add_lesson_generalized_by_another_lesson_meta_lesson():
    """lesson -> lesson (meta-lesson) is an allowed pair (D1 explicitly
    scopes meta-lessons in)."""
    server = fresh_server()
    parent_lesson = _seed_lesson(server, claim="Parent meta-lesson.")
    inc_id = _seed_incident(server)
    r = json.loads(server.engram_add_lesson(payload_json=json.dumps({
        "claim": "child claim", "incident_ids": inc_id, "scaffolding_nudge": "n",
        "logical_chain": "c", "generalized_by": parent_lesson,
    })))
    assert "error" not in r, r
    assert r["generalized_by"] == [parent_lesson], r


def test_add_lesson_generalized_by_non_current_parent_refused():
    server = fresh_server()
    cs1 = _seed_cornerstone(server, "CS1")
    out = json.loads(server.engram_outgrow_cornerstone(payload_json=json.dumps({
        "old_cornerstone_id": cs1, "new_new_frame": "f2",
    })))
    assert "error" not in out, out
    inc_id = _seed_incident(server)
    r = json.loads(server.engram_add_lesson(payload_json=json.dumps({
        "claim": "claim", "incident_ids": inc_id, "scaffolding_nudge": "n",
        "logical_chain": "c", "generalized_by": cs1,  # cs1 is now superseded
    })))
    assert "error" in r, r
    assert "not current" in r["error"], r
    assert "lesson_id" not in r, r


def test_add_lesson_generalized_by_wrong_type_parent_refused():
    """An observation (concrete node) cannot generalize a lesson."""
    server = fresh_server()
    inc_id = _seed_incident(server)
    r = json.loads(server.engram_add_lesson(payload_json=json.dumps({
        "claim": "claim", "incident_ids": inc_id, "scaffolding_nudge": "n",
        "logical_chain": "c", "generalized_by": inc_id,
    })))
    assert "error" in r, r
    assert "does not accept" in r["error"], r
    assert "lesson_id" not in r, r


def test_mutation_check_generalized_by_validation_actually_refuses():
    """Mutation-verify the strict-validation path on engram_add_lesson:
    temporarily widen GENERALIZES_ALLOWED_PAIRS and confirm the SAME
    payload that test_add_lesson_generalized_by_wrong_type_parent_refused
    expects to be refused is instead accepted."""
    server = fresh_server()
    inc_id = _seed_incident(server)
    core_mod = sys.modules["engram_core"]
    original = core_mod.GENERALIZES_ALLOWED_PAIRS
    try:
        core_mod.GENERALIZES_ALLOWED_PAIRS = original | {("observation_factual", "lesson")}
        r = json.loads(server.engram_add_lesson(payload_json=json.dumps({
            "claim": "claim", "incident_ids": inc_id, "scaffolding_nudge": "n",
            "logical_chain": "c", "generalized_by": inc_id,
        })))
        assert "error" not in r, (
            "mutation check did not reproduce expected bypass -- "
            f"validation may not be the actual blocker: {r}"
        )
        assert "lesson_id" in r, r
    finally:
        core_mod.GENERALIZES_ALLOWED_PAIRS = original

    # Restore verified: same shape refused again with the real gate.
    r2 = json.loads(server.engram_add_lesson(payload_json=json.dumps({
        "claim": "claim2", "incident_ids": inc_id, "scaffolding_nudge": "n",
        "logical_chain": "c", "generalized_by": inc_id,
    })))
    assert "error" in r2, r2
    assert "lesson_id" not in r2, r2


# ---------------------------------------------------------------------------
# 5. engram_outgrow_cornerstone carries generalizes edges
# ---------------------------------------------------------------------------

def test_outgrow_carries_generalizes_parents_and_children():
    server = fresh_server()
    ax_id = _seed_axiom(server)
    cs1 = _seed_cornerstone(server, "CS1", generalized_by=ax_id)
    ls1 = _seed_lesson(server, generalized_by=cs1)

    out = json.loads(server.engram_outgrow_cornerstone(payload_json=json.dumps({
        "old_cornerstone_id": cs1, "new_new_frame": "f2",
    })))
    assert "error" not in out, out
    cs2 = out["new_cornerstone_id"]
    assert out["preserved_generalizes_parents"] == [ax_id], out
    assert out["preserved_generalizes_children"] == [ls1], out

    conn = server.core._get_db()
    try:
        parent_edge = conn.execute(
            "SELECT 1 FROM edges WHERE source_id=? AND target_id=? AND relation='generalizes'",
            (ax_id, cs2),
        ).fetchone()
        child_edge = conn.execute(
            "SELECT 1 FROM edges WHERE source_id=? AND target_id=? AND relation='generalizes'",
            (cs2, ls1),
        ).fetchone()
        # Predecessor's own edges remain intact (add-a-copy, don't delete).
        old_parent_edge = conn.execute(
            "SELECT 1 FROM edges WHERE source_id=? AND target_id=? AND relation='generalizes'",
            (ax_id, cs1),
        ).fetchone()
        old_child_edge = conn.execute(
            "SELECT 1 FROM edges WHERE source_id=? AND target_id=? AND relation='generalizes'",
            (cs1, ls1),
        ).fetchone()
    finally:
        conn.close()
    assert parent_edge is not None, "predecessor's parent edge not carried to successor"
    assert child_edge is not None, "predecessor's child edge not carried to successor"
    assert old_parent_edge is not None, "predecessor's own edge was deleted, not preserved"
    assert old_child_edge is not None, "predecessor's own edge was deleted, not preserved"


def test_outgrow_chain_resolves_to_successor_not_predecessor():
    """generalization_chain from the carried-over lesson resolves through
    the outgrowth to the CURRENT cornerstone, never the stale predecessor."""
    server = fresh_server()
    ax_id = _seed_axiom(server)
    cs1 = _seed_cornerstone(server, "CS1", generalized_by=ax_id)
    ls1 = _seed_lesson(server, generalized_by=cs1)

    out = json.loads(server.engram_outgrow_cornerstone(payload_json=json.dumps({
        "old_cornerstone_id": cs1, "new_new_frame": "f2",
    })))
    cs2 = out["new_cornerstone_id"]

    conn = server.core._get_db()
    try:
        chain = server.core.generalization_chain(conn, ls1)
    finally:
        conn.close()
    assert cs1 not in chain["nodes"], chain
    assert cs2 in chain["nodes"], chain
    assert chain["heads"] == [ax_id], chain


def test_outgrow_no_generalizes_edges_is_a_clean_no_op():
    """A cornerstone with no generalizes edges at all outgrows normally,
    with empty carried-edge lists (not an error)."""
    server = fresh_server()
    cs1 = _seed_cornerstone(server, "CS1")
    out = json.loads(server.engram_outgrow_cornerstone(payload_json=json.dumps({
        "old_cornerstone_id": cs1, "new_new_frame": "f2",
    })))
    assert "error" not in out, out
    assert out["preserved_generalizes_parents"] == [], out
    assert out["preserved_generalizes_children"] == [], out


# ---------------------------------------------------------------------------
# Text hydration (#2753 WS1; contract settled on forum #407 after Clio
# measured a real per-fire N+1 on the PreToolUse path and Borges refuted the
# "close to free" reason offered for fixing it inside the walk).
# ---------------------------------------------------------------------------


def test_chain_hydrates_recall_summary_on_every_reached_node():
    """recall_summary is bounded and D3 renders it PER RUNG, so every reached
    node carries it -- middle rungs included."""
    server = fresh_server()
    core = server.core
    conn = _new_conn()
    _add_node(conn, "cs_top", claim="TOP CLAIM", recall_summary="top summary")
    _add_node(conn, "cs_mid", claim="MID CLAIM", recall_summary="mid summary")
    _add_node(conn, "ls_leaf", node_type="lesson", claim="LEAF CLAIM",
              recall_summary="leaf summary")
    _add_edge(conn, "cs_top", "cs_mid", "generalizes")
    _add_edge(conn, "cs_mid", "ls_leaf", "generalizes")

    chain = core.generalization_chain(conn, "ls_leaf")
    for nid, expected in (("ls_leaf", "leaf summary"),
                          ("cs_mid", "mid summary"),
                          ("cs_top", "top summary")):
        assert chain["nodes"][nid]["recall_summary"] == expected, chain


def test_chain_claim_on_heads_only_never_on_middle_rungs():
    """claim is UNBOUNDED and D3 wants it for the head alone. A middle rung
    carrying it would put unbounded text on the per-rung path -- the exact
    thing the batched contract exists to prevent."""
    server = fresh_server()
    core = server.core
    conn = _new_conn()
    _add_node(conn, "cs_top", claim="TOP CLAIM", recall_summary="top summary")
    _add_node(conn, "cs_mid", claim="MID CLAIM", recall_summary="mid summary")
    _add_node(conn, "ls_leaf", node_type="lesson", claim="LEAF CLAIM",
              recall_summary="leaf summary")
    _add_edge(conn, "cs_top", "cs_mid", "generalizes")
    _add_edge(conn, "cs_mid", "ls_leaf", "generalizes")

    chain = core.generalization_chain(conn, "ls_leaf")
    assert chain["heads"] == ["cs_top"], chain
    assert chain["nodes"]["cs_top"]["claim"] == "TOP CLAIM", chain
    # the two non-heads must NOT carry claim at all
    assert "claim" not in chain["nodes"]["cs_mid"], chain
    assert "claim" not in chain["nodes"]["ls_leaf"], chain


def test_chain_hydration_is_two_queries_regardless_of_chain_size():
    """THE load-bearing test: the whole point of the contract is that
    hydration costs a FIXED number of queries, not one per rung. A regression
    to per-node fetching would still return correct text and would still pass
    the two tests above -- only a query COUNT can see it.

    Counts SELECTs against `nodes` carrying a text column, via a tracing
    connection wrapper, on chains of length 2 and 4. The count must not grow.
    """
    server = fresh_server()
    core = server.core

    def _count_text_selects(chain_len):
        conn = _new_conn()
        ids = [f"cs_{i}" for i in range(chain_len)]
        for i, nid in enumerate(ids):
            _add_node(conn, nid, claim=f"claim {i}", recall_summary=f"summary {i}")
        # cs_0 generalizes cs_1 generalizes ... (cs_0 is the head)
        for i in range(chain_len - 1):
            _add_edge(conn, ids[i], ids[i + 1], "generalizes")

        # sqlite3.Connection.execute is read-only, so trace via a delegating
        # wrapper rather than by patching the attribute.
        class _Tracing:
            def __init__(self, real):
                self._real = real
                self.text_selects = []

            def execute(self, sql, *a, **kw):
                if "recall_summary" in sql or "id, claim" in sql:
                    self.text_selects.append(sql)
                return self._real.execute(sql, *a, **kw)

            def __getattr__(self, name):
                return getattr(self._real, name)

        traced_conn = _Tracing(conn)
        chain = core.generalization_chain(traced_conn, ids[-1], max_depth=10)
        seen = traced_conn.text_selects
        # sanity: the walk really did reach every node, so the count below is
        # a count over a real chain rather than over a walk that bailed early
        assert len(chain["nodes"]) == chain_len, chain
        return len(seen)

    short = _count_text_selects(2)
    long = _count_text_selects(4)
    assert short == long, (
        f"hydration query count grew with chain size ({short} -> {long}); "
        "that is an N+1 regression -- the contract is a FIXED number of "
        "batched queries independent of chain size"
    )
    assert long <= 2, f"expected at most 2 text queries, got {long}"


# ── #2812 review blocker — the cycle guard must see the graph the WALK sees ──
#
# The guard originally BFS'd over edges filtered `is_current = 1` on both
# endpoints, while generalization_chain resolves endpoints through
# supersedes. That asymmetry let an edge superseded via the GENERIC
# engram_supersede (the only supersede path lesson/axiom/goal have at all --
# engram_outgrow_cornerstone is cornerstone-only and is the sole edge-carrying
# path) go invisible to the guard while the walk still traversed it. The guard
# would then admit an edge the walk rendered as a live cycle, reported as
# `heads: []` + `truncated: False` -- indistinguishable from a clean result.


def test_cycle_guard_sees_through_a_generically_superseded_endpoint():
    """The #2812 blocker, at fixture level.

    cs_A generalizes ls_B. cs_A is then superseded to cs_A2 the way the
    GENERIC tool leaves it: is_current=0 + superseded_by, edge NOT carried.
    Adding ls_B generalizes cs_A2 would close a cycle in the RESOLVED graph
    (cs_A2 -> ls_B -> cs_A2), so the guard must refuse it.

    Under the pre-fix current-only filter the guard saw no outgoing edge
    from cs_A2 at all and returned None -- the edge was created.
    """
    server = fresh_server()
    core = server.core
    conn = _new_conn()
    _add_node(conn, "cs_A", is_current=0, superseded_by="cs_A2")
    _add_node(conn, "cs_A2", is_current=1)
    _add_node(conn, "ls_B", node_type="lesson")
    _add_edge(conn, "cs_A", "ls_B", "generalizes")  # stale, uncarried

    path = core._generalizes_cycle_path(conn, "ls_B", "cs_A2")
    assert path is not None, (
        "guard did not see the cycle through the superseded endpoint -- "
        "this is the #2812 blocker"
    )
    assert "ls_B" in path and "cs_A2" in path, path


def test_mutation_check_cycle_guard_resolution_is_what_finds_it():
    """Neutralize resolution to identity and confirm the guard goes BLIND
    to the same fixture -- proving the resolve step is what closes the hole
    rather than some incidental property of the fixture.
    """
    core_mod = sys.modules["engram_core"]
    conn = _new_conn()
    _add_node(conn, "cs_A", is_current=0, superseded_by="cs_A2")
    _add_node(conn, "cs_A2", is_current=1)
    _add_node(conn, "ls_B", node_type="lesson")
    _add_edge(conn, "cs_A", "ls_B", "generalizes")

    original = core_mod._resolve_to_current
    try:
        core_mod._resolve_to_current = lambda c, nid: nid  # never resolves
        blind = core_mod._generalizes_cycle_path(conn, "ls_B", "cs_A2")
        assert blind is None, (
            "mutation did not reproduce the blindness -- resolution may not "
            f"be the mechanism actually closing this hole: {blind}"
        )
    finally:
        core_mod._resolve_to_current = original

    assert core_mod._generalizes_cycle_path(conn, "ls_B", "cs_A2") is not None


def test_chain_reports_cycle_detected_rather_than_a_benign_empty_heads():
    """A cycle in the resolved graph must be reported, not rendered as a
    well-formed walk with no heads.

    WS2 consumes this return shape; `heads: []` with `truncated: False` is
    structurally impossible in a valid DAG, so it needs its own signal
    rather than looking exactly like a clean result.
    """
    server = fresh_server()
    core = server.core
    conn = _new_conn()
    _add_node(conn, "cs_A", is_current=0, superseded_by="cs_A2")
    _add_node(conn, "cs_A2", is_current=1)
    _add_node(conn, "ls_B", node_type="lesson")
    _add_edge(conn, "cs_A", "ls_B", "generalizes")   # resolves to cs_A2 -> ls_B
    _add_edge(conn, "ls_B", "cs_A2", "generalizes")  # ls_B -> cs_A2 : cycle

    chain = core.generalization_chain(conn, "ls_B")
    assert chain["heads"] == [], chain
    assert chain["truncated"] is False, chain
    assert chain["cycle_detected"] is True, (
        "a resolved-graph cycle rendered as a benign complete walk", chain
    )


def test_cycle_detected_is_false_on_a_healthy_chain():
    """Control for the test above: the same field must stay False on a
    well-formed chain, or `cycle_detected` is just always-true noise."""
    server = fresh_server()
    core = server.core
    conn = _new_conn()
    _add_node(conn, "ax_TOP", node_type="axiom")
    _add_node(conn, "cs_MID")
    _add_node(conn, "ls_LEAF", node_type="lesson")
    _add_edge(conn, "ax_TOP", "cs_MID", "generalizes")
    _add_edge(conn, "cs_MID", "ls_LEAF", "generalizes")

    chain = core.generalization_chain(conn, "ls_LEAF")
    assert chain["heads"] == ["ax_TOP"], chain
    assert chain["cycle_detected"] is False, chain
    assert chain["truncated"] is False, chain


def test_cycle_detected_is_false_when_merely_truncated():
    """Discriminates cycle_detected from truncation: a depth-capped walk has
    no heads either, and must NOT be reported as a cycle."""
    server = fresh_server()
    core = server.core
    conn = _new_conn()
    for i in range(6):
        _add_node(conn, f"cs_{i}")
    for i in range(5):
        _add_edge(conn, f"cs_{i+1}", f"cs_{i}", "generalizes")

    chain = core.generalization_chain(conn, "cs_0", max_depth=2)
    assert chain["truncated"] is True, chain
    assert chain["cycle_detected"] is False, (
        "truncation misreported as a cycle", chain
    )


# ── #2812 round 2 — cycle_detected false positive on a dead-ended parent ──


def test_dead_ended_parent_makes_a_head_not_a_phantom_cycle():
    """A node whose only generalizes parent was retracted with NO replacement
    has no LIVE parent, so it is a head -- and must not be reported as a
    cycle.

    Round 2's finding: the walk dropped such a parent (resolve -> None) and
    fell out of BOTH branches -- the node was never appended to heads because
    it had parent-edge ROWS -- leaving heads empty on an acyclic graph, which
    the new cycle_detected inference then read as a cycle.
    """
    server = fresh_server()
    core = server.core
    conn = _new_conn()
    # cs_GONE is retracted with no successor: resolve() returns None
    _add_node(conn, "cs_GONE", is_current=0, superseded_by=None)
    _add_node(conn, "ls_LIVE", node_type="lesson")
    _add_edge(conn, "cs_GONE", "ls_LIVE", "generalizes")

    chain = core.generalization_chain(conn, "ls_LIVE")
    assert chain["cycle_detected"] is False, (
        "acyclic graph reported as a cycle -- the inference is firing on a "
        "dead-ended parent", chain
    )
    assert chain["heads"] == ["ls_LIVE"], (
        "a node with no LIVE parent is a head", chain
    )
    assert "cs_GONE" not in chain["nodes"], chain


def test_partially_dead_ended_parents_still_connect_the_live_one():
    """Control: one dead parent + one live parent must NOT promote to head --
    otherwise the fix would over-promote and hide real chain structure."""
    server = fresh_server()
    core = server.core
    conn = _new_conn()
    _add_node(conn, "cs_GONE", is_current=0, superseded_by=None)
    _add_node(conn, "cs_ALIVE")
    _add_node(conn, "ls_LEAF", node_type="lesson")
    _add_edge(conn, "cs_GONE", "ls_LEAF", "generalizes")
    _add_edge(conn, "cs_ALIVE", "ls_LEAF", "generalizes")

    chain = core.generalization_chain(conn, "ls_LEAF")
    assert chain["heads"] == ["cs_ALIVE"], chain
    assert "ls_LEAF" not in chain["heads"], (
        "over-promoted: a node with one live parent is not a head", chain
    )
    assert chain["nodes"]["ls_LEAF"]["parents"] == ["cs_ALIVE"], chain
    assert chain["cycle_detected"] is False, chain


# ── #2812 colleague review (Luria) — cycle_detected must not be masked by a
#    genuine head on a DIFFERENT branch of the same multi-parent walk ──


def test_cycle_detected_when_a_genuine_head_coexists_on_another_branch():
    """Luria's repro, exactly.

    X has TWO live parents: one branch resolves to a genuine head, the other
    loops. The old global inference (`not heads and not truncated`) read the
    non-empty `heads` and stayed silent while a live 2-cycle sat in the
    returned `nodes` -- a false NEGATIVE, the direction WS2 depends on.

    Multi-parent principle nodes are this feature's headline case, not an
    adversarial shape: "a lesson may sit under two cornerstones".
    """
    server = fresh_server()
    core = server.core
    conn = _new_conn()
    for nid in ("cs_A", "cs_B", "cs_C", "cs_X"):
        _add_node(conn, nid)
    _add_edge(conn, "cs_A", "cs_X", "generalizes")   # branch 1 -> genuine head
    _add_edge(conn, "cs_B", "cs_X", "generalizes")   # branch 2 -> into the cycle
    _add_edge(conn, "cs_C", "cs_B", "generalizes")   # B <-> C, guard bypassed
    _add_edge(conn, "cs_B", "cs_C", "generalizes")   #   (direct fixture write)

    chain = core.generalization_chain(conn, "cs_X")

    assert chain["heads"] == ["cs_A"], chain
    assert chain["truncated"] is False, chain
    assert chain["nodes"]["cs_B"]["parents"] == ["cs_C"], chain
    assert chain["nodes"]["cs_C"]["parents"] == ["cs_B"], chain
    assert chain["cycle_detected"] is True, (
        "a live 2-cycle sat in `nodes` while a head on another branch "
        "silenced the signal", chain
    )


def test_cycle_detected_inside_a_truncated_walk():
    """New capability the old form could not have: a cycle co-occurring with
    truncation. `not truncated` made that unreportable by construction."""
    server = fresh_server()
    core = server.core
    conn = _new_conn()
    for nid in ("cs_P", "cs_Q", "cs_START"):
        _add_node(conn, nid)
    for i in range(6):
        _add_node(conn, f"cs_L{i}")
    _add_edge(conn, "cs_P", "cs_START", "generalizes")
    _add_edge(conn, "cs_Q", "cs_P", "generalizes")   # P <-> Q cycle, reached at depth 1-2
    _add_edge(conn, "cs_P", "cs_Q", "generalizes")
    _add_edge(conn, "cs_L0", "cs_START", "generalizes")   # long branch -> truncates
    for i in range(5):
        _add_edge(conn, f"cs_L{i+1}", f"cs_L{i}", "generalizes")

    chain = core.generalization_chain(conn, "cs_START", max_depth=3)
    assert chain["truncated"] is True, chain
    assert chain["cycle_detected"] is True, (
        "cycle inside a truncated walk went unreported", chain
    )


def test_cycle_detected_false_on_a_healthy_multi_parent_walk():
    """Control for the two above: the same multi-parent shape WITHOUT a cycle
    must stay False, or the new check is just always-true on branching."""
    server = fresh_server()
    core = server.core
    conn = _new_conn()
    for nid in ("cs_A2", "cs_B2", "cs_X2"):
        _add_node(conn, nid)
    _add_node(conn, "ax_TOP2", node_type="axiom")
    _add_edge(conn, "cs_A2", "cs_X2", "generalizes")
    _add_edge(conn, "cs_B2", "cs_X2", "generalizes")
    _add_edge(conn, "ax_TOP2", "cs_B2", "generalizes")

    chain = core.generalization_chain(conn, "cs_X2")
    assert chain["cycle_detected"] is False, chain
    assert sorted(chain["heads"]) == ["ax_TOP2", "cs_A2"], chain
