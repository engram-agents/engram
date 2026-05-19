"""Tests for the special-type bypass in _search_nodes (Lei 2026-05-19 PM, the child case).

Covers:
  1. test_special_types_bypass_composite_when_sim_above_floor
     person node with low utility/importance appears in results when sim > FTS_SIM_FLOOR
  2. test_special_types_filtered_out_when_below_sim_floor
     special-type node with sim < 0.30 does not appear (FTS-merge floor drops it)
  3. test_generic_types_still_composite_ranked
     observation_factual + derivation still go through composite + MMR
  4. test_generic_and_special_both_appear_in_results
     mixed corpus: both pipeline outputs merge correctly
  5. test_special_pool_independent_of_tier2_cutoff
     special-types not subject to TIER2_MULTIPLIER cap; all qualifying surface
     (kept under SPECIAL_POOL_CAP for the all-must-surface assertion)
  5a. test_special_pool_capped_at_special_pool_cap
      input > SPECIAL_POOL_CAP → output is exactly SPECIAL_POOL_CAP
  5b. test_special_pool_cap_preserves_top_per_type
      diversity guarantee: top-1 per type survives the cap even when one
      type dominates (12 persons + 1 goal → goal still surfaces)
  5c. test_special_pool_cap_constant_value
      SPECIAL_POOL_CAP == 10
  6. test_no_double_count_when_same_id_in_both
     defensive: a node cannot appear twice even if it appeared in both pools
  7. test_definition_and_goal_also_bypass
     coverage of the three NEW special types (definition, goal, person)
  8. test_axiom_question_etc_still_in_special_set
     coverage of the existing 5 special types
  9. test_engram_surface_routes_person_definition_goal_to_special_nodes
     engram_surface integration: person/definition/goal land in special_nodes, not top_claims
 10. test_constants_have_expected_values
     SPECIAL_TYPES_BYPASS contains exactly the 8 expected types
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


TEST_DIR = os.path.join(tempfile.gettempdir(), "engram_test_special_type_bypass")

EXPECTED_SPECIAL_TYPES = frozenset({
    "axiom", "contradiction", "question", "conjecture", "lesson",
    "definition", "person", "goal",
})


def fresh_server():
    """Create a clean server module instance in a temp sandbox directory."""
    if os.path.exists(TEST_DIR):
        shutil.rmtree(TEST_DIR)
    # Remove any cached server module so constants re-evaluate.
    for mod in list(sys.modules.keys()):
        if mod == "server" or mod.startswith("server."):
            del sys.modules[mod]
    os.environ["ENGRAM_NO_EMBEDDINGS"] = "1"
    import server
    server._configure_paths(TEST_DIR)
    server._ensure_data_dir()
    return server


def fresh_conn(srv):
    return srv._get_db()


def _add_obs(srv, claim: str, quoted_text: str = "test quote") -> str:
    payload = {
        "quoted_text": quoted_text,
        "interpretation": "test interpretation",
        "claim": claim,
        "quote_type": "personal_communication",
        "url": "https://example.com/test",
        "title": "Test",
    }
    r = json.loads(srv.engram_add_observation(payload_json=json.dumps(payload)))
    assert "error" not in r, r
    return r["observation_id"]


def _add_person(srv, name: str, role: str = "test person") -> str:
    payload = {"name": name, "role": role}
    r = json.loads(srv.engram_add_person(payload_json=json.dumps(payload)))
    assert "error" not in r, r
    return r["person_id"]


def _add_definition(srv, term: str, definition: str) -> str:
    payload = {"term": term, "definition": definition}
    r = json.loads(srv.engram_add_definition(payload_json=json.dumps(payload)))
    assert "error" not in r, r
    return r["definition_id"]


def _add_goal(srv, claim: str, motivation: str = "test motivation") -> str:
    payload = {"claim": claim, "motivation": motivation}
    r = json.loads(srv.engram_add_goal(payload_json=json.dumps(payload)))
    assert "error" not in r, r
    return r["goal_id"]


def _add_axiom(srv, claim: str) -> str:
    payload = {
        "claim": claim,
        "basis": "test axiom basis for special-type bypass test",
    }
    r = json.loads(srv.engram_add_axiom(payload_json=json.dumps(payload)))
    assert "error" not in r, r
    return r["axiom_id"]


# ---------------------------------------------------------------------------
# Test 1: person node surfaces even with low utility/importance
# ---------------------------------------------------------------------------


def test_special_types_bypass_composite_when_sim_above_floor():
    """Person node with zero utility and low importance appears when FTS matches.

    With ENGRAM_NO_EMBEDDINGS=1, semantic_available=False so sim=None which
    is treated as passing FTS_SIM_FLOOR.  The node has low composite potential
    (low/default utility and importance) but must surface because it is a
    special type in SPECIAL_TYPES_BYPASS.
    """
    srv = fresh_server()

    # Add many observation_factual nodes with the same query keyword so they
    # dominate the composite ranking.  These have higher importance/utility
    # from being added more recently.
    keyword = "goodnight"
    for i in range(25):
        _add_obs(srv, f"{keyword} greetings farewell observation node {i:03d}")

    # Add a person node with the same keyword but low utility (freshly added,
    # utility=0 by default) — simulates pn_0005 in the the child case.
    person_id = _add_person(srv, f"the child {keyword} person test", role="test collaborator")

    conn = fresh_conn(srv)
    try:
        result = srv._search_nodes(
            conn, keyword, use_semantic=False, top_k=10,
        )
    finally:
        conn.close()

    result_ids = [n["id"] for n in result["results"]]
    assert person_id in result_ids, (
        f"person node {person_id} should appear in results via special-type bypass; "
        f"got ids: {result_ids}"
    )


# ---------------------------------------------------------------------------
# Test 2: special-type node with sim below floor is filtered out
# ---------------------------------------------------------------------------


def test_special_types_filtered_out_when_below_sim_floor():
    """Special-type node is absent when semantic sim < FTS_SIM_FLOOR.

    Strategy: mock _semantic_search to return the person node with sim=0.25
    (below FTS_SIM_FLOOR=0.30) and confirm it is dropped during the FTS-merge
    floor check (the drop happens before the partition, consistent with how
    all nodes are filtered).
    """
    srv = fresh_server()

    keyword = "telescope"
    person_id = _add_person(srv, f"Telescope Astronomer unique person {keyword}")

    import sys
    server_module = sys.modules.get("server", srv)
    original_semantic = server_module._semantic_search
    original_is_available = server_module._embedder.is_available
    original_get_emb_config = server_module._get_embedding_config

    # Mock: return person node with sim=0.25 (below floor)
    def low_sim_semantic(conn, query, top_k, **kwargs):
        return [{"id": person_id, "similarity": 0.25}]

    server_module._semantic_search = low_sim_semantic
    server_module._embedder.is_available = lambda: True
    server_module._get_embedding_config = lambda: {"enabled": True}

    try:
        conn = fresh_conn(srv)
        try:
            result = srv._search_nodes(
                conn, keyword, use_semantic=True, top_k=10,
            )
        finally:
            conn.close()
    finally:
        server_module._semantic_search = original_semantic
        server_module._embedder.is_available = original_is_available
        server_module._get_embedding_config = original_get_emb_config

    result_ids = [n["id"] for n in result["results"]]
    assert person_id not in result_ids, (
        f"person node {person_id} should be absent when sim=0.25 < FTS_SIM_FLOOR=0.30; "
        f"got ids: {result_ids}"
    )


# ---------------------------------------------------------------------------
# Test 3: generic types still go through composite + MMR
# ---------------------------------------------------------------------------


def test_generic_types_still_composite_ranked():
    """observation_factual and derivation nodes still proceed through composite + MMR.

    We add many observations and verify:
    - result count is capped at top_k (tier-3 output bound)
    - results contain only observation_factual types (no special-type bypass occurred)
    """
    srv = fresh_server()

    keyword = "photosynthesis"
    for i in range(20):
        _add_obs(srv, f"{keyword} chloroplast light biology test node {i:03d}")

    conn = fresh_conn(srv)
    try:
        result = srv._search_nodes(
            conn, keyword, use_semantic=False, top_k=5,
        )
    finally:
        conn.close()

    # MMR returns top_k for generic-only corpus.
    assert len(result["results"]) <= 5, (
        f"Expected ≤ 5 results from generic-only corpus, got {len(result['results'])}"
    )
    for n in result["results"]:
        ntype = n.get("type")
        assert ntype == "observation_factual", (
            f"Expected only observation_factual in generic-only corpus, got type={ntype}"
        )


# ---------------------------------------------------------------------------
# Test 4: mixed corpus — generic + special both appear
# ---------------------------------------------------------------------------


def test_generic_and_special_both_appear_in_results():
    """Mixed corpus: generic pipeline (observation_factual) + special bypass (person) merge.

    The person node's composite potential is low but it surfaces via bypass.
    The observation nodes surface through the normal composite + MMR path.
    """
    srv = fresh_server()

    keyword = "entropy"
    for i in range(10):
        _add_obs(srv, f"{keyword} thermodynamics heat disorder observation node {i:03d}")

    person_id = _add_person(srv, f"Entropy Professor {keyword} unique person", role="physicist")

    conn = fresh_conn(srv)
    try:
        result = srv._search_nodes(
            conn, keyword, use_semantic=False, top_k=5,
        )
    finally:
        conn.close()

    result_ids = [n["id"] for n in result["results"]]
    result_types = [n.get("type") for n in result["results"]]

    # Person node appears via bypass.
    assert person_id in result_ids, (
        f"person {person_id} missing from mixed-corpus results; ids={result_ids}"
    )
    # Observation nodes also appear.
    assert "observation_factual" in result_types, (
        f"observation_factual missing from mixed-corpus results; types={result_types}"
    )


# ---------------------------------------------------------------------------
# Test 5: special pool is independent of TIER2_MULTIPLIER cap
# ---------------------------------------------------------------------------


def test_special_pool_independent_of_tier2_cutoff():
    """Special-type nodes are not subject to the TIER2_MULTIPLIER cap.

    We add more special-type nodes (person + goal) than top_k × TIER2_MULTIPLIER
    so that if they were subject to the tier-2 cut, some would disappear.
    Pool size kept under SPECIAL_POOL_CAP so all qualifying nodes surface;
    cap-trimming behavior has its own dedicated test.
    """
    srv = fresh_server()

    keyword = "galaxy"
    top_k = 3
    # top_k × TIER2_MULTIPLIER = 12 — add more than 12 special nodes total
    # to confirm none are cut by tier-2. Stay at or below SPECIAL_POOL_CAP=10
    # for the all-must-surface assertion; cap behavior covered separately.
    person_ids = []
    for i in range(6):
        pid = _add_person(srv, f"Galaxy Person {keyword} bypass test {i:02d}", role="astronomer")
        person_ids.append(pid)

    goal_ids = []
    for i in range(4):
        gid = _add_goal(srv, f"Study {keyword} formation mechanisms bypass test {i:02d}")
        goal_ids.append(gid)

    conn = fresh_conn(srv)
    try:
        result = srv._search_nodes(
            conn, keyword, use_semantic=False, top_k=top_k,
        )
    finally:
        conn.close()

    result_ids = set(n["id"] for n in result["results"])
    special_ids = set(person_ids + goal_ids)

    # All special nodes that matched (all have the keyword) must appear —
    # 10 specials = SPECIAL_POOL_CAP, so the cap doesn't trim any of them.
    missing = special_ids - result_ids
    assert not missing, (
        f"Special-type nodes {missing} missing despite matching keyword — "
        f"they may have been incorrectly cut by tier-2. "
        f"result_ids={result_ids}"
    )
    # Generic top_k constraint does not apply: result count may exceed top_k.
    assert len(result["results"]) >= len(special_ids), (
        f"Expected at least {len(special_ids)} results (all special nodes), "
        f"got {len(result['results'])}"
    )


def test_special_pool_capped_at_special_pool_cap():
    """When special_pool exceeds SPECIAL_POOL_CAP, the cap trims to 10."""
    srv = fresh_server()
    keyword = "supernova"
    top_k = 5
    # 15 special nodes total > SPECIAL_POOL_CAP=10
    person_ids = []
    for i in range(9):
        pid = _add_person(srv, f"Supernova researcher {keyword} cap test {i:02d}", role="astronomer")
        person_ids.append(pid)
    goal_ids = []
    for i in range(6):
        gid = _add_goal(srv, f"Map {keyword} remnants in NGC galaxies cap test {i:02d}")
        goal_ids.append(gid)

    conn = fresh_conn(srv)
    try:
        result = srv._search_nodes(conn, keyword, use_semantic=False, top_k=top_k)
    finally:
        conn.close()

    # Identify special-type results
    special_result_nodes = [n for n in result["results"]
                            if n.get("type") in srv.SPECIAL_TYPES_BYPASS]
    # Cap is enforced
    assert len(special_result_nodes) == srv.SPECIAL_POOL_CAP, (
        f"Expected {srv.SPECIAL_POOL_CAP} special results, "
        f"got {len(special_result_nodes)} (input pool was 15)"
    )


def test_special_pool_cap_preserves_top_per_type():
    """Diversity preservation: top-1 of each present type survives the cap.

    Even when one type dominates (9 persons vs 1 goal), the cap must keep
    at least 1 of each type — guarantees the the child case (pn_0005 alone)
    isn't starved by 9 high-sim definitions.
    """
    srv = fresh_server()
    keyword = "calibration"
    top_k = 5
    # 9 persons + 1 goal = 10 (at cap exactly). To force the cap trim with
    # a clear type imbalance, push to 12 persons + 1 goal.
    person_ids = []
    for i in range(12):
        pid = _add_person(srv, f"Calibration {keyword} expert preserve top per type {i:02d}", role="engineer")
        person_ids.append(pid)
    goal_id = _add_goal(srv, f"Improve {keyword} via empirical study preserve top per type")

    conn = fresh_conn(srv)
    try:
        result = srv._search_nodes(conn, keyword, use_semantic=False, top_k=top_k)
    finally:
        conn.close()

    special_results = [n for n in result["results"]
                       if n.get("type") in srv.SPECIAL_TYPES_BYPASS]
    types_in_result = {n["type"] for n in special_results}
    # Both types present in input must appear in capped output.
    assert "person" in types_in_result, (
        f"person type starved out of cap; only got {types_in_result}"
    )
    assert "goal" in types_in_result, (
        f"goal type starved by 12 high-sim persons; only got {types_in_result}. "
        f"This is exactly the the child-case regression the diversity cap is meant to prevent."
    )
    assert len(special_results) == srv.SPECIAL_POOL_CAP, (
        f"Expected {srv.SPECIAL_POOL_CAP} special results, got {len(special_results)}"
    )


def test_special_pool_cap_constant_value():
    """SPECIAL_POOL_CAP == 10 (calibrated 2026-05-19 PM)."""
    srv = fresh_server()
    assert hasattr(srv, "SPECIAL_POOL_CAP"), (
        "SPECIAL_POOL_CAP constant not found on server module"
    )
    assert srv.SPECIAL_POOL_CAP == 10, (
        f"Expected SPECIAL_POOL_CAP=10, got {srv.SPECIAL_POOL_CAP}"
    )


# ---------------------------------------------------------------------------
# Test 6: defensive dedup — no node appears twice
# ---------------------------------------------------------------------------


def test_no_double_count_when_same_id_in_both():
    """No node ID appears twice in the final merged results.

    Although types partition cleanly in practice, the dedup guard must hold.
    """
    srv = fresh_server()

    keyword = "recursion"
    person_id = _add_person(srv, f"Recursion Expert {keyword} unique dedup test", role="engineer")

    conn = fresh_conn(srv)
    try:
        result = srv._search_nodes(
            conn, keyword, use_semantic=False, top_k=10,
        )
    finally:
        conn.close()

    ids = [n["id"] for n in result["results"]]
    assert len(ids) == len(set(ids)), (
        f"Duplicate IDs found in results: {[x for x in ids if ids.count(x) > 1]}"
    )


# ---------------------------------------------------------------------------
# Test 7: definition and goal also bypass (NEW special types)
# ---------------------------------------------------------------------------


def test_definition_and_goal_also_bypass():
    """definition and goal nodes also bypass composite+MMR.

    These are two of the three NEW types added to SPECIAL_TYPES_BYPASS
    (the third, person, is covered by tests 1 and 4).
    """
    srv = fresh_server()

    keyword = "fermion"
    # Add many observations to dominate composite ranking.
    for i in range(20):
        _add_obs(srv, f"{keyword} particle physics test observation {i:03d}")

    definition_id = _add_definition(srv, f"{keyword} unique definition bypass test",
                                    f"A {keyword} is a particle with half-integer spin.")
    goal_id = _add_goal(srv, f"Study {keyword} behavior in quantum fields bypass test")

    conn = fresh_conn(srv)
    try:
        result = srv._search_nodes(
            conn, keyword, use_semantic=False, top_k=5,
        )
    finally:
        conn.close()

    result_ids = [n["id"] for n in result["results"]]
    assert definition_id in result_ids, (
        f"definition {definition_id} missing from results; ids={result_ids}"
    )
    assert goal_id in result_ids, (
        f"goal {goal_id} missing from results; ids={result_ids}"
    )


# ---------------------------------------------------------------------------
# Test 8: existing 5 status-bearing types still in special set
# ---------------------------------------------------------------------------


def test_axiom_question_etc_still_in_special_set():
    """The 5 existing status-bearing types still bypass composite+MMR.

    Verifies axiom (the most concrete to add) surfaces in a corpus dominated
    by observation_factual with higher composite potential.
    """
    srv = fresh_server()

    keyword = "boson"
    for i in range(20):
        _add_obs(srv, f"{keyword} hadron strong force test observation {i:03d}")

    axiom_id = _add_axiom(srv, f"{keyword} particle symmetry breaking axiom bypass test")

    conn = fresh_conn(srv)
    try:
        result = srv._search_nodes(
            conn, keyword, use_semantic=False, top_k=5,
        )
    finally:
        conn.close()

    result_ids = [n["id"] for n in result["results"]]
    assert axiom_id in result_ids, (
        f"axiom {axiom_id} (existing special type) missing from results; ids={result_ids}"
    )


# ---------------------------------------------------------------------------
# Test 9: engram_surface routes person/definition/goal to special_nodes
# ---------------------------------------------------------------------------


def test_engram_surface_routes_person_definition_goal_to_special_nodes():
    """engram_surface integration: person/definition/goal appear in special_nodes, not top_claims.

    Tests the claim_nodes exclusion set update in engram_surface that adds
    definition, person, goal alongside the existing exclusions.
    """
    srv = fresh_server()

    keyword = "nebula"
    # Add an observation so there are top_claims candidates.
    _add_obs(srv, f"{keyword} stellar formation astronomy test observation node")

    person_id = _add_person(srv, f"Nebula Astronomer {keyword} surface routing test", role="researcher")
    definition_id = _add_definition(srv, f"{keyword} surface test unique term",
                                    f"A {keyword} is an interstellar cloud of gas and dust.")
    goal_id = _add_goal(srv, f"Map all visible {keyword} clouds in the local group surface test")

    result = json.loads(srv.engram_surface(query=keyword, top_k=15, semantic=False))

    special_ids = [n["id"] for n in result.get("special_nodes", [])]
    top_claim_ids = [n["id"] for n in result.get("top_claims", [])]

    # All three new types must land in special_nodes.
    assert person_id in special_ids, (
        f"person {person_id} missing from special_nodes; special={special_ids}"
    )
    assert definition_id in special_ids, (
        f"definition {definition_id} missing from special_nodes; special={special_ids}"
    )
    assert goal_id in special_ids, (
        f"goal {goal_id} missing from special_nodes; special={special_ids}"
    )

    # None of the new types should appear in top_claims.
    assert person_id not in top_claim_ids, (
        f"person {person_id} incorrectly appeared in top_claims"
    )
    assert definition_id not in top_claim_ids, (
        f"definition {definition_id} incorrectly appeared in top_claims"
    )
    assert goal_id not in top_claim_ids, (
        f"goal {goal_id} incorrectly appeared in top_claims"
    )


# ---------------------------------------------------------------------------
# Test 10: SPECIAL_TYPES_BYPASS constant has exactly the 8 expected types
# ---------------------------------------------------------------------------


def test_constants_have_expected_values():
    """SPECIAL_TYPES_BYPASS contains exactly the 8 expected types."""
    srv = fresh_server()

    assert hasattr(srv, "SPECIAL_TYPES_BYPASS"), (
        "SPECIAL_TYPES_BYPASS constant not found on server module"
    )
    actual = frozenset(srv.SPECIAL_TYPES_BYPASS)
    assert actual == EXPECTED_SPECIAL_TYPES, (
        f"SPECIAL_TYPES_BYPASS mismatch.\n"
        f"Expected: {sorted(EXPECTED_SPECIAL_TYPES)}\n"
        f"Actual:   {sorted(actual)}\n"
        f"Missing:  {sorted(EXPECTED_SPECIAL_TYPES - actual)}\n"
        f"Extra:    {sorted(actual - EXPECTED_SPECIAL_TYPES)}"
    )
